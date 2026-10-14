import asyncio
import json
import os
from decimal import Decimal
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from aiohttp import web
from pydantic import SecretStr

from harness.coding.native import Native
from harness.coding.runtime import NativeRuntime
from harness.coding.tool_bridge import ToolBridge
from harness.core.types import Post
from harness.mattermost.client import Client
from harness.tools.core import CoreTools

pytestmark = pytest.mark.skipif(
    os.environ.get("HARNESS_LIVE_TESTS") != "1", reason="PostgreSQL gate"
)


class Remote:
    def __init__(self, model):
        self.model, self.messages, self.prompts = model, [], []
        self.hold = False
        self.started, self.aborted = asyncio.Event(), asyncio.Event()

    async def close(self):
        pass

    async def request(self, method, path, directory="", **kwargs):
        if path == "/provider":
            return {
                "connected": ["openai"],
                "all": [{"id": "openai", "models": {self.model: {"limit": {"context": 1000000}}}}],
            }
        if path == "/session":
            return {"id": "native-session"}
        if path.endswith("/message") and method == "POST":
            body = kwargs["json"]
            self.messages.append(
                {"info": {"role": "user", "id": body["messageID"]}, "parts": body["parts"]}
            )
            return self.messages[-1]
        if path.endswith("prompt_async"):
            body = kwargs["json"]
            self.prompts.append(body)
            self.started.set()
            self.messages.append(
                {
                    "info": {
                        "id": "reply-" + str(len(self.prompts)),
                        "role": "assistant",
                        "parentID": body["messageID"],
                        "cost": 0.01,
                        "tokens": {"input": 20, "output": 5},
                    },
                    "parts": [{"type": "text", "text": "Worked"}],
                }
            )
            return None
        if path.endswith("/message"):
            return self.messages
        if path.endswith("/abort"):
            self.hold = False
            self.aborted.set()
            return True
        if path == "/session/status":
            return {"native-session": {"type": "busy" if self.hold else "idle"}}
        raise AssertionError(path)


@pytest.fixture
def native(runtime_store, monkeypatch):
    store, settings, agent, workspace = runtime_store
    settings = settings.model_copy(
        update={
            "coding_enabled": True,
            "agent_engine": "opencode",
            "opencode_password": SecretStr("worker"),
        }
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
    remote = Remote(settings.openai_model_id)
    monkeypatch.setattr("harness.coding.service.OpenCode", lambda _: remote)
    monkeypatch.setattr("harness.coding.service.Worker.stop_commands", AsyncMock())
    monkeypatch.setattr(
        "harness.coding.native.Worker.command",
        AsyncMock(return_value=json.dumps({"branch": "agents/agent", "commit": "a" * 40})),
    )
    monkeypatch.setattr(
        "harness.coding.service.Worker.workspace",
        AsyncMock(return_value={"directory": "/workspaces/one/agent", "branch": "agents/agent"}),
    )
    return Native(store, settings), remote, agent, workspace


async def test_one_session_imports_history_once_and_reuses_for_all_work(native):
    engine, remote, agent, _ = native
    await engine.store.append(agent, [{"role": "user", "content": "Original objective"}])
    await engine.run(agent, "first", "Continue")
    await engine.run(agent, "second", "Continue")
    users = [m for m in remote.messages if m["info"]["role"] == "user"]
    assert len(users) == 1 and "Original objective" in users[0]["parts"][0]["text"]
    assert all(p["agent"] == "swarm" for p in remote.prompts)
    assert all("post_channel" in p["system"] for p in remote.prompts)
    assert await engine.store.pool.fetchval("SELECT count(*) FROM coding_sessions") == 1
    assert await engine.store.pool.fetchval("SELECT sum(output_tokens) FROM agent_turns") == 10
    assert not await engine.store.continuation_pending(agent)


async def test_busy_session_buffers_channel_messages_until_idle_without_second_loop(native):
    engine, remote, agent, _ = native
    bot = Client("http://mattermost.invalid", "token")
    runtime = NativeRuntime(engine.store, bot, agent, engine.settings, AsyncMock())
    runtime.native = engine
    engine.deliveries[agent] = runtime.deliver
    remote.hold = True
    task = asyncio.create_task(engine.run(agent, "busy", "Continue"))
    try:
        await asyncio.wait_for(remote.started.wait(), 3)
        await engine.store.ingest(
            agent,
            Post(
                id="new-post",
                user_id="human",
                channel_id="general",
                message="Change the approach",
                create_at=1000,
            ),
            "channel_post",
            "server",
        )
        await engine.poll_session(
            remote,
            agent,
            {
                "session_id": "native-session",
                "directory": "/workspaces/one/agent",
            },
        )
        assert await engine.store.unread_count(agent) == 1
        assert "Change the approach" not in json.dumps(remote.messages)
        remote.hold = False
        await asyncio.wait_for(task, 3)
        assert len(remote.prompts) == 1
        assert "Change the approach" in json.dumps(remote.messages)
        assert await engine.store.unread_count(agent) == 0
        # Replaying the same durable batch cannot append a second native message.
        session = dict(await engine.store.pool.fetchrow("SELECT * FROM coding_sessions"))
        user = [m for m in remote.messages if m["info"]["role"] == "user"][0]
        before = len(remote.messages)
        await engine.append_once(remote, session, "same", "test", agent)
        await engine.append_once(remote, session, "same", "test", agent)
        assert len(remote.messages) == before + 1
        assert user["info"]["id"].startswith("msg_")
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await bot.http.aclose()
    assert not remote.aborted.is_set()
    assert await engine.store.pool.fetchval("SELECT state FROM coding_runs") == "completed"


async def test_tool_bridge_auth_identity_pause_and_duplicate_receipts(runtime_store):
    store, settings, agent, workspace = runtime_store
    settings = settings.model_copy(
        update={"agent_engine": "opencode", "swarm_tool_key": SecretStr("bridge-key")}
    )
    bot = Client("http://mattermost.invalid", "bot-token")
    bot.me = AsyncMock(return_value={"id": "bot"})
    bot.request = AsyncMock(return_value={"id": "human", "username": "owner"})
    tools = CoreTools(store, bot, agent, settings)
    bridge = ToolBridge(store, settings, workspace)
    bridge.tools[agent] = tools
    turn = await store.reserve_cost(agent, Decimal(0), [])
    await store.pool.execute(
        "INSERT INTO coding_sessions(agent_id,session_id,directory,branch,worker_id) "
        "VALUES($1,'session','directory','branch','worker')",
        agent,
    )
    await store.pool.execute(
        "INSERT INTO coding_runs(id,agent_id,call_id,turn_id) VALUES($1,$2,'run',$3)",
        uuid4(),
        agent,
        turn,
    )

    class Request:
        headers = {"Authorization": "Bearer bridge-key"}

        async def json(self):
            return {
                "session_id": "session",
                "name": "get_status",
                "call_id": "call",
                "arguments": {},
                "agent_id": str(uuid4()),
            }

    request = Request()
    try:
        first = await bridge.handle(request)
        second = await bridge.handle(request)
        assert json.loads(first.body) == json.loads(second.body)
        assert json.loads(first.body)["agent_id"] == str(agent)
        assert await store.pool.fetchval("SELECT count(*) FROM tool_calls") == 1
        request.headers = {}
        with pytest.raises(web.HTTPUnauthorized):
            await bridge.handle(request)
        request.headers = {"Authorization": "Bearer bridge-key"}
        await store.pool.execute("UPDATE agents SET control_paused=true WHERE id=$1", agent)
        with pytest.raises(web.HTTPForbidden):
            await bridge.handle(request)
        await store.pool.execute("UPDATE agents SET control_paused=false WHERE id=$1", agent)
        request.json = AsyncMock(
            return_value={
                "session_id": "foreign",
                "name": "get_status",
                "call_id": "x",
                "arguments": {},
            }
        )
        with pytest.raises(web.HTTPForbidden):
            await bridge.handle(request)
    finally:
        await bot.http.aclose()


async def test_bridge_owner_cancellation_interrupts_external_tool(runtime_store):
    store, settings, agent, workspace = runtime_store
    settings = settings.model_copy(update={"swarm_tool_key": SecretStr("key")})
    bridge = ToolBridge(store, settings, workspace)
    started = asyncio.Event()
    task = asyncio.create_task(asyncio.sleep(60))
    bridge.tasks[agent] = {task}
    started.set()
    await bridge.cancel(agent)
    assert task.cancelled()


async def test_native_pause_aborts_work_and_resume_reuses_session(native):
    engine, remote, agent, workspace = native
    await engine.store.pool.execute("UPDATE workspaces SET started=true WHERE id=$1", workspace)
    bot = Client("http://mattermost.invalid", "token")
    runtime = NativeRuntime(engine.store, bot, agent, engine.settings, AsyncMock())
    runtime.native = engine
    remote.hold = True
    runtime.task = asyncio.create_task(runtime.activate())
    try:
        await asyncio.wait_for(remote.started.wait(), 3)
        await engine.store.pool.execute(
            "UPDATE workspaces SET state='paused' WHERE id=$1", workspace
        )
        await runtime.pause()
        assert remote.aborted.is_set()
        assert (await engine.store.state(agent))["state"] == "READY"
        assert (await engine.store.state(agent))["reserved"] == 0
        assert await engine.store.pool.fetchval("SELECT state FROM coding_runs") == "interrupted"
        remote.started.clear()
        remote.hold = True
        await engine.store.pool.execute(
            "UPDATE workspaces SET state='active' WHERE id=$1", workspace
        )
        await runtime.wake(agent)
        await asyncio.wait_for(remote.started.wait(), 3)
        assert len(remote.prompts) == 2
        assert await engine.store.pool.fetchval("SELECT count(*) FROM coding_sessions") == 1
    finally:
        runtime.stopping.set()
        await runtime.pause()
        await bot.http.aclose()
