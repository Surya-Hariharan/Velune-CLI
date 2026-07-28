"""Project type detector — classifies workspaces and builds context profiles.

This is a thin presentation layer over :class:`~velune.repository.technology_detector
.TechnologyDetector`, which is the single place in the codebase that reads
manifests/dependency files to decide language + framework. It used to run its
own independent, first-match-wins classification pass over the same files;
that meant a repo could get two disagreeing answers depending on which code
path asked. All this module does now is map a `TechStack` onto the
`ProjectType` enum and the presentation fields (system prompt, display name,
context hints) that are unique to it.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from velune.repository.technology_detector import TechStack


class ProjectType(Enum):
    PYTHON_FASTAPI = "python-fastapi"
    PYTHON_DJANGO = "python-django"
    PYTHON_FLASK = "python-flask"
    PYTHON_CLI = "python-cli"
    PYTHON_GENERIC = "python"
    NODE_REACT = "node-react"
    NODE_NEXTJS = "node-nextjs"
    NODE_EXPRESS = "node-express"
    NODE_GENERIC = "node"
    RUST = "rust"
    GO = "go"
    JAVA_SPRING = "java-spring"
    JAVA_GENERIC = "java"
    DOTNET = "dotnet"
    FLUTTER = "flutter"
    UNKNOWN = "unknown"


@dataclass
class ProjectProfile:
    project_type: ProjectType
    display_name: str
    primary_language: str
    detected_frameworks: list[str]
    entry_points: list[str]
    test_directories: list[str]
    config_files: list[str]
    suggested_model_skill: str  # "coding" | "reasoning" | "balanced"
    context_hints: list[str]
    system_prompt_addon: str


PROJECT_SYSTEM_PROMPTS: dict[ProjectType, str] = {
    ProjectType.PYTHON_FASTAPI: (
        "This is a Python FastAPI project. When suggesting code changes, "
        "prefer async/await patterns, Pydantic models for validation, "
        "and dependency injection. Routes are in routers/. Models in models/."
    ),
    ProjectType.PYTHON_DJANGO: (
        "This is a Django project. Follow Django conventions: fat models, "
        "thin views, use Django ORM patterns, signals, and class-based views "
        "where appropriate. Settings are in settings.py or settings/."
    ),
    ProjectType.PYTHON_FLASK: (
        "This is a Flask project. Follow Flask patterns: blueprints for "
        "routing, application factory pattern, SQLAlchemy for ORM if present."
    ),
    ProjectType.NODE_REACT: (
        "This is a React frontend project. Prefer functional components "
        "with hooks, TypeScript if tsconfig.json exists, and modern React "
        "patterns. Components in src/components/."
    ),
    ProjectType.NODE_NEXTJS: (
        "This is a Next.js project. Use App Router patterns if app/ exists, "
        "Pages Router if pages/ exists. Server components by default, "
        "client components only when needed."
    ),
    ProjectType.RUST: (
        "This is a Rust project. Follow Rust idioms: ownership semantics, "
        "Result/Option for error handling, no unwrap() in library code. "
        "Check Cargo.toml for edition and features."
    ),
    ProjectType.GO: (
        "This is a Go project. Follow Go conventions: simple interfaces, "
        "error returns, goroutines and channels for concurrency. "
        "Entry point is main.go or cmd/."
    ),
}

_DISPLAY_NAMES: dict[ProjectType, str] = {
    ProjectType.PYTHON_FASTAPI: "Python / FastAPI",
    ProjectType.PYTHON_DJANGO: "Python / Django",
    ProjectType.PYTHON_FLASK: "Python / Flask",
    ProjectType.PYTHON_CLI: "Python / CLI",
    ProjectType.PYTHON_GENERIC: "Python",
    ProjectType.NODE_REACT: "Node.js / React",
    ProjectType.NODE_NEXTJS: "Node.js / Next.js",
    ProjectType.NODE_EXPRESS: "Node.js / Express",
    ProjectType.NODE_GENERIC: "Node.js",
    ProjectType.RUST: "Rust",
    ProjectType.GO: "Go",
    ProjectType.JAVA_SPRING: "Java / Spring",
    ProjectType.JAVA_GENERIC: "Java",
    ProjectType.DOTNET: ".NET",
    ProjectType.FLUTTER: "Flutter / Dart",
    ProjectType.UNKNOWN: "Unknown",
}

_LANGUAGE_MAP: dict[ProjectType, str] = {
    ProjectType.PYTHON_FASTAPI: "python",
    ProjectType.PYTHON_DJANGO: "python",
    ProjectType.PYTHON_FLASK: "python",
    ProjectType.PYTHON_CLI: "python",
    ProjectType.PYTHON_GENERIC: "python",
    ProjectType.NODE_REACT: "typescript",
    ProjectType.NODE_NEXTJS: "typescript",
    ProjectType.NODE_EXPRESS: "javascript",
    ProjectType.NODE_GENERIC: "javascript",
    ProjectType.RUST: "rust",
    ProjectType.GO: "go",
    ProjectType.JAVA_SPRING: "java",
    ProjectType.JAVA_GENERIC: "java",
    ProjectType.DOTNET: "csharp",
    ProjectType.FLUTTER: "dart",
    ProjectType.UNKNOWN: "unknown",
}


class ProjectTypeDetector:
    """Classifies a workspace's project type from its detected tech stack.

    All manifest/dependency-file reading is delegated to
    :class:`~velune.repository.technology_detector.TechnologyDetector` —
    this class only maps that single, canonical stack answer onto the
    ``ProjectType`` enum and the presentation fields unique to it (system
    prompt addon, context hints, entry points, test/config file probes).
    """

    def detect(self, workspace: Path) -> ProjectProfile:
        from velune.repository.technology_detector import TechnologyDetector

        files = self._list_root_files(workspace)
        tech = TechnologyDetector(workspace).detect()
        project_type = self._map_tech_stack(tech)
        frameworks = self._collect_frameworks(tech)
        entry_points = self._find_entry_points(workspace, project_type)
        test_dirs = self._find_test_dirs(workspace)
        config_files = self._find_config_files(workspace, files)
        context_hints = self._build_context_hints(project_type)
        system_prompt = PROJECT_SYSTEM_PROMPTS.get(project_type, "")

        return ProjectProfile(
            project_type=project_type,
            display_name=_DISPLAY_NAMES.get(project_type, "Unknown"),
            primary_language=_LANGUAGE_MAP.get(project_type, "unknown"),
            detected_frameworks=frameworks,
            entry_points=entry_points,
            test_directories=test_dirs,
            config_files=config_files,
            suggested_model_skill="coding",
            context_hints=context_hints,
            system_prompt_addon=system_prompt,
        )

    def _list_root_files(self, workspace: Path) -> set[str]:
        try:
            return {f.name for f in workspace.iterdir() if not f.name.startswith(".")}
        except Exception:
            return set()

    def _map_tech_stack(self, tech: TechStack) -> ProjectType:
        """Map a `TechStack` (language + framework) onto the `ProjectType` enum."""
        lang = tech.language
        fw = tech.framework

        if lang == "Python":
            if fw == "FastAPI":
                return ProjectType.PYTHON_FASTAPI
            if fw == "Django":
                return ProjectType.PYTHON_DJANGO
            if fw == "Flask":
                return ProjectType.PYTHON_FLASK
            if fw == "CLI":
                return ProjectType.PYTHON_CLI
            return ProjectType.PYTHON_GENERIC

        if lang in ("JavaScript", "TypeScript"):
            if fw == "Next.js":
                return ProjectType.NODE_NEXTJS
            if tech.frontend == "React":
                return ProjectType.NODE_REACT
            if fw == "Express":
                return ProjectType.NODE_EXPRESS
            return ProjectType.NODE_GENERIC

        if lang == "Rust":
            return ProjectType.RUST
        if lang == "Go":
            return ProjectType.GO
        if lang == "Java":
            return ProjectType.JAVA_SPRING if fw == "Spring" else ProjectType.JAVA_GENERIC
        if lang == "C#":
            return ProjectType.DOTNET
        if lang == "Dart":
            return ProjectType.FLUTTER

        return ProjectType.UNKNOWN

    def _collect_frameworks(self, tech: TechStack) -> list[str]:
        """Flatten the named + secondary TechStack fields into a display list."""
        seen: list[str] = []
        for value in (
            tech.framework,
            tech.frontend,
            tech.database,
            tech.auth,
            tech.state_management,
            *tech.secondary_frameworks,
        ):
            if value and value not in seen:
                seen.append(value)
        return seen

    def _find_entry_points(self, workspace: Path, pt: ProjectType) -> list[str]:
        candidates: dict[ProjectType, list[str]] = {
            ProjectType.PYTHON_FASTAPI: ["main.py", "app/main.py", "src/main.py"],
            ProjectType.PYTHON_DJANGO: ["manage.py"],
            ProjectType.PYTHON_FLASK: ["app.py", "run.py", "main.py"],
            ProjectType.PYTHON_CLI: ["main.py", "cli.py", "__main__.py"],
            ProjectType.PYTHON_GENERIC: ["main.py", "__main__.py"],
            ProjectType.NODE_REACT: ["src/main.tsx", "src/App.tsx", "src/index.tsx"],
            ProjectType.NODE_NEXTJS: ["app/page.tsx", "pages/index.tsx"],
            ProjectType.NODE_EXPRESS: ["index.js", "server.js", "app.js"],
            ProjectType.RUST: ["src/main.rs", "src/lib.rs"],
            ProjectType.GO: ["main.go", "cmd/main.go"],
        }
        found = []
        for rel in candidates.get(pt, []):
            if (workspace / rel).exists():
                found.append(rel)
        return found[:3]

    def _find_test_dirs(self, workspace: Path) -> list[str]:
        names = ["tests", "test", "__tests__", "spec", "specs", "e2e"]
        return [d for d in names if (workspace / d).is_dir()]

    def _find_config_files(self, workspace: Path, files: set[str]) -> list[str]:
        candidates = [
            "pyproject.toml",
            "package.json",
            "Cargo.toml",
            "go.mod",
            "tsconfig.json",
            ".eslintrc.json",
            "webpack.config.js",
            "vite.config.ts",
            "next.config.js",
            "docker-compose.yml",
            "Dockerfile",
            ".env.example",
        ]
        return [f for f in candidates if f in files]

    def _build_context_hints(self, pt: ProjectType) -> list[str]:
        hints: list[str] = []
        if pt in (
            ProjectType.PYTHON_FASTAPI,
            ProjectType.PYTHON_DJANGO,
            ProjectType.PYTHON_FLASK,
            ProjectType.PYTHON_GENERIC,
        ):
            hints.append("Always check imports and type hints in Python files")
            hints.append("Prefer pathlib over os.path for file operations")
        if pt == ProjectType.NODE_REACT:
            hints.append("Check for TypeScript types in *.d.ts files")
            hints.append("Components use named exports, not default where possible")
        if pt == ProjectType.RUST:
            hints.append("Check Cargo.toml features before suggesting dependencies")
            hints.append("Use ? operator for error propagation")
        if pt == ProjectType.GO:
            hints.append("Follow Go error handling: always check returned errors")
            hints.append("Check go.sum when suggesting new imports")
        return hints
