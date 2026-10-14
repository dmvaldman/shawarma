import asyncio
import os
from decimal import Decimal
from uuid import UUID

import pytest
from test_runtime import BlockingProvider

from harness.core.coordination import Coordination
from harness.core.runtime import Runtime
from harness.core.supervisor import Supervisor
from harness.mattermost.client import Client, MattermostError
from harness.persistence.store import QuotaError, decode

pytestmark = pytest.mark.skipif(
    os.environ.get("HARNESS_LIVE_TESTS") != "1", reason="live PostgreSQL gate"
)

ARGS = {
    "system_prompt": "Independently verify the algorithm.",
    "initial_task": "Check edge cases and report to your parent.",
    "tools": [],
}


class FakeAdmin:
    fail_membership = False
    disabled = []

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    async def me(self):
        return {"id": "admin"}

    async def request(self, method, path, **kwargs):
        if path == "users/admin/channels":
            return []
        if path.startswith("users/username/"):
            raise MattermostError(404, "not_found")
        if path.endswith("/channels/name/off-topic"):
            return {"id": "off-topic"}
        if path.startswith("channels/") and method == "GET" and "/members/" not in path:
            return {"id": path.split("/")[-1], "team_id": "team", "type": "O"}
        if path == "users":
            self.bot_id = "user-" + kwargs["json"]["username"]
            return {"id": self.bot_id, "is_bot": False, **kwargs["json"]}
        if path.startswith("bots/") and method in {"GET", "PUT"}:
            return {"user_id": self.bot_id, "owner_id": "admin"}
        if "/members/" in path and self.fail_membership:
            raise ValueError("membership failed")
        if path.endswith("/tokens"):
            return {"token": "private-token"}
        if path.endswith("/disable"):
            self.disabled.append(path)
        return {}


async def test_atomic_birth_without_allocations_ancestry_and_no_private_prompt_leak(
    runtime_store, monkeypatch
):
    store, settings, parent, workspace = runtime_store
    monkeypatch.setattr("harness.core.coordination.Client", FakeAdmin)
    coordinator = Coordination(store, settings)
    child = UUID((await coordinator.spawn(parent, "birth", ARGS))["agent_id"])
    assert (await store.get(child)).parent_id == parent
    assert (await store.state(child))["budget"] is None
    assert "Independently verify" in (await store.get(child)).system_prompt
    assert "Core coordination tools are installed" in (await store.get(child)).system_prompt
    assert (await store.state(parent))["budget"] is None
    assert (
        await store.pool.fetchval(
            "SELECT mode FROM subscriptions WHERE agent_id=$1 AND channel_id='general'", child
        )
        == "all"
    )
    assert (
        await store.pool.fetchval(
            "SELECT mode FROM subscriptions WHERE agent_id=$1 AND channel_id='off-topic'", child
        )
        == "all"
    )
    assert "initial_task" in str(await store.history(child))
    retry = await coordinator.spawn(parent, "birth", ARGS)
    assert retry["agent_id"] == str(child)
    assert "system_prompt" not in retry and "credential" not in retry
    grandchild = UUID(
        (
            await coordinator.spawn(
                child,
                "grandchild",
                {
                    **ARGS,
                },
            )
        )["agent_id"]
    )
    sibling = UUID((await coordinator.spawn(parent, "sibling", ARGS))["agent_id"])
    for caller, target in [(sibling, child), (child, parent)]:
        with pytest.raises(ValueError, match="NOT_DESCENDANT"):
            await coordinator.terminate(caller, target, "unauthorized")
    await coordinator.terminate(parent, child, "done")
    assert (await store.state(grandchild))["state"] == "READY"
    await coordinator.release_allocations(workspace)
    assert (await store.state(parent))["budget"] is None
    await coordinator.terminate(parent, grandchild, "free descendant slot")
    await coordinator.release_allocations(workspace)
    assert (await store.state(parent))["budget"] is None
    await coordinator.release_allocations(workspace)
    assert (await store.state(parent))["budget"] is None


async def test_spawn_caps_model_validation_and_rollback(runtime_store, monkeypatch):
    store, settings, parent, workspace = runtime_store
    monkeypatch.setattr("harness.core.coordination.Client", FakeAdmin)
    coordinator = Coordination(store, settings)
    with pytest.raises(ValueError, match="MODEL_NOT_ALLOWED"):
        await coordinator.reserve(parent, "wrongmodel", {**ARGS, "model": "unverified"})
    with pytest.raises(ValueError, match="CUSTOM_TOOLS_UNAVAILABLE"):
        await coordinator.reserve(parent, "tools", {**ARGS, "tools": [{"name": "invented"}]})
    await store.pool.execute(
        "UPDATE workspaces SET limits=jsonb_set(limits,'{max_active_agents}','\"2\"') WHERE id=$1",
        workspace,
    )
    results = await asyncio.gather(
        coordinator.reserve(parent, "one", ARGS),
        coordinator.reserve(parent, "two", ARGS),
        return_exceptions=True,
    )
    assert sum(isinstance(r, QuotaError) for r in results) == 1
    call = "one" if not isinstance(results[0], Exception) else "two"
    await coordinator.fail(parent, call)
    assert (await store.state(parent))["budget"] is None
    monkeypatch.setattr(FakeAdmin, "fail_membership", True)
    with pytest.raises(ValueError, match="membership failed"):
        await coordinator.spawn(parent, "broken", ARGS)
    assert (await store.state(parent))["budget"] is None
    assert len(await store.list_agents(workspace)) == 1
    assert FakeAdmin.disabled


async def test_parallel_agents_share_concurrency_and_single_activation(runtime_store, monkeypatch):
    store, settings, parent, workspace = runtime_store
    monkeypatch.setattr("harness.core.coordination.Client", FakeAdmin)
    coordinator = Coordination(store, settings)
    child = UUID((await coordinator.spawn(parent, "child", ARGS))["agent_id"])
    await store.pool.execute("UPDATE workspaces SET started=true WHERE id=$1", workspace)
    await store.pool.execute("UPDATE agents SET state='READY' WHERE id=$1", parent)
    provider = BlockingProvider()
    async with (
        Client("https://unused.test", "token") as first,
        Client("https://unused.test", "token") as second,
    ):
        runtimes = [
            Runtime(store, first, parent, settings, provider),
            Runtime(store, second, child, settings, provider),
        ]
        supervisor = Supervisor(store, settings, provider, workspace, "token")
        supervisor.slots = asyncio.Semaphore(2)
        for r in runtimes:
            r.model_slots = supervisor.slots
        await asyncio.gather(*(r.wake(r.agent_id) for r in runtimes for _ in range(5)))
        async with asyncio.timeout(3):
            while provider.calls < 2:  # noqa: ASYNC110
                await asyncio.sleep(0.01)
        assert provider.peak == 2
        provider.release.set()
        await asyncio.gather(*(r.task for r in runtimes))
        assert provider.calls == 2
        for r in runtimes:
            r.stopping.set()


async def test_termination_cannot_resurrect_and_usage_survives_settlement(
    runtime_store, monkeypatch
):
    store, settings, parent, workspace = runtime_store
    monkeypatch.setattr("harness.core.coordination.Client", FakeAdmin)
    coordinator = Coordination(store, settings)
    child = UUID((await coordinator.spawn(parent, "child", ARGS))["agent_id"])
    turn = await store.reserve_cost(child, Decimal("0.1"), [])
    await coordinator.terminate(parent, child, "stop in flight")
    await store.set_state(child, "RUNNING")
    assert (await store.state(child))["state"] == "TERMINATED"
    await coordinator.release_allocations(workspace)
    assert (await store.state(parent))["budget"] is None
    await store.finish(turn, None)
    await coordinator.release_allocations(workspace)
    assert (await store.state(parent))["budget"] is None


async def test_long_tool_work_yields_and_resumes_without_terminal_round_limit(runtime_store):
    from harness.core.types import ModelResponse, Usage

    store, settings, agent, workspace = runtime_store
    await store.pool.execute("UPDATE workspaces SET started=true WHERE id=$1", workspace)
    await store.pool.execute("UPDATE agents SET state='READY' WHERE id=$1", agent)

    class WorkingProvider:
        calls = 0

        async def generate(self, messages, tools, model_id, max_tokens, timeout):  # noqa: ASYNC109
            self.calls += 1
            return ModelResponse(
                message={
                    "output": [
                        {
                            "type": "function_call",
                            "call_id": str(self.calls),
                            "name": "get_status",
                            "arguments": "{}",
                        }
                    ]
                    if self.calls <= 15
                    else []
                },
                usage=Usage(input_tokens=10, output_tokens=10, cost_usd=Decimal("0.0001")),
            )

    provider = WorkingProvider()
    from unittest.mock import AsyncMock

    async with Client("https://unused.test", "token") as bot:
        bot.request = AsyncMock(return_value={"id": "owner-id", "username": "owner"})
        runtime = Runtime(store, bot, agent, settings, provider)
        await runtime.wake(agent)
        async with asyncio.timeout(5):
            while provider.calls < 16 or (await store.state(agent))["state"] != "READY":  # noqa: ASYNC110
                await asyncio.sleep(0.01)
        runtime.stopping.set()
        if runtime.task:
            await runtime.task
    assert provider.calls == 16
    assert (await store.state(agent))["state"] == "READY"


async def test_concurrent_inbox_foreign_keys_and_usage_reservations_share_lock_order(
    runtime_store, monkeypatch
):
    from harness.core.types import Post

    store, settings, parent, workspace = runtime_store
    monkeypatch.setattr("harness.core.coordination.Client", FakeAdmin)
    child = UUID(
        (await Coordination(store, settings).spawn(parent, "lock-order", ARGS))["agent_id"]
    )
    await store.pool.execute("UPDATE workspaces SET owner_id='owner' WHERE id=$1", workspace)

    async def work(index):
        agent = parent if index % 2 else child
        post = Post(
            id=f"concurrent-{index}",
            user_id="owner",
            channel_id="general",
            message="@all steer",
            create_at=1000 + index,
        )
        turn, _ = await asyncio.gather(
            store.reserve_cost(agent, Decimal("0.001"), []),
            store.ingest(agent, post, "channel_post", "s"),
        )
        await store.finish(turn, None)

    await asyncio.wait_for(asyncio.gather(*(work(i) for i in range(20))), 10)
    assert await store.unread_count(parent) == 10
    assert await store.unread_count(child) == 10
    assert (await store.state(parent))["reserved"] == 0
    assert (await store.state(child))["reserved"] == 0


async def test_terminated_bots_disabled_idempotently_without_affecting_descendants(
    runtime_store, monkeypatch
):
    from harness.core.names import agent_bio

    store, settings, parent, workspace = runtime_store
    monkeypatch.setattr("harness.core.coordination.Client", FakeAdmin)
    coordinator = Coordination(store, settings)
    child = UUID((await coordinator.spawn(parent, "cleanup-child", ARGS))["agent_id"])
    grandchild = UUID((await coordinator.spawn(child, "cleanup-grandchild", ARGS))["agent_id"])
    identity = await store.get(child)
    metadata = {
        "provider": "openai",
        "api_id": settings.openai_model_id,
        "class": "lite",
        "cost": "c1",
    }
    disabled = []

    class CleanupAdmin(FakeAdmin):
        description = agent_bio(workspace, child, "child", "Verify correctness.", metadata)
        owner_id = "admin"

        async def request(self, method, path, **kwargs):
            assert path.startswith(f"bots/{identity.mattermost_user_id}")
            if method == "GET":
                assert kwargs["params"] == {"include_deleted": "true"}
                return {
                    "description": self.description,
                    "delete_at": 1 if disabled else 0,
                    "user_id": identity.mattermost_user_id,
                    "owner_id": self.owner_id,
                }
            assert path.endswith("/disable")
            disabled.append(path)
            return {}

    monkeypatch.setattr("harness.core.coordination.Client", CleanupAdmin)
    with pytest.raises(ValueError, match="AGENT_NOT_TERMINATED"):
        await coordinator.disable_terminated_bot(child)
    await coordinator.terminate(parent, child, "finished")
    # Startup reconciliation must work even without an existing worker.
    supervisor = Supervisor(store, settings, object(), workspace, "unused")
    await supervisor.refresh(child)
    await supervisor.refresh(child)
    assert disabled == [f"bots/{identity.mattermost_user_id}/disable"]
    assert (await store.state(parent))["state"] != "TERMINATED"
    assert (await store.state(grandchild))["state"] == "READY"
    CleanupAdmin.owner_id = "unrelated-owner"
    with pytest.raises(ValueError, match="BOT_OWNERSHIP_MISMATCH"):
        await coordinator.disable_terminated_bot(child)


async def test_web_search_and_inference_share_the_same_workspace_concurrency_slot(
    runtime_store, monkeypatch
):
    store, settings, agent, workspace = runtime_store
    settings = settings.model_copy(update={"max_concurrent_llm_calls": 1})
    await store.pool.execute("UPDATE workspaces SET started=true WHERE id=$1", workspace)
    await store.pool.execute("UPDATE agents SET state='READY' WHERE id=$1", agent)
    provider = BlockingProvider()
    searches = []

    async def search(*args, **kwargs):
        searches.append("started")
        assert provider.active == 0
        return {"text": "Retrieved result", "sources": []}

    monkeypatch.setattr("harness.tools.core.search_web", search)
    async with Client("https://unused.test", "token") as bot:
        runtime = Runtime(store, bot, agent, settings, provider)
        await runtime.wake(agent)
        await asyncio.wait_for(provider.entered.wait(), 2)
        turn = await store.reserve_cost(agent, Decimal("0.01"), [])
        call = {
            "call_id": "search-after-model",
            "name": "web_search",
            "arguments": '{"query":"official source"}',
        }
        task = asyncio.create_task(runtime.tools.execute(turn, call))
        await asyncio.sleep(0.03)
        assert searches == [] and not task.done()
        provider.release.set()
        await asyncio.wait_for(runtime.task, 2)
        await asyncio.wait_for(task, 2)
        assert searches == ["started"]
        runtime.stopping.set()


async def test_name_collisions_are_serialized_numbered_and_stable(runtime_store):
    from unittest.mock import AsyncMock
    from uuid import uuid4

    from harness.mattermost.provisioning import available_name

    store, settings, parent, workspace = runtime_store
    agents = [uuid4() for _ in range(8)]
    names = await asyncio.gather(
        *(store.reserve_name("server", agent, "audit") for agent in agents)
    )
    assert set(names) == {"audit"} | {f"audit{i}" for i in range(2, 9)}
    assert names == await asyncio.gather(
        *(store.reserve_name("server", a, "audit") for a in agents)
    )
    admin = type("Admin", (), {})()
    admin.request = AsyncMock(
        side_effect=[{"id": "unrelated-human"}, MattermostError(404, "not_found")]
    )
    external = await available_name(store, admin, "server", uuid4(), "seed")
    assert external == "seed2"


async def test_spawn_with_exhausted_legacy_budget(runtime_store, monkeypatch):
    store, settings, parent, workspace = runtime_store
    monkeypatch.setattr("harness.core.coordination.Client", FakeAdmin)
    await store.pool.execute("UPDATE agents SET budget=0,spent=100 WHERE id=$1", parent)
    await store.pool.execute("UPDATE workspaces SET spent=1000 WHERE id=$1", workspace)
    coordinator = Coordination(store, settings)
    child = UUID((await coordinator.spawn(parent, "no-allocation", ARGS))["agent_id"])
    assert (await store.state(parent))["budget"] == 0
    assert (await store.state(child))["budget"] is None
    turn = await store.reserve_cost(child, Decimal("100"), [])
    await store.finish(turn, None)
    await coordinator.terminate(parent, child, "done")
    await coordinator.release_allocations(workspace)
    assert (await store.state(parent))["budget"] == 0
    assert (await store.state(child))["spent"] == Decimal("100")


async def test_budget_removal_retries_blocked_agents_and_preserves_history(
    runtime_store, monkeypatch
):
    from importlib.resources import files

    store, settings, parent, workspace = runtime_store
    monkeypatch.setattr("harness.core.coordination.Client", FakeAdmin)
    coordinator = Coordination(store, settings)
    child = UUID((await coordinator.spawn(parent, "retired", ARGS))["agent_id"])
    await coordinator.terminate(parent, child, "already done")
    await store.pool.execute(
        "UPDATE agents SET budget=1,spent=0.99,turns=321,state='QUOTA_BLOCKED' WHERE id=$1",
        parent,
    )
    before = await store.history(parent)
    await store.pool.execute(
        files("harness.persistence").joinpath("migrations/009_no_budgets.sql").read_text()
    )
    state = await store.state(parent)
    assert state["state"] == "READY" and state["turns"] == 321
    assert state["spent"] == Decimal("0.99") and state["budget"] == 1
    assert (await store.state(child))["state"] == "TERMINATED"
    assert await store.history(parent) == before


async def test_global_24_cap_counts_pending_births_atomically(runtime_store):
    store, settings, seed, workspace = runtime_store
    coordinator = Coordination(store, settings)
    results = await asyncio.gather(
        *(coordinator.reserve(seed, f"slot-{index}", ARGS) for index in range(30)),
        return_exceptions=True,
    )
    assert sum(isinstance(result, UUID) for result in results) == 23
    assert sum(isinstance(result, QuotaError) for result in results) == 7
    assert (
        await store.pool.fetchval("SELECT count(*) FROM spawn_requests WHERE status='provisioning'")
        == 23
    )
    first = next(index for index, result in enumerate(results) if isinstance(result, UUID))
    await coordinator.fail(seed, f"slot-{first}")
    assert isinstance(await coordinator.reserve(seed, "replacement", ARGS), UUID)


async def test_capacity_escalation_can_retire_cousin_without_cascade(runtime_store, monkeypatch):
    store, settings, seed, workspace = runtime_store
    monkeypatch.setattr("harness.core.coordination.Client", FakeAdmin)
    coordinator = Coordination(store, settings)
    left = UUID((await coordinator.spawn(seed, "left", ARGS))["agent_id"])
    requester = UUID((await coordinator.spawn(left, "requester", ARGS))["agent_id"])
    right = UUID((await coordinator.spawn(seed, "right", ARGS))["agent_id"])
    cousin = UUID((await coordinator.spawn(right, "cousin", ARGS))["agent_id"])
    ongoing = UUID((await coordinator.spawn(cousin, "ongoing", ARGS))["agent_id"])
    await store.pool.execute(
        "UPDATE workspaces SET limits=limits || $2::jsonb WHERE id=$1",
        workspace,
        '{"max_active_agents":"6","max_agents_total":"1","max_child_spawns_per_agent":"0"}',
    )
    with pytest.raises(QuotaError, match="CAPACITY_EXCEEDED"):
        await coordinator.reserve(requester, "at-cap", ARGS)
    with pytest.raises(ValueError, match="NOT_DESCENDANT"):
        await coordinator.terminate(left, cousin, "requires escalation to common ancestor")
    await coordinator.terminate(seed, cousin, "grant requester capacity by retiring cousin")
    assert (await store.state(ongoing))["state"] == "READY"
    replacement = UUID((await coordinator.spawn(requester, "granted", ARGS))["agent_id"])
    assert (await store.get(replacement)).parent_id == requester
    assert (
        await store.pool.fetchval(
            "SELECT count(*) FROM agents WHERE workspace_id=$1 AND state!='TERMINATED'", workspace
        )
        == 6
    )
    with pytest.raises(ValueError, match="NOT_DESCENDANT"):
        await coordinator.terminate(requester, right, "unrelated branch")


async def test_reinitialize_applies_live_cap_without_resetting_workspace(runtime_store):
    store, settings, seed, workspace = runtime_store
    await store.pool.execute(
        "UPDATE workspaces SET state='paused',limits=limits || $2::jsonb WHERE id=$1",
        workspace,
        '{"max_active_agents":"20","max_agents_total":"100","max_child_spawns_per_agent":"20"}',
    )
    await store.initialize(
        {
            "identity": (await store.get(seed)).model_dump(),
            "team_name": "test",
            "team_id": "team",
            "general_id": "general",
        },
        settings,
        1,
    )
    row = await store.state(seed)
    assert row["workspace_state"] == "paused"
    limits = decode(
        await store.pool.fetchval("SELECT limits FROM workspaces WHERE id=$1", workspace)
    )
    assert int(limits["max_active_agents"]) == 24
    assert "max_agents_total" not in limits and "max_child_spawns_per_agent" not in limits
