from unittest.mock import AsyncMock

from harness.api.git_stats import GitStats
from harness.config import Settings


def snapshot(origin="run-one", agent="agent"):
    return {
        "workspace": {"since": origin},
        "agents": [
            {
                "id": agent,
                "git_pr_numbers": [1],
                "issues_created": 2,
                "current_branch": "agents/" + agent,
                "assigned_branch": "agents/" + agent,
                "current_commit": "a" * 40,
                "branch_observed_at": "2026-10-13T00:00:00Z",
            }
        ],
    }


async def test_git_counts_are_cached_stale_on_outage_and_reset_scope_is_invalidated(monkeypatch):
    settings = Settings(_env_file=None, mattermost_url="http://mm", database_url="postgres://db")
    api = AsyncMock(
        return_value={
            "data": [
                {"number": 1, "state": "open", "merged": False, "head": {"ref": "agents/agent"}}
            ]
        }
    )
    payload = api.return_value

    async def catalog(*args):
        return {"data": [{"name": "agents/agent"}]} if args[2].startswith("/branches") else payload

    api.side_effect = catalog
    monkeypatch.setattr("harness.api.git_stats.request_repository", api)
    stats = GitStats(settings)
    first = snapshot()
    await stats.enrich(first)
    await stats.enrich(snapshot())
    assert api.call_count == 2
    assert first["agents"][0]["git"]["open_prs"] == 1
    stats.until = 0
    api.side_effect = OSError("offline")
    stale = snapshot()
    await stats.enrich(stale)
    assert stale["gitea"]["unavailable"]
    assert stale["agents"][0]["git"]["open_prs"] == 1
    fresh = snapshot("run-two", "fresh-seed")
    await stats.enrich(fresh)
    assert fresh["agents"][0]["git"]["open_prs"] is None
    assert fresh["gitea"]["updated_at"] is None


async def test_git_pr_pagination_and_closed_unmerged_is_not_merged(monkeypatch):
    settings = Settings(_env_file=None, mattermost_url="http://mm", database_url="postgres://db")
    page = [
        {"number": n, "state": "closed", "merged": False, "head": {"ref": "agents/agent"}}
        for n in range(50)
    ]
    api = AsyncMock(
        side_effect=[
            {"data": page},
            {
                "data": [
                    {
                        "number": 51,
                        "state": "closed",
                        "merged": True,
                        "head": {"ref": "agents/agent"},
                    }
                ]
            },
        ]
    )
    monkeypatch.setattr("harness.api.git_stats.request_repository", api)
    api.side_effect = [*api.side_effect, {"data": [{"name": "agents/agent"}]}]
    data = snapshot()
    await GitStats(settings).enrich(data)
    assert api.call_count == 3
    assert data["agents"][0]["git"]["merged_prs"] == 1
    assert data["agents"][0]["git"]["open_prs"] == 0


async def test_unpublished_branch_and_unknown_detached_commit_have_no_broken_links(monkeypatch):
    settings = Settings(_env_file=None, mattermost_url="http://mm", database_url="postgres://db")
    monkeypatch.setattr(
        "harness.api.git_stats.request_repository", AsyncMock(return_value={"data": []})
    )
    stats = GitStats(settings)
    data = snapshot()
    await stats.enrich(data)
    assert data["agents"][0]["git"]["branch_published"] is False
    assert data["agents"][0]["git"]["branch_url"] is None
    detached = snapshot()
    detached["agents"][0]["current_branch"] = None
    await stats.enrich(detached)
    assert detached["agents"][0]["git"]["commit_url"] is None


async def test_topic_repository_links_and_identical_pr_numbers_are_isolated(monkeypatch):
    settings = Settings(_env_file=None, mattermost_url="http://mm", database_url="postgres://db")

    async def api(target, method, path):
        if path.startswith("/branches"):
            return {"data": [{"name": "agents/" + target.gitea_repo}]}
        return {
            "data": [
                {
                    "number": 1,
                    "state": "open",
                    "merged": target.gitea_repo == "graphs",
                    "head": {"ref": "agents/" + target.gitea_repo},
                }
            ]
        }

    monkeypatch.setattr("harness.api.git_stats.request_repository", api)
    data = snapshot(agent="collatz")
    second = snapshot(agent="graphs")["agents"][0]
    data["agents"].append(second)
    for agent in data["agents"]:
        agent["repository_name"] = agent["id"]
    await GitStats(settings).enrich(data)
    first, second = data["agents"]
    assert first["git"]["open_prs"] == 1 and first["git"]["merged_prs"] == 0
    assert second["git"]["open_prs"] == 0 and second["git"]["merged_prs"] == 1
    assert "/collatz/src/branch/" in first["git"]["branch_url"]
    assert "/graphs/src/branch/" in second["git"]["branch_url"]
