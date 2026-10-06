"""/roles must offer, apply and report exactly the roles a council run uses."""

from __future__ import annotations

import io
import json
import logging
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from rich.console import Console

from tests.council_fakes import make_model, make_orchestrator
from velune.cli.handlers.councilmodel import (
    apply_role_overrides_to_orchestrator,
    cmd_councilmodel_show,
)
from velune.models.specializations import CouncilRole, ModelSpecializationMapper
from velune.orchestration.role_assignments import (
    EFFECTIVE_ROLES,
    INERT_ROLE_NOTES,
    CouncilRoleMap,
    RoleAssignment,
    apply_persisted_role_overrides,
    apply_role_map,
)


class _Registry:
    """Honours the provider qualifier the way ModelCapabilityRegistry does."""

    def __init__(self, models):
        self._models = list(models)

    def list_all(self):
        return list(self._models)

    def get(self, model_id, provider_id=None):
        for model in self._models:
            if model.model_id == model_id and (
                provider_id is None or model.provider_id == provider_id
            ):
                return model
        return None


def _write(path: Path, data: dict) -> Path:
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


# ── the role set ─────────────────────────────────────────────────────────────


def test_effective_roles_are_exactly_the_runtime_roles():
    assert EFFECTIVE_ROLES == tuple(role.value for role in CouncilRole)
    assert not set(INERT_ROLE_NOTES) & set(EFFECTIVE_ROLES)


@pytest.mark.parametrize("role", ["embedding", "architect", "security"])
def test_inert_roles_are_rejected_with_guidance(role):
    with pytest.raises(ValueError) as excinfo:
        CouncilRoleMap().assign(role, "m", "p")
    message = str(excinfo.value)
    assert "has no effect" in message
    assert INERT_ROLE_NOTES[role] in message
    assert "planner" in message  # lists the valid roles


def test_unknown_roles_are_rejected():
    with pytest.raises(ValueError, match="Unknown role"):
        CouncilRoleMap().assign("wizard", "m", "p")


@pytest.mark.parametrize("role", EFFECTIVE_ROLES)
def test_every_effective_role_can_be_assigned(role):
    role_map = CouncilRoleMap()
    role_map.assign(role, "m", "p")
    assert role_map.get(role).model_id == "m"


# ── loading and saving ───────────────────────────────────────────────────────


def test_one_bad_entry_never_discards_the_valid_ones(tmp_path):
    path = _write(
        tmp_path / "roles.json",
        {
            "coder": {"model_id": "m1", "provider_id": "p1"},
            "embedding": {"model_id": "nomic", "provider_id": "ollama"},
            "wizard": {"model_id": "x", "provider_id": "y"},
            "reviewer": {"model_id": "m2"},  # malformed: no provider
            "planner": "not-a-dict",
        },
    )
    loaded = CouncilRoleMap.load(path)
    assert set(loaded.assignments) == {"coder"}
    assert set(loaded.ignored) == {"embedding", "wizard", "reviewer", "planner"}
    notes = loaded.ignored_notes()
    assert notes["embedding"] == INERT_ROLE_NOTES["embedding"]
    assert notes["wizard"] == "not a council role"


def test_ignored_entries_are_kept_when_the_file_is_saved_again(tmp_path):
    path = _write(
        tmp_path / "roles.json",
        {
            "coder": {"model_id": "m1", "provider_id": "p1"},
            "embedding": {"model_id": "nomic", "provider_id": "ollama"},
        },
    )
    loaded = CouncilRoleMap.load(path)
    loaded.assign("planner", "m2", "p2")
    loaded.save(path)
    on_disk = json.loads(path.read_text(encoding="utf-8"))
    assert set(on_disk) == {"coder", "planner", "embedding"}
    assert CouncilRoleMap.load(path).ignored_notes().keys() == {"embedding"}


def test_clearing_removes_ignored_entries_too(tmp_path):
    loaded = CouncilRoleMap.from_dict(
        {
            "embedding": {"model_id": "n", "provider_id": "o"},
            "coder": {"model_id": "m", "provider_id": "p"},
        }
    )
    loaded.clear_role("embedding")
    assert loaded.ignored == {}
    loaded.clear_all()
    assert loaded.assignments == {}


def test_unreadable_files_load_empty_and_log(tmp_path, caplog):
    path = tmp_path / "roles.json"
    path.write_text("{ not json", encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger="velune.orchestration.role_assignments"):
        assert CouncilRoleMap.load(path).assignments == {}
    assert "Could not read council role assignments" in caplog.text

    path.write_text("[1, 2]", encoding="utf-8")
    assert CouncilRoleMap.load(path).assignments == {}
    assert CouncilRoleMap.load(tmp_path / "missing.json").assignments == {}


# ── applying ─────────────────────────────────────────────────────────────────


def _mapper(*models):
    return ModelSpecializationMapper(_Registry(models))


def test_apply_role_map_reports_applied_and_ignored_and_replaces_old_overrides():
    mapper = _mapper(make_model("m1", "p1"))
    mapper.overrides[CouncilRole.SYNTHESIZER] = "stale"
    role_map = CouncilRoleMap.from_dict(
        {
            "coder": {"model_id": "m1", "provider_id": "p1"},
            "embedding": {"model_id": "n", "provider_id": "o"},
        }
    )
    report = apply_role_map(mapper, role_map)
    assert report.applied == ["coder"]
    assert set(report.ignored) == {"embedding"}
    assert mapper.overrides == {CouncilRole.CODER: "m1"}
    assert mapper.override_providers == {CouncilRole.CODER: "p1"}
    assert apply_role_map(mapper, role_map).applied == ["coder"]  # idempotent


def test_an_unknown_provider_is_not_recorded():
    mapper = _mapper(make_model("m1", "p1"))
    role_map = CouncilRoleMap()
    role_map.assignments["coder"] = RoleAssignment("coder", "m1", "unknown")
    apply_role_map(mapper, role_map)
    assert mapper.override_providers == {}


def test_the_same_model_id_on_two_providers_resolves_by_the_stored_provider():
    on_p1 = make_model("shared", "p1")
    on_p2 = make_model("shared", "p2")
    mapper = _mapper(on_p1, on_p2)
    role_map = CouncilRoleMap()
    role_map.assign("coder", "shared", "p2")
    apply_role_map(mapper, role_map)
    assert mapper.map_roles()[CouncilRole.CODER].provider_id == "p2"
    assert mapper.override_misses == []


def test_an_assigned_model_the_registry_lacks_is_reported_and_auto_routed(caplog):
    mapper = _mapper(make_model("m1", "p1"))
    role_map = CouncilRoleMap()
    role_map.assign("coder", "ghost", "p9")
    apply_role_map(mapper, role_map)
    with caplog.at_level(logging.WARNING, logger="velune.models.specializations"):
        roles = mapper.map_roles()
    assert roles[CouncilRole.CODER].model_id == "m1"  # automatic routing
    assert mapper.override_misses == ["coder=ghost"]
    assert "ghost" in caplog.text


async def test_the_run_announces_an_assigned_model_that_is_unavailable(monkeypatch):
    mapper = _mapper(make_model("m1", "fake"))
    mapper.overrides[CouncilRole.CODER] = "ghost"
    orch, _ = make_orchestrator(monkeypatch, mapper=mapper)
    messages: list[str] = []
    await orch.execute_task(
        "explain it", "ctx", council_tier="instant", progress_callback=messages.append
    )
    assert any("coder=ghost is unavailable" in m for m in messages)


# ── REPL handler ─────────────────────────────────────────────────────────────


def _repl(role_map, mapper=None, registry=None):
    buffer = io.StringIO()
    orchestrator = SimpleNamespace(
        mapper=mapper or _mapper(make_model("m1", "p1")), agent_factory=MagicMock()
    )
    services = {"runtime.council_orchestrator": orchestrator}
    if registry is not None:
        services["runtime.model_registry"] = registry
    repl = SimpleNamespace(
        console=Console(file=buffer, width=120, color_system=None),
        container=SimpleNamespace(get=lambda key: services[key], has=lambda key: key in services),
        _role_map=role_map,
    )
    repl.output = buffer
    repl.orchestrator = orchestrator
    return repl


def test_handler_applies_valid_roles_and_warns_once_about_ignored_ones():
    role_map = CouncilRoleMap.from_dict(
        {
            "coder": {"model_id": "m1", "provider_id": "p1"},
            "embedding": {"model_id": "n", "provider_id": "o"},
        }
    )
    repl = _repl(role_map)
    report = apply_role_overrides_to_orchestrator(repl)
    assert report.applied == ["coder"] and set(report.ignored) == {"embedding"}
    assert repl.orchestrator.mapper.overrides == {CouncilRole.CODER: "m1"}
    repl.orchestrator.agent_factory.clear_cache.assert_called_once()
    assert "embedding" in repl.output.getvalue()
    first = repl.output.getvalue()
    apply_role_overrides_to_orchestrator(repl)
    assert repl.output.getvalue() == first  # warned once per session


def test_handler_is_quiet_when_nothing_is_ignored():
    role_map = CouncilRoleMap()
    role_map.assign("coder", "m1", "p1")
    repl = _repl(role_map)
    apply_role_overrides_to_orchestrator(repl)
    assert repl.output.getvalue() == ""


def test_handler_returns_none_without_an_orchestrator_or_on_error(caplog):
    repl = _repl(CouncilRoleMap())
    repl.container = SimpleNamespace(get=lambda key: None)
    assert apply_role_overrides_to_orchestrator(repl) is None

    broken = _repl(CouncilRoleMap())
    broken._role_map = None  # makes applying fail
    with caplog.at_level(logging.WARNING, logger="velune.cli.handlers.councilmodel"):
        assert apply_role_overrides_to_orchestrator(broken) is None
    assert "Could not apply council role assignments" in caplog.text


async def test_roles_show_lists_only_working_roles_and_reports_ignored_ones():
    role_map = CouncilRoleMap.from_dict(
        {
            "coder": {"model_id": "gone", "provider_id": "p1"},
            "planner": {"model_id": "m1", "provider_id": "p1"},
            "embedding": {"model_id": "n", "provider_id": "o"},
            "architect": {"model_id": "a", "provider_id": "o"},
        }
    )
    repl = _repl(role_map, registry=_Registry([make_model("m1", "p1")]))
    await cmd_councilmodel_show(repl)
    out = repl.output.getvalue()
    for role in EFFECTIVE_ROLES:
        assert role in out
    assert "Ignored (no effect)" in out
    assert INERT_ROLE_NOTES["embedding"][:20] in out
    assert "unavailable - auto-routed" in out  # the 'gone' coder model
    assert "in use" in out  # the planner model


# ── every entry point honours the saved assignments ──────────────────────────


def test_orchestrator_subsystem_applies_the_saved_assignments(monkeypatch, tmp_path):
    from velune.cognition.subsystems import _create_council_orchestrator

    (tmp_path / ".velune").mkdir()
    _write(
        tmp_path / ".velune" / "council_roles.json",
        {
            "coder": {"model_id": "m-x", "provider_id": "p1"},
            "embedding": {"model_id": "n", "provider_id": "o"},
        },
    )
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr("velune.cognition.orchestrator.CognitivePerformanceAnalytics", MagicMock())
    services = {
        "runtime.provider_registry": MagicMock(),
        "runtime.model_registry": _Registry([make_model("m-x", "p1")]),
        "runtime.lineage_memory": None,
    }
    env = SimpleNamespace(
        container=SimpleNamespace(get=lambda key: services[key], has=lambda key: key in services),
        config=None,
    )
    orchestrator = _create_council_orchestrator(env)
    assert orchestrator.mapper.overrides == {CouncilRole.CODER: "m-x"}
    assert orchestrator.mapper.override_providers == {CouncilRole.CODER: "p1"}


def test_apply_persisted_overrides_tolerates_a_missing_file(tmp_path):
    mapper = _mapper(make_model("m1", "p1"))
    report = apply_persisted_role_overrides(mapper, tmp_path / "nope.json")
    assert report.applied == [] and mapper.overrides == {}


def test_doctor_reports_assigned_roles_and_ignored_entries(monkeypatch, tmp_path):
    from velune.cli.commands import doctor

    (tmp_path / ".velune").mkdir()
    _write(
        tmp_path / ".velune" / "council_roles.json",
        {
            "coder": {"model_id": "m1", "provider_id": "p1"},
            "embedding": {"model_id": "n", "provider_id": "o"},
        },
    )
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(
        "velune.models.registry.ModelCapabilityRegistry",
        lambda: _Registry([make_model("m1", "p1"), make_model("m2", "p1")]),
    )
    result = doctor._check_council_roles()
    assert "coder→m1 (assigned)" in result["message"]
    assert "planner→m1 (assigned)" not in result["message"]
    assert "saved role(s) with no effect: embedding" in result["message"]
    assert result["status"] == "warn"
