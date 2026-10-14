import json
import os
from decimal import Decimal
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from test_coordination import ARGS, FakeAdmin

from harness.core.coordination import Coordination
from harness.core.types import Post
from harness.mattermost.client import Client, MattermostError
from harness.tools.core import CoreTools

pytestmark = pytest.mark.skipif(os.environ.get("HARNESS_LIVE_TESTS") != "1", reason="live gate")


async def test_town_square_seed_intake_mentions_broadcasts_and_edit_amendments(
    runtime_store, monkeypatch
):
    store, settings, seed, workspace = runtime_store
    monkeypatch.setattr("harness.core.coordination.Client", FakeAdmin)
    child = UUID((await Coordination(store, settings).spawn(seed, "child", ARGS))["agent_id"])
    username = (await store.public_profile(child))["username"]
    owner = (await store.state(seed))["owner_id"]
    base = Post(
        id="ordinary",
        user_id=owner,
        channel_id="general",
        message="New task",
        create_at=1_999_999_999_999,
    )
    assert not await store.ingest(child, base, "channel_post", "s")
    assert await store.ingest(seed, base, "channel_post", "s")
    for index, text in enumerate(
        ["@all important", "@channel important", "@here important", "@" + username + " question"]
    ):
        post = base.model_copy(update={"id": f"direct-{index}", "message": text})
        assert await store.ingest(child, post, "channel_post", "s")
    for index, text in enumerate(
        ["@" + username + "2 question", "email@" + username, "@alliance question"]
    ):
        assert not await store.ingest(
            child,
            base.model_copy(update={"id": f"not-{index}", "message": text}),
            "channel_post",
            "s",
        )
    edited = base.model_copy(
        update={
            "id": "direct-3",
            "message": "revised without mention",
            "update_at": 2_000_000_000_001,
        }
    )
    assert await store.ingest(child, edited, "post_edited", "s")
    work = base.model_copy(update={"id": "work", "channel_id": "investigation"})
    assert await store.ingest(child, work, "channel_post", "s")
    await store.pool.execute("UPDATE agents SET availability='away' WHERE id=$1", seed)
    pending = base.model_copy(update={"id": "seed-away"})
    assert await store.ingest(seed, pending, "channel_post", "s")
    assert not await store.ingest(child, pending, "channel_post", "s")
    assert (
        await store.pool.fetchval("SELECT agent_id FROM first_deliveries WHERE post_id='seed-away'")
        is None
    )


async def test_spawn_membership_subscriptions_and_initial_context_precede_activation(
    runtime_store, monkeypatch
):
    store, settings, seed, workspace = runtime_store
    memberships = []

    class Admin(FakeAdmin):
        async def request(self, method, path, **kwargs):
            if method == "GET" and "/members/" in path:
                raise MattermostError(404, "not_joined")
            if method == "POST" and path.startswith("channels/") and path.endswith("/members"):
                memberships.append((path.split("/")[1], kwargs["json"]["user_id"]))
                # Membership precedes the activated harness identity.
                assert not await store.pool.fetchval(
                    "SELECT 1 FROM agents WHERE identity->>'mattermost_user_id'=$1",
                    kwargs["json"]["user_id"],
                )
            return await super().request(method, path, **kwargs)

    monkeypatch.setattr("harness.core.coordination.Client", Admin)
    await store.pool.execute("INSERT INTO subscriptions VALUES($1,'research','all')", seed)
    coordinator = Coordination(store, settings)
    child = UUID((await coordinator.spawn(seed, "child", ARGS))["agent_id"])
    assert ("research", (await store.get(child)).mattermost_user_id) in memberships
    assert (
        await store.pool.fetchval(
            "SELECT mode FROM subscriptions WHERE agent_id=$1 AND channel_id='research'", child
        )
        == "all"
    )
    branch = UUID(
        (
            await coordinator.spawn(
                child, "branch", {**ARGS, "subscriptions": ["research-circuits"]}
            )
        )["agent_id"]
    )
    grandchild = UUID((await coordinator.spawn(branch, "grandchild", ARGS))["agent_id"])
    for agent in (branch, grandchild):
        channels = {
            s["channel_id"] for s in (await coordinator.info(agent, workspace))["subscriptions"]
        }
        assert channels == {"general", "off-topic", "research-circuits"}
        assert "research-circuits" in json.dumps(await store.history(agent))
    focused = UUID(
        (await coordinator.spawn(seed, "empty", {**ARGS, "subscriptions": []}))["agent_id"]
    )
    assert {
        s["channel_id"] for s in (await coordinator.info(focused, workspace))["subscriptions"]
    } == {"general", "off-topic"}


async def test_invalid_initial_channel_fails_before_bot_creation(runtime_store, monkeypatch):
    store, settings, seed, workspace = runtime_store

    class Admin(FakeAdmin):
        async def request(self, method, path, **kwargs):
            if path == "channels/foreign":
                return {"team_id": "foreign", "type": "O"}
            assert path != "users", "No staged account should be created for invalid channels"
            return await super().request(method, path, **kwargs)

    monkeypatch.setattr("harness.core.coordination.Client", Admin)
    with pytest.raises(ValueError, match="INITIAL_CHANNEL"):
        await Coordination(store, settings).spawn(
            seed, "invalid", {**ARGS, "subscriptions": ["foreign"]}
        )
    assert len(await store.list_agents(workspace)) == 1


@pytest.mark.parametrize(
    "parent_name,suffix,expected",
    [
        ("p-vs-np", "circuits", "p-vs-np-circuits"),
        ("p-vs-np-circuits", "depth", "p-vs-np-circuits-depth"),
        ("p-vs-np", "p-vs-np-audit", "p-vs-np-audit"),
    ],
)
async def test_branch_channel_preserves_parent_prefix_and_owner_membership(
    runtime_store, parent_name, suffix, expected
):
    store, settings, agent, workspace = runtime_store
    async with Client("https://unused.test", "token") as bot:
        created = {}

        async def request(method, path, **kwargs):
            if path == "channels/parent":
                return {"id": "parent", "team_id": "team", "type": "O", "name": parent_name}
            if path == "channels" and method == "POST":
                created.update({"id": "branch", **kwargs["json"]})
                return created
            if path == "channels/branch":
                return created
            if path == "teams/team":
                return {"name": "my-swarm"}
            return {}

        bot.request = AsyncMock(side_effect=request)
        tools = CoreTools(store, bot, agent, settings)
        turn = await store.reserve_cost(agent, Decimal("0.01"), [])
        await tools.execute(
            turn,
            {
                "call_id": "create",
                "name": "create_channel",
                "arguments": json.dumps(
                    {"parent_channel_id": "parent", "name": suffix, "display_name": "Branch"}
                ),
            },
        )
        result = json.loads(
            await store.pool.fetchval("SELECT result FROM tool_calls WHERE call_id='create'")
        )
        assert result["mention"] == "~" + expected
        assert created["name"] == expected
        assert (
            await store.pool.fetchval(
                "SELECT mode FROM subscriptions WHERE agent_id=$1 AND channel_id='branch'", agent
            )
            == "all"
        )
        owner = (await store.state(agent))["owner_id"]
        assert any(
            c.args == ("POST", "channels/branch/members") and c.kwargs["json"]["user_id"] == owner
            for c in bot.request.await_args_list
        )


async def test_peer_invitation_crosses_ancestry_preserves_channels_and_returns_mention(
    runtime_store, monkeypatch
):
    store, settings, seed, workspace = runtime_store
    monkeypatch.setattr("harness.core.coordination.Client", FakeAdmin)
    coordinator = Coordination(store, settings)
    inviter = UUID((await coordinator.spawn(seed, "inviter", ARGS))["agent_id"])
    peer = UUID((await coordinator.spawn(seed, "peer", ARGS))["agent_id"])
    await store.pool.execute("INSERT INTO subscriptions VALUES($1,'original','all')", peer)
    async with Client("https://unused.test", "token") as bot:
        bot.request = AsyncMock(return_value={"id": "work", "team_id": "team", "type": "O"})
        tools = CoreTools(store, bot, inviter, settings)
        turn = await store.reserve_cost(inviter, Decimal("0.01"), [])
        for call_id, mode in (("invite", "all"), ("repeat", "mentions")):
            await tools.execute(
                turn,
                {
                    "call_id": call_id,
                    "name": "subscribe_channel",
                    "arguments": json.dumps(
                        {"channel_id": "work", "agent_id": str(peer), "mode": mode}
                    ),
                },
            )
            result = json.loads(
                await store.pool.fetchval("SELECT result FROM tool_calls WHERE call_id=$1", call_id)
            )
            assert result["agent_id"] == str(peer)
            assert result["mention"] == "@" + (await store.public_profile(peer))["username"]
            assert result["mode"] == "all"
        peer_user_id = (await store.get(peer)).mattermost_user_id
        assert any(
            call.args == ("POST", "channels/work/members")
            and call.kwargs["json"]["user_id"] == peer_user_id
            for call in bot.request.await_args_list
        )
        assert {
            row["channel_id"] for row in (await coordinator.info(peer, workspace))["subscriptions"]
        } == {"general", "off-topic", "original", "work"}
        assert not await store.pool.fetchval(
            "SELECT 1 FROM subscriptions WHERE agent_id=$1 AND channel_id='work'", inviter
        )
        with pytest.raises(ValueError, match="PEER_UNSUBSCRIBE"):
            await tools.subscribe("work", "none", await store.state(inviter), peer)
        await store.pool.execute("UPDATE agents SET state='TERMINATED' WHERE id=$1", peer)
        bot.request.reset_mock()
        with pytest.raises(ValueError, match="AGENT_TERMINATED"):
            await tools.subscribe("work", "all", await store.state(inviter), peer)
        bot.request.assert_not_called()
