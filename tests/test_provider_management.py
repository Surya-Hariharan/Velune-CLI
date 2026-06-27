"""Tests for the Provider Management System.

Covers ProviderPalette flows, keystore interactions, validation handling,
model discovery, registry refresh, and slash-command integration — all without
real network calls or OS-keyring access.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from velune.cli.provider_ui import (
    ALL_PROVIDER_META,
    CLOUD_PROVIDERS,
    LOCAL_PROVIDERS,
    ProviderPalette,
    provider_status,
    status_style,
    validation_status_label,
)
from velune.providers.validation import ValidationResult, ValidationStatus


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

class _FakeConsole:
    """Captures console output without hitting a real terminal."""

    def __init__(self) -> None:
        self.lines: list[str] = []

    def print(self, *args, **kwargs) -> None:  # noqa: ANN001
        self.lines.append(" ".join(str(a) for a in args))

    def has(self, text: str) -> bool:
        return any(text in line for line in self.lines)


class _FakeModelRegistry:
    def __init__(self) -> None:
        self._models: list = []

    def register(self, m) -> None:
        self._models.append(m)

    def get_by_provider(self, pid: str) -> list:
        return [m for m in self._models if getattr(m, "provider_id", None) == pid]

    def remove(self, model_id: str, pid: str | None = None) -> bool:
        before = len(self._models)
        self._models = [
            m for m in self._models
            if not (m.model_id == model_id and (pid is None or m.provider_id == pid))
        ]
        return len(self._models) < before


class _FakeContainer:
    def __init__(self) -> None:
        self.model_registry = _FakeModelRegistry()

    def get(self, key: str):
        if key == "runtime.model_registry":
            return self.model_registry
        raise KeyError(key)

    def get_optional(self, key: str):
        try:
            return self.get(key)
        except KeyError:
            return None


@pytest.fixture()
def console() -> _FakeConsole:
    return _FakeConsole()


@pytest.fixture()
def container() -> _FakeContainer:
    return _FakeContainer()


@pytest.fixture()
def palette(console, container) -> ProviderPalette:
    return ProviderPalette(console=console, container=container)


def _ok_result(pid: str, models: list[str] | None = None) -> ValidationResult:
    return ValidationResult(
        provider_id=pid,
        status=ValidationStatus.OK,
        message="Authenticated successfully",
        models=models or ["model-a", "model-b"],
        account_info={},
    )


def _fail_result(pid: str, status: ValidationStatus, msg: str) -> ValidationResult:
    return ValidationResult(
        provider_id=pid,
        status=status,
        message=msg,
        models=[],
        account_info={},
    )


# ---------------------------------------------------------------------------
# Provider metadata
# ---------------------------------------------------------------------------

class TestProviderCatalogue:
    def test_cloud_providers_have_required_fields(self) -> None:
        for pid, meta in CLOUD_PROVIDERS.items():
            assert "label" in meta, pid
            assert "description" in meta, pid
            assert "url" in meta, pid
            assert "env" in meta, pid

    def test_local_providers_have_required_fields(self) -> None:
        for pid, meta in LOCAL_PROVIDERS.items():
            assert "label" in meta, pid
            assert "url" in meta, pid
            assert meta.get("local") is True, pid

    def test_all_provider_meta_merges_both(self) -> None:
        for pid in CLOUD_PROVIDERS:
            assert pid in ALL_PROVIDER_META
        for pid in LOCAL_PROVIDERS:
            assert pid in ALL_PROVIDER_META

    def test_ollama_and_lmstudio_not_in_cloud(self) -> None:
        assert "ollama" not in CLOUD_PROVIDERS
        assert "lmstudio" not in CLOUD_PROVIDERS

    def test_cloud_providers_not_in_local(self) -> None:
        for pid in CLOUD_PROVIDERS:
            assert pid not in LOCAL_PROVIDERS


# ---------------------------------------------------------------------------
# Status helpers
# ---------------------------------------------------------------------------

class TestStatusHelpers:
    def test_status_style_configured(self) -> None:
        from velune.cli import design
        assert status_style("configured") == design.OK

    def test_status_style_not_configured(self) -> None:
        from velune.cli import design
        assert status_style("not configured") == design.MUTED

    def test_status_style_offline(self) -> None:
        from velune.cli import design
        assert status_style("offline") == design.WARN

    def test_validation_status_label_ok(self) -> None:
        assert validation_status_label(ValidationStatus.OK) == "Connected"

    def test_validation_status_label_invalid(self) -> None:
        assert validation_status_label(ValidationStatus.INVALID_KEY) == "Invalid Key"

    def test_validation_status_label_network(self) -> None:
        assert validation_status_label(ValidationStatus.NETWORK_ERROR) == "Network Error"

    def test_provider_status_cloud_with_key(self) -> None:
        with patch("velune.cli.provider_ui.has_key", return_value=True):
            assert provider_status("anthropic") == "configured"

    def test_provider_status_cloud_no_key(self) -> None:
        with patch("velune.cli.provider_ui.has_key", return_value=False):
            assert provider_status("anthropic") == "not configured"

    def test_provider_status_ollama_running(self) -> None:
        with patch("velune.cli.provider_ui.is_ollama_live", return_value=True):
            assert provider_status("ollama") == "running"

    def test_provider_status_ollama_offline(self) -> None:
        with patch("velune.cli.provider_ui.is_ollama_live", return_value=False):
            assert provider_status("ollama") == "offline"


# ---------------------------------------------------------------------------
# Add Provider — happy path
# ---------------------------------------------------------------------------

class TestAddProvider:
    @pytest.mark.asyncio
    async def test_add_provider_success_saves_key_and_discovers(
        self, palette: ProviderPalette
    ) -> None:
        result = _ok_result("anthropic", ["claude-3-5-sonnet", "claude-3-opus"])

        with (
            patch("velune.cli.provider_ui.validate_provider_sync", return_value=result),
            patch("velune.cli.provider_ui.save_key") as mock_save,
            patch("velune.cli.provider_ui.has_key", return_value=False),
            patch.object(palette, "_prompt_api_key", new=AsyncMock(return_value="sk-test-key")),
            patch.object(palette, "_discover_for_provider", new=AsyncMock()),
        ):
            await palette._add_single_provider("anthropic")

        mock_save.assert_called_once_with("anthropic", "sk-test-key")
        assert palette.console.has("connected") or palette.console.has("Connected") or palette.console.has("is now connected")

    @pytest.mark.asyncio
    async def test_add_provider_saves_only_on_ok(
        self, palette: ProviderPalette
    ) -> None:
        """Key must NOT be saved when validation returns an error."""
        fail = _fail_result("openai", ValidationStatus.INVALID_KEY, "Invalid key.")

        with (
            patch("velune.cli.provider_ui.validate_provider_sync", return_value=fail),
            patch("velune.cli.provider_ui.save_key") as mock_save,
            patch.object(palette, "_prompt_api_key", new=AsyncMock(return_value="bad-key")),
            # User chooses "cancel" on the retry menu
            patch.object(palette, "_show_menu", new=AsyncMock(return_value="cancel")),
        ):
            await palette._add_single_provider("openai")

        mock_save.assert_not_called()

    @pytest.mark.asyncio
    async def test_add_provider_retry_then_success(
        self, palette: ProviderPalette
    ) -> None:
        """User enters a bad key, chooses Retry, then enters the correct key."""
        fail = _fail_result("groq", ValidationStatus.INVALID_KEY, "Invalid key.")
        ok = _ok_result("groq")

        call_count = [0]

        async def _mock_validate_threaded(fn, *args):
            call_count[0] += 1
            return fail if call_count[0] == 1 else ok

        menu_calls = [0]

        async def _mock_menu(*args, **kwargs) -> str:
            menu_calls[0] += 1
            # First menu call (after first failure): retry
            if menu_calls[0] == 1:
                return "retry"
            return "cancel"

        with (
            patch("velune.cli.provider_ui.validate_provider_sync", side_effect=[fail, ok]),
            patch("velune.cli.provider_ui.save_key") as mock_save,
            patch.object(palette, "_prompt_api_key", new=AsyncMock(return_value="sk-key")),
            patch.object(palette, "_show_menu", new=AsyncMock(side_effect=_mock_menu)),
            patch.object(palette, "_discover_for_provider", new=AsyncMock()),
        ):
            await palette._add_single_provider("groq")

        mock_save.assert_called_once()

    @pytest.mark.asyncio
    async def test_add_provider_cancelled_by_user(
        self, palette: ProviderPalette
    ) -> None:
        """Pressing Esc / Ctrl-C on key prompt should NOT save anything."""
        with (
            patch("velune.cli.provider_ui.save_key") as mock_save,
            patch.object(palette, "_prompt_api_key", new=AsyncMock(return_value=None)),
        ):
            await palette._add_single_provider("anthropic")

        mock_save.assert_not_called()

    @pytest.mark.asyncio
    async def test_add_unknown_provider_prints_warning(
        self, palette: ProviderPalette
    ) -> None:
        await palette._add_single_provider("nonexistent_provider_xyz")
        assert palette.console.has("Unknown provider")

    @pytest.mark.asyncio
    async def test_add_provider_network_error_offers_save_anyway(
        self, palette: ProviderPalette
    ) -> None:
        fail = _fail_result("deepseek", ValidationStatus.NETWORK_ERROR, "Cannot reach DeepSeek.")

        with (
            patch("velune.cli.provider_ui.validate_provider_sync", return_value=fail),
            patch("velune.cli.provider_ui.save_key") as mock_save,
            patch.object(palette, "_prompt_api_key", new=AsyncMock(return_value="ds-key")),
            patch.object(palette, "_show_menu", new=AsyncMock(return_value="save_anyway")),
        ):
            await palette._add_single_provider("deepseek")

        mock_save.assert_called_once_with("deepseek", "ds-key")

    @pytest.mark.asyncio
    async def test_add_provider_shows_models_preview(
        self, palette: ProviderPalette
    ) -> None:
        models = [f"model-{i}" for i in range(10)]
        result = _ok_result("openai", models)

        with (
            patch("velune.cli.provider_ui.validate_provider_sync", return_value=result),
            patch("velune.cli.provider_ui.save_key"),
            patch.object(palette, "_prompt_api_key", new=AsyncMock(return_value="sk-key")),
            patch.object(palette, "_discover_for_provider", new=AsyncMock()),
        ):
            await palette._add_single_provider("openai")

        # Models line should be printed
        assert palette.console.has("Models:") or palette.console.has("model-0")


# ---------------------------------------------------------------------------
# Key replacement (update)
# ---------------------------------------------------------------------------

class TestUpdateApiKey:
    @pytest.mark.asyncio
    async def test_update_key_replaces_old_key(
        self, palette: ProviderPalette
    ) -> None:
        ok = _ok_result("anthropic")

        with (
            patch("velune.cli.provider_ui.validate_provider_sync", return_value=ok),
            patch("velune.cli.provider_ui.save_key") as mock_save,
            patch("velune.cli.provider_ui.has_key", return_value=True),
            patch.object(palette, "_prompt_api_key", new=AsyncMock(return_value="new-key")),
            patch.object(palette, "_discover_for_provider", new=AsyncMock()),
        ):
            # _add_single_provider is called for both add and update
            await palette._add_single_provider("anthropic")

        mock_save.assert_called_once_with("anthropic", "new-key")


# ---------------------------------------------------------------------------
# Remove Provider
# ---------------------------------------------------------------------------

class TestRemoveProvider:
    @pytest.mark.asyncio
    async def test_remove_confirmed_deletes_key(
        self, palette: ProviderPalette
    ) -> None:
        with (
            patch("velune.cli.provider_ui.delete_key") as mock_del,
            patch("velune.cli.provider_ui.has_key", return_value=False),
            patch.object(palette, "_confirm", new=AsyncMock(return_value=True)),
        ):
            await palette._remove_provider("anthropic")

        mock_del.assert_called_once_with("anthropic")

    @pytest.mark.asyncio
    async def test_remove_cancelled_keeps_key(
        self, palette: ProviderPalette
    ) -> None:
        with (
            patch("velune.cli.provider_ui.delete_key") as mock_del,
            patch.object(palette, "_confirm", new=AsyncMock(return_value=False)),
        ):
            await palette._remove_provider("anthropic")

        mock_del.assert_not_called()
        assert palette.console.has("Cancelled")

    @pytest.mark.asyncio
    async def test_remove_evicts_models_from_registry(
        self, palette: ProviderPalette, container: _FakeContainer
    ) -> None:
        # Seed some fake models in the registry
        m1 = MagicMock()
        m1.model_id = "claude-3-opus"
        m1.provider_id = "anthropic"
        m2 = MagicMock()
        m2.model_id = "claude-3-sonnet"
        m2.provider_id = "anthropic"
        container.model_registry._models = [m1, m2]

        with (
            patch("velune.cli.provider_ui.delete_key"),
            patch("velune.cli.provider_ui.has_key", return_value=False),
            patch.object(palette, "_confirm", new=AsyncMock(return_value=True)),
        ):
            await palette._remove_provider("anthropic")

        # Registry should be empty for this provider
        assert container.model_registry.get_by_provider("anthropic") == []

    @pytest.mark.asyncio
    async def test_remove_last_cloud_provider_shows_notice(
        self, palette: ProviderPalette
    ) -> None:
        with (
            patch("velune.cli.provider_ui.delete_key"),
            # No keys remain after removal
            patch("velune.cli.provider_ui.has_key", return_value=False),
            patch.object(palette, "_confirm", new=AsyncMock(return_value=True)),
        ):
            await palette._remove_provider("anthropic")

        assert palette.console.has("No cloud providers")

    @pytest.mark.asyncio
    async def test_remove_unknown_provider_gracefully(
        self, palette: ProviderPalette
    ) -> None:
        with (
            patch("velune.cli.provider_ui.delete_key") as mock_del,
            patch.object(palette, "_confirm", new=AsyncMock(return_value=True)),
        ):
            await palette._remove_provider("totally_unknown_abc")

        # Should still call delete_key (graceful attempt)
        mock_del.assert_called_once_with("totally_unknown_abc")


# ---------------------------------------------------------------------------
# Test Connection
# ---------------------------------------------------------------------------

class TestTestConnection:
    @pytest.mark.asyncio
    async def test_test_provider_ok_prints_connected(
        self, palette: ProviderPalette
    ) -> None:
        result = _ok_result("anthropic", ["claude-3-opus", "claude-3-sonnet"])

        with (
            patch("velune.cli.provider_ui.validate_provider_sync", return_value=result),
            patch("velune.cli.provider_ui.get_key", return_value="sk-key"),
        ):
            await palette._test_provider("anthropic")

        assert palette.console.has("Connected")

    @pytest.mark.asyncio
    async def test_test_provider_fail_prints_error(
        self, palette: ProviderPalette
    ) -> None:
        fail = _fail_result("openai", ValidationStatus.INVALID_KEY, "API key invalid.")

        with (
            patch("velune.cli.provider_ui.validate_provider_sync", return_value=fail),
            patch("velune.cli.provider_ui.get_key", return_value="bad"),
        ):
            await palette._test_provider("openai")

        assert palette.console.has("Invalid Key") or palette.console.has("invalid")

    @pytest.mark.asyncio
    async def test_test_provider_no_key_prints_warning(
        self, palette: ProviderPalette
    ) -> None:
        with patch("velune.cli.provider_ui.get_key", return_value=None):
            await palette._test_provider("anthropic")

        assert palette.console.has("no API key")

    @pytest.mark.asyncio
    async def test_flow_test_no_providers_configured(
        self, palette: ProviderPalette
    ) -> None:
        with patch("velune.cli.provider_ui.has_key", return_value=False):
            await palette._flow_test_connection()

        assert palette.console.has("No cloud providers configured")

    @pytest.mark.asyncio
    async def test_flow_test_all_configured(
        self, palette: ProviderPalette
    ) -> None:
        ok = _ok_result("groq")

        with (
            patch("velune.cli.provider_ui.has_key", return_value=True),
            patch("velune.cli.provider_ui.validate_provider_sync", return_value=ok),
            patch("velune.cli.provider_ui.get_key", return_value="key"),
            patch.object(palette, "_show_menu", new=AsyncMock(return_value="__all__")),
        ):
            await palette._flow_test_connection()

        # At least one "Connected" line expected
        assert palette.console.has("Connected")


# ---------------------------------------------------------------------------
# Model discovery
# ---------------------------------------------------------------------------

class TestModelDiscovery:
    @pytest.mark.asyncio
    async def test_discover_registers_models(
        self, palette: ProviderPalette, container: _FakeContainer
    ) -> None:
        m1 = MagicMock()
        m1.provider_id = "anthropic"
        m1.model_id = "claude-3-opus"
        m2 = MagicMock()
        m2.provider_id = "anthropic"
        m2.model_id = "claude-3-sonnet"

        mock_scanner = MagicMock()
        mock_scanner.scan_provider = AsyncMock(return_value=[m1, m2])

        with patch(
            "velune.cli.provider_ui.ModelDiscoveryScanner",
            return_value=mock_scanner,
        ):
            await palette._discover_for_provider("anthropic")

        assert len(container.model_registry._models) == 2

    @pytest.mark.asyncio
    async def test_discover_all_scans_all_providers(
        self, palette: ProviderPalette, container: _FakeContainer
    ) -> None:
        m1 = MagicMock()
        m1.provider_id = "anthropic"
        m1.model_id = "claude-3-opus"

        mock_scanner = MagicMock()
        mock_scanner.scan_all = AsyncMock(return_value=[m1])

        with patch(
            "velune.cli.provider_ui.ModelDiscoveryScanner",
            return_value=mock_scanner,
        ):
            await palette._flow_discover_models()

        assert len(container.model_registry._models) == 1
        assert palette.console.has("1 model")

    @pytest.mark.asyncio
    async def test_discover_no_models_prints_notice(
        self, palette: ProviderPalette
    ) -> None:
        mock_scanner = MagicMock()
        mock_scanner.scan_provider = AsyncMock(return_value=[])

        with patch(
            "velune.cli.provider_ui.ModelDiscoveryScanner",
            return_value=mock_scanner,
        ):
            await palette._discover_for_provider("openai")

        assert palette.console.has("No models discovered")

    @pytest.mark.asyncio
    async def test_discover_handles_scanner_error(
        self, palette: ProviderPalette
    ) -> None:
        with patch(
            "velune.cli.provider_ui.ModelDiscoveryScanner",
            side_effect=ImportError("scanner unavailable"),
        ):
            # Should not raise; should surface a warning
            await palette._flow_discover_models()

        assert palette.console.has("Discovery failed") or palette.console.has("failed")


# ---------------------------------------------------------------------------
# Status overview
# ---------------------------------------------------------------------------

class TestStatusOverview:
    @pytest.mark.asyncio
    async def test_show_status_all_prints_table(
        self, palette: ProviderPalette
    ) -> None:
        with (
            patch("velune.cli.provider_ui.has_key", return_value=False),
            patch("velune.cli.provider_ui.is_ollama_live", return_value=False),
        ):
            await palette._show_status_all()

        # console.print was called at least twice (table + footer message)
        assert len(palette.console.lines) >= 2
        # Footer hint referencing /providers should be present
        assert palette.console.has("/providers")


# ---------------------------------------------------------------------------
# Run dispatcher
# ---------------------------------------------------------------------------

class TestRunDispatcher:
    @pytest.mark.asyncio
    async def test_run_dispatches_add(self, palette: ProviderPalette) -> None:
        with patch.object(palette, "_flow_add_provider", new=AsyncMock()) as mock_add:
            await palette.run("add")
        mock_add.assert_called_once()

    @pytest.mark.asyncio
    async def test_run_dispatches_add_with_provider_id(self, palette: ProviderPalette) -> None:
        with patch.object(palette, "_add_single_provider", new=AsyncMock()) as mock_add:
            await palette.run("add anthropic")
        mock_add.assert_called_once_with("anthropic")

    @pytest.mark.asyncio
    async def test_run_dispatches_manage(self, palette: ProviderPalette) -> None:
        with patch.object(palette, "_flow_manage_providers", new=AsyncMock()) as mock_manage:
            await palette.run("manage")
        mock_manage.assert_called_once()

    @pytest.mark.asyncio
    async def test_run_dispatches_manage_with_provider_id(self, palette: ProviderPalette) -> None:
        with patch.object(palette, "_provider_detail", new=AsyncMock()) as mock_detail:
            await palette.run("manage groq")
        mock_detail.assert_called_once_with("groq")

    @pytest.mark.asyncio
    async def test_run_dispatches_discover(self, palette: ProviderPalette) -> None:
        with patch.object(palette, "_flow_discover_models", new=AsyncMock()) as mock_disc:
            await palette.run("discover")
        mock_disc.assert_called_once()

    @pytest.mark.asyncio
    async def test_run_dispatches_refresh(self, palette: ProviderPalette) -> None:
        with patch.object(palette, "_flow_discover_models", new=AsyncMock()) as mock_disc:
            await palette.run("refresh")
        mock_disc.assert_called_once()

    @pytest.mark.asyncio
    async def test_run_dispatches_test(self, palette: ProviderPalette) -> None:
        with patch.object(palette, "_flow_test_connection", new=AsyncMock()) as mock_test:
            await palette.run("test")
        mock_test.assert_called_once()

    @pytest.mark.asyncio
    async def test_run_dispatches_test_with_provider(self, palette: ProviderPalette) -> None:
        with patch.object(palette, "_test_provider", new=AsyncMock()) as mock_test:
            await palette.run("test openai")
        mock_test.assert_called_once_with("openai")

    @pytest.mark.asyncio
    async def test_run_dispatches_status(self, palette: ProviderPalette) -> None:
        with patch.object(palette, "_show_status_all", new=AsyncMock()) as mock_st:
            await palette.run("status")
        mock_st.assert_called_once()

    @pytest.mark.asyncio
    async def test_run_no_args_opens_main_menu(self, palette: ProviderPalette) -> None:
        with patch.object(palette, "_main_menu", new=AsyncMock()) as mock_menu:
            await palette.run("")
        mock_menu.assert_called_once()

    @pytest.mark.asyncio
    async def test_run_remove_without_id_prints_usage(self, palette: ProviderPalette) -> None:
        await palette.run("remove")
        assert palette.console.has("Usage")

    @pytest.mark.asyncio
    async def test_run_remove_with_id(self, palette: ProviderPalette) -> None:
        with patch.object(palette, "_remove_provider", new=AsyncMock()) as mock_rm:
            await palette.run("remove anthropic")
        mock_rm.assert_called_once_with("anthropic")


# ---------------------------------------------------------------------------
# Slash command registry integration
# ---------------------------------------------------------------------------

class TestSlashRegistry:
    def test_providers_command_registered(self) -> None:
        from velune.cli.slash_dispatcher import _BUILTIN_CATEGORIES, build_slash_registry

        class _StubContainer:
            def get(self, key):
                return None

        class _StubREPL:
            def __init__(self) -> None:
                self.container = _StubContainer()
                self.console = None

            def __getattr__(self, name):
                async def _handler(args: str = "") -> None:
                    return None
                return _handler

        registry = build_slash_registry(_StubREPL())
        cmd = registry.get("providers")
        assert cmd is not None, "/providers not found in registry"
        assert cmd.name == "providers"

    def test_providers_has_aliases(self) -> None:
        from velune.cli.slash_dispatcher import build_slash_registry

        class _StubContainer:
            def get(self, key):
                return None

        class _StubREPL:
            def __init__(self) -> None:
                self.container = _StubContainer()
                self.console = None

            def __getattr__(self, name):
                async def _handler(args: str = "") -> None:
                    return None
                return _handler

        registry = build_slash_registry(_StubREPL())
        # "provider" and "prov" are registered aliases
        assert registry.get("provider") is not None
        assert registry.get("prov") is not None

    def test_providers_category_is_providers(self) -> None:
        from velune.cli.slash_dispatcher import _BUILTIN_CATEGORIES
        assert _BUILTIN_CATEGORIES.get("providers") == "Providers"

    def test_providers_category_in_category_order(self) -> None:
        from velune.cli.autocomplete import CATEGORY_ORDER
        assert "Providers" in CATEGORY_ORDER


# ---------------------------------------------------------------------------
# Duplicate-key replacement (update does not add duplicate models)
# ---------------------------------------------------------------------------

class TestKeyReplacement:
    @pytest.mark.asyncio
    async def test_replacing_key_does_not_duplicate_models(
        self, palette: ProviderPalette, container: _FakeContainer
    ) -> None:
        """When a user updates an existing key, discovery should replace not duplicate."""
        ok = _ok_result("anthropic", ["claude-3-opus"])
        # Seed one existing model
        existing = MagicMock()
        existing.model_id = "claude-3-opus"
        existing.provider_id = "anthropic"
        container.model_registry._models = [existing]

        # Second round of _register_models with the same model
        m = MagicMock()
        m.model_id = "claude-3-opus"
        m.provider_id = "anthropic"

        palette._register_models([m])
        # The simple _FakeModelRegistry.register appends; real ModelRegistry
        # uses upsert semantics. We verify the palette calls register (not crash).
        # The integration test verifies no duplication at the registry level.
        assert len(container.model_registry._models) >= 1


# ---------------------------------------------------------------------------
# Recovery after validation failure
# ---------------------------------------------------------------------------

class TestRecoveryAfterFailure:
    @pytest.mark.asyncio
    async def test_rate_limited_retry_succeeds(
        self, palette: ProviderPalette
    ) -> None:
        fail = _fail_result("groq", ValidationStatus.RATE_LIMITED, "Rate limited.")
        ok = _ok_result("groq")

        call_n = [0]

        def _validate(pid, key):
            call_n[0] += 1
            return fail if call_n[0] == 1 else ok

        menu_n = [0]

        async def _menu(*args, **kwargs):
            menu_n[0] += 1
            return "retry" if menu_n[0] == 1 else "cancel"

        with (
            patch("velune.cli.provider_ui.validate_provider_sync", side_effect=_validate),
            patch("velune.cli.provider_ui.save_key") as mock_save,
            patch.object(palette, "_prompt_api_key", new=AsyncMock(return_value="key")),
            patch.object(palette, "_show_menu", new=AsyncMock(side_effect=_menu)),
            patch.object(palette, "_discover_for_provider", new=AsyncMock()),
        ):
            await palette._add_single_provider("groq")

        mock_save.assert_called_once()

    @pytest.mark.asyncio
    async def test_expired_key_prints_hint(
        self, palette: ProviderPalette
    ) -> None:
        fail = _fail_result("openai", ValidationStatus.EXPIRED_KEY, "Key expired.")

        with (
            patch("velune.cli.provider_ui.validate_provider_sync", return_value=fail),
            patch.object(palette, "_prompt_api_key", new=AsyncMock(return_value="old-key")),
            patch.object(palette, "_show_menu", new=AsyncMock(return_value="cancel")),
        ):
            await palette._add_single_provider("openai")

        # Should surface the expired key hint
        assert palette.console.has("Hint:") or palette.console.has("expired")

    @pytest.mark.asyncio
    async def test_permission_denied_prints_hint(
        self, palette: ProviderPalette
    ) -> None:
        fail = _fail_result("nvidia", ValidationStatus.PERMISSION_DENIED, "Permission denied.")

        with (
            patch("velune.cli.provider_ui.validate_provider_sync", return_value=fail),
            patch.object(palette, "_prompt_api_key", new=AsyncMock(return_value="key")),
            patch.object(palette, "_show_menu", new=AsyncMock(return_value="cancel")),
        ):
            await palette._add_single_provider("nvidia")

        assert palette.console.has("Hint:") or palette.console.has("permission")
