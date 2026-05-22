import pytest
import asyncio
from pathlib import Path
from velune.core.types.model import CapabilityLevel, ModelCapabilityProfile, ModelDescriptor
from velune.core.types.inference import InferenceRequest, InferenceResponse
from velune.core.types.provider import ProviderCapabilities, ProviderHealth
from velune.providers.base import ModelProvider
from velune.providers.registry import ProviderRegistry
from velune.models.registry import ModelCapabilityRegistry
from velune.models.specializations import ModelSpecializationMapper, CouncilRole
from velune.cognition.orchestrator import CouncilOrchestrator
from velune.execution.executor import ExecutionExecutor
from velune.core.types.task import TaskPlan, TaskStep, TaskStatus

class MockModelProvider(ModelProvider):
    def __init__(self, provider_id: str):
        self._provider_id = provider_id
        self.inferences = []

    @property
    def provider_id(self) -> str:
        return self._provider_id

    async def list_models(self) -> list[ModelDescriptor]:
        return []

    async def infer(self, request: InferenceRequest) -> InferenceResponse:
        self.inferences.append(request)
        system_prompt = next((msg["content"] for msg in request.messages if msg["role"] == "system"), "")
        
        if "Lead Planner" in system_prompt:
            content = """
            {
              "task_id": "integration-task-001",
              "steps": [
                {
                  "id": "create_temp_file",
                  "description": "Create a temporary verification file",
                  "agent_role": "coder",
                  "dependencies": [],
                  "metadata": {
                    "command": "python -c \\"open('temp_verified.txt', 'w').write('Verified')\\"",
                    "expected_files": ["temp_verified.txt"],
                    "syntax_check_files": [],
                    "timeout": 15.0
                  }
                }
              ]
            }
            """
        elif "Lead Coder" in system_prompt:
            content = "def hello():\n    return 'Velune'"
        elif "Senior Code Reviewer" in system_prompt:
            content = """
            {
              "passed": true,
              "critical_issues": [],
              "suggestions": ["Code looks perfectly clean and secure."],
              "confidence_rating": 0.98
            }
            """
        elif "Adversarial Challenger" in system_prompt:
            content = """
            {
              "assumptions_challenged": ["Assumed python is available in sandbox."],
              "failure_vectors": [],
              "severity_rating": 0.05
            }
            """
        elif "Lead Synthesizer" in system_prompt:
            content = "Successfully compiled, audited, and resolved. Implementation is robust and safe."
        else:
            content = "Default mock response"

        return InferenceResponse(
            content=content,
            model_id=request.model_id,
            finish_reason="stop",
            tokens_used=150,
            latency_ms=12.5
        )

    async def stream(self, request: InferenceRequest):
        raise NotImplementedError()

    async def embed(self, texts: list[str], model_id: str) -> list[list[float]]:
        return [[0.5] * 128 for _ in texts]

    async def health_check(self) -> ProviderHealth:
        return ProviderHealth(healthy=True)

    def get_capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities()

    async def initialize(self) -> None:
        pass

    async def shutdown(self) -> None:
        pass


@pytest.mark.asyncio
async def test_end_to_end_cognitive_flow(tmp_path):
    # 1. Setup Providers & Model Registry
    provider_registry = ProviderRegistry(config=None)
    mock_provider = MockModelProvider("mock-provider")
    provider_registry.register("mock-provider", mock_provider)

    model_registry = ModelCapabilityRegistry(scanner=None)
    
    # Register advanced model for reasoning
    adv_model = ModelDescriptor(
        id="advanced-brain",
        provider="mock-provider",
        name="Advanced Brain",
        context_window=32768,
        is_local=False,
        capabilities=ModelCapabilityProfile(
            coding=CapabilityLevel.EXPERT,
            reasoning=CapabilityLevel.EXPERT,
            planning=CapabilityLevel.ADVANCED,
            summarization=CapabilityLevel.ADVANCED
        ),
        speed_tier="fast"
    )
    model_registry.register(adv_model)

    # 2. Setup Specialization Mapper & Verify Council Seats
    mapper = ModelSpecializationMapper(registry=model_registry)
    mappings = mapper.map_roles()
    
    for role in CouncilRole:
        assert role in mappings
        assert mappings[role].model_id == "advanced-brain"

    # 3. Setup Council Orchestrator & Run Deliberation
    orchestrator = CouncilOrchestrator(
        provider_registry=provider_registry,
        mapper=mapper,
        historical_accuracy=0.9
    )
    
    prompt = "Create a temporary verification file that writes 'Verified'"
    repo_context = "# Workspace root initialization\n"
    
    result = await orchestrator.execute_task(prompt, repo_context)
    
    assert "task_plan" in result
    assert "coder_proposal" in result
    assert "reviewer_report" in result
    assert "challenger_report" in result
    assert "arbitration" in result
    assert "final_summary" in result

    # Check parsed outputs from the deliberation flow
    task_plan = result["task_plan"]
    assert isinstance(task_plan, TaskPlan)
    assert task_plan.task_id == "integration-task-001"
    assert len(task_plan.steps) == 1
    assert task_plan.steps[0].id == "create_temp_file"

    assert "def hello():" in result["coder_proposal"]
    assert result["reviewer_report"]["passed"] is True
    assert result["challenger_report"]["severity_rating"] == 0.05
    assert not result["arbitration"]["requires_human_review"]
    assert "Successfully compiled" in result["final_summary"]

    # 4. Compile and Run the Execution Plan in Sandbox
    executor = ExecutionExecutor(workspace_path=tmp_path)
    
    # Execute the generated plan
    exec_result = await executor.execute_plan(task_plan)
    
    # Assert successful subprocess sandbox execution
    assert exec_result.success
    assert exec_result.steps_completed == 1
    
    # Check that file was actually written to the temp workspace!
    target_file = tmp_path / "temp_verified.txt"
    assert target_file.exists()
    assert target_file.read_text().strip() == "Verified"
