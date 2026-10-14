import json
from typing import Any

from harness.core.types import Post
from harness.mattermost.client import Client


class OwnerPostGate:
    """M1 in-process deduplication only; M2 replaces it with the durable inbox."""

    def __init__(self, owner_id: str, general_id: str):
        self.owner_id, self.general_id = owner_id, general_id
        self.seen: set[str] = set()

    def accept(self, event: dict[str, Any]) -> Post | None:
        if event.get("event") != "posted":
            return None
        post = Post.model_validate(json.loads(event["data"]["post"]))
        if (
            post.user_id != self.owner_id
            or post.channel_id != self.general_id
            or post.delete_at
            or post.id in self.seen
        ):
            return None
        return post

    def acknowledge(self, post: Post) -> None:
        self.seen.add(post.id)


async def listen(bot: Client, gate: OwnerPostGate) -> None:
    async for event in bot.events():
        if event["event"] == "harness_connected":
            print("Connected. Waiting for authenticated owner posts in #general.", flush=True)
            continue
        post = gate.accept(event)
        if post is not None:
            await bot.post_channel(
                post.channel_id,
                "M1 communication check: received your post. Model inference is not enabled.",
                root_post_id=post.root_id or post.id,
            )
            gate.acknowledge(post)
            print(f"Acknowledged owner post {post.id} as seed bot.", flush=True)
