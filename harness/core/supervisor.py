"""One workspace lease, bot-scoped workers, shared finite model concurrency."""

import asyncio
import logging
from pathlib import Path
from typing import Any
from uuid import UUID

from harness.api.analytics import Analytics
from harness.api.commands import SlashCommands
from harness.coding.native import Native
from harness.coding.runtime import NativeRuntime
from harness.coding.service import Coding
from harness.coding.tool_bridge import ToolBridge
from harness.config import Settings
from harness.core.avatars import Avatars
from harness.core.coordination import Coordination
from harness.core.coordinator import Coordinator
from harness.core.interfaces import ModelProvider
from harness.core.runtime import Runtime
from harness.mattermost.client import Client
from harness.persistence.store import Store


class Supervisor:
    def __init__(
        self,
        store: Store,
        settings: Settings,
        provider: ModelProvider,
        workspace: UUID,
        seed_token: str,
        checkpoint: Path = Path(".state/workspace.json"),
    ):
        self.store, self.settings, self.provider = store, settings, provider
        self.workspace, self.seed_token = workspace, seed_token
        self.coordination = Coordination(store, settings)
        self.workers: dict[UUID, Runtime] = {}
        self.ingestions: dict[UUID, asyncio.Task[None]] = {}
        self.slots = asyncio.Semaphore(settings.max_concurrent_llm_calls)
        self.lock = asyncio.Lock()
        self.stopping = asyncio.Event()
        self.checkpoint = checkpoint
        self.resetting = False
        self.reset_task: asyncio.Task[None] | None = None
        self.coding = (
            Native(store, settings)
            if settings.agent_engine == "opencode"
            else Coding(store, settings)
        )
        self.tool_bridge = ToolBridge(store, settings, workspace)
        self.avatars = Avatars(store, settings, workspace)

    async def request_reset(self, request_id: str) -> str:
        from harness.core.reset import begin_reset

        async with self.lock:
            status = await begin_reset(self.store, self.workspace, request_id, self.checkpoint)
            if status == "pending" and (self.reset_task is None or self.reset_task.done()):
                self.resetting = True
                self.reset_task = asyncio.create_task(self.perform_reset())
            return status

    async def perform_reset(self) -> None:
        from harness.core.reset import finish_reset

        try:
            async with self.lock:
                for runtime in self.workers.values():
                    runtime.stopping.set()
                    await self.tool_bridge.cancel(runtime.agent_id)
                tasks = list(self.ingestions.values()) + [
                    r.task for r in self.workers.values() if r.task is not None
                ]
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                for runtime in self.workers.values():
                    await runtime.bot.http.aclose()
                self.workers.clear()
                self.ingestions.clear()
                self.tool_bridge.tools.clear()
                if isinstance(self.coding, Native):
                    self.coding.deliveries.clear()
                    self.coding.branch_until.clear()
                    self.coding.activity.seen.clear()
                    self.coding.streams.clear()
                    self.coding.work_starts.clear()
            await self.avatars.close()
            fresh = await finish_reset(self.store, self.settings, self.checkpoint)
            self.seed_token = fresh["bot_token"]
            self.resetting = False
            await self.avatars.start()
            await self.refresh(UUID(fresh["seed_id"]))
            logging.getLogger(__name__).info(
                "Reset complete; fresh seed waits for a new task in Town Square."
            )
            try:
                async with Client(
                    str(self.settings.mattermost_url),
                    self.settings.mattermost_admin_token.get_secret_value(),
                ) as admin:
                    await admin.request(
                        "POST",
                        "posts/ephemeral",
                        json={
                            "user_id": self.settings.mattermost_owner_id,
                            "post": {
                                "channel_id": fresh["general_id"],
                                "user_id": self.settings.mattermost_owner_id,
                                "message": "Reset complete. A fresh seed is waiting for your task.",
                            },
                        },
                    )
            except Exception:
                logging.getLogger(__name__).exception(
                    "Reset completed; owner notification could not be delivered."
                )
        except Exception:
            # Keep all workers stopped; /reset retries the persisted plan.
            logging.getLogger(__name__).exception(
                "Reset interrupted; retry /reset or restart the harness."
            )

    async def refresh(self, agent: UUID) -> None:
        async with self.lock:
            state = await self.store.state(agent)
            if state["workspace_id"] != self.workspace or self.stopping.is_set() or self.resetting:
                return
            if state["state"] == "TERMINATED":
                await self.avatars.cancel(agent)
                runtime = self.workers.pop(agent, None)
                if runtime:
                    runtime.stopping.set()
                    await self.tool_bridge.cancel(agent)
                    self.tool_bridge.tools.pop(agent, None)
                    if isinstance(self.coding, Native):
                        self.coding.deliveries.pop(agent, None)
                        self.coding.work_starts.pop(agent, None)
                        self.coding.streams.pop(agent, None)
                    tasks = [self.ingestions.pop(agent)]
                    if runtime.task:
                        tasks.append(runtime.task)
                    for task in tasks:
                        task.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)
                    await runtime.bot.http.aclose()
                await self.coordination.release_allocations(self.workspace)
                # Cancel reasoning first, including self-termination's persisted tool result.
                # Every startup revisits terminal records, so partial cleanup is retryable.
                await self.coordination.disable_terminated_bot(agent)
                return
            if agent not in self.workers:
                if state["state"] in {"FAILED", "QUOTA_BLOCKED"}:
                    logging.getLogger(__name__).warning(
                        "Agent %s remains %s at startup; it cannot resume automatically.",
                        agent,
                        state["state"],
                    )
                await self.store.recover(agent)
                await self.store.recover_tools(agent)
                token = state["credential"] or self.seed_token
                bot = Client(str(self.settings.mattermost_url), token)
                seed = state["parent_id"] is None
                runtime_type = (
                    Coordinator
                    if seed
                    else (NativeRuntime if self.settings.agent_engine == "opencode" else Runtime)
                )
                runtime = runtime_type(self.store, bot, agent, self.settings, self.provider)
                runtime.model_slots = asyncio.Semaphore(1) if seed else self.slots
                runtime.tools.model_slots = (
                    asyncio.Semaphore(1)
                    if isinstance(runtime, NativeRuntime) or seed
                    else self.slots
                )
                runtime.tools.coding = self.coding
                if isinstance(runtime, NativeRuntime):
                    assert isinstance(self.coding, Native)
                    runtime.native = self.coding
                    if bot.token not in self.coding.activity.secrets:
                        self.coding.activity.secrets.append(bot.token)
                    self.coding.deliveries[agent] = runtime.deliver
                    self.coding.work_starts[agent] = runtime.begin_work
                    self.coding.streams[agent] = runtime.tools.streams
                    self.tool_bridge.tools[agent] = runtime.tools
                self.workers[agent] = runtime
                self.ingestions[agent] = asyncio.create_task(runtime.ingest())
            await self.avatars.enqueue(agent)
            await self.workers[agent].wake(agent)

    async def control(self) -> None:
        async with self.lock:
            if self.resetting:
                return
            state = await self.store.pool.fetchval(
                "SELECT state FROM workspaces WHERE id=$1", self.workspace
            )
            for agent, runtime in self.workers.items():
                current = await self.store.state(agent)
                if state == "paused" or current["control_paused"]:
                    await self.avatars.cancel(agent)
                    await self.tool_bridge.cancel(agent)
                    await runtime.pause()
            if state == "paused":
                logging.getLogger(__name__).info("Swarm paused by owner; /continue resumes work.")
                return
        if state == "active" and not self.stopping.is_set():
            await self.coordination.recover(self.workspace)
            for identity in await self.store.list_agents(self.workspace):
                await self.refresh(identity.id)
            logging.getLogger(__name__).info("Owner controls applied.")

    async def run(self) -> None:
        async with self.store.pool.acquire() as lease:
            if not await lease.fetchval(
                "SELECT pg_try_advisory_lock(hashtextextended($1,0))", str(self.workspace)
            ):
                raise RuntimeError("workspace already has a running supervisor")

            def wake_callback(*args: Any) -> None:
                async def refresh() -> None:
                    try:
                        await self.refresh(UUID(args[-1]))
                    except KeyError:
                        pass  # Other workspace's notification.

                asyncio.create_task(refresh())

            def stop_callback(*args: Any) -> None:
                if args[-1] == str(self.workspace):
                    self.stopping.set()

            def control_callback(*args: Any) -> None:
                if args[-1] == str(self.workspace):
                    asyncio.create_task(self.control())

            await lease.add_listener("harness_control", control_callback)
            await lease.add_listener("harness_wake", wake_callback)
            await lease.add_listener("harness_stop", stop_callback)
            analytics = Analytics(self.store, self.settings, self.workspace)
            commands = SlashCommands(self.store, self.settings, self.workspace)
            commands.reset = self.request_reset
            try:
                await analytics.start()
                await commands.start()
                await self.avatars.start()
                if self.settings.agent_engine == "opencode":
                    await self.tool_bridge.start()
                await self.coding.recover()
                await self.coordination.recover(self.workspace)
                await self.store.apply_main_routing(self.workspace)
                for agent in await self.store.list_agents(self.workspace):
                    await self.refresh(agent.id)
                await self.stopping.wait()
            finally:
                self.stopping.set()
                for runtime in self.workers.values():
                    runtime.stopping.set()
                    if runtime.task:
                        runtime.task.cancel()
                await self.tool_bridge.close()
                await self.avatars.close()
                await analytics.close()
                await commands.close()
                if self.reset_task and not self.reset_task.done():
                    self.reset_task.cancel()
                    await asyncio.gather(self.reset_task, return_exceptions=True)
                self.stopping.set()
                for runtime in self.workers.values():
                    runtime.stopping.set()
                tasks = list(self.ingestions.values()) + [
                    r.task for r in self.workers.values() if r.task is not None
                ]
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                for runtime in self.workers.values():
                    await runtime.bot.http.aclose()
                await lease.remove_listener("harness_control", control_callback)
                await lease.remove_listener("harness_wake", wake_callback)
                await lease.remove_listener("harness_stop", stop_callback)
                await lease.execute(
                    "SELECT pg_advisory_unlock(hashtextextended($1,0))", str(self.workspace)
                )
