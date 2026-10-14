import asyncio
import os
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest
from test_runtime import BlockingProvider

from harness.core.runtime import Runtime
from harness.core.supervisor import Supervisor
from harness.core.types import Post
from harness.notifications.bridge import Bridge
from harness.persistence.store import QuotaError

pytestmark = pytest.mark.skipif(
    os.environ.get("HARNESS_LIVE_TESTS") != "1", reason="live PostgreSQL gate"
)


def command(owner, name="/stop", id="stop", stamp=1000):
    return Post(id=id, user_id=owner, channel_id="general", message=name, create_at=stamp)


async def control(store, agent, post):
    state = await store.state(agent)
    return await store.slash_control(
        state["workspace_id"], post.user_id, post.channel_id, post.message, post.id
    )


async def test_controls_are_owner_only_durable_and_do_not_terminate(runtime_store):
    store, settings, agent, workspace = runtime_store
    owner = (await store.state(agent))["owner_id"]
    with pytest.raises(ValueError, match="OWNER_ONLY"):
        await control(store, agent, command("peer"))
    with pytest.raises(ValueError, match="UNKNOWN_CONTROL"):
        await control(store, agent, command(owner, "I might send /stop"))
    await asyncio.gather(*(control(store, agent, command(owner)) for _ in range(4)))
    assert (await store.state(agent))["workspace_state"] == "paused"
    assert (await store.state(agent))["state"] != "TERMINATED"
    await store.recover(agent)
    assert (await store.state(agent))["workspace_state"] == "paused"
    assert (
        await store.pool.fetchval("SELECT count(*) FROM events WHERE type='owner_slash_control'")
        == 1
    )
    with pytest.raises(QuotaError):
        await store.reserve_cost(agent, Decimal("0.001"), [])
    await control(store, agent, command(owner, "/continue", "continue"))
    await control(store, agent, command(owner))  # Duplicate delivery cannot undo newer control.
    assert (await store.state(agent))["workspace_state"] == "active"
    await store.stop(workspace)
    with pytest.raises(ValueError, match="PERMANENTLY_STOPPED"):
        await control(store, agent, command(owner, "/continue", "later"))
    assert (await store.state(agent))["workspace_state"] == "stopped"


async def test_plain_chat_text_is_no_longer_a_control(runtime_store):
    store, settings, agent, workspace = runtime_store
    owner = (await store.state(agent))["owner_id"]
    bot = AsyncMock()
    bot.request.return_value = {"team_id": "team", "type": "O"}
    bridge = Bridge(store, bot, agent, settings)
    for index, text in enumerate(["@stop", "@resume", "/stop"]):
        post = command(owner, text, str(index))
        await bridge.ingest({"event": "posted", "data": {"post": post.model_dump_json()}})
        assert (await store.state(agent))["workspace_state"] == "active"


async def test_pause_cancels_inflight_inference_and_resume_keeps_ingestion_alive(
    runtime_store, monkeypatch
):
    store, settings, agent, workspace = runtime_store
    await store.pool.execute("UPDATE workspaces SET started=true WHERE id=$1", workspace)
    await store.pool.execute("UPDATE agents SET state='READY' WHERE id=$1", agent)

    async def ingest(self):
        await asyncio.Event().wait()

    paused = asyncio.Event()
    original_pause = Runtime.pause

    async def pause(self):
        await original_pause(self)
        paused.set()

    monkeypatch.setattr(Runtime, "ingest", ingest)
    monkeypatch.setattr(Runtime, "pause", pause)
    provider = BlockingProvider()
    supervisor = Supervisor(store, settings, provider, workspace, "token")
    running = asyncio.create_task(supervisor.run())
    try:
        await asyncio.wait_for(provider.entered.wait(), 2)
        owner = (await store.state(agent))["owner_id"]
        await control(store, agent, command(owner))
        await asyncio.wait_for(paused.wait(), 2)
        assert supervisor.workers[agent].task.done()
        assert not supervisor.ingestions[agent].done()
        assert (await store.state(agent))["reserved"] == 0
        assert (await store.state(agent))["state"] == "READY"
        assert (
            await store.pool.fetchval(
                "SELECT count(*) FROM usage_reservations WHERE state='uncertain'"
            )
            == 1
        )
        await supervisor.refresh(agent)
        assert provider.calls == 1
        provider.entered.clear()
        await control(store, agent, command(owner, "/continue", "resume", 2000))
        await asyncio.wait_for(provider.entered.wait(), 2)
        assert provider.calls == 2
        provider.release.set()
        await asyncio.wait_for(supervisor.workers[agent].task, 2)
    finally:
        supervisor.stopping.set()
        await asyncio.wait_for(running, 2)
        tasks = list(supervisor.ingestions.values()) + [
            r.task for r in supervisor.workers.values() if r.task
        ]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for runtime in supervisor.workers.values():
            await runtime.bot.http.aclose()


async def test_model_requests_have_no_count_or_dollar_caps(runtime_store):
    store, settings, agent, workspace = runtime_store
    await store.pool.execute("UPDATE agents SET turns=1000000 WHERE id=$1", agent)
    turn = await store.reserve_cost(agent, Decimal("0.001"), [])
    assert (await store.state(agent))["turns"] == 1000001
    await store.finish(turn, None)
    large = await store.reserve_cost(agent, Decimal("1000"), [])
    assert (await store.state(agent))["reserved"] == Decimal("1000")
    await store.finish(large, None)
    assert (await store.state(agent))["spent"] == Decimal("1000.001")


async def test_pause_cancels_active_tool_and_preserves_uncertain_tool_result(runtime_store):
    from harness.core.types import ModelResponse, Usage
    from harness.mattermost.client import Client

    store, settings, agent, workspace = runtime_store
    entered, cancelled = asyncio.Event(), asyncio.Event()

    class ToolProvider:
        async def generate(self, *args):
            return ModelResponse(
                message={
                    "output": [
                        {
                            "type": "function_call",
                            "call_id": "fetch",
                            "name": "web_fetch",
                            "arguments": '{"url":"https://example.com"}',
                        }
                    ]
                },
                usage=Usage(input_tokens=1, output_tokens=1, cost_usd=Decimal("0.001")),
            )

    async with Client("https://unused.test", "token") as bot:
        runtime = Runtime(store, bot, agent, settings, ToolProvider())

        async def execute(*args):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        runtime.tools.execute = execute
        runtime.task = asyncio.create_task(runtime.activate())
        await asyncio.wait_for(entered.wait(), 2)
        await control(store, agent, command((await store.state(agent))["owner_id"]))
        await runtime.pause()
        assert cancelled.is_set() and runtime.task.done()
        assert (await store.state(agent))["state"] == "READY"
        history = await store.history(agent)
        assert history[-1]["type"] == "function_call_output"
        assert "INTERRUPTED_TOOL_CALL" in history[-1]["output"]
        runtime.stopping.set()
