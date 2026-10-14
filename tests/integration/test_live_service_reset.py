"""Destructive reset gate uses only a disposable repository/container/volumes."""

import asyncio
import base64
import json
import os
import socket
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
from pydantic import SecretStr

from harness.coding.clients import OpenCode, Worker
from harness.coding.reset import creation_auth, reset_gitea, reset_opencode
from harness.config import Settings
from harness.persistence.bootstrap import load
from harness.tools.gitea import request_repository

pytestmark = pytest.mark.skipif(
    os.environ.get("HARNESS_SERVICE_RESET_LIVE_TESTS") != "1", reason="disposable reset gate"
)


async def test_real_repository_reset_removes_issues_prs_branches_and_source(tmp_path):
    original = Settings()
    settings = original.model_copy(update={"gitea_repo": "reset-test-" + uuid4().hex[:12]})
    data = {
        "workspace_id": str(uuid4()),
        "reset": {
            "seed_id": str(uuid4()),
            "coding_reset": {"enabled": True},
        },
    }
    checkpoint = tmp_path / "state.json"
    async with httpx.AsyncClient(
        base_url=str(settings.gitea_url).rstrip("/") + "/api/v1/",
        timeout=30,
        headers={"Authorization": "token " + settings.gitea_token.get_secret_value()},
    ) as client:
        created = await client.post(
            "user/repos",
            auth=creation_auth(settings),
            json={
                "name": settings.gitea_repo,
                "private": True,
                "auto_init": True,
                "default_branch": "main",
            },
        )
        created.raise_for_status()
        before = created.json()["id"]
        repo_path = f"repos/{settings.gitea_owner}/{settings.gitea_repo}"
        try:

            async def api(method, path, body=None):
                result = await request_repository(
                    settings, method, path, json.dumps(body) if body else None
                )
                assert "error" not in result, result
                return result["data"]

            await api("POST", "/issues", {"title": "Old issue", "body": "Old findings"})
            await api(
                "POST", "/branches", {"new_branch_name": "old-agent", "old_branch_name": "main"}
            )
            await api(
                "POST",
                "/contents/old.txt",
                {
                    "branch": "old-agent",
                    "message": "Old code",
                    "content": base64.b64encode(b"old code").decode(),
                },
            )
            await api("POST", "/pulls", {"head": "old-agent", "base": "main", "title": "Old PR"})
            await reset_gitea(AsyncMock(), settings, data, checkpoint)
            fresh = (await client.get(repo_path)).json()
            assert fresh["id"] != before and fresh["default_branch"] == "main"
            assert fresh["private"]
            assert await api("GET", "/issues?state=all") == []
            assert await api("GET", "/pulls?state=all") == []
            assert [b["name"] for b in await api("GET", "/branches")] == ["main"]
            assert (await client.get(repo_path + "/contents/old.txt")).status_code == 404
            # Replaying after completion keeps the same clean repository identity.
            await reset_gitea(AsyncMock(), settings, load(checkpoint), checkpoint)
            assert (await client.get(repo_path)).json()["id"] == fresh["id"]
        finally:
            deleted = await client.delete(repo_path)
            deleted.raise_for_status()


async def test_real_worker_reset_erases_session_history_files_and_snapshots(tmp_path):
    original = Settings()
    worker = Worker(original)
    suffix = uuid4().hex[:12]
    container = "reset-test-" + suffix
    volumes = ["reset-workspaces-" + suffix, "reset-sessions-" + suffix]
    image = (
        await worker.command(
            [
                "docker",
                "inspect",
                "--format",
                "{{.Image}}",
                original.coding_container,
            ]
        )
    ).strip()
    settings = original.model_copy(update={"coding_container": container})
    client = None
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = str(reservation.getsockname()[1])
    try:
        for volume in volumes:
            await worker.command(["docker", "volume", "create", volume])
        await worker.command(
            [
                "docker",
                "run",
                "-d",
                "--name",
                container,
                "-p",
                "127.0.0.1:" + port + ":4096",
                "-e",
                "OPENCODE_SERVER_PASSWORD=reset-test",
                "-v",
                volumes[0] + ":/workspaces",
                "-v",
                volumes[1] + ":/home/node/.local/share/opencode",
                image,
            ]
        )
        settings = settings.model_copy(
            update={
                "opencode_url": "http://127.0.0.1:" + port,
                "opencode_password": SecretStr("reset-test"),
            }
        )
        client = OpenCode(settings)
        for _ in range(40):
            try:
                await client.request("GET", "/global/health")
                break
            except httpx.TransportError:
                await asyncio.sleep(0.5)
        await worker.command(
            [
                "docker",
                "exec",
                container,
                "python3",
                "-c",
                "from pathlib import Path; "
                "Path('/workspaces/old').mkdir(); "
                "Path('/workspaces/old/findings.txt').write_text('Old findings'); "
                "Path('/home/node/.local/share/opencode/snapshot').mkdir(exist_ok=True); "
                "Path('/home/node/.local/share/opencode/snapshot/old').write_text('Old snapshot')",
            ]
        )
        session = await client.request(
            "POST", "/session", "/workspaces/old", json={"title": "Old conversation"}
        )
        await client.request(
            "POST",
            f"/session/{session['id']}/message",
            "/workspaces/old",
            json={
                "noReply": True,
                "model": {"providerID": "openai", "modelID": original.openai_model_id},
                "parts": [{"type": "text", "text": "Old private findings"}],
            },
        )
        data = {"reset": {"coding_reset": {"enabled": True}}}
        await reset_opencode(AsyncMock(), settings, data, tmp_path / "state.json")
        assert await client.request("GET", "/session", "/workspaces") == []
        result = await worker.command(
            [
                "docker",
                "exec",
                container,
                "python3",
                "-c",
                "from pathlib import Path; "
                "assert not list(Path('/workspaces').iterdir()); "
                "assert not Path('/home/node/.local/share/opencode/snapshot/old').exists()",
            ]
        )
        assert result == ""
        fresh = await client.request(
            "POST", "/session", "/workspaces", json={"title": "Fresh conversation"}
        )
        assert fresh["id"] != session["id"]
        assert await client.request("GET", f"/session/{fresh['id']}/message", "/workspaces") == []
    finally:
        if client:
            await client.close()
        await worker.command(["docker", "rm", "-f", container])
        for volume in volumes:
            await worker.command(["docker", "volume", "rm", volume])
