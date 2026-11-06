"""The orchestration.engine flag, and proof that adding the engine leaves legacy paths alone."""

from __future__ import annotations

import ast
import logging
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from velune.kernel.config import ConfigLoader, VeluneConfig, get_default_config
from velune.orchestration.engine import ENGINE_OFF, ENGINE_ON, engine_enabled, engine_mode

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "velune"
ENGINE_DIR = PACKAGE / "orchestration" / "engine"
STATE_DIR = PACKAGE / "orchestration" / "state"


def config_for(value) -> VeluneConfig:
    return VeluneConfig(orchestration={"engine": value})


# ── the flag ───────────────────────────────────────────────────────────────


def test_the_default_is_off(monkeypatch):
    monkeypatch.delenv("VELUNE_ORCHESTRATION__ENGINE", raising=False)
    assert VeluneConfig().orchestration.engine == ENGINE_OFF == "off"
    assert get_default_config().orchestration.engine == "off"
    assert engine_mode(VeluneConfig()) == "off" and not engine_enabled(VeluneConfig())


def test_the_repository_config_does_not_enable_it(monkeypatch):
    monkeypatch.delenv("VELUNE_ORCHESTRATION__ENGINE", raising=False)
    assert "orchestration" not in (ROOT / "velune.toml").read_text(encoding="utf-8").lower()
    assert ConfigLoader(ROOT / "velune.toml").load().orchestration.engine == "off"


def test_the_env_var_and_toml_can_turn_it_on(monkeypatch, tmp_path):
    monkeypatch.setenv("VELUNE_ORCHESTRATION__ENGINE", "on")
    assert engine_enabled(VeluneConfig())
    monkeypatch.delenv("VELUNE_ORCHESTRATION__ENGINE")
    path = tmp_path / "velune.toml"
    path.write_text('[orchestration]\nengine = "on"\n', encoding="utf-8")
    assert ConfigLoader(path).load().orchestration.engine == ENGINE_ON


def test_values_are_normalised_case_and_whitespace():
    assert config_for("  ON ").orchestration.engine == "on"
    assert config_for("Off").orchestration.engine == "off"


@pytest.mark.parametrize("bad", ["", "yes", "true", "1", "native", "enabled", None])
def test_an_unknown_value_falls_back_to_off_with_a_warning(bad, caplog):
    with caplog.at_level(logging.WARNING, logger="velune.kernel.config"):
        cfg = config_for(bad)
    assert cfg.orchestration.engine == "off" and not engine_enabled(cfg)
    assert any("orchestration.engine" in r.getMessage() for r in caplog.records)


def test_a_config_without_the_section_reads_as_off():
    assert engine_mode(SimpleNamespace()) == "off"
    assert not engine_enabled(SimpleNamespace(orchestration=SimpleNamespace()))
    assert not engine_enabled(None)


def test_the_new_section_does_not_disturb_the_existing_ones():
    config = VeluneConfig()
    assert config.cognition.council_engine == "legacy"
    assert config.cognition.max_council_tier == "full"
    assert "orchestration" in VeluneConfig.model_fields


# ── no legacy-path side effects ────────────────────────────────────────────

HEAVY = (
    "velune.providers",
    "velune.cli",
    "velune.cognition.orchestrator",
    "velune.cognition.council",
    "velune.orchestration.tool_loop",
    "velune.orchestration.subsystems",
    "velune.models",
    "velune.execution",
    "velune.permissions",
    "velune.mcp",
    "velune.runtime",
)
# velune.kernel.config is deliberately absent: the ``velune.core`` package init (which owns
# AgentRole) already loads it for any importer, and it is a plain pydantic settings module.


def run_python(code: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-W", "error", "-c", code],
        capture_output=True,
        text=True,
        cwd=ROOT,
        timeout=60,
    )


def test_importing_the_engine_loads_no_provider_cli_or_legacy_council_module():
    result = run_python(
        "import sys\n"
        "import velune.orchestration.engine, velune.orchestration.state\n"
        f"heavy = {HEAVY!r}\n"
        "loaded = sorted(m for m in sys.modules if m.startswith(heavy))\n"
        "print('LOADED', loaded)\n"
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "LOADED []", result.stdout


def test_importing_the_engine_does_not_change_the_legacy_council_or_tool_loop():
    result = run_python(
        "import velune.cognition.orchestrator as o, velune.orchestration.tool_loop as t\n"
        "before = (sorted(vars(o)), sorted(vars(t)), dict(vars(t)).get('ToolLoopRunner'))\n"
        "import velune.orchestration.engine\n"
        "after = (sorted(vars(o)), sorted(vars(t)), dict(vars(t)).get('ToolLoopRunner'))\n"
        "assert before == after\n"
        "import velune.orchestration as pkg\n"
        "assert sorted(pkg.__all__) == ['ExecutionStatus', 'OrchestrationRequest', "
        "'OrchestrationResult', 'OrchestrationState']\n"
        "print('SAME')\n"
    )
    assert result.returncode == 0, result.stderr
    assert "SAME" in result.stdout


def _imports(path: Path) -> set[str]:
    found: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            found.add(node.module)
    return found


# Phase 4E-6: the single, explicit CLI entry point (``velune native``). It checks the flag first and
# is imported lazily by the command registry only when that command is invoked. No other CLI, REPL,
# council or MCP module may import the engine.
NATIVE_CLI_COMMAND = PACKAGE / "cli" / "commands" / "native.py"


def test_nothing_outside_the_engine_imports_the_engine_or_run_state():
    """Only the opt-in ``velune native`` command (Phase 4E-6) is wired to the engine."""
    offenders = []
    # Phase 4E-1: the one place allowed to bind engine ports to providers (not exported).
    adapters = PACKAGE / "orchestration" / "engine_adapters"
    for path in PACKAGE.rglob("*.py"):
        if ENGINE_DIR in path.parents or STATE_DIR in path.parents or adapters in path.parents:
            continue
        if path == NATIVE_CLI_COMMAND:
            continue
        for module in _imports(path):
            if module.startswith(("velune.orchestration.engine", "velune.orchestration.state")):
                offenders.append(f"{path.relative_to(ROOT)} -> {module}")
    assert offenders == []


def test_nothing_reads_the_flag_yet():
    """No code outside the engine package touches ``<config>.orchestration.engine``."""
    readers = []
    for path in PACKAGE.rglob("*.py"):
        if ENGINE_DIR in path.parents or path == PACKAGE / "kernel" / "config.py":
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            chain = (
                (
                    node.attr == "engine"
                    and isinstance(node.value, ast.Attribute)
                    and node.value.attr == "orchestration"
                )
                if isinstance(node, ast.Attribute)
                else False
            )
            quoted = isinstance(node, ast.Constant) and node.value in ("orchestration.engine",)
            if chain or quoted:
                readers.append(f"{path.relative_to(ROOT)}:{node.lineno}")
    assert readers == []


def test_the_engine_modules_only_depend_on_the_council_core_and_core_types():
    allowed_prefixes = (
        "velune.orchestration.engine",
        "velune.orchestration.state",
        "velune.council",
        "velune.core.types.agent",
        "velune._compat",
        "velune.models.specializations",  # lazy, inside AgentSpec.council_role only
    )
    # The trusted executor (Phase 4D-2) is the one module that must reach the existing enforcement
    # path and redaction; nothing else in the engine may.
    executor_only = (
        "velune.core.redaction",
        "velune.permissions",
        "velune.tools.base.tool",
    )
    offenders = []
    for path in [*ENGINE_DIR.glob("*.py"), *STATE_DIR.glob("*.py")]:
        prefixes = allowed_prefixes + (executor_only if path.name == "toolexec.py" else ())
        for module in _imports(path):
            if module.startswith("velune.") and not module.startswith(prefixes):
                offenders.append(f"{path.name} -> {module}")
    assert offenders == []


def test_the_council_core_was_not_modified_to_fit_the_engine():
    """SeatCall is still keyed by StageId: the engine has its own port rather than a mislabel."""
    from velune.council.domain import StageId
    from velune.council.ports import SeatCall

    assert SeatCall.model_fields["stage"].annotation is StageId
    assert [s.value for s in StageId] == [
        "frame",
        "perspectives",
        "review",
        "revision",
        "arbitration",
        "synthesis",
    ]
