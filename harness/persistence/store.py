"""Harness-only PostgreSQL runtime, transactional inbox and conservative usage ledger."""

from __future__ import annotations

import json
from collections.abc import Sequence
from decimal import Decimal
from hashlib import sha256
from importlib.resources import files
from typing import Any
from uuid import UUID, uuid4

import asyncpg

from harness.config import Settings
from harness.core.types import AgentIdentity, ModelResponse, Notification, Post, Usage
from harness.notifications.routing import addressed


def decode(value: Any) -> Any:
    return json.loads(value) if isinstance(value, str) else value


class QuotaError(RuntimeError):
    pass


class Store:
    def __init__(self, pool: asyncpg.Pool):
        self.pool = pool

    @classmethod
    async def connect(cls, settings: Settings) -> Store:
        pool = await asyncpg.create_pool(
            settings.database_url.replace("postgresql+asyncpg://", "postgresql://"),
            min_size=1,
            # Each native activation holds one advisory-lock connection. Keep
            # ordinary queries/tools available even with the entire roster active.
            max_size=settings.max_active_agents + 10,
        )
        assert pool is not None
        store = cls(pool)
        async with pool.acquire() as conn, conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock(823701)")
            await conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations (version int PRIMARY KEY)"
            )
            if not await conn.fetchval("SELECT 1 FROM schema_migrations WHERE version=1"):
                sql = (
                    files("harness.persistence").joinpath("migrations/001_runtime.sql").read_text()
                )
                await conn.execute(sql)
                await conn.execute("INSERT INTO schema_migrations VALUES (1)")
            if not await conn.fetchval("SELECT 1 FROM schema_migrations WHERE version=2"):
                await conn.execute(
                    files("harness.persistence")
                    .joinpath("migrations/002_coordination.sql")
                    .read_text()
                )
                await conn.execute("INSERT INTO schema_migrations VALUES (2)")
            if not await conn.fetchval("SELECT 1 FROM schema_migrations WHERE version=3"):
                await conn.execute(
                    files("harness.persistence")
                    .joinpath("migrations/003_identity_models.sql")
                    .read_text()
                )
                await conn.execute("INSERT INTO schema_migrations VALUES (3)")
            if not await conn.fetchval("SELECT 1 FROM schema_migrations WHERE version=4"):
                await conn.execute(
                    files("harness.persistence").joinpath("migrations/004_delivery.sql").read_text()
                )
                await conn.execute("INSERT INTO schema_migrations VALUES (4)")
            if not await conn.fetchval("SELECT 1 FROM schema_migrations WHERE version=5"):
                await conn.execute(
                    files("harness.persistence").joinpath("migrations/005_names.sql").read_text()
                )
                await conn.execute("INSERT INTO schema_migrations VALUES (5)")
            if not await conn.fetchval("SELECT 1 FROM schema_migrations WHERE version=6"):
                await conn.execute(
                    files("harness.persistence").joinpath("migrations/006_controls.sql").read_text()
                )
                await conn.execute("INSERT INTO schema_migrations VALUES (6)")
            if not await conn.fetchval("SELECT 1 FROM schema_migrations WHERE version=7"):
                await conn.execute(
                    files("harness.persistence")
                    .joinpath("migrations/007_no_turn_limits.sql")
                    .read_text()
                )
                await conn.execute("INSERT INTO schema_migrations VALUES (7)")
            if not await conn.fetchval("SELECT 1 FROM schema_migrations WHERE version=8"):
                await conn.execute(
                    files("harness.persistence")
                    .joinpath("migrations/008_immediate_delivery.sql")
                    .read_text()
                )
                await conn.execute("INSERT INTO schema_migrations VALUES (8)")
            if not await conn.fetchval("SELECT 1 FROM schema_migrations WHERE version=9"):
                await conn.execute(
                    files("harness.persistence")
                    .joinpath("migrations/009_no_budgets.sql")
                    .read_text()
                )
                await conn.execute("INSERT INTO schema_migrations VALUES (9)")
            if not await conn.fetchval("SELECT 1 FROM schema_migrations WHERE version=10"):
                await conn.execute(
                    files("harness.persistence")
                    .joinpath("migrations/010_slash_controls.sql")
                    .read_text()
                )
                await conn.execute("INSERT INTO schema_migrations VALUES (10)")
            if not await conn.fetchval("SELECT 1 FROM schema_migrations WHERE version=11"):
                await conn.execute(
                    files("harness.persistence").joinpath("migrations/011_reset.sql").read_text()
                )
                await conn.execute("INSERT INTO schema_migrations VALUES (11)")
            if not await conn.fetchval("SELECT 1 FROM schema_migrations WHERE version=12"):
                await conn.execute(
                    files("harness.persistence")
                    .joinpath("migrations/012_idle_catchup.sql")
                    .read_text()
                )
                await conn.execute("INSERT INTO schema_migrations VALUES (12)")
            if not await conn.fetchval("SELECT 1 FROM schema_migrations WHERE version=13"):
                await conn.execute(
                    files("harness.persistence")
                    .joinpath("migrations/013_provider_usage.sql")
                    .read_text()
                )
                await conn.execute("INSERT INTO schema_migrations VALUES (13)")
            if not await conn.fetchval("SELECT 1 FROM schema_migrations WHERE version=14"):
                await conn.execute(
                    files("harness.persistence")
                    .joinpath("migrations/014_turn_model.sql")
                    .read_text()
                )
                await conn.execute("INSERT INTO schema_migrations VALUES (14)")
            if not await conn.fetchval("SELECT 1 FROM schema_migrations WHERE version=15"):
                await conn.execute(
                    files("harness.persistence")
                    .joinpath("migrations/015_agent_failure.sql")
                    .read_text()
                )
                await conn.execute("INSERT INTO schema_migrations VALUES (15)")
            if not await conn.fetchval("SELECT 1 FROM schema_migrations WHERE version=16"):
                await conn.execute(
                    files("harness.persistence")
                    .joinpath("migrations/016_billing_baselines.sql")
                    .read_text()
                )
                await conn.execute("INSERT INTO schema_migrations VALUES (16)")
            if not await conn.fetchval("SELECT 1 FROM schema_migrations WHERE version=17"):
                await conn.execute(
                    files("harness.persistence").joinpath("migrations/017_coding.sql").read_text()
                )
                await conn.execute("INSERT INTO schema_migrations VALUES (17)")
            if not await conn.fetchval("SELECT 1 FROM schema_migrations WHERE version=18"):
                await conn.execute(
                    files("harness.persistence")
                    .joinpath("migrations/018_native_agents.sql")
                    .read_text()
                )
                await conn.execute("INSERT INTO schema_migrations VALUES (18)")
            if not await conn.fetchval("SELECT 1 FROM schema_migrations WHERE version=19"):
                await conn.execute(
                    files("harness.persistence")
                    .joinpath("migrations/019_work_activity.sql")
                    .read_text()
                )
                await conn.execute("INSERT INTO schema_migrations VALUES (19)")
            if not await conn.fetchval("SELECT 1 FROM schema_migrations WHERE version=20"):
                await conn.execute(
                    files("harness.persistence")
                    .joinpath("migrations/020_channel_streams.sql")
                    .read_text()
                )
                await conn.execute("INSERT INTO schema_migrations VALUES (20)")
            if not await conn.fetchval("SELECT 1 FROM schema_migrations WHERE version=21"):
                await conn.execute(
                    files("harness.persistence")
                    .joinpath("migrations/021_agent_activity.sql")
                    .read_text()
                )
                await conn.execute("INSERT INTO schema_migrations VALUES (21)")
            if not await conn.fetchval("SELECT 1 FROM schema_migrations WHERE version=22"):
                await conn.execute(
                    files("harness.persistence")
                    .joinpath("migrations/022_gitea_identities.sql")
                    .read_text()
                )
                await conn.execute("INSERT INTO schema_migrations VALUES (22)")
            if not await conn.fetchval("SELECT 1 FROM schema_migrations WHERE version=23"):
                await conn.execute(
                    files("harness.persistence")
                    .joinpath("migrations/023_work_repositories.sql")
                    .read_text()
                )
                await conn.execute("INSERT INTO schema_migrations VALUES (23)")
            if not await conn.fetchval("SELECT 1 FROM schema_migrations WHERE version=24"):
                await conn.execute(
                    files("harness.persistence")
                    .joinpath("migrations/024_agent_avatars.sql")
                    .read_text()
                )
                await conn.execute("INSERT INTO schema_migrations VALUES (24)")
        return store

    async def reserve_name(
        self,
        server: str,
        agent: UUID,
        role: str,
        *,
        connection: asyncpg.Connection[Any] | asyncpg.pool.PoolConnectionProxy[Any] | None = None,
        unavailable: set[str] | None = None,
    ) -> str:
        if connection is None:
            async with self.pool.acquire() as acquired, acquired.transaction():
                return await self.reserve_name(
                    server, agent, role, connection=acquired, unavailable=unavailable
                )
        c = connection
        await c.execute("SELECT pg_advisory_xact_lock(hashtextextended($1,0))", server + ":names")
        current = await c.fetchval(
            "SELECT username FROM agent_name_reservations WHERE server_id=$1 AND agent_id=$2",
            server,
            agent,
        )
        if current and current not in (unavailable or set()):
            return str(current)
        from harness.core.names import agent_name

        taken = set(
            await c.fetchval(
                "SELECT COALESCE(array_agg(username),ARRAY[]::text[]) FROM agent_name_reservations "
                "WHERE server_id=$1 AND agent_id!=$2",
                server,
                agent,
            )
        ) | (unavailable or set())
        number = 1
        while agent_name(role, number)[0] in taken:
            number += 1
        name = agent_name(role, number)[0]
        await c.execute(
            "INSERT INTO agent_name_reservations VALUES($1,$2,$3) ON CONFLICT(server_id,agent_id) "
            "DO UPDATE SET username=EXCLUDED.username",
            server,
            agent,
            name,
        )
        return name

    async def close(self) -> None:
        await self.pool.close()

    async def initialize(
        self, bootstrap: dict[str, Any], settings: Settings, created_ms: int
    ) -> None:
        identity = AgentIdentity.model_validate(bootstrap["identity"])
        async with self.pool.acquire() as c, c.transaction():
            await c.execute(
                """INSERT INTO workspaces(id,name,seed_id,server_id,team_id,general_id,owner_id,
                   created_ms,budget,limits) VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)
                   ON CONFLICT(id) DO UPDATE SET limits=jsonb_set(
                     workspaces.limits - 'max_agents_total' - 'max_child_spawns_per_agent'
                       - 'max_context_bytes',
                     '{max_active_agents}', EXCLUDED.limits->'max_active_agents')""",
                identity.workspace_id,
                bootstrap["team_name"],
                identity.id,
                settings.mattermost_server_id,
                bootstrap["team_id"],
                bootstrap["general_id"],
                settings.mattermost_owner_id,
                created_ms,
                None,
                json.dumps(
                    {
                        k: str(v)
                        for k, v in settings.model_dump().items()
                        if k.startswith("max_") or k.endswith("timeout_sec")
                    }
                ),
            )
            await c.execute(
                """INSERT INTO agents(id,workspace_id,identity,model_id,budget)
                VALUES($1,$2,$3,$4,$5) ON CONFLICT(id) DO NOTHING""",
                identity.id,
                identity.workspace_id,
                identity.model_dump_json(),
                settings.seed_model_id,
                None,
            )
            await c.execute(
                "INSERT INTO subscriptions VALUES($1,$2,'all') ON CONFLICT DO NOTHING",
                identity.id,
                bootstrap["general_id"],
            )

            if bootstrap.get("off_topic_id"):
                await c.execute(
                    "INSERT INTO subscriptions VALUES($1,$2,'all') ON CONFLICT DO NOTHING",
                    identity.id,
                    bootstrap["off_topic_id"],
                )

    async def get(self, agent_id: UUID) -> AgentIdentity:
        raw = await self.pool.fetchval("SELECT identity FROM agents WHERE id=$1", agent_id)
        if raw is None:
            raise KeyError(agent_id)
        return AgentIdentity.model_validate(decode(raw))

    async def effective_prompt(self, agent_id: UUID) -> str:
        prompt = await self.pool.fetchval(
            "SELECT system_prompt FROM agent_prompt_versions WHERE agent_id=$1 "
            "ORDER BY created_at DESC LIMIT 1",
            agent_id,
        )
        return prompt or (await self.get(agent_id)).system_prompt

    async def public_profile(self, agent_id: UUID) -> dict[str, Any]:
        row = await self.pool.fetchrow("SELECT * FROM agent_profiles WHERE agent_id=$1", agent_id)
        return dict(row) if row else {}

    async def list_agents(self, workspace_id: UUID) -> Sequence[AgentIdentity]:
        rows = await self.pool.fetch(
            "SELECT identity FROM agents WHERE workspace_id=$1 ORDER BY created_at,id", workspace_id
        )
        return [AgentIdentity.model_validate(decode(row["identity"])) for row in rows]

    async def set_state(self, agent_id: UUID, state: str) -> None:
        await self.pool.execute(
            "UPDATE agents SET state=$2 WHERE id=$1 AND state!='TERMINATED'", agent_id, state
        )

    async def state(self, agent_id: UUID) -> dict[str, Any]:
        row = await self.pool.fetchrow(
            """SELECT a.*,w.state workspace_state,w.started,w.general_id,w.owner_id,w.server_id,
            w.team_id,GREATEST(w.created_ms,a.birth_ms) created_ms,w.limits
            FROM agents a JOIN workspaces w ON w.id=a.workspace_id
            WHERE a.id=$1""",
            agent_id,
        )
        if row is None:
            raise KeyError(agent_id)
        return dict(row)

    async def record_failure(self, agent: UUID, failure: dict[str, Any], *, blocked: bool) -> None:
        async with self.pool.acquire() as c, c.transaction():
            await c.execute(
                "UPDATE agents SET failure=$2,state=CASE "
                "WHEN state='TERMINATED' THEN state "
                "WHEN control_paused OR (SELECT state FROM workspaces WHERE id=workspace_id) "
                "!='active' THEN 'READY' ELSE $3 END WHERE id=$1",
                agent,
                json.dumps(failure),
                "QUOTA_BLOCKED" if blocked else "FAILED",
            )
            await c.execute(
                "INSERT INTO events(workspace_id,actor_id,type,data) "
                "SELECT workspace_id,id,'agent_execution_blocked',$2 FROM agents WHERE id=$1",
                agent,
                json.dumps(failure),
            )

    async def set_availability(self, agent_id: UUID, availability: str) -> None:
        if availability not in {"available", "away"}:
            raise ValueError("availability must be available or away")
        await self.pool.execute(
            "UPDATE agents SET availability=$2 WHERE id=$1", agent_id, availability
        )
        await self.pool.execute("SELECT pg_notify('harness_wake',$1)", str(agent_id))

    async def enqueue(self, notification: Notification) -> bool:
        return await self.ingest(
            notification.recipient_agent_id,
            notification.post,
            notification.kind,
            notification.server_id,
        )

    async def ingest(
        self, agent_id: UUID, post: Post, kind: str, server_id: str, delay_sec: float = 0
    ) -> bool:
        preview = post.model_copy(
            update={
                "message": post.message[:1200],
                "content_hash": sha256(post.message.encode()).hexdigest(),
            }
        )
        revision = (
            post.update_at
            if kind == "post_edited"
            else post.delete_at
            if kind == "post_deleted"
            else 0
        )
        if kind == "post_deleted":
            preview.message = "[Post deleted; earlier content is no longer current.]"
        async with self.pool.acquire() as c, c.transaction():
            row = await c.fetchrow(
                """SELECT a.identity,w.owner_id,w.general_id,w.seed_id,w.id workspace_id
                FROM agents a
                JOIN workspaces w ON w.id=a.workspace_id WHERE a.id=$1""",
                agent_id,
            )
            assert row is not None
            # FK inserts acquire agent key-share locks. Take the workspace lock first,
            # matching budget/spawn/termination, before any cursor or receipt insert.
            await c.execute("SELECT id FROM workspaces WHERE id=$1 FOR UPDATE", row["workspace_id"])
            # Receipt cursor and deduplication are committed together.
            await c.execute(
                """INSERT INTO message_cursors VALUES($1,$2,$3,$4,$5) ON CONFLICT
                (agent_id,server_id,channel_id) DO UPDATE SET
                last_seen_at=GREATEST(message_cursors.last_seen_at,EXCLUDED.last_seen_at),
                last_seen_post_id=CASE
                WHEN EXCLUDED.last_seen_at>message_cursors.last_seen_at
                THEN EXCLUDED.last_seen_post_id
                WHEN EXCLUDED.last_seen_at=message_cursors.last_seen_at THEN
                GREATEST(message_cursors.last_seen_post_id,EXCLUDED.last_seen_post_id)
                ELSE message_cursors.last_seen_post_id END""",
                agent_id,
                server_id,
                post.channel_id,
                max(post.create_at, post.update_at, post.delete_at),
                post.id,
            )
            if post.user_id == decode(row["identity"])["mattermost_user_id"]:
                return False
            if (
                post.channel_id == row["general_id"]
                and agent_id != row["seed_id"]
                and kind != "idle_channel_post"
            ):
                username = await c.fetchval(
                    "SELECT username FROM agent_profiles WHERE agent_id=$1", agent_id
                )
                username = username or decode(row["identity"])["mattermost_username"]
                known_amendment = kind in {"post_edited", "post_deleted"} and await c.fetchval(
                    "SELECT 1 FROM notifications WHERE agent_id=$1 AND post_id=$2 LIMIT 1",
                    agent_id,
                    post.id,
                )
                if not addressed(post.message, username) and not known_amendment:
                    return False
            immediate = False
            if (
                kind in {"posted", "channel_post"}
                and not post.delete_at
                and post.user_id == row["owner_id"]
                and post.channel_id == row["general_id"]
            ):
                first = await c.fetchrow(
                    "SELECT agent_id FROM first_deliveries WHERE workspace_id=$1 AND post_id=$2",
                    row["workspace_id"],
                    post.id,
                )
                if first is None and not await c.fetchval(
                    "SELECT 1 FROM notifications n JOIN agents a ON a.id=n.agent_id "
                    "WHERE a.workspace_id=$1 AND n.post_id=$2 LIMIT 1",
                    row["workspace_id"],
                    post.id,
                ):
                    recipient = await c.fetchval(
                        "SELECT a.id FROM agents a LEFT JOIN agent_profiles p ON p.agent_id=a.id "
                        "WHERE a.workspace_id=$1 AND a.availability='available' "
                        "AND a.state IN ('IDLE','READY','RUNNING') "
                        "AND NOT a.control_paused AND a.birth_ms<=$2 "
                        "AND (a.id=$4 OR $3 ~ ('(^|[^[:alnum:]_])@' || "
                        "COALESCE(p.username,a.identity->>'mattermost_username') || "
                        "'([^[:alnum:]_-]|$)')) "
                        "ORDER BY ($3 ~ ('(^|[^[:alnum:]_])@' || "
                        "COALESCE(p.username,a.identity->>'mattermost_username') || "
                        "'([^[:alnum:]_-]|$)')) DESC, "
                        "(a.parent_id IS NULL) DESC,(a.state='IDLE') DESC,"
                        "a.created_at,a.id LIMIT 1",
                        row["workspace_id"],
                        post.create_at,
                        post.message,
                        row["seed_id"],
                    )
                    await c.execute(
                        "INSERT INTO first_deliveries VALUES($1,$2,$3)",
                        row["workspace_id"],
                        post.id,
                        recipient,
                    )
                    immediate = recipient == agent_id
                elif first:
                    immediate = first["agent_id"] == agent_id
                if immediate:
                    delay_sec = 0
                    # Deliver the preceding inbox prefix too, preserving chronological order.
                    await c.execute(
                        "UPDATE notifications SET available_at=LEAST(available_at,now()) "
                        "WHERE agent_id=$1 AND state='unread'",
                        agent_id,
                    )

            if (
                kind in {"posted", "channel_post"}
                and not post.delete_at
                and post.user_id == row["owner_id"]
                and post.channel_id == row["general_id"]
            ):
                await c.execute(
                    "UPDATE workspaces SET started=true,question=COALESCE(question,$2) WHERE id=$1",
                    row["workspace_id"],
                    post.message,
                )
            if kind == "post_edited":
                previous = await c.fetchval(
                    "SELECT post_preview FROM notifications WHERE agent_id=$1 AND post_id=$2 "
                    "AND kind IN ('posted','channel_post','idle_channel_post','dm') "
                    "ORDER BY sequence LIMIT 1",
                    agent_id,
                    post.id,
                )
                if previous:
                    previous = decode(previous)
                    same = (
                        previous.get("content_hash") == preview.content_hash
                        if previous.get("content_hash")
                        else previous.get("message") == preview.message
                    )
                    if same:
                        return False  # Thread counters/metadata changes are not new instructions.
            if kind in {"post_edited", "post_deleted"}:
                # Update pending instructions, preserving delivered history.
                await c.execute(
                    (
                        "UPDATE notifications SET post_preview=$3 WHERE agent_id=$1 AND "
                        "post_id=$2 AND state='unread' AND kind IN "
                        "('posted','channel_post','idle_channel_post','dm')"
                    ),
                    agent_id,
                    post.id,
                    preview.model_dump_json(),
                )
            # Live delivery and idle catch-up share one original-post receipt.
            if kind in {"posted", "channel_post", "idle_channel_post"} and await c.fetchval(
                "SELECT 1 FROM notifications WHERE agent_id=$1 AND server_id=$2 AND post_id=$3 "
                "AND kind IN ('posted','channel_post','idle_channel_post') LIMIT 1",
                agent_id,
                server_id,
                post.id,
            ):
                return False
            inserted = await c.fetchval(
                """INSERT INTO notifications
                (id,agent_id,server_id,post_id,kind,revision,post_preview,available_at,immediate_delivery)
                VALUES($1,$2,$3,$4,$5,$6,$7,now()+$8*interval '1 second',$9)
                ON CONFLICT DO NOTHING RETURNING id""",
                uuid4(),
                agent_id,
                server_id,
                post.id,
                kind,
                revision,
                preview.model_dump_json(),
                delay_sec,
                immediate,
            )
            if inserted:
                await c.execute(
                    (
                        "INSERT INTO events(workspace_id,actor_id,type,data) "
                        "VALUES($1,$2,'notification_received',$3)"
                    ),
                    row["workspace_id"],
                    agent_id,
                    json.dumps({"post_id": post.id, "kind": kind}),
                )
        return inserted is not None

    async def reserve(self, agent_id: UUID, limit: int) -> Sequence[Notification]:
        async with self.pool.acquire() as c, c.transaction():
            rows = await c.fetch(
                """UPDATE notifications SET state='reserved' WHERE id IN
                (SELECT id FROM notifications WHERE agent_id=$1 AND state='unread'
                AND available_at<=now() AND sequence<COALESCE(
                  (SELECT min(sequence) FROM notifications WHERE agent_id=$1
                   AND state='unread' AND available_at>now()),9223372036854775807)
                ORDER BY sequence LIMIT $2 FOR UPDATE SKIP LOCKED) RETURNING *""",
                agent_id,
                limit,
            )
        return self.items(sorted(rows, key=lambda r: r["sequence"]))

    async def immediate_pending(self, agent_id: UUID) -> bool:
        return bool(
            await self.pool.fetchval(
                "SELECT 1 FROM notifications WHERE agent_id=$1 AND state='unread' "
                "AND immediate_delivery LIMIT 1",
                agent_id,
            )
        )

    async def pending_delivery(self, agent_id: UUID) -> tuple[float, str]:
        row = await self.pool.fetchrow(
            "SELECT EXTRACT(EPOCH FROM available_at-now()) delay, "
            "post_preview->>'channel_id' channel FROM notifications "
            "WHERE agent_id=$1 AND state='unread' ORDER BY sequence LIMIT 1",
            agent_id,
        )
        return (max(0.0, float(row["delay"])), row["channel"]) if row else (0.0, "")

    @staticmethod
    def items(rows: Sequence[asyncpg.Record]) -> list[Notification]:
        return [
            Notification(
                id=r["id"],
                recipient_agent_id=r["agent_id"],
                server_id=r["server_id"],
                post=Post.model_validate(decode(r["post_preview"])),
                kind=r["kind"],
                state=r["state"],
            )
            for r in rows
        ]

    async def page(
        self, agent_id: UUID, limit: int = 50, cursor: int = 0
    ) -> tuple[list[Notification], int]:
        rows = await self.pool.fetch(
            (
                "SELECT * FROM notifications WHERE agent_id=$1 AND state='unread' "
                "AND sequence>$2 ORDER BY sequence LIMIT $3"
            ),
            agent_id,
            cursor,
            limit,
        )
        return self.items(rows), int(rows[-1]["sequence"]) if rows else cursor

    async def acknowledge(self, notification_ids: Sequence[UUID]) -> None:
        await self.pool.execute(
            "UPDATE notifications SET state='consumed' WHERE id=ANY($1::uuid[])",
            list(notification_ids),
        )

    async def release(self, notification_ids: Sequence[UUID]) -> None:
        await self.pool.execute(
            "UPDATE notifications SET state='unread',reserved_turn=NULL WHERE id=ANY($1::uuid[])",
            list(notification_ids),
        )

    async def unread_count(self, agent_id: UUID) -> int:
        return int(
            await self.pool.fetchval(
                "SELECT count(*) FROM notifications WHERE agent_id=$1 AND state!='consumed'",
                agent_id,
            )
        )

    async def history(self, agent_id: UUID) -> list[dict[str, Any]]:
        rows = await self.pool.fetch(
            "SELECT item FROM conversations WHERE agent_id=$1 ORDER BY sequence", agent_id
        )
        return [decode(row["item"]) for row in rows]

    async def append(self, agent_id: UUID, items: Sequence[dict[str, Any]]) -> None:
        async with self.pool.acquire() as c, c.transaction():
            for item in items:
                await c.execute(
                    "INSERT INTO conversations(agent_id,item) VALUES($1,$2)",
                    agent_id,
                    json.dumps(item),
                )

    async def reserve_cost(
        self,
        agent_id: UUID,
        amount: Decimal,
        request: list[dict[str, Any]],
        *,
        provider_model_id: str | None = None,
    ) -> UUID:
        turn_id = uuid4()
        async with self.pool.acquire() as c, c.transaction():
            a = await c.fetchrow("SELECT * FROM agents WHERE id=$1", agent_id)
            assert a is not None
            w = await c.fetchrow(
                "SELECT * FROM workspaces WHERE id=$1 FOR UPDATE", a["workspace_id"]
            )
            assert w is not None
            a = await c.fetchrow("SELECT * FROM agents WHERE id=$1 FOR UPDATE", agent_id)
            assert a is not None
            if (
                w["state"] != "active"
                or a["state"] in {"TERMINATED", "FAILED", "QUOTA_BLOCKED"}
                or a["control_paused"]
            ):
                raise QuotaError("workspace/agent state blocks inference")
            await c.execute(
                "UPDATE agents SET reserved=reserved+$2,turns=turns+1 WHERE id=$1", agent_id, amount
            )
            await c.execute(
                "UPDATE workspaces SET reserved=reserved+$2 WHERE id=$1", w["id"], amount
            )
            await c.execute(
                "INSERT INTO agent_turns(id,agent_id,request,provider_model_id) "
                "VALUES($1,$2,$3,$4)",
                turn_id,
                agent_id,
                json.dumps(request),
                provider_model_id or a["model_id"],
            )
            await c.execute(
                "INSERT INTO usage_reservations VALUES($1,$2,$3,$4,'reserved')",
                turn_id,
                agent_id,
                w["id"],
                amount,
            )
        return turn_id

    async def reserve_usage(self, agent_id: UUID, amount_usd: Decimal) -> UUID:
        return await self.reserve_cost(agent_id, amount_usd, [])

    async def record(self, reservation_id: UUID, usage: Usage) -> None:
        await self.finish(reservation_id, usage)

    async def observe_usage(self, turn_id: UUID, usage: Usage) -> None:
        """Publish confirmed intermediate counters on the same row later settled by finish."""
        await self.pool.execute(
            "UPDATE agent_turns SET input_tokens=$2,output_tokens=$3 WHERE id=$1 "
            "AND state='prepared' AND (input_tokens IS DISTINCT FROM $2 "
            "OR output_tokens IS DISTINCT FROM $3)",
            turn_id,
            usage.input_tokens,
            usage.output_tokens,
        )

    async def finish(
        self,
        turn_id: UUID,
        usage: Usage | None,
        response: ModelResponse | None = None,
        inputs: list[dict[str, Any]] | None = None,
        ids: Sequence[UUID] = (),
    ) -> None:
        async with self.pool.acquire() as c, c.transaction():
            r = await c.fetchrow("SELECT * FROM usage_reservations WHERE id=$1 FOR UPDATE", turn_id)
            assert r is not None
            if r["state"] != "reserved":
                return
            await c.execute("SELECT id FROM workspaces WHERE id=$1 FOR UPDATE", r["workspace_id"])
            cost = usage.cost_usd if usage else r["amount"]
            await c.execute(
                "UPDATE agents SET spent=spent+$2,reserved=reserved-$3 WHERE id=$1",
                r["agent_id"],
                cost,
                r["amount"],
            )
            await c.execute(
                "UPDATE workspaces SET spent=spent+$2,reserved=reserved-$3 WHERE id=$1",
                r["workspace_id"],
                cost,
                r["amount"],
            )
            await c.execute(
                "UPDATE usage_reservations SET state=$2 WHERE id=$1",
                turn_id,
                "settled" if usage else "uncertain",
            )
            await c.execute(
                (
                    "UPDATE agent_turns SET "
                    "state=$2,cost=$3,input_tokens=coalesce($4,input_tokens),"
                    "output_tokens=coalesce($5,output_tokens),output=$6,prov"
                    "ider_request_id=$7,provider_usage=$8,finished_at=now() WHERE id=$1"
                ),
                turn_id,
                "completed" if response or usage else "unknown",
                cost,
                usage.input_tokens if usage else None,
                usage.output_tokens if usage else None,
                json.dumps(response.message) if response else None,
                response.message.get("id") if response else None,
                json.dumps(usage.provider_usage if usage else {}),
            )
            if response:
                for item in (inputs or []) + response.message["output"]:
                    await c.execute(
                        "INSERT INTO conversations(agent_id,item) VALUES($1,$2)",
                        r["agent_id"],
                        json.dumps(item),
                    )
                await c.execute(
                    "UPDATE notifications SET state='consumed' WHERE id=ANY($1::uuid[])", list(ids)
                )
            await c.execute(
                "INSERT INTO events(workspace_id,actor_id,type,data) VALUES($1,$2,'model_turn',$3)",
                r["workspace_id"],
                r["agent_id"],
                json.dumps(
                    {"turn_id": str(turn_id), "cost_usd": str(cost), "uncertain": usage is None}
                ),
            )

    async def recover(self, agent_id: UUID) -> None:
        turns = await self.pool.fetch(
            "SELECT id FROM usage_reservations WHERE agent_id=$1 AND state='reserved'", agent_id
        )
        for turn in turns:
            await self.finish(turn["id"], None)
        await self.pool.execute(
            (
                "UPDATE notifications SET state='unread',reserved_turn=NULL WHERE "
                "agent_id=$1 AND state='reserved'"
            ),
            agent_id,
        )
        await self.pool.execute(
            "UPDATE agents SET state='IDLE' WHERE id=$1 AND state='RUNNING'", agent_id
        )

    async def apply_main_routing(self, workspace: UUID) -> None:
        """Drop stale, undelivered general chatter after the owner changes intake policy."""
        async with self.pool.acquire() as c, c.transaction():
            await c.execute("SELECT id FROM workspaces WHERE id=$1 FOR UPDATE", workspace)
            rows = await c.fetch(
                "SELECT n.id,n.post_preview,"
                "COALESCE(p.username,a.identity->>'mattermost_username') username "
                "FROM notifications n JOIN agents a ON a.id=n.agent_id "
                "JOIN workspaces w ON w.id=a.workspace_id "
                "LEFT JOIN agent_profiles p ON p.agent_id=a.id "
                "WHERE w.id=$1 AND a.id!=w.seed_id AND n.state IN ('unread','reserved') "
                "AND n.kind IN ('posted','channel_post') "
                "AND n.post_preview->>'channel_id'=w.general_id",
                workspace,
            )
            stale = [
                r["id"]
                for r in rows
                if not addressed(decode(r["post_preview"])["message"], r["username"])
            ]
            if stale:
                await c.execute(
                    "UPDATE notifications SET state='consumed',reserved_turn=NULL "
                    "WHERE id=ANY($1::uuid[])",
                    stale,
                )

    async def slash_control(
        self, workspace: UUID, owner: str, channel: str, command: str, request_id: str
    ) -> dict[str, Any]:
        if command not in {"/stop", "/pause", "/continue"}:
            raise ValueError("UNKNOWN_CONTROL")
        async with self.pool.acquire() as c, c.transaction():
            w = await c.fetchrow("SELECT * FROM workspaces WHERE id=$1 FOR UPDATE", workspace)
            if not w or owner != w["owner_id"]:
                raise ValueError("OWNER_ONLY_CONTROL")
            previous = await c.fetchval(
                "SELECT result FROM slash_control_receipts WHERE workspace_id=$1 AND request_id=$2",
                workspace,
                request_id,
            )
            if previous:
                return dict(decode(previous))
            if await c.fetchval(
                "SELECT EXISTS(SELECT 1 FROM reset_requests "
                "WHERE workspace_id=$1 AND status='pending')",
                workspace,
            ):
                raise ValueError("RESET_IN_PROGRESS")
            if w["state"] == "stopped":
                raise ValueError("WORKSPACE_PERMANENTLY_STOPPED")
            paused = command != "/continue"
            global_scope = channel == w["general_id"]
            if global_scope:
                await c.execute(
                    "UPDATE workspaces SET state=$2 WHERE id=$1",
                    workspace,
                    "paused" if paused else "active",
                )
                if not paused:
                    await c.execute(
                        "UPDATE agents SET control_paused=false WHERE workspace_id=$1",
                        workspace,
                    )
                affected = await c.fetchval(
                    "SELECT count(*) FROM agents WHERE workspace_id=$1 AND state!='TERMINATED'",
                    workspace,
                )
            else:
                ids = await c.fetch(
                    "UPDATE agents SET control_paused=$3 WHERE workspace_id=$1 "
                    "AND state!='TERMINATED' AND id IN "
                    "(SELECT agent_id FROM subscriptions WHERE channel_id=$2 "
                    "AND mode IN ('all','mentions')) RETURNING id",
                    workspace,
                    channel,
                    paused,
                )
                affected = len(ids)
            if not paused:
                await c.execute(
                    "UPDATE agents SET state='READY',failure=NULL WHERE workspace_id=$1 "
                    "AND state IN ('FAILED','QUOTA_BLOCKED') AND failure IS NOT NULL "
                    "AND ($2 OR id IN (SELECT agent_id FROM subscriptions "
                    "WHERE channel_id=$3 AND mode IN ('all','mentions')))",
                    workspace,
                    global_scope,
                    channel,
                )
            result = {
                "scope": "workspace" if global_scope else "channel",
                "affected_agents": affected,
                "paused": paused,
                "workspace_paused": paused if global_scope else w["state"] == "paused",
            }
            await c.execute(
                "INSERT INTO slash_control_receipts VALUES($1,$2,$3,$4,$5,now())",
                workspace,
                request_id,
                channel,
                command,
                json.dumps(result),
            )
            await c.execute(
                "INSERT INTO events(workspace_id,type,data) VALUES($1,'owner_slash_control',$2)",
                workspace,
                json.dumps({"command": command, "channel_id": channel, **result}),
            )
            await c.execute("SELECT pg_notify('harness_control',$1)", str(workspace))
            return result

    async def stop(self, workspace_id: UUID) -> None:
        async with self.pool.acquire() as c, c.transaction():
            await c.execute("UPDATE workspaces SET state='stopped' WHERE id=$1", workspace_id)
            await c.execute(
                "UPDATE agents SET state='TERMINATED' WHERE workspace_id=$1", workspace_id
            )
            await c.execute(
                "INSERT INTO events(workspace_id,type,data) VALUES($1,'owner_stop','{}')",
                workspace_id,
            )
            await c.execute("SELECT pg_notify('harness_stop',$1)", str(workspace_id))

    async def save_tool_result(
        self, agent_id: UUID, call_id: str, result: dict[str, Any], ids: Sequence[UUID] = ()
    ) -> None:
        async with self.pool.acquire() as c, c.transaction():
            await c.execute(
                (
                    "UPDATE tool_calls SET state='completed',result=$3 WHERE "
                    "agent_id=$1 AND call_id=$2"
                ),
                agent_id,
                call_id,
                json.dumps(result),
            )
            await c.execute(
                "INSERT INTO conversations(agent_id,item) VALUES($1,$2)",
                agent_id,
                json.dumps(
                    {
                        "type": "function_call_output",
                        "call_id": call_id,
                        "output": json.dumps(result),
                    }
                ),
            )
            await c.execute(
                "UPDATE notifications SET state='consumed' WHERE id=ANY($1::uuid[])", list(ids)
            )

    async def recover_tools(self, agent_id: UUID) -> None:
        history = await self.history(agent_id)
        answered = {i["call_id"] for i in history if i.get("type") == "function_call_output"}
        for item in history:
            if item.get("type") == "function_call" and item["call_id"] not in answered:
                await self.save_tool_result(
                    agent_id,
                    item["call_id"],
                    {
                        "error": "INTERRUPTED_TOOL_CALL",
                        "detail": (
                            "Result uncertain after restart; check Mattermost history before "
                            "repeating external sends."
                        ),
                    },
                )

    async def continuation_pending(self, agent_id: UUID) -> bool:
        history = await self.history(agent_id)
        if not history or history[-1].get("type") != "function_call_output":
            return False
        latest = await self.pool.fetchval(
            "SELECT output FROM agent_turns WHERE agent_id=$1 AND state='completed' "
            "ORDER BY started_at DESC LIMIT 1",
            agent_id,
        )
        # An empty completed response still ends activation; it must not resurrect
        # the previous exchange merely because no new conversation item was appended.
        return latest is None or decode(latest).get("output") != []
