"""Decide whether this Velune installation is one we may safely change.

Lifecycle commands (update / repair / uninstall) mutate an installation. They
must only ever do that to an installation they can *prove* is a uv-managed tool
environment. Everything else is refused: a wrong guess here deletes or breaks
something that belongs to another tool.

Evidence and how much each piece is trusted
-------------------------------------------
Observed on uv 0.12.3 / Windows in the P2.0 spike (not documented uv
guarantees, so they can change with a uv release):

* A uv tool environment is ``<uv tool dir>/<tool name>/`` and contains
  ``uv-receipt.toml`` beside ``pyvenv.cfg``. The receipt is TOML with a
  ``[tool]`` table holding ``requirements`` (name, optional ``specifier``,
  ``extras``, ``path``) and ``python``.
* uv writes ``uv`` to the distribution's ``INSTALLER`` file.
* ``INSTALLER=uv`` is **not** proof on its own: a plain ``uv venv`` plus
  ``uv pip install`` writes it too, with no receipt.
* ``uv tool list`` is deliberately **not** used: on a damaged environment it
  printed an error and still exited 0.

From a packaging standard rather than from uv: PEP 610 defines
``direct_url.json``; ``dir_info.editable == true`` marks an editable install.
That key was *not* exercised against uv in the spike. The mere presence of
``direct_url.json`` proves nothing (a local-wheel install writes one and is not
editable).

Policy: fail closed. Only :attr:`OwnershipKind.UV_TOOL` authorises mutation.
Missing, malformed, inconsistent or unverifiable evidence is never promoted to
"probably fine".
"""

from __future__ import annotations

import importlib.metadata as importlib_metadata
import json
import os
import re
import shutil
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

from velune.lifecycle.errors import ExitCode, LifecycleError

try:
    import tomllib  # Python 3.11+
except ModuleNotFoundError:  # pragma: no cover - Python 3.10
    import tomli as tomllib  # type: ignore[no-redef]

DISTRIBUTION = "velune-cli"
RECEIPT_NAME = "uv-receipt.toml"
_MAX_RECEIPT_BYTES = 1024 * 1024
_EXTRA_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
# A PEP 440 specifier list such as "==0.9.9" or ">=0.9,<1". Checked with a strict
# pattern rather than the `packaging` library on purpose: lifecycle code must
# keep working in a damaged environment, which is exactly when `packaging`
# (a core dependency) may be the broken piece.
_SPECIFIER_RE = re.compile(
    r"(?:===|==|!=|~=|<=|>=|<|>)[ ]?[0-9A-Za-z][0-9A-Za-z.*+!_-]*"
    r"(?:[ ]?,[ ]?(?:===|==|!=|~=|<=|>=|<|>)[ ]?[0-9A-Za-z][0-9A-Za-z.*+!_-]*)*"
)


class OwnershipKind(str, Enum):
    """What kind of installation this is. Only ``UV_TOOL`` permits mutation."""

    UV_TOOL = "uv_tool"  # confirmed uv-managed tool environment
    NOT_UV = "not_uv"  # positively installed by something else (pip, ...)
    EDITABLE = "editable"  # development install (PEP 610 editable marker)
    AMBIGUOUS = "ambiguous"  # some uv signals, but they do not agree
    INDETERMINATE = "indeterminate"  # metadata missing, malformed or unverifiable


def normalize_name(name: str) -> str:
    """PEP 503 name normalisation."""
    return re.sub(r"[-_.]+", "-", name).lower()


# --------------------------------------------------------------------------
# Evidence (plain data; gathered separately so evaluation is a pure function)
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ReceiptInfo:
    """The parts of ``uv-receipt.toml`` Velune cares about."""

    specifier: str | None  # e.g. "==0.9.9"; None when installed unpinned
    extras: tuple[str, ...]
    path: str | None  # set when installed from a local file
    python: str | None  # base interpreter recorded by uv


@dataclass(frozen=True)
class OwnershipEvidence:
    prefix: Path
    tool_dir: Path | None
    tool_dir_problem: str | None
    dist_found: bool
    version: str | None
    dist_in_prefix: bool | None  # does the metadata live inside this environment?
    installer: str | None  # stripped INSTALLER text; None when missing/unreadable
    direct_url: str  # none | editable | directory | archive | vcs | malformed
    receipt_state: str  # missing | ok | malformed
    receipt: ReceiptInfo | None
    receipt_problem: str | None


@dataclass(frozen=True)
class OwnershipReport:
    kind: OwnershipKind
    reasons: tuple[str, ...]
    prefix: str
    version: str | None = None
    specifier: str | None = None
    extras: tuple[str, ...] = ()
    python_base: str | None = None
    direct_url: str = "none"
    installed_from_local_file: bool = False

    @property
    def mutation_allowed(self) -> bool:
        return self.kind is OwnershipKind.UV_TOOL

    @property
    def installation(self) -> dict[str, Any]:
        """The ``installation`` block of a lifecycle JSON result."""
        return {"kind": self.kind.value, "owned": self.mutation_allowed, "path": self.prefix}


# --------------------------------------------------------------------------
# Parsing helpers (tolerant of nothing: malformed input raises ValueError)
# --------------------------------------------------------------------------


def parse_receipt(text: str) -> ReceiptInfo:
    """Parse ``uv-receipt.toml`` text for the Velune requirement.

    Raises ``ValueError`` if the TOML is invalid, has no ``[tool]``
    requirements, or does not name ``velune-cli``.
    """
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ValueError("receipt is not valid TOML") from exc
    tool = data.get("tool")
    if not isinstance(tool, dict):
        raise ValueError("receipt has no [tool] table")
    requirements = tool.get("requirements")
    if not isinstance(requirements, list) or not requirements:
        raise ValueError("receipt lists no requirements")
    for entry in requirements:
        if not isinstance(entry, dict) or not isinstance(entry.get("name"), str):
            raise ValueError("receipt has a requirement without a name")
        if normalize_name(entry["name"]) != DISTRIBUTION:
            continue
        specifier = entry.get("specifier")
        if specifier is not None:
            if not isinstance(specifier, str):
                raise ValueError("receipt specifier is not a string")
            if not _SPECIFIER_RE.fullmatch(specifier):
                raise ValueError("receipt specifier is not valid")
        extras = entry.get("extras", [])
        if not isinstance(extras, list) or not all(
            isinstance(e, str) and _EXTRA_RE.fullmatch(e) for e in extras
        ):
            raise ValueError("receipt extras are not a list of extra names")
        path = entry.get("path")
        python = tool.get("python")
        return ReceiptInfo(
            specifier=specifier,
            extras=tuple(extras),
            path=path if isinstance(path, str) else None,
            python=python if isinstance(python, str) else None,
        )
    raise ValueError("receipt does not name velune-cli")


def classify_direct_url(text: str | None) -> str:
    """Classify ``direct_url.json`` (PEP 610): none/editable/directory/archive/vcs/malformed."""
    if text is None:
        return "none"
    try:
        data = json.loads(text)
    except ValueError:
        return "malformed"
    if not isinstance(data, dict) or not isinstance(data.get("url"), str):
        return "malformed"
    dir_info = data.get("dir_info")
    if isinstance(dir_info, dict):
        return "editable" if dir_info.get("editable") is True else "directory"
    if isinstance(data.get("archive_info"), dict):
        return "archive"
    if isinstance(data.get("vcs_info"), dict):
        return "vcs"
    return "malformed"


def _norm(path: Path) -> str:
    try:
        resolved = path.resolve(strict=False)
    except (OSError, RuntimeError):
        resolved = path.absolute()
    return os.path.normcase(str(resolved))


# --------------------------------------------------------------------------
# Evidence collection
# --------------------------------------------------------------------------

ToolDirResolver = Callable[[], "tuple[Path | None, str | None]"]


def resolve_uv_tool_dir(
    uv_executable: str | None = None,
    *,
    run: Callable[..., Any] = subprocess.run,
    timeout: float = 10.0,
) -> tuple[Path | None, str | None]:
    """Ask uv where tool environments live: ``(path, None)`` or ``(None, problem)``.

    Runs the read-only command ``uv tool dir`` (argv list, no shell). This is
    the only place the lifecycle core may start uv; tests inject *run*.
    """
    uv = uv_executable or shutil.which("uv")
    if not uv:
        return None, "uv was not found on PATH"
    try:
        completed = run(
            [uv, "tool", "dir"], capture_output=True, text=True, timeout=timeout, check=False
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return None, f"could not run uv ({type(exc).__name__})"
    lines = [ln.strip() for ln in (completed.stdout or "").splitlines() if ln.strip()]
    if completed.returncode != 0 or len(lines) != 1:
        return None, "uv did not report a single tool directory"
    candidate = Path(lines[0])
    if not candidate.is_absolute():
        return None, "uv reported a relative tool directory"
    return candidate, None


def _read_text(dist: importlib_metadata.Distribution, name: str) -> str | None:
    try:
        return dist.read_text(name)
    except (OSError, UnicodeDecodeError):
        return None


def collect_evidence(
    *,
    prefix: Path | str | None = None,
    distribution: str = DISTRIBUTION,
    resolve_tool_dir: ToolDirResolver = resolve_uv_tool_dir,
) -> OwnershipEvidence:
    """Gather ownership evidence for the running environment. Read-only."""
    env = Path(prefix if prefix is not None else sys.prefix)

    tool_dir, tool_dir_problem = resolve_tool_dir()

    try:
        dist = importlib_metadata.distribution(distribution)
    except importlib_metadata.PackageNotFoundError:
        dist = None

    version = installer = None
    in_prefix: bool | None = None
    direct_url = "none"
    if dist is not None:
        version = dist.version
        raw_installer = _read_text(dist, "INSTALLER")
        installer = raw_installer.strip() if raw_installer is not None else None
        direct_url = classify_direct_url(_read_text(dist, "direct_url.json"))
        try:
            location = Path(str(dist.locate_file("")))
            in_prefix = _norm(location).startswith(_norm(env) + os.sep)
        except (OSError, ValueError):
            in_prefix = None

    receipt_path = env / RECEIPT_NAME
    receipt: ReceiptInfo | None = None
    receipt_problem: str | None = None
    if not os.path.lexists(receipt_path):
        receipt_state = "missing"
    elif not receipt_path.is_file() or receipt_path.is_symlink():
        receipt_state, receipt_problem = "malformed", "receipt is not a regular file"
    else:
        try:
            if receipt_path.stat().st_size > _MAX_RECEIPT_BYTES:
                raise ValueError("receipt is unreasonably large")
            receipt = parse_receipt(receipt_path.read_text(encoding="utf-8"))
            receipt_state = "ok"
        except (OSError, UnicodeDecodeError, ValueError) as exc:
            receipt_state = "malformed"
            receipt_problem = str(exc) if isinstance(exc, ValueError) else "receipt is unreadable"

    return OwnershipEvidence(
        prefix=env,
        tool_dir=tool_dir,
        tool_dir_problem=tool_dir_problem,
        dist_found=dist is not None,
        version=version,
        dist_in_prefix=in_prefix,
        installer=installer,
        direct_url=direct_url,
        receipt_state=receipt_state,
        receipt=receipt,
        receipt_problem=receipt_problem,
    )


# --------------------------------------------------------------------------
# Evaluation (pure)
# --------------------------------------------------------------------------


def evaluate_ownership(ev: OwnershipEvidence) -> OwnershipReport:
    """Classify the installation from *ev*. Pure; no I/O.

    Mutation is authorised only when **all** of these hold: the receipt parses
    and names velune-cli; the environment is ``<uv tool dir>/velune-cli``;
    ``INSTALLER`` is exactly ``uv``; the package metadata belongs to this
    environment; and the install is not marked editable.
    """

    def report(kind: OwnershipKind, *reasons: str) -> OwnershipReport:
        receipt = ev.receipt
        return OwnershipReport(
            kind=kind,
            reasons=tuple(reasons),
            prefix=str(ev.prefix),
            version=ev.version,
            specifier=receipt.specifier if receipt else None,
            extras=receipt.extras if receipt else (),
            python_base=receipt.python if receipt else None,
            direct_url=ev.direct_url,
            installed_from_local_file=bool(
                (receipt and receipt.path) or ev.direct_url in ("archive", "directory")
            ),
        )

    if not ev.dist_found:
        return report(
            OwnershipKind.INDETERMINATE,
            "velune-cli package metadata was not found (running from a source tree?)",
        )
    if ev.direct_url == "malformed":
        return report(OwnershipKind.INDETERMINATE, "direct_url.json is present but unreadable")
    if ev.direct_url == "editable":
        return report(OwnershipKind.EDITABLE, "direct_url.json marks an editable install")
    if ev.receipt_state == "malformed":
        return report(
            OwnershipKind.INDETERMINATE,
            f"{RECEIPT_NAME} is present but unusable: {ev.receipt_problem}",
        )
    if ev.dist_in_prefix is False:
        return report(
            OwnershipKind.AMBIGUOUS,
            "velune-cli metadata was found outside the running environment",
        )
    if ev.dist_in_prefix is None:
        return report(
            OwnershipKind.INDETERMINATE, "could not tell where the velune-cli metadata lives"
        )

    has_receipt = ev.receipt_state == "ok"
    installer_is_uv = ev.installer == "uv"
    installer_other = ev.installer is not None and ev.installer != "uv"

    parent_ok = name_ok = False
    if ev.tool_dir is not None:
        parent_ok = _norm(ev.prefix.parent) == _norm(ev.tool_dir)
        name_ok = normalize_name(ev.prefix.name) == DISTRIBUTION
    in_tool_dir = parent_ok and name_ok

    uv_signals = [has_receipt, in_tool_dir, installer_is_uv]

    if installer_other and not has_receipt and not in_tool_dir:
        return report(OwnershipKind.NOT_UV, f"installed by '{ev.installer}', not by uv")
    if installer_other:
        return report(
            OwnershipKind.AMBIGUOUS,
            f"INSTALLER says '{ev.installer}' but the environment also looks uv-managed",
        )

    if all(uv_signals):
        return report(
            OwnershipKind.UV_TOOL,
            "uv receipt, uv tool directory and INSTALLER=uv all agree",
        )

    if not any(uv_signals):
        return report(
            OwnershipKind.INDETERMINATE,
            "no installer metadata and no uv receipt; the installation method cannot be determined",
        )

    if ev.tool_dir is None and has_receipt and installer_is_uv:
        return report(
            OwnershipKind.INDETERMINATE,
            f"could not verify the uv tool directory ({ev.tool_dir_problem or 'unknown reason'})",
        )

    missing = []
    if not has_receipt:
        missing.append(f"no {RECEIPT_NAME}")
    if not in_tool_dir:
        missing.append("environment is not <uv tool dir>/velune-cli")
    if not installer_is_uv:
        missing.append("INSTALLER is not 'uv'")
    prefix_note = (
        "INSTALLER=uv alone is not proof (plain uv venvs write it too); "
        if (installer_is_uv and not has_receipt)
        else ""
    )
    return report(
        OwnershipKind.AMBIGUOUS, prefix_note + "partial uv evidence: " + ", ".join(missing)
    )


def detect_ownership(
    *,
    prefix: Path | str | None = None,
    resolve_tool_dir: ToolDirResolver = resolve_uv_tool_dir,
) -> OwnershipReport:
    """Collect evidence for the running environment and classify it."""
    return evaluate_ownership(collect_evidence(prefix=prefix, resolve_tool_dir=resolve_tool_dir))


# --------------------------------------------------------------------------
# Refusal
# --------------------------------------------------------------------------

_REFUSAL: dict[OwnershipKind, tuple[str, str]] = {
    OwnershipKind.NOT_UV: (
        "This Velune was not installed as a uv tool, so it will not be changed automatically.",
        "Manage it with the tool that installed it, or reinstall with the official installer "
        "(see the Install section of the README). Nothing was changed.",
    ),
    OwnershipKind.EDITABLE: (
        "This is a development (editable) install, so it will not be changed automatically.",
        "Update it with git in your checkout. Nothing was changed.",
    ),
    OwnershipKind.AMBIGUOUS: (
        "Velune could not confirm that this installation is a uv-managed tool, so it will "
        "not be changed.",
        "Nothing was changed. Run 'velune doctor' for details, or reinstall with the "
        "official installer.",
    ),
    OwnershipKind.INDETERMINATE: (
        "Velune could not determine how it was installed, so it will not be changed.",
        "Nothing was changed. Run 'velune doctor' for details, or reinstall with the "
        "official installer.",
    ),
}


def require_uv_tool(report: OwnershipReport) -> OwnershipReport:
    """Return *report* if mutation is authorised, else raise a REFUSED error."""
    if report.mutation_allowed:
        return report
    message, recovery = _REFUSAL[report.kind]
    raise LifecycleError(
        ExitCode.REFUSED,
        message,
        recovery=recovery,
        details={"ownership": report.kind.value, "reasons": list(report.reasons)},
    )
