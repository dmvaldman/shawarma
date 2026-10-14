"""Town Square intake: seed by default, peers on explicit mentions or broadcasts."""

import re


def addressed(message: str, username: str) -> bool:
    return any(
        re.search(r"(?<![\w])@" + re.escape(name) + r"(?![\w-])", message)
        for name in (username, "all", "channel", "here")
    )
