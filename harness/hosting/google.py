"""Google authorization-code sign-in, with invite activation after authentication."""

import base64
import hashlib
import secrets
import sqlite3
import time
from typing import Any
from urllib.parse import urlencode

import httpx
import jwt
from aiohttp import web

from harness.hosting.store import HostingStore, digest

REDIRECT = "https://swarm.forum/api/google-callback"


class GoogleLogin:
    def __init__(self, store: HostingStore, client_id: str = "", client_secret: str = ""):
        self.store, self.client_id, self.client_secret = store, client_id, client_secret
        self.keys: list[dict[str, Any]] = []
        self.keys_until = 0.0

    @property
    def enabled(self) -> bool:
        return bool(self.client_id and self.client_secret)

    async def start(self, session: str) -> dict[str, str]:
        if not self.enabled:
            raise web.HTTPServiceUnavailable(text="Google sign-in has not been configured yet.")
        now = int(time.time())
        linked = (
            await self.store.sql(
                "SELECT user_id FROM sessions WHERE token_hash=? AND expires>?",
                (digest(session), now),
            )
            if session
            else []
        )
        state, browser, nonce, verifier = (secrets.token_urlsafe(32) for _ in range(4))
        flow = {"nonce": nonce, "verifier": verifier, "session": digest(session) if linked else ""}
        await self.store.sql("DELETE FROM oauth_flows WHERE expires<?", (now,))
        await self.store.sql(
            "INSERT INTO oauth_flows VALUES(?,?,?,?)",
            (
                digest(state),
                digest(browser),
                self.store.seal(flow),
                now + 600,
            ),
        )
        challenge = (
            base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
            .decode()
            .rstrip("=")
        )
        return {
            "browser_token": browser,
            "url": "https://accounts.google.com/o/oauth2/v2/auth?"
            + urlencode(
                {
                    "client_id": self.client_id,
                    "redirect_uri": REDIRECT,
                    "response_type": "code",
                    "scope": "openid email profile",
                    "state": state,
                    "nonce": nonce,
                    "code_challenge": challenge,
                    "code_challenge_method": "S256",
                    "access_type": "online",
                }
            ),
        }

    async def claims(self, code: str, flow: dict[str, Any]) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.post(
                "https://oauth2.googleapis.com/token",
                data={
                    "code": code,
                    "client_id": self.client_id,
                    "client_secret": self.client_secret,
                    "redirect_uri": REDIRECT,
                    "grant_type": "authorization_code",
                    "code_verifier": flow["verifier"],
                },
            )
            if response.is_error:
                raise web.HTTPUnauthorized(text="Google sign-in could not be completed.")
            token = response.json().get("id_token", "")
            if time.monotonic() >= self.keys_until:
                keys = await client.get("https://www.googleapis.com/oauth2/v3/certs")
                keys.raise_for_status()
                self.keys = keys.json()["keys"]
                self.keys_until = time.monotonic() + 300
        try:
            header = jwt.get_unverified_header(token)
            key = next(k for k in self.keys if k.get("kid") == header.get("kid"))
            claims: dict[str, Any] = jwt.decode(
                token,
                jwt.PyJWK.from_dict(key).key,
                algorithms=["RS256"],
                audience=self.client_id,
                issuer=["https://accounts.google.com", "accounts.google.com"],
                options={"require": ["exp", "iat", "iss", "aud", "sub", "nonce"]},
            )
            if (
                claims.get("nonce") != flow["nonce"]
                or claims.get("email_verified") is not True
                or not isinstance(claims.get("sub"), str)
                or not claims["sub"]
                or claims.get("azp", self.client_id) != self.client_id
            ):
                raise ValueError("Google identity claims rejected")
            return claims
        except (jwt.PyJWTError, StopIteration, ValueError) as error:
            raise web.HTTPUnauthorized(text="Google identity could not be verified.") from error

    async def complete(self, body: dict[str, Any]) -> str:
        if not self.enabled:
            raise web.HTTPServiceUnavailable(text="Google sign-in has not been configured yet.")
        now = int(time.time())
        rows = await self.store.sql(
            "DELETE FROM oauth_flows WHERE state_hash=? AND browser_hash=? "
            "AND expires>? RETURNING vault",
            (digest(str(body.get("state", ""))), digest(str(body.get("browser_token", ""))), now),
        )
        if not rows:
            raise web.HTTPUnauthorized(text="Google sign-in expired; please try again.")
        flow = self.store.unseal(rows[0])
        claims = await self.claims(str(body.get("code", "")), flow)
        subject = claims["sub"]
        if flow["session"]:
            users = await self.store.sql(
                "SELECT u.* FROM sessions s JOIN users u ON u.id=s.user_id "
                "WHERE s.token_hash=? AND s.expires>?",
                (flow["session"], now),
            )
            if not users:
                raise web.HTTPUnauthorized(
                    text="Sign in with your invite link again before connecting Google."
                )
            existing = await self.store.sql(
                "SELECT id FROM users WHERE google_subject=?", (subject,)
            )
            if (existing and existing[0]["id"] != users[0]["id"]) or users[0][
                "google_subject"
            ] not in {None, subject}:
                raise web.HTTPConflict(
                    text="This Google account is already connected to another workspace."
                )
            # Unique index also protects simultaneous attempts to link the same identity.
            await self.store.sql(
                "UPDATE users SET google_subject=? WHERE id=?", (subject, users[0]["id"])
            )
        else:
            users = await self.store.sql("SELECT * FROM users WHERE google_subject=?", (subject,))
            if not users:
                # No infrastructure or provider access until this identity redeems an invite.
                try:
                    await self.store.invite(
                        str(claims.get("name", "Your account")),
                        subject=subject,
                        activated=False,
                    )
                except sqlite3.IntegrityError:
                    pass  # A concurrent callback may have created the same identity.
                users = await self.store.sql(
                    "SELECT * FROM users WHERE google_subject=?", (subject,)
                )
        session = secrets.token_urlsafe(32)
        await self.store.sql(
            "INSERT INTO sessions VALUES(?,?,?)",
            (digest(session), users[0]["id"], now + 30 * 86400),
        )
        return session
