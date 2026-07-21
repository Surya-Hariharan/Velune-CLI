"""Tool system."""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    # Static-only imports so type checkers resolve the real classes instead of
    # falling back to whatever they infer from __getattr__ below (pyright, with
    # no annotation to go on here, was resolving every dynamically-imported name
    # to the first concrete type in the function body — the abstract BaseTool
    # itself — and flagging every concrete subclass instantiation in
    # subsystems.py as "Cannot instantiate abstract class"). Never executed at
    # runtime; the lazy __getattr__ import path is unchanged.
    from velune.tools.base.registry import ToolRegistry as ToolRegistry
    from velune.tools.base.tool import BaseTool as BaseTool
    from velune.tools.code.navigate import FindReferences as FindReferences
    from velune.tools.code.navigate import GoToDefinition as GoToDefinition
    from velune.tools.code.search import SemanticCodeSearch as SemanticCodeSearch
    from velune.tools.code.search import SymbolSearch as SymbolSearch
    from velune.tools.filesystem.read import ReadDirectory as ReadDirectory
    from velune.tools.filesystem.read import ReadFile as ReadFile
    from velune.tools.filesystem.search import FindFiles as FindFiles
    from velune.tools.filesystem.search import GrepFiles as GrepFiles
    from velune.tools.filesystem.write import CreateFile as CreateFile
    from velune.tools.filesystem.write import DeleteFile as DeleteFile
    from velune.tools.filesystem.write import WriteFile as WriteFile
    from velune.tools.git.history import GitBlame as GitBlame
    from velune.tools.git.history import GitDiff as GitDiff
    from velune.tools.git.history import GitLog as GitLog
    from velune.tools.git.operations import GitCheckout as GitCheckout
    from velune.tools.git.operations import GitCommit as GitCommit
    from velune.tools.git.state import GitBranch as GitBranch
    from velune.tools.git.state import GitStatus as GitStatus
    from velune.tools.terminal.execute import ExecuteCommand as ExecuteCommand
    from velune.tools.terminal.history import TerminalHistory as TerminalHistory
    from velune.tools.web.fetch import WebFetch as WebFetch

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
