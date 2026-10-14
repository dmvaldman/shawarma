"""Remove Mattermost's automatic bot-setup reminders after harness provisioning."""

from harness.mattermost.client import Client

REMINDER_TYPE = "add_bot_teams_channels"


async def remove_setup_reminder(admin: Client, bot_id: str, owner_id: str) -> int:
    """Only touch setup system posts in this bot's existing DM with its creator."""
    dm_name = "__".join(sorted((bot_id, owner_id)))
    channels = await admin.request("GET", f"users/{owner_id}/channels")
    removed = 0
    for channel in channels:
        if channel["type"] != "D" or channel["name"] != dm_name:
            continue
        reminders: list[str] = []
        has_conversation = False
        page = 0
        while True:
            data = await admin.request(
                "GET", f"channels/{channel['id']}/posts", params={"page": page, "per_page": 100}
            )
            posts = [data["posts"][pid] for pid in data["order"]]
            for post in posts:
                if post.get("delete_at"):
                    continue
                if post.get("type") == REMINDER_TYPE and post["user_id"] == bot_id:
                    reminders.append(post["id"])
                else:
                    has_conversation = True
            if len(posts) < 100:
                break
            page += 1
        # Read before deleting so pagination offsets cannot skip older conversation.
        for post_id in reminders:
            await admin.request("DELETE", f"posts/{post_id}")
            removed += 1
        if not has_conversation:
            await admin.request(
                "PUT",
                f"users/{owner_id}/preferences",
                json=[
                    {
                        "user_id": owner_id,
                        "category": "direct_channel_show",
                        "name": bot_id,
                        "value": "false",
                    }
                ],
            )
    return removed
