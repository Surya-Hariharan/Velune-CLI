"""Build what a swarm run needs from the runtime container.

Shared by every entry point: ``/council`` in the REPL, ``velune ask``,
``velune run`` and the MCP council tool. The container is
always passed in.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

from velune.permissions import ExecutionMode
from velune.swarm.agent import AgentEnv, ModelChoice
from velune.swarm.library import SkillLibrary
from velune.swarm.run import SwarmOutcome, SwarmSettings, run_swarm

_log = logging.getLogger("velune.swarm.setup")

SNAPSHOT_TIMEOUT_S = 5.0  # a cold repository index must not hold up a run
CONTEXT_TOKENS = 6_000  # repository summary given to every agent


def _optional(container: Any, key: str) -> Any:
    try:
        return container.get(key)
    except Exception:
        return None


def workspace_of(container: Any) -> Path:
    workspace = _optional(container, "runtime.workspace")
    return Path(workspace) if workspace else Path.cwd()


def runs_root(workspace: Path) -> Path:
    from velune.core.paths import workspace_storage_dir

    return workspace_storage_dir(workspace) / "runs"


def pick_model(container: Any, preferred: Any = None) -> tuple[Any, Any]:
    """The model and provider to run with: *preferred* if usable, else the first usable one.

    *preferred* is anything with ``provider_id`` and ``model_id`` (the saved
    default model). Returns ``(None, None)`` when nothing can be used.
    """
    from velune.models.registry import model_usable

    models = _optional(container, "runtime.model_registry")
    providers = _optional(container, "runtime.provider_registry")
    if models is None or providers is None:
        return None, None
    candidates = list(models.list_all())
    if preferred is not None:
        candidates.sort(
            key=lambda m: (
                not (m.model_id == preferred.model_id and m.provider_id == preferred.provider_id)
            )
        )
    for model in candidates:
        if not model_usable(model, providers):
            continue
        provider = providers.get(model.provider_id)
        if provider is not None:
            return model, provider
    return None, None


def supports_tools(provider: Any) -> bool:
    try:
        return bool(getattr(provider.get_capabilities(), "supports_function_calling", False))
    except Exception:
        return False


def swarm_settings(config: Any, model: Any) -> SwarmSettings:
    """Settings from the ``[swarm]`` section; one agent at a time on a local model server."""
    from velune.kernel.config import SwarmConfig

    cfg = getattr(config, "swarm", None) or SwarmConfig()
    local = bool(getattr(model, "is_local", False))
    return SwarmSettings(
        max_seconds=cfg.max_seconds,
        max_calls=cfg.max_calls,
        max_tokens=cfg.max_tokens,
        turns_per_agent=cfg.turns_per_agent,
        parallel=cfg.parallel_local if local else cfg.parallel_cloud,
        parallel_cloud=cfg.parallel_cloud,
        parallel_local=cfg.parallel_local,
    )


def find_model(container: Any, ref: str) -> tuple[Any, Any, str]:
    """``(model, provider, problem)`` for a ``"provider/model"`` reference.

    *model* is None when the reference names no known model. *problem* says why
    the reference cannot be used right now ("" when it can).
    """
    from velune.models.registry import model_usable

    provider_id, _, model_id = ref.partition("/")
    models = _optional(container, "runtime.model_registry")
    providers = _optional(container, "runtime.provider_registry")
    model = None
    if models is not None and provider_id and model_id:
        model = models.get(model_id, provider_id)
    if model is None:
        return None, None, "is not a known model (write it as provider/model; see /models)"
    try:
        usable = providers is not None and model_usable(model, providers)
        provider = providers.get(model.provider_id) if usable else None
    except Exception as exc:
        _log.debug("Provider for %s unavailable: %s", ref, exc)
        provider = None
    return model, provider, "" if provider is not None else "is not available right now"


def role_models(
    container: Any, config: Any, tools_for: Callable[[Any, Any], bool]
) -> dict[str, ModelChoice]:
    """``[swarm.models]``, resolved. *tools_for* says whether a model may use tools."""
    configured = getattr(getattr(config, "swarm", None), "models", None) or {}
    choices: dict[str, ModelChoice] = {}
    for role, ref in configured.items():
        model, provider, problem = find_model(container, ref)
        if problem:
            choices[role.lower()] = ModelChoice(ref, problem=problem)
            continue
        choices[role.lower()] = ModelChoice(
            ref,
            provider=provider,
            model_id=model.model_id,
            context_window=int(getattr(model, "context_length", 0) or 32_768),
            tools=tools_for(model, provider),
            local=bool(getattr(model, "is_local", False)),
        )
    return choices


async def repository_summary(container: Any, max_tokens: int = CONTEXT_TOKENS) -> str:
    """A short repository summary, wrapped as untrusted content ("" if unavailable)."""
    summary, _drift = await repository_texts(container, max_tokens)
    return summary


async def repository_texts(container: Any, max_tokens: int) -> tuple[str, str]:
    """The repository summary and architectural-drift notes, each wrapped as untrusted.

    Reads the repository cache (a full index only on a cold start, bounded by a
    timeout). Text that looks like an injection is neutralized, never refused.
    Either part is "" when unavailable.
    """
    service = _optional(container, "runtime.repository_cognition")
    if not service:
        return "", ""
    try:
        fresh = getattr(service, "get_snapshot_fresh", None)
        if fresh is not None:
            snapshot = await asyncio.wait_for(fresh(), timeout=SNAPSHOT_TIMEOUT_S)
        else:
            snapshot = service.get_snapshot()
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # includes the timeout: the run goes on without it
        _log.debug("Repository snapshot unavailable: %s", exc)
        return "", ""
    if snapshot is None:
        return "", ""
    try:
        from velune.repository.context_builder import WorkspaceContextBuilder

        text, drift = WorkspaceContextBuilder().build(
            snapshot,
            delta=None,
            max_snapshot_tokens=max_tokens,
            api_map=getattr(snapshot, "api_map", None),
        )
    except Exception as exc:
        _log.debug("Repository summary failed: %s", exc)
        return "", ""
    return (
        wrap_untrusted(container, "repository_context", text) if text else "",
        wrap_untrusted(container, "architectural_drift", drift) if drift else "",
    )


def wrap_untrusted(container: Any, name: str, content: str) -> str:
    """Mark workspace text as data, neutralizing anything that looks like an injection."""
    firewall = _optional(container, "runtime.firewall")
    if firewall is None:
        from velune.cognition.firewall import CognitiveFirewall

        firewall = CognitiveFirewall()
    try:
        scan = firewall.scan_file_for_injection(name, content)
        if scan.get("quarantined"):
            content = scan.get("neutralized_content", "")
        return firewall.wrap_workspace_content(name, content)
    except Exception:
        return content


def load_library(container: Any, plugin_skills: list | None = None) -> SkillLibrary:
    from velune.core import trust

    workspace = workspace_of(container)
    try:
        trusted = bool(trust.is_trusted(workspace))
    except Exception:
        trusted = False
    return SkillLibrary.load(
        workspace=workspace, trusted=trusted, plugin_skills=plugin_skills or []
    )


async def run_headless(
    container: Any,
    task: str,
    *,
    mode: ExecutionMode = ExecutionMode.PLAN,
    skill: str | None = None,
    permission: str | None = None,
    ask: Any = None,
    max_seconds: float | None = None,
    preferred: Any = None,
    ui: Any = None,
    context: str | None = None,
) -> SwarmOutcome:
    """Run *task* with no screen (``velune ask``/``run``, MCP).

    *ask* answers approval requests (``(tool_name, decision) -> Approval``);
    without it anything that needs approval is refused. *context* replaces the
    repository summary (already wrapped as untrusted). Raises ``RuntimeError``
    when no model is usable and ``RouteError`` for an unknown skill or profile.
    """
    from velune.permissions.boundary import Boundary
    from velune.permissions.gate import PermissionGate
    from velune.permissions.policy import PolicyState
    from velune.tools.base.tool import ToolCallContext

    model, provider = pick_model(container, preferred)
    if model is None:
        raise RuntimeError("No usable model is configured. Run `velune models scan`.")
    workspace = workspace_of(container)
    gate = PermissionGate(state=PolicyState(), boundary=Boundary(workspace), ask=ask)
    gate.state.mode = mode
    ctx = ToolCallContext(run_id="swarm", actor="swarm", workspace=workspace)
    ctx.gate = gate
    config = _optional(container, "runtime.config")
    settings = swarm_settings(config, model)
    if max_seconds is not None:
        settings.max_seconds = min(settings.max_seconds, max_seconds)
    env = AgentEnv(
        provider=provider,
        model_id=model.model_id,
        registry=_optional(container, "runtime.tool_registry"),
        ctx=ctx,
        context=context if context is not None else await repository_summary(container),
        context_window=int(getattr(model, "context_length", 0) or 32_768),
        tools=supports_tools(provider),
        local=bool(getattr(model, "is_local", False)),
        role_models=role_models(container, config, lambda _model, p: supports_tools(p)),
    )
    return await run_swarm(
        task,
        load_library(container),
        env,
        runs_root=runs_root(workspace),
        settings=settings,
        ui=ui,
        skill=skill,
        permission=permission,
        mode=mode,
    )
