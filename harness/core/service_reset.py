"""Checkpointed service orchestration; no fresh agent starts until every reset succeeds."""

import logging
from pathlib import Path
from typing import Any
from uuid import UUID

from harness.coding.reset import prepare, reset_gitea, reset_opencode
from harness.config import Settings
from harness.mattermost.client import Client
from harness.mattermost.reset import reset as reset_mattermost
from harness.mattermost.reset import snapshot
from harness.persistence.bootstrap import save
from harness.persistence.store import Store

logger = logging.getLogger(__name__)


async def reset_services(
    store: Store, settings: Settings, state: dict[str, Any], path: Path
) -> None:
    plan = state["reset"]
    # Validate ownership before any service erases data.
    if "manifest" not in plan:
        async with Client(
            str(settings.mattermost_url), settings.mattermost_admin_token.get_secret_value()
        ) as admin:
            plan["manifest"] = await snapshot(store, admin, state)
            save(path, state)
    await prepare(store, settings, state, path)
    completed = plan.setdefault("services", {})
    for name, reset in (
        ("opencode", reset_opencode),
        ("gitea", reset_gitea),
        ("mattermost", reset_mattermost),
    ):
        if completed.get(name) == "completed":
            continue
        logger.info("Resetting service %s for workspace %s", name, UUID(state["workspace_id"]))
        await reset(store, settings, state, path)
        completed[name] = "completed"
        save(path, state)
