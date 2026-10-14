import asyncio
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from aiohttp import WSServerHandshakeError
from aiohttp.test_utils import TestClient, TestServer

from harness.api.analytics import Analytics
from harness.config import Settings


async def test_websocket_shares_changes_reconnects_and_rejects_other_origins(monkeypatch):
    settings = Settings(
        _env_file=None,
        mattermost_url="http://mattermost.invalid",
        database_url="postgresql://unused",
    )
    dashboard = Analytics(AsyncMock(), settings, uuid4())
    current = {"workspace": {"status": "active", "since": "old"}, "totals": {"messages": 0}}

    async def snapshot():
        return current.copy()

    monkeypatch.setattr(dashboard, "snapshot", snapshot)
    async with TestClient(TestServer(dashboard.app())) as client:
        with pytest.raises(WSServerHandshakeError) as caught:
            await client.ws_connect("/api/ws", headers={"Origin": "http://evil.example"})
        assert caught.value.status == 403
        a, b = await client.ws_connect("/api/ws"), await client.ws_connect("/api/ws")
        assert (await a.receive_json())["data"]["workspace"]["status"] == "active"
        assert (await b.receive_json())["data"]["workspace"]["status"] == "active"
        current = {"workspace": {"status": "resetting", "since": "new"}, "totals": {"messages": 0}}
        for socket in (a, b):
            async with asyncio.timeout(3):
                while (await socket.receive_json())["data"]["workspace"]["status"] != "resetting":
                    pass
        await a.close()
        await b.close()
        current = {
            "workspace": {"status": "waiting for task", "since": "new"},
            "totals": {"messages": 0},
        }
        c = await client.ws_connect("/api/ws")
        assert (await c.receive_json())["data"]["workspace"]["status"] == "waiting for task"
        await c.close()
    assert not dashboard.sockets
    assert dashboard.publisher is None
