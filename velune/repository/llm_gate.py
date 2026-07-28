"""Enforcement guard: no LLM-reasoning pass may run in the synchronous
cold-start indexing path.

Nothing in the repository-intelligence pipeline calls an LLM today — this
is a forward-looking guardrail, not a fix for an existing call site. It
exists because the architecture direction this pipeline is heading toward
(a semantic capability layer above the structural graph — see
``velune.knowledge.schemas.NodeType.CAPABILITY``/``RUNTIME_ENTRYPOINT``,
and docs/REPOSITORY_INTELLIGENCE_BASELINE.md's discussion of "targeted LLM
reasoning") will eventually need one, and the cold-start path is exactly
the wrong place for it to end up.

``RepositoryCognitionService.index()`` is documented as synchronous and
unbounded (baseline §3.1: "no size/time cap; can take seconds on large/
vendor-heavy repos"), and the only thing currently protecting a user from
that is an *outer* 5-second timeout at the context-assembly call site
(``prompt_context.py``) that abandons the result and degrades to zero
repository context — it does not cancel or bound the work already
underway. A blocking LLM call added inside ``index()`` wouldn't be caught
by that timeout in any useful sense: the calling coroutine moves on, but
the worker thread stays blocked for the LLM call's full duration, wasting
the exact resource (time on a cold start) the timeout exists to protect.

Usage
-----
Wrap the synchronous cold-start entry point once::

    with cold_start_scope():
        ...  # RepositoryCognitionService.index()'s body

And have every LLM-invoking function in this pipeline call the guard as
its first line::

    def infer_capability_names(cluster) -> list[Claim]:
        ensure_background_only("capability-name inference")
        ...  # call the LLM

``ensure_background_only`` raises immediately if called while a cold-start
scope is active, so a future regression fails loudly in development/tests
rather than silently shipping a slow cold start.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar

_in_cold_start: ContextVar[bool] = ContextVar("_in_cold_start", default=False)


class ColdStartLLMCallError(RuntimeError):
    """Raised when an LLM-reasoning pass is attempted inside a cold-start scope."""


@contextmanager
def cold_start_scope():
    """Mark the current call stack as the synchronous cold-start indexing path.

    Wrap ``RepositoryCognitionService.index()``'s body with this — any
    ``ensure_background_only()`` call made (directly or transitively) while
    the scope is active raises.
    """
    token = _in_cold_start.set(True)
    try:
        yield
    finally:
        _in_cold_start.reset(token)


def is_in_cold_start_scope() -> bool:
    """True if the current call stack is inside an active ``cold_start_scope``."""
    return _in_cold_start.get()


def ensure_background_only(operation_name: str) -> None:
    """Raise if called from within an active ``cold_start_scope``.

    Call this as the first line of any function that invokes an LLM as
    part of repository-intelligence analysis (capability naming, framework-
    role inference, or any other "targeted LLM reasoning" pass). *operation_name*
    is a short human-readable description used only in the error message.
    """
    if _in_cold_start.get():
        raise ColdStartLLMCallError(
            f"{operation_name!r} attempted an LLM-reasoning pass inside the "
            "synchronous cold-start indexing path. LLM-reasoning passes must "
            "only run in the background (e.g. RepositoryIntelligenceEngine's "
            "downstream worker), never inside RepositoryCognitionService.index()."
        )
