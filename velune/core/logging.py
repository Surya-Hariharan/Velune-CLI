"""Logging configuration for Velune."""

from __future__ import annotations

import logging
import warnings
from dataclasses import dataclass

from rich.logging import RichHandler


@dataclass(frozen=True)
class LoggingConfig:
    """Logging configuration used during CLI bootstrap."""

    level: str = "INFO"
    show_path: bool = False
    rich_tracebacks: bool = True


def configure_logging(config: LoggingConfig) -> None:
    """Configure process-wide logging once at startup."""

    logging.basicConfig(
        level=getattr(logging, config.level.upper(), logging.INFO),
        format="%(message)s",
        datefmt="[%X]",
        handlers=[
            RichHandler(
                rich_tracebacks=config.rich_tracebacks,
                show_path=config.show_path,
                omit_repeated_times=False,
            )
        ],
    )
    logging.captureWarnings(True)
    warnings.simplefilter("default")


def get_logger(name: str) -> logging.Logger:
    """Get a namespaced Velune logger."""

    return logging.getLogger(name)