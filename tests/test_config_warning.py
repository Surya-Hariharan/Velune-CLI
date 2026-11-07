"""A broken velune.toml must warn (once, privately) and fall back to defaults."""

from __future__ import annotations

import logging

import pytest

from velune.kernel import config as config_mod
from velune.kernel.config import ConfigLoader

LOGGER = "velune.kernel.config"
SECRET = "sk-" + "Z" * 30


@pytest.fixture(autouse=True)
def _fresh_dedupe():
    config_mod._REPORTED_CONFIG_PROBLEMS.clear()
    yield
    config_mod._REPORTED_CONFIG_PROBLEMS.clear()


def _warnings(caplog):
    return [r for r in caplog.records if r.name == LOGGER and r.levelno == logging.WARNING]


def test_valid_config_loads_silently(tmp_path, caplog):
    path = tmp_path / "velune.toml"
    path.write_text('[project]\nname = "demo"\n', encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        cfg = ConfigLoader(path).load()
    assert cfg.project.name == "demo"
    assert _warnings(caplog) == []


def test_missing_config_is_silent(tmp_path, caplog):
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        cfg = ConfigLoader(tmp_path / "absent.toml").load()
        none_cfg = ConfigLoader.__new__(ConfigLoader)
        none_cfg.config_path = None
        none_cfg.load()
    assert cfg.project.name == "velune"
    assert _warnings(caplog) == []


def test_malformed_toml_warns_and_uses_defaults(tmp_path, caplog):
    path = tmp_path / "velune.toml"
    path.write_text("invalid [ toml {", encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        cfg = ConfigLoader(path).load()
    assert cfg.project.name == "velune"  # defaults, no crash
    (record,) = _warnings(caplog)
    message = record.getMessage()
    assert "velune.toml" in message
    assert "defaults" in message


def test_invalid_values_warn_with_setting_names_only(tmp_path, caplog):
    path = tmp_path / "velune.toml"
    # Wrong type for a known setting, with a secret-looking value next to it.
    path.write_text(
        f'[execution]\nsandbox_enabled = "{SECRET}"\n[project]\nname = 5\n', encoding="utf-8"
    )
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        cfg = ConfigLoader(path).load()
    assert cfg.execution.sandbox_enabled is True  # defaults
    (record,) = _warnings(caplog)
    message = record.getMessage()
    assert "invalid" in message
    assert SECRET not in message  # never echo file contents


def test_secret_in_malformed_file_is_never_logged(tmp_path, caplog):
    path = tmp_path / "velune.toml"
    path.write_text(f'api_key = "{SECRET}\nbroken = [', encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        ConfigLoader(path).load()
    assert _warnings(caplog)
    assert all(SECRET not in r.getMessage() for r in caplog.records)


def test_unreadable_config_warns(tmp_path, caplog):
    # A directory where a file is expected raises OSError on every platform
    # (PermissionError on Windows, IsADirectoryError elsewhere).
    path = tmp_path / "velune.toml"
    path.mkdir()
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        cfg = ConfigLoader(path).load()
    assert cfg.project.name == "velune"
    (record,) = _warnings(caplog)
    assert "could not be read" in record.getMessage()


def test_oserror_from_exists_is_reported_not_raised(tmp_path, monkeypatch, caplog):
    class Unreadable(type(tmp_path)):  # a path whose stat() is denied
        def exists(self):
            raise PermissionError("denied")

    path = Unreadable(tmp_path / "velune.toml")
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        cfg = ConfigLoader(path).load()
    assert cfg.project.name == "velune"
    assert len(_warnings(caplog)) == 1


def test_path_in_warning_hides_the_home_directory(tmp_path, monkeypatch, caplog):
    home = tmp_path / "alice_home"
    (home / ".velune").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    path = home / ".velune" / "velune.toml"
    path.write_text("invalid [ toml {", encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        ConfigLoader(path).load()
    message = _warnings(caplog)[0].getMessage()
    assert "alice_home" not in message
    assert "velune.toml" in message


def test_repeated_loads_warn_once_per_problem(tmp_path, caplog):
    path = tmp_path / "velune.toml"
    path.write_text("invalid [ toml {", encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        for _ in range(4):
            ConfigLoader(path).load()
    assert len(_warnings(caplog)) == 1


def test_fixing_the_file_stops_the_warning(tmp_path, caplog):
    path = tmp_path / "velune.toml"
    path.write_text("invalid [ toml {", encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        ConfigLoader(path).load()
    assert len(_warnings(caplog)) == 1  # the broken file did warn
    caplog.clear()
    path.write_text('[project]\nname = "ok"\n', encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        cfg = ConfigLoader(path).load()
    assert cfg.project.name == "ok"
    assert _warnings(caplog) == []
