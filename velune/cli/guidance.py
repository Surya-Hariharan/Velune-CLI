"""Workflow guidance: what to suggest after a command completes.

Every command should follow Do → Explain → Suggest → Recover → Next. This module
holds the **Suggest / Next** half as pure data so adding guidance to a command is a
data edit, not new rendering code — mirroring how ``_BUILTIN_CATEGORIES`` centralizes
category metadata in :mod:`velune.cli.slash_dispatcher`.

A command handler looks up its outcome here and feeds the resulting steps to
:func:`velune.cli.ui.next_steps`. Recovery on the failure path is handled by the
existing ``VeluneError.fix`` lists + ``error_panel``; this module covers the success
and soft-failure (e.g. saved-but-unvalidated) paths.

Steps are ``(label, command, why)`` tuples, matching :data:`velune.cli.ui.Step`.
Some entries support ``{value}`` placeholders filled in by the caller via
:func:`steps_for`.
"""

from __future__ import annotations

from velune.cli.ui import Step

# Outcome key → ordered follow-up steps. Keys are stable identifiers a handler
# passes to :func:`steps_for`; they are not user-facing command names.
_GUIDANCE: dict[str, list[Step]] = {
    # ── Providers ────────────────────────────────────────────────────────────
    "provider_added": [
        ("Discover available models", "/models scan", "probe each provider"),
        ("Set your default model", "/model {model}", None),
        ("See everything at a glance", "/status", "live system dashboard"),
    ],
    "provider_added_unvalidated": [
        ("Verify connectivity later", "velune doctor providers", "re-check health"),
        ("Discover models once reachable", "/models scan", None),
    ],
    # ── Models ───────────────────────────────────────────────────────────────
    "models_scanned": [
        ("Assign a default model", "/model {model}", None),
        ("Benchmark for the best fit", "/models benchmark", "empirical scoring"),
        ("Start working", "velune chat", None),
    ],
    "models_listed_multi": [
        ("Pick a default model", "/model {model}", None),
        ("Compare them empirically", "/models benchmark", None),
    ],
    # ── Council / planning ───────────────────────────────────────────────────
    "ask_completed": [
        ("Execute this plan", "velune run {task}", "autonomous, checkpointed"),
        ("Iterate interactively", "velune chat", None),
    ],
    # ── Autonomous run ───────────────────────────────────────────────────────
    "run_succeeded": [
        ("Review the changes", "/diff", None),
        ("Inspect what ran", "velune trace --run {run_id}", "execution trace"),
    ],
    "run_rolled_back": [
        ("Inspect why it blocked", "velune trace --run {run_id}", None),
        ("Diagnose the environment", "velune doctor check", None),
        ("Retry interactively", "velune chat", "step through the fix"),
    ],
}


def steps_for(outcome: str, **values: str) -> list[Step]:
    """Return the follow-up steps for *outcome*, filling ``{name}`` placeholders.

    Unknown outcomes return an empty list, so a handler can call this
    unconditionally and only render a footer when steps exist. A placeholder with
    no matching value is dropped from that step's command gracefully (left as-is).
    """
    steps = _GUIDANCE.get(outcome)
    if not steps:
        return []
    rendered: list[Step] = []
    for label, command, why in steps:
        try:
            command = command.format(**values)
        except (KeyError, IndexError):
            # Missing substitution → keep the literal template rather than crash.
            pass
        rendered.append((label, command, why))
    return rendered
