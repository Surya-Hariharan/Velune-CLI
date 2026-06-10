"""Routing correctness regression tests.

These tests guard against CapabilityLevel enum aliasing and silent mis-routing.
All are marked @pytest.mark.critical so they run first and block CI on failure.
"""

from unittest.mock import MagicMock

import pytest

from velune.core.types.model import CapabilityLevel, ModelCapabilityProfile, ModelDescriptor
from velune.providers.router import ProviderRouter


def _make_model(
    model_id: str,
    coding: CapabilityLevel = CapabilityLevel.NONE,
) -> ModelDescriptor:
    return ModelDescriptor(
        model_id=model_id,
        provider_id="mock",
        display_name=model_id,
        context_length=8192,
        capabilities=ModelCapabilityProfile(coding=coding),
    )


@pytest.mark.critical
def test_capability_level_ordering():
    """NONE < BASIC < INTERMEDIATE < ADVANCED < EXPERT — strict total order."""
    assert CapabilityLevel.NONE < CapabilityLevel.BASIC
    assert CapabilityLevel.BASIC < CapabilityLevel.INTERMEDIATE
    assert CapabilityLevel.INTERMEDIATE < CapabilityLevel.ADVANCED
    assert CapabilityLevel.ADVANCED < CapabilityLevel.EXPERT


@pytest.mark.critical
def test_capability_level_all_values_unique():
    """No two CapabilityLevel members may share the same integer value."""
    values = [member.value for member in CapabilityLevel]
    assert len(values) == len(set(values)), (
        f"Duplicate integer values detected in CapabilityLevel: {values}"
    )


@pytest.mark.critical
def test_advanced_model_does_not_qualify_for_expert_task():
    """An ADVANCED-tier model must NOT pass the EXPERT capability gate."""
    registry = MagicMock()
    router = ProviderRouter(registry)

    advanced_model = _make_model("adv-model", coding=CapabilityLevel.ADVANCED)
    result = router.route_task(
        task_category="coding",
        models_list=[advanced_model],
        min_level=CapabilityLevel.EXPERT,
    )
    assert result is None, (
        "ADVANCED model must not qualify for an EXPERT task — "
        "got a model back when None was expected"
    )


@pytest.mark.critical
def test_expert_model_qualifies_for_advanced_task():
    """An EXPERT-tier model must pass an ADVANCED capability gate."""
    registry = MagicMock()
    router = ProviderRouter(registry)

    expert_model = _make_model("exp-model", coding=CapabilityLevel.EXPERT)
    result = router.route_task(
        task_category="coding",
        models_list=[expert_model],
        min_level=CapabilityLevel.ADVANCED,
    )
    assert result is not None, "EXPERT model must qualify for an ADVANCED task"
    assert result.model_id == "exp-model"


@pytest.mark.critical
def test_route_task_returns_none_when_only_advanced_available_for_expert():
    """route_task must return None — not silently fall back — when min_level=EXPERT
    and the only available model is ADVANCED."""
    registry = MagicMock()
    router = ProviderRouter(registry)

    advanced_only = [_make_model("adv-only", coding=CapabilityLevel.ADVANCED)]
    result = router.route_task(
        task_category="coding",
        models_list=advanced_only,
        min_level=CapabilityLevel.EXPERT,
    )
    assert result is None, (
        "ProviderRouter.route_task() must return None when no model meets "
        "min_level=EXPERT — silent fallback to ADVANCED is forbidden"
    )
