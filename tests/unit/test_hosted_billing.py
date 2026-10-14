from decimal import Decimal
from unittest.mock import AsyncMock

import httpx
import pytest
from cryptography.fernet import Fernet

from harness.api.billing import read_provider_cost
from harness.hosting.billing import HostedBilling
from harness.hosting.store import HostingStore


@pytest.mark.parametrize(
    "provider,amount", [("openai", {"value": "1.25", "currency": "usd"}), ("anthropic", "125")]
)
async def test_shared_cost_reader_uses_provider_units(provider, amount):
    def reply(request):
        return httpx.Response(
            200, json={"data": [{"results": [{"amount": amount, "currency": "USD"}]}]}
        )

    result = await read_provider_cost(
        provider, "https://provider.test", {}, 1000, httpx.MockTransport(reply)
    )
    assert result == Decimal("1.25")


async def test_admin_billing_baselines_are_durable_cached_and_reset_scoped(tmp_path, monkeypatch):
    store = HostingStore(tmp_path / "hosting.db", Fernet.generate_key().decode())
    await store.invite("Alice", code="alice")
    await store.invite("Bob", code="bob")
    alice, bob = await store.sql("SELECT * FROM users ORDER BY name")
    for user in (alice, bob):
        vault = store.unseal(user)
        vault["providers"]["openai"] = "inference-key"
        await store.save_vault(user["id"], vault)
    read = AsyncMock(return_value=Decimal("10"))
    monkeypatch.setattr("harness.hosting.billing.read_provider_cost", read)
    billing = HostedBilling(store, {"openai": "https://provider.test"})
    workspace = {"id": "workspace", "run_ms": 1800000000000}
    missing = await billing.snapshot(alice["id"], workspace)
    assert missing["needs_admin_keys"] and missing["reported_cost_change_usd"] is None
    read.assert_not_called()
    vault = store.unseal(await store.user(alice["id"]))
    vault["admin_keys"] = {"openai": "private-billing-key"}
    await store.save_vault(alice["id"], vault)
    first = await billing.snapshot(alice["id"], workspace)
    assert first["reported_cost_change_usd"] == "0"
    assert not first["needs_admin_keys"]
    assert read.call_args.args[2] == {"Authorization": "Bearer private-billing-key"}
    await billing.snapshot(alice["id"], workspace)
    assert read.await_count == 1
    read.return_value = Decimal("12.50")
    # A new service process reuses the saved origin, rather than resetting costs to zero.
    second = await HostedBilling(store, billing.providers).snapshot(alice["id"], workspace)
    assert second["reported_cost_change_usd"] == "2.50"
    assert (await billing.snapshot(bob["id"], workspace))["needs_admin_keys"]
    reset = await billing.snapshot(alice["id"], {**workspace, "run_ms": 1800000100000})
    assert reset["reported_cost_change_usd"] == "0.00"
    assert b"private-billing-key" not in store.path.read_bytes()


async def test_unavailable_provider_never_becomes_a_zero_cost_estimate(tmp_path, monkeypatch):
    store = HostingStore(tmp_path / "hosting.db", Fernet.generate_key().decode())
    await store.invite("Alice")
    user = (await store.sql("SELECT * FROM users"))[0]
    vault = store.unseal(user)
    vault["admin_keys"] = {"openai": "private-billing-key"}
    await store.save_vault(user["id"], vault)
    monkeypatch.setattr(
        "harness.hosting.billing.read_provider_cost",
        AsyncMock(side_effect=httpx.ConnectError("offline")),
    )
    result = await HostedBilling(store, {"openai": "https://provider.test"}).snapshot(
        user["id"], {"id": "workspace", "run_ms": 1800000000000}
    )
    assert result["reported_cost_change_usd"] is None
    assert result["providers"][0]["status"] == "unavailable"
