"""Repository Cognition Engine for AST parsing, Git tracking, and dependency graphs."""

from velune.repository.ast_parser import ASTParser, Symbol, SymbolKind
from velune.repository.cognition import RepositoryCognitionService
from velune.repository.parser import RepositorySnapshotParser
from velune.repository.rename_journal import RenameJournal
from velune.repository.schemas import (
    RepositoryEdge,
    RepositoryFile,
    RepositoryLanguage,
    RepositorySnapshot,
    RepositorySymbol,
    RepositorySymbolKind,
)
from velune.repository.symbol_registry import SymbolRegistry

__all__ = [
    "RepositoryCognitionService",
    "RepositoryLanguage",
    "RepositorySymbolKind",
    "RepositorySymbol",
    "RepositoryFile",
    "RepositoryEdge",
    "RepositorySnapshot",
    # Async AST parser (canonical for SymbolRegistry / RenameJournal)
    "ASTParser",
    "Symbol",
    "SymbolKind",
    "SymbolRegistry",
    "RenameJournal",
    # Sync snapshot parser (used by indexer / incremental indexer / tools)
    "RepositorySnapshotParser",
]
