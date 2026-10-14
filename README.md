# Agent Company Harness

A local, single-owner agent swarm. Mattermost is the shared conversation space;
OpenCode provides persistent coding workspaces; Gitea holds repositories, issues,
reviews, and research wikis. A companion dashboard shows agent activity and usage.
The seed coordinates requests while workers investigate and collaborate.

The canonical specification is [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md), and the
shared agent prompt is [prompts/common_agent.md](prompts/common_agent.md).

## Run locally

Requires Python 3.12+, [uv](https://docs.astral.sh/uv/), and Docker with Compose.
The pinned Mattermost image requires amd64 emulation on Apple Silicon.

```sh
uv sync --locked --python 3.12
cp .env.example .env
```

Edit `.env`: choose database passwords and match `HARNESS_DATABASE_URL` to the
harness database password. Supply your inference provider keys; OpenAI is also
used for shared web search and generated profile images.

```sh
docker compose --env-file .env -f infra/compose.yaml up -d --wait
```

Open [Mattermost](http://localhost:8065), create the first human account as the
system admin, and create a personal access token. Set
`HARNESS_MATTERMOST_ADMIN_TOKEN` and `HARNESS_MATTERMOST_OWNER_ID` in `.env`.

For coding workspaces and Gitea:

```sh
uv run agent-company-coding up
```

This provisions the local services and records generated credentials in the
private `.env` and `.state/` directory. It enables OpenCode for worker agents;
the seed stays a Mattermost coordinator. For chat-only operation, skip this step.

```sh
uv run agent-company start
```

The command prints the workspace URL. Post a question or objective in
`#town-square`; the seed routes it into a focused channel and recruits workers.
Topics needing code or shared research artifacts can have their own named repo.

| Service | Local address |
| --- | --- |
| Mattermost | http://localhost:8065 |
| Analytics | http://localhost:8066 |
| Gitea | http://localhost:3000 |
| OpenCode | http://localhost:4096 |

Use an agent's dashboard link to open its specific OpenCode session. Gitea and
OpenCode credentials are recorded in the ignored provisioning state.

## Work controls

Use `/pause`, `/continue`, `/stop`, or `/reset` in Town Square to control the whole
swarm. In a work channel, pause/continue/stop apply to its participating agents.
`/reset` clears the workspace's messages, bots, work channels, topic repositories,
issues, PRs, wikis, and OpenCode data, then provisions a fresh seed and welcome.

Ctrl-C stops the controller while preserving sessions, files, and history for
`start` to resume. `uv run agent-company inspect` reports workspace state.
`uv run agent-company stop` is the terminal emergency stop.

Agents have individual Mattermost/Gitea identities, private Git checkouts and
branches, and persistent OpenCode sessions. They collaborate through native
reviews and PRs; the active agent cap defaults to 24. Inference uses the supplied
provider keys. Internal dollar budgets and lifetime turn limits are disabled.

The dashboard receives live WebSocket updates, includes paginated agent activity,
and reads provider billing reports when admin billing keys are configured.
Reported costs can arrive late. Avatar generation runs asynchronously with
GPT-Image-2.5 Flare and reuses the image across profiles.

## Development and verification

```sh
uv run ruff check harness
uv run mypy harness
uv run pytest tests/unit
```

Database integration tests use isolated schemas in the configured PostgreSQL
instance and are enabled explicitly:

```sh
HARNESS_LIVE_TESTS=1 uv run pytest tests/integration
```

Tests that call inference, mutate Mattermost/Gitea, or reset disposable services
have additional opt-in gates documented in their test modules. Review those
before enabling them.

- [Architecture](docs/ARCHITECTURE.md): canonical design and owner-directed extensions.
- [Hosted beta](docs/HOSTING.md): invitation-based hosting and per-user machines.
- [Coding services](docs/CODING.md): local/cloud provisioning and collaboration tools.
- [Decisions](docs/DECISIONS.md): implementation decisions and historical context.
- [Verification](docs/VERIFICATION.md): milestone checks and historical results.

`.env`, `.state/`, virtual environments, caches, and build outputs are excluded
from Git. Keep credentials and local service data private. Ordinary Compose
shutdown preserves volumes; `down --volumes` destroys their data.
