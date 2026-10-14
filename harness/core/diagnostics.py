"""Human-visible operational logs; no agent inbox routing or recursive reporting."""

import asyncio
import logging
import re
import sys
from typing import Any

from harness.config import Settings
from harness.mattermost.client import Client, MattermostError
from harness.mattermost.provisioning import find_or_create, membership


async def ensure_errors_channel(admin: Client, team_id: str, owner_id: str) -> str:
    channel = await find_or_create(
        admin,
        f"teams/{team_id}/channels/name/errors",
        "channels",
        {
            "team_id": team_id,
            "name": "errors",
            "display_name": "Errors",
            "type": "O",
            "purpose": "Harness errors, warnings and important operational logs.",
        },
    )
    if channel.get("delete_at"):
        channel = await admin.request("POST", f"channels/{channel['id']}/restore")
    await membership(admin, team_id, channel["id"], owner_id)
    return str(channel["id"])


class Diagnostics(logging.Handler):
    def __init__(self, settings: Settings, team_id: str, secrets: tuple[str, ...] = ()):
        super().__init__(logging.INFO)
        self.settings, self.team_id = settings, team_id
        self.extra_secrets = secrets
        self.admin = Client(
            str(settings.mattermost_url), settings.mattermost_admin_token.get_secret_value()
        )
        self.channel_id: str | None = None
        self.queue: asyncio.Queue[str] = asyncio.Queue(maxsize=200)
        self.worker: asyncio.Task[None] | None = None
        self.loop = asyncio.get_running_loop()
        self.previous_exception_handler = self.loop.get_exception_handler()
        self.logger = logging.getLogger("harness")
        self.previous_level = self.logger.level
        self.setFormatter(logging.Formatter("%(levelname)s — %(name)s\n%(message)s"))

    def redact(self, text: str) -> str:
        secrets = [
            self.settings.mattermost_admin_token.get_secret_value(),
            self.settings.openai_api_key.get_secret_value(),
            self.settings.avatar_api_key.get_secret_value(),
            self.settings.anthropic_api_key.get_secret_value(),
            self.settings.deepseek_api_key.get_secret_value(),
            self.settings.agent37_api_key.get_secret_value(),
            self.settings.opencode_password.get_secret_value(),
            self.settings.gitea_token.get_secret_value(),
            self.settings.swarm_tool_key.get_secret_value(),
            self.settings.database_url,
            *self.extra_secrets,
        ]
        for secret in sorted(filter(None, secrets), key=len, reverse=True):
            text = text.replace(secret, "[redacted]")
        text = re.sub(
            r"(?i)(authorization\s*[:=]\s*|bearer\s+|(?:api[_-]?key|token|password)\s*[:=]\s*)[^\s,;]+",
            r"\1[redacted]",
            text,
        )
        text = re.sub(r"\bsk-[A-Za-z0-9_-]+", "[redacted]", text)
        text = re.sub(r"(https?://|postgres(?:ql)?://)[^\s/@]+:[^\s/@]+@", r"\1[redacted]@", text)
        # Error-channel text must not trigger broad mentions or be rendered as raw HTML.
        return text.replace("@", "＠").replace("<", "&lt;").replace(">", "&gt;")[:6000]

    def filter(self, record: logging.LogRecord) -> bool:
        # Harness lifecycle/warnings and errors from every Python library/service.
        return record.name.startswith("harness") or record.levelno >= logging.ERROR

    def emit(self, record: logging.LogRecord) -> None:
        message = self.redact(self.format(record))

        def enqueue() -> None:
            try:
                self.queue.put_nowait(message)
            except asyncio.QueueFull:
                print("Mattermost diagnostics queue full; " + message, file=sys.stderr, flush=True)

        if not self.loop.is_closed():
            self.loop.call_soon_threadsafe(enqueue)

    async def start(self) -> None:
        try:
            self.channel_id = await ensure_errors_channel(
                self.admin, self.team_id, self.settings.mattermost_owner_id
            )
        except Exception:
            print(
                "Cannot provision #errors yet; diagnostics will retry.", file=sys.stderr, flush=True
            )
        self.logger.setLevel(logging.INFO)
        logging.getLogger().addHandler(self)

        def unhandled(loop: asyncio.AbstractEventLoop, context: dict[str, Any]) -> None:
            error = context.get("exception")
            self.logger.error(
                "Unhandled async failure: %s",
                type(error).__name__ if error else "UnknownError",
                exc_info=(type(error), error, error.__traceback__) if error else None,
            )
            if self.previous_exception_handler:
                self.previous_exception_handler(loop, context)

        self.loop.set_exception_handler(unhandled)
        self.worker = asyncio.create_task(self.deliver())

    async def deliver(self) -> None:
        while True:
            message = await self.queue.get()
            try:
                for attempt in range(3):
                    try:
                        if self.channel_id is None:
                            self.channel_id = await ensure_errors_channel(
                                self.admin, self.team_id, self.settings.mattermost_owner_id
                            )
                        await self.admin.post_channel(
                            self.channel_id, "**Harness log**\n\n" + message
                        )
                        break
                    except MattermostError as error:
                        if error.status == 404:
                            self.channel_id = None
                        if attempt == 2:
                            print(
                                "Mattermost diagnostics delivery failed; " + message,
                                file=sys.stderr,
                                flush=True,
                            )
                        else:
                            await asyncio.sleep(2**attempt)
                    except Exception:
                        # Never log through this handler while reporting a logging failure.
                        print(
                            "Mattermost diagnostics delivery failed; " + message,
                            file=sys.stderr,
                            flush=True,
                        )
                        break
            finally:
                self.queue.task_done()

    async def shutdown(self) -> None:
        logging.getLogger().removeHandler(self)
        self.logger.setLevel(self.previous_level)
        self.loop.set_exception_handler(self.previous_exception_handler)
        await asyncio.sleep(0)  # Drain enqueues already scheduled by emit().
        try:
            await asyncio.wait_for(self.queue.join(), timeout=5)
        except TimeoutError:
            print(
                "Pending diagnostics could not be delivered before shutdown.",
                file=sys.stderr,
                flush=True,
            )
        if self.worker:
            self.worker.cancel()
            await asyncio.gather(self.worker, return_exceptions=True)
        await self.admin.http.aclose()
        self.close()
