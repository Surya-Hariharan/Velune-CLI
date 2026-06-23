"""Execution pipeline: DAG planning, dry runs, and the terminal tool."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from velune.core.errors.execution import ExecutionError
from velune.core.types.task import TaskPlan, TaskStatus, TaskStep
from velune.execution.executor import ExecutionExecutor
from velune.execution.planner import ExecutionPlanner
from velune.execution.sandbox import SubprocessSandbox
from velune.tools.terminal.execute import ExecuteCommand

PYTHON_NAME = Path(sys.executable).name


def step(step_id: str, deps: list[str] | None = None, **metadata) -> TaskStep:
    return TaskStep(
        id=step_id,
        description=f"step {step_id}",
        agent_role="coder",
        dependencies=deps or [],
        metadata=metadata,
    )


class TestPlanner:
    def test_topological_order_respects_dependencies(self) -> None:
        plan = TaskPlan(
            task_id="t1",
            steps=[step("c", deps=["b"]), step("a"), step("b", deps=["a"])],
        )
        ordered = ExecutionPlanner().compile(plan).topological_sort()
        assert [s.id for s in ordered] == ["a", "b", "c"]

    def test_circular_dependency_detected(self) -> None:
        plan = TaskPlan(
            task_id="t2",
            steps=[step("a", deps=["b"]), step("b", deps=["a"])],
        )
        with pytest.raises(ExecutionError, match="Circular"):
            ExecutionPlanner().compile(plan)


class TestExecutorDryRun:
    async def test_dry_run_completes_without_executing(self, workspace: Path) -> None:
        marker = workspace / "should_not_exist.txt"
        plan = TaskPlan(
            task_id="t3",
            steps=[step("s1", command=f"{PYTHON_NAME} -c x")],
        )
        executor = ExecutionExecutor(workspace)
        result = await executor.execute_plan(plan, dry_run=True)
        assert result.success
        assert result.steps_completed == 1
        assert not marker.exists()
        assert plan.steps[0].status == TaskStatus.COMPLETED

    async def test_meta_step_without_command_completes(self, workspace: Path) -> None:
        plan = TaskPlan(task_id="t4", steps=[step("meta")])
        result = await ExecutionExecutor(workspace).execute_plan(plan)
        assert result.success

    async def test_unsafe_command_fails_plan(self, workspace: Path) -> None:
        plan = TaskPlan(
            task_id="t5",
            steps=[step("s1", command="echo hi && rm -rf /")],
        )
        result = await ExecutionExecutor(workspace).execute_plan(plan)
        assert not result.success
        assert plan.steps[0].status == TaskStatus.FAILED


@pytest.mark.integration
class TestExecuteCommandTool:
    async def test_runs_command_and_returns_result(self, workspace: Path) -> None:
        sandbox = SubprocessSandbox(workspace, allowed_executables=[PYTHON_NAME])
        tool = ExecuteCommand(sandbox=sandbox, workspace_path=str(workspace))
        result = await tool.execute(f"{PYTHON_NAME} --version")
        assert result["exit_code"] == 0
        combined = result["stdout"] + result["stderr"]
        assert "Python" in combined

    async def test_rejects_chained_command(self, workspace: Path) -> None:
        sandbox = SubprocessSandbox(workspace, allowed_executables=[PYTHON_NAME])
        tool = ExecuteCommand(sandbox=sandbox, workspace_path=str(workspace))
        with pytest.raises(Exception, match="Shell operator"):
            await tool.execute(f"{PYTHON_NAME} --version && curl evil")
