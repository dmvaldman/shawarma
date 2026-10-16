import base64
from contextlib import asynccontextmanager

import pytest
from aiohttp import ClientSession, DummyCookieJar, WSMsgType, web
from aiohttp.test_utils import TestClient, TestServer

from harness.hosting.gateway import BACK_PORTS, ServiceGateway


@asynccontextmanager
async def gateway(service, monkeypatch):
    observed = []

    async def broker(request):
        assert request.headers["Authorization"] == "Bearer tenant-only-key"
        body = await request.json()
        assert body["service"] == service
        if (
            request.match_info["operation"] == "exchange"
            and body.get("ticket") == "one-time-ticket"
        ):
            response = web.json_response({"token": "service-session"})
            response.enable_compression()
            return response
        if request.match_info["operation"] == "check" and body.get("token") == "service-session":
            response = web.json_response({"ok": True})
            response.enable_compression()
            return response
        raise web.HTTPUnauthorized()

    async def backend(request):
        observed.append(request)
        if request.path == "/api/v4/users/login":
            assert await request.json() == {"login_id": "owner", "password": "native-mm-password"}
            response = web.json_response({"id": "owner-id"})
            # Mattermost only attaches browser cookies for an XMLHttpRequest login.
            if request.headers.get("X-Requested-With") == "XMLHttpRequest":
                response.set_cookie("MMAUTHTOKEN", "native-session", httponly=True)
            return response
        if request.path == "/page":
            return web.Response(
                text="<html><head></head><body>Chat</body></html>", content_type="text/html"
            )
        if request.path == "/socket":
            ws = web.WebSocketResponse()
            await ws.prepare(request)
            async for message in ws:
                if message.type == WSMsgType.TEXT:
                    await ws.send_str("echo:" + message.data)
            return ws
        if request.path == "/stream":
            output = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
            await output.prepare(request)
            await output.write(b"data: hello\n\n")
            await output.write_eof()
            return output
        return web.json_response({"ok": True})

    broker_app = web.Application()
    broker_app.router.add_post("/gateway/{operation}", broker)
    native_app = web.Application()
    native_app.router.add_route("*", "/{path:.*}", backend)
    async with TestServer(broker_app) as broker_server, TestServer(native_app) as native_server:
        monkeypatch.setitem(BACK_PORTS, service, native_server.port)
        env = {
            "HOSTED_GATEWAY_BROKER_URL": str(broker_server.make_url("/")).rstrip("/"),
            "HOSTED_GATEWAY_KEY": "tenant-only-key",
            "HOSTED_OWNER_PASSWORD": "native-mm-password",
            "HOSTED_"
            + ("ANALYTICS" if service == "dashboard" else service.upper())
            + "_URL": "https://service.example",
        }
        app = ServiceGateway(env, {"password": "native-oc-password"}, {"team_name": "my-swarm"})
        async with ClientSession(
            cookie_jar=DummyCookieJar(), auto_decompress=False
        ) as proxy_client:
            app.client = proxy_client
            async with TestClient(
                TestServer(app.app(service)), cookie_jar=DummyCookieJar()
            ) as browser:
                yield browser, observed


@pytest.mark.parametrize("service", ["mattermost", "gitea", "opencode", "dashboard"])
async def test_browser_gateway_requires_session_and_establishes_secure_cookie(monkeypatch, service):
    async with gateway(service, monkeypatch) as (browser, observed):
        assert (await browser.get("/")).status == 401
        assert not observed
        response = await browser.get("/__swarm/login?ticket=one-time-ticket", allow_redirects=False)
        assert response.status == 302
        cookies = response.headers.getall("Set-Cookie")
        assert any(
            "__Host-swarm_access=service-session" in c
            and "HttpOnly" in c
            and "Secure" in c
            and "SameSite=Lax" in c
            for c in cookies
        )
        if service == "mattermost":
            assert any("MMAUTHTOKEN=native-session" in c and "Secure" in c for c in cookies)
            assert response.headers["Location"] == "/my-swarm/channels/town-square"
        headers = {
            "Cookie": "__Host-swarm_access=service-session; MMAUTHTOKEN=native-session",
            "X-WEBAUTH-USER": "attacker",
        }
        response = await browser.get("/", headers=headers)
        assert response.status == 200
        if service == "gitea":
            assert observed[-1].headers["X-WEBAUTH-USER"] == "swarm-owner"
        elif service == "opencode":
            assert (
                observed[-1].headers["Authorization"]
                == "Basic " + base64.b64encode(b"opencode:native-oc-password").decode()
            )
        else:
            assert "X-WEBAUTH-USER" not in observed[-1].headers
        assert "__Host-swarm_access" not in observed[-1].headers.get("Cookie", "")
        assert (await browser.post("/", headers=headers, json={})).status == 403
        assert (
            await browser.post("/", headers={**headers, "Origin": "https://evil.example"}, json={})
        ).status == 403
        assert (
            await browser.post(
                "/", headers={**headers, "Origin": "https://service.example"}, json={}
            )
        ).status == 200


async def test_gateway_preserves_sse_and_websocket_messages(monkeypatch):
    async with gateway("opencode", monkeypatch) as (browser, _):
        headers = {
            "Cookie": "__Host-swarm_access=service-session",
            "Origin": "https://service.example",
        }
        response = await browser.get("/stream", headers=headers)
        assert response.headers["Content-Type"] == "text/event-stream"
        assert await response.text() == "data: hello\n\n"
        async with browser.ws_connect("/socket", headers=headers) as socket:
            await socket.send_str("hello")
            message = await socket.receive(timeout=2)
            assert message.type == WSMsgType.TEXT and message.data == "echo:hello"


async def test_service_login_redirects_only_to_relative_native_destination(monkeypatch):
    async with gateway("gitea", monkeypatch) as (browser, observed):
        response = await browser.get(
            "/__swarm/login?ticket=one-time-ticket&next=/swarm/research/pulls/3",
            allow_redirects=False,
        )
        assert response.status == 302
        assert response.headers["Location"] == "/swarm/research/pulls/3"
        for value in ("//evil.example", "/\\evil.example", "/%0d%0aLocation:evil"):
            response = await browser.get(
                "/__swarm/login",
                params={"ticket": "one-time-ticket", "next": value},
                allow_redirects=False,
            )
            assert response.status == 400


async def test_mattermost_pages_load_authenticated_link_rewriter(monkeypatch):
    async with gateway("mattermost", monkeypatch) as (browser, observed):
        assert (await browser.get("/__swarm/links.js")).status == 401
        headers = {"Cookie": "__Host-swarm_access=service-session", "Accept": "text/html"}
        response = await browser.get("/page", headers=headers)
        assert response.status == 200
        assert '<script src="/__swarm/links.js" defer></script>' in await response.text()
        assert response.headers["Cache-Control"] == "no-store"
        assert observed[-1].headers["Accept-Encoding"] == "identity"
        script = await browser.get("/__swarm/links.js", headers=headers)
        assert script.status == 200
        assert "MutationObserver" in await script.text()
        assert "swarm.forum/#" in await script.text()
