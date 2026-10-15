"""The provider-only seed must select models for its OpenCode children."""

import time
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from harness.coding.clients import OpenCode
from harness.config import Settings
from harness.core.coordinator import Coordinator
from harness.mattermost.client import Client


async def test_seed_filters_worker_models_before_reserving_a_child(monkeypatch):
    settings = Settings(
        _env_file=None,
        agent_engine="opencode",
        coding_enabled=True,
        mattermost_url="http://mm.test",
        database_url="unused",
        OPENAI_API_KEY="test-key",
    )
    store = SimpleNamespace(state=AsyncMock(return_value={"model_id": "openai:gpt-6-luna"}))
    bot = Client("http://mm.test", "test-token")
    runtime = Coordinator(store, bot, uuid4(), settings, SimpleNamespace())
    try:
        assert runtime.settings.agent_engine == "provider"
        assert not runtime.settings.coding_enabled
        assert "code_task" not in {tool.name for tool in runtime.tools.manifests}
        catalog = runtime.tools.catalog
        assert catalog.settings.agent_engine == "opencode"
        assert catalog is runtime.tools.coordination.catalog
        catalog.result = {
            "models": [
                {"id": "openai:" + mid, "api_id": mid, "agent_candidate": True}
                for mid in ["gpt-3.5-turbo", "gpt-6-luna"]
            ]
        }
        catalog.expires = time.monotonic() + 60
        monkeypatch.setattr(
            OpenCode,
            "request",
            AsyncMock(
                return_value={
                    "connected": ["openai"],
                    "all": [
                        {"id": "openai", "models": {"gpt-6-luna": {"limit": {"context": 1000000}}}}
                    ],
                }
            ),
        )
        models = (await catalog.list_for_agents())["models"]
        assert [m["id"] for m in models if m["agent_candidate"]] == ["openai:gpt-6-luna"]
        with pytest.raises(ValueError, match="MODEL_NOT_ALLOWED"):
            await runtime.tools.coordination.reserve(
                runtime.agent_id, "spawn", {"model": "openai:gpt-3.5-turbo"}
            )
    finally:
        runtime.tools.catalog.expires = 0
        runtime.tools.catalog.result = {}
        await bot.http.aclose()
