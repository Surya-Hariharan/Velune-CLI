"""Non-UI onboarding logic: progress persistence, model scoring, workspace
detection, health-check wiring. Extracted unchanged (behaviorally) from the
former monolithic ``onboarding.py`` so the interaction redesign in
``stages.py`` doesn't also have to re-derive or re-verify this logic.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from velune.core.types.model import ModelDescriptor

_log = logging.getLogger("velune.cli.onboarding")

_MAX_LOCAL_RETRIES = 3
_MAX_KEY_ATTEMPTS = 3

# ── Progress persistence ───────────────────────────────────────────────────

_PROGRESS_FILE = Path.home() / ".velune" / "onboarding_progress.json"

_STAGE_NAMES: tuple[str, ...] = (
    "welcome",
    "detect_environment",
    "configure_providers",
    "discover_models",
    "select_default_model",
    "health_check",
    "workspace_setup",
    "ready",
)


def _read_progress_file() -> dict:
    try:
        if _PROGRESS_FILE.exists():
            return json.loads(_PROGRESS_FILE.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


def _write_progress_file(data: dict) -> None:
    try:
        _PROGRESS_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = _PROGRESS_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        tmp.replace(_PROGRESS_FILE)
    except Exception as exc:
        _log.warning("Could not save onboarding progress: %s", exc)


def save_stage_progress(stage: str) -> None:
    """Append *stage* to the completed-stages list in the progress file."""
    data = _read_progress_file()
    stages: list[str] = data.get("completed_stages", [])
    if stage not in stages:
        stages.append(stage)
    data["completed_stages"] = stages
    data["version"] = 1
    _write_progress_file(data)


def load_stage_progress() -> list[str]:
    """Return the list of completed stage names (empty list if not started)."""
    return _read_progress_file().get("completed_stages", [])


def mark_onboarding_complete() -> None:
    """Write the 'complete' flag so onboarding_state() returns 'returning'."""
    data = _read_progress_file()
    data["complete"] = True
    data["version"] = 1
    _write_progress_file(data)


def has_shown_alt_screen_notice() -> bool:
    """Whether the one-time "Velune takes over your terminal" note has run.

    Tracked separately from ``onboarding_state()`` (which flips to "returning"
    once providers/model are configured) because the alt-screen takeover is a
    REPL-launch behavior, not an onboarding step — a user who runs
    ``velune onboard`` and exits without ever launching the fullscreen REPL
    should still see the note the first time they actually do.
    """
    return bool(_read_progress_file().get("alt_screen_notice_shown"))


def mark_alt_screen_notice_shown() -> None:
    data = _read_progress_file()
    data["alt_screen_notice_shown"] = True
    _write_progress_file(data)


def onboarding_state() -> Literal["returning", "partial", "fresh"]:
    """Classify the current installation state.

    Returns
    -------
    "returning"
        Both providers and a default model are configured — skip onboarding.
    "partial"
        Providers exist but no model is selected — run abbreviated onboarding
        starting at model discovery.
    "fresh"
        No providers configured at all — run the full wizard.
    """
    if _read_progress_file().get("complete"):
        return "returning"

    from velune.cli.model_prefs import load_active_model
    from velune.providers.keystore import list_configured_providers

    configured = list_configured_providers()
    model = load_active_model()

    if configured and model:
        return "returning"
    if configured:
        return "partial"
    return "fresh"


# ── Model scoring ────────────────────────────────────────────────────────────


def score_models(
    models: list[ModelDescriptor],
    preferred_local: bool,
) -> ModelDescriptor | None:
    """Pick the best default model using a lightweight scoring heuristic.

    Does not require Tier-1 subsystems — works purely from the descriptor
    fields returned by ``ModelDiscoveryScanner.scan_all()``.
    """
    if not models:
        return None

    def _cap(m: ModelDescriptor) -> int:
        caps = m.capabilities
        if caps is None:
            return 0
        if hasattr(caps, "coding"):
            return int(caps.coding or 0) + int(getattr(caps, "reasoning", 0) or 0)
        if isinstance(caps, dict):
            return int(caps.get("coding", 0)) + int(caps.get("reasoning", 0))
        return 0

    def _score(m: ModelDescriptor) -> tuple[int, ...]:
        cap = _cap(m)
        local_bonus = 8 if (preferred_local and m.is_local) else (3 if m.is_local else 0)
        speed_bonus = {"fast": 6, "medium": 3, "slow": 0}.get(m.speed_tier, 0)
        ctx_score = min(m.context_length // 10_000, 12)
        return (cap + local_bonus + speed_bonus + ctx_score,)

    return max(models, key=_score)


def model_why(model: ModelDescriptor, preferred_local: bool) -> str:
    """Generate a short human-readable reason string for the recommended model."""
    parts: list[str] = []

    if model.is_local:
        parts.append("local · private · free")
    elif model.cost_per_1k_tokens is not None:
        if model.cost_per_1k_tokens < 0.001:
            parts.append("very affordable")
        elif model.cost_per_1k_tokens < 0.01:
            parts.append("affordable")

    if model.speed_tier == "fast":
        parts.append("fast inference")
    elif model.speed_tier == "slow":
        parts.append("highest quality")

    ctx_k = model.context_length // 1_000
    if ctx_k >= 32:
        parts.append(f"{ctx_k}k context")

    caps = model.capabilities
    coding_score = 0
    if hasattr(caps, "coding"):
        coding_score = int(caps.coding or 0)
    elif isinstance(caps, dict):
        coding_score = int(caps.get("coding", 0))

    if coding_score >= 75:
        parts.append("strong coding")

    return "  ·  ".join(parts) if parts else "best available match"


# ── Workspace detection ─────────────────────────────────────────────────────

_REPO_MARKERS: tuple[tuple[str, str], ...] = (
    (".git", "Git"),
    ("pyproject.toml", "Python"),
    ("requirements.txt", "Python"),
    ("package.json", "Node.js"),
    ("Cargo.toml", "Rust"),
    ("go.mod", "Go"),
    ("pom.xml", "Java"),
    ("build.gradle", "Kotlin/Java"),
)


def detect_repo_marker(workspace: Path) -> tuple[str, str] | None:
    """Return ``(repo_name, project_type)`` if *workspace* looks like a repo."""
    try:
        for marker, ptype in _REPO_MARKERS:
            if (workspace / marker).exists():
                return workspace.name or str(workspace), ptype
    except Exception:
        pass
    return None


# ── Health checks ────────────────────────────────────────────────────────────


def build_health_checks() -> list[tuple[str, Callable[[], dict]]]:
    """Ordered list of ``(display_name, check_fn)`` pairs. ``check_fn()``
    returns a ``{"status": ..., "message": ...}`` dict (see ``commands/doctor.py``).
    """
    from velune.cli.commands.doctor import (
        _check_internet_connectivity,
        _check_ollama_connectivity,
        _check_python_version,
        _check_sqlite,
        _check_velune_dir,
    )

    checks: list[tuple[str, Callable[[], dict]]] = [
        ("Python version", _check_python_version),
        (".velune directory", _check_velune_dir),
        ("SQLite", _check_sqlite),
        ("Internet connectivity", _check_internet_connectivity),
        ("Ollama (local)", _check_ollama_connectivity),
    ]

    try:
        from velune.providers.keystore import has_key

        if has_key("groq"):
            from velune.cli.commands.doctor import _check_groq

            checks.append(("Groq API key", _check_groq))
    except Exception:
        pass

    return checks


def run_health_checks() -> list[dict]:
    """Run every check from :func:`build_health_checks` and return results."""
    checks = build_health_checks()
    results: list[dict] = []
    for name, fn in checks:
        try:
            result = fn()
        except Exception as exc:
            result = {"name": name, "status": "warn", "message": str(exc)}
        results.append(result)
    return results
