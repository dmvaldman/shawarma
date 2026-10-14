import asyncio
import json
import os
import time
from uuid import UUID

import pytest
from test_coordination import ARGS, FakeAdmin
from test_runtime import BlockingProvider

from harness.core.coordination import Coordination
from harness.core.runtime import Runtime
from harness.core.types import Post
from harness.mattermost.client import Client

pytestmark = pytest.mark.skipif(
    os.environ.get("HARNESS_LIVE_TESTS") != "1", reason="live PostgreSQL gate"
)


async def test_seed_is_immediate_even_when_running_and_idle_child_is_not_woken(
    runtime_store, monkeypatch
):
    store, settings, parent, workspace = runtime_store
    monkeypatch.setattr("harness.core.coordination.Client", FakeAdmin)
    child = UUID((await Coordination(store, settings).spawn(parent, "child", ARGS))["agent_id"])
    await store.pool.execute("UPDATE agents SET state='RUNNING' WHERE id=$1", parent)
    await store.pool.execute("UPDATE agents SET state='IDLE' WHERE id=$1", child)
    owner = (await store.state(parent))["owner_id"]
    post = Post(
        id="owner-message",
        user_id=owner,
        channel_id="general",
        message="Question",
        create_at=int(time.time() * 1000) + 1,
    )
    await asyncio.gather(*(store.ingest(a, post, "channel_post", "s", 20) for a in [parent, child]))
    rows = await store.pool.fetch(
        "SELECT agent_id,immediate_delivery FROM notifications WHERE post_id=$1", post.id
    )
    assert [r["agent_id"] for r in rows if r["immediate_delivery"]] == [parent]
    assert (await store.pending_delivery(parent))[0] == 0
    assert await store.reserve(child, 10) == []
    assert not await store.ingest(child, post, "channel_post", "s", 20)
    assert await store.pool.fetchval("SELECT count(*) FROM first_deliveries") == 1


async def test_direct_mention_selects_recipient_and_blocked_agents_are_skipped(
    runtime_store, monkeypatch
):
    store, settings, parent, workspace = runtime_store
    monkeypatch.setattr("harness.core.coordination.Client", FakeAdmin)
    child = UUID((await Coordination(store, settings).spawn(parent, "child", ARGS))["agent_id"])
    profile = await store.public_profile(child)
    owner = (await store.state(parent))["owner_id"]
    post = Post(
        id="directed",
        user_id=owner,
        channel_id="general",
        message="@" + profile["username"] + " please answer",
        create_at=int(time.time() * 1000) + 1,
    )
    await store.ingest(parent, post, "channel_post", "s", 20)
    await store.ingest(child, post, "channel_post", "s", 20)
    assert await store.immediate_pending(child)
    await store.pool.execute("UPDATE agents SET state='QUOTA_BLOCKED' WHERE id=$1", child)
    other = post.model_copy(update={"id": "next", "message": "Next question"})
    await store.ingest(parent, other, "channel_post", "s", 20)
    assert (
        await store.pool.fetchval("SELECT agent_id FROM first_deliveries WHERE post_id='next'")
        == parent
    )
    peer = post.model_copy(update={"id": "peer", "user_id": "peer"})
    await store.ingest(parent, peer, "channel_post", "s", 20)
    assert not await store.pool.fetchval("SELECT 1 FROM first_deliveries WHERE post_id='peer'")


async def test_immediate_message_interrupts_delivery_timer_and_bypasses_typing_wait(runtime_store):
    store, settings, agent, workspace = runtime_store
    settings = settings.model_copy(update={"typing_yield_max_sec": 20})
    await store.pool.execute("UPDATE workspaces SET started=true WHERE id=$1", workspace)
    provider = BlockingProvider()
    async with Client("https://unused.test", "token") as bot:
        bot.typing_until[("general", "peer")] = asyncio.get_running_loop().time() + 30
        runtime = Runtime(store, bot, agent, settings, provider)
        slow = Post(
            id="older",
            user_id="peer",
            channel_id="general",
            message="Earlier context",
            create_at=1000,
        )
        await store.ingest(agent, slow, "channel_post", "s", 30)
        await runtime.wake(agent)
        assert runtime.waiting_delivery
        owner = (await store.state(agent))["owner_id"]
        fast = slow.model_copy(
            update={"id": "owner", "user_id": owner, "message": "Please respond", "create_at": 2000}
        )
        await store.ingest(agent, fast, "channel_post", "s", 20)
        await runtime.wake(agent)
        await asyncio.wait_for(provider.entered.wait(), 1)
        context = json.dumps(provider.requests[0])
        assert context.index("Earlier context") < context.index("Please respond")
        assert provider.calls == 1 and not runtime.waiting_delivery
        provider.release.set()
        await asyncio.wait_for(runtime.task, 2)
        runtime.stopping.set()
