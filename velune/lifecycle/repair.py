"""``velune repair``: reinstall the *current* version of a confirmed uv-managed tool.

Repair is its own operation. It is not an update (never moves the version), not a
rollback, and not an uninstall. In order, refusing at the first problem and always
*before* anything is changed:

1. Prove ownership (P2.1): only a confirmed uv tool is repaired.
2. Windows: never replace the running installation (P2.1 guard). Manual only.
3. Check the installation can be *reconstructed exactly*; otherwise refuse and
   explain. Refused: an unreadable or non-version-shaped installed version; extras
   that are not plain extra names; a local-file, URL or VCS source (the original
   file is not something Velune will go and find); a pin that is not exactly the
   installed version (a range such as ``>=0.9,<1`` cannot be re-created without
   either upgrading or narrowing it).
4. Refuse if a journal already exists (an earlier operation may be unfinished).
5. Find uv on PATH.
6. Journal ``planned`` -> ``applying``, then run
   ``uv tool install --force "velune-cli[extras]==<installed version>"``
   (argument list, no shell, output not shown).
7. **Inspect, do not trust, the result** (uv's exit code proved nothing in P2.0).

The uv operation
----------------
``uv tool install --force`` recreates the tool's environment and replaces its
entry points (uv's documented meaning of ``--force``; the repo's P2.0 findings,
recorded in ``safety.manual_command``, are that a reinstall *without* the extras
drops them, so they are repeated). Without ``==version`` it would resolve the
newest release, i.e. a silent upgrade, so the exact installed version is pinned.

That pin has one visible consequence which Velune states rather than hides: an
install that was *unpinned* becomes pinned to its current version by the repair
(uv records the requirement it was given), after which ``velune update`` refuses
it as pinned and shows the manual command. The index is whatever the user's own uv
configuration says (no index option is passed; ``index_override_warnings`` says
so). The interpreter is chosen by uv's current defaults; it is not forced to the
original one. Dependencies are re-resolved, so their versions can differ; that is
reported when it is noticed.

Not verified live in this change: uv was not available in the development
environment, so these semantics are taken from uv's documentation and the P2.0
spike, and the test suite uses a fake uv. No real repair is ever run by the tests.

Verification (what can honestly be claimed)
-------------------------------------------
After uv finishes the installation is re-inspected: ownership still a confirmed uv
tool, installed version equal to the intended one, the same extras recorded,
every file in the package's ``RECORD`` present with the recorded size and hash, and
the environment's own ``velune`` entry point present and inside the environment.
The CLI is **not** executed: a damaged executable is not a safe verification step,
and nothing here claims runtime health.

Outcomes (the journal is removed only when the state is known):

* uv succeeded and all of the above hold                  -> repaired, verified
* uv failed or was cancelled, the above hold, and the set of
  installed distributions is unchanged                    -> not repaired, intact
* anything else                                          -> outcome uncertain:
  journal kept, no retry, no rollback, manual recovery shown

Cancellation behaves as in ``velune update``: uv is never killed by the first
Ctrl-C. Not atomic: uv replaces files in place.

Only the uv tool environment and the journal are touched. Configuration,
credentials, memory, skills, project files, Git state and other uv tools are not
read or written. uv's output and exception text are never shown.
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from velune.lifecycle import journal as journal_mod
from velune.lifecycle.apply import UV_TIMEOUT, _facts, _run_uv, _why
from velune.lifecycle.errors import ExitCode, LifecycleError, LifecycleResult
from velune.lifecycle.index import index_override_warnings
from velune.lifecycle.integrity import Integrity, check_installation
from velune.lifecycle.ownership import (
    DISTRIBUTION,
    OwnershipKind,
    OwnershipReport,
    detect_ownership,
    require_uv_tool,
)
from velune.lifecycle.safety import (
    _EXTRA_RE,
    _VERSION_RE,
    _requirement,
    check_not_self_mutation,
    pinned_install_command,
)
from velune.lifecycle.uvpath import trusted_which

COMMAND = "repair"
ENTRY_POINT = "velune"

Runner = Callable[..., Any]
EntryPointCheck = Callable[[str | Path, str], "tuple[bool, str | None]"]


def _refuse(message: str, recovery: str, command: str | None, reason: str) -> LifecycleError:
    return LifecycleError(
        ExitCode.REFUSED,
        f"{message} Nothing was changed.",
        recovery=recovery,
        manual_command=command,
        details={"reason": reason},
    )


def _eligibility(report: OwnershipReport) -> str:
    """Return the exact version to reinstall, or raise REFUSED (before any mutation)."""
    version = report.version
    if not version or not _VERSION_RE.fullmatch(version):
        raise _refuse(
            "The installed version could not be read reliably, and Velune will not guess which "
            "version to reinstall.",
            "Reinstall it yourself: close Velune, open a fresh terminal, then run "
            "'uv tool install --force velune-cli' (add your extras, for example "
            '"velune-cli[rag]", if you use any). Note that this installs the newest release.',
            None,
            "version_unreadable",
        )
    if not all(_EXTRA_RE.fullmatch(e) for e in report.extras):
        raise _refuse(
            "The installation's recorded extras could not be read safely, so they cannot be "
            "reproduced.",
            "Reinstall it yourself with the extras you use, for example "
            f'"velune-cli[rag]=={version}".',
            None,
            "extras_unreadable",
        )
    if report.installed_from_local_file or report.direct_url != "none":
        raise _refuse(
            "This copy was installed from a local file or a direct URL. Velune will not "
            "re-install from a path or URL, and reinstalling from the package index would "
            "change where it comes from.",
            "Reinstall it yourself from the same file, or switch to the package index with "
            "the command below.",
            pinned_install_command(report, version),
            "non_index_source",
        )
    if report.specifier is not None and report.specifier.replace(" ", "") != f"=={version}":
        raise _refuse(
            f"This installation is constrained to '{report.specifier}'. Reinstalling "
            f"exactly {version} would narrow that constraint, and reinstalling the constraint "
            "itself could upgrade or downgrade the installed version.",
            "If you want to pin it to the installed version, run the command below yourself.",
            pinned_install_command(report, version),
            "constraint_not_reproducible",
        )
    return version


def verify_entry_point(prefix: str | Path, platform: str) -> tuple[bool, str | None]:
    """Is the environment's own ``velune`` launcher present and inside the environment?

    Returns ``(ok, problem)``. Existence and location only: the launcher is not run.
    """
    try:
        root = Path(prefix).resolve(strict=False)
        folder, name = (
            ("Scripts", f"{ENTRY_POINT}.exe") if platform == "win32" else ("bin", ENTRY_POINT)
        )
        launcher = root / folder / name
        if not launcher.is_file():
            return False, "the 'velune' launcher is missing from the environment"
        resolved = launcher.resolve(strict=False)
        if os.path.commonpath([str(root), str(resolved)]) != str(root):
            return False, "the 'velune' launcher points outside the environment"
    except (OSError, ValueError):
        return False, "the 'velune' launcher could not be checked"
    return True, None


def _recheck(detect: Callable[[], OwnershipReport]) -> OwnershipReport | None:
    try:
        return detect()
    except Exception:
        return None


def run_repair(
    *,
    detect: Callable[[], OwnershipReport] = detect_ownership,
    run: Runner = _run_uv,
    which: Callable[[str], str | None] = trusted_which,
    integrity: Callable[[], Integrity] = check_installation,
    entry_point: EntryPointCheck = verify_entry_point,
    journal_path: Path | None = None,
    python: tuple[int, int, int] | None = None,
    platform: str | None = None,
    current_prefix: str | Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> LifecycleResult:
    """Repair the installation; never raises (``SystemExit`` excepted)."""
    installation: dict[str, Any] = {}
    entry: journal_mod.JournalEntry | None = None
    baseline: Integrity | None = None
    path = journal_path or journal_mod.default_journal_path()
    plat = platform or sys.platform
    env = os.environ if environ is None else environ
    try:
        report = detect()
        installation = report.installation
        require_uv_tool(report)
        check_not_self_mutation("repair", report, platform=plat, current_prefix=current_prefix)
        version = _eligibility(report)

        existing = journal_mod.read_journal(path)
        if existing.state != "missing":
            raise journal_mod.retained_error(path, existing)

        uv = which("uv")
        if not uv:
            raise LifecycleError(
                ExitCode.REFUSED,
                "uv was not found in a trusted location on PATH. A uv in or under the current directory is never run (a security restriction, not necessarily a broken uv): run the command again from a directory that does not contain uv. Nothing was changed, so the repair cannot be run.",
                recovery="Install uv or fix PATH, or run the command below yourself.",
                manual_command=pinned_install_command(report, version),
                details={"reason": "uv_not_found"},
            )

        requirement = _requirement(report.extras, version).strip('"')  # one argv item, no shell
        try:  # what "unchanged" is later compared against; not fatal if unavailable
            baseline = integrity()
        except Exception:
            baseline = None

        py = python or (sys.version_info.major, sys.version_info.minor, sys.version_info.micro)
        entry = journal_mod.new_entry(
            "repair", report, python_version=".".join(map(str, py)), to_version=version
        )
        journal_mod.write_journal(entry, path)
        entry = journal_mod.advance_phase(entry, "applying", path)
    except BaseException as exc:  # before any mutation: nothing to inspect or recover
        if entry is not None:  # interrupted between writing the journal and starting uv
            journal_mod.clear_journal(entry, path)
        if isinstance(exc, SystemExit):
            raise
        return LifecycleResult.from_exception(COMMAND, exc, installation=installation)

    assert entry is not None
    manual = pinned_install_command(report, version)
    warnings = list(index_override_warnings(env))
    if report.specifier is None:
        warnings.append(
            f"Repair pins this installation to {version}, so 'velune update' will now show "
            "a manual command instead of updating it."
        )

    # ---- mutation starts here --------------------------------------------------
    cancelled = False
    uv_ok = False
    try:
        completed = run([uv, "tool", "install", "--force", requirement], timeout=UV_TIMEOUT)
        uv_ok = getattr(completed, "returncode", 1) == 0
    except KeyboardInterrupt:
        cancelled = True
    except (OSError, subprocess.SubprocessError):
        pass  # uv never ran, or was stopped; the inspection below decides what happened
    except BaseException as exc:
        if isinstance(exc, SystemExit):
            raise
        # uv may already have changed things: an unexpected error here is not 'unchanged'.
        return _uncertain(
            entry,
            installation,
            cancelled,
            manual,
            path,
            version,
            why="an unexpected error interrupted the repair.",
        )

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
        after = survive(lambda: _recheck(detect))
        facts = survive(lambda: _facts(integrity))
        entry_ok, entry_problem = survive(lambda: entry_point(report.prefix, plat))
    except BaseException as exc:
        if isinstance(exc, SystemExit):
            raise
        return _uncertain(entry, installation, cancelled, manual, path, version)
    if after is not None:
        installation = after.installation

    problems: list[str] = []
    if after is None:
        problems.append("ownership could not be re-checked.")
    elif after.kind is not OwnershipKind.UV_TOOL:
        problems.append("it is no longer recognised as a uv-managed tool.")
    elif after.version != version:
        problems.append(f"the installed version reads {after.version or 'unknown'}, not {version}.")
    elif after.extras != report.extras:
        problems.append("the recorded extras are not the ones it had before.")
    if facts is None or not facts.intact:
        problems.append(_why(facts))
    if not entry_ok:
        problems.append(f"{entry_problem or 'the launcher could not be verified'}.")

    base = {"version": version, "uv_succeeded": uv_ok}
    if problems:
        return _uncertain(
            entry, installation, cancelled, manual, path, version, base, " ".join(problems)
        )

    checked = [
        "still a confirmed uv-managed tool",
        f"installed version is {version}",
        "recorded extras unchanged",
        "installed files match the package RECORD",
        "the 'velune' launcher is present in the environment",
    ]
    if uv_ok and not cancelled:
        journal_mod.clear_journal(entry, path)
        notes = list(warnings)
        if (
            baseline is not None
            and baseline.distributions is not None
            and facts is not None
            and facts.distributions is not None
            and baseline.distributions != facts.distributions
        ):
            notes.append("The versions of some installed packages differ from before the repair.")
        notes.append("The 'velune' command was not run; only the files were verified.")
        return LifecycleResult(
            COMMAND,
            message=f"Repaired Velune {version}. Restart Velune to use the repaired copy.",
            installation=installation,
            result=base | {"repaired": True, "outcome": "repaired_verified", "verified": checked},
            warnings=tuple(notes),
        )

    if (
        baseline is not None
        and baseline.distributions is not None
        and facts is not None
        and baseline.distributions == facts.distributions
    ):
        journal_mod.clear_journal(entry, path)
        return LifecycleResult(
            COMMAND,
            exit_code=ExitCode.CANCELLED if cancelled else ExitCode.ERROR,
            message=(
                f"Repair cancelled. Velune {version} appears intact (verified); "
                "nothing was repaired."
                if cancelled
                else f"The repair command failed, but Velune {version} appears intact (verified); "
                "nothing was repaired."
            ),
            recovery=None if cancelled else "Run the command below yourself to see uv's messages.",
            manual_command=None if cancelled else manual,
            installation=installation,
            result=base | {"repaired": False, "outcome": "failed_intact", "verified": checked},
            warnings=tuple(warnings),
        )

    return _uncertain(
        entry,
        installation,
        cancelled,
        manual,
        path,
        version,
        base,
        "uv did not report success, and the installation could not be compared with its "
        "state before the repair.",
    )


def _uncertain(
    entry: journal_mod.JournalEntry | None,
    installation: dict[str, Any],
    cancelled: bool,
    manual: str,
    path: Path,
    version: str,
    base: dict[str, Any] | None = None,
    why: str | None = None,
) -> LifecycleResult:
    """The outcome is unknown: keep the journal, no retry, no rollback."""
    detail = f" {why}" if why else ""
    return LifecycleResult(
        COMMAND,
        exit_code=ExitCode.UNCERTAIN,  # unverified outcome wins over 'cancelled'
        message=(
            ("Repair cancelled, but " if cancelled else "")
            + "Velune could not confirm its installation after running uv, so it may still be "
            "damaged. No retry and no rollback were attempted." + detail
        ),
        recovery=(
            "Close Velune, open a fresh terminal, and run the command below. The lifecycle "
            "journal was kept on purpose: it blocks further lifecycle commands until the "
            "installation is confirmed. After running it, check that 'uv tool list' shows "
            f"{DISTRIBUTION} {version} and 'velune --version' runs; only then remove the "
            f"journal file at {path}."
        ),
        manual_command=manual,
        installation=installation,
        result=(base or {"version": version})
        | {"repaired": None, "outcome": "uncertain", "journal": str(path)},
    )
