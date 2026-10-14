import asyncio
import json

import pytest
from websockets.asyncio.server import serve

from harness.mattermost.client import Client, MattermostError


async def test_websocket_authentication_and_automatic_reconnect():
    connections = 0

    async def handler(socket):
        nonlocal connections
        challenge = json.loads(await socket.recv())
        assert challenge == {
            "seq": 1,
            "action": "authentication_challenge",
            "data": {"token": "test-token"},
        }
        await socket.send(json.dumps({"event": "hello", "data": {}}))
        await socket.send(json.dumps({"seq_reply": 1, "status": "OK"}))
        connections += 1
        await socket.send(json.dumps({"event": "posted", "data": {"connection": connections}}))
        await socket.close()

    async with serve(handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        async with Client(f"http://127.0.0.1:{port}", "test-token") as client:
            stream = client.events()
            try:
                async with asyncio.timeout(5):
                    assert (await anext(stream))["event"] == "harness_connected"
                    assert (await anext(stream))["data"]["connection"] == 1
                    assert (await anext(stream))["event"] == "harness_connected"
                    assert (await anext(stream))["data"]["connection"] == 2
            finally:
                await stream.aclose()
    assert connections == 2


async def test_bad_websocket_credentials_fail_without_retry_or_token_leak():
    async def handler(socket):
        await socket.recv()
        await socket.send(json.dumps({"seq_reply": 1, "status": "FAIL"}))

    async with serve(handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        async with Client(f"http://127.0.0.1:{port}", "secret") as client:
            stream = client.events()
            with pytest.raises(MattermostError, match="authentication_failed") as error:
                await anext(stream)
            assert "secret" not in str(error.value)


async def test_typing_uses_authenticated_socket_sequences_and_stops_on_disconnect():
    received = []

    async def handler(socket):
        challenge = json.loads(await socket.recv())
        assert challenge["action"] == "authentication_challenge"
        await socket.send(json.dumps({"seq_reply": 1, "status": "OK"}))
        for _ in range(2):
            received.append(json.loads(await socket.recv()))
        await socket.send(json.dumps({"event": "typing_received", "data": {}}))
        await socket.wait_closed()

    async with serve(handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        async with Client(f"http://127.0.0.1:{port}", "test-token") as client:
            assert not await client.typing("channel")
            stream = client.events()
            assert (await anext(stream))["event"] == "harness_connected"
            assert await client.typing("channel")
            assert await client.typing("channel", "thread-root")
            assert (await anext(stream))["event"] == "typing_received"
            await stream.aclose()
            assert not await client.typing("channel")
    assert received == [
        {"seq": 2, "action": "user_typing", "data": {"channel_id": "channel", "parent_id": ""}},
        {
            "seq": 3,
            "action": "user_typing",
            "data": {"channel_id": "channel", "parent_id": "thread-root"},
        },
    ]


async def test_peer_typing_is_visible_and_cleared_when_the_peer_posts():
    async def handler(socket):
        await socket.recv()
        await socket.send(json.dumps({"seq_reply": 1, "status": "OK"}))
        await socket.send(
            json.dumps(
                {
                    "event": "typing",
                    "data": {"user_id": "peer"},
                    "broadcast": {"channel_id": "channel"},
                }
            )
        )
        await socket.send(
            json.dumps(
                {
                    "event": "posted",
                    "data": {"post": json.dumps({"user_id": "peer", "channel_id": "channel"})},
                }
            )
        )
        await socket.wait_closed()

    async with serve(handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        async with Client(f"http://127.0.0.1:{port}", "token") as client:
            stream = client.events()
            try:
                await anext(stream)
                assert (await anext(stream))["event"] == "typing"
                assert client.typing_users("channel") == ["peer"]
                assert client.typing_changed.is_set()
                assert (await anext(stream))["event"] == "posted"
                assert client.typing_users("channel") == []
                client.typing_until[("channel", "expired")] = asyncio.get_running_loop().time() - 1
                assert client.typing_users("channel") == []
            finally:
                await stream.aclose()
