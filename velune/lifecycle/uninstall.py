"""``velune uninstall``: remove the Velune *program* (a confirmed uv tool), nothing else.

Program-only. Configuration, credentials, memory, skills, project files, the uv
cache and every other uv tool are left alone; there is no purge option. In order,
refusing at the first problem and always *before* anything is changed:

1. Prove ownership (P2.1): only a confirmed uv tool is removed.
2. Windows: never remove the running installation (P2.1 guard). Manual only.
3. Check the target really is the ``velune-cli`` tool environment (its folder in
   uv's tool directory carries that name), so a mis-detected prefix can never aim
   uv at something else.
4. Refuse if a journal already exists.
5. Find uv on PATH.
6. Ask for explicit confirmation (``--yes`` skips only the question). Without a
   way to ask and without ``--yes`` the answer is "no": silence is not consent.
   ``--yes`` bypasses no ownership, platform or journal check.
7. Journal ``planned`` -> ``applying``, then run ``uv tool uninstall velune-cli``
   (argument list, no shell, output not shown). uv does the deleting; Velune
   deletes no directory, launcher or file itself, and never reinstalls.
8. **Inspect, do not trust, the result** (uv's exit code proved nothing in P2.0).

Verification after uv: the tool environment is gone or no longer carries a uv
receipt; the ``velune`` launcher on PATH no longer resolves into that environment;
every *other* entry in uv's tool directory is still there; the Velune data
directory still exists. Observed read-only; the CLI itself cannot be asked
because it has just been removed.

Outcomes (the journal is removed only when the state is known):

* removed and everything above verified   -> uninstalled (journal cleared)
* uv failed/cancelled and the installation is positively still intact
                                           -> not uninstalled (journal cleared)
* anything else (partly removed, another tool or the data directory affected,
  launcher still resolving, cannot tell)   -> uncertain: journal kept, no retry,
  no cleanup by hand, manual guidance shown

Not atomic: uv removes files one by one. uv's output is never shown. uv was not
available when this was written; the command is uv's documented
``uv tool uninstall`` (already named in ``safety.manual_command``).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from velune.core.paths import app_data_root
from velune.lifecycle import journal as journal_mod
from velune.lifecycle.apply import UV_TIMEOUT, _run_uv
from velune.lifecycle.errors import ExitCode, LifecycleError, LifecycleResult
from velune.lifecycle.integrity import Integrity, check_installation
from velune.lifecycle.ownership import (
    DISTRIBUTION,
    RECEIPT_NAME,
    OwnershipReport,
    detect_ownership,
    normalize_name,
    require_uv_tool,
)
from velune.lifecycle.safety import check_not_self_mutation, manual_command
from velune.lifecycle.uvpath import trusted_which

COMMAND = "uninstall"
Runner = Callable[..., Any]
Confirm = Callable[[str], bool]


@dataclass(frozen=True)
class Observed:
    """A read-only look at what uninstall may have affected."""

    prefix_exists: bool
    receipt_exists: bool
    launcher_in_prefix: bool  # the 'velune' on PATH still resolves into the old environment
    siblings: frozenset[str]  # names of the other tools in uv's tool directory
    data_dir_exists: bool


def observe(prefix: str | Path) -> Observed:
    """Look at the filesystem without changing it. Never raises."""
    root = Path(prefix)
    try:
        resolved_root = root.resolve(strict=False)
        siblings = (
            frozenset(p.name for p in root.parent.iterdir() if p.name != root.name)
            if root.parent.is_dir()
            else frozenset()
        )
        found = shutil.which("velune")
        in_prefix = False
        if found:
            target = Path(found).resolve(strict=False)
            in_prefix = os.path.commonpath([str(resolved_root), str(target)]) == str(resolved_root)
        data = app_data_root().exists()
        return Observed(root.exists(), (root / RECEIPT_NAME).exists(), in_prefix, siblings, data)
    except (OSError, ValueError):
        return Observed(True, True, True, frozenset(), True)  # unreadable: assume the worst


def _refuse(message: str, recovery: str, command: str | None, reason: str) -> LifecycleError:
    return LifecycleError(
        ExitCode.REFUSED,
        f"{message} Nothing was changed.",
        recovery=recovery,
        manual_command=command,
        details={"reason": reason},
    )


def describe(report: OwnershipReport) -> str:
    """The text shown before asking for confirmation."""
    return (
        f"This will uninstall the Velune program ({DISTRIBUTION} {report.version or 'unknown version'}, "
        "installed as a uv tool) by running: uv tool uninstall velune-cli\n"
        "It removes only the program and its 'velune' launcher.\n"
        "It does NOT remove: your configuration, API keys and credentials, memory and "
        "conversation data, your skills, your project files, the uv cache, or any other uv tool."
    )


def run_uninstall(
    *,
    confirm: Confirm | None = None,
    assume_yes: bool = False,
    detect: Callable[[], OwnershipReport] = detect_ownership,
    run: Runner = _run_uv,
    which: Callable[[str], str | None] = trusted_which,
    observer: Callable[[str], Observed] = observe,
    integrity: Callable[[], Integrity] = check_installation,
    journal_path: Path | None = None,
    python: tuple[int, int, int] | None = None,
    platform: str | None = None,
    current_prefix: str | Path | None = None,
) -> LifecycleResult:
    """Uninstall the program; never raises (``SystemExit`` excepted)."""
    installation: dict[str, Any] = {}
    entry: journal_mod.JournalEntry | None = None
    path = journal_path or journal_mod.default_journal_path()
    try:
        report = detect()
        installation = report.installation
        require_uv_tool(report)
        check_not_self_mutation(
            "uninstall", report, platform=platform, current_prefix=current_prefix
        )
        if normalize_name(Path(report.prefix).name) != DISTRIBUTION:
            raise _refuse(
                "The detected installation folder is not the 'velune-cli' uv tool, so Velune "
                "will not point uv at it.",
                "Run 'uv tool list' to see what is installed, then remove Velune yourself.",
                manual_command("uninstall", report),
                "unexpected_tool_folder",
            )

        existing = journal_mod.read_journal(path)
        if existing.state != "missing":
            raise journal_mod.retained_error(path, existing)

        uv = which("uv")
        if not uv:
            raise LifecycleError(
                ExitCode.REFUSED,
                "uv was not found in a trusted location on PATH. A uv in or under the current directory is never run (a security restriction, not necessarily a broken uv): run the command again from a directory that does not contain uv. Nothing was changed, so Velune cannot be uninstalled.",
                recovery="Install uv or fix PATH, or run the command below yourself.",
                manual_command=manual_command("uninstall", report),
                details={"reason": "uv_not_found"},
            )

        if not assume_yes:
            if confirm is None:
                raise _refuse(
                    "Uninstall needs your explicit confirmation, and this session cannot ask "
                    "for it.",
                    "Run 'velune uninstall' in an interactive terminal, or pass --yes to "
                    "confirm in advance.",
                    None,
                    "confirmation_unavailable",
                )
            if confirm(describe(report)) is not True:
                return LifecycleResult(
                    COMMAND,
                    exit_code=ExitCode.CANCELLED,
                    message="Uninstall cancelled. Nothing was changed.",
                    installation=installation,
                    result={"uninstalled": False, "outcome": "declined"},
                )

        before = observer(report.prefix)
        py = python or (sys.version_info.major, sys.version_info.minor, sys.version_info.micro)
        entry = journal_mod.new_entry("uninstall", report, python_version=".".join(map(str, py)))
        journal_mod.write_journal(entry, path)
        entry = journal_mod.advance_phase(entry, "applying", path)
    except BaseException as exc:  # before any mutation
        if entry is not None:
            journal_mod.clear_journal(entry, path)
        if isinstance(exc, SystemExit):
            raise
        return LifecycleResult.from_exception(COMMAND, exc, installation=installation)

    manual = manual_command("uninstall", report)
    cancelled = False
    uv_started = True  # see the runner contract on apply._run_uv
    try:
        run([uv, "tool", "uninstall", DISTRIBUTION], timeout=UV_TIMEOUT)
    except KeyboardInterrupt:
        cancelled = True  # uv had started
    except OSError:
        uv_started = False  # the process could not be started: uv never ran
    except subprocess.SubprocessError:
        pass  # e.g. a timeout: uv had started; the inspection below decides what happened
    except BaseException as exc:
        if isinstance(exc, SystemExit):
            raise
        # uv may already have run: an unexpected error here is not 'unchanged'.
        return _uncertain(
            installation,
            cancelled,
            manual,
            path,
            "an unexpected error interrupted the uninstall.",
            uv_started=None,
        )

    try:
        after = observer(report.prefix)
        still_intact: bool | None = None
        if after.prefix_exists and after.receipt_exists:
            try:
                still_intact = integrity().intact
            except Exception:
                still_intact = None
    except BaseException as exc:
        if isinstance(exc, SystemExit):
            raise
        return _uncertain(
            installation,
            cancelled,
            manual,
            path,
            "the result could not be inspected.",
            uv_started=uv_started,
        )

    others_ok = before.siblings <= after.siblings
    data_ok = after.data_dir_exists or not before.data_dir_exists
    removed = not after.prefix_exists and not after.receipt_exists and not after.launcher_in_prefix
    base = {"uv_ran": uv_started}

    # Success needs uv to have actually run: an installation that vanished while uv could not
    # even start was not removed by this command, and is not reported as if it were.
    if removed and others_ok and data_ok and not cancelled and uv_started:
        journal_mod.clear_journal(entry, path)
        return LifecycleResult(
            COMMAND,
            message=(
                "Velune was uninstalled. Your configuration, credentials, memory, skills and "
                "projects were not touched. Close this terminal and open a new one."
            ),
            installation=installation,
            result=base
            | {
                "uninstalled": True,
                "outcome": "uninstalled_verified",
                "verified": [
                    "the uv receipt is gone",
                    "the 'velune' launcher no longer resolves to the old environment",
                    "other uv tools are still present",
                    "the Velune data directory still exists",
                ],
            },
            warnings=(
                "Data and the uv cache were left in place on purpose; this command never purges.",
            ),
        )

    if not removed and still_intact is True and others_ok and data_ok:
        journal_mod.clear_journal(entry, path)
        return LifecycleResult(
            COMMAND,
            exit_code=ExitCode.CANCELLED if cancelled else ExitCode.ERROR,
            message=(
                "Uninstall cancelled. Velune is still installed and intact (verified)."
                if cancelled
                else (
                    "uv did not uninstall Velune. It is still installed and intact (verified)."
                    if uv_started
                    else "uv could not be started, so nothing was uninstalled. Velune is still "
                    "installed and intact (verified)."
                )
            ),
            recovery=None if cancelled else "Run the command below yourself to see uv's messages.",
            manual_command=None if cancelled else manual,
            installation=installation,
            result=base | {"uninstalled": False, "outcome": "failed_intact"},
        )

    problems = []
    if not others_ok:
        problems.append("another uv tool is missing.")
    if not data_ok:
        problems.append("the Velune data directory is missing.")
    if not removed:
        problems.append("Velune appears to be only partly removed or still present.")
    elif not uv_started:
        problems.append("uv could not be started, yet the installation is gone.")
    elif cancelled:
        problems.append("the uninstall was interrupted.")
    return _uncertain(
        installation, cancelled, manual, path, " ".join(problems), uv_started=uv_started
    )


def _uncertain(
    installation: dict[str, Any],
    cancelled: bool,
    manual: str,
    path: Path,
    why: str,
    *,
    uv_started: bool | None = True,
) -> LifecycleResult:
    return LifecycleResult(
        COMMAND,
        exit_code=ExitCode.UNCERTAIN,  # unverified outcome wins over 'cancelled'
        message=(
            ("Uninstall cancelled, but " if cancelled else "")
            + "Velune could not confirm the state of its installation after running uv. "
            "No retry, reinstall or manual deletion was attempted. " + why
        ).strip(),
        recovery=(
            "Open a fresh terminal and check 'uv tool list'. If velune-cli is still listed or "
            "'velune' still runs, run the command below yourself. Your data was not removed. "
            "The lifecycle journal was kept on purpose; remove the file at "
            f"{path} only after you have confirmed the state."
        ),
        manual_command=manual,
        installation=installation,
        result={
            "uv_ran": uv_started,
            "uninstalled": None,
            "outcome": "uncertain",
            "journal": str(path),
        },
    )
