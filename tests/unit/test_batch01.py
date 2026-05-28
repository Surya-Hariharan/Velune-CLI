"""Batch 01 production remediation unit tests."""

import pytest


# ---------------------------------------------------------------------------
# Fix 1 — VeluneMemoryError must NOT shadow Python's built-in MemoryError
# ---------------------------------------------------------------------------

class TestMemoryErrorRename:
    def test_memory_error_does_not_shadow_builtin(self):
        """VeluneMemoryError must not be caught by `except MemoryError` (builtin)."""
        from velune.core.errors.memory import VeluneMemoryError

        caught_by_builtin = False
        try:
            raise VeluneMemoryError("velune memory failure")
        except MemoryError:
            # Python's built-in MemoryError should NOT catch VeluneMemoryError
            caught_by_builtin = True
        except VeluneMemoryError:
            pass

        assert not caught_by_builtin, (
            "VeluneMemoryError was incorrectly caught by the built-in MemoryError handler. "
            "The class must NOT inherit from Python's built-in MemoryError."
        )

    def test_velune_memory_error_importable(self):
        """VeluneMemoryError and subclasses are importable from errors module."""
        from velune.core.errors import (  # noqa: F401
            VeluneMemoryConsolidationError,
            VeluneMemoryError,
            VeluneMemoryRetrievalError,
            VeluneMemoryStoreError,
        )

    def test_velune_memory_subclass_hierarchy(self):
        """Subclasses must still inherit from VeluneMemoryError, not from builtins."""
        from velune.core.errors.memory import (
            VeluneMemoryConsolidationError,
            VeluneMemoryError,
            VeluneMemoryRetrievalError,
            VeluneMemoryStoreError,
        )
        assert issubclass(VeluneMemoryStoreError, VeluneMemoryError)
        assert issubclass(VeluneMemoryRetrievalError, VeluneMemoryError)
        assert issubclass(VeluneMemoryConsolidationError, VeluneMemoryError)
        # None must be a subclass of the built-in MemoryError
        assert not issubclass(VeluneMemoryError, MemoryError)


# ---------------------------------------------------------------------------
# Fix 4 — CapabilityLevel must have exactly 5 members, no alias duplicates
# ---------------------------------------------------------------------------

class TestCapabilityLevelNoAliases:
    def test_capability_level_no_aliases(self):
        """CapabilityLevel must have exactly 5 unique members: NONE, BASIC, INTERMEDIATE, ADVANCED, EXPERT."""
        from velune.core.types.model import CapabilityLevel

        expected = {"NONE", "BASIC", "INTERMEDIATE", "ADVANCED", "EXPERT"}
        actual = {member.name for member in CapabilityLevel}
        assert actual == expected, (
            f"CapabilityLevel has unexpected members. "
            f"Expected {expected}, got {actual}. "
            f"Aliases CAPABLE/STRONG/EXCEPTIONAL must be removed."
        )

    def test_capability_level_member_count(self):
        """CapabilityLevel must have exactly 5 members (no alias duplicates inflate the count)."""
        from velune.core.types.model import CapabilityLevel

        assert len(CapabilityLevel) == 5, (
            f"Expected 5 CapabilityLevel members, got {len(CapabilityLevel)}. "
            f"Enum aliases (CAPABLE/STRONG/EXCEPTIONAL) must be removed."
        )

    def test_no_capable_attribute(self):
        """CAPABLE alias must not exist on CapabilityLevel."""
        from velune.core.types.model import CapabilityLevel

        assert not hasattr(CapabilityLevel, "CAPABLE"), "CapabilityLevel.CAPABLE alias must be removed"

    def test_no_strong_attribute(self):
        """STRONG alias must not exist on CapabilityLevel."""
        from velune.core.types.model import CapabilityLevel

        assert not hasattr(CapabilityLevel, "STRONG"), "CapabilityLevel.STRONG alias must be removed"

    def test_no_exceptional_attribute(self):
        """EXCEPTIONAL alias must not exist on CapabilityLevel."""
        from velune.core.types.model import CapabilityLevel

        assert not hasattr(CapabilityLevel, "EXCEPTIONAL"), "CapabilityLevel.EXCEPTIONAL alias must be removed"


# ---------------------------------------------------------------------------
# Fix 4 — Comparison ordering must be correct after alias removal
# ---------------------------------------------------------------------------

class TestCapabilityComparisons:
    def test_capability_comparisons_correct(self):
        """Verify INTERMEDIATE < ADVANCED < EXPERT ordering."""
        from velune.core.types.model import CapabilityLevel

        assert CapabilityLevel.INTERMEDIATE < CapabilityLevel.ADVANCED
        assert CapabilityLevel.ADVANCED < CapabilityLevel.EXPERT
        assert CapabilityLevel.INTERMEDIATE < CapabilityLevel.EXPERT

    def test_none_is_lowest(self):
        """NONE must be the lowest value."""
        from velune.core.types.model import CapabilityLevel

        assert CapabilityLevel.NONE < CapabilityLevel.BASIC
        assert CapabilityLevel.NONE < CapabilityLevel.INTERMEDIATE

    def test_values_are_stable(self):
        """Integer values must be exactly 0–4 in order."""
        from velune.core.types.model import CapabilityLevel

        assert CapabilityLevel.NONE == 0
        assert CapabilityLevel.BASIC == 1
        assert CapabilityLevel.INTERMEDIATE == 2
        assert CapabilityLevel.ADVANCED == 3
        assert CapabilityLevel.EXPERT == 4


# ---------------------------------------------------------------------------
# Fix 3 — BaseTool.validate_input must be a no-op that returns None safely
# ---------------------------------------------------------------------------

class TestValidateInputNoOp:
    def test_validate_input_no_op_is_safe(self):
        """BaseTool.validate_input(payload) must return None without error."""
        from velune.tools.base.tool import BaseTool

        class _ConcreteTool(BaseTool):
            def get_name(self) -> str:
                return "test_tool"

            def get_description(self) -> str:
                return "A test tool"

            async def execute(self, **kwargs):
                return {}

        tool = _ConcreteTool()
        result = tool.validate_input({"key": "val"})
        assert result is None, "validate_input must return None"

    def test_validate_input_does_not_mutate_payload(self):
        """validate_input must not delete or modify the payload dict."""
        from velune.tools.base.tool import BaseTool

        class _ConcreteTool(BaseTool):
            def get_name(self) -> str:
                return "test_tool"

            def get_description(self) -> str:
                return "A test tool"

            async def execute(self, **kwargs):
                return {}

        tool = _ConcreteTool()
        payload = {"key": "val", "count": 42}
        tool.validate_input(payload)
        # The dict must still be accessible in the caller's scope
        assert payload == {"key": "val", "count": 42}, (
            "validate_input must not delete or mutate the caller's payload dict."
        )


# ---------------------------------------------------------------------------
# Fix 8 — ServiceContainer hot-swap for lazy singleton factories
# ---------------------------------------------------------------------------

class TestServiceContainerHotSwap:
    def test_hot_swap_singleton_factory_clears_cache(self):
        """ServiceContainer.hot_swap must clear cached singleton factory instances in _services."""
        from velune.kernel.registry import ServiceContainer

        container = ServiceContainer()
        call_count = 0

        def factory():
            nonlocal call_count
            call_count += 1
            return f"instance_{call_count}"

        # Register lazy singleton factory
        container.register("my_service", factory, singleton=True)

        # Retrieve first time, which populates the cache in _services
        instance1 = container.get("my_service")
        assert instance1 == "instance_1"
        assert call_count == 1

        # Retrieve second time, must be same cached instance without invoking factory again
        instance2 = container.get("my_service")
        assert instance2 == "instance_1"
        assert call_count == 1

        # Hot-swap the service
        container.hot_swap("my_service", "replacement_value")

        # Retrieve after hot-swap, must return the replacement value
        swapped_instance = container.get("my_service")
        assert swapped_instance == "replacement_value"

    def test_hot_swap_non_existent_service(self):
        """hot_swap must register service from scratch if it doesn't exist."""
        from velune.kernel.registry import ServiceContainer

        container = ServiceContainer()
        assert not container.has("new_service")

        container.hot_swap("new_service", "value")
        assert container.has("new_service")
        assert container.get("new_service") == "value"


# ---------------------------------------------------------------------------
# Fix 10 — Council Tier Ceiling Configuration and MINIMAL Routing Tier
# ---------------------------------------------------------------------------

class TestCouncilTierRouting:
    def test_minimal_tier_classification(self):
        """classify_task_tier must classify simple tweak/typo tasks as MINIMAL."""
        from velune.cognition.council.tiers import classify_task_tier, CouncilTier

        tier = classify_task_tier("fix typo in main.py", "context")
        assert tier == CouncilTier.MINIMAL

        # Short comment change
        tier = classify_task_tier("tweak some comment", "context")
        assert tier == CouncilTier.MINIMAL

    def test_max_tier_ceiling(self):
        """classify_task_tier must respect max_council_tier ceiling option."""
        from velune.cognition.council.tiers import classify_task_tier, CouncilTier

        # Complex prompt typically classified as FULL
        tier = classify_task_tier("refactor concurrent database async", "context", max_council_tier="standard")
        assert tier == CouncilTier.STANDARD

        # When max is instant
        tier = classify_task_tier("refactor concurrent database async", "context", max_council_tier="instant")
        assert tier == CouncilTier.INSTANT

    def test_default_tier_override(self):
        """classify_task_tier must respect default_tier_override option if not auto."""
        from velune.cognition.council.tiers import classify_task_tier, CouncilTier

        tier = classify_task_tier("tweak some comment", "context", default_tier_override="standard")
        assert tier == CouncilTier.STANDARD

        tier = classify_task_tier("refactor concurrent database async", "context", default_tier_override="minimal")
        assert tier == CouncilTier.MINIMAL

    def test_queue_depth_downgrade(self):
        """classify_task_tier must downgrade task tier if queue depth is high to prevent starvation."""
        from velune.cognition.council.tiers import classify_task_tier, CouncilTier

        # Complex prompt classified as STANDARD when queue depth is high
        tier = classify_task_tier("refactor concurrent database async", "context", queue_depth=5)
        assert tier == CouncilTier.STANDARD

        # Fast model classified as STANDARD when queue depth is high
        tier = classify_task_tier("some normal task", "context", available_tps=50.0, queue_depth=2)
        assert tier == CouncilTier.STANDARD

    @pytest.mark.asyncio
    async def test_execute_minimal_tier_in_orchestrator(self):
        """Orchestrator must execute the MINIMAL tier with Planner + Coder only and no Reviewer."""
        from tests.unit.test_council_orchestrator import build_test_orchestrator
        from velune.cognition.council.tiers import CouncilTier

        orchestrator = build_test_orchestrator()
        result = await orchestrator.execute_task("fix typo in main.py", "context", council_tier="minimal")

        assert result["tier"] == "minimal"
        assert result["task_plan"] is not None
        assert result["coder_proposal"] is not None
        assert result["reviewer_report"] is None
        assert result["challenger_report"] is None
