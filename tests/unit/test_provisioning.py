import json
from uuid import uuid4

import httpx
import pytest

from harness.mattermost.client import Client, MattermostError
from harness.mattermost.provisioning import provision_seed


class Server:
    def __init__(self, fail_join=False, general_exists=True):
        self.teams = {}
        self.bots = {}
        self.members = set()
        self.tokens = []
        self.created_bots = 0
        self.disabled = []
        self.fail_join = fail_join
        self.general_exists = general_exists

    def __call__(self, request):
        path = request.url.path.removeprefix("/api/v4/")
        body = json.loads(request.content) if request.content else {}
        method = request.method
        status = 200
        if path == "users/me":
            data = {
                "id": "bot" if request.headers["Authorization"] == "Bearer bot-token" else "admin",
                "roles": "system_admin",
            }
        elif path == "users/admin/channels":
            data = []
        elif path == "users/owner":
            data = {"id": "owner", "is_bot": False, "delete_at": 0}
        elif path.startswith("teams/name/"):
            data = self.teams.get(path.split("/")[-1])
        elif path == "teams" and method == "POST":
            data = {"id": "team", **body}
            self.teams[body["name"]] = data
        elif path == "teams/team/channels/name/errors":
            data = {"id": "errors", "name": "errors", "type": "O", "team_id": "team"}
        elif path == "teams/team/channels/name/off-topic":
            data = {"id": "off-topic"}
        elif path == "teams/team/channels/name/general":
            data = {"id": "general"} if self.general_exists else None
        elif path == "channels" and method == "POST":
            assert body["name"] == "general"
            self.general_exists = True
            data = {"id": "general", **body}
        elif path == "users/bot":
            data = next(iter(self.bots.values()))
        elif path.startswith("users/username/"):
            data = self.bots.get(path.split("/")[-1])
        elif path == "users" and method == "POST":
            data = {"id": "bot", "is_bot": False, **body}
            self.bots[body["username"]] = data
        elif path == "users/bot/convert_to_bot":
            self.created_bots += 1
            data = next(iter(self.bots.values()))
            data.update(is_bot=True, user_id="bot", owner_id="bot")
        elif path == "bots/bot/assign/admin":
            data = next(iter(self.bots.values()))
            data["owner_id"] = "admin"
        elif path == "bots" and method == "POST":
            self.created_bots += 1
            data = {"id": "bot", "user_id": "bot", **body}
            self.bots[body["username"]] = data
        elif path == "bots/bot":
            data = next(iter(self.bots.values()))
            if method == "PUT":
                data.update(body)
        elif path == "bots/bot/disable":
            self.disabled.append("bot")
            data = {"status": "OK"}
        elif path.endswith("members/bot") and self.fail_join and path.startswith("channels/"):
            status, data = 403, {"id": "permission_denied"}
        elif "/members/" in path:
            data = {"user_id": path.split("/")[-1]} if path in self.members else None
        elif path.endswith("members") and method == "POST":
            self.members.add(path + "/" + body["user_id"])
            data = body
        elif path == "users/bot/tokens":
            if method == "GET":
                data = self.tokens
            else:
                data = {"id": "token-id", "token": "bot-token", **body}
                self.tokens.append(data)
        elif path == "users/tokens/revoke":
            self.tokens = [token for token in self.tokens if token["id"] != body["token_id"]]
            data = {"status": "OK"}
        else:
            raise AssertionError(f"Unexpected request {method} {path}")
        if data is None:
            status, data = 404, {"id": "not_found"}
        return httpx.Response(status, json=data)


async def test_setup_creates_exactly_one_seed_and_retry_reuses_it():
    server = Server()
    workspace, seed = uuid4(), uuid4()
    async with Client("https://mm.test", "admin", transport=httpx.MockTransport(server)) as admin:
        first = await provision_seed(admin, workspace, seed, "owner")
        second = await provision_seed(admin, workspace, seed, "owner", first["bot_token"])
        assert first["errors_id"] == second["errors_id"] == "errors"
        assert "channels/errors/members/owner" in server.members
        assert "channels/errors/members/bot" not in server.members
        assert first == second
        assert server.created_bots == 1
        assert len(server.tokens) == 1
        assert "channels/general/members/bot" in server.members
        assert "channels/off-topic/members/bot" in server.members
        assert first["off_topic_id"] == "off-topic"
        assert "channels/general/members/owner" in server.members


async def test_provisioning_failure_disables_partial_seed():
    server = Server(fail_join=True)
    async with Client("https://mm.test", "admin", transport=httpx.MockTransport(server)) as admin:
        with pytest.raises(MattermostError):
            await provision_seed(admin, uuid4(), uuid4(), "owner")
    assert server.created_bots == 1
    assert server.disabled == ["bot"]
    assert server.tokens == []


async def test_retry_without_saved_token_revokes_orphan_token():
    server = Server()
    workspace, seed = uuid4(), uuid4()
    async with Client("https://mm.test", "admin", transport=httpx.MockTransport(server)) as admin:
        await provision_seed(admin, workspace, seed, "owner")
        await provision_seed(admin, workspace, seed, "owner")
    assert server.created_bots == 1
    assert len(server.tokens) == 1


async def test_cli_retry_preserves_identity_prompt_and_private_checkpoint(tmp_path, monkeypatch):
    from harness.cli import main as cli
    from harness.config import Settings
    from harness.persistence.bootstrap import load

    server = Server()

    def client(url, token):
        return Client(url, token, transport=httpx.MockTransport(server))

    monkeypatch.setattr(cli, "Client", client)
    settings = Settings(
        _env_file=None,
        mattermost_url="https://mm.test",
        database_url="postgresql+asyncpg://db/harness",
        mattermost_admin_token="admin",
        mattermost_owner_id="owner",
    )
    path = tmp_path / "workspace.json"
    first = await cli.create(settings, path)

    def no_new_prompt(**kwargs):
        raise AssertionError("must not re-render an existing system prompt")

    monkeypatch.setattr(cli, "birth_record", no_new_prompt)
    second = await cli.create(settings, path)
    assert second == first == load(path)
    assert server.created_bots == 1
    assert second["general_subscription"] == "all"
    assert second["identity"]["parent_id"] is None
    assert "bot-token" not in str(cli.public_state(second))
    settings.mattermost_owner_id = "other-owner"
    with pytest.raises(ValueError, match="different server or owner"):
        await cli.create(settings, path)


async def test_general_is_created_when_mattermost_does_not_supply_it():
    server = Server(general_exists=False)
    async with Client("https://mm.test", "admin", transport=httpx.MockTransport(server)) as admin:
        setup = await provision_seed(admin, uuid4(), uuid4(), "owner")
    assert setup["general_id"] == "general"
    assert server.general_exists
    assert server.created_bots == 1
