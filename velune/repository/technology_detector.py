"""Technology stack detector — reads manifest files to identify frameworks and tools.

Reads package.json, app.json, requirements.txt, Cargo.toml, go.mod, etc.
and produces a structured TechStack that downstream systems (context builder,
architecture detector, explain command) can use without re-reading the disk.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger("velune.repository.technology_detector")

# Dependency name → (human label, category)
# Category: frontend | framework | router | auth | state | i18n | styling | testing | database | ui | build
_PACKAGE_MAP: dict[str, tuple[str, str]] = {
    # ── Frontends ──────────────────────────────────────────────────────────────
    "react-native": ("React Native", "frontend"),
    "react": ("React", "frontend"),
    "@angular/core": ("Angular", "frontend"),
    "vue": ("Vue.js", "frontend"),
    "svelte": ("Svelte", "frontend"),
    "solid-js": ("SolidJS", "frontend"),
    # ── Frameworks ────────────────────────────────────────────────────────────
    "expo": ("Expo", "framework"),
    "next": ("Next.js", "framework"),
    "@nestjs/core": ("NestJS", "framework"),
    "nestjs": ("NestJS", "framework"),
    "express": ("Express", "framework"),
    "fastify": ("Fastify", "framework"),
    "hono": ("Hono", "framework"),
    "koa": ("Koa", "framework"),
    "django": ("Django", "framework"),
    "flask": ("Flask", "framework"),
    "fastapi": ("FastAPI", "framework"),
    "starlette": ("Starlette", "framework"),
    # ── Routers ───────────────────────────────────────────────────────────────
    "expo-router": ("Expo Router", "router"),
    "react-router-dom": ("React Router", "router"),
    "react-router": ("React Router", "router"),
    "@react-navigation/native": ("React Navigation", "router"),
    "wouter": ("Wouter", "router"),
    "tanstack/react-router": ("TanStack Router", "router"),
    "@tanstack/react-router": ("TanStack Router", "router"),
    # ── Auth ──────────────────────────────────────────────────────────────────
    "@clerk/expo": ("Clerk", "auth"),
    "@clerk/nextjs": ("Clerk", "auth"),
    "@clerk/clerk-react": ("Clerk", "auth"),
    "next-auth": ("NextAuth.js", "auth"),
    "firebase": ("Firebase Auth", "auth"),
    "@supabase/supabase-js": ("Supabase Auth", "auth"),
    "passport": ("Passport.js", "auth"),
    "lucia": ("Lucia", "auth"),
    "better-auth": ("Better Auth", "auth"),
    "@auth0/nextjs-auth0": ("Auth0", "auth"),
    # ── State ─────────────────────────────────────────────────────────────────
    "zustand": ("Zustand", "state"),
    "redux": ("Redux", "state"),
    "@reduxjs/toolkit": ("Redux Toolkit", "state"),
    "mobx": ("MobX", "state"),
    "jotai": ("Jotai", "state"),
    "recoil": ("Recoil", "state"),
    "valtio": ("Valtio", "state"),
    "@tanstack/react-query": ("TanStack Query", "state"),
    "react-query": ("TanStack Query", "state"),
    "swr": ("SWR", "state"),
    # ── i18n ──────────────────────────────────────────────────────────────────
    "i18next": ("i18next", "i18n"),
    "react-i18next": ("react-i18next", "i18n"),
    "next-intl": ("next-intl", "i18n"),
    "@lingui/react": ("Lingui", "i18n"),
    "react-intl": ("react-intl", "i18n"),
    # ── Styling ───────────────────────────────────────────────────────────────
    "tailwindcss": ("Tailwind CSS", "styling"),
    "styled-components": ("Styled Components", "styling"),
    "@emotion/react": ("Emotion", "styling"),
    "nativewind": ("NativeWind", "styling"),
    "gluestack-ui": ("Gluestack UI", "styling"),
    "@gluestack-ui/themed": ("Gluestack UI", "styling"),
    # ── Testing ───────────────────────────────────────────────────────────────
    "jest": ("Jest", "testing"),
    "vitest": ("Vitest", "testing"),
    "cypress": ("Cypress", "testing"),
    "playwright": ("Playwright", "testing"),
    "@testing-library/react": ("React Testing Library", "testing"),
    "@testing-library/react-native": ("React Native Testing Library", "testing"),
    "detox": ("Detox", "testing"),
    # ── Database / ORM ────────────────────────────────────────────────────────
    "prisma": ("Prisma", "database"),
    "@prisma/client": ("Prisma", "database"),
    "drizzle-orm": ("Drizzle ORM", "database"),
    "mongoose": ("Mongoose", "database"),
    "typeorm": ("TypeORM", "database"),
    "sequelize": ("Sequelize", "database"),
    "knex": ("Knex.js", "database"),
    "@supabase/postgrest-js": ("Supabase DB", "database"),
    "better-sqlite3": ("SQLite", "database"),
    # ── UI Libraries ──────────────────────────────────────────────────────────
    "react-native-maps": ("react-native-maps", "ui"),
    "lucide-react": ("Lucide Icons", "ui"),
    "lucide-react-native": ("Lucide Icons", "ui"),
    "@radix-ui/react-primitives": ("Radix UI", "ui"),
    "shadcn-ui": ("shadcn/ui", "ui"),
    "@shopify/restyle": ("Restyle", "ui"),
    "react-native-paper": ("React Native Paper", "ui"),
    "@expo/vector-icons": ("Expo Icons", "ui"),
    # ── Build tools ───────────────────────────────────────────────────────────
    "vite": ("Vite", "build"),
    "webpack": ("Webpack", "build"),
    "esbuild": ("esbuild", "build"),
    "turbo": ("Turborepo", "build"),
    "nx": ("Nx", "build"),
}


@dataclass
class TechStack:
    """Structured technology snapshot for a workspace."""

    # Core identity
    language: str = "unknown"  # "TypeScript", "Python", "Go", "Rust"
    frontend: str | None = None  # "React Native", "React"
    framework: str | None = None  # "Expo", "Next.js", "NestJS"
    router: str | None = None  # "Expo Router", "React Router"
    auth: str | None = None  # "Clerk", "NextAuth.js"
    state_management: str | None = None  # "Zustand", "Redux Toolkit"
    i18n: str | None = None  # "i18next"
    styling: str | None = None  # "Tailwind CSS", "NativeWind"
    database: str | None = None  # "Prisma", "Mongoose"

    # Lists (multiple detected)
    testing: list[str] = field(default_factory=list)
    ui_libraries: list[str] = field(default_factory=list)
    build_tools: list[str] = field(default_factory=list)

    # Extras
    framework_version: str | None = None  # "54.0.0"
    is_monorepo: bool = False
    has_typescript: bool = False
    expo_sdk_version: str | None = None
    node_version: str | None = None

    # Raw dependency lists (for downstream querying)
    all_deps: list[str] = field(default_factory=list)
    all_dev_deps: list[str] = field(default_factory=list)

    def as_summary_lines(self) -> list[str]:
        """Human-readable summary lines for display."""
        lines: list[str] = []
        if self.language != "unknown":
            lines.append(f"Language:   {self.language}")
        if self.frontend:
            lines.append(f"Frontend:   {self.frontend}")
        if self.framework:
            ver = f" {self.framework_version}" if self.framework_version else ""
            lines.append(f"Framework:  {self.framework}{ver}")
        if self.router:
            lines.append(f"Router:     {self.router}")
        if self.auth:
            lines.append(f"Auth:       {self.auth}")
        if self.state_management:
            lines.append(f"State:      {self.state_management}")
        if self.i18n:
            lines.append(f"i18n:       {self.i18n}")
        if self.styling:
            lines.append(f"Styling:    {self.styling}")
        if self.database:
            lines.append(f"Database:   {self.database}")
        if self.testing:
            lines.append(f"Testing:    {', '.join(self.testing)}")
        if self.ui_libraries:
            lines.append(f"UI libs:    {', '.join(self.ui_libraries)}")
        return lines

    def to_dict(self) -> dict:
        return {
            "language": self.language,
            "frontend": self.frontend,
            "framework": self.framework,
            "framework_version": self.framework_version,
            "router": self.router,
            "auth": self.auth,
            "state_management": self.state_management,
            "i18n": self.i18n,
            "styling": self.styling,
            "database": self.database,
            "testing": self.testing,
            "ui_libraries": self.ui_libraries,
            "build_tools": self.build_tools,
            "is_monorepo": self.is_monorepo,
            "has_typescript": self.has_typescript,
            "expo_sdk_version": self.expo_sdk_version,
        }


class TechnologyDetector:
    """Detects the technology stack from manifest files in a workspace."""

    def __init__(self, root_path: Path) -> None:
        self.root = root_path.resolve()

    def detect(self) -> TechStack:
        """Read manifests and return a TechStack."""
        stack = TechStack()

        # Try each manifest type
        self._from_package_json(stack)
        self._from_app_json(stack)
        self._from_tsconfig(stack)
        self._from_python_manifests(stack)
        self._from_cargo_toml(stack)
        self._from_go_mod(stack)

        return stack

    # ------------------------------------------------------------------
    # Manifest readers
    # ------------------------------------------------------------------

    def _from_package_json(self, stack: TechStack) -> None:
        pkg_path = self.root / "package.json"
        if not pkg_path.exists():
            return

        try:
            with open(pkg_path, encoding="utf-8") as f:
                pkg = json.load(f)
        except Exception:
            return

        # Collect all deps (prod + dev)
        deps: dict[str, str] = {}
        deps.update(pkg.get("dependencies", {}))
        dev_deps: dict[str, str] = dict(pkg.get("devDependencies", {}))
        deps.update(dev_deps)

        stack.all_deps = list(pkg.get("dependencies", {}).keys())
        stack.all_dev_deps = list(dev_deps.keys())

        # Default language for package.json projects
        if stack.language == "unknown":
            stack.language = "JavaScript"

        # Workspaces → monorepo
        if "workspaces" in pkg or (self.root / "pnpm-workspace.yaml").exists():
            stack.is_monorepo = True

        # Node engine version
        engines = pkg.get("engines", {})
        if "node" in engines:
            stack.node_version = engines["node"]

        # Scan deps against the map
        for dep_name, dep_version in deps.items():
            entry = _PACKAGE_MAP.get(dep_name)
            if not entry:
                # Partial match on @scope/name — check the last segment
                last = dep_name.split("/")[-1] if "/" in dep_name else dep_name
                entry = _PACKAGE_MAP.get(last)
            if not entry:
                continue

            label, category = entry
            version = dep_version.lstrip("^~>=<").split(" ")[0]

            if category == "frontend" and not stack.frontend:
                stack.frontend = label
            elif category == "framework" and not stack.framework:
                stack.framework = label
                stack.framework_version = version
            elif category == "router" and not stack.router:
                stack.router = label
            elif category == "auth" and not stack.auth:
                stack.auth = label
            elif category == "state" and not stack.state_management:
                stack.state_management = label
            elif category == "i18n" and not stack.i18n:
                stack.i18n = label
            elif category == "styling" and not stack.styling:
                stack.styling = label
            elif category == "database" and not stack.database:
                stack.database = label
            elif category == "testing" and label not in stack.testing:
                stack.testing.append(label)
            elif category == "ui" and label not in stack.ui_libraries:
                stack.ui_libraries.append(label)
            elif category == "build" and label not in stack.build_tools:
                stack.build_tools.append(label)

    def _from_app_json(self, stack: TechStack) -> None:
        """Read app.json for Expo SDK version."""
        app_path = self.root / "app.json"
        if not app_path.exists():
            return
        try:
            with open(app_path, encoding="utf-8") as f:
                app = json.load(f)
            expo_cfg = app.get("expo", {})
            sdk = expo_cfg.get("sdkVersion")
            if sdk:
                stack.expo_sdk_version = sdk
            # Confirm Expo if app.json has "expo" key
            if expo_cfg and not stack.framework:
                stack.framework = "Expo"
        except Exception:
            pass

    def _from_tsconfig(self, stack: TechStack) -> None:
        """Presence of tsconfig.json → TypeScript."""
        if (self.root / "tsconfig.json").exists() or (self.root / "tsconfig.base.json").exists():
            stack.has_typescript = True
            stack.language = "TypeScript"

    def _from_python_manifests(self, stack: TechStack) -> None:
        """requirements.txt / pyproject.toml → Python project."""
        if not (
            (self.root / "requirements.txt").exists()
            or (self.root / "pyproject.toml").exists()
            or (self.root / "setup.py").exists()
        ):
            return

        if stack.language == "unknown":
            stack.language = "Python"

        # Try pyproject.toml deps
        pyproject = self.root / "pyproject.toml"
        if pyproject.exists():
            try:
                import tomllib  # Python 3.11+
            except ImportError:
                try:
                    import tomli as tomllib  # type: ignore[no-redef]
                except ImportError:
                    tomllib = None  # type: ignore[assignment]
            if tomllib:
                try:
                    with open(pyproject, "rb") as f:
                        data = tomllib.load(f)
                    deps_raw = data.get("project", {}).get("dependencies", []) or data.get(
                        "tool", {}
                    ).get("poetry", {}).get("dependencies", {})
                    if isinstance(deps_raw, dict):
                        deps_raw = list(deps_raw.keys())
                    for dep in deps_raw:
                        dep_name = dep.split("[")[0].split(">=")[0].split("==")[0].strip().lower()
                        entry = _PACKAGE_MAP.get(dep_name)
                        if entry:
                            label, category = entry
                            if category == "framework" and not stack.framework:
                                stack.framework = label
                            elif category == "database" and not stack.database:
                                stack.database = label
                            elif category == "testing" and label not in stack.testing:
                                stack.testing.append(label)
                except Exception:
                    pass

    def _from_cargo_toml(self, stack: TechStack) -> None:
        cargo = self.root / "Cargo.toml"
        if cargo.exists() and stack.language == "unknown":
            stack.language = "Rust"

    def _from_go_mod(self, stack: TechStack) -> None:
        gomod = self.root / "go.mod"
        if gomod.exists() and stack.language == "unknown":
            stack.language = "Go"
