import json
from unittest.mock import AsyncMock

from harness.hosting import bootstrap


async def test_finalize_waits_for_recreated_services_before_profile(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text(
        "HOSTED_OWNER_NAME=David\n"
        + "".join(
            f"HOSTED_{service}_URL=https://service.example\n"
            for service in ["MATTERMOST", "GITEA", "OPENCODE", "ANALYTICS"]
        )
    )
    (tmp_path / ".state").mkdir()
    (tmp_path / ".state/coding.json").write_text(json.dumps({"gitea_url": "http://localhost:3000"}))
    monkeypatch.setattr(bootstrap.sys, "argv", ["bootstrap", "--finalize"])
    monkeypatch.setattr(bootstrap.subprocess, "check_output", lambda *a, **kw: "127.0.0.1")
    monkeypatch.setattr(bootstrap, "update_env", lambda values: None)
    checks = []
    attempts = {}

    def http_ready(port, path, expected=200):
        checks.append((port, path, expected))
        attempts[port] = attempts.get(port, 0) + 1
        return attempts[port] == 2

    def wait_for(label, check):
        assert not check()  # Socket reset/refused while the recreated server boots.
        assert check()

    async def profile(name, state):
        assert name == "David"
        assert attempts == {3000: 2, 4096: 2}

    monkeypatch.setattr(bootstrap, "http_ready", http_ready)
    monkeypatch.setattr(bootstrap, "wait_for", wait_for)
    monkeypatch.setattr(bootstrap, "gitea_profile", AsyncMock(side_effect=profile))
    await bootstrap.bootstrap()
    bootstrap.gitea_profile.assert_awaited_once()
    assert checks == [(3000, "/user/login", 200)] * 2 + [(4096, "/global/health", 401)] * 2


async def test_finalize_timeout_does_not_update_profile(monkeypatch, tmp_path):
    import pytest

    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text(
        "HOSTED_OWNER_NAME=David\n"
        + "".join(
            f"HOSTED_{service}_URL=https://service.example\n"
            for service in ["MATTERMOST", "GITEA", "OPENCODE", "ANALYTICS"]
        )
    )
    monkeypatch.setattr(bootstrap.sys, "argv", ["bootstrap", "--finalize"])

    def unavailable(label, check):
        raise RuntimeError("Resume timed out waiting for " + label)

    monkeypatch.setattr(bootstrap, "wait_for", unavailable)
    profile = AsyncMock()
    monkeypatch.setattr(bootstrap, "gitea_profile", profile)
    with pytest.raises(RuntimeError, match="Gitea after configuration"):
        await bootstrap.bootstrap()
    profile.assert_not_awaited()
