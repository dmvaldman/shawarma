"""Provision bots without Mattermost's CreateBot setup DM, using supported REST APIs."""

from secrets import token_urlsafe
from typing import Any
from uuid import UUID

from harness.core.names import owned_account
from harness.mattermost.client import Client, MattermostError


def staged_account(user: dict[str, Any], agent: UUID, workspace: UUID | None = None) -> bool:
    props = user.get("props", {})
    return (
        (user.get("is_bot") or user.get("email") == f"harness-{agent.hex}@bots.invalid")
        and props.get("harness_agent") == str(agent)
        and (workspace is None or props.get("harness_workspace") == str(workspace))
        and "system_admin" not in user.get("roles", "").split()
    )


async def quiet_bot(
    admin: Client, workspace: UUID, agent: UUID, username: str, display_name: str, description: str
) -> dict[str, Any]:
    owner = (await admin.me())["id"]
    try:
        user = await admin.request("GET", f"users/username/{username}")
    except MattermostError as error:
        if error.status != 404:
            raise
        user = await admin.request(
            "POST",
            "users",
            json={
                "username": username,
                "email": f"harness-{agent.hex}@bots.invalid",
                "password": token_urlsafe(48),
                "disable_welcome_email": True,
                "props": {"harness_workspace": str(workspace), "harness_agent": str(agent)},
            },
        )
    # Never convert a coincidentally named human or take over a foreign bot.
    if not staged_account(user, agent, workspace):
        raise ValueError("QUIET_BOT_IDENTITY_MISMATCH")
    if not user.get("is_bot"):
        await admin.request("POST", f"users/{user['id']}/convert_to_bot")
    bot = await admin.request("GET", f"bots/{user['id']}", params={"include_deleted": "true"})
    if bot.get("owner_id") not in {user["id"], owner}:
        raise ValueError("BOT_OWNERSHIP_MISMATCH")
    if bot["owner_id"] != owner:
        bot = await admin.request("POST", f"bots/{user['id']}/assign/{owner}")
    if not owned_account(bot, user["id"], owner):
        raise ValueError("BOT_OWNERSHIP_MISMATCH")
    bot = await admin.request(
        "PUT",
        f"bots/{user['id']}",
        json={
            "display_name": display_name,
            "description": description,
        },
    )
    return dict(bot)
