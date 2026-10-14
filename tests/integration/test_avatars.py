import base64
import json
import os
from functools import partial
from unittest.mock import AsyncMock

import httpx
import pytest

from harness.api.analytics import ledger_snapshot
from harness.core.avatars import Avatars, sync_gitea_avatar

pytestmark = pytest.mark.skipif(os.environ.get("HARNESS_LIVE_TESTS") != "1", reason="Postgres gate")


async def test_generate_once_and_reuse_image_for_both_services(runtime_store, monkeypatch):
    store, settings, agent, workspace = runtime_store
    manager = Avatars(store, settings.model_copy(update={"avatars_enabled": True}), workspace)
    image = b"\xff\xd8\xffJPEG-test"
    requests = []

    def api(request):
        requests.append(request)
        if request.url.path.endswith("/images/generations"):
            body = json.loads(request.content)
            assert body["model"] == "gpt-image-2.5-flare"
            assert "specialty" in body["prompt"]
            return httpx.Response(
                200,
                json={
                    "data": [{"b64_json": base64.b64encode(image).decode()}],
                    "usage": {"input_tokens": 20, "output_tokens": 30},
                },
            )
        if request.url.path.endswith("/image"):
            assert image in request.content
            return httpx.Response(200)
        if request.url.path.endswith("/user/avatar"):
            assert base64.b64decode(json.loads(request.content)["image"]) == image
            return httpx.Response(204)
        raise AssertionError(request.url)

    client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", partial(client, transport=httpx.MockTransport(api)))
    from harness.mattermost.client import Client

    monkeypatch.setattr(
        "harness.core.avatars.Client",
        lambda url, token: Client(url, token, transport=httpx.MockTransport(api)),
    )
    await manager.enqueue(agent)
    await manager.enqueue(agent)
    await manager.process(agent)
    await manager.process(agent)
    assert len(requests) == 2
    row = await store.pool.fetchrow("SELECT * FROM agent_avatars WHERE agent_id=$1", agent)
    assert row["image"] == image and row["mattermost_applied"] and not row["gitea_applied"]
    account = {"agent_id": agent, "username": "tester", "password": "private"}
    await sync_gitea_avatar(store, settings, account)
    await sync_gitea_avatar(store, settings, account)
    assert len(requests) == 3
    snapshot = await ledger_snapshot(store, workspace)
    assert snapshot["agents"][0]["avatar_url"].endswith("/avatar")
    assert snapshot["agents"][0]["input_tokens"] == 20
    assert snapshot["agents"][0]["output_tokens"] == 30
    assert "image" not in snapshot["agents"][0]


async def test_uncertain_generation_not_replayed_and_does_not_block_agent(
    runtime_store, monkeypatch
):
    store, settings, agent, workspace = runtime_store
    manager = Avatars(store, settings.model_copy(update={"avatars_enabled": True}), workspace)
    client = httpx.AsyncClient
    requests = []

    def fail(request):
        requests.append(request)
        raise httpx.ReadTimeout("provider timeout", request=request)

    monkeypatch.setattr(httpx, "AsyncClient", partial(client, transport=httpx.MockTransport(fail)))
    await manager.enqueue(agent)
    await manager.process(agent)
    assert len(requests) == 1
    assert (
        await store.pool.fetchval("SELECT state FROM agent_avatars WHERE agent_id=$1", agent)
        == "failed"
    )
    await manager.process(agent)
    assert len(requests) == 1
    assert (await store.state(agent))["state"] not in {"FAILED", "QUOTA_BLOCKED"}
    # Reset/restart recovery must not replay a previously in-flight paid generation either.
    await store.pool.execute("UPDATE agent_avatars SET state='generating' WHERE agent_id=$1", agent)
    monkeypatch.setattr(manager, "run", AsyncMock())
    await manager.start()
    await manager.close()
    assert (
        await store.pool.fetchval("SELECT state FROM agent_avatars WHERE agent_id=$1", agent)
        == "failed"
    )
