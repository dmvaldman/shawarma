"""Owner reset of the dedicated coding worker and configured Gitea repository."""

import asyncio
import json
from pathlib import Path
from typing import Any
from urllib.parse import quote
from uuid import UUID

import httpx

from harness.coding.clients import CodingError, OpenCode, Worker, check
from harness.config import Settings
from harness.persistence.bootstrap import load, save
from harness.persistence.store import Store

# Run in a disposable helper after the worker is stopped. Only named service
# volumes are mounted; no host path, environment credentials or Docker socket.
WIPE = """
from pathlib import Path
import shutil
for name in ('/workspaces', '/home/node/.local/share/opencode'):
    root = Path(name)
    if root.is_symlink() or not root.is_dir():
        raise RuntimeError('Invalid reset volume')
    for child in root.iterdir():
        if child.is_dir() and not child.is_symlink():
            shutil.rmtree(child)
        else:
            child.unlink()
"""


def creation_auth(settings: Settings) -> tuple[str, str]:
    # Creating a repo requires write:user, deliberately absent from worker tokens.
    # Use the existing provisioning service password only in the controller.
    checkpoint = (
        Path(".state/agent37-coding.json")
        if settings.coding_backend == "agent37"
        else Path(".state/coding.json")
    )
    if not checkpoint.exists():
        raise CodingError("Reset requires the coding provisioning checkpoint")
    credentials = load(checkpoint)
    if str(settings.gitea_url).rstrip("/") != credentials.get("gitea_url", "").rstrip("/"):
        raise CodingError("RESET_CREDENTIAL_TARGET: coding checkpoint belongs to another Gitea")
    if not credentials.get("service_password"):
        raise CodingError("Reset requires the Gitea provisioning service password")
    return settings.gitea_owner, str(credentials["service_password"])


async def prepare(store: Store, settings: Settings, state: dict[str, Any], path: Path) -> None:
    plan = state["reset"]
    workspace = UUID(state["workspace_id"])
    has_sessions = await store.pool.fetchval(
        "SELECT EXISTS(SELECT 1 FROM coding_sessions s JOIN agents a ON a.id=s.agent_id "
        "WHERE a.workspace_id=$1)",
        workspace,
    )
    enabled = settings.coding_enabled or bool(has_sessions)
    target = {
        "backend": settings.coding_backend,
        "instance": settings.agent37_instance_id,
        "container": settings.coding_container,
        "opencode_url": str(settings.opencode_url),
        "gitea_url": str(settings.gitea_url),
        "owner": settings.gitea_owner,
        "repo": settings.gitea_repo,
    }
    if "coding_reset" in plan:
        if plan["coding_reset"]["target"] != target:
            raise CodingError("RESET_TARGET_CHANGED: restore the original coding configuration")
        return
    if enabled:
        shared = await store.pool.fetchval(
            "SELECT EXISTS(SELECT 1 FROM coding_sessions s JOIN agents a ON a.id=s.agent_id "
            "WHERE a.workspace_id!=$1)",
            workspace,
        )
        if shared:
            raise CodingError("RESET_SHARED_CODING_WORKER: reset requires a dedicated worker/repo")
        creation_auth(settings)
        if not settings.gitea_token.get_secret_value():
            raise CodingError("Reset requires the configured Gitea service token")
    plan["coding_reset"] = {"enabled": enabled, "target": target}
    save(path, state)


async def reset_opencode(
    store: Store, settings: Settings, state: dict[str, Any], path: Path
) -> None:
    if not state["reset"]["coding_reset"]["enabled"]:
        return
    worker = Worker(settings)
    container = settings.coding_container
    async with asyncio.timeout(120):
        inspected = json.loads(
            await worker.command(
                [
                    "docker",
                    "inspect",
                    "--format",
                    '{"image":{{json .Image}},"mounts":{{json .Mounts}}}',
                    container,
                ]
            )
        )
        volumes = {m["Destination"]: m for m in inspected["mounts"]}
        paths = ("/workspaces", "/home/node/.local/share/opencode")
        if any(p not in volumes or volumes[p]["Type"] != "volume" for p in paths):
            raise CodingError("RESET_VOLUME_GUARD: expected dedicated named coding volumes")
        await worker.command(["docker", "stop", "--time", "10", container])
        # Do not restart on wipe failure. Retry safely stops/wipes the same target.
        await worker.command(
            [
                "docker",
                "run",
                "--rm",
                "--network",
                "none",
                "--mount",
                "type=volume,src=" + volumes[paths[0]]["Name"] + ",dst=" + paths[0],
                "--mount",
                "type=volume,src=" + volumes[paths[1]]["Name"] + ",dst=" + paths[1],
                "--entrypoint",
                "python3",
                inspected["image"],
                "-c",
                WIPE,
            ]
        )
        await worker.command(["docker", "start", container])
        client = OpenCode(settings)
        try:
            for _ in range(40):
                try:
                    await client.request("GET", "/global/health")
                    return
                except (CodingError, httpx.TransportError):
                    await asyncio.sleep(0.5)
            raise CodingError("Reset worker did not become healthy")
        finally:
            await client.close()


async def reset_gitea(store: Store, settings: Settings, state: dict[str, Any], path: Path) -> None:
    plan = state["reset"]
    coding = plan["coding_reset"]
    if not coding["enabled"]:
        return
    owner, repo = quote(settings.gitea_owner, safe=""), quote(settings.gitea_repo, safe="")
    url = str(settings.gitea_url).rstrip("/") + "/api/v1/"
    marker = "Shared Swarm coding workspace; reset " + plan["seed_id"]
    auth = creation_auth(settings)
    async with httpx.AsyncClient(
        base_url=url,
        timeout=40,
        follow_redirects=False,
        headers={"Authorization": "token " + settings.gitea_token.get_secret_value()},
    ) as client:
        if "topic_repositories" not in coding:
            topics = await store.pool.fetch(
                "SELECT * FROM work_repositories WHERE workspace_id=$1",
                UUID(state["workspace_id"]),
            )
            coding["topic_repositories"] = [dict(r) for r in topics]
            for topic in coding["topic_repositories"]:
                topic.pop("workspace_id", None)
            save(path, state)
        for topic in coding["topic_repositories"]:
            if topic.get("deleted"):
                continue
            if (
                topic["server_url"] != str(settings.gitea_url).rstrip("/")
                or topic["owner_name"] != settings.gitea_owner
            ):
                raise CodingError("RESET_REPOSITORY_TARGET_CHANGED")
            target = f"repos/{owner}/" + quote(topic["name"], safe="")
            found = await client.get(target)
            if found.status_code != 404:
                entry = check(found, "Gitea topic reset")
                if (topic["repository_id"] and entry["id"] != topic["repository_id"]) or (
                    not topic["repository_id"] and entry.get("description") != topic["marker"]
                ):
                    raise CodingError("RESET_REPOSITORY_CHANGED: refusing to delete replacement")
                check(await client.delete(target), "Gitea topic reset")
            topic["deleted"] = True
            save(path, state)
        current = await client.get(f"repos/{owner}/{repo}")
        if current.status_code != 404:
            check(current, "Gitea")
        if "gitea_phase" not in coding:
            coding["gitea_original_id"] = (
                current.json()["id"] if current.status_code == 200 else None
            )
            coding["gitea_phase"] = "delete"
            save(path, state)
        if coding["gitea_phase"] == "delete":
            if current.status_code == 200:
                if current.json()["id"] != coding["gitea_original_id"]:
                    raise CodingError("RESET_REPOSITORY_CHANGED: refusing to delete a replacement")
                deleted = await client.delete(f"repos/{owner}/{repo}")
                if deleted.status_code != 404:
                    check(deleted, "Gitea")
            coding["gitea_phase"] = "create"
            save(path, state)
            current = await client.get(f"repos/{owner}/{repo}")
        if current.status_code == 404:
            # On an uncertain response, a retry inspects the generation marker
            # instead of deleting the newly created repository or replaying blindly.
            current = await client.post(
                "user/repos",
                auth=auth,
                json={
                    "name": settings.gitea_repo,
                    "private": True,
                    "auto_init": True,
                    "default_branch": "main",
                    "description": marker,
                },
            )
        data = check(current, "Gitea")
        if (
            data["full_name"] != settings.gitea_owner + "/" + settings.gitea_repo
            or data.get("description") != marker
        ):
            raise CodingError("RESET_REPOSITORY_CHANGED: unexpected replacement repository")
        # Delete only controller-recorded agent accounts for this workspace. The
        # repository has been reset, so old PRs/issues no longer reference them.
        checkpoint = Path(
            ".state/agent37-coding.json"
            if settings.coding_backend == "agent37"
            else ".state/coding.json"
        )
        admin = ("swarm-owner", load(checkpoint)["admin_password"])
        identities = await store.pool.fetch(
            "SELECT i.username FROM gitea_identities i JOIN agents a ON a.id=i.agent_id "
            "WHERE a.workspace_id=$1",
            UUID(state["workspace_id"]),
        )
        for identity in identities:
            removed = await client.delete(
                "admin/users/" + quote(identity["username"], safe=""), auth=admin
            )
            if removed.status_code != 404:
                check(removed, "Gitea agent reset")
        coding["gitea_phase"] = "completed"
        save(path, state)
