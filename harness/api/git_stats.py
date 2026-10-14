"""Current PR state from Gitea, attributed by creation receipts or agent branches."""

import asyncio
import logging
import time
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote

from harness.config import Settings
from harness.tools.gitea import request_repository


class GitStats:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.key: tuple[str, ...] = ()
        self.until = 0.0
        self.counts: dict[str, dict[str, int]] = {}
        self.branches: set[str] | None = None
        self.commits: set[str] = set()
        self.at: str | None = None
        self.unavailable = False
        self.lock = asyncio.Lock()
        self.repositories: dict[str, GitStats] = {}

    async def enrich(self, data: dict[str, Any]) -> None:
        agents = data["agents"]
        names = {a.get("repository_name") or self.settings.gitea_repo for a in agents}
        if names != {self.settings.gitea_repo}:
            reports = []
            for name in sorted(names):
                child = self.repositories.setdefault(
                    name, GitStats(self.settings.model_copy(update={"gitea_repo": name}))
                )
                group = {
                    **data,
                    "agents": [
                        a
                        for a in agents
                        if (a.get("repository_name") or self.settings.gitea_repo) == name
                    ],
                }
                await child.enrich(group)
                reports.append(group["gitea"])
            data["gitea"] = {
                "repository_url": str(self.settings.gitea_url).rstrip("/")
                + "/"
                + self.settings.gitea_owner,
                "updated_at": min(
                    (r["updated_at"] for r in reports if r["updated_at"]), default=None
                ),
                "unavailable": any(r["unavailable"] for r in reports),
            }
            return
        key = (data["workspace"]["since"], *(a["id"] for a in agents))
        repo = (
            str(self.settings.gitea_url).rstrip("/")
            + "/"
            + self.settings.gitea_owner
            + "/"
            + self.settings.gitea_repo
        )
        creators = {
            str(n): a["id"]
            for a in agents
            for n in a.get("git_pr_numbers") or []
            if not a.get("repository_name")
        }
        branches = {"agents/" + a["id"]: a["id"] for a in agents}
        async with self.lock:
            if key != self.key or time.monotonic() >= self.until:
                try:
                    counts = {a["id"]: {"open_prs": 0, "merged_prs": 0} for a in agents}
                    async with asyncio.timeout(5):
                        page = 1
                        seen: set[int] = set()
                        while True:
                            result = await request_repository(
                                self.settings,
                                "GET",
                                f"/pulls?state=all&sort=oldest&limit=50&page={page}",
                            )
                            if result.get("error"):
                                raise ValueError("Gitea unavailable")
                            pulls = result["data"]
                            for pull in pulls:
                                if pull["number"] in seen:
                                    continue
                                seen.add(pull["number"])
                                actor = creators.get(str(pull["number"])) or branches.get(
                                    pull.get("head", {}).get("ref", "")
                                )
                                if actor:
                                    if pull.get("merged"):
                                        counts[actor]["merged_prs"] += 1
                                    elif pull["state"] == "open":
                                        counts[actor]["open_prs"] += 1
                            if len(pulls) < 50:
                                break
                            page += 1
                        remote_branches: set[str] = set()
                        remote_commits: set[str] = set()
                        page = 1
                        while True:
                            result = await request_repository(
                                self.settings, "GET", f"/branches?limit=50&page={page}"
                            )
                            if result.get("error"):
                                raise ValueError("Gitea branches unavailable")
                            for branch in result["data"]:
                                remote_branches.add(branch["name"])
                                if commit := branch.get("commit", {}).get("id"):
                                    remote_commits.add(commit)
                            if len(result["data"]) < 50:
                                break
                            page += 1
                    self.counts, self.at = counts, datetime.now(UTC).isoformat()
                    self.branches, self.commits = remote_branches, remote_commits
                    self.unavailable = False
                except Exception:
                    self.unavailable = True
                    logging.getLogger(__name__).warning("Analytics Gitea metrics unavailable")
                    if key != self.key:
                        self.counts, self.at = {}, None
                        self.branches, self.commits = None, set()
                self.key, self.until = key, time.monotonic() + 10
            for agent in agents:
                branch = agent.get("current_branch") or (
                    agent.get("assigned_branch") if not agent.get("branch_observed_at") else None
                )
                commit = agent.get("current_commit")
                published = branch in self.branches if self.branches is not None else None
                agent_counts = {
                    name: self.counts.get(agent["id"], {}).get(name)
                    for name in ["open_prs", "merged_prs"]
                }
                agent["git"] = {
                    **agent_counts,
                    "issues_created": int(agent["issues_created"]),
                    "branch": branch,
                    "branch_observed_at": agent["branch_observed_at"],
                    "branch_source": "observed" if agent["branch_observed_at"] else "assigned",
                    "branch_published": published,
                    "branch_url": repo + "/src/branch/" + quote(branch, safe="")
                    if branch and published
                    else None,
                    "commit": commit,
                    "commit_url": repo + "/src/commit/" + commit
                    if commit in self.commits
                    else None,
                    "pulls_url": repo + "/pulls",
                    "issues_url": repo + "/issues",
                }
                agent.pop("git_pr_numbers", None)
            data["gitea"] = {
                "repository_url": repo,
                "updated_at": self.at,
                "unavailable": self.unavailable,
                "attribution": "PR creators from tool receipts or agent head branches; "
                "issues count confirmed creations through the Gitea tool.",
            }
