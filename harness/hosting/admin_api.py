"""Admin metadata and explicit, expiring, audited read-only support access."""

import asyncio
import base64
import json
import secrets
import shlex
import time
from pathlib import Path
from typing import Any

from aiohttp import web

from harness.hosting.admin import AdminUsage
from harness.hosting.store import digest

KINDS = {"channels", "messages", "repositories", "issues", "pulls", "files"}


async def handle(usage: AdminUsage, request: web.Request, user: dict[str, Any]) -> web.Response:
    store = usage.store
    route = request.match_info["route"]
    now = int(time.time())
    if route == "support-access":
        if request.method == "POST":
            body = await request.json()
            grant = body.get("grant")
            if not isinstance(grant, bool):
                raise web.HTTPBadRequest(text="Choose whether to grant support access.")
            if grant:
                if not user["instance"]:
                    raise web.HTTPConflict(text="Start your swarm before granting support access.")
                await store.sql(
                    "INSERT INTO support_grants VALUES(?,?) ON CONFLICT(user_id) DO UPDATE SET "
                    "expires=excluded.expires",
                    (user["id"], now + 3600),
                )
            else:
                await store.sql("DELETE FROM support_grants WHERE user_id=?", (user["id"],))
            await store.sql(
                "INSERT INTO support_audit(user_id,actor_id,action,at) VALUES(?,?,?,?)",
                (user["id"], user["id"], "granted" if grant else "revoked", now),
            )
        grant = await store.sql(
            "SELECT expires FROM support_grants WHERE user_id=? AND expires>?", (user["id"], now)
        )
        audit = await store.sql(
            "SELECT action,at FROM support_audit WHERE user_id=? ORDER BY id DESC LIMIT 30",
            (user["id"],),
        )
        return web.json_response(
            {"expires": grant[0]["expires"] if grant else None, "audit": audit}
        )
    if user["role"] != "admin":
        raise web.HTTPForbidden()
    if route == "admin-usage" and request.method == "GET":
        return web.json_response(await usage.report())
    if route == "admin-invite" and request.method == "POST":
        body = await request.json()
        target = await store.sql(
            "SELECT * FROM users WHERE id=? AND activated=1", (str(body.get("user_id", "")),)
        )
        if not target or target[0]["role"] == "admin" or target[0]["google_subject"]:
            raise web.HTTPConflict(text="Only unclaimed friend invitations can be changed.")
        action = body.get("action")
        if action not in {"regenerate", "revoke"}:
            raise web.HTTPBadRequest()
        code = "".join(secrets.choice("abcdefghjkmnpqrstuvwxyz23456789") for _ in range(10))
        changed = await store.sql(
            "UPDATE users SET login_hash=?,invite_revoked=? WHERE id=? AND google_subject "
            "IS NULL RETURNING id",
            (digest(code), int(action == "revoke"), target[0]["id"]),
        )
        if not changed:
            raise web.HTTPConflict(text="This invitation has already been claimed.")
        await store.sql("DELETE FROM sessions WHERE user_id=?", (target[0]["id"],))
        return web.json_response({"code": code if action == "regenerate" else None})
    if route == "admin-content" and request.method == "POST":
        body = await request.json()
        kind = body.get("kind")
        if kind not in KINDS:
            raise web.HTTPBadRequest()
        target = await store.sql(
            "SELECT u.* FROM users u JOIN support_grants g ON u.id=g.user_id WHERE u.id=? "
            "AND g.expires>? AND u.status='running'",
            (str(body.get("user_id", "")), now),
        )
        if not target:
            raise web.HTTPForbidden(
                text="The user must grant active support access and have a running swarm."
            )
        account = target[0]
        await store.sql(
            "INSERT INTO support_audit(user_id,actor_id,action,at) VALUES(?,?,?,?)",
            (account["id"], user["id"], "read_" + str(kind), now),
        )
        safe = {k: body[k] for k in ("kind", "page", "channel", "repository", "path") if k in body}
        source = await asyncio.to_thread(Path(__file__).with_name("support.py").read_bytes)
        encoded = base64.b64encode(source).decode()
        code = (
            "import base64;exec(compile(base64.b64decode("
            + repr(encoded)
            + '),"support.py","exec"),{"__name__":"__main__"})'
        )
        try:
            raw = await usage.cloud.command(
                account["instance"],
                "docker exec swarm-controller python -c "
                + shlex.quote(code)
                + " "
                + shlex.quote(json.dumps(safe)),
            )
            data = json.loads(raw)
        except Exception:
            raise web.HTTPBadGateway(text="Support view is temporarily unavailable.") from None
        # Expiry/revocation during the read must not release content to the administrator.
        if not await store.sql(
            "SELECT 1 FROM support_grants WHERE user_id=? AND expires>?",
            (account["id"], int(time.time())),
        ):
            raise web.HTTPForbidden(text="Support access expired or was revoked.")
        vault = store.unseal(account)
        secrets_to_hide = [
            vault.get("agent_token"),
            vault.get("mattermost_password"),
            *vault.get("providers", {}).values(),
            *vault.get("admin_keys", {}).values(),
        ]

        def redact(value: Any) -> Any:
            if isinstance(value, str):
                for secret in secrets_to_hide:
                    if isinstance(secret, str) and len(secret) >= 8:
                        value = value.replace(secret, "[redacted]")
                return value
            if isinstance(value, list):
                return [redact(v) for v in value]
            if isinstance(value, dict):
                return {k: redact(v) for k, v in value.items()}
            return value

        return web.json_response(redact(data), headers={"Cache-Control": "no-store"})
    raise web.HTTPNotFound()
