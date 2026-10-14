"""Agent-authorized births, finite allocations, and narrow ancestry authority."""

import json
import time
from typing import Any
from uuid import UUID, uuid4

from harness.config import Settings
from harness.core.names import agent_bio, owned_account, owns_bot, role_name
from harness.core.prompts import birth_record
from harness.mattermost.bot_reminders import remove_setup_reminder
from harness.mattermost.client import Client, MattermostError
from harness.mattermost.provisioning import available_name, membership
from harness.mattermost.quiet_bots import quiet_bot
from harness.models.catalog import get_catalog
from harness.persistence.store import QuotaError, Store, decode


class Coordination:
    def __init__(self, store: Store, settings: Settings):
        self.store, self.settings = store, settings
        self.catalog = get_catalog(settings)

    async def reserve(self, parent_id: UUID, call_id: str, args: dict[str, Any]) -> UUID:
        parent = await self.store.state(parent_id)
        model = args.get("model", parent["model_id"])
        metadata = await self.catalog.resolve_for_agent(model)
        args = {**args, "model": metadata["id"]}
        if args.get("tools"):
            raise ValueError("CUSTOM_TOOLS_UNAVAILABLE: M4; core tools are always inherited")
        if "subscriptions" in args and (
            not isinstance(args["subscriptions"], list)
            or len(args["subscriptions"]) > 32
            or not all(isinstance(channel, str) and channel for channel in args["subscriptions"])
        ):
            raise ValueError("INVALID_INITIAL_SUBSCRIPTIONS")
        async with self.store.pool.acquire() as c, c.transaction():
            w = await c.fetchrow(
                "SELECT * FROM workspaces WHERE id=$1 FOR UPDATE", parent["workspace_id"]
            )
            assert w is not None
            p = await c.fetchrow("SELECT * FROM agents WHERE id=$1 FOR UPDATE", parent_id)
            assert p is not None
            previous = await c.fetchrow(
                "SELECT * FROM spawn_requests WHERE parent_id=$1 AND call_id=$2", parent_id, call_id
            )
            if previous:
                if previous["status"] == "ready":
                    return UUID(str(previous["child_id"]))
                raise ValueError("SPAWN_INTERRUPTED: previous attempt requires recovery")
            if (
                w["state"] != "active"
                or p["control_paused"]
                or p["state"] in {"TERMINATED", "FAILED", "QUOTA_BLOCKED"}
            ):
                raise ValueError("PARENT_NOT_ACTIVE")
            limits = decode(w["limits"])
            counts = await c.fetchrow(
                "SELECT count(*) total,count(*) FILTER (WHERE state!='TERMINATED') active "
                "FROM agents WHERE workspace_id=$1",
                w["id"],
            )
            pending = await c.fetchval(
                "SELECT count(*) FROM spawn_requests s JOIN agents a ON a.id=s.parent_id "
                "WHERE a.workspace_id=$1 AND s.status='provisioning'",
                w["id"],
            )
            assert counts is not None
            if counts["active"] + pending >= int(limits["max_active_agents"]):
                raise QuotaError(
                    "CAPACITY_EXCEEDED: ask your parent to deny, free a descendant slot, "
                    "or escalate; retry only after a slot is granted"
                )
            child = uuid4()
            if "subscriptions" in args:
                inherited = dict.fromkeys(args["subscriptions"], "all")
            else:
                subscribed = await c.fetch(
                    "SELECT channel_id,mode FROM subscriptions WHERE agent_id=$1 "
                    "AND mode IN ('all','mentions')",
                    parent_id,
                )
                inherited = {row["channel_id"]: row["mode"] for row in subscribed}
            if args.get("repository"):
                repo_name = args["repository"]
                if repo_name != self.settings.gitea_repo:
                    valid = await c.fetchval(
                        "SELECT repository_id FROM work_repositories WHERE workspace_id=$1 "
                        "AND name=$2 AND server_url=$3 AND owner_name=$4",
                        parent["workspace_id"],
                        repo_name,
                        str(self.settings.gitea_url).rstrip("/"),
                        self.settings.gitea_owner,
                    )
                    if not valid:
                        raise ValueError("REPOSITORY_UNAVAILABLE: use list_repositories")
            else:
                bindings = await c.fetch(
                    "SELECT DISTINCT repository_name FROM channel_repositories "
                    "WHERE workspace_id=$1 AND channel_id=ANY($2::text[])",
                    parent["workspace_id"],
                    list(inherited),
                )
                if len(bindings) > 1:
                    raise ValueError("REPOSITORY_AMBIGUOUS: specify repository for this child")
                repo_name = (
                    bindings[0]["repository_name"]
                    if bindings
                    else await c.fetchval(
                        "SELECT repository_name FROM agent_repositories WHERE agent_id=$1",
                        parent_id,
                    )
                )
                repo_name = repo_name or self.settings.gitea_repo
            args = {**args, "_subscription_modes": inherited, "_repository": repo_name}
            budget = None  # No allocation or dollar limit; retain historical storage.
            await c.execute(
                "INSERT INTO spawn_requests(parent_id,call_id,child_id,budget,request,created_ms) "
                "VALUES($1,$2,$3,$4,$5,$6)",
                parent_id,
                call_id,
                child,
                budget,
                json.dumps(args),
                int(time.time() * 1000),
            )
            username = await self.store.reserve_name(
                self.settings.mattermost_server_id,
                child,
                role_name(args["system_prompt"], args.get("name", "")),
                connection=c,
            )
            await c.execute(
                "UPDATE spawn_requests SET bot_username=$2 WHERE child_id=$1", child, username
            )
            return child

    async def fail(self, parent: UUID, call_id: str) -> None:
        async with self.store.pool.acquire() as c, c.transaction():
            await c.execute(
                "UPDATE spawn_requests SET status='failed' WHERE parent_id=$1 AND call_id=$2 "
                "AND status='provisioning'",
                parent,
                call_id,
            )

    async def spawn(self, parent: UUID, call_id: str, args: dict[str, Any]) -> dict[str, Any]:
        child = await self.reserve(parent, call_id, args)
        existing = await self.store.pool.fetchval("SELECT id FROM agents WHERE id=$1", child)
        if existing:
            return await self.info(child, (await self.store.state(parent))["workspace_id"])
        request = await self.store.pool.fetchrow(
            "SELECT * FROM spawn_requests WHERE child_id=$1", child
        )
        assert request is not None
        p = await self.store.state(parent)
        metadata = await self.catalog.resolve(args.get("model", p["model_id"]))
        role = role_name(args["system_prompt"], args.get("name", ""))
        username = request["bot_username"]
        display_name = username
        bot_id = ""
        async with Client(
            str(self.settings.mattermost_url),
            self.settings.mattermost_admin_token.get_secret_value(),
        ) as admin:
            try:
                requested = decode(request["request"])
                off_topic = await admin.request(
                    "GET", f"teams/{p['team_id']}/channels/name/off-topic"
                )
                extra_channels = {}
                for channel_id, mode in requested.get("_subscription_modes", {}).items():
                    if channel_id in {p["general_id"], off_topic["id"]}:
                        continue
                    channel = await admin.request("GET", f"channels/{channel_id}")
                    if (
                        channel.get("team_id") != p["team_id"]
                        or channel.get("type") not in {"O", "P"}
                        or channel.get("delete_at")
                    ):
                        if "subscriptions" in requested:
                            raise ValueError("INITIAL_CHANNEL_OUTSIDE_WORKSPACE_OR_ARCHIVED")
                        continue  # A formerly subscribed work channel was archived.
                    extra_channels[channel_id] = mode
                username = await available_name(
                    self.store, admin, self.settings.mattermost_server_id, child, role
                )
                display_name = username
                await self.store.pool.execute(
                    "UPDATE spawn_requests SET bot_username=$2 WHERE child_id=$1", child, username
                )
                bot = await quiet_bot(
                    admin,
                    p["workspace_id"],
                    child,
                    username,
                    display_name,
                    agent_bio(p["workspace_id"], child, "child", args["system_prompt"], metadata),
                )
                bot_id = bot["user_id"]
                await membership(admin, p["team_id"], p["general_id"], bot_id)
                off_topic = await admin.request(
                    "GET", f"teams/{p['team_id']}/channels/name/off-topic"
                )
                await membership(admin, p["team_id"], off_topic["id"], bot_id)
                for channel_id in extra_channels:
                    await membership(admin, p["team_id"], channel_id, bot_id)
                await remove_setup_reminder(admin, bot_id, (await admin.me())["id"])
                token = await admin.request(
                    "POST",
                    f"users/{bot_id}/tokens",
                    json={"description": f"harness:{p['workspace_id']}:{child}"},
                )
                identity = birth_record(
                    agent_id=child,
                    workspace_id=p["workspace_id"],
                    parent_id=parent,
                    username=username,
                    mm_user_id=bot_id,
                    settings=self.settings,
                    role_instructions=args["system_prompt"],
                    inference_enabled=True,
                    coordination_enabled=True,
                )
                async with self.store.pool.acquire() as c, c.transaction():
                    w = await c.fetchrow(
                        "SELECT state FROM workspaces WHERE id=$1 FOR UPDATE", p["workspace_id"]
                    )
                    current = await c.fetchval("SELECT state FROM agents WHERE id=$1", parent)
                    if not w or w["state"] != "active" or current == "TERMINATED":
                        raise ValueError("PARENT_NOT_ACTIVE")
                    await c.execute(
                        "INSERT INTO agents(id,workspace_id,parent_id,identity,model_id,budget,"
                        "credential,state,birth_ms) VALUES($1,$2,$3,$4,$5,$6,$7,'READY',$8)",
                        child,
                        p["workspace_id"],
                        parent,
                        identity.model_dump_json(),
                        metadata["id"],
                        request["budget"],
                        token["token"],
                        request["created_ms"],
                    )
                    await c.execute(
                        "INSERT INTO agent_profiles VALUES($1,$2,$3,$4,$5)",
                        child,
                        username,
                        display_name,
                        role,
                        json.dumps(metadata),
                    )
                    await c.execute(
                        "INSERT INTO subscriptions VALUES($1,$2,'all')", child, p["general_id"]
                    )
                    await c.execute(
                        "INSERT INTO subscriptions VALUES($1,$2,'all')", child, off_topic["id"]
                    )
                    for channel_id, mode in extra_channels.items():
                        await c.execute(
                            "INSERT INTO subscriptions VALUES($1,$2,$3)", child, channel_id, mode
                        )
                    repo_name = requested.get("_repository", self.settings.gitea_repo)
                    await c.execute(
                        "INSERT INTO agent_repositories VALUES($1,$2)", child, repo_name
                    )
                    await c.execute(
                        "INSERT INTO conversations(agent_id,item) VALUES($1,$2)",
                        child,
                        json.dumps(
                            {
                                "role": "user",
                                "content": json.dumps(
                                    {
                                        "source": "trusted child startup",
                                        "parent_agent_id": str(parent),
                                        "parent_mattermost_user_id": decode(p["identity"])[
                                            "mattermost_user_id"
                                        ],
                                        "workspace_id": str(p["workspace_id"]),
                                        "general_channel_id": p["general_id"],
                                        "work_channel_ids": list(extra_channels),
                                        "repository": repo_name,
                                        "initial_task": args["initial_task"],
                                        "tools": "Core and Mattermost callable manifest",
                                    }
                                ),
                            }
                        ),
                    )
                    await c.execute(
                        "UPDATE spawn_requests SET status='ready' WHERE child_id=$1", child
                    )
                    await c.execute(
                        "INSERT INTO events(workspace_id,actor_id,type,data) "
                        "VALUES($1,$2,'agent_spawned',$3)",
                        p["workspace_id"],
                        parent,
                        json.dumps({"child_id": str(child)}),
                    )
                    await c.execute("SELECT pg_notify('harness_wake',$1)", str(child))
            except BaseException:
                if bot_id:
                    await admin.request("POST", f"bots/{bot_id}/disable")
                await self.fail(parent, call_id)
                raise
        result = await self.info(child, p["workspace_id"])
        counts = await self.store.pool.fetchrow(
            "SELECT count(*) total,count(*) FILTER (WHERE state!='TERMINATED') active "
            "FROM agents WHERE workspace_id=$1",
            p["workspace_id"],
        )
        result["quota_snapshot"] = dict(counts) if counts else {}
        return result

    async def recover(self, workspace: UUID) -> None:
        rows = await self.store.pool.fetch(
            "SELECT s.* FROM spawn_requests s JOIN agents a ON a.id=s.parent_id "
            "WHERE a.workspace_id=$1 AND s.status='provisioning'",
            workspace,
        )
        async with Client(
            str(self.settings.mattermost_url),
            self.settings.mattermost_admin_token.get_secret_value(),
        ) as admin:
            for row in rows:
                try:
                    user = await admin.request(
                        "GET",
                        "users/username/"
                        + (row["bot_username"] or f"a-{row['child_id'].hex[:12]}"),
                    )
                    bot = await admin.request("GET", f"bots/{user['id']}")
                    admin_user = await admin.me()
                    if not (
                        owns_bot(bot.get("description", ""), workspace, row["child_id"], "child")
                        or owned_account(bot, user["id"], admin_user["id"])
                    ):
                        raise ValueError("orphan bot ownership mismatch")
                    await admin.request("POST", f"bots/{user['id']}/disable")
                except MattermostError as error:
                    if error.status != 404:
                        raise
                await self.fail(row["parent_id"], row["call_id"])

    async def info(self, agent: UUID, workspace: UUID) -> dict[str, Any]:
        a = await self.store.state(agent)
        if a["workspace_id"] != workspace:
            raise ValueError("AGENT_OUTSIDE_WORKSPACE")
        identity = await self.store.get(agent)
        public = await self.store.public_profile(agent)
        children = await self.store.pool.fetch("SELECT id FROM agents WHERE parent_id=$1", agent)
        return {
            "agent_id": str(agent),
            "parent_id": str(identity.parent_id) if identity.parent_id else None,
            "mattermost_user_id": identity.mattermost_user_id,
            "bot_username": public.get("username", identity.mattermost_username),
            "gitea_username": await self.store.pool.fetchval(
                "SELECT username FROM gitea_identities WHERE agent_id=$1", agent
            ),
            "display_name": public.get("display_name", identity.mattermost_username),
            "state": a["state"],
            "availability": a["availability"],
            "control_paused": a["control_paused"],
            "model": a["model_id"],
            "child_ids": [str(r["id"]) for r in children],
            "subscriptions": [
                dict(row)
                for row in await self.store.pool.fetch(
                    "SELECT channel_id,mode FROM subscriptions WHERE agent_id=$1 "
                    "ORDER BY channel_id",
                    agent,
                )
            ],
            "estimated_usage_usd": str(a["spent"]),
            "last_activity": str(
                await self.store.pool.fetchval(
                    "SELECT max(finished_at) FROM agent_turns WHERE agent_id=$1", agent
                )
                or a["created_at"]
            ),
            "repository": await self.store.pool.fetchval(
                "SELECT repository_name FROM agent_repositories WHERE agent_id=$1", agent
            ) or self.settings.gitea_repo,
            "capabilities": "core coordination and Mattermost; "
            + ("OpenCode coding workspace; " if self.settings.coding_enabled else "")
            + "custom Python tools unavailable",
        }

    async def terminate(self, caller: UUID, target: UUID, reason: str) -> dict[str, Any]:
        p = await self.store.state(caller)
        async with self.store.pool.acquire() as c, c.transaction():
            await c.execute("SELECT id FROM workspaces WHERE id=$1 FOR UPDATE", p["workspace_id"])
            a = await c.fetchrow("SELECT * FROM agents WHERE id=$1 FOR UPDATE", target)
            if not a or a["workspace_id"] != p["workspace_id"]:
                raise ValueError("AGENT_OUTSIDE_WORKSPACE")
            descendant = await c.fetchval(
                "WITH RECURSIVE branch AS ("
                "SELECT id FROM agents WHERE id=$1 AND workspace_id=$3 "
                "UNION SELECT a.id FROM agents a JOIN branch b ON a.parent_id=b.id "
                "WHERE a.workspace_id=$3) SELECT EXISTS(SELECT 1 FROM branch WHERE id=$2)",
                caller,
                target,
                p["workspace_id"],
            )
            if not descendant:
                raise ValueError("NOT_DESCENDANT")
            if a["parent_id"] is None:
                raise ValueError("SEED_RETIREMENT_REQUIRES_OWNER_RESET")
            await c.execute("UPDATE agents SET state='TERMINATED' WHERE id=$1", target)
            await c.execute(
                "INSERT INTO events(workspace_id,actor_id,type,data) "
                "VALUES($1,$2,'agent_terminated',$3)",
                p["workspace_id"],
                caller,
                json.dumps({"agent_id": str(target), "reason": reason}),
            )
        return {"agent_id": str(target), "state": "TERMINATED"}

    async def disable_terminated_bot(self, agent: UUID) -> None:
        """Idempotent external cleanup; the durable terminal state drives restart retries."""
        state = await self.store.state(agent)
        if state["state"] != "TERMINATED":
            raise ValueError("AGENT_NOT_TERMINATED")
        identity = await self.store.get(agent)
        async with Client(
            str(self.settings.mattermost_url),
            self.settings.mattermost_admin_token.get_secret_value(),
        ) as admin:
            bot = await admin.request(
                "GET", f"bots/{identity.mattermost_user_id}", params={"include_deleted": "true"}
            )
            admin_user = await admin.me()
            if not owned_account(bot, identity.mattermost_user_id, admin_user["id"]):
                raise ValueError("BOT_OWNERSHIP_MISMATCH")
            if not bot.get("delete_at"):
                await admin.request("POST", f"bots/{identity.mattermost_user_id}/disable")

    async def release_allocations(self, workspace: UUID) -> None:
        # Historical allocation records remain intact; retirement no longer moves money.
        await self.store.pool.execute(
            "UPDATE agents SET allocation_released=true WHERE workspace_id=$1 "
            "AND parent_id IS NOT NULL AND state='TERMINATED' AND reserved=0",
            workspace,
        )
