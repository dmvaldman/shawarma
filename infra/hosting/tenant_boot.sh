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
docker build -q -t swarm-controller -f infra/hosting/controller.Dockerfile .
docker rm -f swarm-controller 2>/dev/null || true
docker run -d --name swarm-controller --restart unless-stopped --network host \
  --mount type=bind,src=/root/swarm/app,dst=/root/swarm/app \
  --mount type=bind,src=/var/run/docker.sock,dst=/var/run/docker.sock \
  swarm-controller
# Do not announce readiness before the controller has opened the dashboard socket.
uv run python - <<'PY'
import time
import urllib.request
for attempt in range(90):
    try:
        with urllib.request.urlopen('http://127.0.0.1:8066/', timeout=3) as response:
            if response.status == 200:
                break
    except OSError:
        time.sleep(2)
else:
    raise SystemExit('Controller did not become ready; inspect protected container logs')
PY
