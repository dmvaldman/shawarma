from hashlib import sha256
from importlib.resources import files
from pathlib import Path
from uuid import UUID

from harness.config import Settings
from harness.core.types import AgentIdentity

SEED_ROLE = "You have no assigned specialty; decide what work is useful."


def birth_record(
    *,
    agent_id: UUID,
    workspace_id: UUID,
    username: str,
    mm_user_id: str,
    settings: Settings,
    parent_id: UUID | None = None,
    role_instructions: str = SEED_ROLE,
    template: str | None = None,
    inference_enabled: bool = False,
    coordination_enabled: bool = False,
) -> AgentIdentity:
    if parent_id is None and coordination_enabled:
        settings = settings.model_copy(update={"agent_engine": "provider", "coding_enabled": False})
    if template is None:
        source = Path(__file__).resolve().parents[2] / "prompts" / "common_agent.md"
        template = (
            source.read_text()
            if source.exists()
            else files("harness").joinpath("prompts/common_agent.md").read_text()
        )
    rendered = template.format(
        agent_id=agent_id,
        workspace_id=workspace_id,
        mattermost_username=username,
        parent_agent_id_or_none=parent_id or "none",
        role_instructions=role_instructions,
    )
    rendered += (
        "\n## Harness Operational Policy\n"
        "No internal dollar budget or model-request-count limit.\n"
        f"Active agent cap: {settings.max_active_agents}; "
        + (
            "native working context/output follows the assigned model; "
            if settings.agent_engine == "opencode"
            else f"per-turn token cap: {settings.max_tokens_per_turn}; "
        )
        + f"tool timeout: {settings.tool_timeout_sec} seconds.\n"
        "Termination is restricted to yourself or descendants; it never cascades.\n"
        + (
            "Callable tools: post_channel, read_channel, get_thread, get_status, "
            "set_status, get_notifications, web_search, web_fetch. "
            "Spawning and custom tools are not installed yet.\n"
            if inference_enabled and not coordination_enabled
            else (
                "Core coordination tools are installed, including spawn_agent, list_agents, "
                "get_agent_info and terminate_agent. Discover schemas in the callable manifest. "
                "Custom Python tools are not installed yet.\n"
                if coordination_enabled
                else "M1 communication proof has no model-callable tools or inference enabled.\n"
            )
        )
    )
    return AgentIdentity(
        id=agent_id,
        workspace_id=workspace_id,
        parent_id=parent_id,
        mattermost_user_id=mm_user_id,
        mattermost_username=username,
        system_prompt=rendered,
        template_revision="1.33.0:" + sha256(template.encode()).hexdigest(),
        role_instructions=role_instructions,
    )
