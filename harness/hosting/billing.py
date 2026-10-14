"""Tenant-scoped provider billing; admin credentials stay in the control-plane vault."""

import asyncio
import json
import time
from datetime import UTC, datetime
from decimal import Decimal, DecimalException
from typing import Any

import httpx

from harness.api.billing import read_provider_cost
from harness.hosting.store import HostingStore, digest

ADMIN_PROVIDERS = {"openai", "anthropic"}


def billing_headers(provider: str, key: str) -> dict[str, str]:
    return (
        {"x-api-key": key, "anthropic-version": "2023-06-01"}
        if provider == "anthropic"
        else {"Authorization": "Bearer " + key}
    )


class HostedBilling:
    def __init__(self, store: HostingStore, providers: dict[str, str]):
        self.store, self.providers = store, providers
        self.cache: dict[tuple[str, str, int, str], tuple[float, dict[str, Any]]] = {}
        self.locks: dict[str, asyncio.Lock] = {}

    async def snapshot(self, user_id: str, workspace: dict[str, Any]) -> dict[str, Any]:
        async with self.locks.setdefault(user_id, asyncio.Lock()):
            vault = self.store.unseal(await self.store.user(user_id))
            admins, inference = vault.get("admin_keys", {}), vault["providers"]
            credentials = {
                p: admins.get(p, "") for p in ADMIN_PROVIDERS if admins.get(p) or inference.get(p)
            }
            if inference.get("deepseek"):
                credentials["deepseek"] = inference["deepseek"]
            key = (
                user_id,
                workspace["id"],
                int(workspace["run_ms"]),
                digest(json.dumps(credentials, sort_keys=True)),
            )
            cached = self.cache.get(key)
            if cached and cached[0] > time.monotonic():
                return cached[1]
            start = int(
                datetime.fromtimestamp(key[2] / 1000, UTC)
                .replace(hour=0, minute=0, second=0, microsecond=0)
                .timestamp()
            )

            async def sample(provider: str, credential: str) -> dict[str, Any]:
                row: dict[str, Any] = {
                    "provider": provider,
                    "kind": "balance_change" if provider == "deepseek" else "cost_report",
                    "status": "unavailable",
                    "amount_usd": None,
                }
                if not credential:
                    row["status"] = "needs_admin_key"
                    return row
                credential_hash = digest(credential)
                try:
                    current = await read_provider_cost(
                        provider,
                        self.providers[provider],
                        billing_headers(provider, credential),
                        start - 86400 if provider == "anthropic" else start,
                    )
                    now = datetime.now(UTC).isoformat()
                    values = (user_id, key[1], key[2], provider, credential_hash)
                    await self.store.sql(
                        "INSERT INTO provider_cost_baselines VALUES(?,?,?,?,?,?,?) "
                        "ON CONFLICT DO NOTHING",
                        (*values, str(current), now),
                    )
                    baseline = (
                        await self.store.sql(
                            "SELECT amount,captured_at FROM provider_cost_baselines "
                            "WHERE user_id=? AND workspace_id=? AND run_ms=? "
                            "AND provider=? AND credential_hash=?",
                            values,
                        )
                    )[0]
                    origin = Decimal(baseline["amount"])
                    delta = origin - current if provider == "deepseek" else current - origin
                    row.update(
                        status="ok" if delta >= 0 else "adjusted",
                        amount_usd=str(delta),
                        baseline_at=baseline["captured_at"],
                        checked_at=now,
                    )
                except (
                    httpx.HTTPError,
                    TimeoutError,
                    ValueError,
                    KeyError,
                    TypeError,
                    StopIteration,
                    AttributeError,
                    DecimalException,
                ):
                    # Provider responses and credentials must never enter user-visible errors.
                    row["detail"] = "Provider billing report is temporarily unavailable."
                return row

            rows = await asyncio.gather(
                *(sample(p, credential) for p, credential in sorted(credentials.items()))
            )
            known = [
                Decimal(r["amount_usd"])
                for r in rows
                if r["kind"] == "cost_report" and r["status"] == "ok"
            ]
            result = {
                "providers": rows,
                "reported_cost_change_usd": str(sum(known, Decimal(0))) if known else None,
                "needs_admin_keys": any(r["status"] == "needs_admin_key" for r in rows),
                "complete": bool(rows)
                and all(r["status"] == "ok" and r["kind"] == "cost_report" for r in rows),
            }
            self.cache = {k: v for k, v in self.cache.items() if k[0] != user_id}
            self.cache[key] = (time.monotonic() + 60, result)
            return result
