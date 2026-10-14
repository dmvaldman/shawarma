import json
import os
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest

from harness.models.catalog import profile
from harness.tools.core import CoreTools

pytestmark = pytest.mark.skipif(os.environ.get("HARNESS_LIVE_TESTS") != "1", reason="live gate")


async def test_list_models_returns_current_catalog_schema_and_continues(runtime_store, monkeypatch):
    store, settings, agent, _ = runtime_store
    models = [
        {
            "id": f"openai:model-{i}",
            "agent_candidate": True,
            **profile("openai", settings.openai_model_id, settings),
        }
        for i in range(3)
    ]
    assert all("estimated_max_request_usd" not in m for m in models)
    tools = CoreTools(store, object(), agent, settings)
    monkeypatch.setattr(
        tools.catalog, "list", AsyncMock(return_value={"models": models, "errors": {}})
    )
    turn = await store.reserve_cost(agent, Decimal("0.01"), [])
    await tools.execute(
        turn, {"name": "list_models", "call_id": "models", "arguments": json.dumps({"limit": 2})}
    )
    row = await store.pool.fetchrow("SELECT state,result FROM tool_calls WHERE call_id='models'")
    result = json.loads(row["result"])
    assert row["state"] == "completed"
    assert result["total"] == 3 and result["next_cursor"] == 2
    assert [m["id"] for m in result["models"]] == ["openai:model-0", "openai:model-1"]
    assert result["models"][0]["input"] == models[0]["input"]
    assert all("pricing_basis" not in m and "agent_candidate" not in m for m in result["models"])
    assert profile("deepseek", "deepseek-flash", settings)["cost"] == "c1"
    assert profile("deepseek", "deepseek-flash", settings)["class"] == "lite"
    history = await store.history(agent)
    assert history[-1]["type"] == "function_call_output"
    assert json.loads(history[-1]["output"]) == result
