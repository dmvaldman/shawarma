"""No-inference API round trip in an isolated disposable repository."""

import asyncio
import base64
import json
import os
from pathlib import Path
from uuid import uuid4

import httpx
import pytest

from harness.config import Settings
from harness.tools.gitea import request_repository

pytestmark = pytest.mark.skipif(
    os.environ.get("HARNESS_GITEA_LIVE_TESTS") != "1", reason="real Gitea API gate"
)


async def test_issue_pr_comment_merge_and_main_contents():
    settings = Settings()
    state = json.loads(await asyncio.to_thread(Path(".state/coding.json").read_text))
    repo = "workflow-test-" + uuid4().hex[:12]
    settings = settings.model_copy(update={"gitea_repo": repo})
    auth = (settings.gitea_owner, state["service_password"])
    async with httpx.AsyncClient(
        base_url=str(settings.gitea_url).rstrip("/") + "/api/v1/", auth=auth, timeout=20
    ) as owner:
        created = await owner.post(
            "user/repos",
            json={"name": repo, "private": True, "auto_init": True, "default_branch": "main"},
        )
        created.raise_for_status()
        try:

            async def api(method, path, body=None):
                result = await request_repository(
                    settings, method, path, json.dumps(body) if body is not None else None
                )
                assert "error" not in result, result
                return result["data"]

            issue = await api(
                "POST",
                "/issues",
                {
                    "title": "Verify workflow",
                    "body": "Investigator: @probe (Mattermost). "
                    "Acceptance: contents merged to main.",
                },
            )
            await api(
                "POST", "/branches", {"new_branch_name": "agents/probe", "old_branch_name": "main"}
            )
            await api(
                "POST",
                "/contents/verified.txt",
                {
                    "branch": "agents/probe",
                    "message": "Verify repository workflow",
                    "content": base64.b64encode(b"verified\n").decode(),
                },
            )
            pr = await api(
                "POST",
                "/pulls",
                {
                    "title": "Verify repository workflow",
                    "head": "agents/probe",
                    "base": "main",
                    "body": f"Related issue #{issue['number']}",
                },
            )
            await api(
                "POST",
                f"/issues/{pr['number']}/comments",
                {
                    "body": "API verification: expected one-file change; "
                    "check main contents after merge."
                },
            )
            for _ in range(40):
                current = await api("GET", f"/pulls/{pr['number']}")
                if current.get("mergeable"):
                    break
                await asyncio.sleep(0.25)
            assert current.get("mergeable"), current.get("mergeable_state")
            await api(
                "POST",
                f"/pulls/{pr['number']}/merge",
                {"Do": "merge", "delete_branch_after_merge": False},
            )
            contents = await api("GET", "/contents/verified.txt?ref=main")
            assert base64.b64decode(contents["content"]) == b"verified\n"
            await api("PATCH", f"/issues/{issue['number']}", {"state": "closed"})
            assert (await api("GET", f"/pulls/{pr['number']}"))["merged"]
        finally:
            deleted = await owner.delete(f"repos/{settings.gitea_owner}/{repo}")
            deleted.raise_for_status()
