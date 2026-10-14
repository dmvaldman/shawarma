"""Runs inside a trusted, single-owner coding worker, never on the control-plane host."""

import json
import os
import re
import subprocess
import sys
from pathlib import Path
from uuid import UUID


def prepare(data: dict) -> dict[str, str]:
    workspace, agent = str(UUID(data["workspace"])), str(UUID(data["agent"]))
    directory = Path("/workspaces") / workspace / agent
    branch = "agents/" + agent
    url = os.environ["SWARM_GITEA_URL"].rstrip("/")
    repo = os.environ["SWARM_GITEA_OWNER"] + "/" + os.environ["SWARM_GITEA_REPO"]

    def git(*args: str) -> str:
        result = subprocess.run(
            ["git", *args],
            cwd=directory,
            capture_output=True,
            text=True,
            check=True,
            timeout=60,
            env=git_env,
        )
        return result.stdout.strip()

    directory.mkdir(parents=True, exist_ok=True)
    identity = data.get("identity")
    if not identity:
        raise RuntimeError("Per-agent Gitea identity required")
    repo = identity.get("repository", repo)
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[a-z][a-z0-9-]{0,62}", repo):
        raise RuntimeError("Invalid repository target")
    # Clone authentication stays in memory and never enters argv or the remote URL.
    git_env = os.environ.copy()
    git_env["SWARM_AGENT_GITEA_USERNAME"] = identity["username"]
    git_env["SWARM_AGENT_GITEA_TOKEN"] = identity["token"]
    git_env["SWARM_AGENT_GITEA_REPOSITORY"] = repo
    # The helper uses this agent credential for clone and its private .git file later.
    # No secret enters the remote URL, tracked source tree or command output.
    if not (directory / ".git").exists():
        git("clone", url + "/" + repo + ".git", ".")
        git("checkout", "-b", branch)
        with (directory / ".git/info/exclude").open("a") as exclusions:
            exclusions.write("\n.env\n.env.*\n!.env.example\n")
    if git("remote", "get-url", "origin") != url + "/" + repo + ".git":
        raise RuntimeError("Workspace repository changed; explicit migration required")
    credential = directory / ".git/swarm-credential.json"
    credential.write_text(
        json.dumps(
            {"username": identity["username"], "token": identity["token"], "repository": repo}
        )
    )
    credential.chmod(0o600)
    git("config", "user.name", identity["username"])
    git("config", "user.email", identity.get("email", identity["username"] + "@agents.invalid"))
    if git("branch", "--show-current") != branch:
        raise RuntimeError("Workspace branch changed; inspect it before continuing")
    return {"directory": str(directory), "branch": branch, "repository": repo}


if __name__ == "__main__":
    print(json.dumps(prepare(json.load(sys.stdin))))
