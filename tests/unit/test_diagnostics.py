import asyncio
import logging
from unittest.mock import AsyncMock

import httpx

from harness.config import Settings
from harness.core.diagnostics import Diagnostics


def settings():
    return Settings(
        _env_file=None,
        mattermost_url="http://mm.test",
        database_url="postgresql://user:secret@db/harness",
        mattermost_admin_token="private-mm",
        mattermost_owner_id="owner",
        OPENAI_API_KEY="private-openai",
        ANTHROPIC_API_KEY="private-claude",
        DEEPSEEK_API_KEY="private-deepseek",
    )


def transport(posts, fail=False):
    def handle(request):
        path = request.url.path
        if path.endswith("/channels/name/errors"):
            return httpx.Response(200, json={"id": "errors", "type": "O", "name": "errors"})
        if "/members/" in path:
            return httpx.Response(200, json={"user_id": "owner"})
        if path.endswith("/posts"):
            posts.append(request.content.decode())
            if fail:
                return httpx.Response(500, json={"id": "unavailable"})
            return httpx.Response(
                200,
                json={
                    "id": "post",
                    "user_id": "owner",
                    "channel_id": "errors",
                    "message": "log",
                    "create_at": 1000,
                },
            )
        raise AssertionError(path)

    return httpx.MockTransport(handle)


async def reporter(posts, fail=False):
    diag = Diagnostics(settings(), "team")
    await diag.admin.http.aclose()
    diag.admin.http = httpx.AsyncClient(
        base_url="http://mm.test/api/v4/", transport=transport(posts, fail)
    )
    await diag.start()
    return diag


async def test_errors_tracebacks_lifecycle_and_unhandled_async_errors_are_reported_redacted():
    posts = []
    diag = await reporter(posts)
    logger = logging.getLogger("harness.test")
    try:
        logger.info("Harness startup")
        logging.getLogger("aiohttp.server").error("HTTP server failure")
        logging.getLogger("httpx").info("routine transport noise")
        try:
            raise KeyError("removed_catalog_field")
        except KeyError:
            logger.exception(
                "Agent failed: private-mm private-openai private-claude private-deepseek "
                "%s token=child-secret @all",
                diag.settings.database_url,
            )
        diag.loop.call_exception_handler(
            {"exception": RuntimeError("async failed"), "message": "Task exception"}
        )
        await asyncio.sleep(0)
        await asyncio.wait_for(diag.queue.join(), 2)
        assert len(posts) == 4
        text = "\n".join(posts)
        assert "removed_catalog_field" in text and "Traceback" in text
        assert "Unhandled async failure" in text and "Harness startup" in text
        for secret in (
            "private-mm",
            "private-openai",
            "private-claude",
            "private-deepseek",
            "child-secret",
            "postgresql://user:secret@db/harness",
        ):
            assert secret not in text
        assert "@all" not in text
        assert "HTTP server failure" in text and "routine transport noise" not in text
    finally:
        await diag.shutdown()
    assert diag not in logging.getLogger().handlers


async def test_reporting_failure_falls_back_without_recursion(monkeypatch, capsys):
    posts = []
    diag = await reporter(posts, fail=True)
    monkeypatch.setattr("harness.core.diagnostics.asyncio.sleep", AsyncMock())
    try:
        logging.getLogger("harness.test").error("original failure private-mm")
        await asyncio.sleep(0)
        # Patched sleep does not yield; allow the thread-safe enqueue to run.
        await asyncio.get_running_loop().run_in_executor(None, lambda: None)
        await asyncio.wait_for(diag.queue.join(), 2)
        assert len(posts) == 3
        stderr = capsys.readouterr().err
        assert "original failure" in stderr and "private-mm" not in stderr
        assert diag.queue.empty()
    finally:
        await diag.shutdown()
