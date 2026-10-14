"""Explicitly opted-in, metered provider tool-call round trips."""

import os
from uuid import UUID

import pytest

from harness.cli.main import STATE
from harness.config import Settings
from harness.core.types import ToolSpec
from harness.models.router import ProviderRouter
from harness.persistence.bootstrap import load
from harness.persistence.store import Store

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("HARNESS_PROVIDER_LIVE_TESTS") != "1", reason="paid multi-provider gate"
    ),
]


@pytest.mark.parametrize(
    "model", ["openai:gpt-6-luna", "anthropic:claude-haiku-5-5", "deepseek:deepseek-flash"]
)
async def test_live_explicit_model_tool_call_and_result_continuation(model):
    settings = Settings()
    agent = UUID(load(STATE)["seed_id"])
    store = await Store.connect(settings)
    router = ProviderRouter(settings)
    tool = ToolSpec(
        name="get_status",
        description="Read the test agent's status.",
        input_schema={
            "type": "object",
            "properties": {},
            "required": [],
            "additionalProperties": False,
        },
    )
    context = [
        {"role": "system", "content": "You are a tool-call integration test."},
        {
            "role": "user",
            "content": "Call get_status now. After seeing its result, "
            "reply briefly with the returned state.",
        },
    ]
    try:
        await router.prepare(model)
        for index in range(2):
            turn = await store.reserve_cost(
                agent,
                router.bound(context, [tool], 512, model),
                context,
            )
            try:
                result = await router.generate(context, [tool], model, 512, 30)
            except BaseException:
                await store.finish(turn, None)
                raise
            await store.finish(turn, result.usage)
            if index == 0:
                calls = [
                    item for item in result.message["output"] if item.get("type") == "function_call"
                ]
                assert calls and all(c["name"] == "get_status" for c in calls)
                context += result.message["output"] + [
                    {
                        "type": "function_call_output",
                        "call_id": c["call_id"],
                        "output": '{"state":"IDLE"}',
                    }
                    for c in calls
                ]
            else:
                assert result.message["output"]
    finally:
        await router.close()
        await store.close()
