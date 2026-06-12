"""Span context system for correlation IDs and trace propagation.

Provides:
- Unique run_id (UUID) for each orchestration run
- Span IDs for agent turns and tool executions
- Parent-child span relationships
- Automatic context binding via structlog
"""

from __future__ import annotations

import contextvars
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Iterator, Optional

import structlog

logger = structlog.get_logger()

# Context variables for span propagation
_run_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar("run_id", default=None)
_span_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar("span_id", default=None)
_parent_span_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar("parent_span_id", default=None)


@dataclass
class SpanContext:
    """Represents a span context for tracing."""

    run_id: str
    span_id: str
    parent_span_id: Optional[str] = None
    operation_name: str = ""
    start_time: float = field(default_factory=lambda: __import__("time").time())
    end_time: Optional[float] = None
    duration_ms: Optional[float] = None
    status: str = "running"  # running, completed, failed
    error: Optional[str] = None
    attributes: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for logging."""
        return {
            "run_id": self.run_id,
            "span_id": self.span_id,
            "parent_span_id": self.parent_span_id,
            "operation_name": self.operation_name,
            "status": self.status,
            "duration_ms": self.duration_ms,
            "error": self.error,
        }


def create_run_id() -> str:
    """Create a unique run ID for an orchestration run."""
    return str(uuid.uuid4())[:8]


def create_span_id() -> str:
    """Create a unique span ID for an operation."""
    return str(uuid.uuid4())[:8]


def get_current_run_id() -> str | None:
    """Get the current run ID from context."""
    return _run_id_var.get()


def get_current_span_id() -> str | None:
    """Get the current span ID from context."""
    return _span_id_var.get()


def get_current_parent_span_id() -> str | None:
    """Get the current parent span ID from context."""
    return _parent_span_id_var.get()


@contextmanager
def span(
    operation_name: str,
    parent_run_id: str | None = None,
    parent_span_id: str | None = None,
    **attributes: Any,
) -> Iterator[SpanContext]:
    """Context manager for creating a new span.

    Automatically binds span context to structlog for duration of operation.
    Handles timing and status tracking.

    Args:
        operation_name: Name of the operation (e.g., "coder_execution")
        parent_run_id: Run ID for this span (creates new if None)
        parent_span_id: Parent span ID (creates new span if None)
        **attributes: Additional attributes to track

    Example:
        with span("coder_execution", parent_run_id=run_id) as span_ctx:
            logger.info("Starting coder", operation=span_ctx.operation_name)
            result = await coder.run()
            # Span duration automatically tracked
    """
    import time

    # Create or use provided IDs
    run_id = parent_run_id or get_current_run_id() or create_run_id()
    span_id = create_span_id()
    current_parent = parent_span_id or get_current_span_id()

    # Create span context
    span_ctx = SpanContext(
        run_id=run_id,
        span_id=span_id,
        parent_span_id=current_parent,
        operation_name=operation_name,
        attributes=attributes,
    )

    # Save old context
    old_run_id = _run_id_var.get()
    old_span_id = _span_id_var.get()
    old_parent_span_id = _parent_span_id_var.get()

    try:
        # Bind to context variables
        _run_id_var.set(run_id)
        _span_id_var.set(span_id)
        _parent_span_id_var.set(current_parent)

        # Bind to structlog
        structlog.contextvars.bind_contextvars(
            run_id=run_id,
            span_id=span_id,
            operation_name=operation_name,
            **attributes,
        )

        logger.debug(
            "Span started",
            span_context=span_ctx.to_dict(),
        )

        yield span_ctx

    except Exception as e:
        span_ctx.status = "failed"
        span_ctx.error = str(e)
        logger.error(
            "Span failed",
            span_context=span_ctx.to_dict(),
            error=str(e),
        )
        raise

    finally:
        # Record end time and duration
        span_ctx.end_time = time.time()
        span_ctx.duration_ms = (span_ctx.end_time - span_ctx.start_time) * 1000

        if span_ctx.status == "running":
            span_ctx.status = "completed"

        logger.debug(
            "Span ended",
            span_context=span_ctx.to_dict(),
        )

        # Unbind from structlog
        unbind_keys = ["operation_name"] + list(attributes.keys())
        structlog.contextvars.unbind_contextvars(*unbind_keys)

        # Restore old context
        if old_run_id is not None:
            _run_id_var.set(old_run_id)
        else:
            _run_id_var.set(None)

        if old_span_id is not None:
            _span_id_var.set(old_span_id)
        else:
            _span_id_var.set(None)

        if old_parent_span_id is not None:
            _parent_span_id_var.set(old_parent_span_id)
        else:
            _parent_span_id_var.set(None)


async def async_span(
    operation_name: str,
    parent_run_id: str | None = None,
    parent_span_id: str | None = None,
    **attributes: Any,
) -> AsyncIterator[SpanContext]:
    """Async context manager for creating a new span.

    Same as span() but for async operations.

    Example:
        async with async_span("council_run", parent_run_id=run_id) as span_ctx:
            result = await orchestrator.run()
    """
    import time

    run_id = parent_run_id or get_current_run_id() or create_run_id()
    span_id = create_span_id()
    current_parent = parent_span_id or get_current_span_id()

    span_ctx = SpanContext(
        run_id=run_id,
        span_id=span_id,
        parent_span_id=current_parent,
        operation_name=operation_name,
        attributes=attributes,
    )

    old_run_id = _run_id_var.get()
    old_span_id = _span_id_var.get()
    old_parent_span_id = _parent_span_id_var.get()

    try:
        _run_id_var.set(run_id)
        _span_id_var.set(span_id)
        _parent_span_id_var.set(current_parent)

        structlog.contextvars.bind_contextvars(
            run_id=run_id,
            span_id=span_id,
            operation_name=operation_name,
            **attributes,
        )

        logger.debug("Span started", span_context=span_ctx.to_dict())

        yield span_ctx

    except Exception as e:
        span_ctx.status = "failed"
        span_ctx.error = str(e)
        logger.error(
            "Span failed",
            span_context=span_ctx.to_dict(),
            error=str(e),
        )
        raise

    finally:
        span_ctx.end_time = time.time()
        span_ctx.duration_ms = (span_ctx.end_time - span_ctx.start_time) * 1000

        if span_ctx.status == "running":
            span_ctx.status = "completed"

        logger.debug("Span ended", span_context=span_ctx.to_dict())

        unbind_keys = ["operation_name"] + list(attributes.keys())
        structlog.contextvars.unbind_contextvars(*unbind_keys)

        if old_run_id is not None:
            _run_id_var.set(old_run_id)
        else:
            _run_id_var.set(None)

        if old_span_id is not None:
            _span_id_var.set(old_span_id)
        else:
            _span_id_var.set(None)

        if old_parent_span_id is not None:
            _parent_span_id_var.set(old_parent_span_id)
        else:
            _parent_span_id_var.set(None)
