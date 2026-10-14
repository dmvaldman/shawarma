"""Real OpenCode + Gitea gate; no inference provider is called."""

import json
import os
from uuid import uuid4

import pytest

from harness.coding.clients import OpenCode, Worker
from harness.config import Settings

pytestmark = pytest.mark.skipif(
    os.environ.get("HARNESS_CODING_LIVE_TESTS") != "1", reason="real coding worker gate"
)


async def test_real_git_isolation_push_fetch_and_native_sessions():
    settings = Settings()
    worker, client = Worker(settings), OpenCode(settings)
    workspace, first_agent, second_agent = uuid4(), uuid4(), uuid4()
    first = await worker.workspace(workspace, first_agent)
    second = await worker.workspace(workspace, second_agent)
    sessions = []

    async def git(directory, *args):
        return await worker.command(
            [
                "docker",
                "exec",
                "--user",
                "node",
                settings.coding_container,
                "git",
                "-C",
                directory,
                *args,
            ]
        )

    try:
        assert first["directory"] != second["directory"]
        assert first["branch"] != second["branch"]
        for space in [first, second]:
            session = await client.request(
                "POST",
                "/session",
                space["directory"],
                json={"title": "No-inference integration gate"},
            )
            sessions.append((session["id"], space["directory"]))
        assert sessions[0][0] != sessions[1][0]
        # Exercise native message validation/storage without making any model request.
        message = await client.request(
            "POST",
            f"/session/{sessions[0][0]}/message",
            first["directory"],
            json={
                "noReply": True,
                "system": "Coding smoke test",
                "parts": [{"type": "text", "text": "No inference"}],
            },
        )
        assert message["info"]["role"] == "user"
        assert (
            len(
                await client.request(
                    "GET", f"/session/{sessions[0][0]}/message", first["directory"]
                )
            )
            == 1
        )
        config = await client.request("GET", "/config", first["directory"])
        assert config["permission"]["task"] == "deny"
        providers = await client.request("GET", "/provider", first["directory"])
        assert "agent37" not in providers["connected"]
        # Stop one checkout's detached command without touching a peer's command.
        pids = []
        for space in [first, second]:
            spawn = (
                "import subprocess,sys; "
                "print(subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)'], "
                "stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,start_new_session=True).pid)"
            )
            pids.append(
                int(
                    (
                        await worker.command(
                            [
                                "docker",
                                "exec",
                                "--user",
                                "node",
                                "--workdir",
                                space["directory"],
                                settings.coding_container,
                                "python3",
                                "-c",
                                spawn,
                            ]
                        )
                    ).strip()
                )
            )
        await worker.stop_commands(first["directory"])
        inspect = (
            "from pathlib import Path; pids=" + repr(pids) + "; "
            "print([Path('/proc/'+str(p)).exists() and "
            "Path('/proc/'+str(p)+'/stat').read_text().rsplit(')',1)[1].split()[0]!='Z' "
            "for p in pids])"
        )
        alive = await worker.command(
            [
                "docker",
                "exec",
                "--user",
                "node",
                settings.coding_container,
                "python3",
                "-c",
                inspect,
            ]
        )
        assert alive.strip() == "[False, True]"
        await worker.stop_commands(second["directory"])
        # Native bash execution is available through OpenCode's persistent PTY API;
        # Git below also proves file execution happens in the remote worker.
        script = "from pathlib import Path; Path('smoke.txt').write_text('verified\\n')"
        await worker.command(
            [
                "docker",
                "exec",
                "--user",
                "node",
                "--workdir",
                first["directory"],
                settings.coding_container,
                "python3",
                "-c",
                script,
            ]
        )
        await git(first["directory"], "add", "smoke.txt")
        await git(first["directory"], "commit", "-m", "Verify worker collaboration")
        await git(first["directory"], "push", "origin", first["branch"])
        await git(second["directory"], "fetch", "origin", first["branch"])
        assert (
            await git(second["directory"], "show", "FETCH_HEAD:smoke.txt")
        ).strip() == "verified"
        assert (await git(second["directory"], "status", "--porcelain")).strip() == ""
        await git(first["directory"], "push", "origin", "--delete", first["branch"])
    finally:
        for session, directory in sessions:
            await client.request("DELETE", f"/session/{session}", directory)
        await client.close()
        await worker.command(
            [
                "docker",
                "exec",
                "--user",
                "node",
                settings.coding_container,
                "python3",
                "-c",
                "import shutil; shutil.rmtree(" + json.dumps("/workspaces/" + str(workspace)) + ")",
            ]
        )
