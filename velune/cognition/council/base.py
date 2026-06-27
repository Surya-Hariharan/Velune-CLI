"""Abstract base class for Reasoning Council agents with specialized prompts."""

from __future__ import annotations

from abc import ABC
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
    ) -> None:
        self.role = role
        self.model = model
        self.provider = provider
        self.system_prompt = system_prompt
        self.live_lock = live_lock
        # Ordered list of (provider, model) pairs tried in sequence on primary failure.
        self._fallback_providers: list[tuple[ModelProvider, ModelDescriptor]] = (
            fallback_providers or []
        )
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
    ) -> str:
        """Runs the deliberation round using the assigned LLM model provider.

        When ``temperature``/``top_p``/``max_tokens`` are left as ``None`` they are
        resolved from this role's :class:`RoleSamplingProfile`, so each council
        seat samples with its own named profile instead of a shared literal.
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
                    from rich.markdown import Markdown
                    from rich.panel import Panel

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
                                            Markdown(current_text),
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

                elapsed = time.perf_counter() - start
                logger.info(
                    "Agent %s completed in %.1fs (%d chars)", self.role.value, elapsed, len(content)
                )
                if elapsed > 60.0:
                    logger.warning("Agent %s took %.1fs (>60s)", self.role.value, elapsed)
                return content
            except TimeoutError:
                logger.error("Agent %s timed out after %.0fs", self.role.value, timeout)
                return f"[Agent {self.role.value} timed out — using empty response]"
            except Exception as e:
                logger.error("deliberation failed for agent %s: %s", self.role.value, e)
                # Attempt fallback providers before giving up.
                for fb_provider, fb_model in self._fallback_providers:
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
                        logger.info(
                            "Agent %s fallback succeeded via %s",
                            self.role.value,
                            fb_model.provider_id,
                        )
                        return fb_response.content
                    except Exception as fb_exc:
                        logger.warning(
                            "Fallback provider %s also failed: %s",
                            fb_model.provider_id,
                            fb_exc,
                        )
                return f"Deliberation failure inside agent {self.role.value}: {e}"

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

        raw = await self.deliberate(context_history, temperature, max_tokens, top_p)
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
            try:
                return response_type.model_construct(parse_error=str(e))
            except Exception:
                raise
