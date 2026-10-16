from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError

from harness.config import Settings
from harness.core.prompts import birth_record


def config(**overrides):
    return Settings(
        _env_file=None,
        mattermost_url="https://mm.example.test",
        database_url="postgresql+asyncpg://harness:test@db/harness",
        **overrides,
    )


@pytest.mark.parametrize(
    "key",
    [
        "max_active_agents",
        "max_tokens_per_turn",
        "tool_timeout_sec",
    ],
)
def test_limits_are_explicitly_positive(key):
    with pytest.raises(ValidationError):
        config(**{key: 0})


def test_default_capacity_is_finite_and_no_budget_settings():
    settings = config()
    assert "max_workspace_cost_usd" not in settings.model_dump()
    assert "max_agent_cost_usd" not in settings.model_dump()
    assert settings.max_active_agents == 100
    assert "max_agents_total" not in settings.model_dump()
    assert "max_child_spawns_per_agent" not in settings.model_dump()


def test_rendered_prompt_is_frozen_and_independent_of_template_changes():
    record = birth_record(
        agent_id=uuid4(),
        workspace_id=uuid4(),
        username="a-test",
        mm_user_id="bot-id",
        settings=config(),
    )
    original = record.system_prompt
    with pytest.raises(ValidationError):
        record.system_prompt = "replacement"
    newer = birth_record(
        agent_id=uuid4(),
        workspace_id=record.workspace_id,
        username="a-next",
        mm_user_id="next",
        settings=config(),
        template="Changed {role_instructions}",
    )
    assert record.system_prompt == original
    assert record.template_revision != newer.template_revision
    assert record.parent_id is None
    assert "You have no assigned specialty" in original
    assert "HARNESS_MATTERMOST_ADMIN_TOKEN" not in original


def test_template_is_exactly_specification_text():
    spec = Path("docs/ARCHITECTURE.md").read_text()
    template = spec.split("## 22. Shared system-prompt template")[1]
    template = template.split("```text\n")[1].split("```")[0]
    assert Path("prompts/common_agent.md").read_text() == template


def test_architecture_requested_copy_matches_canonical():
    assert Path("docs/ARCHITECTURE.md").read_bytes() == Path("docs/ARCHITEFCTURE.md").read_bytes()


@pytest.mark.parametrize("value", [float("inf"), float("nan"), -1])
def test_timeouts_must_be_finite_and_positive(value):
    with pytest.raises(ValidationError):
        config(tool_timeout_sec=value)
