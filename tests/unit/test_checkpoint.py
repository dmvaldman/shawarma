import stat

import pytest

from harness.persistence.bootstrap import load, locked, save


def test_checkpoint_is_private_atomic_and_exclusive(tmp_path):
    path = tmp_path / "private" / "workspace.json"
    save(path, {"seed": "stable", "token": "secret"})
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    with locked(path):
        with pytest.raises(RuntimeError, match="another"):
            with locked(path):
                pass
    save(path, {"seed": "stable", "token": "new"})
    assert load(path) == {"seed": "stable", "token": "new"}
    assert not list(path.parent.glob(".checkpoint-*"))
