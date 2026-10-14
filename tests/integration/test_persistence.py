"""Owner-controlled continued effort, using fixture models and isolated PostgreSQL."""

import asyncio
import json
import os
from decimal import Decimal

import pytest

from harness.core.coordination import Coordination
from harness.core.runtime import Runtime
from harness.core.types import ModelResponse, Usage
from harness.mattermost.client import Client

pytestmark = pytest.mark.skipif(
    os.environ.get("HARNESS_LIVE_TESTS") != "1", reason="live PostgreSQL gate"
)


class CompletingProvider:
    def __init__(self, empty=False):
        self.empty = empty
        self.requests = []
        self.continued = asyncio.Event()

    async def generate(self, messages, *args):
        self.requests.append(messages)
        if len(self.requests) > 1:
            self.continued.set()
            await asyncio.Event().wait()
        return ModelResponse(
            message={
                "output": []
                if self.empty
                else [
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [
                            {
                                "type": "output_text",
                                "text": "Review complete; no proof emerged; the problem is open.",
                            }
                        ],
                    }
                ]
            },
            usage=Usage(input_tokens=10, output_tokens=10, cost_usd=Decimal("0.0001")),
        )


@pytest.mark.parametrize("empty", [False, True])
async def test_tool_free_completion_continues_without_messages_and_owner_pause_wins(
    runtime_store, empty
):
    store, settings, agent, workspace = runtime_store
    await store.pool.execute("UPDATE workspaces SET started=true WHERE id=$1", workspace)
    provider = CompletingProvider(empty)
    async with Client("https://unused.test", "token") as bot:
        runtime = Runtime(store, bot, agent, settings, provider)
        try:
            await runtime.wake(agent)
            await asyncio.wait_for(provider.continued.wait(), 4)
            assert await store.unread_count(agent) == 0
            assert len(provider.requests) == 2
            continuation = json.loads(provider.requests[1][-1]["content"])
            assert continuation["source"].startswith("Harness continuation policy")
            assert "Only the human stops" in continuation["instruction"]
            state = await store.state(agent)
            await store.slash_control(workspace, state["owner_id"], "general", "/pause", "pause")
            await runtime.pause()
            assert (await store.state(agent))["state"] == "READY"
            await runtime.wake(agent)
            await asyncio.sleep(1.1)
            assert len(provider.requests) == 2
        finally:
            runtime.stopping.set()
            await runtime.pause()

        # A new runtime resumes preserved effort after owner /continue, without a post.
        await store.slash_control(workspace, state["owner_id"], "general", "/continue", "resume")
        resumed_provider = CompletingProvider()
        resumed = Runtime(store, bot, agent, settings, resumed_provider)
        try:
            await store.recover(agent)
            await resumed.wake(agent)
            await asyncio.wait_for(resumed_provider.continued.wait(), 4)
            assert "Review complete" in str(await store.history(agent)) or empty
            assert len(resumed_provider.requests) == 2
        finally:
            resumed.stopping.set()
            await resumed.pause()


async def test_fresh_seed_waits_and_agents_cannot_retire_seed(runtime_store):
    store, settings, seed, _ = runtime_store
    provider = CompletingProvider()
    async with Client("https://unused.test", "token") as bot:
        runtime = Runtime(store, bot, seed, settings, provider)
        await runtime.wake(seed)
        assert runtime.task is None and not provider.requests
    with pytest.raises(ValueError, match="SEED_RETIREMENT_REQUIRES_OWNER_RESET"):
        await Coordination(store, settings).terminate(seed, seed, "Review complete")
    assert (await store.state(seed))["state"] != "TERMINATED"


async def test_away_buffers_messages_but_does_not_suspend_started_effort(runtime_store):
    store, settings, agent, workspace = runtime_store
    await store.pool.execute("UPDATE workspaces SET started=true WHERE id=$1", workspace)
    await store.set_availability(agent, "away")
    provider = CompletingProvider()
    async with Client("https://unused.test", "token") as bot:
        runtime = Runtime(store, bot, agent, settings, provider)
        try:
            await runtime.wake(agent)
            await asyncio.wait_for(provider.continued.wait(), 4)
            assert len(provider.requests) == 2
            assert (await store.state(agent))["availability"] == "away"
        finally:
            runtime.stopping.set()
            await runtime.pause()
