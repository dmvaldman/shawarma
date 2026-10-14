import os
from unittest.mock import AsyncMock
from uuid import UUID

import httpx
import pytest
from test_coordination import ARGS, FakeAdmin

from harness.core.coordination import Coordination
from harness.core.runtime import Runtime
from harness.core.types import Post
from harness.mattermost.client import Client
from harness.notifications.bridge import Bridge

pytestmark = pytest.mark.skipif(os.environ.get("HARNESS_LIVE_TESTS") != "1", reason="live gate")


async def test_idle_catchup_unmentioned_posts_pagination_restart_and_live_dedup(
    runtime_store, monkeypatch
):
    store, settings, seed, _ = runtime_store
    monkeypatch.setattr("harness.core.coordination.Client", FakeAdmin)
    child = UUID((await Coordination(store, settings).spawn(seed, "child", ARGS))["agent_id"])
    await store.pool.execute("INSERT INTO subscriptions VALUES($1,'work','mentions')", child)
    await store.pool.execute("INSERT INTO subscriptions VALUES($1,'disabled','none')", child)
    await store.pool.execute("INSERT INTO subscriptions VALUES($1,'errors','all')", child)
    state = await store.state(child)
    start = state["created_ms"] + 1
    channels = [
        {"id": name, "team_id": "team", "type": "O"} for name in ("general", "work", "disabled")
    ] + [
        {"id": "dm", "team_id": "", "type": "D"},
        {"id": "errors", "name": "errors", "team_id": "team", "type": "O"},
    ]
    posts = {
        name: [
            Post(
                id=f"{name}-{i}",
                user_id="peer",
                channel_id=name,
                message=f"new idea {i}",
                create_at=start + i,
            )
            for i in range(105 if name == "work" else 1)
        ]
        for name in ("general", "work")
    }
    posts["work"].append(
        Post(id="old", user_id="peer", channel_id="work", message="old", create_at=start - 2)
    )
    calls = []

    def handle(request):
        path = request.url.path
        if path.endswith("/channels"):
            return httpx.Response(200, json=channels)
        channel = path.split("/")[-2] if path.endswith("posts") else path.split("/")[-1]
        if path.endswith("posts"):
            assert channel in posts, "Disabled and DM channels must not be read"
            calls.append(channel)
            page = int(request.url.params.get("page", 0))
            batch = sorted(posts[channel], key=lambda p: p.create_at, reverse=True)[
                page * 100 : (page + 1) * 100
            ]
            return httpx.Response(
                200,
                json={
                    "order": [p.id for p in batch],
                    "posts": {p.id: p.model_dump() for p in batch},
                },
            )
        return httpx.Response(200, json=next(c for c in channels if c["id"] == channel))

    async with Client("http://mattermost", "token", transport=httpx.MockTransport(handle)) as bot:
        bridge = Bridge(store, bot, child, settings)
        ordinary = {"event": "posted", "data": {"post": posts["work"][0].model_dump_json()}}
        diagnostic = Post(
            id="log", user_id="owner", channel_id="errors", message="error", create_at=start
        )
        assert not await bridge.ingest(
            {"event": "posted", "data": {"post": diagnostic.model_dump_json()}}
        )
        assert not await bridge.ingest(ordinary)  # No immediate unmentioned wake.
        assert not await store.ingest(child, posts["general"][0], "channel_post", "s")
        assert await store.ingest(child, posts["work"][1], "channel_post", state["server_id"])
        assert await bridge.catch_up()
        assert await store.unread_count(child) == 106
        assert calls.count("work") == 2  # Catch-up pages beyond the first 100.
        assert not await Bridge(store, bot, child, settings).catch_up()  # Restart watermark.
        assert await store.unread_count(child) == 106
        assert not await store.ingest(child, posts["work"][0], "channel_post", state["server_id"])
        assert not await store.pool.fetchval("SELECT 1 FROM notifications WHERE post_id='old'")
        await store.apply_main_routing(state["workspace_id"])
        assert await store.unread_count(child) == 106  # Catch-up survives startup seed policy.
        posts["work"].append(
            Post(
                id="late",
                user_id="peer",
                channel_id="work",
                message="follow up",
                create_at=start + 106,
            )
        )
        assert await bridge.catch_up()
        assert await store.unread_count(child) == 107


@pytest.mark.parametrize(
    "gate",
    [
        "idle",
        "empty",
        "paused",
        "global_paused",
        "away",
        "running",
        "ready",
        "failed",
        "blocked",
        "terminated",
        "not_started",
        "disconnected",
    ],
)
async def test_idle_catchup_wake_and_owner_lifecycle_gates(runtime_store, gate):
    store, settings, agent, workspace = runtime_store
    await store.pool.execute("UPDATE workspaces SET started=true WHERE id=$1", workspace)
    runtime = Runtime(store, object(), agent, settings, object())
    runtime.connected.set()
    runtime.bridge.catch_up = AsyncMock(return_value=gate != "empty")
    runtime.wake = AsyncMock()
    if gate == "paused":
        await store.pool.execute("UPDATE agents SET control_paused=true WHERE id=$1", agent)
    elif gate == "global_paused":
        await store.pool.execute("UPDATE workspaces SET state='paused' WHERE id=$1", workspace)
    elif gate == "away":
        await store.set_availability(agent, "away")
    elif gate in {"running", "ready", "failed", "blocked", "terminated"}:
        await store.set_state(agent, "QUOTA_BLOCKED" if gate == "blocked" else gate.upper())
    elif gate == "not_started":
        await store.pool.execute("UPDATE workspaces SET started=false WHERE id=$1", workspace)
    elif gate == "disconnected":
        runtime.connected.clear()
    await runtime.idle_catch_up()
    assert runtime.bridge.catch_up.await_count == int(gate in {"idle", "empty", "running", "ready"})
    assert runtime.wake.await_count == int(gate in {"idle", "running", "ready"})
