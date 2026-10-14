import json
import subprocess
from pathlib import Path

import os
import sys
from urllib.parse import urlsplit

if sys.argv[1] == "get":
    fields = dict(line.rstrip().split("=", 1) for line in sys.stdin if "=" in line)
    target = urlsplit(os.environ["SWARM_GITEA_URL"])
    identity = {}
    repo = os.environ.get("SWARM_AGENT_GITEA_REPOSITORY")
    if not repo:
        root = subprocess.run(
            ["git", "rev-parse", "--absolute-git-dir"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        identity = json.loads((Path(root) / "swarm-credential.json").read_text())
        repo = identity.get("repository") or (
            os.environ["SWARM_GITEA_OWNER"] + "/" + os.environ["SWARM_GITEA_REPO"]
        )
    repo += ".git"
    if (
        fields.get("protocol") == target.scheme
        and fields.get("host") == target.netloc
        and fields.get("path") == repo
    ):
        username = os.environ.get("SWARM_AGENT_GITEA_USERNAME")
        token = os.environ.get("SWARM_AGENT_GITEA_TOKEN")
        if not username or not token:
            root = subprocess.run(
                ["git", "rev-parse", "--absolute-git-dir"],
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip()
            identity = json.loads((Path(root) / "swarm-credential.json").read_text())
            username, token = identity["username"], identity["token"]
        print("username=" + username)
        print("password=" + token)
