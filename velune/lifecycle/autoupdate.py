"""Ask-first automatic updates.

``[update] auto = "ask"`` (default): an interactive session asks PyPI at most once per
``check_interval_hours`` whether a newer release exists (one metadata request via
:func:`velune.lifecycle.check.run_update_check`). Velune then asks once per version;
*no* is remembered. *Yes* marks the update ``pending``, and on exit
:func:`spawn_pending` starts :mod:`velune.lifecycle.post_exit_updater`, which waits until
no Velune is running before it touches the installation (replacing a running install is
what destroyed environments in the P2.0 spike, see ``safety.py``). The next session
reports the outcome once.

``"notify"`` only prints a notice; ``"off"``, no terminal, ``CI`` or
``VELUNE_NO_UPDATE_CHECK=1`` make no request. Only an unpinned uv tool installed from the
index and pip ``--user`` / venv installs are updated automatically.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from velune.core.paths import app_data_root
from velune.lifecycle.errors import ExitCode, LifecycleError, LifecycleResult
from velune.lifecycle.ownership import DISTRIBUTION, OwnershipKind, OwnershipReport

_DETACHED = 0x00000008 | 0x00000200 | 0x08000000  # DETACHED_PROCESS | NEW_PROCESS_GROUP | NO_WINDOW


def state_path() -> Path:
    return app_data_root() / "lifecycle" / "update_state.json"


def load_state(path: Path | None = None) -> dict[str, Any]:
    try:
        data = json.loads((path or state_path()).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save_state(state: dict[str, Any], path: Path | None = None) -> None:
    target = path or state_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f"{target.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    os.replace(tmp, target)


def due(
    mode: str,
    interval_hours: float,
    state: Mapping[str, Any],
    *,
    now: float | None = None,
    environ: Mapping[str, str] | None = None,
    interactive: bool | None = None,
) -> bool:
    """Whether this session should ask PyPI for a newer release."""
    env = os.environ if environ is None else environ
    if mode == "off" or env.get("VELUNE_NO_UPDATE_CHECK", "").strip() not in ("", "0"):
        return False
    if env.get("CI", "").strip().lower() not in ("", "0", "false"):
        return False
    if not (sys.stdin.isatty() and sys.stdout.isatty() if interactive is None else interactive):
        return False
    last = state.get("last_check")
    if not isinstance(last, (int, float)):
        return True
    return (time.time() if now is None else now) - last >= interval_hours * 3600


@dataclass(frozen=True)
class Plan:
    """What the post-exit helper runs, and which executables it waits for."""

    command: list[str]
    repair_command: list[str] | None
    verify_command: list[str]
    watch_files: list[str]
    manual_command: str


def _launchers() -> list[str]:
    found = {os.path.abspath(p) for p in (shutil.which("velune"),) if p}
    argv0 = sys.argv[0] if sys.argv else ""
    if os.path.basename(argv0).lower().startswith("velune") and os.path.isfile(argv0):
        found.add(os.path.abspath(argv0))
    return sorted(found)


def plan_for(
    report: OwnershipReport,
    target_version: str,
    *,
    which: Callable[[str], str | None] | None = None,
    executable: str | None = None,
) -> Plan | None:
    """How to update this installation automatically, or ``None`` if it must stay manual."""
    from velune.lifecycle.safety import manual_command, pip_update_command

    python = executable or sys.executable
    verify = [python, "-m", "velune", "--version"]
    if report.kind is OwnershipKind.UV_TOOL:
        if report.specifier or report.installed_from_local_file or report.uv_problem:
            return None  # `uv tool upgrade` would not move it, or would switch its source
        from velune.lifecycle.uvpath import trusted_which

        uv = (which or trusted_which)("uv")
        if not uv:
            return None
        extras = f"[{','.join(report.extras)}]" if report.extras else ""
        return Plan(
            command=[uv, "tool", "upgrade", DISTRIBUTION],
            repair_command=[
                uv,
                "tool",
                "install",
                "--force",
                f"{DISTRIBUTION}{extras}=={target_version}",
            ],  # fmt: skip
            verify_command=verify,
            watch_files=[*_launchers(), python],  # the env's python.exe is locked while Velune runs
            manual_command=manual_command("update", report),
        )
    if report.kind in (OwnershipKind.PIP_USER, OwnershipKind.PIP_VENV):
        user = ["--user"] if report.kind is OwnershipKind.PIP_USER else []
        return Plan(
            command=[python, "-m", "pip", "install", "--upgrade", *user, DISTRIBUTION],
            repair_command=None,
            verify_command=verify,
            watch_files=_launchers(),  # the interpreter is shared; the launcher is what locks
            manual_command=pip_update_command(report, executable=python),
        )
    return None


@dataclass(frozen=True)
class Offer:
    installed: str
    latest: str
    plan: Plan | None  # None: this installation can only be updated by hand
    manual_command: str | None
    guidance: str | None


def check_for_update(
    *,
    run_check: Callable[[], LifecycleResult] | None = None,
    detect: Callable[[], OwnershipReport] | None = None,
    path: Path | None = None,
    force: bool = False,
) -> Offer | None:
    """Ask the index once and record the time. Never raises.

    Returns an offer for a newer version that is neither declined nor already pending
    (``force`` ignores both, for an explicit ``velune update``).
    """
    try:
        from velune.lifecycle.check import run_update_check
        from velune.lifecycle.ownership import detect_ownership

        result = (run_check or run_update_check)()
        state = load_state(path)
        state["last_check"] = time.time()
        save_state(state, path)
        if result.exit_code is not ExitCode.UPDATE_AVAILABLE:
            return None
        latest = str(result.result["latest_version"])
        pending = state.get("pending") or {}
        if not force and latest in (state.get("declined_version"), pending.get("target_version")):
            return None
        plan = plan_for((detect or detect_ownership)(), latest)
        return Offer(
            installed=str(result.result["installed_version"]),
            latest=latest,
            plan=plan,
            manual_command=plan.manual_command if plan else result.manual_command,
            guidance=None if plan else result.recovery,
        )
    except Exception:
        return None


def decline(offer: Offer, path: Path | None = None) -> None:
    state = load_state(path)
    state["declined_version"] = offer.latest
    save_state(state, path)


def accept(offer: Offer, path: Path | None = None) -> bool:
    """Mark *offer* to be installed after exit. False if this installation is manual-only."""
    if offer.plan is None:
        return False
    state = load_state(path)
    state["pending"] = asdict(offer.plan) | {
        "from_version": offer.installed,
        "target_version": offer.latest,
    }
    save_state(state, path)
    return True


def take_last_result(path: Path | None = None) -> dict[str, Any] | None:
    """The outcome of the last post-exit update, returned once and then forgotten."""
    state = load_state(path)
    result = state.pop("last_result", None)
    if result is not None:
        try:
            save_state(state, path)
        except OSError:
            pass
    return result if isinstance(result, dict) else None


def _base_interpreter() -> str:
    """An interpreter outside the installation (every uv tool is a venv); pythonw on Windows."""
    base = getattr(sys, "_base_executable", None) or sys.executable
    windowless = os.path.join(os.path.dirname(base), "pythonw.exe")
    return windowless if os.name == "nt" and os.path.isfile(windowless) else base


def spawn_pending(
    *,
    path: Path | None = None,
    parent_pid: int | None = None,
    popen: Callable[..., Any] = subprocess.Popen,
) -> bool:
    """Start the detached post-exit helper if an update is pending. Never raises.

    The helper runs from its installed path: Python reads a script and closes it, so the
    running helper holds no file of the installation open.
    """
    try:
        target = path or state_path()
        pending = load_state(target).get("pending")
        if not isinstance(pending, dict) or not pending.get("command"):
            return False
        spec = pending | {
            "parent_pid": os.getpid() if parent_pid is None else parent_pid,
            "state_path": str(target),
            "lock_path": str(target.with_name("post_exit_update.lock")),
            "log_path": str(target.with_name("update.log")),
        }
        detach: dict[str, Any] = (
            {"creationflags": _DETACHED} if os.name == "nt" else {"start_new_session": True}
        )
        helper = Path(__file__).with_name("post_exit_updater.py")
        popen(
            [_base_interpreter(), "-I", str(helper), json.dumps(spec)],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            close_fds=True, cwd=str(target.parent), **detach,
        )  # fmt: skip
        return True
    except Exception:
        return False


def run_scheduled_update(
    *,
    run_check: Callable[[], LifecycleResult] | None = None,
    detect: Callable[[], OwnershipReport] | None = None,
    spawn: Callable[[], bool] | None = None,
    path: Path | None = None,
) -> LifecycleResult:
    """``velune update`` on Windows: hand the update to the post-exit helper. Never raises."""
    from velune.lifecycle import journal as journal_mod
    from velune.lifecycle.check import run_update_check

    try:
        found = (run_check or run_update_check)()
        if found.exit_code is not ExitCode.UPDATE_AVAILABLE:
            return found
        existing = journal_mod.read_journal()
        if existing.state != "missing":
            raise journal_mod.retained_error(journal_mod.default_journal_path(), existing)
        offer = check_for_update(run_check=lambda: found, detect=detect, path=path, force=True)
        if offer is None or not accept(offer, path):
            return found  # manual-only installation: the check's own guidance applies
        if not (spawn or (lambda: spawn_pending(path=path)))():
            raise LifecycleError(
                ExitCode.ERROR,
                "The updater could not be started. Nothing was changed.",
                recovery="Close Velune, open a fresh terminal, and run the command below.",
                manual_command=found.manual_command,
            )
        return LifecycleResult(
            "update",
            message=(
                f"Updating Velune {offer.installed} -> {offer.latest} as soon as this command "
                "exits (and every other Velune window is closed). The next start reports "
                "the result."
            ),
            installation=found.installation,
            result=dict(found.result) | {"scheduled": True},
        )
    except (Exception, KeyboardInterrupt) as exc:  # mapped to a result, like the other commands
        return LifecycleResult.from_exception("update", exc)
