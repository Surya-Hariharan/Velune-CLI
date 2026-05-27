import pytest
from unittest.mock import AsyncMock, MagicMock
from velune.planning.service import AdaptivePlanningService

@pytest.mark.asyncio
async def test_llm_plan_dependencies_remapped():
    """Dependencies from LLM must reference correctly prefixed IDs."""
    service = AdaptivePlanningService()
    
    # Mock LLM response with dependencies
    mock_provider = AsyncMock()
    mock_response = MagicMock()
    mock_response.content = '''[
        {"id": "step-1", "description": "Write code", "agent_role": "coder", "dependencies": []},
        {"id": "step-2", "description": "Run tests", "agent_role": "reviewer", "dependencies": ["step-1"]},
        {"id": "step-3", "description": "Record", "agent_role": "memory", "dependencies": ["step-2"]}
    ]'''
    mock_provider.infer = AsyncMock(return_value=mock_response)
    
    plan = await service.create_plan_with_llm(
        task_id="fix-auth",
        prompt="fix auth bug",
        provider=mock_provider,
        model_id="test-model",
    )
    
    step_ids = {s.id for s in plan.steps}
    
    # All dependency references must exist in the plan
    for step in plan.steps:
        for dep in step.dependencies:
            assert dep in step_ids, f"Dependency '{dep}' not in step IDs: {step_ids}"
    
    # Step 2 must depend on step 1
    step_2 = next(s for s in plan.steps if s.description == "Run tests")
    step_1 = next(s for s in plan.steps if s.description == "Write code")
    assert step_1.id in step_2.dependencies

@pytest.mark.asyncio
async def test_unmapped_deps_logged(caplog):
    """Unknown dependency references must generate warnings, not crashes."""
    import logging
    service = AdaptivePlanningService()
    mock_provider = AsyncMock()
    mock_response = MagicMock()
    # LLM references a step ID that doesn't exist
    mock_response.content = '''[
        {"id": "step-1", "description": "Do thing", "agent_role": "coder",
         "dependencies": ["nonexistent-step"]}
    ]'''
    mock_provider.infer = AsyncMock(return_value=mock_response)
    
    with caplog.at_level(logging.WARNING):
        plan = await service.create_plan_with_llm(
            task_id="test", prompt="test", provider=mock_provider, model_id="m"
        )
    
    # Must not crash
    assert len(plan.steps) == 1
    # Must log warning
    assert any("nonexistent-step" in r.message for r in caplog.records)
