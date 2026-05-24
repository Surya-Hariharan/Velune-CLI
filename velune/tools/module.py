from velune.kernel.bootstrap import SubsystemModule, RuntimeEnvironment

def _create_tool_registry(env: RuntimeEnvironment):
    import logging
    from velune.tools.base.registry import ToolRegistry
    from velune.tools import (
        ReadFile, ReadDirectory, WriteFile, CreateFile, DeleteFile,
        GrepFiles, FindFiles, GitLog, GitDiff, GitBlame, GitStatus, GitBranch,
        GitCommit, GitCheckout, ExecuteCommand, TerminalHistory,
        SemanticCodeSearch, SymbolSearch, GoToDefinition, FindReferences, WebFetch
    )
    
    logger = logging.getLogger("velune.tools.module")
    
    execution_executor = env.container.get("runtime.execution_executor")
    
    tool_registry = ToolRegistry()
    execute_cmd_tool = ExecuteCommand(
        sandbox=execution_executor.sandbox,
        workspace_path=str(env.workspace)
    )
    default_tools = [
        ReadFile(), ReadDirectory(), WriteFile(), CreateFile(), DeleteFile(),
        GrepFiles(), FindFiles(), GitLog(), GitDiff(), GitBlame(), GitStatus(), GitBranch(),
        GitCommit(), GitCheckout(), execute_cmd_tool, TerminalHistory(),
        SemanticCodeSearch(), SymbolSearch(), GoToDefinition(), FindReferences(), WebFetch()
    ]
    for tool in default_tools:
        tool_registry.register(tool)

    broken = tool_registry.list_broken_tools()
    if broken:
        logger.warning("Tools failed validation: %s", broken)
        
    return tool_registry

TOOL_MODULES = [
    SubsystemModule(
        name="tool_registry",
        factory=_create_tool_registry,
        container_key="runtime.tool_registry",
        dependencies=["runtime.execution_executor"],
    )
]
