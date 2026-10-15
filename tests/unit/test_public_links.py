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
