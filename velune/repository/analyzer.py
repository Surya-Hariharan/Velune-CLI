"""Architectural layer classifier and design pattern analyzer.

The classifier now works adaptively on ANY codebase, not just Velune's own
internal structure.  It detects the project type first (Next.js, Django,
FastAPI, Express, etc.) and then assigns files to semantic layers that make
sense for that project type.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from velune.repository.technology_detector import TechStack

# ---------------------------------------------------------------------------
# Project-type detection
# ---------------------------------------------------------------------------
#
# This module used to carry its own independent project-type classifier
# (a `MARKERS` filename table, keyed on the literal presence of e.g. a root
# `main.py` implying FastAPI regardless of content) that duplicated — and
# could disagree with — velune.repository.technology_detector.TechnologyDetector,
# the content-based detector that is the actual source of truth elsewhere in
# the pipeline. The only signal `CodebaseAnalyzer` genuinely needs that isn't
# already answered by TechnologyDetector is "is this workspace Velune itself"
# (which selects the internal layer hierarchy below) — that is now a direct,
# inline check rather than a general-purpose classifier of its own.


# ---------------------------------------------------------------------------
# Adaptive layer rules
# ---------------------------------------------------------------------------

# Generic semantic layers that apply to almost any web/API project.
# Each entry: (layer_name, list_of_path_fragments)
# A file matches the FIRST layer whose fragment appears in its normalised path.
_GENERIC_LAYERS: list[tuple[str, list[str]]] = [
    # API / routing — checked BEFORE ui so Next.js app/api/ routes are not misclassified
    (
        "api",
        [
            "/api/",
            "/routes/",
            "/controllers/",
            "/handlers/",
            "/endpoints/",
            "pages/api/",
            "app/api/",
            "/router",
        ],
    ),
    # Frontend / UI
    (
        "ui",
        [
            "/components/",
            "/views/",
            "/pages/",
            "/screens/",
            "/widgets/",
            "/layouts/",
            "/templates/",
            "src/app/",
            "/app/(",
            "/ui/",
        ],
    ),
    # Business logic / services
    (
        "services",
        [
            "/services/",
            "/service/",
            "/usecases/",
            "/use-cases/",
            "/business/",
            "/domain/",
            "/logic/",
        ],
    ),
    # Data access / ORM / queries
    (
        "data",
        [
            "/models/",
            "/model/",
            "/db/",
            "/database/",
            "/repositories/",
            "/repository/",
            "/schema/",
            "/schemas/",
            "/prisma/",
            "/migrations/",
            "/migration/",
            "/seeds/",
            "/seed/",
        ],
    ),
    # State management (frontend)
    (
        "state",
        [
            "/store/",
            "/stores/",
            "/redux/",
            "/zustand/",
            "/context/",
            "/contexts/",
            "/atoms/",
            "/slices/",
        ],
    ),
    # Authentication / authorization
    (
        "auth",
        [
            "/auth/",
            "/authentication/",
            "/authorization/",
            "/middleware/auth",
            "/guards/",
            "/policies/",
        ],
    ),
    # Shared utilities / helpers
    (
        "utils",
        ["/utils/", "/util/", "/helpers/", "/helper/", "/lib/", "/common/", "/shared/", "/core/"],
    ),
    # Configuration
    ("config", ["/config/", "/configurations/", "/settings/", "/env/"]),
    # Tests
    ("tests", ["/tests/", "/test/", "/__tests__/", "/spec/", "/specs/", ".test.", ".spec."]),
    # Infrastructure / devops
    (
        "infra",
        [
            "/infra/",
            "/infrastructure/",
            "/terraform/",
            "/k8s/",
            "/docker/",
            "Dockerfile",
            ".github/workflows/",
        ],
    ),
    # Middleware
    ("middleware", ["/middleware/", "/interceptors/", "/hooks/", "middleware.ts", "middleware.js"]),
    # Types / interfaces
    ("types", ["/types/", "/interfaces/", "/typings/", "/dtos/", "/dto/"]),
    # Plugins / extensions
    ("plugins", ["/plugins/", "/extensions/", "/addons/"]),
]

# Velune-specific layers (used only when the project IS velune itself)
_VELUNE_LAYERS: list[tuple[str, list[str]]] = [
    ("kernel", ["velune/kernel/"]),
    ("core", ["velune/core/"]),
    ("providers", ["velune/providers/"]),
    ("memory", ["velune/memory/"]),
    ("cognition", ["velune/cognition/"]),
    ("retrieval", ["velune/retrieval/"]),
    ("context", ["velune/context/"]),
    ("execution", ["velune/execution/"]),
    ("repository", ["velune/repository/"]),
    ("cli", ["velune/cli/"]),
    ("observability", ["velune/observability/"]),
    ("models", ["velune/models/"]),
    ("telemetry", ["velune/telemetry/"]),
    ("plugins", ["velune/plugins/"]),
    ("orchestration", ["velune/orchestration/"]),
    ("tools", ["velune/tools/"]),
    ("daemon", ["velune/daemon/"]),
    ("hardware", ["velune/hardware/"]),
    ("mcp", ["velune/mcp/"]),
]

# Velune layering hierarchy (lower number = more fundamental)
_VELUNE_HIERARCHY: dict[str, int] = {
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
    "telemetry": 0,
    "other": 10,
}


class CodebaseAnalyzer:
    """Analyzes the repository's architectural layered structure and checks for design violations."""

    def __init__(self, root_path: Path) -> None:
        self.root_path = root_path.resolve()
        self._detected_types: set[str] = set()

    def classify_architecture_layers(
        self, file_paths: list[str], tech: TechStack | None = None
    ) -> dict[str, list[str]]:
        """Groups files into semantic architectural layers.

        Detects the project type first; uses Velune-specific layers for the
        Velune CLI itself, and generic semantic layers for any other codebase.

        *tech* lets a caller that already ran ``TechnologyDetector`` (as
        ``RepositoryCognitionService`` does) pass that result through instead
        of paying for a second detection pass; if omitted, one is computed.
        """
        self._detected_types = self._detect_project_types(file_paths, tech)
        use_velune = "velune" in self._detected_types

        layer_rules = _VELUNE_LAYERS if use_velune else _GENERIC_LAYERS
        layer_names = [name for name, _ in layer_rules]

        layers: dict[str, list[str]] = {name: [] for name in layer_names}
        layers["other"] = []

        for path in file_paths:
            norm = path.replace("\\", "/")
            classified = False
            for layer_name, fragments in layer_rules:
                if any(frag in norm or norm.startswith(frag.lstrip("/")) for frag in fragments):
                    layers[layer_name].append(norm)
                    classified = True
                    break
            if not classified:
                layers["other"].append(norm)

        return layers

    def _detect_project_types(
        self, file_paths: list[str], tech: TechStack | None
    ) -> set[str]:
        """Cheap tags used for layer-rule selection and summary display.

        "velune" is checked directly against the file list (an exact
        structural fact, not a heuristic) since it is the only tag this
        class branches on. Everything else is informational only and is
        sourced from TechnologyDetector — the same canonical stack answer
        `RepositoryCognitionService` uses elsewhere — rather than a second,
        independent filename-marker pass that could disagree with it.
        """
        path_set = {p.replace("\\", "/") for p in file_paths}
        tags: set[str] = set()

        if any(p == "velune/kernel/__init__.py" or p.endswith("/velune/kernel/__init__.py") for p in path_set):
            tags.add("velune")

        if tech is None:
            try:
                from velune.repository.technology_detector import TechnologyDetector

                tech = TechnologyDetector(self.root_path).detect()
            except Exception:
                tech = None

        if tech is not None:
            if tech.language and tech.language != "unknown":
                tags.add(tech.language.lower())
            if tech.framework:
                tags.add(tech.framework.lower())
            if tech.frontend:
                tags.add(tech.frontend.lower())

        if not tags or tags == {"velune"}:
            has_py = any(p.endswith(".py") for p in path_set)
            has_ts = any(p.endswith(".ts") or p.endswith(".tsx") for p in path_set)
            has_js = any(p.endswith(".js") or p.endswith(".jsx") for p in path_set)
            if has_py and (has_ts or has_js):
                tags.add("fullstack_py_js")
            elif has_py:
                tags.add("python_generic")
            elif has_ts or has_js:
                tags.add("js_generic")

        return tags

    def detect_dependency_violations(
        self, layers: dict[str, list[str]], import_edges: list[tuple]
    ) -> list[dict[str, str]]:
        """Identifies dependency violations.

        For the Velune codebase uses the strict layering hierarchy.
        For other codebases only flags data layer importing UI (clear smell).
        """
        violations: list[dict[str, str]] = []
        use_velune = "velune" in self._detected_types

        # Build file → layer lookup
        layer_by_file: dict[str, str] = {}
        for layer, files in layers.items():
            for f in files:
                layer_by_file[f] = layer

        if use_velune:
            # Strict Velune hierarchy enforcement
            for source, target in import_edges:
                src_layer = layer_by_file.get(source)
                tgt_layer = layer_by_file.get(target)
                if src_layer and tgt_layer:
                    src_val = _VELUNE_HIERARCHY.get(src_layer, 10)
                    tgt_val = _VELUNE_HIERARCHY.get(tgt_layer, 10)
                    if src_val < tgt_val and tgt_layer != "telemetry":
                        violations.append(
                            {
                                "source": source,
                                "target": target,
                                "source_layer": src_layer,
                                "target_layer": tgt_layer,
                                "rule": f"Layer violation: '{src_layer}' imports higher-level '{tgt_layer}'.",
                            }
                        )
        else:
            # Generic: flag data/db importing ui (almost always a mistake)
            for source, target in import_edges:
                src_layer = layer_by_file.get(source)
                tgt_layer = layer_by_file.get(target)
                if src_layer == "data" and tgt_layer == "ui":
                    violations.append(
                        {
                            "source": source,
                            "target": target,
                            "source_layer": src_layer,
                            "target_layer": tgt_layer,
                            "rule": "Data layer importing UI — likely an inversion.",
                        }
                    )
                elif src_layer == "data" and tgt_layer == "api":
                    violations.append(
                        {
                            "source": source,
                            "target": target,
                            "source_layer": src_layer,
                            "target_layer": tgt_layer,
                            "rule": "Data layer importing API layer — check for circular dependency.",
                        }
                    )

        return violations

    def detect_framework_footprint(self, code_files: dict[str, str]) -> list[str]:
        """Detects specific framework libraries and dependencies active in the codebase."""
        footprints: set[str] = set()

        patterns: list[tuple[str, str]] = [
            # Python backends
            (r"(?:import|from)\s+fastapi", "FastAPI"),
            (r"(?:import|from)\s+flask", "Flask"),
            (r"(?:import|from)\s+django", "Django"),
            (r"(?:import|from)\s+starlette", "Starlette"),
            (r"(?:import|from)\s+aiohttp", "aiohttp"),
            (r"(?:import|from)\s+tornado", "Tornado"),
            (r"(?:import|from)\s+litestar", "Litestar"),
            # JS frameworks
            (r'(?:import|require).*?[\'"]next[\'"]', "Next.js"),
            (r'(?:import|require).*?[\'"]react[\'"]', "React"),
            (r'(?:import|require).*?[\'"]vue[\'"]', "Vue"),
            (r'(?:import|require).*?[\'"]svelte[\'"]', "Svelte"),
            (r'(?:import|require).*?[\'"]@angular/core[\'"]', "Angular"),
            (r'(?:import|require).*?[\'"]express[\'"]', "Express"),
            (r'(?:import|require).*?[\'"]koa[\'"]', "Koa"),
            (r'(?:import|require).*?[\'"]hono[\'"]', "Hono"),
            (r'(?:import|require).*?[\'"]fastify[\'"]', "Fastify"),
            # ORMs / DB
            (r"(?:import|from)\s+sqlalchemy", "SQLAlchemy"),
            (r"(?:import|from)\s+tortoise", "Tortoise ORM"),
            (r'(?:import|require).*?[\'"]prisma[\'"]|from\s+[\'"]@prisma/client[\'"]', "Prisma"),
            (r'(?:import|require).*?[\'"]mongoose[\'"]', "Mongoose"),
            (r'(?:import|require).*?[\'"]sequelize[\'"]', "Sequelize"),
            (r'(?:import|require).*?[\'"]drizzle-orm[\'"]', "Drizzle ORM"),
            (r'(?:import|require).*?[\'"]typeorm[\'"]', "TypeORM"),
            (r"(?:import|from)\s+tortoise", "Tortoise ORM"),
            # State management
            (r'(?:import|require).*?[\'"]zustand[\'"]', "Zustand"),
            (r'(?:import|require).*?[\'"]redux[\'"]', "Redux"),
            (r'(?:import|require).*?[\'"]jotai[\'"]', "Jotai"),
            (r'(?:import|require).*?[\'"]recoil[\'"]', "Recoil"),
            (
                r'(?:import|require).*?[\'"]@tanstack/react-query[\'"]|[\'"]react-query[\'"]',
                "TanStack Query",
            ),
            # Infrastructure
            (r"(?:import|from)\s+celery", "Celery"),
            (r"(?:import|from)\s+redis", "Redis"),
            (r'(?:import|require).*?[\'"]ioredis[\'"]', "Redis"),
            # Velune-specific (when running on this repo)
            (r"import typer|from typer", "Typer (CLI)"),
            (r"import qdrant_client|from qdrant_client", "Qdrant (Vector DB)"),
            (r"import sqlite3|from sqlite3", "SQLite"),
            (r"import tree_sitter|from tree_sitter", "Tree-sitter (AST)"),
            (r"import networkx|from networkx", "NetworkX (Graph)"),
            (r"import psutil|from psutil", "psutil"),
        ]

        for code in code_files.values():
            for pattern, label in patterns:
                if re.search(pattern, code, re.IGNORECASE | re.MULTILINE):
                    footprints.add(label)

        return sorted(footprints)

    @property
    def detected_project_types(self) -> set[str]:
        """Project types detected during the last classify_architecture_layers call."""
        return self._detected_types
