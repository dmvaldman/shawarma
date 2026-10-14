"""Mattermost service reset: preserve default channel identities, erase old content."""

from copy import deepcopy
from pathlib import Path
from typing import Any
from uuid import UUID

from harness.config import Settings
from harness.core.names import owned_account, owns_bot
from harness.mattermost.client import Client, MattermostError
from harness.mattermost.provisioning import available_name, find_or_create, provision_seed
from harness.persistence.bootstrap import save
from harness.persistence.store import Store


async def snapshot(store: Store, admin: Client, state: dict[str, Any]) -> dict[str, Any]:
    workspace = UUID(state["workspace_id"])
    admin_id = (await admin.me())["id"]
    agents = await store.list_agents(workspace)
    ids = [str(a.id) for a in agents]
    bots = {a.mattermost_user_id for a in agents}
    pending = await store.pool.fetch(
        "SELECT s.child_id,s.bot_username FROM spawn_requests s JOIN agents a ON a.id=s.parent_id "
        "WHERE a.workspace_id=$1",
        workspace,
    )
    for row in pending:
        ids.append(str(row["child_id"]))
        if row["bot_username"]:
            try:
                user = await admin.request("GET", f"users/username/{row['bot_username']}")
                bot = await admin.request(
                    "GET", f"bots/{user['id']}", params={"include_deleted": "true"}
                )
            except MattermostError as error:
                if error.status == 404:
                    continue
                raise
            if owns_bot(bot.get("description", ""), workspace, row["child_id"], "child"):
                bots.add(user["id"])
    # Validate the complete bot manifest before making any destructive API call.
    for bot_id in bots:
        if bot_id in {state["binding"]["owner_id"], admin_id}:
            raise ValueError("RESET_HUMAN_ACCOUNT_GUARD")
        try:
            user = await admin.request("GET", f"users/{bot_id}")
            bot = await admin.request("GET", f"bots/{bot_id}", params={"include_deleted": "true"})
        except MattermostError as error:
            if error.status == 404:
                continue  # Already removed during an interrupted reset.
            raise
        if not user.get("is_bot") or not owned_account(bot, bot_id, admin_id):
            raise ValueError("RESET_BOT_OWNERSHIP_MISMATCH")
        shared = await store.pool.fetchval(
            "SELECT EXISTS(SELECT 1 FROM agents WHERE workspace_id!=$1 "
            "AND identity->>'mattermost_user_id'=$2)",
            workspace,
            bot_id,
        )
        if shared:
            raise ValueError("RESET_SHARED_BOT")
    channels: list[str] = []
    preserve_channels: list[str] = []
    page = 0
    while True:
        batch = await admin.request(
            "GET", "channels", params={"page": page, "per_page": 200, "include_deleted": "true"}
        )
        channels.extend(
            c["id"] for c in batch if c["team_id"] == state["team_id"] and c["type"] in {"O", "P"}
        )
        preserve_channels.extend(
            channel["id"]
            for channel in batch
            if channel["team_id"] == state["team_id"]
            and channel["type"] == "O"
            and (channel["id"] == state["general_id"] or channel["name"] in {"off-topic", "errors"})
        )
        if len(batch) < 200:
            break
        page += 1
    other = await store.pool.fetchval(
        "SELECT EXISTS(SELECT 1 FROM workspaces WHERE id!=$1 AND team_id=$2)",
        workspace,
        state["team_id"],
    )
    if other:
        raise ValueError("RESET_SHARED_TEAM")
    return {
        "agents": ids,
        "bots": sorted(bots),
        "channels": channels,
        "preserve_channels": preserve_channels,
    }


async def clear_channel_posts(admin: Client, channel_id: str) -> None:
    """Permanently erase history while keeping the browser's channel identity valid."""
    posts: dict[str, Any] = {}
    page = 0
    while True:
        batch = await admin.request(
            "GET",
            f"channels/{channel_id}/posts",
            params={"page": page, "per_page": 200, "include_deleted": "true"},
        )
        posts.update(batch["posts"])
        if len(batch["order"]) < 200:
            break
        page += 1
    # Replies first: deleting a root can remove its replies as well.
    for post in sorted(posts.values(), key=lambda p: not bool(p.get("root_id"))):
        if post["channel_id"] != channel_id:
            raise ValueError("RESET_POST_SCOPE_MISMATCH")
        try:
            await admin.request("DELETE", f"posts/{post['id']}", params={"permanent": "true"})
        except MattermostError as error:
            if error.status != 404:
                raise


async def delete_external(admin: Client, state: dict[str, Any], path: Path) -> None:
    plan = state["reset"]
    if (
        not plan.get("fresh")
        and "staging_id" not in plan
        and not plan["manifest"].get("preserve_channels")
    ):
        # Mattermost cannot create a channel when its creator has zero team channels.
        staging = await find_or_create(
            admin,
            f"teams/{state['team_id']}/channels/name/reset-{plan['seed_id'][:12]}",
            "channels",
            {
                "team_id": state["team_id"],
                "name": "reset-" + plan["seed_id"][:12],
                "display_name": "Reset in progress",
                "type": "P",
            },
        )
        plan["staging_id"] = staging["id"]
        save(path, state)
    service = (await admin.request("GET", "config"))["ServiceSettings"]
    keys = ("EnableAPIChannelDeletion", "EnableAPIUserDeletion", "EnableAPIPostDeletion")
    if "deletion_settings" not in plan:
        plan["deletion_settings"] = {key: service.get(key, False) for key in keys}
        save(path, state)
    if "EnableAPIPostDeletion" not in plan["deletion_settings"]:
        plan["deletion_settings"]["EnableAPIPostDeletion"] = service.get(
            "EnableAPIPostDeletion", False
        )
        save(path, state)
    try:
        await admin.request(
            "PUT", "config/patch", json={"ServiceSettings": dict.fromkeys(keys, True)}
        )
        for channel_id in plan["manifest"]["channels"]:
            try:
                channel = await admin.request("GET", f"channels/{channel_id}")
                if channel["team_id"] != state["team_id"] or channel["type"] not in {"O", "P"}:
                    raise ValueError("RESET_CHANNEL_SCOPE_MISMATCH")
                if channel.get("delete_at"):
                    await admin.request("POST", f"channels/{channel_id}/restore")
                if channel_id in plan["manifest"].get("preserve_channels", []):
                    if channel["type"] != "O" or not (
                        channel_id == state["general_id"]
                        or channel["name"] in {"off-topic", "errors"}
                    ):
                        raise ValueError("RESET_PRESERVED_CHANNEL_MISMATCH")
                    await clear_channel_posts(admin, channel_id)
                else:
                    await admin.request(
                        "DELETE", f"channels/{channel_id}", params={"permanent": "true"}
                    )
            except MattermostError as error:
                if error.status != 404:
                    raise
        admin_id = (await admin.me())["id"]
        for bot_id in plan["manifest"]["bots"]:
            try:
                user = await admin.request("GET", f"users/{bot_id}")
                bot = await admin.request(
                    "GET", f"bots/{bot_id}", params={"include_deleted": "true"}
                )
                if (
                    not user.get("is_bot")
                    or bot_id == state["binding"]["owner_id"]
                    or not owned_account(bot, bot_id, admin_id)
                ):
                    raise ValueError("RESET_BOT_OWNERSHIP_MISMATCH")
                # Clear the owner's side of historical bot DMs and hide their empty shells.
                try:
                    dms = await admin.request("GET", f"users/{bot_id}/channels")
                except MattermostError as error:
                    if error.status != 404:
                        raise
                    dms = []
                for channel in dms:
                    if channel["type"] == "D" and state["binding"]["owner_id"] in channel[
                        "name"
                    ].split("__"):
                        page = 0
                        posts = []
                        while True:
                            batch = await admin.request(
                                "GET",
                                f"channels/{channel['id']}/posts",
                                params={"page": page, "per_page": 200},
                            )
                            posts.extend(batch["posts"].values())
                            if len(batch["order"]) < 200:
                                break
                            page += 1
                        for post in posts:
                            if not post.get("delete_at"):
                                await admin.request("DELETE", f"posts/{post['id']}")
                        await admin.request(
                            "PUT",
                            f"users/{state['binding']['owner_id']}/preferences",
                            json=[
                                {
                                    "user_id": state["binding"]["owner_id"],
                                    "category": "direct_channel_show",
                                    "name": bot_id,
                                    "value": "false",
                                }
                            ],
                        )
                await admin.request("DELETE", f"users/{bot_id}", params={"permanent": "true"})
            except MattermostError as error:
                if error.status != 404:
                    raise
    finally:
        await admin.request(
            "PUT", "config/patch", json={"ServiceSettings": plan["deletion_settings"]}
        )


async def reset(store: Store, settings: Settings, state: dict[str, Any], path: Path) -> None:
    plan = state["reset"]
    workspace = UUID(state["workspace_id"])
    async with Client(
        str(settings.mattermost_url), settings.mattermost_admin_token.get_secret_value()
    ) as admin:
        if "manifest" not in plan:
            plan["manifest"] = await snapshot(store, admin, state)
            save(path, state)
        if plan.get("mattermost_deleted") is not True:
            await delete_external(admin, state, path)
            plan["mattermost_deleted"] = True
            save(path, state)
        if "fresh" not in plan:
            await store.pool.execute(
                "DELETE FROM agent_name_reservations WHERE server_id=$1 "
                "AND agent_id=ANY($2::uuid[])",
                settings.mattermost_server_id,
                [UUID(a) for a in plan["manifest"]["agents"]],
            )
            # Reuse the defaults when preserved; recreate any missing ones after a legacy reset.
            await find_or_create(
                admin,
                f"teams/{state['team_id']}/channels/name/off-topic",
                "channels",
                {
                    "team_id": state["team_id"],
                    "name": "off-topic",
                    "display_name": "Off-Topic",
                    "type": "O",
                },
            )
            seed = UUID(plan["seed_id"])
            if "bot_username" not in plan:
                # Old reservations are released by purge; this seed's reservation persists on retry.
                plan["bot_username"] = await available_name(
                    store, admin, settings.mattermost_server_id, seed, "seed"
                )
                save(path, state)
            plan["fresh"] = await provision_seed(
                admin,
                workspace,
                seed,
                settings.mattermost_owner_id,
                team_name=state["team_name"],
                bot_username=plan["bot_username"],
                channel_name=settings.coordination_channel_name,
            )
            save(path, state)
        if plan.get("staging_id"):
            cleanup = deepcopy(state)
            cleanup["reset"]["manifest"] = {"channels": [plan["staging_id"]], "bots": []}
            await delete_external(admin, cleanup, path)
