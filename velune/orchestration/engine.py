"""LangGraph-based orchestration engine with resilient fallbacks."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Optional

from velune.events.bus.engine import Event, EventBus
from velune.memory.tiers.graph import GraphMemoryTier
from velune.memory.lifecycle import MemoryArtifact, MemoryLifecycleCoordinator
from velune.orchestration.checkpoints import InMemoryCheckpointStore
from velune.orchestration.schemas import (
    AgentMessage,
    ExecutionAttempt,
    ExecutionStatus,
    OrchestrationRequest,
    OrchestrationResult,
    OrchestrationState,
)
from velune.orchestration.validators import ExecutionValidator
from velune.planning.service import AdaptivePlanningService
from velune.repository.schemas import RepositorySnapshot
from velune.repository.cognition import RepositoryCognitionService
from velune.retrieval.hybrid import HybridRetriever
from velune.retrieval.schemas import RetrievalQuery
from velune.tools.base.executor import ToolExecutionCoordinator
from velune.tools.base.registry import ToolRegistry

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
        event_bus: Optional[EventBus] = None,
    ) -> None:
        self.retrieval = retrieval
        self.repository_cognition = repository_cognition
        self.memory_lifecycle = memory_lifecycle
        self.graph_memory = graph_memory
        self.tool_registry = tool_registry
        self.tool_executor = ToolExecutionCoordinator(tool_registry=tool_registry)
        self.event_bus = event_bus or EventBus()

        self.planner = AdaptivePlanningService()
        self.validator = ExecutionValidator()
        self.checkpoints = InMemoryCheckpointStore()
        self._states: dict[str, OrchestrationState] = {}
        self._interrupt_flags: set[str] = set()

        self._graph = self._build_graph() if StateGraph is not None else None

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

        if not self.event_bus._running:
            await self.event_bus.start()

        await self._publish("orchestration.started", run_id, {"prompt": request.prompt})

        final_state = await self._run_graph(state)
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
        )
        self._states[run_id] = state

        yield f"[{run_id}] context reconstruction"
        state = await self._context_reconstruction_node(state)

        yield f"[{run_id}] planning"
        state = await self._planning_node(state)

        while True:
            yield f"[{run_id}] retrieval"
            state = await self._retrieval_node(state)

            yield f"[{run_id}] reasoning"
            state = await self._reasoning_node(state)

            yield f"[{run_id}] tool execution"
            state = await self._tool_execution_node(state)

            yield f"[{run_id}] validation"
            state = await self._validation_node(state)

            if self._route_after_validation(state) == "retry":
                yield f"[{run_id}] retrying"
                state = await self._replan_node(state)
                continue
            break

        yield f"[{run_id}] review"
        state = await self._review_node(state)

        yield f"[{run_id}] finalize"
        state = await self._finalize_node(state)
        self._states[run_id] = state

        if state.error:
            yield f"[{run_id}] failed: {state.error}"
        else:
            yield f"[{run_id}] completed"

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
        resumed = await self._run_graph(state)
        self._states[run_id] = resumed
        return {
            "run_id": run_id,
            "resumed": True,
            "status": resumed.status.value,
            "issues": resumed.validation_issues,
        }

    def get_state(self, run_id: str) -> Optional[OrchestrationState]:
        return self._states.get(run_id)

    def _build_graph(self):
        if StateGraph is None:
            return None

        graph = StateGraph(dict)
        graph.add_node("context_reconstruction", self._graph_node(self._context_reconstruction_node))
        graph.add_node("planning", self._graph_node(self._planning_node))
        graph.add_node("retrieval", self._graph_node(self._retrieval_node))
        graph.add_node("reasoning", self._graph_node(self._reasoning_node))
        graph.add_node("tool_execution", self._graph_node(self._tool_execution_node))
        graph.add_node("validation", self._graph_node(self._validation_node))
        graph.add_node("replan", self._graph_node(self._replan_node))
        graph.add_node("review", self._graph_node(self._review_node))
        graph.add_node("finalize", self._graph_node(self._finalize_node))

        graph.set_entry_point("context_reconstruction")
        graph.add_edge("context_reconstruction", "planning")
        graph.add_edge("planning", "retrieval")
        graph.add_edge("retrieval", "reasoning")
        graph.add_edge("reasoning", "tool_execution")
        graph.add_edge("tool_execution", "validation")
        graph.add_conditional_edges(
            "validation",
            self._route_after_validation,
            {
                "retry": "replan",
                "review": "review",
            },
        )
        graph.add_edge("replan", "retrieval")
        graph.add_edge("review", "finalize")
        graph.add_edge("finalize", END)
        return graph.compile()

    async def _run_graph(self, state: OrchestrationState) -> OrchestrationState:
        if self._graph is None:
            return await self._run_fallback_sequence(state)

        current_state = state
        payload = {"state": current_state.model_dump(mode="json")}
        result = await self._graph.ainvoke(payload)
        if isinstance(result, dict) and "state" in result:
            return OrchestrationState.model_validate(result["state"])
        return current_state

    async def _run_fallback_sequence(self, state: OrchestrationState) -> OrchestrationState:
        nodes = [
            self._context_reconstruction_node,
            self._planning_node,
            self._retrieval_node,
            self._reasoning_node,
            self._tool_execution_node,
            self._validation_node,
        ]

        current = state
        for node in nodes:
            current = await node(current)
            if current.status in {ExecutionStatus.FAILED, ExecutionStatus.INTERRUPTED}:
                return current

        while self._route_after_validation(current) == "retry":
            current = await self._replan_node(current)
            current = await self._retrieval_node(current)
            current = await self._reasoning_node(current)
            current = await self._tool_execution_node(current)
            current = await self._validation_node(current)
            if current.status in {ExecutionStatus.FAILED, ExecutionStatus.INTERRUPTED}:
                return current

        current = await self._review_node(current)
        current = await self._finalize_node(current)
        return current

    def _graph_node(self, handler):
        async def wrapped(payload: dict[str, Any]) -> dict[str, Any]:
            state = OrchestrationState.model_validate(payload["state"])
            state = await handler(state)
            return {"state": state.model_dump(mode="json")}

        return wrapped

    async def _context_reconstruction_node(self, state: OrchestrationState) -> OrchestrationState:
        if self._check_interrupt(state):
            return state

        workspace = Path(state.request.workspace)
        snapshot = self.repository_cognition.index(workspace)
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

        task_id = str(state.task_state.get("task_id") or self._task_id(state.request.prompt))
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
                content=f"Plan generated with {len(plan.steps)} steps",
            )
        )
        await self._checkpoint("planning", state)
        return state

    async def _retrieval_node(self, state: OrchestrationState) -> OrchestrationState:
        if self._check_interrupt(state):
            return state

        query = RetrievalQuery(text=state.request.prompt, top_k=8)
        result = self.retrieval.search(query=query)
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
        state.agent_messages.append(
            AgentMessage(
                sender="reasoner",
                receiver="execution",
                content="Reasoning complete, tool execution authorized",
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
        event = Event(
            event_type=event_type,
            data={"run_id": run_id, **payload},
            timestamp=datetime.now(tz=UTC).timestamp(),
            source="orchestration",
        )
        await self.event_bus.publish(event)

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
