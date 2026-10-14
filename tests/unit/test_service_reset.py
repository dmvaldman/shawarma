import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest

from harness.coding.clients import CodingError
from harness.coding.reset import prepare, reset_gitea, reset_opencode
from harness.config import Settings
from harness.core.service_reset import reset_services
from harness.persistence.bootstrap import load


@pytest.fixture(autouse=True)
def reset_credentials(monkeypatch):
    monkeypatch.setattr("harness.coding.reset.creation_auth", lambda _: ("swarm", "test"))


def settings():
    return Settings(
        _env_file=None,
        mattermost_url="http://mattermost.invalid",
        database_url="postgresql://unused",
        coding_enabled=True,
        opencode_password="test",
        gitea_token="test-token",
    )


def state():
    return {
        "workspace_id": str(uuid4()),
        "reset": {
            "seed_id": str(uuid4()),
            "manifest": {},
            "coding_reset": {"enabled": True, "target": {}},
        },
    }


async def test_services_resume_after_failure_without_rewiping_completed_services(
    tmp_path, monkeypatch
):
    checkpoint = tmp_path / "state.json"
    data = state()
    prepare_mock = AsyncMock()
    opencode, gitea, mattermost = AsyncMock(), AsyncMock(), AsyncMock()
    gitea.side_effect = [CodingError("Gitea unavailable"), None]
    for name, callback in (
        ("prepare", prepare_mock),
        ("reset_opencode", opencode),
        ("reset_gitea", gitea),
        ("reset_mattermost", mattermost),
    ):
        monkeypatch.setattr("harness.core.service_reset." + name, callback)
    with pytest.raises(CodingError):
        await reset_services(AsyncMock(), settings(), data, checkpoint)
    assert load(checkpoint)["reset"]["services"] == {"opencode": "completed"}
    mattermost.assert_not_called()
    await reset_services(AsyncMock(), settings(), load(checkpoint), checkpoint)
    opencode.assert_awaited_once()
    assert gitea.await_count == 2
    mattermost.assert_awaited_once()
    assert set(load(checkpoint)["reset"]["services"].values()) == {"completed"}


async def test_prepare_rejects_shared_worker_and_changed_targets(tmp_path):
    store = SimpleNamespace(pool=SimpleNamespace(fetchval=AsyncMock(side_effect=[True, True])))
    data = state()
    data["reset"].pop("coding_reset")
    with pytest.raises(CodingError, match="SHARED_CODING"):
        await prepare(store, settings(), data, tmp_path / "state.json")
    assert "coding_reset" not in data["reset"]
    store.pool.fetchval.side_effect = [True, False]
    await prepare(store, settings(), data, tmp_path / "state.json")
    store.pool.fetchval.side_effect = None
    store.pool.fetchval.return_value = True
    with pytest.raises(CodingError, match="TARGET_CHANGED"):
        await prepare(
            store,
            settings().model_copy(update={"gitea_repo": "other"}),
            data,
            tmp_path / "state.json",
        )


async def test_repository_create_unknown_outcome_reuses_generation_without_second_delete(
    tmp_path, monkeypatch
):
    data = state()
    checkpoint = tmp_path / "state.json"
    original = {"id": 1, "full_name": "swarm/workspace", "description": "Old"}
    fresh = {
        "id": 2,
        "full_name": "swarm/workspace",
        "description": "Shared Swarm coding workspace; reset " + data["reset"]["seed_id"],
    }
    current = original
    calls = []

    def handle(request):
        nonlocal current
        calls.append(request.method)
        if request.method == "DELETE":
            current = None
            return httpx.Response(204)
        if request.method == "POST":
            current = fresh
            raise httpx.ReadError("Lost create response")
        return httpx.Response(200, json=current) if current else httpx.Response(404)

    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        "harness.coding.reset.httpx.AsyncClient",
        lambda **kwargs: real_client(transport=httpx.MockTransport(handle), **kwargs),
    )
    with pytest.raises(httpx.ReadError):
        await reset_gitea(AsyncMock(), settings(), data, checkpoint)
    assert load(checkpoint)["reset"]["coding_reset"]["gitea_phase"] == "create"
    await reset_gitea(AsyncMock(), settings(), load(checkpoint), checkpoint)
    assert calls.count("DELETE") == calls.count("POST") == 1
    assert load(checkpoint)["reset"]["coding_reset"]["gitea_phase"] == "completed"


async def test_repository_replacement_is_not_deleted(tmp_path, monkeypatch):
    data = state()
    data["reset"]["coding_reset"].update(gitea_phase="delete", gitea_original_id=1)
    calls = []

    def handle(request):
        calls.append(request.method)
        return httpx.Response(200, json={"id": 9})

    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        "harness.coding.reset.httpx.AsyncClient",
        lambda **kwargs: real_client(transport=httpx.MockTransport(handle), **kwargs),
    )
    with pytest.raises(CodingError, match="REPOSITORY_CHANGED"):
        await reset_gitea(AsyncMock(), settings(), data, tmp_path / "state.json")
    assert calls == ["GET"]


async def test_worker_stop_wipe_start_order_and_failure_leaves_it_stopped(tmp_path, monkeypatch):
    inspected = json.dumps(
        {
            "image": "sha256:test",
            "mounts": [
                {"Destination": p, "Type": "volume", "Name": n}
                for p, n in (
                    ("/workspaces", "test-workspaces"),
                    ("/home/node/.local/share/opencode", "test-sessions"),
                )
            ],
        }
    )
    commands = []
    fail = True

    async def command(argv):
        commands.append(argv)
        if argv[1] == "inspect":
            return inspected
        if argv[1] == "run" and fail:
            raise CodingError("Wipe failed")
        return ""

    monkeypatch.setattr("harness.coding.reset.Worker", lambda _: SimpleNamespace(command=command))
    client = SimpleNamespace(request=AsyncMock(), close=AsyncMock())
    monkeypatch.setattr("harness.coding.reset.OpenCode", lambda _: client)
    with pytest.raises(CodingError):
        await reset_opencode(AsyncMock(), settings(), state(), tmp_path / "state.json")
    assert [c[1] for c in commands] == ["inspect", "stop", "run"]
    fail = False
    commands.clear()
    await reset_opencode(AsyncMock(), settings(), state(), tmp_path / "state.json")
    assert [c[1] for c in commands] == ["inspect", "stop", "run", "start"]
    assert "--network" in commands[2] and "none" in commands[2]
    assert "--volumes-from" not in commands[2]
    client.close.assert_awaited_once()


async def test_worker_bind_mount_rejected_before_stop(tmp_path, monkeypatch):
    command = AsyncMock(
        return_value=json.dumps(
            {
                "image": "sha256:test",
                "mounts": [
                    {"Destination": p, "Type": "bind"}
                    for p in ("/workspaces", "/home/node/.local/share/opencode")
                ],
            }
        )
    )
    monkeypatch.setattr("harness.coding.reset.Worker", lambda _: SimpleNamespace(command=command))
    with pytest.raises(CodingError, match="VOLUME_GUARD"):
        await reset_opencode(AsyncMock(), settings(), state(), tmp_path / "state.json")
    command.assert_awaited_once()
