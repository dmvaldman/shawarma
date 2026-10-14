"""Owner-requested presentation and cross-provider spawning acceptance."""

import asyncio
import os
from uuid import UUID

import pytest

from harness.cli.main import STATE
from harness.config import Settings
from harness.mattermost.client import Client
from harness.persistence.bootstrap import load
from harness.persistence.store import Store, decode

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("HARNESS_IDENTITY_LIVE_TESTS") != "1",
        reason="paid running-supervisor identity gate",
    ),
]


async def test_live_names_channels_prompt_versions_cached_discovery_and_multi_model_spawn():
    settings = Settings()
    bootstrap = load(STATE)
    store = await Store.connect(settings)
    workspace = UUID(bootstrap["workspace_id"])
    try:
        before = {a.id for a in await store.list_agents(workspace)}
        async with Client(
            str(settings.mattermost_url), settings.mattermost_admin_token.get_secret_value()
        ) as owner:
            team = await owner.request("GET", f"teams/{bootstrap['team_id']}")
            assert team["display_name"] == "My Swarm"
            channels = await owner.request("GET", f"teams/{bootstrap['team_id']}/channels")
            assert {c["name"] for c in channels} == {"town-square", "off-topic"}
            for a in await store.list_agents(workspace):
                assert "## Communication Etiquette" in await store.effective_prompt(a.id)
            existing = os.environ.get("HARNESS_IDENTITY_TEST_POST_ID")
            if existing:
                question = await owner.request("GET", f"posts/{existing}")
                assert question["user_id"] == settings.mattermost_owner_id
                before = {
                    r["id"]
                    for r in await store.pool.fetch(
                        "SELECT id FROM agents WHERE workspace_id=$1 AND birth_ms<$2",
                        workspace,
                        question["create_at"],
                    )
                }
            else:
                question = await owner.post_channel(
                    bootstrap["general_id"],
                    (
                        "Only @"
                        + bootstrap["bot_username"]
                        + " should handle this check; other agents should "
                        "stay idle. First terminate your previous QUOTA_BLOCKED direct children. "
                        "Use list_models, then create exactly two short-lived peers, each with "
                        "Task: "
                        "name audit, model anthropic:claude-haiku-5-5; and name probe, model "
                        "deepseek:deepseek-flash. Give each this task: "
                        "post a new top-level message "
                        "in #town-square containing the exact marker IDENTITY-OK, "
                        "your provider/model, "
                        "and 7 times 3 = 21; then terminate yourself. Keep their roles minimal. "
                        "After spawning both, post a brief new channel message and become idle."
                    ),
                )
            async with asyncio.timeout(120):
                while True:  # noqa: ASYNC110
                    rows = await store.pool.fetch(
                        "SELECT a.id,a.model_id,p.username FROM agents a JOIN agent_profiles p "
                        "ON p.agent_id=a.id WHERE a.workspace_id=$1",
                        workspace,
                    )
                    created = [
                        r
                        for r in rows
                        if r["id"] not in before
                        and (await store.get(r["id"])).parent_id == UUID(bootstrap["seed_id"])
                    ]
                    if {"anthropic:claude-haiku-5-5", "deepseek:deepseek-flash"} <= {
                        r["model_id"] for r in created
                    }:
                        break
                    await asyncio.sleep(0.3)
            models = {r["model_id"] for r in created}
            assert {"anthropic:claude-haiku-5-5", "deepseek:deepseek-flash"} <= models
            assert any(r["username"].startswith("audit") for r in created)
            assert any(r["username"].startswith("probe") for r in created)
            async with asyncio.timeout(90):
                while True:  # noqa: ASYNC110
                    posts = await owner.read_channel(bootstrap["general_id"], 50)
                    new_users = {(await store.get(r["id"])).mattermost_user_id for r in created}
                    replies = [
                        p for p in posts if p.user_id in new_users and "IDENTITY-OK" in p.message
                    ]
                    if {p.user_id for p in replies} == new_users:
                        break
                    await asyncio.sleep(0.3)
            assert all(not p.root_id for p in replies)
            row = await store.pool.fetchval(
                "SELECT result FROM tool_calls WHERE agent_id=$1 AND tool_name='list_models' "
                "AND state='completed' ORDER BY id DESC LIMIT 1",
                UUID(bootstrap["seed_id"]),
            )
            result = decode(row)
            assert set(result["providers"]) == {"openai", "anthropic", "deepseek"}
            assert result["cached"]
    finally:
        await store.close()
