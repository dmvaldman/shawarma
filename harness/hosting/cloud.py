"""Agent37 lifecycle operations; no credentials in errors or remote command output."""

import asyncio
import base64
import json
import secrets
import shlex
from pathlib import Path
from typing import Any

import httpx

from harness.hosting.deployment import deployment_command, installation_key, runtime_revision


class HostingError(RuntimeError):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class Cloud:
    def __init__(self, key: str):
        self.key = key

    async def request(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        async with httpx.AsyncClient(timeout=240) as client:
            r = await client.request(
                method,
                "https://api.agent37.com/v1/" + path,
                headers={"Authorization": "Bearer " + self.key},
                json=body,
            )
        if r.is_error:
            raise HostingError(
                f"Infrastructure request failed (HTTP {r.status_code})", r.status_code
            )
        return r.json() if r.content else None

    async def command(self, instance: str, command: str) -> str:
        data = await self.request(
            "POST", f"instances/{instance}/exec", {"command": command, "user": "root"}
        )
        if data.get("exit_code"):
            raise HostingError("Machine setup step failed; inspect protected operator diagnostics")
        return str(data.get("stdout", ""))

    async def install(self, instance: str, env: dict[str, str], source_ref: str) -> None:
        payload = base64.b64encode(json.dumps(env).encode()).decode()
        command = (
            "set -eu; mkdir -p /root/swarm; "
            "docker stop --time 60 swarm-service-gateway swarm-controller 2>/dev/null || true; "
            "if [ ! -d /root/swarm/app/.git ]; then "
            "git clone -q https://github.com/dmvaldman/shawarma.git /root/swarm/app; fi; "
            "cd /root/swarm/app; git fetch -q origin; git checkout -q "
            + shlex.quote(source_ref)
            + "; printf %s "
            + shlex.quote(payload)
            + " | base64 -d | python3 infra/hosting/write_env.py; "
            "bash infra/hosting/tenant_boot.sh"
        )
        revision = await asyncio.to_thread(runtime_revision, Path(__file__).parents[2])
        key = installation_key(env, revision)
        command = deployment_command(command, key)
        # Exec is bounded by the provider; detach the installer and poll its durable status.
        for _ in range(60):
            try:
                await self.command(instance, "true")
                break
            except (HostingError, httpx.TransportError):
                await asyncio.sleep(2)
        else:
            raise HostingError("Machine did not become ready")
        generation = secrets.token_hex(8)
        status = "/root/swarm/setup-" + generation + ".status"
        wrapped = "trap " + shlex.quote("echo $? > " + status) + " EXIT; " + command
        launch = (
            "mkdir -p /root/swarm; nohup flock -n /root/swarm/setup.lock bash -c "
            + shlex.quote(wrapped)
            + " >/root/swarm/setup.log 2>&1 </dev/null & echo started"
        )
        await self.command(instance, launch)
        for _ in range(300):
            result = await self.command(
                instance, "if [ -f " + status + " ]; then cat " + status + "; fi"
            )
            if result.strip():
                if result.strip() != "0":
                    raise HostingError("Machine installer failed; protected setup log retained")
                return
            await asyncio.sleep(2)
        raise HostingError("Machine setup is still running; inspect before retrying")
