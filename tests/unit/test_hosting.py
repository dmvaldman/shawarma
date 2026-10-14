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
        "url": "https://alice-node-8066.agent37.app/?a37_token=short"
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


async def test_provider_keys_encrypted_and_only_proxy_credential_enters_vm(hosted):
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
