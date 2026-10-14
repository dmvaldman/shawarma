"""Run only on a disposable local stack. Posts are sent as the configured human owner."""

import asyncio
import json
import os
from contextlib import asynccontextmanager
from uuid import uuid4

import pytest
from websockets.asyncio.client import connect

from harness.config import Settings
from harness.mattermost.client import Client
from harness.mattermost.smoke import OwnerPostGate
from harness.persistence.bootstrap import load

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("HARNESS_LIVE_TESTS") != "1",
        reason="set HARNESS_LIVE_TESTS=1 for the local acceptance gate",
    ),
]


async def wait_post(stream, post_id):
    async with asyncio.timeout(30):
        async for event in stream:
            if event.get("event") == "posted":
                post = json.loads(event["data"]["post"])
                if post["id"] == post_id:
                    return event
    raise AssertionError("stream ended without target post")


async def test_one_seed_live_round_trip_and_reconnect(monkeypatch):
    from harness.cli.main import STATE, create

    sockets = []

    @asynccontextmanager
    async def tracked_connect(*args, **kwargs):
        async with connect(*args, **kwargs) as socket:
            sockets.append(socket)
            yield socket

    monkeypatch.setattr("harness.mattermost.client.connect", tracked_connect)
    settings = Settings()
    before = load(STATE)
    setup = await create(settings)
    assert before["seed_id"] == setup["seed_id"]
    assert before["bot_user_id"] == setup["bot_user_id"]
    owner_token = os.environ.get("HARNESS_TEST_OWNER_TOKEN", "")
    assert owner_token, "HARNESS_TEST_OWNER_TOKEN must authenticate the configured human owner"
    async with (
        Client(str(settings.mattermost_url), setup["bot_token"]) as bot,
        Client(str(settings.mattermost_url), owner_token) as owner,
        Client(
            str(settings.mattermost_url), settings.mattermost_admin_token.get_secret_value()
        ) as admin,
    ):
        assert (await owner.me())["id"] == settings.mattermost_owner_id
        assert (await bot.me())["id"] == setup["bot_user_id"]
        ping = await admin.request("GET", "system/ping")
        print(f"Installed Mattermost ping: {ping}")
        bots = await admin.request("GET", "bots", params={"per_page": 100})
        workspace_bots = [
            b for b in bots if f"workspace {setup['workspace_id']}" in b.get("description", "")
        ]
        assert len(workspace_bots) == 1
        members = await admin.request(
            "GET", f"channels/{setup['general_id']}/members", params={"page": 0, "per_page": 200}
        )
        assert {member["user_id"] for member in members} == {
            settings.mattermost_owner_id,
            setup["bot_user_id"],
        }
        marker = "harnessaccept" + uuid4().hex[:10]
        stream = bot.events()
        gate = OwnerPostGate(settings.mattermost_owner_id, setup["general_id"])
        try:
            connected = await anext(stream)
            assert connected["event"] == "harness_connected"
            question = await owner.post_channel(setup["general_id"], marker)
            event = await wait_post(stream, question.id)
            post = gate.accept(event)
            assert post is not None
            reply = await bot.post_channel(setup["general_id"], "M1 receipt verified", question.id)
            assert reply.user_id == setup["bot_user_id"]
            assert reply.root_id == question.id
            gate.acknowledge(post)
            assert gate.accept(event) is None
            await bot.add_reaction(question.id, "thumbsup")
            reactions = await bot.request("GET", f"posts/{question.id}/reactions")
            assert any(r["user_id"] == setup["bot_user_id"] for r in reactions)
            await bot.remove_reaction(question.id, "thumbsup")
            assert reply.id in {p.id for p in await bot.get_thread(question.id)}
            assert question.id in {p.id for p in await bot.read_channel(setup["general_id"])}
            async with asyncio.timeout(30):
                while question.id not in {  # noqa: ASYNC110 - poll server search indexing
                    p.id for p in await bot.search_messages(setup["team_id"], marker)
                }:
                    await asyncio.sleep(0.5)
            channel_id = await bot.create_channel(
                setup["team_id"], "check-" + uuid4().hex[:10], "M1 check"
            )
            channel_post = await bot.post_channel(channel_id, marker)
            assert channel_post.user_id == setup["bot_user_id"]
            dm = await bot.send_dm(settings.mattermost_owner_id, "M1 DM check")
            assert dm.user_id == setup["bot_user_id"]
            assert dm.id in {p.id for p in await owner.read_channel(dm.channel_id)}
            # Force a real transport close and let Client.events reconnect itself.
            await sockets[-1].close()
            async with asyncio.timeout(30):
                while (await anext(stream))["event"] != "harness_connected":
                    pass
            assert len(sockets) == 2
            second = await owner.post_channel(setup["general_id"], marker + " reconnect")
            assert gate.accept(await wait_post(stream, second.id)) is not None
        finally:
            await stream.aclose()


async def test_cli_listener_replies_once_and_shuts_down_cleanly():
    import signal
    import sys

    from harness.cli.main import STATE

    settings = Settings()
    state = load(STATE)
    owner_token = os.environ.get("HARNESS_TEST_OWNER_TOKEN", "")
    assert owner_token, "configure the live human owner token"
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "harness.cli.main",
        "listen",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        assert process.stdout is not None
        connected = await asyncio.wait_for(process.stdout.readline(), 30)
        assert b"Connected" in connected
        async with (
            Client(str(settings.mattermost_url), owner_token) as owner,
            Client(str(settings.mattermost_url), state["bot_token"]) as bot,
        ):
            question = await owner.post_channel(
                state["general_id"], "CLI acceptance " + uuid4().hex
            )
            async with asyncio.timeout(10):
                while True:  # noqa: ASYNC110 - observe the external CLI's response
                    replies = [
                        post
                        for post in await bot.get_thread(question.id)
                        if post.user_id == state["bot_user_id"]
                    ]
                    if replies:
                        break
                    await asyncio.sleep(0.1)
            await asyncio.sleep(0.2)
            replies = [
                post
                for post in await bot.get_thread(question.id)
                if post.user_id == state["bot_user_id"]
            ]
            assert len(replies) == 1
            assert "M1 communication check" in replies[0].message
        process.send_signal(signal.SIGINT)
        output, errors = await asyncio.wait_for(process.communicate(), 10)
        assert process.returncode == 0, errors.decode()
        assert b"Listener stopped" in output
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
