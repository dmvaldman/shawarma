import json
from decimal import Decimal

import httpx
import pytest

from harness.coding.clients import Agent37, CodingError, OpenCode, check
from harness.coding.service import summarize
from harness.coding.setup import worker_config
from harness.config import Settings
from harness.tools.core import CODING_TOOL


def settings(**kwargs):
    return Settings(
        _env_file=None,
        mattermost_url="http://localhost:8065",
        database_url="postgresql://unused",
        **kwargs,
    )


def test_coding_usage_includes_every_internal_step_and_keeps_raw_counters():
    messages = [
        {"info": {"id": "old", "role": "assistant", "cost": 30}},
        {"info": {"id": "new", "role": "user"}},
        {
            "info": {
                "id": "first",
                "role": "assistant",
                "cost": 0.01,
                "tokens": {"input": 20, "output": 5, "reasoning": 3, "cache": {"read": 10}},
            },
            "parts": [{"type": "text", "text": "Ran tests"}],
        },
        {
            "info": {
                "id": "second",
                "role": "assistant",
                "cost": 0.02,
                "tokens": {"input": 7, "output": 9},
            },
            "parts": [{"type": "text", "text": "Committed"}],
        },
    ]
    report, usage = summarize(messages, {"old"})
    assert report == "Ran tests\n\nCommitted"
    assert usage.cost_usd == Decimal(".03")
    assert usage.input_tokens == 37
    assert usage.output_tokens == 17
    assert len(usage.provider_usage["messages"]) == 2


def test_errors_do_not_expose_provider_body_or_command():
    response = httpx.Response(401, json={"message": "secret-key private code"})
    with pytest.raises(CodingError) as caught:
        check(response, "OpenCode")
    assert "401" in str(caught.value) and "secret-key" not in str(caught.value)


def test_all_internal_steps_are_counted_without_counting_latest_twice():
    _, usage = summarize(
        [
            {
                "info": {
                    "id": "m",
                    "role": "assistant",
                    "cost": 0.1,
                    "tokens": {"input": 99999, "output": 99999},
                },
                "parts": [
                    {
                        "type": "step-finish",
                        "tokens": {"input": 10, "output": 3, "cache": {"read": 20, "write": 5}},
                    },
                    {"type": "step-finish", "tokens": {"input": 15, "output": 4, "reasoning": 2}},
                ],
            }
        ],
        set(),
    )
    assert usage.input_tokens == 50
    assert usage.output_tokens == 9


def test_worker_disables_hidden_delegation_and_external_directory_access():
    config = worker_config(settings())
    assert config["agent"]["swarm"]["options"]["reasoningSummary"] == "auto"
    assert config["permission"]["task"] == "deny"
    assert config["permission"]["external_directory"] == "deny"
    assert "agent37" not in config["enabled_providers"]
    assert "apiKey" in config["provider"]["openai"]["options"]
    assert "secret" not in json.dumps(config)
    assert CODING_TOOL.name == "code_task"


def test_agent37_requires_credentials_before_any_network_request():
    with pytest.raises(CodingError, match="AGENT37_API_KEY"):
        Agent37(settings())


async def test_opencode_directory_and_auth_are_scoped_to_worker():
    client = OpenCode(settings(opencode_password="worker-secret"))
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(200, json={"id": "session"})

    await client.http.aclose()
    client.http = httpx.AsyncClient(
        base_url="http://worker",
        auth=("opencode", "worker-secret"),
        transport=httpx.MockTransport(handle),
    )
    try:
        await client.request("POST", "/session", "/workspaces/one/agent", json={"title": "Agent"})
    finally:
        await client.close()
    assert requests[0].url.params["directory"] == "/workspaces/one/agent"
    assert "authorization" in requests[0].headers
    assert "X-Agent37-Key" not in requests[0].headers


def test_native_pdf_fetch_disabled_and_error_classification_is_sanitized():
    from harness.coding.service import native_error

    assert worker_config(settings())["permission"]["webfetch"] == "deny"
    error = {
        "data": {
            "statusCode": 400,
            "responseHeaders": {"cookie": "secret"},
            "responseBody": json.dumps({"error": {"code": "invalid_file", "message": "secret"}}),
        }
    }
    assert "invalid_file" in native_error(error)
    assert "secret" not in native_error(error)
    assert "HTTP 429" in native_error({"data": {"statusCode": 429}})
