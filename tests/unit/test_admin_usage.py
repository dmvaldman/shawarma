import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from cryptography.fernet import Fernet

from harness.hosting.admin import AdminUsage
from harness.hosting.store import HostingStore


@pytest.fixture
async def usage(tmp_path):
    store = HostingStore(tmp_path / "hosting.db", Fernet.generate_key().decode())
    await store.invite("Alice", code="invite")
    user = (await store.sql("SELECT * FROM users"))[0]
    await store.sql("UPDATE users SET status='running',instance='node' WHERE id=?", (user["id"],))
    return AdminUsage(store, SimpleNamespace(command=AsyncMock())), await store.user(user["id"])


def sample(epoch="workspace:1", count=10, **extra):
    return {
        "epoch": epoch,
        "at": int(time.time()),
        "agents": 3,
        "blocked": 1,
        "models": ["openai:test"],
        "counters": {
            "input_tokens": count * 10,
            "output_tokens": count,
            "messages": count,
            "issues": count,
            "agents_created": count,
        },
        **extra,
    }


async def test_run_deltas_are_idempotent_across_resume_reset_and_outages(usage):
    ledger, user = usage
    await ledger.ingest(user, sample())
    await ledger.ingest(user, sample())
    first = (await ledger.report())["runs"][0]
    assert first["totals"]["messages"] == 10 and first["peak_agents"] == 3
    assert first["started_at"] is None and first["duration_seconds"] is None and first["imported"]
    await ledger.finish(user, "paused")
    await ledger.begin(user)
    await ledger.ingest(user, sample(count=15))
    current = (await ledger.report())["runs"][0]
    # If requests share a second, insertion order need not equal requested_at order.
    current = next(r for r in (await ledger.report())["runs"] if not r["ended_at"])
    assert current["totals"]["messages"] == 5
    await ledger.ingest(user, sample("workspace:2", count=4))
    await ledger.ingest(user, {"epoch": "workspace:2", "counters": {"messages": None}})
    current = next(r for r in (await ledger.report())["runs"] if not r["ended_at"])
    assert current["totals"]["messages"] == 9 and current["totals"]["issues"] == 9
    assert current["totals"]["human_messages"] is None
    # Metadata survives a control-plane process restart.
    restored = AdminUsage(ledger.store, ledger.cloud)
    assert (
        next(r for r in (await restored.report())["runs"] if not r["ended_at"])["totals"]
        == current["totals"]
    )


async def test_metadata_is_allowlisted_and_reset_checkpoints_are_idempotent(usage):
    ledger, user = usage
    data = sample(
        private_prompt="SECRET HUMAN QUESTION",
        channels=["private channel"],
        api_key="sk-secret",
        reset_id="reset-1",
    )
    await ledger.checkpoint(user, data)
    await ledger.checkpoint(user, data)
    await ledger.ingest(user, sample("workspace:2", count=2))
    report = await ledger.report()
    text = json.dumps(report)
    assert "SECRET HUMAN" not in text and "sk-secret" not in text and "private channel" not in text
    assert report["runs"][0]["totals"]["messages"] == 12
    assert len(await ledger.store.sql("SELECT * FROM telemetry_receipts")) == 1


async def test_never_wakes_a_paused_machine_for_sampling(usage):
    ledger, user = usage
    await ledger.store.sql("UPDATE users SET status='sleeping' WHERE id=?", (user["id"],))
    await ledger.sample(user)
    ledger.cloud.command.assert_not_called()


async def test_saved_pause_import_never_exports_contents_or_wakes_tenant(usage):
    ledger, user = usage
    await ledger.store.sql("UPDATE users SET status='sleeping' WHERE id=?", (user["id"],))
    archive = {
        "data": {
            "workspace": {"id": "workspace", "run_ms": 1000},
            "totals": {
                "agents": 1,
                "agents_created": 2,
                "input_tokens": 100,
                "output_tokens": 20,
                "messages": 5,
            },
            "agents": [{"model_id": "openai:test", "state": "IDLE"}],
        },
        "histories": {"secret": "PRIVATE HUMAN MESSAGE"},
    }
    await ledger.store.sql(
        "INSERT INTO analytics_archives VALUES(?,?,?,?)",
        (user["id"], "node", ledger.store.seal(archive), int(time.time())),
    )
    await ledger.import_saved()
    await ledger.import_saved()
    report = await ledger.report()
    assert len(report["runs"]) == 1
    assert report["runs"][0]["totals"]["output_tokens"] == 20
    assert report["runs"][0]["totals"]["issues"] is None
    assert "PRIVATE HUMAN" not in json.dumps(report)
    ledger.cloud.command.assert_not_called()
