"""Repository ownership and inheritance; no provider inference or live Gitea mutations."""

import asyncio
import json
import os
from functools import partial
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest

from harness.coding.clients import CodingError
from harness.coding.repositories import provision, selected
from harness.core.coordination import Coordination

pytestmark = pytest.mark.skipif(os.environ.get("HARNESS_LIVE_TESTS") != "1", reason="Postgres gate")


def fake_gitea(monkeypatch, handler):
    client = httpx.AsyncClient
    monkeypatch.setattr("harness.coding.repositories.creation_auth", lambda _: ("swarm", "secret"))
    monkeypatch.setattr(
        httpx, "AsyncClient", partial(client, transport=httpx.MockTransport(handler))
    )


async def test_provision_retry_reuse_and_unrelated_collision(runtime_store, monkeypatch):
    store, settings, seed, workspace = runtime_store
    repos = {}
    writes = []

    def api(request):
        if request.method == "POST":
            body = json.loads(request.content)
            writes.append(body)
            repos[body["name"]] = {
                "id": len(repos) + 1,
                "full_name": settings.gitea_owner + "/" + body["name"],
                "description": body["description"],
                "html_url": "http://git/topic",
            }
            # Simulate committed mutation but lost response.
            raise httpx.ReadTimeout("lost response", request=request)
        name = request.url.path.rsplit("/", 1)[1]
        return httpx.Response(200, json=repos[name]) if name in repos else httpx.Response(404)

    fake_gitea(monkeypatch, api)
    with pytest.raises(httpx.ReadTimeout):
        await provision(store, settings, workspace, "channel", "collatz", "experiments")
    result = await provision(store, settings, workspace, "channel", "collatz", "experiments")
    assert result["repository"] == settings.gitea_owner + "/collatz"
    await provision(store, settings, workspace, "subchannel", "collatz", "same topic")
    assert len(writes) == 1
    assert writes[0]["private"] and writes[0]["default_branch"] == "main"
    with pytest.raises(ValueError, match="CHANNEL_ALREADY"):
        await provision(store, settings, workspace, "channel", "other", "")
    repos["foreign"] = {
        "id": 999,
        "full_name": settings.gitea_owner + "/foreign",
        "description": "unrelated",
    }
    with pytest.raises(CodingError, match="COLLISION"):
        await provision(store, settings, workspace, "other", "foreign", "")
    with pytest.raises(ValueError, match="UNAVAILABLE"):
        await selected(store, settings, seed, "foreign")


async def test_birth_selects_channel_repo_descendants_inherit_and_ambiguity_rejected(runtime_store):
    store, settings, seed, workspace = runtime_store
    for name, channel in (("collatz", "channel"), ("graphs", "graphs-channel")):
        await store.pool.execute(
            "INSERT INTO work_repositories VALUES($1,$2,$3,$4,'marker',123)",
            workspace,
            name,
            str(settings.gitea_url).rstrip("/"),
            settings.gitea_owner,
        )
        await store.pool.execute(
            "INSERT INTO channel_repositories VALUES($1,$2,$3)", workspace, channel, name
        )
    coordination = Coordination(store, settings)
    args = {
        "system_prompt": "Investigate",
        "initial_task": "test",
        "tools": [],
        "subscriptions": ["channel"],
    }
    child = await coordination.reserve(seed, "child", args)
    request = json.loads(
        await store.pool.fetchval("SELECT request FROM spawn_requests WHERE child_id=$1", child)
    )
    assert request["_repository"] == "collatz"
    await store.pool.execute(
        "INSERT INTO agents(id,workspace_id,parent_id,identity,model_id,state) "
        "SELECT $1,workspace_id,id,identity,model_id,'READY' FROM agents WHERE id=$2",
        child,
        seed,
    )
    await store.pool.execute("INSERT INTO agent_repositories VALUES($1,'collatz')", child)
    assert (await selected(store, settings, child)).gitea_repo == "collatz"
    # Inviting an existing worker into another channel does not change its checkout.
    await store.pool.execute("INSERT INTO subscriptions VALUES($1,'graphs-channel','all')", child)
    assert (await selected(store, settings, child)).gitea_repo == "collatz"
    grandchild = await coordination.reserve(child, "grandchild", {**args, "subscriptions": []})
    request = json.loads(
        await store.pool.fetchval(
            "SELECT request FROM spawn_requests WHERE child_id=$1", grandchild
        )
    )
    assert request["_repository"] == "collatz"
    with pytest.raises(ValueError, match="AMBIGUOUS"):
        await coordination.reserve(
            seed, "ambiguous", {**args, "subscriptions": ["channel", "graphs-channel"]}
        )
    chosen = await coordination.reserve(
        seed,
        "chosen",
        {**args, "subscriptions": ["channel", "graphs-channel"], "repository": "graphs"},
    )
    request = json.loads(
        await store.pool.fetchval("SELECT request FROM spawn_requests WHERE child_id=$1", chosen)
    )
    assert request["_repository"] == "graphs"
    with pytest.raises(ValueError, match="UNAVAILABLE"):
        await selected(store, settings, seed, "not-ours")


async def test_only_seed_can_provision(runtime_store, monkeypatch):
    from harness.mattermost.client import Client
    from harness.tools.core import CoreTools

    store, settings, seed, workspace = runtime_store
    child = uuid4()
    await store.pool.execute(
        "INSERT INTO agents(id,workspace_id,parent_id,identity,model_id,state) "
        "SELECT $1,workspace_id,id,identity,model_id,'READY' FROM agents WHERE id=$2",
        child,
        seed,
    )
    create = AsyncMock()
    monkeypatch.setattr("harness.tools.core.provision", create)
    bot = Client("http://mm.invalid", "token")
    try:
        tools = CoreTools(store, bot, child, settings)
        turn = await store.reserve_cost(child, 0, [])
        result = await tools.execute(
            turn,
            {
                "name": "create_repository",
                "call_id": "no",
                "arguments": json.dumps({"channel_id": "x", "name": "topic"}),
            },
        )
        assert "SEED_ONLY" in result["detail"]
        create.assert_not_awaited()
    finally:
        await bot.http.aclose()


@pytest.mark.skipif(os.environ.get("HARNESS_GITEA_LIVE_TESTS") != "1", reason="Live repo gate")
async def test_real_topic_repos_same_agent_access_and_worker_clone(runtime_store):
    from pathlib import Path

    from harness.coding.clients import Worker
    from harness.coding.identities import identity
    from harness.config import Settings
    from harness.tools.gitea import request_repository

    store, _, agent, workspace = runtime_store
    settings = Settings()
    username = "probe-" + agent.hex[:12]
    await store.pool.execute(
        "INSERT INTO agent_profiles(agent_id,username,display_name,role_name,model_metadata) "
        "VALUES($1,$2,$2,'test','{}') ON CONFLICT(agent_id) DO UPDATE SET username=$2",
        agent,
        username,
    )
    names = ["topic-test-" + uuid4().hex[:12] for _ in range(2)]
    creds = json.loads(await asyncio.to_thread(Path(".state/coding.json").read_text))
    worker = Worker(settings)
    async with httpx.AsyncClient(
        base_url=str(settings.gitea_url).rstrip("/") + "/api/v1/",
        auth=("swarm-owner", creds["admin_password"]),
        timeout=30,
    ) as admin:
        try:
            for n, name in enumerate(names):
                await provision(store, settings, workspace, "test-channel-" + str(n), name, "test")
                target = await selected(store, settings, agent, name)
                account = await identity(store, target, agent)
                result = await request_repository(
                    target, "POST", "/issues", '{"title":"Test issue"}', token=account["token"]
                )
                assert result["status"] == 201
            # The controller gives the native checkout its assigned repo, not workspace.
            first = await selected(store, settings, agent, names[0])
            account = await identity(store, first, agent)
            space = await worker.workspace(
                workspace,
                agent,
                {
                    "username": account["username"],
                    "token": account["token"],
                    "email": account["email"],
                    "repository": settings.gitea_owner + "/" + names[0],
                },
            )
            assert space["repository"].endswith("/" + names[0])
            await worker.command(
                [
                    "docker",
                    "exec",
                    "--user",
                    "node",
                    settings.coding_container,
                    "git",
                    "-C",
                    space["directory"],
                    "push",
                    "origin",
                    space["branch"],
                ]
            )
            branch = await request_repository(
                first, "GET", "/branches/" + space["branch"], token=account["token"]
            )
            assert branch["status"] == 200
            with pytest.raises(CodingError):
                await worker.workspace(
                    workspace,
                    agent,
                    {
                        "username": account["username"],
                        "token": account["token"],
                        "email": account["email"],
                        "repository": settings.gitea_owner + "/" + names[1],
                    },
                )
        finally:
            await worker.command(
                [
                    "docker",
                    "exec",
                    settings.coding_container,
                    "rm",
                    "-rf",
                    "/workspaces/" + str(workspace),
                ]
            )
            for name in names:
                removed = await admin.delete(f"repos/{settings.gitea_owner}/{name}")
                assert removed.status_code in {204, 404}
            removed = await admin.delete("admin/users/" + username)
            assert removed.status_code in {204, 404}


async def test_reset_deletes_registered_topics_and_refuses_replacements(
    runtime_store, monkeypatch, tmp_path
):
    from harness.coding.reset import reset_gitea

    store, settings, _, workspace = runtime_store
    url = str(settings.gitea_url).rstrip("/")
    await store.pool.execute(
        "INSERT INTO work_repositories VALUES($1,'topic',$2,$3,'marker',123)",
        workspace,
        url,
        settings.gitea_owner,
    )
    repos = {
        "topic": {"id": 456, "description": "replacement"},
        settings.gitea_repo: {
            "id": 10,
            "full_name": settings.gitea_owner + "/" + settings.gitea_repo,
        },
    }
    deleted = []

    def api(request):
        name = request.url.path.rsplit("/", 1)[1]
        if request.method == "GET":
            return httpx.Response(200, json=repos[name]) if name in repos else httpx.Response(404)
        if request.method == "DELETE":
            deleted.append(name)
            repos.pop(name)
            return httpx.Response(204)
        if request.method == "POST":
            body = json.loads(request.content)
            repo = {
                "id": 99,
                "full_name": settings.gitea_owner + "/" + body["name"],
                "description": body["description"],
            }
            repos[body["name"]] = repo
            return httpx.Response(201, json=repo)
        raise AssertionError(request.method)

    fake_gitea(monkeypatch, api)
    monkeypatch.setattr("harness.coding.reset.creation_auth", lambda _: ("swarm", "secret"))
    state = {
        "workspace_id": str(workspace),
        "reset": {"seed_id": str(uuid4()), "coding_reset": {"enabled": True}},
    }
    path = tmp_path / "reset.json"
    with pytest.raises(CodingError, match="RESET_REPOSITORY_CHANGED"):
        await reset_gitea(store, settings, state, path)
    assert deleted == []
    repos["topic"]["id"] = 123
    await reset_gitea(store, settings, state, path)
    assert deleted == ["topic", settings.gitea_repo]
    await reset_gitea(store, settings, state, path)
    assert len(deleted) == 2
