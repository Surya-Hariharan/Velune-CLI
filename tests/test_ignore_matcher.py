"""The shared hierarchical ignore matcher: Git semantics, safety, and agreement with Git.

Every table-driven case is checked twice: against the matcher, and against Git's own
``check-ignore`` in an isolated temporary repository (skipped if ``git`` is absent -- Git
is only a test oracle, never a runtime dependency). A seeded fuzz of random trees with
random nested ignore files is compared with Git the same way.
"""

from __future__ import annotations

import os
import random
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from velune.repository.scanner import _HARDCODED_PATTERNS
from velune.tools.filesystem import ignore as ignore_mod
from velune.tools.filesystem.ignore import (
    _DEFAULT_PATTERNS,
    MAX_DIAGNOSTICS_SHOWN,
    VeluneIgnore,
    default_ignore,
    ignore_warning,
    load_ignore,
    parse_ignore_text,
)

HAS_GIT = shutil.which("git") is not None
needs_git = pytest.mark.skipif(not HAS_GIT, reason="git is only the test oracle")


def build(root: Path, files: dict[str, str | bytes]) -> None:
    for rel, content in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            path.write_bytes(content)
        else:
            path.write_text(content, encoding="utf-8", newline="")


def matcher(root: Path, **kwargs) -> VeluneIgnore:
    kwargs.setdefault("defaults", ())  # isolate the rule semantics from the built-in list
    kwargs.setdefault("case_sensitive", True)  # the case tests opt in to auto-detection
    return VeluneIgnore(root, **kwargs)


def git_ignored(repo: Path, rels: list[str], ignorecase: bool | None = False) -> set[str]:
    """Paths Git ignores. *ignorecase* ``None`` leaves the repository's own config in force."""
    env = {
        **os.environ,
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_SYSTEM": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
    }
    if not (repo / ".git").exists():
        subprocess.run(["git", "init", "-q"], cwd=repo, env=env, check=True)
    override = [] if ignorecase is None else ["-c", f"core.ignorecase={str(ignorecase).lower()}"]
    result = subprocess.run(
        [
            "git",
            *override,
            "-c",
            "core.excludesFile=",
            "check-ignore",
            "--no-index",
            "-z",
            "--stdin",
        ],
        cwd=repo,
        input=("\0".join(rels) + "\0").encode(),
        capture_output=True,
        env=env,
    )
    assert result.returncode in (0, 1), result.stderr
    return {item.decode() for item in result.stdout.split(b"\0") if item}


# (id, files, ignored, kept). Directories are created implicitly by the files in them.
CASES = [
    (
        "wildcard",
        {".gitignore": "*.log\n", "a.log": "", "d/b.log": "", "a.txt": ""},
        ["a.log", "d/b.log"],
        ["a.txt"],
    ),
    (
        "question-and-star",
        {
            ".gitignore": "?.py\nfoo*bar\n",
            "a.py": "",
            "ab.py": "",
            "fooXbar": "",
            "foobar": "",
            "foo/bar": "",
        },
        ["a.py", "fooXbar", "foobar"],
        ["ab.py", "foo/bar"],
    ),
    (
        "char-classes",
        {
            ".gitignore": "[ab].txt\n[!a]z\n[[:digit:]]*.dat\n",
            "a.txt": "",
            "b.txt": "",
            "c.txt": "",
            "bz": "",
            "az": "",
            "1x.dat": "",
            "x.dat": "",
        },
        ["a.txt", "b.txt", "bz", "1x.dat"],
        ["c.txt", "az", "x.dat"],
    ),
    (
        "double-star-leading",
        {
            ".gitignore": "**/gen/*.py\n",
            "gen/a.py": "",
            "x/y/gen/a.py": "",
            "gen/sub/a.py": "",
            "gen/a.txt": "",
        },
        ["gen/a.py", "x/y/gen/a.py"],
        ["gen/sub/a.py", "gen/a.txt"],
    ),
    (
        "double-star-middle",
        {
            ".gitignore": "a/**/z.txt\n",
            "a/z.txt": "",
            "a/b/c/z.txt": "",
            "z.txt": "",
            "b/a/z.txt": "",
        },
        ["a/z.txt", "a/b/c/z.txt"],
        ["z.txt", "b/a/z.txt"],
    ),
    (
        "double-star-trailing",
        {".gitignore": "docs/**\n", "docs/x/y.md": "", "docs/z.md": "", "other/docs/z.md": ""},
        ["docs/x/y.md", "docs/z.md"],
        ["other/docs/z.md"],
    ),
    (
        "directory-only",
        {
            ".gitignore": "build/\n",
            "build/x.txt": "",
            "y/build/z.txt": "",
            "z/build": "",
            "builder.py": "",
        },
        ["build/x.txt", "y/build/z.txt"],
        ["z/build", "builder.py"],
    ),
    (
        "root-anchored",
        {".gitignore": "/top.txt\n", "top.txt": "", "sub/top.txt": ""},
        ["top.txt"],
        ["sub/top.txt"],
    ),
    (
        "slash-in-middle-anchors",
        {".gitignore": "a/b.txt\n", "a/b.txt": "", "x/a/b.txt": ""},
        ["a/b.txt"],
        ["x/a/b.txt"],
    ),
    (
        "unanchored-matches-at-depth",
        {
            ".gitignore": "cache\n",
            "cache": "",
            "a/cache": "",
            "a/b/cache/f.txt": "",
            "cached.txt": "",
        },
        ["cache", "a/cache", "a/b/cache/f.txt"],
        ["cached.txt"],
    ),
    (
        "negation",
        {".gitignore": "*.log\n!keep.log\n", "a.log": "", "keep.log": "", "d/keep.log": ""},
        ["a.log"],
        ["keep.log", "d/keep.log"],
    ),
    (
        "later-line-wins",
        {".gitignore": "!keep.log\n*.log\n", "keep.log": "", "a.log": ""},
        ["keep.log", "a.log"],
        [],
    ),
    (
        "negation-cannot-reinclude-inside-excluded-parent",
        {".gitignore": "out/\n!out/keep.txt\n", "out/keep.txt": "", "out/other.txt": ""},
        ["out/keep.txt", "out/other.txt"],
        [],
    ),
    (
        "negation-works-when-only-the-contents-are-excluded",
        {".gitignore": "out/*\n!out/keep.txt\n", "out/keep.txt": "", "out/other.txt": ""},
        ["out/other.txt"],
        ["out/keep.txt"],
    ),
    (
        "nested-negation-reincludes",
        {
            ".gitignore": "*.log\n",
            "sub/.gitignore": "!keep.log\n",
            "keep.log": "",
            "sub/keep.log": "",
            "sub/other.log": "",
            "sub/deep/keep.log": "",
        },
        ["keep.log", "sub/other.log"],
        ["sub/keep.log", "sub/deep/keep.log"],
    ),
    (
        "deeper-file-overrides-shallower",
        {
            ".gitignore": "*.md\n",
            "docs/.gitignore": "!*.md\n",
            "other.md": "",
            "docs/x.md": "",
            "docs/sub/y.md": "",
        },
        ["other.md"],
        ["docs/x.md", "docs/sub/y.md"],
    ),
    (
        "deeper-ignore-overrides-root-negation",
        {
            ".gitignore": "*.txt\n!a.txt\n",
            "sub/.gitignore": "a.txt\n",
            "a.txt": "",
            "sub/a.txt": "",
        },
        ["sub/a.txt"],
        ["a.txt"],
    ),
    (
        "nested-rules-are-relative-to-their-directory",
        {
            "sub/.gitignore": "/only.txt\nx/y.txt\n",
            "only.txt": "",
            "sub/only.txt": "",
            "sub/deeper/only.txt": "",
            "sub/x/y.txt": "",
            "sub/a/x/y.txt": "",
        },
        ["sub/only.txt", "sub/x/y.txt"],
        ["only.txt", "sub/deeper/only.txt", "sub/a/x/y.txt"],
    ),
    (
        "nested-rules-do-not-leak-sideways",
        {"a/.gitignore": "*.tmp\n", "a/f.tmp": "", "b/f.tmp": ""},
        ["a/f.tmp"],
        ["b/f.tmp"],
    ),
    (
        "veluneignore-overrides-gitignore-in-the-same-directory",
        {".gitignore": "*.txt\n", ".veluneignore": "!x.txt\n", "x.txt": "", "y.txt": ""},
        ["y.txt"],
        ["x.txt"],
    ),
    (
        "nested-veluneignore",
        {"other/.veluneignore": "private/\n", "other/private/s.py": "", "other/public.py": ""},
        ["other/private/s.py"],
        ["other/public.py"],
    ),
    (
        "escaped-bang-and-hash",
        {".gitignore": "\\!important\n\\#hash\n", "!important": "", "#hash": "", "important": ""},
        ["!important", "#hash"],
        ["important"],
    ),
    ("trailing-spaces-are-dropped", {".gitignore": "tr   \n", "tr": ""}, ["tr"], []),
    (
        "comments-and-blank-lines",
        {".gitignore": "# a comment\n\n   \n*.bak\n", "x.bak": "", "x.txt": ""},
        ["x.bak"],
        ["x.txt"],
    ),
    (
        "crlf-and-bom",
        {".gitignore": b"\xef\xbb\xbf*.bak\r\n!keep.bak\r\n", "x.bak": "", "keep.bak": ""},
        ["x.bak"],
        ["keep.bak"],
    ),
    (
        "malformed-patterns-are-skipped",
        {
            ".gitignore": "[\n[a\nbad\\\n[z-a]\n[[:bogus:]]\nreal.log\n",
            "real.log": "",
            "bad": "",
            "a": "",
        },
        ["real.log"],
        ["bad", "a"],
    ),
    ("star-never-crosses-a-slash", {".gitignore": "a*b\n", "a/b": "", "axb": ""}, ["axb"], ["a/b"]),
    (
        "two-double-stars",
        {
            ".gitignore": "a/**/b/**/c.txt\n",
            "a/b/c.txt": "",
            "a/q/b/r/c.txt": "",
            "a/b/z/y/c.txt": "",
            "a/c.txt": "",
            "q/a/b/c.txt": "",
            "a/b/c.py": "",
        },
        ["a/b/c.txt", "a/q/b/r/c.txt", "a/b/z/y/c.txt"],
        ["a/c.txt", "q/a/b/c.txt", "a/b/c.py"],
    ),
    (
        "leading-and-trailing-double-stars-together",
        {
            ".gitignore": "**/x/**/y/**\n",
            "x/y/f": "",
            "p/x/m/n/y/f": "",
            "x/f": "",
            "p/x/y": "",
            "y/x/f": "",
        },
        ["x/y/f", "p/x/m/n/y/f"],
        ["x/f", "y/x/f"],
    ),
    (
        "six-double-stars",
        {
            ".gitignore": "/a/**/b/**/c/**/d/**/e/**/f\n",
            "a/b/c/d/e/f": "",
            "a/1/b/2/c/3/d/4/e/5/f": "",
            "a/b/c/d/f": "",
        },
        ["a/b/c/d/e/f", "a/1/b/2/c/3/d/4/e/5/f"],
        ["a/b/c/d/f"],
    ),
    (
        "consecutive-double-stars-and-negation",
        {
            ".gitignore": "a/**/**/b\n!a/keep/**/b\n",
            "a/b": "",
            "a/x/y/b": "",
            "a/keep/b": "",
            "a/keep/z/b": "",
        },
        ["a/b", "a/x/y/b"],
        ["a/keep/b", "a/keep/z/b"],
    ),
]


@pytest.mark.parametrize("name,files,ignored,kept", CASES, ids=[c[0] for c in CASES])
def test_matcher_follows_git_semantics(tmp_path, name, files, ignored, kept):
    build(tmp_path, files)
    spec = matcher(tmp_path)
    for rel in ignored:
        assert spec.is_ignored(tmp_path / rel), f"{rel} should be ignored"
    for rel in kept:
        assert not spec.is_ignored(tmp_path / rel), f"{rel} should be kept"


@needs_git
@pytest.mark.parametrize("name,files,ignored,kept", CASES, ids=[c[0] for c in CASES])
def test_the_expectations_above_are_what_git_does(tmp_path, name, files, ignored, kept):
    if any(rel.endswith(".veluneignore") for rel in files):
        pytest.skip(".veluneignore is a Velune file; git has no equivalent to compare with")
    build(tmp_path, files)
    paths = [*ignored, *kept]
    assert git_ignored(tmp_path, paths) == set(ignored)


# ── agreement with Git on random trees ───────────────────────────────────────

_NAMES = [
    "a",
    "b",
    "ab",
    "a.txt",
    "b.log",
    "x.log",
    "build",
    "src",
    "keep",
    "x",
    "y",
    "[z]",
    "q r",
    "d.e",
    "abab",
    "baab.txt",
    "aabb",
]
_PATTERNS = [
    "a",
    "/a",
    "a/",
    "*.log",
    "!*.log",
    "!b.log",
    "build/",
    "!build/",
    "src/*",
    "!src/keep",
    "!src/",
    "**/a",
    "a/**",
    "a/**/b",
    "?b",
    "[ab].txt",
    "!a.txt",
    "*",
    "!*/",
    "x/y",
    "/x/*",
    "\\!a",
    "#c",
    "a ",
    "a\\ ",
    "**",
    "**/",
    "src/**/keep",
    "!**/keep",
    "*.txt",
    "!keep",
    "/build",
    "[!a]b",
    "[a-b]",
    "\\[z]",
    "x*",
    "!x/",
    "/src/",
    "**/build/",
    "a**b",
    "d.*",
    "b/**",
    "[",
    "[a",
    "a\\",
    "a*b*a*b",
    "*a*b*a*",
    "*a*a*a*a",
    "?*a*b*",
    "*b*a*b.txt",
    "**/a*b*a/**",
    "a/**/b/**/a",
]


def _random_tree(
    root: Path,
    rnd: random.Random,
    names: list[str] = _NAMES,
    patterns: list[str] = _PATTERNS,
) -> list[str]:
    dirs = [""]
    paths: list[str] = []

    def populate(prefix: str, depth: int) -> None:
        chosen: dict[str, str] = {}  # one spelling per name: case-insensitive filesystems merge
        for name in rnd.sample(names, rnd.randint(2, 6)):
            chosen.setdefault(name.lower(), name)
        for name in chosen.values():
            rel = f"{prefix}{name}"
            if rnd.random() < 0.4 and depth < 3:
                (root / rel).mkdir(parents=True, exist_ok=True)
                dirs.append(rel)
                paths.append(rel)
                populate(rel + "/", depth + 1)
            else:
                (root / rel).parent.mkdir(parents=True, exist_ok=True)
                (root / rel).write_text("x")
                paths.append(rel)

    populate("", 0)
    for directory in dirs:
        if rnd.random() < 0.6:
            lines = [rnd.choice(patterns) for _ in range(rnd.randint(1, 6))]
            (root / directory / ".gitignore").write_text("\n".join(lines) + "\n")
    return [p for p in paths if not p.endswith(".gitignore")]


@needs_git
@pytest.mark.parametrize("seed", range(30))
def test_random_trees_agree_with_git(tmp_path, seed):
    paths = _random_tree(tmp_path, random.Random(seed))
    want = git_ignored(tmp_path, paths)
    spec = matcher(tmp_path)
    disagreements = [
        (rel, spec.is_ignored(tmp_path / rel), rel in want)
        for rel in paths
        if spec.is_ignored(tmp_path / rel) != (rel in want)
    ]
    assert not disagreements, {
        "mismatches (path, matcher, git)": disagreements[:5],
        "ignore files": {
            str(f.relative_to(tmp_path)): f.read_text() for f in tmp_path.rglob(".gitignore")
        },
    }


# ── case: follow Git's core.ignorecase (or the filesystem) ────────────────────

_CASE_NAMES = [
    "Build", "build2", "SRC", "Src", "docs", "Docs", "README.TXT", "Notes.txt", "Todo.Txt", "KEEP.md",
    "keep2.MD", "Local", "local2", "Cache", "x.DAT", "Bx.dat", "a", "B", "Ab", "aB.PY",
]  # fmt: skip
_CASE_PATTERNS = [
    "build/", "BUILD/", "Build2/", "*.TXT", "*.txt", "!todo.txt", "!TODO.TXT", "/src/main.py", "/SRC/*",
    "src/", "DOCS/", "docs/*.md", "*.Md", "!keep.MD", "!KEEP2.md", "/LOCAL/", "local/", "Cache/",
    "[A-C]x.dat", "[a-b]*", "[[:upper:]]b", "**/KEEP*", "a/**/B", "**/ab.py", "!**/*.PY", "*", "!*/",
    "Docs/**", "x.dat", "!X.DAT",
]  # fmt: skip


def _init_repo(root: Path, ignorecase: bool | None) -> None:
    env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull}
    subprocess.run(["git", "init", "-q"], cwd=root, env=env, check=True)
    if ignorecase is not None:
        subprocess.run(
            ["git", "config", "--local", "core.ignorecase", str(ignorecase).lower()],
            cwd=root, env=env, check=True,
        )  # fmt: skip


def _compare_with_git(root: Path, paths: list[str]):
    spec = VeluneIgnore(root, defaults=())  # case mode auto-detected, as the tools do
    want = git_ignored(root, paths, ignorecase=None)  # the repository's own config decides
    return spec, [
        (rel, spec.is_ignored(root / rel), rel in want)
        for rel in paths
        if spec.is_ignored(root / rel) != (rel in want)
    ]


CASE_FILES = {
    ".gitignore": "build/\n*.TXT\n/src/main.py\n!todo.txt\nCache/\n*.Md\n!KEEP.MD\n[A-C]x.dat\n",
    "Docs/.gitignore": "/LOCAL/\nSecret*\n!SECRET.keep\n",
    "Build/out.o": "", "notes.txt": "", "Todo.TXT": "", "Src/MAIN.PY": "", "Src/other.py": "",
    "Docs/Readme.md": "", "Docs/KEEP.md": "", "cache/data.bin": "", "bx.dat": "", "dx.dat": "",
    "Docs/local/a.py": "", "Docs/Local2/b.py": "", "Docs/SECRET_1.py": "", "Docs/secret.KEEP": "",
}  # fmt: skip


@needs_git
@pytest.mark.parametrize(
    "ignorecase", [True, False, None], ids=["ignorecase", "case-exact", "git-default"]
)
def test_case_variations_agree_with_git_under_its_own_configuration(tmp_path, ignorecase):
    """Upper/lower-case directories and files, anchored, nested, negated and directory-only
    patterns, with the repository's real ``core.ignorecase`` -- ``None`` is whatever ``git init``
    chose for this filesystem (``true`` on Windows)."""
    _init_repo(tmp_path, ignorecase)
    build(tmp_path, CASE_FILES)
    paths = [
        p.relative_to(tmp_path).as_posix()
        for p in tmp_path.rglob("*")
        if ".git" not in p.relative_to(tmp_path).parts and p.name != ".gitignore"
    ]

    spec, mismatches = _compare_with_git(tmp_path, paths)

    assert not mismatches, mismatches
    if ignorecase is not None:
        assert spec.case_sensitive is (not ignorecase), "the repository's config must decide"


@needs_git
@pytest.mark.parametrize("ignorecase", [True, False])
@pytest.mark.parametrize("seed", range(20))
def test_random_mixed_case_trees_agree_with_git(tmp_path, seed, ignorecase):
    _init_repo(tmp_path, ignorecase)
    paths = _random_tree(tmp_path, random.Random(seed), _CASE_NAMES, _CASE_PATTERNS)
    spec, mismatches = _compare_with_git(tmp_path, paths)
    assert not mismatches, {
        "mismatches (path, matcher, git)": mismatches[:5],
        "ignorecase": ignorecase,
        "ignore files": {
            str(f.relative_to(tmp_path)): f.read_text() for f in tmp_path.rglob(".gitignore")
        },
    }


def test_the_case_mode_comes_from_the_repository_config_then_the_filesystem(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    for value, expect_sensitive in ((True, False), (False, True)):
        repo = tmp_path / f"repo{value}"
        repo.mkdir()
        (repo / ".git").mkdir()
        (repo / ".git" / "config").write_text(
            f"[core]\n\tbare = false\n\tignorecase = {str(value).lower()}\n"
            f"[other]\n\tignorecase = {not value}\n"
        )
        assert VeluneIgnore(repo, defaults=()).case_sensitive is expect_sensitive
    # no config: the filesystem decides (Windows and macOS volumes are case-insensitive)
    assert VeluneIgnore(plain, defaults=()).case_sensitive is (
        sys.platform not in ("win32", "darwin")
    )


def test_case_sensitivity_can_still_be_forced(tmp_path):
    build(tmp_path, {".gitignore": "Build/\n", "build/x.txt": ""})
    assert not matcher(tmp_path).is_ignored(tmp_path / "build" / "x.txt")
    assert matcher(tmp_path, case_sensitive=False).is_ignored(tmp_path / "build" / "x.txt")


# ── defaults ─────────────────────────────────────────────────────────────────


def test_defaults_hide_vcs_dependencies_and_secrets(tmp_path):
    build(
        tmp_path,
        {
            "src/main.py": "",
            "node_modules/x/index.js": "",
            ".env": "",
            "k/server.pem": "",
            ".git/config": "",
            "vendor/lib.go": "",
            "third_party/x/y.c": "",
            ".velune/state": "",
            "web/app.min.js": "",
            "web/app.min.css": "",
            "data/table.parquet": "",
        },
    )
    spec = load_ignore(tmp_path)
    for rel in ("src/main.py", "web/app.min.js", "web/app.min.css", "data/table.parquet"):
        assert not spec.is_ignored(tmp_path / rel), f"{rel} is the repository's call, not a default"
    for rel in (
        "node_modules/x/index.js",
        ".env",
        "k/server.pem",
        ".git/config",
        "vendor/lib.go",
        "third_party/x/y.c",
        ".velune/state",
    ):
        assert spec.is_ignored(tmp_path / rel), rel


def test_repository_rules_control_files_that_are_not_defaults(tmp_path):
    build(
        tmp_path,
        {".gitignore": "*.parquet\n*.min.js\n", "a.parquet": "", "b.min.js": "", "c.min.css": ""},
    )
    spec = load_ignore(tmp_path)
    assert spec.is_ignored(tmp_path / "a.parquet") and spec.is_ignored(tmp_path / "b.min.js")
    assert not spec.is_ignored(tmp_path / "c.min.css")


def test_every_default_has_a_rationale_and_the_policy_holds():
    from velune.tools.filesystem.ignore import DEFAULT_IGNORE_RULES

    patterns = [pattern for pattern, _ in DEFAULT_IGNORE_RULES]
    assert len(patterns) == len(set(patterns)), "duplicate default"
    assert all(why.strip() for _, why in DEFAULT_IGNORE_RULES), "a default without a rationale"
    assert _DEFAULT_PATTERNS == patterns
    # safety-critical and established exclusions stay
    assert {
        ".git/",
        ".hg/",
        ".svn/",
        ".env",
        ".env.*",
        "*.pem",
        "*.key",
        "node_modules/",
        "__pycache__/",
        ".velune/",
        "vendor/",
        "third_party/",
    } <= set(patterns)
    # convenience is not a reason to hide a file from a repository that wants it
    assert not {"*.parquet", "*.min.js", "*.min.css"} & set(patterns)


def test_a_workspace_rule_can_reinclude_a_default(tmp_path):
    build(tmp_path, {".gitignore": "!*.png\n", "logo.png": "", "pic.jpg": ""})
    spec = load_ignore(tmp_path)
    assert not spec.is_ignored(tmp_path / "logo.png")
    assert spec.is_ignored(tmp_path / "pic.jpg")


def test_default_ignore_reads_no_ignore_files(tmp_path):
    build(tmp_path, {".gitignore": "*.txt\n", "a.txt": "", "b.pyc": ""})
    spec = default_ignore(tmp_path)
    assert not spec.is_ignored(tmp_path / "a.txt")
    assert spec.is_ignored(tmp_path / "b.pyc")


def test_the_scanners_built_in_patterns_match_the_shared_defaults_except_the_documented_three():
    """Indexing keeps its own list; the file tools share one. They may differ only here."""
    only_indexing_hides = set(_HARDCODED_PATTERNS) - set(_DEFAULT_PATTERNS)
    assert only_indexing_hides == {"*.min.js", "*.min.css", "*.parquet"}


# ── safety and boundaries ────────────────────────────────────────────────────


def test_paths_outside_the_workspace_are_always_ignored(tmp_path):
    ws = tmp_path / "ws"
    build(ws, {"in.txt": ""})
    build(tmp_path, {"out.txt": ""})
    spec = load_ignore(ws)
    assert spec.is_ignored(tmp_path / "out.txt")
    assert spec.is_ignored(ws / ".." / "out.txt")
    assert spec.is_ignored(ws.parent)
    assert not spec.is_ignored(ws / "sub" / ".." / "in.txt")


def test_relative_paths_are_workspace_relative(tmp_path):
    build(tmp_path, {".gitignore": "*.log\n", "sub/a.log": "", "sub/a.txt": ""})
    spec = matcher(tmp_path)
    assert spec.is_ignored("sub/a.log")
    assert not spec.is_ignored("sub/a.txt")


def test_ignore_files_above_the_workspace_are_not_read(tmp_path):
    ws = tmp_path / "project"
    build(tmp_path, {".gitignore": "*.py\n"})
    build(ws, {"a.py": ""})
    assert not matcher(ws).is_ignored(ws / "a.py")


def test_a_very_deep_path_is_matched_without_recursion_or_slowness(tmp_path):
    build(tmp_path, {".gitignore": "*.tmp\n!/keep.tmp\n"})
    deep = "/".join(f"d{i}" for i in range(500))
    spec = matcher(tmp_path)

    started = time.perf_counter()
    assert spec.is_ignored(tmp_path / deep / "x.tmp", is_dir=False)
    assert not spec.is_ignored(tmp_path / deep / "x.py", is_dir=False)
    assert time.perf_counter() - started < 1.0


def test_the_workspace_root_itself_is_never_ignored(tmp_path):
    build(tmp_path, {".gitignore": "*\n"})
    assert not matcher(tmp_path).is_ignored(tmp_path)


def test_an_ignore_file_that_is_not_a_regular_file_is_skipped_not_fatal(tmp_path):
    build(tmp_path, {"a.txt": "", "sub/b.log": "", "sub/.gitignore": "*.log\n"})
    (tmp_path / ".gitignore").mkdir()  # a directory where the file should be
    spec = matcher(tmp_path)

    assert not spec.is_ignored(tmp_path / "a.txt")
    assert spec.is_ignored(tmp_path / "sub" / "b.log")  # other levels still apply
    assert any(".gitignore" in path and "regular" in why for path, why in spec.skipped)


def test_an_unreadable_ignore_file_is_skipped_not_fatal(tmp_path, monkeypatch):
    build(
        tmp_path,
        {".gitignore": "*.log\n", "a.log": "", "sub/.gitignore": "*.tmp\n", "sub/b.tmp": ""},
    )
    real = os.path.realpath

    def flaky(path, *args, **kwargs):
        if str(path).endswith(os.path.join("sub", ".gitignore")):
            raise PermissionError("access denied")
        return real(path, *args, **kwargs)

    monkeypatch.setattr(ignore_mod.os.path, "realpath", flaky)
    spec = matcher(tmp_path)

    assert spec.is_ignored(tmp_path / "a.log")  # the readable root file still works
    assert not spec.is_ignored(tmp_path / "sub" / "b.tmp")  # the unreadable one is skipped
    assert any("unreadable" in why for _, why in spec.skipped)


def test_an_oversized_ignore_file_is_skipped(tmp_path, monkeypatch):
    build(tmp_path, {".gitignore": "*.log\n" + "# padding\n" * 50, "a.log": ""})
    monkeypatch.setattr(ignore_mod, "MAX_IGNORE_FILE_BYTES", 64)
    spec = matcher(tmp_path)
    assert not spec.is_ignored(tmp_path / "a.log")
    assert any("larger than" in why for _, why in spec.skipped)


def test_an_ignore_file_with_undecodable_bytes_still_applies_its_valid_lines(tmp_path):
    build(tmp_path, {".gitignore": b"\xff\xfe\x80 junk\n*.log\n", "a.log": ""})
    assert matcher(tmp_path).is_ignored(tmp_path / "a.log")


def test_a_hostile_pattern_cannot_stall_matching(tmp_path):
    """``a*a*a*...b`` is exponential for a backtracking regex, and ``re`` cannot be interrupted."""
    name = "a" * 200
    patterns = ["(a+)+$", "a*" * 150 + "b", "[" * 500, "**/" * 40 + "x", "/".join(["**", "a"] * 30)]
    build(tmp_path, {".gitignore": "\n".join(patterns) + "\n"})  # the name need not exist on disk
    spec = matcher(tmp_path)

    started = time.perf_counter()
    result = spec.is_ignored(tmp_path / name, is_dir=False)
    elapsed = time.perf_counter() - started

    assert result is False
    assert elapsed < 1.0, f"matching took {elapsed:.1f}s"
    assert spec.diagnostics == [] or all("[" in d.get("pattern", "") for d in spec.diagnostics)


def test_many_double_stars_are_matched_not_dropped_and_in_bounded_time(tmp_path):
    """Forty ``**`` against a 400-directory path: valid, applied, and linear in the work."""
    pattern = "/".join(["**", "a"] * 40) + "/zzz.txt"
    build(tmp_path, {".gitignore": pattern + "\n"})
    spec = matcher(tmp_path)

    deep_hit = "/".join(["a"] * 400) + "/zzz.txt"
    deep_miss = "/".join(["a"] * 400) + "/zzz.py"
    short_miss = (
        "/".join(["a"] * 39) + "/zzz.txt"
    )  # too few ``a`` directories for forty ``a`` steps

    started = time.perf_counter()
    assert spec.is_ignored(tmp_path / deep_hit, is_dir=False)
    assert not spec.is_ignored(tmp_path / deep_miss, is_dir=False)
    assert not spec.is_ignored(tmp_path / short_miss, is_dir=False)
    assert time.perf_counter() - started < 1.0
    assert spec.diagnostics == [], "a valid pattern must not be reported as unsupported"


def test_adversarial_star_heavy_and_double_star_patterns_stay_bounded_and_correct(tmp_path):
    patterns = [
        "a*" * 60 + "b",  # exponential for a naive regex
        "*a" * 60 + "*",
        "/".join(["**", "*a*a*a*a*"] * 12) + "/**",
        "**/**/**/**/**/**/**/**/q",
    ]
    build(tmp_path, {".gitignore": "\n".join(patterns) + "\n"})
    spec = matcher(tmp_path)

    started = time.perf_counter()
    assert not spec.is_ignored(tmp_path / ("c" * 250), is_dir=False)  # no ``a``, ``b`` or ``q``
    assert spec.is_ignored(
        tmp_path / ("a" * 250), is_dir=False
    )  # at least sixty ``a``: ``*a`` x 60
    assert not spec.is_ignored(
        tmp_path / ("a" * 59 + "b"), is_dir=False
    )  # one ``a`` short of sixty
    assert spec.is_ignored(tmp_path / ("a" * 60 + "b"), is_dir=False)  # ``a*`` x 60 then ``b``
    assert spec.is_ignored(tmp_path / "x" / "y" / "q", is_dir=False)  # zero or more ``**`` then q
    deep = tmp_path / "/".join(["a"] * 100) / ("a" * 20 + "q")  # too few ``a`` per segment to match
    assert not spec.is_ignored(deep, is_dir=False)
    assert time.perf_counter() - started < 1.0
    assert spec.diagnostics == []


def test_a_pattern_that_cannot_be_applied_is_reported_with_file_line_and_reason(tmp_path):
    build(
        tmp_path,
        {
            ".gitignore": "ok.log\n[\nbad\\\n[z-a]\n[[:bogus:]]\n" + "x" * 5000 + "\nfine.tmp\n",
            "sub/.veluneignore": "# c\n[unclosed\n",
        },
    )
    spec = matcher(tmp_path)

    assert spec.is_ignored(tmp_path / "ok.log", is_dir=False)  # valid lines still apply
    assert spec.is_ignored(tmp_path / "fine.tmp", is_dir=False)
    assert not spec.is_ignored(tmp_path / "sub" / "z.tmp", is_dir=False)

    found = {(d["file"], d["line"]): d for d in spec.diagnostics}
    assert set(found) == {
        (".gitignore", 2),
        (".gitignore", 3),
        (".gitignore", 4),
        (".gitignore", 5),
        (".gitignore", 6),
        ("sub/.veluneignore", 2),
    }
    assert "unterminated" in found[(".gitignore", 2)]["problem"]
    assert "backslash" in found[(".gitignore", 3)]["problem"]
    assert "reversed range" in found[(".gitignore", 4)]["problem"]
    assert "unknown character class" in found[(".gitignore", 5)]["problem"]
    assert "longer than 4096" in found[(".gitignore", 6)]["problem"]
    assert all("matches nothing" in d["problem"] for d in found.values())
    assert len(found[(".gitignore", 6)]["pattern"]) <= 200  # long patterns are not echoed whole


def test_an_unapplied_rule_never_takes_effect_and_is_never_reported_as_applied(tmp_path):
    build(tmp_path, {".gitignore": "[\n", "[": "", "x": ""})
    spec = matcher(tmp_path)

    assert not spec.is_ignored(tmp_path / "[", is_dir=False), "an unusable rule must match nothing"
    assert not spec.is_ignored(tmp_path / "x", is_dir=False)
    warning = ignore_warning(spec)
    assert warning is not None and warning["details"][0]["pattern"] == "["
    assert "could NOT be applied" in warning["ignore_warning"]


def test_no_warning_when_every_rule_is_applied(tmp_path):
    build(tmp_path, {".gitignore": "*.log\n!keep.log\n**/a/**/b/**/c\n"})
    spec = matcher(tmp_path)
    spec.is_ignored(tmp_path / "x.log", is_dir=False)
    assert spec.diagnostics == [] and ignore_warning(spec) is None


def test_the_warning_lists_a_bounded_number_of_details_and_counts_the_rest(tmp_path):
    build(tmp_path, {".gitignore": "\n".join(f"[bad{i}" for i in range(50)) + "\n"})
    spec = matcher(tmp_path)
    spec.is_ignored(tmp_path / "x", is_dir=False)
    warning = ignore_warning(spec)
    assert warning is not None
    assert (
        len(warning["details"]) == MAX_DIAGNOSTICS_SHOWN
        and warning["more"] == 50 - MAX_DIAGNOSTICS_SHOWN
    )


def test_an_unreadable_ignore_file_is_reported_not_just_skipped(tmp_path):
    build(tmp_path, {"a.txt": ""})
    (tmp_path / ".gitignore").mkdir()
    spec = matcher(tmp_path)
    spec.is_ignored(tmp_path / "a.txt", is_dir=False)
    assert [d["file"] for d in spec.diagnostics] == [".gitignore"]
    assert "not applied" in spec.diagnostics[0]["problem"]


@pytest.mark.parametrize("seed", range(5))
def test_the_linear_star_matcher_agrees_with_a_plain_regex(seed):
    """Patterns with many ``*`` use a different translation; it must mean the same thing."""
    rnd = random.Random(seed)
    for _ in range(300):
        pattern = "".join(rnd.choice("ab*?") for _ in range(rnd.randint(3, 10)))
        if re.sub(r"\*+", "*", pattern).count("*") < 3:
            continue
        rule = ignore_mod._compile_line(pattern)
        assert rule is not None
        fast = re.compile(rule.regex, re.DOTALL)
        collapsed = re.sub(r"\*+", "*", pattern)
        plain = re.compile(
            "^(?:.*/)?"
            + "".join("[^/]*" if c == "*" else "[^/]" if c == "?" else c for c in collapsed)
            + "$",
            re.DOTALL,
        )
        for _ in range(40):
            name = "".join(rnd.choice("ab") for _ in range(rnd.randint(0, 9)))
            assert bool(fast.match(name)) == bool(plain.match(name)), (pattern, name)


def test_parse_ignore_text_skips_invalid_lines_without_raising():
    rules = parse_ignore_text("ok\n[\n!neg\n\n# c\nbad\\\n")
    assert [(r.negate) for r in rules] == [False, True]


@pytest.mark.skipif(os.name != "nt", reason="junctions are a Windows feature")
def test_an_ignore_file_reached_through_a_junction_leaving_the_workspace_is_not_read(tmp_path):
    ws, outside = tmp_path / "ws", tmp_path / "outside"
    build(ws, {"in.txt": ""})
    build(outside, {".gitignore": "*.txt\n", "secret.txt": ""})
    made = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(ws / "link"), str(outside)], capture_output=True
    )
    assert made.returncode == 0, made.stderr

    spec = matcher(ws)

    assert not spec.is_ignored(ws / "link" / "secret.txt")  # the outside rule was not applied
    assert any(path.endswith(os.path.join("link", ".gitignore")) for path, _ in spec.skipped)


def test_a_large_ignore_file_matches_correctly_and_quickly(tmp_path):
    """Plain names, anchored paths and ``*suffix`` rules are looked up, not run as regexes."""
    lines = []
    for i in range(3000):
        lines.append(
            [f"/dir{i}/", f"*.ext{i}", f"name{i}.txt", f"pre{i}_*.py", f"!keep{i}.ext{i}"][i % 5]
        )
    build(tmp_path, {".gitignore": "\n".join(lines) + "\n"})
    spec = matcher(tmp_path)

    assert spec.is_ignored(tmp_path / "dir5" / "x.txt", is_dir=False)  # via /dir5/
    assert spec.is_ignored(tmp_path / "a" / "b" / "f.ext1", is_dir=False)  # *.ext1
    assert spec.is_ignored(tmp_path / "x" / "name2.txt", is_dir=False)
    assert spec.is_ignored(tmp_path / "pre3_main.py", is_dir=False)
    assert not spec.is_ignored(
        tmp_path / "keep4.ext4", is_dir=False
    )  # a negation with nothing to undo
    assert not spec.is_ignored(tmp_path / "other" / "plain.py", is_dir=False)

    started = time.perf_counter()
    for i in range(2000):
        spec.is_ignored(tmp_path / f"d{i % 40}" / f"file{i}.py", is_dir=False)
    assert (time.perf_counter() - started) / 2000 < 0.002, (
        "more than 2 ms per path with 3,000 rules"
    )


# ── caching: each ignore file is read once, however many paths are checked ───


def test_ignore_files_are_read_once_per_directory_not_once_per_file(tmp_path, monkeypatch):
    files = {".gitignore": "*.log\n", "sub/.gitignore": "*.tmp\n"}
    files.update({f"sub/d{i % 5}/f{i}.txt": "" for i in range(300)})
    build(tmp_path, files)
    reads: list[tuple[str, str]] = []
    original = VeluneIgnore._read_ignore_file

    def counting(self, directory, name):
        reads.append((directory, name))
        return original(self, directory, name)

    monkeypatch.setattr(VeluneIgnore, "_read_ignore_file", counting)
    spec = matcher(tmp_path)
    for rel in files:
        spec.is_ignored(tmp_path / rel)
    for rel in files:  # and again: fully cached
        spec.is_ignored(tmp_path / rel)

    assert len(reads) == len(set(reads)), "an ignore file was read more than once"
    assert len(reads) <= 2 * (1 + 1 + 5)  # root, sub and the five d* directories, two names each
