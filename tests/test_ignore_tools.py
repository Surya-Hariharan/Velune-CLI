"""``read_directory``, ``find_files`` and ``grep_files`` share one ignore matcher.

These tests check the integration: the three tools return the same visible files for the
same tree, excluded directories are never entered, ignore files are re-read on every call,
nothing needs ``git``, the scan deadline and cancel flag still work, and no link can lead a
tool out of the workspace.
"""

from __future__ import annotations

import os
import subprocess
import threading
import time
from pathlib import Path

import pytest

from velune.repository.scanner import FilesystemScanner
from velune.tools.filesystem import search as search_mod
from velune.tools.filesystem.ignore import load_ignore
from velune.tools.filesystem.read import ReadDirectory
from velune.tools.filesystem.search import FindFiles, GrepFiles


def _write(root: Path, rel: str, text: str = "needle\n") -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="")


# rel path -> visible to the tools? (every file, including ignore files, contains "needle")
LAYOUT: dict[str, bool] = {
    "main.py": True,
    "src/app.py": True,
    "src/gen/out.py": False,  # src/.gitignore: gen/
    "src/scratch.tmp": False,  # src/.gitignore: *.tmp
    "src/important.tmp": True,  # src/.gitignore: !important.tmp
    "src/deep/er/x.tmp": False,  # *.tmp applies at depth
    "src/deep/er/important.tmp": True,
    "dist-local/a.py": False,  # root .gitignore: /dist-local/ (anchored)
    "pkg/dist-local/a.py": True,  # ... only at the root
    "debug.log": False,  # root .gitignore: *.log
    "keep.log": True,  # root .gitignore: !keep.log
    "notes/trace.log": False,
    "secrets-1.txt": False,  # root .gitignore: secrets-*.txt
    "excluded/keep.txt": False,  # excluded/ then !excluded/keep.txt: cannot re-include
    "excluded/other.txt": False,
    "docs/final/b.md": True,
    "docs/draft/a.md": False,  # docs/.veluneignore: draft/
    "node_modules/lib/index.js": False,  # built-in default
    "vendor/v.go": False,  # built-in default (the scanner's list, now shared)
    "build/out.o": False,  # built-in default
    "web/app.min.js": True,  # not a default: a repository decides about minified assets
    "data/table.parquet": True,  # ... and about data files
    ".env": False,  # built-in default: secrets
}
IGNORE_FILES: dict[str, str] = {
    ".gitignore": "# needle\n/dist-local/\n*.log\n!keep.log\nsecrets-*.txt\nexcluded/\n!excluded/keep.txt\n",
    "src/.gitignore": "# needle\ngen/\n*.tmp\n!important.tmp\n",
    "docs/.veluneignore": "# needle\ndraft/\n",
}


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    for rel in LAYOUT:
        _write(tmp_path, rel)
    for rel, text in IGNORE_FILES.items():
        _write(tmp_path, rel, text)
    return tmp_path


def _relative(root: Path, names) -> set[str]:
    """Workspace-relative paths. Only the *directory* is resolved: a file symlink keeps its own
    name instead of collapsing into the file it points at."""
    base = root.resolve()
    out: set[str] = set()
    for n in names:
        p = Path(n)
        out.add((p.parent.resolve() / p.name).relative_to(base).as_posix())
    return out


async def find_set(root: Path, directory: str = ".") -> set[str]:
    found = await FindFiles(workspace=root).execute("*", directory)
    return _relative(root, [f for f in found if isinstance(f, str)])


async def grep_set(root: Path, directory: str = ".") -> set[str]:
    hits = await GrepFiles(workspace=root).execute("needle", directory)
    return _relative(root, [h["file"] for h in hits if "file" in h])


async def listing_set(root: Path, directory: str = ".") -> set[str]:
    """Every file reachable by walking ``read_directory`` -- the way an agent would."""
    tool = ReadDirectory(workspace=root)
    out: set[str] = set()
    todo = [directory]
    while todo:
        current = todo.pop()
        for name in await tool.execute(current):
            if isinstance(name, dict):  # the ignore warning, not a directory entry
                continue
            rel = name if current == "." else f"{current}/{name}"
            if (root / rel).is_dir():
                todo.append(rel)
            else:
                out.add(rel)
    return out


VISIBLE = {rel for rel, shown in LAYOUT.items() if shown} | set(IGNORE_FILES)


async def test_all_three_tools_show_exactly_the_same_files(repo):
    found, grepped, listed = await find_set(repo), await grep_set(repo), await listing_set(repo)

    assert found == VISIBLE
    assert grepped == found
    assert listed == found


async def test_all_three_tools_agree_when_started_from_a_subdirectory(repo):
    """Rules from above the starting directory still apply, with workspace-relative matching."""
    want = {rel for rel in VISIBLE if rel.startswith("src/")}

    assert await find_set(repo, "src") == want
    assert await grep_set(repo, "src") == want
    assert await listing_set(repo, "src") == want


async def test_a_starting_directory_inside_an_excluded_one_yields_nothing(repo):
    for directory in ("node_modules", "node_modules/lib", "src/gen", "excluded"):
        assert await find_set(repo, directory) == set(), directory
        assert await grep_set(repo, directory) == set(), directory
        assert await listing_set(repo, directory) == set(), directory


async def test_find_files_gives_up_at_once_inside_an_excluded_directory(repo, monkeypatch):
    walked: list[str] = []
    real_walk = os.walk
    monkeypatch.setattr(
        search_mod.os, "walk", lambda *a, **k: walked.append(str(a[0])) or real_walk(*a, **k)
    )

    assert await FindFiles(workspace=repo).execute("*", "node_modules") == []
    assert walked == []


async def test_the_prewritten_consistency_scenario_agrees_once_ignore_files_are_accounted_for(
    tmp_path,
):
    """``tests/test_ignore_consistency.py`` compares grep with find minus *only* the root
    ``.gitignore`` and ``*.veluneignore``, but ``find_files("*")`` also (rightly) lists
    ``sub/.gitignore``, whose text has no "needle". Same scenario, with every ignore file
    accounted for, must agree."""
    for rel in (
        "main.py",
        "generated/out.py",
        "debug.log",
        "sub/keep.log",
        "sub/local_a.py",
        "sub/deep/local_b.py",
        "sub/deep/ok.py",
        "other/private/secret_notes.py",
        "other/public.py",
    ):
        _write(tmp_path, rel)
    _write(tmp_path, ".gitignore", "generated/\n*.log\n")
    _write(tmp_path, "sub/.gitignore", "local_*.py\n!keep.log\n")
    _write(tmp_path, "other/.veluneignore", "private/\n")

    found = {
        p for p in await find_set(tmp_path) if Path(p).name not in (".gitignore", ".veluneignore")
    }

    assert await grep_set(tmp_path) == found
    assert found == {"main.py", "sub/keep.log", "sub/deep/ok.py", "other/public.py"}


# ── SVG is text, so it is visible by default ────────────────────────────────

_SVG = '<svg xmlns="http://www.w3.org/2000/svg"><text>needle</text></svg>\n'


async def test_svg_files_are_visible_to_all_three_tools_by_default(tmp_path):
    _write(tmp_path, "logo.svg", _SVG)
    _write(tmp_path, "assets/icons/arrow.svg", _SVG)
    _write(tmp_path, "main.py")
    want = {"logo.svg", "assets/icons/arrow.svg", "main.py"}

    assert await find_set(tmp_path) == want
    assert await grep_set(tmp_path) == want
    assert await listing_set(tmp_path) == want
    assert not load_ignore(tmp_path).is_ignored(tmp_path / "logo.svg")


async def test_repository_rules_can_still_hide_svg(tmp_path):
    _write(tmp_path, "logo.svg", _SVG)
    _write(tmp_path, "keep.svg", _SVG)
    _write(tmp_path, "gen/x.svg", _SVG)
    _write(tmp_path, "a/b.svg", _SVG)
    _write(tmp_path, ".gitignore", "logo.svg\ngen/\n")
    _write(tmp_path, "a/.veluneignore", "*.svg\n")
    want = {"keep.svg", ".gitignore", "a/.veluneignore"}

    assert await find_set(tmp_path) == want
    assert await listing_set(tmp_path) == want
    assert await grep_set(tmp_path) == {"keep.svg"}


async def test_removing_svg_from_the_defaults_changed_no_other_file_type(tmp_path):
    """Other media stays hidden; text, data and minified assets stay visible."""
    hidden = ["a.png", "a.jpg", "a.gif", "a.ico", "a.webp", "a.zip", "a.exe", "a.db", "a.mp4"]
    shown = ["a.svg", "a.py", "a.md", "a.json", "a.min.js", "a.min.css", "a.parquet", "a.html"]
    for rel in hidden + shown:
        _write(tmp_path, rel)

    assert await find_set(tmp_path) == set(shown)
    assert await grep_set(tmp_path) == set(shown)
    assert await listing_set(tmp_path) == set(shown)


# ── ignore files are re-read on every call ───────────────────────────────────


async def test_an_edited_gitignore_is_seen_by_the_next_call_of_the_same_tool_instance(repo):
    find, grep, listing = (
        FindFiles(workspace=repo),
        GrepFiles(workspace=repo),
        ReadDirectory(workspace=repo),
    )
    assert "main.py" in _relative(repo, await find.execute("*"))

    (repo / ".gitignore").write_text("main.py\n", encoding="utf-8")

    assert "main.py" not in _relative(
        repo, [f for f in await find.execute("*") if isinstance(f, str)]
    )
    assert "main.py" not in _relative(
        repo, [h["file"] for h in await grep.execute("needle") if "file" in h]
    )
    assert "main.py" not in await listing.execute(".")


# ── excluded directories are not entered ─────────────────────────────────────


async def test_excluded_directories_are_never_entered_by_find_or_grep(repo, monkeypatch):
    walked: list[Path] = []
    real_walk = os.walk
    real_iterdir = Path.iterdir

    def walk(top, *args, **kwargs):
        for entry in real_walk(top, *args, **kwargs):
            walked.append(Path(entry[0]))
            yield entry

    def iterdir(self):
        walked.append(self)
        return real_iterdir(self)

    monkeypatch.setattr(search_mod.os, "walk", walk)
    monkeypatch.setattr(Path, "iterdir", iterdir)

    await FindFiles(workspace=repo).execute("*")
    await GrepFiles(workspace=repo).execute("needle")

    entered = {p.resolve().relative_to(repo.resolve()).as_posix() for p in walked}
    for excluded in (
        "node_modules",
        "src/gen",
        "dist-local",
        "excluded",
        "docs/draft",
        "vendor",
        "build",
    ):
        assert not any(e == excluded or e.startswith(excluded + "/") for e in entered), excluded
    assert {"src", "src/deep/er", "pkg/dist-local"} <= entered


async def test_a_huge_excluded_tree_costs_a_search_nothing(tmp_path):
    for i in range(400):
        _write(tmp_path, f"cache/part{i % 20}/f{i}.txt")
    _write(tmp_path, ".gitignore", "cache/\n")
    _write(tmp_path, "real.txt")

    hits = await GrepFiles(workspace=tmp_path).execute("needle")
    marker = [h for h in hits if h.get("truncated")]

    assert _relative(tmp_path, [h["file"] for h in hits if "file" in h]) == {"real.txt"}
    assert not marker


# ── no git needed ────────────────────────────────────────────────────────────


async def test_nothing_needs_git_and_no_git_process_is_started(repo, monkeypatch):
    started: list[object] = []
    real_run = subprocess.run

    def spy(cmd, *a, **k):
        if cmd and "git" in str(cmd[0]):
            started.append(cmd)
            raise FileNotFoundError("git is not installed")
        return real_run(cmd, *a, **k)

    monkeypatch.setattr(subprocess, "run", spy)
    monkeypatch.setenv("PATH", "")

    assert await find_set(repo) == VISIBLE
    assert await grep_set(repo) == VISIBLE
    assert await listing_set(repo) == VISIBLE
    assert started == []


async def test_it_does_not_matter_whether_the_directory_is_a_git_repository(repo):
    without = await find_set(repo)
    subprocess.run(["git", "init", "-q"], cwd=repo, check=False, capture_output=True)
    assert await find_set(repo) == without == VISIBLE


# ── robustness ───────────────────────────────────────────────────────────────


async def test_unreadable_and_malformed_ignore_files_do_not_break_discovery(tmp_path):
    _write(tmp_path, "a.py")
    _write(tmp_path, "b.log")
    _write(tmp_path, "sub/c.log")
    _write(tmp_path, "sub/.gitignore", "[\n[a\nbad\\\n*.log\n")  # malformed lines, then a good one
    (tmp_path / ".gitignore").mkdir()  # not a file at all

    assert await find_set(tmp_path) == {"a.py", "b.log", "sub/.gitignore"}
    assert await grep_set(tmp_path) == {"a.py", "b.log"}
    assert await listing_set(tmp_path) == {"a.py", "b.log", "sub/.gitignore"}


def _warnings(results) -> list[dict]:
    return [r for r in results if isinstance(r, dict) and "ignore_warning" in r]


async def test_rules_that_cannot_be_applied_are_reported_by_every_tool(tmp_path):
    """Never silently dropped: the agent is told that files may appear that should be hidden."""
    _write(tmp_path, "a.py")
    _write(tmp_path, "sub/c.log")
    _write(tmp_path, "sub/.gitignore", "[\n[a\nbad\\\n*.log\n")
    (tmp_path / ".gitignore").mkdir()

    for results in (
        await FindFiles(workspace=tmp_path).execute("*"),
        await GrepFiles(workspace=tmp_path).execute("needle"),
        await ReadDirectory(workspace=tmp_path).execute("sub"),
    ):
        (warning,) = _warnings(results)
        assert "could NOT be applied" in warning["ignore_warning"]
        details = warning["details"]
        assert {d["file"] for d in details} == {"sub/.gitignore", ".gitignore"}
        bad = {d["pattern"]: d for d in details if "pattern" in d}
        assert set(bad) == {"[", "[a", "bad\\"}
        assert all("matches nothing" in d["problem"] and d["line"] >= 1 for d in bad.values())
        assert any("not a regular file" in d["problem"] for d in details if "pattern" not in d)
        assert results[-1] is warning  # one trailing entry, never mixed in with the results


async def test_a_clean_workspace_adds_no_warning_entry(repo):
    for results in (
        await FindFiles(workspace=repo).execute("*"),
        await GrepFiles(workspace=repo).execute("needle"),
        await ReadDirectory(workspace=repo).execute("."),
    ):
        assert not _warnings(results)


async def test_a_deeply_nested_tree_and_a_deep_rule_work(tmp_path):
    deep = "/".join(f"d{i}" for i in range(30))  # deeper trips Windows' 260-character path limit
    _write(tmp_path, f"{deep}/keep.py")
    _write(tmp_path, f"{deep}/skip.tmp")
    _write(tmp_path, f"{deep}/.gitignore", "*.tmp\n")

    found = await find_set(tmp_path)

    assert f"{deep}/keep.py" in found
    assert f"{deep}/skip.tmp" not in found
    assert load_ignore(tmp_path).is_ignored(tmp_path / deep / "skip.tmp")


# ── the scanner keeps its deadline, cancel flag and legacy behaviour ─────────


def _tree(root: Path) -> None:
    for d in range(30):
        for f in range(20):
            _write(root, f"pkg{d}/m{f}.py")


def test_a_scanner_using_the_shared_matcher_still_honours_a_deadline(tmp_path):
    _tree(tmp_path)
    scanner = FilesystemScanner(tmp_path, ignore=load_ignore(tmp_path))

    files = scanner.scan([".py"], deadline=time.monotonic() - 1)

    assert len(files) < 600 and scanner.truncated is True


def test_a_scanner_using_the_shared_matcher_still_honours_a_cancel_flag(tmp_path):
    _tree(tmp_path)
    cancel = threading.Event()
    cancel.set()
    scanner = FilesystemScanner(tmp_path, ignore=load_ignore(tmp_path))

    assert len(scanner.scan([".py"], cancel=cancel)) < 600
    assert scanner.truncated is True


def test_the_scanner_still_finds_everything_when_nothing_expires(tmp_path):
    _tree(tmp_path)
    scanner = FilesystemScanner(tmp_path, ignore=load_ignore(tmp_path))
    assert len(scanner.scan([".py"])) == 600 and scanner.truncated is False


def test_the_scanner_without_a_matcher_is_unchanged_so_indexing_is_unaffected(tmp_path):
    scanner = FilesystemScanner(tmp_path)
    assert scanner._matcher is None and scanner._spec is not None


# ── links must not lead out of the workspace ────────────────────────────────


def _make_dir_link(link: Path, target: Path) -> None:
    try:
        os.symlink(target, link, target_is_directory=True)
        return
    except (OSError, NotImplementedError):
        pass
    if os.name == "nt":
        made = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(target)], capture_output=True
        )
        if made.returncode == 0:
            return
    pytest.skip("cannot create a directory link here")


@pytest.fixture
def linked(tmp_path: Path) -> Path:
    ws, outside = tmp_path / "ws", tmp_path / "outside"
    _write(ws, "in.txt")
    _write(outside, "secret.txt")
    _write(outside, ".gitignore", "*.zzz\n")
    _make_dir_link(ws / "link", outside)
    return ws


async def test_no_tool_lists_or_searches_through_a_link_that_leaves_the_workspace(linked):
    from velune.execution.path_guard import PathTraversalError

    assert await find_set(linked) == {"in.txt"}
    assert await grep_set(linked) == {"in.txt"}
    # the link's own name is listed, but the permission boundary refuses to open it
    assert "in.txt" in await ReadDirectory(workspace=linked).execute(".")
    with pytest.raises(PathTraversalError):
        await ReadDirectory(workspace=linked).execute("link")


async def test_a_link_that_loops_back_into_the_workspace_does_not_hang_discovery(tmp_path):
    _write(tmp_path, "sub/a.py")
    _make_dir_link(tmp_path / "sub" / "loop", tmp_path)

    started = time.monotonic()
    found = await find_set(tmp_path)
    grepped = await grep_set(tmp_path)

    assert time.monotonic() - started < 10
    assert "sub/a.py" in found and "sub/a.py" in grepped


@pytest.mark.parametrize(
    "link_name", ["alias", "zlink"], ids=["link-sorts-first", "link-sorts-last"]
)
async def test_a_directory_link_inside_the_workspace_is_never_followed_by_any_tool(
    tmp_path, link_name
):
    """One path per file: find_files and grep_files used to disagree about internal links."""
    _write(tmp_path, "real/a.py")
    _write(tmp_path, "real/sub/b.py")
    _make_dir_link(tmp_path / link_name, tmp_path / "real")

    want = {"real/a.py", "real/sub/b.py"}
    assert await find_set(tmp_path) == want
    assert await grep_set(tmp_path) == want
    # read_directory shows names, so the link appears as an entry, exactly like any other
    assert set(await ReadDirectory(workspace=tmp_path).execute(".")) == {"real", link_name}


async def test_a_link_chain_back_into_the_tree_neither_hangs_nor_duplicates(tmp_path):
    _write(tmp_path, "a/one.py")
    _write(tmp_path, "a/b/two.py")
    _make_dir_link(tmp_path / "a" / "b" / "up", tmp_path / "a")  # a cycle
    _make_dir_link(tmp_path / "shortcut", tmp_path / "a" / "b")  # a second route in

    started = time.monotonic()
    found, grepped = await find_set(tmp_path), await grep_set(tmp_path)

    assert time.monotonic() - started < 10
    assert found == grepped == {"a/one.py", "a/b/two.py"}


def test_the_matcher_hides_a_directory_link_that_leaves_the_workspace_but_not_one_that_stays(
    tmp_path,
):
    ws, outside = tmp_path / "ws", tmp_path / "outside"
    _write(ws, "real/a.py")
    _write(outside, "x.py")
    _make_dir_link(ws / "inside", ws / "real")
    _make_dir_link(ws / "leaving", outside)
    spec = load_ignore(ws)

    assert not spec.is_ignored(ws / "inside")
    assert spec.is_ignored(ws / "leaving")


def _make_file_link(link: Path, target: Path) -> None:
    try:
        os.symlink(target, link)
    except (OSError, NotImplementedError):
        pytest.skip("file symlinks need a privilege this account does not have")


async def test_file_links_are_listed_alike_by_find_and_grep_and_escaping_ones_by_none(tmp_path):
    ws, outside = tmp_path / "ws", tmp_path / "outside"
    _write(ws, "real.py")
    _write(outside, "secret.py")
    _make_file_link(ws / "alias.py", ws / "real.py")
    _make_file_link(ws / "leak.py", outside / "secret.py")

    want = {"real.py", "alias.py"}
    assert await find_set(ws) == want
    assert await grep_set(ws) == want
    assert set(await ReadDirectory(workspace=ws).execute(".")) == want


async def test_file_symlinks_obey_the_workspace_boundary_in_every_tool(tmp_path):
    """Internal file links are ordinary files; every way of pointing out of the workspace is
    hidden from discovery and refused by the permission boundary -- nothing outside is read."""
    from velune.execution.path_guard import PathTraversalError
    from velune.tools.filesystem.read import ReadFile

    ws, outside = tmp_path / "ws", tmp_path / "outside"
    _write(ws, "real.py", "inside needle\n")
    _write(ws, "pkg/mod.py", "module needle\n")
    _write(outside, "secret.py", "OUTSIDE-SECRET needle\n")
    # internal: absolute, relative, nested, in a subdirectory, and a chain of links
    _make_file_link(ws / "abs.py", ws / "real.py")
    _make_file_link(ws / "pkg" / "rel.py", Path("mod.py"))
    _make_file_link(ws / "up.py", Path("pkg") / "mod.py")
    _make_file_link(ws / "chain1.py", ws / "up.py")
    # external: absolute, relative with .., through an internal hop, in a subdirectory
    _make_file_link(ws / "leak_abs.py", outside / "secret.py")
    _make_file_link(ws / "leak_rel.py", Path("..") / "outside" / "secret.py")
    _make_file_link(ws / "hop.py", ws / "leak_abs.py")
    _make_file_link(ws / "pkg" / "leak_sub.py", outside / "secret.py")
    # dangling: must neither crash nor appear to be a file with content
    _make_file_link(ws / "dangling.py", ws / "nowhere.py")

    internal = {"real.py", "pkg/mod.py", "abs.py", "pkg/rel.py", "up.py", "chain1.py"}
    leaks = {"leak_abs.py", "leak_rel.py", "hop.py", "pkg/leak_sub.py"}

    found = await find_set(ws)
    assert internal <= found
    assert not (found & leaks)
    assert await listing_set(ws) == found
    # grep reports the same files, and never any text that lives outside the workspace
    hits = await GrepFiles(workspace=ws).execute("needle")
    assert _relative(ws, [h["file"] for h in hits if "file" in h]) == internal
    assert not any("OUTSIDE-SECRET" in str(h) for h in hits)

    read = ReadFile(workspace=ws)
    assert "inside needle" in str(await read.execute("abs.py"))
    for leak in sorted(leaks):
        with pytest.raises(PathTraversalError):
            await read.execute(leak)


def test_the_scanner_does_not_follow_internal_directory_links_and_keeps_its_deadline(tmp_path):
    _write(tmp_path, "real/a.py")
    _make_dir_link(tmp_path / "alias", tmp_path / "real")
    scanner = FilesystemScanner(tmp_path, ignore=load_ignore(tmp_path))

    assert [p.relative_to(tmp_path).as_posix() for p in scanner.scan([".py"])] == ["real/a.py"]

    scanner.scan([".py"], deadline=time.monotonic() - 1)
    assert scanner.truncated is True


def test_the_matcher_never_widens_access_only_hides(tmp_path):
    """A `!` rule re-includes a path for the *listing*; it cannot make an outside path reachable."""
    ws = tmp_path / "ws"
    _write(ws, ".gitignore", "!**\n")
    _write(tmp_path, "outside.txt")
    spec = load_ignore(ws)

    assert spec.is_ignored(tmp_path / "outside.txt")
    assert spec.is_ignored(ws / ".." / "outside.txt")
