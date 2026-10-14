"""Analytics counters and controls against an isolated database; no model calls."""

import json
import os
from decimal import Decimal
from uuid import uuid4

import pytest
from aiohttp.test_utils import TestClient, TestServer

from harness.api.analytics import Analytics, ledger_snapshot
from harness.core.prompts import birth_record
from harness.core.reset import purge
from harness.core.types import Usage

pytestmark = pytest.mark.skipif(
    os.environ.get("HARNESS_LIVE_TESTS") != "1", reason="live PostgreSQL gate"
)


async def test_totals_count_tokens_and_confirmed_posts_once(runtime_store):
    store, settings, agent, workspace = runtime_store
    await store.pool.execute("UPDATE workspaces SET started=true WHERE id=$1", workspace)
    turn = await store.reserve_cost(agent, Decimal("1"), [])
    await store.finish(turn, Usage(input_tokens=120, output_tokens=30, cost_usd=Decimal(".25")))
    unknown = await store.reserve_cost(agent, Decimal(".10"), [])
    await store.finish(unknown, None)
    await store.reserve_cost(agent, Decimal(".20"), [])
    for call, name, result in [
        ("posted", "post_channel", {"id": "post1", "channel_id": "general"}),
        ("duplicate", "post_channel", {"id": "post1", "channel_id": "general"}),
        ("deferred", "post_channel", {"deferred": True}),
        ("failed", "post_channel", {"error": "TOOL_TIMEOUT"}),
        ("private", "web_search", {"id": "search", "channel_id": "general"}),
    ]:
        await store.pool.execute(
            "INSERT INTO tool_calls(id,agent_id,call_id,tool_name,arguments,result,state) "
            "VALUES($1,$2,$3,$4,'{}',$5,'completed')",
            uuid4(),
            agent,
            call,
            name,
            json.dumps(result),
        )
    await store.set_state(agent, "RUNNING")
    data = await ledger_snapshot(store, workspace)
    assert data["totals"] == {
        "agents": 1,
        "agents_created": 1,
        "working": 1,
        "input_tokens": 120,
        "output_tokens": 30,
        "tokens": 150,
        "messages": 1,
        "estimated_cost_usd": "0.35",
        "reserved_estimate_usd": "0.20",
        "uncertain_calls": 1,
        "in_flight_calls": 1,
    }
    assert data["agents"][0]["tokens"] == 150
    assert data["agents"][0]["messages"] == 1
    assert "identity" not in data["agents"][0]
    assert "request" not in json.dumps(data)
    await store.set_state(agent, "TERMINATED")
    data = await ledger_snapshot(store, workspace)
    assert data["totals"]["agents"] == 0
    assert data["totals"]["tokens"] == 150
    assert data["agents"][0]["activity"] == "retired"


async def test_pause_stop_continue_and_reset_reflected(runtime_store):
    store, settings, agent, workspace = runtime_store
    await store.pool.execute("UPDATE workspaces SET started=true WHERE id=$1", workspace)
    await store.set_state(agent, "RUNNING")
    owner = settings.mattermost_owner_id
    for command in ["/pause", "/continue", "/stop", "/continue"]:
        await store.slash_control(workspace, owner, "general", command, uuid4().hex)
        data = await ledger_snapshot(store, workspace)
        paused = command != "/continue"
        assert data["workspace"]["status"] == ("paused" if paused else "active")
        assert data["agents"][0]["working"] is not paused
        assert data["workspace"]["last_control"]["command"] == command
    await store.pool.execute("INSERT INTO subscriptions VALUES($1,'work','all')", agent)
    await store.slash_control(workspace, owner, "work", "/pause", uuid4().hex)
    data = await ledger_snapshot(store, workspace)
    assert data["workspace"]["status"] == "active"
    assert data["agents"][0]["activity"] == "paused"
    await store.slash_control(workspace, owner, "work", "/continue", uuid4().hex)
    turn = await store.reserve_cost(agent, Decimal("1"), [])
    await store.finish(turn, Usage(input_tokens=100, output_tokens=5, cost_usd=Decimal(".1")))
    await store.pool.execute("INSERT INTO reset_requests VALUES($1,'reset','pending')", workspace)
    assert (await ledger_snapshot(store, workspace))["workspace"]["status"] == "resetting"
    fresh_id = uuid4()
    await purge(
        store,
        workspace,
        {
            "binding": {"server_id": settings.mattermost_server_id},
            "reset": {
                "manifest": {"agents": [str(agent)]},
                "seed_id": str(fresh_id),
                "fresh": {"general_id": "general"},
                "created_ms": 1000,
            },
        },
    )
    fresh = birth_record(
        agent_id=fresh_id,
        workspace_id=workspace,
        username="seed",
        mm_user_id="newbot",
        settings=settings,
        inference_enabled=True,
    )
    await store.initialize(
        {
            "identity": fresh.model_dump(),
            "team_id": "team",
            "team_name": "test",
            "general_id": "general",
        },
        settings,
        1000,
    )
    await store.pool.execute(
        "UPDATE reset_requests SET status='completed' WHERE workspace_id=$1", workspace
    )
    await store.pool.execute("UPDATE workspaces SET state='active' WHERE id=$1", workspace)
    data = await ledger_snapshot(store, workspace)
    assert data["totals"]["tokens"] == 0
    assert Decimal(data["totals"]["estimated_cost_usd"]) == 0
    assert data["totals"]["messages"] == 0
    assert data["totals"]["agents"] == 1
    assert data["workspace"]["last_control"] is None
    assert data["workspace"]["status"] == "waiting for task"
    assert data["agents"][0]["id"] == str(fresh_id)
    assert data["agents"][0]["activity"] == "idle"


async def test_http_dashboard_workspace_guard_and_no_secrets(runtime_store, monkeypatch):
    store, settings, agent, workspace = runtime_store
    dashboard = Analytics(store, settings, workspace)

    async def channels(data):
        for a in data["agents"]:
            a["channels"] = [
                {
                    "id": "general",
                    "name": "town-square",
                    "url": "http://localhost:8065/test/channels/town-square",
                }
            ]
        data["memberships_unavailable"] = False

    monkeypatch.setattr(dashboard, "channels", channels)
    async with TestClient(TestServer(dashboard.app())) as client:
        assert (await client.get("/")).status == 200
        response = await client.get(f"/api/v1/workspaces/{workspace}/stats")
        assert response.status == 200
        data = await response.json()
        assert data["agents"][0]["channels"][0]["name"] == "town-square"
        assert response.headers["Cache-Control"] == "no-store"
        text = json.dumps(data)
        assert "system_prompt" not in text and "credential" not in text
        assert settings.mattermost_admin_token.get_secret_value() not in text
        assert (await client.get(f"/api/v1/workspaces/{uuid4()}/stats")).status == 404
        assert (
            await client.get("/api/stats", headers={"Origin": "http://evil.example"})
        ).status == 403
        assert (await client.get("/api/stats", headers={"Host": "evil.example"})).status == 403
        assert (await client.post("/api/stats")).status == 405
