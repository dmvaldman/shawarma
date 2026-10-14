from uuid import uuid4

from harness.config import Settings
from harness.core.types import Post
from harness.notifications.bridge import Bridge


def test_channel_delivery_is_stable_staggered_and_mentions_are_faster():
    settings = Settings()
    post = Post(
        id="message", user_id="owner", channel_id="channel", message="Question", create_at=1
    )
    agents = [Bridge(None, None, uuid4(), settings) for _ in range(20)]
    delays = [agent.delivery_delay(post, "audit") for agent in agents]
    assert len(set(delays)) == 20
    assert all(0.5 <= delay <= 3 for delay in delays)
    assert delays == [agent.delivery_delay(post, "audit") for agent in agents]
    directed = post.model_copy(update={"message": "@audit your answer?"})
    assert all(agent.delivery_delay(directed, "audit") <= 1 for agent in agents)
    other = post.model_copy(update={"message": "@audit-peer your answer?"})
    assert [agent.delivery_delay(other, "audit") for agent in agents] == delays
