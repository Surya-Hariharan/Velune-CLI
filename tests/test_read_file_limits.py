"""``read_file`` is bounded, pageable, encoding-tolerant and honest about what it did not show.

It used to ``open(path, encoding="utf-8").read()``: no size limit, no way to read a
section of a large file (the tool loop then cut the result blindly at 16 000
characters, leaving the rest unreachable), and a raw ``UnicodeDecodeError`` for any
binary or non-UTF-8 file.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from velune.execution.path_guard import PathTraversalError
from velune.tools.filesystem.read import READ_MAX_CHARS, READ_MAX_FILE_BYTES, ReadFile


def _lines(n: int, width: int = 60) -> str:
    return "".join(f"line {i:05d} " + "x" * width + "\n" for i in range(1, n + 1))


@pytest.fixture
def tool(tmp_path) -> ReadFile:
    return ReadFile(workspace=tmp_path)


async def test_a_small_file_is_returned_verbatim(tool, tmp_path):
    (tmp_path / "a.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    assert await tool.execute("a.py") == "def f():\n    return 1\n"


async def test_a_line_range_is_returned_with_accurate_line_numbers(tool, tmp_path):
    (tmp_path / "a.txt").write_text("one\ntwo\nthree\nfour\n", encoding="utf-8")

    out = await tool.execute("a.txt", start_line=2, end_line=3)

    body = [line for line in out.splitlines() if "\t" in line]
    assert [ln.split("\t")[0].strip() for ln in body] == ["2", "3"]
    assert [ln.split("\t")[1] for ln in body] == ["two", "three"]
    assert "four" not in out and "one" not in out


async def test_a_range_past_the_end_says_how_long_the_file_is(tool, tmp_path):
    (tmp_path / "a.txt").write_text("one\ntwo\n", encoding="utf-8")
    out = await tool.execute("a.txt", start_line=50)
    assert "2 lines" in out


@pytest.mark.parametrize("start,end", [(0, 5), (-1, None), (5, 2)])
async def test_an_invalid_range_is_rejected(tool, tmp_path, start, end):
    (tmp_path / "a.txt").write_text("x\n", encoding="utf-8")
    with pytest.raises(ValueError):
        await tool.execute("a.txt", start_line=start, end_line=end)


async def test_a_long_file_is_truncated_and_says_where_to_continue(tool, tmp_path):
    (tmp_path / "big.txt").write_text(_lines(2000), encoding="utf-8")

    out = await tool.execute("big.txt")

    assert len(out) <= READ_MAX_CHARS + 400
    assert "line 00001" in out
    assert "line 02000" not in out
    footer = out.splitlines()[-1]
    assert "of 2000" in footer and "start_line=" in footer
    next_start = int(footer.split("start_line=")[1].split()[0].rstrip("].,)"))
    assert 1 < next_start < 2000

    # following the footer reaches the rest, with no gap and no overlap
    rest = await tool.execute("big.txt", start_line=next_start, end_line=next_start)
    assert f"line {next_start:05d}" in rest
    shown = [ln for ln in out.splitlines() if ln.startswith(" ")]
    assert int(shown[-1].split("\t")[0]) == next_start - 1


async def test_a_file_over_the_hard_cap_is_not_slurped_but_can_be_ranged(tool, tmp_path):
    big = tmp_path / "huge.log"
    with open(big, "w", encoding="utf-8") as fh:
        row = "y" * 99 + "\n"
        for _ in range(READ_MAX_FILE_BYTES // 100 + 5000):
            fh.write(row)

    refused = await tool.execute("huge.log")
    assert "start_line" in refused and len(refused) < 1000

    section = await tool.execute("huge.log", start_line=3, end_line=4)
    assert section.count("\t") == 2


async def test_a_binary_file_is_reported_not_decoded(tool, tmp_path):
    (tmp_path / "blob.dat").write_bytes(b"\x00\x01\x02" * 100)
    out = await tool.execute("blob.dat")
    assert "binary" in out.lower() and "300" in out


async def test_a_non_utf8_file_is_read_with_a_fallback_encoding(tool, tmp_path):
    (tmp_path / "legacy.txt").write_bytes("caf\xe9\n".encode("cp1252"))
    assert "café" in await tool.execute("legacy.txt")


async def test_a_utf8_bom_is_not_part_of_the_content(tool, tmp_path):
    (tmp_path / "bom.txt").write_bytes(b"\xef\xbb\xbfhello\n")
    assert await tool.execute("bom.txt") == "hello\n"


async def test_a_directory_is_a_clear_error(tool, tmp_path):
    (tmp_path / "d").mkdir()
    with pytest.raises(IsADirectoryError):
        await tool.execute("d")


async def test_a_missing_file_is_a_clear_error(tool):
    with pytest.raises(FileNotFoundError):
        await tool.execute("nope.py")


async def test_the_workspace_boundary_still_applies(tool, tmp_path):
    outside = tmp_path.parent / "outside_secret.txt"
    outside.write_text("nope", encoding="utf-8")
    try:
        with pytest.raises((PathTraversalError, PermissionError)):
            await tool.execute(str(outside))
    finally:
        outside.unlink()


def test_the_schema_advertises_the_range_arguments(tool):
    props = tool.get_schema()["properties"]
    assert {"file_path", "start_line", "end_line"} <= set(props)
    assert tool.get_schema()["required"] == ["file_path"]
    assert isinstance(Path(__file__), Path)  # keep the Path import honest for linters
