"""Which tools an acting agent may be offered: a pure policy resolver.

The set an agent may use is an intersection of three things, none of which the model controls:

1. the agent's declared allowlist (``AgentSpec.tool_policy``),
2. the trusted catalog of registered tools, and
3. a driver-controlled ceiling: a fixed set of tool names *and* the most powerful permission scopes
   a tool may require.

A request outside that intersection is an error, not a silent drop, so a misconfigured spec fails at
setup. Reasoning agents resolve to the empty set. Resolution grants nothing by itself: it only says
which tools may be *considered*; permission checks and approval are later, separate steps.

This module never imports or calls a tool. It reads names, schemas and declared permission scopes
from a registry through duck typing (``list_tools`` / ``get``), so nothing here can run a tool.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from velune.council.serialization import Contract
from velune.orchestration.engine.errors import EngineError
from velune.orchestration.engine.spec import AgentKind, AgentSpec
from velune.orchestration.engine.toolschema import schema_problem

# What a tool may require, as ``ToolPermission`` string values. Read-only scopes only.
READ_ONLY_PERMISSIONS: frozenset[str] = frozenset({"filesystem.read", "git.read"})

# The Phase 4D-1 ceiling: read-only filesystem and Git tools. Code-intelligence tools need an index
# container, and ``terminal_history`` can expose pasted secrets, so neither is in the first set.
READ_ONLY_TOOL_CEILING: frozenset[str] = frozenset(
    {
        "read_file",
        "read_directory",
        "grep_files",
        "find_files",
        "git_log",
        "git_diff",
        "git_blame",
        "git_status",
        "git_branch",
    }
)

# Named so the refusal can say *why*. Anything not in the ceiling is refused either way.
EXCLUDED_TOOLS: frozenset[str] = frozenset(
    {
        "execute_command",
        "web_fetch",
        "write_file",
        "edit_file",
        "create_file",
        "delete_file",
        "create_directory",
        "delete_directory",
        "move_path",
        "write_plan",
        "git_commit",
        "git_checkout",
        "git_push",
        "create_pr",
        "terminal_history",
    }
)


class ToolPolicyError(EngineError, ValueError):
    """An acting spec asked for tools it may not have. Lists every offending tool and why."""

    def __init__(self, problems: Mapping[str, str]) -> None:
        self.problems = dict(sorted(problems.items()))
        detail = "; ".join(f"{name}: {why}" for name, why in self.problems.items())
        super().__init__(f"tool policy refused: {detail}")


class ToolDescriptor(Contract):
    """What the engine knows about a registered tool: its name, schema and declared scopes.

    A descriptor is data. It has no reference to the tool, so holding one cannot run anything.
    """

    name: str
    schema_: dict[str, Any]
    permissions: frozenset[str]


def descriptors_from_registry(registry: Any) -> dict[str, ToolDescriptor]:
    """Snapshot ``name -> ToolDescriptor`` from a tool registry without executing any tool.

    Uses only ``list_tools()``, ``get(name)``, ``get_name()``, ``get_schema()`` and
    ``get_required_permissions()``. A tool that fails to describe itself is left out (so it is
    unknown, hence refused).
    """
    found: dict[str, ToolDescriptor] = {}
    for name in registry.list_tools():
        tool = registry.get(name)
        if tool is None:
            continue
        try:
            schema = dict(tool.get_schema() or {})
            perms = frozenset(str(getattr(p, "value", p)) for p in tool.get_required_permissions())
            found[name] = ToolDescriptor(name=name, schema_=schema, permissions=perms)
        except Exception:
            continue
    return found


class ResolvedToolPolicy(Contract):
    """The tools one agent may be offered. Empty for reasoning agents. Not an authorization."""

    allowed: frozenset[str] = frozenset()

    def permits(self, name: str) -> bool:
        return name in self.allowed


def resolve_tool_policy(
    spec: AgentSpec,
    catalog: Mapping[str, ToolDescriptor],
    *,
    ceiling: Iterable[str] = READ_ONLY_TOOL_CEILING,
    permission_ceiling: Iterable[str] = READ_ONLY_PERMISSIONS,
) -> ResolvedToolPolicy:
    """Intersect the spec's allowlist with the catalog and the ceiling, or raise ``ToolPolicyError``.

    A tool whose schema the trusted validator cannot enforce strictly is refused rather than offered
    with unchecked arguments.
    """
    if spec.kind is not AgentKind.ACTING:
        return ResolvedToolPolicy()

    names_ceiling = frozenset(ceiling)
    perms_ceiling = frozenset(permission_ceiling)
    problems: dict[str, str] = {}
    allowed: set[str] = set()
    for name in spec.tool_policy.allowed_tools:
        descriptor = catalog.get(name)
        if name in EXCLUDED_TOOLS:
            problems[name] = "excluded in this phase"
        elif descriptor is None:
            problems[name] = "not a registered tool"
        elif name not in names_ceiling:
            problems[name] = "outside the driver's tool ceiling"
        elif not descriptor.permissions:
            problems[name] = "declares no permission scope, so its power is unknown"
        elif not descriptor.permissions <= perms_ceiling:
            extra = ", ".join(sorted(descriptor.permissions - perms_ceiling))
            problems[name] = f"requires scopes beyond the ceiling ({extra})"
        elif why := schema_problem(descriptor.schema_):
            problems[name] = f"schema cannot be enforced ({why})"
        else:
            allowed.add(name)
    if problems:
        raise ToolPolicyError(problems)
    return ResolvedToolPolicy(allowed=frozenset(allowed))
