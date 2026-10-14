"""Start a prepared tenant without installing, building, or changing its data."""

import subprocess
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from functools import partial


def wait_for(label: str, check: Callable[[], bool], timeout: int = 180) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if check():
            return
        time.sleep(1)
    raise RuntimeError(f"Resume timed out waiting for {label}; existing data is preserved")


def start(*names: str) -> None:
    subprocess.run(["docker", "start", *names], check=True, timeout=60)


def postgres_ready() -> bool:
    result = subprocess.run(
        ["docker", "exec", "agent-company-postgres-1", "pg_isready", "-U", "mattermost"],
        capture_output=True,
        timeout=10,
    )
    return result.returncode == 0


def http_ready(port: int, path: str = "/", expected: int = 200) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=3) as response:
            return bool(response.status == expected)
    except urllib.error.HTTPError as error:
        return error.code == expected
    except OSError:
        return False


def main() -> None:
    start("agent-company-postgres-1")
    wait_for("Postgres", postgres_ready)
    start(
        "agent-company-mattermost-1",
        "agent-company-command-gateway-1",
        "swarm-coding-gitea-1",
        "swarm-coding-worker",
    )
    wait_for("Mattermost", lambda: http_ready(8065, "/api/v4/system/ping"))
    wait_for("Gitea", lambda: http_ready(3000, "/user/login"))
    # OpenCode's authentication challenge confirms its HTTP server is ready.
    wait_for("OpenCode", lambda: http_ready(4096, "/global/health", 401))
    start("swarm-controller")
    wait_for("dashboard", lambda: http_ready(8066))
    start("swarm-service-gateway")
    for port in (18065, 13000, 14096, 18066):
        wait_for(f"service gateway {port}", partial(http_ready, port, expected=401))
    print("Existing swarm services resumed", flush=True)


if __name__ == "__main__":
    main()
