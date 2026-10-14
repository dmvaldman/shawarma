"""Explicit owner wipe; checkpointed external mutations are replayable after interruption."""

import time
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from harness.config import Settings
from harness.core.presentation import configure_workspace
from harness.core.prompts import birth_record
from harness.core.service_reset import reset_services
from harness.mattermost.reset import (
    clear_channel_posts as clear_channel_posts,
)
from harness.mattermost.reset import (
    delete_external as delete_external,
)
from harness.mattermost.reset import (
    snapshot as snapshot,
)
from harness.persistence.bootstrap import load, save
from harness.persistence.store import Store


async def begin_reset(store: Store, workspace: UUID, request_id: str, path: Path) -> str:
    async with store.pool.acquire() as c, c.transaction():
        await c.fetchrow("SELECT id FROM workspaces WHERE id=$1 FOR UPDATE", workspace)
        previous = await c.fetchval(
            "SELECT status FROM reset_requests WHERE workspace_id=$1 AND request_id=$2",
            workspace,
            request_id,
        )
        if previous == "completed":
            return "completed"
        checkpoint = load(path)
        if UUID(checkpoint["workspace_id"]) != workspace:
            raise ValueError("RESET_CHECKPOINT_MISMATCH")
        if "reset" not in checkpoint:
            checkpoint["reset"] = {
                "request_id": request_id,
                "seed_id": str(uuid4()),
                "created_ms": int(time.time() * 1000),
            }
            save(path, checkpoint)
        request_id = checkpoint["reset"]["request_id"]
        await c.execute(
            "INSERT INTO reset_requests VALUES($1,$2,'pending') ON CONFLICT DO NOTHING",
            workspace,
            request_id,
        )
        await c.execute("UPDATE workspaces SET state='paused' WHERE id=$1", workspace)
    return "pending"


async def purge(store: Store, workspace: UUID, state: dict[str, Any]) -> None:
    """Narrow owner-authorized exception; triggers and foreign keys remain intact after commit."""
    triggers = {
        "events": "event_append_only",
        "agent_activity": "activity_append_only",
        "conversations": "conversation_append_only",
        "agent_prompt_versions": "prompt_version_append_only",
        "subscriptions": "general_subscription",
    }
    async with store.pool.acquire() as c, c.transaction():
        await c.fetchrow("SELECT id FROM workspaces WHERE id=$1 FOR UPDATE", workspace)
        for table, trigger in triggers.items():
            await c.execute(f"ALTER TABLE {table} DISABLE TRIGGER {trigger}")
        for table in (
            "agent_activity",
            "agent_activity_sources",
            "channel_streams",
            "coding_runs",
            "coding_sessions",
            "tool_calls",
            "usage_reservations",
            "agent_turns",
            "notifications",
            "message_cursors",
            "idle_channel_cursors",
            "conversations",
            "agent_tool_versions",
            "agent_profiles",
            "agent_prompt_versions",
            "subscriptions",
        ):
            await c.execute(
                f"DELETE FROM {table} WHERE agent_id IN "
                "(SELECT id FROM agents WHERE workspace_id=$1)",
                workspace,
            )
        await c.execute(
            "DELETE FROM spawn_requests WHERE parent_id IN "
            "(SELECT id FROM agents WHERE workspace_id=$1)",
            workspace,
        )
        for table in (
            "first_deliveries",
            "events",
            "workspace_commands",
            "slash_control_receipts",
            "work_repositories",
        ):
            await c.execute(f"DELETE FROM {table} WHERE workspace_id=$1", workspace)
        await c.execute("DELETE FROM agents WHERE workspace_id=$1", workspace)
        await c.execute(
            "DELETE FROM agent_name_reservations WHERE server_id=$1 AND agent_id=ANY($2::uuid[])",
            state["binding"]["server_id"],
            [UUID(a) for a in state["reset"]["manifest"]["agents"]],
        )
        await c.execute(
            "UPDATE workspaces SET seed_id=$2,general_id=$3,created_ms=$4,"
            "started=false,question=NULL,spent=0,reserved=0,budget=NULL,state='paused' WHERE id=$1",
            workspace,
            UUID(state["reset"]["seed_id"]),
            state["reset"]["fresh"]["general_id"],
            state["reset"]["created_ms"],
        )
        for table, trigger in triggers.items():
            await c.execute(f"ALTER TABLE {table} ENABLE TRIGGER {trigger}")


async def finish_reset(store: Store, settings: Settings, path: Path) -> dict[str, Any]:
    state = load(path)
    plan = state["reset"]
    workspace = UUID(state["workspace_id"])
    await begin_reset(store, workspace, plan["request_id"], path)
    from harness.api.billing import Billing

    # Workers have been drained by Supervisor before this call. Failed access never blocks reset.
    await Billing(store, settings).snapshot(workspace, int(plan["created_ms"]))
    await reset_services(store, settings, state, path)
    await purge(store, workspace, state)
    fresh = {
        "workspace_id": str(workspace),
        "seed_id": plan["seed_id"],
        "state": "ready",
        "created_ms": plan["created_ms"],
        "binding": state["binding"],
        **plan["fresh"],
    }
    fresh["identity"] = birth_record(
        agent_id=UUID(fresh["seed_id"]),
        workspace_id=workspace,
        username=fresh["bot_username"],
        mm_user_id=fresh["bot_user_id"],
        settings=settings,
        inference_enabled=True,
        coordination_enabled=True,
    ).model_dump(mode="json")
    await store.initialize(fresh, settings, fresh["created_ms"])
    # Keep the recovery plan in the checkpoint until reconciliation is fully complete.
    fresh["reset"] = plan
    await configure_workspace(store, settings, fresh, path)
    async with store.pool.acquire() as c, c.transaction():
        await c.execute(
            "UPDATE reset_requests SET status='completed' WHERE workspace_id=$1", workspace
        )
        await c.execute("UPDATE workspaces SET state='active' WHERE id=$1", workspace)
    fresh.pop("reset")
    save(path, fresh)
    return fresh
