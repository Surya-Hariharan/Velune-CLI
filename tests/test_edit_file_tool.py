"""``edit_file``: exact, unique, targeted replacement with actionable errors."""

from __future__ import annotations

from pathlib import Path

import pytest

from velune.tools.filesystem.edit import EditError, EditFile
from velune.tools.filesystem.read import READ_MAX_CHARS, ReadFile


@pytest.fixture
def ws(tmp_path: Path) -> Path:
    (tmp_path / "app.py").write_bytes(
        b"def add(a, b):\n    return a + b\n\n\ndef sub(a, b):\n    return a + b\n"
    )
    return tmp_path


async def test_unique_edit_changes_only_that_place(ws):
    out = await EditFile(ws).execute(
        file_path="app.py",
        old_string="def sub(a, b):\n    return a + b",
        new_string="def sub(a, b):\n    return a - b",
    )
    assert (ws / "app.py").read_bytes() == (
        b"def add(a, b):\n    return a + b\n\n\ndef sub(a, b):\n    return a - b\n"
    )
    assert out.startswith("edit_file OK app.py (+1 -1) verified sha256=")


async def test_ambiguous_match_is_refused_with_line_numbers(ws):
    before = (ws / "app.py").read_bytes()
    with pytest.raises(EditError) as err:
        await EditFile(ws).execute(
            file_path="app.py", old_string="return a + b", new_string="return a - b"
        )
    message = str(err.value)
    assert "matches 2 places (lines 2, 6)" in message
    assert "replace_all=true" in message
    assert (ws / "app.py").read_bytes() == before


async def test_replace_all_changes_every_occurrence(ws):
    out = await EditFile(ws).execute(
        file_path="app.py", old_string="a + b", new_string="b + a", replace_all=True
    )
    assert (ws / "app.py").read_text(encoding="utf-8").count("b + a") == 2
    assert "2 replacements" in out


async def test_missing_target_points_at_the_closest_line(ws):
    with pytest.raises(EditError, match=r"not found.*closest line is 2"):
        await EditFile(ws).execute(
            file_path="app.py", old_string="    return a * b", new_string="x"
        )


async def test_whitespace_mismatch_is_called_out(ws):
    with pytest.raises(EditError, match="whitespace or indentation differs"):
        await EditFile(ws).execute(
            file_path="app.py", old_string="def add(a, b):\n  return a + b", new_string="x"
        )


@pytest.mark.parametrize(
    ("old", "new", "fragment"),
    [("", "x", "old_string is empty"), ("return a + b", "return a + b", "identical")],
)
async def test_degenerate_edits_are_refused(ws, old, new, fragment):
    with pytest.raises(EditError, match=fragment):
        await EditFile(ws).execute(file_path="app.py", old_string=old, new_string=new)


async def test_empty_new_string_deletes_the_text(ws):
    await EditFile(ws).execute(file_path="app.py", old_string="def add(a, b):\n", new_string="")
    assert (ws / "app.py").read_bytes().startswith(b"    return a + b\n")


async def test_edits_far_past_what_read_file_shows(tmp_path):
    lines = [f"value_{i} = {i}\n" for i in range(5000)]
    (tmp_path / "big.py").write_text("".join(lines), encoding="utf-8", newline="")
    shown = await ReadFile(tmp_path).execute(file_path="big.py")
    assert "value_4999" not in shown and len(shown) <= READ_MAX_CHARS + 200

    await EditFile(tmp_path).execute(
        file_path="big.py", old_string="value_4999 = 4999", new_string="value_4999 = -1"
    )
    text = (tmp_path / "big.py").read_text(encoding="utf-8")
    assert text.endswith("value_4999 = -1\n")
    assert text.count("\n") == 5000


@pytest.mark.parametrize(
    ("setup", "error"),
    [
        (lambda p: (p / "bin.dat").write_bytes(b"\x00\x01\x02binary"), "binary"),
        (lambda p: (p / "bin.dat").mkdir(), "folder"),
        (lambda p: None, "not found"),
    ],
)
async def test_non_text_targets_are_refused(tmp_path, setup, error):
    setup(tmp_path)
    with pytest.raises((ValueError, OSError), match=error):
        await EditFile(tmp_path).execute(file_path="bin.dat", old_string="a", new_string="b")


def test_preview_returns_the_proposed_content_without_writing(ws):
    before = (ws / "app.py").read_bytes()
    planned = EditFile(ws).preview(
        {"file_path": "app.py", "old_string": "def add", "new_string": "def plus"}
    )
    assert planned is not None
    path, proposed = planned
    assert path == (ws / "app.py").resolve()
    assert proposed.startswith("def plus(a, b):")
    assert (ws / "app.py").read_bytes() == before
    assert EditFile(ws).preview({"file_path": "app.py", "old_string": "nope"}) is None
