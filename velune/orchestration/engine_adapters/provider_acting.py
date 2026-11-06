"""``ActingInvoker`` over ``ModelProvider.infer()``: provider output in, *proposals* out.

The model is an untrusted proposal generator. This adapter turns one provider response into an
:class:`ActingResult` and does nothing else:

* It never executes a tool, never approves a call, never touches an approval ledger, an artifact store
  or driver state. It holds no executor, no registry of live tools and no permission object.
* Tool definitions come only from the trusted catalog (``ToolDescriptor``) and only for the names in
  ``request.offered_tools``. Names are used exactly as given: nothing is renamed or sanitized, and a
  tool the adapter cannot describe strictly is refused, not offered with fewer constraints.
* Provider function calling is only a proposal *format*. A tool call in the response is checked
  (offered name, no ``_raw_arguments``, trusted schema) and handed on as a ``ProposedToolCall``; the
  runner then validates it again and the trusted executor still decides whether it runs.
* Nothing is repaired, coerced or guessed. Malformed arguments, a bad or un-offered name, more than
  one call, an empty answer, a refusal and a truncated answer are all failures. Text that accompanies
  a call is dropped.
* Failures carry a stable kind and a fixed message. Provider exception text, response text and
  argument values are never copied into a failure.

Completeness. ``InferenceResponse.finish_reason`` is overwritten with ``"tool_calls"`` by the
adapters whenever a call is present, so a call cut off by the token limit looks complete. A tool
proposal is therefore accepted only when the response *also* reports the provider's native stop
reason in ``metadata["stop_reason"]`` and that reason says the model finished. No provider adapter
sets that key yet, so against today's real providers tool proposals are refused (fail closed); a text
answer is judged by ``finish_reason`` alone. Providing the key is a provider-layer change that is
deliberately not made here.

Reliability (Phase 4E-3). This class is the **single retry owner** for acting-provider calls:

* A provider obtained through the registry arrives wrapped in ``RetryingProvider``; that layer is
  stripped here so retries are never nested (3 x 3). Normal chat is unaffected: the registry and the
  wrapper are not changed, and an acting invoker simply holds the inner provider.
* The wrapper's *concurrency semaphore* is kept (``split_retry_layer``): each request waits for a
  permit on the same provider-wide gate the wrapper uses, so the limit holds across acting runs and
  other users of that wrapper. A provider that arrives unwrapped gets one weakly-keyed shared gate
  (default 4). A permit is held only around a request, never across a retry delay, and is released on
  success, error, deadline and cancellation. Waiting for a permit counts against the call's deadline.
* Provider attempts are *reserved* on the run's budget immediately before each request, after the
  permit is held (``request.attempt_gate``). A denied reservation sends nothing. A reservation is
  never refunded: ``provider_attempts`` counts reserved/started attempts.
* Retryable: rate limits, provider-side timeouts, connection failures and the generic transient
  ``InferenceError`` bucket (5xx). A model call has no side effects until a response is accepted, so
  repeating it is safe. Not retryable: cancellation, authentication, unknown model or bad request,
  any malformed, truncated, refused or otherwise invalid *response*, and the adapter's own deadline.
* Attempts are bounded (``ProviderRetryPolicy.max_attempts``), every delay is bounded, and the whole
  call, retries included, shares one deadline (``request.timeout_s``). Delays are deterministic.
* Never retried once a tool call has started in the run (``request.provider_retry_allowed`` is False),
  and this class has no fallback to another provider or model at all.
* Sizes are measured in **characters**, not tokens: ``max_request_chars`` (messages plus the tool
  definitions' JSON) is checked before any request is sent and ``max_response_chars`` (text plus
  argument JSON) before a response is interpreted. Nothing is truncated; over the limit is a failure.
* Secrets: provider-bound non-system text is scrubbed; a system prompt that would be changed by the
  scrubber is refused instead of edited; model text is scrubbed; a proposal whose arguments contain a
  secret is rejected, never rewritten. Failures and retry diagnostics carry kinds and numbers only.

Not here, on purpose: streaming, token-based budgets, approval, CLI use.
"""

from __future__ import annotations

import asyncio
import copy
import json
import math
import re
import sys
import time
import weakref
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from velune._compat import StrEnum
from velune.core.errors.provider import (
    InferenceError,
    ProviderAuthenticationError,
    ProviderConnectionError,
    ProviderTimeoutError,
    RateLimitError,
)
from velune.core.redaction import redact_secrets
from velune.core.types.inference import InferenceRequest, InferenceResponse

# ModelRef / SeatStatus come through the engine's own port module: the council core is reachable only
# via the engine (see tests/test_council_core_boundaries.py).
from velune.orchestration.engine.ports import ActingRequest, ActingResult, ModelRef, SeatStatus
from velune.orchestration.engine.proposals import ProposedToolCall
from velune.orchestration.engine.toolpolicy import (
    EXCLUDED_TOOLS,
    READ_ONLY_PERMISSIONS,
    READ_ONLY_TOOL_CEILING,
    ToolDescriptor,
)
from velune.orchestration.engine.toolschema import (
    ArgumentError,
    canonical_arguments,
    check_arguments,
    schema_problem,
)

if TYPE_CHECKING:
    from velune.providers.base import ModelProvider
    from velune.providers.registry import ProviderRegistry

STOP_REASON_KEY = "stop_reason"
RAW_ARGUMENTS_KEY = "_raw_arguments"
_PROVIDER_TOOL_NAME = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_MAX_DESCRIPTION_CHARS = 1000

# Normalised (lower-case) finish / stop reasons.
_TEXT_COMPLETE = frozenset({"stop", "end_turn", "complete"})
_TOOL_FINISH = "tool_calls"
_TOOL_STOP_COMPLETE = frozenset({"tool_use", "tool_calls", "function_call", "stop", "end_turn"})
_TRUNCATED = frozenset({"length", "max_tokens", "max_output_tokens", "model_length", "incomplete"})
_REFUSED = frozenset({"refusal", "content_filter", "safety", "blocked", "prohibited_content"})


class Rejection(StrEnum):
    """Stable, value-free reasons the adapter did not deliver a proposal or an answer."""

    TOOLS_MISMATCH = "offered_tools_mismatch"
    PROVIDER_ERROR = "provider_error"
    RATE_LIMITED = "rate_limited"
    AUTH_ERROR = "auth_error"
    TIMEOUT = "timeout"
    BAD_RESPONSE = "bad_response"
    REFUSAL = "refusal"
    EMPTY = "empty"
    TRUNCATED = "truncated"
    UNKNOWN_STOP = "unknown_stop_reason"
    NO_STOP_REASON = "no_stop_reason"
    INCONSISTENT = "inconsistent_response"
    MULTIPLE_CALLS = "multiple_tool_calls"
    BAD_TOOL_NAME = "bad_tool_name"
    BAD_ARGUMENTS = "bad_arguments"
    RAW_ARGUMENTS = "raw_arguments"
    BAD_CALL_ID = "bad_call_id"
    RESPONSE_TOO_LARGE = "response_too_large"
    REQUEST_TOO_LARGE = "request_too_large"
    SECRET_IN_ARGUMENTS = "secret_in_arguments"
    SECRET_IN_REQUEST = "secret_in_request"
    ATTEMPT_BUDGET = "attempt_budget_exhausted"


_FAILURE: Mapping[Rejection, tuple[SeatStatus, str]] = {
    Rejection.TOOLS_MISMATCH: (
        SeatStatus.BLOCKED,
        "the offered tools do not match the trusted catalog",
    ),
    Rejection.PROVIDER_ERROR: (SeatStatus.PROVIDER_ERROR, "the provider call failed"),
    Rejection.RATE_LIMITED: (SeatStatus.PROVIDER_ERROR, "the provider rate-limited the call"),
    Rejection.AUTH_ERROR: (SeatStatus.AUTH_ERROR, "the provider rejected the credentials"),
    Rejection.TIMEOUT: (SeatStatus.TIMEOUT, "the provider call timed out"),
    Rejection.BAD_RESPONSE: (SeatStatus.UNPARSEABLE, "the provider response was not usable"),
    Rejection.REFUSAL: (SeatStatus.BLOCKED, "the provider refused to answer"),
    Rejection.EMPTY: (SeatStatus.EMPTY, "the provider returned nothing usable"),
    Rejection.TRUNCATED: (SeatStatus.UNPARSEABLE, "the provider response was cut off"),
    Rejection.UNKNOWN_STOP: (SeatStatus.UNPARSEABLE, "the provider stop reason is not recognised"),
    Rejection.NO_STOP_REASON: (
        SeatStatus.UNPARSEABLE,
        "the provider did not report why it stopped, so completeness is unproven",
    ),
    Rejection.INCONSISTENT: (SeatStatus.UNPARSEABLE, "the provider response contradicts itself"),
    Rejection.MULTIPLE_CALLS: (SeatStatus.UNPARSEABLE, "more than one tool call was proposed"),
    Rejection.BAD_TOOL_NAME: (
        SeatStatus.UNPARSEABLE,
        "the proposed tool is not one that was offered",
    ),
    Rejection.BAD_ARGUMENTS: (SeatStatus.UNPARSEABLE, "the proposed tool arguments are invalid"),
    Rejection.RAW_ARGUMENTS: (
        SeatStatus.UNPARSEABLE,
        "the proposed tool arguments were not valid JSON",
    ),
    Rejection.BAD_CALL_ID: (SeatStatus.UNPARSEABLE, "the proposed tool call has no usable id"),
    Rejection.RESPONSE_TOO_LARGE: (
        SeatStatus.UNPARSEABLE,
        "the provider response exceeds the size limit",
    ),
    Rejection.REQUEST_TOO_LARGE: (
        SeatStatus.BLOCKED,
        "the request exceeds the size limit and was not sent",
    ),
    Rejection.SECRET_IN_ARGUMENTS: (
        SeatStatus.UNPARSEABLE,
        "the proposed tool arguments contain secret material",
    ),
    Rejection.SECRET_IN_REQUEST: (
        SeatStatus.BLOCKED,
        "the trusted instructions contain secret material and were not sent",
    ),
    Rejection.ATTEMPT_BUDGET: (
        SeatStatus.BLOCKED,
        "the provider-attempt budget is exhausted and the request was not sent",
    ),
}

DEFAULT_MAX_REQUEST_CHARS = 400_000
DEFAULT_MAX_RESPONSE_CHARS = 100_000
_REAP_GRACE_S = 1.0
Scrub = Callable[[str], str]


@dataclass(frozen=True)
class ProviderRetryPolicy:
    """Bounds for the one retry owner. ``max_attempts`` counts the first try. No jitter, so it is
    deterministic. A provider-supplied ``retry_after`` is honoured only when it fits ``max_delay_s``
    and the call's deadline; otherwise the call fails rather than waiting."""

    max_attempts: int = 3
    base_delay_s: float = 0.25
    max_delay_s: float = 4.0

    def __post_init__(self) -> None:
        if isinstance(self.max_attempts, bool) or not 1 <= self.max_attempts <= 5:
            raise ValueError("max_attempts must be between 1 and 5")
        for value in (self.base_delay_s, self.max_delay_s):
            if not isinstance(value, int | float) or not math.isfinite(value) or value < 0:
                raise ValueError("delays must be finite and non-negative")
        if self.max_delay_s < self.base_delay_s or self.max_delay_s > 60:
            raise ValueError("max_delay_s must be between base_delay_s and 60")

    def delay(self, attempt: int) -> float:
        return min(self.base_delay_s * (2 ** (attempt - 1)), self.max_delay_s)


NO_RETRY = ProviderRetryPolicy(max_attempts=1)


@dataclass(frozen=True)
class RetryEvent:
    """A retry diagnostic: a count, a stable kind and a delay. Never an exception or response text."""

    attempt: int
    reason: Rejection
    delay_s: float


def split_retry_layer(provider: Any) -> tuple[Any, asyncio.Semaphore | None]:
    """``(inner provider, the wrapper's concurrency semaphore)``.

    Strips the registry's ``RetryingProvider`` wrapper(s), so this adapter is the only retry owner,
    but hands back the wrapper's own provider-wide semaphore (the outermost one found) so the
    concurrency limit it enforced is kept: every acting run, and every other user of the same wrapper
    (normal chat, Council), then shares one limit. Detected without importing provider code: a wrapped
    provider implies its module is already loaded.
    """
    module = sys.modules.get("velune.providers.retrying")
    layer = getattr(module, "RetryingProvider", None)
    gate: asyncio.Semaphore | None = None
    for _ in range(8):
        if layer is None or not isinstance(provider, layer):
            break
        found = getattr(provider, "_concurrency_gate", None)
        if gate is None and isinstance(found, asyncio.Semaphore):
            gate = found
        provider = provider._inner
    return provider, gate


def unwrap_retry_layer(provider: Any) -> Any:
    """The provider with the registry's ``RetryingProvider`` wrapper(s) removed."""
    return split_retry_layer(provider)[0]


# One semaphore per provider instance that arrives without a wrapper (so there is none to reuse).
# Weakly keyed: it never keeps a provider alive. Same default limit as ``RetryingProvider``.
DEFAULT_PROVIDER_CONCURRENCY = 4
_SHARED_GATES: weakref.WeakKeyDictionary[Any, asyncio.Semaphore] = weakref.WeakKeyDictionary()


def _shared_gate(provider: Any) -> asyncio.Semaphore:
    try:
        gate = _SHARED_GATES.get(provider)
        if gate is None:
            gate = _SHARED_GATES[provider] = asyncio.Semaphore(DEFAULT_PROVIDER_CONCURRENCY)
        return gate
    except TypeError:  # not weak-referenceable / hashable: limit only this invoker
        return asyncio.Semaphore(DEFAULT_PROVIDER_CONCURRENCY)


def classify_provider_error(exc: BaseException) -> tuple[Rejection, bool]:
    """``(failure kind, retryable)`` for an exception raised by ``provider.infer``."""
    if isinstance(exc, ProviderAuthenticationError):
        return Rejection.AUTH_ERROR, False
    if isinstance(exc, RateLimitError):
        return Rejection.RATE_LIMITED, True
    if isinstance(exc, ProviderTimeoutError | TimeoutError):
        return Rejection.TIMEOUT, True
    if isinstance(exc, InferenceError | ProviderConnectionError):
        return Rejection.PROVIDER_ERROR, True
    return Rejection.PROVIDER_ERROR, False  # unknown model, bad request, anything unrecognised


def fixed_failure_messages() -> frozenset[str]:
    """Every message this adapter can put in a failure (for tests and audits)."""
    return frozenset(message for _, message in _FAILURE.values())


def _failure(kind: Rejection, model: ModelRef | None, attempts: int = 1) -> ActingResult:
    status, message = _FAILURE[kind]
    extra: dict[str, Any] = {"attempts": attempts}
    if model is not None:
        extra["model"] = model
    return ActingResult.failure(status, message, **extra)


# ── trusted tool definitions ──────────────────────────────────────────────────


def tool_definition(descriptor: ToolDescriptor) -> dict[str, Any] | None:
    """The provider tool definition for one trusted descriptor, or ``None`` if it cannot be offered.

    ``None`` means *do not offer anything*: the name is outside the read-only ceiling, the scopes are
    not read-only, the name is not representable exactly as given, or the schema cannot be enforced
    strictly by the trusted validator. The schema is copied and closed with
    ``additionalProperties: false``; no constraint is dropped.
    """
    name = descriptor.name
    if (
        name not in READ_ONLY_TOOL_CEILING
        or name in EXCLUDED_TOOLS
        or not _PROVIDER_TOOL_NAME.match(name)
        or not descriptor.permissions
        or not descriptor.permissions <= READ_ONLY_PERMISSIONS
        or schema_problem(descriptor.schema_) is not None
    ):
        return None
    parameters = copy.deepcopy(descriptor.schema_)
    parameters["additionalProperties"] = False
    description = parameters.get("description")
    if not isinstance(description, str) or not 0 < len(description) <= _MAX_DESCRIPTION_CHARS:
        description = f"Read-only tool {name}."
    return {
        "type": "function",
        "function": {"name": name, "description": description, "parameters": parameters},
    }


# ── response interpretation (pure) ────────────────────────────────────────────


def interpret_response(
    response: Any,
    *,
    offered: Mapping[str, ToolDescriptor],
    model: ModelRef | None = None,
    scrub: Scrub = redact_secrets,
    max_response_chars: int = DEFAULT_MAX_RESPONSE_CHARS,
    attempts: int = 1,
) -> ActingResult:
    """Turn one provider response into an :class:`ActingResult`. Never raises, never executes.

    ``offered`` maps each name that was offered on this request to its trusted descriptor.
    """
    try:
        return _interpret(response, offered, model, scrub, max_response_chars, attempts)
    except Exception:  # a response of an unexpected shape is a bad response, not a crash
        return _failure(Rejection.BAD_RESPONSE, model, attempts)


def _norm(value: Any) -> str | None:
    return value.strip().lower() if isinstance(value, str) and value.strip() else None


def _response_chars(response: InferenceResponse) -> int:
    total = len(response.content) if isinstance(response.content, str) else 0
    for call in response.tool_calls or []:
        total += len(str(call.name)) + len(json.dumps(call.arguments, default=str))
    return total


def _interpret(
    response: Any,
    offered: Mapping[str, ToolDescriptor],
    model: ModelRef | None,
    scrub: Scrub,
    max_response_chars: int,
    attempts: int,
) -> ActingResult:
    def failure(kind: Rejection) -> ActingResult:
        return _failure(kind, model, attempts)

    if not isinstance(response, InferenceResponse):
        return failure(Rejection.BAD_RESPONSE)
    if _response_chars(response) > max_response_chars:
        return failure(Rejection.RESPONSE_TOO_LARGE)

    finish = _norm(response.finish_reason)
    metadata = response.metadata if isinstance(response.metadata, dict) else {}
    stop = _norm(metadata.get(STOP_REASON_KEY))
    calls = list(response.tool_calls or [])

    # A refusal or a cut-off answer is never usable, whichever field says so.
    reasons = {r for r in (finish, stop) if r is not None}
    if reasons & _REFUSED:
        return failure(Rejection.REFUSAL)
    if reasons & _TRUNCATED:
        return failure(Rejection.TRUNCATED)

    if not calls:
        if finish is None or finish not in _TEXT_COMPLETE:
            return failure(
                Rejection.INCONSISTENT if finish == _TOOL_FINISH else Rejection.UNKNOWN_STOP
            )
        if stop is not None and stop not in _TEXT_COMPLETE:
            return failure(Rejection.UNKNOWN_STOP)
        text = scrub(response.content) if isinstance(response.content, str) else ""
        if not text.strip():
            return failure(Rejection.EMPTY)
        return ActingResult.success(text=text, **_with(model, attempts))

    # A tool proposal. Completeness must be positively shown, not assumed.
    if finish != _TOOL_FINISH:
        return failure(Rejection.INCONSISTENT)
    if stop is None:
        return failure(Rejection.NO_STOP_REASON)
    if stop not in _TOOL_STOP_COMPLETE:
        return failure(Rejection.UNKNOWN_STOP)
    if len(calls) > 1:
        return failure(Rejection.MULTIPLE_CALLS)

    call = calls[0]
    name = call.name
    descriptor = offered.get(name) if isinstance(name, str) else None
    if descriptor is None:
        return failure(Rejection.BAD_TOOL_NAME)
    arguments = call.arguments
    if not isinstance(arguments, dict):
        return failure(Rejection.BAD_ARGUMENTS)
    if _contains_key(arguments, RAW_ARGUMENTS_KEY):
        return failure(Rejection.RAW_ARGUMENTS)
    try:
        canonical = canonical_arguments(arguments)
        if scrub(canonical) != canonical:
            return failure(Rejection.SECRET_IN_ARGUMENTS)
        clean = json.loads(canonical)
        check_arguments(descriptor.schema_, clean)
    except (ArgumentError, ValueError, TypeError):
        return failure(Rejection.BAD_ARGUMENTS)
    try:
        # Only these three fields are taken. Text that came with the call is not used anywhere.
        proposal = ProposedToolCall(call_id=call.id, name=name, arguments=clean)
    except ValueError:
        return failure(Rejection.BAD_CALL_ID)
    return ActingResult.success(proposals=(proposal,), **_with(model, attempts))


def _with(model: ModelRef | None, attempts: int = 1) -> dict[str, Any]:
    extra: dict[str, Any] = {"attempts": attempts}
    if model is not None:
        extra["model"] = model
    return extra


def _contains_key(value: Any, key: str) -> bool:
    if isinstance(value, dict):
        return key in value or any(_contains_key(v, key) for v in value.values())
    if isinstance(value, list):
        return any(_contains_key(v, key) for v in value)
    return False


# ── the invoker ───────────────────────────────────────────────────────────────


class _Deadline(Exception):
    """The call's deadline passed while a provider request was in flight."""


class _AttemptDenied(Exception):
    """The run's provider-attempt budget refused a reservation: no request was sent."""


class _CallState:
    """Attempts actually reserved/started during one ``propose``. Touched only on the event loop."""

    def __init__(self) -> None:
        self.attempts = 0


class ProviderActingInvoker:
    """``ActingInvoker`` over a :class:`ModelProvider`.

    ``catalog`` is a trusted snapshot of ``ToolDescriptor``s (copied here, so later changes cannot widen
    what is offered). ``model_id`` is trusted configuration chosen by the caller, never by the model.
    This class is the one retry owner (see the module docstring) and has no fallback.

    ``clock`` and ``sleep`` are injectable so retry timing is deterministic in tests.
    """

    def __init__(
        self,
        provider: ModelProvider,
        model_id: str,
        catalog: Mapping[str, ToolDescriptor],
        *,
        temperature: float = 0.0,
        retry: ProviderRetryPolicy | None = None,
        scrub: Scrub | None = None,
        max_request_chars: int = DEFAULT_MAX_REQUEST_CHARS,
        max_response_chars: int = DEFAULT_MAX_RESPONSE_CHARS,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        if not isinstance(model_id, str) or not model_id.strip():
            raise ValueError("model_id must be a non-empty string")
        for limit in (max_request_chars, max_response_chars):
            if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
                raise ValueError("size limits must be positive integers")
        self._provider, wrapper_gate = split_retry_layer(provider)
        # Provider-wide concurrency: the wrapper's semaphore when there was one, else one shared per
        # provider instance. Held only around a request, never across a retry delay.
        self._gate = wrapper_gate if wrapper_gate is not None else _shared_gate(self._provider)
        self._model_id = model_id
        self._catalog = {name: copy.deepcopy(d) for name, d in catalog.items()}
        self._temperature = temperature
        self._retry = retry if retry is not None else ProviderRetryPolicy()
        self._scrub: Scrub = scrub if scrub is not None else redact_secrets
        self._max_request_chars = max_request_chars
        self._max_response_chars = max_response_chars
        self._clock = clock
        self._sleep = sleep
        self._events: list[RetryEvent] = []

    @classmethod
    def from_registry(
        cls,
        registry: ProviderRegistry,
        provider_id: str,
        model_id: str,
        catalog: Mapping[str, ToolDescriptor],
        **kw: Any,
    ) -> ProviderActingInvoker:
        """Look the provider up in the existing registry. Its retry wrapper is removed (see above)."""
        return cls(registry.get_or_raise(provider_id), model_id, catalog, **kw)

    @property
    def retry_events(self) -> tuple[RetryEvent, ...]:
        """Content-free diagnostics of the retries made so far (most recent 100)."""
        return tuple(self._events)

    def _model_ref(self) -> ModelRef | None:
        try:
            provider_id = str(self._provider.provider_id)[:64]
            return ModelRef(provider_id=provider_id, model_id=self._model_id[:128])
        except Exception:
            return None

    def _definitions(
        self, offered: tuple[str, ...]
    ) -> tuple[list[dict[str, Any]], dict[str, ToolDescriptor]] | None:
        if len(set(offered)) != len(offered):
            return None
        definitions: list[dict[str, Any]] = []
        chosen: dict[str, ToolDescriptor] = {}
        for name in offered:
            descriptor = self._catalog.get(name)
            if descriptor is None or descriptor.name != name:
                return None
            definition = tool_definition(descriptor)
            if definition is None:
                return None
            definitions.append(definition)
            chosen[name] = descriptor
        return definitions, chosen

    def _outgoing(self, request: ActingRequest) -> list[dict[str, Any]] | None:
        """Provider-bound messages, or ``None`` if the trusted instructions would need editing.

        Non-system text (the task and framed tool output) is scrubbed. A system prompt that the
        scrubber would change is refused outright: trusted instructions are never silently edited.
        """
        messages: list[dict[str, Any]] = []
        try:
            for m in request.messages:
                content = m.content
                cleaned = self._scrub(content)
                if m.role == "system" and cleaned != content:
                    return None
                messages.append({"role": m.role, "content": cleaned})
        except Exception:
            return None
        return messages

    def _note(self, attempt: int, reason: Rejection, delay: float) -> None:
        self._events.append(RetryEvent(attempt, reason, round(delay, 3)))
        del self._events[:-100]

    async def _guarded(
        self,
        inference: InferenceRequest,
        reserve: Callable[[], bool] | None,
        state: _CallState,
    ) -> Any:
        """Wait for a provider-wide permit, reserve ONE attempt, then send. The permit is released
        on success, exception, timeout and cancellation (``async with``)."""
        async with self._gate:
            if reserve is not None and not reserve():
                raise _AttemptDenied  # nothing is sent
            state.attempts += 1  # reserved = started, whatever happens next
            return await self._provider.infer(inference)

    async def _attempt(
        self,
        inference: InferenceRequest,
        remaining: float,
        reserve: Callable[[], bool] | None,
        state: _CallState,
    ) -> Any:
        """One provider request, bounded by ``remaining`` seconds, never leaving a task behind.
        The wait for a permit counts against ``remaining``."""
        task = asyncio.ensure_future(self._guarded(inference, reserve, state))
        try:
            done, _ = await asyncio.wait({task}, timeout=remaining)
        except BaseException:  # includes CancelledError: stop the request, then let it propagate
            await self._reap(task)
            raise
        if not done:
            await self._reap(task)
            raise _Deadline
        return task.result()

    @staticmethod
    async def _reap(task: asyncio.Future[Any]) -> None:
        """Cancel a request and give it a short moment to unwind. A provider that ignores
        cancellation can still leave its task pending; nothing here can force it."""
        task.cancel()
        try:
            await asyncio.wait({task}, timeout=_REAP_GRACE_S)
        except asyncio.CancelledError:
            pass
        if task.done() and not task.cancelled():
            task.exception()  # mark retrieved

    async def propose(self, request: ActingRequest) -> ActingResult:
        """Ask the provider for the next move. Returns a typed result; raises only on cancellation."""
        model = self._model_ref()
        built = self._definitions(tuple(request.offered_tools))
        if built is None:
            return _failure(Rejection.TOOLS_MISMATCH, model, 0)  # the provider is not called
        definitions, chosen = built

        messages = self._outgoing(request)
        if messages is None:
            return _failure(Rejection.SECRET_IN_REQUEST, model, 0)
        size = sum(len(m["content"]) for m in messages) + len(json.dumps(definitions))
        if size > self._max_request_chars:
            return _failure(Rejection.REQUEST_TOO_LARGE, model, 0)  # never truncated, never sent

        inference = InferenceRequest(
            model_id=self._model_id,
            messages=messages,
            temperature=self._temperature,
            max_tokens=request.max_tokens,
            tools=definitions or None,
            tool_choice="auto" if definitions else None,
        )
        policy = self._retry
        max_attempts = policy.max_attempts if request.provider_retry_allowed else 1
        deadline = self._clock() + request.timeout_s
        gate = getattr(request, "attempt_gate", None)
        reserve = gate.reserve if callable(getattr(gate, "reserve", None)) else None
        state = _CallState()
        while True:
            remaining = deadline - self._clock()
            if remaining <= 0:
                return _failure(Rejection.TIMEOUT, model, state.attempts)
            try:
                response = await self._attempt(inference, remaining, reserve, state)
            except asyncio.CancelledError:
                raise
            except _AttemptDenied:
                return _failure(Rejection.ATTEMPT_BUDGET, model, state.attempts)
            except _Deadline:
                return _failure(Rejection.TIMEOUT, model, state.attempts)
            except Exception as exc:  # never echo it: its text may carry paths, bodies or secrets
                attempts = state.attempts
                kind, retryable = classify_provider_error(exc)
                delay = self._delay(exc, policy, attempts)
                if (
                    not retryable
                    or attempts >= max_attempts
                    or delay is None
                    or self._clock() + delay >= deadline
                ):
                    return _failure(kind, model, attempts)
                self._note(attempts, kind, delay)
                await self._sleep(delay)  # cancellation during the wait propagates
                continue
            return interpret_response(
                response,
                offered=chosen,
                model=model,
                scrub=self._scrub,
                max_response_chars=self._max_response_chars,
                attempts=state.attempts,
            )

    @staticmethod
    def _delay(exc: BaseException, policy: ProviderRetryPolicy, attempt: int) -> float | None:
        """The wait before the next try, or ``None`` if the provider asked for longer than allowed."""
        hint = getattr(exc, "retry_after", None)
        if isinstance(hint, int | float) and not isinstance(hint, bool) and math.isfinite(hint):
            return float(hint) if 0 <= hint <= policy.max_delay_s else None
        return policy.delay(attempt)


__all__ = [
    "DEFAULT_MAX_REQUEST_CHARS",
    "DEFAULT_MAX_RESPONSE_CHARS",
    "NO_RETRY",
    "RAW_ARGUMENTS_KEY",
    "STOP_REASON_KEY",
    "ProviderActingInvoker",
    "ProviderRetryPolicy",
    "Rejection",
    "RetryEvent",
    "classify_provider_error",
    "fixed_failure_messages",
    "interpret_response",
    "split_retry_layer",
    "tool_definition",
    "unwrap_retry_layer",
]
