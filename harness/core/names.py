import re
from typing import Any
from uuid import UUID


def slug(value: str, length: int = 12) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")[:length].strip("-") or "peer"


def role_name(instructions: str, supplied: str = "", seed: bool = False) -> str:
    if supplied:
        return slug(supplied)
    if seed:
        return "seed"
    for word, name in (
        ("newton", "newt"),
        ("binary", "bisq"),
        ("review", "audit"),
        ("test", "probe"),
        ("research", "rsh"),
        ("design", "dsgn"),
        ("code", "forge"),
    ):
        if word in instructions.lower():
            return name
    return slug(instructions.splitlines()[0])


def agent_name(role: str, number: int = 1) -> tuple[str, str]:
    name = slug(role) + (str(number) if number > 1 else "")
    return name, name


def agent_bio(workspace: UUID, agent: UUID, kind: str, role: str, metadata: dict[str, Any]) -> str:
    model = re.sub(r"-\d{8}$", "", metadata["api_id"].removeprefix("claude-"))
    model = model.replace("-", " ")
    model = re.sub(r"(?<=\d) (?=\d)", ".", model)
    return model if len(model) <= 20 else model[:19].rstrip() + "…"


def owns_bot(description: str, workspace: UUID, agent: UUID, kind: str) -> bool:
    """Legacy bootstrap marker only; registered bots use private ID bindings."""
    return description.split("\n", 1)[0] == (f"Agent Company workspace {workspace}; {kind} {agent}")


def owned_account(bot: dict[str, Any], user_id: str, admin_id: str) -> bool:
    return bot.get("user_id") == user_id and bot.get("owner_id") == admin_id
