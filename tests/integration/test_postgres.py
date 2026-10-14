"""Verify first-boot database separation on an isolated, disposable container."""

import os
import subprocess
import time
from pathlib import Path
from uuid import uuid4

import pytest

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(os.environ.get("HARNESS_LIVE_TESTS") != "1", reason="live Docker gate"),
]


def test_fresh_postgres_initializes_separate_harness_database():
    name = "agent-company-init-" + uuid4().hex[:10]
    script = Path("infra/init-databases.sql").resolve()
    subprocess.run(
        [
            "docker",
            "run",
            "-d",
            "--name",
            name,
            "--tmpfs",
            "/var/lib/postgresql/data",
            "-e",
            "POSTGRES_USER=mattermost",
            "-e",
            "POSTGRES_DB=mattermost",
            "-e",
            "POSTGRES_PASSWORD=isolated-init-test",
            "-e",
            "HARNESS_DB_PASSWORD=isolated-harness-test",
            "-v",
            f"{script}:/docker-entrypoint-initdb.d/10-harness.sql:ro",
            "postgres:16.10-alpine",
        ],
        check=True,
        capture_output=True,
        timeout=30,
    )
    try:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            ready = subprocess.run(
                [
                    "docker",
                    "exec",
                    name,
                    "psql",
                    "-U",
                    "mattermost",
                    "-d",
                    "mattermost",
                    "-tAc",
                    "SELECT datname FROM pg_database WHERE datname='harness'",
                ],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if ready.returncode == 0 and ready.stdout.strip() == "harness":
                break
            time.sleep(0.5)
        else:
            raise AssertionError("fresh initialization did not create the harness database")
        # Test password-authenticated role access, not just the superuser view.
        query = subprocess.run(
            [
                "docker",
                "exec",
                "-e",
                "PGPASSWORD=isolated-harness-test",
                name,
                "psql",
                "-h",
                "127.0.0.1",
                "-U",
                "harness",
                "-d",
                "harness",
                "-tAc",
                "SELECT current_user, current_database()",
            ],
            capture_output=True,
            text=True,
            check=True,
            timeout=5,
        )
        assert query.stdout.strip() == "harness|harness"
    finally:
        subprocess.run(
            ["docker", "rm", "-f", "-v", name], check=True, capture_output=True, timeout=10
        )
