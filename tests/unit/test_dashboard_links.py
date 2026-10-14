import base64

from harness.api.analytics import service_links
from harness.config import Settings


def test_service_links_open_actual_agent_session_without_credentials():
    directory = "/workspaces/project/agent ü"
    data = {
        "agents": [
            {"state": "RUNNING", "opencode_directory": directory, "opencode_session_id": "ses_one"},
            {"state": "READY", "opencode_directory": None, "opencode_session_id": None},
        ]
    }
    settings = Settings(opencode_password="private-password", gitea_token="private-token")
    service_links(data, settings)
    url = data["agents"][0]["opencode_url"]
    encoded = url.split("/")[-3]
    assert base64.urlsafe_b64decode(encoded + "=" * ((-len(encoded)) % 4)).decode() == directory
    assert url.endswith("/session/ses_one")
    assert data["services"]["opencode"] == url
    assert data["services"]["gitea"].endswith("/swarm/workspace")
    assert data["services"]["mattermost"] == str(settings.mattermost_url)
    assert "private-" not in str(data)
    assert "opencode_directory" not in data["agents"][0]
    assert data["agents"][1]["opencode_url"] is None


def test_reset_falls_back_to_opencode_home_without_stale_session():
    data = {"agents": []}
    settings = Settings()
    service_links(data, settings)
    assert data["services"]["opencode"] == str(settings.opencode_url)
