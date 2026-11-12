"""Overwriting a file too large for the undo journal, through the Council applier (CodeRabbit #2).

``EditBlockApplier.write`` discarded the "did it fit?" flag of ``read_before_image`` and always
journaled the write with the (absent) before-image, i.e. as the *creation* of a file. ``/rewind``
then deleted the user's file and reported it restored. An overwrite whose original cannot be kept
must be journaled as irreversible and said so, never as a creation.
"""

from __future__ import annotations

from io import StringIO
from pathlib import Path
from types import SimpleNamespace

import pytest
from rich.console import Console

from velune.cli.handlers import council as council_handlers
from velune.cli.handlers.council import apply_council_edits
from velune.cli.handlers.git import cmd_rewind
from velune.execution.edit_formats import EditBlockApplier
from velune.tools.filesystem import mutation as mut

MIB = 1024 * 1024


@pytest.fixture
def ws(tmp_path: Path) -> Path:
    root = tmp_path / "ws"
    root.mkdir()
    return root


def _repl(ws: Path) -> SimpleNamespace:
    container = SimpleNamespace(get=lambda key: ws, get_optional=lambda key, default=None: default)
    return SimpleNamespace(
        container=container,
        console=Console(file=StringIO(), force_terminal=False, width=140),
        active_model=None,
        _hunk_review_mode=False,
        _last_turn_journal=None,
    )


def _text(repl) -> str:
    """Console output with whitespace collapsed: Rich wraps long lines (and a temp path on Linux
    is long), which would otherwise split a phrase across two lines."""
    return " ".join(repl.console.file.getvalue().split())


@pytest.fixture(autouse=True)
def _auto_accept(monkeypatch):
    async def allow(repl, file_writes):
        return True

    monkeypatch.setattr(council_handlers, "_authorize_council_edits", allow)


def _journaled(ws: Path, fn):
    journal = mut.TurnJournal()
    token = mut.set_active_journal(journal)
    try:
        fn(EditBlockApplier(ws))
    finally:
        mut.reset_active_journal(token)
    return journal


def test_overwriting_a_file_over_the_journal_limit_is_not_recorded_as_a_creation(ws):
    big = ws / "big.bin"
    payload = b"\x00\x01" * (11 * MIB)  # 22 MiB, over the 20 MiB journal limit
    big.write_bytes(payload)

    journal = _journaled(ws, lambda applier: applier.write(big, "replacement\n"))

    assert not any(isinstance(e, mut.FileChange) and e.before is None for e in journal.entries)
    assert journal.irreversible == [f"overwrote {big}"]
    report = journal.rewind()
    assert big.read_text() == "replacement\n"  # NOT deleted
    assert report.restored == []
    assert any("cannot be rewound" in line for line in report.skipped)


def test_a_real_new_file_is_still_journaled_as_a_creation(ws):
    new = ws / "new.txt"
    journal = _journaled(ws, lambda applier: applier.write(new, "hello\n"))
    assert [type(e).__name__ for e in journal.entries] == ["FileChange"]
    assert journal.entries[0].before is None
    assert journal.irreversible == []
    journal.rewind()
    assert not new.exists()


def test_a_binary_file_within_the_limit_is_still_restorable(ws):
    blob = ws / "small.bin"
    blob.write_bytes(b"\x00\xff" * 100)
    journal = _journaled(ws, lambda applier: applier.write(blob, "text now\n"))
    assert journal.irreversible == []
    report = journal.rewind()
    assert blob.read_bytes() == b"\x00\xff" * 100
    assert report.restored == [str(blob)]


def test_a_text_file_within_the_limit_is_still_restorable(ws):
    source = ws / "m.py"
    source.write_bytes(b"x = 1\r\ny = 2\r\n")
    journal = _journaled(ws, lambda applier: applier.write(source, "x = 9\ny = 2\n"))
    assert source.read_bytes() == b"x = 9\r\ny = 2\r\n"  # CRLF kept
    journal.rewind()
    assert source.read_bytes() == b"x = 1\r\ny = 2\r\n"


def test_deleting_an_oversized_file_keeps_its_existing_irreversible_note(ws):
    big = ws / "big.bin"
    big.write_bytes(b"\x00" * (21 * MIB))
    journal = _journaled(ws, lambda applier: applier.write(big, ""))
    assert not big.exists()
    assert journal.irreversible == [f"deleted {big}"]


async def test_council_overwrite_of_a_large_text_file_then_rewind_never_deletes_it(ws):
    """The end-to-end path: a SEARCH/REPLACE edit to a 21 MiB text file, then ``/rewind``."""
    big = ws / "huge.txt"
    big.write_text("x = 1\n" + "filler line of text\n" * (21 * MIB // 20), encoding="utf-8")
    assert big.stat().st_size > 20 * MIB
    repl = _repl(ws)

    proposal = "huge.txt\n<<<<<<< SEARCH\nx = 1\n=======\nx = 2\n>>>>>>> REPLACE\n"
    await apply_council_edits(repl, proposal, "bump x")
    assert big.read_text(encoding="utf-8").startswith("x = 2\n")
    # The limitation is stated when the change is applied...
    assert "cannot restore it" in _text(repl)

    await cmd_rewind(repl, "")
    assert big.exists(), "/rewind must never delete a file that merely could not be backed up"
    assert big.read_text(encoding="utf-8").startswith("x = 2\n")
    # ...and again, accurately, when the user rewinds.
    assert "cannot be rewound" in _text(repl)
    assert "↺ restored" not in _text(repl), "nothing was restored, so nothing may be reported so"


async def test_council_overwrite_within_the_limit_is_rewound_as_before(ws):
    small = ws / "small.py"
    small.write_text("x = 1\n", encoding="utf-8")
    repl = _repl(ws)
    proposal = "small.py\n<<<<<<< SEARCH\nx = 1\n=======\nx = 2\n>>>>>>> REPLACE\n"
    await apply_council_edits(repl, proposal, "bump x")
    assert "cannot restore it" not in _text(repl)
    await cmd_rewind(repl, "")
    assert small.read_text(encoding="utf-8") == "x = 1\n"
    assert "Rewound 1 of 1" in _text(repl)
