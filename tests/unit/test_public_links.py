from harness.config import Settings
from harness.core.links import public_html_urls, public_service_links, service_link_policy


def settings():
    return Settings(
        _env_file=None,
        database_url="unused",
        mattermost_url="http://localhost:8065",
        gitea_public_url="https://git.example.test",
        mattermost_public_url="https://chat.example.test",
    )


def test_shared_links_preserve_paths_queries_and_markdown():
    text = "PR [#10](http://127.0.0.1:3000/swarm/collatz/pulls/10) and <http://localhost:8065/team/channels/work?x=1#post>."
    assert public_service_links(text, settings()) == (
        "PR [#10](https://git.example.test/swarm/collatz/pulls/10) and "
        "<https://chat.example.test/team/channels/work?x=1#post>."
    )
    assert (
        public_service_links("http://gitea:3000/swarm/r", settings())
        == "https://git.example.test/swarm/r"
    )
    for unrelated in [
        "http://127.0.0.1:30001/x",
        "https://other.test/x",
        "http://localhost:9999/x",
    ]:
        assert public_service_links(unrelated, settings()) == unrelated


def test_gitea_html_links_are_public_without_changing_clone_urls_or_content():
    data = {
        "html_url": "http://localhost:3000/swarm/r/pulls/10",
        "clone_url": "http://gitea:3000/swarm/r.git",
        "body": "http://localhost:3000/example",
        "reviews": [{"html_url": "http://localhost:3000/swarm/r/pulls/10#review"}],
    }
    converted = public_html_urls(data, settings())
    assert converted["html_url"] == "https://git.example.test/swarm/r/pulls/10"
    assert converted["clone_url"] == data["clone_url"]
    assert converted["body"] == data["body"]
    assert converted["reviews"][0]["html_url"].endswith("/pulls/10#review")
    assert "https://git.example.test" in service_link_policy(settings())


def test_local_setup_keeps_local_links():
    local = Settings(_env_file=None, database_url="unused", mattermost_url="http://localhost:8065")
    url = "http://127.0.0.1:3000/swarm/r/pulls/10"
    assert public_service_links(url, local) == url
    assert service_link_policy(local) == ""


def test_hosted_links_go_through_portal_with_exact_destination_and_no_credentials():
    from urllib.parse import parse_qs, urlsplit

    hosted = settings().model_copy(
        update={
            "gitea_public_url": "https://abc123-13000.agent37.app",
            "mattermost_public_url": "https://abc123-18065.agent37.app",
        }
    )
    for origin in ("http://127.0.0.1:3000", "https://abc123-13000.agent37.app"):
        path = "/swarm/collatz/pulls/10?tab=files#review"
        link = public_service_links(origin + path, hosted)
        assert link.startswith("https://swarm.forum/#")
        assert parse_qs(urlsplit(link).fragment) == {
            "open": ["gitea"],
            "instance": ["abc123"],
            "path": [path],
        }
        assert public_service_links(link, hosted) == link
    other = "https://another-13000.agent37.app/private"
    assert public_service_links(other, hosted) == other
    text = "[PR](https://abc123-13000.agent37.app/swarm/r/pulls/10) next"
    assert public_service_links(text, hosted).endswith("path=%2Fswarm%2Fr%2Fpulls%2F10) next")
