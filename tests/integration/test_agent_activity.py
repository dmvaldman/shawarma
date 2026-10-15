import asyncio
import json
import os
from uuid import uuid4

import asyncpg
import pytest
from aiohttp.test_utils import TestClient, TestServer
from pydantic import SecretStr

from harness.api.analytics import Analytics
from harness.core.activity import Activity
from harness.core.reset import purge

pytestmark = pytest.mark.skipif(
    os.environ.get("HARNESS_LIVE_TESTS") != "1", reason="PostgreSQL gate"
)


def output(text, ended=False):
    return [
        {
            "info": {"id": "m", "role": "assistant", "time": {"created": 1700000000000}},
            "parts": [
                {
                    "id": "r",
                    "type": "reasoning",
                    "text": text,
                    "time": {"end": 1700000001000} if ended else {},
                }
            ],
        }
    ]


async def test_append_only_milestones_live_projection_dedup_and_secrets(runtime_store):
    store, settings, agent, _ = runtime_store
    settings = settings.model_copy(update={"openai_api_key": SecretStr("secret-key")})
    capture = Activity(store, settings)
    session = {"session_id": "session"}
    await capture.native(agent, session, output("First thought"))
    await capture.native(agent, session, output("First thought, tested secret-key"))
    assert await store.pool.fetchval("SELECT count(*) FROM agent_activity") == 1
    current = json.loads(await store.pool.fetchval("SELECT payload FROM agent_activity_sources"))
    assert current["part"]["text"] == "First thought, tested [redacted]"
    await capture.native(agent, session, output("First thought, tested secret-key", True))
    await capture.native(agent, session, output("First thought, tested secret-key", True))
    assert await store.pool.fetchval("SELECT count(*) FROM agent_activity") == 2
    assert "secret-key" not in str(await store.pool.fetch("SELECT payload FROM agent_activity"))
    for command in ("DELETE FROM agent_activity", "UPDATE agent_activity SET summary='changed'"):
        with pytest.raises(asyncpg.RaiseError, match="append-only"):
            await store.pool.execute(command)
    # A new capture process must not duplicate persisted events after restart.
    await Activity(store, settings).native(
        agent, session, output("First thought, tested secret-key", True)
    )
    assert await store.pool.fetchval("SELECT count(*) FROM agent_activity") == 2


async def test_concurrent_capture_and_interruption_are_durable(runtime_store):
    store, settings, agent, _ = runtime_store
    first, second = Activity(store, settings), Activity(store, settings)
    await asyncio.gather(
        first.native(agent, {"session_id": "s"}, output("Working")),
        second.native(agent, {"session_id": "s"}, output("Working")),
    )
    assert await store.pool.fetchval("SELECT count(*) FROM agent_activity") == 1
    await first.interrupt(agent)
    assert await store.pool.fetchval("SELECT phase FROM agent_activity_sources") == "interrupted"
    assert await store.pool.fetchval("SELECT count(*) FROM agent_activity") == 2


async def test_activity_pagination_scope_current_and_controls(runtime_store):
    store, settings, agent, workspace = runtime_store
    capture = Activity(store, settings)
    await capture.record_many(
        agent,
        [
            (
                str(i),
                "text",
                "completed",
                "Message " + str(i),
                {"text": "<script>untrusted text</script>"},
                None,
            )
            for i in range(8)
        ],
    )
    dashboard = Analytics(store, settings, workspace)
    async with TestClient(TestServer(dashboard.app())) as client:
        url = f"/api/agents/{agent}/activity"
        page = await (await client.get(url + "?limit=3")).json()
        ids = [e["sequence"] for e in page["events"]]
        assert len(ids) == 3 and ids == sorted(ids, reverse=True)
        await capture.record(
            agent, "new", "tool", "running", "bash", {"command": "python check.py"}
        )
        older = await (await client.get(url + "?limit=3&before=" + str(page["next_cursor"]))).json()
        assert not set(ids).intersection(e["sequence"] for e in older["events"])
        assert older["next_cursor"]
        await store.pool.execute("UPDATE workspaces SET started=true WHERE id=$1", workspace)
        await store.set_state(agent, "RUNNING")
        page = await (await client.get(url)).json()
        assert len(page["current"]) == 1 and page["current"][0]["summary"] == "bash"
        await store.pool.execute("UPDATE agents SET control_paused=true WHERE id=$1", agent)
        assert not (await (await client.get(url)).json())["current"]
        for suffix in ("?limit=0", "?limit=101", "?before=-1", "?before=bad"):
            assert (await client.get(url + suffix)).status == 400
        assert (await client.get(f"/api/agents/{uuid4()}/activity")).status == 404
        assert (await client.get(url, headers={"Origin": "http://evil.example"})).status == 403
        other_dashboard = Analytics(store, settings, uuid4())
        async with TestClient(TestServer(other_dashboard.app())) as other:
            assert (await other.get(url)).status == 404


async def test_reset_erases_activity_and_restores_append_only_guard(runtime_store):
    store, settings, agent, workspace = runtime_store
    await Activity(store, settings).record(
        agent, "one", "text", "completed", "Message", {"text": "old"}
    )
    await purge(
        store,
        workspace,
        {
            "binding": {"server_id": settings.mattermost_server_id},
            "reset": {
                "manifest": {"agents": [str(agent)]},
                "seed_id": str(uuid4()),
                "fresh": {"general_id": "general"},
                "created_ms": 1000,
            },
        },
    )
    assert await store.pool.fetchval("SELECT count(*) FROM agent_activity") == 0
    assert await store.pool.fetchval("SELECT count(*) FROM agent_activity_sources") == 0
    assert (
        await store.pool.fetchval(
            "SELECT tgenabled FROM pg_trigger WHERE tgname='activity_append_only' "
            "AND tgrelid='agent_activity'::regclass"
        )
        == b"O"
    )


async def test_late_backfill_cannot_regress_live_or_completed_reasoning(runtime_store):
    store, settings, agent, _ = runtime_store
    live, stale = Activity(store, settings), Activity(store, settings)
    session = {"session_id": "s"}
    await live.native(agent, session, output("Long current thought"))
    await stale.native(agent, session, output("Long"))
    payload = json.loads(await store.pool.fetchval("SELECT payload FROM agent_activity_sources"))
    assert payload["part"]["text"] == "Long current thought"
    await live.native(agent, session, output("Long current thought", True))
    await Activity(store, settings).native(agent, session, output("Old unfinished thought"))
    assert await store.pool.fetchval("SELECT phase FROM agent_activity_sources") == "completed"
    assert await store.pool.fetchval("SELECT count(*) FROM agent_activity") == 2


async def test_opaque_reasoning_metadata_is_redacted_without_discarding_summary(runtime_store):
    store, settings, agent, _ = runtime_store
    capture = Activity(store, settings)
    messages = output("Readable provider summary", True)
    messages[0]["info"]["providerID"] = "openai"
    messages[0]["parts"][0]["metadata"] = {"openai": {"reasoningEncryptedContent": "opaque-blob"}}
    await capture.native(agent, {"session_id": "s"}, messages)
    payload = json.loads(await store.pool.fetchval("SELECT payload FROM agent_activity"))
    assert payload["part"]["text"] == "Readable provider summary"
    assert payload["part"]["metadata"]["openai"]["reasoningEncryptedContent"] == "[redacted]"
    assert await store.pool.fetchval("SELECT summary FROM agent_activity") == "Reasoning summary"


async def test_pause_archive_exports_all_pages_with_same_redaction(runtime_store, monkeypatch):
    from unittest.mock import AsyncMock

    store, settings, agent, workspace = runtime_store
    settings = settings.model_copy(update={"openai_api_key": SecretStr("archive-secret")})
    capture = Activity(store, settings)
    for i in range(105):
        await capture.native(agent, {"session_id": str(i)}, output("Finding archive-secret", True))
    api = Analytics(store, settings, workspace)
    data = {"agents": [{"id": str(agent)}], "workspace": {}, "totals": {}}
    monkeypatch.setattr(api, "snapshot", AsyncMock(return_value=data))
    async with TestClient(TestServer(api.app())) as client:
        response = await client.get("/api/archive")
        assert response.status == 200
        archive = await response.json()
        events = archive["histories"][str(agent)]["events"]
        assert len(events) == 105
        assert len({event["sequence"] for event in events}) == 105
        assert "archive-secret" not in json.dumps(archive)
        assert "[redacted]" in json.dumps(archive)
