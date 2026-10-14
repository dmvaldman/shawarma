"""Paid M3 behavior gate against a running supervisor on the configured workspace."""

import asyncio
import os
from uuid import UUID

import pytest

from harness.cli.main import STATE
from harness.config import Settings
from harness.core.coordination import Coordination
from harness.mattermost.client import Client
from harness.persistence.bootstrap import load
from harness.persistence.store import Store

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("HARNESS_SWARM_LIVE_TESTS") != "1", reason="paid running-supervisor M3 gate"
    ),
]


async def test_seed_chooses_delegation_children_post_and_native_dms():
    settings = Settings()
    state = load(STATE)
    seed = UUID(state["seed_id"])
    store = await Store.connect(settings)
    try:
        async with Client(
            str(settings.mattermost_url), settings.mattermost_admin_token.get_secret_value()
        ) as owner:
            assert (await owner.me())["id"] == settings.mattermost_owner_id
            await owner.post_channel(
                state["general_id"],
                (
                    "Develop a robust pure-Python integer square root for arbitrary nonnegative "
                    "integers. Compare binary search and Newton approaches, "
                    "independently scrutinize "
                    "the correctness proofs and edge cases (0, 1, perfect squares and enormous "
                    "integers), and synthesize an implementation with a useful test plan. "
                    "Be explicit about what has actually been executed. Keep detailed review in "
                    "focused channels or threads and put the synthesis in #town-square."
                ),
            )
            async with asyncio.timeout(150):
                while True:  # noqa: ASYNC110 - observe external agent behavior
                    children = await store.pool.fetch(
                        "SELECT id,identity,credential FROM agents WHERE parent_id=$1", seed
                    )
                    if children:
                        break
                    await asyncio.sleep(0.5)
            child = children[0]["id"]
            identity = await store.get(child)
            assert identity.parent_id == seed
            assert "Core coordination tools are installed" in identity.system_prompt
            assert (
                await store.pool.fetchval(
                    "SELECT mode FROM subscriptions WHERE agent_id=$1 AND channel_id=$2",
                    child,
                    state["general_id"],
                )
                == "all"
            )
            async with asyncio.timeout(120):
                while not await store.pool.fetchval(  # noqa: ASYNC110
                    "SELECT count(*) FROM tool_calls WHERE agent_id=$1 "
                    "AND tool_name='post_channel' AND state='completed'",
                    child,
                ):
                    await asyncio.sleep(0.5)
            coordinator = Coordination(store, settings)
            with pytest.raises(ValueError, match="NOT_DESCENDANT"):
                await coordinator.terminate(child, seed, "M3 unauthorized termination test")
            assert (await store.state(seed))["state"] != "TERMINATED"
    finally:
        await store.close()
