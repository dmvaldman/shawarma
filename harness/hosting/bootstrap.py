"""Bootstrap a fresh private tenant VM; never prints service passwords or tokens."""

import asyncio
import json
import subprocess
import sys
from pathlib import Path

import httpx
from dotenv import dotenv_values

from harness.coding.setup import update_env


async def bootstrap() -> None:
    env = dotenv_values(".env")
    if "--finalize" in sys.argv:
        addresses = await asyncio.to_thread(subprocess.check_output, ["hostname", "-I"], text=True)
        update_env(
            {
                "HARNESS_ANALYTICS_BIND_HOST": "127.0.0.1",
                "HARNESS_ANALYTICS_PROXY_HOSTS": json.dumps(addresses.split()),
                "HARNESS_MATTERMOST_PUBLIC_URL": str(env["HOSTED_MATTERMOST_URL"]),
                "HARNESS_GITEA_PUBLIC_URL": str(env["HOSTED_GITEA_URL"]),
                "HARNESS_OPENCODE_PUBLIC_URL": str(env["HOSTED_OPENCODE_URL"]),
                "HARNESS_ANALYTICS_PUBLIC_URL": str(env["HOSTED_ANALYTICS_URL"]),
            }
        )
        return
    async with httpx.AsyncClient(base_url="http://127.0.0.1:8065/api/v4/", timeout=30) as client:
        password = env["HOSTED_OWNER_PASSWORD"]
        response = await client.post(
            "users/login", json={"login_id": "owner", "password": password}
        )
        if response.status_code == 401:
            created = await client.post(
                "users",
                json={
                    "username": "owner",
                    "email": "owner@swarm.invalid",
                    "password": password,
                },
            )
            if created.is_error:
                raise RuntimeError("Fresh Mattermost owner creation failed")
            response = await client.post(
                "users/login", json={"login_id": "owner", "password": password}
            )
        response.raise_for_status()
        owner = response.json()
        if "system_admin" not in owner["roles"].split():
            raise RuntimeError("Tenant owner must administer its own private Mattermost")
        token = str(env.get("HARNESS_MATTERMOST_ADMIN_TOKEN") or "")
        if not token:
            issued = await client.post(
                "users/" + owner["id"] + "/tokens",
                headers={"Authorization": "Bearer " + response.headers["Token"]},
                json={"description": "Hosted swarm controller"},
            )
            issued.raise_for_status()
            token = issued.json()["token"]
        update_env(
            {"HARNESS_MATTERMOST_ADMIN_TOKEN": token, "HARNESS_MATTERMOST_OWNER_ID": owner["id"]}
        )
    # A fresh VM has no existing local bootstrap or user data to replace.
    await asyncio.to_thread(Path(".state").mkdir, mode=0o700, exist_ok=True)


if __name__ == "__main__":
    asyncio.run(bootstrap())
