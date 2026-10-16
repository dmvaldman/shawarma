"""Read-only, bounded support exports. Executed only after a user grants access."""

import asyncio
import base64
import json
import sys
from typing import Any
from urllib.parse import quote

import httpx

from harness.config import Settings


async def read(request: dict[str, Any]) -> dict[str, Any]:
    settings = Settings()
    kind = request["kind"]
    page = int(request.get("page", 1))
    if not 1 <= page <= 10000:
        raise ValueError("Invalid page")

    def q(value: Any) -> str:
        return quote(str(value), safe="")

    if kind in {"channels", "messages"}:
        async with httpx.AsyncClient(
            base_url=str(settings.mattermost_url).rstrip("/") + "/api/v4/",
            timeout=15,
            headers={
                "Authorization": "Bearer " + settings.mattermost_admin_token.get_secret_value()
            },
        ) as client:
            from pathlib import Path

            state = json.loads(await asyncio.to_thread(Path(".state/workspace.json").read_text))
            channels = await client.get(
                "users/"
                + settings.mattermost_owner_id
                + "/teams/"
                + q(state["team_id"])
                + "/channels"
            )
            channels.raise_for_status()
            items = channels.json()
            if kind == "channels":
                return {"items": [{"id": c["id"], "name": c["display_name"]} for c in items]}
            channel = str(request.get("channel", ""))
            if channel not in {c["id"] for c in items}:
                raise ValueError("Unknown channel")
            posts = await client.get(
                "channels/" + q(channel) + "/posts", params={"page": page - 1, "per_page": 30}
            )
            posts.raise_for_status()
            data = posts.json()
            return {
                "items": [
                    {
                        "id": p["id"],
                        "human": p["user_id"] == settings.mattermost_owner_id
                        and not p["message"].startswith("**Harness log**"),
                        "text": p["message"][:30000],
                        "at": p["create_at"],
                    }
                    for key in data["order"]
                    if not (p := data["posts"][key]).get("delete_at")
                ],
                "page": page,
                "more": len(data["order"]) == 30,
            }
    async with httpx.AsyncClient(
        base_url=str(settings.gitea_url).rstrip("/") + "/api/v1/",
        timeout=15,
        headers={"Authorization": "token " + settings.gitea_token.get_secret_value()},
    ) as client:
        if kind == "repositories":
            result = await client.get(
                "users/" + q(settings.gitea_owner) + "/repos", params={"page": page, "limit": 30}
            )
        else:
            repo = str(request.get("repository", ""))
            if not repo or "/" in repo or repo in {".", ".."}:
                raise ValueError("Invalid repository")
            base = "repos/" + q(settings.gitea_owner) + "/" + q(repo)
            if kind in {"issues", "pulls"}:
                result = await client.get(
                    base + "/" + kind, params={"page": page, "limit": 30, "state": "all"}
                )
            elif kind == "files":
                path = str(request.get("path", ""))
                if any(part in {".", ".."} for part in path.split("/")) or "\\" in path:
                    raise ValueError("Invalid file path")
                result = await client.get(base + "/contents/" + quote(path, safe="/"))
            else:
                raise ValueError("Unsupported support view")
        result.raise_for_status()
        data = result.json()
        if kind == "repositories":
            return {
                "items": [
                    {"name": r["name"], "description": r.get("description", "")[:1000]}
                    for r in data
                ],
                "page": page,
                "more": len(data) == 30,
            }
        if kind in {"issues", "pulls"}:
            return {
                "items": [
                    {
                        "number": i["number"],
                        "title": i["title"],
                        "body": (i.get("body") or "")[:30000],
                        "state": i["state"],
                        "merged": i.get("merged", False),
                    }
                    for i in data
                ],
                "page": page,
                "more": len(data) == 30,
            }
        if isinstance(data, list):
            return {
                "items": [
                    {"name": f["name"], "path": f["path"], "type": f["type"]} for f in data[:200]
                ]
            }
        size = int(data.get("size", 0))
        if size > 65536:
            return {"text": "File exceeds the 64 KB support preview limit."}
        return {
            "text": base64.b64decode(data.get("content", "")).decode("utf-8", errors="replace")[
                :65536
            ]
        }


if __name__ == "__main__":
    print(json.dumps(asyncio.run(read(json.loads(sys.argv[1])))))
