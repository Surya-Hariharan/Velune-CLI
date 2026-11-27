"""``[mcp]`` in velune.toml: table-form servers load, and the REPL honours ``allowed_hosts``."""

from __future__ import annotations

from velune.kernel.config import ConfigLoader
from velune.mcp.registry import _load_toml_mcp

_TOML = """
[context]
max_tokens = 12345

[mcp]
allowed_hosts = ["tools.example.com"]

[mcp.servers]
short = "https://tools.example.com/sse"

[mcp.servers.filesystem]
command = "npx"
args = ["-y", "server-filesystem", "."]
"""


def test_a_table_form_server_does_not_discard_the_rest_of_the_config(tmp_path):
    path = tmp_path / "velune.toml"
    path.write_text(_TOML, encoding="utf-8")

    config = ConfigLoader(path).load()

    assert config.context.max_tokens == 12345
    assert config.mcp.allowed_hosts == ["tools.example.com"]
    assert config.mcp.servers["filesystem"]["command"] == "npx"
    assert {s.name for s in _load_toml_mcp(tmp_path)} == {"short", "filesystem"}


def test_the_repl_passes_allowed_hosts_to_the_mcp_registry(tmp_path):
    from velune.cli.repl import VeluneREPL
    from velune.core.runtime import build_runtime

    (tmp_path / "velune.toml").write_text(_TOML, encoding="utf-8")
    runtime = build_runtime(tmp_path, defer_background=True)

    repl = VeluneREPL(runtime)

    assert repl._mcp_registry._allowed_hosts == ["tools.example.com"]
