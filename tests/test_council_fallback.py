"""Per-seat fallback: bounded, deterministic, consented and visible."""

from __future__ import annotations

import itertools
from unittest.mock import MagicMock

import pytest

from tests.council_fakes import (
    FakeProvider,
    FakeProviderRegistry,
    healthy,
    make_model,
    make_orchestrator,
)
from velune.cognition.council.base import CouncilAgentError
from velune.cognition.council.factory import CouncilAgentFactory
from velune.cognition.council.reviewer import ReviewerAgent
from velune.cognition.execution_trace import CallReason, current_trace, trace_request
from velune.cognition.orchestrator import _fallback_settings
from velune.core.errors.provider import (
    InferenceError,
    ProviderAuthenticationError,
    ProviderConnectionError,
)
from velune.kernel.config import ProvidersConfig, VeluneConfig
from velune.models.specializations import CouncilRole, ModelSpecializationMapper

MESSAGES = [{"role": "user", "content": "judge this"}]


class _Registry:
    def __init__(self, models):
        self._models = list(models)

    def list_all(self):
        return list(self._models)

    def get(self, model_id, provider_id=None):
        return next((m for m in self._models if m.model_id == model_id), None)


def _mapper(*models):
    return ModelSpecializationMapper(_Registry(models))


def _chain(mapper, primary, **kwargs):
    kwargs.setdefault("allowed_provider_ids", ["p2", "p3"])
    return mapper.fallback_chain(CouncilRole.REVIEWER, primary, **kwargs)


def _ids(models):
    return [m.model_id for m in models]


PRIMARY = make_model("m-primary", "p1")


# ── fallback_chain ───────────────────────────────────────────────────────────


def test_chain_is_deterministic_whatever_order_models_were_discovered():
    models = [
        PRIMARY,
        make_model("z-model", "p2"),
        make_model("a-model", "p2"),
        make_model("only", "p3"),
    ]
    chains = {
        tuple(_ids(_chain(_mapper(*order), PRIMARY))) for order in itertools.permutations(models)
    }
    assert chains == {("a-model", "only")}


def test_chain_never_includes_the_primary_provider():
    mapper = _mapper(PRIMARY, make_model("other-p1", "p1"), make_model("b", "p2"))
    chain = _chain(mapper, PRIMARY, allowed_provider_ids=["p1", "p2"])
    assert _ids(chain) == ["b"]


def test_chain_follows_the_allow_list_order_and_is_bounded():
    mapper = _mapper(PRIMARY, make_model("b2", "p2"), make_model("b3", "p3"))
    assert _ids(_chain(mapper, PRIMARY, allowed_provider_ids=["p3", "p2"])) == ["b3", "b2"]
    assert _ids(_chain(mapper, PRIMARY, allowed_provider_ids=["p3", "p2"], max_n=1)) == ["b3"]


def test_chain_skips_providers_that_are_not_usable():
    mapper = _mapper(PRIMARY, make_model("b2", "p2"), make_model("b3", "p3"))
    assert _ids(_chain(mapper, PRIMARY, usable_provider_ids={"p3"})) == ["b3"]
    assert _chain(mapper, PRIMARY, usable_provider_ids=set()) == []


def test_chain_is_empty_when_nothing_is_eligible():
    mapper = _mapper(PRIMARY, make_model("b2", "p2"))
    assert _chain(mapper, PRIMARY, allowed_provider_ids=[]) == []
    assert _chain(mapper, PRIMARY, allowed_provider_ids=["nowhere"]) == []
    assert _chain(mapper, PRIMARY, max_n=0) == []


def test_a_local_primary_never_falls_back_to_a_cloud_model_by_default():
    cloud = make_model("cloud-model", "p2", is_local=False)
    mapper = _mapper(PRIMARY, cloud)
    assert PRIMARY.is_local
    assert _chain(mapper, PRIMARY) == []
    assert _ids(_chain(mapper, PRIMARY, allow_local_to_cloud=True)) == ["cloud-model"]


def test_a_local_primary_may_still_fall_back_to_another_local_model():
    mapper = _mapper(
        PRIMARY, make_model("local-b", "p2"), make_model("cloud-c", "p3", is_local=False)
    )
    assert _ids(_chain(mapper, PRIMARY)) == ["local-b"]


def test_a_cloud_primary_may_fall_back_to_local_or_cloud():
    cloud_primary = make_model("cloud-primary", "p1", is_local=False)
    mapper = _mapper(
        cloud_primary, make_model("local-b", "p2"), make_model("cloud-c", "p3", is_local=False)
    )
    assert _ids(_chain(mapper, cloud_primary)) == ["local-b", "cloud-c"]


# ── agent ────────────────────────────────────────────────────────────────────


def _agent(primary_failure, *backups: FakeProvider):
    primary = FakeProvider("p1", lambda seat, request: primary_failure)
    agent = ReviewerAgent(make_model("m-primary", "p1"), primary)
    agent._fallback_providers = [
        (backup, make_model(f"m-{backup.provider_id}", backup.provider_id)) for backup in backups
    ]
    return agent, primary


@pytest.mark.parametrize(
    "failure",
    [
        ProviderConnectionError("down"),
        InferenceError("backend exploded"),
        TimeoutError(),
    ],
)
async def test_a_failed_primary_is_answered_by_the_fallback(failure):
    backup = FakeProvider("p2", lambda seat, request: "backup answer")
    agent, _ = _agent(failure, backup)
    with trace_request(prompt_preview="x"):
        text = await agent.deliberate(MESSAGES, strict=True)
        reasons = [call.reason for call in current_trace().calls]
    assert text == "backup answer"
    assert CallReason.FALLBACK in reasons


async def test_an_auth_failure_marks_the_key_and_still_falls_back(monkeypatch):
    marked: list[str] = []
    monkeypatch.setattr(
        "velune.providers.keystore.mark_invalid", lambda pid, reason="": marked.append(pid)
    )
    backup = FakeProvider("p2", lambda seat, request: "backup answer")
    agent, _ = _agent(ProviderAuthenticationError("bad key"), backup)
    assert await agent.deliberate(MESSAGES, strict=True) == "backup answer"
    assert marked == ["p1"]


async def test_alternates_are_tried_in_order_and_empty_ones_are_skipped():
    first = FakeProvider("p2", lambda seat, request: "  ")
    second = FakeProvider("p3", lambda seat, request: "second answer")
    third = FakeProvider("p4", lambda seat, request: "third answer")
    agent, _ = _agent(InferenceError("down"), first, second, third)
    assert await agent.deliberate(MESSAGES, strict=True) == "second answer"
    assert third.requests == []


async def test_on_fallback_fires_once_with_the_models_involved():
    backup = FakeProvider("p2", lambda seat, request: "backup answer")
    agent, _ = _agent(InferenceError("down"), backup)
    calls: list[tuple[str, str, str]] = []
    agent.on_fallback = lambda seat, a, b: calls.append((seat, a, b))
    await agent.deliberate(MESSAGES, strict=True)
    assert calls == [("reviewer", "p1/m-primary", "p2/m-p2")]


async def test_when_every_alternate_fails_a_strict_call_raises_the_original_kind():
    down = FakeProvider("p2", lambda seat, request: InferenceError("also down"))
    agent, _ = _agent(InferenceError("down"), down)
    with pytest.raises(CouncilAgentError) as excinfo:
        await agent.deliberate(MESSAGES, strict=True)
    assert excinfo.value.kind == "provider"

    agent, _ = _agent(TimeoutError(), FakeProvider("p2", lambda seat, request: InferenceError("x")))
    with pytest.raises(CouncilAgentError) as excinfo:
        await agent.deliberate(MESSAGES, strict=True)
    assert excinfo.value.kind == "timeout"


async def test_without_alternates_behaviour_is_unchanged():
    agent, primary = _agent(InferenceError("down"))
    assert (await agent.deliberate(MESSAGES)).startswith("Deliberation failure inside agent")
    agent, _ = _agent(TimeoutError())
    assert (await agent.deliberate(MESSAGES)).startswith("[Agent reviewer timed out")


async def test_a_healthy_primary_never_touches_the_fallback():
    backup = FakeProvider("p2", lambda seat, request: "backup answer")
    primary = FakeProvider("p1", lambda seat, request: "primary answer")
    agent = ReviewerAgent(make_model("m-primary", "p1"), primary)
    agent._fallback_providers = [(backup, make_model("m-p2", "p2"))]
    assert await agent.deliberate(MESSAGES, strict=True) == "primary answer"
    assert backup.requests == []


# ── factory ──────────────────────────────────────────────────────────────────


def _factory(*, allowed=("p2", "p3"), providers=("p1", "p2", "p3"), allow_cloud=False, models=None):
    models = models or [
        make_model("m1", "p1"),
        make_model("m2", "p2"),
        make_model("m3", "p3"),
    ]
    registry = FakeProviderRegistry(*(FakeProvider(pid) for pid in providers))
    return CouncilAgentFactory(
        registry,
        _mapper(*models),
        fallback_provider_ids=allowed,
        allow_cloud_fallback_from_local=allow_cloud,
    )


def _fallback_models(agent):
    return [model.model_id for _, model in agent._fallback_providers]


def test_every_seat_gets_the_same_chain_for_the_same_role():
    factory = _factory()
    first = factory.create_reviewer("run")
    second = factory.create_reviewer("run")
    assert _fallback_models(first) == _fallback_models(second) == ["m2", "m3"]
    assert _fallback_models(factory.create_planner("run")) == ["m2", "m3"]
    assert _fallback_models(factory.create_coder("run")) == ["m2", "m3"]
    assert _fallback_models(factory.create_synthesizer("run")) == ["m2", "m3"]


def test_critics_get_the_chain_of_the_role_they_run_on():
    factory = _factory()
    for create in (
        factory.create_challenger,
        factory.create_scalability_critic,
        factory.create_security_critic,
        factory.create_performance_critic,
        factory.create_maintainability_critic,
    ):
        assert _fallback_models(create("run")) == ["m2", "m3"]


def test_factory_without_allowed_providers_builds_agents_without_fallbacks():
    factory = _factory(allowed=())
    assert factory.create_reviewer("run")._fallback_providers == []


def test_factory_skips_providers_the_registry_cannot_serve():
    factory = _factory(providers=("p1", "p3"))
    assert _fallback_models(factory.create_reviewer("run")) == ["m3"]


def test_factory_tolerates_a_mapper_without_fallback_chain():
    from tests.council_fakes import FakeMapper

    registry = FakeProviderRegistry(FakeProvider("fake"))
    factory = CouncilAgentFactory(registry, FakeMapper(), fallback_provider_ids=("p2",))
    assert factory.create_reviewer("run")._fallback_providers == []


def test_factory_applies_the_local_to_cloud_guard():
    models = [
        make_model("m1", "p1"),
        make_model("cloud", "p2", is_local=False),
        make_model("local", "p3"),
    ]
    guarded = _factory(models=models)
    assert _fallback_models(guarded.create_reviewer("run")) == ["local"]
    allowed = _factory(models=models, allow_cloud=True)
    assert _fallback_models(allowed.create_reviewer("run")) == ["cloud", "local"]


# ── settings ─────────────────────────────────────────────────────────────────


def test_provider_config_defaults_keep_cloud_fallback_off():
    assert ProvidersConfig().allow_cloud_fallback_from_local is False


def test_fallback_settings_read_the_provider_config():
    config = VeluneConfig()
    config.providers.fallback_providers = ["p2", "p3"]
    assert _fallback_settings(config) == (("p2", "p3"), False)
    config.providers.allow_cloud_fallback_from_local = True
    assert _fallback_settings(config) == (("p2", "p3"), True)


def test_fallback_settings_without_a_config_mean_no_fallback():
    assert _fallback_settings(None) == ((), False)


def test_fallback_settings_ignore_mock_configs():
    assert _fallback_settings(MagicMock()) == ((), False)


# ── orchestrator ─────────────────────────────────────────────────────────────


def _two_provider_run(monkeypatch, *, backup_local=True, allow_cloud=False, with_config=True):
    primary_model = make_model("m-primary", "p1")
    backup_model = make_model("m-backup", "p2", is_local=backup_local)

    def p1_responder(seat, request):
        if seat == "reviewer":
            return ProviderConnectionError("p1 down")
        return healthy(seat, request)

    p1 = FakeProvider("p1", p1_responder)
    p2 = FakeProvider("p2")
    config = None
    if with_config:
        config = VeluneConfig()
        config.providers.fallback_providers = ["p2"]
        config.providers.allow_cloud_fallback_from_local = allow_cloud
    orch, _ = make_orchestrator(
        monkeypatch,
        providers=[p1, p2],
        mapper=_mapper(primary_model, backup_model),
        config=config,
    )
    return orch, p1, p2


async def test_the_run_completes_through_the_fallback_and_says_so(monkeypatch):
    orch, _, p2 = _two_provider_run(monkeypatch)
    messages: list[str] = []
    result = await orch.execute_task(
        "explain it", "ctx", council_tier="standard", progress_callback=messages.append
    )
    assert p2.seats_called() == ["reviewer"]
    assert result["reviewer_report"].usable
    assert not any(f.startswith("JUDGE_UNAVAILABLE") for f in result["arbitration"]["flags"])
    assert result["degraded"] is False
    assert "[Fallback] reviewer: p1/m-primary -> p2/m-backup" in messages
    assert result["contract_verdict"]["ok"] is True


async def test_a_local_seat_does_not_leak_to_a_cloud_fallback_by_default(monkeypatch):
    orch, _, p2 = _two_provider_run(monkeypatch, backup_local=False)
    result = await orch.execute_task("explain it", "ctx", council_tier="standard")
    assert p2.requests == []
    assert "JUDGE_UNAVAILABLE:reviewer" in result["arbitration"]["flags"]


async def test_the_local_to_cloud_opt_in_is_honoured(monkeypatch):
    orch, _, p2 = _two_provider_run(monkeypatch, backup_local=False, allow_cloud=True)
    result = await orch.execute_task("explain it", "ctx", council_tier="standard")
    assert p2.seats_called() == ["reviewer"]
    assert result["reviewer_report"].usable


async def test_without_a_config_a_failed_seat_is_simply_unavailable(monkeypatch):
    orch, _, p2 = _two_provider_run(monkeypatch, with_config=False)
    result = await orch.execute_task("explain it", "ctx", council_tier="standard")
    assert p2.requests == []
    assert "JUDGE_UNAVAILABLE:reviewer" in result["arbitration"]["flags"]
