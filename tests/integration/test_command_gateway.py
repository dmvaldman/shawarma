"""Live public endpoint checks; no paid inference or destructive slash command is invoked."""

import asyncio
import os
from pathlib import Path

import httpx
import pytest

from harness.api.commands import TRIGGERS
from harness.config import Settings
from harness.mattermost.client import Client, MattermostError
from harness.persistence.bootstrap import load

pytestmark = pytest.mark.skipif(
    os.environ.get("HARNESS_LIVE_TESTS") != "1", reason="live Mattermost gateway gate"
)


async def test_command_lists_and_suggestions_only_expose_swarm_controls():
    settings = Settings()
    team = load(Path(".state/workspace.json"))["team_id"]
    async with Client(
        str(settings.mattermost_url), settings.mattermost_admin_token.get_secret_value()
    ) as admin:
        for endpoint in ("commands", f"teams/{team}/commands/autocomplete"):
            commands = await admin.request("GET", endpoint, params={"team_id": team})
            assert {command["trigger"] for command in commands} == set(TRIGGERS)
        suggestions = await admin.request(
            "GET", f"teams/{team}/commands/autocomplete_suggestions", params={"user_input": "/"}
        )
        assert {s["Complete"] for s in suggestions} == set(TRIGGERS)
        removed = await admin.request(
            "GET",
            f"teams/{team}/commands/autocomplete_suggestions",
            params={"user_input": "/shr"},
        )
        assert removed == []


async def test_execution_blocks_other_commands_and_preserves_mattermost_validation():
    settings = Settings()
    async with Client(
        str(settings.mattermost_url), settings.mattermost_admin_token.get_secret_value()
    ) as admin:
        for command in ("/shrug", "/logout", "/msg owner hi", "/resetting"):
            with pytest.raises(MattermostError, match="swarm.command_not_available"):
                await admin.request("POST", "commands/execute", json={"command": command})
        # Allowed commands reach Mattermost's authentication/channel validation.
        # Deliberately omit channel_id so nothing can execute against the live swarm.
        with pytest.raises(MattermostError) as error:
            await admin.request("POST", "commands/execute", json={"command": "/pause"})
        assert "swarm.command_not_available" not in str(error.value)
        assert (await admin.me())["id"] == settings.mattermost_owner_id
        assert (await admin.request("GET", "system/ping"))["status"] == "OK"
    async with httpx.AsyncClient(base_url=str(settings.mattermost_url)) as unauthenticated:
        response = await unauthenticated.get("api/v4/teams/invalid/commands/autocomplete")
        assert response.status_code == 401
        assert isinstance(response.json(), dict)
        page = await unauthenticated.get("")
        assert page.status_code == 200 and "<html" in page.text


async def test_authenticated_websocket_still_connects_through_gateway():
    settings = Settings()
    async with Client(
        str(settings.mattermost_url), settings.mattermost_admin_token.get_secret_value()
    ) as admin:
        events = admin.events()
        try:
            event = await asyncio.wait_for(anext(events), 5)
            assert event["event"] == "harness_connected"
        finally:
            await events.aclose()
