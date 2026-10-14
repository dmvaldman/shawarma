"""Real accounts, native review assignment/approval and reviewer merge; no inference."""

import asyncio
import base64
import json
import os
from pathlib import Path
from uuid import uuid4

import httpx
import pytest

from harness.coding.identities import identity
from harness.config import Settings
from harness.tools.gitea import request_repository

pytestmark = pytest.mark.skipif(
    os.environ.get("HARNESS_GITEA_LIVE_TESTS") != "1", reason="Gitea gate"
)


async def test_distinct_agents_native_review_approval_and_reviewer_merge(runtime_store):
    store, _, author, workspace = runtime_store
    reviewer = uuid4()
    await store.pool.execute(
        "INSERT INTO agents(id,workspace_id,parent_id,identity,model_id,state) "
        "SELECT $1,workspace_id,id,identity,model_id,'READY' FROM agents WHERE id=$2",
        reviewer,
        author,
    )
    for agent in (author, reviewer):
        await store.pool.execute(
            "INSERT INTO agent_profiles(agent_id,username,display_name,role_name,model_metadata) "
            "VALUES($1,$2,$2,'test','{}') ON CONFLICT(agent_id) DO UPDATE SET username=$2",
            agent,
            "probe-" + agent.hex[:12],
        )
    repo = "identity-test-" + uuid4().hex[:12]
    settings = Settings().model_copy(update={"gitea_repo": repo})
    checkpoint = json.loads(await asyncio.to_thread(Path(".state/coding.json").read_text))
    names = []
    async with httpx.AsyncClient(
        base_url=str(settings.gitea_url).rstrip("/") + "/api/v1/",
        auth=("swarm-owner", checkpoint["admin_password"]),
    ) as admin:
        created = await admin.post(
            "admin/users/swarm/repos",
            json={"name": repo, "private": True, "auto_init": True, "default_branch": "main"},
        )
        created.raise_for_status()
        try:
            a, b = await asyncio.gather(
                identity(store, settings, author), identity(store, settings, reviewer)
            )
            names = [a["username"], b["username"]]
            assert a["username"] == "probe-" + author.hex[:12]
            old_id = (await admin.get("users/" + a["username"])).json()["id"]
            legacy = a["username"] + "-legacy"
            (
                await admin.post(
                    "admin/users/" + a["username"] + "/rename", json={"new_username": legacy}
                )
            ).raise_for_status()
            await store.pool.execute(
                "UPDATE gitea_identities SET username=$2 WHERE agent_id=$1", author, legacy
            )
            restored = await identity(store, settings, author)
            assert restored["username"] == a["username"] and restored["token"] == a["token"]
            assert (await admin.get("users/" + a["username"])).json()["id"] == old_id

            assert a["token"] != b["token"] and a["username"] != b["username"]
            assert (await identity(store, settings, author))["token"] == a["token"]

            async def api(account, method, path, body=None):
                r = await request_repository(
                    settings,
                    method,
                    path,
                    json.dumps(body) if body is not None else None,
                    token=account["token"],
                )
                assert "error" not in r, r
                return r["data"]

            from harness.tools.wiki import read_page, write_page

            async def wiki_api(method, path, body=None):
                return await request_repository(settings, method, path, body, token=a["token"])

            created_page = await write_page(
                wiki_api, "Home", "# Findings\nVerified evidence.", "", "Initialize synthesis"
            )
            assert "error" not in created_page, created_page
            page = await read_page(wiki_api, "Home")
            assert page["markdown"] == "# Findings\nVerified evidence."
            updated = await write_page(
                wiki_api,
                "Home",
                "# Findings\nFailed routes and evidence.",
                page["revision"],
                "Record failed approach",
            )
            assert "error" not in updated, updated
            conflict = await write_page(
                wiki_api, "Home", "Stale overwrite", page["revision"], "Stale edit"
            )
            assert conflict["error"] == "WIKI_REVISION_CONFLICT"
            assert (await read_page(wiki_api, "Home"))["markdown"].endswith(
                "Failed routes and evidence."
            )
            issue = await api(a, "POST", "/issues", {"title": "Identity test"})
            assert issue["user"]["login"] == a["username"]
            await api(
                a, "POST", "/branches", {"new_branch_name": "test", "old_branch_name": "main"}
            )
            await api(
                a,
                "POST",
                "/contents/result.txt",
                {
                    "branch": "test",
                    "message": "Test",
                    "content": base64.b64encode(b"verified\n").decode(),
                },
            )
            pr = await api(
                a, "POST", "/pulls", {"head": "test", "base": "main", "title": "Native review test"}
            )
            assert pr["user"]["login"] == a["username"]
            n = pr["number"]
            await api(a, "POST", f"/pulls/{n}/requested_reviewers", {"reviewers": [b["username"]]})
            reviews = await api(b, "GET", f"/pulls/{n}/reviews")
            assert any(
                r["user"]["login"] == b["username"] and r["state"] == "REQUEST_REVIEW"
                for r in reviews
            )
            await api(
                b,
                "POST",
                f"/pulls/{n}/reviews",
                {"event": "APPROVED", "body": "Checked actual contents."},
            )
            reviews = await api(b, "GET", f"/pulls/{n}/reviews")
            assert any(
                r["user"]["login"] == b["username"] and r["state"] == "APPROVED" for r in reviews
            )
            for _ in range(30):
                if (await api(b, "GET", f"/pulls/{n}")).get("mergeable"):
                    break
                await asyncio.sleep(0.2)
            await api(
                b, "POST", f"/pulls/{n}/merge", {"Do": "merge", "delete_branch_after_merge": False}
            )
            merged = await api(a, "GET", f"/pulls/{n}")
            assert merged["merged"] and merged["merged_by"]["login"] == b["username"]
        finally:
            (await admin.delete(f"repos/swarm/{repo}")).raise_for_status()
            # Include partially provisioned accounts after any failed assertion.
            names = [
                r["username"]
                for r in await store.pool.fetch("SELECT username FROM gitea_identities")
            ]
            for name in names:
                response = await admin.delete("admin/users/" + name)
                if response.status_code != 404:
                    response.raise_for_status()
