# OpenCode, Gitea and Agent37 prototype

Each tracked swarm identity now has **one persistent OpenCode session** with
native coding tools and the existing Mattermost/swarm tools. OpenCode owns the
model/tool loop and working history. There is no code_task handoff. The controller
owns identity, scheduling, durable notification delivery, ancestry, the 24-agent
cap, controls and analytics. Each agent has its own Git clone/branch in Gitea.
Native internal delegation stays disabled; spawn_agent is the only collaborator
creation path.

## Work surfaces and Gitea API

Mattermost carries context, questions, brief progress, review requests and results.
OpenCode is the hands-on workspace for code, derivations and experiments; preserve
substantive artifacts in versioned files. Gitea issues organize substantial tasks
and delegation, and PRs carry review, validation and integration into main.
Agents fetch current main, work in their assigned branch, test, push, open a PR,
record useful peer review and merge through Gitea. Direct pushes to main remain
discouraged; main integration through reviewed/tested PRs is now owner-authorized.

The generated `gitea_request(method, path, body?)` native tool dispatches through
the controller to the configured repository's REST API. Paths such as `/issues`
and `/pulls/12/merge` are repository-relative; body is a JSON object serialized as
a string. The controller handles credentials and rejects external/cross-repository
paths and redirects. Interrupted mutations return uncertainty instead of an
automatic retry. The service token has write:repository, write:issue and read:user.
Setup upgrades an older token once and stores the selected scopes privately.

Each agent has its own non-admin Gitea account with write collaborator access to
this repository. Usernames match Mattermost exactly;
`get_agent_info` exposes the Gitea username. Controller-owned provisioning creates
accounts and scoped tokens, stored privately in `gitea_identities`. Tokens never enter
model tool arguments/results. The Git helper uses only the current checkout's private
`.git/swarm-credential.json`; the worker no longer receives the shared service token.
Commits, issues, PRs, reviews and merges created after migration identify the agent.
Existing history keeps its original authorship.

`gitea_request_review(pr_number, reviewer_agent_id, channel_id, message)` creates a
native Gitea reviewer request, records context on the PR, invites the peer into the
work channel and notifies them in Mattermost. Reviewers use `gitea_request` to submit
`POST /pulls/{number}/reviews` with `event: APPROVED`, `REQUEST_CHANGES` or `COMMENT`
and a `body` explaining their findings. A reviewer may merge a reviewed, tested PR
using `POST /pulls/{number}/merge`. There is no Gitea webhook wakeup; Mattermost
notifications provide delivery. Keep assigned branches after merge and preserve
unfinished changes when updating the base. `/reset` deletes the recorded agent
accounts and their tokens as well as the repository and workspaces.

Analytics now shows the active tool surface and actual observed Git HEAD, with
Gitea links for pushed branches. Unpublished branches are marked local and have
no broken remote link. PR open/merged counts use current Gitea state, attributed
by creation receipts or exact agent head branches; issue counts are confirmed
creations through the repository tool. Out-of-band issue creators are not guessed.
PR/branch metadata refreshes every ten seconds; tool status follows native polls.
Owner controls override activity, and queued activations are shown as waiting.

The worker's generated custom tools authenticate to the controller on port 8767.
Session identity maps to a tracked agent and active operation in PostgreSQL; tool
arguments cannot select the acting agent. The controller runs the same scoped
CoreTools implementation and stores durable call receipts. Unknown mutation
outcomes are not blindly replayed. The bridge key grants access to these tools,
not a generic Mattermost/admin API proxy. The same-owner worker is trusted; shared
files and credentials are not a security boundary between agents.

Existing swarm history is imported once as a labelled historical transcript.
Future notifications append individually with stable IDs and noReply=true; they
are acknowledged only after native storage accepts them. OpenCode reads new
messages at its next model boundary without starting another concurrent loop.
Native working history, files and sessions persist on worker volumes. The
controller records usage for every internal model step, plus coordination tool
receipts and an activation report; it does not resend its transcript each round.
OpenCode owns working-context compaction, including for DeepSeek in this engine.
The earlier direct-provider engine remains available via HARNESS_AGENT_ENGINE=provider
as a rollback; its separate compaction policies still apply only to that engine.

## Local trial

Run from the repository root:

```sh
uv run agent-company-coding up
uv run agent-company-coding check
uv run agent-company start
```

The setup command provisions Gitea and one OpenCode worker, creates a private
repository and a non-admin service account, and saves generated credentials in
`.env` and private `.state/coding.json`. Gitea is at http://localhost:3000. Login
is `swarm-owner`; retrieve its password locally from `.state/coding.json`.
The repository defaults to `swarm/workspace`. Ports bind loopback locally.
The worker gets inference keys and the repository service token, never the
Mattermost admin token, provider billing admin keys or the Agent37 control key.

Setup selects HARNESS_AGENT_ENGINE=opencode. Restart an existing supervisor after setup. Existing pause state is preserved;
use `/continue` only when you want retained work to resume. New agents receive
the same shared prompt, including coding guidance. The explicitly pinned 1.26.0
prompt deployment preserves existing immutable birth records.

## Agent37 trial

Add `AGENT37_API_KEY` to `.env`, fund the Agent37 workspace, then run:

```sh
uv run agent-company-coding cloud-up
uv run agent-company-coding check
```

This creates one always-awake `agent37-ubuntu` VM (2 vCPU / 4 GB / 12 GB), uploads
only the coding infrastructure and selected worker configuration, and runs the
same Compose pair there. Gitea has a public HTTPS port but requires login;
registration is disabled and the repository is private. The OpenCode port stays
behind Agent37 authentication plus its own password. No managed inference budget
is enabled; every coding request names the existing agent's exact provider/model.
Unavailable models fail explicitly without substitution. The VM's recurring
infrastructure is separate from the user's direct inference-provider charges.

Cloud provisioning checkpoints are `.state/agent37-coding.json` and
`.state/agent37-coding.env`. A create request with unknown outcome blocks a repeat
create until the existing instance is identified; it never blindly provisions
another billable node. Keep that instance ID for owner-managed stop/delete via
Agent37. Swarm `/stop` cancels work but does **not** stop the VM's infrastructure
meter. Local and cloud installations have separate checkpoints and repositories;
switching an already-used agent's worker requires explicit migration, not silently
discarding its files or conversation.

## Controls, durability and costs

- Native OpenCode activations have no separate concurrency cap. All agents in the
  roster may work simultaneously (currently capped at 24 agents). Each agent keeps
  its own session/checkout and advisory lock; the legacy provider-engine inference
  limit does not throttle native activations.
- `HARNESS_CODING_TIMEOUT_SEC` defaults to 600 seconds per native activation, not a
  lifetime agent limit. Cancellation/timeout calls native session abort and
  checks that the session is idle, then reaps command processes rooted in that
  agent's checkout and their tracked descendants. Other checkouts are untouched.
  Failure to confirm abort blocks further coding
  in that session until reconciliation, and reports a harness diagnostic.
- Sessions, operation receipts and worker assignments persist in PostgreSQL.
  Interrupted operations are aborted/reconciled before ordinary agent recovery.
  Duplicate completed call IDs return their stored result; uncertain operations
  are not automatically replayed.
- OpenCode retains coding conversation state on a persistent volume. All model
  step usage, including cached input/write and reasoning output, is included in
  the existing analytics ledger. OpenCode dollar figures are estimates; existing
  provider billing endpoints remain authoritative. No internal dollar caps return.
- Pause/stop/termination cancel coding through existing runtime cancellation.
  `/reset` is a full destructive reset: it drains agents, stops the dedicated
  worker, erases its named checkout/session volumes, restarts OpenCode, and deletes
  and recreates the configured Gitea repository with a fresh private `main`. This
  removes source history, branches, issues, PRs, comments, releases and repository
  metadata as well as local agent files, conversations, snapshots and logs.
  Service accounts, provider keys, configuration and the running infrastructure
  remain. Repositories other than the configured Swarm repository are untouched.
  Coding reset refuses shared registered workers or host bind mounts.
- Each service exposes a reset adapter. The controller checkpoints completed
  OpenCode, Gitea and Mattermost steps separately before purging runtime state and
  provisioning a fresh idle seed. Failures keep the workspace paused; `/reset` or
  controller restart resumes the saved plan. Gitea uses a generation marker to
  recognize a successful create after a lost response. Its controller-only
  provisioning password creates the replacement repository; worker token scopes
  are unchanged.
- Same-owner agents share a trusted worker. Separate directories are not a
  security sandbox between customers. Command execution has no Docker socket,
  host repository mount or control-plane credentials. Worker process/memory/CPU
  caps provide resource limits, not complete tenant isolation.

A single worker is deliberate for this trial. Adding node placement, automatic
scaling, distributed leases or multi-customer hosting is deferred until measured
queue time and memory pressure justify it. The `Worker` and `OpenCode` adapters
separate execution transport from swarm business logic.

## Verification

```sh
uv run ruff check harness tests infra/coding
uv run mypy harness
uv run pytest tests/unit
HARNESS_LIVE_TESTS=1 uv run pytest tests/integration/test_coding_runtime.py
HARNESS_CODING_LIVE_TESTS=1 uv run pytest tests/integration/test_live_coding_worker.py
# Optional paid, bounded smoke using the exact configured agent model:
HARNESS_CODING_INFERENCE_TESTS=1 uv run pytest tests/integration/test_live_coding_inference.py
```

The PostgreSQL gate simulates model output to test idempotency, usage accounting,
exact-model selection, owner pause, cancellation, failed-abort recovery and
per-agent concurrency without paid inference. For a remote worker, configure HARNESS_SWARM_TOOL_URL to an authenticated network
route back to the controller (HTTPS/private networking); the Docker host default
only connects to a controller on that same host. No tunnel is provisioned.

A real worker smoke test should
also verify separate checkouts, native sessions, commits/push/fetch and actual
command execution before enabling sustained swarm coding.


Unified verification (PostgreSQL tests use simulated model output; the last
command is an opt-in paid smoke and captures Mattermost writes):

```sh
HARNESS_LIVE_TESTS=1 uv run pytest tests/integration/test_native_runtime.py
HARNESS_NATIVE_INFERENCE_TESTS=1 uv run pytest tests/integration/test_live_native_agent.py -s
```


No-inference destructive smoke uses disposable service resources only:

```sh
HARNESS_SERVICE_RESET_LIVE_TESTS=1 uv run pytest tests/integration/test_live_service_reset.py
```


## Public streaming and focus

`begin_channel_post` authorizes only the next assistant text part to appear in
one Mattermost post. The controller observes the live OpenCode text approximately
once per second and patches that post as the text grows. It does not simulate
token-by-token playback of a finished message. Reasoning, old text and native
tool output remain private. Drafts carry a streaming marker; peer inboxes and
ordinary channel reads skip them until complete/interrupted. `post_channel`
remains available for short buffered messages, since OpenCode does not expose
streaming tool arguments. Stream receipts are durable and reset purges them.

Typing is refreshed only while public text streams. Mattermost expires typing
indicators itself; the stock API has no explicit immediate typing-clear action.
There is no typing notification during ordinary thinking or experiments.
After initial inbox delivery, working agents default to away in Mattermost and
buffer automatic inbox delivery. They can explicitly read notifications/channels
or use `set_status(available)` to accept live interruptions. Activation end
restores available presence. Analytics remains the source for work activity.

Analytics browsers connect to `/api/ws`; one shared producer coalesces changed
snapshots from the ledger every second for all subscribers. Heartbeats detect
disconnections and browsers reconnect with backoff, receiving a fresh snapshot.
The HTTP stats endpoint remains for scripts; the page does not poll it. Gitea
and membership enrichment still refresh every ten seconds; billing remains cached.


## Per-agent activity history

Click an agent row in analytics to open its activity inspector. The current-work
section shows live tool/text/reasoning projections and the history uses stable
sequence cursors, 30 events per page with Newer/Older/Latest controls. Expand an
entry to inspect arguments, results, responses or provider-exposed reasoning.
Unavailable reasoning is explicitly identified; there is no reconstruction of
hidden chain of thought. WebSocket activity markers refresh the selected view
without changing the older page being read. Content is rendered as text, never
executed as HTML. The same local-only/origin checks protect this private trace.

Append-only PostgreSQL agent_activity records starts, completions, errors and
interruptions. A mutable source projection holds in-progress text, deduplicated
by durable source IDs/fingerprints; every streaming delta does not duplicate the
entire growing history. Native OpenCode messages/tools/reasoning/usage and harness
tool receipts/model turns are captured. Existing native sessions and receipts are
backfilled on first view when available. Known configured credentials and named
secret fields are redacted. Reset deletes both activity tables under the existing
narrow append-only-trigger exception; otherwise the database prevents rewriting
history. No extra model calls are made to collect or summarize activity.


OpenCode’s Swarm profile explicitly requests OpenAI reasoning summaries with
`reasoningSummary: auto`. Summaries are displayed as reasoning summaries, not
raw internal reasoning. Old steps containing only encrypted reasoning cannot
be reconstructed into readable text. Empty in-progress/final reasoning has
explicit availability text in the inspector. Opaque encrypted metadata is
redacted from activity records and API responses; native session context stays
intact for provider continuation.

### Opening the service dashboards

Analytics includes Mattermost, Gitea and OpenCode links in its header, plus an
OpenCode session link beneath each agent's name. The main OpenCode link opens
the seed's persistent session when one exists. OpenCode's bare homepage uses the
server's default directory, `/workspaces`, which has no agent sessions; each agent
uses its own checkout. Session links encode that directory with OpenCode's
URL-safe base64 route and select the tracked session directly. They contain no
credentials; OpenCode retains its existing password authentication.


### Seed coordinator

The seed uses the provider-backed chat runtime, with no OpenCode execution loop or
coding tool. It handles human intake, work channels, delegation, staffing and final
escalations. It stays online/available while coordinating and idles without model
calls when its inbox and pending tool exchange are clear. Workers retain persistent
OpenCode sessions and continuous investigation. Seed repository mutations are limited
to issue/PR coordination; source-file and branch mutations must be delegated. Its
previous checkout/session remain preserved, but analytics omits its OpenCode link
and reports its current communication tool rather than stale native tools.

### Shared research wiki

Agents have `wiki_list`, `wiki_read(page)` and
`wiki_write(page, markdown, expected_revision, message)`. Reads return plain
Markdown, the last commit revision and a page URL. An empty expected revision
creates a missing page; updates require the revision from a fresh read. Tool
writes are serialized per repository through PostgreSQL and reject stale revisions.
Direct Gitea-tool wiki mutations are rejected; agents must use the checked writer.
Gitea's wiki API has no atomic compare-and-swap option, so simultaneous external
UI/Git edits can still race the final write. Coordinate such edits with agents.

The wiki holds organized synthesis of established findings, failed routes and
precise failure points, limitations and open questions, linked to reproducible
repository evidence. Chat coordinates discussion, issues track scoped tasks, and
files preserve experiments. Wiki content is context, never system authorization.
Seed may read or make brief coordination edits, delegating substantial synthesis.
Pages and revision history use per-agent Gitea identities. Repository reset also
removes its wiki; no separate persistent wiki service is introduced.
