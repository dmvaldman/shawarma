"""Portal-authenticated analytics over existing tenant SSO, with no browser credentials."""

import asyncio
import contextlib
import json
import secrets
import time
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from uuid import UUID

from aiohttp import ClientSession, ClientTimeout, WSMsgType, web

from harness.api.page import dashboard_page
from harness.hosting.billing import HostedBilling
from harness.hosting.cloud import Cloud, HostingError
from harness.hosting.store import HostingStore, digest

PORT = 18066


def portal_page() -> tuple[str, str]:
    text = dashboard_page()
    nonce = secrets.token_urlsafe(24)
    text = text.replace("<script>", f'<script nonce="{nonce}">')
    text = text.replace("'use strict';", "'use strict';\nconst hostedPortal=true;", 1)
    text = text.replace(
        "e.href=parsed.href;", "e.href=parsed.href;e.target='_blank';e.rel='noopener noreferrer';"
    )
    text = text.replace("image.src=a.avatar_url", "image.src=analyticsURL(a.avatar_url)")
    activity = (
        "'/api/agents/'+encodeURIComponent(id)+'/activity?limit=30'+(cursor?'&before='+cursor:'')"
    )
    text = text.replace("fetch(" + activity + ",", "fetch(analyticsURL(" + activity + "),")
    socket = (
        "function connect(){const socket=new WebSocket("
        "(location.protocol==='https:'?'wss://':'ws://')+location.host+'/api/ws');"
    )
    text = text.replace(
        socket,
        "async function connect(){const url=await portalSocketURL();"
        "if(!url){disconnected();setTimeout(connect,5000);return;}"
        "const socket=new WebSocket(url);",
    )
    text = text.replace(
        "function render(data){latest=data;",
        "function render(data){latest=data;parent.postMessage("
        "{type:'swarm-workspace-status',status:data.workspace.status},location.origin);",
    )
    bridge = Path(__file__).with_name("portal_dashboard.js").read_text()
    text = text.replace("let latest=null;", bridge + "\nlet latest=null;")
    text = text.replace(
        "</style>",
        "header,.activity-heading{display:none!important}main{padding:0!important;max-width:none}"
        ".connection{margin-left:auto}.statusbar{margin-bottom:18px}"
        "body{overflow-x:hidden}</style>",
    )
    policy = (
        f"default-src 'self'; script-src 'nonce-{nonce}'; style-src 'self' 'unsafe-inline'; "
        "connect-src 'self' https://*.agent37.app wss://*.agent37.app; "
        "img-src 'self' data:; frame-ancestors 'self'; base-uri 'none'; form-action 'none'"
    )
    return text, policy


class AnalyticsBridge:
    def __init__(
        self,
        store: HostingStore,
        cloud: Cloud,
        public_url: str,
        billing: HostedBilling | None = None,
    ):
        self.store, self.cloud, self.public_url = store, cloud, public_url
        self.billing = billing
        self.connections: dict[tuple[str, str, str], tuple[ClientSession, str, float]] = {}
        self.lock = asyncio.Lock()

    async def connection(
        self, user: dict[str, Any], session_hash: str
    ) -> tuple[ClientSession, str]:
        if user["status"] != "running" or not user["instance"]:
            raise web.HTTPConflict(text="Start your swarm to view analytics.")
        key = (user["id"], session_hash, user["instance"])
        async with self.lock:
            for old, (client, _, until) in list(self.connections.items()):
                if until <= time.monotonic():
                    await client.close()
                    del self.connections[old]
            if key not in self.connections:
                access = await self.cloud.request(
                    "POST",
                    f"instances/{user['instance']}/signed-url",
                    {"port": PORT, "ttl_seconds": 3600},
                )
                parsed = urlsplit(access["url"])
                if (
                    parsed.scheme != "https"
                    or parsed.hostname != f"{user['instance']}-{PORT}.agent37.app"
                ):
                    raise HostingError("Unexpected analytics access URL")
                ticket = secrets.token_urlsafe(32)
                await self.store.sql(
                    "INSERT INTO service_tickets VALUES(?,?,?,?,?)",
                    (digest(ticket), user["id"], session_hash, "dashboard", int(time.time()) + 60),
                )
                query = dict(parse_qsl(parsed.query))
                query["ticket"] = ticket
                login = urlunsplit(
                    (parsed.scheme, parsed.netloc, "/__swarm/login", urlencode(query), "")
                )
                client = ClientSession(timeout=ClientTimeout(total=25))
                try:
                    async with client.get(login) as response:
                        if response.status != 200:
                            raise web.HTTPBadGateway(
                                text="Analytics sign-in is temporarily unavailable."
                            )
                        await response.read()
                except BaseException:
                    await client.close()
                    raise
                base = urlunsplit((parsed.scheme, parsed.netloc, "", "", ""))
                self.connections[key] = (client, base, time.monotonic() + 3000)
            client, base, _ = self.connections[key]
            return client, base

    async def resource(self, request: web.Request, user: dict[str, Any]) -> web.Response:
        resource = request.query.get("resource")
        if resource == "page":
            if user["status"] != "running":
                raise web.HTTPConflict(text="Start your swarm to view analytics.")
            page, policy = portal_page()
            return web.Response(
                text=page, content_type="text/html", headers={"Content-Security-Policy": policy}
            )
        if resource not in {"avatar", "activity"}:
            raise web.HTTPBadRequest()
        try:
            agent = str(UUID(request.query.get("agent", "")))
        except ValueError:
            raise web.HTTPBadRequest() from None
        query = urlencode({k: request.query[k] for k in ("limit", "before") if k in request.query})
        path = f"/api/agents/{agent}/{resource}" + ("?" + query if query else "")
        client, base = await self.connection(user, digest(request.headers["X-Swarm-Session"]))
        async with client.get(base + path, allow_redirects=False) as response:
            content = await response.read()
            if response.status != 200:
                raise web.HTTPBadGateway(text="Agent activity is temporarily unavailable.")
            expected = "image/jpeg" if resource == "avatar" else "application/json"
            if response.content_type != expected:
                raise web.HTTPBadGateway()
            return web.Response(body=content, content_type=expected)

    async def ticket(self, request: web.Request, user: dict[str, Any]) -> web.Response:
        if user["status"] != "running":
            raise web.HTTPConflict(text="Start your swarm to view analytics.")
        token, now = secrets.token_urlsafe(32), int(time.time())
        await self.store.sql("DELETE FROM service_tickets WHERE expires<?", (now,))
        await self.store.sql(
            "INSERT INTO service_tickets VALUES(?,?,?,?,?)",
            (
                digest(token),
                user["id"],
                digest(request.headers["X-Swarm-Session"]),
                "portal_analytics",
                now + 60,
            ),
        )
        parsed = urlsplit(self.public_url)
        return web.json_response(
            {
                "url": urlunsplit(
                    ("wss", parsed.netloc, "/portal/analytics", urlencode({"ticket": token}), "")
                )
            }
        )

    async def socket(self, request: web.Request) -> web.WebSocketResponse:
        if request.headers.get("Origin") != "https://swarm.forum":
            raise web.HTTPForbidden()
        now = int(time.time())
        rows = await self.store.sql(
            "DELETE FROM service_tickets WHERE token_hash=? AND service='portal_analytics' "
            "AND expires>? AND session_hash IN (SELECT token_hash FROM sessions WHERE expires>?) "
            "RETURNING user_id,session_hash",
            (digest(request.query.get("ticket", "")), now, now),
        )
        if not rows:
            raise web.HTTPUnauthorized()
        identity = rows[0]
        user = await self.store.user(identity["user_id"])
        client, base = await self.connection(user, identity["session_hash"])
        async with client.ws_connect(
            base + "/api/ws", heartbeat=20, max_msg_size=16 * 1024 * 1024
        ) as upstream:
            output = web.WebSocketResponse(heartbeat=20, max_msg_size=1024)
            await output.prepare(request)

            async def relay() -> None:
                async for message in upstream:
                    if message.type == WSMsgType.TEXT:
                        payload = json.loads(message.data)
                        if (
                            self.billing
                            and payload.get("type") == "snapshot"
                            and payload.get("data", {}).get("workspace")
                        ):
                            payload["data"]["billing"] = await self.billing.snapshot(
                                user["id"], payload["data"]["workspace"]
                            )
                        await output.send_json(payload)

            async def watch() -> None:
                async for _ in output:
                    pass

            async def validate() -> None:
                while True:
                    await asyncio.sleep(5)
                    valid = await self.store.sql(
                        "SELECT s.token_hash FROM sessions s JOIN users u ON u.id=s.user_id "
                        "WHERE s.token_hash=? AND s.expires>? AND u.id=? AND u.status='running' "
                        "AND u.instance=?",
                        (identity["session_hash"], int(time.time()), user["id"], user["instance"]),
                    )
                    if not valid:
                        return

            tasks = [asyncio.create_task(fn()) for fn in (relay, watch, validate)]
            try:
                await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            finally:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                await output.close()
            return output

    async def close(self) -> None:
        for client, _, _ in self.connections.values():
            with contextlib.suppress(Exception):
                await client.close()
        self.connections.clear()
