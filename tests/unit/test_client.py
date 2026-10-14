import json

import httpx
import pytest

from harness.mattermost.client import Client, MattermostError


async def test_agent_authorship_threads_dm_search_and_reactions():
    calls = []

    def handle(request):
        assert request.headers["Authorization"] == "Bearer bot-secret"
        body = json.loads(request.content) if request.content else None
        calls.append((request.method, request.url.path, body))
        if request.url.path.endswith("users/me"):
            result = {"id": "bot"}
        elif request.url.path.endswith("channels/direct"):
            assert body == ["bot", "owner"]
            result = {"id": "dm"}
        elif request.url.path.endswith("posts") and request.method == "POST":
            result = {"id": "post", "user_id": "bot", "create_at": 1, **body}
        elif request.url.path.endswith("search") or request.url.path.endswith("thread"):
            result = {"order": [], "posts": {}}
        else:
            result = {"status": "OK"}
        return httpx.Response(200, json=result)

    async with Client(
        "https://mm.test", "bot-secret", transport=httpx.MockTransport(handle)
    ) as bot:
        post = await bot.post_channel("general", "reply", "root")
        assert post.user_id == "bot" and post.root_id == "root"
        assert (await bot.send_dm("owner", "DM")).channel_id == "dm"
        await bot.get_thread("root")
        await bot.search_messages("team", '"payment failed" in:general')
        await bot.add_reaction("post", "thumbsup")
        await bot.remove_reaction("post", "thumbsup")
    search = next(call for call in calls if call[1].endswith("search"))
    assert search[1] == "/api/v4/teams/team/posts/search"
    assert search[2] == {"terms": '"payment failed" in:general', "is_or_search": False}


async def test_error_does_not_expose_server_text_or_credentials():
    def handle(request):
        return httpx.Response(403, json={"id": "permission_denied", "message": "SECRET"})

    async with Client("https://mm.test", "SECRET", transport=httpx.MockTransport(handle)) as bot:
        with pytest.raises(MattermostError, match="permission_denied") as error:
            await bot.me()
        assert "SECRET" not in str(error.value)
