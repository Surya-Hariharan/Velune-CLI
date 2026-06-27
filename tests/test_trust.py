"""Workspace trust-store regression tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from velune.core import trust


@pytest.fixture
def trust_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "trusted_dirs.json"
    monkeypatch.setattr(trust, "_trust_file", lambda: path)
    return path


def test_trust_round_trip_uses_directory_schema(trust_file: Path, tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    trust.trust(workspace)

    payload = json.loads(trust_file.read_text(encoding="utf-8"))
    assert payload["version"] == 2
    assert str(workspace.resolve()) in payload["directories"]
    assert trust.is_trusted(workspace)
    assert trust.list_trusted() == [str(workspace.resolve())]


def test_legacy_schema_migrates_on_write(trust_file: Path, tmp_path: Path) -> None:
    old_workspace = tmp_path / "old"
    new_workspace = tmp_path / "new"
    old_workspace.mkdir()
    new_workspace.mkdir()
    old_key = str(old_workspace.resolve())
    trust_file.write_text(
        json.dumps({"version": 1, "trusted": {old_key: {"trusted_at": "earlier"}}}),
        encoding="utf-8",
    )

    assert trust.is_trusted(old_workspace)
    trust.trust(new_workspace)

    payload = json.loads(trust_file.read_text(encoding="utf-8"))
    assert payload["version"] == 2
    assert set(payload["directories"]) == {old_key, str(new_workspace.resolve())}
    assert "trusted" not in payload


def test_forget_removes_workspace(trust_file: Path, tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    trust.trust(workspace)

    assert trust.forget(workspace)
    assert not trust.is_trusted(workspace)
    assert not trust.forget(workspace)
