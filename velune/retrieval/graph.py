"""Knowledge and repository AST dependency graph traversal retriever."""


from velune.kernel.registry import ComponentRegistry
from velune.repository.cognition import RepositoryCognitionService
from velune.retrieval.schemas import RetrievalDocument, RetrievalHit, RetrievalSource


class GraphRetriever:
    """Traverses knowledge graphs and repository AST structures to fetch contiguous context."""

    def __init__(self) -> None:
        self.registry = ComponentRegistry()

    def retrieve(self, node_id: str, depth: int = 1, top_k: int = 10) -> list[RetrievalHit]:
        """Traverses adjacent AST and symbol imports from the repository cognition service."""
        hits: list[RetrievalHit] = []

        try:
            # Try to grab the active RepositoryCognitionService from the kernel registry
            repo_service = self.registry.get(RepositoryCognitionService)
            if not repo_service:
                return []

            # Traverse neighboring nodes in the import/dependency call graph
            neighbors = repo_service.traverse(node_id, depth=depth)

            # Remove self from traversal to avoid duplicates
            norm_node = node_id.replace("\\", "/")
            neighbors = [n for n in neighbors if n != norm_node]

            # Fetch files and symbols from the snapshot index
            snapshot = repo_service.index()
            file_map = {f.path: f for f in snapshot.files}
            symbol_map = {s.name: s for s in snapshot.symbols}

            rank = 1
            for n in neighbors[:top_k]:
                content = ""
                metadata = {}

                # Check if neighbor is a file path
                if n in file_map:
                    f = file_map[n]
                    metadata = {
                        "path": f.path,
                        "language": f.language.value,
                        "size_bytes": f.size_bytes,
                        "sha256": f.sha256
                    }
                    content = f"File: {f.path}\nLanguage: {f.language.value}\nSymbols: " + ", ".join(s.name for s in f.symbols)
                # Check if neighbor is a symbol name
                elif n in symbol_map:
                    s = symbol_map[n]
                    metadata = {
                        "name": s.name,
                        "kind": s.kind.value,
                        "file_path": s.file_path,
                        "parent": s.parent or ""
                    }
                    content = f"Symbol: {s.name}\nKind: {s.kind.value}\nDefined in: {s.file_path}\nLine range: {s.line_start}-{s.line_end}"
                    if s.docstring:
                        content += f"\nDocstring: {s.docstring}"

                if content:
                    doc = RetrievalDocument(
                        id=f"graph-{n}",
                        content=content,
                        namespace="repository_graph",
                        metadata=metadata
                    )
                    hits.append(
                        RetrievalHit(
                            document=doc,
                            score=1.0 / (depth + 0.1),  # Closer connections get higher heuristic weightings
                            source=RetrievalSource.GRAPH,
                            rank=rank
                        )
                    )
                    rank += 1
        except Exception:
            pass

        return hits
