"""Plain-Markdown wiki access with explicit revision checks."""

import base64
import json
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import quote

Api = Callable[..., Awaitable[dict[str, Any]]]


async def read_page(api: Api, page: str) -> dict[str, Any]:
    result = await api("GET", "/wiki/page/" + quote(page, safe=""))
    if result.get("error"):
        return result
    data = result["data"]
    content = base64.b64decode(data.get("content_base64", ""), validate=True)
    if len(content) > 120_000:
        raise ValueError("WIKI_PAGE_TOO_LARGE: page exceeds readable size")
    return {
        "page": data["title"],
        "markdown": content.decode("utf-8"),
        "revision": data["last_commit"]["sha"],
        "url": data["html_url"],
        "source_type": "shared_workspace_document",
    }


async def write_page(
    api: Api, page: str, markdown: str, expected: str, message: str
) -> dict[str, Any]:
    current = await read_page(api, page)
    missing = current.get("status") == 404
    if current.get("error") and not missing:
        return current
    if (missing and expected) or (not missing and current["revision"] != expected):
        return {
            "error": "WIKI_REVISION_CONFLICT",
            "detail": "Read the current page and reconcile before writing.",
        }
    body = {
        "title": page if missing else current["page"],
        "message": message,
        "content_base64": base64.b64encode(markdown.encode()).decode(),
    }
    result = await api(
        "POST" if missing else "PATCH",
        "/wiki/new" if missing else "/wiki/page/" + quote(page, safe=""),
        json.dumps(body),
    )
    if result.get("error"):
        return result
    data = result["data"]
    return {"page": data["title"], "revision": data["last_commit"]["sha"], "url": data["html_url"]}
