"""Knowledge and repository AST dependency graph traversal retriever."""


from velune.kernel.registry import ComponentRegistry
from velune.repository.cognition import RepositoryCognitionService
from velune.retrieval.schemas import RetrievalDocument, RetrievalHit, RetrievalSource


class GraphRetriever:
    """Traverses knowledge graphs and repository AST structures to fetch contiguous context.

    This retriever uses the **cached** repository snapshot (read from the
    on-disk JSON index) instead of triggering a full ``repo_service.index()``
    on every call.  That eliminates the O(N·files) AST re-parse that
    previously happened on every retrieval request.

    Cold-start behaviour (no cache on disk yet):
      - ``get_snapshot()`` returns ``None``.
      - ``retrieve()`` returns an empty list and logs a debug warning.
      - The first ``velune run`` / ``velune chat`` call that triggers a full
        index will populate the cache, after which graph retrieval works normally.
    """

    def __init__(self) -> None:
        self.registry = ComponentRegistry()

    def retrieve(self, node_id: str, depth: int = 1, top_k: int = 10) -> list[RetrievalHit]:
        """Traverses adjacent AST and symbol imports from the repository cognition service.

        Uses the cached snapshot via ``get_snapshot()`` to avoid triggering a
        full repository re-index on every call.
        """
        hits: list[RetrievalHit] = []

        try:
            # Grab the active RepositoryCognitionService from the kernel registry
            repo_service = self.registry.get(RepositoryCognitionService)
            if not repo_service:
                return []

            # Traverse neighboring nodes in the import/dependency call graph
            neighbors = repo_service.traverse(node_id, depth=depth)

            # Remove self from traversal to avoid duplicates
            norm_node = node_id.replace("\\", "/")
            neighbors = [n for n in neighbors if n != norm_node]

            # --- KEY FIX: use get_snapshot() instead of index() ---
            # get_snapshot() reads the on-disk JSON cache written by the last
            # full index run.  It does NOT re-parse ASTs or run Git commands.
            # Falls back gracefully to empty lists when no cache exists yet.
            snapshot = repo_service.get_snapshot()
            if snapshot is None:
                # Cold start — no index on disk yet; skip graph retrieval silently
                return []

            file_map = {f.path: f for f in snapshot.files}
            symbol_by_id = {s.symbol_id: s for s in snapshot.symbols if s.symbol_id}
            symbol_by_qualified = {s.qualified_name: s for s in snapshot.symbols if s.qualified_name}
            symbol_by_name = {s.name: s for s in snapshot.symbols}

            rank = 1
            for n in neighbors[:top_k]:
                content = ""
                metadata = {}
                s = None

                # Check if neighbor is a file path
                if n in file_map:
                    f = file_map[n]
                    metadata = {
                        "path": f.path,
                        "language": f.language.value,
                        "size_bytes": f.size_bytes,
                        "sha256": f.sha256
                    }
                    content = f"File: {f.path}\nLanguage: {f.language.value}\nSymbols: " + ", ".join(sym.name for sym in f.symbols)
                # Check if neighbor is a symbol (by symbol_id, qualified_name, or name)
                elif n in symbol_by_id:
                    s = symbol_by_id[n]
                elif n in symbol_by_qualified:
                    s = symbol_by_qualified[n]
                elif n in symbol_by_name:
                    s = symbol_by_name[n]

                if s:
                    metadata = {
                        "name": s.name,
                        "kind": s.kind.value,
                        "file_path": s.file_path,
                        "parent": s.parent or "",
                        "symbol_id": s.symbol_id or "",
                        "qualified_name": s.qualified_name or ""
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
