import json
import re
from hashlib import sha256
from typing import Any
from uuid import UUID

from harness.config import Settings
from harness.core.types import Post
from harness.mattermost.client import Client, MattermostError
from harness.notifications.routing import addressed
from harness.persistence.store import Store


class Bridge:
    def __init__(self, store: Store, bot: Client, agent_id: UUID, settings: Settings | None = None):
        self.store, self.bot, self.agent_id = store, bot, agent_id
        self.settings = settings

    def delivery_delay(self, post: Post, username: str) -> float:
        if self.settings is None:
            return 0
        # Deterministic jitter remains stable on replay, distinct for each recipient/post.
        fraction = int.from_bytes(
            sha256(f"{self.agent_id}:{post.id}".encode()).digest()[:8], "big"
        ) / (2**64 - 1)
        low, high = self.settings.channel_delivery_min_sec, self.settings.channel_delivery_max_sec
        if re.search(r"(?<!\w)@" + re.escape(username) + r"(?![\w-])", post.message):
            high = min(high, 1.0)
            low = min(low, high)
        return low + (high - low) * fraction

    async def channel_allowed(self, channel_id: str) -> tuple[bool, str]:
        state = await self.store.state(self.agent_id)
        channel = await self.bot.request("GET", f"channels/{channel_id}")
        if channel.get("name") == "errors":
            return False, "channel_post"  # Operational logs never wake swarm agents.
        if channel["type"] in {"D", "G"}:
            return False, "dm"  # Disabled until a human-observable DM UI exists.
        mode = await self.store.pool.fetchval(
            "SELECT mode FROM subscriptions WHERE agent_id=$1 AND channel_id=$2",
            self.agent_id,
            channel_id,
        )
        return channel["team_id"] == state["team_id"] and mode in {
            "all",
            "mentions",
        }, "channel_post"

    async def ingest(self, event: dict[str, Any]) -> bool:
        kind = event.get("event")
        if kind not in {"posted", "post_edited", "post_deleted"}:
            return False
        post = Post.model_validate(json.loads(event["data"]["post"]))
        if post.type.startswith("system_") or post.props.get("swarm_stream") == "open":
            return False
        if post.props.get("swarm_stream") in {"completed", "interrupted"} and kind == "post_edited":
            kind = "posted"
        allowed, channel_kind = await self.channel_allowed(post.channel_id)
        if not allowed:
            return False
        state = await self.store.state(self.agent_id)
        mode = await self.store.pool.fetchval(
            "SELECT mode FROM subscriptions WHERE agent_id=$1 AND channel_id=$2",
            self.agent_id,
            post.channel_id,
        )
        if channel_kind == "channel_post" and mode == "mentions":
            identity = await self.store.get(self.agent_id)
            public = await self.store.public_profile(self.agent_id)
            if not addressed(post.message, public.get("username", identity.mattermost_username)):
                return False
        if post.create_at < state["created_ms"]:
            return False
        identity = await self.store.get(self.agent_id)
        public = await self.store.public_profile(self.agent_id)
        username = public.get("username", identity.mattermost_username)
        return await self.store.ingest(
            self.agent_id,
            post,
            channel_kind if kind == "posted" else kind,
            state["server_id"],
            self.delivery_delay(post, username),
        )

    async def catch_up(self) -> bool:
        """Read new ordinary channel posts independently of mention-only wake routing."""
        state = await self.store.state(self.agent_id)
        channels = await self.bot.request("GET", f"users/me/teams/{state['team_id']}/channels")
        received = False
        for channel in channels:
            if channel.get("name") == "errors":
                continue
            if channel["type"] not in {"O", "P"} or channel["team_id"] != state["team_id"]:
                continue
            mode = await self.store.pool.fetchval(
                "SELECT mode FROM subscriptions WHERE agent_id=$1 AND channel_id=$2",
                self.agent_id,
                channel["id"],
            )
            if mode not in {"all", "mentions"}:
                continue
            since = await self.store.pool.fetchval(
                "SELECT last_read_at FROM idle_channel_cursors WHERE agent_id=$1 AND channel_id=$2",
                self.agent_id,
                channel["id"],
            )
            since = max(since or 0, state["created_ms"])
            posts: dict[str, Post] = {}
            page = 0
            while True:
                batch = self.bot.posts(
                    await self.bot.request(
                        "GET",
                        f"channels/{channel['id']}/posts",
                        params={"page": page, "per_page": 100},
                    )
                )
                for post in batch:
                    if max(post.create_at, post.update_at) >= since:
                        posts[post.id] = post
                if len(batch) < 100 or min(p.create_at for p in batch) < since:
                    break
                page += 1
            for post in sorted(posts.values(), key=lambda p: (p.create_at, p.id)):
                if (
                    post.type.startswith("system_")
                    or post.delete_at
                    or post.props.get("swarm_stream") == "open"
                ):
                    continue
                received = (
                    await self.store.ingest(
                        self.agent_id,
                        post,
                        "idle_channel_post",
                        state["server_id"],
                    )
                    or received
                )
            # Save only after durable ingestion; a crash replays safely through deduplication.
            latest = max([since, *(p.create_at for p in posts.values())])
            await self.store.pool.execute(
                "INSERT INTO idle_channel_cursors VALUES($1,$2,$3) "
                "ON CONFLICT(agent_id,channel_id) "
                "DO UPDATE SET last_read_at=GREATEST("
                "idle_channel_cursors.last_read_at,EXCLUDED.last_read_at)",
                self.agent_id,
                channel["id"],
                latest,
            )
        return received

    async def replay(self) -> None:
        """Page full accessible history: Mattermost's since query silently caps at 1000."""
        state = await self.store.state(self.agent_id)
        channels = await self.bot.request("GET", f"users/me/teams/{state['team_id']}/channels")
        for channel in channels:
            allowed, _ = await self.channel_allowed(channel["id"])
            if not allowed:
                continue
            posts: dict[str, Post] = {}
            page = 0
            while True:
                batch = self.bot.posts(
                    await self.bot.request(
                        "GET",
                        f"channels/{channel['id']}/posts",
                        params={"page": page, "per_page": 100},
                    )
                )
                for post in batch:
                    posts[post.id] = post
                if len(batch) < 100:
                    break
                page += 1
            for post in sorted(posts.values(), key=lambda p: (p.create_at, p.id)):
                await self.ingest({"event": "posted", "data": {"post": post.model_dump_json()}})
                if post.update_at > post.create_at:
                    await self.ingest(
                        {"event": "post_edited", "data": {"post": post.model_dump_json()}}
                    )
            # Known deleted posts can disappear from bot history; preserve an amendment.
            known = await self.store.pool.fetch(
                (
                    "SELECT DISTINCT post_id,post_preview::text FROM notifications "
                    "WHERE agent_id=$1 AND post_preview->>'channel_id'=$2"
                ),
                self.agent_id,
                channel["id"],
            )
            for row in known:
                if row["post_id"] in posts:
                    continue
                try:
                    await self.bot.request("GET", f"posts/{row['post_id']}")
                except MattermostError as error:
                    if error.status != 404:
                        raise
                    post = Post.model_validate(json.loads(row["post_preview"]))
                    # Stable revision for an unavailable historical post; no fresh instruction.
                    post.delete_at = post.delete_at or 1
                    await self.ingest(
                        {"event": "post_deleted", "data": {"post": post.model_dump_json()}}
                    )
