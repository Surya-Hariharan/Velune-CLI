"""Tool system."""



__all__ = [
    "BaseTool",
    "ToolRegistry",
    "ReadFile",
    "ReadDirectory",
    "WriteFile",
    "CreateFile",
    "DeleteFile",
    "GrepFiles",
    "FindFiles",
    "GitLog",
    "GitDiff",
    "GitBlame",
    "GitStatus",
    "GitBranch",
    "GitCommit",
    "GitCheckout",
    "ExecuteCommand",
    "TerminalHistory",
    "SemanticCodeSearch",
    "SymbolSearch",
    "GoToDefinition",
    "FindReferences",
    "WebFetch",
]

def __getattr__(name: str):
    if name == "BaseTool":
        from velune.tools.base.tool import BaseTool
        return BaseTool
    if name == "ToolRegistry":
        from velune.tools.base.registry import ToolRegistry
        return ToolRegistry
    if name in {"FindReferences", "GoToDefinition"}:
        import velune.tools.code.navigate as mod
        return getattr(mod, name)
    if name in {"SemanticCodeSearch", "SymbolSearch"}:
        import velune.tools.code.search as mod
        return getattr(mod, name)
    if name in {"ReadDirectory", "ReadFile"}:
        import velune.tools.filesystem.read as mod
        return getattr(mod, name)
    if name in {"FindFiles", "GrepFiles"}:
        import velune.tools.filesystem.search as mod
        return getattr(mod, name)
    if name in {"CreateFile", "DeleteFile", "WriteFile"}:
        import velune.tools.filesystem.write as mod
        return getattr(mod, name)
    if name in {"GitBlame", "GitDiff", "GitLog"}:
        import velune.tools.git.history as mod
        return getattr(mod, name)
    if name in {"GitCheckout", "GitCommit"}:
        import velune.tools.git.operations as mod
        return getattr(mod, name)
    if name in {"GitBranch", "GitStatus"}:
        import velune.tools.git.state as mod
        return getattr(mod, name)
    if name == "ExecuteCommand":
        from velune.tools.terminal.execute import ExecuteCommand
        return ExecuteCommand
    if name == "TerminalHistory":
        from velune.tools.terminal.history import TerminalHistory
        return TerminalHistory
    if name == "WebFetch":
        from velune.tools.web.fetch import WebFetch
        return WebFetch
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
