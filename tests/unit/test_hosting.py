import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from aiohttp import web
from cryptography.fernet import Fernet

from harness.hosting.server import Hosting
from harness.hosting.store import HostingStore, digest


@pytest.fixture
async def hosted(tmp_path, monkeypatch):
    store = HostingStore(tmp_path / "hosting.db", Fernet.generate_key().decode())
    cloud = SimpleNamespace(request=AsyncMock(), command=AsyncMock())
    hosting = Hosting(store, cloud, "service-secret", "https://broker.example", "ref", "owner-code")
    monkeypatch.setattr(hosting, "jobs", AsyncMock())
    runner = web.AppRunner(hosting.app())
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    client = httpx.AsyncClient(
        base_url=f"http://127.0.0.1:{port}", headers={"X-Swarm-Service-Key": "service-secret"}
    )
    try:
        yield hosting, client
    finally:
        await client.aclose()
        await runner.cleanup()


async def login(client, code):
    result = await client.post("/api/login", json={"code": code})
    assert result.status_code == 200
    return result.json()["session"]


async def test_invites_sessions_and_service_access_are_scoped(hosted):
    hosting, client = hosted
    assert (await client.get("/api/me")).status_code == 401
    owner_token = await login(client, "owner-code")
    owner_headers = {"X-Swarm-Session": owner_token}
    result = await client.post("/api/invite", json={"name": "Alice"}, headers=owner_headers)
    assert result.status_code == 200
    invite = result.json()["url"].split("#invite=")[1]
    alice_token = await login(client, invite)
    alice_headers = {"X-Swarm-Session": alice_token}
    owner = (await client.get("/api/me", headers=owner_headers)).json()
    alice = (await client.get("/api/me", headers=alice_headers)).json()
    assert owner["id"] != alice["id"]
    assert "vault" not in alice and "login_hash" not in alice
    assert (
        await client.post("/api/invite", json={"name": "Eve"}, headers=alice_headers)
    ).status_code == 403
    assert (
        await client.get("/api/me", headers={**alice_headers, "X-Swarm-Service-Key": "wrong"})
    ).status_code == 401
    await hosting.store.sql(
        "UPDATE users SET instance='alice-node',status='running' WHERE id=?", (alice["id"],)
    )
    hosting.cloud.request.return_value = {
        "url": "https://alice-node-18066.agent37.app/?a37_token=short"
    }
    result = await client.post(
        "/api/open", json={"service": "dashboard", "user_id": owner["id"]}, headers=alice_headers
    )
    assert result.status_code == 200
    assert hosting.cloud.request.call_args.args[1] == "instances/alice-node/signed-url"
    assert (
        await client.post("/api/open", json={"service": "ssh"}, headers=alice_headers)
    ).status_code == 409
    await client.post("/api/logout", json={}, headers=alice_headers)
    assert (await client.get("/api/me", headers=alice_headers)).status_code == 401


async def test_provider_keys_encrypted_and_only_proxy_credential_enters_vm(hosted, monkeypatch):
    hosting, client = hosted
    token = await login(client, "owner-code")
    user = (await client.get("/api/me", headers={"X-Swarm-Session": token})).json()
    row = await hosting.store.user(user["id"])
    vault = hosting.store.unseal(row)
    vault["providers"]["openai"] = "private-real-provider-key"
    await hosting.store.save_vault(user["id"], vault)
    row = await hosting.store.user(user["id"])
    assert "private-real-provider-key" not in row["vault"]
    assert (await hosting.store.sql("SELECT token_hash FROM sessions"))[0]["token_hash"] == digest(
        token
    )
    monkeypatch.setattr(
        hosting,
        "models",
        AsyncMock(
            return_value={
                "models": [
                    {
                        "id": "openai:gpt-6-luna",
                        "agent_candidate": True,
                        "input": ".25",
                        "output": ".75",
                        "class": "lite",
                    }
                ]
            }
        ),
    )
    hosting.cloud.install = AsyncMock()
    row["instance"] = "test-node"
    await hosting.install(row)
    env = hosting.cloud.install.call_args.args[1]
    assert env["OPENAI_API_KEY"] == vault["agent_token"]
    assert "private-real-provider-key" not in json.dumps(env)
    assert "AGENT37_API_KEY" not in env
    assert env["HARNESS_OPENAI_BASE_URL"].startswith("https://broker.example/proxy/")


async def test_start_requires_key_and_pause_preserves_machine(hosted):
    hosting, client = hosted
    token = await login(client, "owner-code")
    headers = {"X-Swarm-Session": token}
    assert (await client.post("/api/start", json={"hours": 8}, headers=headers)).status_code == 400
    user = (await client.get("/api/me", headers=headers)).json()
    row = await hosting.store.user(user["id"])
    vault = hosting.store.unseal(row)
    vault["providers"]["openai"] = "secret"
    await hosting.store.save_vault(row["id"], vault)
    assert (await client.post("/api/start", json={"hours": 8}, headers=headers)).status_code == 200
    row = await hosting.store.user(user["id"])
    assert row["action"] == "start" and row["run_until"]
    assert (await client.post("/api/start", json={"hours": 8}, headers=headers)).status_code == 409
    await hosting.store.sql(
        "UPDATE users SET action=NULL,status='running',instance='same-node' WHERE id=?",
        (user["id"],),
    )
    assert (await client.post("/api/pause", json={}, headers=headers)).status_code == 200
    row = await hosting.store.user(user["id"])
    assert row["instance"] == "same-node" and row["action"] == "pause"
    assert row["run_until"] is None
    assert (await client.post("/api/credentials", json={}, headers=headers)).status_code == 409
    hosting.cloud.command.assert_not_awaited()


async def test_broker_authentication_and_path_restrictions(hosted):
    _, client = hosted
    assert (await client.post("/proxy/openai/v1/responses", json={})).status_code == 401
    assert (await client.post("/proxy/openai/v1/files", json={})).status_code == 403
    assert (await client.get("/proxy/not-a-provider/v1/models")).status_code == 403
    assert (await client.get("/proxy/openai/v1/models/%252e%252e")).status_code == 403


async def test_broker_inserts_only_owners_key_and_preserves_stream(hosted, monkeypatch):
    from harness.hosting.server import PROVIDERS

    hosting, client = hosted
    observed = []

    async def upstream(request):
        observed.append((request.headers.get("Authorization"), await request.json()))
        response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
        await response.prepare(request)
        await response.write(b"data: first\n\n")
        await response.write(b"data: [DONE]\n\n")
        await response.write_eof()
        return response

    app = web.Application()
    app.router.add_post("/responses", upstream)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    monkeypatch.setitem(PROVIDERS, "openai", f"http://127.0.0.1:{port}")
    try:
        token = await login(client, "owner-code")
        user = (await client.get("/api/me", headers={"X-Swarm-Session": token})).json()
        row = await hosting.store.user(user["id"])
        vault = hosting.store.unseal(row)
        vault["providers"]["openai"] = "owners-real-key"
        await hosting.store.save_vault(row["id"], vault)
        response = await client.post(
            "/proxy/openai/v1/responses",
            headers={"Authorization": "Bearer " + vault["agent_token"]},
            json={"stream": True, "input": "hello"},
        )
        assert response.status_code == 200
        assert response.headers["content-type"] == "text/event-stream"
        assert response.text == "data: first\n\ndata: [DONE]\n\n"
        assert observed == [("Bearer owners-real-key", {"stream": True, "input": "hello"})]
        assert "owners-real-key" not in response.text
    finally:
        await runner.cleanup()


async def test_expiry_stops_own_machine_while_another_user_is_provisioning(hosted, monkeypatch):
    import asyncio

    hosting, client = hosted
    owner_token = await login(client, "owner-code")
    owner = (await client.get("/api/me", headers={"X-Swarm-Session": owner_token})).json()
    friend_code = "friend-test-code"
    await hosting.store.invite("Friend", "user", friend_code)
    friend = (
        await hosting.store.sql("SELECT id FROM users WHERE login_hash=?", (digest(friend_code),))
    )[0]["id"]
    await hosting.store.sql(
        "UPDATE users SET instance='owner-node',status='running',run_until=1 WHERE id=?",
        (owner["id"],),
    )
    await hosting.store.sql(
        "UPDATE users SET instance='friend-node',status='queued',action='start' WHERE id=?",
        (friend,),
    )
    installing = asyncio.Event()
    release = asyncio.Event()

    async def blocked_install(user):
        installing.set()
        await release.wait()

    monkeypatch.setattr(hosting, "install", blocked_install)
    task = asyncio.create_task(Hosting.jobs(hosting))
    try:
        await asyncio.wait_for(installing.wait(), timeout=1)
        for _ in range(100):
            row = await hosting.store.user(owner["id"])
            if row["status"] == "sleeping":
                break
            await asyncio.sleep(0.01)
        assert row["status"] == "sleeping"
        assert row["instance"] == "owner-node"
        assert (await hosting.store.user(friend))["status"] == "provisioning"
        hosting.cloud.request.assert_any_await("POST", "instances/owner-node/stop")
        assert all(
            call.args[1] != "instances/friend-node/stop"
            for call in hosting.cloud.request.await_args_list
        )
    finally:
        hosting.stopping = True
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        for operation in hosting.operations.values():
            operation.cancel()
        await asyncio.gather(*hosting.operations.values(), return_exceptions=True)


@pytest.mark.parametrize(
    "provider,model", [("anthropic", "claude-haiku-5-5"), ("deepseek", "deepseek-flash")]
)
async def test_non_openai_account_can_start_and_configure_seed(
    hosted, monkeypatch, provider, model
):
    hosting, client = hosted
    token = await login(client, "owner-code")
    headers = {"X-Swarm-Session": token}
    me = (await client.get("/api/me", headers=headers)).json()
    row = await hosting.store.user(me["id"])
    vault = hosting.store.unseal(row)
    vault["providers"][provider] = "the-users-provider-key"
    await hosting.store.save_vault(row["id"], vault)
    monkeypatch.setattr(
        hosting,
        "models",
        AsyncMock(
            return_value={
                "models": [
                    {
                        "id": provider + ":" + model,
                        "agent_candidate": True,
                        "input": "1",
                        "output": "2",
                        "class": "lite",
                    }
                ]
            }
        ),
    )
    hosting.cloud.install = AsyncMock()
    assert (await client.post("/api/start", json={"hours": 2}, headers=headers)).status_code == 200
    row = await hosting.store.user(row["id"])
    row["instance"] = "test-node"
    await hosting.install(row)
    env = hosting.cloud.install.call_args.args[1]
    assert env["HARNESS_SEED_MODEL"] == provider + ":" + model
    assert env["OPENAI_API_KEY"] == ""
    assert env[provider.upper() + "_API_KEY"] == vault["agent_token"]
    assert env["HARNESS_AVATAR_API_KEY"] == vault["agent_token"]
    assert env["HARNESS_AVATAR_BASE_URL"].endswith("/proxy/avatar/v1")
    assert "the-users-provider-key" not in json.dumps(env)


async def test_masked_keys_and_models_are_owner_scoped_cached_and_deleted(hosted, monkeypatch):
    hosting, client = hosted
    original = httpx.AsyncClient
    calls = []

    def provider_api(request):
        calls.append(request)
        return httpx.Response(200, json={"data": [{"id": "deepseek-flash"}]})

    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: original(**{**kwargs, "transport": httpx.MockTransport(provider_api)}),
    )
    token = await login(client, "owner-code")
    headers = {"X-Swarm-Session": token}
    key = "private-provider-key-that-stays-secret"
    assert (
        await client.post(
            "/api/providers", headers=headers, json={"provider": "deepseek", "key": key}
        )
    ).status_code == 200
    me = (await client.get("/api/me", headers=headers)).json()
    assert me["provider_keys"]["deepseek"] == "private-" + "*" * 12
    assert key not in json.dumps(me)
    for _ in range(2):
        models = (await client.get("/api/models", headers=headers)).json()
        assert models["models"][0]["id"] == "deepseek:deepseek-flash"
        assert key not in json.dumps(models)
    assert len(calls) == 2  # key validation, then one cached catalog retrieval
    assert (
        await client.post(
            "/api/providers", headers=headers, json={"provider": "deepseek", "key": ""}
        )
    ).status_code == 200
    assert (await client.get("/api/models", headers=headers)).json()["models"] == []
    assert (await client.post("/api/start", headers=headers, json={"hours": 2})).status_code == 400
    assert hosting.masked_key("short-key") == "*" * 12


async def test_host_avatar_key_only_authorizes_fixed_image_requests(hosted, monkeypatch):
    hosting, client = hosted
    hosting.avatar_key = "private-host-image-key"
    original = httpx.AsyncClient
    observed = []

    def upstream(request):
        observed.append(request)
        assert request.headers["Authorization"] == "Bearer private-host-image-key"
        return httpx.Response(200, json={"data": [{"b64_json": "test"}]})

    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: original(**{**kwargs, "transport": httpx.MockTransport(upstream)}),
    )
    await login(client, "owner-code")
    row = (await hosting.store.sql("SELECT * FROM users WHERE role='admin'"))[0]
    vault = hosting.store.unseal(row)
    assert not vault["providers"]
    headers = {"Authorization": "Bearer " + vault["agent_token"]}
    assert (
        await client.post("/proxy/avatar/v1/responses", headers=headers, json={})
    ).status_code == 403
    body = {
        "model": "gpt-image-2.5-flare",
        "prompt": "a friendly research teammate",
        "n": 1,
        "size": "1024x1024",
        "quality": "low",
        "output_format": "jpeg",
        "output_compression": 80,
    }
    assert (
        await client.post(
            "/proxy/avatar/v1/images/generations", headers=headers, json={**body, "n": 2}
        )
    ).status_code == 400
    response = await client.post("/proxy/avatar/v1/images/generations", headers=headers, json=body)
    assert response.status_code == 200
    assert len(observed) == 1 and observed[0].url.path == "/v1/images/generations"
    assert "private-host-image-key" not in response.text
    assert (
        await client.post("/proxy/openai/v1/responses", headers=headers, json={})
    ).status_code == 401


async def test_service_tickets_are_single_use_bound_and_revoked_on_logout(hosted):
    from urllib.parse import parse_qs, urlsplit

    hosting, client = hosted
    token = await login(client, "owner-code")
    headers = {"X-Swarm-Session": token}
    me = (await client.get("/api/me", headers=headers)).json()
    await hosting.store.sql(
        "UPDATE users SET status='running',instance='owner-node' WHERE id=?", (me["id"],)
    )
    hosting.cloud.request.return_value = {
        "url": "https://owner-node-18065.agent37.app/?a37_token=edge"
    }
    opened = (
        await client.post("/api/open", headers=headers, json={"service": "mattermost"})
    ).json()["url"]
    assert urlsplit(opened).path == "/__swarm/login"
    ticket = parse_qs(urlsplit(opened).query)["ticket"][0]
    vault = hosting.store.unseal(await hosting.store.user(me["id"]))
    gateway_headers = {"Authorization": "Bearer " + vault["agent_token"]}
    wrong = await client.post(
        "/gateway/exchange", headers=gateway_headers, json={"service": "gitea", "ticket": ticket}
    )
    assert wrong.status_code == 401
    exchange = await client.post(
        "/gateway/exchange",
        headers=gateway_headers,
        json={"service": "mattermost", "ticket": ticket},
    )
    assert exchange.status_code == 200
    cookie = exchange.json()["token"]
    assert (
        await client.post(
            "/gateway/exchange",
            headers=gateway_headers,
            json={"service": "mattermost", "ticket": ticket},
        )
    ).status_code == 401
    check = {"service": "mattermost", "token": cookie}
    assert (
        await client.post("/gateway/check", headers=gateway_headers, json=check)
    ).status_code == 200
    assert (
        await client.post(
            "/gateway/check", headers=gateway_headers, json={**check, "service": "gitea"}
        )
    ).status_code == 401
    await client.post("/api/logout", headers=headers, json={})
    assert (
        await client.post("/gateway/check", headers=gateway_headers, json=check)
    ).status_code == 401


async def test_pending_identity_cannot_access_resources_until_invited(hosted):
    hosting, client = hosted
    await hosting.store.invite("Pending", code="pending-login", subject="pending", activated=False)
    token = await login(client, "pending-login")
    headers = {"X-Swarm-Session": token}
    assert not (await client.get("/api/me", headers=headers)).json()["activated"]
    for route in ("providers", "start", "pause", "invite", "open", "credentials"):
        assert (await client.post("/api/" + route, headers=headers, json={})).status_code == 403
    assert (await client.get("/api/models", headers=headers)).status_code == 403
    hosting.cloud.request.assert_not_called()
    assert (
        await client.post("/api/redeem", headers=headers, json={"code": "wrong"})
    ).status_code == 403
    assert (
        await client.post("/api/redeem", headers=headers, json={"code": "owner-code"})
    ).status_code == 200
    me = (await client.get("/api/me", headers=headers)).json()
    assert me["activated"] and me["role"] == "admin"
    await client.post("/api/logout", headers=headers, json={})
    assert (await client.get("/api/me", headers=headers)).status_code == 401


async def test_portal_stop_discards_only_own_machine_and_play_starts_fresh(hosted, monkeypatch):
    hosting, client = hosted
    headers = {"X-Swarm-Session": await login(client, "owner-code")}
    owner = (await client.get("/api/me", headers=headers)).json()
    row = await hosting.store.user(owner["id"])
    vault = hosting.store.unseal(row)
    vault["providers"]["openai"] = "owners-key"
    vault["database"] = {"postgres": "old-database"}
    old_token = vault["agent_token"]
    await hosting.store.save_vault(row["id"], vault)
    await hosting.store.sql(
        "UPDATE users SET status='running',instance='old-node' WHERE id=?", (row["id"],)
    )
    await hosting.store.invite("Alice", code="alice-invite")
    await hosting.store.sql(
        "UPDATE users SET status='running',instance='alice-node' WHERE name='Alice'"
    )
    assert (await client.post("/api/stop", json={}, headers=headers)).status_code == 200
    assert (await client.get("/api/me", headers=headers)).json()["status"] == "stopping"
    assert (
        await client.post(
            "/proxy/openai/v1/responses", headers={"Authorization": "Bearer " + old_token}, json={}
        )
    ).status_code == 403
    assert (await client.post("/api/start", json={}, headers=headers)).status_code == 409
    await hosting.run_job(await hosting.store.user(row["id"]))
    hosting.cloud.request.assert_awaited_once_with("DELETE", "instances/old-node")
    stopped = await hosting.store.user(row["id"])
    assert stopped["instance"] is None and stopped["status"] == "stopped"
    fresh = hosting.store.unseal(stopped)
    assert fresh["providers"] == {"openai": "owners-key"}
    assert "database" not in fresh and fresh["agent_token"] != old_token
    assert (await hosting.store.sql("SELECT instance FROM users WHERE name='Alice'"))[0][
        "instance"
    ] == "alice-node"
    hosting.cloud.request.reset_mock()
    hosting.cloud.request.return_value = {"id": "fresh-node"}
    monkeypatch.setattr(hosting, "install", AsyncMock())
    assert (await client.post("/api/start", json={"hours": 2}, headers=headers)).status_code == 200
    await hosting.run_job(await hosting.store.user(row["id"]))
    assert (await hosting.store.user(row["id"]))["instance"] == "fresh-node"
    assert hosting.cloud.request.call_args.args[:2] == ("POST", "instances")


async def test_embedded_analytics_requires_access_and_preserves_dashboard_features(hosted):
    hosting, client = hosted
    assert (await client.get("/api/analytics?resource=page")).status_code == 401
    headers = {"X-Swarm-Session": await login(client, "owner-code")}
    assert (await client.get("/api/analytics?resource=page", headers=headers)).status_code == 409
    owner = (await client.get("/api/me", headers=headers)).json()
    await hosting.store.sql(
        "UPDATE users SET status='running',instance='own-node' WHERE id=?", (owner["id"],)
    )
    page = await client.get("/api/analytics?resource=page", headers=headers)
    assert page.status_code == 200
    assert "nonce-" in page.headers["Content-Security-Policy"]
    assert "frame-ancestors 'self'" in page.headers["Content-Security-Policy"]
    assert "await portalSocketURL()" in page.text and "function analyticsURL(" in page.text
    assert "fetch(analyticsURL('/api/agents/" in page.text
    assert "image.src=analyticsURL(a.avatar_url)" in page.text
    assert 'id="agent-detail"' in page.text
    assert (
        await client.get("/api/analytics?resource=credentials", headers=headers)
    ).status_code == 400
    assert (
        await client.get("/api/analytics?resource=activity&agent=../etc", headers=headers)
    ).status_code == 400
    hosting.cloud.request.assert_not_called()


async def test_analytics_websocket_ticket_is_single_use_and_revoked_on_logout(hosted, monkeypatch):
    import asyncio
    from urllib.parse import urlsplit

    from aiohttp import ClientSession, WSServerHandshakeError

    hosting, client = hosted
    headers = {"X-Swarm-Session": await login(client, "owner-code")}
    owner = (await client.get("/api/me", headers=headers)).json()
    await hosting.store.sql(
        "UPDATE users SET status='running',instance='own-node' WHERE id=?", (owner["id"],)
    )
    native = web.Application()

    async def socket(request):
        output = web.WebSocketResponse()
        await output.prepare(request)
        await output.send_json({"type": "snapshot", "data": {"owner": owner["id"]}})
        async for _ in output:
            pass
        return output

    native.router.add_get("/api/ws", socket)
    runner = web.AppRunner(native)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    try:
        async with ClientSession() as upstream, ClientSession() as browser:
            connection = AsyncMock(return_value=(upstream, f"http://127.0.0.1:{port}"))
            monkeypatch.setattr(hosting.analytics, "connection", connection)
            ticket = (await client.post("/api/dashboard-connect", headers=headers, json={})).json()[
                "url"
            ]
            url = str(client.base_url).rstrip("/") + "/portal/analytics?" + urlsplit(ticket).query
            with pytest.raises(WSServerHandshakeError) as denied:
                await browser.ws_connect(url, origin="https://evil.example")
            assert denied.value.status == 403
            async with browser.ws_connect(url, origin="https://swarm.forum") as ws:
                assert (await ws.receive_json())["data"]["owner"] == owner["id"]
                with pytest.raises(WSServerHandshakeError) as replay:
                    await browser.ws_connect(url, origin="https://swarm.forum")
                assert replay.value.status == 401
                await client.post("/api/logout", json={}, headers=headers)
                message = await asyncio.wait_for(ws.receive(), timeout=7)
                assert message.type.name in {"CLOSE", "CLOSED"}
            assert connection.await_args.args[0]["id"] == owner["id"]
    finally:
        await runner.cleanup()


async def test_admin_keys_are_separate_encrypted_and_never_sent_to_workers(hosted, monkeypatch):
    from decimal import Decimal

    hosting, client = hosted
    headers = {"X-Swarm-Session": await login(client, "owner-code")}
    owner = (await client.get("/api/me", headers=headers)).json()
    row = await hosting.store.user(owner["id"])
    vault = hosting.store.unseal(row)
    vault["providers"]["openai"] = "private-inference-key"
    await hosting.store.save_vault(row["id"], vault)
    monkeypatch.setattr(
        "harness.hosting.server.read_provider_cost", AsyncMock(return_value=Decimal("10"))
    )
    secret = "private-admin-key-for-billing"
    await hosting.store.sql("UPDATE users SET status='running' WHERE id=?", (row["id"],))
    saved = await client.post(
        "/api/admin-keys", headers=headers, json={"provider": "openai", "key": secret}
    )
    assert saved.status_code == 200
    me = (await client.get("/api/me", headers=headers)).json()
    assert me["admin_keys"]["openai"] == secret[:8] + "*" * 12
    assert secret not in json.dumps(me)
    row = await hosting.store.user(row["id"])
    assert secret not in row["vault"]
    assert hosting.store.unseal(row)["providers"]["openai"] == "private-inference-key"
    await hosting.store.invite("Alice", code="alice")
    other = {"X-Swarm-Session": await login(client, "alice")}
    assert (await client.get("/api/me", headers=other)).json()["admin_keys"] == {}
    assert (
        await client.post(
            "/api/admin-keys", headers=headers, json={"provider": "deepseek", "key": secret}
        )
    ).status_code == 400
    monkeypatch.setattr(
        hosting,
        "models",
        AsyncMock(
            return_value={
                "models": [
                    {
                        "id": "openai:model",
                        "agent_candidate": True,
                        "input": "1",
                        "output": "2",
                        "class": "lite",
                    }
                ]
            }
        ),
    )
    hosting.cloud.install = AsyncMock()
    row["instance"] = "test-node"
    await hosting.install(row)
    env = hosting.cloud.install.call_args.args[1]
    assert secret not in json.dumps(env) and "private-inference-key" not in json.dumps(env)
    assert (
        await client.post(
            "/api/admin-keys", headers=headers, json={"provider": "openai", "key": ""}
        )
    ).status_code == 200
    assert (await client.get("/api/me", headers=headers)).json()["admin_keys"] == {}
    assert (
        hosting.store.unseal(await hosting.store.user(row["id"]))["providers"]["openai"]
        == "private-inference-key"
    )


async def test_pause_archives_private_paginated_history_without_waking_machine(hosted, monkeypatch):
    import base64
    from urllib.parse import urlsplit
    from uuid import uuid4

    from aiohttp import ClientSession

    from harness.hosting.analytics_bridge import AnalyticsBridge

    hosting, client = hosted
    headers = {"X-Swarm-Session": await login(client, "owner-code")}
    owner = (await client.get("/api/me", headers=headers)).json()
    agent = str(uuid4())
    archive = {
        "data": {
            "workspace": {"status": "active"},
            "totals": {"working": 1, "output_tokens": 123},
            "agents": [{"id": agent, "activity": "working", "working": True}],
        },
        "histories": {
            agent: {
                "agent": {"working": True},
                "current": [{"summary": "experiment"}],
                "events": [{"sequence": i, "summary": "private finding"} for i in range(65, 0, -1)],
                "next_cursor": None,
            }
        },
        "avatars": {agent: base64.b64encode(b"portrait").decode()},
    }
    native = web.Application()

    async def export(request):
        return web.json_response(archive)

    native.router.add_get("/api/archive", export)
    runner = web.AppRunner(native)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    try:
        async with ClientSession() as upstream:
            monkeypatch.setattr(
                hosting.analytics,
                "connection",
                AsyncMock(
                    return_value=(
                        upstream,
                        f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}",
                    )
                ),
            )
            hosting.analytics.billing = None
            await hosting.store.sql(
                "UPDATE users SET status='pausing',action='pause',instance='node' WHERE id=?",
                (owner["id"],),
            )
            await hosting.run_job(await hosting.store.user(owner["id"]))
        stored = await hosting.store.sql("SELECT * FROM analytics_archives")
        assert len(stored) == 1 and "private finding" not in stored[0]["vault"]
        # A fresh bridge after platform restart can serve it with no tenant connection.
        hosting.analytics = AnalyticsBridge(hosting.store, hosting.cloud, "https://broker.example")
        hosting.cloud.request.reset_mock()
        hosting.cloud.command.reset_mock()
        me = (await client.get("/api/me", headers=headers)).json()
        assert me["status"] == "sleeping" and me["analytics_available"]
        assert (
            await client.get("/api/analytics?resource=page", headers=headers)
        ).status_code == 200
        path = f"/api/analytics?resource=activity&agent={agent}"
        pages = []
        cursor = None
        while True:
            page = (
                await client.get(path + (f"&before={cursor}" if cursor else ""), headers=headers)
            ).json()
            assert page["current"] == [] and not page["agent"]["working"]
            pages.extend(e["sequence"] for e in page["events"])
            cursor = page["next_cursor"]
            if cursor is None:
                break
        assert pages == list(range(65, 0, -1))
        for suffix in ("&limit=101", "&before=-1", "&before=bad"):
            assert (await client.get(path + suffix, headers=headers)).status_code == 400
        assert (
            await client.get(path.replace(agent, str(uuid4())), headers=headers)
        ).status_code == 404
        avatar = await client.get(f"/api/analytics?resource=avatar&agent={agent}", headers=headers)
        assert avatar.content == b"portrait"
        ticket = (await client.post("/api/dashboard-connect", json={}, headers=headers)).json()[
            "url"
        ]
        async with ClientSession() as browser:
            async with browser.ws_connect(
                str(client.base_url).rstrip("/") + "/portal/analytics?" + urlsplit(ticket).query,
                origin="https://swarm.forum",
            ) as socket:
                snapshot = (await socket.receive_json())["data"]
                assert snapshot["totals"]["output_tokens"] == 123
                assert snapshot["totals"]["working"] == 0
                assert snapshot["agents"][0]["activity_label"] == "Paused"
        other_code = await hosting.store.invite("Other")
        other_headers = {"X-Swarm-Session": await login(client, other_code)}
        await hosting.store.sql(
            "UPDATE users SET status='sleeping',instance='node' WHERE name='Other'"
        )
        assert not (await client.get("/api/me", headers=other_headers)).json()[
            "analytics_available"
        ]
        assert (await client.get(path, headers=other_headers)).status_code == 409
        hosting.cloud.request.assert_not_called()
        hosting.cloud.command.assert_not_called()
        # Stop removes the previous run; a later fresh instance cannot see its archive.
        await hosting.store.sql("UPDATE users SET action='stop' WHERE id=?", (owner["id"],))
        await hosting.run_job(await hosting.store.user(owner["id"]))
        assert not await hosting.store.sql("SELECT * FROM analytics_archives")
    finally:
        await runner.cleanup()


async def test_archive_failure_does_not_prevent_pause_and_is_visible(hosted, monkeypatch):
    hosting, client = hosted
    token = await login(client, "owner-code")
    owner = (await client.get("/api/me", headers={"X-Swarm-Session": token})).json()
    await hosting.store.sql(
        "UPDATE users SET status='pausing',action='pause',instance='node' WHERE id=?",
        (owner["id"],),
    )
    monkeypatch.setattr(
        hosting.analytics, "capture", AsyncMock(side_effect=RuntimeError("offline"))
    )
    await hosting.run_job(await hosting.store.user(owner["id"]))
    user = await hosting.store.user(owner["id"])
    assert user["status"] == "sleeping" and "could not be saved" in user["error"]
    hosting.cloud.request.assert_awaited_with("POST", "instances/node/stop")
