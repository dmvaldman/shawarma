import os
from datetime import UTC, datetime
from decimal import Decimal

import httpx
import pytest

from harness.api.billing import Billing

SNAPSHOT = Billing.snapshot
pytestmark = pytest.mark.skipif(os.environ.get("HARNESS_LIVE_TESTS") != "1", reason="live gate")


async def test_baseline_delta_units_pagination_and_reset(runtime_store, monkeypatch):
    store, settings, _, workspace = runtime_store
    monkeypatch.setattr(Billing, "snapshot", SNAPSHOT)
    settings = settings.model_copy(
        update={
            "openai_admin_key": settings.openai_api_key,
            "anthropic_admin_key": settings.anthropic_api_key,
        }
    )
    values = {"openai": Decimal("10"), "anthropic": Decimal("2000"), "deepseek": Decimal("8")}
    calls = []

    def response(request):
        calls.append(str(request.url))
        if request.url.host == "api.deepseek.com":
            return httpx.Response(
                200,
                json={
                    "balance_infos": [{"currency": "USD", "total_balance": str(values["deepseek"])}]
                },
            )
        name = "openai" if request.url.host == "api.openai.com" else "anthropic"
        amount = (
            {"currency": "usd", "value": str(values[name])}
            if name == "openai"
            else str(values[name])
        )
        # Two pages; second page has no values, verifies cursor traversal and no double counts.
        page2 = request.url.params.get("page") == "next"
        return httpx.Response(
            200,
            json={
                "data": [] if page2 else [{"results": [{"amount": amount, "currency": "USD"}]}],
                "has_more": not page2,
                "next_page": None if page2 else "next",
            },
        )

    billing = Billing(store, settings, httpx.MockTransport(response))
    first = await billing.snapshot(workspace, 1000)
    assert all(Decimal(p["amount_usd"]) == 0 for p in first["providers"])
    assert len(calls) == 5
    await billing.snapshot(workspace, 1000)
    assert len(calls) == 5
    values.update(openai=Decimal("11"), anthropic=Decimal("2150"), deepseek=Decimal("7.25"))
    # Reconstructing the service retains durable baselines across restarts.
    data = await Billing(store, settings, httpx.MockTransport(response)).snapshot(workspace, 1000)
    amounts = {p["provider"]: Decimal(p["amount_usd"]) for p in data["providers"]}
    assert amounts == {
        "openai": Decimal("1"),
        "anthropic": Decimal("1.5"),
        "deepseek": Decimal(".75"),
    }
    assert Decimal(data["reported_cost_change_usd"]) == Decimal("2.5")
    assert not data["complete"]  # Balance movement is not a billing cost report.
    reset = await Billing(store, settings, httpx.MockTransport(response)).snapshot(workspace, 2000)
    assert all(Decimal(p["amount_usd"]) == 0 for p in reset["providers"])
    assert (
        await store.pool.fetchval(
            "SELECT count(*) FROM billing_baselines WHERE workspace_id=$1", workspace
        )
        == 6
    )


async def test_denied_access_is_unavailable_not_zero_or_estimate(runtime_store, monkeypatch):
    store, settings, _, workspace = runtime_store
    monkeypatch.setattr(Billing, "snapshot", SNAPSHOT)

    def response(request):
        return httpx.Response(403, json={"error": {"message": "secret provider message"}})

    data = await Billing(store, settings, httpx.MockTransport(response)).snapshot(workspace, 1000)
    assert data["reported_cost_change_usd"] is None
    assert not data["complete"]
    assert all(p["amount_usd"] is None for p in data["providers"])
    assert "secret provider message" not in str(data)
    assert (
        await store.pool.fetchval(
            "SELECT count(*) FROM billing_baselines WHERE workspace_id=$1", workspace
        )
        == 0
    )


async def test_anthropic_current_run_includes_a_completed_daily_bucket(runtime_store, monkeypatch):
    store, settings, _, workspace = runtime_store
    monkeypatch.setattr(Billing, "snapshot", SNAPSHOT)
    today = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    queried = []

    def response(request):
        if request.url.host != "api.anthropic.com":
            return httpx.Response(403)
        start = datetime.fromisoformat(request.url.params["starting_at"])
        queried.append(start)
        if start >= today:
            return httpx.Response(
                400,
                json={
                    "error": {
                        "message": "Invalid date range: ending date must be after starting date"
                    }
                },
            )
        return httpx.Response(200, json={"data": [], "has_more": False})

    data = await Billing(store, settings, httpx.MockTransport(response)).snapshot(
        workspace, int(today.timestamp() * 1000)
    )
    result = next(p for p in data["providers"] if p["provider"] == "anthropic")
    assert result["status"] == "ok"
    assert Decimal(result["amount_usd"]) == 0
    assert len(queried) == 1 and (today - queried[0]).total_seconds() == 86400
