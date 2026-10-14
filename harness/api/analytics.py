"""Read-only local dashboard; aggregate ledger data without exposing conversations."""

import asyncio
import base64
import json
import logging
import time
from datetime import UTC, datetime
from importlib.resources import files
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID

from aiohttp import web

from harness.api.activity import AgentActivity
from harness.api.billing import Billing
from harness.api.git_stats import GitStats
from harness.config import Settings
from harness.mattermost.client import Client
from harness.persistence.store import Store


def activity(row: dict[str, Any], workspace: dict[str, Any], resetting: bool) -> str:
    if resetting:
        return "resetting"
    if row["state"] in {"TERMINATED", "FAILED", "QUOTA_BLOCKED"}:
        return {"TERMINATED": "retired", "FAILED": "failed", "QUOTA_BLOCKED": "blocked"}[
            row["state"]
        ]
    if workspace["state"] != "active" or row["control_paused"]:
        return "paused" if workspace["state"] != "stopped" else "stopped"
    if not workspace["started"]:
        return "idle"
    return (
        "working" if row["state"] == "RUNNING" else "ready" if row["state"] == "READY" else "idle"
    )


async def ledger_snapshot(
    store: Store, workspace_id: UUID, engine: str | None = None
) -> dict[str, Any]:
    # A repeatable-read snapshot prevents reset from mixing an old roster with new totals.
    async with store.pool.acquire() as c, c.transaction(isolation="repeatable_read", readonly=True):
        w = await c.fetchrow(
            "SELECT id,name,state,started,team_id,created_ms,spent,reserved FROM workspaces "
            "WHERE id=$1",
            workspace_id,
        )
        if w is None:
            raise KeyError(workspace_id)
        workspace = dict(w)
        resetting = bool(
            await c.fetchval(
                "SELECT EXISTS(SELECT 1 FROM reset_requests "
                "WHERE workspace_id=$1 AND status='pending')",
                workspace_id,
            )
        )
        rows = await c.fetch(
            """WITH usage AS (
                SELECT t.agent_id,coalesce(sum(input_tokens),0) input_tokens,
                  coalesce(sum(output_tokens),0) output_tokens,
                  count(*) FILTER (WHERE t.state='unknown') uncertain_calls,
                  count(*) FILTER (WHERE t.state='prepared') in_flight_calls
                FROM agent_turns t JOIN agents a ON a.id=t.agent_id
                WHERE a.workspace_id=$1 GROUP BY t.agent_id
            ), messages AS (
                SELECT agent_id,count(DISTINCT post_id) messages FROM (
                  SELECT t.agent_id,result->>'id' post_id
                  FROM tool_calls t JOIN agents a ON a.id=t.agent_id
                  WHERE a.workspace_id=$1 AND tool_name IN ('post_channel','gitea_request_review')
                    AND t.state='completed' AND result->>'id' IS NOT NULL
                    AND result->>'channel_id' IS NOT NULL
                    AND NOT (result ? 'error') AND NOT (result ? 'deferred')
                  UNION ALL SELECT s.agent_id,s.post_id FROM channel_streams s
                    JOIN agents a ON a.id=s.agent_id WHERE a.workspace_id=$1
                      AND s.post_id IS NOT NULL
                ) posted GROUP BY agent_id
            ), git_created AS (
                SELECT t.agent_id,
                  count(DISTINCT result->'data'->>'id') FILTER (WHERE
                    regexp_replace(split_part(arguments->>'path','?',1),'/$','')='/issues')
                    issues_created,
                  array_agg(DISTINCT result->'data'->>'number') FILTER (WHERE
                    regexp_replace(split_part(arguments->>'path','?',1),'/$','')='/pulls')
                    pr_numbers
                FROM tool_calls t JOIN agents a ON a.id=t.agent_id
                WHERE a.workspace_id=$1 AND tool_name='gitea_request' AND t.state='completed'
                  AND arguments->>'method'='POST' AND result->>'status'='201'
                  AND NOT (result ? 'error') GROUP BY t.agent_id
            ) SELECT a.id,a.parent_id,a.identity->>'mattermost_username' username,
                a.identity->>'mattermost_user_id' user_id,a.model_id,a.state,
                a.control_paused,a.failure,
                (SELECT max(updated_at) FROM agent_activity_sources x WHERE x.agent_id=a.id)
                  activity_updated_at,
                a.spent,a.reserved,coalesce(u.input_tokens,0)
                  +coalesce((av.usage->>'input_tokens')::bigint,0) input_tokens,
                coalesce(u.output_tokens,0)
                  +coalesce((av.usage->>'output_tokens')::bigint,0) output_tokens,
                coalesce(m.messages,0) messages,
                coalesce(u.uncertain_calls,0) uncertain_calls,
                coalesce(u.in_flight_calls,0) in_flight_calls,
                coalesce(g.issues_created,0) issues_created,g.pr_numbers git_pr_numbers,
                ar.repository_name,(av.image IS NOT NULL) avatar_ready,
                s.session_id opencode_session_id,s.directory opencode_directory,
                s.branch assigned_branch,s.current_branch,s.current_commit,s.branch_observed_at,
                s.current_tool native_tool,p.tool_name core_tool,
                EXISTS(SELECT 1 FROM coding_runs r WHERE r.agent_id=a.id AND r.state='running')
                  native_running
            FROM agents a LEFT JOIN usage u ON u.agent_id=a.id
              LEFT JOIN messages m ON m.agent_id=a.id
              LEFT JOIN git_created g ON g.agent_id=a.id
              LEFT JOIN agent_repositories ar ON ar.agent_id=a.id
              LEFT JOIN agent_avatars av ON av.agent_id=a.id
              LEFT JOIN coding_sessions s ON s.agent_id=a.id
              LEFT JOIN LATERAL (SELECT tool_name FROM tool_calls t
                WHERE t.agent_id=a.id AND t.state='pending'
                ORDER BY started_at DESC LIMIT 1) p ON true
            WHERE a.workspace_id=$1 ORDER BY a.parent_id IS NOT NULL,a.created_at,a.id""",
            workspace_id,
        )
        last_control = await c.fetchrow(
            "SELECT command,channel_id,created_at FROM slash_control_receipts "
            "WHERE workspace_id=$1 ORDER BY created_at DESC LIMIT 1",
            workspace_id,
        )
    agents = []
    for raw in rows:
        row = dict(raw)
        row["failure"] = (
            json.loads(row["failure"]) if isinstance(row["failure"], str) else row["failure"]
        )
        coordinator = row.pop("parent_id") is None
        agent_engine = "provider" if coordinator else engine
        row["avatar_url"] = (
            "/api/agents/" + str(row["id"]) + "/avatar" if row.pop("avatar_ready") else None
        )
        row["coordinator"] = coordinator
        row["activity"] = activity(row, workspace, resetting)
        if (
            row["activity"] == "working"
            and agent_engine == "opencode"
            and not row["native_running"]
        ):
            row["activity"] = "waiting"
        row["working"] = row["activity"] == "working"
        core_tool, native_tool = row.pop("core_tool"), row.pop("native_tool")
        tool = core_tool if coordinator else core_tool or native_tool
        row["current_tool"] = tool if row["working"] else None
        surface = (
            "Gitea"
            if tool and (tool.startswith("gitea_") or tool.startswith("wiki_"))
            else "Mattermost"
            if tool
            in {
                "post_channel",
                "begin_channel_post",
                "read_channel",
                "get_thread",
                "search_messages",
                "list_channels",
                "create_channel",
                "subscribe_channel",
                "unsubscribe_channel",
                "add_reaction",
                "remove_reaction",
                "get_notifications",
            }
            else "OpenCode"
            if agent_engine == "opencode"
            else "Mattermost"
            if coordinator
            else "model"
        )
        row["work_surface"] = surface if row["working"] else None
        row["activity_label"] = (
            "Working in " + surface
            if row["working"]
            else (
                "Waiting for OpenCode"
                if row["activity"] == "waiting"
                else row["activity"].capitalize()
            )
        )
        row["activity_updated_at"] = (
            row["activity_updated_at"].isoformat() if row["activity_updated_at"] else None
        )
        row["branch_observed_at"] = (
            row["branch_observed_at"].isoformat() if row["branch_observed_at"] else None
        )
        row["input_tokens"] = int(row["input_tokens"])
        row["output_tokens"] = int(row["output_tokens"])
        row["tokens"] = row["input_tokens"] + row["output_tokens"]
        row["estimated_cost_usd"] = str(row.pop("spent"))
        row["reserved_estimate_usd"] = str(row.pop("reserved"))
        row["id"] = str(row["id"])
        agents.append(row)
    status = "resetting" if resetting else workspace["state"]
    if status == "active" and not workspace["started"]:
        status = "waiting for task"
    return {
        "workspace": {
            "id": str(workspace_id),
            "name": workspace["name"],
            "status": status,
            "started": workspace["started"],
            "team_id": workspace["team_id"],
            "run_ms": workspace["created_ms"],
            "since": datetime.fromtimestamp(workspace["created_ms"] / 1000, UTC).isoformat(),
            "last_control": {
                "command": last_control["command"],
                "channel_id": last_control["channel_id"],
                "at": last_control["created_at"].isoformat(),
            }
            if last_control
            else None,
        },
        "totals": {
            "agents": sum(a["state"] != "TERMINATED" for a in agents),
            "agents_created": len(agents),
            "working": sum(a["working"] for a in agents),
            "input_tokens": sum(int(a["input_tokens"]) for a in agents),
            "output_tokens": sum(int(a["output_tokens"]) for a in agents),
            "tokens": sum(a["tokens"] for a in agents),
            "messages": sum(int(a["messages"]) for a in agents),
            "estimated_cost_usd": str(workspace["spent"]),
            "reserved_estimate_usd": str(workspace["reserved"]),
            "uncertain_calls": sum(a["uncertain_calls"] for a in agents),
            "in_flight_calls": sum(a["in_flight_calls"] for a in agents),
        },
        "agents": agents,
        "updated_at": datetime.now(UTC).isoformat(),
    }


def service_links(data: dict[str, Any], settings: Settings) -> None:
    """OpenCode routes encode the checkout directory as unpadded URL-safe base64."""
    root = (settings.opencode_public_url or str(settings.opencode_url)).rstrip("/")
    for agent in data["agents"]:
        directory = agent.pop("opencode_directory", None)
        session = agent.pop("opencode_session_id", None)
        encoded = (
            base64.urlsafe_b64encode(directory.encode()).decode().rstrip("=") if directory else None
        )
        agent["opencode_url"] = (
            f"{root}/{encoded}/session/{session}"
            if encoded and session and not agent.get("coordinator")
            else None
        )
    seed = next(
        (a for a in data["agents"] if a["state"] != "TERMINATED" and a["opencode_url"]), None
    )
    data["services"] = {
        "mattermost": settings.mattermost_public_url or str(settings.mattermost_url),
        "gitea": (settings.gitea_public_url or str(settings.gitea_url)).rstrip("/")
        + "/"
        + settings.gitea_owner
        + "/"
        + settings.gitea_repo,
        "opencode": seed["opencode_url"] if seed else root + "/",
    }


class Analytics:
    def __init__(self, store: Store, settings: Settings, workspace: UUID):
        self.store, self.settings, self.workspace = store, settings, workspace
        self.activity = AgentActivity(store, settings, workspace)
        self.billing = Billing(store, settings)
        self.git_stats = GitStats(settings)
        self.runner: web.AppRunner | None = None
        self.memberships: dict[str, list[dict[str, str]]] = {}
        self.membership_key: tuple[str, ...] = ()
        self.membership_until = 0.0
        self.membership_at: str | None = None
        self.membership_error = False
        self.lock = asyncio.Lock()
        self.sockets: set[web.WebSocketResponse] = set()
        self.publisher: asyncio.Task[None] | None = None

    async def channels(self, data: dict[str, Any]) -> None:
        # Native membership (not just wakeup subscriptions) includes Off-Topic and manual joins.
        key = (data["workspace"]["since"], *(a["id"] for a in data["agents"]))
        async with self.lock:
            if key != self.membership_key or time.monotonic() >= self.membership_until:
                result: dict[str, list[dict[str, str]]] = {}
                try:
                    async with (
                        asyncio.timeout(5),
                        Client(
                            str(self.settings.mattermost_url),
                            self.settings.mattermost_admin_token.get_secret_value(),
                        ) as admin,
                    ):
                        # The team endpoint supplies its canonical URL name once per refresh.
                        team = await admin.request("GET", f"teams/{data['workspace']['team_id']}")
                        for agent in data["agents"]:
                            if agent["state"] == "TERMINATED":
                                result[agent["id"]] = []
                                continue
                            channels = await admin.request(
                                "GET", f"users/{agent['user_id']}/teams/{team['id']}/channels"
                            )
                            result[agent["id"]] = [
                                {
                                    "id": c["id"],
                                    "name": c["name"],
                                    "url": (self.settings.mattermost_public_url or admin.base_url)
                                    + f"/{team['name']}/channels/{c['name']}",
                                }
                                for c in channels
                                if not c.get("delete_at")
                            ]
                    self.memberships = result
                    self.membership_at = datetime.now(UTC).isoformat()
                    self.membership_error = False
                except Exception:
                    # Keep the counters available when Mattermost is down; never expose its token.
                    logging.getLogger(__name__).warning("Analytics channel membership unavailable")
                    self.membership_error = True
                    if key != self.membership_key:
                        self.memberships = {}
                        self.membership_at = None
                self.membership_key = key
                self.membership_until = time.monotonic() + 10
            for agent in data["agents"]:
                agent["channels"] = self.memberships.get(agent["id"], [])
            data["memberships_updated_at"] = self.membership_at
            data["memberships_unavailable"] = self.membership_error

    async def snapshot(self) -> dict[str, Any]:
        data = await ledger_snapshot(self.store, self.workspace, self.settings.agent_engine)
        # Retrieve run origin from the same repeatable-read ledger snapshot.
        billing, _, _ = await asyncio.gather(
            self.billing.snapshot(self.workspace, data["workspace"]["run_ms"]),
            self.channels(data),
            self.git_stats.enrich(data),
        )
        data["billing"] = billing
        service_links(data, self.settings)
        return data

    async def avatar(self, request: web.Request) -> web.Response:
        try:
            agent = UUID(request.match_info["agent"])
        except ValueError:
            raise web.HTTPNotFound() from None
        image = await self.store.pool.fetchval(
            "SELECT v.image FROM agent_avatars v JOIN agents a ON a.id=v.agent_id "
            "WHERE a.id=$1 AND a.workspace_id=$2",
            agent,
            self.workspace,
        )
        if not image:
            raise web.HTTPNotFound()
        return web.Response(
            body=image,
            content_type="image/jpeg",
            headers={
                "Cache-Control": "private, max-age=3600",
                "X-Content-Type-Options": "nosniff",
            },
        )

    async def stats(self, request: web.Request) -> web.Response:
        if request.match_info.get("workspace", str(self.workspace)) != str(self.workspace):
            raise web.HTTPNotFound()
        return web.json_response(await self.snapshot(), headers={"Cache-Control": "no-store"})

    async def websocket(self, request: web.Request) -> web.WebSocketResponse:
        socket = web.WebSocketResponse(heartbeat=20, max_msg_size=1024)
        await socket.prepare(request)
        self.sockets.add(socket)
        if self.publisher is None or self.publisher.done():
            self.publisher = asyncio.create_task(self.publish())
        try:
            await socket.send_json({"type": "snapshot", "data": await self.snapshot()})
            async for _ in socket:
                pass  # Read-only: client messages never change workspace state.
        finally:
            self.sockets.discard(socket)
        return socket

    async def publish(self) -> None:
        previous = ""
        while self.sockets:
            try:
                data = await self.snapshot()
                key = json.dumps(
                    {k: v for k, v in data.items() if k != "updated_at"}, sort_keys=True
                )
                if key != previous:
                    await asyncio.gather(
                        *(
                            self.send(s, {"type": "snapshot", "data": data})
                            for s in tuple(self.sockets)
                        )
                    )
                    previous = key
            except Exception:
                logging.getLogger(__name__).warning("Analytics snapshot unavailable")
                await asyncio.gather(
                    *(self.send(s, {"type": "unavailable"}) for s in tuple(self.sockets))
                )
            await asyncio.sleep(1)

    async def send(self, socket: web.WebSocketResponse, data: dict[str, Any]) -> None:
        try:
            async with asyncio.timeout(2):
                await socket.send_json(data)
        except (ConnectionError, TimeoutError, RuntimeError):
            self.sockets.discard(socket)
            await socket.close()

    async def index(self, request: web.Request) -> web.Response:
        return web.Response(
            text=files("harness.api").joinpath("dashboard.html").read_text(),
            content_type="text/html",
            headers={"Cache-Control": "no-store"},
        )

    def app(self) -> web.Application:
        @web.middleware
        async def local_only(request: web.Request, handler: Any) -> web.StreamResponse:
            host = urlsplit("http://" + request.host).hostname
            origin = request.headers.get("Origin")
            public = self.settings.analytics_public_url.rstrip("/")
            allowed_hosts = {"localhost", "127.0.0.1", "::1"}
            if public:
                allowed_hosts.add(urlsplit(public).hostname or "")
                # Agent37 forwards the VM's private address as Host. These addresses
                # are recorded by tenant bootstrap; public browser Origin stays exact.
                allowed_hosts.update(self.settings.analytics_proxy_hosts)
            if host not in allowed_hosts or (
                origin and origin not in {f"{request.scheme}://{request.host}", public}
            ):
                raise web.HTTPForbidden()
            response: web.StreamResponse = await handler(request)
            response.headers["X-Content-Type-Options"] = "nosniff"
            response.headers["Content-Security-Policy"] = (
                "default-src 'self'; script-src 'self' 'unsafe-inline'; "
                "style-src 'self' 'unsafe-inline'; connect-src 'self'; "
                "frame-ancestors 'none'; base-uri 'none'"
            )
            return response

        app = web.Application(middlewares=[local_only], client_max_size=1024)
        app.on_shutdown.append(self.shutdown_streams)
        app.router.add_get("/", self.index)
        app.router.add_get("/api/stats", self.stats)
        app.router.add_get("/api/ws", self.websocket)
        app.router.add_get("/api/agents/{agent}/activity", self.activity.page)
        app.router.add_get("/api/agents/{agent}/avatar", self.avatar)
        app.router.add_get("/api/v1/workspaces/{workspace}/stats", self.stats)
        return app

    async def start(self) -> None:
        self.runner = web.AppRunner(self.app(), access_log=None)
        try:
            await self.runner.setup()
            await web.TCPSite(
                self.runner, self.settings.analytics_bind_host, self.settings.analytics_port
            ).start()
            logging.getLogger(__name__).info(
                "Analytics dashboard: http://localhost:%s", self.settings.analytics_port
            )
        except BaseException:
            await self.close()
            raise

    async def shutdown_streams(self, app: web.Application | None = None) -> None:
        if self.publisher:
            self.publisher.cancel()
            await asyncio.gather(self.publisher, return_exceptions=True)
            self.publisher = None
        await asyncio.gather(*(s.close() for s in tuple(self.sockets)))
        self.sockets.clear()

    async def close(self) -> None:
        await self.shutdown_streams()
        if self.runner:
            await self.runner.cleanup()
            self.runner = None
