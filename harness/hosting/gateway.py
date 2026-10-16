"""Tenant browser gateway: portal-bound sessions, native login, streaming and sockets.

Backend ports stay on loopback. Each service has its own origin and host-only cookie.
The gateway receives a scoped tenant capability, never the platform's cloud key.
"""

import asyncio
import base64
import contextlib
import json
import logging
import re
import time
from http.cookies import SimpleCookie
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlsplit

from aiohttp import ClientSession, ClientTimeout, DummyCookieJar, WSMsgType, web
from dotenv import dotenv_values

from harness.hosting.paths import service_path

FRONT_PORTS = {"mattermost": 18065, "gitea": 13000, "opencode": 14096, "dashboard": 18066}
BACK_PORTS = {"mattermost": 8065, "gitea": 3000, "opencode": 4096, "dashboard": 8066}
COOKIE = "__Host-swarm_access"
HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}


class ServiceGateway:
    def __init__(self, env: dict[str, Any], coding: dict[str, Any], workspace: dict[str, Any]):
        self.env, self.coding, self.workspace = env, coding, workspace
        self.client: ClientSession | None = None
        self.checked: dict[tuple[str, str], float] = {}
        self.runners: list[web.AppRunner] = []

    def public_url(self, service: str) -> str:
        return str(
            self.env[
                "HOSTED_" + ("ANALYTICS" if service == "dashboard" else service.upper()) + "_URL"
            ]
        ).rstrip("/")

    async def broker(self, operation: str, body: dict[str, Any]) -> dict[str, Any]:
        assert self.client
        async with self.client.post(
            str(self.env["HOSTED_GATEWAY_BROKER_URL"]).rstrip("/") + "/gateway/" + operation,
            headers={"Authorization": "Bearer " + str(self.env["HOSTED_GATEWAY_KEY"])},
            json=body,
            timeout=ClientTimeout(total=20),
            auto_decompress=True,
        ) as response:
            if response.status != 200:
                raise web.HTTPUnauthorized(
                    text="Open this service from https://swarm.forum to sign in."
                )
            return dict(await response.json())

    async def authorized(self, request: web.Request, service: str) -> None:
        token = request.cookies.get(COOKIE, "")
        if not token:
            raise web.HTTPUnauthorized(
                text="Open this service from https://swarm.forum to sign in."
            )
        key = (service, token)
        if self.checked.get(key, 0) > time.monotonic():
            return
        await self.broker("check", {"service": service, "token": token})
        self.checked = {k: v for k, v in self.checked.items() if v > time.monotonic()}
        self.checked[key] = time.monotonic() + 5

    @staticmethod
    def cookies(response: web.StreamResponse, values: list[str]) -> None:
        for value in values:
            cookie: SimpleCookie = SimpleCookie()
            cookie.load(value)
            for morsel in cookie.values():
                morsel["domain"] = ""
                morsel["secure"] = True
                response.headers.add("Set-Cookie", morsel.OutputString())

    async def login(self, request: web.Request, service: str) -> web.Response:
        assert self.client
        result = await self.broker(
            "exchange", {"service": service, "ticket": request.query.get("ticket", "")}
        )
        destination = request.query.get("next", "")
        try:
            location = service_path(destination) if destination else "/"
        except ValueError:
            raise web.HTTPBadRequest(text="Invalid service path") from None
        response = web.HTTPFound(location)
        if service == "mattermost":
            async with self.client.post(
                "http://127.0.0.1:" + str(BACK_PORTS["mattermost"]) + "/api/v4/users/login",
                headers={"X-Requested-With": "XMLHttpRequest", "X-Forwarded-Proto": "https"},
                json={
                    "login_id": "owner",
                    "password": self.env["HOSTED_OWNER_PASSWORD"],
                },
                timeout=ClientTimeout(total=30),
            ) as native:
                if native.status != 200:
                    raise web.HTTPBadGateway(
                        text="Mattermost sign-in could not be completed. Try opening it again."
                    )
                self.cookies(response, native.headers.getall("Set-Cookie", []))
            team = self.workspace.get("team_name", "")
            if team and not destination:
                response.headers["Location"] = "/" + team + "/channels/town-square"
        response.set_cookie(
            COOKIE,
            result["token"],
            max_age=3600,
            path="/",
            secure=True,
            httponly=True,
            samesite="Lax",
        )
        response.headers["Cache-Control"] = "no-store"
        response.headers["Referrer-Policy"] = "no-referrer"
        raise response

    def headers(self, request: web.Request, service: str) -> dict[str, str]:
        headers = {
            k: v
            for k, v in request.headers.items()
            if k.lower() not in HOP
            and not k.lower().startswith(
                ("x-forwarded-", "x-webauth-", "sec-websocket-", "x-agent37-")
            )
            and k.lower() not in {"host", "cookie", "content-length"}
        }
        headers["Host"] = urlsplit(self.public_url(service)).netloc
        headers["X-Forwarded-Proto"] = "https"
        headers["X-Forwarded-Host"] = headers["Host"]
        if request.cookies:
            cookie: SimpleCookie = SimpleCookie()
            for key, value in request.cookies.items():
                if key != COOKIE:
                    cookie[key] = value
            headers["Cookie"] = cookie.output(header="", sep=";").strip()
        if service == "opencode":
            basic = base64.b64encode(("opencode:" + self.coding["password"]).encode()).decode()
            headers["Authorization"] = "Basic " + basic
        elif service == "gitea":
            headers["X-WEBAUTH-USER"] = "swarm-owner"
        return headers

    async def socket(
        self, request: web.Request, service: str, target: str, headers: dict[str, str]
    ) -> web.WebSocketResponse:
        assert self.client
        protocols = [
            p.strip()
            for p in request.headers.get("Sec-WebSocket-Protocol", "").split(",")
            if p.strip()
        ]
        async with self.client.ws_connect(
            target, headers=headers, protocols=protocols, heartbeat=20, max_msg_size=0
        ) as upstream:
            output = web.WebSocketResponse(
                protocols=[upstream.protocol] if upstream.protocol else (),
                heartbeat=20,
                max_msg_size=0,
            )
            await output.prepare(request)

            async def relay(source: Any, destination: Any) -> None:
                async for message in source:
                    if message.type == WSMsgType.TEXT:
                        await destination.send_str(message.data)
                    elif message.type == WSMsgType.BINARY:
                        await destination.send_bytes(message.data)
                    elif message.type in {WSMsgType.CLOSE, WSMsgType.ERROR}:
                        break

            async def check_session() -> None:
                while True:
                    await asyncio.sleep(5)
                    await self.authorized(request, service)

            tasks = [
                asyncio.create_task(relay(request_socket, destination))
                for request_socket, destination in [(upstream, output), (output, upstream)]
            ]
            tasks.append(asyncio.create_task(check_session()))
            try:
                await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            finally:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                await output.close()
            return output

    async def handle(self, request: web.Request, service: str) -> web.StreamResponse:
        assert self.client
        if request.path == "/__swarm/login" and request.method == "GET":
            return await self.login(request, service)
        await self.authorized(request, service)
        if request.path == "/__swarm/links.js" and request.method == "GET":
            services = []
            for name, port in FRONT_PORTS.items():
                public = str(
                    self.env.get(
                        "HOSTED_" + ("ANALYTICS" if name == "dashboard" else name.upper()) + "_URL",
                        "",
                    )
                ).rstrip("/")
                match = re.fullmatch(
                    r"https://([a-z0-9]+)-" + str(port) + r"\.agent37\.app", public
                )
                if match:
                    services.append({"name": name, "origin": public, "instance": match[1]})
            script = "const swarmServices=" + json.dumps(services) + ";\n"
            script += Path(__file__).with_name("service_links.js").read_text()
            return web.Response(
                text=script,
                content_type="application/javascript",
                headers={"Cache-Control": "no-store"},
            )
        origin = request.headers.get("Origin")
        if (origin and origin != self.public_url(service)) or (
            request.method not in {"GET", "HEAD", "OPTIONS"} and not origin
        ):
            raise web.HTTPForbidden(text="Origin rejected.")
        target = "http://127.0.0.1:" + str(BACK_PORTS[service]) + request.rel_url.raw_path_qs
        headers = self.headers(request, service)
        browser_page = service == "mattermost" and "text/html" in request.headers.get("Accept", "")
        if browser_page:
            headers["Accept-Encoding"] = "identity"
            headers.pop("If-None-Match", None)
            headers.pop("If-Modified-Since", None)
        if request.headers.get("Upgrade", "").lower() == "websocket":
            return await self.socket(request, service, target, headers)
        # Stream request and response bodies; never buffer uploads or SSE in the gateway.
        async with self.client.request(
            request.method,
            target,
            headers=headers,
            data=request.content.iter_chunked(64 * 1024) if request.can_read_body else None,
            allow_redirects=False,
        ) as upstream:
            if (
                browser_page
                and upstream.status == 200
                and upstream.content_type == "text/html"
                and not upstream.headers.get("Content-Encoding")
            ):
                body = await upstream.text()
                body = body.replace(
                    "</head>", '<script src="/__swarm/links.js" defer></script></head>', 1
                )
                response_headers = {
                    k: v
                    for k, v in upstream.headers.items()
                    if k.lower()
                    not in HOP | {"set-cookie", "content-length", "etag", "content-type"}
                }
                response_headers["Cache-Control"] = "no-store"
                response_headers["Referrer-Policy"] = "no-referrer"
                html = web.Response(text=body, content_type="text/html", headers=response_headers)
                self.cookies(html, upstream.headers.getall("Set-Cookie", []))
                return html
            response = web.StreamResponse(status=upstream.status)
            for key, value in upstream.headers.items():
                if key.lower() not in HOP | {"set-cookie", "content-length"}:
                    response.headers.add(key, value)
            self.cookies(response, upstream.headers.getall("Set-Cookie", []))
            response.headers["Referrer-Policy"] = "no-referrer"
            await response.prepare(request)
            async for chunk in upstream.content.iter_chunked(64 * 1024):
                await response.write(chunk)
            await response.write_eof()
            return response

    def app(self, service: str) -> web.Application:
        @web.middleware
        async def errors(request: web.Request, handler: Any) -> web.StreamResponse:
            try:
                return cast(web.StreamResponse, await handler(request))
            except web.HTTPException:
                raise
            except (TimeoutError, ConnectionError):
                raise web.HTTPServiceUnavailable(
                    text="Service temporarily unavailable; return to swarm.forum."
                ) from None
            except Exception:
                logging.getLogger(__name__).warning(
                    "Service gateway request failed for %s", service
                )
                raise web.HTTPBadGateway(
                    text="Service temporarily unavailable; return to swarm.forum."
                ) from None

        async def route(request: web.Request) -> web.StreamResponse:
            return await self.handle(request, service)

        app = web.Application(middlewares=[errors], client_max_size=0)
        app.router.add_route("*", "/{path:.*}", route)
        return app

    async def run(self) -> None:
        async with ClientSession(
            cookie_jar=DummyCookieJar(),
            auto_decompress=False,
            timeout=ClientTimeout(total=None, sock_connect=15, sock_read=3600),
        ) as client:
            self.client = client
            try:
                for service, port in FRONT_PORTS.items():
                    runner = web.AppRunner(self.app(service), access_log=None)
                    self.runners.append(runner)
                    await runner.setup()
                    await web.TCPSite(runner, "0.0.0.0", port).start()
                await asyncio.Event().wait()
            finally:
                for runner in self.runners:
                    with contextlib.suppress(Exception):
                        await runner.cleanup()


def main() -> None:
    gateway = ServiceGateway(
        dict(dotenv_values(".env")),
        json.loads(Path(".state/coding.json").read_text()),
        json.loads(Path(".state/workspace.json").read_text()),
    )
    try:
        asyncio.run(gateway.run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
