import os
import time
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import asyncpg
import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from harness.api.commands import SlashCommands
from harness.core.prompts import birth_record
from harness.core.reset import begin_reset, delete_external, finish_reset, purge, snapshot
from harness.mattermost.client import Client, MattermostError
from harness.mattermost.provisioning import provision_seed
from harness.persistence.bootstrap import load, save

pytestmark = pytest.mark.skipif(os.environ.get("HARNESS_LIVE_TESTS") != "1", reason="live gate")


async def test_reset_callback_is_owner_only_global_and_asynchronous(runtime_store, monkeypatch):
    store, settings, agent, workspace = runtime_store
    owner = (await store.state(agent))["owner_id"]
    await store.pool.execute(
        "INSERT INTO slash_commands VALUES($1,'reset','id','secret')", workspace
    )
    admin = AsyncMock()
    admin.__aenter__.return_value = admin
    admin.request.return_value = {"team_id": "team", "type": "O"}
    monkeypatch.setattr("harness.api.commands.Client", lambda *a, **k: admin)
    handler = SlashCommands(store, settings, workspace)
    handler.reset = AsyncMock(return_value="pending")
    app = web.Application()
    app.router.add_post("/commands", handler.handle)
    body = {
        "token": "secret",
        "command": "/reset",
        "user_id": owner,
        "team_id": "team",
        "channel_id": "general",
        "trigger_id": "call",
        "text": "",
    }
    async with TestClient(TestServer(app)) as http:
        for update in ({"token": "bad"}, {"user_id": "bot"}):
            assert (await http.post("/commands", data={**body, **update})).status == 403
        assert (await http.post("/commands", data={**body, "channel_id": "work"})).status == 400
        handler.reset.assert_not_called()
        response = await http.post("/commands", data=body)
        assert response.status == 200
        assert (await response.json())["response_type"] == "ephemeral"
        handler.reset.assert_awaited_once()


async def test_reset_receipts_guards_purge_isolation_and_trigger_restoration(
    runtime_store, tmp_path
):
    store, settings, agent, workspace = runtime_store
    other = uuid4()
    identity = birth_record(
        agent_id=other,
        workspace_id=uuid4(),
        username="other",
        mm_user_id="other",
        settings=settings,
        inference_enabled=True,
    )
    await store.initialize(
        {
            "identity": identity.model_dump(),
            "team_name": "other",
            "team_id": "other",
            "general_id": "other",
        },
        settings,
        1,
    )
    await store.pool.execute(
        "INSERT INTO events(workspace_id,type,data) VALUES($1,'test','{}')", workspace
    )
    await store.pool.execute("INSERT INTO conversations(agent_id,item) VALUES($1,'{}')", agent)
    path = tmp_path / "workspace.json"
    state = {
        "workspace_id": str(workspace),
        "seed_id": str(agent),
        "binding": {"server_id": settings.mattermost_server_id},
    }
    save(path, state)
    assert await begin_reset(store, workspace, "request", path) == "pending"
    first = load(path)["reset"]["seed_id"]
    await begin_reset(store, workspace, "retry", path)
    assert load(path)["reset"]["seed_id"] == first
    with pytest.raises(ValueError, match="RESET_IN_PROGRESS"):
        await store.slash_control(
            workspace, settings.mattermost_owner_id, "general", "/continue", "resume"
        )
    state = load(path)
    state["reset"].update(manifest={"agents": [str(agent)]}, fresh={"general_id": "fresh"})
    broken = {**state, "reset": {**state["reset"], "fresh": {}}}
    with pytest.raises(KeyError):
        await purge(store, workspace, broken)
    assert await store.list_agents(workspace)
    with pytest.raises(asyncpg.RaiseError, match="append-only"):
        await store.pool.execute("DELETE FROM events WHERE workspace_id=$1", workspace)
    await store.pool.execute("INSERT INTO idle_channel_cursors VALUES($1,'general',1000)", agent)
    await store.pool.execute("INSERT INTO idle_channel_cursors VALUES($1,'other',1000)", other)
    await purge(store, workspace, state)
    assert await store.pool.fetchval("SELECT count(*) FROM idle_channel_cursors") == 1
    assert not await store.list_agents(workspace)
    assert (await store.state(other))["id"] == other
    assert await store.pool.fetchval("SELECT count(*) FROM reset_requests") == 1
    await store.pool.execute(
        "INSERT INTO events(workspace_id,type,data) VALUES($1,'new','{}')", workspace
    )
    with pytest.raises(asyncpg.RaiseError, match="append-only"):
        await store.pool.execute("DELETE FROM events WHERE workspace_id=$1", workspace)
    assert await store.pool.fetchval("SELECT count(*) FROM conversations") == 0


async def test_live_reset_clears_archived_private_and_default_channels_and_restarts_idle(
    runtime_store, tmp_path, monkeypatch
):
    store, settings, _, original = runtime_store
    workspace, seed = uuid4(), uuid4()
    team_name = "reset-test-" + workspace.hex[:10]
    path = tmp_path / "workspace.json"
    async with Client(
        str(settings.mattermost_url), settings.mattermost_admin_token.get_secret_value()
    ) as admin:
        owner_before = await admin.request("GET", f"users/{settings.mattermost_owner_id}")
        service_before = (await admin.request("GET", "config"))["ServiceSettings"]
        info = await provision_seed(
            admin,
            workspace,
            seed,
            settings.mattermost_owner_id,
            team_name=team_name,
            bot_username="reset-" + seed.hex[:10],
            channel_name="town-square",
        )
        identity = birth_record(
            agent_id=seed,
            workspace_id=workspace,
            username=info["bot_username"],
            mm_user_id=info["bot_user_id"],
            settings=settings,
            inference_enabled=True,
            coordination_enabled=True,
        )
        state = {
            "workspace_id": str(workspace),
            "seed_id": str(seed),
            "created_ms": int(time.time() * 1000),
            "state": "ready",
            "identity": identity.model_dump(mode="json"),
            **info,
            "binding": {
                "owner_id": settings.mattermost_owner_id,
                "server_id": settings.mattermost_server_id,
                "mattermost_url": str(settings.mattermost_url).rstrip("/"),
            },
        }
        save(path, state)
        await store.initialize(state, settings, state["created_ms"])
        private = await admin.request(
            "POST",
            "channels",
            json={
                "team_id": info["team_id"],
                "type": "P",
                "name": "private",
                "display_name": "Private",
            },
        )
        archived = await admin.request(
            "POST",
            "channels",
            json={
                "team_id": info["team_id"],
                "type": "O",
                "name": "archived",
                "display_name": "Archived",
            },
        )
        post = await admin.request(
            "POST",
            "posts",
            json={"channel_id": info["general_id"], "message": "erase this test message"},
        )
        reply = await admin.request(
            "POST",
            "posts",
            json={
                "channel_id": info["general_id"],
                "message": "erase reply",
                "root_id": post["id"],
            },
        )
        deleted_post = await admin.request(
            "POST",
            "posts",
            json={"channel_id": info["off_topic_id"], "message": "old deleted"},
        )
        await admin.request("DELETE", f"posts/{deleted_post['id']}")
        await admin.request("DELETE", f"channels/{archived['id']}")
        try:
            await begin_reset(store, workspace, "live-test", path)
            # Simulate a crash after external deletion; recovery must tolerate missing old IDs.
            checkpoint = load(path)
            checkpoint["reset"]["manifest"] = await snapshot(store, admin, checkpoint)
            save(path, checkpoint)
            await delete_external(admin, checkpoint, path)
            fresh = await finish_reset(store, settings, path)
            assert UUID(fresh["seed_id"]) != seed
            assert fresh["general_id"] == info["general_id"]
            assert fresh["off_topic_id"] == info["off_topic_id"]
            assert fresh["errors_id"] == info["errors_id"]
            assert not (await admin.request("GET", f"channels/{info['general_id']}"))["delete_at"]
            assert "staging_id" not in checkpoint["reset"]
            agents = await store.list_agents(workspace)
            assert len(agents) == 1
            status = await store.state(agents[0].id)
            assert status["state"] == "IDLE" and not status["started"]
            assert await begin_reset(store, workspace, "live-test", path) == "completed"
            for endpoint in (
                f"channels/{private['id']}",
                f"channels/{archived['id']}",
                f"users/{info['bot_user_id']}",
                f"posts/{post['id']}",
                f"posts/{reply['id']}",
                f"posts/{deleted_post['id']}",
            ):
                with pytest.raises(MattermostError) as caught:
                    await admin.request("GET", endpoint)
                assert caught.value.status == 404
            for channel in (info["general_id"], info["off_topic_id"]):
                remaining = await admin.request(
                    "GET", f"channels/{channel}/posts", params={"include_deleted": "true"}
                )
                assert not {post["id"], reply["id"], deleted_post["id"]}.intersection(
                    remaining["posts"]
                )
            owner_after = await admin.request("GET", f"users/{settings.mattermost_owner_id}")
            assert owner_after["id"] == owner_before["id"] and not owner_after["delete_at"]
            assert await store.list_agents(original)
            service_after = (await admin.request("GET", "config"))["ServiceSettings"]
            for key in (
                "EnableAPIChannelDeletion",
                "EnableAPIUserDeletion",
                "EnableAPIPostDeletion",
            ):
                assert service_after[key] == service_before[key]
            assert "reset" not in load(path)
        finally:
            current = load(path)
            current["reset"] = {"fresh": True, "manifest": await snapshot(store, admin, current)}
            current["reset"]["manifest"]["preserve_channels"] = []
            await delete_external(admin, current, path)
            await admin.request("DELETE", f"teams/{info['team_id']}")


async def test_reset_quiesces_reasoning_and_ingestion_before_external_cleanup(
    runtime_store, tmp_path, monkeypatch
):
    import asyncio

    from test_runtime import BlockingProvider

    from harness.core.runtime import Runtime
    from harness.core.supervisor import Supervisor

    store, settings, seed, workspace = runtime_store
    await store.pool.execute("UPDATE workspaces SET started=true WHERE id=$1", workspace)
    await store.set_state(seed, "READY")
    # Seed coordinator only calls inference when it has actual input.
    from test_runtime import post

    await store.ingest(seed, post(), "channel_post", "reset-test")

    async def ingest(self):
        await asyncio.Event().wait()

    monkeypatch.setattr(Runtime, "ingest", ingest)
    provider = BlockingProvider()
    path = tmp_path / "workspace.json"
    save(path, {"workspace_id": str(workspace), "binding": {}})
    supervisor = Supervisor(store, settings, provider, workspace, "token", checkpoint=path)
    await supervisor.refresh(seed)
    await asyncio.wait_for(provider.entered.wait(), 2)
    reasoning = supervisor.workers[seed].task
    ingestion = supervisor.ingestions[seed]

    async def finish(*args):
        assert reasoning.done() and ingestion.done()
        assert not supervisor.workers and not supervisor.ingestions
        assert (await store.state(seed))["workspace_state"] == "paused"
        return {"seed_id": str(seed), "bot_token": "fresh-token", "general_id": "fresh"}

    monkeypatch.setattr("harness.core.reset.finish_reset", finish)
    admin = AsyncMock()
    admin.__aenter__.return_value = admin
    monkeypatch.setattr("harness.core.supervisor.Client", lambda *a, **k: admin)
    assert await supervisor.request_reset("request") == "pending"
    supervisor.refresh = AsyncMock()
    await asyncio.wait_for(supervisor.reset_task, 2)
    assert not supervisor.resetting and supervisor.seed_token == "fresh-token"
    supervisor.refresh.assert_awaited_once_with(seed)


async def test_external_failure_restores_server_deletion_flags(tmp_path):
    admin = AsyncMock()
    admin.me.return_value = {"id": "admin"}
    config = {
        "ServiceSettings": {"EnableAPIChannelDeletion": False, "EnableAPIUserDeletion": False}
    }

    async def request(method, endpoint, **kwargs):
        if endpoint == "config":
            return config
        if endpoint == "config/patch":
            config["ServiceSettings"].update(kwargs["json"]["ServiceSettings"])
            return config
        if method == "GET":
            return {"team_id": "team", "type": "O"}
        raise MattermostError(500, "simulated_failure")

    admin.request.side_effect = request
    state = {
        "team_id": "team",
        "reset": {"fresh": True, "manifest": {"channels": ["channel"], "bots": []}},
    }
    with pytest.raises(MattermostError):
        await delete_external(admin, state, tmp_path / "checkpoint.json")
    assert not config["ServiceSettings"]["EnableAPIChannelDeletion"]
    assert not config["ServiceSettings"]["EnableAPIUserDeletion"]
    assert not config["ServiceSettings"]["EnableAPIPostDeletion"]


async def test_clear_default_posts_pages_before_deleting_and_preserves_channel():
    from harness.core.reset import clear_channel_posts

    admin = AsyncMock()
    posts = [
        {"id": str(i), "channel_id": "town", "root_id": "root" if i % 2 else ""} for i in range(205)
    ]
    deleted = []
    fetched = []

    async def request(method, endpoint, **kwargs):
        if method == "GET":
            assert kwargs["params"]["include_deleted"] == "true"
            assert not deleted  # Snapshot all pages before changing pagination offsets.
            page = kwargs["params"]["page"]
            fetched.append(page)
            batch = posts[page * 200 : (page + 1) * 200]
            return {"order": [p["id"] for p in batch], "posts": {p["id"]: p for p in batch}}
        assert endpoint.startswith("posts/") and kwargs["params"]["permanent"] == "true"
        deleted.append(endpoint.split("/")[1])
        if len(deleted) == 1:
            raise MattermostError(404, "already_deleted")
        return {"status": "OK"}

    admin.request.side_effect = request
    await clear_channel_posts(admin, "town")
    assert fetched == [0, 1] and len(deleted) == 205
    assert all(int(i) % 2 for i in deleted[:102])  # Replies before roots.
