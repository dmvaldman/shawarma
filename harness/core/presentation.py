"""Owner-requested presentation/prompt upgrade; immutable birth records stay intact."""

import json
from pathlib import Path
from typing import Any
from uuid import UUID

from harness.config import Settings
from harness.core.names import agent_bio, owned_account, role_name
from harness.core.prompts import birth_record
from harness.mattermost.bot_reminders import remove_setup_reminder
from harness.mattermost.client import Client
from harness.mattermost.provisioning import available_name, membership
from harness.models.catalog import get_catalog
from harness.persistence.bootstrap import save
from harness.persistence.store import Store


async def join_owner_channels(
    store: Store, admin: Client, workspace: UUID, team_id: str, owner_id: str
) -> None:
    channels: dict[str, dict[str, Any]] = {}
    page = 0
    while True:
        public = await admin.request(
            "GET", f"teams/{team_id}/channels", params={"page": page, "per_page": 200}
        )
        channels.update({channel["id"]: channel for channel in public})
        if len(public) < 200:
            break
        page += 1
    # The team listing contains public channels. Harness subscriptions also
    # discover private work channels created before owner auto-membership.
    subscribed = await store.pool.fetch(
        "SELECT DISTINCT s.channel_id FROM subscriptions s JOIN agents a "
        "ON a.id=s.agent_id WHERE a.workspace_id=$1",
        workspace,
    )
    for row in subscribed:
        if row["channel_id"] not in channels:
            channel = await admin.request("GET", f"channels/{row['channel_id']}")
            channels[channel["id"]] = channel
    for channel in channels.values():
        if (
            channel["team_id"] == team_id
            and channel["type"] in {"O", "P"}
            and not channel.get("delete_at")
        ):
            await admin.request(
                "POST", f"channels/{channel['id']}/members", json={"user_id": owner_id}
            )


async def configure_workspace(
    store: Store, settings: Settings, bootstrap: dict[str, Any], path: Path
) -> None:
    from harness.core.diagnostics import ensure_errors_channel

    workspace = UUID(bootstrap["workspace_id"])
    async with Client(
        str(settings.mattermost_url), settings.mattermost_admin_token.get_secret_value()
    ) as admin:
        bootstrap["errors_id"] = await ensure_errors_channel(
            admin, bootstrap["team_id"], settings.mattermost_owner_id
        )
        admin_user = await admin.me()
        team = await admin.request("GET", f"teams/{bootstrap['team_id']}")
        if team["display_name"] != settings.workspace_name:
            await admin.request(
                "PUT", f"teams/{team['id']}", json={**team, "display_name": settings.workspace_name}
            )
        channel = await admin.request(
            "GET", f"teams/{team['id']}/channels/name/{settings.coordination_channel_name}"
        )
        await join_owner_channels(store, admin, workspace, team["id"], settings.mattermost_owner_id)
        old_main = bootstrap.pop("previous_general_id", bootstrap["general_id"])
        # Record the new mandatory main before changing the old subscription.
        async with store.pool.acquire() as c, c.transaction():
            await c.execute(
                "UPDATE workspaces SET general_id=$2,name=$3 WHERE id=$1",
                workspace,
                channel["id"],
                settings.workspace_name,
            )
            await c.execute(
                "INSERT INTO subscriptions(agent_id,channel_id,mode) "
                "SELECT id,$2,'all' FROM agents WHERE workspace_id=$1 "
                "ON CONFLICT(agent_id,channel_id) DO UPDATE SET mode='all'",
                workspace,
                channel["id"],
            )
            if old_main != channel["id"]:
                await c.execute(
                    "UPDATE subscriptions SET mode='none' WHERE channel_id=$1 "
                    "AND agent_id IN (SELECT id FROM agents WHERE workspace_id=$2)",
                    old_main,
                    workspace,
                )
        if old_main != channel["id"]:
            old = await admin.request("GET", f"channels/{old_main}")
            if not old.get("delete_at"):
                await admin.request("DELETE", f"channels/{old_main}")
        # Historical DMs remain in Mattermost; stop pending DM inbox delivery.
        await store.pool.execute(
            "UPDATE notifications SET state='consumed',reserved_turn=NULL "
            "WHERE kind='dm' AND state!='consumed' AND agent_id IN "
            "(SELECT id FROM agents WHERE workspace_id=$1)",
            workspace,
        )
        bootstrap["general_id"] = channel["id"]
        bootstrap["coordination_channel_name"] = channel["name"]
        off_topic = await admin.request("GET", f"teams/{team['id']}/channels/name/off-topic")
        bootstrap["off_topic_id"] = off_topic["id"]
        for identity in await store.list_agents(workspace):
            state = await store.state(identity.id)
            metadata = await get_catalog(settings).resolve(state["model_id"])
            public = await store.public_profile(identity.id)
            role = public.get("role_name") or role_name(
                identity.role_instructions, seed=identity.parent_id is None
            )
            account = await admin.request(
                "GET", f"bots/{identity.mattermost_user_id}", params={"include_deleted": "true"}
            )
            if not owned_account(account, identity.mattermost_user_id, admin_user["id"]):
                raise ValueError("BOT_OWNERSHIP_MISMATCH")
            await remove_setup_reminder(admin, identity.mattermost_user_id, admin_user["id"])
            if state["state"] != "TERMINATED":
                await membership(admin, team["id"], off_topic["id"], identity.mattermost_user_id)
                await store.pool.execute(
                    "INSERT INTO subscriptions VALUES($1,$2,'all') "
                    "ON CONFLICT(agent_id,channel_id) DO UPDATE SET mode='all'",
                    identity.id,
                    off_topic["id"],
                )
            username = await available_name(
                store,
                admin,
                settings.mattermost_server_id,
                identity.id,
                role,
                identity.mattermost_user_id,
            )
            display = username
            user = await admin.request("GET", f"users/{identity.mattermost_user_id}")
            if user["username"] != username:
                await admin.request("PUT", f"users/{user['id']}/patch", json={"username": username})
            await admin.request(
                "PUT",
                f"bots/{user['id']}",
                json={
                    "display_name": display,
                    "description": agent_bio(
                        workspace,
                        identity.id,
                        "seed" if identity.parent_id is None else "child",
                        identity.role_instructions,
                        metadata,
                    ),
                },
            )
            await store.pool.execute(
                "INSERT INTO agent_profiles VALUES($1,$2,$3,$4,$5) ON CONFLICT(agent_id) "
                "DO UPDATE SET username=EXCLUDED.username,display_name=EXCLUDED.display_name, "
                "role_name=EXCLUDED.role_name,model_metadata=EXCLUDED.model_metadata",
                identity.id,
                username,
                display,
                role,
                json.dumps(metadata),
            )
            prompt = birth_record(
                agent_id=identity.id,
                workspace_id=workspace,
                parent_id=identity.parent_id,
                username=username,
                mm_user_id=identity.mattermost_user_id,
                settings=settings,
                role_instructions=identity.role_instructions,
                inference_enabled=True,
                coordination_enabled=True,
            )
            if identity.id == UUID(bootstrap["seed_id"]):
                bootstrap["bot_username"] = username
            if settings.owner_prompt_revision != prompt.template_revision:
                continue
            await store.pool.execute(
                "INSERT INTO agent_prompt_versions(agent_id,revision,system_prompt) "
                "VALUES($1,$2,$3) "
                "ON CONFLICT DO NOTHING",
                identity.id,
                prompt.template_revision,
                prompt.system_prompt,
            )
        from harness.mattermost.welcome import ensure_welcome

        async with Client(str(settings.mattermost_url), bootstrap["bot_token"]) as seed_bot:
            await ensure_welcome(seed_bot, bootstrap, path)
        save(path, bootstrap)
