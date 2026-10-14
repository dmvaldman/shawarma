import subprocess
from unittest.mock import Mock

import pytest

from harness.hosting import resume
from harness.hosting.deployment import deployment_command, installation_key, runtime_revision


def test_only_runtime_or_configuration_changes_require_installation(tmp_path):
    (tmp_path / "harness").mkdir()
    source = tmp_path / "harness" / "runtime.py"
    source.write_text("version = 1")
    before = runtime_revision(tmp_path)
    (tmp_path / "web").mkdir()
    (tmp_path / "web" / "index.html").write_text("New landing page")
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "README.md").write_text("New documentation")
    runtime_revision.cache_clear()
    assert runtime_revision(tmp_path) == before
    source.write_text("version = 2")
    runtime_revision.cache_clear()
    after = runtime_revision(tmp_path)
    assert after != before
    assert installation_key({"provider": "a"}, before) != installation_key(
        {"provider": "b"}, before
    )
    assert installation_key({"a": "1", "b": "2"}, before) == installation_key(
        {"b": "2", "a": "1"}, before
    )


def test_matching_installation_resumes_without_running_installer(tmp_path):
    app = tmp_path / "app"
    (app / ".state").mkdir(parents=True)
    (app / ".venv/bin").mkdir(parents=True)
    log = tmp_path / "resume.log"
    python = app / ".venv/bin/python"
    python.write_text(f"#!/bin/sh\necho resumed >> '{log}'\n")
    python.chmod(0o700)
    docker = tmp_path / "docker"
    docker.write_text("#!/bin/sh\nexit 0\n")
    docker.chmod(0o700)
    marker = app / ".state/runtime-ready"
    marker.write_text("expected")
    command = deployment_command("exit 99", "expected", str(app))
    subprocess.run(
        ["bash", "-c", command], env={"PATH": str(tmp_path) + ":/usr/bin:/bin"}, check=True
    )
    assert log.read_text().strip() == "resumed"
    # A changed version must run the installer and must not mark failed setup ready.
    failed = deployment_command("exit 99", "new-version", str(app))
    assert subprocess.run(["bash", "-c", failed]).returncode == 99
    assert not marker.exists()
    updated = deployment_command("true", "new-version", str(app))
    subprocess.run(["bash", "-c", updated], check=True)
    assert marker.read_text() == "new-version"


def test_resume_orders_dependencies_and_checks_all_services(monkeypatch):
    operations = []
    monkeypatch.setattr(resume, "start", lambda *names: operations.append(("start", names)))
    monkeypatch.setattr(
        resume, "wait_for", lambda label, check: operations.append(("ready", label))
    )
    resume.main()
    assert operations[0] == ("start", ("agent-company-postgres-1",))
    assert operations[1] == ("ready", "Postgres")
    controller = operations.index(("start", ("swarm-controller",)))
    assert controller > operations.index(("ready", "OpenCode"))
    assert operations.index(("start", ("swarm-service-gateway",))) > operations.index(
        ("ready", "dashboard")
    )
    assert all(
        ("ready", f"service gateway {p}") in operations for p in (18065, 13000, 14096, 18066)
    )


def test_resume_readiness_timeout_is_explicit(monkeypatch):
    monkeypatch.setattr(resume.time, "monotonic", Mock(side_effect=[0, 0, 200]))
    monkeypatch.setattr(resume.time, "sleep", Mock())
    with pytest.raises(RuntimeError, match="data is preserved"):
        resume.wait_for("Mattermost", lambda: False)
