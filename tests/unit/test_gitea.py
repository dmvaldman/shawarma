import json

import httpx
import pytest

from harness.config import Settings
from harness.tools.gitea import repository_path, request_repository


@pytest.mark.parametrize(
    "path",
    [
        "https://outside/",
        "//outside/x",
        "/../other",
        "/%2e%2e/other",
        "/%252e%252e/other",
        "/x\\..\\other",
        "/issues#fragment",
    ],
)
def test_gitea_rejects_cross_repository_paths(path):
    with pytest.raises(ValueError, match="GITEA_PATH"):
        repository_path(path)


async def test_repository_api_auth_is_hidden_and_redirects_are_not_followed(monkeypatch):
    settings = Settings(
        _env_file=None,
        mattermost_url="http://mm",
        database_url="postgres://db",
        gitea_url="http://gitea",
        gitea_token="hidden-token",
        gitea_owner="swarm",
        gitea_repo="workspace",
    )
    requests = []
    client_class = httpx.AsyncClient

    def handle(request):
        requests.append(request)
        assert request.headers["Authorization"] == "token hidden-token"
        assert request.url.path == "/api/v1/repos/swarm/workspace/issues"
        if len(requests) == 1:
            assert json.loads(request.content) == {"title": "Task"}
            return httpx.Response(201, json={"number": 1})
        return httpx.Response(302, headers={"Location": "https://outside/"})

    monkeypatch.setattr(
        "harness.tools.gitea.httpx.AsyncClient",
        lambda **kwargs: client_class(**kwargs, transport=httpx.MockTransport(handle)),
    )
    created = await request_repository(settings, "POST", "/issues", '{"title":"Task"}')
    assert created["data"]["number"] == 1
    rejected = await request_repository(settings, "GET", "/issues")
    assert rejected["status"] == 302
    assert "hidden-token" not in json.dumps([created, rejected])
    assert len(requests) == 2


async def test_agent_api_uses_its_token_instead_of_shared_owner_token(monkeypatch):
    settings = Settings(gitea_token="owner-secret")
    client_class = httpx.AsyncClient

    def handle(request):
        assert request.headers["Authorization"] == "token agent-secret"
        return httpx.Response(201, json={"number": 1})

    monkeypatch.setattr(
        "harness.tools.gitea.httpx.AsyncClient",
        lambda **kwargs: client_class(**kwargs, transport=httpx.MockTransport(handle)),
    )
    result = await request_repository(
        settings, "POST", "/issues", '{"title":"Task"}', token="agent-secret"
    )
    assert result["status"] == 201
    assert "secret" not in json.dumps(result)
