import json

from harness.hosting.mattermost_defaults import THEME, owner_preferences


def test_hosted_owner_gets_discord_theme_and_completed_tour():
    prefs = owner_preferences("owner-id", [])
    assert all(p["user_id"] == "owner-id" for p in prefs)
    values = {(p["category"], p["name"]): p["value"] for p in prefs}
    assert json.loads(values["theme", ""]) == THEME
    assert THEME["centerChannelBg"] == "#1a1a1e"
    assert values["tutorial_step", "owner-id"] == "999"
    assert values["tutorial_step", "tutorial_step_auto_tour_status"] == "0"
    assert values["onboarding_task_list", "onboarding_task_list_show"] == "false"


def test_resume_preserves_customized_theme():
    previous = owner_preferences("owner-id", [])
    preferences = owner_preferences("owner-id", previous)
    assert not any(p["category"] == "theme" for p in preferences)
    assert any(p["category"] == "tutorial_step" for p in preferences)
