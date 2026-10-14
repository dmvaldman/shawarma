"""Controller-owned per-agent Gitea provisioning; secrets never enter tool results."""

import secrets
from pathlib import Path
from typing import Any
from uuid import UUID

import httpx

from harness.coding.clients import CodingError, check
from harness.config import Settings
from harness.persistence.bootstrap import load
from harness.persistence.store import Store


async def identity(store: Store, settings: Settings, agent: UUID) -> dict[str, Any]:
    checkpoint = Path(
        ".state/agent37-coding.json"
        if settings.coding_backend == "agent37"
        else ".state/coding.json"
    )
    credentials = load(checkpoint)
    url = str(settings.gitea_url).rstrip("/")
    if credentials.get("gitea_url", "").rstrip("/") != url:
        raise CodingError(
            "GITEA_IDENTITY_TARGET: provisioning checkpoint belongs to another server"
        )
    async with store.pool.acquire() as conn:
        lock = "gitea-identity:" + str(agent)
        await conn.execute("SELECT pg_advisory_lock(hashtextextended($1,0))", lock)
        try:
            name = await conn.fetchval(
                "SELECT coalesce(p.username,a.identity->>'mattermost_username') "
                "FROM agents a LEFT JOIN agent_profiles p ON p.agent_id=a.id WHERE a.id=$1",
                agent,
            )
            row = await conn.fetchrow("SELECT * FROM gitea_identities WHERE agent_id=$1", agent)
            if not row:
                if not name:
                    raise CodingError("GITEA_AGENT_NAME: Mattermost username required")
                username = name
                await conn.execute(
                    "INSERT INTO gitea_identities(agent_id,username,password,server_url) "
                    "VALUES($1,$2,$3,$4)",
                    agent,
                    username,
                    secrets.token_urlsafe(32),
                    url,
                )
                row = await conn.fetchrow("SELECT * FROM gitea_identities WHERE agent_id=$1", agent)
            assert row is not None
            result = dict(row)
            if result["server_url"] != url:
                raise CodingError("GITEA_IDENTITY_TARGET: identity belongs to another server")
            if result["token"]:
                async with httpx.AsyncClient(base_url=url + "/api/v1/", timeout=30) as client:
                    account = check(
                        await client.get(
                            "user", headers={"Authorization": "token " + result["token"]}
                        ),
                        "Gitea identity lookup",
                    )
                    if account["login"] != name:
                        target = await client.get(
                            "users/" + name, auth=("swarm-owner", credentials["admin_password"])
                        )
                        own_alias = (
                            target.status_code in {301, 302, 307, 308}
                            and target.headers.get("location")
                            == "/api/v1/users/" + account["login"]
                        )
                        if target.status_code != 404 and not own_alias:
                            raise CodingError(
                                "GITEA_USERNAME_COLLISION: matching name belongs to another account"
                            )
                        check(
                            await client.post(
                                "admin/users/" + account["login"] + "/rename",
                                auth=("swarm-owner", credentials["admin_password"]),
                                json={"new_username": name},
                            ),
                            "Gitea agent rename",
                        )
                    if result["username"] != name:
                        await conn.execute(
                            "UPDATE gitea_identities SET username=$2 WHERE agent_id=$1", agent, name
                        )
                        result["username"] = name
                    check(
                        await client.put(
                            f"repos/{settings.gitea_owner}/{settings.gitea_repo}/collaborators/{name}",
                            auth=("swarm-owner", credentials["admin_password"]),
                            json={"permission": "write"},
                        ),
                        "Gitea collaborator",
                    )
                    result["email"] = account["email"]
                return result
            admin = ("swarm-owner", credentials["admin_password"])
            user = result["username"]
            async with httpx.AsyncClient(base_url=url + "/api/v1/", timeout=30) as client:
                found = await client.get("users/" + user, auth=admin)
                if found.status_code == 404:
                    check(
                        await client.post(
                            "admin/users",
                            auth=admin,
                            json={
                                "username": user,
                                "password": result["password"],
                                "email": agent.hex + "@agents.invalid",
                                "full_name": name or user,
                                "must_change_password": False,
                                "send_notify": False,
                            },
                        ),
                        "Gitea agent creation",
                    )
                else:
                    check(found, "Gitea agent lookup")
                account = check(
                    await client.get("user", auth=(user, result["password"])),
                    "Gitea agent ownership",
                )
                result["email"] = account["email"]
                repo = f"repos/{settings.gitea_owner}/{settings.gitea_repo}"
                check(
                    await client.put(
                        repo + "/collaborators/" + user, auth=admin, json={"permission": "write"}
                    ),
                    "Gitea collaborator",
                )
                auth = (user, result["password"])
                tokens = check(
                    await client.get("users/" + user + "/tokens", auth=auth), "Gitea tokens"
                )
                for token in tokens:
                    if token["name"] == "swarm-agent":
                        check(
                            await client.delete(f"users/{user}/tokens/{token['id']}", auth=auth),
                            "Gitea orphan token cleanup",
                        )
                token = check(
                    await client.post(
                        "users/" + user + "/tokens",
                        auth=auth,
                        json={
                            "name": "swarm-agent",
                            "scopes": ["write:repository", "write:issue", "read:user"],
                        },
                    ),
                    "Gitea agent token",
                )
                result["token"] = token["sha1"]
                await conn.execute(
                    "UPDATE gitea_identities SET token=$2 WHERE agent_id=$1", agent, result["token"]
                )
            return result
        finally:
            await conn.execute("SELECT pg_advisory_unlock(hashtextextended($1,0))", lock)
