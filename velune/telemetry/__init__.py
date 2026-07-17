"""Telemetry package for Velune CLI.

Provides structured logging, span tracking, and usage analytics.
"""

from __future__ import annotations

from velune.telemetry.cognition import CognitivePerformanceAnalytics
from velune.telemetry.debug import (
    DebugTimer,
    PipelineMetrics,
    RoutingDecision,
    debug_timer,
    log_debug_info,
)
from velune.telemetry.doctor import (
    get_telemetry_status,
    print_provider_health_report,
    print_telemetry_report,
)
from velune.telemetry.logging import bind_context, clear_context, configure_logging, context_scope
from velune.telemetry.spans import (
    SpanContext,
    async_span,
    create_run_id,
    create_span_id,
    get_current_run_id,
    get_current_span_id,
    span,
)
from velune.telemetry.usage_tracker import SessionUsageTracker, UsageSummary, get_tracker

__all__ = [
    "CognitivePerformanceAnalytics",
    "configure_logging",
    "bind_context",
    "clear_context",
    "context_scope",
    "SpanContext",
    "create_run_id",
    "create_span_id",
    "get_current_run_id",
    "get_current_span_id",
    "span",
    "async_span",
    "SessionUsageTracker",
    "get_tracker",
    "UsageSummary",
    "DebugTimer",
    "debug_timer",
    "RoutingDecision",
    "PipelineMetrics",
    "log_debug_info",
    "print_telemetry_report",
    "get_telemetry_status",
    "print_provider_health_report",
]
