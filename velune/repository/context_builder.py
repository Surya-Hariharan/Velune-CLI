"""WorkspaceContextBuilder — converts a RepositorySnapshot into rich, structured context text.

The orchestrator previously injected a flat list of the first 25 files into every
council run, discarding the architecture analysis, symbol counts, git state, dependency
violations, and delta information that the indexer already computed.  This module
replaces that with a compact, layered context string that gives agents a genuine
mental model of the workspace.

Output sections (in order):
  1. Workspace header   — root, branch, uncommitted count, frameworks  (~120 tokens)
  2. Recent changes     — delta add/modify/delete with blast-radius hint  (only when delta)
  3. File index         — files grouped by architectural layer, symbols noted  (budget-capped)
  4. Architecture drift — violations that are never trimmed from the prompt  (separate block)

Design constraints:
  • No I/O inside the builder — it operates only on the already-computed snapshot.
  • The returned (repo_context, violations_context) pair maps directly to the
    REPOSITORY_SNAPSHOT and ARCHITECTURAL_DRIFT context sections.
  • Token budget is approximated at ~4 chars/token (fast, good-enough for truncation).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from velune.repository.incremental_indexer import IndexDelta
    from velune.repository.schemas import RepositorySnapshot

logger = logging.getLogger("velune.repository.context_builder")

# Approximate chars-per-token ratio used for budget estimation.
_CHARS_PER_TOKEN = 4

# Hard cap on the file-index section so large monorepos don't blow the budget.
_MAX_FILE_INDEX_TOKENS = 1200
# Maximum violations to surface in ARCHITECTURAL_DRIFT.
_MAX_VIOLATIONS = 8


class WorkspaceContextBuilder:
    """Converts a RepositorySnapshot (+ optional IndexDelta) into context strings.

    Usage::

        builder = WorkspaceContextBuilder()
        repo_ctx, drift_ctx = builder.build(snapshot, delta=cog_service.last_delta)
        # repo_ctx  → inject as REPOSITORY_SNAPSHOT
        # drift_ctx → inject as ARCHITECTURAL_DRIFT (or None if no violations)
    """

    def build(
        self,
        snapshot: RepositorySnapshot,
        delta: IndexDelta | None = None,
        max_snapshot_tokens: int = 2000,
    ) -> tuple[str, str | None]:
        """Return (repository_snapshot_text, architectural_drift_text_or_None).

        Both strings are plain text suitable for direct injection into an LLM
        user-message or context section.
        """
        parts: list[str] = []

        # ── 1. Workspace header ────────────────────────────────────────────
        parts.append(self._build_header(snapshot))

        # ── 2. Recent changes (highest relevance when present) ─────────────
        if delta and not delta.is_empty:
            parts.append(self._build_delta_section(snapshot, delta))

        # ── 3. File index (budget-capped) ─────────────────────────────────
        used_tokens = sum(len(p) // _CHARS_PER_TOKEN for p in parts)
        remaining = max_snapshot_tokens - used_tokens
        if remaining > 200:
            file_index = self._build_file_index(snapshot, max_tokens=min(remaining, _MAX_FILE_INDEX_TOKENS))
            if file_index:
                parts.append(file_index)

        repo_context = "\n\n".join(p for p in parts if p)

        # ── 4. Architectural drift (separate — never trimmed) ─────────────
        drift_context = self._build_drift_section(snapshot)

        return repo_context, drift_context

    # ------------------------------------------------------------------
    # Section builders
    # ------------------------------------------------------------------

    def _build_header(self, snapshot: RepositorySnapshot) -> str:
        lines: list[str] = []

        # Root + git state
        git = snapshot.summary.get("git", {})
        branch = git.get("active_branch") or "unknown"
        uncommitted = git.get("uncommitted_changes_count", 0)
        root = snapshot.root_path

        git_badge = f"branch:{branch}"
        if uncommitted:
            git_badge += f"  {uncommitted} uncommitted"

        lines.append(f"[WORKSPACE: {root}  |  {git_badge}]")

        # Language breakdown
        lang_counts: dict[str, int] = {}
        for f in snapshot.files:
            lang = f.language.value if hasattr(f.language, "value") else str(f.language)
            if lang and lang != "unknown":
                lang_counts[lang] = lang_counts.get(lang, 0) + 1
        if lang_counts:
            lang_str = "  ".join(
                f"{lang}×{count}"
                for lang, count in sorted(lang_counts.items(), key=lambda x: -x[1])
            )
            lines.append(f"Languages: {lang_str}")

        # Framework stack
        arch = snapshot.summary.get("architecture", {})
        frameworks = arch.get("frameworks_detected", [])
        if frameworks:
            lines.append("Frameworks: " + "  ".join(sorted(frameworks)[:10]))

        # Layer breakdown
        layers = arch.get("layers", {})
        if layers:
            layer_str = "  ".join(
                f"{name}({count})"
                for name, count in sorted(layers.items(), key=lambda x: -x[1])
                if count > 0
            )
            lines.append(f"Layers: {layer_str}")

        # Summary counts
        total_files = snapshot.summary.get("total_files", len(snapshot.files))
        total_symbols = snapshot.summary.get("total_symbols", len(snapshot.symbols))
        violations_count = arch.get("violations_count", 0)
        viol_badge = f"  ⚠ {violations_count} layer violations" if violations_count else ""
        lines.append(f"Files: {total_files}  Symbols: {total_symbols}{viol_badge}")

        # Recent commits
        recent_commits = git.get("recent_commits", [])
        if recent_commits:
            commit_lines = []
            for c in recent_commits[:3]:
                if isinstance(c, dict):
                    msg = c.get("message", "")[:60]
                    sha = c.get("sha", "")[:7]
                    commit_lines.append(f"  {sha} {msg}")
                elif isinstance(c, str):
                    commit_lines.append(f"  {c[:70]}")
            if commit_lines:
                lines.append("Recent commits:\n" + "\n".join(commit_lines))

        return "\n".join(lines)

    def _build_delta_section(self, snapshot: RepositorySnapshot, delta: IndexDelta) -> str:
        lines: list[str] = ["[RECENT CHANGES]"]

        # Build a quick blast-radius lookup from the snapshot's edge data
        fan_in: dict[str, int] = {}
        for edge in snapshot.edges:
            fan_in[edge.target] = fan_in.get(edge.target, 0) + 1

        def _fmt(path: str, prefix: str) -> str:
            fi = fan_in.get(path, 0)
            blast = f"  (fan-in: {fi})" if fi >= 3 else ""
            return f"  {prefix} {path}{blast}"

        for p in sorted(delta.to_add)[:15]:
            lines.append(_fmt(p, "+"))
        for p in sorted(delta.to_update)[:15]:
            lines.append(_fmt(p, "~"))
        for p in sorted(delta.to_remove)[:10]:
            lines.append(_fmt(p, "-"))

        total = delta.total
        shown = min(total, 40)
        if total > shown:
            lines.append(f"  ... and {total - shown} more")

        return "\n".join(lines)

    def _build_file_index(self, snapshot: RepositorySnapshot, max_tokens: int) -> str:
        """File list grouped by architectural layer, truncated to *max_tokens*."""
        arch = snapshot.summary.get("architecture", {})
        layers: dict[str, list[str]] = arch.get("layers", {})

        # Build layer → file list from the snapshot
        # layers dict currently maps layer_name → count; we need actual file paths
        # So we classify files by matching their path prefixes to known layer names.
        layer_to_files: dict[str, list] = {}
        uncategorized: list = []

        _LAYER_PREFIXES: dict[str, list[str]] = {
            "kernel": ["kernel"],
            "core": ["core"],
            "providers": ["providers"],
            "memory": ["memory"],
            "cognition": ["cognition"],
            "retrieval": ["retrieval"],
            "context": ["context"],
            "execution": ["execution"],
            "repository": ["repository"],
            "cli": ["cli"],
            "observability": ["observability"],
            "models": ["models"],
            "telemetry": ["telemetry"],
            "plugins": ["plugins"],
        }

        # Build volatility lookup
        metrics = snapshot.summary.get("metrics", {})
        high_vol = {path for path, _ in metrics.get("high_volatility_files", [])}

        for f in snapshot.files:
            path_norm = f.path.replace("\\", "/")
            matched = False
            for layer, prefixes in _LAYER_PREFIXES.items():
                if any(f"/{p}/" in f"/{path_norm}" or path_norm.startswith(f"{p}/") for p in prefixes):
                    layer_to_files.setdefault(layer, []).append(f)
                    matched = True
                    break
            if not matched:
                uncategorized.append(f)

        lines: list[str] = ["[FILE INDEX — by layer]"]
        chars_used = len(lines[0])
        budget_chars = max_tokens * _CHARS_PER_TOKEN

        for layer in sorted(layer_to_files.keys()):
            files = sorted(layer_to_files[layer], key=lambda f: f.path)
            layer_header = f"{layer}/"
            lines.append(layer_header)
            chars_used += len(layer_header)

            for f in files:
                path_norm = f.path.replace("\\", "/")
                basename = path_norm.split("/")[-1]
                sym_names = [s.name for s in f.symbols[:4]]
                sym_str = f"({', '.join(sym_names)})" if sym_names else ""
                vol_mark = "★" if f.path in high_vol else ""
                entry = f"  {basename}{vol_mark} {sym_str}"
                if chars_used + len(entry) > budget_chars:
                    remaining_count = sum(len(v) for v in layer_to_files.values()) - len(files)
                    lines.append(f"  ... [{remaining_count}+ more files omitted for brevity]")
                    return "\n".join(lines)
                lines.append(entry)
                chars_used += len(entry)

        if uncategorized and chars_used < budget_chars:
            lines.append("other/")
            for f in uncategorized[:10]:
                basename = f.path.split("/")[-1]
                entry = f"  {basename}"
                lines.append(entry)
                chars_used += len(entry)

        return "\n".join(lines)

    def _build_drift_section(self, snapshot: RepositorySnapshot) -> str | None:
        """Build ARCHITECTURAL_DRIFT text from computed violations, or None."""
        arch = snapshot.summary.get("architecture", {})
        violations = arch.get("violations", [])
        if not violations:
            return None

        lines = [f"[ARCHITECTURE VIOLATIONS — {len(violations)} issue(s) detected]"]
        for v in violations[:_MAX_VIOLATIONS]:
            if isinstance(v, dict):
                src = v.get("from", v.get("source", "?"))
                tgt = v.get("to", v.get("target", "?"))
                reason = v.get("reason", v.get("message", "layer boundary crossed"))
                lines.append(f"  ⚠ {src} → {tgt}: {reason}")
            elif isinstance(v, str):
                lines.append(f"  ⚠ {v}")

        if len(violations) > _MAX_VIOLATIONS:
            lines.append(f"  ... and {len(violations) - _MAX_VIOLATIONS} more (run `velune workspace graph`)")

        return "\n".join(lines)
