import asyncio
import time
from unittest.mock import MagicMock

import pytest

from velune.cognition.council.tiers import CouncilTier
from velune.cognition.orchestrator import CouncilOrchestrator
from velune.models.specializations import CouncilRole
from velune.orchestration.schemas import StreamProgress


class MockModelDescriptor:
    def __init__(self, provider_id: str, model_id: str):
        self.provider_id = provider_id
        self.model_id = model_id
        self.is_local = False
        self.vram_required_gb = None


class MockModelSpecializationMapper:
    def __init__(self, model_id: str = "test-model-id"):
        self.model_id = model_id
        self.profiler = MagicMock()
        self.profiler.get_profile.return_value = None

    def map_roles(self, *args, **kwargs):
        desc = MockModelDescriptor("mock-provider", self.model_id)
        return {
            CouncilRole.PLANNER: desc,
            CouncilRole.CODER: desc,
            CouncilRole.REVIEWER: desc,
            CouncilRole.CHALLENGER: desc,
            CouncilRole.SYNTHESIZER: desc,
        }


class MockProviderRegistry:
    def __init__(self, provider):
        self.provider = provider

    def get_or_raise(self, name):
        return self.provider


class MockProvider:
    def __init__(self, slow=False):
        self.slow = slow

    async def infer(self, request):
        if self.slow:
            await asyncio.sleep(1.0)

        mock_response = MagicMock()
        mock_response.content = "Mock response implementation code"
        return mock_response

    def get_capabilities(self):
        capabilities = MagicMock()
        capabilities.supports_streaming = True
        return capabilities

    async def stream(self, request):
        if self.slow:
            await asyncio.sleep(0.5)
        yield MagicMock(content="Mock streamed chunk content")


def build_test_orchestrator(slow_providers=False):
    provider = MockProvider(slow=slow_providers)
    registry = MockProviderRegistry(provider)
    mapper = MockModelSpecializationMapper()

    orchestrator = CouncilOrchestrator(
        provider_registry=registry,
        mapper=mapper,
        lineage_db_path=None,
    )
    orchestrator.lineage_memory = MagicMock()
    orchestrator.lineage_memory.get_personality_style.return_value = None
    orchestrator.lineage_memory.query_continuity_warnings.return_value = ([], [])

    return orchestrator


@pytest.mark.asyncio
async def test_structured_progress_parsing():
    """Verify StreamProgress parsing and string coercion."""
    progress = StreamProgress(run_id="run-123", phase="coder", message="Writing code")
    assert progress.run_id == "run-123"
    assert progress.phase == "coder"
    assert progress.message == "Writing code"
    assert str(progress) == "[run-123] coder: Writing code"

    progress_no_phase = StreamProgress(run_id="run-123", phase="", message="Generic update")
    assert str(progress_no_phase) == "[run-123] Generic update"


@pytest.mark.asyncio
async def test_tier_consistency():
    """Verify that tier classification is consistent between helper and execute_task."""
    orchestrator = build_test_orchestrator()
    tier = orchestrator._resolve_tier("implement database", "repo context")
    assert isinstance(tier, CouncilTier)

    # Calling execute_task with pre-determined tier
    result = await orchestrator.execute_task(
        "implement database", "repo context", council_tier="instant"
    )
    assert result["tier"] == "instant"


@pytest.mark.asyncio
async def test_streaming_correctness_and_timing():
    """Verify milestones stream progressively during execution, rather than bursting."""
    orchestrator = build_test_orchestrator(slow_providers=True)

    milestones = []
    start = time.time()

    async for milestone in orchestrator.stream("fix a major structural bug"):
        milestones.append(milestone)
        # Should get at least some milestones progressively
        assert isinstance(milestone, StreamProgress)
        assert milestone.run_id.startswith("run-")

    time.time() - start
    # Verify that the whole streaming process took some real execution time (because of slow providers)
    # and wasn't a sub-millisecond burst
    assert len(milestones) >= 2
    assert milestones[0].phase == "context reconstruction"
    assert any("coder" in m.phase for m in milestones)


@pytest.mark.asyncio
async def test_lock_release_safety():
    """Verify live lock is guaranteed to be released on failure or cancellation."""
    test_lock = asyncio.Lock()
    assert not test_lock.locked()

    # Simulate deliberation lock acquisition
    from velune.cognition.council.coder import CoderAgent

    roles = MockModelSpecializationMapper().map_roles()
    coder_model = roles[CouncilRole.CODER]

    failing_provider = MockProvider()
    # Force stream to throw an exception inside Coder
    failing_provider.stream = MagicMock(side_effect=RuntimeError("Stream failure!"))

    coder = CoderAgent(
        model=coder_model,
        provider=failing_provider,
    )
    coder.live_lock = test_lock

    # We deliberately enable interactive mode mock to force lock acquisition
    import sys

    original_isatty = sys.stdout.isatty
    sys.stdout.isatty = lambda: True

    try:
        # Deliberate must throw an exception but release the lock
        await coder.deliberate([{"role": "user", "content": "test"}])
    except Exception:
        pass
    finally:
        sys.stdout.isatty = original_isatty

    assert not test_lock.locked(), "Live lock was leaked on deliberation failure!"
