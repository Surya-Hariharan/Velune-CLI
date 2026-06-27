"""Incremental symbol and repository metadata indexer."""

import json
import logging
from collections.abc import Callable
from pathlib import Path

from velune.cognition.firewall import CognitiveFirewall
from velune.repository._native import sha256_file as _sha256_file
from velune.repository.parser import RepositorySnapshotParser
from velune.repository.scanner import FilesystemScanner
from velune.repository.schemas import (
    RepositoryFile,
    RepositoryLanguage,
    RepositorySnapshot,
    RepositorySymbol,
)

logger = logging.getLogger("velune.repository.indexer")


class SecretFileDetector:
    """Detects files that are likely to contain credentials or private keys.

    Checks are layered: filename patterns first (fast, no I/O), then file
    extension, then an optional shallow content scan of the first 500 chars.
    The content scan is only used by callers that have already read the file
    for another purpose — the indexer never reads a file solely to check it.
    """

    SECRET_PATTERNS = [
        ".env",
        ".env.local",
        ".env.production",
        ".env.staging",
        ".env.development",
        "id_rsa",
        "id_dsa",
        "id_ed25519",
        "id_ecdsa",
        ".netrc",
        ".aws/credentials",
        "credentials.json",
        "service-account.json",
        "gcp-credentials.json",
    ]
    SECRET_EXTENSIONS = [".pem", ".key", ".p12", ".pfx", ".crt", ".cer"]
    SECRET_CONTENT_PATTERNS = [
        "PRIVATE KEY",
        "BEGIN RSA PRIVATE",
        "BEGIN EC PRIVATE",
        "AWS_SECRET_ACCESS_KEY",
        "ANTHROPIC_API_KEY",
        "OPENAI_API_KEY",
    ]

    def is_likely_secret(self, file_path: str, content: str | None = None) -> bool:
        """Return True if the file looks like a secrets/credentials file.

        Args:
            file_path: Relative or absolute path string (forward-slashes preferred).
            content: Optional file content — only first 500 chars are scanned.
        """
        normalised = file_path.replace("\\", "/")
        name = normalised.split("/")[-1]
        suffix = ("." + name.rsplit(".", 1)[-1]).lower() if "." in name else ""

        # 1. Filename / path suffix match
        for pattern in self.SECRET_PATTERNS:
            if "/" in pattern:
                # Path-component pattern (e.g. ".aws/credentials")
                if normalised.endswith(pattern):
                    return True
            elif name == pattern:
                return True

        # 2. Extension match
        if suffix in self.SECRET_EXTENSIONS:
            return True

        # 3. Shallow content scan
        if content is not None:
            excerpt = content[:500]
            for marker in self.SECRET_CONTENT_PATTERNS:
                if marker in excerpt:
                    return True

        return False

    def get_warning(self, file_path: str) -> str:
        return f"SECURITY: '{file_path}' looks like a secrets file and was not indexed."


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
        self.parser = RepositorySnapshotParser()
        self.scanner = FilesystemScanner(self.root_path)
        self.firewall = firewall or CognitiveFirewall()
        self.secret_detector = SecretFileDetector()
        # Optional progress hook: called with (processed: int, total: int, rel_path: str)
        # after each file is indexed. Assign before calling index().
        self.progress_callback: Callable[[int, int, str], None] | None = None

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
        excluded_paths: list[str] = []

        # Scan code files
        code_files = self.scanner.scan_code_files()
        _total_files = len(code_files)
        _processed = 0

        for file_path in code_files:
            rel_path = str(file_path.relative_to(self.root_path)).replace("\\", "/")

            # SECURITY: hard gate — never index secret/credential files, even if
            # they somehow passed the scanner's .veluneignore filter.
            if self.secret_detector.is_likely_secret(rel_path):
                logger.warning(self.secret_detector.get_warning(rel_path))
                excluded_paths.append(rel_path)
                continue

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
                        rel_path,
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
            finally:
                _processed += 1
                if self.progress_callback is not None:
                    self.progress_callback(_processed, _total_files, rel_path)

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
            "languages": self._compile_language_summary(files),
            "excluded_paths": excluded_paths,
        }

        return RepositorySnapshot(
            root_path=str(self.root_path),
            files=files,
            symbols=all_symbols,
            edges=[],  # Edges are derived and resolved by Grapher
            summary=summary,
        )

    def _compute_sha256(self, file_path: Path) -> str:
        return _sha256_file(file_path)

    def _compile_language_summary(self, files: list[RepositoryFile]) -> dict[str, int]:
        """Summarizes counting of files grouped by programming language."""
        counts: dict[str, int] = {}
        for f in files:
            counts[f.language.value] = counts.get(f.language.value, 0) + 1
        return counts
