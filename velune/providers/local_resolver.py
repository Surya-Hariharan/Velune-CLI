"""LocalModelResolver — filesystem discovery and path resolution for local GGUF models."""

from __future__ import annotations

import os
import re
from collections.abc import Iterator
from pathlib import Path

_MAX_DEPTH = 5
_MAX_FILES_PER_ROOT = 100_000

_PARAM_RE = re.compile(r"(\d+(?:\.\d+)?)\s*b\b", re.IGNORECASE)

_QUANT_PATTERNS: list[tuple[str, str]] = [
    ("q4_k_m", "Q4_K_M"),
    ("q4_k_s", "Q4_K_S"),
    ("q4_0", "Q4_0"),
    ("q4_1", "Q4_1"),
    ("q5_k_m", "Q5_K_M"),
    ("q5_k_s", "Q5_K_S"),
    ("q5_0", "Q5_0"),
    ("q6_k", "Q6_K"),
    ("q8_0", "Q8_0"),
    ("fp16", "FP16"),
    ("f16", "FP16"),
    ("f32", "FP32"),
    ("q4", "Q4"),
    ("q5", "Q5"),
    ("q8", "Q8"),
]

_FAMILIES = [
    "llama",
    "qwen",
    "mistral",
    "phi",
    "gemma",
    "falcon",
    "mpt",
    "bloom",
    "gpt",
    "codellama",
    "deepseek",
    "starcoder",
    "yi",
    "baichuan",
    "internlm",
    "vicuna",
    "alpaca",
    "wizard",
    "orca",
    "hermes",
    "solar",
    "openchat",
    "nous",
    "neural",
]


class LocalModelResolver:
    """Discovers and resolves local GGUF model files across well-known directories."""

    @staticmethod
    def _scan_paths() -> list[Path]:
        """Well-known GGUF locations, resolved fresh from the current environment.

        Every user's machine lays these out differently — a different username,
        a Windows profile that lives on D:\\ instead of C:\\, a home directory on
        a second drive — so nothing here is hardcoded. Each path is derived from
        an environment variable or ``Path.home()`` at call time, which resolves
        correctly regardless of which drive/mount the profile actually lives on.
        """
        home = Path.home()
        paths: list[Path] = [
            home / "models",
            home / "Downloads",
            home / ".cache" / "huggingface" / "hub",
            home / ".ollama" / "models",
            home / "LM Studio" / "models",
            home / ".lmstudio" / "models",
        ]
        local_appdata = os.environ.get("LOCALAPPDATA")
        if local_appdata:
            paths.append(Path(local_appdata) / "LM Studio" / "models")
        else:
            paths.append(Path("/usr/share/ollama/models"))
            paths.append(home / "Library" / "Application Support" / "LM Studio" / "models")
        return paths

    # ------------------------------------------------------------------ #
    # Public API                                                           #
    # ------------------------------------------------------------------ #

    def scan_gguf_files(self) -> list[Path]:
        """Recursively scan well-known GGUF locations for *.gguf files.

        Skips non-existent directories, caps per-root traversal at
        _MAX_FILES_PER_ROOT items, and limits descent to _MAX_DEPTH levels.
        Returns a deduplicated, sorted list.
        """
        seen: set[Path] = set()
        for root in self._scan_paths():
            if not root.exists() or not root.is_dir():
                continue
            counter = [0]
            for path in self._walk_gguf(root, 0, counter):
                resolved = path.resolve()
                if resolved not in seen:
                    seen.add(resolved)
        return sorted(seen)

    def resolve_model_path(self, model_id: str) -> Path | None:
        """Resolve *model_id* to an existing *.gguf Path, or return None.

        Resolution order:
        1. Treat as absolute path — check existence.
        2. Treat as relative path against each SCAN_PATH.
        3. Filename-stem fuzzy match across all GGUF files found.
        """
        p = Path(model_id)

        # 1. Absolute path
        if p.is_absolute():
            return p if (p.exists() and p.suffix.lower() == ".gguf") else None

        # 2. Relative to scan paths
        for root in self._scan_paths():
            if not root.exists():
                continue
            candidate = root / model_id
            if candidate.exists() and candidate.suffix.lower() == ".gguf":
                return candidate

        # 3. Stem / filename scan
        target_stem = p.stem.lower()
        target_name = p.name.lower()
        for found in self.scan_gguf_files():
            if found.name.lower() == target_name or found.stem.lower() == target_stem:
                return found

        return None

    def prompt_for_path(self, model_name: str) -> Path | None:
        """Interactively ask the user for a .gguf path. Returns None on skip."""
        try:
            raw = input(
                f"\nModel '{model_name}' not found automatically.\n"
                f"Enter full path to .gguf file (or press Enter to skip): "
            ).strip()
        except (EOFError, KeyboardInterrupt):
            return None

        if not raw:
            return None

        path = Path(raw)
        if path.exists() and path.is_file():
            return path

        print(f"  Path does not exist: {path}")
        return None

    def get_model_metadata(self, gguf_path: Path) -> dict:
        """Extract metadata from a GGUF file.

        Tries the ``gguf`` library first; falls back to filename heuristics.
        Returns a dict with keys: param_count_b, quantization, family, context_length.
        """
        try:
            return self._metadata_from_gguf_lib(gguf_path)
        except Exception:
            pass
        return self._metadata_from_filename(gguf_path.stem)

    # ------------------------------------------------------------------ #
    # Internal helpers                                                     #
    # ------------------------------------------------------------------ #

    def _walk_gguf(self, directory: Path, depth: int, counter: list[int]) -> Iterator[Path]:
        if depth > _MAX_DEPTH or counter[0] >= _MAX_FILES_PER_ROOT:
            return
        try:
            for item in directory.iterdir():
                counter[0] += 1
                if counter[0] >= _MAX_FILES_PER_ROOT:
                    return
                if item.is_file() and item.suffix.lower() == ".gguf":
                    yield item
                elif item.is_dir():
                    yield from self._walk_gguf(item, depth + 1, counter)
        except PermissionError:
            pass

    def _metadata_from_gguf_lib(self, path: Path) -> dict:
        from gguf import GGUFReader  # type: ignore[import]

        reader = GGUFReader(str(path))
        meta = reader.metadata

        raw_params = meta.get("parameter_count") or meta.get("llm.parameter_count") or 0
        if isinstance(raw_params, int) and raw_params > 1_000_000:
            param_count_b: float | None = raw_params / 1e9
        elif isinstance(raw_params, int | float) and raw_params > 0:
            param_count_b = float(raw_params)
        else:
            param_count_b = None

        ctx = meta.get("context_length") or meta.get("llm.context_length") or 4096

        return {
            "param_count_b": param_count_b,
            "quantization": self._extract_quantization(path.stem),
            "family": self._extract_family(path.stem),
            "context_length": int(ctx),
        }

    def _metadata_from_filename(self, stem: str) -> dict:
        return {
            "param_count_b": self._extract_param_count(stem),
            "quantization": self._extract_quantization(stem),
            "family": self._extract_family(stem),
            "context_length": 4096,
        }

    def _extract_param_count(self, name: str) -> float | None:
        m = _PARAM_RE.search(name)
        if m:
            return float(m.group(1))
        return None

    def _extract_quantization(self, name: str) -> str | None:
        lower = name.lower()
        for pattern, label in _QUANT_PATTERNS:
            if pattern in lower:
                return label
        return None

    def _extract_family(self, name: str) -> str | None:
        lower = name.lower()
        for family in _FAMILIES:
            if family in lower:
                return family
        return None
