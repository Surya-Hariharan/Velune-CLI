from velune.kernel.bootstrap import RuntimeEnvironment, SubsystemModule


def _create_tool_registry(env: RuntimeEnvironment):
    import logging

    from velune.tools import (
        CreateFile,
        DeleteFile,
        ExecuteCommand,
        FindFiles,
        FindReferences,
        GitBlame,
        GitBranch,
        GitCheckout,
        GitCommit,
        GitDiff,
        GitLog,
        GitStatus,
        GoToDefinition,
        GrepFiles,
        ReadDirectory,
        ReadFile,
        SemanticCodeSearch,
        SymbolSearch,
        TerminalHistory,
        WebFetch,
        WriteFile,
    )
    from velune.tools.base.registry import ToolRegistry

    logger = logging.getLogger("velune.tools.module")

    execution_executor = env.container.get("runtime.execution_executor")
    job_registry = (
        env.container.get("runtime.job_registry")
        if env.container.has("runtime.job_registry")
        else None
    )

    tool_registry = ToolRegistry()
    execute_cmd_tool = ExecuteCommand(
        sandbox=execution_executor.sandbox,
        workspace_path=str(env.workspace),
        job_registry=job_registry,
    )
    ws = env.workspace
    default_tools = [
        ReadFile(workspace=ws),
        ReadDirectory(workspace=ws),
        # confirm=False: the REPL tool loop's approver owns preview/approval;
        # the tools' own console preview would render to detached raw stdout
        # and its blocking prompt would collide with the fullscreen app.
        WriteFile(workspace=ws, confirm=False),
        CreateFile(workspace=ws, confirm=False),
        DeleteFile(workspace=ws, confirm=False),
        GrepFiles(workspace=ws),
        FindFiles(workspace=ws),
        GitLog(workspace=ws),
        GitDiff(workspace=ws),
        GitBlame(workspace=ws),
        GitStatus(workspace=ws),
        GitBranch(workspace=ws),
        GitCommit(workspace=ws),
        GitCheckout(workspace=ws),
        execute_cmd_tool,
        TerminalHistory(),
        SemanticCodeSearch(workspace=ws),
        SymbolSearch(workspace=ws),
        GoToDefinition(workspace=ws),
        FindReferences(workspace=ws),
        WebFetch(),
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
