"""Apply an accepted Velune update once no Velune process is running.

Replacing a running installation destroys it: on Windows uv/pip fail part-way with
"Access is denied" after deleting files (P2.0 spike, see ``safety.py``). So this helper,
started detached by :mod:`velune.lifecycle.autoupdate` as Velune exits:

1. waits until every watched executable (the ``velune`` launcher, and a uv tool's
   environment ``python.exe``) can be opened for writing. Windows refuses that while
   the program runs, which is exactly the condition that breaks an update. On POSIX
   nothing is locked, so it waits for the parent process only. It gives up, changing
   nothing, after ``WAIT_TIMEOUT_S``;
2. runs the update command (argument list, no shell; output never stored);
3. verifies with ``python -m velune --version``, runs the repair command once if that
   fails, and verifies again;
4. records the outcome in the state file for the next Velune start.

Standard library only, never imports ``velune``: it runs under the *base* interpreter
in isolated mode so it holds none of the installation's files open.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from typing import Any

WAIT_TIMEOUT_S = 600.0
POLL_S = 1.0
COMMAND_TIMEOUT_S = 900.0
VERIFY_TIMEOUT_S = 120.0
STALE_LOCK_S = WAIT_TIMEOUT_S + 3 * COMMAND_TIMEOUT_S

Runner = Callable[[Sequence[str], float], tuple[int, str]]


def _locked(path: str) -> bool:
    """True while a running program holds *path* (opening for append writes nothing)."""
    try:
        with open(path, "ab"):
            return False
    except PermissionError:
        return True
    except OSError:
        return False  # missing or unreadable: nothing to wait for


def _alive(pid: int | None) -> bool:
    if not pid or os.name == "nt":  # on Windows the locked files are the signal
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    return True


def in_use(spec: dict[str, Any]) -> bool:
    return _alive(spec.get("parent_pid")) or any(_locked(f) for f in spec.get("watch_files", ()))


def wait_until_clear(
    spec: dict[str, Any],
    *,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> bool:
    """True once Velune has exited; False if ``wait_timeout`` passes first."""
    deadline = clock() + float(spec.get("wait_timeout", WAIT_TIMEOUT_S))
    while in_use(spec):
        if clock() >= deadline:
            return False
        sleep(POLL_S)
    sleep(POLL_S)  # Windows releases handles a moment after a process ends
    return not in_use(spec)


def run_command(argv: Sequence[str], timeout: float) -> tuple[int, str]:
    """Run *argv* without a shell or console window; ``(exit code, stdout)``, never logged."""
    flags = 0x08000000 if os.name == "nt" else 0  # CREATE_NO_WINDOW
    try:
        done = subprocess.run(
            list(argv), stdin=subprocess.DEVNULL, capture_output=True, text=True,
            timeout=timeout, check=False, creationflags=flags,
        )  # fmt: skip
    except (OSError, subprocess.SubprocessError):
        return -1, ""
    return done.returncode, done.stdout or ""


def verified(spec: dict[str, Any], run: Runner) -> bool:
    """The installed Velune starts and reports exactly the target version."""
    code, out = run(spec["verify_command"], VERIFY_TIMEOUT_S)
    return code == 0 and f"v{spec['target_version']}" in out.split()


def apply_update(
    spec: dict[str, Any],
    *,
    run: Runner = run_command,
    log: Callable[[str], None] = lambda _msg: None,
) -> dict[str, Any]:
    """Run the update, and at most one repair. Returns the result record."""
    target = spec["target_version"]
    for step, argv in (("update", spec["command"]), ("repair", spec.get("repair_command"))):
        if not argv:
            continue
        code, _ = run(argv, COMMAND_TIMEOUT_S)
        log(f"{step} command exited {code}")
        if verified(spec, run):
            note = "" if step == "update" else " (after one repair)"
            return _result("updated", spec, f"Updated Velune to {target}{note}.")
    return _result(
        "failed",
        spec,
        f"The automatic update to {target} could not be verified. "
        "Close every Velune window and run the command below in a fresh terminal.",
    )


def _result(status: str, spec: dict[str, Any], message: str) -> dict[str, Any]:
    return {
        "status": status,
        "from_version": spec.get("from_version"),
        "target_version": spec.get("target_version"),
        "message": message,
        "manual_command": spec.get("manual_command"),
        "at": time.time(),
    }


def record_result(state_path: str, result: dict[str, Any]) -> None:
    try:
        with open(state_path, encoding="utf-8") as fh:
            state = json.load(fh)
    except (OSError, ValueError):
        state = {}
    if not isinstance(state, dict):
        state = {}
    state.update(last_result=result, pending=None)
    tmp = f"{state_path}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=2)
    os.replace(tmp, state_path)


def acquire_lock(lock_path: str, *, now: Callable[[], float] = time.time) -> bool:
    """Create *lock_path* exclusively; a lock older than any real run is taken over."""
    os.makedirs(os.path.dirname(lock_path), exist_ok=True)
    for _ in range(2):
        try:
            os.close(os.open(lock_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600))
            return True
        except FileExistsError:
            try:
                if now() - os.path.getmtime(lock_path) < STALE_LOCK_S:
                    return False
                os.unlink(lock_path)
            except OSError:
                return False
    return False


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    try:
        spec = json.loads(args[0]) if len(args) == 1 else None
    except ValueError:
        spec = None
    if not isinstance(spec, dict) or not all(
        spec.get(k)
        for k in ("command", "verify_command", "target_version", "state_path", "lock_path")
    ):
        return 2

    log_path = spec.get("log_path")

    def log(message: str) -> None:
        if log_path:
            try:
                with open(log_path, "a", encoding="utf-8") as fh:
                    fh.write(time.strftime("%Y-%m-%dT%H:%M:%SZ ", time.gmtime()) + message + "\n")
            except OSError:
                pass

    if not acquire_lock(spec["lock_path"]):
        log("another update is already running; nothing done")
        return 0
    try:
        log(f"waiting for Velune to exit before updating to {spec['target_version']}")
        if not wait_until_clear(spec):
            log("Velune was still running when the wait ended; nothing changed")
            return 0  # `pending` stays set: the next exit tries again
        result = apply_update(spec, log=log)
        log(f"result: {result['status']}")
        record_result(spec["state_path"], result)
        return 0 if result["status"] == "updated" else 1
    finally:
        try:
            os.unlink(spec["lock_path"])
        except OSError:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
