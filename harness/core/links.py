"""Keep internal service transport separate from links shared with the human."""

import re
from typing import Any

from harness.config import Settings


def public_service_links(text: str, settings: Settings) -> str:
    replacements = {}
    for public, internal, port, aliases in (
        (
            settings.gitea_public_url,
            str(settings.gitea_url),
            3000,
            [str(settings.gitea_worker_url)],
        ),
        (settings.mattermost_public_url, str(settings.mattermost_url), 8065, []),
        (settings.opencode_public_url, str(settings.opencode_url), 4096, []),
        (settings.analytics_public_url, "http://localhost:8066", 8066, []),
    ):
        if not public:
            continue
        for origin in [internal, *aliases, f"http://localhost:{port}", f"http://127.0.0.1:{port}"]:
            replacements[origin.rstrip("/")] = public.rstrip("/")
    if not replacements:
        return text
    pattern = "|".join(re.escape(url) for url in sorted(replacements, key=len, reverse=True))
    return re.sub(
        r"(?<![\w/])(?:" + pattern + r")(?=[/?#\s)\]>`]|$)",
        lambda match: replacements[match[0]],
        text,
    )


def public_html_urls(data: Any, settings: Settings) -> Any:
    """Gitea browser links are public; clone/API URLs retain their transport addresses."""
    if isinstance(data, dict):
        return {
            key: public_service_links(value, settings)
            if key == "html_url" and isinstance(value, str)
            else public_html_urls(value, settings)
            for key, value in data.items()
        }
    if isinstance(data, list):
        return [public_html_urls(value, settings) for value in data]
    return data


def service_link_policy(settings: Settings) -> str:
    services = [
        ("Mattermost", settings.mattermost_public_url),
        ("Gitea", settings.gitea_public_url),
        ("OpenCode", settings.opencode_public_url),
        ("Analytics", settings.analytics_public_url),
    ]
    configured = [f"- {name}: {url.rstrip('/')}" for name, url in services if url]
    if not configured:
        return ""
    return (
        "\n## Shared service links\n"
        "Use these public base URLs when sharing links in chat, issues, PRs, or wiki pages. "
        "Preserve the resource path. localhost, 127.0.0.1 and container hostnames are internal "
        "transport addresses, not links for the human. "
        "Keep internal Git/API transport as configured.\n" + "\n".join(configured) + "\n"
    )
