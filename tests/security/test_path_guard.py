"""Path traversal protection — the primary filesystem security boundary."""

from __future__ import annotations

from pathlib import Path

import pytest

from velune.execution.path_guard import (
    PathGuard,
    PathTraversalError,
    is_within_workspace,
    resolve_in_workspace,
    validate_workspace_path,
)


class TestPathGuard:
    def test_accepts_workspace_root_itself(self, workspace: Path) -> None:
        assert PathGuard(workspace).validate(workspace) == workspace.resolve()

    def test_accepts_nested_path(self, workspace: Path) -> None:
        nested = workspace / "src" / "deep" / "file.py"
        assert PathGuard(workspace).validate(nested) == nested.resolve()

    def test_rejects_parent_directory(self, workspace: Path) -> None:
        with pytest.raises(PathTraversalError):
            PathGuard(workspace).validate(workspace.parent)

    def test_rejects_dotdot_traversal(self, workspace: Path) -> None:
        with pytest.raises(PathTraversalError):
            PathGuard(workspace).validate(workspace / ".." / "outside.txt")

    def test_rejects_sibling_with_shared_prefix(self, workspace: Path) -> None:
        # "workspace-evil" starts with "workspace" — naive startswith string
        # comparison would wrongly accept it.
        sibling = workspace.parent / (workspace.name + "-evil") / "file.txt"
        with pytest.raises(PathTraversalError):
            PathGuard(workspace).validate(sibling)

    def test_rejects_absolute_path_outside(self, workspace: Path, tmp_path: Path) -> None:
        with pytest.raises(PathTraversalError):
            PathGuard(workspace).validate(tmp_path / "elsewhere.txt")

    def test_error_inherits_valueerror(self) -> None:
        # Tool layers catch ValueError; traversal must be caught with it.
        assert issubclass(PathTraversalError, ValueError)


class TestResolveInWorkspace:
    def test_relative_path_anchors_to_workspace_not_cwd(
        self, workspace: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        monkeypatch.chdir(elsewhere)
        resolved = resolve_in_workspace("notes.txt", workspace)
        assert resolved == (workspace / "notes.txt").resolve()

    def test_relative_traversal_rejected(self, workspace: Path) -> None:
        with pytest.raises(PathTraversalError):
            resolve_in_workspace("../escape.txt", workspace)

    def test_absolute_inside_accepted(self, workspace: Path) -> None:
        target = workspace / "a.txt"
        assert resolve_in_workspace(target, workspace) == target.resolve()

    def test_label_included_in_error(self, workspace: Path) -> None:
        with pytest.raises(PathTraversalError, match="WriteFile"):
            resolve_in_workspace("../x", workspace, label="WriteFile")


class TestLegacyApi:
    def test_is_within_workspace_true(self, workspace: Path) -> None:
        assert is_within_workspace(workspace / "f.txt", workspace)

    def test_is_within_workspace_false(self, workspace: Path, tmp_path: Path) -> None:
        assert not is_within_workspace(tmp_path / "out.txt", workspace)

    def test_validate_workspace_path_raises_valueerror(self, workspace: Path) -> None:
        with pytest.raises(ValueError, match="Security"):
            validate_workspace_path(workspace / ".." / "x", workspace)
