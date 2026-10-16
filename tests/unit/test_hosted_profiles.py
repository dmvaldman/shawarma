import httpx
import pytest

from harness.hosting.profiles import human_username, mattermost_profile


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("David", "david"),
        ("Jane Smith", "jane-smith"),
        ("José", "jose"),
        ("李", "human"),
        ("A", "a-user"),
        ("System", "system-user"),
    ],
)
def test_human_handle(name, expected):
    assert human_username(name) == expected
    assert len(human_username(name * 30)) <= 22


async def test_profile_keeps_owner_id_and_avoids_agent_handle_collision():
    updates = []

    def handler(request):
        assert request.headers["Authorization"] == "Bearer private-token"
        if request.method == "GET":
            if request.url.path.endswith("/david"):
                return httpx.Response(200, json={"id": "existing-agent"})
            return httpx.Response(404)
        import json

        assert request.url.path == "/api/v4/users/human-id/patch"
        updates.append(json.loads(request.content))
        return httpx.Response(200, json={"id": "human-id", **updates[-1]})

    async with httpx.AsyncClient(
        base_url="http://chat/api/v4/", transport=httpx.MockTransport(handler)
    ) as client:
        username = await mattermost_profile(client, {"id": "human-id"}, "private-token", "David")
    assert username == "david2"
    assert updates == [
        {"username": "david2", "first_name": "David", "last_name": "", "nickname": "David"}
    ]


async def test_existing_human_handle_is_reused():
    def handler(request):
        return httpx.Response(200, json={"id": "human-id"})

    async with httpx.AsyncClient(
        base_url="http://chat/api/v4/", transport=httpx.MockTransport(handler)
    ) as client:
        assert (
            await mattermost_profile(client, {"id": "human-id"}, "token", "Jane Smith")
            == "jane-smith"
        )
