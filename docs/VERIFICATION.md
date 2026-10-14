# Milestone verification — 2026-10-09

## M0 — passed

- Read the canonical specification in full and copied Section 22's template verbatim.
- `uv sync --locked --python 3.12`: Python 3.12.10; reproducible lockfile.
- Docker Compose v2.39.4 standalone CLI: `--env-file .env.example -f infra/compose.yaml config --quiet` passed without a running daemon. The validation binary was downloaded outside the repository while Docker Desktop was being installed.
- Ruff formatting/lint, strict mypy, and the pure unit suite passed before M1 implementation began (10 tests).
- Local startup and clean shutdown are documented in README.

## M1 — passed

- Docker Desktop 4.94.0 / Engine 29.8.2 / Compose v5.5.1 runs the stack. Both services report healthy after `docker compose --env-file .env -f infra/compose.yaml up -d --wait`.
- Installed Mattermost version: 10.11.0. `mmctl --local system status` reports server, database and filestore OK.
- Bootstrap created one local human owner, one workspace/team, explicit public `#general`, and exactly one seed bot. Repeating setup preserves the seed ID, bot ID and rendered prompt. `#general` membership is precisely owner plus seed.
- Installed settings: bot creation enabled; personal access tokens enabled; maximum users per team 200; `IsLicensed=false`. Bot provisioning/token issuance and caller-scoped operations work on this installed version. Source review found no bot-count/license gate in its creation path; a sustained 20-agent load test remains M5.
- Fixed two live startup issues: mounted shell-script execution during PostgreSQL initialization and curl missing from the Mattermost image. Initialization now uses SQL with `\getenv`; health uses the bundled mmctl binary. Fresh initialization/password access is verified on an isolated disposable PostgreSQL container, which is removed after the test.
- Full verification: **32 tests pass** (29 unit, 3 live integration), Ruff formatting/lint, strict mypy and Compose config pass. The package wheel/source build also passed during M0/M1 development.
- Real-service tests verify authenticated owner-post receipt, a bot-authored thread reply, posts/history, public channel creation, native DM, adding/removing reactions, native search, and automatic WebSocket reauthentication/receipt after a forced transport close.
- The actual CLI listener is exercised as a separate process: an authenticated owner post gets exactly one seed acknowledgement; SIGINT exits cleanly and releases its lock.
- Unit coverage includes exact shared template fidelity, frozen rendered prompts, finite positive limits, rejection of owner impersonation/edit/delete events, thread roots, successful-response deduplication, bot-scoped calls, single-seed retries, partial-bot cleanup, orphan-token recovery, and private atomic checkpoints.
- M2 has not begun: durable inbox/backfill, PostgreSQL runtime repositories, paid inference, agent spawning/lifecycle policies, custom Python tools, and swarm metrics remain at their specified milestones. M1 replies are fixed communication-check acknowledgements.

## M2 — passed

- Verified configured `gpt-6-luna` through the authenticated provider models endpoint and a real JSON tool-call round trip. Credentials remain in private `.env`; no alternate paid model was substituted.
- **40 tests passed**: 32 unit tests, seven isolated-schema PostgreSQL runtime tests, and a fresh-container database initialization test. **One additional paid live acceptance test passed** against the configured Mattermost workspace and OpenAI adapter (41 tests across these gates). M1's original real-service gates had passed before M2 began.
- The automated paid gate starts the actual runtime, sends an authenticated owner instruction, verifies a threaded bot reply containing 437 for 19 × 23, and checks a persisted successful `post_channel` call. It then marks the agent away, queues 17 + 25, restarts the runtime with a fresh database connection, verifies unchanged prompt and no away inference, restores available, and receives 42 with an empty inbox and zero outstanding usage reservations.
- Runtime tests verify durable receipt deduplication/self-echo suppression, chronological batching, reservation recovery, persisted availability, immutable birth records, append-only audit, mandatory general subscription, concurrent budget enforcement, conservative ambiguous-call charging, stop enforcement, stale edit/delete previews, single activation despite concurrent wakes, safe model boundaries and bounded Unicode notification storms without message loss.
- Ruff formatting/lint, strict mypy (24 modules), and wheel/source package builds passed. Docker services remain healthy.
- The retired M1 workspace and its original prompt remain archived. The active M2 workspace is `6888e509-3280-4112-a484-309e61d14fef`, containing exactly one seed. Spawning/coordination, custom Python tools, 20-agent load and cloud deployment remain M3–M6.

## M3 — spawning/coordination gate passed

- Root cause confirmed from the installed tool manifest, unimplemented repository birth method, and saved M2 operational prompt: no spawn capability existed. Added `spawn_agent`, `list_agents`, `get_agent_info`, `terminate_agent` and Mattermost channel/subscription/search/DM/reaction tools.
- **46 tests passed across the automated gates** (34 unit, seven M2 database tests, five coordination database tests); strict mypy and Ruff checks passed. Coordination tests cover immutable child instructions and startup task, idempotent birth, mandatory general subscription, private prompt/credential non-disclosure, recursive ancestry, unrelated and transitive termination refusal, descendant survival, exactly-once unused allocation return, concurrent capacity reservations, model/tool allowlisting, provisioning rollback, parallel activations and terminal identity non-resurrection.
- One additional paid running-supervisor M3 gate passed. An authenticated owner asked for an integer-square-root design with independent correctness review, without a spawn instruction. The seed autonomously created **two children** with unique bot identities and parent-authored tasks. Child tool-authored communication and automatic native DM delivery in both directions were verified. Attempted child-to-parent termination was rejected.
- Actual model-driven `send_dm` calls also appeared from the agents; the harness did not impose the decomposition. The common Section 22 prompt is unchanged. Old M2 workspace is stopped/archived; active M3 workspace is `982d34d3-7969-43aa-8a46-67b8db72d785` (see bootstrap checkpoint for authoritative UUID).
- After recovery/restart, the seed posted its final proof-backed synthesis, terminated one completed direct child, and the surviving agents returned to IDLE with empty inboxes. A mock-provider test verifies 15 consecutive tool rounds yield/resume and complete without the old per-activation terminal round limit. It also exposed and fixed an empty-completion case that otherwise resurrected the previous tool exchange.
- Longer-run reliability is not a completed M5 load gate. Two explicit provider `invalid_prompt` failures were observed; diagnostic retries succeeded. Custom Python tools, sustained 20-agent load and cloud deployment remain M4–M6.

## Owner-requested identity/model/etiquette revision — verified

- Actual authenticated discovery succeeded for all three configured providers. OpenAI returned accessible model IDs, Anthropic returned model/capability metadata, and DeepSeek returned `deepseek-flash` (display name DeepSeek-V4.1-Flash) and `deepseek-v4-pro`. No credentials are printed or exposed in tool results.
- **53 deterministic tests** cover the prior runtime plus provider catalog coalescing/TTL/pagination/partial failures, name composition, Anthropic parallel tool-result translation, normalized Anthropic/DeepSeek continuations, and concurrency between inbox foreign-key inserts and usage reservations. Ruff, strict mypy and package build pass.
- **Three paid provider gates passed**, each with a real tool call followed by a tool-result continuation on the exact requested model: OpenAI `gpt-6-luna`, Anthropic `claude-haiku-5-5`, DeepSeek `deepseek-flash`. Requests reserve budget before calling the provider and settle actual usage.
- A live workspace gate passed after repair/recovery of the original test request, without replaying that owner request or spawning another test batch. It verifies My Swarm, exactly Town Square and Off-Topic as active channels, Markdown Communication Etiquette for all stored agents, cached `list_models`, semantic parent-chosen child names, distinct Anthropic/DeepSeek model IDs, and top-level marker/arithmetic replies from both providers.
- The live concurrency run exposed a lock-order deadlock: inbox cursor/receipt foreign keys acquired agent key-share locks before requesting the workspace lock, while spend/termination acquired the workspace first. Inbox ingestion now acquires the workspace lock before those foreign-key inserts. A 20-way concurrent inbox/reservation regression test passes; interrupted runtime state was recovered without changing conversations/birth records.
- The first cross-provider attempt also exposed too-small parent-chosen budgets. Spawn now rejects allocations below a conservative first-request bound before provisioning; the cached catalog exposes `minimum_turn_budget_usd`. The live gate uses $0.10 allocations and temporary children terminate after posting.
- Existing team URL remains `org-982d34d37969`; display name is My Swarm. `#general` is archived with history preserved; main URL is `/org-982d34d37969/channels/town-square`. Agent IDs/ancestry and original immutable birth prompts remain intact. Owner-approved effective prompt versions are append-only and pinned to the requested template hash.


Typing indicator verification: 59 unit/database tests passed, including authenticated
WebSocket sequence/reconnect behavior and a blocked model activation that refreshes
typing and cancels the heartbeat on becoming idle. Ruff and strict mypy passed.
A real bot-authenticated `user_typing` event in Town Square was received by the
human owner's WebSocket, without a model call or chat post.


Termination cleanup: 60 unit/database tests passed. The regression verifies
restart cleanup without a worker, repeated disable reconciliation, ownership
checks, and preservation of live descendants. Provisioning and cleanup tests
were repeated after adding `include_deleted` to disabled-bot reads (6 passed).
Live reconciliation deactivated all 12 terminated bots; the original seed remains
enabled and is the sole surviving harness agent.


Investigative persistence: unit tests passed (45); exact canonical template and
requested copy checks passed again after revision 1.4.1 (13 bootstrap tests).
Ruff and strict mypy passed. A paid exact-request prompt eval selected one public
work channel and two distinct peer assignments (1 passed, fixture tool results).
Initial revision 1.4.0 passed the fresh-context eval but failed a real replay in
the existing conversation: it returned a disclaimer after searching old messages.
Revision 1.4.1 explicitly overrides earlier disclaimer-only behavior and requires
a concrete exploration/review cycle. Real replay then created #p-vs-np, spawned
hierarchy and barrier peers, and posted a concrete hierarchy argument and gap.
The original seed reached its existing 100-turn cap during this work; no limits
were raised. New peers retain their own finite turn allocations.
`HARNESS_INVESTIGATION_HISTORY=1` optionally includes existing bounded chat history
in the paid fixture eval; run with the supervisor stopped and remaining seed turns.


Default retrieval and channel pacing: 74 unit/database tests passed, followed by
one additional shared-concurrency regression and the 9 web tests (10 passed);
75 distinct tests pass. Ruff and strict mypy passed. Tests cover durable delayed
chronological delivery, explicit early reads, bounded typing waits and coalesced
peer replies, live WebSocket typing awareness/expiry, stale-post deferral, public
address pinning and redirect validation, bounded HTML/text extraction, metered
search citations and uncertain/cancelled usage settlement. The concurrency gate
blocks search while a regular inference owns the sole workspace slot.

Live page retrieval fetched the official Clay P-versus-NP page (5541 characters).
A metered real gpt-6-luna hosted web search returned 12 source entries and charged
USD 0.01226675 to an existing agent ledger. Deployed revision 1.5.0 and migration
004 were verified for all three non-terminated agents. Code execution, PDF
extraction and authenticated browsing remain deferred. Duplicate replies are
reduced by pacing, fresh-context checks and prompt etiquette; no guarantee of
perfect model participation decisions is claimed. The original seed remains
subject to its exhausted 100-turn lifetime cap.


Compact names and model biographies: 76 unit/database tests passed, including
concurrent collision reservations, stable repeat reservations, existing human
username avoidance, compact model labels and bot owner/identity checks. Ruff and
strict mypy passed. Canonical specification and requested typo copy match.
Migration 005 and revision 1.6.0 were applied to the live workspace. All 15
Mattermost accounts have unique compact names and model-only bios of at most
20 characters. Live surviving accounts are seed (gpt 6 luna), hierarchy
(haiku 5.5), and barrier (haiku 5.5). All 12 terminated accounts remain disabled.
A second supervisor start recovered the compact seed profile successfully and
reconciled all three surviving agents. The seed's existing quota remains blocked;
no turn or budget limits were changed.


Readable Mattermost references and owner channel visibility: 80 unit/database
tests passed; the 13 bootstrap/template tests passed again after final prompt
revision 1.6.1. Ruff and strict mypy passed. Tests cover native channel mentions
and verified URLs, owner username lookup, owner membership at both public and
private channel creation, and startup reconciliation excluding archived channels
and DMs. Live verification confirmed the owner is now joined to the existing
P vs NP investigation channel. A real child bot created a temporary private
channel and successfully added the owner; the empty verification channel was
archived immediately afterward. No chat posts or model calls were made by that
verification. All surviving agents have the updated effective system prompt.


Creator-directed retirement: 80 unit/database tests passed, including immutable
birth/effective prompt revision checks and existing direct-child-only,
non-cascading termination and bot cleanup regressions. Ruff and strict mypy
passed. Revision 1.7.0 is deployed to all three surviving agents. Existing
get_agent_info resolves both surviving children's creator to the current
username seed, enabling a readable @seed completion report. This policy is
prompt guidance using existing tools; creator judgment requires the creator
to be runnable. The seed's exhausted lifetime turn cap was not reset or raised.


Persistent work and owner chat controls: the full unit/database suite passed
86 tests; a further inbox/tool grouping regression was added, and the affected
context/runtime/control gate passed all 20 tests (87 distinct tests verified).
Ruff and strict mypy passed. Command tests exercise owner-only exact posts,
ignored quotes/edits/foreign-team messages, deduplication, ordering across replay,
paused inference rejection, preserved agents/history, PostgreSQL control listener
cancellation of in-flight inference, conservative uncertain usage settlement,
active tool cancellation and interrupted-result recovery, and resumption while
ingestion remains connected. Paused state survives worker recovery; permanent
CLI stop cannot be reversed by chat resume. Per-agent turn overrides retain all
existing counters and dollar-cap enforcement.

Live rollout applied migration 006 and revision 1.8.0. The seed's lifetime limit
is 1,000 by explicit owner approval; peer limits and all dollar caps are unchanged.
Actual histories fit bounded context after fixing oversized latest-result previews
and splitting earlier inbox context from paired tool exchanges. The investigation
resumed, created #pnp-proof-attempt with owner membership verified, and progressed
past the former 100-turn blockage (114 turns observed, actively running). Peer
allocations can still hit their finite budget limits; these were not increased.
No owner chat control posts were fabricated in the production workspace.


Default off-topic membership: 27 provisioning/bootstrap/coordination tests passed. Ruff and strict mypy passed. Tests verify seed membership, idempotent setup and child default subscription, alongside existing birth/lifecycle invariants. Revision 1.8.1 was deployed. Live checks confirmed all six surviving agents are members of off-topic, subscribed with mode all, and have the updated shared prompt. Terminated accounts were not reactivated.


Removal of request-count limits and accounting explanation: 87 unit/database
tests passed, followed by 8 status/control tests after adding explicit accounting
labels. Ruff and strict mypy passed. A regression reserves inference with over
one million historical requests, while still rejecting requests exceeding dollar
budgets. Migration 007 removes turn overrides and obsolete workspace count-limit
settings. Both model inference and shared search now use reservation without a
request-count limit; immutable history and usage counters are retained.

Live revision 1.9.0 was verified for all surviving agents. The seed was actively
running at 344 counted requests, with no turn-limit column or enforcement. The
original $1.2297733 balance is reconstructed from a $2 seed allocation, net child
funding of $0.608386, and $0.1618407 recorded estimated usage over its first 100
requests ($0.15091245 settled estimates plus $0.01092825 uncertain reservation
charges). These figures are internal estimates, not an OpenAI dashboard balance.
The OpenAI adapter does not account for cache-specific pricing; no claim is made
that this ledger reconciles the owner's reported $0.12 dashboard charges.


Prompt first delivery and removal of internal dollar budgets: 90 unit/database
tests passed. Ruff and strict mypy passed. Timing regressions verify one durable
immediate recipient under concurrent ingestion, preference for direct mentions
and idle workers, interruption of an existing delivery timer, bypass of peer
typing waits, and preservation of chronological context. Accounting regressions
verify concurrent calls beyond historical allocations, no count or dollar cap,
spawning from an exhausted allocation, no funding transfer on retirement, and
conservative recovery of unknown usage. Migration preserves history, usage and
counts, retries previously blocked workers, and leaves terminated agents intact.

Live migrations 008/009 and prompt revision 1.10.0 were applied. All seven
surviving workers reconnected with the current prompt; six were observed RUNNING
and one IDLE, with no remaining budget-blocked worker. Workspace state is active.
Configured ordinary staggering is 0.5–3 seconds and typing yield is at most
3 seconds; one eligible recipient of owner Town Square posts bypasses both.
Timing is verified using isolated database/fake-provider tests; no owner messages
were fabricated in the production workspace. The supervisor remains running.


Bot setup reminder cleanup: 22 provisioning, lifecycle and cleanup tests passed.
Ruff and strict mypy passed. Regressions verify exact system-post type and bot
author matching, existing creator-DM matching, preservation of real conversation
and unrelated messages, pagination before deletion, and no creation of missing
DMs. Live cleanup removed 23 generated setup reminders using Mattermost REST;
startup reconciliation is idempotent and hides reminder-only DMs. Automatic
Town Square/off-topic membership remains in the existing provisioning path.
Live follow-up found zero remaining setup reminders; repeating cleanup removed
zero posts. The restarted supervisor reconnected workers and honored the owner's
@stop, leaving listeners running in paused state. Eleven canonical prompt and
bootstrap tests also passed after documentation updates.


Search investigation and channel reuse: the historical relativ/Haiku web_search
error at 2026-10-10 01:17:38 UTC was QuotaError with detail "workspace/agent budget
or state blocks inference". Later successful searches from relativ, barrier and
oracle (all Haiku) confirm shared retrieval works after removing dollar budgets.
No provider-specific search adapter change was needed. Owner pause/resume
controls were preserved.

35 prompt/bootstrap, retrieval, channel-reference and coordination tests passed,
along with Ruff and strict mypy. Discovery regressions verify matching a display
name unlike its handle, case-insensitive purpose lookup, pagination beyond 200
public channels, deduplication of joined/public listings and archive exclusion.
The common prompt and tool descriptions now specify discovery, subscription,
catch-up and reuse before creation. Canonical architecture copies remain equal.
Live revision 1.11.0 was verified in all 10 surviving agent prompts. Workers
reconnected; workspace state is active following the owner's @resume.


Native slash controls: 99 unit/database tests passed, followed by five focused
checks including two additional registration/channel-cancellation regressions
(101 distinct checks overall). Ruff and strict mypy passed. Tests cover owner
authentication, invalid tokens/teams, argument and DM rejection, ephemeral
responses, durable duplicate receipt behavior, prevention of terminal revival,
channel subscriber targeting (including mentions, excluding none), persistence
across recovery, global pause precedence, global continue clearing local pauses,
blocked inference/spawning, selective cancellation and listener survival. Command
registration is idempotent, recovers a partial creation and rejects conflicts.

The larger prompt exposed a conservative inbox batching loop that could release
every notification and retry forever. Batching now retains at least one item
for the actual bounded-context check; oversized context still fails explicitly.
The Unicode notification-storm regression passes with no loss or empty-batch loop.

Live migration 010 and revision 1.12.0 were deployed. Native commands were
registered with autocomplete and verified through Mattermost commands/execute.
/pause in pnp-audit affected three current subscribers; /continue there cleared
their pauses while preserving the pre-existing global pause. /stop in Town Square
affected all 11 surviving agents. All responses were ephemeral. The swarm remains
paused, with ingestion and the command receiver running. Port 8766 is used after
finding another local service bound to 127.0.0.1:8765. No agent was deleted or
revived, and the owner's existing global pause was preserved.


Typing investigation: current workspace was paused. Histories showed tools-only
activations, explicit decisions to remain silent, and uncertain turns cancelled
by pause. Some useful plain model text also remained private without a
post_channel call; no automatic forwarding is intended by the architecture.
Prompt revision 1.12.1 makes explicit public tool delivery and pre-idle delivery
verification mandatory while retaining silence and active-work typing semantics.
23 prompt/bootstrap/runtime checks passed (including one repeated storm check),
as did Ruff and strict mypy. Context bounds and typing refresh/cleanup were
verified; no private draft was republished into a channel.


## 2026-10-10: Owner `/reset`

104 checks passed in the unit/runtime/coordination/reference/control/delivery/slash
control/reset suite; two additional reset checks then passed with the complete
five-test reset suite (106 distinct checks). Ruff and strict mypy pass for all
34 harness files. Isolated PostgreSQL schemas verify purge isolation, transaction
rollback, restoration of append-only triggers, command authentication and
workspace-wide scope, durable reset receipts, blocked continue during reset,
and cancellation of reasoning and ingestion before cleanup.

A throwaway Mattermost team verifies permanent removal of public, private,
archived and both default channels, old messages and bot accounts; replay after
external deletion; preservation of the owner and unrelated workspace; and an
idle, unstarted replacement seed. Deletion flags return to their original values.
The test artifacts are removed and temporary teams archived. The live supervisor
was restarted and all four native slash commands registered. Production remains
paused with its 11 nonterminated agents; its history was not reset. Both
permanent-deletion API flags remain false outside cleanup.


## 2026-10-10: Slash-command public allowlist

The container's LuaJIT policy checks pass for the four allowed controls, removed
commands, malformed requests, similarly prefixed names, command-list and suggestion
filtering, empty JSON arrays, chunked responses and unmodified unrelated responses.
OpenResty's configuration validation succeeds. Seventy selected Python checks
(unit tests, slash-control integration and gateway integration) pass. Live API
checks show exactly the four swarm controls in both command lists and slash-root
suggestions; `/shr` suggestions are empty. `/shrug`, `/logout`, `/msg` and
`/resetting` are rejected before execution. An allowed `/pause` with no channel ID
reaches Mattermost's validation without applying any control. Login, unauthenticated
error responses, the ordinary web page and authenticated WebSocket connection
remain functional. All three Compose services are healthy; only the gateway
publishes host port 8065 and the Mattermost backend has no host port. The swarm's
paused state and historical agent records remain intact. No reset or paid model
call was executed for these checks.


## 2026-10-10: Quiet bot provisioning

113 distinct unit/integration checks pass across runtime, coordination, references,
controls, delivery, reset, command gateway and quiet provisioning. The full run
passed 112 checks and exposed an existing 80 ms delivery-test race under live
swarm load; the test now advances its stored eligibility timestamp explicitly,
and its focused rerun passes. Ruff and strict mypy pass for 35 harness files.

A live throwaway bot was created and inspected before any cleanup: it is a bot,
owned by the configured human, and no creator DM exists. Repeating provisioning
reuses that bot. Unit checks verify recovery after conversion succeeds but its
response fails; preservation of existing humans; rejection of foreign bot owners;
and avoidance of the DM-generating `POST /bots` endpoint. Seed/child provisioning
and isolated reset tests pass with the quiet route. Tests account for Mattermost
rewriting the account email during bot profile updates; staged humans require the
reserved email, while staged bots require identity markers plus ownership checks.
Test accounts were removed and deletion flags restored. Two historical generated
setup reminders were deleted from owned legacy harness bots, preserving unrelated
conversation. The live supervisor was restarted with quiet provisioning.


## 2026-10-10: Seed intake and work-channel inheritance

119 selected unit/integration checks pass across runtime, coordination, references,
controls, immediate delivery, slash controls, reset, command gateway and quiet
provisioning. Ruff and strict mypy pass for 36 harness files. New integration
checks cover seed-only ordinary Town Square delivery, exact mentions/broadcasts,
edit amendments, no takeover while the seed is away, and normal work-channel
delivery. Membership/subscriptions and initial work-channel context exist before
child activation; explicit branch placement, inherited descendant placement and
explicit empty extra subscriptions are verified. Invalid foreign channels fail
before bot creation. Nested branch names preserve parent prefixes, and the owner
joins the new channel. Existing Unicode batching and immediate context ordering
checks pass with the unchanged 32 KB context cap and shortened shared prompt.

The supervisor was gracefully restarted and reconnected to Mattermost. Read-only
live checks confirm an active workspace with one idle seed and its current 1.14.0
prompt installed. No production reset or demonstration investigation was run.
Existing channel/history records are preserved; new routing applies on startup.


## 2026-10-10: Cross-problem existing-peer invitations

120 selected unit/integration checks pass; Ruff and strict mypy pass for all 36
harness files. The new peer invitation test exercises subscribe_channel through
the tool manifest between siblings, verifies real membership requests and readable
mentions, preserves other channels and existing all delivery on repeat invitations,
and rejects peer unsubscription and terminated targets before external mutation.
Runtime Unicode context bounds remain green with the updated prompt and schema.
The supervisor was gracefully restarted, reconnected, and the active workspace's
seed has the current cross-talk policy installed. No production reset or paid
demonstration task was initiated.


## 2026-10-10: Off-Topic watercooler prompt

Shared prompt revision 1.14.2 encourages optional proactive observations, jokes,
side notes and casual peer conversation in Off-Topic during downtime. Owner
pauses still apply. Twelve focused checks pass for specification/template parity,
prompt rendering and bounded Unicode notification batches. The supervisor was
restarted and reconnected; read-only verification confirms the watercooler
guidance is installed in the seed's effective prompt.


## 2026-10-10: Direct mentions and broadcast etiquette

Shared prompt revision 1.14.3 encourages contextual direct mentions for
collaboration without DMs, inviting nonmembers first. It reserves work-channel
@channel and Town Square @all for broad relevance, and @here for timely ongoing
coordination, explicitly noting that the harness has no @here presence filter.
Routine updates, jokes and rosters need no broad ping or repeated acknowledgments.
Repeated lifecycle/communication prose was shortened while preserving policy to
retain context space. Nineteen focused checks pass for template/specification
parity, prompt rendering, Unicode context bounds and work-channel collaboration.
Ruff and strict mypy pass for 36 source files. The restarted supervisor reconnected;
a read-only check confirms the live seed has the new mention etiquette.


## 2026-10-10: 24-agent cap and descendant capacity escalation

122 selected unit/integration checks pass across runtime, coordination, references,
controls, delivery, reset, gateway and provisioning; Ruff and strict mypy pass
for 36 source files. Concurrent reservation tests accept exactly 23 pending
births alongside one seed, reject the other seven, and permit replacement after
a failed reservation. A multi-branch test verifies a requester cannot retire a
cousin, the common seed ancestor can, grandchildren survive the selected
termination, and the requester can fill the freed slot despite legacy historical
limit keys. Ancestor/unrelated targets remain unauthorized. Reinitialization
applies 24 and removes obsolete lifetime limits while preserving a paused state.
Unicode batching still fits the unchanged context cap.

The supervisor was gracefully restarted and reconnected. Read-only live checks
confirm an active workspace with one live seed, persisted cap 24, and current
1.15.0 capacity escalation guidance installed. No production agents were retired
or demonstration tasks created for verification.


## 2026-10-10: Provider compaction and DeepSeek overflow retry

130 selected unit/integration checks pass, covering runtime, coordination,
references, controls, immediate delivery, slash controls, reset, command gateway,
work channels, quiet bot provisioning and durable compaction. Mock API checks
verify OpenAI inline compaction/encrypted checkpoint replay, Claude beta headers
and native summary replay, and DeepSeek overflow-only pruning/retry with durable
reset replay. PostgreSQL cases preserve append-only history and recover the
active checkpoint for subsequent requests. Parallel tool/result pairs remain
complete; unrelated errors and irreducible requests do not get pruning retries.
Unicode notifications retain full history beyond the former byte ceiling.
Ruff and strict mypy pass (38 source files).

The supervisor was gracefully restarted and reconnected with its durable inbox
reconciled. Read-only live verification confirms an active workspace, cap 24,
no saved max_context_bytes limit, and the new Working Context prompt installed
in the seed. At verification, the seed was the only live agent and was idle.
No production chat tasks or paid compaction probes were generated; actual
provider compaction at its token threshold was not exercised in the live workspace.


## 2026-10-10: Periodic idle channel catch-up

115 selected unit/integration checks pass for templates, runtime, idle catch-up,
work channels, reset, compaction, immediate delivery and slash controls. New
cases verify unmentioned work-channel and Town Square delivery, paging past
100 messages, pre-birth exclusion, disabled subscriptions/DM exclusion, durable
watermarks, cross-path live/catch-up deduplication and startup routing preservation.
Lifecycle gates cover paused channels/workspaces, away, running, failed, blocked,
terminated, disconnected and not-started agents; an empty check does not wake
the model. Reset removes this workspace's cursors and preserves other workspaces.
Ruff formatting/lint and strict mypy pass (38 source files).

The supervisor was gracefully restarted and reconnected. Read-only live checks
confirm migration 12, the 30-second poll setting, updated seed prompt, active
workspace and idle seed. No demonstration posts or standalone paid model calls
were created for verification.
The current live workspace has started=false (fresh seed awaiting its first
owner task), so its poller correctly produces no cursors/model calls yet.


## 2026-10-10: Reset preserves the open Town Square

97 selected unit/integration checks pass across reset, idle catch-up, slash
controls and controls, plus template/specification parity. Ruff formatting/lint
and strict mypy pass (38 source files). A real reset in a temporary Mattermost
team, retried after simulated interruption during external deletion, preserves
Town Square and Off-Topic IDs, permanently removes their old root/reply and
soft-deleted posts, deletes archived/private work channels and the old bot,
and starts one idle seed. All three API deletion flags are restored. A 205-post
mock case verifies pages are snapshotted before deletion, replies precede roots
and already-removed posts are tolerated.

No production workspace reset was used for testing. Browser interaction was not
exercised; the regression verifies the stable channel identities and permanent
post deletion through native Mattermost APIs.


## 2026-10-10: Fix stalled model discovery

Live supervisor evidence showed the seed FAILED with KeyError for the removed
estimated_max_request_usd field in list_models. The catalog no longer computes
a maximum request price after removing the artificial context ceiling, but the
agent-facing tool still required that field. Remove the stale projection rather
than invent a new cap/price. A real CoreTools/database regression case uses the
current catalog profile without that field, checks pagination and pricing, and
confirms the tool result is durably returned. 96 unit/runtime/coordination/model
listing checks pass; Ruff and strict mypy pass (38 source files).

Stopped the supervisor, reconciled the seed's interrupted tool call through
standard recovery, and changed only the matching FAILED seed to READY with an
audit event. The original task and history are retained; no reset, replacement
task or automatic extra workers were created. Restarted with the fix installed.

## 2026-10-10: Persistent operational diagnostics channel

118 unit/integration checks pass across diagnostics, provisioning, reset,
runtime, idle catch-up, work channels, model discovery and slash controls.
After adding transport reconnect warnings, all 72 unit checks pass again.
Ruff and strict mypy pass (39 source files). Tests cover secret redaction,
Python and unhandled async errors, failed delivery without recursive reporting,
owner membership, stable channel identity through reset, and exclusion from
automatic agent notifications and idle catch-up.

Gracefully restarted the live supervisor. Native Mattermost API checks confirm
the public errors channel is active, the owner is a member, and the real harness
startup log was delivered. No production reset or synthetic crash was used.
Reporting uses a bounded in-memory queue; unavailable Mattermost delivery falls
back to sanitized terminal output rather than providing durable log storage.

## 2026-10-10: Human-controlled persistent investigation

135 selected unit/integration checks pass across runtime, persistence, controls,
coordination/capacity, notifications, immediate delivery, work channels and reset.
New regressions use fixture models that announce a completed review or return
empty output: both resume without another message. Owner pause cancels the next
inference, repeated wake calls cannot override it, and a fresh runtime after
/continue resumes retained effort. Away buffers notifications without stopping
reasoning; a fresh unstarted seed makes no model calls. Seed retirement is
rejected in the coordination layer. Periodic channel catch-up covers running
and ready workers. The existing tool-round boundary regression now expects READY
rather than IDLE. Ruff and strict mypy pass (39 source files); template/specification
parity passes again after the final persistence wording update.

Gracefully restarted the live supervisor without resetting the workspace or
replacing history. The updated effective seed prompt is installed. The seed
resumed the original P vs NP investigation, advanced from 27 to 40 model turns,
and spawned audit and prooftest; both were RUNNING in the live check. The prior
retired collaborators remain historical terminated records. A durable runtime
continuation reminder is present, and recent real seed tools include channel
catch-up, two spawn_agent calls, public reporting and web searches. No synthetic
owner task or standalone paid evaluation was posted for verification.

## 2026-10-11: Provider prompt caching

108 selected unit/integration checks pass across provider adapters, runtime,
controls, coordination and persistent effort. A final additional Anthropic
cache-category regression brings the focused provider suite to 15 passing
checks; template/specification parity also passes. Ruff and strict mypy pass.
Tests verify automatic Claude cache control, stable OpenAI routing across turns
with separation between agent system identities, DeepSeek's provider-managed
caching, preservation of cache counters and nested details, normalized Claude
input totals, and durable usage settlement without duplicate-finish overwrites.

The live Claude cache probe was rejected before successful inference: the
provider reports the account's configured API usage limit has been reached,
with access returning November 1 at 00:00 UTC. No real cache hit was confirmed.
This is an account limit, not a cache-parameter rejection. Gracefully restarted
the supervisor with caching enabled and verified migration 13/provider_usage
storage. All existing agents remain FAILED or TERMINATED (23 and 13 records);
no failed work was resumed. Existing cost summaries remain conservative estimates
and historical cache usage is not reconstructed.


## 2026-10-11: Retrieval and posting loop fixes

120 selected unit/integration checks passed, followed by 37 focused checks after
owner-control coverage. The final model-attribution change passed 27 focused
unit/live-integration checks. Ruff and strict mypy pass. Tests cover continuous
new channel traffic through both posting attempts, owner pause before send,
timeout recovery without worker failure, provider-wide quota cooldowns, failed
URL isolation, safe error details, and actual web-search model settlement.

One live OpenAI search succeeded; retrieval is not universally unavailable.
Historical logs show repeated search timeouts and unsupported/missing/blocked
fetches, but generic historical ProviderError entries discarded their underlying
provider causes. Those causes cannot be reconstructed from the retained logs.

Gracefully restarted the supervisor without a Docker rebuild or workspace reset.
Verified migration 14, the 90-second tool deadline, updated effective seed prompt,
and canonical specification/alias parity. All current agents remain FAILED or
TERMINATED (23 and 11 records); failed effort was not resumed. No real swarm
inference was used to exercise the new retry behavior; deterministic regressions
cover those paths. Cooldowns and posting deferral markers reset on restart.


## 2026-10-11: Companion analytics dashboard

108 unit/live-integration checks pass, including five analytics regressions.
Verified token sums without join multiplication, deduplicated successful chat
receipts, exclusion of deferred/error/private results, unknown/in-flight call
counts, retired usage retention, global and channel-scoped controls, pending
reset status, and reset to a fresh idle seed with zero counters. HTTP tests
verify workspace scoping, no credential/prompt exposure, no mutation routes,
and rejection of cross-origin and foreign Host requests. Membership tests cover
native Off-Topic membership, caching, reset invalidation and outage staleness.
Ruff and strict mypy pass (40 source files).

Gracefully restarted the existing supervisor; index and stats return HTTP 200.
Live native memberships are available and include seed Off-Topic, P-vs-NP and
Town Square. Verified 23 failed agents and 11 retired historical records, with
zero working/in-flight calls. Existing failed work was not resumed. Read-only
browser verification confirms populated totals, agent states/channel links and
functional filtering. The browser tab is retained as the local deliverable;
preview saved in .state/analytics-preview.jpg. Canonical specification and typo
alias remain byte-identical. No real workspace reset was performed for testing.


## 2026-10-11: Simplified model selection

11 provider/catalog integration checks pass; Ruff and strict mypy pass. Verified
lite/c1 DeepSeek Flash grouping and absence of pricing_basis/agent_candidate
from public list_models entries. Gracefully restarted the supervisor; analytics
responds successfully with zero working agents. Existing failed effort remains
stopped. Numeric pricing and internal candidate eligibility are unchanged.


## 2026-10-11: Visible execution blockage and timeout recovery

114 selected unit/live-integration checks pass; Ruff and strict mypy pass.
Regressions cover first-timeout recovery, exactly two attempts on persistent
timeout with separate unknown ledger entries, no wake override of blockage,
owner pause cancellation during backoff, billing rejection without retry,
durable safe failure reasons, unexpected harness error reporting, and explicit
owner /continue clearing a diagnosed blockage. Existing reset/analytics/control
checks also pass. Migration 15 installed and supervisor restarted gracefully.
Live workspace is now a fresh reset with one IDLE waiting seed and no working
agents; no objective or paid model inference was started to test deployment.
Legacy errors are only labeled unknown when records exist; no cause is invented.


## 2026-10-11: Search accounting rejection and model spelling

25 selected provider/retrieval/model-listing checks pass; Ruff and strict mypy
pass. A regression returns two retrieval calls and usage above the reservation:
evidence is returned and usage settles exactly once with both call fees included.
Missing usage remains an uncertain failure. Known provider/model spelling resolves
to the exact discovered provider:model ID; nonexistent models remain rejected.
The observed failed spawn requested openai/gpt-6-luna. The old search error
combined overrun and multi-call rejection, and retained no response details to
distinguish which check failed historically. Gracefully restarted the existing
supervisor; analytics responds HTTP 200. No standalone paid search probe was
run and no owner controls were changed for deployment.

## Provider billing dashboard (2026-10-11)

Removed local estimated USD prices from the dashboard and agent rows. Added
read-only provider billing adapters and migration 16 for immutable reset
baselines. OpenAI dollars and Anthropic decimal cents are normalized to USD;
DeepSeek balance changes remain separate. Provider reads paginate, cache for
60 seconds, and fail explicitly without substituting local estimates.

Checks: 21 selected tests passed across billing, analytics, reset and bootstrap;
Ruff passed and strict mypy passed for 42 source files. Mocked billing tests
verify pagination, unit conversion, restart-persistent baselines, fresh reset
origins, cache reuse, excluding balances from cost totals, and denied access
without leaking provider error bodies or writing zero baselines.

Restarted the local supervisor and verified GET /api/stats and the browser.
OpenAI billing returned 403 and Anthropic returned 401 with the existing keys;
both are displayed unavailable. DeepSeek balance succeeded and established the
current run's first baseline. The total cost card correctly displays unavailable
rather than zero or an estimate. No admin billing keys are configured yet. No
owner reset was invoked for verification, and existing conversations remain.

Provider reports are account-wide and delayed daily aggregates; baseline deltas
must not be treated as exact spending attributable to a reset. Admin credentials
added later establish their origin at first successful access. Screenshot:
.state/analytics-preview.jpg.

## Recursive delegation prompt 1.22.0 (2026-10-11)

Made independent branch discovery, peer recruitment/child spawning, and sustained
subtopic-channel discussion an explicit responsibility of every agent, including
children with narrow roles. Canonical architecture and requested spelling copy
match the template. Ten bootstrap/specification tests passed; Ruff and strict
mypy passed. Restarted the supervisor with the owner-authorized revision and
verified every current non-terminated agent uses revision 1.22.0, through an
append-only effective version or its new birth record. No reset or force-resume
was issued; blocked/owner-paused workers remain subject to their existing gates.

## circuits invalid_prompt investigation (2026-10-11)

The saved block was OpenAI HTTP 400 invalid_prompt, not a timeout. Its original
provider explanation was not retained. Retried the exact saved input once with
a 16-output-token diagnostic limit; OpenAI accepted it (103,536 input tokens,
16 output tokens). Recorded usage and discarded diagnostic output without tool
execution/chat posting. Cleared only circuits' diagnosed block and restarted
the supervisor; no global /continue or reset was invoked.

Added one bounded retry for invalid_prompt without an explicit policy rejection;
explicit flagged-policy responses block directly. Provider error classification
remains sanitized. 29 OpenAI/runtime tests passed, including first-attempt
recovery, repeated invalid_prompt blocking, policy/billing rejection without
retry, and cancellation during backoff. Ruff and strict mypy passed.


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

Three billing integration tests passed, including a current-day regression;
Ruff and strict mypy passed. Anthropic live billing status is ok.

## Slash-command Docker connection recovery (2026-10-11)

Mattermost logs confirmed failed owner /stop and /reset callbacks timed out on
host.docker.internal:8766 before reaching the harness. The callback listener was
healthy on the host; Docker's CLI/API and normal Desktop restart were hung.
Paused the swarm through its authenticated local callback. A temporary alternate
IP callback test was rejected by Mattermost's environment-controlled internal
allowlist; original command/config values were restored.

Forced Docker Desktop to stop after its normal restart timed out, started it,
brought the existing compose stack back up without deleting volumes, and
restarted the supervisor. Mattermost /stop then returned HTTP 200, recorded a
native control receipt, and the dashboard showed paused. Ten slash-control/reset
integration tests passed. /reset shares the restored callback route; no live
reset was invoked, and conversations/channels/agents were preserved. The swarm
remains paused pending the owner's next command.


## 2026-10-13: OpenCode/Gitea worker trial

- Ruff and strict mypy pass; 89 unit tests pass.
- Six PostgreSQL coding tests pass: usage/idempotency, per-agent concurrency,
  exact-model rejection, owner pause, cancellation and failed-abort reconciliation.
- Reset/control regressions pass (11 tests), including purge with coding tables.
- Real OpenCode 1.19.0 + Gitea 28.1.0 gate passes: separate checkouts/sessions,
  native no-inference message storage, commit/push/fetch, and scoped process cleanup
  that kills one checkout's command while preserving another checkout's command.
- Paid isolated gpt-6-luna smoke passes: actual file editing, Python unittest
  execution, commit and branch push. 738 output tokens; OpenCode estimate $0.001398880.
- Real coding cancellation gate passes: cancel during a sleeping shell command,
  verify the command is gone and the operation is durably interrupted.
- Mattermost and analytics return HTTP 200; the current fresh seed is unstarted.
- Agent37 cloud provisioning was initially unverified without a key; the funded
  cloud trial below subsequently verified it.

The initial Docker build exhausted host disk space and made Docker storage
read-only. Cleared disposable npm download cache (approximately 8 GB), restarted
Docker and restored existing services without deleting source or volumes. A tight
two-second existing runtime test timed out during build/recovery; its isolated
rerun passed after services stabilized. A cancellation probe initially matched
/proc/self; corrected it to inspect numeric peer process IDs only.

## 2026-10-13: Funded Agent37 cloud trial

Provisioned one Agent37 VM (2 vCPU / 4 GB / 12 GB), running the same Gitea and
OpenCode Compose services. Cloud OpenCode reports version 1.19.0. The real worker
gate passed in 45.27 seconds, including separate checkouts/sessions, push/fetch,
and cancellation isolated to one checkout. The paid coding smoke passed in
34.25 seconds: file editing, two Python unit tests, commit and push; 719 output
tokens and native estimated cost $0.001434595. The test used an isolated database
schema and did not start a production swarm objective.

Restarted the local supervisor against the cloud backend; its seed connected and
reconciled its durable inbox. There were no existing production coding assignments
to migrate. The local Gitea installation remains a separate repository. Cloud
Gitea is https://gitea-uban0ob9g9.agent37.app/swarm/workspace; credentials remain in
private .state/agent37-coding.json. The always-awake VM now incurs infrastructure
charges, independently of inference; Mattermost /stop does not stop the VM.

The owner subsequently requested local experimentation. Explicitly stopped the
Agent37 VM (API acknowledged status stopped), preserving its disk and Git data;
disk-only billing remains. Switched back to local Docker and verified the real
worker gate again (9.44 seconds). Fixed local setup to select Docker transport
even when the previous configuration selected Agent37. Ruff and mypy pass for
that change. No production coding assignments existed to migrate.

## 2026-10-13: Unified native agent loop

- 91 unit tests pass; Ruff and strict mypy pass (49 source files).
- Five new PostgreSQL native tests pass: one-time history import/session reuse,
  busy-session notification injection without a second loop, bridge authentication
  and identity mapping, duplicate tool receipts and owner pause/resume with native
  abort and the same session. Bridge cancellation is also covered.
- Six prior coding gates and 11 owner control/reset regression tests pass.
  Provider-mocked supervisor tests explicitly select the provider engine instead
  of inheriting the operator's new OpenCode selection from .env.
- A real paid OpenCode/gpt-6-luna gate passed in 28.86 seconds. One session used
  get_status, edited and executed native_sum.py with two assertions, committed and
  pushed its branch, then called post_channel through the authenticated bridge.
  Mattermost transport was captured; no test messages reached the real team.
  817 output tokens; native estimated cost $0.002794545. Other providers were not
  separately live-tested in this gate; exact-model checks remain enforced.
- Deployed locally with HARNESS_AGENT_ENGINE=opencode and owner-pinned prompt
  revision 1.24.1. Supervisor connected; seed remains idle/unstarted pending a human
  objective. Mattermost, analytics and Gitea return HTTP 200. The native tool bridge
  rejects unauthenticated requests with HTTP 401. Agent37 reports the VM stopped.

The provider adapter engine remains an explicit rollback option. Native working
history and compaction now belong to OpenCode; the controller retains policy,
delivery receipts, native usage, source-labelled imported context and owner controls.

## 2026-10-13: Heads-down workflow and Gitea API

Added a dedicated shared prompt section for communication versus hands-on work,
versioned research artifacts, issue-based delegation, main/branch synchronization,
tests, PR review and authorized main integration. Pinned revision 1.25.0 for
existing agents, preserving birth records and authored roles.

99 unit tests pass; Ruff and strict mypy pass (50 source files). API invariants
cover repository path traversal, external URLs, credential hiding and redirects.
16 native/control/reset integration tests pass. A live Gitea API gate passed in
1.49 seconds in a disposable private repository: issue creation, branch/content
creation, PR, comment, merge, main content verification and issue closure. The
first attempt exposed asynchronous mergeability (HTTP 405); the gate waits for
the read-only PR state, and prompt guidance now explains this. No forced merge
or bypass was added. No test commits changed the shared workspace repository;
the disposable repository was deleted, and no inference was needed for this gate.

Provisioning upgraded the service token with write:issue and regenerated native
tools. Existing work was quiesced during deployment; source, sessions and agent
assignments were retained and the local controller restarted.

## 2026-10-13: Activity surfaces, Git metrics and peer review

102 unit tests, 21 PostgreSQL analytics/native/control/reset checks, Ruff and strict
mypy (51 source files) pass. Coverage includes current tool surfaces, real versus
assigned Git HEAD, confirmed issue creation deduplication, PR creator/branch
attribution, pagination, closed-unmerged PRs, stale-data handling, reset cache
invalidation and preventing broken links to unpublished branches/unknown commits.
Peer-review assignment tests verify PR comments, contextual mentions, membership,
message accounting and rejection of self-review. Transport is captured in tests;
no test review messages were sent to the real team.

Deployed migration 19 and owner-pinned prompt revision 1.26.0 locally. Generated
native gitea_request_review is installed. Analytics/API return HTTP 200 and Gitea
metadata is available. Inspected the live rendered dashboard and saved a private
preview under .state/analytics-preview.png. The current post-reset seed is idle,
so its branch/activity populate only after the next human objective. No workspace
reset or new human task was issued during this deployment.


## 2026-10-13: Remove native concurrency caps

Seven targeted unit tests and eleven PostgreSQL native/coding integration tests
passed. A regression holds all 24 native activations open simultaneously with
the legacy inference semaphore exhausted, verifies all reach their work loop,
and verifies session advisory-lock cleanup. Ruff and strict mypy pass. Restarted
the local controller without rebuilding the worker; existing sessions/branches
remain intact. Usage settlement remains unchanged in this change.


## 2026-10-13: Full service reset

109 unit tests, eleven Mattermost reset/native PostgreSQL checks, and two real
disposable service checks passed. Reset regressions cover service checkpoint
resume, shared-worker/configuration guards, replacement-repository identity,
lost Gitea create responses, worker stop/wipe/start ordering, and wipe failure
leaving the worker stopped. The live Gitea gate deletes/recreates a disposable
repository and verifies old issues, PRs, source and branches are absent; replay
keeps the fresh repo identity. The OpenCode gate stops/wipes/restarts a disposable
worker and verifies private message/session history, files and snapshots are
gone and a fresh session works. No inference calls or actual Swarm reset were
performed. Ruff and strict mypy pass. Disposable worker ports are fixed across
restart; Docker-assigned ephemeral ports can change after stop/start.


## 2026-10-13: WebSocket analytics and public message streams

110 unit tests pass, including origin rejection, shared WebSocket updates and
reconnect snapshots. Eighteen PostgreSQL native/analytics/reset/stream checks pass.
Streaming tests verify growing text updates one post, previous/private reasoning
never publishes, finalization counts one message, paused streams preserve only
already-shared text, peer inboxes ignore drafts, and focused presence restores
available with explicit set_status overrides supported. A real isolated
Mattermost test verifies post creation/patch/final props and that posting leaves
away presence intact; all test channels and bots are removed afterwards. No model
inference is used for these gates. The live analytics WebSocket serves snapshots,
and the generated begin_channel_post tool is installed in the existing worker.


## 2026-10-13: Agent activity inspector

110 unit tests and nineteen targeted PostgreSQL activity/native/analytics/reset
checks passed. New regressions cover immutable milestones, live reasoning updates
without duplicate growing-history writes, configured-secret redaction, concurrent
capture, restart deduplication, stale backfill rejection, stable cursor pages
while new events arrive, workspace/origin scope, interrupted work and reset
purge/trigger restoration. Dashboard JavaScript syntax, Ruff and strict mypy pass.
The live endpoint serves the actual seed’s empty current history and migration21
is applied. Browser visual inspection was blocked by the computer-use tool’s URL
policy, so no browser screenshot verification is claimed. Activity capture does
not make model calls or publish private traces to Mattermost.

The additional legacy runtime/activity gate passes 27 checks. Activity capture is
bounded to three seconds per best-effort operation; durable source replay can
backfill missing observations without delaying inference or owner controls
indefinitely.


## 2026-10-13: Streaming typing event order

Typing refresh now follows the POST/PUT chunk update so a post arrival does not
clear the just-sent indicator. The streaming regression verifies exact ordering
and no refresh after finalization. Native prompts/tool descriptions default to
streaming even for short newly composed responses; buffered posting is retained
for prewritten verbatim content.

The isolated live Mattermost check also verifies a real authenticated typing
WebSocket event reaches the owner observer after a streamed chunk, while the
bot remains away. Native instructions deployed as revision 1.28.0.


## 2026-10-13: Readable reasoning summaries

Observed OpenAI reasoning parts had empty text and reasoningEncryptedContent
metadata. The native Swarm config now opts in to reasoningSummary:auto; display
tests distinguish readable summaries, pending summaries and unavailable text.
Activity capture/API redacts opaque reasoning ciphertext without dropping
readable summary text. No extra inference calls are made for historical repair.

2026-10-13 PDF retrieval repair: 20 focused web-retrieval/coding unit tests passed, including byte-preserving PDF extraction, malformed PDF rejection, disabling native webfetch, and sanitized provider error classification. Ruff and targeted mypy passed. Repaired one corrupted PDF attachment in cycles' existing native session, removed its corrupted cache file, recreated the local worker without rebuilding its image, verified the effective webfetch deny rule, and resumed only cycles.

2026-10-13 native Gitea identity rollout: real disposable-repository test passed for distinct account/token provisioning, idempotent credential reuse, per-agent issue/PR authorship, native requested-reviewer state, reviewer approval, and reviewer-attributed merge. Test repository and test accounts were deleted afterward. 31 PostgreSQL coordination/activity/native/coding tests passed; unit suite, Ruff and mypy passed. Five live agents were provisioned without resetting work, each checkout's Git identity updated, and each authenticated against origin. No outstanding comment-based requests remained on open PRs to convert. Historical authorship was preserved.

2026-10-13 timeout recovery: five regression tests passed for safe-read retry/sustained outage, no mutation replay, confirmed-cleanup requeue, repeated timeout visibility and uncertain-cleanup blocking. Eleven PostgreSQL native/coding lifecycle tests passed. Controller shutdown was confirmed before restart; no worker rebuild or workspace reset.

2026-10-13 matching usernames: disposable Gitea workflow test verifies rename preserving account ID/token, exact Mattermost-name provisioning, native reviews/approval and reviewer merge. Eleven native/coding lifecycle tests passed. Live agent accounts renamed and Git authentication checked in their preserved checkouts. Prompt revised to 1.31.0 with matching-username guidance.

2026-10-13 reasoning display follow-up: verified effective Swarm reasoningSummary=auto and recent native responses with both readable text and empty encrypted-only reasoning items. Hide only completed empty OpenAI reasoning items from dashboard cards; retain the append-only record and in-progress indicators. Correct fallback wording to describe a reasoning item, not an entire model step. Two formatter/filter regression tests passed; live analytics HTML includes the fix and its JavaScript parses. No agent restart or extra inference calls required.

2026-10-13 dashboard navigation: five link/view/socket unit tests and five analytics/workflow database tests passed. Updated the older mocked review test to assert native requested-reviewer assignment and actor-token routing without contacting production Gitea. Live analytics serves all three navigation links and valid JavaScript. Each of five OpenCode deep links returned the UI and resolved to the intended persisted session through the directory-scoped API. The default /session listing was empty while tracked agent sessions existed in their checkout directories. Visual browser inspection was unavailable; verification used served HTML, JavaScript syntax and live APIs.

2026-10-13 seed coordinator: 123 unit tests and 21 coordination/analytics PostgreSQL tests passed before final hardening; three coordinator regression tests additionally verified no coding/native-stream tools under global OpenCode settings, available presence/away rejection, no inference while idle, message wakeup, idle return, forbidden Gitea source mutation, and supervisor seed/worker engine routing. Ruff/mypy passed. Live seed confirmed idle/available with no failure and zero active native runs; seed OpenCode link is omitted, worker sessions continue, and the seed's previous clean checkout/branch and session are preserved.

2026-10-13 wiki tools: disposable Gitea identity workflow passed with wiki create/read/update and stale-revision rejection, plus native review and merge. 123 unit tests passed. Ruff/mypy passed after final concurrency adjustment (resolve agent credentials before acquiring the repository wiki lock to avoid nested-pool starvation). Live wiki enabled, Home index created without research claims, existing pages preserved, native tool definitions regenerated, and worker recreated without an image rebuild. Provider seed gets the same structured wiki tools through its chat runtime.

2026-10-14 typing follow-up: configuration enabled and server throttle 5 seconds. Recent seed traffic used buffered post_channel, while workers used begin_channel_post; first-snapshot completed text previously sent no typing. Two provider-stream tests verify only public post arguments trigger hints and final usage is preserved, with missing-final transport failures not replayed by the parser. Nine stream/coordinator database tests passed, including a live isolated Mattermost observer receiving typing events from an away bot and a public composer signaling before any text/post exists. All 125 unit tests and mypy passed. Controller restarted cleanly; native sessions/workspaces retained.
