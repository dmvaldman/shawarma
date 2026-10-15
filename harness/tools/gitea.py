"""Repository-scoped API access; credentials never enter model arguments/results."""

import json
from typing import Any
from urllib.parse import unquote, urlsplit

import httpx

from harness.config import Settings
from harness.core.links import public_html_urls


def repository_path(path: str) -> str:
    parsed = urlsplit(path)
    decoded = parsed.path
    for _ in range(5):
        value = unquote(decoded)
        if value == decoded:
            break
        decoded = value
    if (
        parsed.scheme
        or parsed.netloc
        or parsed.fragment
        or not path.startswith("/")
        or "\\" in decoded
        or "%" in decoded
        or any(part in {".", ".."} for part in decoded.split("/"))
    ):
        raise ValueError("GITEA_PATH: use a path relative to the configured repository")
    return "" if path == "/" else path


async def request_repository(
    settings: Settings, method: str, path: str, body: str | None = None, *, token: str | None = None
) -> dict[str, Any]:
    path = repository_path(path)
    data = json.loads(body) if body else None
    if data is not None and not isinstance(data, dict):
        raise ValueError("GITEA_BODY: JSON object required")
    url = (
        str(settings.gitea_url).rstrip("/")
        + "/api/v1/repos/"
        + settings.gitea_owner
        + "/"
        + settings.gitea_repo
        + path
    )
    async with httpx.AsyncClient(timeout=40, follow_redirects=False) as client:
        try:
            response = await client.request(
                method,
                url,
                json=data,
                headers={
                    "Authorization": "token "
                    + (token if token is not None else settings.gitea_token.get_secret_value())
                },
            )
        except httpx.TransportError:
            return {
                "error": "GITEA_TRANSPORT",
                "result_uncertain": method != "GET",
                "detail": "Connection interrupted. Inspect issues/PRs before repeating "
                "a mutation; it may already have succeeded.",
                "retry_after_sec": 60,
            }
    if response.is_error or response.is_redirect:
        return {
            "error": "GITEA_HTTP",
            "status": response.status_code,
            "detail": "Repository API rejected the request; inspect arguments/permissions. "
            "Do not repeat a mutation with an uncertain outcome.",
        }
    return {
        "status": response.status_code,
        "data": public_html_urls(response.json(), settings) if response.content else None,
        "repository": settings.gitea_owner + "/" + settings.gitea_repo,
    }
