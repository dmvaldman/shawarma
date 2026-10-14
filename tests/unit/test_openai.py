import json
from decimal import Decimal

import httpx
import pytest

from harness.config import Settings
from harness.models.openai import OpenAIProvider, ProviderError
from harness.tools.core import TOOLS


def settings():
    return Settings(
        _env_file=None,
        mattermost_url="https://mm.test",
        database_url="unused",
        OPENAI_API_KEY="private-unit-token",
    )


async def test_responses_api_function_calls_usage_and_no_provider_state():
    def handle(request):
        assert request.headers["Authorization"] == "Bearer private-unit-token"
        body = json.loads(request.content)
        assert body["model"] == "gpt-6-luna"
        assert body["store"] is False
        assert body["reasoning"] == {"effort": "none"}
        assert body["service_tier"] == "default"
        return httpx.Response(
            200,
            json={
                "id": "r1",
                "status": "completed",
                "output": [
                    {
                        "type": "function_call",
                        "call_id": "c1",
                        "name": "get_status",
                        "arguments": "{}",
                    }
                ],
                "usage": {"input_tokens": 100, "output_tokens": 20},
            },
        )

    provider = OpenAIProvider(settings(), httpx.MockTransport(handle))
    try:
        result = await provider.generate(
            [{"role": "user", "content": "hello"}], TOOLS, "gpt-6-luna", 100, 2
        )
        assert result.message["output"][0]["call_id"] == "c1"
        assert result.usage.cost_usd == Decimal("0.000040")
    finally:
        await provider.close()


async def test_unavailable_model_is_never_substituted_and_key_never_logged():
    provider = OpenAIProvider(
        settings(),
        httpx.MockTransport(
            lambda r: httpx.Response(
                404, json={"error": {"code": "model_not_found", "message": "private-unit-token"}}
            )
        ),
    )
    try:
        with pytest.raises(ProviderError, match="no substitute") as error:
            await provider.verify_model()
        assert "private-unit-token" not in str(error.value)
        with pytest.raises(ProviderError, match="allowlist"):
            await provider.generate([], [], "other-model", 10, 1)
    finally:
        await provider.close()


async def test_cost_estimate_has_no_artificial_byte_ceiling():
    provider = OpenAIProvider(settings())
    try:
        assert provider.bound([], TOOLS, 4096) > 0
        assert provider.bound([{"role": "user", "content": "x" * 1_000_000}], TOOLS, 4096) > 0
    finally:
        await provider.close()


async def test_native_compaction_output_is_replayed_without_old_history():
    from harness.models.context import active_context

    checkpoint = {"type": "compaction", "id": "cmp", "encrypted_content": "opaque-checkpoint"}
    requests = []

    def handle(request):
        body = json.loads(request.content)
        requests.append(body)
        assert body["context_management"] == [{"type": "compaction", "compact_threshold": 200000}]
        assert "reasoning.encrypted_content" in body["include"]
        return httpx.Response(
            200,
            json={
                "id": "r",
                "status": "completed",
                "output": [checkpoint] if len(requests) == 1 else [],
                "usage": {"input_tokens": 10, "output_tokens": 10},
            },
        )

    provider = OpenAIProvider(settings(), httpx.MockTransport(handle))
    original = [{"role": "user", "content": "old task"}]
    try:
        response = await provider.generate(original, [], "gpt-6-luna", 100, 2)
        context = active_context("fixed", original + response.message["output"], [])
        await provider.generate(context, [], "gpt-6-luna", 100, 2)
        assert requests[1]["input"] == [{"role": "system", "content": "fixed"}, checkpoint]
    finally:
        await provider.close()


async def test_cache_routing_key_is_stable_across_turns_and_separates_agents():
    requests = []
    raw_usage = {
        "input_tokens": 5000,
        "output_tokens": 10,
        "input_tokens_details": {"cached_tokens": 4096, "cache_write_tokens": 512},
    }

    def handle(request):
        requests.append(json.loads(request.content))
        return httpx.Response(
            200, json={"id": "r", "status": "completed", "output": [], "usage": raw_usage}
        )

    provider = OpenAIProvider(settings(), httpx.MockTransport(handle))
    try:
        for identity, turn in [
            ("agent-one", "first"),
            ("agent-one", "next"),
            ("agent-two", "first"),
        ]:
            response = await provider.generate(
                [{"role": "system", "content": identity}, {"role": "user", "content": turn}],
                [],
                "gpt-6-luna",
                100,
                2,
            )
            assert response.usage.provider_usage == raw_usage
        assert requests[0]["prompt_cache_key"] == requests[1]["prompt_cache_key"]
        assert requests[0]["prompt_cache_key"] != requests[2]["prompt_cache_key"]
        assert all(request["store"] is False for request in requests)
    finally:
        await provider.close()


def test_provider_error_reports_safe_code_without_raw_message():
    from harness.models.openai import response_error

    error = response_error(
        httpx.Response(
            429,
            json={
                "error": {
                    "type": "rate_limit_error",
                    "message": "Your credit balance is low; secret payload",
                }
            },
        ),
        "anthropic",
    )
    assert error.status == 429
    assert error.code == "billing_or_usage_limit"
    assert "secret payload" not in str(error)


def test_prompt_policy_rejection_is_explicit_without_exposing_prompt():
    from harness.models.openai import response_error

    error = response_error(
        httpx.Response(
            400,
            json={
                "error": {
                    "code": "invalid_prompt",
                    "message": "Your prompt was flagged as potentially violating our usage policy. "
                    "secret payload",
                }
            },
        ),
        "OpenAI",
    )
    assert error.status == 400
    assert error.code == "invalid_prompt"
    assert error.policy_rejected
    assert "provider flagged the prompt under its usage policy" in str(error)
    assert "secret payload" not in str(error)
