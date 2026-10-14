import os
from uuid import uuid4

import pytest

from harness.config import Settings
from harness.mattermost.client import Client
from harness.mattermost.quiet_bots import quiet_bot

pytestmark = pytest.mark.skipif(os.environ.get("HARNESS_LIVE_TESTS") != "1", reason="live gate")


async def test_quiet_bot_has_no_creator_dm_before_any_reminder_cleanup():
    settings = Settings()
    workspace, agent = uuid4(), uuid4()
    async with Client(
        str(settings.mattermost_url), settings.mattermost_admin_token.get_secret_value()
    ) as admin:
        original = (await admin.request("GET", "config"))["ServiceSettings"][
            "EnableAPIUserDeletion"
        ]
        bot = await quiet_bot(admin, workspace, agent, "quiet-" + agent.hex[:10], "quiet", "test")
        try:
            dm_name = "__".join(sorted((bot["user_id"], settings.mattermost_owner_id)))
            channels = await admin.request("GET", f"users/{settings.mattermost_owner_id}/channels")
            assert not any(c["type"] == "D" and c["name"] == dm_name for c in channels)
            assert bot["owner_id"] == settings.mattermost_owner_id
            user = await admin.request("GET", f"users/{bot['user_id']}")
            assert user["is_bot"]
            retry = await quiet_bot(admin, workspace, agent, user["username"], "quiet", "test")
            assert retry["user_id"] == bot["user_id"]
        finally:
            await admin.request(
                "PUT", "config/patch", json={"ServiceSettings": {"EnableAPIUserDeletion": True}}
            )
            try:
                await admin.request(
                    "DELETE", f"users/{bot['user_id']}", params={"permanent": "true"}
                )
            finally:
                await admin.request(
                    "PUT",
                    "config/patch",
                    json={"ServiceSettings": {"EnableAPIUserDeletion": original}},
                )
