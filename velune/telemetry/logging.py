"""Production-grade structured logging configuration using structlog.

Provides:
- JSON output to file (rotated daily, 7-day retention)
- Human-readable console output (unless --json flag)
- Automatic context binding (session_id, workspace_root, active_model)
- Log level from config or --debug flag
"""

from __future__ import annotations

import logging
import logging.handlers
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

import structlog
from structlog.processors import (
    CallsiteParameterAdder,
    JSONRenderer,
    TimeStamper,
    add_log_level,
    format_exc_info,
)
from structlog.stdlib import BoundLogger, ProcessorFormatter, add_logger_name
from structlog.typing import Processor

if TYPE_CHECKING:
    from velune.kernel.config import VeluneConfig

logger = structlog.get_logger()

# Global state for configure_logging
_configured = False
_log_dir: Path | None = None


def configure_logging(
    config: VeluneConfig | None = None,
    debug: bool = False,
    json_output: bool = False,
) -> None:
    """Configure structlog with file and console output.

    MUST be called once before any other initialization in kernel/entrypoint.py.

    Args:
        config: VeluneConfig with log_level setting
        debug: If True, override to DEBUG level
        json_output: If True, output JSON to console (default: human-readable)
    """
    global _configured, _log_dir

    if _configured:
        return

    _configured = True

    # Determine log level
    log_level = "DEBUG" if debug else (config.log_level.upper() if config else "INFO")
    numeric_level = getattr(logging, log_level, logging.INFO)

    # Create log directory
    _log_dir = Path.home() / ".velune" / "logs"
    _log_dir.mkdir(parents=True, exist_ok=True)

    # Shared processors for structure
    shared_processors: list[Processor] = [
        add_log_level,
        add_logger_name,
        TimeStamper(fmt="iso"),
        CallsiteParameterAdder(parameters=[CallsiteParameterAdder.FILENAME, CallsiteParameterAdder.LINENO]),
        format_exc_info,
    ]

    # File handler: JSON output
    file_handler = logging.handlers.RotatingFileHandler(
        filename=_log_dir / f"velune-{datetime.now().strftime('%Y-%m-%d')}.log",
        maxBytes=50 * 1024 * 1024,  # 50 MB per file
        backupCount=7,  # Keep 7 days
    )
    file_handler.setLevel(numeric_level)
    file_handler.setFormatter(
        ProcessorFormatter(
            processor=JSONRenderer(),
            foreign_pre_chain=shared_processors,
        )
    )

    # Console handler: Human-readable or JSON
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(numeric_level)

    if json_output:
        console_processor = JSONRenderer()
    else:
        console_processor = _ConsoleRenderer(colors=_supports_color())

    console_handler.setFormatter(
        ProcessorFormatter(
            processor=console_processor,
            foreign_pre_chain=shared_processors,
        )
    )

    # Configure root logger
    root_logger = logging.getLogger()
    root_logger.setLevel(numeric_level)
    root_logger.handlers.clear()
    root_logger.addHandler(file_handler)
    root_logger.addHandler(console_handler)

    # Configure structlog
    structlog.configure(
        processors=shared_processors + [
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )

    logger.info(
        "Logging configured",
        log_level=log_level,
        file=str(_log_dir / f"velune-{datetime.now().strftime('%Y-%m-%d')}.log"),
        json_console=json_output,
    )

    # Schedule log cleanup (keep only 7 days)
    _cleanup_old_logs()


def get_log_directory() -> Path:
    """Get the configured log directory."""
    if _log_dir is None:
        return Path.home() / ".velune" / "logs"
    return _log_dir


def _cleanup_old_logs(retention_days: int = 7) -> None:
    """Remove log files older than retention_days."""
    log_dir = get_log_directory()
    if not log_dir.exists():
        return

    cutoff = datetime.now() - timedelta(days=retention_days)

    for log_file in log_dir.glob("velune-*.log*"):
        try:
            # Parse date from filename: velune-2026-06-12.log
            date_str = log_file.stem.replace("velune-", "").split(".")[0]
            file_date = datetime.strptime(date_str, "%Y-%m-%d")

            if file_date < cutoff:
                log_file.unlink()
        except (ValueError, OSError):
            # Skip files we can't parse or delete
            pass


class _ConsoleRenderer:
    """Human-readable console output renderer."""

    def __init__(self, colors: bool = True) -> None:
        self.colors = colors

    def __call__(self, logger, method_name: str, event_dict: dict[str, Any]) -> str:
        """Render event dict to human-readable console format."""
        level = event_dict.pop("level", "INFO").upper()
        timestamp = event_dict.pop("timestamp", "")
        logger_name = event_dict.pop("logger_name", "")
        filename = event_dict.pop("filename", "")
        lineno = event_dict.pop("lineno", "")

        # Build prefix
        prefix_parts = []
        if timestamp:
            prefix_parts.append(f"[{timestamp}]")
        if self.colors:
            level_colored = self._colorize_level(level)
            prefix_parts.append(level_colored)
        else:
            prefix_parts.append(f"[{level}]")

        prefix = " ".join(prefix_parts)

        # Main message
        message = event_dict.pop("event", "")

        # Build context (remaining fields)
        context_parts = []
        for key, value in sorted(event_dict.items()):
            if key.startswith("_"):
                continue
            context_parts.append(f"{key}={value!r}")

        context = " " + " ".join(context_parts) if context_parts else ""

        # Source location
        location = f" ({logger_name}:{filename}:{lineno})" if filename and lineno else ""

        return f"{prefix} {message}{context}{location}"

    def _colorize_level(self, level: str) -> str:
        """Add ANSI color codes to log level."""
        colors = {
            "DEBUG": "\033[36m[DEBUG]\033[0m",      # Cyan
            "INFO": "\033[32m[INFO]\033[0m",        # Green
            "WARNING": "\033[33m[WARNING]\033[0m",  # Yellow
            "ERROR": "\033[31m[ERROR]\033[0m",      # Red
            "CRITICAL": "\033[35m[CRITICAL]\033[0m",  # Magenta
        }
        return colors.get(level, f"[{level}]")


def _supports_color() -> bool:
    """Check if terminal supports color output."""
    import os

    # Check if stdout is a tty
    if not hasattr(sys.stdout, "isatty"):
        return False

    if not sys.stdout.isatty():
        return False

    # Check environment variables
    term = os.environ.get("TERM", "").lower()
    no_color = os.environ.get("NO_COLOR", "").lower()

    if no_color:
        return False

    # Common terminals that support color
    if term in ("dumb", ""):
        return False

    return True


def bind_context(**kwargs) -> None:
    """Bind key-value pairs to the structlog context.

    These will appear in all subsequent log lines until unbind_context() is called.

    Example:
        bind_context(session_id="sess-123", workspace_root="/path/to/project")
    """
    structlog.contextvars.bind_contextvars(**kwargs)


def unbind_context(*keys: str) -> None:
    """Unbind keys from the structlog context.

    Example:
        unbind_context("session_id", "workspace_root")
    """
    structlog.contextvars.unbind_contextvars(*keys)


def clear_context() -> None:
    """Clear all context variables."""
    structlog.contextvars.clear_contextvars()


@contextmanager_like
def context_scope(**kwargs) -> Any:
    """Context manager for temporarily binding context variables.

    Example:
        with context_scope(span_id="span-456"):
            # All logs here have span_id
            logger.info("Processing")
    """
    structlog.contextvars.bind_contextvars(**kwargs)
    try:
        yield
    finally:
        structlog.contextvars.unbind_contextvars(*kwargs.keys())


# Python 3.9+ compatible context manager
from contextlib import contextmanager


@contextmanager
def context_scope(**kwargs) -> Any:
    """Context manager for temporarily binding context variables."""
    structlog.contextvars.bind_contextvars(**kwargs)
    try:
        yield
    finally:
        structlog.contextvars.unbind_contextvars(*kwargs.keys())
