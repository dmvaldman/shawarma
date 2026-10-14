"""Mattermost v10.11 REST and bot-scoped WebSocket transport."""

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from typing import Any, cast
from urllib.parse import urlsplit, urlunsplit

import httpx
from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed

from harness.core.types import Post


class MattermostError(RuntimeError):
    def __init__(self, status: int, code: str):
        self.status = status
        super().__init__(f"Mattermost HTTP {status}: {code}")


class Client:
    def __init__(
        self, base_url: str, token: str, *, transport: httpx.AsyncBaseTransport | None = None
    ):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.http = httpx.AsyncClient(
            base_url=self.base_url + "/api/v4/",
            headers={"Authorization": f"Bearer {token}"},
            timeout=30,
            transport=transport,
        )
        self.user_id: str | None = None
        self.socket: ClientConnection | None = None
        self.socket_sequence = 1
        self.socket_send_lock = asyncio.Lock()
        self.typing_changed = asyncio.Event()
        self.typing_until: dict[tuple[str, str], float] = {}

    async def __aenter__(self) -> "Client":
        return self

    async def __aexit__(self, *args: Any) -> None:
        await self.http.aclose()

    async def request(self, method: str, path: str, **kwargs: Any) -> Any:
        response = await self.http.request(method, path, **kwargs)
        if response.is_error:
            # Do not log request bodies, tokens, or untrusted server error text.
            try:
                code = response.json().get("id", "request_failed")
            except ValueError:
                code = "invalid_response"
            raise MattermostError(response.status_code, code)
        return response.json() if response.content else None

    async def me(self) -> dict[str, Any]:
        user = cast(dict[str, Any], await self.request("GET", "users/me"))
        self.user_id = user["id"]
        return user

    async def presence(self, status: str) -> None:
        try:
            async with asyncio.timeout(2):
                user = self.user_id or (await self.me())["id"]
                await self.request(
                    "PUT", f"users/{user}/status", json={"user_id": user, "status": status}
                )
        except (MattermostError, httpx.TransportError, TimeoutError):
            logging.getLogger(__name__).warning("Mattermost presence update unavailable")

    async def post_channel(self, channel_id: str, text: str, root_post_id: str = "") -> Post:
        return Post.model_validate(
            await self.request(
                "POST",
                "posts",
                json={
                    "channel_id": channel_id,
                    "message": text,
                    "root_id": root_post_id,
                },
            )
        )

    @staticmethod
    def posts(data: dict[str, Any]) -> list[Post]:
        return [Post.model_validate(data["posts"][pid]) for pid in data["order"]]

    async def read_channel(self, channel_id: str, limit: int = 50) -> list[Post]:
        if not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        posts = self.posts(
            await self.request(
                "GET", f"channels/{channel_id}/posts", params={"per_page": limit, "page": 0}
            )
        )
        return [p for p in posts if p.props.get("swarm_stream") != "open"]

    async def get_thread(self, root_post_id: str) -> list[Post]:
        return self.posts(await self.request("GET", f"posts/{root_post_id}/thread"))

    async def create_channel(self, team_id: str, name: str, display_name: str) -> str:
        data = await self.request(
            "POST",
            "channels",
            json={
                "team_id": team_id,
                "name": name,
                "display_name": display_name,
                "type": "O",
            },
        )
        return str(data["id"])

    async def send_dm(self, recipient_id: str, text: str) -> Post:
        if self.user_id is None:
            await self.me()
        channel = await self.request("POST", "channels/direct", json=[self.user_id, recipient_id])
        return await self.post_channel(channel["id"], text)

    async def add_reaction(self, post_id: str, emoji_name: str) -> None:
        if self.user_id is None:
            await self.me()
        await self.request(
            "POST",
            "reactions",
            json={
                "user_id": self.user_id,
                "post_id": post_id,
                "emoji_name": emoji_name,
            },
        )

    async def remove_reaction(self, post_id: str, emoji_name: str) -> None:
        if self.user_id is None:
            await self.me()
        await self.request("DELETE", f"users/{self.user_id}/posts/{post_id}/reactions/{emoji_name}")

    async def search_messages(self, team_id: str, query: str) -> list[Post]:
        if not query.strip():
            raise ValueError("query must be nonempty Mattermost search syntax")
        return self.posts(
            await self.request(
                "POST",
                f"teams/{team_id}/posts/search",
                json={
                    "terms": query,
                    "is_or_search": False,
                },
            )
        )

    def typing_users(self, channel_id: str) -> list[str]:
        now = asyncio.get_running_loop().time()
        self.typing_until = {key: until for key, until in self.typing_until.items() if until > now}
        return sorted(
            user
            for (channel, user) in self.typing_until
            if channel == channel_id and user != self.user_id
        )

    async def typing(self, channel_id: str, parent_id: str = "") -> bool:
        """Best-effort ephemeral indicator on the existing bot-authenticated socket."""
        async with self.socket_send_lock:
            socket = self.socket
            if socket is None:
                return False
            self.socket_sequence += 1
            try:
                async with asyncio.timeout(2):
                    await socket.send(
                        json.dumps(
                            {
                                "seq": self.socket_sequence,
                                "action": "user_typing",
                                "data": {"channel_id": channel_id, "parent_id": parent_id},
                            }
                        )
                    )
                return True
            except (OSError, ConnectionClosed, TimeoutError):
                return False

    async def events(self) -> AsyncIterator[dict[str, Any]]:
        """Reconnect transport. Durable replay/notification acknowledgement belong to M2."""
        parts = urlsplit(self.base_url)
        url = urlunsplit(
            (
                "wss" if parts.scheme == "https" else "ws",
                parts.netloc,
                parts.path.rstrip("/") + "/api/v4/websocket",
                "",
                "",
            )
        )
        delay = 1
        while True:
            try:
                async with connect(url, open_timeout=20, max_size=2**20) as socket:
                    await socket.send(
                        json.dumps(
                            {
                                "seq": 1,
                                "action": "authentication_challenge",
                                "data": {"token": self.token},
                            }
                        )
                    )
                    async with asyncio.timeout(20):
                        reply = json.loads(await socket.recv())
                        # A hello event may precede the authentication acknowledgement.
                        while "seq_reply" not in reply:
                            reply = json.loads(await socket.recv())
                    if reply.get("status") != "OK":
                        raise MattermostError(401, "websocket_authentication_failed")
                    self.socket_sequence = 1
                    self.socket = socket
                    delay = 1
                    yield {"event": "harness_connected", "data": {}}
                    async for raw in socket:
                        event = json.loads(raw)
                        if event.get("event") == "typing":
                            channel = event.get("broadcast", {}).get("channel_id")
                            user = event.get("data", {}).get("user_id")
                            if channel and user:
                                self.typing_until[(channel, user)] = (
                                    asyncio.get_running_loop().time() + 6
                                )
                                self.typing_changed.set()
                        elif event.get("event") == "posted" and "post" in event.get("data", {}):
                            post = json.loads(event["data"]["post"])
                            self.typing_until.pop((post["channel_id"], post["user_id"]), None)
                            self.typing_changed.set()
                        if "event" in event:
                            yield event
            except (OSError, ConnectionClosed, TimeoutError) as error:
                logging.getLogger(__name__).warning(
                    "Mattermost event connection interrupted for user %s (%s); reconnecting",
                    self.user_id,
                    type(error).__name__,
                )
            finally:
                self.socket = None
                self.typing_until.clear()
                self.typing_changed.set()
            # Back off even after a clean server close to avoid reconnect spin.
            await asyncio.sleep(delay)
            delay = min(delay * 2, 30)
