"""Opt-in paid, bounded end-to-end coding smoke; isolated from the live swarm."""

import asyncio
import json
import os

import pytest

from harness.coding.clients import OpenCode, Worker
from harness.coding.service import Coding

pytestmark = pytest.mark.skipif(
    os.environ.get("HARNESS_CODING_INFERENCE_TESTS") != "1", reason="explicit paid coding gate"
)


async def test_real_opencode_edit_execute_commit_and_push(runtime_store):
    store, settings, agent, workspace = runtime_store
    settings = settings.model_copy(update={"coding_enabled": True, "coding_timeout_sec": 90})
    service, worker, client = Coding(store, settings), Worker(settings), OpenCode(settings)
    try:
        result = await service.run(
            agent,
            "paid-coding-smoke",
            (
                "This is a tiny integration smoke test. In your assigned checkout create "
                "smoke_sum.py with add(a,b) returning a+b and two unittest cases for positive "
                "and negative integers. Run python3 smoke_sum.py with unittest.main(). "
                "Commit only smoke_sum.py and push your assigned branch to origin. "
                "Do not read other code, spawn agents, search the web, or do additional work. "
                "Report the test results and commit."
            ),
        )
        session = await store.pool.fetchrow(
            "SELECT * FROM coding_sessions WHERE agent_id=$1", agent
        )
        output = await worker.command(
            [
                "docker",
                "exec",
                "--user",
                "node",
                "--workdir",
                session["directory"],
                settings.coding_container,
                "python3",
                "smoke_sum.py",
            ]
        )
        assert result["output_tokens"] > 0
        log = await worker.command(
            [
                "docker",
                "exec",
                "--user",
                "node",
                settings.coding_container,
                "git",
                "-C",
                session["directory"],
                "log",
                "-1",
                "--format=%s",
            ]
        )
        assert log.strip() and not output.strip()
        await worker.command(
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
        print(
            "Coding smoke output tokens:",
            result["output_tokens"],
            "estimated USD:",
            result["cost_usd"],
        )
    finally:
        session = await store.pool.fetchrow(
            "SELECT * FROM coding_sessions WHERE agent_id=$1", agent
        )
        if session:
            await client.request(
                "POST", f"/session/{session['session_id']}/abort", session["directory"]
            )
            await client.request(
                "DELETE", f"/session/{session['session_id']}", session["directory"]
            )
            await worker.command(
                [
                    "docker",
                    "exec",
                    "--user",
                    "node",
                    settings.coding_container,
                    "python3",
                    "-c",
                    "import shutil; shutil.rmtree('/workspaces/" + str(workspace) + "')",
                ]
            )
        await client.close()


async def test_real_abort_stops_running_command(runtime_store):
    store, settings, agent, workspace = runtime_store
    settings = settings.model_copy(update={"coding_enabled": True, "coding_timeout_sec": 90})
    service, worker, client = Coding(store, settings), Worker(settings), OpenCode(settings)
    marker = "cancel_marker_" + agent.hex
    directory = "/workspaces/" + str(workspace) + "/" + str(agent)
    command = (
        "python3 -c 'import time; from pathlib import Path; Path(\""
        + marker
        + '").write_text("started"); time.sleep(60)\''
    )
    task = asyncio.create_task(
        service.run(
            agent,
            "cancel-smoke",
            (
                "Cancellation integration test. Run exactly this shell command, without editing "
                "other files or doing anything else: " + command
            ),
        )
    )
    try:
        for _ in range(30):
            await asyncio.sleep(1)
            if task.done():
                await task
                pytest.fail("Coding ended before cancellation could be tested")
            out = await worker.command(
                [
                    "docker",
                    "exec",
                    "--user",
                    "node",
                    settings.coding_container,
                    "python3",
                    "-c",
                    "from pathlib import Path; print(Path("
                    + json.dumps(directory + "/" + marker)
                    + ").exists())",
                ]
            )
            if out.strip() == "True":
                break
        else:
            pytest.fail("Command did not start within the smoke deadline")
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.sleep(1)
        script = (
            "import os; from pathlib import Path; marker=" + json.dumps(marker) + "; "
            "print(any(marker.encode() in p.read_bytes() for p in Path('/proc').glob('*/cmdline') "
            "if p.parent.name.isdigit() and p.parent.name != str(os.getpid()) and p.exists()))"
        )
        out = await worker.command(
            [
                "docker",
                "exec",
                "--user",
                "node",
                settings.coding_container,
                "python3",
                "-c",
                script,
            ]
        )
        assert out.strip() == "False", "Native abort left the command running"
        assert await store.pool.fetchval("SELECT state FROM coding_runs") == "interrupted"
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        session = await store.pool.fetchrow(
            "SELECT * FROM coding_sessions WHERE agent_id=$1", agent
        )
        if session:
            await client.request(
                "DELETE", f"/session/{session['session_id']}", session["directory"]
            )
            await worker.command(
                [
                    "docker",
                    "exec",
                    "--user",
                    "node",
                    settings.coding_container,
                    "python3",
                    "-c",
                    "import shutil; shutil.rmtree('/workspaces/" + str(workspace) + "')",
                ]
            )
        await client.close()
