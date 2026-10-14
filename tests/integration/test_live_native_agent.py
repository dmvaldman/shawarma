"""One real native loop combines swarm tools, editing, execution and Git.

The Mattermost transport is captured, so no test messages reach the real team.
"""

import json
import os
from unittest.mock import AsyncMock

import pytest

from harness.coding.clients import OpenCode, Worker
from harness.coding.native import Native
from harness.coding.tool_bridge import ToolBridge
from harness.core.types import Post
from harness.mattermost.client import Client
from harness.tools.core import CoreTools

pytestmark = pytest.mark.skipif(
    os.environ.get("HARNESS_NATIVE_INFERENCE_TESTS") != "1", reason="explicit paid native gate"
)


async def test_real_single_session_swarm_tools_and_coding(runtime_store):
    store, settings, agent, workspace = runtime_store
    settings = settings.model_copy(
        update={"coding_enabled": True, "agent_engine": "opencode", "coding_timeout_sec": 120}
    )
    engine, worker, native = Native(store, settings), Worker(settings), OpenCode(settings)
    bot = Client("http://mattermost.invalid", "test")
    bot.request = AsyncMock(
        return_value={"id": "general", "type": "O", "team_id": "team", "username": "owner"}
    )
    bot.read_channel = AsyncMock(return_value=[])

    async def capture(channel, text, root=""):
        return Post(id="captured", channel_id=channel, user_id="bot", message=text, create_at=1000)

    bot.post_channel = AsyncMock(side_effect=capture)
    tools = CoreTools(store, bot, agent, settings)
    bridge = ToolBridge(store, settings, workspace)
    bridge.tools[agent] = tools
    session = None
    try:
        await bridge.start()
        result = await engine.run(
            agent,
            "native-smoke",
            (
                "This is a bounded integration smoke, not an ongoing research task. "
                "Call get_status to check your Swarm identity. In your own checkout create "
                "native_sum.py containing add(a,b) and two assert tests (positive and negative). "
                "Execute python3 native_sum.py. Commit only that file and push your branch. "
                "Then call post_channel with channel_id='general' to report the test results "
                "and branch. Do not spawn peers, browse, or perform extra tasks. Return when done."
            ),
        )
        session = dict(
            await store.pool.fetchrow("SELECT * FROM coding_sessions WHERE agent_id=$1", agent)
        )
        calls = await store.pool.fetch("SELECT tool_name,state,result FROM tool_calls")
        assert {"get_status", "post_channel"} <= {r["tool_name"] for r in calls}
        assert all(r["state"] == "completed" for r in calls)
        assert bot.post_channel.call_count == 1
        await worker.command(
            [
                "docker",
                "exec",
                "--user",
                "node",
                "--workdir",
                session["directory"],
                settings.coding_container,
                "python3",
                "native_sum.py",
            ]
        )
        pushed = await worker.command(
            [
                "docker",
                "exec",
                "--user",
                "node",
                settings.coding_container,
                "git",
                "-C",
                session["directory"],
                "ls-remote",
                "--exit-code",
                "origin",
                session["branch"],
            ]
        )
        assert pushed.strip()
        assert await store.pool.fetchval("SELECT count(*) FROM coding_sessions") == 1
        assert await store.pool.fetchval("SELECT count(*) FROM coding_runs") == 1
        print(
            "Unified smoke output tokens:",
            result["output_tokens"],
            "estimated USD:",
            result["cost_usd"],
        )
        # The native conversation itself contains the coordination tool results.
        messages = await native.request(
            "GET", f"/session/{session['session_id']}/message", session["directory"]
        )
        assert "post_channel" in json.dumps(messages)
    finally:
        await bridge.close()
        if session:
            await native.request(
                "DELETE", f"/session/{session['session_id']}", session["directory"]
            )
        await native.close()
        await bot.http.aclose()
        await worker.command(
            [
                "docker",
                "exec",
                "--user",
                "node",
                settings.coding_container,
                "python3",
                "-c",
                "import shutil; shutil.rmtree("
                + json.dumps("/workspaces/" + str(workspace))
                + ",ignore_errors=True)",
            ]
        )
