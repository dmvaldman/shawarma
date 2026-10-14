from importlib.resources import files
from uuid import uuid4

import asyncpg
import pytest_asyncio

from harness.config import Settings
from harness.core.prompts import birth_record
from harness.persistence.store import Store


@pytest_asyncio.fixture
async def runtime_store(monkeypatch):
    # Runtime tests exercise controls without binding ports or registering real integrations.
    from unittest.mock import AsyncMock

    monkeypatch.setattr(
        "harness.api.billing.Billing.snapshot",
        AsyncMock(
            return_value={"providers": [], "reported_cost_change_usd": None, "complete": False}
        ),
    )
    monkeypatch.setattr("harness.api.analytics.Analytics.start", AsyncMock())
    monkeypatch.setattr("harness.api.analytics.Analytics.close", AsyncMock())
    monkeypatch.setattr("harness.api.commands.SlashCommands.start", AsyncMock())
    monkeypatch.setattr("harness.api.commands.SlashCommands.close", AsyncMock())
    settings = Settings(
        agent_engine="provider",
        channel_delivery_min_sec=0,
        channel_delivery_max_sec=0,
        typing_yield_max_sec=0,
        coding_enabled=False,
        avatars_enabled=False,
    )
    from harness.models.catalog import ModelCatalog, profile

    async def resolve(self, value):
        if value not in {settings.openai_model_id, "openai:" + settings.openai_model_id}:
            raise ValueError("MODEL_NOT_ALLOWED")
        return {
            "id": "openai:" + settings.openai_model_id,
            "provider": "openai",
            "api_id": settings.openai_model_id,
            **profile("openai", settings.openai_model_id, settings),
        }

    monkeypatch.setattr(ModelCatalog, "resolve", resolve)
    url = settings.database_url.replace("postgresql+asyncpg://", "postgresql://")
    schema = "test_" + uuid4().hex
    admin = await asyncpg.connect(url)
    await admin.execute(f'CREATE SCHEMA "{schema}"')
    pool = await asyncpg.create_pool(
        url, min_size=1, max_size=6, server_settings={"search_path": schema}
    )
    store = Store(pool)
    await pool.execute(
        files("harness.persistence").joinpath("migrations/001_runtime.sql").read_text()
    )
    await pool.execute(
        files("harness.persistence").joinpath("migrations/002_coordination.sql").read_text()
    )
    await pool.execute(
        files("harness.persistence").joinpath("migrations/003_identity_models.sql").read_text()
    )
    await pool.execute(
        files("harness.persistence").joinpath("migrations/004_delivery.sql").read_text()
    )
    await pool.execute(
        files("harness.persistence").joinpath("migrations/005_names.sql").read_text()
    )
    await pool.execute(
        files("harness.persistence").joinpath("migrations/006_controls.sql").read_text()
    )
    await pool.execute(
        files("harness.persistence").joinpath("migrations/007_no_turn_limits.sql").read_text()
    )
    await pool.execute(
        files("harness.persistence").joinpath("migrations/008_immediate_delivery.sql").read_text()
    )
    await pool.execute(
        files("harness.persistence").joinpath("migrations/009_no_budgets.sql").read_text()
    )
    await pool.execute(
        files("harness.persistence").joinpath("migrations/010_slash_controls.sql").read_text()
    )
    await pool.execute(
        files("harness.persistence").joinpath("migrations/011_reset.sql").read_text()
    )
    await pool.execute(
        files("harness.persistence").joinpath("migrations/012_idle_catchup.sql").read_text()
    )
    await pool.execute(
        files("harness.persistence").joinpath("migrations/013_provider_usage.sql").read_text()
    )
    await pool.execute(
        files("harness.persistence").joinpath("migrations/014_turn_model.sql").read_text()
    )
    await pool.execute(
        files("harness.persistence").joinpath("migrations/015_agent_failure.sql").read_text()
    )
    await pool.execute(
        files("harness.persistence").joinpath("migrations/016_billing_baselines.sql").read_text()
    )
    await pool.execute(
        files("harness.persistence").joinpath("migrations/017_coding.sql").read_text()
    )
    agent, workspace = uuid4(), uuid4()
    await pool.execute(
        files("harness.persistence").joinpath("migrations/018_native_agents.sql").read_text()
    )
    await pool.execute(
        files("harness.persistence").joinpath("migrations/019_work_activity.sql").read_text()
    )
    await pool.execute(
        files("harness.persistence").joinpath("migrations/020_channel_streams.sql").read_text()
    )
    await pool.execute(
        files("harness.persistence").joinpath("migrations/021_agent_activity.sql").read_text()
    )
    identity = birth_record(
        agent_id=agent,
        workspace_id=workspace,
        username="bot",
        mm_user_id="bot",
        settings=settings,
        inference_enabled=True,
    )
    await store.initialize(
        {
            "identity": identity.model_dump(),
            "team_name": "test",
            "team_id": "team",
            "general_id": "general",
        },
        settings,
        1,
    )
    await pool.execute(
        files("harness.persistence").joinpath("migrations/022_gitea_identities.sql").read_text()
    )
    await pool.execute(
        files("harness.persistence").joinpath("migrations/023_work_repositories.sql").read_text()
    )
    await pool.execute(
        files("harness.persistence").joinpath("migrations/024_agent_avatars.sql").read_text()
    )
    try:
        yield store, settings, agent, workspace
    finally:
        await store.close()
        await admin.execute(f'DROP SCHEMA "{schema}" CASCADE')
        await admin.close()
