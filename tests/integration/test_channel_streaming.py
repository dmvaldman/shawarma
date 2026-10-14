import asyncio
import json
import os
from decimal import Decimal
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from harness.api.analytics import ledger_snapshot
from harness.core.runtime import Runtime
from harness.mattermost.client import Client
from harness.mattermost.streaming import ChannelStream
from harness.notifications.bridge import Bridge

pytestmark = pytest.mark.skipif(
    os.environ.get("HARNESS_LIVE_TESTS") != "1", reason="PostgreSQL gate"
)


async def test_stream_updates_single_post_omits_reasoning_and_counts_once(
    runtime_store, monkeypatch
):
    store, settings, agent, workspace = runtime_store
    bot = Client("http://mattermost.invalid", "test")
    calls = []
    order = []

    async def typing(*args):
        order.append("typing")

    bot.typing = AsyncMock(side_effect=typing)

    async def request(method, path, **kwargs):
        calls.append((method, path, kwargs.get("json", {})))
        order.append(method)
        return {
            "id": "public",
            "channel_id": "general",
            "user_id": "bot",
            "create_at": 1000,
            "message": kwargs.get("json", {}).get("message", ""),
        }

    bot.request = request
    stream = ChannelStream(store, bot, agent, settings)
    stream_id = uuid4()
    await store.pool.execute(
        "INSERT INTO channel_streams(id,agent_id,call_id,channel_id,baseline) "
        "VALUES($1,$2,'c','general',$3)",
        stream_id,
        agent,
        json.dumps(["old"]),
    )

    def messages(text, ended=False):
        return [
            {
                "info": {"id": "old", "role": "assistant"},
                "parts": [{"id": "oldp", "type": "text", "text": "Old private work"}],
            },
            {
                "info": {"id": "new", "role": "assistant"},
                "parts": [
                    {"id": "r", "type": "reasoning", "text": "Secret reasoning"},
                    {"id": "p", "type": "text", "text": text, "time": {"end": 1} if ended else {}},
                ],
            },
        ]

    try:
        await stream.update(messages("**Finding"))
        await stream.update(messages("**Finding:** 42"))
        await stream.update(messages("**Finding:** 42", True))
        await stream.update(messages("Later private text", True))
        assert [c[0] for c in calls] == ["POST", "PUT", "PUT"]
        assert calls[-1][2] == {
            "message": "**Finding:** 42",
            "props": {"swarm_stream": "completed"},
        }
        assert "Secret" not in json.dumps(calls) and "private" not in json.dumps(calls)
        assert bot.typing.await_count == 2
        assert order == ["POST", "typing", "PUT", "typing", "PUT"]
        assert await store.pool.fetchval("SELECT state FROM channel_streams") == "completed"
        assert (await ledger_snapshot(store, workspace))["totals"]["messages"] == 1
    finally:
        await bot.http.aclose()


async def test_pause_interrupts_stream_without_appending_more_text(runtime_store):
    store, settings, agent, _ = runtime_store
    bot = Client("http://mattermost.invalid", "test")
    bot.typing = AsyncMock()
    bot.request = AsyncMock(return_value={})
    await store.pool.execute(
        "INSERT INTO channel_streams(id,agent_id,call_id,channel_id,baseline,post_id,text) "
        "VALUES($1,$2,'c','general','[]','post','Already shared')",
        uuid4(),
        agent,
    )
    await store.pool.execute("UPDATE agents SET control_paused=true WHERE id=$1", agent)
    try:
        await ChannelStream(store, bot, agent, settings).update(
            [
                {
                    "info": {"id": "new", "role": "assistant"},
                    "parts": [{"id": "p", "type": "text", "text": "Never publish"}],
                }
            ]
        )
        assert bot.request.call_args.kwargs["json"]["message"] == "Already shared"
        assert await store.pool.fetchval("SELECT state FROM channel_streams") == "interrupted"
        bot.typing.assert_not_called()
    finally:
        await bot.http.aclose()


async def test_peer_inbox_ignores_drafts_but_receives_final_update(runtime_store):
    store, settings, agent, _ = runtime_store
    bot = Client("http://mattermost.invalid", "test")
    bridge = Bridge(store, bot, agent, settings)
    bridge.channel_allowed = AsyncMock(return_value=(True, "channel_post"))
    state = await store.state(agent)
    post = {
        "id": "stream",
        "user_id": "peer",
        "channel_id": "general",
        "message": "Hello",
        "create_at": state["created_ms"] + 10,
        "update_at": state["created_ms"] + 20,
        "props": {"swarm_stream": "open"},
    }
    try:
        assert not await bridge.ingest({"event": "posted", "data": {"post": json.dumps(post)}})
        assert await store.unread_count(agent) == 0
        post["props"]["swarm_stream"] = "completed"
        assert await bridge.ingest({"event": "post_edited", "data": {"post": json.dumps(post)}})
        assert await store.unread_count(agent) == 1
    finally:
        await bot.http.aclose()


async def test_work_presence_defaults_away_and_restores_available(runtime_store):
    store, settings, agent, _ = runtime_store
    bot = Client("http://mattermost.invalid", "test")
    bot.presence = AsyncMock()
    runtime = Runtime(store, bot, agent, settings, AsyncMock())
    try:
        await runtime.begin_work()
        assert (await store.state(agent))["availability"] == "away"
        bot.presence.assert_awaited_with("away")
        # Existing set_status tool remains the agent's interruption preference.
        turn = await store.reserve_cost(agent, Decimal(0), [])
        result = await runtime.tools.execute(
            turn,
            {
                "name": "set_status",
                "call_id": "interruptible",
                "arguments": json.dumps({"status": "available"}),
            },
        )
        assert result["availability"] == "available"
        assert (await store.state(agent))["availability"] == "available"
        bot.presence.assert_awaited_with("online")
        await runtime.end_work()
        assert (await store.state(agent))["availability"] == "available"
        bot.presence.assert_awaited_with("online")
    finally:
        await bot.http.aclose()


@pytest.mark.skipif(
    os.environ.get("HARNESS_MM_STREAM_LIVE_TESTS") != "1", reason="isolated Mattermost gate"
)
async def test_real_mattermost_post_edits_and_away_presence(runtime_store, tmp_path):
    from harness.core.reset import delete_external
    from harness.mattermost.provisioning import provision_seed

    store, settings, agent, workspace = runtime_store
    admin = Client(str(settings.mattermost_url), settings.mattermost_admin_token.get_secret_value())
    info = await provision_seed(
        admin,
        workspace,
        uuid4(),
        settings.mattermost_owner_id,
        team_name="stream-test-" + uuid4().hex[:10],
        bot_username="stream-test-" + uuid4().hex[:10],
        channel_name="town-square",
    )
    bot = Client(str(settings.mattermost_url), info["bot_token"])
    received = asyncio.Queue()
    ready_bot, ready_owner = asyncio.Event(), asyncio.Event()

    async def listen(client, ready, capture=False):
        async for event in client.events():
            if event.get("event") == "harness_connected":
                ready.set()
            if capture:
                await received.put(event)

    pumps = [
        asyncio.create_task(listen(bot, ready_bot)),
        asyncio.create_task(listen(admin, ready_owner, True)),
    ]
    channel = info["general_id"]
    try:
        await store.pool.execute(
            "INSERT INTO channel_streams(id,agent_id,call_id,channel_id,baseline) "
            "VALUES($1,$2,'live',$3,'[]')",
            uuid4(),
            agent,
            channel,
        )
        await asyncio.wait_for(asyncio.gather(ready_bot.wait(), ready_owner.wait()), 5)
        await bot.presence("away")
        stream = ChannelStream(store, bot, agent, settings)

        def output(text, ended=False):
            return [
                {
                    "info": {"id": "live", "role": "assistant"},
                    "parts": [
                        {
                            "id": "p",
                            "type": "text",
                            "text": text,
                            "time": {"end": 1} if ended else {},
                        },
                    ],
                }
            ]

        await stream.update(output("**Integration test:** streaming"))
        async with asyncio.timeout(5):
            while True:
                event = await received.get()
                if (
                    event.get("event") == "typing"
                    and event.get("data", {}).get("user_id") == info["bot_user_id"]
                ):
                    assert event["broadcast"]["channel_id"] == channel
                    break
        await stream.update(output("**Integration test:** streaming verified.", True))
        post_id = await store.pool.fetchval("SELECT post_id FROM channel_streams")
        post = await bot.request("GET", f"posts/{post_id}")
        assert post["message"] == "**Integration test:** streaming verified."
        assert post["props"]["swarm_stream"] == "completed"
        user = await bot.me()
        assert (await bot.request("GET", f"users/{user['id']}/status"))["status"] == "away"
        posts = await bot.read_channel(channel)
        assert len([p for p in posts if p.id == post_id]) == 1
    finally:
        for pump in pumps:
            pump.cancel()
        await asyncio.gather(*pumps, return_exceptions=True)
        channels = await admin.request("GET", f"teams/{info['team_id']}/channels")
        cleanup = {
            "workspace_id": str(workspace),
            "team_id": info["team_id"],
            "binding": {"owner_id": settings.mattermost_owner_id},
            "reset": {
                "fresh": True,
                "manifest": {
                    "channels": [c["id"] for c in channels],
                    "bots": [info["bot_user_id"]],
                },
            },
        }
        await delete_external(admin, cleanup, tmp_path / "mm.json")
        await admin.request("DELETE", f"teams/{info['team_id']}")
        await bot.http.aclose()
        await admin.http.aclose()


async def test_begin_signals_public_composition_before_any_text(runtime_store, monkeypatch):
    from types import SimpleNamespace

    store, settings, agent, _ = runtime_store
    await store.pool.execute(
        "INSERT INTO coding_sessions(agent_id,session_id,directory,branch,worker_id) "
        "VALUES($1,'session','directory','branch','worker')",
        agent,
    )
    monkeypatch.setattr(
        "harness.mattermost.streaming.OpenCode",
        lambda _: SimpleNamespace(request=AsyncMock(return_value=[]), close=AsyncMock()),
    )
    bot = Client("http://mm.invalid", "token")
    bot.typing = AsyncMock()
    bot.request = AsyncMock()
    stream = ChannelStream(store, bot, agent, settings)
    try:
        await stream.begin("public-composer", "general", "")
        bot.typing.assert_awaited_once_with("general", "")
        bot.request.assert_not_called()
        assert stream.typing_task is not None
        await stream.interrupt()
        assert stream.typing_task is None
        assert await store.pool.fetchval("SELECT state FROM channel_streams") == "interrupted"
    finally:
        await stream.interrupt()
        await bot.http.aclose()
