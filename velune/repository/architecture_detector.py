"""Architecture pattern detector — infers project pattern, features, and key files.

Operates purely on:
  - The list of RepositoryFile objects from the index
  - The TechStack from TechnologyDetector
  - The workspace root path (for reading package.json feature hints)

No LLM calls. No additional I/O beyond the already-indexed file list.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

if __debug__:
    from typing import TYPE_CHECKING

    if TYPE_CHECKING:
        from velune.repository.schemas import RepositoryFile
        from velune.repository.technology_detector import TechStack

logger = logging.getLogger("velune.repository.architecture_detector")


@dataclass
class ArchitectureReport:
    """Detected architecture information for a workspace."""

    pattern: str = "Unknown"  # "Feature-Based React Native", "MVC", "Layered"
    features: list[str] = field(default_factory=list)  # ["auth", "profile", "maps"]
    entry_points: list[str] = field(default_factory=list)  # ["src/app/_layout.tsx"]
    critical_files: list[str] = field(default_factory=list)  # ["AppContext.tsx", "api.ts"]
    state_files: list[str] = field(default_factory=list)  # context/store files
    api_files: list[str] = field(default_factory=list)  # service/api files
    routing_files: list[str] = field(default_factory=list)  # layout / router files
    config_files: list[str] = field(default_factory=list)  # app.json, tsconfig, etc.

    # Summary text ready for display
    state_mechanism: str | None = None  # "React Context", "Zustand", "Redux"
    routing_description: str | None = None  # "Expo Router (file-based)", "React Navigation"
    api_layer: str | None = None  # "src/services/api.ts"

    def summary_lines(self) -> list[str]:
        """Human-readable lines for display."""
        lines: list[str] = [f"Pattern:    {self.pattern}"]
        if self.features:
            lines.append(f"Features:   {' • '.join(self.features)}")
        if self.routing_description:
            lines.append(f"Routing:    {self.routing_description}")
        if self.state_mechanism:
            lines.append(f"State:      {self.state_mechanism}")
        if self.api_layer:
            lines.append(f"API layer:  {self.api_layer}")
        if self.entry_points:
            lines.append(f"Entry:      {self.entry_points[0]}")
        return lines

    def to_dict(self) -> dict:
        return {
            "pattern": self.pattern,
            "features": self.features,
            "entry_points": self.entry_points,
            "critical_files": self.critical_files,
            "state_files": self.state_files,
            "api_files": self.api_files,
            "routing_files": self.routing_files,
            "state_mechanism": self.state_mechanism,
            "routing_description": self.routing_description,
            "api_layer": self.api_layer,
        }


# ── Heuristic name sets ───────────────────────────────────────────────────────

_ENTRY_POINT_NAMES = {
    "_layout.tsx",
    "_layout.ts",  # Expo Router
    "index.tsx",
    "index.ts",
    "index.js",  # Standard entry
    "app.tsx",
    "app.ts",
    "app.js",  # React/React Native app root
    "main.py",
    "app.py",
    "server.py",
    "wsgi.py",  # Python
    "main.go",
    "cmd/main.go",  # Go
    "main.rs",
    "lib.rs",  # Rust
}

_STATE_FILE_PATTERNS = [
    re.compile(r"(?:context|store|state|provider|atom)", re.I),
]

_API_FILE_PATTERNS = [
    re.compile(r"(?:api|service|client|http|request|fetch)", re.I),
]

_CONFIG_FILE_NAMES = {
    "app.json",
    "app.config.ts",
    "app.config.js",
    "tsconfig.json",
    "tsconfig.base.json",
    "package.json",
    ".eslintrc.json",
    ".eslintrc.js",
    "babel.config.js",
    "metro.config.js",
    "tailwind.config.js",
    "tailwind.config.ts",
    "next.config.js",
    "next.config.ts",
    "next.config.mjs",
    "vite.config.ts",
    "vite.config.js",
}

_FEATURE_DIR_CANDIDATES = {
    # Common feature folder names that signal an isolated feature area
    "auth",
    "authentication",
    "login",
    "signup",
    "register",
    "profile",
    "user",
    "account",
    "settings",
    "home",
    "dashboard",
    "feed",
    "chat",
    "messages",
    "messaging",
    "booking",
    "checkout",
    "cart",
    "payment",
    "order",
    "maps",
    "location",
    "map",
    "notifications",
    "notification",
    "onboarding",
    "welcome",
    "search",
    "explore",
    "discover",
    "admin",
}


class ArchitectureDetector:
    """Infers project architecture from indexed file list + tech stack."""

    def __init__(
        self,
        root_path: Path,
        files: list[RepositoryFile],
        tech: TechStack,
    ) -> None:
        self.root = root_path.resolve()
        self.files = files
        self.tech = tech

    def detect(self) -> ArchitectureReport:
        report = ArchitectureReport()

        paths = [f.path.replace("\\", "/") for f in self.files]

        self._find_entry_points(report, paths)
        self._find_critical_files(report, paths)
        self._find_features(report, paths)
        self._classify_pattern(report, paths)
        self._describe_state(report, paths)
        self._describe_routing(report, paths)
        self._describe_api_layer(report, paths)

        return report

    # ------------------------------------------------------------------
    # Detection passes
    # ------------------------------------------------------------------

    def _find_entry_points(self, report: ArchitectureReport, paths: list[str]) -> None:
        for p in paths:
            basename = p.split("/")[-1].lower()
            if basename in _ENTRY_POINT_NAMES:
                report.entry_points.append(p)
                report.routing_files.append(p)
        # Sort: shorter paths (closer to root) first
        report.entry_points.sort(key=len)

    def _find_critical_files(self, report: ArchitectureReport, paths: list[str]) -> None:
        for p in paths:
            basename_lower = p.split("/")[-1].lower()
            basename_nosuffix = re.sub(r"\.[^.]+$", "", basename_lower)

            # State/context files
            if any(pat.search(basename_nosuffix) for pat in _STATE_FILE_PATTERNS):
                report.state_files.append(p)
                if p not in report.critical_files:
                    report.critical_files.append(p)

            # API/service files
            if any(pat.search(basename_nosuffix) for pat in _API_FILE_PATTERNS):
                report.api_files.append(p)
                if p not in report.critical_files:
                    report.critical_files.append(p)

            # Config files
            if basename_lower in _CONFIG_FILE_NAMES:
                report.config_files.append(p)

        # Limit critical_files to most relevant (shorter paths = higher level)
        report.critical_files.sort(key=lambda p: (len(p.split("/")), len(p)))
        report.critical_files = report.critical_files[:8]

    def _find_features(self, report: ArchitectureReport, paths: list[str]) -> None:
        """Detect feature areas from directory names."""
        feature_dirs: set[str] = set()

        for p in paths:
            parts = p.split("/")
            for part in parts[:-1]:  # skip the filename itself
                clean = part.lower().strip("()")
                if clean in _FEATURE_DIR_CANDIDATES:
                    feature_dirs.add(clean)

        # Also check Expo Router route groups like "(tabs)", "(auth)", "(app)"
        for p in paths:
            parts = p.split("/")
            for part in parts:
                if part.startswith("(") and part.endswith(")"):
                    group = part.strip("()")
                    if group and group not in {"tabs", "app", "root", "main"}:
                        feature_dirs.add(group)

        # Normalize and deduplicate similar names
        _alias = {
            "authentication": "auth",
            "login": "auth",
            "signup": "auth",
            "register": "auth",
            "account": "profile",
            "user": "profile",
            "notification": "notifications",
            "messages": "chat",
            "messaging": "chat",
            "checkout": "payment",
            "order": "payment",
            "location": "maps",
            "map": "maps",
        }
        normalized: set[str] = set()
        for f in feature_dirs:
            normalized.add(_alias.get(f, f))

        report.features = sorted(normalized)

    def _classify_pattern(self, report: ArchitectureReport, paths: list[str]) -> None:
        """Infer the architecture pattern label."""
        tech = self.tech

        # Expo Router → file-based routing = Feature-Based React Native
        if tech.router == "Expo Router" or tech.framework == "Expo":
            report.pattern = "Feature-Based React Native (Expo Router)"
            return

        # React Native with React Navigation → Feature-Based
        if tech.frontend == "React Native":
            report.pattern = "Feature-Based React Native"
            return

        # Next.js App Router detection (src/app/ directory with page.tsx files)
        has_page_tsx = any(p.endswith("page.tsx") or p.endswith("page.ts") for p in paths)
        has_app_dir = any("/app/" in p or p.startswith("app/") for p in paths)
        if tech.framework == "Next.js" and has_app_dir and has_page_tsx:
            report.pattern = "Next.js App Router"
            return
        if tech.framework == "Next.js":
            report.pattern = "Next.js Pages Router"
            return

        # NestJS → Layered / Module-based
        if tech.framework == "NestJS":
            report.pattern = "NestJS Module Architecture"
            return

        # Python frameworks
        if tech.framework in ("Django", "FastAPI", "Flask"):
            report.pattern = f"{tech.framework} MVC"
            return

        # Feature-based heuristic: many feature directories
        if len(report.features) >= 3:
            frontend_label = tech.frontend or "React"
            report.pattern = f"Feature-Based {frontend_label}"
            return

        # Check for src/components + src/pages pattern (SPA)
        has_components = any("/components/" in p for p in paths)
        has_pages = any("/pages/" in p or "/screens/" in p for p in paths)
        if has_components and has_pages:
            frontend_label = tech.frontend or "Web"
            report.pattern = f"Component/Screen-Based {frontend_label}"
            return

        report.pattern = "Unknown"

    def _describe_state(self, report: ArchitectureReport, paths: list[str]) -> None:
        tech = self.tech
        if tech.state_management:
            report.state_mechanism = tech.state_management
            return

        # Infer from file names
        has_context = any("context" in p.lower() for p in report.state_files)
        has_store = any("store" in p.lower() for p in report.state_files)
        has_atom = any("atom" in p.lower() for p in report.state_files)
        has_provider = any("provider" in p.lower() for p in report.state_files)

        if has_context or has_provider:
            report.state_mechanism = "React Context"
        elif has_store:
            report.state_mechanism = "Custom Store"
        elif has_atom:
            report.state_mechanism = "Atomic State"

    def _describe_routing(self, report: ArchitectureReport, paths: list[str]) -> None:
        tech = self.tech
        if tech.router == "Expo Router":
            report.routing_description = "Expo Router (file-based)"
        elif tech.router == "React Navigation":
            report.routing_description = "React Navigation (stack/tab)"
        elif tech.router == "React Router":
            report.routing_description = "React Router"
        elif tech.router == "TanStack Router":
            report.routing_description = "TanStack Router"
        elif tech.framework == "Next.js":
            has_page_tsx = any(p.endswith("page.tsx") or p.endswith("page.ts") for p in paths)
            report.routing_description = (
                "Next.js App Router" if has_page_tsx else "Next.js Pages Router"
            )
        elif tech.framework in ("Django", "FastAPI", "Flask", "Express", "NestJS"):
            report.routing_description = f"{tech.framework} routes"

    def _describe_api_layer(self, report: ArchitectureReport, paths: list[str]) -> None:
        # Pick the most representative API file
        if report.api_files:
            # Prefer files with "api" or "service" in name at src/services/ level
            preferred = [p for p in report.api_files if "/services/" in p or "/api/" in p]
            if preferred:
                report.api_layer = preferred[0]
            else:
                report.api_layer = report.api_files[0]
