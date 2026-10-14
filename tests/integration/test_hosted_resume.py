"""Resuming preserves immutable agent records across model ID formatting changes."""

import os

import pytest

pytestmark = pytest.mark.skipif(os.environ.get("HARNESS_LIVE_TESTS") != "1", reason="Postgres gate")


async def test_resume_never_rewrites_existing_seed_model(runtime_store):
    store, settings, agent, _ = runtime_store
    identity = await store.get(agent)
    original = await store.pool.fetchrow("SELECT identity,model_id FROM agents WHERE id=$1", agent)
    await store.initialize(
        {
            "identity": identity.model_dump(),
            "team_name": "test",
            "team_id": "team",
            "general_id": "general",
        },
        settings.model_copy(update={"seed_model": "openai:" + settings.openai_model_id}),
        1,
    )
    resumed = await store.pool.fetchrow("SELECT identity,model_id FROM agents WHERE id=$1", agent)
    assert dict(resumed) == dict(original)
