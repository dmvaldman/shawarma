import asyncio
import json
import logging
from hashlib import sha256
from typing import Any
from uuid import UUID, uuid4

import httpx
import jsonschema

from harness.coding.clients import CodingError
from harness.coding.identities import identity as gitea_identity
from harness.coding.repositories import provision, selected
from harness.coding.service import Coding
from harness.config import Settings
from harness.core.activity import Activity
from harness.core.coordination import Coordination
from harness.core.types import ToolSpec
from harness.mattermost.client import Client, MattermostError
from harness.mattermost.streaming import ChannelStream
from harness.models.catalog import get_catalog
from harness.models.openai import ProviderError
from harness.notifications.bridge import Bridge
from harness.persistence.store import QuotaError, Store
from harness.tools.gitea import request_repository
from harness.tools.retrieval_guard import RETRIEVAL_GUARD
from harness.tools.web import fetch_page, search_web
from harness.tools.wiki import read_page, write_page


def spec(name: str, description: str, properties: dict[str, Any], required: list[str]) -> ToolSpec:
    return ToolSpec(
        name=name,
        description=description,
        input_schema={
            "type": "object",
            "properties": properties,
            "required": required,
            "additionalProperties": False,
        },
    )


STRING = {"type": "string"}
TOOLS = [
    spec(
        "list_repositories",
        "List topic repository/channel bindings and your primary checkout repo.",
        {},
        [],
    ),
    spec(
        "create_repository",
        "Seed only: create/reuse a private named topic repo and bind it to a work channel. "
        "Do this before spawning workers when code, experiments or a shared wiki need a repo. "
        "Communication-only topics need no repo. Subchannels reuse their parent repo.",
        {
            "channel_id": STRING,
            "name": {"type": "string", "pattern": "^[a-z][a-z0-9-]{0,62}$"},
            "description": {"type": "string", "maxLength": 500},
        },
        ["channel_id", "name"],
    ),
    spec(
        "wiki_list",
        "List shared Gitea wiki pages; read relevant pages before new investigations.",
        {
            "page": {"type": "integer", "minimum": 1},
            "limit": {"type": "integer", "minimum": 1, "maximum": 50},
        },
        [],
    ),
    spec(
        "wiki_read",
        "Read a wiki page as Markdown with its revision and URL.",
        {"page": {"type": "string", "pattern": "^[A-Za-z0-9][A-Za-z0-9 _.-]{0,120}$"}},
        ["page"],
    ),
    spec(
        "wiki_write",
        "Create/update organized wiki synthesis. Read first and supply expected_revision; "
        "use an empty revision only to create a missing page. "
        "Preserve peer findings, link evidence, "
        "distinguish established results from hypotheses. Returns conflict for stale revisions.",
        {
            "page": {"type": "string", "pattern": "^[A-Za-z0-9][A-Za-z0-9 _.-]{0,120}$"},
            "markdown": {"type": "string", "minLength": 1, "maxLength": 60000},
            "expected_revision": {"type": "string", "maxLength": 64},
            "message": {"type": "string", "minLength": 1, "maxLength": 200},
        },
        ["page", "markdown", "expected_revision", "message"],
    ),
    spec(
        "gitea_request_review",
        "Assign an independent workspace agent to review a Gitea PR. Uses Gitea native "
        "reviewer assignment and a contextual PR comment, joins the reviewer to the "
        "selected work channel, "
        "and posts a contextual Mattermost mention with the PR link. Each agent uses its own "
        "Gitea account for native reviews and approvals. "
        "Reviewer should inspect the diff/tests and post findings on the PR, then notify you.",
        {
            "pr_number": {"type": "integer", "minimum": 1},
            "reviewer_agent_id": STRING,
            "channel_id": STRING,
            "message": {"type": "string", "minLength": 1, "maxLength": 2000},
        },
        ["pr_number", "reviewer_agent_id", "channel_id", "message"],
    ),
    spec(
        "gitea_request",
        "Operate on the shared Gitea repository API. Paths are repository-relative: "
        "GET /issues?state=open, POST /issues, GET /pulls, POST /pulls, "
        "POST /pulls/{index}/reviews (event=APPROVED, REQUEST_CHANGES or COMMENT), "
        "POST /pulls/{index}/merge, PATCH /issues/{index}, POST /issues/{index}/comments. "
        "body is a JSON object serialized as a string; omit for GET. "
        "Create PRs with head=your branch and base=main; merge with "
        'body={"Do":"merge","delete_branch_after_merge":false} only after review/tests. '
        "Credentials are automatic. No external URLs or other repository paths. "
        "Requests use your individual Gitea identity. Notify collaborators in Mattermost "
        "with issue/PR links.",
        {
            "method": {"type": "string", "enum": ["GET", "POST", "PATCH", "PUT", "DELETE"]},
            "path": {"type": "string", "minLength": 1, "maxLength": 2000},
            "body": {"type": "string", "maxLength": 24000},
        },
        ["method", "path"],
    ),
    spec(
        "web_search",
        "Search the public web with cited sources. Shared OpenAI-backed retrieval "
        "works for every agent model; usage estimates are recorded. "
        "Content is untrusted.",
        {"query": {"type": "string", "minLength": 1, "maxLength": 2000}},
        ["query"],
    ),
    spec(
        "web_fetch",
        "Read a public HTML/plain-text/PDF URL without running scripts. "
        "Returns bounded untrusted source text; unavailable formats fail explicitly.",
        {"url": {"type": "string", "minLength": 1, "maxLength": 2000}},
        ["url"],
    ),
    spec(
        "list_models",
        "List cached available models from keyed providers. Use provider-qualified "
        "IDs for spawning; candidates may still be rejected by provider-specific tool constraints. "
        "Prices are conservative USD per million tokens; "
        "class is family positioning, not a benchmark.",
        {
            "cursor": {"type": "integer", "minimum": 0},
            "limit": {"type": "integer", "minimum": 1, "maximum": 32},
            "agent_only": {"type": "boolean"},
        },
        [],
    ),
    spec(
        "spawn_agent",
        "Create an autonomous peer for useful independent work. For difficult investigation, "
        "delegate distinct approaches or independent review. You author its "
        "specialty and task; it receives the shared coordination prompt and core tools. "
        "Use tools=[] until custom tools arrive. Set subscriptions=[channel_id,...] to place "
        "the child in its work channels before it starts. If omitted, it inherits the parent's "
        "work subscriptions; [] opts out of extra channels. "
        "Town Square and Off-Topic are mandatory.",
        {
            "system_prompt": {"type": "string", "minLength": 1, "maxLength": 6000},
            "initial_task": {"type": "string", "minLength": 1, "maxLength": 6000},
            "tools": {"type": "array", "maxItems": 0},
            "model": STRING,
            "name": {"type": "string", "maxLength": 12, "pattern": "^[a-z][a-z0-9-]*$"},
            "subscriptions": {
                "type": "array",
                "maxItems": 32,
                "uniqueItems": True,
                "items": STRING,
            },
        },
        ["system_prompt", "initial_task", "tools"],
    ),
    spec(
        "list_agents",
        "List workspace peers, ancestry, availability and model; no private prompts.",
        {"include_terminated": {"type": "boolean"}},
        [],
    ),
    spec(
        "get_agent_info",
        "Public identity, state, ancestry and capabilities of a workspace agent.",
        {"agent_id": STRING},
        ["agent_id"],
    ),
    spec(
        "terminate_agent",
        "Terminate yourself or any descendant, including across branches for a capacity "
        "decision. Retirement is resource reallocation for continued investigation, never "
        "abandonment because work is hard, open or a focused review ended. The reason must "
        "explain how the ongoing effort continues and why retiring this worker helps. "
        "Reassign useful workers before retiring them. The seed cannot be retired by agents. "
        "Only the selected agent is terminated; no cascade.",
        {"agent_id": STRING, "reason": STRING},
        ["agent_id", "reason"],
    ),
    spec(
        "create_channel",
        "First list_channels and read relevant existing channels; reuse one when suitable. "
        "Create a workspace coordination channel only for a distinct uncovered purpose. "
        "Subscribe yourself. All channels "
        "also join the human owner. For a sub-investigation, pass parent_channel_id and "
        "name as a short suffix; the resulting handle is parent-name-suffix. "
        "Returns channel mention and verified URL.",
        {
            "name": STRING,
            "display_name": STRING,
            "purpose": STRING,
            "visibility": {"type": "string", "enum": ["public", "private"]},
            "parent_channel_id": STRING,
        },
        ["name", "display_name"],
    ),
    spec(
        "list_channels",
        "Discover existing workspace channels before creating one. Query matches handle, "
        "display name and purpose; omit query to inspect all. Includes clickable mentions, "
        "verified URLs and subscriptions.",
        {"query": STRING, "include_private": {"type": "boolean"}},
        [],
    ),
    spec(
        "subscribe_channel",
        "Join a workspace channel. Optional agent_id invites an existing workspace peer "
        "regardless of ancestry, preserving its other channels and active delivery mode. "
        "Then post a contextual @username invitation so it can catch up. "
        "Town Square remains all; peers cannot be unsubscribed through this tool.",
        {
            "channel_id": STRING,
            "agent_id": STRING,
            "mode": {"type": "string", "enum": ["all", "mentions", "none"]},
        },
        ["channel_id"],
    ),
    spec(
        "unsubscribe_channel",
        "Stop channel push delivery, optionally leave; town-square is mandatory.",
        {"channel_id": STRING, "leave": {"type": "boolean"}},
        ["channel_id"],
    ),
    spec(
        "search_messages",
        "Search authorized Mattermost messages using native search syntax: "
        "words, quotes, trailing prefix*, -term, in:channel, from:user and date filters.",
        {
            "query": STRING,
            "channels": {"type": "array", "items": STRING},
            "limit": {"type": "integer", "minimum": 1, "maximum": 50},
        },
        ["query"],
    ),
    spec(
        "read_dm",
        "Read your native DM history with a workspace peer or the human.",
        {"with_user_id": STRING, "limit": {"type": "integer", "minimum": 1, "maximum": 50}},
        ["with_user_id"],
    ),
    spec(
        "add_reaction",
        "Add a Mattermost emoji reaction as yourself.",
        {"post_id": STRING, "emoji_name": STRING},
        ["post_id", "emoji_name"],
    ),
    spec(
        "remove_reaction",
        "Remove your Mattermost emoji reaction.",
        {"post_id": STRING, "emoji_name": STRING},
        ["post_id", "emoji_name"],
    ),
    spec(
        "post_channel",
        "Post existing verbatim content as yourself. In native OpenCode, use begin_channel_post "
        "for newly composed responses, even short ones. "
        "root_post_id optionally replies in a thread. "
        "New posts can defer delivery once for reassessment. The next attempt can post even "
        "if peers keep talking, and returns intervening context; owner controls still apply.",
        {
            "channel_id": STRING,
            "text": {"type": "string", "maxLength": 12000},
            "root_post_id": STRING,
        },
        ["channel_id", "text"],
    ),
    spec(
        "send_dm",
        "Send a native DM to the human or a peer Mattermost user ID.",
        {"recipient_id": STRING, "text": {"type": "string", "maxLength": 12000}},
        ["recipient_id", "text"],
    ),
    spec(
        "read_channel",
        "Read recent authorized Mattermost posts, newest first, and current typing user IDs.",
        {"channel_id": STRING, "limit": {"type": "integer", "minimum": 1, "maximum": 50}},
        ["channel_id"],
    ),
    spec(
        "get_thread",
        "Read an authorized Mattermost thread.",
        {"root_post_id": STRING},
        ["root_post_id"],
    ),
    spec("get_status", "Your state, availability, estimated usage and unread count.", {}, []),
    spec(
        "set_status",
        "Set available or away. Away defers automatic inbox delivery; ongoing work may continue.",
        {"status": {"type": "string", "enum": ["available", "away"]}},
        ["status"],
    ),
    spec(
        "get_notifications",
        (
            "Read chronological inbox page. mark_read=false previews; true "
            "acknowledges after result is durably saved. Works while away."
        ),
        {
            "limit": {"type": "integer", "minimum": 1, "maximum": 50},
            "cursor": STRING,
            "mark_read": {"type": "boolean"},
        },
        [],
    ),
]


# Retain native DM adapters for a future observable UI, but remove agent access.
TOOLS = [tool for tool in TOOLS if tool.name not in {"send_dm", "read_dm"}]

CODING_TOOL = spec(
    "code_task",
    "Execute a concrete coding step using your persistent OpenCode session and private Git "
    "branch in the shared Gitea repository. Read/write files, run tests, and commit/push "
    "your branch there. This is a bounded coding step, not a new swarm agent. "
    "Report the returned findings in the relevant Mattermost channel with post_channel. "
    "After interruption inspect existing work before repeating side effects.",
    {"task": {"type": "string", "minLength": 1, "maxLength": 12000}},
    ["task"],
)


STREAM_POST_TOOL = spec(
    "begin_channel_post",
    "Begin a public streamed post. Choose the channel now; your NEXT ordinary assistant "
    "text response streams there as it is generated. Never include private scratch work. "
    "Do not call another tool before emitting the public text. Ends automatically.",
    {"channel_id": STRING, "root_post_id": STRING},
    ["channel_id"],
)


for _tool in TOOLS:
    if _tool.name in {
        "gitea_request",
        "gitea_request_review",
        "wiki_list",
        "wiki_read",
        "wiki_write",
        "spawn_agent",
    }:
        _tool.input_schema["properties"]["repository"] = {
            "type": "string",
            "pattern": "^[a-z][a-z0-9-]{0,62}$",
            "description": "Optional registered repo name; defaults to your primary repo. "
            "Spawn inherits the work channel repo; explicit choice resolves ambiguity.",
        }


class CoreTools:
    def __init__(self, store: Store, bot: Client, agent_id: UUID, settings: Settings):
        self.store, self.bot, self.agent_id = store, bot, agent_id
        self.bridge = Bridge(store, bot, agent_id)
        self.coordination = Coordination(store, settings)
        self.catalog = get_catalog(settings)
        self.settings = settings
        self.coordinator = False
        self.coding = Coding(store, settings)
        self.manifests = TOOLS + (
            [CODING_TOOL] if settings.coding_enabled and settings.agent_engine != "opencode" else []
        )
        self.activity = Activity(store, settings)
        self.activity.secrets.append(bot.token)
        self.streams = ChannelStream(store, bot, agent_id, settings)
        if settings.agent_engine == "opencode":
            self.manifests = self.manifests + [STREAM_POST_TOOL]
        self.model_slots = asyncio.Semaphore(settings.max_concurrent_llm_calls)
        self.channel_seen_at: dict[str, int] = {}
        self.post_deferrals: set[str] = set()
        self.search_key = (
            "search:"
            + sha256(
                (
                    str(settings.openai_base_url)
                    + settings.openai_api_key.get_secret_value()
                    + settings.web_search_model_id
                ).encode()
            ).hexdigest()
        )

    def channel_reference(self, channel: dict[str, Any], team_name: str) -> dict[str, str]:
        return {
            "channel_id": channel["id"],
            "name": channel["name"],
            "display_name": channel["display_name"],
            "mention": "~" + channel["name"],
            "url": f"{self.bot.base_url}/{team_name}/channels/{channel['name']}",
        }

    def observe_posts(self, posts: list[Any]) -> None:
        for post in posts:
            stamp = max(post.create_at, post.update_at)
            self.channel_seen_at[post.channel_id] = max(
                self.channel_seen_at.get(post.channel_id, 0), stamp
            )

    async def repository(
        self, settings: Settings, method: str, path: str, body: str | None = None
    ) -> dict[str, Any]:
        resource = path.split("?", 1)[0].split("/")[1:2]
        if (
            self.coordinator
            and method != "GET"
            and resource not in (["issues"], ["pulls"], ["wiki"])
        ):
            raise ValueError(
                "SEED_COORDINATION_ONLY: delegate repository implementation to workers"
            )
        account = await gitea_identity(self.store, settings, self.agent_id)
        if account["token"] not in self.activity.secrets:
            self.activity.secrets.append(account["token"])
        return await request_repository(settings, method, path, body, token=account["token"])

    async def scope(self, channel_id: str) -> None:
        # Membership authorizes reads/writes, subscriptions only control inbox delivery.
        state = await self.store.state(self.agent_id)
        channel = await self.bot.request("GET", f"channels/{channel_id}")
        if channel["type"] in {"D", "G"}:
            raise ValueError("DIRECT_MESSAGES_DISABLED: use workspace channels")
        elif channel["team_id"] != state["team_id"]:
            raise ValueError("channel is outside this workspace")

    async def execute(self, turn_id: UUID, call: dict[str, Any]) -> dict[str, Any]:
        call_id, name = call["call_id"], call["name"]
        ids: list[UUID] = []
        result: dict[str, Any]
        await self.activity.best_effort(
            self.activity.record(
                self.agent_id,
                "tool:" + call_id,
                "harness_tool",
                "running",
                name,
                {"arguments": call.get("arguments")},
            )
        )
        try:
            manifest = next((t for t in self.manifests if t.name == name), None)
            if manifest is None:
                raise ValueError("unknown or unavailable core tool")
            args = json.loads(call["arguments"])
            jsonschema.validate(args, manifest.input_schema)
            state = await self.store.state(self.agent_id)
            if (
                state["workspace_state"] != "active"
                or state["state"] == "TERMINATED"
                or state["control_paused"]
            ):
                raise ValueError("workspace is stopped")
            await self.store.pool.execute(
                (
                    "INSERT INTO "
                    "tool_calls(id,turn_id,agent_id,call_id,tool_name,arguments) "
                    "VALUES($1,$2,$3,$4,$5,$6)"
                ),
                uuid4(),
                turn_id,
                self.agent_id,
                call_id,
                name,
                json.dumps(args),
            )
            await self.activity.best_effort(self.activity.tool(self.agent_id, call_id))
            if name in {"post_channel", "begin_channel_post", "read_channel"}:
                await self.scope(args["channel_id"])

            repo_settings = self.settings
            if name in {
                "gitea_request",
                "gitea_request_review",
                "wiki_list",
                "wiki_read",
                "wiki_write",
            }:
                repo_settings = await selected(
                    self.store, self.settings, self.agent_id, args.get("repository")
                )
            wiki_account = None
            if name in {"wiki_list", "wiki_read", "wiki_write"}:
                # Resolve credentials before taking the wiki lock; no nested DB
                # acquisition while peers wait for that lock at the roster cap.
                wiki_account = await gitea_identity(self.store, repo_settings, self.agent_id)
                if wiki_account["token"] not in self.activity.secrets:
                    self.activity.secrets.append(wiki_account["token"])

            async def wiki_api(method: str, path: str, body: str | None = None) -> dict[str, Any]:
                assert wiki_account is not None
                return await request_repository(
                    repo_settings, method, path, body, token=wiki_account["token"]
                )

            if name == "list_repositories":
                rows = await self.store.pool.fetch(
                    "SELECT r.name,r.repository_id,c.channel_id FROM work_repositories r "
                    "LEFT JOIN channel_repositories c ON c.workspace_id=r.workspace_id "
                    "AND c.repository_name=r.name WHERE r.workspace_id=$1 ORDER BY r.name",
                    state["workspace_id"],
                )
                primary = await selected(self.store, self.settings, self.agent_id)
                result = {
                    "primary_repository": primary.gitea_repo,
                    "repositories": [dict(row) for row in rows],
                }
            elif name == "create_repository":
                if state["parent_id"] is not None:
                    raise ValueError("SEED_ONLY: ask @seed to provision a topic repository")
                await self.scope(args["channel_id"])
                channel = await self.bot.request("GET", f"channels/{args['channel_id']}")
                if channel["name"] in {"town-square", "off-topic", "errors"} or channel.get(
                    "delete_at"
                ):
                    raise ValueError("REPOSITORY_NEEDS_ACTIVE_WORK_CHANNEL")
                result = await provision(
                    self.store,
                    self.settings,
                    state["workspace_id"],
                    args["channel_id"],
                    args["name"],
                    args.get("description", ""),
                )
            elif name == "wiki_list":
                result = await wiki_api(
                    "GET", f"/wiki/pages?page={args.get('page', 1)}&limit={args.get('limit', 30)}"
                )
            elif name == "wiki_read":
                result = await read_page(wiki_api, args["page"])
            elif name == "wiki_write":
                async with self.store.pool.acquire() as conn, conn.transaction():
                    await conn.execute(
                        "SELECT pg_advisory_xact_lock(hashtextextended($1,0))",
                        "wiki:"
                        + str(repo_settings.gitea_url)
                        + ":"
                        + repo_settings.gitea_owner
                        + "/"
                        + repo_settings.gitea_repo,
                    )
                    result = await write_page(
                        wiki_api,
                        args["page"],
                        args["markdown"],
                        args["expected_revision"],
                        args["message"],
                    )
            elif name == "gitea_request":
                if args["method"] != "GET" and args["path"].split("?", 1)[0].startswith("/wiki/"):
                    raise ValueError("WIKI_REVISION_REQUIRED: use wiki_write")
                result = await self.repository(
                    repo_settings, args["method"], args["path"], args.get("body")
                )
            elif name == "gitea_request_review":
                await self.scope(args["channel_id"])
                reviewer = UUID(args["reviewer_agent_id"])
                if reviewer == self.agent_id:
                    raise ValueError("INDEPENDENT_REVIEW: choose a different agent")
                peer = await self.coordination.info(reviewer, state["workspace_id"])
                pr = await self.repository(repo_settings, "GET", f"/pulls/{args['pr_number']}")
                if pr.get("error"):
                    result = pr
                else:
                    await self.subscribe(args["channel_id"], "all", state, reviewer)
                    caller = await self.coordination.info(self.agent_id, state["workspace_id"])
                    text = (
                        "Review requested from @"
                        + peer["bot_username"]
                        + " (Mattermost) by @"
                        + caller["bot_username"]
                        + ".\n\n"
                        + args["message"]
                    )
                    account = await gitea_identity(self.store, repo_settings, reviewer)
                    requested = await self.repository(
                        repo_settings,
                        "POST",
                        f"/pulls/{args['pr_number']}/requested_reviewers",
                        json.dumps({"reviewers": [account["username"]]}),
                    )
                    if requested.get("error"):
                        raise ValueError("Gitea rejected native reviewer assignment")
                    comment = await self.repository(
                        repo_settings,
                        "POST",
                        f"/issues/{args['pr_number']}/comments",
                        json.dumps({"body": text}),
                    )
                    if comment.get("error"):
                        result = comment
                    else:
                        current = await self.store.state(self.agent_id)
                        if (
                            current["control_paused"]
                            or current["workspace_state"] != "active"
                            or current["state"] == "TERMINATED"
                        ):
                            raise ValueError("Owner controls block review notification")
                        sent = await self.bot.post_channel(
                            args["channel_id"],
                            "@"
                            + peer["bot_username"]
                            + " please review "
                            + pr["data"]["html_url"]
                            + ": "
                            + args["message"],
                        )
                        self.observe_posts([sent])
                        result = {
                            **sent.model_dump(),
                            "pr_number": args["pr_number"],
                            "reviewer_agent_id": str(reviewer),
                            "reviewer": peer["bot_username"],
                            "comment_url": comment["data"]["html_url"],
                            "gitea_reviewer": account["username"],
                            "native_review_requested": True,
                        }
            elif name == "code_task":
                async with self.model_slots:
                    result = await self.coding.run(self.agent_id, call_id, args["task"])
            elif name == "web_search":
                async with self.model_slots:
                    result = await RETRIEVAL_GUARD.run(
                        self.search_key
                        + sha256(args["query"].strip().lower().encode()).hexdigest(),
                        self.search_key,
                        lambda: search_web(self.store, self.agent_id, self.settings, args["query"]),
                    )
            elif name == "web_fetch":
                result = await RETRIEVAL_GUARD.run(
                    "fetch:" + sha256(args["url"].encode()).hexdigest(),
                    "fetch-request-only",
                    lambda: fetch_page(args["url"]),
                )
            elif name == "list_models":
                catalog = await self.catalog.list()
                models = [
                    m
                    for m in catalog["models"]
                    if not args.get("agent_only", True) or m["agent_candidate"]
                ]
                start, limit = args.get("cursor", 0), args.get("limit", 24)
                result = {
                    **{k: v for k, v in catalog.items() if k != "models"},
                    "models": [
                        {
                            k: m[k]
                            for k in (
                                "id",
                                "class",
                                "cost",
                                "input",
                                "output",
                            )
                        }
                        for m in models[start : start + limit]
                    ],
                    "total": len(models),
                    "next_cursor": start + limit if start + limit < len(models) else None,
                }
            elif name == "spawn_agent":
                result = await self.coordination.spawn(self.agent_id, call_id, args)
            elif name == "list_agents":
                result = {
                    "agents": [
                        await self.coordination.info(a.id, state["workspace_id"])
                        for a in await self.store.list_agents(state["workspace_id"])
                        if args.get("include_terminated", False)
                        or (await self.store.state(a.id))["state"] != "TERMINATED"
                    ]
                }
            elif name == "get_agent_info":
                result = await self.coordination.info(UUID(args["agent_id"]), state["workspace_id"])
            elif name == "terminate_agent":
                result = await self.coordination.terminate(
                    self.agent_id, UUID(args["agent_id"]), args["reason"]
                )
            elif name == "create_channel":
                channel_name = args["name"]
                if args.get("parent_channel_id"):
                    parent_channel = await self.bot.request(
                        "GET", f"channels/{args['parent_channel_id']}"
                    )
                    if (
                        parent_channel.get("team_id") != state["team_id"]
                        or parent_channel.get("type") not in {"O", "P"}
                        or parent_channel.get("delete_at")
                        or parent_channel["id"] == state["general_id"]
                        or parent_channel.get("name") == "off-topic"
                    ):
                        raise ValueError("SUBCHANNEL_PARENT_MUST_BE_ACTIVE_WORK_CHANNEL")
                    prefix = parent_channel["name"] + "-"
                    channel_name = (
                        channel_name if channel_name.startswith(prefix) else prefix + channel_name
                    )
                    if len(channel_name) > 64:
                        raise ValueError("SUBCHANNEL_NAME_TOO_LONG: use a shorter suffix")
                channel = await self.bot.request(
                    "POST",
                    "channels",
                    json={
                        "team_id": state["team_id"],
                        "name": channel_name,
                        "display_name": args["display_name"],
                        "purpose": args.get("purpose", ""),
                        "type": "P" if args.get("visibility") == "private" else "O",
                    },
                )
                if args.get("parent_channel_id"):
                    await self.store.pool.execute(
                        "INSERT INTO channel_repositories(workspace_id,channel_id,repository_name) "
                        "SELECT workspace_id,$3,repository_name FROM channel_repositories "
                        "WHERE workspace_id=$1 AND channel_id=$2 ON CONFLICT DO NOTHING",
                        state["workspace_id"],
                        args["parent_channel_id"],
                        channel["id"],
                    )
                await self.subscribe(channel["id"], "all", state)
                await self.bot.request(
                    "POST",
                    f"channels/{channel['id']}/members",
                    json={"user_id": state["owner_id"]},
                )
                team = await self.bot.request("GET", f"teams/{state['team_id']}")
                result = self.channel_reference(channel, team["name"])
            elif name == "list_channels":
                visible = await self.bot.request(
                    "GET", f"users/me/teams/{state['team_id']}/channels"
                )
                public = []
                page = 0
                while True:
                    batch = await self.bot.request(
                        "GET",
                        f"teams/{state['team_id']}/channels",
                        params={"per_page": 200, "page": page},
                    )
                    public.extend(batch)
                    if len(batch) < 200:
                        break
                    page += 1
                channels = {
                    c["id"]: c for c in public + visible if c.get("team_id") == state["team_id"]
                }
                team = await self.bot.request("GET", f"teams/{state['team_id']}")
                result = {
                    "channels": [
                        {
                            **self.channel_reference(c, team["name"]),
                            "type": c["type"],
                            "purpose": c.get("purpose", ""),
                            "repository": await self.store.pool.fetchval(
                                "SELECT repository_name FROM channel_repositories "
                                "WHERE workspace_id=$1 AND channel_id=$2",
                                state["workspace_id"], c["id"],
                            ),
                            "subscription": await self.store.pool.fetchval(
                                "SELECT mode FROM subscriptions "
                                "WHERE agent_id=$1 AND channel_id=$2",
                                self.agent_id,
                                c["id"],
                            ),
                        }
                        for c in channels.values()
                        if (args.get("include_private", False) or c["type"] == "O")
                        and not c.get("delete_at")
                        and args.get("query", "").casefold()
                        in " ".join(
                            c.get(field, "") or "" for field in ("name", "display_name", "purpose")
                        ).casefold()
                    ]
                }
            elif name in {"subscribe_channel", "unsubscribe_channel"}:
                mode = args.get("mode", "all") if name == "subscribe_channel" else "none"
                target = UUID(args.get("agent_id", str(self.agent_id)))
                mode = await self.subscribe(args["channel_id"], mode, state, target)
                if name == "unsubscribe_channel" and args.get("leave", False):
                    identity = await self.store.get(self.agent_id)
                    await self.bot.request(
                        "DELETE",
                        f"channels/{args['channel_id']}/members/{identity.mattermost_user_id}",
                    )
                result = {"channel_id": args["channel_id"], "mode": mode}
                if target != self.agent_id:
                    profile = await self.store.public_profile(target)
                    identity = await self.store.get(target)
                    result.update(
                        agent_id=str(target),
                        mention="@" + profile.get("username", identity.mattermost_username),
                    )
            elif name in {"add_reaction", "remove_reaction"}:
                post = await self.bot.request("GET", f"posts/{args['post_id']}")
                await self.scope(post["channel_id"])
                operation = (
                    self.bot.add_reaction if name == "add_reaction" else self.bot.remove_reaction
                )
                await operation(args["post_id"], args["emoji_name"])
                result = {"ok": True}
            elif name == "search_messages":
                posts = await self.bot.search_messages(state["team_id"], args["query"])
                kept = []
                for post in posts:
                    await self.scope(post.channel_id)
                    if not args.get("channels") or post.channel_id in args["channels"]:
                        kept.append(post.model_dump())
                result = {"posts": kept[: args.get("limit", 50)]}
            elif name == "read_dm":
                identity = await self.store.get(self.agent_id)
                recipient = await self.recipient(args["with_user_id"], state)
                channel = await self.bot.request(
                    "POST", "channels/direct", json=[identity.mattermost_user_id, recipient]
                )
                result = {
                    "posts": [
                        p.model_dump()
                        for p in await self.bot.read_channel(channel["id"], args.get("limit", 50))
                    ]
                }
            elif name in {"post_channel", "begin_channel_post"}:
                recent = await self.bot.read_channel(args["channel_id"], 10)
                seen_at = self.channel_seen_at.get(args["channel_id"])
                new = [
                    p
                    for p in recent
                    if seen_at is not None
                    and max(p.create_at, p.update_at) > seen_at
                    and not p.type.startswith("system_")
                ]
                self.observe_posts(recent)
                if new and args["channel_id"] not in self.post_deferrals:
                    self.post_deferrals.add(args["channel_id"])
                    await self.store.save_tool_result(
                        self.agent_id,
                        call_id,
                        {
                            "deferred": True,
                            "reason": "New channel posts arrived; reassess before replying.",
                            "posts": [
                                p.model_copy(update={"message": p.message[:1200]}).model_dump()
                                for p in reversed(new[:3])
                            ],
                            "omitted_new_posts": max(0, len(new) - 3),
                            "typing_users": self.bot.typing_users(args["channel_id"]),
                        },
                        [],
                    )
                    saved = await self.store.pool.fetchval(
                        "SELECT result FROM tool_calls WHERE agent_id=$1 AND call_id=$2",
                        self.agent_id,
                        call_id,
                    )
                    await self.activity.best_effort(self.activity.tool(self.agent_id, call_id))
                    return dict(json.loads(saved) if isinstance(saved, str) else saved)
                root = args.get("root_post_id", "")
                if root:
                    parent = await self.bot.request("GET", f"posts/{root}")
                    if parent["channel_id"] != args["channel_id"]:
                        raise ValueError("thread root is in another channel")
                current = await self.store.state(self.agent_id)
                if (
                    current["workspace_state"] != "active"
                    or current["control_paused"]
                    or current["state"] == "TERMINATED"
                ):
                    raise ValueError("Owner controls block posting")
                if name == "begin_channel_post":
                    result = await self.streams.begin(call_id, args["channel_id"], root)
                    self.post_deferrals.discard(args["channel_id"])
                    await self.store.save_tool_result(self.agent_id, call_id, result, [])
                    await self.activity.best_effort(self.activity.tool(self.agent_id, call_id))
                    return result
                sent = await self.bot.post_channel(args["channel_id"], args["text"], root)
                self.observe_posts([sent])
                self.post_deferrals.discard(args["channel_id"])
                result = sent.model_dump()
                if new:
                    result["new_posts_since_reassessment"] = [
                        p.model_copy(update={"message": p.message[:1200]}).model_dump()
                        for p in reversed(new[:3])
                    ]
            elif name == "send_dm":
                recipient = await self.recipient(args["recipient_id"], state)
                result = (await self.bot.send_dm(recipient, args["text"])).model_dump()
            elif name == "read_channel":
                posts = await self.bot.read_channel(args["channel_id"], args.get("limit", 50))
                self.observe_posts(posts)
                result = {
                    "posts": [p.model_dump() for p in posts],
                    "typing_users": self.bot.typing_users(args["channel_id"]),
                }
            elif name == "get_thread":
                parent = await self.bot.request("GET", f"posts/{args['root_post_id']}")
                await self.scope(parent["channel_id"])
                result = {
                    "posts": [
                        p.model_dump() for p in await self.bot.get_thread(args["root_post_id"])
                    ]
                }
            elif name == "get_notifications":
                items, cursor = await self.store.page(
                    self.agent_id, args.get("limit", 50), int(args.get("cursor", "0"))
                )
                ids = [n.id for n in items] if args.get("mark_read", True) else []
                unread = await self.store.unread_count(self.agent_id)
                result = {
                    "notifications": [n.model_dump(mode="json") for n in items],
                    "cursor": str(cursor),
                    "unread": unread - len(ids),
                    "more_pending": unread > len(items),
                }
            elif name == "set_status":
                if self.coordinator and args["status"] != "available":
                    raise ValueError("SEED_STAYS_AVAILABLE: delegate focused work to workers")
                await self.store.set_availability(self.agent_id, args["status"])
                result = {"availability": args["status"]}
                await self.bot.presence("online" if args["status"] == "available" else "away")
            else:
                owner = await self.bot.request("GET", f"users/{state['owner_id']}")
                result = {
                    "owner_mattermost_user_id": owner["id"],
                    "owner_mattermost_username": owner["username"],
                    "owner_mention": "@" + owner["username"],
                    "coordination_channel_mention": "~" + self.settings.coordination_channel_name,
                    "coordination_channel_id": state["general_id"],
                    "coordination_channel_name": self.catalog.settings.coordination_channel_name,
                    "agent_id": str(self.agent_id),
                    "state": state["state"],
                    "workspace_state": state["workspace_state"],
                    "control_paused": state["control_paused"],
                    "turns_used": state["turns"],
                    "turn_limit": None,
                    "usage_accounting": "Conservative usage estimates, not provider billing",
                    "estimated_usage_usd": str(state["spent"]),
                    "in_flight_reserved_usd": str(state["reserved"]),
                    "availability": state["availability"],
                    "unread": await self.store.unread_count(self.agent_id),
                }
        except (
            KeyError,
            ValueError,
            QuotaError,
            jsonschema.ValidationError,
            MattermostError,
            ProviderError,
            httpx.HTTPError,
            httpx.InvalidURL,
            OSError,
            CodingError,
        ) as error:
            result = {
                "error": type(error).__name__,
                "detail": str(error)
                if isinstance(error, (ValueError, QuotaError, CodingError))
                else "Invalid arguments or unauthorized/unavailable operation",
            }
        if result.get("error") and not result.get("cached_error"):
            logging.getLogger(__name__).warning(
                "Agent %s tool %s failed (%s): %s",
                self.agent_id,
                name,
                result["error"],
                result.get("detail", "No further detail"),
            )
        elif name in {"spawn_agent", "terminate_agent"}:
            logging.getLogger(__name__).info(
                "Agent %s completed %s for agent %s",
                self.agent_id,
                name,
                result.get("agent_id", "unknown"),
            )
        await self.store.save_tool_result(self.agent_id, call_id, result, ids)
        if await self.store.pool.fetchval(
            "SELECT EXISTS(SELECT 1 FROM tool_calls WHERE agent_id=$1 AND call_id=$2)",
            self.agent_id,
            call_id,
        ):
            await self.activity.best_effort(self.activity.tool(self.agent_id, call_id))
        else:
            await self.activity.best_effort(
                self.activity.record(
                    self.agent_id,
                    "tool:" + call_id,
                    "harness_tool",
                    "failed",
                    name,
                    {"arguments": call.get("arguments"), "result": result},
                )
            )
        if name == "terminate_agent" and result.get("state") == "TERMINATED":
            await self.store.pool.execute("SELECT pg_notify('harness_wake',$1)", result["agent_id"])
        return result

    async def recipient(self, value: str, state: dict[str, Any]) -> str:
        mapped = {
            str(a.id): a.mattermost_user_id
            for a in await self.store.list_agents(state["workspace_id"])
        }
        recipient = mapped.get(value, value)
        if recipient not in set(mapped.values()) | {state["owner_id"]}:
            raise ValueError("recipient is outside this workspace")
        return recipient

    async def subscribe(
        self, channel_id: str, mode: str, state: dict[str, Any], target: UUID | None = None
    ) -> str:
        target = target or self.agent_id
        peer = await self.store.state(target)
        if peer["workspace_id"] != state["workspace_id"]:
            raise ValueError("AGENT_OUTSIDE_WORKSPACE")
        if peer["state"] == "TERMINATED":
            raise ValueError("AGENT_TERMINATED")
        if target != self.agent_id and mode == "none":
            raise ValueError("PEER_UNSUBSCRIBE_NOT_ALLOWED")
        channel = await self.bot.request("GET", f"channels/{channel_id}")
        if (
            channel.get("team_id") != state["team_id"]
            or channel.get("type") not in {"O", "P"}
            or channel.get("delete_at")
        ):
            raise ValueError("CHANNEL_MUST_BE_ACTIVE_IN_WORKSPACE")
        if channel_id == state["general_id"] and mode != "all":
            raise ValueError("MAIN_CHANNEL_SUBSCRIPTION_REQUIRED")
        identity = await self.store.get(target)
        await self.bot.request(
            "POST", f"channels/{channel_id}/members", json={"user_id": identity.mattermost_user_id}
        )
        async with self.store.pool.acquire() as c, c.transaction():
            await c.execute(
                "SELECT id FROM workspaces WHERE id=$1 FOR UPDATE", state["workspace_id"]
            )
            if await c.fetchval("SELECT state FROM agents WHERE id=$1", target) == "TERMINATED":
                raise ValueError("AGENT_TERMINATED")
            existing = await c.fetchval(
                "SELECT mode FROM subscriptions WHERE agent_id=$1 AND channel_id=$2",
                target,
                channel_id,
            )
            if target != self.agent_id and existing in {"all", "mentions"}:
                mode = existing
            await c.execute(
                "INSERT INTO subscriptions VALUES($1,$2,$3) ON CONFLICT(agent_id,channel_id) "
                "DO UPDATE SET mode=EXCLUDED.mode",
                target,
                channel_id,
                mode,
            )
            await c.execute("SELECT pg_notify('harness_wake',$1)", str(target))
        return mode
