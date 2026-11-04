"""PTY session driver: spawn the real CLI, send keys, read the emulated screen.

* Windows uses ConPTY via ``pywinpty``; POSIX uses ``ptyprocess``.
* A reader thread feeds every byte the program writes into a ``pyte`` terminal
  emulator, so assertions run against what a user would *see*, not raw escapes.
* ``snapshot()`` writes the screen as ``.txt`` and a colored ``.svg`` (rendered
  with Rich, which is already a core dependency) after redacting secrets.

Caveat: pyte has no alternate-screen buffer, so ``?1049h/l`` are ignored and
the emulator shows the live frame only. That is fine for asserting visible
state, but it cannot prove the shell scrollback is clean after exit (see README).
"""

from __future__ import annotations

import io
import os
import re
import sys
import threading
import time
from collections import deque
from pathlib import Path

import pyte

from .fake_backend import FakeBackend

IS_WINDOWS = sys.platform == "win32"
winpty = None
ptyprocess = None
if IS_WINDOWS:
    try:
        import winpty  # type: ignore[import-not-found,no-redef]
    except ImportError:  # pragma: no cover
        pass
else:
    try:
        import ptyprocess  # type: ignore[import-not-found,no-redef]
    except ImportError:  # pragma: no cover
        pass

BACKEND_AVAILABLE = (winpty is not None) if IS_WINDOWS else (ptyprocess is not None)

# Fake credential: wrong shape for every real provider, can never authenticate.
from .fake_backend import FAKE_KEY  # noqa: E402
from .frames import ARTIFACT_DIR  # noqa: E402  (transient output lives outside the repo)

KEYS = {
    "enter": "\r",
    "esc": "\x1b",
    "tab": "\t",
    "backspace": "\x7f",
    "up": "\x1b[A",
    "down": "\x1b[B",
    "right": "\x1b[C",
    "left": "\x1b[D",
    "pgup": "\x1b[5~",
    "pgdn": "\x1b[6~",
    "ctrl-c": "\x03",
    "ctrl-d": "\x04",
    "ctrl-l": "\x0c",
    "ctrl-u": "\x15",
}

_SECRET_RE = re.compile(r"(sk-[A-Za-z0-9_\-]{16,}|gsk_[A-Za-z0-9]{16,}|AIza[0-9A-Za-z_\-]{20,})")


def redact(text: str) -> str:
    return _SECRET_RE.sub("[REDACTED]", text)


# ── failure diagnostics (bounded) ────────────────────────────────────────────
_TAIL_CHUNKS = 200  # PTY chunks kept with their arrival time
_TAIL_CHUNK_CHARS = 2000  # per-chunk cap
_TAIL_CHARS = 8000  # cap on the tail printed in a failure report
# Stricter than ``_SECRET_RE``: any prefix of a key shape is masked, because the
# tail can end mid-token. NUL is allowed inside a token so a key split across two
# chunks (joined with NUL markers) is still caught.
_DIAG_SECRET_RE = re.compile(r"(?:sk-|gsk_|AIza|tui-test-passphrase)(?:[A-Za-z0-9_\-]|\x00)*")
_CTRL_RE = re.compile(r"[\x00-\x1f\x7f]")


def _diag_redact(text: str) -> str:
    """Mask secret-shaped tokens, keeping any NUL chunk markers so splitting still works."""
    return _DIAG_SECRET_RE.sub(lambda m: "[REDACTED]" + "\x00" * m.group().count("\x00"), text)


def _escape(chunk: str) -> str:
    """Render control characters visibly (``\\e`` for ESC) so one chunk is one line."""
    return _CTRL_RE.sub(
        lambda m: "\\e" if m.group() == "\x1b" else f"\\x{ord(m.group()):02x}", chunk
    )


def _clip(data: str) -> str:
    """Keep the end of an oversized chunk, without the half-token the cut may leave."""
    if len(data) <= _TAIL_CHUNK_CHARS:
        return data
    return re.sub(r"^[A-Za-z0-9_\-]{1,256}", "", data[-_TAIL_CHUNK_CHARS:])


def _exc_summary(exc: BaseException) -> str:
    """Type and a short, redacted message (never the traceback or any environment)."""
    return f"{type(exc).__name__}: {_diag_redact(_escape(str(exc)))[:200]}"


def _child_state(p: object) -> str:
    """Best-effort, non-blocking child status. Anything unavailable is ``unknown``.

    ``isalive`` is read first because ptyprocess updates its exit/signal status
    inside it. ``exitstatus`` is ``None`` while the child runs. Nothing here waits.
    """
    parts = []
    try:
        parts.append(f"alive={bool(p.isalive())}")  # type: ignore[attr-defined]
    except Exception as exc:
        parts.append(f"alive=unknown({type(exc).__name__})")
    for attr in ("exitstatus", "signalstatus"):
        try:
            parts.append(f"{attr}={getattr(p, attr)!r}")
        except AttributeError:
            if attr == "exitstatus":
                parts.append("exitstatus=unknown(unsupported)")
        except Exception as exc:
            parts.append(f"{attr}=unknown({type(exc).__name__})")
    return " ".join(parts)


class TermSession:
    def __init__(
        self,
        *,
        rows: int = 40,
        cols: int = 120,
        backend: FakeBackend | None,
        sandbox: Path,
        workdir: Path,
        extra_env: dict[str, str] | None = None,
        argv: list[str] | None = None,
        ollama: bool = False,
    ) -> None:
        self.rows, self.cols = rows, cols
        self.screen = pyte.Screen(cols, rows)
        self._stream = pyte.Stream(self.screen)
        self._lock = threading.Lock()
        self.raw = ""  # every byte written by the program (for secret scans)
        # Failure diagnostics only (never read by assertions). Bounded: the last
        # _TAIL_CHUNKS chunks with their arrival times, plus two scalars.
        self._t0 = time.monotonic()
        self._tail: deque[tuple[float, str]] = deque(maxlen=_TAIL_CHUNKS)
        self._chunk_count = 0
        self._last_output: float | None = None
        # How/when the reader stopped, and the child's state at that moment.
        self._reader_end: tuple[float, str, str, str] | None = None
        self.eof = False
        env = {
            k: v
            for k, v in os.environ.items()
            if not k.endswith("_API_KEY") and k not in ("HF_TOKEN", "NO_COLOR")
        }
        # ~/.velune holds the model cache, active model, model locations and
        # local-model registrations; point the home directory into the sandbox
        # too, so a journey can never read or write the developer's real ones.
        home = sandbox / "home"
        home.mkdir(parents=True, exist_ok=True)
        env.update(
            {
                "HOME": str(home),
                "USERPROFILE": str(home),
                "VELUNE_TUI_SANDBOX": str(sandbox),
                "VELUNE_DATA_HOME": str(sandbox / "data"),
                "VELUNE_MASTER_PASSPHRASE": "tui-test-passphrase",
                "TERM": "xterm-256color",
                "PYTHONIOENCODING": "utf-8",
                "PYTHONUTF8": "1",
            }
        )
        # Velune now honours OLLAMA_HOST everywhere (setup, liveness, discovery,
        # chat), so pointing it at the fake backend makes Ollama "running" —
        # a different starting state from the fresh, nothing-configured user
        # the baselines record. A local Ollama is therefore opt-in
        # (``ollama=True``); otherwise it points at a closed loopback port, so a
        # developer's real Ollama on :11434 can't leak into a journey either.
        env["OLLAMA_HOST"] = backend.host if (backend is not None and ollama) else "127.0.0.1:9"
        if backend is not None:
            env["VELUNE_TUI_FAKE_HOST"] = backend.host
        env.update(extra_env or {})
        cmd = [sys.executable, str(Path(__file__).parent / "_launcher.py"), *(argv or [])]
        spawn = winpty.PtyProcess.spawn if IS_WINDOWS else ptyprocess.PtyProcess.spawn
        self._p = spawn(cmd, dimensions=(rows, cols), env=env, cwd=str(workdir))
        self._t = threading.Thread(target=self._pump, daemon=True)
        self._t.start()

    # ── I/O ──────────────────────────────────────────────────────────────
    def _pump(self) -> None:
        end, detail = "empty-read", ""
        try:
            while True:
                try:
                    data = self._p.read(65536)
                except (EOFError, OSError) as exc:
                    end, detail = "read-raised", _exc_summary(exc)
                    break
                if not data:
                    break
                if isinstance(data, bytes):
                    data = data.decode("utf-8", "replace")
                now = time.monotonic() - self._t0
                with self._lock:
                    self.raw += data
                    self._stream.feed(data)
                    self._tail.append((now, _clip(data)))
                    self._chunk_count += 1
                    self._last_output = now
        except BaseException as exc:
            # Unchanged behaviour: the thread still dies and ``eof`` stays False.
            self._record_reader_end("exception", _exc_summary(exc))
            raise
        self._record_reader_end(end, detail)
        self.eof = True

    def _record_reader_end(self, end: str, detail: str) -> None:
        """Diagnostics only; must never raise (it runs on the reader thread)."""
        try:
            self._reader_end = (time.monotonic() - self._t0, end, detail, _child_state(self._p))
        except Exception:
            pass

    def send(self, text: str) -> None:
        self._p.write(text)

    def key(self, name: str) -> None:
        self.send(KEYS[name])

    def type(self, text: str, delay: float = 0.01) -> None:
        for ch in text:
            self.send(ch)
            time.sleep(delay)

    def resize(self, rows: int, cols: int) -> None:
        self.rows, self.cols = rows, cols
        self._p.setwinsize(rows, cols)
        with self._lock:
            self.screen.resize(rows, cols)

    # ── screen ───────────────────────────────────────────────────────────
    def text(self) -> str:
        with self._lock:
            return "\n".join(line.rstrip() for line in self.screen.display)

    def wait_for(self, needle: str | re.Pattern[str], timeout: float = 20.0) -> str:
        pat = needle if isinstance(needle, re.Pattern) else re.compile(re.escape(needle))
        start = time.monotonic()
        end = start + timeout
        while time.monotonic() < end:
            t = self.text()
            if pat.search(t):
                return t
            if self.eof:
                break
            time.sleep(0.05)
        raise AssertionError(self._wait_failure_report(pat.pattern, time.monotonic() - start))

    def _wait_failure_report(self, pattern: str, waited: float) -> str:
        """Timing and recent PTY output for a failed ``wait_for`` (diagnostic only).

        Everything is passed through ``_diag_redact`` and the PTY tail is bounded
        (``_TAIL_CHUNKS`` chunks, ``_TAIL_CHARS`` characters). The same report is
        also written under ``ARTIFACT_DIR`` so CI can upload it.
        """
        with self._lock:
            tail = list(self._tail)
            chunks, last = self._chunk_count, self._last_output
        age = time.monotonic() - self._t0
        quiet = "none" if last is None else f"{last:.1f}s (quiet for {age - last:.1f}s)"
        # Redact across chunk boundaries (a key may arrive split over two chunks):
        # join with NUL markers the secret pattern is allowed to span, then split.
        joined = _diag_redact("\x00".join(_escape(chunk) for _ts, chunk in tail))
        pieces = joined.split("\x00") if tail else []
        tail_text = "".join(f"[{ts:8.2f}s] {c}\n" for (ts, _), c in zip(tail, pieces, strict=True))
        if len(tail_text) > _TAIL_CHARS:  # keep whole lines only, never half a token
            tail_text = tail_text[-_TAIL_CHARS:]
            tail_text = tail_text[tail_text.find("\n") + 1 :]
        report = (
            f"timed out waiting for {pattern!r}\n"
            f"waited {waited:.1f}s; session age {age:.1f}s; last PTY output at {quiet}; "
            f"{chunks} chunks; eof={self.eof}\n"
            f"{self._reader_child_lines()}"
            f"--- screen ---\n{_diag_redact(self.text())}\n"
            f"--- last PTY output (escaped, newest last, up to {_TAIL_CHUNKS} chunks) ---\n"
            f"{tail_text}"
        )
        try:
            ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
            name = f"wait_for_failure_{os.getpid()}_{int(time.time() * 1000)}.txt"
            (ARTIFACT_DIR / name).write_text(report, encoding="utf-8")
        except OSError:
            pass  # diagnostics must never mask the real failure
        return report

    def _reader_child_lines(self) -> str:
        """Reader-thread and child-process state for the failure report."""
        try:
            end = self._reader_end
            thread = "alive" if self._t.is_alive() else "stopped"
            if end is None:
                reader = f"reader: still running (thread {thread})"
            else:
                when, how, detail, child = end
                reader = (
                    f"reader: ended at {when:.1f}s via {how}"
                    f"{' (' + detail + ')' if detail else ''}; thread {thread}"
                )
                child = f"child at reader end: {child}"
            now_child = f"child now: {_child_state(self._p)}"
            lines = [reader, child, now_child] if end is not None else [reader, now_child]
            return "\n".join(lines) + "\n"
        except Exception as exc:  # diagnostics must never mask the real failure
            return f"reader/child state: unknown ({type(exc).__name__})\n"

    def wait_gone(self, needle: str, timeout: float = 20.0) -> None:
        end = time.time() + timeout
        while time.time() < end:
            if needle not in self.text():
                return
            time.sleep(0.05)
        raise AssertionError(f"{needle!r} still on screen\n{self.text()}")

    def settle(self, quiet: float = 0.4, timeout: float = 10.0) -> str:
        """Block until the screen stops changing for *quiet* seconds."""
        last, since, end = self.text(), time.time(), time.time() + timeout
        while time.time() < end:
            cur = self.text()
            if cur != last:
                last, since = cur, time.time()
            elif time.time() - since >= quiet:
                break
            time.sleep(0.05)
        return last

    def count(self, needle: str) -> int:
        return self.text().count(needle)

    # ── lifecycle ────────────────────────────────────────────────────────
    def alive(self) -> bool:
        return bool(self._p.isalive())

    def wait_exit(self, timeout: float = 15.0) -> bool:
        end = time.time() + timeout
        while time.time() < end:
            if not self.alive():
                return True
            time.sleep(0.05)
        return False

    def close(self) -> None:
        try:
            if self.alive():
                self._p.terminate(force=True)
        except Exception:
            pass

    # ── evidence ─────────────────────────────────────────────────────────
    def snapshot(self, name: str) -> Path:
        """Write ``<name>.txt`` and a colored ``<name>.svg`` (secrets redacted)."""
        from rich.console import Console
        from rich.style import Style
        from rich.text import Text

        ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
        txt = ARTIFACT_DIR / f"{name}.txt"
        txt.write_text(redact(self.text()) + "\n", encoding="utf-8")

        def _col(c: str) -> str | None:
            if c == "default":
                return None
            if re.fullmatch(r"[0-9a-fA-F]{6}", c):
                return f"#{c}"
            # pyte names the 16 ANSI colours its own way ("brown" for 33,
            # "brightred" for 91); translate to Rich's names.
            c = {"brown": "yellow", "brightbrown": "bright_yellow"}.get(c, c)
            return f"bright_{c[6:]}" if c.startswith("bright") and "_" not in c else c

        con = Console(
            record=True,
            width=self.cols,
            file=io.StringIO(),
            force_terminal=True,
            color_system="truecolor",
        )
        with self._lock:
            rows = []
            for y in range(self.screen.lines):
                line = self.screen.buffer[y]
                t = Text(no_wrap=True)
                for x in range(self.screen.columns):
                    ch = line[x]
                    t.append(
                        redact(ch.data) if len(ch.data) > 1 else ch.data,
                        Style(
                            color=_col(ch.fg), bgcolor=_col(ch.bg), bold=ch.bold, reverse=ch.reverse
                        ),
                    )
                rows.append(t)
        for t in rows:
            con.print(t)
        svg = con.export_svg(title=name)
        (ARTIFACT_DIR / f"{name}.svg").write_text(svg, encoding="utf-8")
        return txt


def assert_no_secret(session: TermSession, secret: str = FAKE_KEY) -> None:
    assert secret not in session.raw, "plaintext credential was echoed to the terminal"
    assert secret not in session.text()
