from unittest.mock import AsyncMock

from harness.mattermost.welcome import PREAMBLE, ensure_welcome
from harness.persistence.bootstrap import load


async def test_welcome_once_and_recovery_after_post_before_checkpoint(tmp_path):
    state = {"general_id": "town", "seed_id": "seed", "bot_user_id": "bot"}
    path = tmp_path / "state.json"
    bot = AsyncMock()
    bot.request.side_effect = [{"posts": {}, "order": []}, {"id": "welcome"}]
    await ensure_welcome(bot, state, path)
    assert bot.request.call_count == 2
    body = bot.request.call_args.kwargs["json"]
    assert body["message"] == PREAMBLE
    assert body["channel_id"] == "town"
    await ensure_welcome(bot, state, path)
    assert bot.request.call_count == 2
    assert load(path)["welcome"]["post_id"] == "welcome"
    # A lost checkpoint must find the seed's existing marked post, not post again.
    state.pop("welcome")
    bot.request.side_effect = [
        {
            "posts": {
                "welcome": {
                    "id": "welcome",
                    "user_id": "bot",
                    "props": {"swarm_welcome_seed_id": "seed"},
                }
            },
            "order": ["welcome"],
        }
    ]
    await ensure_welcome(bot, state, path)
    assert bot.request.call_count == 3
    # Reset changes seed identity and clears the checkpoint welcome entry.
    state = {"general_id": "town", "seed_id": "fresh", "bot_user_id": "fresh-bot"}
    bot.request.side_effect = [{"posts": {}, "order": []}, {"id": "fresh-welcome"}]
    await ensure_welcome(bot, state, path)
    assert state["welcome"]["post_id"] == "fresh-welcome"
