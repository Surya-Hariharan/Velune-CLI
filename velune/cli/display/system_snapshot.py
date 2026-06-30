"""One-glance system state for the live dashboard and workflow guidance.

`SystemSnapshot` answers *"What is Velune doing right now?"* by composing the
report builders Velune already maintains into a single, truthful structure. It
deliberately splits two clocks:

* **Static** fields (workspace, index freshness, memory tiers, integration counts,
  config path, health) are derived from on-disk state via
  :func:`velune.observability.context_report.build_context_report` plus the live
  registries. They are expensive-ish, so the dashboard builds the snapshot **once**
  and reuses it across the 500 ms refresh loop.
* **Live** session fields (active model, mode, context %) change every keystroke and
  are read each tick via :class:`LiveSessionState`, never cached.

Every value is read from real state. An absent index yields zeroed counts and a
``no-index`` freshness, never invented numbers — mirroring the ``velune context``
truthfulness contract.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from velune.observability.context_report import MemoryTableStat, build_context_report


@dataclass
class IndexLine:
    """Compact index-freshness summary for the State row."""

    freshness: str  # "synced" | "stale" | "unknown" | "no-index"
    files: int
    symbols: int
    exists: bool


@dataclass
class IntegrationsLine:
    """Counts of the optional subsystems wired into this session."""

    plugins: int
    mcp_servers: int
    sessions: int
    config_path: str
    config_exists: bool


@dataclass
class SystemSnapshot:
    """Static, build-once snapshot of repository + subsystem state.

    Live session fields (model/mode/context) intentionally live in
    :class:`LiveSessionState`, not here, so the dashboard can refresh them without
    rebuilding the expensive on-disk reads.
    """

    workspace: str
    git_branch: str | None
    working_tree_dirty: int  # files changed vs HEAD (-1 if unknown)
    index: IndexLine
    memory_tables: list[MemoryTableStat]
    integrations: IntegrationsLine
    health: list[tuple[str, str]] = field(default_factory=list)  # (state, message)

    @property
    def memory_total_rows(self) -> int:
        return sum(t.rows for t in self.memory_tables)


@dataclass
class LiveSessionState:
    """Fast-changing session fields, re-read every dashboard tick."""

    model_id: str | None
    mode_label: str
    context_pct: float
    provider_id: str | None = None
    context_used: int | None = None
    context_max: int | None = None


def build_system_snapshot(
    workspace: Path,
    *,
    plugin_count: int = 0,
    mcp_count: int = 0,
    session_count: int = 0,
) -> SystemSnapshot:
    """Compose a :class:`SystemSnapshot` for *workspace* from real on-disk state.

    The subsystem counts (plugins/MCP/sessions) are passed in by the caller because
    they live on the running REPL's registries, not on disk; everything else is
    derived from :func:`build_context_report`.
    """
    workspace = Path(workspace).resolve()
    report = build_context_report(workspace)

    config_path = workspace / "velune.toml"

    return SystemSnapshot(
        workspace=report.workspace,
        git_branch=report.git_branch,
        working_tree_dirty=report.working_tree_dirty,
        index=IndexLine(
            freshness=report.freshness,
            files=report.indexed_file_count,
            symbols=report.total_symbols,
            exists=report.index_exists,
        ),
        memory_tables=list(report.memory_tables),
        integrations=IntegrationsLine(
            plugins=plugin_count,
            mcp_servers=mcp_count,
            sessions=session_count,
            config_path=str(config_path),
            config_exists=config_path.exists(),
        ),
        health=list(report.health),
    )
