"""The seed coordinates through chat and never owns an execution loop in OpenCode."""

import asyncio
import logging
from uuid import UUID

from harness.config import Settings
from harness.core.interfaces import ModelProvider
from harness.core.runtime import Runtime
from harness.mattermost.client import Client
from harness.models.public_typing import COMPOSER
from harness.persistence.store import Store


class Coordinator(Runtime):
    coordinator = True

    def __init__(
        self, store: Store, bot: Client, agent_id: UUID, settings: Settings, provider: ModelProvider
    ):
        settings = settings.model_copy(update={"agent_engine": "provider", "coding_enabled": False})
        super().__init__(store, bot, agent_id, settings, provider)
        self.tools.coordinator = True
        self.tools.manifests = [tool for tool in self.tools.manifests if tool.name != "code_task"]

    async def begin_work(self) -> None:
        # Intake remains live between model/tool calls, including during coordination.
        await self.store.set_availability(self.agent_id, "available")
        await self.bot.presence("online")

    async def activate(self) -> None:
        typing: asyncio.Task[None] | None = None
        target: tuple[str, str] | None = None

        async def compose(channel: str, root: str) -> None:
            nonlocal typing, target
            if not channel:
                if typing:
                    typing.cancel()
                    await asyncio.gather(typing, return_exceptions=True)
                    typing = None
                target = None
                return
            if target == (channel, root):
                return
            target = (channel, root)
            try:
                async with asyncio.timeout(2):
                    await self.tools.scope(channel)
                    state = await self.store.state(self.agent_id)
                if state["control_paused"] or state["workspace_state"] != "active":
                    return
            except Exception:
                logging.getLogger(__name__).warning("Seed public composer typing unavailable")
                return
            if typing:
                typing.cancel()
                await asyncio.gather(typing, return_exceptions=True)
            target = (channel, root)
            await self.bot.typing(channel, root)

            async def refresh() -> None:
                while True:
                    await asyncio.sleep(3)
                    await self.bot.typing(channel, root)

            typing = asyncio.create_task(refresh())

        token = COMPOSER.set(compose)
        try:
            await super().activate()
        finally:
            COMPOSER.reset(token)
            if typing:
                typing.cancel()
                await asyncio.gather(typing, return_exceptions=True)
