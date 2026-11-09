"""Build the production tool registry, and gated contexts, for filesystem tests.

Tests that claim to exercise "the agent" must use the registry the REPL uses
(velune/tools/subsystems.py:_create_tool_registry) — the real tool classes with
their production options (``confirm=False``, a shared conflict tracker) — not a
hand-assembled one.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from velune.permissions import ExecutionMode, PolicyState
from velune.permissions.boundary import Boundary
from velune.permissions.gate import PermissionGate
from velune.tools.base.tool import ToolCallContext


def production_registry(workspace: Path):
    from velune.tools.subsystems import _create_tool_registry

    services = {"runtime.execution_executor": SimpleNamespace(sandbox=None)}
    container = SimpleNamespace(get=lambda k: services[k], has=lambda k: k in services)
    return _create_tool_registry(SimpleNamespace(workspace=workspace, container=container))


def gated_ctx(workspace: Path, mode: ExecutionMode = ExecutionMode.AUTO, ask=None, **kw):
    gate = PermissionGate(PolicyState(mode=mode), Boundary(workspace), ask=ask, **kw)
    return ToolCallContext(run_id="t", actor="test", workspace=workspace, gate=gate)
