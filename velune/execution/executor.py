"""DAG task execution manager coordinates sandbox runs, state saves, and validations."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

from velune.core.errors.execution import SandboxError
from velune.core.trace import TracedLogger
from velune.core.types.task import TaskPlan, TaskResult, TaskStatus
from velune.execution.command_spec import CommandSpec
from velune.execution.planner import ExecutionPlanner
from velune.execution.rollback import RollbackManager
from velune.execution.sandbox import SubprocessSandbox
from velune.execution.validator import PostExecutionValidator

logger = TracedLogger("velune.execution.executor")


class ExecutionExecutor:
    """Executes a TaskPlan DAG inside the isolated sandbox, handling validations and rollbacks."""

    def __init__(
        self,
        workspace_path: Path,
        config: Any | None = None,
        bus: Any | None = None,
    ) -> None:
        self.workspace_path = Path(workspace_path).resolve()
        allowed = config.execution.allowed_executables if config and config.execution else None
        self.sandbox = SubprocessSandbox(self.workspace_path, allowed_executables=allowed, bus=bus)
        self.validator = PostExecutionValidator(self.workspace_path, self.sandbox)
        self.rollback_manager = RollbackManager(self.workspace_path)
        self.planner = ExecutionPlanner()

    async def execute_plan(self, plan: TaskPlan, dry_run: bool = False) -> TaskResult:
        """Process a task plan DAG.

        Sequentially runs each step, performing stashes, runs, validations, and rollbacks.
        """
        start_time = time.perf_counter()
        steps_completed = 0
        total_steps = len(plan.steps)

        # 1. Compile plan into topological order
        try:
            dag = self.planner.compile(plan)
            ordered_steps = dag.topological_sort()
        except Exception as e:
            duration_ms = (time.perf_counter() - start_time) * 1000
            return TaskResult(
                task_id=plan.task_id,
                success=False,
                error=f"Plan compilation failed: {e}",
                steps_completed=0,
                steps_total=total_steps,
                execution_time_ms=duration_ms,
            )

        # 2. Run steps sequentially
        for step in ordered_steps:
            from velune.core.trace import TraceContext

            with TraceContext(run_id=plan.task_id, step_id=step.id):
                logger.info("Executing step %s: %s", step.id, step.description)
                step.status = TaskStatus.IN_PROGRESS

                # Extract step details from metadata
                cmd = step.metadata.get("command")
                expected_files = [Path(f) for f in step.metadata.get("expected_files", [])]
                syntax_files = [Path(f) for f in step.metadata.get("syntax_check_files", [])]
                test_cmd = step.metadata.get("test_command")
                timeout = float(step.metadata.get("timeout", 60.0))

                if dry_run:
                    logger.info("[DRY RUN] Would execute command: %s", cmd)
                    step.status = TaskStatus.COMPLETED
                    steps_completed += 1
                    continue

                if not cmd:
                    # Meta steps with no command automatically complete
                    step.status = TaskStatus.COMPLETED
                    steps_completed += 1
                    continue

                try:
                    spec = CommandSpec.from_string(cmd, cwd=self.workspace_path, timeout=timeout)
                except SandboxError as e:
                    logger.error("Rejected unsafe command from step %s: %s", step.id, e)
                    if self.sandbox:
                        self.sandbox.emit_rejection(cmd, str(e))
                    step.status = TaskStatus.FAILED
                    break

                # Capture snapshot state of expected and syntax check files before execution
                files_to_track = list(set(expected_files + syntax_files))
                checkpoint_id = f"cp-{step.id}-{int(time.time())}"

                logger.info("Saving checkpoint state for step %s...", step.id)
                checkpoint_state = self.rollback_manager.save_state(checkpoint_id, files_to_track)

                # Execute command inside sandbox (offloaded to prevent event loop blocking)
                try:
                    sandbox_res = await asyncio.to_thread(self.sandbox.execute, spec)
                    logger.info(
                        "Command completed with exit code %d in %.2fms",
                        sandbox_res.exit_code,
                        sandbox_res.duration_ms,
                    )

                    if sandbox_res.exit_code != 0:
                        # Task failure triggered rollback
                        logger.error(
                            "Command failed with exit code %d.\nSTDOUT:\n%s\nSTDERR:\n%s",
                            sandbox_res.exit_code,
                            sandbox_res.stdout,
                            sandbox_res.stderr,
                        )
                        self.rollback_manager.rollback(checkpoint_state)
                        step.status = TaskStatus.FAILED
                        break

                except Exception as e:
                    logger.error("Command execution triggered system error: %s", e)
                    self.rollback_manager.rollback(checkpoint_state)
                    step.status = TaskStatus.FAILED
                    break

                # Validate postconditions (validator may run subprocesses with a
                # blocking poll loop — offload like the sandbox execution above)
                try:
                    validation_res = await asyncio.to_thread(
                        self.validator.validate,
                        expected_files=expected_files,
                        syntax_check_files=syntax_files,
                        test_command=test_cmd,
                    )

                    if not validation_res.success:
                        logger.error(
                            "Step postconditions validation failed: %s", validation_res.errors
                        )
                        self.rollback_manager.rollback(checkpoint_state)
                        step.status = TaskStatus.FAILED
                        break

                except Exception as e:
                    logger.error("Post-execution validation process errored: %s", e)
                    self.rollback_manager.rollback(checkpoint_state)
                    step.status = TaskStatus.FAILED
                    break

                # Success! Drop the stash if git is active and successful
                if checkpoint_state.get("git_active") and checkpoint_state.get("git_stash_success"):
                    try:
                        self.rollback_manager.git_tracker.drop_stash()
                    except Exception as e:
                        logger.warning(
                            "Could not drop stash after successful step execution: %s", e
                        )

                # Success! Mark completed
                step.status = TaskStatus.COMPLETED
                steps_completed += 1

        duration_ms = (time.perf_counter() - start_time) * 1000
        success = steps_completed == total_steps

        return TaskResult(
            task_id=plan.task_id,
            success=success,
            error=None
            if success
            else f"Execution failed during step: {[s.id for s in ordered_steps if s.status == TaskStatus.FAILED]}",
            steps_completed=steps_completed,
            steps_total=total_steps,
            execution_time_ms=duration_ms,
            metadata={"dry_run": dry_run},
        )
