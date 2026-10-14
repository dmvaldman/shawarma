"""Authenticated native Mattermost controls; hosted by the workspace supervisor."""

import hmac
import logging
from collections.abc import Awaitable, Callable
from hashlib import sha256
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID

from aiohttp import web

from harness.config import Settings
from harness.mattermost.client import Client
from harness.persistence.store import Store

TRIGGERS = ("stop", "pause", "continue", "reset")


class SlashCommands:
    def __init__(self, store: Store, settings: Settings, workspace: UUID):
        self.store, self.settings, self.workspace = store, settings, workspace
        self.runner: web.AppRunner | None = None
        self.reset: Callable[[str], Awaitable[str]] | None = None

    async def register(self, admin: Client, team_id: str) -> None:
        current = await admin.request(
            "GET", "commands", params={"team_id": team_id, "custom_only": "true"}
        )
        creator = (await admin.me())["id"]
        config = await admin.request("GET", "config")
        service = config["ServiceSettings"]
        allowed = service.get("AllowedUntrustedInternalConnections", "").split()
        host = urlsplit(str(self.settings.slash_callback_url)).hostname
        patch: dict[str, Any] = {}
        if not service.get("EnableCommands"):
            patch["EnableCommands"] = True
        if host == "host.docker.internal" and host not in allowed:
            patch["AllowedUntrustedInternalConnections"] = " ".join([*allowed, host])
        if patch:
            await admin.request("PUT", "config/patch", json={"ServiceSettings": patch})
        for trigger in TRIGGERS:
            existing = next((c for c in current if c["trigger"] == trigger), None)
            known = await self.store.pool.fetchval(
                "SELECT command_id FROM slash_commands WHERE workspace_id=$1 AND trigger=$2",
                self.workspace,
                trigger,
            )
            owned_recovery = (
                existing
                and known is None
                and existing.get("url") == str(self.settings.slash_callback_url)
                and existing.get("display_name") == "Swarm work controls"
            )
            if existing and (
                existing["creator_id"] != creator
                or (known != existing["id"] and not owned_recovery)
            ):
                raise ValueError(
                    f"SLASH_COMMAND_CONFLICT: /{trigger} already belongs to another integration"
                )
            body = {
                "team_id": team_id,
                "trigger": trigger,
                "method": "P",
                "url": str(self.settings.slash_callback_url),
                "auto_complete": True,
                "auto_complete_desc": (
                    "Permanently clear the swarm and start fresh (owner, Town Square only)"
                    if trigger == "reset"
                    else "Control the swarm here (Town Square controls everyone)"
                ),
                "display_name": "Swarm work controls",
            }
            if existing:
                command = await admin.request(
                    "PUT", f"commands/{existing['id']}", json={**existing, **body}
                )
            else:
                command = await admin.request("POST", "commands", json=body)
            await self.store.pool.execute(
                "INSERT INTO slash_commands VALUES($1,$2,$3,$4) "
                "ON CONFLICT(workspace_id,trigger) DO UPDATE "
                "SET command_id=EXCLUDED.command_id,token=EXCLUDED.token",
                self.workspace,
                trigger,
                command["id"],
                command["token"],
            )

    async def handle(self, request: web.Request) -> web.Response:
        def reply(text: str, status: int = 200) -> web.Response:
            return web.json_response({"response_type": "ephemeral", "text": text}, status=status)

        if request.content_type != "application/x-www-form-urlencoded":
            return reply("Expected a Mattermost slash command.", 400)
        form = await request.post()
        command = str(form.get("command", ""))
        trigger = command.removeprefix("/")
        expected = await self.store.pool.fetchval(
            "SELECT token FROM slash_commands WHERE workspace_id=$1 AND trigger=$2",
            self.workspace,
            trigger,
        )
        if not expected or not hmac.compare_digest(str(form.get("token", "")), expected):
            return reply("Invalid command authentication.", 403)
        if command != "/" + trigger or trigger not in TRIGGERS or str(form.get("text", "")).strip():
            return reply("Use /stop, /pause, /continue or /reset with no arguments.", 400)
        w = await self.store.pool.fetchrow("SELECT * FROM workspaces WHERE id=$1", self.workspace)
        assert w is not None
        if form.get("user_id") != w["owner_id"] or form.get("team_id") != w["team_id"]:
            return reply("Only the human owner can control this workspace.", 403)
        channel_id = str(form.get("channel_id", ""))
        async with Client(
            str(self.settings.mattermost_url),
            self.settings.mattermost_admin_token.get_secret_value(),
        ) as admin:
            channel = await admin.request("GET", f"channels/{channel_id}")
        if (
            channel["team_id"] != w["team_id"]
            or channel["type"] not in {"O", "P"}
            or channel.get("delete_at")
        ):
            return reply("Use these controls in a workspace channel.", 400)
        trigger_id = str(form.get("trigger_id", ""))
        if not trigger_id:
            return reply("Missing Mattermost invocation ID.", 400)
        request_id = sha256((command + ":" + trigger_id).encode()).hexdigest()
        if trigger == "reset":
            if channel_id != w["general_id"]:
                return reply(
                    "Use /reset in Town Square; it permanently clears the whole workspace.", 400
                )
            if self.reset is None:
                return reply("Reset service is unavailable.", 503)
            try:
                status = await self.reset(request_id)
            except ValueError as error:
                logging.getLogger(__name__).warning("Owner reset rejected: %s", error)
                return reply(str(error), 400)
            return reply(
                "Reset already completed."
                if status == "completed"
                else "Reset started. Old messages, bots and channels will be deleted. "
                "The configured Gitea repository and OpenCode workspaces/sessions will be erased. "
                "Town Square stays open; one fresh seed will wait for your task."
            )
        try:
            result = await self.store.slash_control(
                self.workspace, str(form["user_id"]), channel_id, command, request_id
            )
        except ValueError as error:
            logging.getLogger(__name__).warning("Owner slash control rejected: %s", error)
            return reply(str(error), 400)
        action = "Paused" if result["paused"] else "Continued"
        scope = (
            "the whole swarm"
            if result["scope"] == "workspace"
            else "agents subscribed to this channel"
        )
        text = f"{action} {scope} ({result['affected_agents']} agents)."
        if not result["paused"] and result["workspace_paused"]:
            text += " The whole swarm is still paused; use /continue in Town Square."
        return reply(text)

    async def start(self) -> None:
        team = await self.store.pool.fetchval(
            "SELECT team_id FROM workspaces WHERE id=$1", self.workspace
        )
        app = web.Application(client_max_size=16384)
        app.router.add_post(urlsplit(str(self.settings.slash_callback_url)).path, self.handle)
        self.runner = web.AppRunner(app, access_log=None)
        try:
            await self.runner.setup()
            await web.TCPSite(
                self.runner, self.settings.slash_bind_host, self.settings.slash_port
            ).start()
            async with Client(
                str(self.settings.mattermost_url),
                self.settings.mattermost_admin_token.get_secret_value(),
            ) as admin:
                await self.register(admin, team)
        except BaseException:
            await self.close()
            raise

    async def close(self) -> None:
        if self.runner:
            await self.runner.cleanup()
            self.runner = None
