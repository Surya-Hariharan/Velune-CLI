"""Telemetry package for Velune CLI.

Provides structured logging, span tracking, and usage analytics.
"""

from __future__ import annotations

from velune.telemetry.cognition import CognitivePerformanceAnalytics
from velune.telemetry.logging import configure_logging, bind_context, clear_context, context_scope
from velune.telemetry.spans import (
    SpanContext,
    create_run_id,
    create_span_id,
    get_current_run_id,
    get_current_span_id,
    span,
    async_span,
)
from velune.telemetry.usage_tracker import SessionUsageTracker, get_tracker, UsageSummary
from velune.telemetry.debug import (
    DebugTimer,
    debug_timer,
    TokenCounter,
    RoutingDecision,
    PipelineMetrics,
    log_debug_info,
)
from velune.telemetry.doctor import print_telemetry_report, get_telemetry_status, print_provider_health_report

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
    "TokenCounter",
    "RoutingDecision",
    "PipelineMetrics",
    "log_debug_info",
    "print_telemetry_report",
    "get_telemetry_status",
    "print_provider_health_report",
]
