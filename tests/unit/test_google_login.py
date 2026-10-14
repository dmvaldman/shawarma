import time
from urllib.parse import parse_qs, urlsplit

import httpx
import jwt
import pytest
from aiohttp import web
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives.asymmetric import rsa

from harness.hosting.google import REDIRECT, GoogleLogin
from harness.hosting.store import HostingStore, digest


@pytest.fixture
async def google(tmp_path):
    store = HostingStore(tmp_path / "users.db", Fernet.generate_key().decode())
    await store.invite("Owner", "admin", "invited-owner")
    owner = (await store.sql("SELECT id FROM users"))[0]["id"]
    await store.sql(
        "INSERT INTO sessions VALUES(?,?,?)",
        (digest("owner-session"), owner, int(time.time()) + 3600),
    )
    return GoogleLogin(store, "test-client", "test-secret"), owner


async def callback_input(login, session):
    result = await login.start(session)
    query = parse_qs(urlsplit(result["url"]).query)
    assert query["redirect_uri"] == [REDIRECT]
    assert query["code_challenge_method"] == ["S256"]
    assert query["scope"] == ["openid email profile"]
    return {
        "state": query["state"][0],
        "browser_token": result["browser_token"],
        "code": "google-code",
    }


async def test_google_requires_invite_then_links_stable_subject_and_retains_invite(
    google, monkeypatch
):
    login, owner = google

    async def claims(code, flow):
        assert code == "google-code" and flow["nonce"] and flow["verifier"]
        return {"sub": "stable-google-subject", "email_verified": True}

    monkeypatch.setattr(login, "claims", claims)
    public = await callback_input(login, "")
    with pytest.raises(web.HTTPForbidden):
        await login.complete(public)
    linked = await callback_input(login, "owner-session")
    with pytest.raises(web.HTTPUnauthorized):
        await login.complete({**linked, "browser_token": "wrong-browser"})
    token = await login.complete(linked)
    assert (await login.store.user(owner))["google_subject"] == "stable-google-subject"
    assert (await login.store.user(owner))["login_hash"] == digest("invited-owner")
    assert (
        await login.store.sql("SELECT user_id FROM sessions WHERE token_hash=?", (digest(token),))
    )[0]["user_id"] == owner
    with pytest.raises(web.HTTPUnauthorized):
        await login.complete(linked)
    returning = await login.complete(await callback_input(login, ""))
    assert (
        await login.store.sql(
            "SELECT user_id FROM sessions WHERE token_hash=?", (digest(returning),)
        )
    )[0]["user_id"] == owner


async def test_linking_requires_still_valid_invite_session(google, monkeypatch):
    login, _ = google

    async def claims(code, flow):
        return {"sub": "google-sub", "email_verified": True}

    monkeypatch.setattr(login, "claims", claims)
    flow = await callback_input(login, "owner-session")
    await login.store.sql("DELETE FROM sessions")
    with pytest.raises(web.HTTPUnauthorized):
        await login.complete(flow)
    assert not (await login.store.sql("SELECT google_subject FROM users"))[0]["google_subject"]


@pytest.mark.parametrize("invalid", [None, "nonce", "aud", "email_verified", "iss", "exp"])
async def test_google_verifies_signature_audience_nonce_and_verified_identity(
    google, monkeypatch, invalid
):
    login, _ = google
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key(), as_dict=True)
    jwk["kid"] = "test-key"
    claims = {
        "sub": "stable-sub",
        "iss": "https://accounts.google.com",
        "aud": "test-client",
        "iat": int(time.time()),
        "exp": int(time.time()) + 300,
        "nonce": "expected-nonce",
        "email_verified": True,
    }
    if invalid:
        claims[invalid] = {"email_verified": False, "exp": int(time.time()) - 100}.get(
            invalid, "wrong"
        )
    token = jwt.encode(claims, key, algorithm="RS256", headers={"kid": "test-key"})
    original = httpx.AsyncClient

    def provider(request):
        if request.url.path == "/token":
            body = parse_qs(request.content.decode())
            assert body["code_verifier"] == ["private-verifier"]
            assert body["client_secret"] == ["test-secret"]
            return httpx.Response(200, json={"id_token": token})
        return httpx.Response(200, json={"keys": [jwk]})

    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: original(**{**kwargs, "transport": httpx.MockTransport(provider)}),
    )
    if invalid:
        with pytest.raises(web.HTTPUnauthorized):
            await login.claims("code", {"nonce": "expected-nonce", "verifier": "private-verifier"})
    else:
        assert (
            await login.claims("code", {"nonce": "expected-nonce", "verifier": "private-verifier"})
        )["sub"] == "stable-sub"
