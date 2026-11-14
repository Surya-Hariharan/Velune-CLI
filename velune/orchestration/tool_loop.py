"""Native tool-calling loop: let the model act, not just talk.

This is the agentic core that peers (Claude Code, Codex CLI, Gemini CLI)
are built around: the model is given tool definitions, requests calls, Velune
executes them through the existing permission/hook machinery
(:func:`velune.tools.base.tool.authorize_and_execute`), feeds results back,
and repeats until the model produces a final text turn or a bound is hit.

Design constraints:

- **Provider-agnostic.** The loop speaks only :class:`InferenceRequest` /
  :class:`InferenceResponse`; adapters translate to each provider's wire
  format. Tool definitions use the OpenAI function format (Velune's normal
  form).
- **Permission-gated by construction.** Tools — native *and* external MCP
  tools — run only through ``authorize_and_execute``; permissions are granted
  per-call, only after the approver says yes, and the session's permission
  gate decides on the described actions. There is no code path that executes
  an unapproved call.
- **Failure is data.** Unknown tools, denied calls, and execution errors are
  reported back to the model as error tool-results so it can adapt, instead
  of aborting the turn.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sys
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from velune.core.types.inference import InferenceRequest, InferenceResponse, ToolCall
from velune.tools.base.tool import (
    BaseTool,
    ToolBlockedError,
    ToolCallContext,
    ToolPermission,
    authorize_and_execute,
)

if TYPE_CHECKING:
    from velune.mcp.registry import MCPServerRegistry
    from velune.providers.base import ModelProvider
    from velune.tools.base.registry import ToolRegistry

logger = logging.getLogger("velune.orchestration.tool_loop")

# Read-only scopes that never need per-call approval.
READONLY_PERMISSIONS: frozenset[ToolPermission] = frozenset(
    {
        ToolPermission.FILESYSTEM_READ,
        ToolPermission.GIT_READ,
        ToolPermission.INTEGRATION_READ,
    }
)

# OpenAI tool names must match this; MCP server/tool names may not.
_NAME_SAFE = re.compile(r"[^a-zA-Z0-9_-]")

# Callback deciding whether a specific call may run. Receives the exposed tool
# name, the parsed arguments, and the permissions the call would be granted.
Approver = Callable[[str, dict[str, Any], "set[ToolPermission]"], Awaitable[bool]]

# Observer for loop progress ("turn", "tool_start", "tool_end", "final").
EventHook = Callable[[str, dict[str, Any]], None]


class DynamicToolSource(Protocol):
    """A provider of tools whose set can change *between* model turns.

    The loop never mutates the tool list inside a turn: it compares ``version``
    after each batch of tool calls and, if it changed, rebuilds the definitions
    for the next request. Every provider adapter receives a fresh, complete tool
    list on every request, so this needs no provider-specific support.

    A tool returned here is called through the same enforced entry point as any
    other (``authorize_and_execute``); being offered grants no permission. A tool
    may expose ``resolve_call(arguments) -> (tool, arguments)`` to be authorized as
    a different tool (the integration dispatcher is authorized as its target).
    """

    @property
    def version(self) -> int: ...

    def tools(self) -> list[BaseTool]: ...

    def unknown_tool_hint(self, name: str) -> str | None: ...


@dataclass(slots=True)
class ToolInvocation:
    """Record of one executed (or attempted) tool call."""

    call: ToolCall
    result: str
    error: bool = False
    duration_ms: float = 0.0
    source: str = "local"  # "local" | "mcp" | "integration"


@dataclass(slots=True)
class ToolLoopResult:
    """Outcome of a full tool-loop run."""

    content: str
    turns: int
    invocations: list[ToolInvocation] = field(default_factory=list)
    tokens_used: int = 0
    stop_reason: str = "completed"  # "completed" | "max_turns" | "no_tools"
    # Full message list (OpenAI normal form) including tool traffic, so the
    # caller can persist the real conversation state.
    messages: list[dict[str, Any]] = field(default_factory=list)


_MAX_RESULT_CHARS = 400
_MAX_TRANSCRIPT_CHARS = 4000


def format_tool_activity(
    invocations: list[ToolInvocation],
    *,
    max_result_chars: int = _MAX_RESULT_CHARS,
    max_total_chars: int = _MAX_TRANSCRIPT_CHARS,
) -> str:
    """Render executed tool calls as a compact record for conversation history.

    The REPL's ``_conversation`` is a list of plain text entries that get folded
    into WORKING_MEMORY — it is not an OpenAI message array, so the loop's native
    ``messages`` (with ``tool_calls`` and ``role="tool"``) cannot be spliced in
    directly: their ``content`` is None and the assembler drops empty content.

    Without this, every record of what the agent did was discarded at the turn
    boundary, so the next turn re-read the same files and re-ran the same
    commands. Results are truncated because the point is continuity, not a
    verbatim replay.
    """
    if not invocations:
        return ""

    lines: list[str] = ["[Tool activity from the previous turn]"]
    for inv in invocations:
        args = inv.call.arguments or {}
        rendered_args = ", ".join(f"{k}={v!r}" for k, v in list(args.items())[:4])
        if len(args) > 4:
            rendered_args += ", …"

        result = (inv.result or "").strip()
        if len(result) > max_result_chars:
            result = result[:max_result_chars] + f"… [{len(inv.result)} chars total]"
        result = result.replace("\n", "\n    ")

        marker = "FAILED" if inv.error else "ok"
        lines.append(f"- {inv.call.name}({rendered_args}) -> {marker}")
        if result:
            lines.append(f"    {result}")

    text = "\n".join(lines)
    if len(text) > max_total_chars:
        text = text[:max_total_chars] + "\n… [tool activity truncated]"
    return text


async def approve_readonly_only(
    name: str, arguments: dict[str, Any], permissions: set[ToolPermission]
) -> bool:
    """Default approver: allow only calls whose scopes are all read-only.

    MCP tools are remote calls (their permission set is ``{NETWORK_ACCESS}``)
    and therefore never satisfy the read-only test, so this policy denies them.
    """
    return bool(permissions) and permissions <= READONLY_PERMISSIONS


class ToolLoopRunner:
    """Bounded infer → execute-tools → append-results loop.

    Args:
        provider:      Any :class:`ModelProvider`; must support tool calling
                       for the loop to do anything beyond a single turn.
        registry:      Velune's local :class:`ToolRegistry` (may be None).
        mcp_registry:  Optional :class:`MCPServerRegistry`; connected servers'
                       tools are exposed alongside local ones.
        approver:      Async policy callback; defaults to
                       :func:`approve_readonly_only`. Return False to deny —
                       the denial is reported to the model, not raised.
        ctx:           Base :class:`ToolCallContext` (workspace, hooks,
                       session). Per-call permissions are injected on a copy;
                       any permissions pre-granted on this context are kept.
        max_turns:     Max model turns (a final text turn counts as one).
        max_result_chars: Tool output larger than this is truncated before
                       being sent back to the model.
        on_event:      Optional sync observer for progress events.
    """

    def __init__(
        self,
        provider: ModelProvider,
        registry: ToolRegistry | None = None,
        *,
        mcp_registry: MCPServerRegistry | None = None,
        approver: Approver | None = None,
        ctx: ToolCallContext | None = None,
        max_turns: int = 10,
        max_result_chars: int = 16_000,
        on_event: EventHook | None = None,
        dynamic_tools: DynamicToolSource | None = None,
    ) -> None:
        self._provider = provider
        self._registry = registry
        self._mcp = mcp_registry
        self._approver = approver or approve_readonly_only
        self._base_ctx = ctx
        self._max_turns = max(1, max_turns)
        self._max_result_chars = max_result_chars
        self._on_event = on_event
        # Anthropic's prompt cache (cache_control on the system prompt + first
        # user message) previously only ran through the Council path — every
        # loop turn here re-sent that stable prefix uncached, even though a
        # tool-calling conversation is exactly the case with the most turns
        # sharing one prefix. NoOp for every other provider, so this is a
        # dict-lookup no-op cost everywhere else.
        from velune.context.cache.manager import make_cache_manager

        self._cache_manager = make_cache_manager(getattr(provider, "provider_id", ""))
        # exposed (sanitized) name → ("local" | "mcp", BaseTool). MCP tools are
        # wrapped in MCPRegistryTool so they take the same gated path.
        self._route: dict[str, tuple[str, Any]] = {}
        self._executed_invocations: list[ToolInvocation] = []
        # Optional source whose tools can change between turns (see DynamicToolSource).
        self._dynamic = dynamic_tools
        self._dynamic_version: int | None = None

    @property
    def executed_invocations(self) -> list[ToolInvocation]:
        """Tool calls executed by the most recent ``run``, including a partial one.

        Lets an interrupted turn report the work that actually happened rather
        than reporting nothing.
        """
        return list(self._executed_invocations)

    # ── Tool definition assembly ─────────────────────────────────────────

    def build_tool_definitions(self) -> list[dict[str, Any]]:
        """OpenAI-format tool definitions for all local + MCP tools.

        Also (re)builds the routing table used at execution time. Name
        collisions resolve local-first; a colliding MCP tool keeps its
        server-qualified name, which is unique per registry.
        """
        self._route.clear()
        definitions: list[dict[str, Any]] = []

        if self._registry is not None:
            for name in self._registry.list_tools():
                tool = self._registry.get(name)
                if tool is None:
                    continue
                exposed = _NAME_SAFE.sub("_", name)[:64]
                self._route[exposed] = ("local", tool)
                definitions.append(
                    {
                        "type": "function",
                        "function": {
                            "name": exposed,
                            "description": tool.get_description(),
                            "parameters": tool.get_schema() or {"type": "object", "properties": {}},
                        },
                    }
                )

        if self._dynamic is not None:
            try:
                dynamic_tools = self._dynamic.tools()
                self._dynamic_version = self._dynamic.version
            except Exception as exc:  # a broken source must not break the turn
                logger.warning("Could not build dynamic tools: %s", exc)
                dynamic_tools = []
            for tool in dynamic_tools:
                exposed = _NAME_SAFE.sub("_", tool.get_name())[:64]
                if exposed in self._route:
                    continue  # native tools win
                self._route[exposed] = ("integration", tool)
                definitions.append(
                    {
                        "type": "function",
                        "function": {
                            "name": exposed,
                            "description": tool.get_description(),
                            "parameters": tool.get_schema() or {"type": "object", "properties": {}},
                        },
                    }
                )

        if self._mcp is not None:
            from velune.mcp.authorization import MCPRegistryTool

            try:
                mcp_tools = self._mcp.all_tools()
            except Exception as exc:
                logger.warning("Could not enumerate MCP tools: %s", exc)
                mcp_tools = []
            for info in mcp_tools:
                qualified = f"{info.server_name}_{info.name}" if info.server_name else info.name
                exposed = _NAME_SAFE.sub("_", qualified)[:64]
                if exposed in self._route:
                    continue  # local tools win; duplicate MCP names keep first
                self._route[exposed] = ("mcp", MCPRegistryTool(self._mcp, info, qualified))
                definitions.append(
                    {
                        "type": "function",
                        "function": {
                            "name": exposed,
                            "description": info.description or f"MCP tool from {info.server_name}",
                            "parameters": info.input_schema or {"type": "object", "properties": {}},
                        },
                    }
                )

        return definitions

    # ── Loop ─────────────────────────────────────────────────────────────

    async def run(self, request: InferenceRequest) -> ToolLoopResult:
        """Run the loop to completion starting from *request*.

        The request's ``messages`` are copied, never mutated. If the request
        does not already carry tools, definitions are built from the
        registries; if none exist, this degrades to a single plain turn.
        """
        messages: list[dict[str, Any]] = list(request.messages)
        tools = request.tools if request.tools is not None else self.build_tool_definitions()
        if not self._route and tools:
            # Caller supplied raw definitions without routes — rebuild routes
            # so registry-backed names still resolve.
            self.build_tool_definitions()

        invocations: list[ToolInvocation] = []
        # Exposed so a caller whose run is cancelled mid-loop can still recover
        # the calls that already executed. Their side effects are on disk; a
        # transcript that omits them would be a lie about what happened.
        self._executed_invocations = invocations
        tokens_used = 0
        # Identical calls that already failed (→ the path they targeted), and how often
        # they were re-requested.
        failed_calls: dict[tuple[str, str], str | None] = {}
        repeated = 0
        # Where the tools anchor relative paths; the same anchor keys the failure record.
        workspace = self._base_ctx.workspace if self._base_ctx else None

        for turn in range(1, self._max_turns + 1):
            req = request.model_copy(
                update={
                    # Snapshot: the live list keeps growing after this turn, and
                    # providers (or tests) may hold the request beyond the call.
                    "messages": list(messages),
                    "tools": tools or None,
                    # Never force tool use on iterated turns; the model must be
                    # able to finish with text.
                    "tool_choice": request.tool_choice if turn == 1 else "auto",
                }
            )
            req = self._cache_manager.prepare(req)
            self._emit("turn", {"turn": turn, "max_turns": self._max_turns})
            response: InferenceResponse = await self._infer_turn(req)
            self._cache_manager.record(response.metadata)
            tokens_used += response.tokens_used
            self._emit(
                "turn_end",
                {"turn": turn, "tool_calls": len(response.tool_calls or [])},
            )

            if not response.tool_calls:
                self._emit("final", {"turn": turn})
                messages.append({"role": "assistant", "content": response.content})
                return ToolLoopResult(
                    content=response.content,
                    turns=turn,
                    invocations=invocations,
                    tokens_used=tokens_used,
                    stop_reason="completed" if tools else "no_tools",
                    messages=messages,
                )

            messages.append(_assistant_tool_message(response))
            await self._preauthorize_batch(response.tool_calls)
            try:
                for call in response.tool_calls:
                    key = _call_key(call)
                    if key in failed_calls:
                        repeated += 1
                        invocation = ToolInvocation(
                            call=call,
                            result=(
                                "Error: not retried — this exact call already failed. Change "
                                "the arguments or the approach before trying again."
                            ),
                            error=True,
                        )
                    else:
                        invocation = await self._execute_call(call)
                        if invocation.error:
                            failed_calls[key] = _call_target(call, workspace)
                        else:
                            # A successful call on a path (typically a re-read after a
                            # conflict) means its state has been looked at again: an
                            # identical call that failed on that path may now succeed.
                            target = _call_target(call, workspace)
                            if target is not None:
                                for stale in [k for k, t in failed_calls.items() if t == target]:
                                    del failed_calls[stale]
                    invocations.append(invocation)
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": call.id,
                            "content": invocation.result,
                            **({"is_error": True} if invocation.error else {}),
                        }
                    )
            finally:
                gate = self._base_ctx.gate if self._base_ctx else None
                if gate is not None:
                    gate.clear_batch()
            if request.tools is None and self._dynamic_tools_changed():
                # The set of tools changed (e.g. an integration was enabled): the
                # next request carries the new, complete list. A call that failed
                # only because its tool was not offered yet (the model batched
                # "enable" with a use of it) must not be refused as "already
                # failed" now that the tool exists, so that memory is reset.
                tools = self.build_tool_definitions()
                failed_calls.clear()
                self._emit("tools_changed", {"turn": turn, "tools": len(tools)})
            if repeated >= _MAX_REPEATED_FAILURES:
                logger.warning("Tool loop stopped: repeated identical failing calls.")
                return ToolLoopResult(
                    content=(
                        "I stopped because I kept retrying a tool call that had already "
                        "failed, without changing anything. See the errors above."
                    ),
                    turns=turn,
                    invocations=invocations,
                    tokens_used=tokens_used,
                    stop_reason="repeated_failure",
                    messages=messages,
                )

        # Bound hit: one last chance to summarize is intentionally NOT taken —
        # returning honestly beats burning another turn.
        logger.warning("Tool loop hit max_turns=%d without a final answer.", self._max_turns)
        return ToolLoopResult(
            content="",
            turns=self._max_turns,
            invocations=invocations,
            tokens_used=tokens_used,
            stop_reason="max_turns",
            messages=messages,
        )

    # ── Execution ────────────────────────────────────────────────────────

    def _dynamic_tools_changed(self) -> bool:
        """True when the dynamic source now offers a different set of tools.

        A source that raises is treated as unchanged: a broken integration
        layer must never break the turn.
        """
        if self._dynamic is None:
            return False
        try:
            return self._dynamic.version != self._dynamic_version
        except Exception as exc:
            logger.warning("Dynamic tool source failed; keeping the current tools: %s", exc)
            return False

    async def _infer_turn(self, req: InferenceRequest) -> InferenceResponse:
        """One model turn — streamed when the provider supports streaming tool
        calls, so text deltas reach the UI in real time via ``content_delta``
        events; otherwise a plain blocking ``infer()``.

        The opt-in is the adapter class attribute
        ``SUPPORTS_STREAMING_TOOL_CALLS``: only adapters that accumulate
        tool-call deltas and emit a final ``metadata["tool_calls"]`` chunk set
        it. Streaming through an adapter without that support would silently
        drop the model's tool calls, so absence means "don't stream".
        """
        if not self._can_stream():
            return await self._provider.infer(req)

        start = time.perf_counter()
        parts: list[str] = []
        tool_calls: list[ToolCall] = []
        finish_reason: str | None = None
        async for chunk in self._provider.stream(req):
            if chunk.content:
                parts.append(chunk.content)
                self._emit("content_delta", {"text": chunk.content})
            metadata_calls = (chunk.metadata or {}).get("tool_calls")
            if metadata_calls:
                for item in metadata_calls:
                    tool_calls.append(item if isinstance(item, ToolCall) else ToolCall(**item))
            if chunk.finish_reason:
                finish_reason = chunk.finish_reason

        content = "".join(parts)
        return InferenceResponse(
            content=content,
            model_id=req.model_id,
            finish_reason="tool_calls" if tool_calls else (finish_reason or "stop"),
            # Streaming chunks carry no usage data; estimate like the legacy
            # render path (~4 chars/token) so cost display stays populated.
            tokens_used=len(content) // 4,
            latency_ms=(time.perf_counter() - start) * 1000.0,
            tool_calls=tool_calls or None,
        )

    def _can_stream(self) -> bool:
        if not getattr(self._provider, "SUPPORTS_STREAMING_TOOL_CALLS", False):
            return False
        try:
            caps = self._provider.get_capabilities()
        except Exception:
            return False
        return bool(getattr(caps, "supports_streaming", False))

    async def _execute_call(self, call: ToolCall) -> ToolInvocation:
        start = time.perf_counter()
        route = self._route.get(call.name)
        if route is None:
            hint = ""
            if self._dynamic is not None:
                try:
                    hint = self._dynamic.unknown_tool_hint(call.name) or ""
                except Exception:
                    hint = ""
            return ToolInvocation(
                call=call,
                result=f"Error: unknown tool '{call.name}'.{hint} Available tools: "
                + ", ".join(sorted(self._route)),
                error=True,
            )

        kind, target = route
        exit_code: Any = None
        name, arguments = call.name, call.arguments
        resolver = getattr(target, "resolve_call", None)
        if callable(resolver):
            # A dispatcher is authorized as the operation it stands for: the gate,
            # the approver and any session grant see the real tool and scope.
            try:
                target, arguments = resolver(call.arguments)
            except (LookupError, ValueError) as exc:
                return ToolInvocation(call=call, result=f"Error: {exc}", error=True, source=kind)
            name = target.get_name()
        # MCP tools report {NETWORK_ACCESS}: remote calls, so read-only
        # auto-approval policies never green-light them silently.
        permissions: set[ToolPermission] = set(target.get_required_permissions())

        try:
            approved = await self._approver(name, arguments, permissions)
        except Exception as exc:  # an approver crash must fail closed
            logger.warning("Approver raised for %s; denying call: %s", name, exc)
            approved = False
        if not approved:
            self._emit("tool_denied", {"id": call.id, "name": name})
            return ToolInvocation(
                call=call,
                result=f"Error: the user denied permission to run '{name}'.",
                error=True,
                duration_ms=(time.perf_counter() - start) * 1000.0,
                source=kind,
            )

        self._emit(
            "tool_start",
            {"id": call.id, "name": name, "arguments": arguments},
        )
        try:
            # Native and MCP tools alike: the enforced entry point (scope
            # check, permission gate, hooks). MCP calls no longer bypass it.
            result = await self._run_local(target, call, permissions, arguments)
            text = _stringify(result)
            error = False
            if isinstance(result, dict) and "exit_code" in result:
                exit_code = result.get("exit_code")
        except (ToolBlockedError, PermissionError) as exc:
            text, error = f"Error: {exc}", True
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.debug("Tool %s failed: %s", call.name, exc, exc_info=True)
            text, error = f"Error: {type(exc).__name__}: {exc}", True

        # Never hand secrets to the model: a file or command output may contain
        # API keys or tokens (e.g. a printed config). Same redaction the logs use.
        from velune.core.redaction import redact_secrets

        text = redact_secrets(text)
        if len(text) > self._max_result_chars:
            text = (
                text[: self._max_result_chars]
                + f"\n… [truncated {len(text) - self._max_result_chars} characters]"
            )
        duration = (time.perf_counter() - start) * 1000.0
        self._emit(
            "tool_end",
            {
                "id": call.id,
                "name": name,
                "error": error,
                "duration_ms": duration,
                "result": text[:2000],
                "exit_code": exit_code,
                "arguments": arguments,
            },
        )
        return ToolInvocation(
            call=call, result=text, error=error, duration_ms=duration, source=kind
        )

    async def _preauthorize_batch(self, calls: list[ToolCall]) -> None:
        """Let the gate confirm a turn's related changes with one prompt."""
        gate = self._base_ctx.gate if self._base_ctx else None
        if gate is None or len(calls) < 2:
            return
        described: list[tuple[str, list[Any]]] = []
        for call in calls:
            route = self._route.get(call.name)
            if route is None:
                continue
            try:
                tool, args, name = route[1], call.arguments, call.name
                resolver = getattr(tool, "resolve_call", None)
                if callable(resolver):
                    tool, args = resolver(call.arguments)
                    name = tool.get_name()
                described.append((name, tool.describe_actions(args, gate.boundary)))
            except Exception as exc:  # bad arguments: the call itself will report it
                logger.debug("describe_actions failed for %s: %s", call.name, exc)
        try:
            await gate.preauthorize(described)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # batching is a convenience; per-call checks still run
            logger.debug("batch pre-authorization failed: %s", exc)

    async def _run_local(
        self,
        tool: BaseTool,
        call: ToolCall,
        permissions: set[ToolPermission],
        arguments: dict[str, Any] | None = None,
    ) -> Any:
        """Run a local tool through the enforced permission/hook entry point."""
        base = self._base_ctx
        ctx = ToolCallContext(
            run_id=base.run_id if base else f"toolloop_{uuid.uuid4().hex[:8]}",
            actor=base.actor if base else "tool_loop",
            workspace=base.workspace if base else None,
            # Grant exactly what this approved call requires, plus anything
            # the caller pre-granted on the base context.
            permissions=(base.permissions if base else set()) | permissions,
            hook_dispatcher=base.hook_dispatcher if base else None,
            session_id=base.session_id if base else "",
            # The execution-mode gate must travel with every call; dropping it
            # here would make every state change fail closed.
            gate=base.gate if base else None,
        )
        args = call.arguments if arguments is None else arguments
        tool.validate_input(args)
        return await authorize_and_execute(tool, ctx, **args)

    def _emit(self, event: str, data: dict[str, Any]) -> None:
        if self._on_event is None:
            return
        try:
            self._on_event(event, data)
        except Exception as exc:
            logger.debug("on_event hook error (non-fatal): %s", exc)


# ── Helpers ─────────────────────────────────────────────────────────────────

# Identical failing calls the model may re-request before the loop stops.
_MAX_REPEATED_FAILURES = 3


def _target_key(raw: str, workspace: Path | None) -> str:
    """One key for the file or folder *raw* names, however the model spelled it.

    The location is resolved the way the filesystem tools resolve a path argument: relative
    paths are anchored to the workspace (not the process CWD), ``~`` is expanded, ``.``/``..``
    and doubled or foreign separators collapse, and symbolic links are followed, so a link and
    its target are one file. Nothing is checked or refused here: a location outside the
    workspace simply gets its own key, and the tool's own boundary check still decides whether
    it may be used. Case is folded only where the filesystem is case-insensitive by default,
    and Unicode normalization only where the filesystem normalizes names (macOS), so two
    distinct files are never merged.
    """
    base = workspace if workspace is not None else Path.cwd()
    try:
        candidate = Path(raw).expanduser()
        if not candidate.is_absolute():
            candidate = Path(base) / candidate
        resolved = str(candidate.resolve())
    except (OSError, RuntimeError, ValueError):
        # Not resolvable (an embedded NUL, a symlink loop, an unreadable parent): fall back to
        # the lexical location so the same spelling still maps to the same key.
        try:
            resolved = os.path.abspath(os.path.join(str(base), os.path.expanduser(raw)))
        except (OSError, RuntimeError, ValueError):
            resolved = raw
    if sys.platform == "win32":
        return os.path.normcase(resolved)
    if sys.platform == "darwin":
        from velune.tools.filesystem.mutation import fold_name

        return fold_name(resolved)
    return resolved


def _call_target(call: ToolCall, workspace: Path | None = None) -> str | None:
    """The file or folder a call acts on, when it names one (see :func:`_target_key`)."""
    args = call.arguments if isinstance(call.arguments, dict) else {}
    for key in ("file_path", "path", "source", "directory_path"):
        value = args.get(key)
        if isinstance(value, str) and value:
            return _target_key(value, workspace)
    return None


def _call_key(call: ToolCall) -> tuple[str, str]:
    try:
        args = json.dumps(call.arguments, sort_keys=True, default=str)
    except Exception:
        args = repr(call.arguments)
    return (call.name, args)


def _assistant_tool_message(response: InferenceResponse) -> dict[str, Any]:
    """Build the assistant message (OpenAI normal form) echoing tool calls."""
    return {
        "role": "assistant",
        "content": response.content or "",
        "tool_calls": [
            {
                "id": c.id,
                "type": "function",
                "function": {"name": c.name, "arguments": json.dumps(c.arguments)},
            }
            for c in (response.tool_calls or [])
        ],
    }


def _stringify(result: Any) -> str:
    """Render a tool result for the model."""
    if isinstance(result, str):
        return result
    try:
        return json.dumps(result, default=str, ensure_ascii=False, indent=None)
    except (TypeError, ValueError):
        return str(result)
