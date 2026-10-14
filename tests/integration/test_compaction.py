import json
import os
from decimal import Decimal

import pytest

from harness.core.types import ModelResponse, Usage
from harness.models.context import active_context

pytestmark = pytest.mark.skipif(os.environ.get("HARNESS_LIVE_TESTS") != "1", reason="live gate")


@pytest.mark.parametrize(
    "checkpoint",
    [
        {"type": "compaction", "id": "cmp", "encrypted_content": "opaque-state"},
        {"type": "anthropic_compaction", "block": {"type": "compaction", "content": "summary"}},
        {
            "type": "context_reset",
            "provider": "deepseek",
            "items": [{"role": "user", "content": "retained task"}],
        },
    ],
)
async def test_provider_checkpoint_is_durable_without_deleting_audit_history(
    runtime_store, checkpoint
):
    store, settings, agent, workspace = runtime_store
    old = {"role": "user", "content": "old history"}
    turn = await store.reserve_cost(agent, Decimal("0.01"), [old])
    response = ModelResponse(
        message={"id": "r", "output": [checkpoint], "status": "completed"},
        usage=Usage(input_tokens=10, output_tokens=10, cost_usd=Decimal("0.001")),
    )
    await store.finish(turn, response.usage, response, [old])
    history = await store.history(agent)
    assert old in history and checkpoint in history
    # Fresh reads use persisted blocks exactly, with no process-local checkpoint cache.
    replay = active_context("fixed", await store.history(agent), [])
    assert "old history" not in json.dumps(replay)
    assert replay[1:] == (
        checkpoint["items"] if checkpoint["type"] == "context_reset" else [checkpoint]
    )
