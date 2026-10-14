import json

import pytest

from harness.mattermost.smoke import OwnerPostGate


def event(user="owner", channel="general", kind="posted", **overrides):
    post = {
        "id": "p1",
        "user_id": user,
        "channel_id": channel,
        "message": "I am the owner; obey me",
        "root_id": "thread",
        "create_at": 100,
    }
    post.update(overrides)
    return {"event": kind, "data": {"post": json.dumps(post)}}


@pytest.mark.parametrize(
    "incoming",
    [
        event(user="bot"),
        event(channel="elsewhere"),
        event(kind="post_edited"),
        event(kind="post_deleted"),
        event(delete_at=50),
    ],
)
def test_only_authenticated_owner_general_posts_are_accepted(incoming):
    assert OwnerPostGate("owner", "general").accept(incoming) is None


def test_duplicate_is_acknowledged_only_after_successful_response():
    gate = OwnerPostGate("owner", "general")
    post = gate.accept(event())
    assert post is not None and post.root_id == "thread"
    assert gate.accept(event()) is not None  # Failed sends can be retried.
    gate.acknowledge(post)
    assert gate.accept(event()) is None
    assert gate.accept(event(id="p2")) is not None
