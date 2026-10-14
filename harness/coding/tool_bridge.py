"""Authenticated native tools dispatch through existing swarm policy and audit."""

import asyncio
import hmac
import json
from typing import Any
from uuid import UUID

from aiohttp import web

from harness.config import Settings
from harness.persistence.store import Store, decode
from harness.tools.core import CoreTools


class ToolBridge:
    def __init__(self, store: Store, settings: Settings, workspace: UUID):
        self.store, self.settings, self.workspace = store, settings, workspace
        self.tools: dict[UUID, CoreTools] = {}
        self.tasks: dict[UUID, set[asyncio.Task[Any]]] = {}
        self.runner: web.AppRunner | None = None

    async def cancel(self, agent: UUID) -> None:
        tasks = list(self.tasks.get(agent, set()))
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def handle(self, request: web.Request) -> web.Response:
        key = self.settings.swarm_tool_key.get_secret_value()
        if not key or not hmac.compare_digest(
            request.headers.get("Authorization", ""), "Bearer " + key
        ):
            raise web.HTTPUnauthorized()
        try:
            body = await request.json()
            session, name, call_id = body["session_id"], body["name"], body["call_id"]
            if not all(isinstance(x, str) and 0 < len(x) < 200 for x in [session, name, call_id]):
                raise ValueError()
            row = await self.store.pool.fetchrow(
                "SELECT s.agent_id,r.turn_id FROM coding_sessions s "
                "JOIN agents a ON a.id=s.agent_id "
                "JOIN coding_runs r ON r.agent_id=a.id AND r.state='running' "
                "WHERE s.session_id=$1 AND a.workspace_id=$2",
                session,
                self.workspace,
            )
            if not row or row["agent_id"] not in self.tools:
                raise web.HTTPForbidden()
            agent, turn = row["agent_id"], row["turn_id"]
            state = await self.store.state(agent)
            if (
                state["control_paused"]
                or state["workspace_state"] != "active"
                or state["state"] == "TERMINATED"
            ):
                raise web.HTTPForbidden()
            tools = self.tools[agent]
            if name not in {t.name for t in tools.manifests} or name == "code_task":
                raise web.HTTPForbidden()
            task = asyncio.current_task()
            assert task is not None
            self.tasks.setdefault(agent, set()).add(task)
            try:
                async with self.store.pool.acquire() as lease:
                    if not await lease.fetchval(
                        "SELECT pg_try_advisory_lock(hashtextextended($1,0))",
                        "native-tool:" + str(agent) + ":" + call_id,
                    ):
                        raise web.HTTPConflict(text="Tool call is already executing")
                    try:
                        old = await lease.fetchrow(
                            "SELECT state,result FROM tool_calls WHERE agent_id=$1 AND call_id=$2",
                            agent,
                            call_id,
                        )
                        if old:
                            if old["state"] == "completed":
                                return web.json_response(decode(old["result"]))
                            raise web.HTTPConflict(text="Prior tool outcome is uncertain; inspect")
                        call = {
                            "type": "function_call",
                            "name": name,
                            "call_id": call_id,
                            "arguments": json.dumps(body["arguments"]),
                        }
                        await self.store.append(agent, [call])
                        try:
                            async with asyncio.timeout(self.settings.tool_timeout_sec):
                                result = await tools.execute(turn, call)
                        except (TimeoutError, asyncio.CancelledError):
                            await self.store.save_tool_result(
                                agent,
                                call_id,
                                {
                                    "error": "TOOL_INTERRUPTED",
                                    "result_uncertain": True,
                                    "detail": "Inspect effects before retrying external mutations.",
                                },
                            )
                            raise
                        return web.json_response(result)
                    finally:
                        await lease.execute(
                            "SELECT pg_advisory_unlock(hashtextextended($1,0))",
                            "native-tool:" + str(agent) + ":" + call_id,
                        )
            finally:
                self.tasks[agent].discard(task)
        except (KeyError, ValueError, TypeError):
            raise web.HTTPBadRequest() from None

    async def start(self) -> None:
        if not self.settings.swarm_tool_key.get_secret_value():
            raise ValueError("Configure HARNESS_SWARM_TOOL_KEY before selecting OpenCode")
        app = web.Application(client_max_size=128 * 1024)
        app.router.add_post("/tools", self.handle)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        await web.TCPSite(
            self.runner, self.settings.swarm_tool_bind_host, self.settings.swarm_tool_port
        ).start()

    async def close(self) -> None:
        for agent in list(self.tasks):
            await self.cancel(agent)
        if self.runner:
            await self.runner.cleanup()
