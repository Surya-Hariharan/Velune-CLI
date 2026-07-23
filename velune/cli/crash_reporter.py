"""Opt-in, local-only crash reporter.

Off by default, consistent with Velune's "zero telemetry" stance (see
SECURITY.md's "Zero telemetry" section) — nothing is written, and nothing is
ever transmitted anywhere, unless a user explicitly opts in via
``telemetry.crash_reports_enabled`` in ``velune.toml`` (or ``/crashreports
on``). When enabled, an unhandled exception that reaches
``kernel.entrypoint.launch()`` gets a redacted JSON snapshot written to
``~/.velune/crash_reports/`` — for the user's own diagnosis, or to attach to
a GitHub issue themselves. Velune has no server to send it to; this module
never makes a network call.

The snapshot deliberately excludes local variables and conversation content —
``traceback.format_exception()`` without ``capture_locals`` only captures
file/line/function frames and the exception's own message, which keeps the
redaction surface small and bounded instead of having to scrub arbitrary
in-memory values.
"""

from __future__ import annotations

import json
import logging
import platform
import sys
import time
import traceback
import uuid
from pathlib import Path

_log = logging.getLogger("velune.cli.crash_reporter")

CRASH_REPORT_DIR = Path.home() / ".velune" / "crash_reports"


def is_enabled() -> bool:
    """Whether the user has opted in to local crash reports."""
    try:
        from velune.kernel.config import ConfigLoader

        return bool(ConfigLoader(None).load().telemetry.crash_reports_enabled)
    except Exception:
        return False


def _velune_version() -> str:
    try:
        from velune import __version__

        return __version__
    except Exception:
        return "unknown"


def write_crash_report(exc: BaseException) -> Path | None:
    """Write a redacted local crash report for *exc*.

    Returns the report path, or None if crash reporting is disabled or
    writing failed. Never raises — a broken reporter must never mask or
    replace the real crash it was trying to record.
    """
    if not is_enabled():
        return None

    from velune.core.redaction import redact_secrets

    try:
        CRASH_REPORT_DIR.mkdir(parents=True, exist_ok=True)
        tb_text = redact_secrets(
            "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        )
        report_id = uuid.uuid4().hex[:12]
        report = {
            "id": report_id,
            "timestamp": time.time(),
            "velune_version": _velune_version(),
            "python_version": sys.version.split()[0],
            "platform": platform.platform(),
            "exception_type": type(exc).__name__,
            "exception_message": redact_secrets(str(exc)),
            "traceback": tb_text,
        }
        path = CRASH_REPORT_DIR / f"{time.strftime('%Y%m%dT%H%M%S')}_{report_id}.json"
        path.write_text(json.dumps(report, indent=2), encoding="utf-8")
        return path
    except Exception as write_exc:
        _log.debug("Could not write crash report: %s", write_exc)
        return None
