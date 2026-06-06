"""Tests for council role assignment data model."""

import pytest

from velune.orchestration.role_assignments import (
    COUNCIL_ROLES,
    ROLE_DESCRIPTIONS,
    CouncilRoleMap,
    RoleAssignment,
)


def test_assign_role():
    rm = CouncilRoleMap()
    rm.assign("coder", "qwen2.5-coder:7b", "ollama")
    a = rm.get("coder")
    assert a is not None
    assert a.model_id == "qwen2.5-coder:7b"
    assert a.provider_id == "ollama"


def test_clear_role():
    rm = CouncilRoleMap()
    rm.assign("reviewer", "gpt-4o", "openai")
    rm.clear_role("reviewer")
    assert rm.get("reviewer") is None


def test_clear_all():
    rm = CouncilRoleMap()
    rm.assign("coder", "model-a", "ollama")
    rm.assign("planner", "model-b", "groq")
    rm.clear_all()
    assert rm.assignments == {}


def test_invalid_role_raises():
    rm = CouncilRoleMap()
    with pytest.raises(ValueError, match="Unknown role"):
        rm.assign("nonexistent_role", "model", "provider")


def test_save_and_load(tmp_path):
    rm = CouncilRoleMap()
    rm.assign("synthesizer", "gemini-2.0-flash", "google")
    rm.assign("coder", "qwen2.5-coder:7b", "ollama")
    path = tmp_path / "roles.json"
    rm.save(path)

    loaded = CouncilRoleMap.load(path)
    assert loaded.get("synthesizer").model_id == "gemini-2.0-flash"
    assert loaded.get("synthesizer").provider_id == "google"
    assert loaded.get("coder").model_id == "qwen2.5-coder:7b"


def test_load_missing_file_returns_empty(tmp_path):
    rm = CouncilRoleMap.load(tmp_path / "nonexistent.json")
    assert rm.assignments == {}


def test_all_roles_are_valid():
    rm = CouncilRoleMap()
    for role in COUNCIL_ROLES:
        rm.assign(role, f"model-{role}", "ollama")
    assert len(rm.assignments) == len(COUNCIL_ROLES)


def test_to_dict_and_from_dict():
    rm = CouncilRoleMap()
    rm.assign("reviewer", "gemini-2.0-flash", "google")
    data = rm.to_dict()
    assert data["reviewer"]["model_id"] == "gemini-2.0-flash"
    assert data["reviewer"]["provider_id"] == "google"

    restored = CouncilRoleMap.from_dict(data)
    a = restored.get("reviewer")
    assert a is not None
    assert a.model_id == "gemini-2.0-flash"


def test_role_descriptions_cover_all_roles():
    for role in COUNCIL_ROLES:
        assert role in ROLE_DESCRIPTIONS, f"Missing description for role: {role}"
        assert len(ROLE_DESCRIPTIONS[role]) > 10


def test_save_creates_parent_dirs(tmp_path):
    rm = CouncilRoleMap()
    rm.assign("coder", "test-model", "ollama")
    nested = tmp_path / "deep" / "nested" / "roles.json"
    rm.save(nested)
    assert nested.exists()


def test_load_corrupt_file_returns_empty(tmp_path):
    bad = tmp_path / "corrupt.json"
    bad.write_text("this is not valid json {{{{")
    rm = CouncilRoleMap.load(bad)
    assert rm.assignments == {}


def test_clear_nonexistent_role_is_noop():
    rm = CouncilRoleMap()
    rm.clear_role("reviewer")  # should not raise
    assert rm.get("reviewer") is None


def test_get_returns_none_for_unassigned():
    rm = CouncilRoleMap()
    assert rm.get("planner") is None
    assert rm.get("synthesizer") is None
