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
from velune.orchestration.tool_loop import ToolLoopRunner, _call_target, _same_target
from velune.tools.base.registry import ToolRegistry
from velune.tools.base.tool import BaseTool, ToolCallContext, ToolPermission

BS = chr(92)  # a backslash, spelled without an escape so this file reads the same everywhere
NUL = chr(0)
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


# ── identity is the filesystem's call, not the platform's ──────────────────────────────────────


def _same(ws: Path, a: str, b: str) -> bool:
    return _same_target(_target(ws, file_path=a), _target(ws, file_path=b))


def _is_case_insensitive(root: Path) -> bool:
    probe = root / "CaseProbe.tmp"
    probe.write_text("x", encoding="utf-8")
    try:
        return (root / "caseprobe.tmp").exists()
    finally:
        probe.unlink()


def test_two_spellings_are_one_file_exactly_when_the_filesystem_says_so(tmp_path):
    (tmp_path / "Src").mkdir()
    (tmp_path / "Src" / "A.py").write_text("x", encoding="utf-8")
    one_file = _is_case_insensitive(tmp_path)
    assert _same(tmp_path, "Src/A.py", "src/a.PY") is one_file


def test_a_case_sensitive_volume_keeps_differently_cased_files_distinct(tmp_path):
    """CodeRabbit on #48: case folding chosen from sys.platform merged A.py and a.py."""
    (tmp_path / "A.py").write_text("upper", encoding="utf-8")
    try:
        (tmp_path / "a.py").write_text("lower", encoding="utf-8")
    except OSError:
        pytest.skip("cannot create both names")
    if (tmp_path / "A.py").read_text(encoding="utf-8") == "lower":
        pytest.skip("this volume is case-insensitive: the two names are one file")
    assert not _same(tmp_path, "A.py", "a.py")
    assert _same(tmp_path, "A.py", "./A.py")


async def test_the_loop_follows_the_filesystem_when_clearing_a_differently_cased_failure(tmp_path):
    (tmp_path / "A.py").write_text("x", encoding="utf-8")
    one_file = _is_case_insensitive(tmp_path)
    _Flaky.runs = 0
    turns = [
        [_call(1, "flaky", file_path="A.py")],
        [_call(2, "reread", file_path="a.py")],  # may not exist: a read of it is not an error here
        [_call(3, "flaky", file_path="A.py")],
    ]
    await _run(tmp_path, turns)
    assert _Flaky.runs == (2 if one_file else 1)


def test_a_missing_file_is_only_ever_equal_to_its_own_spelling(tmp_path):
    # Nothing exists, so the filesystem cannot vouch that two spellings are one object.
    assert not _same(tmp_path, "nope/A.py", "nope/a.py")
    assert _same(tmp_path, "nope/A.py", "./nope//A.py")


def test_none_never_matches():
    assert not _same_target(None, None)
    assert not _same_target("x", None)


def test_a_vanished_file_does_not_raise(tmp_path):
    gone = tmp_path / "gone.txt"
    gone.write_text("x", encoding="utf-8")
    key = _target(tmp_path, file_path="gone.txt")
    gone.unlink()
    assert not _same_target(key, str(tmp_path / "other.txt"))
    assert _same_target(key, key)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows spelling rules")
def test_windows_drive_letter_case_and_separators_agree(tmp_path):
    (tmp_path / "a.py").write_text("x", encoding="utf-8")
    absolute = str(tmp_path / "a.py")
    assert _same(tmp_path, absolute.lower(), absolute.upper().replace(BS, "/"))


@pytest.mark.skipif(sys.platform != "win32", reason="Windows does not normalize Unicode")
def test_windows_keeps_composed_and_decomposed_names_distinct(tmp_path):
    composed = "caf" + chr(0xE9) + ".py"
    decomposed = "cafe" + chr(0x301) + ".py"
    (tmp_path / composed).write_text("composed", encoding="utf-8")
    (tmp_path / decomposed).write_text("decomposed", encoding="utf-8")
    assert not _same(tmp_path, composed, decomposed)


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS normalizes Unicode names")
def test_macos_treats_composed_and_decomposed_names_as_one_file_when_the_volume_does(tmp_path):
    composed = "caf" + chr(0xE9) + ".py"
    decomposed = "cafe" + chr(0x301) + ".py"
    (tmp_path / composed).write_text("x", encoding="utf-8")
    assert _same(tmp_path, composed, decomposed) is (tmp_path / decomposed).exists()
