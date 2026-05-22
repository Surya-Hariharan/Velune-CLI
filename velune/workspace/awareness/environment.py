from __future__ import annotations

import os
from typing import Dict


class EnvironmentAwareness:
    """Provides awareness of environment and available tools."""

    def __init__(self):
        pass

    def get_environment_variables(self) -> Dict[str, str]:
        """Get relevant environment variables."""
        relevant_vars = [
            "PATH",
            "PYTHONPATH",
            "VIRTUAL_ENV",
            "CONDA_DEFAULT_ENV",
            "OPENAI_API_KEY",
            "ANTHROPIC_API_KEY",
        ]
        
        env_vars = {}
        for var in relevant_vars:
            if var in os.environ:
                # Don't expose API keys fully
                if "API_KEY" in var:
                    env_vars[var] = "***" + os.environ[var][-4:]
                else:
                    env_vars[var] = os.environ[var]
        
        return env_vars

    def check_tool_availability(self, tool: str) -> bool:
        """Check if a tool is available in PATH."""
        import shutil
        return shutil.which(tool) is not None

    def get_available_tools(self) -> list[str]:
        """Get list of commonly available tools."""
        common_tools = [
            "git",
            "python",
            "node",
            "npm",
            "docker",
            "make",
            "gcc",
            "clang",
        ]
        
        available = []
        for tool in common_tools:
            if self.check_tool_availability(tool):
                available.append(tool)
        
        return available
