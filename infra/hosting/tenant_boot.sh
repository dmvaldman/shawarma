#!/usr/bin/env bash
set -euo pipefail
cd /root/swarm/app
export PATH="/root/.local/bin:$PATH"
if ! command -v uv >/dev/null; then
  curl -LsSf https://astral.sh/uv/install.sh | sh
fi
uv sync --locked --python 3.12
# Public provider URLs are edge-authenticated; the runtime uses local REST endpoints.
docker compose --env-file .env -f infra/compose.yaml -f infra/hosting/tenant-compose.yaml up -d --wait
uv run python -m harness.hosting.bootstrap
uv run agent-company-coding up
# Setup generates worker config using the inference broker, with no real provider keys.
# Make the OpenCode port reachable by the private Agent37 edge, retaining Basic auth.
docker compose --env-file .env --env-file .state/coding.env -f infra/coding/compose.yaml -f infra/hosting/coding-compose.yaml up -d worker gitea
uv run python -m harness.hosting.bootstrap --finalize
cat >/etc/systemd/system/swarm.service <<'UNIT'
[Unit]
Description=Personal swarm controller
After=docker.service
[Service]
WorkingDirectory=/root/swarm/app
ExecStart=/root/swarm/app/.venv/bin/agent-company start
Restart=on-failure
RestartSec=5
[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable swarm.service
systemctl restart swarm.service
