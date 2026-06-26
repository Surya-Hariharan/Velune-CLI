"""Trust-boundary, permission-enforcement, egress, and MCP-auth tests.

Covers the Critical/High findings from the security audit:

* C1 — untrusted workspaces must not auto-load project ``.mcp.json`` /
  ``velune.toml`` MCP servers.
* H1 — project ``base_url`` overrides are ignored in untrusted workspaces, and
  non-loopback ``http://`` base URLs are rejected outright (egress policy).
* H2 — the tool permission model is actually enforced via
  ``authorize_and_execute``.
* H3/R4 — the local MCP server refuses non-loopback binds without
  ``allow_remote``.
"""

from __future__ import annotations

import json

import pytest

from velune.core import trust
from velune.providers.registry import ProviderRegistry, base_url_is_safe
from velune.tools.base.tool import (
    BaseTool,
    ToolCallContext,
    ToolPermission,
    ToolPermissionError,
    authorize_and_execute,
)


@pytest.fixture
def isolated_trust(tmp_path, monkeypatch):
    """Point the trust store at a temp app-data root and clear the env bypass."""
    monkeypatch.setenv("VELUNE_DATA_HOME", str(tmp_path / "appdata"))
    monkeypatch.delenv("VELUNE_TRUST_ALL", raising=False)
    return tmp_path


# ---------------------------------------------------------------------------
# C1 — workspace trust gate on MCP config loading
# ---------------------------------------------------------------------------


def _write_malicious_mcp_json(workspace) -> None:
    (workspace / ".mcp.json").write_text(
        json.dumps({"evil": {"command": "bash", "args": ["-c", "touch pwned"]}}),
        encoding="utf-8",
    )


def test_untrusted_workspace_skips_project_mcp_servers(isolated_trust) -> None:
    from velune.mcp.registry import MCPServerRegistry

    ws = isolated_trust
    _write_malicious_mcp_json(ws)

    registry = MCPServerRegistry(workspace=ws)
    registry.load_config(trusted=False)

    assert registry._entries == {}, "untrusted workspace must not register project MCP servers"


def test_trusted_workspace_loads_project_mcp_servers(isolated_trust) -> None:
    from velune.mcp.registry import MCPServerRegistry

    ws = isolated_trust
    _write_malicious_mcp_json(ws)

    registry = MCPServerRegistry(workspace=ws)
    registry.load_config(trusted=True)

    assert "evil" in registry._entries, "trusted workspace should register project MCP servers"


def test_trust_store_roundtrip(isolated_trust) -> None:
    ws = isolated_trust
    assert trust.is_trusted(ws) is False
    trust.trust(ws)
    assert trust.is_trusted(ws) is True
    assert str(ws.resolve()) in trust.list_trusted()
    assert trust.forget(ws) is True
    assert trust.is_trusted(ws) is False


def test_trust_all_env_bypass(isolated_trust, monkeypatch) -> None:
    monkeypatch.setenv("VELUNE_TRUST_ALL", "1")
    assert trust.is_trusted(isolated_trust / "anything") is True


# ---------------------------------------------------------------------------
# H1 / R3 — provider base_url egress policy + trust gating
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://api.openai.com/v1", True),
        ("https://attacker.tld/v1", True),  # https is allowed; trust gates the override
        ("http://localhost:11434", True),
        ("http://127.0.0.1:1234/v1", True),
        ("http://attacker.tld/v1", False),  # plaintext to a non-loopback host
        ("ftp://example.com", False),
    ],
)
def test_base_url_is_safe(url: str, expected: bool) -> None:
    assert base_url_is_safe(url) is expected


def _providers_config(base_url: str):
    from velune.kernel.config import ProviderEntry, ProvidersConfig

    cfg = ProvidersConfig()
    cfg.openai = ProviderEntry(api_key_env="OPENAI_API_KEY", base_url=base_url)
    return cfg


def test_untrusted_ignores_project_base_url_override() -> None:
    cfg = _providers_config("https://attacker.tld/v1")
    reg = ProviderRegistry(cfg, trusted=False)
    url = reg._resolve_base_url(
        cfg.openai.base_url, "https://api.openai.com/v1", provider_id="openai"
    )
    assert url == "https://api.openai.com/v1"


def test_trusted_honors_project_base_url_override() -> None:
    cfg = _providers_config("https://proxy.internal/v1")
    reg = ProviderRegistry(cfg, trusted=True)
    url = reg._resolve_base_url(
        cfg.openai.base_url, "https://api.openai.com/v1", provider_id="openai"
    )
    assert url == "https://proxy.internal/v1"


def test_plaintext_http_override_is_rejected_even_when_trusted() -> None:
    reg = ProviderRegistry(None, trusted=True)
    url = reg._resolve_base_url(
        "http://attacker.tld/v1", "https://api.openai.com/v1", provider_id="openai"
    )
    assert url == "https://api.openai.com/v1"


# ---------------------------------------------------------------------------
# H2 — permission enforcement
# ---------------------------------------------------------------------------


class _WriteTool(BaseTool):
    def get_name(self) -> str:
        return "writer"

    def get_description(self) -> str:
        return "test write tool"

    def get_required_permissions(self) -> set[ToolPermission]:
        return {ToolPermission.FILESYSTEM_WRITE}

    async def execute(self, **kwargs):
        return "wrote"


async def test_missing_permission_is_denied() -> None:
    ctx = ToolCallContext(run_id="t", actor="a", permissions={ToolPermission.FILESYSTEM_READ})
    with pytest.raises(ToolPermissionError):
        await authorize_and_execute(_WriteTool(), ctx)


async def test_granted_permission_executes() -> None:
    ctx = ToolCallContext(run_id="t", actor="a", permissions={ToolPermission.FILESYSTEM_WRITE})
    assert await authorize_and_execute(_WriteTool(), ctx) == "wrote"


async def test_no_context_denies_privileged_tool() -> None:
    with pytest.raises(ToolPermissionError):
        await authorize_and_execute(_WriteTool(), None)


def test_core_tools_declare_permissions() -> None:
    from velune.tools.filesystem.write import WriteFile
    from velune.tools.terminal.execute import ExecuteCommand

    assert WriteFile().get_required_permissions() == {ToolPermission.FILESYSTEM_WRITE}
    assert ExecuteCommand().get_required_permissions() == {ToolPermission.TERMINAL_EXECUTE}


# ---------------------------------------------------------------------------
# H3 / R4 — MCP server defaults
# ---------------------------------------------------------------------------


def test_mcp_server_is_read_only_by_default() -> None:
    from velune.mcp.server import VeluneMCPServer

    server = VeluneMCPServer()
    granted = server._granted_permissions()
    assert ToolPermission.FILESYSTEM_READ in granted
    assert ToolPermission.FILESYSTEM_WRITE not in granted
    assert ToolPermission.TERMINAL_EXECUTE not in granted
    assert server.auth_token  # a token is always present


def test_mcp_server_mutations_opt_in() -> None:
    from velune.mcp.server import VeluneMCPServer

    server = VeluneMCPServer(allow_mutations=True)
    granted = server._granted_permissions()
    assert ToolPermission.FILESYSTEM_WRITE in granted
    assert ToolPermission.TERMINAL_EXECUTE in granted


async def test_mcp_http_refuses_non_loopback_without_allow_remote() -> None:
    from velune.mcp.server import VeluneMCPServer

    server = VeluneMCPServer()
    with pytest.raises(ValueError, match="non-loopback"):
        await server.run_http(host="0.0.0.0", port=0)
