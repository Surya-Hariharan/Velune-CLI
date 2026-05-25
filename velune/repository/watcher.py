"""Background Workspace Watcher for adaptive, real-time repository changes.

Tracks file creations, updates, and deletions in real-time, performing
incremental AST delta updates and cache invalidation.
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from velune.repository.indexer import RepositoryIndexer

logger = logging.getLogger("velune.repository.watcher")

# Optional watchdog import fallback
HAS_WATCHDOG = False
try:
    from watchdog.events import FileSystemEventHandler
    from watchdog.observers import Observer
    HAS_WATCHDOG = True
except ImportError:
    FileSystemEventHandler = object  # type: ignore
    Observer = None


class WatcherEventHandler(FileSystemEventHandler):
    """Event handler for Watchdog API if installed."""

    def __init__(self, callback: Callable[[Path, str], None]) -> None:
        self.callback = callback

    def on_modified(self, event: Any) -> None:
        if not event.is_directory:
            self.callback(Path(event.src_path), "modified")

    def on_created(self, event: Any) -> None:
        if not event.is_directory:
            self.callback(Path(event.src_path), "created")

    def on_deleted(self, event: Any) -> None:
        if not event.is_directory:
            self.callback(Path(event.src_path), "deleted")


class WorkspaceEvolutionWatcher:
    """
    Background daemon that monitors file additions, updates, and removals.
    Performs real-time AST parsing of modifications and invalidates outdated cache symbols.
    """

    lifecycle_key = "workspace_watcher"

    async def initialize(self) -> None:
        """Lifecycle start callback."""
        self.start()

    async def shutdown(self) -> None:
        """Lifecycle shutdown callback."""
        self.stop()

    def __init__(
        self,
        root_path: Path,
        indexer: RepositoryIndexer,
        grapher: Any | None = None,
        semantic_memory: Any | None = None,
        event_bus: Any | None = None,
        poll_interval: float = 1.0,
    ) -> None:
        self.root_path = root_path.resolve()
        self.indexer = indexer
        self.grapher = grapher
        self.semantic_memory = semantic_memory
        self.event_bus = event_bus
        self.poll_interval = poll_interval

        self._running = False
        self._thread: threading.Thread | None = None
        self._observer: Any | None = None
        self._file_states: dict[str, tuple[float, int]] = {}

        self.ignored_dirs = {".git", ".venv", "node_modules", "__pycache__", ".pytest_cache", ".velune"}
        self.supported_extensions = {".py", ".ts", ".js", ".go", ".rs", ".java", ".c", ".cpp"}

    def start(self) -> None:
        """Start the background watcher."""
        if self._running:
            return

        self._running = True
        logger.info("Starting WorkspaceEvolutionWatcher background thread...")

        if HAS_WATCHDOG and Observer is not None:
            try:
                self._observer = Observer()
                handler = WatcherEventHandler(self._handle_file_event)
                self._observer.schedule(handler, str(self.root_path), recursive=True)
                self._observer.start()
                logger.info("Workspace Watcher started successfully using Watchdog.")
                return
            except Exception as e:
                logger.warning("Watchdog startup failed. Falling back to polling: %s", e)
                self._observer = None

        # Fallback Polling Loop
        self._scan_initial_states()
        self._thread = threading.Thread(target=self._polling_loop, daemon=True)
        self._thread.start()
        logger.info("Workspace Watcher started successfully using Polling fallback.")

    def stop(self) -> None:
        """Stop the background watcher."""
        if not self._running:
            return

        self._running = False
        if self._observer:
            try:
                self._observer.stop()
                self._observer.join()
            except Exception:
                pass
            self._observer = None

        if self._thread:
            self._thread.join(timeout=1.0)
            self._thread = None

        logger.info("WorkspaceEvolutionWatcher stopped.")

    def _should_watch(self, path: Path) -> bool:
        """Check if file path should be scanned and watched."""
        parts = path.parts
        if any(ignored in parts for ignored in self.ignored_dirs):
            return False
        return path.suffix in self.supported_extensions

    def _scan_initial_states(self) -> None:
        """Scan current directory tree and record initial mtime/size states."""
        self._file_states.clear()
        for root, dirs, files in os.walk(self.root_path):
            dirs[:] = [d for d in dirs if d not in self.ignored_dirs]
            for file in files:
                file_path = Path(root) / file
                if self._should_watch(file_path):
                    try:
                        stat = file_path.stat()
                        rel_path = str(file_path.relative_to(self.root_path)).replace("\\", "/")
                        self._file_states[rel_path] = (stat.st_mtime, stat.st_size)
                    except Exception:
                        pass

    def _polling_loop(self) -> None:
        """Fall-back mtime and size comparison loop."""
        while self._running:
            try:
                current_states: dict[str, tuple[float, int]] = {}
                for root, dirs, files in os.walk(self.root_path):
                    if not self._running:
                        break
                    dirs[:] = [d for d in dirs if d not in self.ignored_dirs]

                    for file in files:
                        file_path = Path(root) / file
                        if self._should_watch(file_path):
                            try:
                                stat = file_path.stat()
                                rel_path = str(file_path.relative_to(self.root_path)).replace("\\", "/")
                                current_states[rel_path] = (stat.st_mtime, stat.st_size)
                            except Exception:
                                pass

                if not self._running:
                    break

                # 1. Detect creations and modifications
                for rel_path, state in current_states.items():
                    prev = self._file_states.get(rel_path)
                    file_path = self.root_path / rel_path
                    if prev is None:
                        self._handle_file_event(file_path, "created")
                    elif prev != state:
                        self._handle_file_event(file_path, "modified")

                # 2. Detect deletions
                for rel_path in list(self._file_states.keys()):
                    if rel_path not in current_states:
                        file_path = self.root_path / rel_path
                        self._handle_file_event(file_path, "deleted")

                self._file_states = current_states
            except Exception as e:
                logger.error("Error in watcher polling loop: %s", e)

            time.sleep(self.poll_interval)

    def _handle_file_event(self, file_path: Path, event_type: str) -> None:
        """Processes a file creation, modification, or deletion event."""
        if not self._should_watch(file_path):
            return

        rel_path = str(file_path.relative_to(self.root_path)).replace("\\", "/")
        logger.info("File event detected [%s]: %s", event_type, rel_path)

        # 1. Invalidate Semantic Memory Tier if provided
        if self.semantic_memory and hasattr(self.semantic_memory, "delete_by_payload"):
            self.semantic_memory.delete_by_payload(
                collection_name="codebase_symbols",
                payload_filter={"file_path": rel_path}
            )

        # 2. Re-parse AST / Delta Symbol Updates
        if event_type in ("created", "modified"):
            try:
                if file_path.exists():
                    code = file_path.read_text(encoding="utf-8", errors="ignore")
                    symbols, edges = self.indexer.parser.parse(file_path, code)
                    language = self.indexer.parser._detect_language(file_path)

                    import json
                    cache = {}
                    if self.indexer.cache_path.exists():
                        try:
                            with open(self.indexer.cache_path, encoding="utf-8") as f:
                                cache = json.load(f)
                        except Exception:
                            pass

                    cache[rel_path] = {
                        "sha256": self.indexer._compute_sha256(file_path),
                        "language": language.value,
                        "size_bytes": file_path.stat().st_size,
                        "symbols": [s.model_dump() for s in symbols]
                    }

                    with open(self.indexer.cache_path, "w", encoding="utf-8") as f:
                        json.dump(cache, f, indent=2)

                    if self.grapher and hasattr(self.grapher, "index_file"):
                        self.grapher.index_file(file_path, symbols)

                    # Trigger Subsystem Health and Architecture Drift Alarm (ADA)
                    try:
                        from velune.cognition.architecture import ArchitectureCognitionAgent
                        agent = ArchitectureCognitionAgent(workspace_root=str(self.root_path))
                        agent.audit_architecture(str(file_path), code)
                    except Exception as e:
                        logger.error("Failed to execute background ADA/SHI audit on %s: %s", rel_path, e)

                    logger.debug("Successfully performed incremental AST index for %s", rel_path)
            except Exception as e:
                logger.error("Failed incremental index for %s: %s", rel_path, e)
        elif event_type == "deleted":
            try:
                import json
                if self.indexer.cache_path.exists():
                    with open(self.indexer.cache_path, encoding="utf-8") as f:
                        cache = json.load(f)
                    if rel_path in cache:
                        del cache[rel_path]
                        with open(self.indexer.cache_path, "w", encoding="utf-8") as f:
                            json.dump(cache, f, indent=2)
            except Exception:
                pass

        # 3. Publish notification on the event bus if available
        if self.event_bus:
            try:
                loop = asyncio.get_event_loop()
                if loop.is_running():
                    loop.create_task(self.event_bus.emit(
                        "repository.file_changed",
                        {
                            "path": rel_path,
                            "event": event_type,
                            "timestamp": time.time()
                        }
                    ))
                else:
                    asyncio.run(self.event_bus.emit(
                        "repository.file_changed",
                        {
                            "path": rel_path,
                            "event": event_type,
                            "timestamp": time.time()
                        }
                    ))
            except Exception:
                pass
