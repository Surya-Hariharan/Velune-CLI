"""Abstract base class for Reasoning Council agents with specialized prompts."""

from __future__ import annotations

from abc import ABC
from collections.abc import Callable
from typing import TypeVar

from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)

from velune.core.trace import TracedLogger
from velune.core.types.inference import InferenceRequest
from velune.core.types.model import ModelDescriptor
from velune.models.specializations import CouncilRole
from velune.providers.base import ModelProvider

logger = TracedLogger("velune.cognition.council.base")

import asyncio

# Text a non-strict ``deliberate`` returns in place of a real answer when the
# call fails. Anything that consumes agent text must treat these as failures.
AGENT_FAILURE_PREFIXES = ("Deliberation failure inside agent", "[Agent ")


def is_failure_text(text: str | None) -> bool:
    """True for empty output or one of the failure sentinels above."""
    return not text or not text.strip() or text.startswith(AGENT_FAILURE_PREFIXES)


class CouncilAgentError(Exception):
    """A council seat produced no usable output (raised by ``deliberate(strict=True)``).

    ``kind`` is one of ``timeout``, ``provider``, ``auth`` or ``empty``.
    """

    def __init__(self, kind: str, role: str, detail: str, seat: str | None = None) -> None:
        super().__init__(f"{seat or role}: {kind}: {detail}")
        self.kind = kind
        self.role = role
        self.seat = seat or role
        self.detail = detail


class BaseCouncilAgent(ABC):
    """Base interface for specialized deliberation models within the Reasoning Council."""

    def __init__(
        self,
        role: CouncilRole,
        model: ModelDescriptor,
        provider: ModelProvider,
        system_prompt: str,
        live_lock: asyncio.Lock | None = None,
        fallback_providers: list[tuple[ModelProvider, ModelDescriptor]] | None = None,
        seat_name: str | None = None,
    ) -> None:
        self.role = role
        self.model = model
        self.provider = provider
        self.system_prompt = system_prompt
        self.live_lock = live_lock
        # The council *seat* this agent occupies, which is not the same thing
        # as its CouncilRole: the security, performance, and maintainability
        # critics are all built on the REVIEWER role descriptor, so role
        # identity alone cannot tell their provider calls apart in a trace.
        # Defaults to the role for the five agents where the two coincide.
        self.seat_name = seat_name or role.value
        # Ordered list of (provider, model) pairs tried in sequence on primary failure.
        self._fallback_providers: list[tuple[ModelProvider, ModelDescriptor]] = (
            fallback_providers or []
        )
        # Called as (seat, from_model, to_model) when a fallback answers; the
        # orchestrator points it at the progress stream.
        self.on_fallback: Callable[[str, str, str], None] | None = None
        # Cache manager — one per agent instance so fingerprint history is
        # preserved across consecutive deliberations within the same run.
        from velune.context.cache.manager import make_cache_manager

        self._cache_manager = make_cache_manager(provider.provider_id)

    async def deliberate(
        self,
        context_history: list[dict[str, str]],
        temperature: float | None = None,
        max_tokens: int | None = None,
        top_p: float | None = None,
        strict: bool = False,
    ) -> str:
        """Runs the deliberation round using the assigned LLM model provider.

        When ``temperature``/``top_p``/``max_tokens`` are left as ``None`` they are
        resolved from this role's :class:`RoleSamplingProfile`, so each council
        seat samples with its own named profile instead of a shared literal.

        With ``strict=False`` (legacy) a failure is returned as a sentinel
        string; with ``strict=True`` a timeout, an exhausted provider error or
        an empty answer raises :class:`CouncilAgentError` so it can never be
        mistaken for the seat's answer.
        """
        import asyncio

        from velune.cognition.council.sampling import get_sampling_profile
        from velune.core.trace import TraceContext, _run_id

        profile = get_sampling_profile(self.role)
        if temperature is None:
            temperature = profile.temperature
        if top_p is None:
            top_p = profile.top_p
        if max_tokens is None:
            max_tokens = profile.max_tokens

        with TraceContext(
            run_id=_run_id.get() or "unknown",
            agent_id=self.role.value,
        ):
            from velune.cognition.firewall import WORKSPACE_SANDBOX_NOTICE, CognitiveFirewall

            system_content = self.system_prompt + "\n\n" + WORKSPACE_SANDBOX_NOTICE
            messages = [{"role": "system", "content": system_content}] + context_history
            firewall = CognitiveFirewall()
            if not firewall.scan_conversation(messages):
                logger.error("Prompt injection detected in Council message history")
                raise ValueError("Security: Potential prompt injection detected in messages")

            request = InferenceRequest(
                model_id=self.model.model_id,
                messages=messages,
                temperature=temperature,
                max_tokens=max_tokens,
                top_p=top_p,
            )

            # Annotate request with cache hints for providers that support caching.
            request = self._cache_manager.prepare(request)

            agent_timeouts = {
                CouncilRole.PLANNER: 120.0,
                CouncilRole.CODER: 180.0,
                CouncilRole.REVIEWER: 120.0,
                CouncilRole.CHALLENGER: 90.0,
                CouncilRole.SYNTHESIZER: 120.0,
            }

            timeout = agent_timeouts.get(self.role, 120.0)

            import time

            from velune.cognition.execution_trace import (
                current_reason,
                current_trace,
            )

            # Open a ledger entry for the physical provider call this method is
            # about to make, attributed to the seat (not the role) and carrying
            # the reason the orchestrator established for this scope. Purely
            # structural: no prompt text, no completion, no credentials.
            _trace = current_trace()
            _call = None
            if _trace is not None:
                _call = _trace.open_call(
                    seat=self.seat_name,
                    component=type(self).__name__,
                    provider=self.provider.provider_id,
                    model=self.model.model_id,
                    purpose=f"{self.seat_name}.deliberate",
                    reason=current_reason(),
                )

            start = time.perf_counter()
            try:
                logger.info(
                    "Agent %s (%s) initiating inference...", self.role.value, self.model.model_id
                )

                supports_streaming = False
                try:
                    capabilities = self.provider.get_capabilities()
                    supports_streaming = getattr(capabilities, "supports_streaming", False)
                except Exception:
                    pass

                if not hasattr(self.provider, "stream"):
                    supports_streaming = False

                if supports_streaming:
                    import sys

                    from rich.console import Console
                    from rich.live import Live
                    from rich.panel import Panel

                    from velune.cli.rendering import CustomMarkdown

                    console = Console()
                    is_interactive = sys.stdout.isatty()

                    acquired = False
                    try:
                        if is_interactive and self.live_lock:
                            if not self.live_lock.locked():
                                await asyncio.shield(self.live_lock.acquire())
                                acquired = True

                        async def run_streaming():
                            full_content = []
                            role_name = self.role.value.capitalize()

                            agent_colors = {
                                CouncilRole.PLANNER: "magenta",
                                CouncilRole.CODER: "green",
                                CouncilRole.REVIEWER: "yellow",
                                CouncilRole.CHALLENGER: "red",
                                CouncilRole.SYNTHESIZER: "cyan",
                            }
                            color = agent_colors.get(self.role, "cyan")
                            panel_title = f"[bold {color}]{role_name} Agent Deliberating...[/bold {color}] ([dim]{self.model.model_id}[/dim])"

                            if acquired:
                                panel = Panel(
                                    "",
                                    title=panel_title,
                                    border_style=color,
                                    padding=(1, 2),
                                    subtitle="[dim]Streaming response...[/dim]",
                                    subtitle_align="right",
                                )
                                with Live(
                                    panel, console=console, refresh_per_second=10, transient=False
                                ) as live:
                                    async for chunk in self.provider.stream(request):
                                        full_content.append(chunk.content)
                                        current_text = "".join(full_content)
                                        panel = Panel(
                                            CustomMarkdown(current_text, streaming=True),
                                            title=panel_title,
                                            border_style=color,
                                            padding=(1, 2),
                                            subtitle=f"[dim]Streaming: {len(current_text)} chars[/dim]",
                                            subtitle_align="right",
                                        )
                                        live.update(panel)
                            else:
                                async for chunk in self.provider.stream(request):
                                    full_content.append(chunk.content)

                            return "".join(full_content)

                        content = await asyncio.wait_for(
                            run_streaming(),
                            timeout=timeout,
                        )
                    finally:
                        if acquired and self.live_lock:
                            self.live_lock.release()
                else:
                    response = await asyncio.wait_for(
                        self.provider.infer(request),
                        timeout=timeout,
                    )
                    self._cache_manager.record(response.metadata)
                    content = response.content

                if strict and not (content or "").strip():
                    raise CouncilAgentError(
                        "empty", self.role.value, "the model returned no text", self.seat_name
                    )

                elapsed = time.perf_counter() - start
                logger.info(
                    "Agent %s completed in %.1fs (%d chars)", self.role.value, elapsed, len(content)
                )
                if elapsed > 60.0:
                    logger.warning("Agent %s took %.1fs (>60s)", self.role.value, elapsed)
                if _call is not None:
                    _call.finish("ok")
                return content
            except TimeoutError:
                logger.error("Agent %s timed out after %.0fs", self.role.value, timeout)
                if _call is not None:
                    _call.finish("timeout", error_type="TimeoutError")
                fallback = await self._try_fallbacks(
                    messages, temperature, max_tokens, top_p, timeout, _trace, _call
                )
                if fallback is not None:
                    return fallback
                if strict:
                    raise CouncilAgentError(
                        "timeout",
                        self.role.value,
                        f"timed out after {timeout:.0f}s",
                        self.seat_name,
                    ) from None
                return f"[Agent {self.role.value} timed out — using empty response]"
            except CouncilAgentError:
                if _call is not None:
                    _call.finish("error", error_type="CouncilAgentError")
                raise
            except Exception as e:
                if _call is not None:
                    _call.finish("error", error_type=type(e).__name__)
                self._note_provider_failure(self.provider.provider_id, e)
                logger.error("deliberation failed for agent %s: %s", self.role.value, str(e)[:300])
                # Attempt fallback providers before giving up.
                fallback = await self._try_fallbacks(
                    messages, temperature, max_tokens, top_p, timeout, _trace, _call
                )
                if fallback is not None:
                    return fallback
                if strict:
                    from velune.core.errors.provider import ProviderAuthenticationError

                    kind = "auth" if isinstance(e, ProviderAuthenticationError) else "provider"
                    raise CouncilAgentError(
                        kind, self.role.value, str(e)[:300], self.seat_name
                    ) from e
                return f"Deliberation failure inside agent {self.role.value}: {e}"

    async def _try_fallbacks(
        self,
        messages: list[dict[str, str]],
        temperature: float,
        max_tokens: int | None,
        top_p: float,
        timeout: float,
        trace,
        primary_call,
    ) -> str | None:
        """Try this seat's fallback (provider, model) pairs in order; the first usable answer wins.

        The chain is fixed when the agent is built (see
        ``ModelSpecializationMapper.fallback_chain``), so the order is deterministic.
        Returns ``None`` when there is no fallback or every alternate failed.
        """
        from velune.cognition.execution_trace import CallReason

        for fb_index, (fb_provider, fb_model) in enumerate(self._fallback_providers, start=1):
            fb_call = None
            if trace is not None:
                fb_call = trace.open_call(
                    seat=self.seat_name,
                    component=type(self).__name__,
                    provider=fb_model.provider_id,
                    model=fb_model.model_id,
                    purpose=f"{self.seat_name}.deliberate",
                    reason=CallReason.FALLBACK,
                    attempt=fb_index + 1,
                    streaming=False,
                    parent_call_id=primary_call.call_id if primary_call else None,
                )
            try:
                logger.info(
                    "Agent %s retrying with fallback provider %s/%s",
                    self.role.value,
                    fb_model.provider_id,
                    fb_model.model_id,
                )
                fb_request = InferenceRequest(
                    model_id=fb_model.model_id,
                    messages=messages,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    top_p=top_p,
                )
                fb_response = await asyncio.wait_for(
                    fb_provider.infer(fb_request),
                    timeout=timeout,
                )
                if not (fb_response.content or "").strip():
                    raise ValueError("the fallback model returned no text")
                logger.info(
                    "Agent %s fallback succeeded via %s", self.role.value, fb_model.provider_id
                )
                if fb_call is not None:
                    fb_call.finish("ok")
                if self.on_fallback is not None:
                    self.on_fallback(
                        self.seat_name,
                        f"{self.provider.provider_id}/{self.model.model_id}",
                        f"{fb_model.provider_id}/{fb_model.model_id}",
                    )
                return fb_response.content
            except Exception as fb_exc:
                if fb_call is not None:
                    fb_call.finish("error", error_type=type(fb_exc).__name__)
                self._note_provider_failure(fb_model.provider_id, fb_exc)
                logger.warning(
                    "Fallback provider %s also failed: %s",
                    fb_model.provider_id,
                    str(fb_exc)[:300],
                )
        return None

    @staticmethod
    def _note_provider_failure(provider_id: str, exc: Exception) -> None:
        """Persist a definitive key rejection so the next run fails fast at preflight.

        Only :class:`ProviderAuthenticationError` qualifies — it is raised solely
        for 401-style verdicts, which say something about the key itself. Network
        failures and rate limits must not poison the stored key state.
        """
        from velune.core.errors.provider import ProviderAuthenticationError

        if not isinstance(exc, ProviderAuthenticationError):
            return
        try:
            from velune.providers import keystore

            keystore.mark_invalid(provider_id, reason=str(exc))
        except Exception:  # noqa: BLE001 — key-state bookkeeping must never break a run
            pass

    async def typed_deliberate(
        self,
        context_history: list[dict[str, str]],
        response_type: type[T],
        temperature: float | None = None,
        max_tokens: int | None = None,
        top_p: float | None = None,
    ) -> T:
        """Runs deliberation and parses the output into a strongly-typed Pydantic model."""
        from pydantic import ValidationError

        degrade = getattr(response_type, "degraded", None)
        try:
            raw = await self.deliberate(
                context_history, temperature, max_tokens, top_p, strict=True
            )
        except CouncilAgentError as exc:
            # The seat abstains. Never substitute default field values: those
            # used to read as "passed" and counted as an approval.
            logger.error("Agent %s unavailable (%s): %s", self.role.value, exc.kind, exc.detail)
            if degrade is None:
                raise
            return degrade("unavailable", f"{exc.kind}: {exc.detail}")
        cleaned = raw.strip()
        for prefix in ("```json", "```"):
            if cleaned.startswith(prefix):
                cleaned = cleaned[len(prefix) :]
        if cleaned.endswith("```"):
            cleaned = cleaned[:-3]
        cleaned = cleaned.strip()

        try:
            return response_type.model_validate_json(cleaned)
        except (ValidationError, Exception) as e:
            logger.error(
                "Agent %s returned unparseable response: %s\nRaw: %s", self.role.value, e, raw[:200]
            )
            if degrade is not None:
                return degrade("unparseable", str(e))
            return response_type.model_construct(parse_error=str(e))
