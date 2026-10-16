import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest

from harness.coding.clients import CodingUnavailable, OpenCode
from harness.coding.runtime import NativeRuntime
from harness.config import Settings


@pytest.mark.parametrize("failures,unavailable", [(1, False), (2, True)])
async def test_safe_reads_retry_once(failures, unavailable):
    client = OpenCode(Settings())
    await client.http.aclose()
    calls = 0

    def handle(request):
        nonlocal calls
        calls += 1
        if calls <= failures:
            raise httpx.ReadTimeout("timed out", request=request)
        return httpx.Response(200, json={"ready": True})

    client.http = httpx.AsyncClient(base_url="http://worker", transport=httpx.MockTransport(handle))
    try:
        if unavailable:
            with pytest.raises(CodingUnavailable, match="two read attempts"):
                await client.request("GET", "/session/status")
        else:
            assert await client.request("GET", "/session/status") == {"ready": True}
        assert calls == 2
    finally:
        await client.close()


@pytest.mark.parametrize(
    "error_type", [httpx.ReadTimeout, httpx.ConnectError, httpx.RemoteProtocolError]
)
async def test_uncertain_mutation_is_never_replayed(error_type):
    client = OpenCode(Settings())
    await client.http.aclose()
    calls = 0

    def handle(request):
        nonlocal calls
        calls += 1
        raise error_type("unknown outcome", request=request)

    client.http = httpx.AsyncClient(base_url="http://worker", transport=httpx.MockTransport(handle))
    try:
        with pytest.raises(error_type):
            await client.request("POST", "/session/id/prompt_async", json={})
        assert calls == 1
    finally:
        await client.close()


@pytest.mark.parametrize(
    "error_type", [httpx.ReadTimeout, httpx.ConnectError, httpx.RemoteProtocolError]
)
@pytest.mark.parametrize("unreconciled", [False, True])
async def test_transport_requeues_only_after_confirmed_cleanup(
    unreconciled, error_type, monkeypatch
):
    runtime = NativeRuntime.__new__(NativeRuntime)
    runtime.lock = asyncio.Lock()
    runtime.agent_id = uuid4()
    runtime.store = SimpleNamespace(
        pool=SimpleNamespace(fetchval=AsyncMock(return_value=unreconciled)),
        state=AsyncMock(
            return_value={"state": "RUNNING", "control_paused": False, "workspace_state": "active"}
        ),
        set_state=AsyncMock(),
        record_failure=AsyncMock(),
    )
    runtime.native = SimpleNamespace(
        allowed=AsyncMock(), run=AsyncMock(side_effect=error_type("connection interrupted"))
    )
    runtime.tools = SimpleNamespace(streams=SimpleNamespace(interrupt=AsyncMock()))
    runtime.end_work = AsyncMock()
    runtime.wake = AsyncMock()
    delay = AsyncMock()
    monkeypatch.setattr("harness.coding.runtime.asyncio.sleep", delay)
    await runtime.activate()
    if unreconciled:
        assert runtime.store.record_failure.await_args.kwargs["blocked"]
        delay.assert_not_awaited()
    else:
        runtime.store.record_failure.assert_not_awaited()
        delay.assert_awaited_once_with(5)
        assert runtime.store.set_state.await_args.args[1] == "READY"
        await runtime.activate()
        assert runtime.store.record_failure.await_args.kwargs["blocked"]
        assert runtime.store.record_failure.await_args.args[1]["category"] == "worker_unavailable"
    await asyncio.sleep(0)


@pytest.mark.parametrize("unreconciled", [False, True])
async def test_aborted_turn_requeues_after_cleanup_with_backoff(unreconciled, monkeypatch):
    from harness.coding.clients import CodingInterrupted

    runtime = NativeRuntime.__new__(NativeRuntime)
    runtime.lock = asyncio.Lock()
    runtime.agent_id = uuid4()
    runtime.store = SimpleNamespace(
        pool=SimpleNamespace(fetchval=AsyncMock(return_value=unreconciled)),
        set_state=AsyncMock(),
        record_failure=AsyncMock(),
    )
    runtime.native = SimpleNamespace(
        allowed=AsyncMock(), run=AsyncMock(side_effect=CodingInterrupted("aborted"))
    )
    runtime.tools = SimpleNamespace(streams=SimpleNamespace(interrupt=AsyncMock()))
    runtime.end_work = AsyncMock()
    runtime.wake = AsyncMock()
    delay = AsyncMock()
    monkeypatch.setattr("harness.coding.runtime.asyncio.sleep", delay)
    await runtime.activate()
    if unreconciled:
        assert runtime.store.record_failure.await_args.kwargs["blocked"]
        delay.assert_not_awaited()
    else:
        runtime.store.record_failure.assert_not_awaited()
        assert runtime.store.set_state.await_args.args[1] == "READY"
        delay.assert_awaited_once_with(5)


async def test_inbox_waits_for_idle_session_boundary():
    from harness.coding.native import Native

    engine = Native.__new__(Native)
    agent = uuid4()
    deliver = AsyncMock()
    engine.deliveries = {agent: deliver}
    remote = SimpleNamespace(request=AsyncMock(return_value={"session": {"type": "busy"}}))
    session = {"session_id": "session", "directory": "/workspace"}
    await engine.poll_session(remote, agent, session)
    deliver.assert_not_awaited()
    remote.request.return_value = {"session": {"type": "idle"}}
    await engine.poll_session(remote, agent, session)
    deliver.assert_awaited_once_with(remote, session)


@pytest.mark.parametrize(
    "state,paused,workspace",
    [
        ("TERMINATED", False, "active"),
        ("RUNNING", True, "active"),
        ("RUNNING", False, "paused"),
    ],
)
async def test_connection_error_during_shutdown_does_not_block_or_revive(state, paused, workspace):
    runtime = NativeRuntime.__new__(NativeRuntime)
    runtime.lock = asyncio.Lock()
    runtime.agent_id = uuid4()
    runtime.store = SimpleNamespace(
        state=AsyncMock(
            return_value={"state": state, "control_paused": paused, "workspace_state": workspace}
        ),
        set_state=AsyncMock(),
        record_failure=AsyncMock(),
        pool=SimpleNamespace(fetchval=AsyncMock()),
    )
    runtime.native = SimpleNamespace(
        allowed=AsyncMock(), run=AsyncMock(side_effect=httpx.ConnectError("shutting down"))
    )
    runtime.tools = SimpleNamespace(streams=SimpleNamespace(interrupt=AsyncMock()))
    runtime.end_work = AsyncMock()
    runtime.wake = AsyncMock()
    await runtime.activate()
    runtime.store.record_failure.assert_not_awaited()
    runtime.store.pool.fetchval.assert_not_awaited()
    if state == "TERMINATED":
        assert runtime.store.set_state.await_count == 1  # Initial RUNNING, never requeued.
    else:
        assert runtime.store.set_state.await_args.args[1] == "READY"
    await asyncio.sleep(0)
