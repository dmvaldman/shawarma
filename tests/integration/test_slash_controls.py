import os
from decimal import Decimal
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from test_coordination import ARGS, FakeAdmin

from harness.api.commands import SlashCommands
from harness.core.coordination import Coordination
from harness.persistence.store import QuotaError

pytestmark = pytest.mark.skipif(
    os.environ.get("HARNESS_LIVE_TESTS") != "1", reason="live PostgreSQL gate"
)


async def test_channel_controls_target_subscribers_and_global_continue_clears_pauses(
    runtime_store, monkeypatch
):
    store, settings, seed, workspace = runtime_store
    monkeypatch.setattr("harness.core.coordination.Client", FakeAdmin)
    coordinator = Coordination(store, settings)
    child = UUID((await coordinator.spawn(seed, "child", ARGS))["agent_id"])
    other = UUID((await coordinator.spawn(seed, "other", ARGS))["agent_id"])
    await store.pool.execute("INSERT INTO subscriptions VALUES($1,'audit','mentions')", child)
    await store.pool.execute("INSERT INTO subscriptions VALUES($1,'audit','none')", other)
    owner = (await store.state(seed))["owner_id"]
    result = await store.slash_control(workspace, owner, "audit", "/pause", "pause-audit")
    assert result["affected_agents"] == 1 and not result["workspace_paused"]
    assert (await store.state(child))["control_paused"]
    assert not (await store.state(seed))["control_paused"]
    assert not (await store.state(other))["control_paused"]
    with pytest.raises(QuotaError):
        await store.reserve_cost(child, Decimal("0.001"), [])
    with pytest.raises(ValueError, match="PARENT_NOT_ACTIVE"):
        await coordinator.reserve(child, "blocked-birth", ARGS)
    await store.recover(child)
    assert (await store.state(child))["control_paused"]
    await store.slash_control(workspace, owner, "general", "/stop", "global-stop")
    local = await store.slash_control(workspace, owner, "audit", "/continue", "local-continue")
    assert local["workspace_paused"] and not (await store.state(child))["control_paused"]
    with pytest.raises(QuotaError):
        await store.reserve_cost(child, Decimal("0.001"), [])
    await store.slash_control(workspace, owner, "audit", "/pause", "again")
    await store.slash_control(workspace, owner, "general", "/continue", "global-continue")
    assert not (await store.state(child))["control_paused"]
    assert (await store.state(seed))["workspace_state"] == "active"
    await coordinator.terminate(seed, child, "done")
    await store.slash_control(workspace, owner, "audit", "/continue", "no-resurrection")
    assert (await store.state(child))["state"] == "TERMINATED"


async def test_native_callback_authentication_validation_and_ephemeral_response(
    runtime_store, monkeypatch
):
    store, settings, agent, workspace = runtime_store
    owner = (await store.state(agent))["owner_id"]
    await store.pool.execute(
        "INSERT INTO slash_commands VALUES($1,'pause','command-id','secret')", workspace
    )
    admin = AsyncMock()
    admin.__aenter__.return_value = admin
    admin.request.return_value = {"team_id": "team", "type": "O"}
    monkeypatch.setattr("harness.api.commands.Client", lambda *a, **k: admin)
    handler = SlashCommands(store, settings, workspace)
    app = web.Application()
    app.router.add_post("/commands", handler.handle)
    body = {
        "token": "secret",
        "command": "/pause",
        "user_id": owner,
        "team_id": "team",
        "channel_id": "general",
        "trigger_id": "invocation",
        "text": "",
    }
    async with TestClient(TestServer(app)) as http:
        for update in ({"token": "wrong"}, {"user_id": "bot"}, {"team_id": "other"}):
            response = await http.post("/commands", data={**body, **update})
            assert response.status == 403
        response = await http.post("/commands", data={**body, "text": "extra"})
        assert response.status == 400
        admin.request.return_value = {"team_id": "team", "type": "D"}
        response = await http.post("/commands", data=body)
        assert response.status == 400
        assert (await store.state(agent))["workspace_state"] == "active"
        admin.request.return_value = {"team_id": "team", "type": "P"}
        for _ in range(2):
            response = await http.post("/commands", data=body)
            assert response.status == 200
            assert (await response.json())["response_type"] == "ephemeral"
        assert (await store.state(agent))["workspace_state"] == "paused"
        assert await store.pool.fetchval("SELECT count(*) FROM slash_control_receipts") == 1


async def test_registration_is_idempotent_recovers_partial_creation_and_refuses_conflicts(
    runtime_store,
):
    store, settings, agent, workspace = runtime_store
    admin = AsyncMock()
    admin.me.return_value = {"id": "creator"}
    commands = {}
    config = {
        "ServiceSettings": {
            "EnableCommands": False,
            "AllowedUntrustedInternalConnections": "localhost",
        }
    }

    async def request(method, path, **kwargs):
        if path == "config":
            return config
        if path == "config/patch":
            config["ServiceSettings"].update(kwargs["json"]["ServiceSettings"])
            return config
        if path == "commands" and method == "GET":
            return list(commands.values())
        if path == "commands" and method == "POST":
            body = kwargs["json"]
            command = {
                **body,
                "id": body["trigger"],
                "token": "private-token",
                "creator_id": "creator",
            }
            commands[body["trigger"]] = command
            return command
        if path.startswith("commands/") and method == "PUT":
            commands[kwargs["json"]["trigger"]] = kwargs["json"]
            return kwargs["json"]
        raise AssertionError(path)

    admin.request.side_effect = request
    handler = SlashCommands(store, settings, workspace)
    await handler.register(admin, "team")
    await handler.register(admin, "team")
    assert sum(c.args == ("POST", "commands") for c in admin.request.await_args_list) == 4
    assert (
        config["ServiceSettings"]["AllowedUntrustedInternalConnections"]
        == "localhost host.docker.internal"
    )
    await store.pool.execute(
        "DELETE FROM slash_commands"
    )  # Crash after API success, before receipt.
    await handler.register(admin, "team")
    assert await store.pool.fetchval("SELECT count(*) FROM slash_commands") == 4
    commands["stop"] = {**commands["stop"], "creator_id": "other-creator"}
    with pytest.raises(ValueError, match="SLASH_COMMAND_CONFLICT"):
        await handler.register(admin, "team")


async def test_channel_pause_cancels_only_targeted_work_and_keeps_listeners(
    runtime_store, monkeypatch
):
    import asyncio

    from test_runtime import BlockingProvider

    from harness.core.runtime import Runtime
    from harness.core.supervisor import Supervisor

    store, settings, seed, workspace = runtime_store
    monkeypatch.setattr("harness.core.coordination.Client", FakeAdmin)
    child = UUID((await Coordination(store, settings).spawn(seed, "worker", ARGS))["agent_id"])
    await store.pool.execute("INSERT INTO subscriptions VALUES($1,'audit','all')", child)
    await store.pool.execute("UPDATE workspaces SET started=true WHERE id=$1", workspace)
    await store.set_state(seed, "READY")

    async def ingest(self):
        await asyncio.Event().wait()

    monkeypatch.setattr(Runtime, "ingest", ingest)
    provider = BlockingProvider()
    supervisor = Supervisor(store, settings, provider, workspace, "token")
    try:
        await supervisor.refresh(seed)
        await supervisor.refresh(child)
        async with asyncio.timeout(2):
            while provider.calls < 2:
                provider.entered.clear()
                await provider.entered.wait()
        owner = (await store.state(seed))["owner_id"]
        await store.slash_control(workspace, owner, "audit", "/stop", "local-stop")
        await supervisor.control()
        assert supervisor.workers[child].task.done()
        assert not supervisor.workers[seed].task.done()
        assert not supervisor.ingestions[child].done()
        assert (await store.state(child))["state"] == "READY"
        assert (await store.state(child))["reserved"] == 0
        await store.slash_control(workspace, owner, "audit", "/continue", "local-continue")
        await supervisor.control()
        async with asyncio.timeout(2):
            while provider.calls < 3:
                provider.entered.clear()
                await provider.entered.wait()
    finally:
        supervisor.stopping.set()
        tasks = list(supervisor.ingestions.values()) + [
            r.task for r in supervisor.workers.values() if r.task
        ]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for runtime in supervisor.workers.values():
            await runtime.bot.http.aclose()
