"""Runs inside a trusted, single-owner coding worker, never on the control-plane host."""

import json
import os
import re
import subprocess
import sys
from pathlib import Path
from uuid import UUID


class WorkspaceRecoveryRequired(RuntimeError):
    """A safe, fixed explanation that may be shown to the workspace owner."""


def restore_assigned_branch(directory: Path, branch: str, git) -> None:
    current = git("branch", "--show-current")
    if current == branch or current.startswith(branch + "-"):
        # Separate PRs may use feature branches in this agent's own namespace.
        return
    if current != "main":
        raise WorkspaceRecoveryRequired(
            "Workspace is on an unexpected branch; inspect it before continuing"
        )
    # A canceled activation may have temporarily checked main for synchronization.
    # Switch only a clean checkout with no unfinished Git operation; never reset/stash.
    if any(
        (directory / ".git" / name).exists()
        for name in (
            "MERGE_HEAD",
            "CHERRY_PICK_HEAD",
            "REVERT_HEAD",
            "rebase-merge",
            "rebase-apply",
            "sequencer",
            "BISECT_LOG",
        )
    ):
        raise WorkspaceRecoveryRequired(
            "Workspace has an unfinished Git operation; preserve it for recovery"
        )
    if git("status", "--porcelain"):
        raise WorkspaceRecoveryRequired(
            "Workspace is on main with uncommitted changes; inspect before continuing"
        )
    if not git("branch", "--list", branch):
        raise WorkspaceRecoveryRequired(
            "Assigned workspace branch is missing; inspect before continuing"
        )
    git("switch", branch)


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
    restore_assigned_branch(directory, branch, git)
    return {"directory": str(directory), "branch": branch, "repository": repo}


if __name__ == "__main__":
    try:
        print(json.dumps(prepare(json.load(sys.stdin))))
    except WorkspaceRecoveryRequired as error:
        print(json.dumps({"error": "WORKSPACE_RECOVERY_REQUIRED", "detail": str(error)}))
