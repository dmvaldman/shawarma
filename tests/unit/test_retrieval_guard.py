from unittest.mock import AsyncMock

import httpx
import pytest

from harness.tools.retrieval_guard import RetrievalError, RetrievalGuard


async def test_provider_failure_shared_across_queries_then_retries_after_cooldown(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr("harness.tools.retrieval_guard.time.monotonic", lambda: clock[0])
    guard = RetrievalGuard()
    operation = AsyncMock(
        side_effect=RetrievalError(
            "Web search HTTP 429 (insufficient_quota)",
            "insufficient_quota",
            retry_after=3600,
            provider=True,
        )
    )
    first = await guard.run("query-one", "credential-one", operation)
    second = await guard.run("query-two", "credential-one", operation)
    assert first["error"] == "insufficient_quota" and second["cached_error"]
    assert operation.await_count == 1
    await guard.run("query-two", "credential-two", operation)
    assert operation.await_count == 2
    clock[0] += 3601
    await guard.run("query-one", "credential-one", operation)
    assert operation.await_count == 3


@pytest.mark.parametrize(
    "error",
    [
        ValueError("Only HTML and plain-text pages are supported"),
        httpx.ReadTimeout("contains-private-query"),
    ],
)
async def test_bad_source_does_not_repeat_or_block_other_sources(error):
    guard = RetrievalGuard()
    operation = AsyncMock(side_effect=error)
    first = await guard.run("bad-url", "fetch", operation)
    second = await guard.run("bad-url", "fetch", operation)
    assert second["cached_error"] and operation.await_count == 1
    assert "contains-private-query" not in first["detail"]
    good = AsyncMock(return_value={"text": "Evidence"})
    assert await guard.run("other-url", "fetch", good) == {"text": "Evidence"}
