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
