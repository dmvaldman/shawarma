"""Opt-in, metered M2 acceptance. Stop the foreground harness before running."""

import asyncio
import os
from uuid import UUID

import pytest

from harness.cli.main import STATE
from harness.config import Settings
from harness.core.runtime import Runtime
from harness.mattermost.client import Client
from harness.models.openai import OpenAIProvider
from harness.persistence.bootstrap import load
from harness.persistence.store import Store

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("HARNESS_OPENAI_LIVE_TESTS") != "1",
        reason="explicit paid M2 acceptance gate",
    ),
]


async def wait_reply(owner, store, agent, post_id, bot_id, expected):
    async with asyncio.timeout(60):
        while True:  # noqa: ASYNC110 - await external provider/Mattermost completion
            state = await store.state(agent)
            if state["state"] in {"FAILED", "QUOTA_BLOCKED"}:
                raise AssertionError("live activation stopped: " + state["state"])
            replies = [p for p in await owner.get_thread(post_id) if p.user_id == bot_id]
            if replies and state["state"] == "IDLE" and await store.unread_count(agent) == 0:
                assert any(expected in p.message for p in replies)
                return
            await asyncio.sleep(0.1)


async def test_openai_tool_round_trip_away_restart_and_recovery():
    settings = Settings()
    bootstrap = load(STATE)
    agent = UUID(bootstrap["seed_id"])
    store = await Store.connect(settings)
    provider = OpenAIProvider(settings)
    task = None
    try:
        await provider.verify_model()
        original = await store.get(agent)
        before = await store.state(agent)
        assert before["workspace_state"] == "active"
        await store.set_availability(agent, "available")
        async with (
            Client(str(settings.mattermost_url), bootstrap["bot_token"]) as bot,
            Client(
                str(settings.mattermost_url), settings.mattermost_admin_token.get_secret_value()
            ) as owner,
        ):
            assert (await owner.me())["id"] == settings.mattermost_owner_id
            runtime = Runtime(store, bot, agent, settings, provider)
            task = asyncio.create_task(runtime.run())
            await asyncio.wait_for(runtime.connected.wait(), 30)
            question = await owner.post_channel(
                bootstrap["general_id"],
                "M2 automated acceptance: calculate 19 × 23 and reply in this thread "
                "with a short calculation.",
            )
            await wait_reply(owner, store, agent, question.id, bootstrap["bot_user_id"], "437")
            assert await store.pool.fetchval(
                "SELECT count(*) FROM tool_calls WHERE agent_id=$1 "
                "AND tool_name='post_channel' AND state='completed'",
                agent,
            )
            await store.set_availability(agent, "away")
            turns = (await store.state(agent))["turns"]
            pending = await owner.post_channel(
                bootstrap["general_id"],
                "M2 automated restart acceptance: calculate 17 + 25 and reply in this thread.",
            )
            async with asyncio.timeout(10):
                while await store.unread_count(agent) < 1:  # noqa: ASYNC110
                    await asyncio.sleep(0.05)
            assert (await store.state(agent))["turns"] == turns
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            task = None
            await store.close()
            store = await Store.connect(settings)
            assert (await store.get(agent)).model_dump() == original.model_dump()
            assert pending.id in {n.post.id for n in (await store.page(agent))[0]}
            runtime = Runtime(store, bot, agent, settings, provider)
            task = asyncio.create_task(runtime.run())
            await asyncio.wait_for(runtime.connected.wait(), 30)
            assert (await store.state(agent))["turns"] == turns
            assert (await store.state(agent))["availability"] == "away"
            await store.set_availability(agent, "available")
            await wait_reply(owner, store, agent, pending.id, bootstrap["bot_user_id"], "42")
            after = await store.state(agent)
            assert after["spent"] > before["spent"]
            assert after["reserved"] == 0
            print("Live M2 conservative spend delta USD:", after["spent"] - before["spent"])
    finally:
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await provider.close()
        await store.close()
