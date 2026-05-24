import pytest
from pathlib import Path
from velune.tools.terminal.execute import ExecuteCommand
from velune.execution.sandbox import SubprocessSandbox
from velune.core.errors.execution import SandboxError

@pytest.mark.asyncio
async def test_execute_command_sandbox_rejections(tmp_path):
    # Setup a subprocess sandbox for a temporary workspace directory
    sandbox = SubprocessSandbox(tmp_path)
    tool = ExecuteCommand(sandbox=sandbox, workspace_path=str(tmp_path))
    
    # 1. Dangerous command blocking (rm -rf)
    with pytest.raises(SandboxError, match="Command contains blocked pattern or is unsafe"):
        await tool.execute("rm -rf /")
        
    # 2. Blocked utilities (curl)
    with pytest.raises(SandboxError, match="Command contains blocked pattern or is unsafe"):
        await tool.execute("curl https://example.com")

@pytest.mark.asyncio
async def test_execute_command_safe_execution(tmp_path):
    sandbox = SubprocessSandbox(tmp_path)
    tool = ExecuteCommand(sandbox=sandbox, workspace_path=str(tmp_path))
    
    # Execute a safe command
    result = await tool.execute("echo hello")
    
    assert "exit_code" in result
    assert result["exit_code"] == 0
    assert "stdout" in result
    assert result["stdout"].strip() == "hello"
    assert "duration_ms" in result
    assert result["duration_ms"] > 0
