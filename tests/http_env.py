"""Helpers for the HTTP connector tests (VAIR A3)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from tests.integration_fakes import make_env
from velune.permissions.boundary import Boundary
from velune.permissions.gate import Approval
from velune.permissions.policy import ExecutionMode, PolicyState, authorize
from velune.resources.base import ResourceResult
from velune.resources.bridge import ResourceOperationTool
from velune.resources.http.connector import HttpConnector


def http_connector(
    tmp_path: Path,
    *,
    trusted: tuple[str, ...] = (),
    environments: dict[str, Any] | None = None,
    workspace_trusted: bool = True,
    **settings: Any,
) -> HttpConnector:
    config: dict[str, Any] = {"trusted_origins": list(trusted), **settings}
    if environments is not None:
        config["environments"] = environments
    return HttpConnector(config=config, workspace=tmp_path, workspace_trusted=workspace_trusted)


def decide(
    connector: HttpConnector,
    action: str,
    params: dict[str, Any],
    mode: ExecutionMode = ExecutionMode.MANUAL,
    tmp_path: Path | None = None,
):
    """The permission decision for a call, exactly as the bridge describes it."""
    tool = ResourceOperationTool(connector, action)
    boundary = Boundary(tmp_path or connector.workspace or Path.cwd())
    actions = tool.describe_actions({"params": params}, boundary)
    return authorize(actions, PolicyState(mode=mode)), actions


def verdict(connector, action, params, mode=ExecutionMode.MANUAL, tmp_path=None) -> str:
    return decide(connector, action, params, mode, tmp_path)[0].verdict.value


async def authorized(
    connector: HttpConnector, action: str, params: dict[str, Any]
) -> ResourceResult:
    """Describe (which records what was authorized) and then execute — the gate's two steps."""
    connector.describe_actions(action, params)
    return await connector.execute(action, params)


def http_env(
    tmp_path: Path,
    connector: HttpConnector,
    mode: ExecutionMode = ExecutionMode.MANUAL,
    *,
    answer: Approval = Approval.ALLOW_ONCE,
    **kw,
):
    """The production wiring (manager, gate, tool source) around *connector*."""
    asked: list[Any] = []

    async def ask(tool_name: str, decision: Any) -> Approval:
        asked.append((tool_name, decision))
        return answer

    env = make_env(tmp_path, mode, connectors={"http": lambda: connector}, ask=ask, **kw)
    return env, asked


def write_collection(tmp_path: Path, document: dict[str, Any], name: str | None = None) -> Path:
    directory = tmp_path / ".velune" / "api" / "collections"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{name or document['name']}.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    return path
