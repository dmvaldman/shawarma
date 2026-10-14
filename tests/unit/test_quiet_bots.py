from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from harness.mattermost.client import MattermostError
from harness.mattermost.quiet_bots import quiet_bot


class Provisioner:
    def __init__(self):
        self.user = None
        self.bot = None
        self.created = 0
        self.fail_conversion_once = False
        self.client = AsyncMock()
        self.client.me.return_value = {"id": "owner"}
        self.client.request.side_effect = self.request

    async def request(self, method, path, **kwargs):
        if path.startswith("users/username/"):
            if self.user is None:
                raise MattermostError(404, "missing")
            return self.user
        if path == "users":
            self.created += 1
            self.user = {"id": "bot", "is_bot": False, **kwargs["json"]}
            assert self.user["disable_welcome_email"]
            return self.user
        if path == "users/bot/convert_to_bot":
            self.user["is_bot"] = True
            self.bot = {"user_id": "bot", "owner_id": "bot"}
            if self.fail_conversion_once:
                self.fail_conversion_once = False
                raise MattermostError(500, "interrupted_after_conversion")
            return self.bot
        if path == "bots/bot/assign/owner":
            self.bot["owner_id"] = "owner"
            return self.bot
        if path == "bots/bot":
            if method == "PUT":
                self.bot.update(kwargs["json"])
                self.user["email"] = self.user["username"] + "@localhost"
            return self.bot
        raise AssertionError(path)


async def test_quiet_creation_and_partial_conversion_recovery():
    server = Provisioner()
    workspace, agent = uuid4(), uuid4()
    server.fail_conversion_once = True
    with pytest.raises(MattermostError):
        await quiet_bot(server.client, workspace, agent, "worker", "worker", "haiku 5.5")
    bot = await quiet_bot(server.client, workspace, agent, "worker", "worker", "haiku 5.5")
    assert bot["owner_id"] == "owner" and bot["description"] == "haiku 5.5"
    again = await quiet_bot(server.client, workspace, agent, "worker", "worker", "haiku 5.5")
    assert again["user_id"] == bot["user_id"]
    assert server.created == 1
    assert (
        sum(
            c.args == ("POST", "users/bot/convert_to_bot")
            for c in server.client.request.await_args_list
        )
        == 1
    )
    assert not any(c.args == ("POST", "bots") for c in server.client.request.await_args_list)


async def test_coincidentally_named_human_is_never_converted():
    server = Provisioner()
    server.user = {"id": "human", "is_bot": False, "email": "human@example.com"}
    with pytest.raises(ValueError, match="QUIET_BOT_IDENTITY_MISMATCH"):
        await quiet_bot(server.client, uuid4(), uuid4(), "worker", "worker", "test")
    assert all(call.args[0] == "GET" for call in server.client.request.await_args_list)


async def test_foreign_bot_cannot_be_assigned_to_owner():
    server = Provisioner()
    workspace, agent = uuid4(), uuid4()
    await quiet_bot(server.client, workspace, agent, "worker", "worker", "test")
    server.bot["owner_id"] = "foreign"
    server.client.request.reset_mock()
    with pytest.raises(ValueError, match="BOT_OWNERSHIP_MISMATCH"):
        await quiet_bot(server.client, workspace, agent, "worker", "worker", "test")
    assert all(call.args[0] == "GET" for call in server.client.request.await_args_list)
