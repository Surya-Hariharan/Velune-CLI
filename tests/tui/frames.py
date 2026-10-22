"""Deterministic terminal-frame capture, normalization, baselines and diffs.

A *frame* is the emulated screen at one instant: per-cell character and style
(fg, bg, bold, italics, underline, strike, reverse) plus the cursor. It is
compared to an approved baseline stored under ``tests/tui/baselines/``.

What is stored (and therefore reviewable in a normal ``git diff``):

* ``rows``    normalized row text, one string per row, right-trimmed;
* ``styles``  one run-length style signature per row (``None`` for rows that
              contained a masked dynamic value, so only their text is compared);
* ``cursor``  row, column, hidden.

What is *not* stored: temp paths, versions, elapsed times, token/cost counts and
anything key-shaped are replaced by fixed tokens (``MASKS``) before saving and
before comparing, so a baseline is identical across machines and runs.

This compares terminal *cells*, not pixels. There is intentionally no image
diff: the SVG/PNG renderings are drawn from these same cells, so a pixel diff
would only add font-rasterization noise from our reconstruction, never new
information about what a real terminal shows.
"""

from __future__ import annotations

import difflib
import json
import os
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

BASELINE_DIR = Path(__file__).parent / "baselines"
UPDATE_ENV = "VELUNE_TUI_UPDATE_BASELINES"
# Transient output (failed-frame dumps, debug screenshots) never goes in the repo.
ARTIFACT_DIR = Path(
    os.environ.get("VELUNE_TUI_ARTIFACTS") or Path(tempfile.gettempdir()) / "velune-tui-artifacts"
)

SCHEMA = 1

# (name, pattern, replacement). Order matters. Each replacement is a fixed token,
# so column positions to its right are deterministic only because the dynamic
# values sit at the end of their rows; rows that were masked skip style checks.
MASKS: tuple[tuple[str, re.Pattern[str], str], ...] = (
    ("path", re.compile(r"(?:~|[A-Za-z]:)?[/\\][^\s]*(?:[Tt]emp|tmp)[^\s]*"), "<PATH>"),
    ("version", re.compile(r"\bv\d+\.\d+\.\d+[\w.\-+]*"), "<VERSION>"),
    ("tokens", re.compile(r"\d[\d,]* tokens(?: · ~?\$[\d.]+)?"), "<N> tokens"),
    ("ctx", re.compile(r"\d+/\d+k"), "<CTX>"),
    ("elapsed", re.compile(r"\b\d+(?:\.\d+)?\s?(?:ms|s)\b"), "<T>"),
    ("clock", re.compile(r"\b\d{1,2}:\d{2}(?::\d{2})?\b"), "<CLOCK>"),
    ("key", re.compile(r"(?:sk-|gsk_|AIza)[A-Za-z0-9_\-]{12,}"), "<KEY>"),
)

_STYLE_FIELDS = ("fg", "bg", "bold", "italics", "underscore", "strikethrough", "reverse")


@dataclass
class Cursor:
    y: int
    x: int
    hidden: bool


@dataclass
class Frame:
    rows: list[str]
    styles: list[str | None]
    cursor: Cursor
    cols: int
    masked_rows: set[int] = field(default_factory=set)

    @property
    def n_rows(self) -> int:
        return len(self.rows)

    def text(self) -> str:
        return "\n".join(self.rows)

    def to_json(self) -> dict:
        return {
            "schema": SCHEMA,
            "size": [self.n_rows, self.cols],
            "cursor": {"row": self.cursor.y, "col": self.cursor.x, "hidden": self.cursor.hidden},
            "rows": self.rows,
            "styles": self.styles,
        }

    @classmethod
    def from_json(cls, data: dict) -> Frame:
        c = data["cursor"]
        return cls(
            rows=list(data["rows"]),
            styles=list(data["styles"]),
            cursor=Cursor(c["row"], c["col"], c["hidden"]),
            cols=data["size"][1],
            masked_rows={i for i, s in enumerate(data["styles"]) if s is None},
        )


# ── capture ──────────────────────────────────────────────────────────────────


def _cell_style(ch) -> tuple:
    return tuple(getattr(ch, f) for f in _STYLE_FIELDS)


def _fmt_style(t: tuple) -> str:
    fg, bg, *flags = t
    names = "".join(c for c, on in zip("biusr", flags, strict=True) if on)
    return f"{fg}/{bg}{'+' + names if names else ''}"


def _signature(line) -> str:
    """Run-length style signature for one buffer row: ``0-9:fg/bg+flags;10-119:...``."""
    runs: list[str] = []
    start, cur = 0, None
    width = max(line.keys(), default=-1) + 1
    for x in range(width):
        st = _cell_style(line[x])
        if st != cur:
            if cur is not None:
                runs.append(f"{start}-{x - 1}:{_fmt_style(cur)}")
            start, cur = x, st
    if cur is not None:
        runs.append(f"{start}-{width - 1}:{_fmt_style(cur)}")
    return ";".join(runs)


def normalize_text(text: str) -> tuple[str, bool]:
    """Apply ``MASKS``; return ``(normalized, was_masked)``."""
    masked = False
    for _name, pat, repl in MASKS:
        text, n = pat.subn(repl, text)
        masked = masked or n > 0
    return text.rstrip(), masked


def capture(session, *, extra_masks: tuple[re.Pattern[str], ...] = ()) -> Frame:
    """Snapshot *session*'s emulated screen as a normalized :class:`Frame`."""
    with session._lock:
        scr = session.screen
        raw_rows = [
            "".join(scr.buffer[y][x].data for x in range(scr.columns)) for y in range(scr.lines)
        ]
        sigs = [_signature(scr.buffer[y]) for y in range(scr.lines)]
        cur = Cursor(scr.cursor.y, scr.cursor.x, bool(scr.cursor.hidden))
        cols = scr.columns
    rows: list[str] = []
    styles: list[str | None] = []
    for raw, sig in zip(raw_rows, sigs, strict=True):
        text, masked = normalize_text(raw)
        for pat in extra_masks:
            text, n = pat.subn("<X>", text)
            masked = masked or n > 0
        rows.append(text.rstrip())
        styles.append(None if masked else sig)
    return Frame(rows, styles, cur, cols, {i for i, s in enumerate(styles) if s is None})


# ── diff ─────────────────────────────────────────────────────────────────────


def _expand(sig: str) -> dict[int, str]:
    out: dict[int, str] = {}
    for run in filter(None, sig.split(";")):
        span, _, st = run.partition(":")
        a, _, b = span.partition("-")
        for x in range(int(a), int(b) + 1):
            out[x] = st
    return out


def _text_changes(exp: str, act: str) -> list[str]:
    out = []
    sm = difflib.SequenceMatcher(None, exp, act, autojunk=False)
    for op, a0, a1, b0, b1 in sm.get_opcodes():
        if op == "equal":
            continue
        cols = f"col {a0}" if a1 - a0 <= 1 and b1 - b0 <= 1 else f"cols {a0}-{max(a0, a1 - 1)}"
        out.append(f"{cols}: {exp[a0:a1]!r} -> {act[b0:b1]!r}")
    return out


def _style_changes(exp: str, act: str) -> list[str]:
    e, a = _expand(exp), _expand(act)
    out: list[str] = []
    run: tuple[int, int, str, str] | None = None
    for x in sorted(set(e) | set(a)):
        pair = (e.get(x, "<none>"), a.get(x, "<none>"))
        if pair[0] == pair[1]:
            if run:
                out.append(_fmt_run(run))
                run = None
            continue
        if run and run[1] == x - 1 and (run[2], run[3]) == pair:
            run = (run[0], x, pair[0], pair[1])
        else:
            if run:
                out.append(_fmt_run(run))
            run = (x, x, pair[0], pair[1])
    if run:
        out.append(_fmt_run(run))
    return out


def _fmt_run(r: tuple[int, int, str, str]) -> str:
    cols = f"col {r[0]}" if r[0] == r[1] else f"cols {r[0]}-{r[1]}"
    return f"{cols}: style {r[2]} -> {r[3]}"


def diff_frames(expected: Frame, actual: Frame, *, cursor: bool = True) -> list[str]:
    """Human-readable differences; empty when the frames match."""
    out: list[str] = []
    if (expected.n_rows, expected.cols) != (actual.n_rows, actual.cols):
        out.append(
            f"size: expected {expected.n_rows}x{expected.cols}, got {actual.n_rows}x{actual.cols}"
        )
        return out
    for y in range(expected.n_rows):
        et, at = expected.rows[y], actual.rows[y]
        if et != at:
            out.append(f"row {y}: text differs")
            out.append(f"    expected: {et!r}")
            out.append(f"    actual:   {at!r}")
            out.extend(f"    {c}" for c in _text_changes(et, at))
            continue  # style of a changed row is noise; the text is the finding
        es, as_ = expected.styles[y], actual.styles[y]
        if es is None or as_ is None:
            continue
        if es != as_:
            out.append(f"row {y}: style differs  ({et[:60]!r})")
            out.extend(f"    {c}" for c in _style_changes(es, as_))
    if cursor and expected.cursor != actual.cursor:
        e, a = expected.cursor, actual.cursor
        out.append(
            f"cursor: expected (row {e.y}, col {e.x}, hidden={e.hidden}) "
            f"got (row {a.y}, col {a.x}, hidden={a.hidden})"
        )
    return out


# ── baselines ────────────────────────────────────────────────────────────────


def _dump(frame: Frame) -> str:
    # One row per line so reviews are ordinary line diffs.
    d = frame.to_json()
    head = {k: d[k] for k in ("schema", "size", "cursor")}
    lines = ["{", f'  "meta": {json.dumps(head)},', '  "rows": [']
    lines += [
        f"    {json.dumps(r, ensure_ascii=False)}{',' if i < len(d['rows']) - 1 else ''}"
        for i, r in enumerate(d["rows"])
    ]
    lines += ["  ],", '  "styles": [']
    lines += [
        f"    {json.dumps(s)}{',' if i < len(d['styles']) - 1 else ''}"
        for i, s in enumerate(d["styles"])
    ]
    lines += ["  ]", "}"]
    return "\n".join(lines) + "\n"


def _load(path: Path) -> Frame:
    data = json.loads(path.read_text(encoding="utf-8"))
    meta = data.pop("meta")
    return Frame.from_json({**meta, **data})


def baseline_path(name: str) -> Path:
    return BASELINE_DIR / f"{name}.json"


def write_baseline(name: str, frame: Frame) -> Path:
    BASELINE_DIR.mkdir(parents=True, exist_ok=True)
    p = baseline_path(name)
    p.write_text(_dump(frame), encoding="utf-8", newline="\n")
    return p


def dump_failure(name: str, actual: Frame) -> Path:
    """Write the actual frame for review, *outside the repository*."""
    ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
    p = ARTIFACT_DIR / f"{name}.actual.json"
    p.write_text(_dump(actual), encoding="utf-8", newline="\n")
    return p


def assert_frame(
    session,
    name: str,
    *,
    cursor: bool = True,
    extra_masks: tuple[re.Pattern[str], ...] = (),
) -> Frame:
    """Fail unless the live screen matches the approved baseline *name*.

    With ``VELUNE_TUI_UPDATE_BASELINES=1`` the baseline is (re)written instead;
    review it with ``git diff tests/tui/baselines`` before committing.
    """
    actual = capture(session, extra_masks=extra_masks)
    path = baseline_path(name)
    if os.environ.get(UPDATE_ENV) == "1":
        write_baseline(name, actual)
        return actual
    if not path.exists():
        dump_failure(name, actual)
        raise AssertionError(
            f"no approved baseline for {name!r} ({path}).\n"
            f"Review the frame, then run with {UPDATE_ENV}=1 to record it.\n"
            f"--- actual frame ---\n{actual.text()}"
        )
    expected = _load(path)
    problems = diff_frames(expected, actual, cursor=cursor)
    if problems:
        where = dump_failure(name, actual)
        raise AssertionError(
            f"visual regression in {name!r}: {sum(1 for p in problems if p.startswith(('row', 'cursor', 'size')))} "
            f"difference(s)\n" + "\n".join(problems) + f"\n(actual frame saved to {where})"
        )
    return actual
