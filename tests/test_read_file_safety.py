"""Edge cases for ``read_file`` beyond the headline behaviours in ``test_read_file_limits``.

Covers the size boundary, a file that changes while it is read, memory use for a small
range of a large file, encodings and binary detection, the workspace boundary
(including symlinks) and callers that pass none of the new parameters.
"""

from __future__ import annotations

import asyncio
import tracemalloc
from pathlib import Path

import pytest

from velune.execution.path_guard import PathTraversalError
from velune.tools.filesystem import read as read_mod
from velune.tools.filesystem.read import READ_MAX_CHARS, READ_MAX_FILE_BYTES, ReadFile


@pytest.fixture
def tool(tmp_path) -> ReadFile:
    return ReadFile(workspace=tmp_path)


def _numbered(out: str) -> list[tuple[int, str]]:
    rows = []
    for line in out.splitlines():
        if "\t" in line and line.split("\t")[0].strip().isdigit():
            number, _, text = line.partition("\t")
            rows.append((int(number), text))
    return rows


# ── empty and tiny files ─────────────────────────────────────────────────────


async def test_an_empty_file_reads_as_empty_text(tool, tmp_path):
    (tmp_path / "empty.py").write_bytes(b"")
    assert await tool.execute("empty.py") == ""


async def test_a_range_on_an_empty_file_reports_zero_lines(tool, tmp_path):
    (tmp_path / "empty.py").write_bytes(b"")
    assert "0 lines" in await tool.execute("empty.py", start_line=1)


async def test_a_last_line_without_a_trailing_newline_is_kept(tool, tmp_path):
    (tmp_path / "a.txt").write_bytes(b"one\ntwo")
    assert _numbered(await tool.execute("a.txt", start_line=1, end_line=5)) == [
        (1, "one"),
        (2, "two"),
    ]


async def test_crlf_files_come_back_with_plain_newlines(tool, tmp_path):
    (tmp_path / "w.txt").write_bytes(b"one\r\ntwo\r\n")
    assert await tool.execute("w.txt") == "one\ntwo\n"
    rows = _numbered(await tool.execute("w.txt", start_line=1, end_line=2))
    assert rows == [(1, "one"), (2, "two")]  # no stray carriage returns


# ── the size limit ───────────────────────────────────────────────────────────


async def test_a_file_exactly_at_the_limit_is_read_and_one_byte_over_is_not(
    tool, tmp_path, monkeypatch
):
    monkeypatch.setattr(read_mod, "READ_MAX_FILE_BYTES", 2000)
    (tmp_path / "at.txt").write_bytes(b"x" * 1999 + b"\n")
    (tmp_path / "over.txt").write_bytes(b"x" * 2000 + b"\n")

    assert await tool.execute("at.txt") == "x" * 1999 + "\n"
    refused = await tool.execute("over.txt")
    assert "start_line" in refused and "2001" in refused
    assert "x" * 100 not in refused


async def test_a_file_that_grows_after_the_size_check_is_still_bounded(tool, tmp_path, monkeypatch):
    """The size is checked, then the file is read: the read itself must enforce the limit."""
    monkeypatch.setattr(read_mod, "READ_MAX_FILE_BYTES", 1000)
    (tmp_path / "grew.log").write_bytes(b"y" * 9000)
    monkeypatch.setattr(read_mod, "_file_size", lambda handle: 10)  # what the check "saw"

    out = await tool.execute("grew.log")

    assert "y" * 100 not in out and "start_line" in out


async def test_a_file_that_shrinks_after_the_size_check_does_not_crash(tool, tmp_path, monkeypatch):
    target = tmp_path / "shrinks.txt"
    target.write_bytes(b"hello\nworld\n")

    def size_then_truncate(handle) -> int:
        size = 12
        with open(target, "wb") as other:
            other.write(b"")
        return size

    monkeypatch.setattr(read_mod, "_file_size", size_then_truncate)

    assert await tool.execute("shrinks.txt") == ""


async def test_a_small_range_of_a_large_file_does_not_allocate_the_whole_file(tool, tmp_path):
    big = tmp_path / "big.log"
    row = b"z" * 99 + b"\n"
    big.write_bytes(row * (80_000))  # ~8 MB, well over the whole-file limit
    assert big.stat().st_size > READ_MAX_FILE_BYTES

    tracemalloc.start()
    try:
        out = await tool.execute("big.log", start_line=3, end_line=4)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert [n for n, _ in _numbered(out)] == [3, 4]
    assert peak < 1_000_000, f"allocated {peak} bytes for a two-line read of an 8 MB file"


async def test_a_range_far_into_a_huge_file_stops_scanning(tool, tmp_path, monkeypatch):
    monkeypatch.setattr(read_mod, "READ_MAX_SCAN_CHARS", 5_000)
    (tmp_path / "long.txt").write_text("line\n" * 5_000, encoding="utf-8")

    with pytest.raises(ValueError, match="scan"):
        await tool.execute("long.txt", start_line=4_900)


# ── ranges ───────────────────────────────────────────────────────────────────


async def test_a_range_ending_past_the_end_returns_what_exists(tool, tmp_path):
    (tmp_path / "a.txt").write_text("a\nb\nc\n", encoding="utf-8")
    assert [n for n, _ in _numbered(await tool.execute("a.txt", start_line=2, end_line=99))] == [
        2,
        3,
    ]


async def test_a_single_line_can_be_requested(tool, tmp_path):
    (tmp_path / "a.txt").write_text("a\nb\nc\n", encoding="utf-8")
    assert _numbered(await tool.execute("a.txt", start_line=2, end_line=2)) == [(2, "b")]


async def test_start_alone_reads_from_there_to_the_end(tool, tmp_path):
    (tmp_path / "a.txt").write_text("a\nb\nc\n", encoding="utf-8")
    assert [n for n, _ in _numbered(await tool.execute("a.txt", start_line=2))] == [2, 3]


async def test_numeric_strings_are_accepted_but_other_types_are_not(tool, tmp_path):
    (tmp_path / "a.txt").write_text("a\nb\nc\n", encoding="utf-8")
    assert [n for n, _ in _numbered(await tool.execute("a.txt", start_line="2", end_line="3"))] == [
        2,
        3,
    ]
    for bad in (True, 1.5, "two", [1]):
        with pytest.raises(ValueError):
            await tool.execute("a.txt", start_line=bad)


async def test_a_range_larger_than_the_output_budget_says_where_to_continue(tool, tmp_path):
    (tmp_path / "wide.txt").write_text(
        "".join(f"row {i:05d} " + "w" * 70 + "\n" for i in range(1, 1001)), encoding="utf-8"
    )

    out = await tool.execute("wide.txt", start_line=1, end_line=1000)

    assert len(out) <= READ_MAX_CHARS + 400
    footer = out.splitlines()[-1]
    next_start = int(footer.split("start_line=")[1].split()[0].rstrip("].,)"))
    assert _numbered(out)[-1][0] == next_start - 1


async def test_one_enormous_line_is_clipped_not_returned_whole(tool, tmp_path):
    (tmp_path / "min.js").write_text("a" * 200_000 + "\nnext\n", encoding="utf-8")

    out = await tool.execute("min.js")

    assert len(out) <= READ_MAX_CHARS + 400
    assert "clipped" in out


async def test_the_output_stays_under_the_tool_loops_own_truncation(tool, tmp_path):
    from velune.orchestration.tool_loop import ToolLoopRunner  # noqa: F401  (documents the link)

    assert READ_MAX_CHARS + 400 < 16_000  # the loop's silent cut-off
    (tmp_path / "big.txt").write_text("q" * 60 + "\n" * 1, encoding="utf-8")
    assert len(await tool.execute("big.txt")) < 16_000


# ── encodings and binary files ───────────────────────────────────────────────


async def test_unicode_source_is_returned_unchanged(tool, tmp_path):
    text = "def f():\n    return 'é日本語🙂'\n"
    (tmp_path / "u.py").write_text(text, encoding="utf-8")
    assert await tool.execute("u.py") == text


async def test_truncation_counts_characters_not_bytes(tool, tmp_path):
    (tmp_path / "u.txt").write_text("日本語🙂" * 3000 + "\nlast\n", encoding="utf-8")
    out = await tool.execute("u.txt")
    assert len(out) <= READ_MAX_CHARS + 400


async def test_utf16_with_a_bom_is_decoded_not_called_binary(tool, tmp_path):
    (tmp_path / "ps.txt").write_bytes("hello\nworld\n".encode("utf-16"))  # includes the BOM
    assert await tool.execute("ps.txt") == "hello\nworld\n"


async def test_ordinary_utf8_source_is_not_mistaken_for_binary(tool, tmp_path):
    text = "".join(f"x{i} = 'ünï©ødé\\t'\n" for i in range(500))
    (tmp_path / "s.py").write_text(text, encoding="utf-8")
    assert "binary" not in (await tool.execute("s.py", start_line=1, end_line=2)).lower()


async def test_bytes_that_decode_in_no_supported_encoding_are_a_clear_error(tool, tmp_path):
    (tmp_path / "bad.txt").write_bytes(b"ok \x81 \x8d \x8f \x90 \x9d \xff\xfe\xfd text")
    with pytest.raises(ValueError, match="decode"):
        await tool.execute("bad.txt")


async def test_a_binary_file_never_puts_its_bytes_in_the_result(tool, tmp_path):
    (tmp_path / "x.bin").write_bytes(b"\x00\x01\x02MZ" + bytes(range(256)) * 4)
    out = await tool.execute("x.bin")
    assert "binary" in out.lower() and "\x00" not in out and "MZ" not in out


async def test_a_binary_file_is_also_refused_for_a_range(tool, tmp_path):
    (tmp_path / "x.bin").write_bytes(b"\x00" * 500)
    assert "binary" in (await tool.execute("x.bin", start_line=1, end_line=5)).lower()


# ── handles are always released ──────────────────────────────────────────────


@pytest.mark.parametrize(
    "name,payload,kwargs,expect",
    [
        ("ok.txt", b"hi\n", {}, None),
        ("rng.txt", b"a\nb\n", {"start_line": 1, "end_line": 1}, None),
        ("bin.dat", b"\x00" * 50, {}, None),
        ("bad.txt", b"\x81\x8d\x8f\x90\x9d \xff\xfe", {}, ValueError),
    ],
)
async def test_the_file_is_closed_on_every_path(tool, tmp_path, name, payload, kwargs, expect):
    target = tmp_path / name
    target.write_bytes(payload)
    if expect:
        with pytest.raises(expect):
            await tool.execute(name, **kwargs)
    else:
        await tool.execute(name, **kwargs)

    target.unlink()  # Windows refuses this while a handle is still open
    assert not target.exists()


# ── workspace boundary ───────────────────────────────────────────────────────


async def test_relative_nested_and_absolute_in_workspace_paths_work(tool, tmp_path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "m.py").write_text("x = 1\n", encoding="utf-8")
    assert await tool.execute("pkg/m.py") == "x = 1\n"
    assert await tool.execute(str(tmp_path / "pkg" / "m.py")) == "x = 1\n"


async def test_parent_traversal_is_refused_for_whole_and_ranged_reads(tool, tmp_path):
    outside = tmp_path.parent / "outside_traversal.txt"
    outside.write_text("secret\n", encoding="utf-8")
    try:
        for kwargs in ({}, {"start_line": 1, "end_line": 1}):
            with pytest.raises(PathTraversalError):
                await tool.execute("../outside_traversal.txt", **kwargs)
    finally:
        outside.unlink()


def _symlink(link: Path, target: Path) -> None:
    try:
        link.symlink_to(target)
    except (OSError, NotImplementedError) as exc:  # Windows needs a privilege for this
        pytest.skip(f"cannot create symlinks here: {exc}")


async def test_a_symlink_that_escapes_the_workspace_is_refused(tool, tmp_path):
    outside = tmp_path.parent / "outside_link_target.txt"
    outside.write_text("secret\n", encoding="utf-8")
    try:
        _symlink(tmp_path / "link.txt", outside)
        for kwargs in ({}, {"start_line": 1}):
            with pytest.raises(PathTraversalError):
                await tool.execute("link.txt", **kwargs)
    finally:
        outside.unlink()


async def test_a_symlink_to_a_file_inside_the_workspace_is_readable(tool, tmp_path):
    (tmp_path / "real.txt").write_text("inside\n", encoding="utf-8")
    _symlink(tmp_path / "alias.txt", tmp_path / "real.txt")
    assert await tool.execute("alias.txt") == "inside\n"


# ── callers that pass none of the new parameters ─────────────────────────────


async def test_existing_call_styles_still_work(tool, tmp_path):
    (tmp_path / "a.py").write_text("a = 1\n", encoding="utf-8")
    assert await tool.execute("a.py") == "a = 1\n"
    assert await tool.execute(file_path="a.py") == "a = 1\n"
    assert await tool.execute(**{"file_path": "a.py"}) == "a = 1\n"


async def test_a_non_regular_file_is_refused_rather_than_opened(tool, tmp_path, monkeypatch):
    (tmp_path / "dev").write_text("x", encoding="utf-8")
    monkeypatch.setattr(Path, "is_file", lambda self: False)
    monkeypatch.setattr(Path, "is_dir", lambda self: False)
    with pytest.raises(ValueError, match="regular file"):
        await tool.execute("dev")


def test_concurrent_reads_do_not_interfere(tmp_path):
    (tmp_path / "a.txt").write_text("".join(f"{i}\n" for i in range(500)), encoding="utf-8")
    tool = ReadFile(workspace=tmp_path)

    async def many():
        return await asyncio.gather(
            *(tool.execute("a.txt", start_line=i, end_line=i) for i in range(1, 40))
        )

    results = asyncio.run(many())
    assert [_numbered(r)[0][0] for r in results] == list(range(1, 40))


# ── Windows directory junctions (no privilege needed, followed by resolve() like a symlink) ──


def _junction(link: Path, target: Path) -> None:
    import subprocess
    import sys

    if sys.platform != "win32":
        pytest.skip("directory junctions are Windows-only; symlink tests cover POSIX")
    result = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link), str(target)], capture_output=True, text=True
    )
    if result.returncode != 0:
        pytest.skip(f"cannot create a junction here: {result.stderr.strip() or result.stdout}")


async def test_a_junction_that_escapes_the_workspace_is_refused(tool, tmp_path):
    outside_dir = tmp_path.parent / "outside_junction_dir"
    outside_dir.mkdir()
    (outside_dir / "secret.txt").write_text("secret\n", encoding="utf-8")
    try:
        _junction(tmp_path / "jump", outside_dir)
        for kwargs in ({}, {"start_line": 1, "end_line": 1}):
            with pytest.raises(PathTraversalError):
                await tool.execute("jump/secret.txt", **kwargs)
    finally:
        link = tmp_path / "jump"
        if link.exists():
            import os

            os.rmdir(link)  # removes the junction itself, never its target
        (outside_dir / "secret.txt").unlink()
        outside_dir.rmdir()


async def test_a_junction_to_a_directory_inside_the_workspace_is_readable(tool, tmp_path):
    (tmp_path / "real").mkdir()
    (tmp_path / "real" / "m.py").write_text("x = 1\n", encoding="utf-8")
    _junction(tmp_path / "alias", tmp_path / "real")
    try:
        assert await tool.execute("alias/m.py") == "x = 1\n"
    finally:
        import os

        os.rmdir(tmp_path / "alias")


async def test_an_enormous_line_in_a_range_is_clipped_and_numbering_continues(tool, tmp_path):
    (tmp_path / "min.js").write_text(
        "first\n" + "a" * 300_000 + "\nthird\nfourth\n", encoding="utf-8"
    )

    out = await tool.execute("min.js", start_line=1, end_line=4)

    rows = _numbered(out)
    assert [n for n, _ in rows] == [1, 2, 3, 4]  # the long line did not shift later numbers
    assert rows[2][1] == "third" and rows[3][1] == "fourth"
    assert "clipped" in out and "300000" in out  # reports the true length
    assert len(out) <= READ_MAX_CHARS + 400


async def test_a_long_line_before_the_range_is_skipped_without_being_held(tool, tmp_path):
    (tmp_path / "min.js").write_text("a" * 2_000_000 + "\nwanted\n", encoding="utf-8")

    tracemalloc.start()
    try:
        out = await tool.execute("min.js", start_line=2, end_line=2)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert _numbered(out) == [(2, "wanted")]
    assert peak < 1_000_000
