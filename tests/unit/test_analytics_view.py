from unittest.mock import AsyncMock
from uuid import uuid4

from harness.api.analytics import Analytics, activity
from harness.config import Settings


def test_activity_respects_controls_before_runtime_state():
    w = {"state": "active", "started": True}
    row = {"state": "RUNNING", "control_paused": False}
    assert activity(row, w, False) == "working"
    assert activity(row, {**w, "state": "paused"}, False) == "paused"
    assert activity({**row, "control_paused": True}, w, False) == "paused"
    assert activity(row, {**w, "started": False}, False) == "idle"
    assert activity({**row, "state": "FAILED"}, w, False) == "failed"
    assert activity({**row, "state": "TERMINATED"}, w, False) == "retired"
    assert activity(row, w, True) == "resetting"


async def test_actual_membership_cache_reset_and_outage(monkeypatch):
    from harness.mattermost.client import Client

    calls = []

    async def request(self, method, path, **kwargs):
        calls.append(path)
        if path.startswith("teams/"):
            return {"id": "team", "name": "my-swarm"}
        return [
            {"id": "off-topic", "name": "off-topic", "delete_at": 0},
            {"id": "deleted", "name": "deleted", "delete_at": 123},
        ]

    monkeypatch.setattr(Client, "request", request)
    dashboard = Analytics(AsyncMock(), Settings(), uuid4())
    data = {
        "workspace": {"since": "before-reset", "team_id": "team"},
        "agents": [{"id": "a", "user_id": "bot", "state": "RUNNING"}],
    }
    await dashboard.channels(data)
    assert len(calls) == 2
    assert data["agents"][0]["channels"][0]["name"] == "off-topic"
    assert len(data["agents"][0]["channels"]) == 1
    await dashboard.channels(data)
    assert len(calls) == 2
    data["workspace"]["since"] = "after-reset"
    data["agents"][0]["id"] = "newseed"
    await dashboard.channels(data)
    assert len(calls) == 4

    async def unavailable(*args, **kwargs):
        raise OSError("private diagnostic")

    monkeypatch.setattr(Client, "request", unavailable)
    dashboard.membership_until = 0
    await dashboard.channels(data)
    assert data["memberships_unavailable"]
    assert data["agents"][0]["channels"][0]["name"] == "off-topic"
    data["workspace"]["since"] = "another-reset"
    await dashboard.channels(data)
    assert data["agents"][0]["channels"] == []
