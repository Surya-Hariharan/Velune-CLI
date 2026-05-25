"""Incremental symbol and repository metadata indexer."""

import hashlib
import json
import logging
from pathlib import Path

from velune.cognition.firewall import CognitiveFirewall
from velune.repository.parser import ASTParser
from velune.repository.scanner import FilesystemScanner
from velune.repository.schemas import (
    RepositoryFile,
    RepositoryLanguage,
    RepositorySnapshot,
    RepositorySymbol,
)

logger = logging.getLogger("velune.repository.indexer")


class RepositoryIndexer:
    """Orchestrates multi-file symbol indexing and caches hashes for incremental indexing."""

    def __init__(
        self,
        root_path: Path,
        cache_path: Path | None = None,
        firewall: CognitiveFirewall | None = None,
    ) -> None:
        self.root_path = root_path.resolve()
        self.cache_path = cache_path or self.root_path / ".velune" / "index_cache.json"
        self.parser = ASTParser()
        self.scanner = FilesystemScanner(self.root_path)
        self.firewall = firewall or CognitiveFirewall()

    def index(self, force: bool = False) -> RepositorySnapshot:
        """Indexes the workspace, loading cached results incrementally for unmodified files."""
        # Ensure cache directory exists
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)

        # Load index cache if it exists and force is False
        cache: dict[str, dict] = {}
        if not force and self.cache_path.exists():
            try:
                with open(self.cache_path, encoding="utf-8") as f:
                    cache = json.load(f)
            except Exception:
                pass

        files: list[RepositoryFile] = []
        all_symbols: list[RepositorySymbol] = []
        new_cache: dict[str, dict] = {}

        # Scan code files
        code_files = self.scanner.scan_code_files()

        for file_path in code_files:
            rel_path = str(file_path.relative_to(self.root_path)).replace("\\", "/")

            try:
                # Get hash and size
                sha = self._compute_sha256(file_path)
                size_bytes = file_path.stat().st_size

                # Check cache match
                cached_entry = cache.get(rel_path)
                if cached_entry and cached_entry.get("sha256") == sha:
                    # Restore from cache
                    symbols = [RepositorySymbol(**s) for s in cached_entry.get("symbols", [])]
                    language = RepositoryLanguage(cached_entry.get("language", "unknown"))
                    metadata = cached_entry.get("metadata", {})

                    file_rec = RepositoryFile(
                        path=rel_path,
                        language=language,
                        size_bytes=size_bytes,
                        sha256=sha,
                        symbols=symbols,
                        metadata=metadata,
                    )
                    files.append(file_rec)
                    all_symbols.extend(symbols)
                    new_cache[rel_path] = cached_entry
                    continue

                # Otherwise, parse file
                code = file_path.read_text(encoding="utf-8", errors="ignore")

                # SECURITY: Scan for injection before any processing
                file_metadata = {}
                scan_result = self.firewall.scan_file_for_injection(str(file_path), code)
                if not scan_result["is_safe"]:
                    logger.warning(
                        "SECURITY: Potential prompt injection in %s — using sanitized version",
                        rel_path
                    )
                    code = scan_result["neutralized_content"]
                    file_metadata["injection_risk"] = True

                symbols, edges = self.parser.parse(file_path, code)

                language = self.parser._detect_language(file_path)
                file_rec = RepositoryFile(
                    path=rel_path,
                    language=language,
                    size_bytes=size_bytes,
                    sha256=sha,
                    symbols=symbols,
                    metadata=file_metadata,
                )
                files.append(file_rec)
                all_symbols.extend(symbols)

                # Store in cache map
                new_cache[rel_path] = {
                    "sha256": sha,
                    "language": language.value,
                    "size_bytes": size_bytes,
                    "symbols": [s.model_dump() for s in symbols],
                    "metadata": file_metadata,
                }
            except Exception:
                # Gracefully ignore parsing failures of single files
                pass

        # Save new cache
        try:
            with open(self.cache_path, "w", encoding="utf-8") as f:
                json.dump(new_cache, f, indent=2)
        except Exception:
            pass

        # Assemble summary
        summary = {
            "total_files": len(files),
            "total_symbols": len(all_symbols),
            "languages": self._compile_language_summary(files)
        }

        return RepositorySnapshot(
            root_path=str(self.root_path),
            files=files,
            symbols=all_symbols,
            edges=[],  # Edges are derived and resolved by Grapher
            summary=summary
        )

    def _compute_sha256(self, file_path: Path) -> str:
        """Computes the SHA-256 hash of a file's contents."""
        sha = hashlib.sha256()
        with open(file_path, "rb") as f:
            for chunk in iter(lambda: f.read(8192), b""):
                sha.update(chunk)
        return sha.hexdigest()

    def _compile_language_summary(self, files: list[RepositoryFile]) -> dict[str, int]:
        """Summarizes counting of files grouped by programming language."""
        counts: dict[str, int] = {}
        for f in files:
            counts[f.language.value] = counts.get(f.language.value, 0) + 1
        return counts
