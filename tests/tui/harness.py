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
        while True:
            try:
                data = self._p.read(65536)
            except (EOFError, OSError):
                break
            if not data:
                break
            if isinstance(data, bytes):
                data = data.decode("utf-8", "replace")
            with self._lock:
                self.raw += data
                self._stream.feed(data)
        self.eof = True

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
        end = time.time() + timeout
        while time.time() < end:
            t = self.text()
            if pat.search(t):
                return t
            if self.eof:
                break
            time.sleep(0.05)
        raise AssertionError(
            f"timed out waiting for {pat.pattern!r}\n--- screen ---\n{self.text()}"
        )

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
