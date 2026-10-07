"""The feature flag, the facade it gates, and proof that legacy behaviour is untouched."""

from __future__ import annotations

import asyncio
import logging
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import velune.cognition.prompts as prompts_pkg
from tests.council_fakes import FakeMapper, FakeProviderRegistry, make_orchestrator
from tests.council_scripted import PERSPECTIVE_SEATS
from tests.council_wire import ScriptedProvider, runtime_invoker
from velune.cognition.council.factory import CouncilAgentFactory
from velune.cognition.execution_trace import NodeType, trace_request
from velune.cognition.prompts import deliberation_digest, reset_prompt_layer
from velune.council.adapters.engine import (
    ENGINE_DELIBERATIVE,
    ENGINE_LEGACY,
    DeliberativeEngine,
    EngineDisabled,
    engine_enabled,
    engine_mode,
)
from velune.council.adapters.prompts import LibraryPrompts
from velune.council.domain import StageId
from velune.council.profiles import GENERAL_PROFILE
from velune.council.report import frame_of, perspectives_of
from velune.council.request import EvidenceItem, ResponseRequirements
from velune.council.results import OutcomeStatus
from velune.kernel.config import ConfigLoader, VeluneConfig, get_default_config

ROOT = Path(__file__).resolve().parents[1]


def config_for(mode: str) -> VeluneConfig:
    return VeluneConfig(cognition={"council_engine": mode})


class Untouchable:
    """A container that fails the test if the engine so much as looks at the runtime."""

    def __getattr__(self, name):
        raise AssertionError(f"the runtime was touched ({name})")


class Container:
    def __init__(self, provider):
        self._factory = CouncilAgentFactory(FakeProviderRegistry(provider), FakeMapper())

    def get(self, name):
        assert name == "runtime.council_orchestrator"
        return SimpleNamespace(agent_factory=self._factory)


def run(coro, timeout: float = 30):
    return asyncio.run(asyncio.wait_for(coro, timeout))


# ── the flag ────────────────────────────────────────────────────────────────


def test_the_default_is_legacy(monkeypatch):
    monkeypatch.delenv("VELUNE_COGNITION__COUNCIL_ENGINE", raising=False)
    assert VeluneConfig().cognition.council_engine == ENGINE_LEGACY
    assert get_default_config().cognition.council_engine == ENGINE_LEGACY
    assert engine_mode(VeluneConfig()) == ENGINE_LEGACY and not engine_enabled(VeluneConfig())


def test_the_repository_config_does_not_enable_it():
    toml_text = (ROOT / "velune.toml").read_text(encoding="utf-8")
    assert "council_engine" not in toml_text


def test_the_env_var_enables_it(monkeypatch):
    monkeypatch.setenv("VELUNE_COGNITION__COUNCIL_ENGINE", "deliberative")
    assert VeluneConfig().cognition.council_engine == ENGINE_DELIBERATIVE
    assert engine_enabled(VeluneConfig())


def test_toml_enables_it(tmp_path):
    path = tmp_path / "velune.toml"
    path.write_text('[cognition]\ncouncil_engine = "deliberative"\n', encoding="utf-8")
    assert ConfigLoader(path).load().cognition.council_engine == ENGINE_DELIBERATIVE


def test_values_are_normalised_case_and_whitespace():
    assert config_for("  Deliberative ").cognition.council_engine == ENGINE_DELIBERATIVE
    assert config_for("LEGACY").cognition.council_engine == ENGINE_LEGACY


@pytest.mark.parametrize("bad", ["", "council", "true", "1", "deliberate", None])
def test_an_unknown_value_falls_back_to_legacy_with_a_warning(bad, caplog):
    with caplog.at_level(logging.WARNING, logger="velune.kernel.config"):
        cfg = config_for(bad)
    assert cfg.cognition.council_engine == ENGINE_LEGACY
    assert any("council_engine" in r.getMessage() for r in caplog.records)
    assert not engine_enabled(cfg)


def test_a_missing_or_foreign_config_means_legacy():
    assert engine_mode(None) == ENGINE_LEGACY
    assert engine_mode(SimpleNamespace()) == ENGINE_LEGACY
    assert (
        engine_mode(SimpleNamespace(cognition=SimpleNamespace(council_engine="x"))) == ENGINE_LEGACY
    )
    from unittest.mock import MagicMock

    assert engine_mode(MagicMock()) == ENGINE_LEGACY


# ── the facade ──────────────────────────────────────────────────────────────


def test_create_refuses_when_disabled_and_never_touches_the_runtime():
    for mode in ("legacy", "bogus"):
        with pytest.raises(EngineDisabled, match="council_engine"):
            DeliberativeEngine.create(Untouchable(), config_for(mode))
    with pytest.raises(EngineDisabled):
        DeliberativeEngine.create(Untouchable(), None)


def test_create_works_when_enabled_and_explores_through_the_real_adapter():
    provider = ScriptedProvider()
    engine = DeliberativeEngine.create(Container(provider), config_for("deliberative"))
    outcome = run(engine.explore("Which database should we pick?"))
    assert outcome.status is OutcomeStatus.COMPLETED and outcome.answer is None
    assert list(perspectives_of(outcome)) == list(PERSPECTIVE_SEATS)
    assert (
        frame_of(outcome, engine.build_request("Which database should we pick?")).degraded is False
    )
    assert len(provider.requests) == 6  # one frame, five perspectives; nothing else
    assert [s.stage for s in outcome.stage_results] == [StageId.FRAME, StageId.PERSPECTIVES]


def test_an_exploration_never_returns_an_answer_even_on_success():
    provider = ScriptedProvider()
    engine = DeliberativeEngine.create(Container(provider), config_for("deliberative"))
    outcome = run(engine.explore("anything"))
    assert outcome.succeeded and outcome.answer is None and outcome.artifacts == ()


def test_the_request_records_the_deliberation_prompt_digest_and_the_inputs():
    engine = DeliberativeEngine(invoker_factory=lambda profile, run_id: None)
    request = engine.build_request(
        "Why?",
        context="ctx",
        evidence=[EvidenceItem(id="e1", text="fact")],
        response=ResponseRequirements(language="en"),
    )
    assert dict(request.metadata)["deliberation_prompt_digest"] == deliberation_digest()
    assert request.profile_id == "general" and request.context == "ctx"
    assert request.evidence[0].id == "e1" and request.response.language == "en"
    assert engine.build_request("Why?").request_id != request.request_id  # fresh ids


def test_explore_passes_evidence_and_requirements_to_the_seats():
    provider = ScriptedProvider()
    engine = DeliberativeEngine(invoker_factory=lambda p, r: runtime_invoker(provider))
    run(
        engine.explore(
            "Why?",
            evidence=[EvidenceItem(id="e1", text="EV-TOKEN")],
            response=ResponseRequirements(language="fr"),
        )
    )
    assert "EV-TOKEN" not in str(provider.requests_for("moderator")[0].messages)
    for seat in PERSPECTIVE_SEATS:
        assert "EV-TOKEN" in str(provider.requests_for(seat)[0].messages)
        assert "language: fr" in str(provider.requests_for(seat)[0].messages)


def test_explore_projects_stages_onto_an_active_request_trace():
    provider = ScriptedProvider()
    engine = DeliberativeEngine(invoker_factory=lambda p, r: runtime_invoker(provider))

    async def scenario():
        with trace_request("outer") as trace:
            outcome = await engine.explore("Why?")
        return trace, outcome

    trace, outcome = run(scenario())
    assert outcome.status is OutcomeStatus.COMPLETED
    kinds = [n.type for n in trace.nodes if n.type is not NodeType.REQUEST]
    assert kinds == [NodeType.FRAMING, NodeType.PERSPECTIVES]
    assert len(trace.calls) == 6 and trace.request_id == "outer"


def test_explore_works_without_an_active_request_trace():
    provider = ScriptedProvider()
    engine = DeliberativeEngine(invoker_factory=lambda p, r: runtime_invoker(provider))
    assert run(engine.explore("Why?")).status is OutcomeStatus.COMPLETED


def test_each_explore_gets_its_own_run_id_for_role_routing():
    seen = []
    provider = ScriptedProvider()

    def factory(profile, run_id):
        seen.append(run_id)
        return runtime_invoker(provider)

    engine = DeliberativeEngine(invoker_factory=factory)
    run(engine.explore("a"))
    run(engine.explore("b"))
    assert len(seen) == 2 and seen[0] != seen[1]


# ── the prompt adapter ──────────────────────────────────────────────────────


def test_library_prompts_serve_the_committed_text_and_refuse_unprompted_seats():
    from velune.cognition.prompts._deliberation import PROMPTS

    prompts = LibraryPrompts(GENERAL_PROFILE)
    assert prompts.shared_prompt(StageId.PERSPECTIVES) == PROMPTS["council.general.shared"]
    for seat in ("moderator", *PERSPECTIVE_SEATS):
        assert prompts.role_prompt(seat, StageId.PERSPECTIVES) == PROMPTS[f"council.general.{seat}"]
    with pytest.raises(KeyError):
        prompts.role_prompt("arbitrator", StageId.ARBITRATION)  # no prompt yet: loud, not empty
    with pytest.raises(KeyError):
        prompts.role_prompt("ghost", StageId.PERSPECTIVES)


def test_library_prompts_follow_the_premium_layer_for_wording(monkeypatch):
    import types

    fake = types.ModuleType("velune.cognition.prompts._premium")
    fake.PROMPTS = {"council.general.skeptic": "PREMIUM SKEPTIC"}
    monkeypatch.setitem(sys.modules, "velune.cognition.prompts._premium", fake)
    monkeypatch.setattr(prompts_pkg, "_premium", fake, raising=False)  # a real one may be cached
    monkeypatch.setenv("VELUNE_PROMPT_LAYER", "premium")
    reset_prompt_layer()
    try:
        assert LibraryPrompts(GENERAL_PROFILE).role_prompt("skeptic", StageId.PERSPECTIVES) == (
            "PREMIUM SKEPTIC"
        )
    finally:
        monkeypatch.delenv("VELUNE_PROMPT_LAYER", raising=False)
        reset_prompt_layer()


# ── legacy behaviour is untouched, flag on or off ───────────────────────────


def test_the_legacy_council_behaves_identically_with_the_flag_on_or_off(monkeypatch):
    async def legacy_run(mode: str):
        orch, provider = make_orchestrator(monkeypatch, config=config_for(mode))
        result = await orch.execute_task("explain it", "ctx", council_tier="standard")
        return result["final_summary"], provider.seats_called(), result["degraded"]

    off = asyncio.run(legacy_run("legacy"))
    on = asyncio.run(legacy_run("deliberative"))
    assert off == on
    assert off[1] == ["planner", "coder", "reviewer", "synthesizer"]


def test_no_live_module_reads_the_flag():
    allowed = {
        ROOT / "velune" / "kernel" / "config.py",
        ROOT / "velune" / "council" / "adapters" / "engine.py",
    }
    readers = [
        str(path.relative_to(ROOT))
        for path in (ROOT / "velune").rglob("*.py")
        if "council_engine" in path.read_text(encoding="utf-8") and path not in allowed
    ]
    assert readers == []


_ENTRY_POINTS = """
import sys
import velune.main
import velune.cli.handlers.council
import velune.cli.commands.ask
import velune.mcp.server
loaded = sorted(m for m in sys.modules if m == "velune.council" or m.startswith("velune.council."))
print("LOADED=" + ",".join(loaded))
"""


def test_the_cli_repl_and_mcp_entry_points_never_import_the_engine():
    run_ = subprocess.run(
        [sys.executable, "-c", _ENTRY_POINTS],
        capture_output=True,
        text=True,
        cwd=ROOT,
        timeout=180,
    )
    assert run_.returncode == 0, run_.stderr[-500:]
    assert run_.stdout.strip().endswith("LOADED="), run_.stdout[-300:]
