"""Week 4 integration tests — Google, council roles, project detection, Ollama, Together/Fireworks."""

import pytest
import json
from pathlib import Path
from unittest.mock import MagicMock, patch, AsyncMock

# ── Google Gemini ──────────────────────────────────────────────────────

def test_gemini_message_no_system_in_contents():
    from velune.core.types.inference import InferenceRequest
    from velune.providers.adapters.google import GoogleProvider
    with patch("velune.providers.keystore.get_key", return_value="key"):
        p = GoogleProvider()
    req = InferenceRequest(
        model_id="gemini-2.0-flash",
        messages=[
            {"role": "system", "content": "Be a coding expert"},
            {"role": "user", "content": "Hello"},
        ],
        temperature=0.3, max_tokens=100,
    )
    payload = p._convert_messages(req)
    assert "systemInstruction" in payload
    roles_in_contents = [c["role"] for c in payload["contents"]]
    assert "system" not in roles_in_contents

def test_gemini_all_models_have_context_length():
    from velune.providers.adapters.google import GEMINI_MODELS
    for m in GEMINI_MODELS:
        assert m.context_length >= 8192

def test_gemini_flash_2_in_model_list():
    from velune.providers.adapters.google import GEMINI_MODELS
    ids = [m.model_id for m in GEMINI_MODELS]
    assert "gemini-2.0-flash" in ids

# ── Council Role Map ───────────────────────────────────────────────────

def test_role_map_assign_all_roles():
    from velune.orchestration.role_assignments import (
        CouncilRoleMap, COUNCIL_ROLES
    )
    rm = CouncilRoleMap()
    for role in COUNCIL_ROLES:
        rm.assign(role, f"{role}-model", "test-provider")
    assert len(rm.assignments) == len(COUNCIL_ROLES)

def test_role_map_serialization_roundtrip(tmp_path):
    from velune.orchestration.role_assignments import CouncilRoleMap
    rm = CouncilRoleMap()
    rm.assign("coder", "qwen2.5-coder:7b", "ollama")
    rm.assign("reviewer", "gemini-2.0-flash", "google")
    path = tmp_path / "roles.json"
    rm.save(path)
    loaded = CouncilRoleMap.load(path)
    assert loaded.get("coder").model_id == "qwen2.5-coder:7b"
    assert loaded.get("reviewer").provider_id == "google"

def test_role_map_clear_single_role():
    from velune.orchestration.role_assignments import CouncilRoleMap
    rm = CouncilRoleMap()
    rm.assign("planner", "gpt-4o", "openai")
    rm.assign("coder", "qwen:7b", "ollama")
    rm.clear_role("planner")
    assert rm.get("planner") is None
    assert rm.get("coder") is not None

# ── Project Type Detection ─────────────────────────────────────────────

def test_fastapi_detection(tmp_path):
    from velune.repository.project_type import ProjectTypeDetector, ProjectType
    (tmp_path / "requirements.txt").write_text("fastapi\nuvicorn\npydantic")
    (tmp_path / "main.py").write_text("from fastapi import FastAPI\napp = FastAPI()")
    profile = ProjectTypeDetector().detect(tmp_path)
    assert profile.project_type == ProjectType.PYTHON_FASTAPI
    assert profile.primary_language == "python"

def test_nextjs_detection(tmp_path):
    import json as _json
    from velune.repository.project_type import ProjectTypeDetector, ProjectType
    (tmp_path / "package.json").write_text(
        _json.dumps({"dependencies": {"next": "14.0.0", "react": "18.0.0"}})
    )
    profile = ProjectTypeDetector().detect(tmp_path)
    assert profile.project_type == ProjectType.NODE_NEXTJS

def test_rust_detection(tmp_path):
    from velune.repository.project_type import ProjectTypeDetector, ProjectType
    (tmp_path / "Cargo.toml").write_text("[package]\nname='test'\nversion='0.1.0'")
    profile = ProjectTypeDetector().detect(tmp_path)
    assert profile.project_type == ProjectType.RUST
    assert profile.primary_language == "rust"

def test_go_detection(tmp_path):
    from velune.repository.project_type import ProjectTypeDetector, ProjectType
    (tmp_path / "go.mod").write_text("module test\ngo 1.21")
    profile = ProjectTypeDetector().detect(tmp_path)
    assert profile.project_type == ProjectType.GO

def test_unknown_detection(tmp_path):
    from velune.repository.project_type import ProjectTypeDetector, ProjectType
    profile = ProjectTypeDetector().detect(tmp_path)
    assert profile.project_type == ProjectType.UNKNOWN

def test_project_profile_has_system_prompt(tmp_path):
    from velune.repository.project_type import (
        ProjectTypeDetector, ProjectType, PROJECT_SYSTEM_PROMPTS
    )
    (tmp_path / "requirements.txt").write_text("fastapi")
    profile = ProjectTypeDetector().detect(tmp_path)
    prompt = PROJECT_SYSTEM_PROMPTS.get(profile.project_type, "")
    assert "FastAPI" in prompt or "fastapi" in prompt.lower()

# ── Ollama Manager ─────────────────────────────────────────────────────

def test_recommended_models_cover_skills():
    from velune.providers.ollama_manager import RECOMMENDED_MODELS
    skills = {m["skill"] for m in RECOMMENDED_MODELS}
    assert "coding" in skills
    assert "reasoning" in skills
    assert "embedding" in skills

def test_all_recommended_models_have_ram_field():
    from velune.providers.ollama_manager import RECOMMENDED_MODELS
    for m in RECOMMENDED_MODELS:
        assert "ram_needed" in m
        assert "GB" in m["ram_needed"]

def test_nomic_embed_is_small():
    from velune.providers.ollama_manager import RECOMMENDED_MODELS
    nomic = next(m for m in RECOMMENDED_MODELS if m["model_id"] == "nomic-embed-text")
    assert nomic["size_gb"] < 1.0

# ── Together + Fireworks ───────────────────────────────────────────────

def test_together_and_fireworks_both_have_deepseek():
    from velune.providers.adapters.together import TOGETHER_MODELS
    from velune.providers.adapters.fireworks import FIREWORKS_MODELS
    together_ids = " ".join(m.model_id.lower() for m in TOGETHER_MODELS)
    fireworks_ids = " ".join(m.model_id.lower() for m in FIREWORKS_MODELS)
    assert "deepseek" in together_ids
    assert "deepseek" in fireworks_ids

def test_together_has_qwen_coder():
    from velune.providers.adapters.together import TOGETHER_MODELS
    ids = " ".join(m.model_id.lower() for m in TOGETHER_MODELS)
    assert "qwen" in ids

def test_fireworks_has_qwen_coder():
    from velune.providers.adapters.fireworks import FIREWORKS_MODELS
    ids = " ".join(m.model_id.lower() for m in FIREWORKS_MODELS)
    assert "qwen" in ids

def test_together_costs_in_tracker():
    from velune.telemetry.token_tracker import PROVIDER_COSTS
    assert "together" in PROVIDER_COSTS
    assert "fireworks" in PROVIDER_COSTS

def test_together_env_var_registered():
    from velune.providers.keystore import PROVIDER_ENV_VARS
    assert "together" in PROVIDER_ENV_VARS
    assert "fireworks" in PROVIDER_ENV_VARS

# ── Token tracker with new providers ──────────────────────────────────

def test_together_cost_calculation():
    from velune.telemetry.token_tracker import TokenUsage
    model_id = "meta-llama/Llama-3.3-70B-Instruct-Turbo"
    u = TokenUsage.from_response("together", model_id, 1000, 500)
    assert u.cost_usd == pytest.approx((1500 / 1000) * 0.00088, rel=1e-4)

def test_fireworks_cost_calculation():
    from velune.telemetry.token_tracker import TokenUsage
    model_id = "accounts/fireworks/models/deepseek-r1"
    u = TokenUsage.from_response("fireworks", model_id, 1000, 500)
    assert u.cost_usd == pytest.approx((1500 / 1000) * 0.003, rel=1e-4)
