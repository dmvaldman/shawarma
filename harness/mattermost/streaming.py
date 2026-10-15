"""Publish only the explicitly selected next assistant text part, never reasoning/tools."""

import asyncio
import json
import logging
from typing import Any
from uuid import UUID, uuid4

from harness.coding.clients import OpenCode
from harness.config import Settings
from harness.core.links import public_service_links
from harness.mattermost.client import Client
from harness.persistence.store import Store

logger = logging.getLogger(__name__)


class ChannelStream:
    def __init__(self, store: Store, bot: Client, agent: UUID, settings: Settings):
        self.store, self.bot, self.agent, self.settings = store, bot, agent, settings
        self.lock = asyncio.Lock()
        self.typing_task: asyncio.Task[None] | None = None

    async def begin(self, call_id: str, channel: str, root: str) -> dict[str, Any]:
        session = await self.store.pool.fetchrow(
            "SELECT session_id,directory FROM coding_sessions WHERE agent_id=$1", self.agent
        )
        if not session:
            raise ValueError("Channel streaming requires an active native OpenCode session")
        client = OpenCode(self.settings)
        try:
            messages = await client.request(
                "GET", f"/session/{session['session_id']}/message", session["directory"]
            )
        finally:
            await client.close()
        await self.update(messages)
        if await self.store.pool.fetchval(
            "SELECT EXISTS(SELECT 1 FROM channel_streams WHERE agent_id=$1 AND state='open')",
            self.agent,
        ):
            raise ValueError("A channel post stream is already open")
        stream = uuid4()
        await self.store.pool.execute(
            "INSERT INTO channel_streams(id,agent_id,call_id,channel_id,root_post_id,baseline) "
            "VALUES($1,$2,$3,$4,$5,$6)",
            stream,
            self.agent,
            call_id,
            channel,
            root,
            json.dumps([m["info"]["id"] for m in messages]),
        )
        await self.bot.typing(channel, root)
        if self.typing_task:
            self.typing_task.cancel()
        self.typing_task = asyncio.create_task(self.keep_typing(stream, channel, root))
        return {
            "stream_id": str(stream),
            "channel_id": channel,
            "instruction": "Your NEXT ordinary assistant text response is public in this "
            "channel. Emit only the message you intend to share. Reasoning stays private; "
            "the stream closes when this text part ends. Do not call another tool first.",
        }

    async def keep_typing(self, stream: UUID, channel: str, root: str) -> None:
        """An explicitly opened public composer is typing, even before its first chunk."""
        try:
            while True:
                await asyncio.sleep(3)
                opened = await self.store.pool.fetchval(
                    "SELECT EXISTS(SELECT 1 FROM channel_streams WHERE id=$1 AND state='open')",
                    stream,
                )
                state = await self.store.state(self.agent)
                if (
                    not opened
                    or state["control_paused"]
                    or state["workspace_state"] != "active"
                    or state["state"] == "TERMINATED"
                ):
                    return
                await self.bot.typing(channel, root)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("Agent %s public typing refresh interrupted", self.agent)

    async def update(self, messages: list[dict[str, Any]]) -> None:
        async with self.lock:
            row = await self.store.pool.fetchrow(
                "SELECT * FROM channel_streams WHERE agent_id=$1 AND state='open'", self.agent
            )
            if not row:
                return
            state = await self.store.state(self.agent)
            if (
                state["workspace_state"] != "active"
                or state["control_paused"]
                or state["state"] == "TERMINATED"
            ):
                await self._finish(dict(row), "interrupted")
                return
            baseline = (
                json.loads(row["baseline"]) if isinstance(row["baseline"], str) else row["baseline"]
            )
            for message in messages:
                info = message["info"]
                if info["role"] != "assistant" or info["id"] in baseline:
                    continue
                for part in message.get("parts", []):
                    if part.get("type") == "tool":
                        # The promised public text was skipped; do not leak a later response.
                        await self._finish(dict(row), "interrupted")
                        return
                    if part.get("type") != "text" or part.get("synthetic"):
                        continue
                    text = public_service_links(part.get("text", ""), self.settings)[:12000]
                    if not text:
                        continue
                    if row["message_id"] and (
                        info["id"] != row["message_id"] or part["id"] != row["part_id"]
                    ):
                        await self._finish(dict(row), "interrupted")
                        return
                    current = dict(row)
                    ended = bool(
                        part.get("time", {}).get("end") or info.get("time", {}).get("completed")
                    )
                    if text != row["text"]:
                        props = {"swarm_stream": "open"}
                        if not row["post_id"]:
                            # Never retry an uncertain creation automatically.
                            await self.store.pool.execute(
                                "UPDATE channel_streams SET state='interrupted' WHERE id=$1",
                                row["id"],
                            )
                            post = await self.bot.request(
                                "POST",
                                "posts",
                                json={
                                    "channel_id": row["channel_id"],
                                    "root_id": row["root_post_id"],
                                    "message": text,
                                    "props": props,
                                },
                            )
                            current["post_id"] = post["id"]
                        else:
                            await self.bot.request(
                                "PUT",
                                f"posts/{row['post_id']}/patch",
                                json={"message": text, "props": props},
                            )
                        current.update(text=text, message_id=info["id"], part_id=part["id"])
                        await self.store.pool.execute(
                            "UPDATE channel_streams SET post_id=$2,text=$3,message_id=$4,"
                            "part_id=$5,state='open' WHERE id=$1",
                            row["id"],
                            current["post_id"],
                            text,
                            info["id"],
                            part["id"],
                        )
                    # Posted/edited events can clear a typing indicator. Refresh
                    # after publishing the chunk, and never after the stream ends.
                    if not ended:
                        await self.bot.typing(row["channel_id"], row["root_post_id"])
                    if part.get("time", {}).get("end") or info.get("time", {}).get("completed"):
                        await self._finish(current, "completed")
                    return

    async def _finish(self, row: dict[str, Any], state: str) -> None:
        if self.typing_task:
            self.typing_task.cancel()
            await asyncio.gather(self.typing_task, return_exceptions=True)
            self.typing_task = None
        if row["post_id"]:
            await self.bot.request(
                "PUT",
                f"posts/{row['post_id']}/patch",
                json={"message": row["text"], "props": {"swarm_stream": state}},
            )

        await self.store.pool.execute(
            "UPDATE channel_streams SET state=$2 WHERE id=$1", row["id"], state
        )

    async def interrupt(self) -> None:
        async with self.lock:
            row = await self.store.pool.fetchrow(
                "SELECT * FROM channel_streams WHERE agent_id=$1 AND state='open'", self.agent
            )
            if row:
                await self._finish(dict(row), "interrupted")
