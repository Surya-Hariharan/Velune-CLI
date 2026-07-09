"""Subsystem module registry, split by startup tier.

The synchronous startup path imports **only** the Tier-0 modules below. The
Tier-1 (background warm-up) module lists are imported lazily by
:func:`load_background_modules` — importing them eagerly would run the heavy
package ``__init__`` chains (``orchestration.engine`` alone is ~1s, plus
memory / retrieval / tools / repository) on the critical path, which is exactly
what made the first prompt slow.
"""

from __future__ import annotations

from velune.kernel.bootstrap import SubsystemModule
from velune.kernel.subsystems import KERNEL_MODULES
from velune.models.subsystems import MODEL_MODULES
from velune.observability.subsystems import OBSERVABILITY_MODULES
from velune.providers.subsystems import PROVIDER_MODULES

# Tier 0 — cheap, synchronous, required to render an interactive prompt and to
# serve lightweight commands (/model, /models, /help, /mode). All have trivial
# package __init__ files, so importing them here costs ~0.007s.
CORE_MODULES: tuple[SubsystemModule, ...] = tuple(
    KERNEL_MODULES + PROVIDER_MODULES + MODEL_MODULES + OBSERVABILITY_MODULES
)


def load_background_modules() -> list[SubsystemModule]:
    """Import and return the Tier-1 subsystem modules (background warm-up).

    Deferred so the expensive package imports never run on the synchronous
    startup path. Called from the REPL's background bootstrap task once the
    prompt is already interactive.
    """
    from velune.cognition.subsystems import COGNITION_MODULES
    from velune.execution.subsystems import EXECUTION_MODULES
    from velune.intelligence.subsystems import INTELLIGENCE_MODULES
    from velune.knowledge.subsystems import KNOWLEDGE_MODULES
    from velune.memory.subsystems import MEMORY_MODULES
    from velune.orchestration.subsystems import ORCHESTRATION_MODULES
    from velune.repository.subsystems import REPOSITORY_MODULES
    from velune.retrieval.subsystems import RETRIEVAL_MODULES
    from velune.tools.subsystems import TOOL_MODULES

    return list(
        REPOSITORY_MODULES
        + MEMORY_MODULES
        + RETRIEVAL_MODULES
        + EXECUTION_MODULES
        + TOOL_MODULES
        + COGNITION_MODULES
        + ORCHESTRATION_MODULES
        + KNOWLEDGE_MODULES
        + INTELLIGENCE_MODULES
    )


def __getattr__(name: str):
    # Back-compat: a few call sites and tests import ``ALL_MODULES``. Resolve it
    # lazily so merely importing this module never triggers the heavy Tier-1
    # imports.
    if name == "ALL_MODULES":
        return list(CORE_MODULES) + load_background_modules()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
