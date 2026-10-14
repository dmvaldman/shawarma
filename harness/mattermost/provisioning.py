from typing import Any
from uuid import UUID

from harness.core.names import owned_account, owns_bot, slug
from harness.mattermost.bot_reminders import remove_setup_reminder
from harness.mattermost.client import Client, MattermostError
from harness.mattermost.quiet_bots import quiet_bot, staged_account
from harness.persistence.store import Store


async def find_or_create(
    admin: Client, path: str, create_path: str, payload: dict[str, Any]
) -> dict[str, Any]:
    try:
        result: dict[str, Any] = await admin.request("GET", path)
        return result
    except MattermostError as error:
        if error.status != 404:
            raise
    result = await admin.request("POST", create_path, json=payload)
    return result


async def membership(admin: Client, team_id: str, channel_id: str, user_id: str) -> None:
    try:
        await admin.request("GET", f"teams/{team_id}/members/{user_id}")
    except MattermostError as error:
        if error.status != 404:
            raise
        await admin.request(
            "POST", f"teams/{team_id}/members", json={"team_id": team_id, "user_id": user_id}
        )
    try:
        await admin.request("GET", f"channels/{channel_id}/members/{user_id}")
    except MattermostError as error:
        if error.status != 404:
            raise
        await admin.request("POST", f"channels/{channel_id}/members", json={"user_id": user_id})


async def available_name(
    store: Store, admin: Client, server: str, agent: UUID, role: str, known_user: str = ""
) -> str:
    unavailable: set[str] = set()
    while True:
        name = await store.reserve_name(server, agent, role, unavailable=unavailable)
        try:
            user = await admin.request("GET", f"users/username/{name}")
        except MattermostError as error:
            if error.status == 404:
                return name
            raise
        if (known_user and user["id"] == known_user) or staged_account(user, agent):
            return name
        unavailable.add(name)


async def provision_seed(
    admin: Client,
    workspace_id: UUID,
    seed_id: UUID,
    owner_id: str,
    existing_token: str = "",
    *,
    workspace_name: str = "",
    team_name: str = "",
    bot_username: str = "",
    existing_bot_id: str = "",
    channel_name: str = "general",
) -> dict[str, str]:
    me = await admin.me()
    if "system_admin" not in me.get("roles", "").split():
        raise ValueError("bootstrap credential must belong to a Mattermost system admin")
    owner = await admin.request("GET", f"users/{owner_id}")
    if owner.get("is_bot") or owner.get("delete_at", 0):
        raise ValueError("configured owner must be an active human account")
    name = team_name or (
        (slug(workspace_name, 20) + "-" + workspace_id.hex[:8])
        if workspace_name
        else "org-" + workspace_id.hex[:12]
    )
    team = await find_or_create(
        admin,
        f"teams/name/{name}",
        "teams",
        {"name": name, "display_name": workspace_name or name, "type": "I"},
    )
    general = await find_or_create(
        admin,
        f"teams/{team['id']}/channels/name/{channel_name}",
        "channels",
        {
            "team_id": team["id"],
            "name": channel_name,
            "display_name": channel_name.replace("-", " ").title(),
            "type": "O",
        },
    )
    from harness.core.diagnostics import ensure_errors_channel

    errors_id = await ensure_errors_channel(admin, team["id"], owner_id)
    await membership(admin, team["id"], general["id"], owner_id)
    username = bot_username or "a-" + seed_id.hex[:12]
    created = False
    try:
        bot_user = await admin.request(
            "GET", f"users/{existing_bot_id}" if existing_bot_id else f"users/username/{username}"
        )
        username = bot_user.get("username", username)
        bot_id = bot_user["id"]
        if staged_account(bot_user, seed_id, workspace_id):
            await quiet_bot(
                admin,
                workspace_id,
                seed_id,
                username,
                username,
                f"Agent Company workspace {workspace_id}; seed {seed_id}",
            )
        # Never take over a coincidentally named human account.
        existing_bot = await admin.request(
            "GET", f"bots/{bot_id}", params={"include_deleted": "true"}
        )
        if not (
            owns_bot(existing_bot.get("description", ""), workspace_id, seed_id, "seed")
            or (existing_bot_id == bot_id and owned_account(existing_bot, bot_id, me["id"]))
        ):
            raise ValueError("seed username belongs to a different bot")
        if bot_user.get("delete_at", 0):
            await admin.request("POST", f"bots/{bot_id}/enable")
    except MattermostError as error:
        if error.status != 404:
            raise
        bot = await quiet_bot(
            admin,
            workspace_id,
            seed_id,
            username,
            username,
            f"Agent Company workspace {workspace_id}; seed {seed_id}",
        )
        bot_id = bot["user_id"]
        created = True
    token_id = ""
    try:
        await membership(admin, team["id"], general["id"], bot_id)
        off_topic = await admin.request("GET", f"teams/{team['id']}/channels/name/off-topic")
        await membership(admin, team["id"], off_topic["id"], bot_id)
        await remove_setup_reminder(admin, bot_id, me["id"])
        if not existing_token:
            description = f"harness:{workspace_id}:{seed_id}"
            # Recover a crash after token issuance: revoke only tokens for this seed.
            page = 0
            while True:
                tokens = await admin.request(
                    "GET", f"users/{bot_id}/tokens", params={"page": page, "per_page": 100}
                )
                for old in tokens:
                    if old["description"] == description:
                        await admin.request(
                            "POST", "users/tokens/revoke", json={"token_id": old["id"]}
                        )
                if len(tokens) < 100:
                    break
                page += 1
            issued = await admin.request(
                "POST", f"users/{bot_id}/tokens", json={"description": description}
            )
            existing_token, token_id = issued["token"], issued["id"]
        return {
            "team_id": team["id"],
            "general_id": general["id"],
            "off_topic_id": off_topic["id"],
            "errors_id": errors_id,
            "team_name": name,
            "bot_user_id": bot_id,
            "bot_username": username,
            "bot_token": existing_token,
        }
    except BaseException:
        if token_id:
            await admin.request("POST", "users/tokens/revoke", json={"token_id": token_id})
        if created:
            await admin.request("POST", f"bots/{bot_id}/disable")
        raise
