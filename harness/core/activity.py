"""Append-only activity milestones plus a mutable projection of streaming work."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime
from hashlib import sha256
from typing import TYPE_CHECKING, Any
from uuid import UUID

from pydantic import SecretStr

from harness.config import Settings

if TYPE_CHECKING:
    from harness.persistence.store import Store

logger = logging.getLogger(__name__)


def decoded(value: Any) -> Any:
    return json.loads(value) if isinstance(value, str) else value


class Activity:
    def __init__(self, store: Store, settings: Settings):
        self.store, self.settings = store, settings
        self.secrets = [
            v.get_secret_value()
            for v in settings.__dict__.values()
            if isinstance(v, SecretStr) and v.get_secret_value()
        ]
        self.seen: dict[tuple[UUID, str], str] = {}

    def safe(self, value: Any) -> Any:
        if isinstance(value, dict):
            return {
                k: (
                    "[redacted]"
                    if k.lower()
                    in {
                        "authorization",
                        "api_key",
                        "apikey",
                        "password",
                        "credential",
                        "token",
                        "encrypted_content",
                        "reasoningencryptedcontent",
                        "access_token",
                        "refresh_token",
                        "bot_token",
                    }
                    else self.safe(v)
                )
                for k, v in value.items()
            }
        if isinstance(value, list):
            return [self.safe(v) for v in value]
        if isinstance(value, str):
            for secret in self.secrets:
                value = value.replace(secret, "[redacted]")
        return value

    async def record(
        self,
        agent: UUID,
        key: str,
        kind: str,
        phase: str,
        summary: str,
        payload: Any,
        timestamp: Any = None,
    ) -> None:
        await self.record_many(agent, [(key, kind, phase, summary, payload, timestamp)])

    async def record_many(self, agent: UUID, records: list[tuple[Any, ...]]) -> None:
        prepared = []
        for key, kind, phase, summary, payload, timestamp in records:
            data = self.safe(payload)
            encoded = json.dumps(data, ensure_ascii=False)
            fingerprint = sha256((phase + encoded).encode()).hexdigest()
            if self.seen.get((agent, key)) == fingerprint:
                continue
            at = datetime.now(UTC)
            if isinstance(timestamp, (int, float)) and 0 < timestamp < 253402300799000:
                at = datetime.fromtimestamp(timestamp / 1000, UTC)
            elif isinstance(timestamp, datetime):
                at = timestamp
            prepared.append((agent, key, kind, phase, summary, fingerprint, encoded, at))
        for offset in range(0, len(prepared), 200):
            chunk = prepared[offset : offset + 200]
            async with self.store.pool.acquire() as c, c.transaction():
                await c.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended($1,0))", "activity:" + str(agent)
                )
                previous = {
                    row["source_key"]: row
                    for row in await c.fetch(
                        "SELECT source_key,fingerprint,phase,kind,payload "
                        "FROM agent_activity_sources "
                        "WHERE agent_id=$1 AND source_key=ANY($2::text[])",
                        agent,
                        [v[1] for v in chunk],
                    )
                }
                changed, events = [], []
                for value in chunk:
                    _, key, _, phase, _, fingerprint, _, _ = value
                    old = previous.get(key)
                    if old and old["fingerprint"] == fingerprint:
                        continue
                    if old and phase == "running":
                        if old["phase"] != "running":
                            continue  # Late backfill cannot reopen completed/interrupted work.
                        previous_text = decoded(old["payload"]).get("part", {}).get("text")
                        incoming_text = decoded(value[6]).get("part", {}).get("text")
                        if (
                            isinstance(previous_text, str)
                            and isinstance(incoming_text, str)
                            and previous_text.startswith(incoming_text)
                            and len(previous_text) > len(incoming_text)
                        ):
                            continue  # Ignore an older partial snapshot racing the live observer.
                    changed.append(value)
                    if not old or old["phase"] != phase or phase != "running":
                        events.append(value[:5] + value[6:])
                if events:
                    await c.executemany(
                        "INSERT INTO agent_activity(agent_id,source_key,kind,phase,summary,"
                        "payload,occurred_at) VALUES($1,$2,$3,$4,$5,$6,$7)",
                        events,
                    )
                if changed:
                    await c.executemany(
                        "INSERT INTO agent_activity_sources(agent_id,source_key,kind,phase,summary,"
                        "fingerprint,payload,occurred_at) VALUES($1,$2,$3,$4,$5,$6,$7,$8) "
                        "ON CONFLICT(agent_id,source_key) DO UPDATE SET kind=EXCLUDED.kind,"
                        "phase=EXCLUDED.phase,summary=EXCLUDED.summary,fingerprint=EXCLUDED.fingerprint,"
                        "payload=EXCLUDED.payload,updated_at=now()",
                        [v[:6] + (v[6] if v[3] == "running" else "{}",) + v[7:] for v in changed],
                    )
            for value in chunk:
                self.seen[(agent, value[1])] = value[5]

    async def native(
        self, agent: UUID, session: dict[str, Any], messages: list[dict[str, Any]]
    ) -> None:
        credential = await self.store.pool.fetchrow(
            "SELECT token,password FROM gitea_identities WHERE agent_id=$1", agent
        )
        if credential:
            for value in credential.values():
                if value and value not in self.secrets:
                    self.secrets.append(value)
        records: list[tuple[Any, ...]] = []
        for message in messages:
            info = message["info"]
            timestamp = info.get("time", {}).get("created")
            base = "native:" + session["session_id"] + ":" + info["id"]
            for index, part in enumerate(message.get("parts", [])):
                kind = part.get("type", "unknown")
                if kind not in {"tool", "text", "reasoning", "step-finish"}:
                    continue
                key = base + ":" + str(part.get("id", index))
                if kind == "tool":
                    status = part.get("state", {}).get("status", "pending")
                    phase = (
                        "failed"
                        if status == "error"
                        else "completed"
                        if status == "completed"
                        else "running"
                    )
                    summary = str(part.get("tool", "Tool"))
                else:
                    phase = (
                        "completed"
                        if (
                            info.get("role") == "user"
                            or kind == "step-finish"
                            or part.get("time", {}).get("end")
                            or info.get("time", {}).get("completed")
                        )
                        else "running"
                    )
                    summary = (
                        "Incoming context"
                        if info.get("role") == "user"
                        else {
                            "text": "Assistant response",
                            "reasoning": "Reasoning summary"
                            if info.get("providerID") == "openai"
                            else "Reasoning",
                            "step-finish": "Model step usage",
                        }[kind]
                    )
                records.append(
                    (
                        key,
                        kind,
                        phase,
                        summary,
                        {
                            "role": info.get("role"),
                            "model": info.get("modelID"),
                            "provider": info.get("providerID"),
                            "part": part,
                        },
                        timestamp,
                    )
                )
            if info.get("error"):
                records.append(
                    (
                        base + ":error",
                        "error",
                        "failed",
                        "Model error",
                        info["error"],
                        timestamp,
                    )
                )

        await self.record_many(agent, records)

    async def tool(self, agent: UUID, call_id: str) -> None:
        row = await self.store.pool.fetchrow(
            "SELECT * FROM tool_calls WHERE agent_id=$1 AND call_id=$2", agent, call_id
        )
        if row:
            result = decoded(row["result"])
            phase = (
                "running"
                if row["state"] == "pending"
                else "failed"
                if result and result.get("error")
                else "completed"
            )
            await self.record(
                agent,
                "tool:" + call_id,
                "harness_tool",
                phase,
                row["tool_name"],
                {"arguments": decoded(row["arguments"]), "result": result},
                row["started_at"],
            )

    async def turn(self, agent: UUID, turn_id: UUID) -> None:
        row = await self.store.pool.fetchrow(
            "SELECT * FROM agent_turns WHERE id=$1 AND agent_id=$2", turn_id, agent
        )
        if row:
            phase = (
                "running"
                if row["state"] == "prepared"
                else "unknown"
                if row["state"] == "unknown"
                else "completed"
            )
            await self.record(
                agent,
                "turn:" + str(turn_id),
                "model_turn",
                phase,
                "Model activation",
                {
                    "state": row["state"],
                    "input_tokens": row["input_tokens"],
                    "output_tokens": row["output_tokens"],
                    "usage": decoded(row["provider_usage"]),
                    "output": decoded(row["output"])
                    if self.settings.agent_engine != "opencode"
                    else None,
                },
                row["started_at"],
            )

    async def core(self, agent: UUID) -> None:
        # Backfill existing receipts once; revisit only unfinished work thereafter.
        for row in await self.store.pool.fetch(
            "SELECT t.call_id FROM tool_calls t LEFT JOIN agent_activity_sources s "
            "ON s.agent_id=t.agent_id AND s.source_key='tool:'||t.call_id "
            "WHERE t.agent_id=$1 AND (s.source_key IS NULL OR s.phase='running') "
            "ORDER BY t.started_at",
            agent,
        ):
            await self.tool(agent, row["call_id"])
        for row in await self.store.pool.fetch(
            "SELECT t.id FROM agent_turns t LEFT JOIN agent_activity_sources s "
            "ON s.agent_id=t.agent_id AND s.source_key='turn:'||t.id::text "
            "WHERE t.agent_id=$1 AND (s.source_key IS NULL OR s.phase='running') "
            "ORDER BY t.started_at",
            agent,
        ):
            await self.turn(agent, row["id"])

    async def interrupt(self, agent: UUID) -> None:
        rows = await self.store.pool.fetch(
            "SELECT source_key,kind,summary,payload FROM agent_activity_sources "
            "WHERE agent_id=$1 AND phase='running'",
            agent,
        )
        await self.record_many(
            agent,
            [
                (
                    v["source_key"],
                    v["kind"],
                    "interrupted",
                    v["summary"],
                    decoded(v["payload"]),
                    None,
                )
                for v in rows
            ],
        )

    async def best_effort(self, operation: Any) -> None:
        try:
            async with asyncio.timeout(3):
                await operation
        except Exception:
            logger.warning("Activity capture unavailable; agent work continues")
