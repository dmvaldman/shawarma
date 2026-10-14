from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from harness.core.names import agent_bio, owned_account
from harness.notifications.bridge import Bridge
from harness.tools.core import TOOLS, CoreTools


def test_profile_is_only_a_short_model_name_and_ownership_uses_ids():
    workspace, agent = uuid4(), uuid4()
    metadata = {"api_id": "claude-haiku-5-5"}
    bio = agent_bio(workspace, agent, "child", "Check proofs and edge cases.", metadata)
    assert bio == "haiku 5.5"
    assert (
        len(agent_bio(workspace, agent, "child", "", {"api_id": "very-long-model-name-1234567890"}))
        <= 20
    )
    account = {"user_id": "bot", "owner_id": "admin", "description": bio}
    assert owned_account(account, "bot", "admin")
    assert not owned_account(account, "other-bot", "admin")
    assert not owned_account(account, "bot", "other-admin")


def test_dm_tools_are_unavailable_but_channel_subscription_remains():
    names = {tool.name for tool in TOOLS}
    assert not {"send_dm", "read_dm"} & names
    assert {"post_channel", "subscribe_channel", "create_channel"} <= names


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["D", "G"])
async def test_dm_channels_cannot_bypass_disabled_tools_or_wake_agents(kind):
    store = SimpleNamespace(state=AsyncMock(return_value={"team_id": "team"}))
    bot = SimpleNamespace(request=AsyncMock(return_value={"type": kind}))
    bridge = Bridge(store, bot, uuid4())
    assert await bridge.channel_allowed("private-chat") == (False, "dm")
    tools = CoreTools.__new__(CoreTools)
    tools.store, tools.bot, tools.agent_id = store, bot, uuid4()
    with pytest.raises(ValueError, match="DIRECT_MESSAGES_DISABLED"):
        await tools.scope("private-chat")


async def test_owner_membership_reconciles_public_and_private_work_channels():
    from harness.core.presentation import join_owner_channels

    def channel(name, kind="O", archived=False, team="team"):
        return {"id": name, "type": kind, "team_id": team, "delete_at": int(archived)}

    store = SimpleNamespace(
        pool=SimpleNamespace(
            fetch=AsyncMock(
                return_value=[
                    {"channel_id": "public"},
                    {"channel_id": "private"},
                    {"channel_id": "dm"},
                ]
            )
        )
    )

    async def request(method, path, **kwargs):
        if path == "teams/team/channels":
            return [channel("public"), channel("archived", archived=True)]
        if path == "channels/private":
            return channel("private", "P")
        if path == "channels/dm":
            return channel("dm", "D", team="")
        return {}

    admin = SimpleNamespace(request=AsyncMock(side_effect=request))
    await join_owner_channels(store, admin, uuid4(), "team", "owner")
    joined = [c.args[1] for c in admin.request.await_args_list if c.args[0] == "POST"]
    assert joined == ["channels/public/members", "channels/private/members"]
    assert all(
        c.kwargs["json"] == {"user_id": "owner"}
        for c in admin.request.await_args_list
        if c.args[0] == "POST"
    )
