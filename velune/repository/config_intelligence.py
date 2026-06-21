"""Config intelligence extractor for orchestrator-level project context injection.

Deeply parses project config files (pyproject.toml, package.json, Cargo.toml, go.mod)
to extract actionable project intelligence: test commands, build commands, lint commands,
key dependencies, and available scripts. Formatted as a token-efficient context block.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass
class ConfigIntelligence:
    """Structured project intelligence extracted from config files."""

    project_name: str | None = None
    version: str | None = None
    test_cmd: str | None = None
    build_cmd: str | None = None
    lint_cmd: str | None = None
    dev_cmd: str | None = None
    key_dependencies: list[str] = field(default_factory=list)
    available_scripts: dict[str, str] = field(default_factory=dict)
    config_source: str = ""

    def is_empty(self) -> bool:
        return not any(
            [
                self.test_cmd,
                self.build_cmd,
                self.lint_cmd,
                self.dev_cmd,
                self.key_dependencies,
                self.available_scripts,
            ]
        )


def _load_toml(path: Path) -> dict:
    """Load a TOML file using stdlib tomllib (3.11+), then tomli, then regex fallback."""
    try:
        import tomllib  # Python 3.11+

        with open(path, "rb") as f:
            return tomllib.load(f)
    except ImportError:
        pass
    try:
        import tomli  # type: ignore[import]

        with open(path, "rb") as f:
            return tomli.load(f)
    except ImportError:
        pass
    # Minimal regex fallback for simple key = "value" lines (not nested tables)
    data: dict = {}
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
        for line in text.splitlines():
            m = re.match(r'^(\w[\w.-]*)\s*=\s*"([^"]*)"', line)
            if m:
                data[m.group(1)] = m.group(2)
    except Exception:
        pass
    return data


class ConfigIntelligenceExtractor:
    """Extracts structured project intelligence from common config file formats."""

    MAX_DEPS = 15
    MAX_SCRIPTS = 10

    def extract(self, workspace: Path, config_files: list[str]) -> ConfigIntelligence:
        """Try each known config file in priority order, returning best-effort result."""
        priority = ["pyproject.toml", "package.json", "Cargo.toml", "go.mod"]
        parsers = {
            "pyproject.toml": self._parse_pyproject_toml,
            "package.json": self._parse_package_json,
            "Cargo.toml": self._parse_cargo_toml,
            "go.mod": self._parse_go_mod,
        }

        for fname in priority:
            if fname not in config_files:
                continue
            path = workspace / fname
            if not path.exists():
                continue
            try:
                intel = parsers[fname](path)
                if not intel.is_empty():
                    return intel
            except Exception as e:
                logger.debug("Config parse failed for %s: %s", fname, e)

        return ConfigIntelligence()

    def _parse_pyproject_toml(self, path: Path) -> ConfigIntelligence:
        data = _load_toml(path)

        project = data.get("project", {})
        poetry = data.get("tool", {}).get("poetry", {})
        name = project.get("name") or poetry.get("name")
        version = project.get("version") or poetry.get("version")

        # Dependencies
        deps: list[str] = []
        raw_deps = project.get("dependencies", []) or list(poetry.get("dependencies", {}).keys())
        if isinstance(raw_deps, list):
            deps = [d.split("[")[0].split(">=")[0].split("==")[0].strip() for d in raw_deps]
        elif isinstance(raw_deps, dict):
            deps = list(raw_deps.keys())
        deps = [d for d in deps if d and d.lower() != "python"]
        deps = deps[: self.MAX_DEPS]

        # Scripts / entry points
        scripts: dict[str, str] = {}
        project_scripts = project.get("scripts", {}) or poetry.get("scripts", {})
        if isinstance(project_scripts, dict):
            for k, v in list(project_scripts.items())[: self.MAX_SCRIPTS]:
                scripts[k] = str(v)

        # Detect common tool configs for test/lint commands
        tools = data.get("tool", {})
        test_cmd: str | None = None
        lint_cmd: str | None = None
        build_cmd: str | None = None

        if "pytest" in tools or "pytest" in str(deps).lower():
            test_cmd = "pytest"
            addopts = tools.get("pytest", {}).get("ini_options", {}).get("addopts", "")
            if addopts:
                test_cmd = f"pytest {addopts}"
        elif "unittest" in str(data):
            test_cmd = "python -m unittest"

        if "ruff" in tools:
            lint_cmd = "ruff check ."
        elif "flake8" in tools:
            lint_cmd = "flake8 ."
        elif "pylint" in tools:
            lint_cmd = "pylint ."
        elif "mypy" in tools:
            lint_cmd = "mypy ."

        # Build system
        build_sys = data.get("build-system", {}).get("build-backend", "")
        if "hatchling" in build_sys or "hatch" in str(tools):
            build_cmd = "hatch build"
        elif "flit" in build_sys:
            build_cmd = "flit build"
        elif "setuptools" in build_sys or "setup.py" in str(path.parent):
            build_cmd = "python -m build"

        # Dev server (common frameworks)
        dev_cmd: str | None = None
        dep_str = " ".join(deps).lower()
        if "fastapi" in dep_str or "uvicorn" in dep_str:
            dev_cmd = "uvicorn app.main:app --reload"
        elif "flask" in dep_str:
            dev_cmd = "flask run"
        elif "django" in dep_str:
            dev_cmd = "python manage.py runserver"

        return ConfigIntelligence(
            project_name=name,
            version=version,
            test_cmd=test_cmd,
            build_cmd=build_cmd,
            lint_cmd=lint_cmd,
            dev_cmd=dev_cmd,
            key_dependencies=deps,
            available_scripts=scripts,
            config_source="pyproject.toml",
        )

    def _parse_package_json(self, path: Path) -> ConfigIntelligence:
        import json

        data = json.loads(path.read_text(encoding="utf-8"))
        name = data.get("name")
        version = data.get("version")

        scripts = data.get("scripts", {})
        available: dict[str, str] = {}
        for k, v in list(scripts.items())[: self.MAX_SCRIPTS]:
            available[k] = str(v)

        test_cmd = scripts.get("test")
        build_cmd = scripts.get("build")
        lint_cmd = scripts.get("lint") or scripts.get("lint:check")
        dev_cmd = scripts.get("dev") or scripts.get("start") or scripts.get("serve")

        all_deps = {
            **data.get("dependencies", {}),
            **data.get("devDependencies", {}),
        }
        deps = list(all_deps.keys())[: self.MAX_DEPS]

        return ConfigIntelligence(
            project_name=name,
            version=version,
            test_cmd=test_cmd,
            build_cmd=build_cmd,
            lint_cmd=lint_cmd,
            dev_cmd=dev_cmd,
            key_dependencies=deps,
            available_scripts=available,
            config_source="package.json",
        )

    def _parse_cargo_toml(self, path: Path) -> ConfigIntelligence:
        data = _load_toml(path)
        pkg = data.get("package", {})
        name = pkg.get("name")
        version = pkg.get("version")

        deps = list(data.get("dependencies", {}).keys())[: self.MAX_DEPS]

        # Workspace members as additional context
        workspace_members = data.get("workspace", {}).get("members", [])
        scripts: dict[str, str] = {}
        if workspace_members:
            scripts["workspace_members"] = ", ".join(workspace_members[:5])

        features = list(data.get("features", {}).keys())
        if features:
            scripts["features"] = ", ".join(features[:8])

        return ConfigIntelligence(
            project_name=name,
            version=version,
            test_cmd="cargo test",
            build_cmd="cargo build --release",
            lint_cmd="cargo clippy",
            dev_cmd="cargo run",
            key_dependencies=deps,
            available_scripts=scripts,
            config_source="Cargo.toml",
        )

    def _parse_go_mod(self, path: Path) -> ConfigIntelligence:
        text = path.read_text(encoding="utf-8", errors="ignore")

        module_match = re.search(r"^module\s+(\S+)", text, re.MULTILINE)
        go_match = re.search(r"^go\s+(\S+)", text, re.MULTILINE)
        require_matches = re.findall(r"^\s+(\S+)\s+v[\d.]+", text, re.MULTILINE)

        name = module_match.group(1).split("/")[-1] if module_match else None
        version = go_match.group(1) if go_match else None
        deps = [r.split("/")[-1] for r in require_matches][: self.MAX_DEPS]

        return ConfigIntelligence(
            project_name=name,
            version=version,
            test_cmd="go test ./...",
            build_cmd="go build ./...",
            lint_cmd="golangci-lint run",
            dev_cmd="go run .",
            key_dependencies=deps,
            available_scripts={},
            config_source="go.mod",
        )

    def render_context_block(self, intel: ConfigIntelligence) -> str:
        """Format ConfigIntelligence as a [PROJECT COMMANDS] markdown section.

        Returns empty string if no actionable information is available.
        """
        if intel.is_empty():
            return ""

        lines: list[str] = ["## PROJECT COMMANDS"]
        if intel.project_name:
            header = f"Project: **{intel.project_name}**"
            if intel.version:
                header += f" v{intel.version}"
            if intel.config_source:
                header += f" ({intel.config_source})"
            lines.append(header)
            lines.append("")

        cmds: list[str] = []
        if intel.test_cmd:
            cmds.append(f"- Test:  `{intel.test_cmd}`")
        if intel.build_cmd:
            cmds.append(f"- Build: `{intel.build_cmd}`")
        if intel.lint_cmd:
            cmds.append(f"- Lint:  `{intel.lint_cmd}`")
        if intel.dev_cmd:
            cmds.append(f"- Dev:   `{intel.dev_cmd}`")
        if cmds:
            lines.extend(cmds)

        if intel.available_scripts:
            lines.append("")
            lines.append("Scripts:")
            for name, cmd in list(intel.available_scripts.items())[: self.MAX_SCRIPTS]:
                lines.append(f"  - `{name}`: {cmd}")

        if intel.key_dependencies:
            lines.append("")
            lines.append("Key deps: " + ", ".join(intel.key_dependencies))

        return "\n".join(lines)
