"""Architectural layer classifier and design pattern analyzer."""

from pathlib import Path


class CodebaseAnalyzer:
    """Analyzes the repository's architectural layered structure and checks for design violations."""

    def __init__(self, root_path: Path) -> None:
        self.root_path = root_path.resolve()

    def classify_architecture_layers(self, file_paths: list[str]) -> dict[str, list[str]]:
        """Groups files into functional architectural layers based on folder topology."""
        layers: dict[str, list[str]] = {
            "kernel": [],
            "providers": [],
            "models": [],
            "memory": [],
            "context": [],
            "intent": [],
            "repository": [],
            "retrieval": [],
            "execution": [],
            "cognition": [],
            "cli": [],
            "tools": [],
            "plugins": [],
            "telemetry": [],
            "other": [],
        }

        for path in file_paths:
            # Normalize to forward slashes
            norm = path.replace("\\", "/")
            classified = False

            for layer in layers.keys():
                if f"velune/{layer}/" in norm or norm.startswith(f"{layer}/"):
                    layers[layer].append(norm)
                    classified = True
                    break

            if not classified:
                layers["other"].append(norm)

        return layers

    def detect_dependency_violations(
        self, layers: dict[str, list[str]], import_edges: list[tuple]
    ) -> list[dict[str, str]]:
        """Identifies circular dependencies or violations of layered architectural boundaries.

        Rules:
        - Kernel layer MUST NOT import higher levels (cli, cognition, retrieval).
        - Providers layer MUST NOT import cli or cognition.
        - Higher levels can import kernel/providers freely.
        """
        violations: list[dict[str, str]] = []

        # Build mapping of file to its layer
        layer_by_file: dict[str, str] = {}
        for layer, files in layers.items():
            for f in files:
                layer_by_file[f] = layer

        # Layer importance hierarchy (lower number is more fundamental/lower layer)
        hierarchy = {
            "kernel": 0,
            "providers": 1,
            "models": 2,
            "memory": 3,
            "context": 4,
            "intent": 5,
            "repository": 5,
            "retrieval": 6,
            "tools": 7,
            "execution": 8,
            "cognition": 9,
            "plugins": 9,
            "cli": 10,
            "telemetry": 0,  # Telemetry can be imported everywhere
            "other": 10,
        }

        for source, target in import_edges:
            src_layer = layer_by_file.get(source)
            tgt_layer = layer_by_file.get(target)

            if src_layer and tgt_layer:
                src_val = hierarchy.get(src_layer, 10)
                tgt_val = hierarchy.get(tgt_layer, 10)

                # Violation: Lower layer imports a strictly higher layer (excluding telemetry)
                if src_val < tgt_val and tgt_layer != "telemetry":
                    violations.append(
                        {
                            "source": source,
                            "target": target,
                            "source_layer": src_layer,
                            "target_layer": tgt_layer,
                            "rule": f"Layer violation: Fundamental '{src_layer}' layer imports higher-level '{tgt_layer}' layer.",
                        }
                    )

        return violations

    def detect_framework_footprint(self, code_files: dict[str, str]) -> list[str]:
        """Detects specific framework libraries and dependencies active in the codebase."""
        footprints: set[str] = set()

        for code in code_files.values():
            if "import typer" in code or "from typer" in code:
                footprints.add("Typer (CLI)")
            if "import langgraph" in code or "from langgraph" in code:
                footprints.add("LangGraph (Orchestration)")
            if "import qdrant_client" in code or "from qdrant_client" in code:
                footprints.add("Qdrant (Vector DB)")
            if "import sqlite3" in code or "from sqlite3" in code:
                footprints.add("SQLite (Episodic Storage)")
            if "import tree_sitter" in code or "from tree_sitter" in code:
                footprints.add("Tree-sitter (AST Parsing)")
            if "import networkx" in code or "from networkx" in code:
                footprints.add("NetworkX (Graph Traversal)")
            if "import psutil" in code or "from psutil" in code:
                footprints.add("psutil (Subprocess Limits)")

        return list(footprints)
