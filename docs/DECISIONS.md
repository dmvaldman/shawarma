# Agent Company Harness

The canonical specification is [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md). Mattermost is the only conversational UI. No organizational hierarchy is built into the harness.

## Local development

Install Python 3.12+ with uv and Docker with the Compose plugin. The pinned Mattermost image is amd64; Docker Desktop must support amd64 emulation on Apple Silicon. From the repository root:

```sh
uv sync --locked --python 3.12
cp .env.example .env
# Edit both database passwords and HARNESS_DATABASE_URL to match.
docker compose --env-file .env -f infra/compose.yaml config --quiet
docker compose --env-file .env -f infra/compose.yaml up -d --wait
```

Open http://localhost:8065 and create the first human account (the system admin). Enable/create a personal access token in Mattermost and set `HARNESS_MATTERMOST_ADMIN_TOKEN` and `HARNESS_MATTERMOST_OWNER_ID` in `.env`. Bot creation and tokens are enabled by Compose. The owner must be an authenticated human account; text claiming to be the owner grants no authority.

PostgreSQL binds to 127.0.0.1:5432 and Mattermost to 127.0.0.1:8065. Change the port mapping if already occupied. Service URLs are explicit environment settings, so moving to a VM requires configuration changes rather than changes to business logic. The Compose database initialization script runs only on first initialization of its data volume.

## Verification

```sh
uv run ruff format --check .
uv run ruff check .
uv run mypy
uv run pytest tests/unit
```

Unit tests use no credentials or live services.

## Agent runtime

Set `OPENAI_API_KEY` in the private, gitignored `.env`. The runtime uses the explicitly configured `HARNESS_OPENAI_MODEL_ID=gpt-6-luna`; unavailable models fail without substitution. OpenAI, Anthropic, and direct DeepSeek adapters are installed. `list_models` queries providers with configured keys, combines their results, and pages through a shared one-hour cache. Use provider-qualified IDs such as `anthropic:claude-haiku-5-5` or `deepseek:deepseek-flash` in `spawn_agent`. Available text candidates are permitted; provider-specific API/tool constraints can still reject a request without model substitution. Price estimates are explicit settings in `.env.example`; internal dollar budgets are disabled.

```sh
uv run agent-company start
uv run agent-company inspect
```

`start` provisions exactly one seed if needed and prints its Mattermost URL. Post your instruction in that workspace's `#town-square` as the configured owner. The seed responds using its own bot identity. PostgreSQL stores immutable birth prompts, conversation/tool records, availability, durable notification receipts, usage reservations and audit events. The private `.state/workspace.json` retains bootstrap IDs and the bot credential; it contains no chat transcript.

Notifications are batched at model boundaries. Away agents retain unread messages; returning available delivers the backlog. Reconnect replays accessible Mattermost history with durable deduplication. The seed and children can spawn peers, inspect workspace agents, terminate themselves or direct children, create channels, manage subscriptions, communicate in workspace channels, post/thread/read/search, and react. Children receive unique bot identities, immutable parent-authored role instructions, initial tasks and mandatory `#town-square` subscriptions. They share the finite model-concurrency limit and run independently. Custom Python tools arrive in M4; `spawn_agent` currently accepts `tools=[]` and defaults to the parent's model. Spawning does not require or transfer a dollar allocation. All agents receive owner steering in `#town-square`.

Spawning remains the agent's choice. The live M3 test asks for an integer-square-root design and independent correctness review without instructing it to spawn; the seed chose two children. The harness creates exactly one initial seed and supplies no organizational hierarchy.

Ctrl-C stops the supervisor while preserving the workspace for `start` to resume. Owner emergency stop remains usable from another terminal:

```sh
uv run agent-company stop
```

Explicit stop terminates the workspace and disables the bot, retaining history and audit records. Archive its bootstrap checkpoint before provisioning an unrelated new workspace. M1's archived workspace retains its original prompt; it was replaced with a fresh M2 workspace because birth prompts are immutable. `create` and `listen` retain the M1 communication smoke test.

Run database integration checks against the local stack, with the supervisor stopped:

```sh
HARNESS_LIVE_TESTS=1 uv run pytest tests/unit tests/integration/test_runtime.py tests/integration/test_postgres.py
HARNESS_OPENAI_LIVE_TESTS=1 uv run pytest tests/integration/test_live_model.py
```

The second command makes paid model calls and writes acceptance messages in Mattermost. M1 adapter checks additionally require `HARNESS_TEST_OWNER_TOKEN` and are documented in the test module. Integration checks skip unless explicitly enabled.

```sh
docker compose --env-file .env -f infra/compose.yaml down
```

Volumes and chat history survive shutdown. `down --volumes` destroys the local stack data.

## Milestones

M0 creates the repository, contracts, immutable prompt template, finite configuration, and local stack. M1 proves one bot's Mattermost communication. M2 adds durable runtime state and first inference. Later milestones follow Section 23 in order, with an acceptance gate before proceeding. See [docs/DECISIONS.md](docs/DECISIONS.md) for settled ambiguities and [docs/VERIFICATION.md](docs/VERIFICATION.md) for actual verification results.

## Current local instance

M0, M1, M2 and the M3 spawning/coordination gate are verified. Open [Mattermost #general](http://localhost:8065/org-982d34d37969/channels/town-square). The owner username is `owner`; its generated password is in the private, ignored `.state/owner-login.txt`. This checkout's `.env` contains the configured bootstrap credentials and OpenAI key. Keep both files private.

Run `uv run agent-company start` to resume all surviving agents. Stop the supervisor before running acceptance tests.

## Names, models, and prompt revisions

The workspace display name is **My Swarm**. New workspace slugs use `my-swarm-<short-id>`; existing URLs stay stable. Mattermost makes Town Square mandatory, so it is the sole main channel. The old duplicate `#general` is archived with its history preserved; `#off-topic` remains.

Parents may supply a short function name at spawn time, such as `audit`, `newt`, or `probe`. Names use that shorthand alone, with an incrementing number only on collision (`audit`, `audit2`, `audit3`). Reservations are serialized per Mattermost server and retained after termination, since historical accounts still occupy usernames. Profiles contain only a friendly model name, such as `haiku 5.5`, capped at 20 characters. Full role instructions and provider/model metadata remain in harness records and `list_models`. Model rates and cost classes remain available through the catalog; override them with `HARNESS_MODEL_PROFILES` JSON without lowering unverified price ceilings.

Shared prompts use Markdown and default to top-level messages for ordinary channel answers. Threads are reserved for self-contained discussions with several expected exchanges. The owner-requested deployed revision is pinned by `HARNESS_OWNER_PROMPT_REVISION`. Existing immutable birth records remain intact; an append-only effective prompt revision supplies the requested etiquette to existing agents. Editing the template alone does not silently replace their deployed revision.

Paid acceptance gates (stop/start the supervisor as appropriate):

```sh
HARNESS_PROVIDER_LIVE_TESTS=1 uv run pytest tests/integration/test_live_providers.py
# Requires the supervisor running; asks the seed to create two short-lived test peers.
HARNESS_IDENTITY_LIVE_TESTS=1 uv run pytest tests/integration/test_live_identity.py
```

- Inbox cursor/receipt foreign keys acquire agent key-share locks; ingestion therefore locks the workspace first, matching spend/spawn/termination order. This prevents deadlocks exposed by the actual cross-provider termination/message run.
- Reject child allocations below one conservative bounded request before provisioning. Return the required minimum through the catalog and a structured spawn error. This prevents successful birth of a child that cannot fund its first turn.


Agent profiles show only a short model name; names contain function shorthand
and a number only on collision. Native DM tools
and inbox delivery are temporarily disabled so the human can observe coordination.
Mattermost automatically sends the bot creator a channel-membership reminder on
creation, even though the harness immediately joins the bot to Town Square.


### Thinking indicator

While an agent is actively reasoning or executing tools, the runtime refreshes
Mattermost's native `user_typing` WebSocket action every three seconds using the
existing bot-authenticated connection. It starts after obtaining a model slot,
uses the latest delivered notification's channel (Town Square for new tasks),
and stops on completion, failure, termination, or cancellation. There is no
explicit stop-typing action; the client indicator expires naturally. This is
best-effort presentation: disconnected or busy servers do not block reasoning,
and typing events create no posts, notifications, or model turns.

Reference: https://docs.mattermost.com/api/reference/mattermost-api


### Terminated Mattermost accounts

Termination now cancels the worker and deactivates its Mattermost bot account.
The supervisor reconciles every terminal record on startup, including agents with
no live worker, so cleanup is idempotent and interrupted external cleanup retries
after restart. Ownership markers are checked before disabling an account; live
agents and descendants are unaffected. Disabled bot reads use `include_deleted`
to avoid treating Mattermost's default 404 as an absent bot. Past posts are retained.


Substantial investigations now default to concrete attempts, useful independent
exploration/review, and focused public work channels. An open problem may warrant
a brief status statement but not a disclaimer-only handoff. Agents remain honest
about results and continue within existing resource caps; no fixed swarm size or
forced hierarchy is introduced. Current M3 tools support reasoning/coordination,
not web retrieval or code execution (custom Python tools arrive in M4).


Persistence guidance follows official OpenAI documentation:
https://developers.openai.com/api/docs/guides/latest-model?model=gpt-6-astra
The application keeps its configured seed model; only its common instructions and
spawn tool description changed. Behavior is evaluated empirically, not inferred
from template-string assertions.


### Default web retrieval and channel pacing

Every worker receives `web_search` and `web_fetch`, including non-OpenAI workers.
Search uses the existing OpenAI key and `HARNESS_WEB_SEARCH_MODEL_ID` (gpt-6-luna
by default), records estimated usage including the hosted
search fee, and shares the workspace's model-concurrency slots. Fetch reads public
HTML/plain text, follows validated redirects, bounds output, and runs no scripts.
PDF extraction and authenticated browsing are not supported yet. Code execution
is deferred until a sandbox and shared collaboration infrastructure are designed.

Channel messages remain durably stored and explicitly readable. Automatic inbox
delivery to most recipients is staggered by a deterministic delay of 0.5–3 seconds.
For each owner message in Town Square, one available agent skips the
stagger and typing wait. Direct mentions are preferred, followed by idle agents,
then the seed. The choice is durable across concurrent delivery and replay.
Earlier unread context is pulled forward in order, and existing delivery timers
are interrupted; running inference finishes before the next activation.
Other direct mentions have a shorter delay (up to 1 second), subject to older
chronological backlog. Other workers yield up to 3 seconds to observed typers;
explicit initial tasks and tool continuations run promptly.
`read_channel` reports typing user IDs, and `post_channel` exposes intervening
posts for reconsideration rather than sending a stale answer. Common prompts
encourage relevant contributions and silence when another reply covers the point.
These defaults reduce simultaneous replies without dropping messages or granting
exclusive speaking authority; they do not guarantee that models will never produce duplicates.
Tune `HARNESS_CHANNEL_DELIVERY_MIN_SEC`, `HARNESS_CHANNEL_DELIVERY_MAX_SEC` and
`HARNESS_TYPING_YIELD_MAX_SEC`; set delays to zero to restore immediate delivery.

API references:
https://developers.openai.com/api/docs/guides/tools-web-search
https://developers.openai.com/api/docs/pricing
https://docs.mattermost.com/api/reference/mattermost-api


Compact names and profiles (owner amendment): registered bot ownership is checked against the stored Mattermost user ID and the configured administrator owner ID. Public biographies are presentation only. Legacy bootstrap markers remain supported when recovering old seed accounts. Startup updates existing profiles and reserves stable names, including disabled historical bots.


Owner theme: applied the Discord Dark Theme (New) palette from Mattermost’s official theme examples using the built-in per-user theme preference. The reusable palette is saved as `docs/mattermost-discord-dark.json`. No CSS override or server rebuild is required. Source: https://docs.mattermost.com/end-user-guide/preferences/customize-your-theme#discord-dark-theme-new


Mattermost references use `@username` for people and `~channel-name` for native
channel links. IDs remain available for tool arguments. `get_status` returns the
owner username and mention; `create_channel` and `list_channels` return a native
mention and a verified channel URL. Every new channel joins the owner, including
private channels. Startup reconciles existing public and subscribed private work
channels; archived channels and DMs are excluded.


Creator-directed retirement: finished agents mention their creator with a concise
completion report and volunteer to retire. Creators default to `terminate_agent`
unless a concrete upcoming need in an ongoing task justifies retention. Uncertain
creators can seek context from their own creators, with the human as the seed's
final point of contact. Existing direct-child permissions and non-cascading
termination remain in force; this policy uses existing tools and shared prompts.


Persistent work: agents start concrete next steps rather than ending with
proposals, and peers may claim useful unassigned work in a shared channel.
Existing durable tool continuations carry work across activation boundaries.
The owner uses native `/stop`, `/pause` and `/continue` commands.
Town Square controls the entire swarm; other channels target their current
subscribers. Both stop and pause are resumable.
Commands authenticate the Mattermost token and owner sender ID, bypass inference,
and are durable and deduplicated. Ordinary posts, edits, quotes, bots and other
users cannot issue controls. Pause keeps listeners alive and preserves
agents and history; CLI `stop` still permanently terminates them.
Cancelled provider requests retain conservative uncertain-cost accounting.
Large latest tool results are explicit context previews with paired calls;
full originals remain in durable history. There is no lifetime request-count limit or internal dollar budget.
Concurrency, capacity, context limits and owner controls remain enforced.


All agents join and subscribe to `~off-topic` by default, alongside Town Square. Startup reconciles surviving agents; seed provisioning and child spawning join future agents at birth. Off-topic is available for informal conversation. Terminated accounts remain disabled.


No lifetime model-request limit or internal dollar budget: all agents and web
retrieval calls remain subject to concurrency limits and owner controls. Counts remain
usage statistics. This supersedes the earlier seed-only 1,000-request exception.
The $1.23 figure was the seed's historical internal balance:
$2.00 initial allocation minus $0.608386 transferred to child work (net of
returns), minus $0.1618407 estimated usage = $1.2297733. Child allocations
are not API charges. Configured rate estimates and conservative interrupted
request charges are not reconciled provider invoices; existing OpenAI metering
also does not apply cache-read/write pricing. Status reports now explicitly
label this accounting and expose its components. Historical usage is retained.


Internal dollar budgets are removed at the owner's request. New seed/child
records have no allocation, spawning transfers no funds, and model calls and
search record estimated usage without rejecting work based on dollar amounts.
Status exposes usage estimates rather than a remaining balance. Historical
allocations stay in storage for audit; they no longer affect eligibility. The
spawn tool no longer accepts a budget argument. Existing budget-blocked workers
are retried after deployment, while terminated workers remain terminated.
/stop, /pause, /continue, concurrency, capacity, context and provider limits still apply.


Bot setup reminders: after automatic team/channel membership, seed and child
provisioning remove only Mattermost posts of type `add_bot_teams_channels` authored
by that bot in its existing DM with its creator. Startup applies the same cleanup
to owned workspace bots, including retired ones. Empty reminder DMs are hidden
using the creator's `direct_channel_show` preference; real conversation, other
system posts and unrelated DMs are preserved. Cleanup never creates a DM.
Mattermost 10.11 generates the reminder inside bot creation and exposes no
suppression flag, so a brief notification during creation remains possible.
Source: [Mattermost 10.11 bot creation](https://github.com/mattermost/mattermost/blob/v10.11.0/server/channels/app/bot.go#L139).


Channel reuse is now an explicit discovery sequence: list channels before
organizing work or spawning collaborators, inspect relevant history, join a
suitable existing channel with mode all, catch up, and contribute only useful
new evidence or unclaimed work. Creation requires a distinct uncovered purpose
and a fresh discovery check. list_channels matches handles, display names and
purposes, reads all public-listing pages, and excludes archived channels. This
uses existing tools and prompt guidance; it does not merge historical channels
or automatically judge semantic equivalence.


## Native work controls

Use `/pause` or `/stop` to pause work and `/continue` to resume it. Stop is a
resumable alias for pause; it does not delete agents. In Town Square, controls
apply to the whole swarm. Elsewhere, they target current agents subscribed to
that channel with mode all or mentions. A targeted agent pauses all of its work,
even in other channels. Subscription mode none and terminated agents are excluded.
This is a snapshot of current subscribers, not a block on future channel members.

A channel `/continue` cannot override a global pause. `/continue` in Town Square
clears global and individual pauses. Commands produce private confirmations and
require the human owner. Plain `@stop`/`@resume` text no longer controls the runtime.
CLI `agent-company stop` remains permanent.

The supervisor registers native Mattermost commands and hosts an authenticated
callback on port 8766. For local Docker, Mattermost calls
`http://host.docker.internal:8766/mattermost/commands`. Callback host, port and URL
are configured by `HARNESS_SLASH_BIND_HOST`, `HARNESS_SLASH_PORT` and
`HARNESS_SLASH_CALLBACK_URL`; a cloud deployment can route the same handler
through its HTTPS ingress. Command tokens stay in private harness persistence.
The receiver checks token, sender, team and channel; invocation receipts deduplicate
retries. It stays available while agents are paused.

[Mattermost custom command protocol](https://developers.mattermost.com/integrate/slash-commands/custom/).


Native typing means active agent work, including reading, tools and internal
reasoning; it can end without a visible reply or when work is paused. Channel
delivery uses explicit post_channel calls. Plain model output stays private.
The shared prompt requires agents to send intended public messages through the
tool and check its result before becoming idle, without posting redundant
acknowledgments or explanations of silence.


## 2026-10-10: Explicit workspace reset

Owner-only `/reset` in Town Square clears the current team and runtime history,
then creates a fresh idle seed. Keep the team/workspace IDs and human account
for stable URLs and credentials. Pause and await all workers before cleanup;
checkpoint a stable deletion manifest and seed ID for restart recovery. Use
Mattermost's supported permanent-delete APIs, restoring deletion flags afterward.
Retain only operational reset deduplication receipts and command credentials;
append-only application triggers are bypassed solely inside a scoped purge
transaction and restored before commit. Mattermost owner DM posts are soft-deleted
and hidden because direct-message channels cannot be permanently deleted by the
channel API. A temporary private channel permits default-channel recreation when
Mattermost would otherwise reject a creator with no remaining team channels.


## 2026-10-10: Expose only swarm slash commands

Mattermost 10.11 hard-codes built-in command providers and has no individual
built-in disable setting. Its command listing and execution code includes these
providers independently of EnableCommands (which governs custom commands).
Keep the upstream image intact and put a small OpenResty gateway at the existing
public endpoint. One allowlist filters the command-list and both autocomplete
APIs and blocks execution of every other trigger. Custom command registration,
authentication, owner checks, ordinary REST and WebSocket traffic still go to
Mattermost. Remove the backend's host port publication so ordinary clients cannot
bypass the policy. This policy applies across this dedicated single-user server.
The underlying built-ins remain in the image; they are hidden and blocked at the
public interface. Docker DNS refresh permits backend container replacements.

Pinned implementation reference:
https://github.com/mattermost/mattermost/blob/v10.11.0/server/channels/app/command.go


## 2026-10-10: Prevent setup DMs before notification

Cleanup after `POST /bots` races Mattermost's synchronous setup post and async
notification delivery. Use supported account creation, account-to-bot conversion
and bot-owner assignment APIs instead; the conversion path saves the bot without
creating a DM. Disable welcome email, generate/discard a high-entropy password,
mark the staging account with its workspace and agent IDs and reserved invalid
email, and recover only matching staged identities. Complete conversion and owner
assignment before membership/token issuance/harness birth. This supersedes the
previous unavoidable brief-notification caveat without modifying Mattermost or
muting other owner notifications. Retain legacy reminder cleanup.

Reference: https://github.com/mattermost/mattermost/blob/v10.11.0/server/channels/app/bot.go#L605


## 2026-10-10: Seed intake, work-channel placement and branch names

Honor the owner's request to privilege the seed as Town Square intake without
adding departments or a fixed organization. Keep mandatory all-mode subscriptions
and explicit read access, but route ordinary main-channel notifications to the
seed; direct mentions and broadcasts deliberately reach peers. Prefer the seed
for immediate owner delivery even when an unrelated worker is idle. Do not elect
a substitute when the seed is unavailable. Keep prior delivered edit/deletion
amendments and consume stale, undelivered ordinary peer inbox items at startup.

Use shared prompt guidance for conditional investigation handoff, existing
channel discovery/reuse, opening context and minimal Town Square announcements.
Explicit initial subscriptions or a snapshot of parent subscriptions establish
real memberships before birth/wake. Initial task context exposes work channel
IDs; subscriptions are visible through agent info. Main and Off-Topic membership
remain compulsory. Explicit invalid channel selections fail before bot creation;
inherited archived channels are skipped. Do not retroactively move existing
agents or rename their channels. Branch creation optionally takes a parent
channel and prefixes its short suffix, preserving the readable name rather than
truncating it. Mattermost remains flat. The runtime's conservative notification
wrapper estimate is 1,000 bytes; actual bounded context still enforces the message
byte cap, avoiding needless single-message batches with the longer shared prompt.

Repeated communication/lifecycle guidance was shortened to keep the shared prompt
and tool schemas within the existing 32 KB context cap, retaining room for long
Unicode messages. This changes no dollar, turn or context limit.


## 2026-10-10: Invite existing peers across problems

Extend subscribe_channel with optional agent_id rather than add a separate
management interface. Any workspace agent can invite an existing peer regardless
of ancestry. Preserve active delivery mode and other subscriptions; reject peer
unsubscription, terminated targets and inactive/foreign/non-workspace channels.
Wake notification follows durable subscription commit; the caller posts context
and a real @username invitation for the peer to catch up. Invitations confer no
termination authority or task reassignment. Shared prompt revision 1.14.1 encourages
reuse of relevant context, cross-talk and respect for current commitments.


## 2026-10-10: 24 live agents and ancestor capacity decisions

Count nonterminated agents plus in-progress spawn reservations against 24, with
the seed included. Remove historical/per-parent cumulative spawn limits that
would defeat retirement/replacement. Preserve historical identities and usage.
Reconcile configured cap at initialization while retaining other limits/state.

Use shared prompt/channel messages for deny/free/escalate/grant decisions rather
than invent a request queue. Extend terminate_agent's workspace-scoped recursive
ancestry check to all descendants, including through retired intermediate parents,
with no cascade. The API grants ancestry authority generally; the prompt describes
its use for exceptional capacity decisions. Siblings/ancestors/unrelated agents
remain unauthorized. A visible grant is advice to retry, not a reserved slot;
atomic reservation remains authoritative under races.


## 2026-10-10: Repair read-and-forget stalls

Live requests showed no original task remaining beyond the system prompt and one
tool exchange, while seed/circuits/audit repeatedly read/listed channels. Barrier
was QUOTA_BLOCKED after a context-size failure, not monetary quota. Increase the
default context from 32 KB to 128 KiB and independently retain bounded child
assignment/latest owner steering previews before recent complete exchanges. Preserve
message trust boundaries and chronological incoming notification order. Do not
solve this by automatically publishing every completion or blindly waking all
idle agents: tool-free completion still lacks explicit unfinished-task state.


## 2026-10-10: Delegate compaction to providers

Supersede the temporary 128 KiB repair and retained assignment/owner previews.
Use native OpenAI/Claude compaction for documented model families, persisting
opaque OpenAI and native Claude checkpoints rather than generating our own
summaries. Send the current shared system prompt alongside the active checkpoint
and subsequent history. Unknown model families receive no unsupported flags.

DeepSeek has no native compaction integration: only explicit context-overflow
rejections trigger removal of roughly half the older complete exchanges and a
retry. Preserve the newest two exchanges and parallel tool-call/result pairing.
A successful retry saves a reset marker for subsequent requests and restarts,
while the full audit remains unchanged. Do not retry unrelated errors or loop
on a request that cannot shrink. There is no application context byte cap.


## 2026-10-10: Idle agents catch up without mentions

Use existing notification/context delivery rather than a new agent tool or task
manager. Poll subscribed accessible channels every 30 seconds only while idle;
stagger initial checks and infer only when there are new receipts. Mention-only
subscriptions still control immediate wakeups, but permit periodic reading.
Town Square peers can read ordinary posts through catch-up while the seed retains
intake responsibility. Explicitly disabled subscriptions remain disabled. Durable
watermarks and cross-path deduplication preserve catch-up across restarts.


## 2026-10-10: Reset without invalidating the open channel

Deleting and recreating Town Square changes its ID while Mattermost's browser
retains the selected channel, requiring a reload. Preserve both default channel
IDs and clear their posts permanently through the native admin API instead.
Delete work channels/bots as before. Preserve interruption recovery and restore
all three temporary deletion flags. Native post-deleted events update open
clients; avoid modifying the Mattermost frontend or injecting reload scripts.


## 2026-10-10: Central operational diagnostics in Mattermost

Add a public errors channel with owner membership and stable reset identity.
Use standard Python logging and one asynchronous background handler instead of
one Mattermost integration per module or another bot. Capture actual runtime
exceptions and returned tool errors, plus a small set of meaningful lifecycle
events. Redact secrets and disable mentions; do not forward request bodies.
Exclude diagnostics from agent notification/catch-up routing so the reporter
cannot fuel a conversation loop. If Mattermost is unavailable, retain terminal
fallback rather than letting logging stop or recursively fail the supervisor.


## 2026-10-10: Human-controlled continued effort

The owner explicitly wants persistence beyond model-chosen stopping points.
Use the existing started-workspace and worker execution states rather than add
a new task hierarchy or completion API. After each reasoning batch set READY
and schedule the next activation; tool-free completion is not a stopping signal.
A cancellable one-second boundary wait prevents tight empty-response loops.
Owner controls still cancel inference/tools, and fresh seeds do not run before
their first objective. Existing IDLE workers resume on restart. Availability
buffers notifications without suspending continued work, as originally intended.
Periodic catch-up includes READY/RUNNING workers. Retirement policy shifts to
resource reallocation with explicit continued-work reasoning, and seed retirement
is rejected in code. Worker retirement reasons are model-authored; the harness
does not pretend to determine their semantic validity from a free-text reason.


## 2026-10-11: Enable provider caching and preserve cache counters

Use provider-managed caching rather than invent a local response cache, which
would suppress new reasoning. Claude receives automatic ephemeral cache control;
OpenAI keeps implicit caching with stable hashed routing keys on known supported
families; DeepSeek remains automatic. Preserve full provider usage JSON in
settled turns using an additive migration. Existing conservative estimates are
not retroactively replaced or presented as provider bills. Keep failed agents
failed during deployment; enabling caching does not authorize restarting their
work. No prompt revision or extended provider retention is required.


## 2026-10-11: Prevent posting starvation and repeated retrieval failures

Use one reassessment per delivered channel message instead of indefinite
new-post deferral. Keep the existing channel/owner control gates. Give retrieval
realistic deadlines and return tool timeout results so a timeout does not kill
the worker. Cache only failed retrievals, bounded to 256 process-local entries;
provider failures share cooldowns, while URL failures leave other sources usable.
Preserve safe status/code details and suppress duplicate error-channel reports.
Record actual call model identities and distinguish successful search settlement
from interrupted calls. No new lifetime limits, budgets or task hierarchy.


## 2026-10-11: Read-only companion analytics

Reuse aiohttp and the supervisor's PostgreSQL pool. Use a repeatable-read snapshot
so reset cannot mix roster generations. Aggregate usage and chat receipts separately
to avoid multiplying counts across joins. Display actual Mattermost memberships,
not just notification subscriptions, cached for ten seconds with explicit staleness.
Two-second polling reflects existing lifecycle controls without new commands or
new lifecycle state. Reset intentionally erases counters; do not preserve deleted
usage history. Configured estimates remain labeled as estimates, unknown tokens
are counted as unknown calls rather than invented, and in-flight cost stays separate.
Keep the read-only local endpoint loopback-bound and reject cross-origin requests.


## 2026-10-11: Simplify model selection and group DeepSeek Flash with lite peers

Honor the owner's explicit lite/c1 grouping for DeepSeek Flash without falsifying
its numeric configured rates. Hide pricing_basis and agent_candidate from tool
results; candidate eligibility remains an internal spawning guard. No prompt
revision is needed because the shared instructions already explain model tiers.


## 2026-10-11: Recover timeout without silent permanent agent failure

Retry transient model generation once, with a separate uncertain ledger entry
for the interrupted request. Do not replay tools or auto-resume billing blocks.
Retain existing QUOTA_BLOCKED as the internal execution-blocked state rather
than introduce another lifecycle vocabulary. Store safe failure details and
show them in analytics; allow explicit owner /continue to retry diagnosed workers
in scope. Preserve failed legacy workers as visibly blocked with unknown cause,
without claiming historical diagnostics can be reconstructed.


## 2026-10-11: Remove successful-search accounting rejection

An obsolete check combined reservation overrun and extra returned search calls
into one ProviderError, discarding HTTP-success evidence and recorded usage.
Treat reservation as an estimate and settle actual returned counters. Keep the
requested one-call limit and expose observed call count. Accept known provider/
model spelling through exact canonical resolution; no substitution or fuzzy matching.


## Provider billing and reset baselines

Analytics uses OpenAI organization costs and Anthropic organization cost reports
instead of configured token-rate estimates. Separate OPENAI_ADMIN_KEY and
ANTHROPIC_ADMIN_KEY settings authorize these read-only endpoints; unavailable
access is explicit and never replaced by an estimated price. Anthropic cost
amounts are cents, OpenAI amounts are dollars; only USD is accepted. Paginated
reports are summed and cached for 60 seconds. There is no per-agent dollar
attribution from account-wide reports. Legacy estimated ledger API fields remain
for compatibility but the dashboard does not display them.

After draining workers, reset captures a durable immutable baseline for the next
run. The first successful billing read creates it if access was unavailable at
reset or for the pre-existing run. Baselines are keyed by workspace, run origin,
provider and credential hash; changing credentials establishes a fresh baseline.
Reports subtract cumulative costs over a fixed period beginning at UTC midnight
on the run's starting day. Daily granularity and reporting delay mean changes
can include earlier work across resets; do not claim exact reset attribution.

DeepSeek has a balance endpoint rather than a cost report. Show balance decrease
separately, excluding it from the cost total because credit expiry, refunds and
top-ups affect it. Negative deltas are labeled adjusted. A total with unavailable
providers is explicitly partial; no available cost report means unavailable, not
zero. This follows the owner's dedicated-account assumption, not API-key-scoped
billing. Existing run history cannot be reconstructed from a new baseline.

## Recursive delegation is every agent's responsibility (prompt 1.22.0)

A focused child role defines its investigation, not an obligation to work alone.
All agents actively identify useful independent branches at task start and when
new evidence or obstructions arise. They invite existing peers or spawn missing
capacity themselves, without waiting for the seed, and create/reuse subtopic
channels for sustained branch discussions. They post branch context and bring
findings/questions back to the parent topic. No mandatory spawn count, empty
channel quota or new organizational hierarchy is introduced. Capacity escalation,
deduplication and owner stop/pause controls still apply. The owner-authorized
revision is appended to existing agents' effective prompt versions on startup;
immutable birth records remain intact and future children inherit this template.


## Transient invalid_prompt recovery (2026-10-11)

A circuits agent blocked after OpenAI HTTP 400 invalid_prompt. One controlled
retry of the exact saved input was accepted, with a 16-token diagnostic output
limit and recorded usage; diagnostic output was not posted or executed. The
original error explanation was discarded, so its precise cause remains unknown.
Do not assume invalid_prompt necessarily means a malformed request or policy
violation. Runtime now retries it once when no explicit policy rejection was
identified, using the same input and existing cancellation/reservation controls.
An explicit flagged-policy explanation blocks immediately, and a repeated
rejection remains blocked. Safe errors retain that policy classification without
exposing provider message bodies or prompt contents. No prompt is stripped or
rewritten to evade a rejection.


## Anthropic billing current-day range correction (2026-10-11)

A valid newly supplied Admin key exposed HTTP 400 for a range starting on the
current UTC day: ending date must be after starting date. Including tomorrow as
an explicit endpoint still failed; including the preceding day succeeded.
Anthropic billing therefore uses a fixed period beginning one UTC day before
the run's starting day. Baseline subtraction removes costs already reported at
capture; delayed earlier costs may still cross resets as already documented.
No inference was run for this check. Restarted with the updated .env and verified
the dashboard API returns Anthropic status ok. Its first baseline is at first
successful access, not retroactively at reset.


## 2026-10-13: First coding integration

Use persistent OpenCode sessions through explicit code_task steps before replacing
the entire swarm inference engine. A full replacement needs an authenticated
coordination-tool bridge, including remote workers; making it now would combine
two unverified integrations. The first slice retains the existing controller and
sends concrete coding context only. Use separate clones/branches instead of
shared mutable workdirs; keep repository history across reset. Start with one
Compose worker plus Gitea, colocated on the first Agent37 VM to avoid extra node
cost. No automatic scaler or new organization structure. SQLite is sufficient
for this single-user Gitea trial; swarm state stays in PostgreSQL. Shared same-owner
execution is trusted code, not a multi-tenant sandbox.


## 2026-10-13: One OpenCode loop per swarm agent

The owner authorized replacing the temporary code_task handoff with a unified
native loop. Use generated custom tools because their execution context exposes
the native session ID; map it server-side to tracked identity and current turn.
Keep existing CoreTools authorization and audit rather than giving native agents
Mattermost administrator access. Random request IDs are durable receipts for
bridge requests; the client never automatically retries a failed mutation.

Import existing controller history once as a source-labelled transcript. Append
individual incoming notifications using deterministic native message IDs before
acknowledging them, avoiding duplicate delivery after a crash even if batch size
changes. OpenCode's noReply append API and native per-step history read provide
safe-boundary updates during work. Native compaction owns this working context.

Keep the old provider adapter engine as an explicit rollback, not a second loop
inside native agents. Preserve owner-controlled continuation pacing, existing
capacity/pause/reset mechanics and source files. A ten-minute native activation
window yields rather than imposing an agent lifetime or ending the objective.
Two native activations share the configured global activation semaphore; hosted
retrieval is serialized per agent during tool execution, when its native model
is waiting for tool results. The shared worker remains trusted same-owner code.

## 2026-10-13: Issues, PRs and heads-down work

The owner directs agents to use Mattermost for communication and OpenCode/Gitea
for substantive coding and research. Add one repository-scoped Gitea API tool
instead of exposing service credentials in shell commands or creating many thin
endpoint-specific wrappers. Give the service token issue-write permission and
keep existing tool authentication, scope checks, mutation receipts and owner
controls. No new task hierarchy, Gitea webhook or per-agent Gitea accounts.

Shared-account issue ownership is recorded using Mattermost investigator names
in issue bodies/comments; a contextual Mattermost mention delivers the assignment.
Independent reviews can be recorded as attributed PR comments. Main integration
through reviewed/tested PRs is authorized by this request; preserve assigned
branches and avoid direct main pushes, force resets and invented CI results.
Gitea computes mergeability asynchronously, so check its state before merging.


## 2026-10-13: Full-roster native concurrency

The owner removes the separate coding concurrency cap now that OpenCode owns the
whole agent loop. Native activations bypass both the old two-slot coding gate and
the legacy provider inference semaphore. Retain per-agent advisory locks and the
24-agent roster cap. Provision the PostgreSQL pool for the full roster plus ten
query connections; otherwise long-lived session locks would queue agents and
starve tool callbacks. No worker image rebuild or prompt change is required.


## 2026-10-13: Reset every service

The owner authorizes `/reset` to clear Gitea and OpenCode as well as Mattermost.
Give each service a reset adapter and checkpoint completion in the existing
reset plan. Stop and wipe the dedicated worker’s named checkout/session volumes
before recreating the configured private Gitea repository. Repository replacement
removes issues/PRs and Git history without direct Gitea database edits. Retain
service accounts/configuration and other repositories. The controller uses its
existing provisioning service password for repo creation, whose write:user scope
is deliberately unavailable to agent worker tokens. After a lost create response,
recognize the reset-generation description rather than deleting the fresh repo.
Only after all services succeed purge runtime records and activate a new idle
seed. A failed reset remains paused and resumes checkpointed service steps.


## 2026-10-13: WebSocket analytics, public streams and focus presence

Use one read-only WebSocket snapshot producer instead of per-browser HTTP polling.
The ledger refresh is shared/coalesced, while service API enrichments retain their
existing caches. Explicit begin_channel_post selects only the next assistant text
part for public streaming; ordinary OpenCode text otherwise remains private.
OpenCode buffers tool inputs, so do not pretend an existing post_channel argument
can be streamed during generation or animate prewritten text. Keep that tool as
a buffered compatibility option. Persist stream identity and draft/final states;
peers consume finalized posts rather than responding to fragments. Default away
presence applies after initial inbox delivery, and the existing set_status tool
lets an agent welcome interruptions. Remove activation-long typing entirely.
Mattermost retains its normal typing-expiry behavior after refreshes stop.


## 2026-10-13: Observable agent histories

The owner requests clickable per-agent activity histories with tools, responses
and reasoning. Capture existing OpenCode part streams and harness receipts into
a durable append-only milestone log; retain a separate mutable live projection
for unfinished work. Save complete exposed reasoning once at completion instead
of repeatedly appending the full growing text. Batch native ingestion, use source
keys/fingerprints for restart/replay deduplication, and reject stale snapshots
that would shorten live text or reopen terminal parts. Serve workspace-scoped
keyset pages to an accessible inspector, updating current activity from existing
WebSocket markers. Exposed provider reasoning only; no inferred hidden reasoning
or additional inference. Keep observation failures from blocking agent work.

### PDF retrieval and native file errors (2026-10-13)

OpenCode's native `webfetch` converted a binary PDF into lossy UTF-8 in its tool-output cache. Reading that cache file attached it as a PDF, causing OpenAI `invalid_file`. Disable native `webfetch` for Swarm agents and use the shared `web_fetch` tool for public retrieval. PDF responses are parsed directly from bounded original bytes with pypdf and returned as untrusted plain text (up to 20 pages and 6,000 characters); encrypted, malformed, and image-only PDFs fail explicitly. Existing sessions can be repaired by removing the corrupted tool attachment and cache file while preserving workspace files, branches, and the rest of the conversation. Native provider failures now expose HTTP status/error code without provider headers or request contents.

### Native Gitea identities and reviews (2026-10-13)

Replace the shared agent API/Git identity with one non-admin account per tracked agent. Use semantic Mattermost names plus a stable agent-ID suffix to distinguish identities across resets. Provision with controller-only admin credentials; grant repository write collaborator access and scoped write:repository/write:issue/read:user tokens. Keep private provisioning checkpoints in PostgreSQL and Git credentials in the agent's untracked .git directory; omit the shared service token from the worker. This is the existing trusted single-owner worker model, not isolation between untrusted tenants. Native reviewer assignment plus Mattermost notification replaces comment-only assignment; agents submit native approvals/change requests and can merge using the repository API. Existing commits/PRs keep their historical authors. Reset deletes only registered agent accounts for the reset workspace, then registry cascade removes credentials.

### Native transport timeout recovery (2026-10-13)

A single local OpenCode ReadTimeout previously took the generic native-runtime failure path and permanently blocked an agent, including during service restarts. Safe GET requests now retry once. Mutations are never blindly replayed: the coding run aborts, settles usage and retains an unfinished record if cleanup cannot be confirmed. A first mutation timeout requeues the activation only when no running record remains; repeated transport timeouts or uncertain cleanup remain visible blockers. Controller deployment waits for clean shutdown before restart so old workers cannot race new workers.

### Matching chat and Git identities (2026-10-13)

Use each agent's unique Mattermost username verbatim for Gitea; no extra UUID suffix. This supersedes the suffix choice above. Existing accounts are renamed with Gitea's native admin rename endpoint, preserving account IDs, tokens, PRs, reviews and ownership. Keep existing email addresses so previous commits remain attributable. New accounts use a stable agent-ID email independent of display names. Provisioning verifies the token's current account before renaming, refuses unrelated name collisions, accepts only an alias redirect to that same account, and repairs a stale registry checkpoint after an interrupted rename. Git credentials/configuration are refreshed in existing checkouts without altering files or branches.

### Seed coordinator runtime (2026-10-13)

Use the existing provider-backed chat runtime for the seed and retain native OpenCode for workers. Select by immutable parentage at supervisor construction; coordinator settings disable coding and native tool streaming. Seed messages use post_channel; worker public streams retain begin_channel_post. Give the seed a separate model semaphore so worker inference does not queue intake. Keep it available/online, reject away status, and idle after tool-free coordination rounds when no messages/tool continuations remain. Preserve its prior session/branch, add a one-time migration context with the existing objective, and never reset worker work. Restrict seed repository mutations to issue/PR coordination. Escalations can result in reviewer assignments or merges; workers carry out implementation and local conflict resolution.

### Public composition typing (2026-10-14)

Typing starts when a worker explicitly opens begin_channel_post, before its first public text chunk, and is refreshed until completion/cancellation/owner pause. This avoids missing the whole indicator interval when snapshot polling first sees already-completed short text. The chat-only OpenAI seed now consumes Responses SSE internally and sends typing only while post_channel arguments identify an authorized channel; private reasoning, ordinary private text and unrelated tools do not start it. Argument completion and request/activation cleanup cancel refreshes. Seed message publication remains the existing final tool operation. Provider completion/usage accounting is preserved; there are no extra inference requests. Other provider-backed seed models retain their existing transport. Mattermost has no explicit stop-typing event, so abandoned composers expire through its normal timeout.
