import asyncio
import json
from decimal import Decimal

import httpx
import pytest

from harness.config import Settings
from harness.core.names import agent_name, role_name
from harness.core.types import ToolSpec
from harness.models.catalog import ModelCatalog
from harness.models.router import ProviderRouter, anthropic_messages


def settings():
    return Settings(
        _env_file=None,
        mattermost_url="https://mm.test",
        database_url="unused",
        OPENAI_API_KEY="private-oa",
        ANTHROPIC_API_KEY="private-cl",
        DEEPSEEK_API_KEY="private-ds",
    )


async def test_catalog_combines_providers_paginates_and_coalesces_cached_calls():
    calls = []

    async def handle(request):
        calls.append(str(request.url))
        await asyncio.sleep(0)
        if request.url.host == "api.anthropic.com":
            assert request.headers["x-api-key"] == "private-cl"
            if "after_id" not in request.url.params:
                return httpx.Response(
                    200,
                    json={
                        "data": [{"id": "claude-haiku-5-5"}],
                        "has_more": True,
                        "last_id": "claude-haiku-5-5",
                    },
                )
            return httpx.Response(
                200, json={"data": [{"id": "claude-opus-5-5"}], "has_more": False}
            )
        mid = "gpt-6-luna" if request.url.host == "api.openai.com" else "deepseek-flash"
        return httpx.Response(200, json={"data": [{"id": mid}]})

    catalog = ModelCatalog(settings(), httpx.MockTransport(handle))
    results = await asyncio.gather(*(catalog.list() for _ in range(8)))
    assert len(calls) == 4
    assert len(results[0]["models"]) == 4
    assert sum(not r["cached"] for r in results) == 1
    assert "private-" not in json.dumps(results)
    assert (await catalog.resolve("deepseek:deepseek-flash"))["provider"] == "deepseek"
    assert (await catalog.resolve("deepseek/deepseek-flash"))["id"] == "deepseek:deepseek-flash"
    assert (await catalog.resolve("openai/gpt-6-luna"))["id"] == "openai:gpt-6-luna"
    with pytest.raises(ValueError, match="MODEL_NOT_ALLOWED"):
        await catalog.resolve("openai/imaginary")
    with pytest.raises(ValueError, match="MODEL_NOT_ALLOWED"):
        await catalog.resolve("imaginary")
    catalog.expires = 0
    await catalog.list()
    assert len(calls) == 8


async def test_provider_failure_is_reported_without_hiding_other_models_or_secrets():
    def handle(request):
        if request.url.host == "api.anthropic.com":
            return httpx.Response(401, json={"error": "private-cl"})
        return httpx.Response(200, json={"data": [{"id": "gpt-6-luna"}]})

    result = await ModelCatalog(settings(), httpx.MockTransport(handle)).list()
    assert result["providers"]["anthropic"]["status"] == "unavailable"
    assert len(result["models"]) == 2
    assert "private-" not in json.dumps(result)


def test_names_only_add_a_number_when_needed():
    role = role_name("Compare Newton iteration", "newt")
    username, display = agent_name(role)
    assert username == display == "newt"
    assert agent_name(role, 2) == ("newt2", "newt2")
    assert "newt" in display and "haiku" not in display
    assert len(username) <= 64


def test_anthropic_translation_keeps_parallel_tool_results_together():
    system, messages = anthropic_messages(
        [
            {"role": "system", "content": "fixed"},
            {"role": "user", "content": "task"},
            {"type": "function_call", "call_id": "a", "name": "foo", "arguments": "{}"},
            {"type": "function_call", "call_id": "b", "name": "foo", "arguments": "{}"},
            {"type": "function_call_output", "call_id": "a", "output": "ok"},
            {"type": "function_call_output", "call_id": "b", "output": "ok"},
            {"role": "user", "content": "new notification"},
        ]
    )
    assert system == "fixed"
    assert len(messages) == 3
    assert [b["type"] for b in messages[1]["content"]] == ["tool_use", "tool_use"]
    assert [b["type"] for b in messages[2]["content"]] == ["tool_result", "tool_result", "text"]


@pytest.mark.parametrize("provider", ["anthropic", "deepseek"])
async def test_router_tool_calls_normalize_usage_and_continue_same_explicit_model(provider):
    mid = "claude-haiku-5-5" if provider == "anthropic" else "deepseek-flash"
    catalog = ModelCatalog(settings())
    metadata = {
        "id": provider + ":" + mid,
        "provider": provider,
        "api_id": mid,
        "input": "1",
        "output": "2",
    }

    async def resolve(value):
        assert value == metadata["id"]
        return metadata

    catalog.resolve = resolve
    requests = []

    def handle(request):
        body = json.loads(request.content)
        requests.append(body)
        assert body["model"] == mid
        if provider == "anthropic":
            assert body["cache_control"] == {"type": "ephemeral"}
            return httpx.Response(
                200,
                json={
                    "id": "r",
                    "content": [
                        {"type": "tool_use", "id": "call", "name": "get_status", "input": {}}
                    ],
                    "usage": {
                        "input_tokens": 100,
                        "output_tokens": 20,
                        "cache_read_input_tokens": 0,
                        "cache_creation_input_tokens": 0,
                    },
                },
            )
        assert body["thinking"] == {"type": "disabled"}
        assert "cache_control" not in body  # DeepSeek enables caching itself.
        return httpx.Response(
            200,
            json={
                "id": "r",
                "choices": [
                    {
                        "message": {
                            "tool_calls": [
                                {
                                    "id": "call",
                                    "function": {"name": "get_status", "arguments": "{}"},
                                }
                            ]
                        }
                    }
                ],
                "usage": {
                    "prompt_tokens": 100,
                    "completion_tokens": 20,
                    "prompt_cache_hit_tokens": 80,
                    "prompt_cache_miss_tokens": 20,
                },
            },
        )

    router = ProviderRouter(settings(), catalog, httpx.MockTransport(handle))
    tool = ToolSpec(name="get_status", description="Get state", input_schema={"type": "object"})
    initial = [{"role": "system", "content": "fixed"}, {"role": "user", "content": "task"}]
    try:
        response = await router.generate(initial, [tool], metadata["id"], 100, 2)
        assert response.usage.cost_usd == Decimal("0.00014")
        if provider == "deepseek":
            assert response.usage.provider_usage["prompt_cache_hit_tokens"] == 80
        else:
            assert "cache_creation_input_tokens" in response.usage.provider_usage
        await router.generate(
            initial
            + response.message["output"]
            + [{"type": "function_call_output", "call_id": "call", "output": "{}"}],
            [tool],
            metadata["id"],
            100,
            2,
        )
        assert len(requests) == 2
    finally:
        await router.close()


async def test_anthropic_cached_usage_keeps_all_token_categories():
    raw = {
        "input_tokens": 100,
        "output_tokens": 20,
        "cache_creation_input_tokens": 200,
        "cache_read_input_tokens": 800,
        "cache_creation": {"ephemeral_5m_input_tokens": 200},
    }

    def handle(request):
        assert json.loads(request.content)["cache_control"] == {"type": "ephemeral"}
        return httpx.Response(200, json={"id": "r", "content": [], "usage": raw})

    router = await compaction_router("anthropic", "claude-haiku-5-5", handle)
    try:
        result = await router.generate(
            [{"role": "user", "content": "task"}], [], "anthropic:claude-haiku-5-5", 100, 2
        )
        assert result.usage.input_tokens == 1100
        assert result.usage.provider_usage == raw
    finally:
        await router.close()


async def compaction_router(provider, mid, handle):
    catalog = ModelCatalog(settings())

    async def resolve(value):
        return {
            "id": provider + ":" + mid,
            "provider": provider,
            "api_id": mid,
            "input": "1",
            "output": "2",
        }

    catalog.resolve = resolve
    return ProviderRouter(settings(), catalog, httpx.MockTransport(handle))


async def test_claude_enables_compaction_and_replays_native_summary_with_tool_pairs():
    from harness.models.context import active_context

    requests = []
    block = {"type": "compaction", "content": "Continue checking SAT circuits"}

    def handle(request):
        body = json.loads(request.content)
        requests.append(body)
        assert request.headers["anthropic-beta"] == "compact-2026-01-12"
        assert body["context_management"] == {"edits": [{"type": "compact_20260112"}]}
        return httpx.Response(
            200,
            json={
                "id": "r",
                "content": [
                    block,
                    {"type": "tool_use", "id": "call", "name": "get_status", "input": {}},
                ],
                "usage": {"input_tokens": 10, "output_tokens": 10},
            },
        )

    router = await compaction_router("anthropic", "claude-haiku-5-5", handle)
    history = [{"role": "user", "content": "old task"}]
    try:
        response = await router.generate(history, [], "anthropic:claude-haiku-5-5", 100, 2)
        history += response.message["output"] + [
            {"type": "function_call_output", "call_id": "call", "output": "ok"}
        ]
        await router.generate(
            active_context("fixed", history, []), [], "anthropic:claude-haiku-5-5", 100, 2
        )
        assert requests[1]["messages"][0]["content"][0] == block
        assert requests[1]["messages"][0]["content"][1]["type"] == "tool_use"
        assert requests[1]["messages"][1]["content"][0]["type"] == "tool_result"
        assert "old task" not in json.dumps(requests[1])
    finally:
        await router.close()


async def test_deepseek_overflow_prunes_retries_and_returns_durable_reset():
    from harness.models.context import active_context

    requests = []

    def handle(request):
        body = json.loads(request.content)
        requests.append(body)
        if len(requests) == 1:
            return httpx.Response(400, json={"error": {"code": "context_length_exceeded"}})
        return httpx.Response(
            200,
            json={
                "id": "r",
                "choices": [{"message": {"content": "answer"}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 10},
            },
        )

    router = await compaction_router("deepseek", "deepseek-flash", handle)
    history = [{"role": "system", "content": "fixed"}]
    for i in range(10):
        history += [
            {"role": "user", "content": f"message-{i}"},
            {"role": "assistant", "content": "response"},
        ]
    try:
        response = await router.generate(history, [], "deepseek:deepseek-flash", 100, 2)
        assert len(requests) == 2
        assert len(requests[1]["messages"]) < len(requests[0]["messages"])
        assert requests[1]["messages"][0] == history[0]
        assert requests[1]["messages"][-2:] == history[-2:]
        checkpoint = response.message["output"][0]
        assert checkpoint["type"] == "context_reset"
        replay = active_context("fixed", history[1:] + response.message["output"], [])
        await router.generate(replay, [], "deepseek:deepseek-flash", 100, 2)
        assert "message-0" not in json.dumps(requests[2])
    finally:
        await router.close()


async def test_deepseek_does_not_retry_other_bad_requests_or_irreducible_overflow():
    from harness.models.openai import ProviderError

    for code in ("invalid_tool_schema", "context_length_exceeded"):
        calls = []

        def handle(request, calls=calls, code=code):
            calls.append(request)
            return httpx.Response(400, json={"error": {"code": code}})

        router = await compaction_router("deepseek", "deepseek-flash", handle)
        try:
            with pytest.raises(ProviderError):
                await router.generate(
                    [{"role": "user", "content": "current task"}],
                    [],
                    "deepseek:deepseek-flash",
                    100,
                    2,
                )
            assert len(calls) == 1
        finally:
            await router.close()


@pytest.mark.parametrize(
    "provider,model", [("anthropic", "claude-haiku-5-5"), ("deepseek", "deepseek-flash")]
)
async def test_seed_verification_uses_only_configured_provider(provider, model):
    s = Settings(
        _env_file=None,
        mattermost_url="https://mm.test",
        database_url="unused",
        OPENAI_API_KEY="",
        ANTHROPIC_API_KEY="anthropic-key" if provider == "anthropic" else "",
        DEEPSEEK_API_KEY="deepseek-key" if provider == "deepseek" else "",
        seed_model=provider + ":" + model,
    )
    calls = []

    def api(request):
        calls.append(request)
        assert "openai" not in request.url.host
        return httpx.Response(200, json={"data": [{"id": model}]})

    transport = httpx.MockTransport(api)
    router = ProviderRouter(s, ModelCatalog(s, transport), transport)
    try:
        await router.verify_model()
        assert router.profiles[s.seed_model_id]["provider"] == provider
        assert len(calls) == 1
    finally:
        await router.close()
