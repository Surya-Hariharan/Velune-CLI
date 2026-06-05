import asyncio
import os
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, patch, MagicMock

import pytest

from velune.core.errors.execution import SandboxError
from velune.execution.benchmarker import SubsystemBenchmarker
from velune.execution.command_spec import CommandSpec
from velune.tools.filesystem.read import ReadFile, ReadDirectory
from velune.tools.filesystem.write import WriteFile, CreateFile, DeleteFile
from velune.cognition.orchestrator import CouncilOrchestrator
from velune.execution.executor import ExecutionExecutor


@pytest.fixture
def temp_workspace():
    with tempfile.TemporaryDirectory() as td:
        yield Path(td)


def test_write_tools_enforce_workspace(temp_workspace):
    outside_path = temp_workspace.parent / "outside.txt"
    
    write_tool = WriteFile(workspace=temp_workspace)
    with pytest.raises(ValueError, match="is outside workspace"):
        asyncio.run(write_tool.execute(str(outside_path), "content"))

    create_tool = CreateFile(workspace=temp_workspace)
    with pytest.raises(ValueError, match="is outside workspace"):
        asyncio.run(create_tool.execute(str(outside_path)))

    delete_tool = DeleteFile(workspace=temp_workspace)
    with pytest.raises(ValueError, match="is outside workspace"):
        asyncio.run(delete_tool.execute(str(outside_path)))


def test_read_tools_enforce_workspace(temp_workspace):
    """ReadFile and ReadDirectory must reject paths outside the workspace boundary."""
    outside_path = temp_workspace.parent / "etc" / "passwd"

    read_tool = ReadFile(workspace=temp_workspace)
    with pytest.raises(ValueError, match="is outside workspace"):
        asyncio.run(read_tool.execute(str(outside_path)))

    read_dir_tool = ReadDirectory(workspace=temp_workspace)
    with pytest.raises(ValueError, match="is outside workspace"):
        asyncio.run(read_dir_tool.execute(str(temp_workspace.parent)))


def test_benchmarker_isolation_env(temp_workspace):
    benchmarker = SubsystemBenchmarker(workspace=temp_workspace)
    
    # We run a snippet that prints the env
    code = """
import os
print(f"VELUNE_BENCH:latency_ms=0.0")
print(f"VELUNE_BENCH:peak_rss_kb=0.0")
if "PYTHONNOUSERSITE" in os.environ:
    print("NO_USER_SITE_SET")
if "PYTHONSTARTUP" not in os.environ:
    print("STARTUP_CLEARED")
print(f"CWD:{os.getcwd()}")
"""
    # Temporarily set dangerous env vars on host
    os.environ["PYTHONSTARTUP"] = "/tmp/bad.py"
    try:
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="VELUNE_BENCH:latency_ms=1.0\nVELUNE_BENCH:peak_rss_kb=1.0\nNO_USER_SITE_SET\nSTARTUP_CLEARED")
            benchmarker.run_benchmark(code, "test")
            
            # Assert subprocess was called with stripped env
            called_env = mock_run.call_args[1]["env"]
            assert called_env.get("PYTHONNOUSERSITE") == "1"
            assert "PYTHONSTARTUP" not in called_env
            assert "PYTHONPATH" not in called_env
            
            # Assert cwd is a temp dir and not the workspace
            called_cwd = mock_run.call_args[1]["cwd"]
            assert called_cwd != str(temp_workspace)
    finally:
        del os.environ["PYTHONSTARTUP"]


@pytest.mark.asyncio
async def test_executor_offloads_sandbox_to_thread(temp_workspace):
    executor = ExecutionExecutor(workspace_path=temp_workspace)
    executor.sandbox.execute = MagicMock(return_value=MagicMock(exit_code=0, duration_ms=100))
    
    # We mock to_thread to verify it's called
    with patch("asyncio.to_thread", new_callable=AsyncMock) as mock_to_thread:
        mock_to_thread.return_value = MagicMock(exit_code=0, duration_ms=100)
        
        plan = MagicMock()
        plan.task_id = "test_task"
        step = MagicMock(id="step1", metadata={"command": "echo test"})
        plan.steps = [step]
        executor.planner.compile = MagicMock(return_value=MagicMock(topological_sort=lambda: [step]))
        
        await executor.execute_plan(plan)
        
        mock_to_thread.assert_called_once()
        assert mock_to_thread.call_args[0][0] == executor.sandbox.execute


@pytest.mark.asyncio
async def test_orchestrator_cancellation_propagates():
    orchestrator = CouncilOrchestrator(provider_registry=MagicMock(), mapper=MagicMock())
    orchestrator.execute_task = AsyncMock(side_effect=asyncio.CancelledError)
    
    stream_gen = orchestrator.stream("test prompt")
    
    async for chunk in stream_gen:
        pass
    
    # Verify state was saved before raising
    states = orchestrator._states
    assert len(states) == 1
    state = list(states.values())[0]
    assert state.error == "Cancelled"


@pytest.mark.asyncio
async def test_orchestrator_scans_repository_context():
    orchestrator = CouncilOrchestrator(provider_registry=MagicMock(), mapper=MagicMock())
    orchestrator.firewall.scan_file_for_injection = MagicMock(return_value={
        "is_safe": False,
        "quarantined": True,
        "neutralized_content": "Sanitized Context"
    })
    
    # Mock execute_task so it doesn't do real work
    orchestrator.execute_task = AsyncMock(return_value={})
    
    # Need to mock repository cognition injection
    mock_container = MagicMock()
    mock_repo = MagicMock()
    mock_repo.index.return_value = MagicMock(root_path="/tmp", files=[MagicMock(path="test.py", language=MagicMock(value="python"))])
    mock_container.has.return_value = True
    mock_container.get.return_value = mock_repo
    
    with patch("velune.kernel.registry.get_container", return_value=mock_container):
        async for chunk in orchestrator.stream("prompt"):
            pass
        
    orchestrator.firewall.scan_file_for_injection.assert_called_once()
    args = orchestrator.firewall.scan_file_for_injection.call_args[0]
    assert args[0] == "repo_context"
    # Execute task should receive the sanitized context
    orchestrator.execute_task.assert_called_once()
    assert orchestrator.execute_task.call_args[1]["repo_context"] == "Sanitized Context"
