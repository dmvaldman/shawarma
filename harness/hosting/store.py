import asyncio
import json
import secrets
import sqlite3
import time
from hashlib import sha256
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet


def digest(value: str) -> str:
    return sha256(value.encode()).hexdigest()


class HostingStore:
    def __init__(self, path: Path, key: str):
        self.path, self.cipher = path, Fernet(key.encode())
        path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(path) as conn:
            conn.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS users (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL, role TEXT NOT NULL DEFAULT 'user',
                    login_hash TEXT UNIQUE NOT NULL, vault TEXT NOT NULL,
                    agent_token_hash TEXT UNIQUE NOT NULL, instance TEXT,
                    status TEXT NOT NULL DEFAULT 'new', stage TEXT NOT NULL DEFAULT '',
                    action TEXT, creating INTEGER NOT NULL DEFAULT 0,
                    run_until INTEGER, error TEXT, created_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS sessions (
                    token_hash TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id),
                    expires INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS service_tickets (
                    token_hash TEXT PRIMARY KEY, user_id TEXT NOT NULL,
                    session_hash TEXT NOT NULL, service TEXT NOT NULL, expires INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS service_sessions (
                    token_hash TEXT PRIMARY KEY, user_id TEXT NOT NULL,
                    session_hash TEXT NOT NULL, service TEXT NOT NULL, expires INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS provider_cost_baselines (
                    user_id TEXT NOT NULL, workspace_id TEXT NOT NULL, run_ms INTEGER NOT NULL,
                    provider TEXT NOT NULL, credential_hash TEXT NOT NULL, amount TEXT NOT NULL,
                    captured_at TEXT NOT NULL,
                    PRIMARY KEY(user_id,workspace_id,run_ms,provider,credential_hash)
                );
                CREATE TABLE IF NOT EXISTS analytics_archives (
                    user_id TEXT PRIMARY KEY, instance TEXT NOT NULL,
                    vault TEXT NOT NULL, captured_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS oauth_flows (
                    state_hash TEXT PRIMARY KEY, browser_hash TEXT NOT NULL,
                    vault TEXT NOT NULL, expires INTEGER NOT NULL
                );
            """)
            columns = {row[1] for row in conn.execute("PRAGMA table_info(users)")}
            if "google_subject" not in columns:
                conn.execute("ALTER TABLE users ADD COLUMN google_subject TEXT")
            if "activated" not in columns:
                conn.execute("ALTER TABLE users ADD COLUMN activated INTEGER NOT NULL DEFAULT 1")
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS google_subject ON users(google_subject)"
            )
        path.chmod(0o600)

    def seal(self, data: dict[str, Any]) -> str:
        return self.cipher.encrypt(json.dumps(data).encode()).decode()

    def unseal(self, row: dict[str, Any]) -> dict[str, Any]:
        result: dict[str, Any] = json.loads(self.cipher.decrypt(row["vault"].encode()))
        return result

    async def sql(self, query: str, args: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        def execute() -> list[dict[str, Any]]:
            with sqlite3.connect(self.path, timeout=20) as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.execute(query, args)
                return [dict(r) for r in cursor.fetchall()] if cursor.description else []

        return await asyncio.to_thread(execute)

    async def invite(
        self,
        name: str,
        role: str = "user",
        code: str | None = None,
        *,
        subject: str | None = None,
        activated: bool = True,
    ) -> str:
        code = code or secrets.token_urlsafe(32)
        agent_token = secrets.token_urlsafe(32)
        await self.sql(
            "INSERT INTO users(id,name,role,login_hash,vault,agent_token_hash,created_at,"
            "google_subject,activated) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (
                secrets.token_hex(12),
                name[:60],
                role,
                digest(code),
                self.seal(
                    {
                        "providers": {},
                        "agent_token": agent_token,
                        "mattermost_password": secrets.token_urlsafe(32),
                    }
                ),
                digest(agent_token),
                int(time.time()),
                subject,
                int(activated),
            ),
        )
        return code

    async def user(self, user_id: str) -> dict[str, Any]:
        rows = await self.sql("SELECT * FROM users WHERE id=?", (user_id,))
        if not rows:
            raise KeyError(user_id)
        return rows[0]

    async def save_vault(self, user_id: str, vault: dict[str, Any]) -> None:
        await self.sql("UPDATE users SET vault=? WHERE id=?", (self.seal(vault), user_id))

    async def redeem(self, user_id: str, code: str) -> None:
        """Bind a pending Google identity to its invited workspace atomically."""

        def execute() -> None:
            with sqlite3.connect(self.path, timeout=20) as conn:
                conn.row_factory = sqlite3.Row
                conn.execute("BEGIN IMMEDIATE")
                source = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
                target = conn.execute(
                    "SELECT * FROM users WHERE login_hash=? AND activated=1", (digest(code),)
                ).fetchone()
                if not source or source["activated"] or not source["google_subject"]:
                    raise ValueError("This account already has access.")
                if not target or target["google_subject"]:
                    raise ValueError("Invalid or already claimed invite code.")
                # Move authenticated sessions; preserve the invited workspace's ID and vault.
                conn.execute("UPDATE users SET google_subject=NULL WHERE id=?", (user_id,))
                conn.execute(
                    "UPDATE users SET google_subject=? WHERE id=?",
                    (source["google_subject"], target["id"]),
                )
                conn.execute(
                    "UPDATE sessions SET user_id=? WHERE user_id=?", (target["id"], user_id)
                )
                conn.execute("DELETE FROM users WHERE id=?", (user_id,))

        await asyncio.to_thread(execute)
