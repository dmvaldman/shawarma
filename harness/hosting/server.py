"""Small invite-only control plane and owner-scoped inference broker."""

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import secrets
import time
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx
from aiohttp import web

from harness.api.billing import read_provider_cost
from harness.config import Settings
from harness.hosting.analytics_bridge import AnalyticsBridge
from harness.hosting.billing import ADMIN_PROVIDERS, HostedBilling, billing_headers
from harness.hosting.cloud import Cloud, HostingError
from harness.hosting.google import GoogleLogin
from harness.hosting.paths import service_path
from harness.hosting.store import HostingStore, digest
from harness.models.catalog import ModelCatalog

PROVIDERS = {
    "openai": "https://api.openai.com/v1",
    "anthropic": "https://api.anthropic.com/v1",
    "deepseek": "https://api.deepseek.com",
}
PORTS = {"mattermost": 18065, "gitea": 13000, "dashboard": 18066, "opencode": 14096}
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
        avatar_key: str = "",
        google_client_id: str = "",
        google_client_secret: str = "",
    ):
        self.store, self.cloud = store, cloud
        self.service_key, self.public_url, self.source_ref = service_key, public_url, source_ref
        self.owner_code = owner_code
        self.avatar_key = avatar_key
        self.google = GoogleLogin(store, google_client_id, google_client_secret)
        self.billing = HostedBilling(store, PROVIDERS)
        self.analytics = AnalyticsBridge(store, cloud, public_url, self.billing)
        self.catalogs: dict[str, tuple[str, ModelCatalog]] = {}
        self.runner: asyncio.Task[None] | None = None
        self.stopping = False
        self.operations: dict[str, asyncio.Task[None]] = {}
        self.login_attempts: dict[str, list[float]] = {}

    async def models(self, user: dict[str, Any], vault: dict[str, Any]) -> dict[str, Any]:
        keys = vault["providers"]
        fingerprint = hashlib.sha256(json.dumps(keys, sort_keys=True).encode()).hexdigest()
        cached = self.catalogs.get(user["id"])
        if cached is None or cached[0] != fingerprint:
            settings = Settings(
                _env_file=None,
                mattermost_url="http://localhost:8065",
                database_url="unused",
                OPENAI_API_KEY=keys.get("openai", ""),
                ANTHROPIC_API_KEY=keys.get("anthropic", ""),
                DEEPSEEK_API_KEY=keys.get("deepseek", ""),
            )
            self.catalogs[user["id"]] = (fingerprint, ModelCatalog(settings))
        return await self.catalogs[user["id"]][1].list()

    @staticmethod
    def masked_key(key: str) -> str:
        return (key[:8] if len(key) > 12 else "") + "*" * 12

    async def user(self, request: web.Request) -> dict[str, Any]:
        token = request.headers.get("X-Swarm-Session", "")
        rows = await self.store.sql(
            "SELECT u.* FROM sessions s JOIN users u ON u.id=s.user_id "
            "WHERE s.token_hash=? AND s.expires>?",
            (digest(token), int(time.time())),
        )
        if not rows:
            raise web.HTTPUnauthorized(text="Sign in to continue.")
        return rows[0]

    async def api(self, request: web.Request) -> web.Response:
        if not secrets.compare_digest(
            request.headers.get("X-Swarm-Service-Key", ""), self.service_key
        ):
            raise web.HTTPUnauthorized()
        route = request.match_info["route"]
        if request.method == "GET" and route == "auth-config":
            return web.json_response({"google": self.google.enabled})
        if request.method == "POST" and route == "google-start":
            return web.json_response(
                await self.google.start(request.headers.get("X-Swarm-Session", ""))
            )
        if request.method == "POST" and route == "google-complete":
            return web.json_response({"session": await self.google.complete(await request.json())})
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
                    "instance_id": user["instance"],
                    "name": user["name"],
                    "role": user["role"],
                    "google_connected": bool(user["google_subject"]),
                    "activated": bool(user["activated"]),
                    "status": user["status"],
                    "stage": user["stage"],
                    "error": user["error"],
                    "run_until": user["run_until"],
                    "providers": {p: bool(vault["providers"].get(p)) for p in PROVIDERS},
                    "admin_keys": {
                        p: self.masked_key(k) for p, k in vault.get("admin_keys", {}).items() if k
                    },
                    "provider_keys": {
                        p: self.masked_key(k) for p, k in vault["providers"].items() if k
                    },
                    "services": list(PORTS) if user["status"] == "running" else [],
                    "analytics_available": user["status"] == "running"
                    or bool(user["status"] == "sleeping" and await self.analytics.has_saved(user)),
                }
            )
        if request.method == "POST" and route == "redeem":
            token = request.headers["X-Swarm-Session"]
            attempts = [t for t in self.login_attempts.get(token, []) if t > time.time() - 60]
            if len(attempts) >= 10:
                raise web.HTTPTooManyRequests(text="Try again in a minute.")
            self.login_attempts[token] = attempts + [time.time()]
            body = await request.json()
            try:
                await self.store.redeem(user["id"], str(body.get("code", "")).strip())
            except ValueError as error:
                raise web.HTTPForbidden(text=str(error)) from error
            return web.json_response({"ok": True})
        if not user["activated"]:
            raise web.HTTPForbidden(text="Enter an invite code to unlock your swarm.")
        if request.method == "GET" and route == "analytics":
            return await self.analytics.resource(request, user)
        if request.method == "POST" and route == "dashboard-connect":
            return await self.analytics.ticket(request, user)
        if request.method == "GET" and route == "models":
            catalog = await self.models(user, vault)
            return web.json_response(
                {
                    "models": [
                        {k: m[k] for k in ("id", "provider", "api_id", "name")}
                        for m in catalog["models"]
                    ],
                    "providers": catalog["providers"],
                }
            )
        if request.method == "POST" and route == "invite":
            if user["role"] != "admin":
                raise web.HTTPForbidden()
            body = await request.json()
            name = str(body.get("name", "")).strip()
            if not 1 <= len(name) <= 60:
                raise web.HTTPBadRequest(text="Enter a name (1–60 characters).")
            count = (await self.store.sql("SELECT count(*) n FROM users WHERE activated=1"))[0]["n"]
            if count >= 10:
                raise web.HTTPConflict(text="The friends beta currently has ten places.")
            code = await self.store.invite(name)
            return web.json_response({"url": "https://swarm.forum/#invite=" + code})
        if request.method == "POST" and route == "admin-keys":
            body = await request.json()
            provider, key = body.get("provider"), str(body.get("key", "")).strip()
            if provider not in ADMIN_PROVIDERS or len(key) > 1000:
                raise web.HTTPBadRequest(text="Choose OpenAI or Anthropic and enter an admin key.")
            if key:
                try:
                    # Include yesterday because current-day Anthropic reports may not exist yet.
                    start = int(time.time()) // 86400 * 86400 - 86400
                    await read_provider_cost(
                        provider, PROVIDERS[provider], billing_headers(provider, key), start
                    )
                except (httpx.HTTPError, TimeoutError, ValueError, KeyError, TypeError):
                    raise web.HTTPBadRequest(
                        text="Could not read billing with this key. Check its billing permissions."
                    ) from None
                vault.setdefault("admin_keys", {})[provider] = key
            else:
                vault.setdefault("admin_keys", {}).pop(provider, None)
            await self.store.save_vault(user["id"], vault)
            return web.json_response({"ok": True})
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
            self.catalogs.pop(user["id"], None)
            return web.json_response({"ok": True})
        if request.method == "POST" and route in {"start", "pause", "stop"}:
            if user["action"]:
                raise web.HTTPConflict(text="Your previous machine operation is still in progress.")
            if route == "start":
                body = await request.json()
                hours = body.get("hours", 8)
                if hours not in {2, 8, 24}:
                    raise web.HTTPBadRequest(text="Choose a 2, 8, or 24 hour session.")
                if not any(vault["providers"].values()):
                    raise web.HTTPBadRequest(text="Add a provider API key first.")
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
            elif route == "stop":
                await self.store.sql(
                    "UPDATE users SET action='stop',status='stopping',run_until=NULL WHERE id=?",
                    (user["id"],),
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
            target_instance = body.get("instance")
            if target_instance is not None and target_instance != user["instance"]:
                raise web.HTTPForbidden(text="This link belongs to a different swarm.")
            data = await self.cloud.request(
                "POST",
                f"instances/{user['instance']}/signed-url",
                {"port": PORTS[service], "ttl_seconds": 3600},
            )
            destination = str(body.get("path", ""))
            if destination:
                try:
                    destination = service_path(destination)
                except ValueError:
                    raise web.HTTPBadRequest(text="Invalid service path") from None
            url = data["url"]
            if urlsplit(url).hostname != f"{user['instance']}-{PORTS[service]}.agent37.app":
                raise HostingError("Unexpected service access URL")
            ticket = secrets.token_urlsafe(32)
            await self.store.sql("DELETE FROM service_tickets WHERE expires<?", (int(time.time()),))
            await self.store.sql(
                "INSERT INTO service_tickets VALUES(?,?,?,?,?)",
                (
                    digest(ticket),
                    user["id"],
                    digest(request.headers["X-Swarm-Session"]),
                    service,
                    int(time.time()) + 60,
                ),
            )
            parts = urlsplit(url)
            query = parse_qsl(parts.query) + [("ticket", ticket)]
            if destination:
                query.append(("next", destination))
            return web.json_response(
                {"url": urlunsplit(parts._replace(path="/__swarm/login", query=urlencode(query)))}
            )
        if request.method == "POST" and route == "credentials":
            if not user["instance"] or user["status"] != "running":
                raise web.HTTPConflict(text="Start your swarm before viewing credentials.")
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

    async def gateway(self, request: web.Request) -> web.Response:
        supplied = request.headers.get("Authorization", "").removeprefix("Bearer ")
        users = await self.store.sql(
            "SELECT * FROM users WHERE agent_token_hash=? AND status IN ('running','pausing')",
            (digest(supplied),),
        )
        if not supplied or not users:
            raise web.HTTPUnauthorized()
        user = users[0]
        body = await request.json()
        service, now = body.get("service"), int(time.time())
        if user["status"] == "pausing" and service != "dashboard":
            raise web.HTTPUnauthorized()
        if service not in PORTS:
            raise web.HTTPBadRequest()
        if request.match_info["operation"] == "exchange":
            tickets = await self.store.sql(
                "DELETE FROM service_tickets WHERE token_hash=? AND user_id=? AND service=? "
                "AND expires>? AND session_hash IN "
                "(SELECT token_hash FROM sessions WHERE expires>?) "
                "RETURNING session_hash",
                (digest(str(body.get("ticket", ""))), user["id"], service, now, now),
            )
            if not tickets:
                raise web.HTTPUnauthorized(
                    text="Service sign-in expired. Open it from the portal again."
                )
            cookie = secrets.token_urlsafe(32)
            await self.store.sql(
                "INSERT INTO service_sessions VALUES(?,?,?,?,?)",
                (
                    digest(cookie),
                    user["id"],
                    tickets[0]["session_hash"],
                    service,
                    now + 3600,
                ),
            )
            return web.json_response({"token": cookie})
        if request.match_info["operation"] == "check":
            rows = await self.store.sql(
                "SELECT ss.token_hash FROM service_sessions ss "
                "JOIN sessions s ON s.token_hash=ss.session_hash "
                "WHERE ss.token_hash=? AND ss.user_id=? AND ss.service=? "
                "AND ss.expires>? AND s.expires>?",
                (digest(str(body.get("token", ""))), user["id"], service, now, now),
            )
            if not rows:
                raise web.HTTPUnauthorized()
            return web.json_response({"ok": True})
        raise web.HTTPNotFound()

    async def proxy(self, request: web.Request) -> web.StreamResponse:
        provider, path = request.match_info["provider"], request.match_info["path"]
        sponsored = provider == "avatar"
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
            (provider not in PROVIDERS and not sponsored)
            or not allowed
            or (sponsored and (request.method != "POST" or path != "images/generations"))
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
        if rows[0]["status"] in {"stopping", "stopped"}:
            raise web.HTTPForbidden(text="This swarm has been stopped.")
        key = (
            self.avatar_key if sponsored else self.store.unseal(rows[0])["providers"].get(provider)
        )
        if not key:
            return web.json_response(
                {"error": {"message": "Provider not enabled", "code": "provider_not_enabled"}},
                status=401,
            )
        content = await request.read()
        if sponsored:
            try:
                body = json.loads(content)
            except (ValueError, UnicodeDecodeError) as error:
                raise web.HTTPBadRequest(text="Invalid avatar request.") from error
            if (
                not isinstance(body, dict)
                or body.get("model") != "gpt-image-2.5-flare"
                or body.get("n") != 1
                or body.get("size") != "1024x1024"
                or body.get("quality") != "low"
                or body.get("output_format") != "jpeg"
                or body.get("output_compression") != 80
                or not isinstance(body.get("prompt"), str)
                or not 1 <= len(body["prompt"]) <= 4000
                or set(body)
                != {
                    "model",
                    "n",
                    "size",
                    "quality",
                    "output_format",
                    "output_compression",
                    "prompt",
                }
            ):
                raise web.HTTPBadRequest(text="Invalid avatar request.")
        upstream = "openai" if sponsored else provider
        headers = self.provider_headers(upstream, key)
        headers["Content-Type"] = "application/json"
        if request.headers.get("anthropic-beta"):
            headers["anthropic-beta"] = request.headers["anthropic-beta"]
        async with httpx.AsyncClient(timeout=300, follow_redirects=False) as client:
            async with client.stream(
                request.method,
                PROVIDERS[upstream] + "/" + path,
                params=list(request.query.items()),
                content=content,
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
        candidates = [m for m in (await self.models(user, vault))["models"] if m["agent_candidate"]]
        if not candidates:
            raise HostingError("No available agent models found for your connected providers.")
        preferred = ["openai:gpt-6-luna", "anthropic:claude-haiku-5-5", "deepseek:deepseek-flash"]
        seed = min(
            candidates,
            key=lambda m: (
                preferred.index(m["id"]) if m["id"] in preferred else len(preferred),
                {"lite": 0, "balanced": 1, "frontier": 2}.get(m["class"], 3),
                m["id"],
            ),
        )
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
            "MATTERMOST_SITE_URL": prefix + str(PORTS["mattermost"]) + ".agent37.app",
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
            "HARNESS_SEED_MODEL": seed["id"],
            "HARNESS_MODEL_PROFILES": json.dumps(
                {
                    seed["id"]: {
                        "input": seed["input"],
                        "output": seed["output"],
                        "class": seed["class"],
                    }
                }
            ),
            "HARNESS_AVATAR_BASE_URL": self.public_url + "/proxy/avatar/v1",
            "HARNESS_AVATAR_API_KEY": token,
            "HOSTED_GATEWAY_KEY": token,
            "HOSTED_GATEWAY_BROKER_URL": self.public_url,
            "OPENAI_API_KEY": token if vault["providers"].get("openai") else "",
            "ANTHROPIC_API_KEY": token if vault["providers"].get("anthropic") else "",
            "DEEPSEEK_API_KEY": token if vault["providers"].get("deepseek") else "",
            "HOSTED_OWNER_PASSWORD": vault["mattermost_password"],
            "HOSTED_OWNER_NAME": json.dumps(user["name"], ensure_ascii=True),
            "HOSTED_MATTERMOST_URL": prefix + str(PORTS["mattermost"]) + ".agent37.app",
            "HOSTED_GITEA_URL": prefix + str(PORTS["gitea"]) + ".agent37.app",
            "HOSTED_OPENCODE_URL": prefix + str(PORTS["opencode"]) + ".agent37.app",
            "HOSTED_ANALYTICS_URL": prefix + str(PORTS["dashboard"]) + ".agent37.app",
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
                    previous = self.operations.get(user["id"])
                    if previous is None or previous.done():
                        self.operations[user["id"]] = asyncio.create_task(self.run_job(user))
            except Exception:
                logger.warning("Hosting job queue unavailable")
            await asyncio.sleep(5)

    async def run_job(self, user: dict[str, Any]) -> None:
        try:
            if user["action"] == "stop":
                if user["instance"]:
                    try:
                        await self.cloud.request("DELETE", f"instances/{user['instance']}")
                    except HostingError as error:
                        if error.status != 404:
                            raise
                vault = self.store.unseal(user)
                vault.pop("database", None)
                vault["agent_token"] = secrets.token_urlsafe(32)
                vault["mattermost_password"] = secrets.token_urlsafe(32)
                await self.store.sql(
                    "UPDATE users SET instance=NULL,creating=0,status='stopped',action=NULL,"
                    "stage='',error=NULL,run_until=NULL,vault=?,agent_token_hash=? WHERE id=?",
                    (self.store.seal(vault), digest(vault["agent_token"]), user["id"]),
                )
                await self.store.sql("DELETE FROM service_tickets WHERE user_id=?", (user["id"],))
                await self.store.sql("DELETE FROM service_sessions WHERE user_id=?", (user["id"],))
                await self.store.sql(
                    "DELETE FROM analytics_archives WHERE user_id=?", (user["id"],)
                )
                return
            if user["action"] == "pause":
                archive_error = None
                if user["instance"]:
                    await self.store.sql(
                        "UPDATE users SET stage='Saving work' WHERE id=?", (user["id"],)
                    )
                    try:
                        await self.analytics.capture(user)
                    except Exception:
                        logging.getLogger(__name__).exception("Could not archive paused analytics")
                        archive_error = (
                            "Swarm paused, but its latest dashboard history could not be saved."
                        )
                    with contextlib.suppress(HostingError):
                        await self.cloud.command(
                            user["instance"],
                            "docker stop --time 60 swarm-service-gateway swarm-controller; "
                            "cd /root/swarm/app; "
                            "docker compose --env-file .env "
                            "--env-file .state/coding.env "
                            "-f infra/coding/compose.yaml stop; "
                            "docker compose --env-file .env "
                            "-f infra/compose.yaml stop; sync",
                        )
                    await self.cloud.request("POST", f"instances/{user['instance']}/stop")
                await self.store.sql(
                    "UPDATE users SET status='sleeping',action=NULL,stage='',error=? WHERE id=?",
                    (archive_error, user["id"]),
                )
                return
            await self.store.sql(
                "UPDATE users SET status='provisioning',stage='Starting your machine' WHERE id=?",
                (user["id"],),
            )
            if not user["instance"]:
                if user["creating"]:
                    listing = await self.cloud.request("GET", "instances")
                    entries = listing if isinstance(listing, list) else listing.get("instances", [])
                    found = [
                        i for i in entries if i.get("metadata", {}).get("swarm_user") == user["id"]
                    ]
                    if len(found) != 1:
                        raise HostingError(
                            "Previous machine creation outcome is unknown; "
                            "operator reconciliation required"
                        )
                    instance = found[0]
                else:
                    await self.store.sql("UPDATE users SET creating=1 WHERE id=?", (user["id"],))
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
                "UPDATE users SET stage='Preparing Mattermost, Gitea and OpenCode' WHERE id=?",
                (user["id"],),
            )
            await self.install(user)
            await self.store.sql(
                "UPDATE users SET status='running',stage='',action=NULL,error=NULL WHERE id=?",
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
                await self.store.sql("UPDATE users SET creating=0 WHERE id=?", (user["id"],))
            if user["instance"]:
                # Failed setup must not leave an unattended billable machine running.
                with contextlib.suppress(Exception):
                    await self.cloud.request("POST", f"instances/{user['instance']}/stop")
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

    async def startup(self, app: web.Application) -> None:
        owner = await self.store.sql("SELECT id FROM users WHERE role=?", ("admin",))
        if not owner:
            await self.store.invite("David", "admin", self.owner_code)
        self.runner = asyncio.create_task(self.jobs())

    async def cleanup(self, app: web.Application) -> None:
        self.stopping = True
        await self.analytics.close()
        tasks = list(self.operations.values())
        if self.runner:
            tasks.append(self.runner)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

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
        app.router.add_post("/gateway/{operation}", self.gateway)
        app.router.add_get("/health", self.health)
        app.router.add_get("/portal/analytics", self.analytics.socket)
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
        os.environ.get("HOSTING_AVATAR_OPENAI_KEY", ""),
        os.environ.get("HOSTING_GOOGLE_CLIENT_ID", ""),
        os.environ.get("HOSTING_GOOGLE_CLIENT_SECRET", ""),
    ).app()
    web.run_app(app, host="0.0.0.0", port=8780, access_log=None)


if __name__ == "__main__":
    main()
