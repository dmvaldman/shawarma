"""Identify changes that require installation, independently of portal-only commits."""

import hashlib
import json
import shlex
from functools import lru_cache
from pathlib import Path

CONTAINERS = (
    "agent-company-postgres-1",
    "agent-company-mattermost-1",
    "agent-company-command-gateway-1",
    "swarm-coding-gitea-1",
    "swarm-coding-worker",
    "swarm-controller",
    "swarm-service-gateway",
)


@lru_cache
def runtime_revision(root: Path) -> str:
    digest = hashlib.sha256()
    paths = [root / name for name in ("pyproject.toml", "uv.lock", ".dockerignore")]
    for directory in ("harness", "prompts", "infra"):
        paths.extend(
            p
            for p in (root / directory).rglob("*")
            if p.is_file() and "__pycache__" not in p.parts and p.suffix != ".pyc"
        )
    for path in sorted(paths):
        if path.is_file():
            digest.update(str(path.relative_to(root)).encode() + b"\0" + path.read_bytes() + b"\0")
    return digest.hexdigest()


def installation_key(env: dict[str, str], revision: str) -> str:
    return hashlib.sha256(json.dumps([revision, env], sort_keys=True).encode()).hexdigest()


def deployment_command(full_install: str, key: str, root: str = "/root/swarm/app") -> str:
    app = shlex.quote(root)
    marker = shlex.quote(root + "/.state/runtime-ready")
    python = shlex.quote(root + "/.venv/bin/python")
    containers = " ".join(CONTAINERS)
    return (
        f'set -eu; if [ -f {marker} ] && [ "$(cat {marker})" = {shlex.quote(key)} ] '
        f"&& [ -x {python} ] && docker inspect {containers} >/dev/null 2>&1; then "
        f"cd {app}; echo 'Resuming existing services'; "
        f"{python} -m harness.hosting.resume; "
        f"else rm -f {marker}; {full_install}; "
        f"printf %s {shlex.quote(key)} > {marker}.tmp; mv {marker}.tmp {marker}; fi"
    )
