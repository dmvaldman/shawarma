"""Workspace-scoped, cursor-paginated activity for the local owner dashboard."""

from typing import Any
from uuid import UUID

from aiohttp import web

from harness.coding.clients import OpenCode
from harness.core.activity import Activity, decoded


class AgentActivity:
    def __init__(self, store: Any, settings: Any, workspace: UUID):
        self.store, self.settings, self.workspace = store, settings, workspace
        self.capture = Activity(store, settings)
        self.backfilled: set[UUID] = set()

    async def page(self, request: web.Request) -> web.Response:
        try:
            agent = UUID(request.match_info["agent"])
            before = int(request.query["before"]) if "before" in request.query else None
            limit = int(request.query.get("limit", "30"))
            if not 1 <= limit <= 100 or (
                before is not None and not 0 < before <= 9223372036854775807
            ):
                raise ValueError()
        except ValueError:
            raise web.HTTPBadRequest(text="Invalid agent/cursor/page size") from None
        row = await self.store.pool.fetchrow(
            "SELECT a.state,a.control_paused,a.credential,a.availability,"
            "coalesce(p.username,a.identity->>'mattermost_username') username,"
            "w.state workspace_state,w.started FROM agents a "
            "JOIN workspaces w ON w.id=a.workspace_id "
            "LEFT JOIN agent_profiles p ON p.agent_id=a.id WHERE a.id=$1 AND a.workspace_id=$2",
            agent,
            self.workspace,
        )
        if not row:
            raise web.HTTPNotFound()
        if row["credential"] and row["credential"] not in self.capture.secrets:
            self.capture.secrets.append(row["credential"])
        identity = await self.store.pool.fetchrow(
            "SELECT token,password FROM gitea_identities WHERE agent_id=$1", agent
        )
        if identity:
            self.capture.secrets.extend(v for v in identity.values() if v)
        await self.capture.best_effort(self.capture.core(agent))
        unavailable = False
        if agent not in self.backfilled:
            session = await self.store.pool.fetchrow(
                "SELECT * FROM coding_sessions WHERE agent_id=$1", agent
            )
            if session:
                client = OpenCode(self.settings)
                try:
                    messages = await client.request(
                        "GET", f"/session/{session['session_id']}/message", session["directory"]
                    )
                    await self.capture.native(agent, dict(session), messages)
                    self.backfilled.add(agent)
                except Exception:
                    unavailable = True
                finally:
                    await client.close()
        rows = await self.store.pool.fetch(
            "SELECT sequence,source_key,kind,phase,summary,payload,occurred_at,created_at "
            "FROM agent_activity WHERE agent_id=$1 AND ($2::bigint IS NULL OR sequence<$2) "
            "ORDER BY sequence DESC LIMIT $3",
            agent,
            before,
            limit + 1,
        )
        working = (
            row["state"] == "RUNNING"
            and row["workspace_state"] == "active"
            and not row["control_paused"]
            and row["started"]
        )
        current = (
            await self.store.pool.fetch(
                "SELECT source_key,kind,phase,summary,payload,occurred_at,updated_at "
                "FROM agent_activity_sources WHERE agent_id=$1 AND phase='running' "
                "ORDER BY updated_at DESC LIMIT 10",
                agent,
            )
            if working
            else []
        )

        def entry(value: Any) -> dict[str, Any]:
            data = dict(value)
            data["payload"] = self.capture.safe(decoded(data["payload"]))
            for field in ("occurred_at", "created_at", "updated_at"):
                if field in data:
                    data[field] = data[field].isoformat()
            return data

        return web.json_response(
            {
                "agent": {
                    "id": str(agent),
                    "username": row["username"],
                    "state": row["state"],
                    "availability": row["availability"],
                    "working": working,
                },
                "events": [entry(v) for v in rows[:limit]],
                "current": [entry(v) for v in current],
                "next_cursor": rows[limit - 1]["sequence"] if len(rows) > limit else None,
                "source_unavailable": unavailable,
                "reasoning_note": "Reasoning is included only when exposed by provider/OpenCode; "
                "hidden or omitted reasoning cannot be recovered.",
            },
            headers={"Cache-Control": "no-store"},
        )
