"""Configuration unification and startup validation tests.

Guards three properties:
1. VeluneConfig is defined in exactly one location.
2. Every re-export path resolves to that same class object.
3. validate() correctly catches missing/broken configuration at startup.
"""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest
from pydantic_settings import BaseSettings

from velune.kernel.config import (
    MemoryConfig,
    ProviderEntry,
    ProvidersConfig,
    VeluneConfig,
    WorkspaceConfig,
)
from velune.kernel.lifecycle import LifecycleCoordinator

# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def _make_config(**kwargs) -> VeluneConfig:
    """Construct VeluneConfig while suppressing .env file and env-var leakage."""
    # Pass explicit kwargs — constructor args have highest priority in BaseSettings,
    # so they override any stray VELUNE_* env vars in the test environment.
    return VeluneConfig(**kwargs)


# ---------------------------------------------------------------------------
# Test 1: VeluneConfig has exactly one canonical definition
# ---------------------------------------------------------------------------


def test_velune_config_defined_in_single_module():
    """VeluneConfig.__module__ must point to velune.kernel.config and nowhere else."""
    assert VeluneConfig.__module__ == "velune.kernel.config", (
        f"VeluneConfig is defined in '{VeluneConfig.__module__}', expected 'velune.kernel.config'."
    )


# ---------------------------------------------------------------------------
# Test 2: All re-export paths resolve to the identical class object
# ---------------------------------------------------------------------------


def test_all_import_paths_resolve_to_same_class():
    """Every public re-export of VeluneConfig must be the same object."""
    from velune.core import VeluneConfig as FromCore
    from velune.core.config import VeluneConfig as FromCoreConfig
    from velune.kernel import VeluneConfig as FromKernel
    from velune.kernel.config import VeluneConfig as FromConfig

    assert FromConfig is FromKernel, (
        "velune.kernel.VeluneConfig is not the same object as velune.kernel.config.VeluneConfig"
    )
    assert FromConfig is FromCoreConfig, (
        "velune.core.config.VeluneConfig is not the same object as velune.kernel.config.VeluneConfig"
    )
    assert FromConfig is FromCore, (
        "velune.core.VeluneConfig is not the same object as velune.kernel.config.VeluneConfig"
    )


# ---------------------------------------------------------------------------
# Test 3: VeluneConfig is a pydantic-settings BaseSettings subclass
# ---------------------------------------------------------------------------


def test_velune_config_is_base_settings():
    """VeluneConfig must inherit from pydantic_settings.BaseSettings."""
    assert issubclass(VeluneConfig, BaseSettings), (
        "VeluneConfig does not inherit from pydantic_settings.BaseSettings. "
        "Add BaseSettings as the base class with SettingsConfigDict."
    )


def test_velune_config_has_env_prefix():
    """model_config must declare env_prefix='VELUNE_'."""
    prefix = VeluneConfig.model_config.get("env_prefix", "")
    assert prefix == "VELUNE_", (
        f"Expected env_prefix='VELUNE_', got '{prefix}'. Update SettingsConfigDict in VeluneConfig."
    )


# ---------------------------------------------------------------------------
# Test 4: validate() — happy path (local provider, no API key required)
# ---------------------------------------------------------------------------


def test_validate_returns_empty_for_local_provider():
    """A config pointing at a local provider (ollama) with no api_key_env must pass cleanly."""
    config = _make_config(providers=ProvidersConfig(default_provider="ollama"))
    errors = config.validate()
    assert errors == [], f"Expected no validation errors for ollama (local) provider, got: {errors}"


# ---------------------------------------------------------------------------
# Test 5: validate() — CRITICAL error when API key env var is missing
# ---------------------------------------------------------------------------


def test_validate_critical_when_api_key_env_missing():
    """validate() must return a CRITICAL error when the default provider needs an API key
    and the corresponding env var is not set."""
    config = _make_config(
        providers=ProvidersConfig(
            default_provider="openai",
            openai=ProviderEntry(api_key_env="VELUNE_TEST_MISSING_KEY_XYZ"),
        )
    )

    # Guarantee the env var is absent in this test.
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("VELUNE_TEST_MISSING_KEY_XYZ", None)
        errors = config.validate()

    critical = [e for e in errors if e.severity == "CRITICAL"]
    assert len(critical) >= 1, (
        "Expected at least one CRITICAL ConfigValidationError for missing API key env var, "
        f"got: {errors}"
    )
    assert any("VELUNE_TEST_MISSING_KEY_XYZ" in e.reason for e in critical), (
        f"Expected the error reason to mention 'VELUNE_TEST_MISSING_KEY_XYZ', got: {[e.reason for e in critical]}"
    )


# ---------------------------------------------------------------------------
# Test 6: validate() — no error when API key env var IS set
# ---------------------------------------------------------------------------


def test_validate_ok_when_api_key_env_is_set():
    """validate() must return no errors when the required env var is present."""
    config = _make_config(
        providers=ProvidersConfig(
            default_provider="openai",
            openai=ProviderEntry(api_key_env="VELUNE_TEST_PRESENT_KEY_XYZ"),
        )
    )

    with patch.dict(os.environ, {"VELUNE_TEST_PRESENT_KEY_XYZ": "sk-test-value"}):
        errors = config.validate()

    critical = [e for e in errors if e.severity == "CRITICAL"]
    assert critical == [], (
        f"Expected no CRITICAL errors when API key env var is set, got: {critical}"
    )


# ---------------------------------------------------------------------------
# Test 7: validate() — CRITICAL error for non-existent workspace root
# ---------------------------------------------------------------------------


def test_validate_critical_for_missing_workspace_root(tmp_path: Path):
    """validate() must flag a CRITICAL error when workspace.root is set but absent."""
    phantom = tmp_path / "does_not_exist"
    config = _make_config(
        providers=ProvidersConfig(default_provider="ollama"),
        workspace=WorkspaceConfig(root=phantom),
    )
    errors = config.validate()
    critical = [e for e in errors if e.severity == "CRITICAL"]
    assert any(e.field == "workspace.root" for e in critical), (
        f"Expected a CRITICAL error for workspace.root, got: {errors}"
    )


# ---------------------------------------------------------------------------
# Test 8: validate() — WARNING (not CRITICAL) for missing storage_dir
# ---------------------------------------------------------------------------


def test_validate_warning_for_missing_storage_dir(tmp_path: Path):
    """validate() must emit a WARNING (not CRITICAL) when memory.storage_dir does not exist."""
    phantom = tmp_path / "missing_storage"
    config = _make_config(
        providers=ProvidersConfig(default_provider="ollama"),
        memory=MemoryConfig(storage_dir=phantom),
    )
    errors = config.validate()
    warnings = [e for e in errors if e.severity == "WARNING" and e.field == "memory.storage_dir"]
    assert len(warnings) == 1, f"Expected exactly one WARNING for memory.storage_dir, got: {errors}"


# ---------------------------------------------------------------------------
# Test 9: LifecycleCoordinator.startup() raises on CRITICAL config errors
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_lifecycle_startup_raises_on_critical_config_error():
    """LifecycleCoordinator.startup() must raise RuntimeError when the attached config
    has CRITICAL validation errors."""
    config = _make_config(
        providers=ProvidersConfig(
            default_provider="openai",
            openai=ProviderEntry(api_key_env="VELUNE_TEST_LC_MISSING_KEY_XYZ"),
        )
    )

    lc = LifecycleCoordinator()
    lc.set_config(config)

    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("VELUNE_TEST_LC_MISSING_KEY_XYZ", None)
        with pytest.raises(RuntimeError, match="critical error"):
            await lc.startup()


# ---------------------------------------------------------------------------
# Test 10: LifecycleCoordinator.startup() proceeds normally for valid config
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_lifecycle_startup_proceeds_for_valid_config():
    """LifecycleCoordinator.startup() must not raise when config validates cleanly."""
    config = _make_config(providers=ProvidersConfig(default_provider="ollama"))

    lc = LifecycleCoordinator()
    lc.set_config(config)

    # No subsystems registered — startup should complete without error.
    await lc.startup()
    assert lc._started is True
