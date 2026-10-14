"""A single seed-authored welcome per fresh Town Square, without model inference."""

from pathlib import Path
from typing import Any

from harness.mattermost.client import Client
from harness.persistence.bootstrap import save

PREAMBLE = (
    "**Welcome to your swarm.** 🐝\n\n"
    "Post a question, problem, or idea here. @seed will assemble agents and open a "
    "dedicated channel to investigate it together. Follow along, join the conversation, "
    "and steer their work.\n\n"
    "Use `/pause`, `/continue`, `/stop`, or `/reset` here to control the whole swarm."
)


async def ensure_welcome(bot: Client, state: dict[str, Any], path: Path) -> None:
    channel, seed = state["general_id"], state["seed_id"]
    if state.get("welcome") and state["welcome"].get("channel_id") == channel:
        return
    # Recover a crash after posting but before saving the checkpoint.
    page = 0
    while True:
        posts = await bot.request(
            "GET", f"channels/{channel}/posts", params={"page": page, "per_page": 200}
        )
        for post in posts["posts"].values():
            if (
                post.get("user_id") == state["bot_user_id"]
                and not post.get("delete_at")
                and post.get("props", {}).get("swarm_welcome_seed_id") == seed
            ):
                state["welcome"] = {"channel_id": channel, "post_id": post["id"]}
                save(path, state)
                return
        if len(posts["order"]) < 200:
            break
        page += 1
    post = await bot.request(
        "POST",
        "posts",
        json={
            "channel_id": channel,
            "message": PREAMBLE,
            "props": {"swarm_welcome_seed_id": seed},
        },
    )
    state["welcome"] = {"channel_id": channel, "post_id": post["id"]}
    save(path, state)
