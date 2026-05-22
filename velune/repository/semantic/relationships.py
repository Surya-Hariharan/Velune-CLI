"""Cross-file semantic relationship detection."""

from typing import list, Dict, Tuple
from pathlib import Path
from velune.core.types import SymbolNode


class RelationshipDetector:
    """Detects semantic relationships between files."""

    def __init__(self):
        pass

    def detect_imports(
        self,
        file_path: Path,
        code: str,
        all_symbols: Dict[str, list[SymbolNode]],
    ) -> list[Tuple[str, str]]:
        """Detect import relationships."""
        relationships = []
        lines = code.split("\n")
        
        for line in lines:
            stripped = line.strip()
            
            # Python imports
            if stripped.startswith("import ") or stripped.startswith("from "):
                # Extract module name
                if stripped.startswith("from "):
                    parts = stripped.split()
                    if len(parts) >= 2:
                        module = parts[1]
                        relationships.append((str(file_path), module))
                elif stripped.startswith("import "):
                    parts = stripped[7:].split(",")
                    for part in parts:
                        module = part.strip().split(" as ")[0]
                        relationships.append((str(file_path), module))
        
        return relationships

    def detect_function_calls(
        self,
        file_path: Path,
        code: str,
        all_symbols: Dict[str, list[SymbolNode]],
    ) -> list[Tuple[str, str]]:
        """Detect function call relationships."""
        relationships = []
        
        # Simple detection: find function names in code
        for other_file, symbols in all_symbols.items():
            if other_file == str(file_path):
                continue
            
            for symbol in symbols:
                if symbol.kind == "function":
                    if symbol.name in code:
                        relationships.append((str(file_path), f"{other_file}::{symbol.name}"))
        
        return relationships
