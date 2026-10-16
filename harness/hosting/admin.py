"""Durable metadata ledger. No prompts, channel/repository names or credentials."""

import asyncio
import base64
import json
import secrets
import shlex
import sqlite3
import time
from pathlib import Path
from typing import Any

from harness.hosting.cloud import Cloud
from harness.hosting.store import HostingStore

COUNTERS = (
    "agents_created",
    "input_tokens",
    "cached_input_tokens",
    "output_tokens",
    "messages",
    "human_messages",
    "issues",
    "prs",
    "merged_prs",
    "commits",
    "errors",
)


class AdminUsage:
    def __init__(self, store: HostingStore, cloud: Cloud):
        self.store, self.cloud = store, cloud
        self.tasks: dict[str, asyncio.Task[None]] = {}
        self.due: dict[str, float] = {}
        self.locks: dict[str, asyncio.Lock] = {}

    async def begin(self, user: dict[str, Any], *, imported: bool = False) -> None:
        if await self.store.sql(
            "SELECT id FROM hosted_runs WHERE user_id=? AND ended_at IS NULL", (user["id"],)
        ):
            return
        now = int(time.time())
        await self.store.sql(
            "INSERT OR IGNORE INTO hosted_runs(id,user_id,requested_at,started_at,imported) "
            "VALUES(?,?,?,?,?)",
            (secrets.token_hex(12), user["id"], now, None, int(imported)),
        )

    async def ingest(
        self, user: dict[str, Any], snapshot: dict[str, Any], run_id: str | None = None
    ) -> None:
        # Explicit allowlist is applied even to a compromised/malformed tenant response.
        values = {}
        for key in COUNTERS:
            value = snapshot.get("counters", {}).get(key)
            values[key] = int(value) if isinstance(value, (int, float)) and value >= 0 else None
        now = int(time.time())
        epoch = str(snapshot["epoch"])[:100]
        at = snapshot.get("at")
        observed_at = int(at) if isinstance(at, (int, float)) and 0 <= at <= now + 300 else now
        safe = {"epoch": epoch, "at": observed_at, "counters": values}
        for key in (
            "agents",
            "blocked",
            "failed",
            "last_human_at",
            "uncertain_calls",
            "in_flight_calls",
            "disk_used_bytes",
            "disk_total_bytes",
        ):
            value = snapshot.get(key)
            safe[key] = int(value) if isinstance(value, (int, float)) and value >= 0 else None
        safe["models"] = [str(m)[:100] for m in snapshot.get("models", []) if isinstance(m, str)][
            :100
        ]
        if run_id is None:
            await self.begin(user, imported=True)

        def save() -> None:
            with sqlite3.connect(self.store.path, timeout=20) as conn:
                conn.row_factory = sqlite3.Row
                conn.execute("BEGIN IMMEDIATE")
                run = conn.execute(
                    "SELECT * FROM hosted_runs WHERE user_id=? AND "
                    + ("id=?" if run_id else "ended_at IS NULL"),
                    (user["id"], run_id) if run_id else (user["id"],),
                ).fetchone()
                if not run:
                    return
                segment = conn.execute(
                    "SELECT * FROM hosted_run_segments WHERE run_id=? AND epoch=?",
                    (run["id"], epoch),
                ).fetchone()
                previous = conn.execute(
                    "SELECT * FROM usage_latest WHERE user_id=?", (user["id"],)
                ).fetchone()
                baseline = dict.fromkeys(COUNTERS, 0)
                if segment:
                    baseline = json.loads(segment["baseline"])
                    latest = json.loads(segment["latest"])
                else:
                    latest = dict.fromkeys(COUNTERS, None)
                    if previous:
                        old = json.loads(previous["snapshot"])
                        if old["epoch"] == epoch:
                            baseline = old["counters"]
                for key, value in values.items():
                    if value is not None:
                        latest[key] = max(latest[key] or 0, value)
                conn.execute(
                    "INSERT INTO hosted_run_segments VALUES(?,?,?,?) ON CONFLICT(run_id,epoch) DO "
                    "UPDATE SET latest=excluded.latest",
                    (run["id"], epoch, json.dumps(baseline), json.dumps(latest)),
                )
                # Preserve confirmed counts through transient outages.
                safe["counters"] = latest
                if not previous or json.loads(previous["snapshot"]).get("at", 0) <= observed_at:
                    conn.execute(
                        "INSERT INTO usage_latest(user_id,instance,snapshot,updated_at) "
                        "VALUES(?,?,?,?) "
                        "ON CONFLICT(user_id) DO UPDATE SET "
                        "instance=excluded.instance,snapshot=excluded.snapshot,"
                        "updated_at=excluded.updated_at,unavailable=0",
                        (user["id"], user["instance"], json.dumps(safe), now),
                    )
                conn.execute(
                    "UPDATE hosted_runs SET peak_agents=max(peak_agents,?),"
                    "last_sample=max(coalesce(last_sample,0),?) WHERE id=?",
                    (safe.get("agents") or 0, observed_at, run["id"]),
                )

        await asyncio.to_thread(save)

    async def checkpoint(self, user: dict[str, Any], snapshot: dict[str, Any]) -> None:
        reset_id = str(snapshot.get("reset_id", ""))[:100]
        if not reset_id or await self.store.sql(
            "SELECT 1 FROM telemetry_receipts WHERE user_id=? AND reset_id=?",
            (user["id"], reset_id),
        ):
            return
        at = snapshot.get("at", int(time.time()))
        runs = await self.store.sql(
            "SELECT id FROM hosted_runs WHERE user_id=? AND requested_at<=? "
            "AND (ended_at IS NULL OR ended_at>=?) ORDER BY requested_at DESC LIMIT 1",
            (user["id"], at, at),
        )
        await self.ingest(user, snapshot, runs[0]["id"] if runs else None)
        await self.store.sql(
            "INSERT OR IGNORE INTO telemetry_receipts VALUES(?,?)", (user["id"], reset_id)
        )

    async def sample(self, user: dict[str, Any], *, offline: bool = False) -> None:
        async with self.locks.setdefault(user["id"], asyncio.Lock()):
            try:
                current = await self.store.user(user["id"])
                if current["status"] not in {"running", "pausing", "stopping"}:
                    return
                source = await asyncio.to_thread(
                    Path(__file__).with_name("telemetry.py").read_bytes
                )
                encoded = base64.b64encode(source).decode()
                code = (
                    "import base64;exec(compile(base64.b64decode("
                    + repr(encoded)
                    + '),"telemetry.py","exec"),{"__name__":"__main__"})'
                )
                command = (
                    "docker run --rm --network host --entrypoint python "
                    "--mount type=bind,src=/root/swarm/app,dst=/root/swarm/app "
                    "--mount type=bind,src=/var/run/docker.sock,dst=/var/run/docker.sock "
                    "swarm-controller -c "
                    if offline
                    else "docker exec swarm-controller python -c "
                )
                async with asyncio.timeout(60):
                    raw = await self.cloud.command(user["instance"], command + shlex.quote(code))
                current = await self.store.user(user["id"])
                if current["status"] in {"running", "pausing", "stopping"}:
                    snapshot = json.loads(raw)
                    for pending in snapshot.pop("pending", []):
                        await self.checkpoint(current, pending)
                    await self.ingest(current, snapshot)
            except Exception:
                await self.store.sql(
                    "UPDATE usage_latest SET unavailable=1 WHERE user_id=?", (user["id"],)
                )
            finally:
                self.due[user["id"]] = time.monotonic() + 60

    async def finish(self, user: dict[str, Any], reason: str) -> None:
        await self.store.sql(
            "UPDATE hosted_runs SET ended_at=?,end_reason=? WHERE user_id=? AND ended_at IS NULL",
            (int(time.time()), reason, user["id"]),
        )

    async def tick(self) -> None:
        for user in await self.store.sql("SELECT * FROM users WHERE status='running'"):
            old = self.tasks.get(user["id"])
            if (not old or old.done()) and self.due.get(user["id"], 0) <= time.monotonic():
                self.tasks[user["id"]] = asyncio.create_task(self.sample(user))

    async def report(self) -> dict[str, Any]:
        users = await self.store.sql(
            "SELECT id,name,role,status,created_at,last_seen,"
            "google_subject IS NOT NULL claimed,invite_revoked,instance "
            "FROM users WHERE activated=1 ORDER BY created_at DESC"
        )
        runs = await self.store.sql(
            "SELECT * FROM hosted_runs ORDER BY requested_at DESC LIMIT 500"
        )
        segments = await self.store.sql("SELECT * FROM hosted_run_segments")
        for run in runs:
            totals: dict[str, int | None] = dict.fromkeys(COUNTERS, None)
            for segment in segments:
                if segment["run_id"] != run["id"]:
                    continue
                baseline, latest = json.loads(segment["baseline"]), json.loads(segment["latest"])
                for key in COUNTERS:
                    if latest.get(key) is not None:
                        # If the first baseline was unknown, don't invent that run's delta.
                        if baseline.get(key) is not None:
                            totals[key] = (totals[key] or 0) + max(0, latest[key] - baseline[key])
            run["totals"] = totals
            run["duration_seconds"] = (
                None
                if run["started_at"] is None
                else max(0, (run["ended_at"] or int(time.time())) - run["started_at"])
            )
        latest = {
            r["user_id"]: {**json.loads(r["snapshot"]), "unavailable": bool(r["unavailable"])}
            for r in await self.store.sql("SELECT * FROM usage_latest")
        }
        grants = {
            r["user_id"]: r["expires"]
            for r in await self.store.sql(
                "SELECT * FROM support_grants WHERE expires>?", (int(time.time()),)
            )
        }
        for user in users:
            user["invite_status"] = (
                "claimed"
                if user.pop("claimed")
                else "revoked"
                if user.pop("invite_revoked")
                else "pending"
            )
            user.pop("invite_revoked", None)
            user["latest"] = latest.get(user["id"])
            user["support_until"] = grants.get(user["id"])
        return {
            "users": users,
            "runs": runs,
            "updated_at": int(time.time()),
            "total_runs": (await self.store.sql("SELECT count(*) n FROM hosted_runs"))[0]["n"],
            "tracking_started_at": (
                await self.store.sql("SELECT min(requested_at) at FROM hosted_runs")
            )[0]["at"],
        }

    async def close(self) -> None:
        for task in self.tasks.values():
            task.cancel()
        await asyncio.gather(*self.tasks.values(), return_exceptions=True)
