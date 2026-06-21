"""WorkspaceContextBuilder — converts a RepositorySnapshot into rich, structured context text.

Output sections (in order):
  1. Workspace header       — root, branch, tech-stack, layer breakdown  (~120 tokens)
  2. API connection map     — frontend↔backend↔db chains (never trimmed if present)
  3. Recent changes         — delta add/modify/delete with blast-radius hint
  4. File index             — files grouped by adaptive architectural layer (budget-capped)
  5. Architectural drift    — violations, separate block, never trimmed

Design constraints:
  • No I/O inside the builder — operates only on the already-computed snapshot.
  • (repo_context, violations_context) pair maps directly to REPOSITORY_SNAPSHOT
    and ARCHITECTURAL_DRIFT context sections.
  • Token budget approximated at ~4 chars/token.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from velune.repository.api_mapper import APIConnectionMap
    from velune.repository.incremental_indexer import IndexDelta
    from velune.repository.schemas import RepositorySnapshot

logger = logging.getLogger("velune.repository.context_builder")

_CHARS_PER_TOKEN = 4
_MAX_FILE_INDEX_TOKENS = 1200
_MAX_VIOLATIONS = 8


class WorkspaceContextBuilder:
    """Converts a RepositorySnapshot (+ optional IndexDelta) into context strings."""

    def build(
        self,
        snapshot: RepositorySnapshot,
        delta: IndexDelta | None = None,
        max_snapshot_tokens: int = 2500,
        api_map: APIConnectionMap | None = None,
    ) -> tuple[str, str | None]:
        """Return (repository_snapshot_text, architectural_drift_text_or_None)."""
        parts: list[str] = []

        # ── 1. Workspace header ────────────────────────────────────────────
        parts.append(self._build_header(snapshot))

        # ── 2. API connection map (highest signal — always include if present) ─
        if api_map is not None and not api_map.is_empty:
            api_section = self._build_api_map_section(api_map)
            if api_section:
                parts.append(api_section)

        # ── 3. Recent changes ──────────────────────────────────────────────
        if delta and not delta.is_empty:
            parts.append(self._build_delta_section(snapshot, delta))

        # ── 4. File index (budget-capped) ──────────────────────────────────
        used_tokens = sum(len(p) // _CHARS_PER_TOKEN for p in parts)
        remaining = max_snapshot_tokens - used_tokens
        if remaining > 200:
            file_index = self._build_file_index(
                snapshot, max_tokens=min(remaining, _MAX_FILE_INDEX_TOKENS)
            )
            if file_index:
                parts.append(file_index)

        repo_context = "\n\n".join(p for p in parts if p)

        # ── 5. Architectural drift (separate — never trimmed) ──────────────
        drift_context = self._build_drift_section(snapshot)

        return repo_context, drift_context

    # ------------------------------------------------------------------
    # Section builders
    # ------------------------------------------------------------------

    def _build_header(self, snapshot: RepositorySnapshot) -> str:
        lines: list[str] = []

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

        arch = snapshot.summary.get("architecture", {})

        # Rich tech stack (from TechnologyDetector) — preferred over old project_types
        tech = arch.get("tech_stack", {})
        if tech:
            tech_parts: list[str] = []
            if tech.get("language"):
                tech_parts.append(tech["language"])
            if tech.get("framework"):
                ver = tech.get("framework_version", "")
                tech_parts.append(tech["framework"] + (f" {ver}" if ver else ""))
            if tech.get("frontend") and tech.get("frontend") != tech.get("framework"):
                tech_parts.append(tech["frontend"])
            if tech.get("auth"):
                tech_parts.append(tech["auth"])
            if tech.get("router"):
                tech_parts.append(tech["router"])
            if tech_parts:
                lines.append("Stack: " + "  |  ".join(tech_parts))
        else:
            # Fallback: old project_types
            project_types = arch.get("project_types", [])
            if project_types:
                lines.append("Stack: " + "  ".join(sorted(project_types)))

        # Architecture pattern (from ArchitectureDetector)
        arch_report = arch.get("arch_report", {})
        if arch_report.get("pattern") and arch_report["pattern"] != "Unknown":
            lines.append("Pattern: " + arch_report["pattern"])
            if arch_report.get("features"):
                lines.append("Features: " + "  ".join(arch_report["features"]))

        # Framework stack (legacy / supplemental)
        frameworks = arch.get("frameworks_detected", [])
        if frameworks and not tech:
            lines.append("Frameworks: " + "  ".join(frameworks[:12]))

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

    def _build_api_map_section(self, api_map: APIConnectionMap) -> str | None:
        """Render the cross-stack API connection map."""
        from velune.repository.api_mapper import render_api_map

        return render_api_map(api_map, max_tokens=1500)

    def _build_delta_section(self, snapshot: RepositorySnapshot, delta: IndexDelta) -> str:
        lines: list[str] = ["[RECENT CHANGES]"]

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
        """File list grouped by adaptive architectural layer."""
        arch = snapshot.summary.get("architecture", {})

        # Build layer → file list.  The layers in arch come from the adaptive
        # classifier which already assigned each file to a semantic layer, so
        # we only need to re-group by those layer names.
        layer_to_files: dict[str, list] = {}
        uncategorized: list = []

        # Get the layer membership from summary (set by cognition._run_pipeline)
        layer_membership: dict[str, list[str]] = arch.get("layer_membership", {})

        if layer_membership:
            # Fast path: use pre-computed membership
            path_to_layer: dict[str, str] = {}
            for layer, paths in layer_membership.items():
                for p in paths:
                    path_to_layer[p.replace("\\", "/")] = layer

            for f in snapshot.files:
                path_norm = f.path.replace("\\", "/")
                layer = path_to_layer.get(path_norm)
                if layer and layer != "other":
                    layer_to_files.setdefault(layer, []).append(f)
                else:
                    uncategorized.append(f)
        else:
            # Fallback: heuristic grouping when layer_membership not stored
            uncategorized = list(snapshot.files)

        # Volatility lookup
        metrics = snapshot.summary.get("metrics", {})
        high_vol = {path for path, _ in metrics.get("high_volatility_files", [])}

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
                    lines.append("  ... [more files omitted for brevity]")
                    return "\n".join(lines)
                lines.append(entry)
                chars_used += len(entry)

        if uncategorized and chars_used < budget_chars:
            lines.append("other/")
            for f in uncategorized[:15]:
                basename = f.path.split("/")[-1] if "/" in f.path else f.path.split("\\")[-1]
                entry = f"  {basename}"
                lines.append(entry)
                chars_used += len(entry)
                if chars_used > budget_chars:
                    break

        return "\n".join(lines)

    def _build_drift_section(self, snapshot: RepositorySnapshot) -> str | None:
        arch = snapshot.summary.get("architecture", {})
        violations = arch.get("violations", [])
        if not violations:
            return None

        lines = [f"[ARCHITECTURE VIOLATIONS — {len(violations)} issue(s) detected]"]
        for v in violations[:_MAX_VIOLATIONS]:
            if isinstance(v, dict):
                src = v.get("from", v.get("source", "?"))
                tgt = v.get("to", v.get("target", "?"))
                reason = v.get("reason", v.get("rule", v.get("message", "layer boundary crossed")))
                lines.append(f"  ⚠ {src} → {tgt}: {reason}")
            elif isinstance(v, str):
                lines.append(f"  ⚠ {v}")

        if len(violations) > _MAX_VIOLATIONS:
            lines.append(
                f"  ... and {len(violations) - _MAX_VIOLATIONS} more (run `velune workspace graph`)"
            )

        return "\n".join(lines)
