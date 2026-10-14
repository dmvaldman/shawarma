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
uv run python - <<'PY'
import json
import subprocess
from harness.coding.setup import update_env
details = json.loads(subprocess.check_output(['docker', 'inspect', 'swarm-coding-gitea-1']))[0]
network = next(iter(details['NetworkSettings']['Networks'].values()))
update_env({'HOSTED_GITEA_TRUSTED_PROXY': network['Gateway']})
PY
# Setup generates worker config using the inference broker, with no real provider keys.
# Native ports stay private; only the session gateway is reachable by the edge.
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
docker rm -f swarm-service-gateway 2>/dev/null || true
docker run -d --name swarm-service-gateway --restart unless-stopped --network host \
  --mount type=bind,src=/root/swarm/app/.env,dst=/root/swarm/app/.env,readonly \
  --mount type=bind,src=/root/swarm/app/.state,dst=/root/swarm/app/.state,readonly \
  --entrypoint python swarm-controller -m harness.hosting.gateway
uv run python - <<'PY'
import time
import urllib.request
import urllib.error
for attempt in range(30):
    try:
        urllib.request.urlopen('http://127.0.0.1:18065/', timeout=3)
    except urllib.error.HTTPError as error:
        if error.code == 401:
            break
    except OSError:
        pass
    time.sleep(1)
else:
    raise SystemExit('Service gateway did not become ready')
PY
