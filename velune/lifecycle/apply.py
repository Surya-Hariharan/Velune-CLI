"""``velune update``: apply an available update to a confirmed uv-managed tool.

The smallest workflow that is safe, in order, refusing at the first problem and
always *before* anything is changed:

1. Prove ownership (P2.1): only a confirmed uv tool is ever changed.
2. Windows: never replace the running installation (P2.1 guard). Manual only.
3. Refuse the cases whose only uv command would silently change the install's
   release channel or source: a pinned install, an install from a local file,
   and ``--pre``. They get the exact manual command instead.
4. Refuse if a journal already exists (an earlier operation may be unfinished).
5. Find the update with the P2.2 check (one index request). Up to date, or any
   index/network problem, ends here with nothing changed.
6. Find uv on PATH.
7. Journal ``planned`` -> ``applying``, then run
   ``uv tool upgrade velune-cli`` (argument list, no shell, output not shown).
8. **Inspect, do not trust, the result.** uv's exit code is not proof of the
   installation state (P2.0: exit 1 after a successful upgrade, exit 0 on a
   damaged environment), and neither is the version: an interrupted Linux
   upgrade (P2.3.1) left the *new* version's metadata next to missing files.
   Ownership, the version and the installed files (``integrity``) are re-checked.

Outcomes after step 7 (the journal is removed only when the state is known):

* ownership intact, version newer, files match RECORD   -> applied (journal cleared)
* ownership intact, version same, files match RECORD and the set of installed
  distributions is unchanged                            -> not applied, verified
  unchanged (cleared)
* anything else (including a newer or unchanged version whose files fail
  verification or cannot be checked)                    -> state unknown: journal
  kept, no automatic recovery attempted, manual recovery shown

Cancellation (Ctrl-C) never kills uv part-way: uv runs in its own session, so the
terminal's interrupt does not reach it, and Velune waits (bounded) for it to
finish. A second Ctrl-C, or the bound expiring, stops uv, and the checks above
then decide what state that left.

There is deliberately **no rollback**. A reinstall of the old version is just
another mutation of a possibly damaged environment, and nothing here can prove
it would succeed; claiming otherwise would be false. Not atomic: uv replaces
files in place.

The command only changes the uv tool environment and the journal. It passes no
index, source or version options to uv (the user's own uv configuration applies,
and ``index_override_warnings`` says so), and reads no configuration, credentials
or memory. uv's output and exception text are never shown: they can carry URLs,
proxy credentials or local paths.

Not tested on macOS/Linux; see ``safety.py``. No real upgrade is run by the test
suite.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from velune.lifecycle import journal as journal_mod
from velune.lifecycle.check import run_update_check
from velune.lifecycle.errors import ExitCode, LifecycleError, LifecycleResult
from velune.lifecycle.integrity import Integrity, check_installation
from velune.lifecycle.ownership import (
    DISTRIBUTION,
    OwnershipKind,
    OwnershipReport,
    detect_ownership,
    require_uv_tool,
)
from velune.lifecycle.safety import _requirement, check_not_self_mutation, manual_command
from velune.lifecycle.versions import VersionTools, load_version_tools

COMMAND = "update"
UV_TIMEOUT = 900.0
CANCEL_GRACE = 120.0  # how long a cancel request waits for uv to finish its change

Runner = Callable[..., Any]


def _stop(process: subprocess.Popen[Any]) -> None:
    process.kill()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:  # pragma: no cover - kill is not catchable
        pass


def _run_uv(argv: list[str], *, timeout: float) -> subprocess.CompletedProcess[str]:
    """Run uv without ever killing it merely because the user pressed Ctrl-C.

    Interrupting uv during its file copy left a broken installation in testing
    (P2.3.1), and ``subprocess.run`` kills the child on ``KeyboardInterrupt``.
    Here uv runs in its own session (the terminal's SIGINT does not reach it), a
    first interrupt waits up to ``CANCEL_GRACE`` for it to finish, and
    ``KeyboardInterrupt`` is re-raised afterwards so the caller still reports the
    cancellation. A second interrupt, the grace expiring, or the overall timeout
    stops uv. Output is discarded at the OS level: it is never shown.
    """
    process = subprocess.Popen(
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=(os.name == "posix"),
    )
    try:
        process.wait(timeout=timeout)
    except KeyboardInterrupt:
        print(
            "Cancel requested. Waiting for uv to finish so the installation is not left "
            "half-updated (press Ctrl-C again to stop it immediately).",
            file=sys.stderr,
        )
        try:
            process.wait(timeout=CANCEL_GRACE)
        except (KeyboardInterrupt, subprocess.TimeoutExpired):
            _stop(process)
        raise KeyboardInterrupt from None
    except BaseException:
        _stop(process)
        raise
    return subprocess.CompletedProcess(argv, process.returncode)


def _refuse(message: str, recovery: str, command: str | None, reason: str) -> LifecycleError:
    return LifecycleError(
        ExitCode.REFUSED,
        f"{message} Nothing was changed.",
        recovery=recovery,
        manual_command=command,
        details={"reason": reason},
    )


def _channel_guard(report: OwnershipReport, include_prereleases: bool) -> None:
    """Refuse updates whose uv command would change the pin, source or release channel."""
    if include_prereleases:
        raise _refuse(
            "Applying a pre-release is not supported: the only verified way to install one "
            "pins it exactly, which would change this installation's release channel.",
            "Use 'velune update --check --pre' to see it, or run the command below yourself.",
            None,
            "prerelease_apply_unsupported",
        )
    if report.specifier:
        raise _refuse(
            "This installation is pinned, and 'uv tool upgrade' would not move it. Replacing "
            "a pin changes the release channel, so Velune will not do it for you.",
            "To move the pin yourself, run the command below.",
            manual_command("update", report),
            "pinned_install",
        )
    if report.installed_from_local_file:
        raise _refuse(
            "This copy was installed from a local file. Switching it to the package index "
            "changes where it comes from, so Velune will not do it for you.",
            "To switch to the package index yourself, run the command below.",
            manual_command("update", report),
            "local_file_install",
        )


def _inspect(
    detect: Callable[[], OwnershipReport], tools: VersionTools
) -> tuple[OwnershipReport | None, Any | None]:
    """Re-detect the installation: ``(report, installed Version)``, ``None`` where unreadable."""
    try:
        report = detect()
    except Exception:
        return None, None
    if report.kind is not OwnershipKind.UV_TOOL or not report.version:
        return report, None
    try:
        return report, tools.Version(report.version)
    except tools.InvalidVersion:
        return report, None


def _recovery_command(entry: journal_mod.JournalEntry) -> str:
    """Reinstall at the original channel (unpinned, original extras). Shown, never run."""
    return f"uv tool install --force {_requirement(entry.extras)}"


def run_update(
    *,
    include_prereleases: bool = False,
    detect: Callable[[], OwnershipReport] = detect_ownership,
    load_tools: Callable[[], VersionTools] = load_version_tools,
    fetch: Callable[[], Mapping[str, Any]] | None = None,
    run: Runner = _run_uv,
    which: Callable[[str], str | None] = shutil.which,
    integrity: Callable[[], Integrity] = check_installation,
    journal_path: Path | None = None,
    python: tuple[int, int, int] | None = None,
    platform: str | None = None,
    current_prefix: str | Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> LifecycleResult:
    """Apply the update; never raises (``SystemExit`` excepted)."""
    installation: dict[str, Any] = {}
    entry: journal_mod.JournalEntry | None = None
    baseline: Integrity | None = None
    path = journal_path or journal_mod.default_journal_path()
    try:
        report = detect()
        installation = report.installation
        require_uv_tool(report)
        check_not_self_mutation("update", report, platform=platform, current_prefix=current_prefix)
        _channel_guard(report, include_prereleases)

        existing = journal_mod.read_journal(path)
        if existing.state != "missing":
            raise journal_mod.retained_error(path, existing)

        kwargs: dict[str, Any] = {"python": python, "platform": platform, "environ": environ}
        if fetch is not None:
            kwargs["fetch"] = fetch
        found = run_update_check(detect=lambda: report, load_tools=load_tools, **kwargs)
        if found.exit_code is not ExitCode.UPDATE_AVAILABLE:
            return found  # up to date, newer than the index, or the check itself failed
        target = found.result["latest_version"]

        uv = which("uv")
        if not uv:
            raise LifecycleError(
                ExitCode.ERROR,
                "uv was not found on PATH, so the update cannot be applied. Nothing was changed.",
                recovery="Install uv or fix PATH, or run the command below yourself.",
                manual_command=manual_command("update", report),
                details={"reason": "uv_not_found"},
            )

        try:  # what "unchanged" must later be compared against; not fatal if unavailable
            baseline = integrity()
        except Exception:
            baseline = None

        py = python or (sys.version_info.major, sys.version_info.minor, sys.version_info.micro)
        entry = journal_mod.new_entry(
            "update", report, python_version=".".join(map(str, py)), to_version=target
        )
        journal_mod.write_journal(entry, path)
        entry = journal_mod.advance_phase(entry, "applying", path)
    except BaseException as exc:  # before any mutation: nothing to inspect or recover
        if entry is not None:  # interrupted between writing the journal and starting uv
            journal_mod.clear_journal(entry, path)
        if isinstance(exc, SystemExit):
            raise
        return LifecycleResult.from_exception(COMMAND, exc, installation=installation)

    # ---- mutation starts here --------------------------------------------------
    cancelled = False
    try:
        run([uv, "tool", "upgrade", DISTRIBUTION], timeout=UV_TIMEOUT)
    except KeyboardInterrupt:
        cancelled = True
    except (OSError, subprocess.SubprocessError):
        pass  # uv never ran, or was stopped; the inspection below decides what happened
    except BaseException as exc:
        if isinstance(exc, SystemExit):
            raise
        return LifecycleResult.from_exception(COMMAND, exc, installation=installation)

    def survive(step: Callable[[], Any]) -> Any:
        """Run a read-only check; an interrupt marks the cancel and retries the check."""
        nonlocal cancelled
        for _ in range(3):
            try:
                return step()
            except KeyboardInterrupt:
                cancelled = True
        return step()

    try:
        tools = survive(load_tools)
        before = tools.Version(str(found.result["installed_version"]))
        after_report, after = survive(lambda: _inspect(detect, tools))
        facts = survive(lambda: _facts(integrity))
    except BaseException as exc:
        if isinstance(exc, SystemExit):
            raise
        return _unknown(entry, installation, cancelled, path=path)
    if after_report is not None:
        installation = after_report.installation

    base = {"index": found.result["index"], "from_version": str(before), "to_version": target}

    if after is not None and after > before:
        if facts is not None and facts.intact:
            journal_mod.clear_journal(entry, path)
            notes = list(found.warnings)
            if str(after) != target:
                notes.append(f"Installed {after}, not the {target} found by the check.")
            if cancelled:
                notes.append(
                    "Cancellation was deferred until uv finished, and the update completed."
                )
            return LifecycleResult(
                COMMAND,
                message=f"Updated Velune {before} -> {after}. Restart Velune to use it.",
                installation=installation,
                result=base | {"applied": True, "installed_version": str(after)},
                warnings=tuple(notes),
            )
        why = f"The installed version reads {after}, but its files could not be confirmed: " + _why(
            facts
        )
        return _unknown(entry, installation, cancelled, base, why=why, path=path)

    if after is not None and after == before:
        if _verified_unchanged(baseline, facts):
            journal_mod.clear_journal(entry, path)
            return LifecycleResult(
                COMMAND,
                exit_code=ExitCode.CANCELLED if cancelled else ExitCode.ERROR,
                message=(
                    f"Update cancelled. Velune is still {before} (verified); nothing was changed."
                    if cancelled
                    else f"The update did not take effect. Velune is still {before} (verified)."
                ),
                recovery=(
                    None if cancelled else "Run the command below yourself to see uv's messages."
                ),
                manual_command=None if cancelled else manual_command("update", report),
                installation=installation,
                result=base | {"applied": False, "installed_version": str(before)},
                warnings=tuple(found.warnings),
            )
        why = (
            f"The installed version is still {before}, but that does not show uv left the "
            "installation alone: " + _why(facts, baseline)
        )
        return _unknown(entry, installation, cancelled, base, why=why, path=path)

    return _unknown(entry, installation, cancelled, base, path=path)


def _facts(integrity: Callable[[], Integrity]) -> Integrity | None:
    try:
        return integrity()
    except Exception:
        return None


def _verified_unchanged(baseline: Integrity | None, after: Integrity | None) -> bool:
    """True only with positive evidence that uv changed nothing we can see."""
    return (
        baseline is not None
        and after is not None
        and after.intact
        and baseline.distributions is not None
        and baseline.distributions == after.distributions
    )


def _why(after: Integrity | None, baseline: Integrity | None = None) -> str:
    if after is None:
        return "the integrity check could not run."
    if not after.intact:
        detail = "; ".join(after.problems) or after.record
        return f"{detail}."
    if baseline is None or baseline.distributions is None or after.distributions is None:
        return "the installation could not be compared with its state before the update."
    return "the set of installed packages differs from before the update."


def _unknown(
    entry: journal_mod.JournalEntry | None,
    installation: dict[str, Any],
    cancelled: bool,
    base: dict[str, Any] | None = None,
    *,
    why: str | None = None,
    path: Path | None = None,
) -> LifecycleResult:
    """The installation could not be verified after uv ran: keep the journal, no rollback."""
    command = _recovery_command(entry) if entry is not None else None
    detail = f" {why}" if why else ""
    return LifecycleResult(
        COMMAND,
        exit_code=ExitCode.CANCELLED if cancelled else ExitCode.ERROR,
        message=(
            ("Update cancelled, but " if cancelled else "")
            + "Velune could not verify its installation after running uv, so it may be "
            "damaged or half-updated. No automatic recovery was attempted." + detail
        ),
        recovery=(
            "Close Velune, open a fresh terminal, and run the command below to reinstall it. "
            "The lifecycle journal was kept on purpose: it blocks further updates until the "
            "installation is confirmed. After reinstalling, check that 'uv tool list' shows "
            "velune-cli and 'velune --version' runs; only then remove the journal file"
            + (f" at {path}" if path else "")
            + "."
        ),
        manual_command=command,
        installation=installation,
        result=(base or {}) | {"applied": None, "journal": str(path) if path else None},
    )
