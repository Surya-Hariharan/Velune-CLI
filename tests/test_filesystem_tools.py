"""Filesystem tools: workspace anchoring and traversal containment."""

from __future__ import annotations

import io
from pathlib import Path

import pytest
from rich.console import Console

from velune.execution import diff_preview
from velune.tools.filesystem.read import ReadDirectory, ReadFile
from velune.tools.filesystem.write import CreateFile, DeleteFile, WriteFile


@pytest.fixture
def quiet_console() -> Console:
    # StringIO sidesteps platform default encodings that can't render "✓"
    return Console(file=io.StringIO(), force_terminal=False)


@pytest.fixture
def auto_accept(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(diff_preview, "_auto_accept", True)


class TestReadFile:
    async def test_reads_relative_path_from_workspace_not_cwd(
        self, workspace: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        (workspace / "data.txt").write_text("workspace-content", encoding="utf-8")
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        # A decoy with the same relative name outside the workspace
        (elsewhere / "data.txt").write_text("DECOY", encoding="utf-8")
        monkeypatch.chdir(elsewhere)

        content = await ReadFile(workspace=workspace).execute("data.txt")
        assert content == "workspace-content"

    async def test_rejects_traversal(self, workspace: Path, tmp_path: Path) -> None:
        (tmp_path / "secret.txt").write_text("secret", encoding="utf-8")
        with pytest.raises(ValueError):
            await ReadFile(workspace=workspace).execute("../secret.txt")

    async def test_rejects_absolute_outside(self, workspace: Path, tmp_path: Path) -> None:
        outside = tmp_path / "outside.txt"
        outside.write_text("x", encoding="utf-8")
        with pytest.raises(ValueError):
            await ReadFile(workspace=workspace).execute(str(outside))


class TestReadDirectory:
    async def test_lists_workspace_dir(self, workspace: Path) -> None:
        (workspace / "a.txt").touch()
        (workspace / "b.txt").touch()
        names = await ReadDirectory(workspace=workspace).execute(".")
        assert sorted(names) == ["a.txt", "b.txt"]

    async def test_rejects_parent(self, workspace: Path) -> None:
        with pytest.raises(ValueError):
            await ReadDirectory(workspace=workspace).execute("..")


class TestWriteFile:
    async def test_relative_write_lands_in_workspace(
        self,
        workspace: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        quiet_console: Console,
        auto_accept: None,
    ) -> None:
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        monkeypatch.chdir(elsewhere)

        await WriteFile(workspace=workspace, console=quiet_console).execute(
            "out/result.txt", "payload"
        )
        assert (workspace / "out" / "result.txt").read_text(encoding="utf-8") == "payload"
        assert not (elsewhere / "out").exists()

    async def test_rejects_traversal_before_any_io(
        self, workspace: Path, quiet_console: Console, auto_accept: None
    ) -> None:
        with pytest.raises(ValueError):
            await WriteFile(workspace=workspace, console=quiet_console).execute(
                "../escape.txt", "evil"
            )
        assert not (workspace.parent / "escape.txt").exists()


class TestCreateAndDelete:
    async def test_create_then_delete_inside_workspace(
        self, workspace: Path, quiet_console: Console, auto_accept: None
    ) -> None:
        create = CreateFile(workspace=workspace, console=quiet_console)
        await create.execute("new.txt")
        assert (workspace / "new.txt").exists()

        delete = DeleteFile(workspace=workspace, console=quiet_console)
        await delete.execute("new.txt")
        assert not (workspace / "new.txt").exists()

    async def test_delete_rejects_outside_path(
        self, workspace: Path, tmp_path: Path, quiet_console: Console, auto_accept: None
    ) -> None:
        victim = tmp_path / "victim.txt"
        victim.write_text("important", encoding="utf-8")
        with pytest.raises(ValueError):
            await DeleteFile(workspace=workspace, console=quiet_console).execute(str(victim))
        assert victim.exists()
