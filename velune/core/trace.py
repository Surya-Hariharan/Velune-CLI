"""Structured tracing utilities for distributed execution and agent deliberation in Velune."""

import contextvars
import logging
from typing import Any

_run_id: contextvars.ContextVar[str | None] = contextvars.ContextVar("run_id", default=None)
_agent_id: contextvars.ContextVar[str | None] = contextvars.ContextVar("agent_id", default=None)
_step_id: contextvars.ContextVar[str | None] = contextvars.ContextVar("step_id", default=None)


class TraceContext:
    """Context manager to manage the active run, agent, and step trace ids in context variables."""

    def __init__(self, run_id: str, agent_id: str | None = None, step_id: str | None = None) -> None:
        self._tokens: list[contextvars.Token[str | None]] = []
        self.run_id = run_id
        self.agent_id = agent_id
        self.step_id = step_id

    def __enter__(self) -> "TraceContext":
        self._tokens = [
            _run_id.set(self.run_id),
            _agent_id.set(self.agent_id),
            _step_id.set(self.step_id),
        ]
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        for tok in self._tokens:
            tok.var.reset(tok)


def get_trace_prefix() -> str:
    """Generate the structured trace prefix string from active context variables."""
    import os
    if os.environ.get("VELUNE_LOG_FORMAT", "").lower() == "json":
        return ""
    parts = []
    if r := _run_id.get():
        parts.append(f"run={r[:8]}")
    if a := _agent_id.get():
        parts.append(f"agent={a}")
    if s := _step_id.get():
        parts.append(f"step={s}")
    return "[" + " ".join(parts) + "] " if parts else ""


class TracedLogger:
    """Logger wrapper that prepends trace context to all messages."""

    def __init__(self, name: str | logging.Logger) -> None:
        if isinstance(name, str):
            self._logger = logging.getLogger(name)
        else:
            self._logger = name

    def info(self, msg: Any, *args: Any, **kwargs: Any) -> None:
        self._logger.info(get_trace_prefix() + str(msg), *args, **kwargs)

    def warning(self, msg: Any, *args: Any, **kwargs: Any) -> None:
        self._logger.warning(get_trace_prefix() + str(msg), *args, **kwargs)

    def error(self, msg: Any, *args: Any, **kwargs: Any) -> None:
        self._logger.error(get_trace_prefix() + str(msg), *args, **kwargs)

    def debug(self, msg: Any, *args: Any, **kwargs: Any) -> None:
        self._logger.debug(get_trace_prefix() + str(msg), *args, **kwargs)

    def exception(self, msg: Any, *args: Any, **kwargs: Any) -> None:
        self._logger.exception(get_trace_prefix() + str(msg), *args, **kwargs)

    def critical(self, msg: Any, *args: Any, **kwargs: Any) -> None:
        self._logger.critical(get_trace_prefix() + str(msg), *args, **kwargs)

    def log(self, level: int, msg: Any, *args: Any, **kwargs: Any) -> None:
        self._logger.log(level, get_trace_prefix() + str(msg), *args, **kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._logger, name)
