"""LangGraph-based orchestration engine with resilient fallbacks."""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from velune.kernel.bus import CognitiveBus
from velune.kernel.schemas import Event as KernelEvent
from velune.memory.lifecycle import MemoryArtifact, MemoryLifecycleCoordinator
from velune.memory.tiers.graph import GraphMemoryTier
from velune.orchestration.schemas import (
    AgentMessage,
    ExecutionAttempt,
    ExecutionStatus,
    OrchestrationRequest,
    OrchestrationResult,
    OrchestrationState,
)
from velune.cognition.verification import ReasoningVerifier

class ExecutionValidator:
    """Validates state quality before allowing orchestration to finalize."""

    def __init__(self, reasoning_verifier: ReasoningVerifier | None = None) -> None:
        self.verifier = reasoning_verifier or ReasoningVerifier()

    def validate(self, state: OrchestrationState) -> list[str]:
        issues: list[str] = []

        if not state.task_plan or not state.task_plan.steps:
            issues.append("missing_task_plan")

        if not state.retrieval_result or not state.retrieval_result.hits:
            issues.append("insufficient_retrieval_evidence")

        if not state.repository_snapshot:
            issues.append("missing_repository_snapshot")

        workspace_path = Path(state.request.workspace)
        if not workspace_path.exists():
            issues.append("workspace_not_found")

        if state.output and "TODO" in state.output:
            issues.append("incomplete_reasoning_output")

        # Wire ReasoningVerifier patch-auditing
        proposed_patches = state.execution_state.get("proposed_patches", [])
        for patch in proposed_patches:
            file_path = patch.get("file_path")
            proposed_code = patch.get("proposed_code", "")
            existing_code = patch.get("existing_code", "")

            if file_path:
                full_path = file_path
                if not Path(full_path).is_absolute():
                    full_path = str(workspace_path / file_path)

                audit = self.verifier.audit_patch(
                    file_path=full_path,
                    proposed_code=proposed_code,
                    existing_code=existing_code,
                )
                if not audit["passed"]:
                    for issue in audit["issues"]:
                        issues.append(f"patch_contradiction: {file_path} - {issue}")

        return issues

    def should_retry(self, issues: list[str], attempt: int, max_retries: int) -> bool:
        """Gate autonomous retry loops based on issue severity and budget."""

        if not issues:
            return False
        if attempt >= max_retries + 1:
            return False

        retryable = {
            "insufficient_retrieval_evidence",
            "incomplete_reasoning_output",
            "missing_task_plan",
        }
        # Mark contradiction issues as retryable so the agent gets a chance to replan and fix AST errors!
        for issue in issues:
            if issue.startswith("patch_contradiction:"):
                return True

        return any(issue in retryable for issue in issues)

from velune.planning.service import AdaptivePlanningService
from velune.repository.cognition import RepositoryCognitionService
from velune.retrieval.hybrid import HybridRetriever
from velune.retrieval.schemas import RetrievalQuery
from velune.tools.base.executor import ToolExecutionCoordinator
from velune.tools.base.registry import ToolRegistry
from velune.core.types.inference import InferenceRequest

logger = logging.getLogger("velune.orchestration.engine")

REASONING_TIMEOUT = 60.0
PLANNING_TIMEOUT = 60.0

REASONING_PROMPT_TEMPLATE = """You are the Reasoning Engine for an autonomous software engineering system.

TASK: {prompt}

WORKSPACE: {workspace}

EXECUTION PLAN:
{plan_summary}

RETRIEVED CONTEXT (top matches from codebase):
{context_text}

Your job: Analyze the task and context. Produce a concrete execution strategy.

Respond with a JSON object:
{{
  "strategy": "2-3 sentence description of the execution approach",
  "key_files": ["list of files most likely to need modification"],
  "risks": ["list of 1-3 potential issues to watch for"],
  "requires_tools": true/false,
  "confidence": 0.0-1.0
}}

Respond with ONLY the JSON object. No markdown. No explanation."""

try:
    from langgraph.graph import END, StateGraph
except ImportError:  # pragma: no cover - fallback path if dependency is missing.
    END = "__end__"
    StateGraph = None


class LangGraphOrchestrationEngine:
    """Stateful multi-agent orchestration engine for Velune execution."""

    def __init__(
        self,
        retrieval: HybridRetriever,
        repository_cognition: RepositoryCognitionService,
        memory_lifecycle: MemoryLifecycleCoordinator,
        graph_memory: GraphMemoryTier,
        tool_registry: ToolRegistry,
        event_bus: CognitiveBus | None = None,
        workspace_path: Path | None = None,
    ) -> None:
        self.retrieval = retrieval
        self.repository_cognition = repository_cognition
        self.memory_lifecycle = memory_lifecycle
        self.graph_memory = graph_memory
        self.tool_registry = tool_registry
        self.tool_executor = ToolExecutionCoordinator(tool_registry=tool_registry)
        self.event_bus = event_bus or CognitiveBus()

        self.planner = AdaptivePlanningService()
        self.validator = ExecutionValidator()
        self.workspace_path = workspace_path

        # Resolve shared SQLiteManager from container if available
        sqlite_manager = None
        try:
            from velune.kernel.registry import get_container
            container = get_container()
            if container.has("runtime.sqlite_manager"):
                sqlite_manager = container.get("runtime.sqlite_manager")
        except Exception:
            pass

        from velune.orchestration.checkpoints import SQLiteCheckpointStore
        db_path = None
        if self.workspace_path:
            db_path = Path(self.workspace_path) / ".velune" / "velune_cognitive_core.db"
        self.checkpoints = SQLiteCheckpointStore(db_path=db_path, sqlite_manager=sqlite_manager)

        self._states: dict[str, OrchestrationState] = {}
        self._interrupt_flags: set[str] = set()

    async def plan(self, prompt: str) -> dict[str, object]:
        """Produce an adaptive execution plan for the given prompt."""

        task_id = self._task_id(prompt)
        plan = self.planner.create_plan(task_id=task_id, prompt=prompt)
        return {
            "task_id": task_id,
            "steps": [step.model_dump() for step in plan.steps],
            "metadata": plan.metadata,
        }

    async def execute(self, prompt: str) -> dict[str, object]:
        """Execute an autonomous pipeline and return a structured result."""

        request = OrchestrationRequest(prompt=prompt, workspace=str(Path.cwd()))
        state = await self.execute_request(request)
        result = self._result_from_state(state)
        return result.model_dump()

    async def execute_request(self, request: OrchestrationRequest) -> OrchestrationState:
        """Execute an explicit orchestration request contract."""

        run_id = request.task_id or f"run-{uuid.uuid4().hex[:12]}"
        state = OrchestrationState(
            run_id=run_id,
            request=request,
            status=ExecutionStatus.IN_PROGRESS,
            task_state={"task_id": self._task_id(request.prompt)},
            execution_state={"max_retries": request.max_retries, "attempt": 0},
            retrieval_state={},
            memory_state={},
            repository_state={},
            context_state={},
        )
        self._states[run_id] = state



        await self._publish("orchestration.started", run_id, {"prompt": request.prompt})

        final_state = await self._execute_nodes(state, emit_fn=None)
        self._states[run_id] = final_state

        await self._publish(
            "orchestration.completed",
            run_id,
            {
                "status": final_state.status.value,
                "issues": final_state.validation_issues,
                "attempts": len(final_state.attempts),
            },
        )
        return final_state

    async def stream(self, prompt: str) -> AsyncIterator[str]:
        """Stream orchestration milestones for real-time CLI feedback."""

        request = OrchestrationRequest(prompt=prompt, workspace=str(Path.cwd()))
        run_id = request.task_id or f"run-{uuid.uuid4().hex[:12]}"

        state = OrchestrationState(
            run_id=run_id,
            request=request,
            status=ExecutionStatus.IN_PROGRESS,
            task_state={"task_id": self._task_id(request.prompt)},
            execution_state={"max_retries": request.max_retries, "attempt": 0},
            retrieval_state={},
            memory_state={},
            repository_state={},
            context_state={},
        )
        self._states[run_id] = state

        queue = asyncio.Queue()
        task = asyncio.create_task(self._execute_nodes(state, emit_fn=lambda s: queue.put_nowait(s)))

        while not task.done() or not queue.empty():
            try:
                milestone = await asyncio.wait_for(queue.get(), timeout=0.1)
                yield milestone
                queue.task_done()
            except TimeoutError:
                continue

        final_state = await task
        self._states[run_id] = final_state

    async def interrupt(self, run_id: str) -> bool:
        """Request interruption for a running orchestration."""

        if run_id not in self._states:
            return False
        self._interrupt_flags.add(run_id)
        await self._publish("orchestration.interrupt_requested", run_id, {})
        return True

    async def resume(self, run_id: str) -> dict[str, object]:
        """Resume the latest checkpoint for a run."""

        state = self.checkpoints.latest(run_id)
        if state is None:
            return {"run_id": run_id, "resumed": False, "reason": "checkpoint_not_found"}

        state.status = ExecutionStatus.IN_PROGRESS
        state.updated_at = datetime.now(tz=UTC)
        resumed = await self._execute_nodes(state, emit_fn=None)
        self._states[run_id] = resumed
        return {
            "run_id": run_id,
            "resumed": True,
            "status": resumed.status.value,
            "issues": resumed.validation_issues,
        }

    def get_state(self, run_id: str) -> OrchestrationState | None:
        return self._states.get(run_id)

    async def _execute_nodes(
        self,
        state: OrchestrationState,
        emit_fn: Callable[[str], None] | None = None,
    ) -> OrchestrationState:
        """Single consolidated execution path for orchestration state graph."""
        run_id = state.run_id

        async def emit(milestone: str):
            if emit_fn:
                if asyncio.iscoroutinefunction(emit_fn):
                    await emit_fn(milestone)
                else:
                    emit_fn(milestone)

        # 1. Context Reconstruction
        await emit(f"[{run_id}] context reconstruction")
        state = await self._context_reconstruction_node(state)
        if state.status in {ExecutionStatus.FAILED, ExecutionStatus.INTERRUPTED}:
            return state

        # 2. Planning
        await emit(f"[{run_id}] planning")
        state = await self._planning_node(state)
        if state.status in {ExecutionStatus.FAILED, ExecutionStatus.INTERRUPTED}:
            return state

        # 3. Validation Loop
        while True:
            await emit(f"[{run_id}] retrieval")
            state = await self._retrieval_node(state)
            if state.status in {ExecutionStatus.FAILED, ExecutionStatus.INTERRUPTED}:
                return state

            await emit(f"[{run_id}] reasoning")
            state = await self._reasoning_node(state)
            if state.status in {ExecutionStatus.FAILED, ExecutionStatus.INTERRUPTED}:
                return state

            await emit(f"[{run_id}] tool execution")
            state = await self._tool_execution_node(state)
            if state.status in {ExecutionStatus.FAILED, ExecutionStatus.INTERRUPTED}:
                return state

            await emit(f"[{run_id}] validation")
            state = await self._validation_node(state)
            if state.status in {ExecutionStatus.FAILED, ExecutionStatus.INTERRUPTED}:
                return state

            if self._route_after_validation(state) == "retry":
                await emit(f"[{run_id}] retrying")
                state = await self._replan_node(state)
                continue
            break

        # 4. Review
        await emit(f"[{run_id}] review")
        state = await self._review_node(state)
        if state.status in {ExecutionStatus.FAILED, ExecutionStatus.INTERRUPTED}:
            return state

        # 5. Finalize
        await emit(f"[{run_id}] finalize")
        state = await self._finalize_node(state)

        if state.error:
            await emit(f"[{run_id}] failed: {state.error}")
        else:
            await emit(f"[{run_id}] completed")

        return state

    async def _context_reconstruction_node(self, state: OrchestrationState) -> OrchestrationState:
        if self._check_interrupt(state):
            return state

        import time
        force_reindex = state.request.metadata.get("force_reindex", False)
        
        logger.info(
            "Repository index: force=%s, workspace=%s",
            force_reindex,
            self.repository_cognition.root_path
        )
        logger.info(
            "Repository index: force=%s, cache=%s",
            force_reindex,
            'hit' if not force_reindex else 'bypassed'
        )
        
        start_time = time.perf_counter()
        snapshot = self.repository_cognition.index(force=force_reindex)
        elapsed = time.perf_counter() - start_time
        
        logger.info("Repository index completed in %.2fs", elapsed)
        logger.info(
            "Repository index completed in %.2fs (%d files)",
            elapsed,
            len(snapshot.files)
        )

        state.repository_snapshot = snapshot
        state.repository_state = snapshot.summary
        state.context_state = {
            "intent": state.request.prompt,
            "workspace": state.request.workspace,
            "repository_summary": snapshot.summary,
        }
        state.agent_messages.append(
            AgentMessage(
                sender="repository_analyst",
                receiver="planner",
                content="Repository context reconstructed",
                metadata=snapshot.summary,
            )
        )
        await self._checkpoint("context_reconstruction", state)
        return state

    async def _planning_node(self, state: OrchestrationState) -> OrchestrationState:
        if self._check_interrupt(state):
            return state

        import os
        use_llm = os.environ.get("VELUNE_LLM_ORCHESTRATION", "false").lower() == "true"

        task_id = str(state.task_state.get("task_id") or self._task_id(state.request.prompt))

        if use_llm:
            provider = self._get_reasoning_provider()
            model_id = self._get_model_id_for_role("planner")
            if provider is not None:
                plan = await self.planner.create_plan_with_llm(
                    task_id=task_id,
                    prompt=state.request.prompt,
                    provider=provider,
                    model_id=model_id,
                    repository_summary=state.repository_state,
                )
            else:
                plan = self.planner.create_plan(
                    task_id=task_id,
                    prompt=state.request.prompt,
                    repository_summary=state.repository_state,
                )
        else:
            plan = self.planner.create_plan(
                task_id=task_id,
                prompt=state.request.prompt,
                repository_summary=state.repository_state,
            )

        state.task_plan = plan
        state.task_state["plan_steps"] = len(plan.steps)
        state.agent_messages.append(
            AgentMessage(
                sender="planner",
                receiver="retriever",
                content=f"Plan generated: {len(plan.steps)} steps",
                metadata={"strategy": plan.metadata.get("strategy", "keyword")},
            )
        )
        await self._checkpoint("planning", state)
        return state

    async def _retrieval_node(self, state: OrchestrationState) -> OrchestrationState:
        if self._check_interrupt(state):
            return state

        query = RetrievalQuery(text=state.request.prompt, top_k=8)
        result = await self.retrieval.retrieve(query=query)
        state.retrieval_result = result
        state.retrieval_state = {
            "hits": len(result.hits),
            "strategy": result.strategy,
        }
        state.agent_messages.append(
            AgentMessage(
                sender="retriever",
                receiver="reasoner",
                content=f"Retrieved {len(result.hits)} candidate contexts",
                metadata={"strategy": result.strategy},
            )
        )
        await self._checkpoint("retrieval", state)
        return state

    async def _reasoning_node(self, state: OrchestrationState) -> OrchestrationState:
        if self._check_interrupt(state):
            return state

        import os
        use_llm = os.environ.get("VELUNE_LLM_ORCHESTRATION", "false").lower() == "true"

        def apply_legacy_reasoning():
            hit_count = len(state.retrieval_result.hits) if state.retrieval_result else 0
            plan_steps = len(state.task_plan.steps) if state.task_plan else 0
            reasoning = (
                f"Execution strategy for '{state.request.prompt}': "
                f"{plan_steps} planned steps with {hit_count} retrieval candidates. "
                "Proceed with bounded tool execution and validation-first loops."
            )
            state.output = reasoning
            state.execution_state["reasoning"] = reasoning
            state.execution_state["requires_tools"] = True

        if not use_llm:
            apply_legacy_reasoning()
            await self._checkpoint("reasoning", state)
            return state

        # LLM-backed reasoning node
        provider = self._get_reasoning_provider()
        if provider is None:
            logger.warning("No provider available for reasoning node; using deterministic fallback")
            apply_legacy_reasoning()
            await self._checkpoint("reasoning", state)
            return state

        # Build reasoning context from retrieval hits
        context_snippets = []
        if state.retrieval_result:
            for hit in state.retrieval_result.hits[:5]:  # Top 5 hits only
                snippet = hit.document.content[:500]  # Truncate for context
                context_snippets.append(f"[{hit.source.value}] {snippet}")

        context_text = "\n---\n".join(context_snippets) if context_snippets else "No context retrieved."

        plan_summary = ""
        if state.task_plan:
            plan_summary = "\n".join([
                f"  {i+1}. [{step.agent_role}] {step.description}"
                for i, step in enumerate(state.task_plan.steps[:10])
            ])

        reasoning_prompt = REASONING_PROMPT_TEMPLATE.format(
            prompt=state.request.prompt,
            workspace=state.request.workspace,
            plan_summary=plan_summary or "No plan generated yet.",
            context_text=context_text,
        )

        try:
            model_id = self._get_model_id_for_role("reasoning")
            request = InferenceRequest(
                model_id=model_id,
                messages=[{"role": "user", "content": reasoning_prompt}],
                temperature=0.3,
                max_tokens=500,
            )
            response = await asyncio.wait_for(
                provider.infer(request),
                timeout=REASONING_TIMEOUT,
            )

            # Parse JSON response
            content = response.content.strip()
            if content.startswith("```"):
                content = content.split("```")[1]
                if content.startswith("json"):
                    content = content[4:]
                content = content.split("```")[0].strip()

            reasoning_data = json.loads(content)
            strategy = reasoning_data.get("strategy", "Proceed with standard execution.")

            state.output = strategy
            state.execution_state["reasoning"] = strategy
            state.execution_state["requires_tools"] = reasoning_data.get("requires_tools", True)
            state.execution_state["key_files"] = reasoning_data.get("key_files", [])
            state.execution_state["risks"] = reasoning_data.get("risks", [])
            state.execution_state["confidence"] = reasoning_data.get("confidence", 0.7)

        except asyncio.TimeoutError:
            logger.warning("Reasoning node LLM timeout; using empty strategy")
            apply_legacy_reasoning()
        except (json.JSONDecodeError, KeyError) as e:
            logger.warning("Reasoning node JSON parse error: %s; using raw response", e)
            logger.debug("Raw reasoning response: %s", response.content if 'response' in locals() else "")
            state.execution_state["reasoning"] = response.content[:500] if 'response' in locals() else "Parse error"
            state.execution_state["requires_tools"] = True
        except Exception as e:
            logger.error("Reasoning node LLM call failed: %s", e)
            apply_legacy_reasoning()

        state.agent_messages.append(
            AgentMessage(
                sender="reasoner",
                receiver="execution",
                content="Reasoning complete",
                metadata={"strategy_length": len(state.execution_state.get("reasoning") or "")},
            )
        )
        await self._checkpoint("reasoning", state)
        return state

    async def _tool_execution_node(self, state: OrchestrationState) -> OrchestrationState:
        if self._check_interrupt(state):
            return state

        state.execution_state["attempt"] = int(state.execution_state.get("attempt", 0)) + 1
        attempt = ExecutionAttempt(attempt=int(state.execution_state["attempt"]))

        tool_output: dict[str, Any] = {
            "executions": [],
            "warnings": [],
        }

        if self.tool_registry.has("read_directory"):
            result = await self.tool_executor.execute(
                tool_name="read_directory",
                arguments={"directory_path": state.request.workspace},
                run_id=state.run_id,
                actor="execution",
            )
            tool_output["executions"].append(result.model_dump())
            if not result.success:
                attempt.issues.append("read_directory_failed")

        if self.tool_registry.has("git_status"):
            result = await self.tool_executor.execute(
                tool_name="git_status",
                arguments={"directory": state.request.workspace},
                run_id=state.run_id,
                actor="execution",
            )
            tool_output["executions"].append(result.model_dump())
            if not result.success:
                attempt.issues.append("git_status_failed")

        attempt.success = len(attempt.issues) == 0
        attempt.completed_at = datetime.now(tz=UTC)

        state.attempts.append(attempt)
        state.execution_state["tool_output"] = tool_output
        state.agent_messages.append(
            AgentMessage(
                sender="execution",
                receiver="validator",
                content="Tool execution complete",
                metadata={"attempt": attempt.attempt},
            )
        )
        await self._checkpoint("tool_execution", state)
        return state

    async def _validation_node(self, state: OrchestrationState) -> OrchestrationState:
        if self._check_interrupt(state):
            return state

        state.status = ExecutionStatus.VALIDATING
        issues = self.validator.validate(state)
        state.validation_issues = issues

        attempt = len(state.attempts) if state.attempts else 1
        should_retry = self.validator.should_retry(
            issues=issues,
            attempt=attempt,
            max_retries=state.request.max_retries,
        )

        if should_retry:
            state.status = ExecutionStatus.RETRYING
        elif issues:
            state.status = ExecutionStatus.FAILED
            state.error = ", ".join(issues)
        else:
            state.status = ExecutionStatus.IN_PROGRESS

        state.agent_messages.append(
            AgentMessage(
                sender="validator",
                receiver="planner" if should_retry else "reviewer",
                content="Validation completed",
                metadata={"issues": issues, "retry": should_retry},
            )
        )
        await self._checkpoint("validation", state)
        return state

    async def _replan_node(self, state: OrchestrationState) -> OrchestrationState:
        if self._check_interrupt(state):
            return state

        if state.task_plan is not None:
            state.task_plan = self.planner.replan(
                state.task_plan,
                feedback=state.validation_issues,
            )
        state.agent_messages.append(
            AgentMessage(
                sender="planner",
                receiver="retriever",
                content="Plan revised for retry execution",
                metadata={"issues": state.validation_issues},
            )
        )
        await self._checkpoint("replan", state)
        return state

    async def _review_node(self, state: OrchestrationState) -> OrchestrationState:
        if self._check_interrupt(state):
            return state

        reviewer_notes = {
            "plan_steps": len(state.task_plan.steps) if state.task_plan else 0,
            "attempts": len(state.attempts),
            "issues": state.validation_issues,
            "status": state.status.value,
        }
        state.execution_state["review"] = reviewer_notes
        state.agent_messages.append(
            AgentMessage(
                sender="reviewer",
                receiver="memory",
                content="Execution review completed",
                metadata=reviewer_notes,
            )
        )
        await self._checkpoint("review", state)
        return state

    async def _finalize_node(self, state: OrchestrationState) -> OrchestrationState:
        if self._check_interrupt(state):
            return state

        success = state.status not in {ExecutionStatus.FAILED, ExecutionStatus.INTERRUPTED}
        if success:
            state.status = ExecutionStatus.COMPLETED
            state.error = None
            if not state.output:
                state.output = "Execution completed with validated orchestration flow."
        else:
            state.output = state.output or "Execution ended with validation failures."

        artifact = MemoryArtifact(
            id=f"artifact-{state.run_id}",
            memory_type="episodic",
            content=state.output,
            importance=0.8 if success else 0.5,
            metadata={
                "run_id": state.run_id,
                "issues": state.validation_issues,
                "attempts": len(state.attempts),
            },
        )
        self.memory_lifecycle.ingest(artifact)
        self.graph_memory.upsert_entity(state.run_id, "execution_run", status=state.status.value)
        for issue in state.validation_issues:
            issue_id = f"issue:{issue}"
            self.graph_memory.upsert_entity(issue_id, "validation_issue")
            self.graph_memory.upsert_relationship(state.run_id, issue_id, "detected")

        state.memory_state = self.memory_lifecycle.summary()
        await self._checkpoint("finalize", state)
        return state

    def _route_after_validation(self, state: OrchestrationState) -> str:
        if state.status == ExecutionStatus.RETRYING:
            return "retry"
        return "review"

    def _check_interrupt(self, state: OrchestrationState) -> bool:
        if state.run_id in self._interrupt_flags:
            state.status = ExecutionStatus.INTERRUPTED
            state.error = "execution_interrupted"
            self._interrupt_flags.discard(state.run_id)
            return True
        return False

    async def _checkpoint(self, node_name: str, state: OrchestrationState) -> None:
        state.updated_at = datetime.now(tz=UTC)
        checkpoint_id = self.checkpoints.save(state.run_id, node_name, state)
        state.checkpoints = self.checkpoints.list_ids(state.run_id)
        await self._publish(
            "orchestration.checkpoint",
            state.run_id,
            {"node": node_name, "checkpoint_id": checkpoint_id},
        )

    async def _publish(self, event_type: str, run_id: str, payload: dict[str, Any]) -> None:
        event = KernelEvent(
            event_type=event_type,
            data={"run_id": run_id, **payload},
            source="orchestration",
        )
        await self.event_bus.emit(event)

    def _result_from_state(self, state: OrchestrationState) -> OrchestrationResult:
        return OrchestrationResult(
            run_id=state.run_id,
            task_id=str(state.task_state.get("task_id", state.run_id)),
            success=state.status == ExecutionStatus.COMPLETED,
            status=state.status,
            output=state.output,
            error=state.error,
            plan_steps=len(state.task_plan.steps) if state.task_plan else 0,
            attempts=len(state.attempts),
            validation_issues=state.validation_issues,
            metadata={
                "checkpoints": state.checkpoints,
                "repository_summary": state.repository_state,
                "memory_summary": state.memory_state,
            },
        )

    def _task_id(self, prompt: str) -> str:
        compact = "-".join(prompt.lower().split())[:48]
        return compact or "task"

    def _get_reasoning_provider(self):
        """Get the best available provider for reasoning tasks."""
        try:
            from velune.kernel.registry import get_container
            container = get_container()
            if container.has("runtime.provider_registry"):
                registry = container.get("runtime.provider_registry")
                config = container.get("runtime.config") if container.has("runtime.config") else None
                provider_name = "openai"
                if config and hasattr(config, "providers"):
                    provider_name = config.providers.default_provider
                return registry.get(provider_name)
        except Exception as e:
            logger.debug("Could not get reasoning provider: %s", e)
        return None

    def _get_model_id_for_role(self, role: str) -> str:
        """Get configured model ID for a given role, with fallback."""
        try:
            from velune.kernel.registry import get_container
            container = get_container()
            if container.has("runtime.model_registry"):
                registry = container.get("runtime.model_registry")
                config = container.get("runtime.config") if container.has("runtime.config") else None
                provider_name = config.providers.default_provider if config else "openai"
                models = registry.get_by_provider(provider_name)
                if models:
                    return models[0].model_id
        except Exception:
            pass
        return "gpt-4o-mini"  # Safe fallback
