import asyncio
import json
import os
import time
from decimal import Decimal

import asyncpg
import pytest

from harness.core.runtime import Runtime
from harness.core.types import ModelResponse, Post, Usage
from harness.mattermost.client import Client
from harness.persistence.store import QuotaError, Store

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("HARNESS_LIVE_TESTS") != "1", reason="live harness PostgreSQL gate"
    ),
]


def post(name="p1", sender="owner", channel="general"):
    return Post(id=name, user_id=sender, channel_id=channel, message="hello", create_at=1000)


async def test_dedup_chronological_batch_ack_preview_away_and_restart(runtime_store):
    store, settings, agent, workspace = runtime_store
    settings.mattermost_owner_id = "owner"
    await store.pool.execute("UPDATE workspaces SET owner_id='owner' WHERE id=$1", workspace)
    assert await store.ingest(agent, post(), "channel_post", "s1")
    assert not await store.ingest(agent, post(), "channel_post", "s1")
    assert not await store.ingest(agent, post("self", "bot"), "channel_post", "s1")
    assert (await store.state(agent))["started"]
    assert await store.ingest(agent, post("p2"), "dm", "s1")
    await store.set_availability(agent, "away")
    preview, cursor = await store.page(agent, 1)
    assert preview[0].post.id == "p1" and await store.unread_count(agent) == 2
    assert (await store.page(agent, 1, cursor))[0][0].post.id == "p2"
    batch = await store.reserve(agent, 1)
    assert batch[0].post.id == "p1"
    await store.recover(agent)
    assert (await store.page(agent, 1))[0][0].id == preview[0].id
    assert await store.unread_count(agent) == 2
    assert (await store.state(agent))["availability"] == "away"
    await store.acknowledge([preview[0].id])
    assert await store.unread_count(agent) == 1
    await store.set_availability(agent, "available")
    assert (await store.state(agent))["availability"] == "available"
    assert (await store.get(agent)).system_prompt == (await store.get(agent)).system_prompt


async def test_database_enforces_prompt_subscription_and_audit_invariants(runtime_store):
    store, _, agent, workspace = runtime_store
    with pytest.raises(asyncpg.RaiseError, match="immutable"):
        await store.pool.execute("UPDATE agents SET identity='{}' WHERE id=$1", agent)
    with pytest.raises(asyncpg.RaiseError, match="mandatory"):
        await store.pool.execute("UPDATE subscriptions SET mode='none' WHERE agent_id=$1", agent)
    await store.pool.execute(
        "INSERT INTO events(workspace_id,type,data) VALUES($1,'test','{}')", workspace
    )
    with pytest.raises(asyncpg.RaiseError, match="append-only"):
        await store.pool.execute("DELETE FROM events")
    original = (await store.get(agent)).system_prompt
    await store.pool.execute(
        "INSERT INTO agent_prompt_versions(agent_id,revision,system_prompt) "
        "VALUES($1,'owner-test',$2)",
        agent,
        "# Worker\n\n## Communication Etiquette\nUse channel messages.",
    )
    assert "Communication Etiquette" in await store.effective_prompt(agent)
    assert (await store.get(agent)).system_prompt == original
    with pytest.raises(asyncpg.RaiseError, match="append-only"):
        await store.pool.execute("UPDATE agent_prompt_versions SET system_prompt='changed'")


async def test_atomic_usage_reservations_without_budget_and_crash_unknown_charge(runtime_store):
    store, _, agent, workspace = runtime_store
    await store.pool.execute("UPDATE agents SET budget=1 WHERE id=$1", agent)
    calls = await asyncio.gather(
        *(store.reserve_cost(agent, Decimal("0.6"), []) for _ in range(2)),
        return_exceptions=True,
    )
    assert all(not isinstance(result, Exception) for result in calls)
    assert (await store.state(agent))["reserved"] == Decimal("1.2")
    await store.recover(agent)
    assert (await store.state(agent))["reserved"] == 0
    assert (await store.state(agent))["spent"] == Decimal("1.2")
    await store.stop(workspace)
    with pytest.raises(QuotaError):
        await store.reserve_cost(agent, Decimal("0.001"), [])


async def test_cache_usage_survives_settlement_and_duplicate_finish(runtime_store):
    store, _, agent, _ = runtime_store
    raw_usage = {
        "input_tokens": 10,
        "output_tokens": 5,
        "cache_read_input_tokens": 9000,
        "cache_creation_input_tokens": 100,
        "cache_creation": {"ephemeral_5m_input_tokens": 100, "ephemeral_1h_input_tokens": 0},
    }
    usage = Usage(
        input_tokens=9110,
        output_tokens=5,
        cost_usd=Decimal("0.001"),
        provider_usage=raw_usage,
    )
    response = ModelResponse(message={"id": "cached", "output": []}, usage=usage)
    turn = await store.reserve_cost(agent, Decimal("0.01"), [])
    await store.finish(turn, usage, response)
    await store.finish(turn, None)
    stored = await store.pool.fetchrow("SELECT * FROM agent_turns WHERE id=$1", turn)
    assert json.loads(stored["provider_usage"]) == raw_usage
    assert stored["input_tokens"] == 9110 and stored["state"] == "completed"
    search = await store.reserve_cost(
        agent, Decimal("0.01"), [{"web_search": "query"}], provider_model_id="openai:search-model"
    )
    await store.finish(search, usage)
    search_record = await store.pool.fetchrow("SELECT * FROM agent_turns WHERE id=$1", search)
    assert search_record["state"] == "completed"
    assert search_record["provider_model_id"] == "openai:search-model"


async def test_edit_and_delete_do_not_reintroduce_obsolete_instructions(runtime_store):
    store, _, agent, _ = runtime_store
    await store.ingest(agent, post(), "channel_post", "s")
    edited = post().model_copy(update={"message": "new version", "update_at": 1100})
    await store.ingest(agent, edited, "post_edited", "s")
    assert (await store.page(agent))[0][0].post.message == "new version"
    deleted = edited.model_copy(update={"delete_at": 1200})
    await store.ingest(agent, deleted, "post_deleted", "s")
    assert all(n.post.message != "hello" for n in (await store.page(agent))[0])
    assert "deleted" in (await store.page(agent))[0][0].post.message


class BlockingProvider:
    def __init__(self):
        self.calls = 0
        self.active = 0
        self.peak = 0
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.requests = []

    async def generate(self, messages, tools, model_id, max_tokens, timeout):  # noqa: ASYNC109
        self.calls += 1
        self.active += 1
        self.peak = max(self.peak, self.active)
        self.requests.append(json.loads(json.dumps(messages)))
        self.entered.set()
        await self.release.wait()
        self.active -= 1
        return ModelResponse(
            message={
                "id": "mock",
                "output": [
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "done"}],
                    }
                ],
            },
            usage=Usage(input_tokens=100, output_tokens=10, cost_usd=Decimal("0.0001")),
        )


async def test_away_no_wake_and_safe_boundary_single_activation(runtime_store):
    store, settings, agent, workspace = runtime_store
    await store.pool.execute("UPDATE workspaces SET started=true WHERE id=$1", workspace)
    provider = BlockingProvider()
    async with Client("https://unused.test", "no-secret") as bot:
        runtime = Runtime(store, bot, agent, settings, provider)
        await store.set_availability(agent, "away")
        await store.ingest(agent, post(), "channel_post", "s")
        await runtime.wake(agent)
        assert provider.calls == 0 and await store.unread_count(agent) == 1
        await store.set_availability(agent, "available")
        await asyncio.gather(*(runtime.wake(agent) for _ in range(10)))
        await asyncio.wait_for(provider.entered.wait(), 2)
        await store.ingest(agent, post("p2"), "dm", "s")
        await runtime.wake(agent)
        assert provider.calls == 1 and "p2" not in json.dumps(provider.requests[0])
        provider.release.set()
        deadline = time.monotonic() + 3
        while await store.unread_count(agent) and time.monotonic() < deadline:  # noqa: ASYNC110
            await asyncio.sleep(0.05)
        assert await store.unread_count(agent) == 0
        assert provider.peak == 1 and provider.calls == 2
        assert "p2" in json.dumps(provider.requests[1])
        runtime.stopping.set()
        if runtime.task:
            await runtime.task
        recovered = await store.get(agent)
        assert recovered.system_prompt == (await store.get(agent)).system_prompt
        assert len(await store.history(agent)) == 4


async def test_prompt_and_unread_recover_through_new_database_connection(runtime_store):
    store, settings, agent, _ = runtime_store
    schema = await store.pool.fetchval("SELECT current_schema()")
    original = await store.get(agent)
    await store.ingest(agent, post(), "dm", "s")
    await store.reserve(agent, 1)
    pool = await asyncpg.create_pool(
        settings.database_url.replace("postgresql+asyncpg://", "postgresql://"),
        min_size=1,
        max_size=2,
        server_settings={"search_path": schema},
    )
    restarted = Store(pool)
    try:
        await restarted.recover(agent)
        assert (await restarted.get(agent)).model_dump() == original.model_dump()
        assert (await restarted.page(agent))[0][0].post.id == "p1"
        assert await restarted.unread_count(agent) == 1
    finally:
        await restarted.close()


async def test_unicode_notification_storm_uses_batches_without_discarding_history(runtime_store):
    store, settings, agent, workspace = runtime_store
    await store.pool.execute("UPDATE workspaces SET started=true WHERE id=$1", workspace)
    for index in range(20):
        item = post(f"storm-{index}").model_copy(update={"message": "🌍" * 1200})
        await store.ingest(agent, item, "channel_post", "s")
    provider = BlockingProvider()
    provider.release.set()
    async with Client("https://unused.test", "no-secret") as bot:
        runtime = Runtime(store, bot, agent, settings, provider)
        await runtime.wake(agent)
        async with asyncio.timeout(5):
            while await store.unread_count(agent):  # noqa: ASYNC110 - external scheduler state
                await asyncio.sleep(0.05)
        runtime.stopping.set()
        if runtime.task:
            await runtime.task
        assert provider.peak == 1
        assert provider.calls > 1
        assert len(json.dumps(provider.requests[-1], ensure_ascii=False).encode()) > 32768
        delivered = [
            n
            for request in provider.requests
            for message in request
            if message.get("role") == "user" and message.get("content", "").startswith('{"source"')
            for n in json.loads(message["content"])["notifications"]
        ]
        assert {n["post"]["id"] for n in delivered} == {f"storm-{i}" for i in range(20)}


async def test_background_work_is_away_without_typing(runtime_store):
    from unittest.mock import AsyncMock

    store, settings, agent, workspace = runtime_store
    await store.pool.execute("UPDATE workspaces SET started=true WHERE id=$1", workspace)
    provider = BlockingProvider()
    async with Client("https://unused.test", "no-secret") as bot:
        bot.typing = AsyncMock(return_value=True)
        bot.presence = AsyncMock()
        runtime = Runtime(store, bot, agent, settings, provider)
        await store.ingest(agent, post(), "channel_post", "s")
        await runtime.wake(agent)
        await asyncio.wait_for(provider.entered.wait(), 2)
        await asyncio.sleep(0.05)
        bot.typing.assert_not_called()
        assert (await store.state(agent))["availability"] == "away"
        bot.presence.assert_awaited_with("away")
        provider.release.set()
        await asyncio.wait_for(runtime.task, 2)
        runtime.stopping.set()
        count = bot.typing.await_count
        await asyncio.sleep(0.05)
        assert bot.typing.await_count == count
        assert runtime.typing_channel == ""
        assert (await store.state(agent))["state"] == "READY"


async def test_delayed_delivery_is_durable_chronological_and_explicitly_readable(runtime_store):
    store, settings, agent, workspace = runtime_store
    await store.ingest(agent, post("slow"), "channel_post", "s", delay_sec=3600)
    await store.ingest(agent, post("fast"), "channel_post", "s")
    assert await store.reserve(agent, 10) == []
    items, _ = await store.page(agent)
    assert [n.post.id for n in items] == ["slow", "fast"]
    await store.recover(agent)
    delay, channel = await store.pending_delivery(agent)
    assert delay > 0 and channel == "general"
    await store.pool.execute("UPDATE notifications SET available_at=now() WHERE agent_id=$1", agent)
    assert [n.post.id for n in await store.reserve(agent, 10)] == ["slow", "fast"]


async def test_typing_yield_is_bounded_and_includes_intervening_reply(runtime_store):
    store, settings, agent, workspace = runtime_store
    settings = settings.model_copy(update={"typing_yield_max_sec": 0.1})
    await store.pool.execute("UPDATE workspaces SET started=true WHERE id=$1", workspace)
    provider = BlockingProvider()
    async with Client("https://unused.test", "no-secret") as bot:
        bot.typing_until[("general", "peer")] = asyncio.get_running_loop().time() + 10
        runtime = Runtime(store, bot, agent, settings, provider)
        await store.ingest(agent, post("question"), "channel_post", "s")
        await runtime.wake(agent)
        await asyncio.sleep(0.02)
        assert provider.calls == 0
        await store.ingest(agent, post("peer-reply"), "channel_post", "s")
        await runtime.wake(agent)
        await asyncio.wait_for(provider.entered.wait(), 1)
        assert provider.calls == 1
        content = str(provider.requests[0])
        assert "question" in content and "peer-reply" in content and "typing_users" in content
        provider.release.set()
        await asyncio.wait_for(runtime.task, 2)
        runtime.stopping.set()


async def test_post_rechecks_new_messages_before_sending_a_stale_reply(runtime_store):
    from unittest.mock import AsyncMock

    from harness.tools.core import CoreTools

    store, settings, agent, workspace = runtime_store
    async with Client("https://unused.test", "no-secret") as bot:
        original = post("question").model_copy(update={"create_at": 1000})
        reply = post("peer-reply").model_copy(update={"create_at": 2000})
        bot.request = AsyncMock(return_value={"team_id": "team", "type": "O"})
        bot.read_channel = AsyncMock(return_value=[reply, original])
        later = reply.model_copy(update={"id": "another-peer", "create_at": 2500})
        bot.read_channel.side_effect = [[reply, original], [later, reply, original]]
        bot.post_channel = AsyncMock(
            return_value=post("sent").model_copy(update={"create_at": 3000})
        )
        tools = CoreTools(store, bot, agent, settings)
        tools.observe_posts([original])
        turn = await store.reserve_cost(agent, Decimal("0.01"), [])
        args = {"channel_id": "general", "text": "My reply"}
        await tools.execute(
            turn, {"call_id": "before", "name": "post_channel", "arguments": json.dumps(args)}
        )
        bot.post_channel.assert_not_awaited()
        result = await store.pool.fetchval("SELECT result FROM tool_calls WHERE call_id='before'")
        assert json.loads(result)["deferred"] is True
        await tools.execute(
            turn, {"call_id": "after", "name": "post_channel", "arguments": json.dumps(args)}
        )
        bot.post_channel.assert_awaited_once()
        delivered = json.loads(
            await store.pool.fetchval("SELECT result FROM tool_calls WHERE call_id='after'")
        )
        assert delivered["new_posts_since_reassessment"][0]["id"] == "another-peer"


async def test_tool_deadline_returns_result_instead_of_killing_worker(runtime_store, monkeypatch):
    store, settings, agent, _ = runtime_store
    settings = settings.model_copy(update={"tool_timeout_sec": 0.02})

    async def stalled_fetch(url):
        await asyncio.sleep(10)

    monkeypatch.setattr("harness.tools.core.fetch_page", stalled_fetch)

    class Provider:
        calls = 0

        async def generate(self, *args):
            self.calls += 1
            return ModelResponse(
                message={
                    "output": [
                        {
                            "type": "function_call",
                            "call_id": "deadline",
                            "name": "web_fetch",
                            "arguments": '{"url":"https://timeout-test.example"}',
                        }
                    ]
                    if self.calls == 1
                    else []
                },
                usage=Usage(input_tokens=1, output_tokens=1, cost_usd=Decimal("0.001")),
            )

    provider = Provider()
    async with Client("https://unused.test", "token") as bot:
        runtime = Runtime(store, bot, agent, settings, provider)
        await runtime.activate()
        runtime.stopping.set()
    assert provider.calls == 2
    assert (await store.state(agent))["state"] == "READY"
    result = await store.pool.fetchval("SELECT result FROM tool_calls WHERE call_id='deadline'")
    assert json.loads(result)["error"] == "TOOL_TIMEOUT"


async def test_owner_pause_during_channel_read_still_blocks_post(runtime_store):
    from unittest.mock import AsyncMock

    from harness.tools.core import CoreTools

    store, settings, agent, workspace = runtime_store
    async with Client("https://unused.test", "token") as bot:
        bot.request = AsyncMock(return_value={"team_id": "team", "type": "O"})

        async def read(*args):
            await store.pool.execute("UPDATE workspaces SET state='paused' WHERE id=$1", workspace)
            return []

        bot.read_channel = AsyncMock(side_effect=read)
        bot.post_channel = AsyncMock()
        tools = CoreTools(store, bot, agent, settings)
        turn = await store.reserve_cost(agent, Decimal("0.01"), [])
        await tools.execute(
            turn,
            {
                "call_id": "paused-post",
                "name": "post_channel",
                "arguments": '{"channel_id":"general","text":"A finding"}',
            },
        )
        bot.post_channel.assert_not_awaited()
        result = json.loads(
            await store.pool.fetchval("SELECT result FROM tool_calls WHERE call_id='paused-post'")
        )
        assert result["detail"] == "Owner controls block posting"


@pytest.mark.parametrize("failures", [1, 2])
@pytest.mark.parametrize("error_kind", ["timeout", "invalid_prompt"])
async def test_model_timeout_retries_once_then_recovers_or_reports_blocked(
    runtime_store, caplog, failures, error_kind
):
    store, settings, agent, workspace = runtime_store

    class Provider:
        calls = 0

        async def generate(self, *args):
            self.calls += 1
            if self.calls <= failures:
                if error_kind == "invalid_prompt":
                    from harness.models.openai import ProviderError

                    raise ProviderError(
                        "OpenAI HTTP 400: invalid_prompt", status=400, code="invalid_prompt"
                    )
                raise TimeoutError("private request content")
            return ModelResponse(
                message={"output": []},
                usage=Usage(input_tokens=3, output_tokens=1, cost_usd=Decimal(".001")),
            )

    provider = Provider()
    async with Client("https://unused.test", "token") as bot:
        runtime = Runtime(store, bot, agent, settings, provider)
        await runtime.activate()
        runtime.stopping.set()
    assert provider.calls == 2
    state = await store.state(agent)
    assert state["state"] == ("READY" if failures == 1 else "QUOTA_BLOCKED")
    rows = await store.pool.fetch("SELECT state FROM agent_turns WHERE agent_id=$1", agent)
    assert len(rows) == 2
    assert sum(r["state"] == "unknown" for r in rows) == failures
    assert "retrying once" in caplog.text
    if failures == 2:
        assert "execution stopped" in caplog.text
        failure = json.loads(state["failure"])
        assert "private request content" not in failure["reason"]
        assert failure["category"] == "provider_blocked"
        # Wake-up messages cannot override a provider block.
        await runtime.wake(agent)
        assert provider.calls == 2
        await store.slash_control(
            workspace, settings.mattermost_owner_id, "general", "/continue", "retry-timeout"
        )
        assert (await store.state(agent))["state"] == "READY"
        assert (await store.state(agent))["failure"] is None


@pytest.mark.parametrize("policy", [False, True])
async def test_provider_billing_block_is_not_retried_and_reason_is_durable(
    runtime_store, caplog, policy
):
    from harness.models.openai import ProviderError

    store, settings, agent, _ = runtime_store

    class Provider:
        calls = 0

        async def generate(self, *args):
            self.calls += 1
            raise ProviderError(
                "Provider rejected request",
                status=400 if policy else 429,
                code="invalid_prompt" if policy else "billing_or_usage_limit",
                policy_rejected=policy,
            )

    provider = Provider()
    async with Client("https://unused.test", "token") as bot:
        runtime = Runtime(store, bot, agent, settings, provider)
        await runtime.activate()
        runtime.stopping.set()
    assert provider.calls == 1
    state = await store.state(agent)
    assert state["state"] == "QUOTA_BLOCKED"
    assert json.loads(state["failure"])["reason"] == "Provider rejected request"
    assert "execution stopped" in caplog.text


async def test_owner_pause_cancels_timeout_retry(runtime_store):
    store, settings, agent, workspace = runtime_store
    interrupted = asyncio.Event()

    class Provider:
        calls = 0

        async def generate(self, *args):
            self.calls += 1
            interrupted.set()
            raise TimeoutError()

    provider = Provider()
    async with Client("https://unused.test", "token") as bot:
        runtime = Runtime(store, bot, agent, settings, provider)
        runtime.task = asyncio.create_task(runtime.activate())
        await interrupted.wait()
        await store.pool.execute("UPDATE workspaces SET state='paused' WHERE id=$1", workspace)
        await runtime.pause()
        runtime.stopping.set()
    assert provider.calls == 1
    assert (await store.state(agent))["state"] == "READY"
    assert (await store.state(agent))["failure"] is None


async def test_unexpected_harness_error_is_visible_and_durable(runtime_store, caplog):
    store, settings, agent, _ = runtime_store

    class Provider:
        async def generate(self, *args):
            raise ValueError("private payload")

    async with Client("https://unused.test", "token") as bot:
        runtime = Runtime(store, bot, agent, settings, Provider())
        await runtime.activate()
        runtime.stopping.set()
    state = await store.state(agent)
    assert state["state"] == "FAILED"
    failure = json.loads(state["failure"])
    assert failure["category"] == "harness_error"
    assert "ValueError" in failure["reason"]
    assert "private payload" not in failure["reason"]
    assert "execution stopped" in caplog.text
