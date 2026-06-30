"""Unified backup / restore / recovery for all Velune persistent state.

Velune keeps durable state in several stores (conversation sessions, config
TOML, provider credentials, the SQLite cognitive core + LanceDB semantic store,
and the workspace trust list). This package is the single place that knows where
each of those lives and how to snapshot and restore it, so ``velune backup`` and
``velune restore`` never drift from the real on-disk layout.
"""

from __future__ import annotations

from velune.recovery.archive import (
    SUBSYSTEMS,
    BackupResult,
    RestoreResult,
    create_backup,
    restore_backup,
)

__all__ = [
    "SUBSYSTEMS",
    "BackupResult",
    "RestoreResult",
    "create_backup",
    "restore_backup",
]
