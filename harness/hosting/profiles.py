"""Human-facing service names, independent of stable owner IDs and credentials."""

import re
import unicodedata
from typing import Any

import httpx


def human_username(name: str) -> str:
    ascii_name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower()
    name = re.sub(r"[^a-z0-9]+", "-", ascii_name).strip("-") or "human"
    if len(name) < 3 or name in {"admin", "system", "all", "channel", "here", "owner"}:
        name += "-user"
    return name[:22].rstrip("-")


async def mattermost_profile(
    client: httpx.AsyncClient, owner: dict[str, Any], token: str, name: str
) -> str:
    headers = {"Authorization": "Bearer " + token}
    base = human_username(name)
    # Keep an existing agent's handle intact if it already has the person's name.
    for suffix in range(100):
        tail = str(suffix + 1) if suffix else ""
        username = base[: 22 - len(tail)] + tail
        found = await client.get("users/username/" + username, headers=headers)
        if found.status_code == 404 or (found.is_success and found.json()["id"] == owner["id"]):
            break
        found.raise_for_status()
    else:
        raise RuntimeError("No available Mattermost handle for the human")
    first, _, last = name.partition(" ")
    response = await client.put(
        "users/" + owner["id"] + "/patch",
        headers=headers,
        json={"username": username, "first_name": first, "last_name": last, "nickname": name},
    )
    response.raise_for_status()
    return username


async def gitea_profile(name: str, state: dict[str, Any]) -> None:
    async with httpx.AsyncClient(base_url=state["gitea_url"] + "/api/v1/", timeout=30) as client:
        response = await client.patch(
            "user/settings", auth=("swarm-owner", state["admin_password"]), json={"full_name": name}
        )
        response.raise_for_status()
