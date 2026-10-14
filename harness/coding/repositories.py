"""Workspace-owned topic repositories and immutable primary worker assignments."""

import re
from typing import Any
from uuid import UUID, uuid4

import httpx

from harness.coding.clients import CodingError, check
from harness.coding.reset import creation_auth
from harness.config import Settings
from harness.persistence.store import Store


async def selected(
    store: Store, settings: Settings, agent: UUID, name: str | None = None
) -> Settings:
    state = await store.state(agent)
    if name is None:
        name = await store.pool.fetchval(
            "SELECT repository_name FROM agent_repositories WHERE agent_id=$1", agent
        )
    if not name or name == settings.gitea_repo:
        return settings
    row = await store.pool.fetchrow(
        "SELECT * FROM work_repositories WHERE workspace_id=$1 AND name=$2",
        state["workspace_id"],
        name,
    )
    if not row or not row["repository_id"]:
        raise ValueError("REPOSITORY_UNAVAILABLE: choose a ready repository from list_repositories")
    if (
        row["server_url"] != str(settings.gitea_url).rstrip("/")
        or row["owner_name"] != settings.gitea_owner
    ):
        raise CodingError("REPOSITORY_TARGET_CHANGED")
    return settings.model_copy(update={"gitea_repo": name})


async def provision(
    store: Store, settings: Settings, workspace: UUID, channel: str, name: str, description: str
) -> dict[str, Any]:
    if not re.fullmatch(r"[a-z][a-z0-9-]{0,62}", name) or name == settings.gitea_repo:
        raise ValueError("REPOSITORY_NAME: use a relevant lowercase topic name")
    auth = creation_auth(settings)
    url = str(settings.gitea_url).rstrip("/")
    async with store.pool.acquire() as conn:
        lock = "work-repository:" + str(workspace)
        await conn.execute("SELECT pg_advisory_lock(hashtextextended($1,0))", lock)
        try:
            bound = await conn.fetchval(
                "SELECT repository_name FROM channel_repositories "
                "WHERE workspace_id=$1 AND channel_id=$2",
                workspace,
                channel,
            )
            if bound and bound != name:
                raise ValueError("CHANNEL_ALREADY_HAS_REPOSITORY: reuse " + bound)
            await conn.execute(
                "INSERT INTO work_repositories(workspace_id,name,server_url,owner_name,marker) "
                "VALUES($1,$2,$3,$4,$5) ON CONFLICT DO NOTHING",
                workspace,
                name,
                url,
                settings.gitea_owner,
                "Swarm topic " + str(uuid4()) + ": " + description,
            )
            row = await conn.fetchrow(
                "SELECT * FROM work_repositories WHERE workspace_id=$1 AND name=$2",
                workspace,
                name,
            )
            assert row is not None
            if row["server_url"] != url or row["owner_name"] != settings.gitea_owner:
                raise CodingError("REPOSITORY_TARGET_CHANGED")
            async with httpx.AsyncClient(
                base_url=url + "/api/v1/", auth=auth, timeout=40, follow_redirects=False
            ) as client:
                response = await client.get(f"repos/{settings.gitea_owner}/{name}")
                if response.status_code == 404:
                    if row["repository_id"]:
                        raise CodingError("REPOSITORY_DISAPPEARED: owner intervention required")
                    response = await client.post(
                        "user/repos",
                        json={
                            "name": name,
                            "description": row["marker"],
                            "private": True,
                            "auto_init": True,
                            "default_branch": "main",
                        },
                    )
                repo = check(response, "Topic repository")
                if (
                    repo["full_name"] != settings.gitea_owner + "/" + name
                    or (row["repository_id"] and repo["id"] != row["repository_id"])
                    or (not row["repository_id"] and repo.get("description") != row["marker"])
                ):
                    raise CodingError("REPOSITORY_COLLISION: refusing to adopt an unrelated repo")
            await conn.execute(
                "UPDATE work_repositories SET repository_id=$3 WHERE workspace_id=$1 AND name=$2",
                workspace,
                name,
                repo["id"],
            )
            await conn.execute(
                "INSERT INTO channel_repositories VALUES($1,$2,$3) ON CONFLICT DO NOTHING",
                workspace,
                channel,
                name,
            )
            return {
                "repository": repo["full_name"],
                "name": name,
                "url": repo["html_url"],
                "channel_id": channel,
                "default_branch": "main",
            }
        finally:
            await conn.execute("SELECT pg_advisory_unlock(hashtextextended($1,0))", lock)
