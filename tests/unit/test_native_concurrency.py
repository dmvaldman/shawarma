"""Full-roster activation must not queue behind legacy coding/inference gates."""

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

from harness.coding.runtime import NativeRuntime
from harness.coding.service import Coding
from harness.config import Settings


async def test_entire_roster_runs_with_legacy_inference_gate_exhausted(monkeypatch):
    settings = Settings(
        _env_file=None,
        coding_enabled=True,
        opencode_password="test",
        max_active_agents=100,
        mattermost_url="http://mattermost.invalid",
        database_url="postgresql://unused",
    )
    connections = []

    @asynccontextmanager
    async def acquire():
        conn = SimpleNamespace(fetchval=AsyncMock(return_value=True), execute=AsyncMock())
        connections.append(conn)
        yield conn

    store = SimpleNamespace(
        pool=SimpleNamespace(acquire=acquire),
        set_state=AsyncMock(),
        state=AsyncMock(return_value={"state": "RUNNING"}),
        record_failure=AsyncMock(),
    )
    client = SimpleNamespace(close=AsyncMock())
    monkeypatch.setattr("harness.coding.service.OpenCode", lambda _: client)
    coding = Coding(store, settings)
    coding.allowed = AsyncMock()
    started = set()
    all_started = asyncio.Event()
    release = asyncio.Event()

    async def run_step(client, agent, call_id, task):
        started.add(agent)
        if len(started) == settings.max_active_agents:
            all_started.set()
        await release.wait()
        return {}

    coding._run = run_step
    exhausted_legacy_gate = asyncio.Semaphore(0)
    runtimes = []
    for _ in range(settings.max_active_agents):
        runtime = NativeRuntime.__new__(NativeRuntime)
        runtime.agent_id = uuid4()
        runtime.lock = asyncio.Lock()
        runtime.store = store
        runtime.native = coding
        runtime.model_slots = exhausted_legacy_gate
        runtime.end_work = AsyncMock()
        runtime.tools = SimpleNamespace(streams=SimpleNamespace(interrupt=AsyncMock()))
        runtime.wake = AsyncMock()
        runtimes.append(runtime)
    tasks = [asyncio.create_task(runtime.activate()) for runtime in runtimes]
    try:
        await asyncio.wait_for(all_started.wait(), 3)
        assert len(started) == 100
        assert all(not task.done() for task in tasks)
        assert len(connections) == 100
    finally:
        release.set()
        await asyncio.gather(*tasks)
    store.record_failure.assert_not_called()
    # Per-session advisory locking and cleanup still apply to every activation.
    assert all(conn.fetchval.await_count == 1 for conn in connections)
    assert all(conn.execute.await_count == 1 for conn in connections)
    assert client.close.await_count == 100
