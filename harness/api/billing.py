"""Provider billing reports and immutable reset baselines; no local rate estimates."""

import asyncio
import time
from datetime import UTC, datetime
from decimal import Decimal, DecimalException
from hashlib import sha256
from typing import Any
from uuid import UUID

import httpx

from harness.config import Settings
from harness.persistence.store import Store


async def read_provider_cost(
    name: str,
    base: str,
    headers: dict[str, str],
    start: int,
    transport: httpx.AsyncBaseTransport | None = None,
) -> Decimal:
    async with asyncio.timeout(8), httpx.AsyncClient(transport=transport, timeout=6) as c:
        if name == "deepseek":
            r = await c.get(base + "/user/balance", headers=headers)
            r.raise_for_status()
            balances = r.json()["balance_infos"]
            usd = next(x for x in balances if x["currency"] == "USD")
            amount = Decimal(usd["total_balance"])
            if not amount.is_finite():
                raise ValueError("Invalid balance")
            return amount
        endpoint = "/organization/costs" if name == "openai" else "/organizations/cost_report"
        params: dict[str, Any] = (
            {"start_time": start, "bucket_width": "1d", "limit": 180}
            if name == "openai"
            else {
                "starting_at": datetime.fromtimestamp(start, UTC).isoformat(),
                "bucket_width": "1d",
                "limit": 31,
            }
        )
        total = Decimal(0)
        seen: set[str] = set()
        while True:
            r = await c.get(base + endpoint, headers=headers, params=params)
            r.raise_for_status()
            data = r.json()
            for bucket in data["data"]:
                for row in bucket["results"]:
                    raw = row["amount"]
                    # OpenAI reports dollars; Claude reports decimal cents.
                    if name == "openai":
                        if raw["currency"].lower() != "usd":
                            raise ValueError("Non-USD cost")
                        amount = Decimal(str(raw["value"]))
                    else:
                        if row["currency"].lower() != "usd":
                            raise ValueError("Non-USD cost")
                        amount = Decimal(str(raw)) / 100
                    if not amount.is_finite():
                        raise ValueError("Invalid cost")
                    total += amount
            if not data.get("has_more"):
                return total
            cursor = data.get("next_page")
            if not cursor or cursor in seen:
                raise ValueError("Invalid billing pagination")
            seen.add(cursor)
            params["page"] = cursor


class Billing:
    def __init__(
        self, store: Store, settings: Settings, transport: httpx.AsyncBaseTransport | None = None
    ):
        self.store, self.settings, self.transport = store, settings, transport
        self.lock = asyncio.Lock()
        self.cache: dict[tuple[UUID, int], tuple[float, dict[str, Any]]] = {}

    def providers(self) -> dict[str, tuple[str, dict[str, str]]]:
        s = self.settings
        values = {}
        for name, base, key in (
            (
                "openai",
                str(s.openai_base_url),
                s.openai_admin_key.get_secret_value() or s.openai_api_key.get_secret_value(),
            ),
            (
                "anthropic",
                str(s.anthropic_base_url),
                s.anthropic_admin_key.get_secret_value() or s.anthropic_api_key.get_secret_value(),
            ),
            ("deepseek", str(s.deepseek_base_url), s.deepseek_api_key.get_secret_value()),
        ):
            if key:
                headers = (
                    {"x-api-key": key, "anthropic-version": "2023-06-01"}
                    if name == "anthropic"
                    else {"Authorization": "Bearer " + key}
                )
                values[name] = (base.rstrip("/"), headers)
        return values

    async def read(self, name: str, base: str, headers: dict[str, str], start: int) -> Decimal:
        return await read_provider_cost(name, base, headers, start, self.transport)

    async def snapshot(self, workspace: UUID, run_ms: int) -> dict[str, Any]:
        key = (workspace, run_ms)
        async with self.lock:
            cached = self.cache.get(key)
            if cached and cached[0] > time.monotonic():
                return cached[1]
            # Subtract cumulative reported costs over a fixed period across snapshots.
            origin = datetime.fromtimestamp(run_ms / 1000, UTC).replace(
                hour=0, minute=0, second=0, microsecond=0
            )
            start = int(origin.timestamp())

            async def sample(name: str, base: str, headers: dict[str, str]) -> dict[str, Any]:
                digest = sha256((base + str(sorted(headers.items()))).encode()).hexdigest()
                result: dict[str, Any] = {
                    "provider": name,
                    "kind": "balance_change" if name == "deepseek" else "cost_report",
                    "status": "unavailable",
                    "amount_usd": None,
                }
                try:
                    # Claude rejects current-day-only ranges until a daily bucket closes.
                    # Include the preceding day consistently; baseline subtraction removes
                    # already reported costs, while the displayed lag caveat still applies.
                    period_start = start - 86400 if name == "anthropic" else start
                    current = await self.read(name, base, headers, period_start)
                    await self.store.pool.execute(
                        "INSERT INTO billing_baselines(workspace_id,run_ms,provider,"
                        "credential_hash,"
                        "kind,period_start,amount) VALUES($1,$2,$3,$4,$5,$6,$7) "
                        "ON CONFLICT DO NOTHING",
                        workspace,
                        run_ms,
                        name,
                        digest,
                        result["kind"],
                        period_start,
                        current,
                    )
                    baseline = await self.store.pool.fetchrow(
                        "SELECT amount,captured_at FROM billing_baselines "
                        "WHERE workspace_id=$1 AND run_ms=$2 AND provider=$3 "
                        "AND credential_hash=$4",
                        workspace,
                        run_ms,
                        name,
                        digest,
                    )
                    assert baseline is not None
                    delta = (
                        baseline["amount"] - current
                        if name == "deepseek"
                        else current - baseline["amount"]
                    )
                    result.update(
                        status="ok",
                        amount_usd=str(delta),
                        baseline_at=baseline["captured_at"].isoformat(),
                        checked_at=datetime.now(UTC).isoformat(),
                    )
                    if delta < 0:
                        result["status"] = "adjusted"
                    result["detail"] = (
                        "Balance change; top-ups, refunds and credit expiry affect it."
                        if name == "deepseek"
                        else "Account cost change since baseline; reporting lag can cross resets."
                    )
                except httpx.HTTPStatusError as error:
                    status = error.response.status_code
                    result["detail"] = (
                        f"HTTP {status}: denied. Set {name.upper()}_ADMIN_KEY in .env."
                        if status in {401, 403} and name != "deepseek"
                        else f"Billing API HTTP {status}."
                    )
                except (
                    httpx.HTTPError,
                    TimeoutError,
                    ValueError,
                    KeyError,
                    StopIteration,
                    TypeError,
                    AttributeError,
                    DecimalException,
                ):
                    result["detail"] = (
                        "Provider billing report unavailable; no estimate substituted."
                    )
                return result

            rows = await asyncio.gather(
                *(sample(name, *config) for name, config in self.providers().items())
            )
            costs = [r for r in rows if r["kind"] == "cost_report" and r["status"] == "ok"]
            data = {
                "providers": rows,
                "reported_cost_change_usd": str(
                    sum((Decimal(r["amount_usd"]) for r in costs), Decimal(0))
                )
                if costs
                else None,
                "complete": bool(rows)
                and all(r["status"] == "ok" and r["kind"] == "cost_report" for r in rows),
                "checked_at": datetime.now(UTC).isoformat(),
            }
            self.cache = {key: (time.monotonic() + 60, data)}
            return data
