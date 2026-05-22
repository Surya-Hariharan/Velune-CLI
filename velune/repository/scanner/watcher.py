"""File watcher for detecting changes."""

from pathlib import Path
from typing import Callable, Optional
import time
import threading


class FileWatcher:
    """Simple file watcher for detecting repository changes."""

    def __init__(self, root_path: Path, poll_interval: float = 1.0):
        self.root_path = root_path
        self.poll_interval = poll_interval
        self._file_states: dict[str, float] = {}
        self._callbacks: list[Callable[[Path, str], None]] = []
        self._running = False
        self._thread: Optional[threading.Thread] = None

    def add_callback(self, callback: Callable[[Path, str], None]) -> None:
        """Add a callback for file changes."""
        self._callbacks.append(callback)

    def _scan(self) -> None:
        """Scan for file changes."""
        from velune.repository.scanner.filesystem import FilesystemScanner
        
        scanner = FilesystemScanner(self.root_path)
        files = scanner.scan_code_files()
        
        current_states = {}
        
        for file_path in files:
            mtime = file_path.stat().st_mtime
            current_states[str(file_path)] = mtime
            
            if str(file_path) not in self._file_states:
                # New file
                self._notify(file_path, "created")
            elif self._file_states[str(file_path)] != mtime:
                # Modified file
                self._notify(file_path, "modified")
        
        # Check for deleted files
        for file_path in self._file_states:
            if file_path not in current_states:
                self._notify(Path(file_path), "deleted")
        
        self._file_states = current_states

    def _notify(self, file_path: Path, event_type: str) -> None:
        """Notify callbacks of file change."""
        for callback in self._callbacks:
            try:
                callback(file_path, event_type)
            except Exception:
                pass

    def start(self) -> None:
        """Start the file watcher."""
        if self._running:
            return
        
        self._running = True
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()

    def _run_loop(self) -> None:
        """Run the watcher loop."""
        while self._running:
            try:
                self._scan()
            except Exception:
                pass
            time.sleep(self.poll_interval)

    def stop(self) -> None:
        """Stop the file watcher."""
        self._running = False
        if self._thread:
            self._thread.join(timeout=5.0)
            self._thread = None
