"""Full repository indexing pipeline."""

from pathlib import Path
from typing import Dict, list
from velune.repository.scanner.filesystem import FilesystemScanner
from velune.repository.ast.parser import ASTParser
from velune.repository.ast.extractors.python import PythonSymbolExtractor
from velune.repository.ast.chunker import ASTAwareChunker
from velune.repository.semantic.summarizer import SemanticSummarizer
from velune.repository.graph.store import RepositoryGraphStore
from velune.core.types import FileNode, SymbolNode


class RepositoryIndexer:
    """Full repository indexing pipeline."""

    def __init__(self, root_path: Path):
        self.root_path = root_path
        self.scanner = FilesystemScanner(root_path)
        self.parser = ASTParser()
        self.chunker = ASTAwareChunker()
        self.summarizer = SemanticSummarizer()
        self.graph_store = RepositoryGraphStore()
        
        # Symbol extractors
        self.extractors = {
            "python": PythonSymbolExtractor(),
        }

    async def index(self) -> Dict[str, any]:
        """Index the entire repository."""
        files = self.scanner.scan_code_files()
        
        results = {
            "files_indexed": 0,
            "symbols_extracted": 0,
            "chunks_created": 0,
        }
        
        for file_path in files:
            await self.index_file(file_path)
            results["files_indexed"] += 1
        
        return results

    async def index_file(self, file_path: Path) -> None:
        """Index a single file."""
        # Add to graph
        self.graph_store.add_file_node(str(file_path))
        
        # Parse AST
        ast_tree = self.parser.parse(file_path)
        if not ast_tree:
            return
        
        # Extract symbols
        language = self.parser._detect_language(file_path)
        if language in self.extractors:
            symbols = self.extractors[language].extract(ast_tree, str(file_path))
            
            for symbol in symbols:
                self.graph_store.add_function_node(
                    f"{file_path}::{symbol.name}",
                    str(file_path),
                )
        
        # Chunk file
        with open(file_path, "r", encoding="utf-8") as f:
            code = f.read()
        
        chunks = self.chunker.chunk_file(file_path, code)
