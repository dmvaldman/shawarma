"""Hosted owner display defaults, using Mattermost's native preferences API."""

import json
from pathlib import Path
from typing import Any

THEME = json.loads(Path(__file__).with_name("discord_theme.json").read_text())


def owner_preferences(owner_id: str, existing: list[dict[str, Any]]) -> list[dict[str, str]]:
    values = [
        ("tutorial_step", owner_id, "999"),
        ("tutorial_step", "tutorial_step_auto_tour_status", "0"),
        ("onboarding_task_list", "onboarding_task_list_show", "false"),
        ("onboarding_task_list", "onboarding_task_list_open", "false"),
    ]
    # Apply the theme once, allowing subsequent user customization to survive resumes.
    if not any(p["category"] == "swarm_ui_defaults" and p["name"] == "version" for p in existing):
        values.extend(
            [
                ("theme", "", json.dumps(THEME)),
                ("swarm_ui_defaults", "version", "1"),
            ]
        )
    return [
        {"user_id": owner_id, "category": category, "name": name, "value": value}
        for category, name, value in values
    ]
