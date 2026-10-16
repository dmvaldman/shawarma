import asyncio
import os
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest
from pydantic import SecretStr

from harness.coding.clients import CodingError
from harness.coding.service import Coding

pytestmark = pytest.mark.skipif(
    os.environ.get("HARNESS_LIVE_TESTS") != "1", reason="PostgreSQL gate"
)


class OpenCodeFake:
    def __init__(self, model):
        self.model, self.messages, self.prompts = model, [], []
        self.hold = False
        self.aborted = asyncio.Event()
        self.started = asyncio.Event()
        self.fail_abort = False

    async def close(self):
        pass

    async def request(self, method, path, directory="", **kwargs):
        if path == "/provider":
            return {
                "connected": ["openai"],
                "all": [{"id": "openai", "models": {self.model: {"limit": {"context": 1000000}}}}],
            }
        if path == "/session":
            return {"id": "session-one"}
        if path.endswith("prompt_async"):
            body = kwargs["json"]
            self.prompts.append(body)
            self.started.set()
            if not self.hold:
                self.messages.append(
                    {
                        "info": {
                            "id": "reply-one",
                            "parentID": body["messageID"],
                            "role": "assistant",
                            "tokens": {"input": 100, "output": 12},
                            "cost": 0.02,
                        },
                        "parts": [{"type": "text", "text": "Tested and committed"}],
                    }
                )
            return None
        if path.endswith("/abort"):
            if self.fail_abort:
                raise CodingError("Worker unavailable")
            self.aborted.set()
            self.hold = False
            return True
        if path == "/session/status":
            return {"session-one": {"type": "busy" if self.hold else "idle"}}
        if path.endswith("/message"):
            return self.messages
        raise AssertionError(path)


@pytest.fixture
def coding(runtime_store, monkeypatch):
    store, settings, agent, workspace = runtime_store
    settings = settings.model_copy(
        update={"coding_enabled": True, "opencode_password": SecretStr("test")}
    )
    monkeypatch.setattr(
        "harness.coding.service.gitea_identity",
        AsyncMock(
            return_value={
                "username": "test-agent",
                "token": "private-agent-token",
                "email": "test@agents.invalid",
            }
        ),
    )
    monkeypatch.setattr(
        "harness.coding.native.gitea_identity",
        AsyncMock(
            return_value={
                "username": "test-agent",
                "token": "private-agent-token",
                "email": "test@agents.invalid",
            }
        ),
    )
    remote = OpenCodeFake(settings.openai_model_id)
    monkeypatch.setattr("harness.coding.service.OpenCode", lambda _: remote)
    monkeypatch.setattr("harness.coding.service.Worker.stop_commands", AsyncMock())
    monkeypatch.setattr(
        "harness.coding.service.Worker.workspace",
        AsyncMock(
            return_value={
                "directory": "/workspaces/one/agent",
                "branch": "agents/agent",
                "repository": "swarm/workspace",
            }
        ),
    )
    return Coding(store, settings), remote, agent, workspace


async def test_persistent_session_idempotency_and_coding_usage(coding):
    service, remote, agent, _ = coding
    result = await service.run(agent, "coding-call", "Implement a test")
    assert result["report"] == "Tested and committed"
    assert result["branch"] == "agents/agent"
    assert await service.run(agent, "coding-call", "Implement a test") == result
    assert len(remote.prompts) == 1
    state = await service.store.state(agent)
    assert state["spent"] == Decimal(".02")
    assert state["reserved"] == 0
    assert await service.store.pool.fetchval("SELECT output_tokens FROM agent_turns") == 12


async def test_cancel_aborts_remote_and_preserves_uncertain_call(coding):
    service, remote, agent, _ = coding
    remote.hold = True
    task = asyncio.create_task(service.run(agent, "cancel-call", "Build"))
    await asyncio.wait_for(remote.started.wait(), 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert remote.aborted.is_set()
    assert await service.store.pool.fetchval("SELECT state FROM coding_runs") == "interrupted"
    with pytest.raises(CodingError, match="uncertain"):
        await service.run(agent, "cancel-call", "Build")
    assert len(remote.prompts) == 1


async def test_owner_pause_blocks_coding_before_remote_side_effect(coding):
    service, remote, agent, workspace = coding
    await service.store.pool.execute("UPDATE workspaces SET state='paused' WHERE id=$1", workspace)
    with pytest.raises(CodingError, match="paused"):
        await service.run(agent, "paused-call", "Build")
    assert not remote.prompts


async def test_exact_model_rejection_does_not_substitute_or_spend(coding):
    service, remote, agent, _ = coding
    remote.model = "another-model"
    with pytest.raises(CodingError, match="Exact agent model"):
        await service.run(agent, "model-call", "Build")
    assert not remote.prompts
    assert await service.store.pool.fetchval("SELECT count(*) FROM agent_turns") == 0


async def test_failed_abort_blocks_new_work_and_restart_reconciles(coding):
    service, remote, agent, _ = coding
    remote.hold = remote.fail_abort = True
    task = asyncio.create_task(service.run(agent, "broken-cancel", "Build"))
    await asyncio.wait_for(remote.started.wait(), 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert await service.store.pool.fetchval("SELECT state FROM coding_runs") == "running"
    with pytest.raises(CodingError, match="Unreconciled"):
        await service.run(agent, "next-call", "Build")
    remote.fail_abort = False
    await service.recover()
    assert remote.aborted.is_set()
    assert await service.store.pool.fetchval("SELECT state FROM coding_runs") == "interrupted"
    assert len(remote.prompts) == 1


async def test_at_most_one_coding_operation_per_agent(coding):
    service, remote, agent, _ = coding
    remote.hold = True
    task = asyncio.create_task(service.run(agent, "first-call", "Build"))
    await asyncio.wait_for(remote.started.wait(), 5)
    with pytest.raises(CodingError, match="already has"):
        await service.run(agent, "second-call", "Build")
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_aborted_reply_is_recoverable_and_run_is_settled(coding):
    from harness.coding.clients import CodingInterrupted

    service, remote, agent, _ = coding
    original = remote.request

    async def request(method, path, directory="", **kwargs):
        result = await original(method, path, directory, **kwargs)
        if path.endswith("prompt_async"):
            remote.messages[-1]["info"]["error"] = {
                "name": "MessageAbortedError",
                "data": {"message": "Aborted"},
            }
        return result

    remote.request = request
    with pytest.raises(CodingInterrupted, match="interrupted"):
        await service.run(agent, "aborted-turn", "Continue existing work")
    assert await service.store.pool.fetchval("SELECT state FROM coding_runs") == "interrupted"
    assert not await service.store.pool.fetchval(
        "SELECT EXISTS(SELECT 1 FROM coding_runs WHERE state='running')"
    )
    assert (await service.store.state(agent))["reserved"] == 0


@pytest.mark.parametrize(
    "reason,recovered",
    [
        ("Native runtime interrupted (ConnectError); inspect #errors", True),
        ("Native runtime interrupted (RemoteProtocolError); inspect #errors", True),
        ("Gitea HTTP 401: Unauthorized", False),
    ],
)
async def test_restart_recovers_only_known_connection_bug_without_unpausing(
    coding, reason, recovered
):
    import json

    service, remote, agent, workspace = coding
    await service.store.pool.execute("UPDATE workspaces SET state='paused' WHERE id=$1", workspace)
    await service.store.pool.execute(
        "UPDATE agents SET state='QUOTA_BLOCKED',control_paused=true,failure=$2 WHERE id=$1",
        agent,
        json.dumps({"category": "native_runtime", "reason": reason}),
    )
    await service.recover()
    row = await service.store.pool.fetchrow(
        "SELECT state,failure,control_paused FROM agents WHERE id=$1", agent
    )
    assert row["state"] == ("READY" if recovered else "QUOTA_BLOCKED")
    assert (row["failure"] is None) == recovered
    assert row["control_paused"]
    assert not remote.prompts
    assert (
        await service.store.pool.fetchval("SELECT state FROM workspaces WHERE id=$1", workspace)
        == "paused"
    )
