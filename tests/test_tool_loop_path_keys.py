"""Issue #33: a failed call's record is keyed by the file, not by how the model spelled it.

The tool loop remembers which calls failed (so an identical retry is refused) and forgets a
failure when a later call succeeds on the same file (typically the re-read after a conflict).
The file was identified by the raw spelling of the path argument, so an absolute re-read never
cleared a failure recorded under the relative spelling, and "/work/a.py" even collided with the
different file "work/a.py". The key is now the resolved location, the way the tools resolve it.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from tests.test_tool_loop_safety import _Provider
from velune.core.types.inference import InferenceRequest, ToolCall
from velune.orchestration.tool_loop import ToolLoopRunner, _call_target
from velune.tools.base.registry import ToolRegistry
from velune.tools.base.tool import BaseTool, ToolCallContext, ToolPermission

BS = chr(92)  # a backslash, spelled without an escape so this file reads the same everywhere
NUL = chr(0)
CASE_INSENSITIVE = sys.platform in ("win32", "darwin")
BACKSLASH_IS_SEPARATOR = os.sep == BS


def _call(i: int, name: str, **args: object) -> ToolCall:
    return ToolCall(id=f"c{i}", name=name, arguments=args)


def _target(ws: Path, **args: object) -> str | None:
    return _call_target(_call(0, "t", **args), ws)


class _Flaky(BaseTool):
    runs = 0

    def get_name(self):
        return "flaky"

    def get_description(self):
        return ""

    def get_required_permissions(self):
        return {ToolPermission.FILESYSTEM_READ}

    async def execute(self, **kw):
        _Flaky.runs += 1
        raise RuntimeError("stale")


class _Reread(BaseTool):
    def get_name(self):
        return "reread"

    def get_description(self):
        return ""

    def get_required_permissions(self):
        return {ToolPermission.FILESYSTEM_READ}

    async def execute(self, **kw):
        return "fresh"


async def _yes(*_):
    return True


async def _run(ws: Path, turns):
    reg = ToolRegistry()
    reg.register(_Flaky())
    reg.register(_Reread())
    ctx = ToolCallContext(run_id="r", actor="t", workspace=ws)
    return await ToolLoopRunner(_Provider(turns), reg, approver=_yes, ctx=ctx).run(
        InferenceRequest(model_id="m", messages=[])
    )


def _make_tree(root: Path) -> None:
    (root / "src" / "sub").mkdir(parents=True)
    (root / "src" / "a.py").write_text("x", encoding="utf-8")


# ── the reproduction from the issue ────────────────────────────────────────────────────────────


async def test_a_failed_edit_is_cleared_by_an_absolute_re_read_of_the_same_file(tmp_path):
    _make_tree(tmp_path)
    _Flaky.runs = 0
    turns = [
        [_call(1, "flaky", file_path="src/a.py")],
        [_call(2, "reread", file_path=str(tmp_path / "src" / "a.py"))],
        [_call(3, "flaky", file_path="src/a.py")],
    ]
    result = await _run(tmp_path, turns)
    assert _Flaky.runs == 2  # the second attempt ran: the re-read cleared the record
    assert "not retried" not in result.invocations[2].result


def _spellings(root: Path) -> dict[str, str]:
    absolute = str(root / "src" / "a.py")
    found = {
        "relative": "src/a.py",
        "dot-slash": "./src/a.py",
        "doubled-slash": "src//a.py",
        "dot-dot": "src/sub/../a.py",
        "absolute": absolute,
        "absolute-dot-dot": str(root / "src" / "sub" / ".." / "a.py"),
    }
    if BACKSLASH_IS_SEPARATOR:
        found["backslash"] = "src" + BS + "a.py"
        found["absolute-backslash"] = absolute.replace("/", BS)
    return found


@pytest.mark.parametrize(
    "name",
    ["relative", "dot-slash", "doubled-slash", "dot-dot", "absolute", "absolute-dot-dot"]
    + (["backslash", "absolute-backslash"] if BACKSLASH_IS_SEPARATOR else []),
)
async def test_every_spelling_of_the_file_clears_the_failure(tmp_path, name):
    _make_tree(tmp_path)
    _Flaky.runs = 0
    turns = [
        [_call(1, "flaky", file_path="src/a.py")],
        [_call(2, "reread", file_path=_spellings(tmp_path)[name])],
        [_call(3, "flaky", file_path="src/a.py")],
    ]
    await _run(tmp_path, turns)
    assert _Flaky.runs == 2, name


async def test_a_failure_recorded_under_an_absolute_spelling_is_cleared_by_a_relative_read(
    tmp_path,
):
    (tmp_path / "a.py").write_text("x", encoding="utf-8")
    _Flaky.runs = 0
    turns = [
        [_call(1, "flaky", file_path=str(tmp_path / "a.py"))],
        [_call(2, "reread", file_path="a.py")],
        [_call(3, "flaky", file_path=str(tmp_path / "a.py"))],
    ]
    await _run(tmp_path, turns)
    assert _Flaky.runs == 2


@pytest.mark.skipif(not CASE_INSENSITIVE, reason="needs a case-insensitive platform")
async def test_a_differently_cased_spelling_clears_the_failure_on_case_insensitive_platforms(
    tmp_path,
):
    (tmp_path / "Src").mkdir()
    (tmp_path / "Src" / "A.py").write_text("x", encoding="utf-8")
    _Flaky.runs = 0
    turns = [
        [_call(1, "flaky", file_path="Src/A.py")],
        [_call(2, "reread", file_path="src/a.PY")],
        [_call(3, "flaky", file_path="Src/A.py")],
    ]
    await _run(tmp_path, turns)
    assert _Flaky.runs == 2


# ── distinct files stay distinct ───────────────────────────────────────────────────────────────


async def test_a_read_of_a_different_file_does_not_clear_the_failure(tmp_path):
    for name in ("a.py", "b.py"):
        (tmp_path / name).write_text("x", encoding="utf-8")
    _Flaky.runs = 0
    turns = [
        [_call(1, "flaky", file_path="a.py")],
        [_call(2, "reread", file_path=str(tmp_path / "b.py"))],
        [_call(3, "flaky", file_path="a.py")],
    ]
    result = await _run(tmp_path, turns)
    assert _Flaky.runs == 1
    assert "not retried" in result.invocations[2].result


def test_an_absolute_path_is_not_the_same_file_as_the_same_text_made_relative(tmp_path):
    """The old key stripped the leading slash, so '/work/a.py' collided with 'work/a.py'."""
    absolute = Path(tmp_path.anchor) / "work" / "a.py"
    assert _target(tmp_path, file_path=str(absolute)) != _target(tmp_path, file_path="work/a.py")


def test_distinct_files_get_distinct_keys(tmp_path):
    names = ("a.py", "b.py", "sub/a.py", "a.py.bak", "a")
    assert len({_target(tmp_path, file_path=name) for name in names}) == len(names)


def test_equivalent_spellings_share_one_key(tmp_path):
    spellings = [
        "a.py",
        "./a.py",
        "sub/../a.py",
        "sub//../a.py",
        str(tmp_path / "a.py"),
        str(tmp_path / "sub" / ".." / "a.py"),
    ]
    assert len({_target(tmp_path, file_path=s) for s in spellings}) == 1


def test_every_argument_name_the_loop_knows_is_keyed_the_same_way(tmp_path):
    keys = {
        _target(tmp_path, **{name: "sub/x"})
        for name in ("file_path", "path", "source", "directory_path")
    }
    assert len(keys) == 1


def test_the_workspace_root_has_one_key_whatever_its_spelling(tmp_path):
    keys = {
        _target(tmp_path, directory_path="."),
        _target(tmp_path, directory_path=str(tmp_path)),
        _target(tmp_path, directory_path="sub/.."),
    }
    assert len(keys) == 1


# ── outside the workspace, symlinks, invalid input ─────────────────────────────────────────────


def test_a_path_outside_the_workspace_is_keyed_by_its_resolved_location(tmp_path):
    ws = tmp_path / "ws"
    other = tmp_path / "other"
    ws.mkdir()
    other.mkdir()
    assert _target(ws, file_path=str(other / "f.txt")) == _target(ws, file_path="../other/f.txt")
    assert _target(ws, file_path=str(other / "f.txt")) != _target(ws, file_path="f.txt")


def test_the_key_depends_on_the_workspace_it_is_anchored_to(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    assert _target(a, file_path="f.txt") != _target(b, file_path="f.txt")


def test_traversal_is_keyed_by_where_it_lands_and_never_collapses_onto_a_workspace_file(
    tmp_path,
):
    ws = tmp_path / "ws"
    ws.mkdir()
    assert _target(ws, file_path="../ws/a.py") == _target(ws, file_path="a.py")
    assert _target(ws, file_path="../a.py") != _target(ws, file_path="a.py")
    assert _target(ws, file_path="../" * 12 + "etc/passwd") != _target(ws, file_path="a.py")


def _symlink(target: Path, link: Path) -> None:
    try:
        os.symlink(target, link)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are not available")


def test_a_symlink_and_its_target_are_one_file(tmp_path):
    (tmp_path / "real.txt").write_text("x", encoding="utf-8")
    _symlink(tmp_path / "real.txt", tmp_path / "link.txt")
    assert _target(tmp_path, file_path="link.txt") == _target(tmp_path, file_path="real.txt")


def test_a_symlink_pointing_outside_is_keyed_by_where_it_leads(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    (tmp_path / "secret.txt").write_text("x", encoding="utf-8")
    _symlink(tmp_path / "secret.txt", ws / "link.txt")
    outside = str(tmp_path / "secret.txt")
    assert _target(ws, file_path="link.txt") == _target(ws, file_path=outside)
    assert _target(ws, file_path="link.txt") != _target(ws, file_path="other.txt")


def test_a_symlink_loop_does_not_raise(tmp_path):
    _symlink(tmp_path / "b", tmp_path / "a")
    _symlink(tmp_path / "a", tmp_path / "b")
    key = _target(tmp_path, file_path="a")
    assert isinstance(key, str) and key == _target(tmp_path, file_path="a")


@pytest.mark.parametrize(
    "bad", ["a" + NUL + "b", "x" * 5000, NUL], ids=["embedded-nul", "very-long", "nul"]
)
def test_invalid_paths_yield_a_stable_key_instead_of_raising(tmp_path, bad):
    first = _target(tmp_path, file_path=bad)
    assert isinstance(first, str) and first
    assert first == _target(tmp_path, file_path=bad)


def test_a_call_without_a_path_has_no_target(tmp_path):
    assert _target(tmp_path, command="ls") is None
    assert _target(tmp_path, file_path="") is None
    assert _target(tmp_path, file_path=5) is None


# ── platform spelling rules ────────────────────────────────────────────────────────────────────


@pytest.mark.skipif(CASE_INSENSITIVE, reason="needs a case-sensitive platform")
def test_case_differences_are_kept_distinct_on_case_sensitive_platforms(tmp_path):
    assert _target(tmp_path, file_path="A.py") != _target(tmp_path, file_path="a.py")


@pytest.mark.skipif(sys.platform != "win32", reason="Windows spelling rules")
def test_windows_drive_letter_case_and_separators_agree(tmp_path):
    absolute = str(tmp_path / "a.py")
    assert _target(tmp_path, file_path=absolute.lower()) == _target(
        tmp_path, file_path=absolute.upper().replace(BS, "/")
    )


@pytest.mark.skipif(sys.platform != "win32", reason="Windows does not normalize Unicode")
def test_windows_keeps_composed_and_decomposed_names_distinct(tmp_path):
    composed = "caf" + chr(0xE9) + ".py"
    decomposed = "cafe" + chr(0x301) + ".py"
    assert _target(tmp_path, file_path=composed) != _target(tmp_path, file_path=decomposed)


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS normalizes Unicode names")
def test_macos_treats_composed_and_decomposed_names_as_one_file(tmp_path):
    composed = "caf" + chr(0xE9) + ".py"
    decomposed = "cafe" + chr(0x301) + ".py"
    assert _target(tmp_path, file_path=composed) == _target(tmp_path, file_path=decomposed)
