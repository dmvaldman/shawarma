"""Run inside a tenant: return counters only; never export chat, code or credentials."""

import asyncio
import json
import os
import subprocess
from pathlib import Path
from typing import Any
from urllib.parse import quote
from uuid import UUID

import httpx

from harness.api.analytics import ledger_snapshot
from harness.config import Settings
from harness.persistence.store import Store


async def pages(
    client: httpx.AsyncClient, path: str, params: dict[str, Any] | None = None
) -> list[Any]:
    result = []
    for page in range(1, 1001):
        response = await client.get(path, params={**(params or {}), "limit": 50, "page": page})
        response.raise_for_status()
        items = response.json()
        result.extend(items)
        if len(items) < 50:
            return result
    raise RuntimeError("Metadata pagination exceeded safe bound")


async def collect() -> dict[str, Any]:
    settings = Settings()
    state = json.loads(await asyncio.to_thread(Path(".state/workspace.json").read_text))
    workspace = UUID(state["workspace_id"])
    store = await Store.connect(settings)
    try:
        data = await ledger_snapshot(store, workspace, settings.agent_engine)
        counters = {
            k: data["totals"][k]
            for k in (
                "agents_created",
                "input_tokens",
                "output_tokens",
                "messages",
            )
        }
        counters["errors"] = await store.pool.fetchval(
            "SELECT count(*) FROM agent_activity x JOIN agents a ON a.id=x.agent_id "
            "WHERE a.workspace_id=$1 AND x.phase IN ('failed','error')",
            workspace,
        )
        counters["cached_input_tokens"] = await store.pool.fetchval(
            """SELECT coalesce(sum(CASE WHEN provider_usage->>'source'='opencode' THEN
              (SELECT coalesce(sum(v::text::bigint),0) FROM jsonb_path_query(
                provider_usage,'$.messages[*].steps[*].cache.read') v)
              ELSE coalesce((provider_usage->'input_tokens_details'->>'cached_tokens')::bigint,
                (provider_usage->'prompt_tokens_details'->>'cached_tokens')::bigint,
                (provider_usage->>'cache_read_input_tokens')::bigint,
                (provider_usage->>'prompt_cache_hit_tokens')::bigint,0) END),0)
            FROM agent_turns t JOIN agents a ON a.id=t.agent_id WHERE a.workspace_id=$1""",
            workspace,
        )
        now = await store.pool.fetchval("SELECT extract(epoch FROM now())::bigint")
        owner = settings.mattermost_owner_id
        if not owner.isalnum():
            raise ValueError("Invalid owner ID")
        query = (
            "SELECT json_build_object('count',count(*),'last_at',max(createat)/1000) "
            "FROM posts WHERE message NOT LIKE '**Harness log**%' AND userid='"
            + owner
            + "' AND type='' AND createat>="
            + str(data["workspace"]["run_ms"])
        )
        try:
            raw = await asyncio.to_thread(
                subprocess.check_output,
                [
                    "docker",
                    "exec",
                    "agent-company-postgres-1",
                    "psql",
                    "-U",
                    "mattermost",
                    "-d",
                    "mattermost",
                    "-At",
                    "-c",
                    query,
                ],
                timeout=10,
                stderr=subprocess.DEVNULL,
            )
            human = json.loads(raw)
            counters["human_messages"] = human["count"]
            last_human_at = human["last_at"]
        except Exception:
            counters["human_messages"], last_human_at = None, None
        for key in ("issues", "prs", "merged_prs", "commits"):
            counters[key] = None
        try:
            async with httpx.AsyncClient(
                base_url=str(settings.gitea_url).rstrip("/") + "/api/v1/",
                timeout=8,
                headers={"Authorization": "token " + settings.gitea_token.get_secret_value()},
            ) as client:
                async with asyncio.timeout(35):
                    repos = await pages(
                        client, "users/" + quote(settings.gitea_owner, safe="") + "/repos"
                    )
                    git = dict.fromkeys(("issues", "prs", "merged_prs", "commits"), 0)
                    for repo in repos:
                        base = (
                            "repos/"
                            + quote(settings.gitea_owner, safe="")
                            + "/"
                            + quote(repo["name"], safe="")
                        )
                        issues = await pages(
                            client, base + "/issues", {"state": "all", "type": "issues"}
                        )
                        pulls = await pages(client, base + "/pulls", {"state": "all"})
                        commits = (
                            await pages(client, base + "/commits") if not repo.get("empty") else []
                        )
                        git["issues"] += len(issues)
                        git["prs"] += len(pulls)
                        git["merged_prs"] += sum(bool(p.get("merged")) for p in pulls)
                        git["commits"] += len(commits)
                    counters.update(git)
        except Exception:
            pass  # Unknown is distinct from zero; never replace a known count on an outage.
        disk = os.statvfs("/root/swarm/app")
        counters = {
            key: int(value) if value is not None else None for key, value in counters.items()
        }
        pending_path = Path(".state/usage-checkpoints.json")
        pending = (
            json.loads(await asyncio.to_thread(pending_path.read_text))
            if await asyncio.to_thread(pending_path.exists)
            else []
        )
        return {
            "pending": pending,
            "epoch": str(workspace) + ":" + str(data["workspace"]["run_ms"]),
            "at": int(now),
            "counters": counters,
            "agents": data["totals"]["agents"],
            "blocked": sum(a["state"] == "QUOTA_BLOCKED" for a in data["agents"]),
            "failed": sum(a["state"] == "FAILED" for a in data["agents"]),
            "models": sorted({a["model_id"] for a in data["agents"]}),
            "last_human_at": last_human_at,
            "uncertain_calls": data["totals"]["uncertain_calls"],
            "in_flight_calls": data["totals"]["in_flight_calls"],
            "disk_used_bytes": (disk.f_blocks - disk.f_bfree) * disk.f_frsize,
            "disk_total_bytes": disk.f_blocks * disk.f_frsize,
        }
    finally:
        await store.pool.close()


async def checkpoint(reset_id: str) -> None:
    """Preserve final counts before destructive reset; queue if the control plane is unavailable."""
    from dotenv import dotenv_values

    env = dotenv_values(".env")
    if not env.get("HOSTED_GATEWAY_BROKER_URL"):
        return
    data = await collect()
    pending = data.pop("pending", [])
    data["reset_id"] = reset_id
    if not any(item.get("reset_id") == reset_id for item in pending):
        pending.append(data)
    path = Path(".state/usage-checkpoints.json")

    def save_pending() -> None:
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(pending[-100:]))
        temporary.chmod(0o600)
        temporary.replace(path)

    await asyncio.to_thread(save_pending)
    async with httpx.AsyncClient(timeout=15) as client:
        response = await client.post(
            str(env["HOSTED_GATEWAY_BROKER_URL"]) + "/telemetry",
            headers={"Authorization": "Bearer " + str(env["HOSTED_GATEWAY_KEY"])},
            json=data,
        )
        response.raise_for_status()


if __name__ == "__main__":
    print(json.dumps(asyncio.run(collect())))
