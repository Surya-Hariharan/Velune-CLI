"""The one place the native acting stack is assembled, with one credential-aware scrubber.

The provider adapter, the trusted tool executor and the acting runner each accept a ``scrub``
callable, and each alone would default to something weaker. This factory builds a single scrubber and
hands the same instance to all three, so provider-bound text, approval displays, tool output (scrubbed
before it is clipped), model text and the final draft are all covered by the same rules.

If the scrubber cannot be built or verified, :class:`ScrubberUnavailable` is raised before anything
else is constructed. There is no fallback to weaker redaction.

What this does not do: create a provider, read a feature flag, touch the CLI, change approval (no
``approvals`` still means a port that denies everything), or change retries, budgets, concurrency or
cancellation. Scrubbing is pattern and exact-value matching; it cannot find an encoded, split, hashed
or short (under ``MIN_SECRET_CHARS``) secret, or one this process does not hold.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from velune.orchestration.engine.acting import ActingAgentRunner
from velune.orchestration.engine.approval import ApprovalLedger, ApprovalPort
from velune.orchestration.engine.ports import PromptResolver
from velune.orchestration.engine.toolexec import TrustedToolExecutor
from velune.orchestration.engine.toolpolicy import descriptors_from_registry
from velune.orchestration.engine_adapters.provider_acting import ProviderActingInvoker
from velune.orchestration.engine_adapters.secrets import credential_scrubber


@dataclass(frozen=True)
class ActingStack:
    runner: ActingAgentRunner
    invoker: ProviderActingInvoker
    executor: TrustedToolExecutor
    scrub: Callable[[str], str]


def build_acting_stack(
    provider: Any,
    model_id: str,
    registry: Any,
    workspace: Path,
    prompts: PromptResolver,
    ids: Any,  # a council ``IdSource``; typed loosely so adapters import no council module
    *,
    approvals: ApprovalPort | None = None,
    ledger: ApprovalLedger | None = None,
    audit: Callable[[dict[str, Any]], None] | None = None,
    scrub: Callable[[str], str] | None = None,
    invoker_options: Mapping[str, Any] | None = None,
    runner_options: Mapping[str, Any] | None = None,
) -> ActingStack:
    """Assemble invoker, executor and runner around one scrubber.

    ``scrub`` is for tests and callers that already hold a verified scrubber; by default the strict,
    verified :func:`credential_scrubber` is built here. ``provider`` and ``model_id`` are explicit:
    nothing is looked up or substituted.
    """
    cleaner = scrub if scrub is not None else credential_scrubber()  # raises; builds nothing else
    catalog = descriptors_from_registry(registry)
    invoker = ProviderActingInvoker(
        provider, model_id, catalog, scrub=cleaner, **dict(invoker_options or {})
    )
    executor = TrustedToolExecutor(
        registry, workspace, approvals, ledger=ledger, audit=audit, scrub=cleaner
    )
    runner = ActingAgentRunner(
        invoker, executor, prompts, catalog, ids=ids, scrub=cleaner, **dict(runner_options or {})
    )
    return ActingStack(runner=runner, invoker=invoker, executor=executor, scrub=cleaner)


__all__ = ["ActingStack", "build_acting_stack"]
