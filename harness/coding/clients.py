"""Small, explicit adapters; mutations are never blindly retried."""

import asyncio
import base64
import json
import shlex
from typing import Any
from uuid import UUID

import httpx

from harness.config import Settings


class CodingError(RuntimeError):
    pass


def check(response: httpx.Response, service: str) -> Any:
    if response.is_error:
        # Bodies can contain commands, provider keys or private source code.
        raise CodingError(f"{service} HTTP {response.status_code}; inspect worker diagnostics")
    return response.json() if response.content else None


class CodingInterrupted(CodingError):
    """A settled, aborted OpenCode turn can continue in its existing session."""


class CodingUnavailable(CodingError):
    """Two safe read attempts failed; the local/cloud worker is unavailable."""


class Agent37:
    def __init__(self, settings: Settings):
        self.settings = settings
        if not settings.agent37_api_key.get_secret_value():
            raise CodingError("Set AGENT37_API_KEY in .env")

    async def request(self, method: str, path: str, **kwargs: Any) -> Any:
        async with httpx.AsyncClient(timeout=180) as client:
            return check(
                await client.request(
                    method,
                    str(self.settings.agent37_base_url).rstrip("/") + "/" + path,
                    headers={
                        "Authorization": "Bearer "
                        + self.settings.agent37_api_key.get_secret_value()
                    },
                    **kwargs,
                ),
                "Agent37",
            )

    async def execute(self, command: str) -> dict[str, Any]:
        result: dict[str, Any] = await self.request(
            "POST",
            f"instances/{self.settings.agent37_instance_id}/exec",
            json={"command": command, "user": "root"},
        )
        if result["exit_code"]:
            raise CodingError("Worker command failed; output withheld to protect credentials")
        return result


class Worker:
    def __init__(self, settings: Settings):
        self.settings = settings

    async def command(self, argv: list[str], data: bytes = b"") -> str:
        if self.settings.coding_backend == "agent37":
            # The control-plane key remains in this process, never in the worker container.
            payload = base64.b64encode(data).decode()
            command = "printf %s " + shlex.quote(payload) + " | base64 -d | " + shlex.join(argv)
            result = await Agent37(self.settings).execute(command)
            return str(result["stdout"])
        if self.settings.coding_backend != "docker":
            raise CodingError("Unknown coding backend")
        process = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            out, _ = await process.communicate(data)
        except BaseException:
            process.kill()
            await process.wait()
            raise
        if process.returncode:
            raise CodingError("Worker command failed; output withheld to protect credentials")
        return out.decode()

    async def workspace(
        self, workspace: UUID, agent: UUID, identity: dict[str, Any] | None = None
    ) -> dict[str, str]:
        out = await self.command(
            [
                "docker",
                "exec",
                "-i",
                "--user",
                "node",
                self.settings.coding_container,
                "python3",
                "/opt/swarm/workspace.py",
            ],
            json.dumps(
                {"workspace": str(workspace), "agent": str(agent), "identity": identity}
            ).encode(),
        )
        result: dict[str, str] = json.loads(out)
        return result

    async def stop_commands(self, directory: str) -> None:
        await self.command(
            [
                "docker",
                "exec",
                "-i",
                "--user",
                "node",
                self.settings.coding_container,
                "python3",
                "/opt/swarm/cancel.py",
            ],
            json.dumps({"directory": directory}).encode(),
        )


class OpenCode:
    def __init__(self, settings: Settings):
        headers = {}
        if settings.coding_backend == "agent37":
            headers["X-Agent37-Key"] = settings.agent37_api_key.get_secret_value()
        self.http = httpx.AsyncClient(
            base_url=str(settings.opencode_url).rstrip("/"),
            timeout=30,
            auth=("opencode", settings.opencode_password.get_secret_value()),
            headers=headers,
        )

    async def request(self, method: str, path: str, directory: str = "", **kwargs: Any) -> Any:
        params = {"directory": directory} if directory else {}
        for attempt in range(2 if method == "GET" else 1):
            try:
                return check(
                    await self.http.request(method, path, params=params, **kwargs), "OpenCode"
                )
            except httpx.TimeoutException as error:
                if method != "GET":
                    # A timed-out mutation may have succeeded. Let the caller reconcile it.
                    raise
                if attempt == 1:
                    raise CodingUnavailable(
                        "OpenCode unavailable: two read attempts timed out; work preserved"
                    ) from error
                await asyncio.sleep(0.5)
        raise AssertionError("Unreachable request retry state")

    async def close(self) -> None:
        await self.http.aclose()
