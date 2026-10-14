import asyncio
import json
import os
from unittest.mock import AsyncMock

import pytest
from test_runtime import BlockingProvider, post

from harness.api.analytics import ledger_snapshot, service_links
from harness.core.coordinator import Coordinator
from harness.mattermost.client import Client

pytestmark = pytest.mark.skipif(
    os.environ.get("HARNESS_LIVE_TESTS") != "1", reason="PostgreSQL gate"
)


async def test_seed_has_no_execution_tools_and_idles_without_inference(runtime_store):
    store, settings, agent, workspace = runtime_store
    bot = Client("http://mm.invalid", "token")
    bot.presence = AsyncMock()
    provider = BlockingProvider()
    provider.release.set()
    settings = settings.model_copy(update={"agent_engine": "opencode", "coding_enabled": True})
    runtime = Coordinator(store, bot, agent, settings, provider)
    runtime.stopping.set()  # no automatic follow-on scheduling during direct checks
    try:
        assert "code_task" not in {t.name for t in runtime.tools.manifests}
        assert "begin_channel_post" not in {t.name for t in runtime.tools.manifests}
        assert {"spawn_agent", "create_channel", "gitea_request_review"} <= {
            t.name for t in runtime.tools.manifests
        }
        await runtime.activate()
        assert (await store.state(agent))["state"] == "IDLE"
        assert not await store.pool.fetchval(
            "SELECT count(*) FROM agent_turns WHERE agent_id=$1", agent
        )
        turn = await store.reserve_cost(agent, 0, [])
        result = await runtime.tools.execute(
            turn,
            {
                "name": "code_task",
                "call_id": "forbidden",
                "arguments": json.dumps({"task": "Write code"}),
            },
        )
        assert "error" in result
        rejected = await runtime.tools.execute(
            turn, {"name": "set_status", "call_id": "away", "arguments": '{"status":"away"}'}
        )
        assert "SEED_STAYS_AVAILABLE" in rejected["detail"]
        with pytest.raises(ValueError, match="SEED_COORDINATION_ONLY"):
            await runtime.tools.repository(settings, "POST", "/contents/code.py", "{}")
        await runtime.begin_work()
        assert (await store.state(agent))["availability"] == "available"
        bot.presence.assert_awaited_with("online")
    finally:
        await bot.http.aclose()


async def test_seed_wakes_for_messages_and_remains_chat_only(runtime_store):
    store, settings, agent, workspace = runtime_store
    bot = Client("http://mm.invalid", "token")
    bot.presence = AsyncMock()
    provider = BlockingProvider()
    provider.release.set()
    runtime = Coordinator(store, bot, agent, settings, provider)
    runtime.tools.observe_posts = lambda _: None
    try:
        await store.pool.execute(
            "UPDATE workspaces SET owner_id='owner',started=true WHERE id=$1", workspace
        )
        assert await store.ingest(agent, post(), "channel_post", "s1")
        await runtime.wake(agent)
        await asyncio.wait_for(runtime.task, 5)
        await asyncio.sleep(0.05)
        assert (await store.state(agent))["state"] == "IDLE"
        assert await store.unread_count(agent) == 0
        snapshot = await ledger_snapshot(store, workspace, "opencode")
        seed = snapshot["agents"][0]
        assert seed["coordinator"]
        service_links(snapshot, settings)
        assert seed["opencode_url"] is None
        first = await store.pool.fetchval(
            "SELECT count(*) FROM agent_turns WHERE agent_id=$1", agent
        )
        await runtime.wake(agent)
        assert (
            await store.pool.fetchval("SELECT count(*) FROM agent_turns WHERE agent_id=$1", agent)
            == first
        )
    finally:
        runtime.stopping.set()
        await bot.http.aclose()


async def test_supervisor_assigns_seed_to_chat_and_workers_to_opencode(runtime_store, monkeypatch):
    from uuid import uuid4

    from harness.coding.runtime import NativeRuntime
    from harness.core.runtime import Runtime
    from harness.core.supervisor import Supervisor

    store, settings, seed, workspace = runtime_store
    child = uuid4()
    await store.pool.execute(
        "INSERT INTO agents(id,workspace_id,parent_id,identity,model_id,state) "
        "SELECT $1,workspace_id,id,identity,model_id,'READY' FROM agents WHERE id=$2",
        child,
        seed,
    )
    settings = settings.model_copy(update={"agent_engine": "opencode", "coding_enabled": True})
    monkeypatch.setattr(Runtime, "ingest", AsyncMock())
    supervisor = Supervisor(store, settings, BlockingProvider(), workspace, "seed-token")
    try:
        await supervisor.refresh(seed)
        await supervisor.refresh(child)
        assert isinstance(supervisor.workers[seed], Coordinator)
        assert isinstance(supervisor.workers[child], NativeRuntime)
        assert seed not in supervisor.tool_bridge.tools
        assert child in supervisor.tool_bridge.tools
    finally:
        for runtime in supervisor.workers.values():
            runtime.stopping.set()
            if runtime.task:
                runtime.task.cancel()
            await runtime.bot.http.aclose()
        await asyncio.gather(*supervisor.ingestions.values(), return_exceptions=True)
