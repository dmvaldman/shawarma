"""Small invite-only control plane and owner-scoped inference broker."""

import asyncio
import contextlib
import json
import logging
import os
import secrets
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
from aiohttp import web

from harness.hosting.cloud import Cloud, HostingError
from harness.hosting.store import HostingStore, digest

PROVIDERS = {
    "openai": "https://api.openai.com/v1",
    "anthropic": "https://api.anthropic.com/v1",
    "deepseek": "https://api.deepseek.com",
}
PORTS = {"mattermost": 8065, "gitea": 3000, "dashboard": 8066, "opencode": 4096}
logger = logging.getLogger(__name__)


class Hosting:
    def __init__(
        self,
        store: HostingStore,
        cloud: Cloud,
        service_key: str,
        public_url: str,
        source_ref: str,
        owner_code: str,
    ):
        self.store, self.cloud = store, cloud
        self.service_key, self.public_url, self.source_ref = service_key, public_url, source_ref
        self.owner_code = owner_code
        self.runner: asyncio.Task[None] | None = None
        self.stopping = False
        self.login_attempts: dict[str, list[float]] = {}

    async def user(self, request: web.Request) -> dict[str, Any]:
        token = request.headers.get("X-Swarm-Session", "")
        rows = await self.store.sql(
            "SELECT u.* FROM sessions s JOIN users u ON u.id=s.user_id "
            "WHERE s.token_hash=? AND s.expires>?",
            (digest(token), int(time.time())),
        )
        if not rows:
            raise web.HTTPUnauthorized(text="Sign in with your private invite link.")
        return rows[0]

    async def api(self, request: web.Request) -> web.Response:
        if not secrets.compare_digest(
            request.headers.get("X-Swarm-Service-Key", ""), self.service_key
        ):
            raise web.HTTPUnauthorized()
        route = request.match_info["route"]
        if request.method == "POST" and route == "login":
            client_ip = request.headers.get("X-Swarm-Client", request.remote or "unknown")
            now = time.time()
            attempts = [t for t in self.login_attempts.get(client_ip, []) if t > now - 60]
            if len(attempts) >= 10:
                raise web.HTTPTooManyRequests(text="Try again in a minute.")
            self.login_attempts[client_ip] = attempts + [now]
            body = await request.json()
            code = str(body.get("code", ""))
            rows = await self.store.sql("SELECT * FROM users WHERE login_hash=?", (digest(code),))
            if not rows:
                raise web.HTTPUnauthorized(text="Invalid invite link.")
            session = secrets.token_urlsafe(32)
            await self.store.sql(
                "INSERT INTO sessions VALUES(?,?,?)",
                (digest(session), rows[0]["id"], int(now) + 30 * 86400),
            )
            return web.json_response({"session": session})
        user = await self.user(request)
        vault = self.store.unseal(user)
        if request.method == "POST" and route == "logout":
            await self.store.sql(
                "DELETE FROM sessions WHERE token_hash=?",
                (digest(request.headers["X-Swarm-Session"]),),
            )
            return web.json_response({"ok": True})
        if request.method == "GET" and route == "me":
            return web.json_response(
                {
                    "id": user["id"],
                    "name": user["name"],
                    "role": user["role"],
                    "status": user["status"],
                    "stage": user["stage"],
                    "error": user["error"],
                    "run_until": user["run_until"],
                    "providers": {p: bool(vault["providers"].get(p)) for p in PROVIDERS},
                    "services": list(PORTS) if user["status"] == "running" else [],
                }
            )
        if request.method == "POST" and route == "invite":
            if user["role"] != "admin":
                raise web.HTTPForbidden()
            body = await request.json()
            name = str(body.get("name", "")).strip()
            if not 1 <= len(name) <= 60:
                raise web.HTTPBadRequest(text="Enter a name (1–60 characters).")
            count = (await self.store.sql("SELECT count(*) n FROM users"))[0]["n"]
            if count >= 10:
                raise web.HTTPConflict(text="The friends beta currently has ten places.")
            code = await self.store.invite(name)
            return web.json_response({"url": "https://swarm.forum/#invite=" + code})
        if request.method == "POST" and route == "providers":
            body = await request.json()
            provider, key = body.get("provider"), str(body.get("key", "")).strip()
            if provider not in PROVIDERS or len(key) > 1000:
                raise web.HTTPBadRequest(text="Invalid provider key.")
            if user["action"] or user["status"] == "running":
                raise web.HTTPConflict(text="Pause your swarm before changing provider keys.")
            if key:
                headers = self.provider_headers(provider, key)
                async with httpx.AsyncClient(timeout=20) as client:
                    response = await client.get(PROVIDERS[provider] + "/models", headers=headers)
                if response.is_error:
                    raise web.HTTPBadRequest(
                        text=f"{provider} rejected this key (HTTP {response.status_code})."
                    )
                vault["providers"][provider] = key
            else:
                vault["providers"].pop(provider, None)
            await self.store.save_vault(user["id"], vault)
            return web.json_response({"ok": True})
        if request.method == "POST" and route in {"start", "pause"}:
            if user["action"]:
                raise web.HTTPConflict(text="Your previous machine operation is still in progress.")
            if route == "start":
                body = await request.json()
                hours = body.get("hours", 8)
                if hours not in {2, 8, 24}:
                    raise web.HTTPBadRequest(text="Choose a 2, 8, or 24 hour session.")
                if not vault["providers"].get("openai"):
                    raise web.HTTPBadRequest(text="Add your OpenAI API key first.")
                deadline = int(time.time()) + int(hours) * 3600
                if user["status"] == "running":
                    await self.store.sql(
                        "UPDATE users SET run_until=? WHERE id=?", (deadline, user["id"])
                    )
                else:
                    await self.store.sql(
                        "UPDATE users SET action='start',status='queued',error=NULL,run_until=?"
                        " WHERE id=?",
                        (deadline, user["id"]),
                    )
            else:
                await self.store.sql(
                    "UPDATE users SET action='pause',status='pausing',run_until=NULL WHERE id=?",
                    (user["id"],),
                )
            return web.json_response({"ok": True})
        if request.method == "POST" and route == "open":
            body = await request.json()
            service = body.get("service")
            if service not in PORTS or user["status"] != "running" or not user["instance"]:
                raise web.HTTPConflict(text="Start your swarm before opening a service.")
            data = await self.cloud.request(
                "POST",
                f"instances/{user['instance']}/signed-url",
                {"port": PORTS[service], "ttl_seconds": 3600},
            )
            url = data["url"]
            if urlsplit(url).hostname != f"{user['instance']}-{PORTS[service]}.agent37.app":
                raise HostingError("Unexpected service access URL")
            return web.json_response({"url": url})
        if request.method == "POST" and route == "credentials":
            if not user["instance"]:
                raise web.HTTPConflict(text="Provision your swarm first.")
            data = json.loads(
                await self.cloud.command(
                    user["instance"],
                    "cd /root/swarm/app; .venv/bin/python -m harness.hosting.credentials",
                )
            )
            return web.json_response(data)
        raise web.HTTPNotFound()

    @staticmethod
    def provider_headers(provider: str, key: str) -> dict[str, str]:
        return (
            {"x-api-key": key, "anthropic-version": "2023-06-01"}
            if provider == "anthropic"
            else {"Authorization": "Bearer " + key}
        )

    async def proxy(self, request: web.Request) -> web.StreamResponse:
        provider, path = request.match_info["provider"], request.match_info["path"]
        allowed = (
            request.method == "GET"
            and (path == "models" or path.startswith("models/"))
            or request.method == "POST"
            and path
            in {
                "responses",
                "responses/compact",
                "chat/completions",
                "images/generations",
                "messages",
            }
        )
        if (
            provider not in PROVIDERS
            or not allowed
            or any(x in path for x in ["..", "%", "\\", "?"])
        ):
            raise web.HTTPForbidden()
        supplied = request.headers.get("Authorization", "").removeprefix("Bearer ")
        supplied = supplied or request.headers.get("x-api-key", "")
        rows = await self.store.sql(
            "SELECT * FROM users WHERE agent_token_hash=?", (digest(supplied),)
        )
        if not supplied or not rows:
            raise web.HTTPUnauthorized()
        key = self.store.unseal(rows[0])["providers"].get(provider)
        if not key:
            return web.json_response(
                {"error": {"message": "Provider not enabled", "code": "provider_not_enabled"}},
                status=401,
            )
        headers = self.provider_headers(provider, key)
        headers["Content-Type"] = "application/json"
        if request.headers.get("anthropic-beta"):
            headers["anthropic-beta"] = request.headers["anthropic-beta"]
        async with httpx.AsyncClient(timeout=300, follow_redirects=False) as client:
            async with client.stream(
                request.method,
                PROVIDERS[provider] + "/" + path,
                params=list(request.query.items()),
                content=await request.read(),
                headers=headers,
            ) as response:
                if response.is_redirect:
                    raise web.HTTPBadGateway()
                output = web.StreamResponse(
                    status=response.status_code,
                    headers={
                        "Content-Type": response.headers.get("content-type", "application/json"),
                        "Cache-Control": "no-store",
                        "X-Content-Type-Options": "nosniff",
                    },
                )
                await output.prepare(request)
                async for chunk in response.aiter_bytes():
                    await output.write(chunk)
                await output.write_eof()
                return output

    async def install(self, user: dict[str, Any]) -> None:
        vault = self.store.unseal(user)
        creds = vault.setdefault(
            "database",
            {"postgres": secrets.token_urlsafe(32), "harness": secrets.token_urlsafe(32)},
        )
        await self.store.save_vault(user["id"], vault)
        node = user["instance"]
        prefix = f"https://{node}-"
        token = vault["agent_token"]
        env = {
            "POSTGRES_PASSWORD": creds["postgres"],
            "HARNESS_DB_PASSWORD": creds["harness"],
            "HARNESS_DATABASE_URL": f"postgresql+asyncpg://harness:{creds['harness']}@localhost:5432/harness",
            "MATTERMOST_SITE_URL": prefix + "8065.agent37.app",
            "HARNESS_MATTERMOST_URL": "http://localhost:8065",
            "HARNESS_MATTERMOST_SERVER_ID": user["id"],
            "HARNESS_WORKSPACE_NAME": user["name"] + "'s Swarm",
            "HARNESS_COORDINATION_CHANNEL_NAME": "town-square",
            "HARNESS_SLASH_BIND_HOST": "0.0.0.0",
            "HARNESS_SLASH_CALLBACK_URL": "http://host.docker.internal:8766/mattermost/commands",
            "MATTERMOST_ALLOWED_INTERNAL_CONNECTIONS": "host.docker.internal",
            "HARNESS_GITEA_URL": "http://localhost:3000",
            "HARNESS_GITEA_WORKER_URL": "http://gitea:3000",
            "HARNESS_OPENCODE_URL": "http://localhost:4096",
            "HARNESS_MAX_ACTIVE_AGENTS": "24",
            "HARNESS_OPENAI_BASE_URL": self.public_url + "/proxy/openai/v1",
            "HARNESS_ANTHROPIC_BASE_URL": self.public_url + "/proxy/anthropic/v1",
            "HARNESS_DEEPSEEK_BASE_URL": self.public_url + "/proxy/deepseek/v1",
            "OPENAI_API_KEY": token,
            "ANTHROPIC_API_KEY": token if vault["providers"].get("anthropic") else "",
            "DEEPSEEK_API_KEY": token if vault["providers"].get("deepseek") else "",
            "HOSTED_OWNER_PASSWORD": vault["mattermost_password"],
            "HOSTED_MATTERMOST_URL": prefix + "8065.agent37.app",
            "HOSTED_GITEA_URL": prefix + "3000.agent37.app",
            "HOSTED_OPENCODE_URL": prefix + "4096.agent37.app",
            "HOSTED_ANALYTICS_URL": prefix + "8066.agent37.app",
        }
        await self.cloud.install(node, env, self.source_ref)

    async def jobs(self) -> None:
        while not self.stopping:
            try:
                await self.store.sql(
                    "UPDATE users SET action='pause',status='pausing' "
                    "WHERE status='running' AND run_until IS NOT NULL AND run_until<?",
                    (int(time.time()),),
                )
                rows = await self.store.sql(
                    "SELECT * FROM users WHERE action IS NOT NULL ORDER BY created_at"
                )
                for user in rows:
                    try:
                        if user["action"] == "pause":
                            if user["instance"]:
                                await self.store.sql(
                                    "UPDATE users SET stage='Saving work' WHERE id=?", (user["id"],)
                                )
                                with contextlib.suppress(HostingError):
                                    await self.cloud.command(
                                        user["instance"], "systemctl stop swarm.service; sync"
                                    )
                                await self.cloud.request(
                                    "POST", f"instances/{user['instance']}/stop"
                                )
                            await self.store.sql(
                                "UPDATE users SET status='sleeping',action=NULL,stage='',error=NULL"
                                " WHERE id=?",
                                (user["id"],),
                            )
                            continue
                        await self.store.sql(
                            "UPDATE users SET status='provisioning',stage='Starting your machine'"
                            " WHERE id=?",
                            (user["id"],),
                        )
                        if not user["instance"]:
                            if user["creating"]:
                                listing = await self.cloud.request("GET", "instances")
                                entries = (
                                    listing
                                    if isinstance(listing, list)
                                    else listing.get("instances", [])
                                )
                                found = [
                                    i
                                    for i in entries
                                    if i.get("metadata", {}).get("swarm_user") == user["id"]
                                ]
                                if len(found) != 1:
                                    raise HostingError(
                                        "Previous machine creation outcome is unknown; "
                                        "operator reconciliation required"
                                    )
                                instance = found[0]
                            else:
                                await self.store.sql(
                                    "UPDATE users SET creating=1 WHERE id=?", (user["id"],)
                                )
                                instance = await self.cloud.request(
                                    "POST",
                                    "instances",
                                    {
                                        "type": "vm",
                                        "template": "agent37-ubuntu",
                                        "name": "Swarm " + user["id"],
                                        "resources": {"cpu": 4, "memory": 8, "disk": 20},
                                        "auto_sleep": False,
                                        "metadata": {
                                            "application": "swarm-forum",
                                            "swarm_user": user["id"],
                                        },
                                    },
                                )
                            await self.store.sql(
                                "UPDATE users SET instance=?,creating=0 WHERE id=?",
                                (instance["id"], user["id"]),
                            )
                            user["instance"] = instance["id"]
                        else:
                            await self.cloud.request("POST", f"instances/{user['instance']}/start")
                        await self.store.sql(
                            "UPDATE users SET stage='Preparing Mattermost, Gitea and OpenCode'"
                            " WHERE id=?",
                            (user["id"],),
                        )
                        await self.install(user)
                        await self.store.sql(
                            "UPDATE users SET status='running',stage='',action=NULL,error=NULL"
                            " WHERE id=?",
                            (user["id"],),
                        )
                    except Exception as error:
                        if isinstance(error, HostingError) and error.status in {
                            400,
                            401,
                            402,
                            403,
                            404,
                            422,
                        }:
                            await self.store.sql(
                                "UPDATE users SET creating=0 WHERE id=?", (user["id"],)
                            )
                        if user["instance"]:
                            # Failed setup must not leave an unattended billable machine running.
                            with contextlib.suppress(Exception):
                                await self.cloud.request(
                                    "POST", f"instances/{user['instance']}/stop"
                                )
                        message = (
                            str(error)
                            if isinstance(error, HostingError)
                            else "Setup interrupted; your data is preserved. "
                            "Ask the operator to inspect diagnostics."
                        )
                        await self.store.sql(
                            "UPDATE users SET status='failed',action=NULL,error=? WHERE id=?",
                            (message, user["id"]),
                        )
                        logger.warning(
                            "Hosted workspace %s setup failed (%s)",
                            user["id"],
                            type(error).__name__,
                        )
            except Exception:
                logger.warning("Hosting job queue unavailable")
            await asyncio.sleep(5)

    async def startup(self, app: web.Application) -> None:
        owner = await self.store.sql("SELECT id FROM users WHERE role=?", ("admin",))
        if not owner:
            await self.store.invite("David", "admin", self.owner_code)
        self.runner = asyncio.create_task(self.jobs())

    async def cleanup(self, app: web.Application) -> None:
        self.stopping = True
        if self.runner:
            self.runner.cancel()
            await asyncio.gather(self.runner, return_exceptions=True)

    async def health(self, request: web.Request) -> web.Response:
        return web.json_response({"ok": True})

    def app(self) -> web.Application:
        @web.middleware
        async def errors(request: web.Request, handler: Any) -> web.StreamResponse:
            response: web.StreamResponse
            try:
                response = await handler(request)
            except web.HTTPException as error:
                response = web.json_response({"error": error.text}, status=error.status)
            except Exception:
                logger.warning("Hosted API request failed")
                response = web.json_response(
                    {"error": "Service temporarily unavailable."}, status=503
                )
            response.headers["Cache-Control"] = "no-store"
            response.headers["Referrer-Policy"] = "no-referrer"
            return response

        app = web.Application(middlewares=[errors], client_max_size=32 * 1024 * 1024)
        app.router.add_route("*", "/api/{route}", self.api)
        app.router.add_route("*", "/proxy/{provider}/v1/{path:.*}", self.proxy)
        app.router.add_get("/health", self.health)
        app.on_startup.append(self.startup)
        app.on_cleanup.append(self.cleanup)
        return app


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    app = Hosting(
        HostingStore(Path(os.environ["HOSTING_DB"]), os.environ["HOSTING_VAULT_KEY"]),
        Cloud(os.environ["AGENT37_API_KEY"]),
        os.environ["HOSTING_SERVICE_KEY"],
        os.environ["HOSTING_PUBLIC_URL"].rstrip("/"),
        os.environ["HOSTING_SOURCE_REF"],
        os.environ["HOSTING_OWNER_CODE"],
    ).app()
    web.run_app(app, host="0.0.0.0", port=8780, access_log=None)


if __name__ == "__main__":
    main()
