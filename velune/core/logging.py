"""Logging configuration for Velune."""

from __future__ import annotations

import json
import logging
import os
import warnings
from dataclasses import dataclass
from datetime import datetime, timezone

from rich.logging import RichHandler

from velune.core.redaction import SecretRedactingFilter, redact_secrets
from velune.core.trace import TracedLogger, _agent_id, _run_id, _step_id


class JsonFormatter(logging.Formatter):
    """Production JSON log formatter that extracts context tracing attributes."""

    def format(self, record: logging.LogRecord) -> str:
        exc_info = None
        if record.exc_info:
            exc_info = redact_secrets(self.formatException(record.exc_info))

        ts = datetime.now(timezone.utc).isoformat().replace("+00:00", "")

        log_data = {
            "ts": ts,
            "level": record.levelname,
            "logger": record.name,
            "msg": redact_secrets(record.getMessage()),
            "run_id": _run_id.get(),
            "agent_id": _agent_id.get(),
            "step_id": _step_id.get(),
        }
        if exc_info:
            log_data["exception"] = exc_info

        return json.dumps(log_data)


@dataclass(frozen=True)
class LoggingConfig:
    """Logging configuration used during CLI bootstrap."""

    level: str = "INFO"
    show_path: bool = False
    rich_tracebacks: bool = True


def configure_logging(config: LoggingConfig) -> None:
    """Configure process-wide logging once at startup."""

    log_format = os.environ.get("VELUNE_LOG_FORMAT", "").lower()

    if log_format == "json":
        handler = logging.StreamHandler()
        handler.setFormatter(JsonFormatter())
        handlers: list[logging.Handler] = [handler]
    else:
        handlers = [
            RichHandler(
                rich_tracebacks=config.rich_tracebacks,
                show_path=config.show_path,
                omit_repeated_times=False,
            )
        ]

    # Scrub provider credentials from every emitted record before it reaches a
    # formatter — protects log files, JSON shippers, and terminal scrollback.
    redactor = SecretRedactingFilter()
    for handler in handlers:
        handler.addFilter(redactor)

    logging.basicConfig(
        level=getattr(logging, config.level.upper(), logging.INFO),
        format="%(message)s" if log_format != "json" else None,
        datefmt="[%X]" if log_format != "json" else None,
        handlers=handlers,
        force=True,
    )
    logging.captureWarnings(True)
    warnings.simplefilter("default")


def get_logger(name: str) -> TracedLogger:
    """Get a namespaced Velune logger."""

    return TracedLogger(name)
