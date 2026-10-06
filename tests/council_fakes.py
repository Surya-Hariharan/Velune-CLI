"""Test doubles for exercising the real CouncilOrchestrator without any model.

``FakeProvider`` answers each council seat from a responder function, so tests
can make one seat time out, raise, or return garbage while every other seat
behaves. ``make_orchestrator`` builds the real orchestrator around fakes.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

from velune.cognition.orchestrator import CouncilOrchestrator
from velune.core.types.inference import InferenceRequest, InferenceResponse
from velune.core.types.model import ModelCapabilityProfile, ModelDescriptor
from velune.models.specializations import CouncilRole

PLAN_JSON = json.dumps(
    {
        "task_id": "t1",
        "steps": [
            {
                "id": "s1",
                "description": "implement",
                "agent_role": "coder",
                "dependencies": [],
                "metadata": {},
            }
        ],
    }
)
REVIEW_OK = json.dumps(
    {"passed": True, "critical_issues": [], "suggestions": [], "confidence_rating": 0.9}
)
REVIEW_FAIL = json.dumps(
    {
        "passed": False,
        "critical_issues": ["missing check"],
        "suggestions": [],
        "confidence_rating": 0.8,
    }
)
CHALLENGE_OK = json.dumps(
    {"assumptions_challenged": [], "failure_vectors": [], "severity_rating": 0.1}
)
CRITIC_OK = json.dumps({"passed": True, "issues": [], "score": 0.9, "rationale": "fine"})
CODE = "```python\nprint('hello')\n```\nA short note."
FINAL = "FINAL ANSWER FROM SYNTHESIZER"

_SEAT_MARKERS = (
    ("planner", "Lead Planner"),
    ("synthesizer", "Lead Synthesizer"),
    ("challenger", "Adversarial Challenger"),
    ("scalability", "Scalability Critic"),
    ("security", "Security Critic"),
    ("performance", "Performance Critic"),
    ("maintainability", "Maintainability Critic"),
    ("reviewer", "Code Reviewer"),
    ("coder", "Lead Coder"),
)


def seat_of(request: InferenceRequest) -> str:
    """Which council seat is this request for (read from its system prompt)."""
    system = next((m["content"] for m in request.messages if m["role"] == "system"), "")
    for seat, marker in _SEAT_MARKERS:
        if marker in system:
            return seat
    return "unknown"


def healthy(seat: str, request: InferenceRequest) -> str:
    return {
        "planner": PLAN_JSON,
        "coder": CODE,
        "reviewer": REVIEW_OK,
        "challenger": CHALLENGE_OK,
        "scalability": CRITIC_OK,
        "security": CRITIC_OK,
        "performance": CRITIC_OK,
        "maintainability": CRITIC_OK,
        "synthesizer": FINAL,
    }.get(seat, "?")


class Delay:
    """Sleep *seconds* before answering *text* (to provoke timeouts)."""

    def __init__(self, seconds: float, text: str = "late") -> None:
        self.seconds = seconds
        self.text = text


Responder = Callable[[str, InferenceRequest], "str | Exception | Delay"]


class FakeProvider:
    """Provider whose answers come from ``responder(seat, request)``."""

    def __init__(self, provider_id: str = "fake", responder: Responder | None = None) -> None:
        self.provider_id = provider_id
        self.responder: Responder = responder or healthy
        self.requests: list[InferenceRequest] = []

    def seats_called(self) -> list[str]:
        return [seat_of(r) for r in self.requests]

    def get_capabilities(self) -> Any:
        return SimpleNamespace(supports_streaming=False)

    async def infer(self, request: InferenceRequest) -> InferenceResponse:
        self.requests.append(request)
        result = self.responder(seat_of(request), request)
        if isinstance(result, Delay):
            await asyncio.sleep(result.seconds)
            result = result.text
        if isinstance(result, Exception):
            raise result
        return InferenceResponse(
            content=result,
            model_id=request.model_id,
            finish_reason="stop",
            tokens_used=10,
            latency_ms=1.0,
        )


class FakeProviderRegistry:
    def __init__(self, *providers: FakeProvider) -> None:
        self._providers = {p.provider_id: p for p in providers}

    def get(self, name: str) -> FakeProvider | None:
        return self._providers.get(name)

    def get_or_raise(self, name: str) -> FakeProvider:
        return self._providers[name]

    def check_provider_available(self, provider_id: str) -> bool:
        return provider_id in self._providers

    def list_available_providers(self) -> list[str]:
        return list(self._providers)


def make_model(
    model_id: str = "m1", provider_id: str = "fake", *, is_local: bool = True
) -> ModelDescriptor:
    return ModelDescriptor(
        model_id=model_id,
        provider_id=provider_id,
        display_name=model_id,
        context_length=32768,
        capabilities=ModelCapabilityProfile(),
        is_local=is_local,
    )


class FakeMapper:
    """Maps every council role to one model unless told otherwise."""

    def __init__(self, model: ModelDescriptor | None = None, per_role: dict | None = None) -> None:
        self._model = model or make_model()
        self._per_role = per_role or {}
        self.overrides: dict[CouncilRole, str] = {}
        self.profiler = SimpleNamespace(get_profile=lambda provider_id, model_id: None)

    def map_roles(self, *args: Any, **kwargs: Any) -> dict[CouncilRole, ModelDescriptor]:
        return {role: self._per_role.get(role, self._model) for role in CouncilRole}


def make_orchestrator(
    monkeypatch: Any,
    responder: Responder | None = None,
    *,
    provider: FakeProvider | None = None,
    providers: list[FakeProvider] | None = None,
    mapper: Any = None,
    config: Any = None,
) -> tuple[CouncilOrchestrator, FakeProvider]:
    """The real orchestrator wired to fakes (no DB, no network, no stdin prompts)."""
    monkeypatch.setenv("VELUNE_YES", "1")
    provider = provider or FakeProvider(responder=responder)
    registry = FakeProviderRegistry(*(providers or [provider]))
    orch = CouncilOrchestrator(
        provider_registry=registry,
        mapper=mapper or FakeMapper(),
        analytics=MagicMock(),
        config=config,
    )
    return orch, provider
