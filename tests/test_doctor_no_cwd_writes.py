"""`velune doctor` must not write into the directory it is run from.

Regression: the writability and SQLite checks created `./.velune/` plus an
empty `velune_cognitive_core.db` in the current directory — stray state in
whatever folder the user happened to be in (re-creating the in-workspace
storage Velune deliberately moved out of cloud-synced folders), and a false
*fail* when run from a read-only directory. They now probe Velune's own
data/config directories with self-cleaning temp files.
"""

from __future__ import annotations

import platformdirs
import pytest

from velune.cli.commands.doctor import _check_sqlite, _check_velune_dir


@pytest.fixture
def isolated_dirs(tmp_path, monkeypatch):
    data = tmp_path / "data"
    config = tmp_path / "config"
    cwd = tmp_path / "some-project"
    cwd.mkdir()
    monkeypatch.setenv("VELUNE_DATA_HOME", str(data))
    monkeypatch.setattr(platformdirs, "user_config_dir", lambda *a, **k: str(config))
    monkeypatch.chdir(cwd)
    return data, config, cwd


def test_checks_pass_and_leave_the_cwd_untouched(isolated_dirs):
    data, config, cwd = isolated_dirs

    assert _check_velune_dir()["status"] == "ok"
    assert _check_sqlite()["status"] == "ok"

    assert list(cwd.iterdir()) == [], "doctor wrote into the current directory"
    # Probes clean up after themselves inside Velune's own directories too.
    assert list(data.iterdir()) == []
    assert list(config.iterdir()) == []


def test_unwritable_data_dir_fails_naming_the_path(isolated_dirs, monkeypatch, tmp_path):
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("x")  # mkdir under a regular file fails on every OS
    monkeypatch.setenv("VELUNE_DATA_HOME", str(blocker / "data"))

    result = _check_velune_dir()
    assert result["status"] == "fail"
    assert str(blocker / "data") in result["message"]

    sqlite_result = _check_sqlite()
    assert sqlite_result["status"] == "fail"
    assert str(blocker / "data") in sqlite_result["message"]
