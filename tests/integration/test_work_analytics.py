import json
import os
from decimal import Decimal
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from harness.api.analytics import ledger_snapshot
from harness.api.git_stats import GitStats
from harness.core.prompts import birth_record
from harness.core.types import Post
from harness.mattermost.client import Client
from harness.tools.core import CoreTools

pytestmark = pytest.mark.skipif(
    os.environ.get("HARNESS_LIVE_TESTS") != "1", reason="PostgreSQL gate"
)


async def test_live_tool_surface_git_head_and_confirmed_creations(runtime_store, monkeypatch):
    store, settings, seed, workspace = runtime_store
    agent = uuid4()
    identity = birth_record(
        agent_id=agent,
        workspace_id=workspace,
        parent_id=seed,
        username="worker",
        mm_user_id="worker",
        settings=settings,
    )
    await store.pool.execute(
        "INSERT INTO agents(id,workspace_id,identity,model_id,parent_id) VALUES($1,$2,$3,$4,$5)",
        agent,
        workspace,
        identity.model_dump_json(),
        settings.openai_model_id,
        seed,
    )
    await store.pool.execute("UPDATE workspaces SET started=true WHERE id=$1", workspace)
    await store.set_state(agent, "RUNNING")
    await store.pool.execute(
        "INSERT INTO coding_sessions(agent_id,session_id,directory,branch,worker_id,"
        "current_branch,current_tool,branch_observed_at) "
        "VALUES($1,'session','directory',$2,'worker','feature/math','bash',now())",
        agent,
        "agents/" + str(agent),
    )
    turn = await store.reserve_cost(agent, Decimal(0), [])
    await store.pool.execute(
        "INSERT INTO coding_runs(id,agent_id,call_id,turn_id) VALUES($1,$2,'native',$3)",
        uuid4(),
        agent,
        turn,
    )
    for call, path, data in [
        ("issue", "/issues", {"id": 100, "number": 1}),
        ("duplicate", "/issues", {"id": 100, "number": 1}),
        ("pr", "/pulls", {"id": 101, "number": 17}),
    ]:
        await store.pool.execute(
            "INSERT INTO tool_calls(id,agent_id,call_id,tool_name,arguments,result,state) "
            "VALUES($1,$2,$3,'gitea_request',$4,$5,'completed')",
            uuid4(),
            agent,
            call,
            json.dumps({"method": "POST", "path": path}),
            json.dumps({"status": 201, "data": data}),
        )
    snapshot = await ledger_snapshot(store, workspace, "opencode")
    row = next(a for a in snapshot["agents"] if a["id"] == str(agent))
    assert row["activity_label"] == "Working in OpenCode"
    assert row["current_tool"] == "bash"
    assert row["issues_created"] == 1

    async def pulls(*args):
        if args[2].startswith("/branches"):
            return {"data": [{"name": "feature/math"}]}
        return {
            "data": [
                {"number": 17, "merged": True, "state": "closed", "head": {"ref": "feature/math"}},
                {
                    "number": 18,
                    "merged": False,
                    "state": "open",
                    "head": {"ref": "agents/" + str(agent)},
                },
                {"number": 19, "merged": True, "state": "closed", "head": {"ref": "unrelated"}},
            ]
        }

    monkeypatch.setattr("harness.api.git_stats.request_repository", pulls)
    await GitStats(settings).enrich(snapshot)
    assert row["git"]["open_prs"] == row["git"]["merged_prs"] == 1
    assert row["git"]["branch"] == "feature/math"
    assert row["git"]["branch_url"].endswith("/src/branch/feature%2Fmath")
    await store.pool.execute(
        "INSERT INTO tool_calls(id,agent_id,call_id,tool_name,arguments,state) "
        "VALUES($1,$2,'posting','post_channel','{}','pending')",
        uuid4(),
        agent,
    )
    assert (
        next(
            a
            for a in (await ledger_snapshot(store, workspace, "opencode"))["agents"]
            if a["id"] == str(agent)
        )
    )["activity_label"] == ("Working in Mattermost")
    await store.pool.execute("UPDATE coding_runs SET state='completed'")
    assert (
        next(
            a
            for a in (await ledger_snapshot(store, workspace, "opencode"))["agents"]
            if a["id"] == str(agent)
        )
    )["activity"] == "waiting"
    await store.pool.execute("UPDATE workspaces SET state='paused' WHERE id=$1", workspace)
    assert (
        next(
            a
            for a in (await ledger_snapshot(store, workspace, "opencode"))["agents"]
            if a["id"] == str(agent)
        )
    )["activity"] == "paused"


async def test_review_assignment_validates_peer_records_comment_and_notifies(
    runtime_store, monkeypatch
):
    store, settings, agent, workspace = runtime_store
    reviewer = uuid4()
    identity = birth_record(
        agent_id=reviewer,
        workspace_id=workspace,
        parent_id=agent,
        username="audit",
        mm_user_id="review-bot",
        settings=settings,
    )
    await store.pool.execute(
        "INSERT INTO agents(id,workspace_id,identity,model_id,parent_id) VALUES($1,$2,$3,$4,$5)",
        reviewer,
        workspace,
        identity.model_dump_json(),
        settings.openai_model_id,
        agent,
    )
    bot = Client("http://mm.invalid", "token")
    bot.request = AsyncMock(return_value={"id": "work", "team_id": "team", "type": "O"})
    bot.post_channel = AsyncMock(
        return_value=Post(
            id="review-message",
            channel_id="work",
            user_id="bot",
            message="Review requested",
            create_at=1000,
        )
    )
    api = AsyncMock(
        side_effect=[
            {"data": {"html_url": "http://gitea/swarm/workspace/pulls/3"}},
            {"status": 201, "data": []},
            {"data": {"html_url": "http://gitea/swarm/workspace/pulls/3#comment-1"}},
        ]
    )
    monkeypatch.setattr("harness.tools.core.request_repository", api)

    async def account(store, settings, target):
        return {"username": "audit" if target == reviewer else "seed", "token": "actor-secret"}

    monkeypatch.setattr("harness.tools.core.gitea_identity", account)
    tools = CoreTools(store, bot, agent, settings)
    turn = await store.reserve_cost(agent, Decimal(0), [])
    args = {
        "pr_number": 3,
        "reviewer_agent_id": str(reviewer),
        "channel_id": "work",
        "message": "Check the proof and reproduce the experiment.",
    }
    try:
        result = await tools.execute(
            turn,
            {"name": "gitea_request_review", "call_id": "review", "arguments": json.dumps(args)},
        )
        assert result["reviewer"] == "audit"
        assert result["native_review_requested"]
        assert api.await_args_list[1].args[2] == "/pulls/3/requested_reviewers"
        assert json.loads(api.await_args_list[1].args[3]) == {"reviewers": ["audit"]}
        assert all(call.kwargs["token"] == "actor-secret" for call in api.await_args_list)
        assert "@audit" in bot.post_channel.call_args.args[1]
        assert "Review requested from @audit" in api.call_args.args[3]
        assert (
            await store.pool.fetchval(
                "SELECT mode FROM subscriptions WHERE agent_id=$1 AND channel_id='work'", reviewer
            )
            == "all"
        )
        assert (await ledger_snapshot(store, workspace))["totals"]["messages"] == 1
        args["reviewer_agent_id"] = str(agent)
        rejected = await tools.execute(
            turn, {"name": "gitea_request_review", "call_id": "self", "arguments": json.dumps(args)}
        )
        assert "INDEPENDENT_REVIEW" in rejected["detail"]
        assert api.call_count == 3
    finally:
        await bot.http.aclose()
