import json
import os
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest

from harness.mattermost.client import Client
from harness.tools.core import CoreTools

pytestmark = pytest.mark.skipif(
    os.environ.get("HARNESS_LIVE_TESTS") != "1", reason="live PostgreSQL gate"
)


@pytest.mark.parametrize("visibility,kind", [("public", "O"), ("private", "P")])
async def test_created_channel_joins_owner_and_returns_real_references(
    runtime_store, visibility, kind
):
    store, settings, agent, workspace = runtime_store
    owner = (await store.state(agent))["owner_id"]
    channel = {
        "id": "work",
        "name": "p-vs-np",
        "display_name": "P vs NP investigation",
        "type": kind,
        "team_id": "team",
    }
    async with Client("https://unused.test", "token") as bot:

        async def request(method, path, **kwargs):
            if path == "channels" or path == "channels/work":
                return channel
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
                    {
                        "name": "p-vs-np",
                        "display_name": channel["display_name"],
                        "visibility": visibility,
                    }
                ),
            },
        )
        result = json.loads(
            await store.pool.fetchval("SELECT result FROM tool_calls WHERE call_id='create'")
        )
        assert result["mention"] == "~p-vs-np"
        assert result["url"] == "https://unused.test/my-swarm/channels/p-vs-np"
        assert result["channel_id"] == "work"
        assert any(
            c.args == ("POST", "channels/work/members") and c.kwargs["json"] == {"user_id": owner}
            for c in bot.request.await_args_list
        )


async def test_status_and_listing_expose_readable_references(runtime_store):
    store, settings, agent, workspace = runtime_store
    async with Client("https://unused.test", "token") as bot:

        async def request(method, path, **kwargs):
            if (
                path.startswith("users/")
                and path.endswith("/channels")
                or path == "teams/team/channels"
            ):
                return [
                    {
                        "id": "work",
                        "name": "p-vs-np",
                        "display_name": "P vs NP",
                        "type": "O",
                        "team_id": "team",
                    }
                ]
            if path == "teams/team":
                return {"name": "my-swarm"}
            return {"id": "internal-owner-id", "username": "owner"}

        bot.request = AsyncMock(side_effect=request)
        tools = CoreTools(store, bot, agent, settings)
        turn = await store.reserve_cost(agent, Decimal("0.01"), [])
        for name in ["get_status", "list_channels"]:
            await tools.execute(turn, {"call_id": name, "name": name, "arguments": "{}"})
        status = json.loads(
            await store.pool.fetchval("SELECT result FROM tool_calls WHERE call_id='get_status'")
        )
        assert status["owner_mention"] == "@owner"
        assert status["owner_mattermost_username"] == "owner"
        assert status["turn_limit"] is None
        assert not any("budget" in key for key in status)
        assert "not provider billing" in status["usage_accounting"]
        assert "estimated_usage_usd" in status and "in_flight_reserved_usd" in status
        channels = json.loads(
            await store.pool.fetchval("SELECT result FROM tool_calls WHERE call_id='list_channels'")
        )["channels"]
        assert len(channels) == 1
        assert channels[0]["mention"] == "~p-vs-np"
        assert channels[0]["url"].endswith("/my-swarm/channels/p-vs-np")


async def test_channel_discovery_searches_display_name_purpose_and_all_public_pages(runtime_store):
    store, settings, agent, workspace = runtime_store
    target = {
        "id": "audit",
        "name": "pnp-audit",
        "display_name": "P vs NP audit",
        "purpose": "Check oracle arguments",
        "type": "O",
        "team_id": "team",
    }
    archived = {**target, "id": "old", "delete_at": 1}
    filler = [
        {
            **target,
            "id": f"other-{i}",
            "name": f"other-{i}",
            "display_name": "Other work",
            "purpose": "Unrelated",
        }
        for i in range(200)
    ]
    async with Client("https://unused.test", "token") as bot:

        async def request(method, path, **kwargs):
            if path.startswith("users/") and path.endswith("/channels"):
                return [target, archived]  # Joined/public listings may overlap.
            if path == "teams/team/channels":
                return filler if kwargs["params"]["page"] == 0 else [target, archived]
            if path == "teams/team":
                return {"name": "my-swarm"}
            raise AssertionError(path)

        bot.request = AsyncMock(side_effect=request)
        tools = CoreTools(store, bot, agent, settings)
        turn = await store.reserve_cost(agent, Decimal("0.01"), [])
        for index, query in enumerate(["P vs NP", "ORACLE", "pnp-audit"]):
            call_id = f"discovery-{index}"
            await tools.execute(
                turn,
                {
                    "call_id": call_id,
                    "name": "list_channels",
                    "arguments": json.dumps({"query": query}),
                },
            )
            result = json.loads(
                await store.pool.fetchval("SELECT result FROM tool_calls WHERE call_id=$1", call_id)
            )
            assert [c["channel_id"] for c in result["channels"]] == ["audit"]
            assert result["channels"][0]["purpose"] == "Check oracle arguments"
