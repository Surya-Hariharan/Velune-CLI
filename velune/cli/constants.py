"""Shared constants for the Velune CLI layer."""

from __future__ import annotations

# Colour codes for background-job status values, used by /jobs, /cognition status,
# and the progress dashboard. Single source of truth so they are always consistent.
JOB_STATUS_STYLES: dict[str, str] = {
    "running": "yellow",
    "completed": "green",
    "failed": "red",
    "cancelled": "dim",
    "pending": "cyan",
}
