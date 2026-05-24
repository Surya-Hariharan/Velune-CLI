"""Comprehensive unit tests for the Velune Personality Style & Repository Identity (Phase 4)."""

from __future__ import annotations

import os
import shutil
import sqlite3
import time
from pathlib import Path
import pytest

from velune.cognition.personality import RepositoryPersonalityAgent
from velune.memory.tiers.lineage import LineageMemoryTier
from velune.cognition.council.coder import CoderAgent
from velune.models.specializations import CouncilRole
from velune.core.types.model import ModelDescriptor, ModelCapabilityProfile, CapabilityLevel
from velune.providers.base import ModelProvider
from velune.core.types.inference import InferenceRequest, InferenceResponse


# =====================================================================
# 1. Naming Convention, Type-Hinting, Docstring & Paradigm Analysis Tests
# =====================================================================

def test_ast_personality_style_extraction(tmp_path):
    subsystem_dir = tmp_path / "mock_personality"
    subsystem_dir.mkdir()

    # Create module_a with:
    # - PascalCase classes: 1 (PascalCaseClass)
    # - snake_case functions: 2 (do_this_task, do_that_task)
    # - camelCase functions: 0
    # - google-style docstrings (contains Args: and Returns:)
    # - 100% type hinting strictness (all parameters and returns annotated)
    file_a = subsystem_dir / "module_a.py"
    file_a.write_text("""
class PascalCaseClass:
    \"\"\"Cohesive class.
    
    Args:
        x: An integer.
    Returns:
        None.
    \"\"\"
    def __init__(self, x: int) -> None:
        self.x: int = x

    def do_this_task(self, param: str) -> bool:
        return True

def do_that_task(val: float) -> int:
    return int(val)
""", encoding="utf-8")

    # Create module_b with:
    # - camelCase functions: 3 (doMyJob, fetchInfo, printLog)
    # - sphinx-style docstrings (contains :param and :return:)
    # - 0% type hinting (no annotations)
    file_b = subsystem_dir / "module_b.py"
    file_b.write_text("""
def doMyJob(x, y):
    \"\"\"Runs the job.
    
    :param x: first param
    :param y: second param
    :return: status
    \"\"\"
    return x + y

def fetchInfo(url):
    return url

def printLog(msg):
    print(msg)
""", encoding="utf-8")

    agent = RepositoryPersonalityAgent(workspace_root=str(tmp_path))

    # Analyze file_a directory
    # module_a: PascalCaseClass (Pascal), do_this_task (snake), do_that_task (snake). Dominant is snake/Pascal.
    profile_a = agent.analyze_directory_style(str(subsystem_dir))
    
    # Assert naming conventions breakdown exists
    assert "snake_case" in profile_a["naming_conventions"]["breakdown"]
    assert "camelCase" in profile_a["naming_conventions"]["breakdown"]
    
    # Assert type hinting strictness calculation is correct
    # module_a has:
    # PascalCaseClass.__init__: self (ignored), x (annotated) -> 1/1 param, return annotated (-> None) -> 1/1 return. Total annotated=2, total=2
    # do_this_task: self (ignored), param (annotated) -> 1/1 param, return annotated (-> bool) -> 1/1 return. Total annotated=2, total=2
    # do_that_task: val (annotated) -> 1/1 param, return annotated (-> int) -> 1/1 return. Total annotated=2, total=2
    # Total A annotated = 6, total = 6.
    # module_b has:
    # doMyJob: x (none), y (none) -> 0/2 param, return none -> 0/1 return. Total = 3
    # fetchInfo: url (none) -> 0/1 param, return none -> 0/1 return. Total = 2
    # printLog: msg (none) -> 0/1 param, return none -> 0/1 return. Total = 2
    # Total B annotated = 0, total = 7.
    # Combined annotated = 6, total = 13. strictness = 6/13 = 0.462
    assert 0.4 <= profile_a["type_hinting_strictness"] <= 0.5

    # Assert programming paradigm classification
    # 1 class, 5 functions total (2 in module_a, 3 in module_b). Since top functions > classes * 2, it should be Functional.
    assert profile_a["class_vs_functional"] == "Functional"

    # Assert docstrings classification
    # module_a is Google, module_b is Sphinx. Google count = 1, Sphinx count = 1. Falling back to Google.
    assert profile_a["docstring_style"] == "Google"

    # Assert imports preferred constructs mining
    # Let's add imports to module_a
    file_c = subsystem_dir / "module_c.py"
    file_c.write_text("""
import asyncio
import logging
from pydantic import BaseModel
""", encoding="utf-8")
    
    profile_c = agent.analyze_directory_style(str(subsystem_dir))
    assert "asyncio" in profile_c["preferred_constructs"]
    assert "logging" in profile_c["preferred_constructs"]
    assert "pydantic" in profile_c["preferred_constructs"]


# =====================================================================
# 2. Database Persistence Test
# =====================================================================

def test_db_personality_styles_persistence(tmp_path):
    db_file = tmp_path / "test_lineage.db"
    lineage = LineageMemoryTier(db_path=db_file)

    naming_conventions = {
        "dominant": "snake_case",
        "breakdown": {"snake_case": 10, "camelCase": 0, "PascalCase": 1, "UPPER_CASE": 2}
    }
    preferred = ["asyncio", "sqlite3"]

    # Save to SQLite
    lineage.save_personality_style(
        subsystem="velune/cognition",
        naming_conventions=naming_conventions,
        type_hinting_strictness=0.95,
        preferred_constructs=preferred,
        class_vs_functional="OOP",
        docstring_style="Google",
    )

    # Allow write queue to process
    time.sleep(0.2)

    # Get from SQLite
    profile = lineage.get_personality_style("velune/cognition")
    assert profile is not None
    assert profile["subsystem"] == "velune/cognition"
    assert profile["naming_conventions"]["dominant"] == "snake_case"
    assert profile["type_hinting_strictness"] == 0.95
    assert "asyncio" in profile["preferred_constructs"]
    assert profile["class_vs_functional"] == "OOP"
    assert profile["docstring_style"] == "Google"

    lineage.shutdown()


# =====================================================================
# 3. Coder Agent Style Enforcement Injection Test
# =====================================================================

class MockProvider(ModelProvider):
    """Mock Provider to capture Coder prompt contents."""
    
    def __init__(self) -> None:
        self.last_prompt = ""

    @property
    def provider_id(self) -> str:
        return "mock"

    async def list_models(self) -> list[ModelDescriptor]:
        return []

    async def infer(self, request: InferenceRequest) -> InferenceResponse:
        self.last_prompt = request.messages[-1]["content"]
        return InferenceResponse(
            content="class NewFeature:\n    pass",
            model_id=request.model_id,
            finish_reason="stop",
            tokens_used=10,
            latency_ms=1.5,
        )


@pytest.mark.anyio
async def test_coder_style_enforcement_prompt_injection():
    model = ModelDescriptor(
        id="mock-coder",
        provider="mock",
        name="Mock Coder",
        context_window=4096,
        is_local=False,
        capabilities=ModelCapabilityProfile(coding=CapabilityLevel.ADVANCED),
        speed_tier="fast",
    )
    provider = MockProvider()
    
    coder = CoderAgent(model=model, provider=provider)
    
    style_profile = {
        "naming_conventions": {
            "dominant": "snake_case",
            "breakdown": {"snake_case": 12, "camelCase": 1}
        },
        "type_hinting_strictness": 0.85,
        "class_vs_functional": "OOP",
        "docstring_style": "Google",
        "preferred_constructs": ["asyncio", "logging"]
    }

    await coder.write_code(
        prompt="Implement event listener in event.py",
        current_code="class EventBus:\n    pass",
        plan_context="Plan Step 1",
        style_profile=style_profile
    )

    prompt_content = provider.last_prompt
    assert "[COGNITIVE STYLE ENFORCEMENT]" in prompt_content
    assert "Dominant style is `snake_case`" in prompt_content
    assert "Type Hinting Strictness" in prompt_content
    assert "Preferred style is `OOP`" in prompt_content
    assert "Use `Google` format" in prompt_content
    assert "asyncio, logging" in prompt_content
