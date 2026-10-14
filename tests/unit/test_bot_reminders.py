from unittest.mock import AsyncMock

import pytest

from harness.mattermost.bot_reminders import REMINDER_TYPE, remove_setup_reminder


def post(pid, kind=REMINDER_TYPE, user="bot", deleted=0):
    return {"id": pid, "type": kind, "user_id": user, "delete_at": deleted}


def page(posts):
    return {"order": [p["id"] for p in posts], "posts": {p["id"]: p for p in posts}}


def client(pages):
    async def request(method, path, **kwargs):
        if path == "users/owner/channels":
            return [
                {"id": "dm", "type": "D", "name": "bot__owner"},
                {"id": "other", "type": "D", "name": "other__owner"},
                {"id": "public", "type": "O", "name": "bot__owner"},
            ]
        if path == "channels/dm/posts":
            return page(pages[kwargs["params"]["page"]])
        return {"status": "OK"}

    bot = AsyncMock()
    bot.request.side_effect = request
    return bot


async def test_removes_only_generated_reminder_and_hides_empty_dm():
    admin = client([[post("reminder"), post("already-deleted", deleted=1)]])
    assert await remove_setup_reminder(admin, "bot", "owner") == 1
    admin.request.assert_any_await("DELETE", "posts/reminder")
    admin.request.assert_any_await(
        "PUT",
        "users/owner/preferences",
        json=[
            {"user_id": "owner", "category": "direct_channel_show", "name": "bot", "value": "false"}
        ],
    )
    assert not any("other" in c.args[1] for c in admin.request.await_args_list)


@pytest.mark.parametrize(
    "conversation",
    [post("reply", ""), post("system", "system_join"), post("someone-else", user="other")],
)
async def test_preserves_conversation_and_dm_visibility(conversation):
    admin = client([[post("reminder"), conversation]])
    assert await remove_setup_reminder(admin, "bot", "owner") == 1
    deletes = [c.args[1] for c in admin.request.await_args_list if c.args[0] == "DELETE"]
    assert deletes == ["posts/reminder"]
    assert not any(c.args[0] == "PUT" for c in admin.request.await_args_list)


async def test_reads_older_pages_before_deciding_dm_is_empty():
    admin = client([[post(str(i)) for i in range(100)], [post("older-reply", "")]])
    assert await remove_setup_reminder(admin, "bot", "owner") == 100
    calls = admin.request.await_args_list
    assert calls[2].kwargs["params"]["page"] == 1
    assert calls[3].args[0] == "DELETE"
    assert not any(c.args[0] == "PUT" for c in calls)


async def test_missing_dm_does_not_create_one():
    admin = AsyncMock()
    admin.request.return_value = []
    assert await remove_setup_reminder(admin, "bot", "owner") == 0
    admin.request.assert_awaited_once_with("GET", "users/owner/channels")
