#!/bin/sh
set -eu
python3 - <<'PY'
import json, os
from pathlib import Path
root = Path('/home/node/.config/opencode/tools')
root.mkdir(parents=True, exist_ok=True)
for name, source in json.loads(os.environ.get('SWARM_TOOL_DEFINITIONS') or '{}').items():
    if not name.replace('_', '').isalnum():
        raise ValueError('Invalid native tool name')
    path = root / (name + '.ts')
    path.write_text(source)
    os.chown(path, 1000, 1000)
os.chown(root, 1000, 1000)
os.chown(root.parent, 1000, 1000)
PY
mkdir -p /workspaces /home/node/.local/share/opencode /home/node/.local/state \
  /home/node/.cache /home/node/.config
chown node:node /workspaces /home/node/.local /home/node/.local/share \
  /home/node/.local/share/opencode /home/node/.local/state /home/node/.cache /home/node/.config
exec gosu node "$@"
