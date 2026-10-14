"""Called only by the authenticated hosting control plane, never by an agent tool."""

import json
from pathlib import Path

from dotenv import dotenv_values

if __name__ == "__main__":
    env = dotenv_values(".env")
    state = json.loads(Path(".state/coding.json").read_text())
    print(
        json.dumps(
            {
                "mattermost_username": "owner",
                "mattermost_password": env["HOSTED_OWNER_PASSWORD"],
                "gitea_username": "swarm-owner",
                "gitea_password": state["admin_password"],
                "opencode_username": "opencode",
                "opencode_password": state["password"],
            }
        )
    )
