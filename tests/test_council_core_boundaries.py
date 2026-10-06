"""The package boundary, enforced: import rules per layer, light import, neutrality, no I/O."""

from __future__ import annotations

import ast
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "velune"
COUNCIL = PACKAGE / "council"
ADAPTERS = COUNCIL / "adapters"

CORE_FILES = sorted(p for p in COUNCIL.glob("*.py"))
ADAPTER_FILES = sorted(p for p in ADAPTERS.glob("*.py"))

# What the core may import beyond the standard library and itself.
CORE_THIRD_PARTY = {"pydantic"}
CORE_VELUNE = ("velune._compat", "velune.council", "velune.cognition.execution_trace")

# Stdlib modules that would give the core a side channel (I/O, time, randomness, processes).
FORBIDDEN_STDLIB = {
    "os",
    "sys",
    "subprocess",
    "socket",
    "ssl",
    "shutil",
    "pathlib",
    "tempfile",
    "glob",
    "time",
    "datetime",
    "uuid",
    "random",
    "secrets",
    "urllib",
    "http",
    "threading",
    "multiprocessing",
    "ctypes",
    "importlib",
    "pickle",
    "shelve",
    "sqlite3",
    "logging",
}

# Heavy / authority-bearing parts of the codebase the core must never reach.
FORBIDDEN_VELUNE = (
    "velune.providers",
    "velune.models",
    "velune.cognition.council",
    "velune.cognition.orchestrator",
    "velune.core.trace",
    "velune.permissions",
    "velune.tools",
    "velune.execution",
    "velune.cli",
    "velune.mcp",
    "velune.kernel",
    "velune.memory",
    "velune.orchestration",
)

BUILTIN_SIDE_EFFECTS = {"open", "print", "input", "exec", "eval", "compile", "__import__"}


def imports_of(path: Path, *, strict: bool = True) -> list[tuple[str, int]]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.extend((alias.name, node.lineno) for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                assert not strict, f"{path.name}:{node.lineno} uses a relative import"
                continue
            found.append((node.module or "", node.lineno))
    return found


def under(module: str, prefixes: tuple[str, ...] | set[str]) -> bool:
    return any(module == p or module.startswith(p + ".") for p in prefixes)


def stdlib(module: str) -> bool:
    return module.split(".")[0] in sys.stdlib_module_names


def test_expected_modules_exist():
    names = {p.stem for p in CORE_FILES}
    assert {
        "__init__",
        "domain",
        "profiles",
        "contracts",
        "request",
        "results",
        "stages",
        "state",
        "runner",
        "ports",
        "serialization",
        "trace",
    } <= names
    assert {p.stem for p in ADAPTER_FILES} == {"__init__", "runtime", "legacy", "prompts", "engine"}


@pytest.mark.parametrize("path", CORE_FILES, ids=lambda p: p.name)
def test_core_imports_only_stdlib_pydantic_and_the_light_allowlist(path):
    for module, line in imports_of(path):
        if stdlib(module) or under(module, CORE_THIRD_PARTY):
            continue
        assert under(module, CORE_VELUNE), f"{path.name}:{line} imports {module}"


@pytest.mark.parametrize("path", CORE_FILES, ids=lambda p: p.name)
def test_core_never_reaches_heavy_or_authority_bearing_packages(path):
    for module, line in imports_of(path):
        assert not under(module, FORBIDDEN_VELUNE), f"{path.name}:{line} imports {module}"
        assert not module.startswith("velune.council.adapters"), f"{path.name}:{line} -> adapters"


@pytest.mark.parametrize("path", CORE_FILES, ids=lambda p: p.name)
def test_core_has_no_io_clock_randomness_or_process_access(path):
    for module, line in imports_of(path):
        top = module.split(".")[0]
        assert top not in FORBIDDEN_STDLIB, f"{path.name}:{line} imports {module}"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            assert node.func.id not in BUILTIN_SIDE_EFFECTS, f"{path.name}:{node.lineno}"


def test_legacy_adapter_imports_only_the_core_and_stdlib():
    for module, line in imports_of(ADAPTERS / "legacy.py"):
        assert (
            stdlib(module) or under(module, ("velune.council",)) or under(module, CORE_THIRD_PARTY)
        ), f"legacy.py:{line} imports {module}"


def test_runtime_adapter_never_reaches_authority_bearing_packages():
    banned = (
        "velune.permissions",
        "velune.tools",
        "velune.execution",
        "velune.cli",
        "velune.mcp",
    )
    for name in ("runtime.py", "prompts.py", "engine.py"):
        for module, line in imports_of(ADAPTERS / name):
            assert not under(module, banned), f"{name}:{line} imports {module}"


def test_only_the_runtime_adapter_touches_the_provider_stack():
    light = ["__init__.py", "legacy.py", "prompts.py", "engine.py"]
    for path in [*CORE_FILES, *(ADAPTERS / name for name in light)]:
        for module, line in imports_of(path):
            assert not under(module, FORBIDDEN_VELUNE), f"{path.name}:{line} imports {module}"


# ── nothing live depends on the core (zero behavioural change) ──────────────


def test_no_module_outside_the_core_imports_it():
    offenders = []
    for path in PACKAGE.rglob("*.py"):
        if COUNCIL in path.parents:
            continue
        for module, line in imports_of(path, strict=False):
            if module == "velune.council" or module.startswith("velune.council."):
                offenders.append(f"{path.relative_to(ROOT)}:{line} imports {module}")
    assert offenders == []


def test_legacy_orchestrator_does_not_import_the_core():
    orchestrator = PACKAGE / "cognition" / "orchestrator.py"
    assert not any(under(m, ("velune.council",)) for m, _ in imports_of(orchestrator, strict=False))
    for path in (PACKAGE / "cognition" / "council").glob("*.py"):
        assert not any(under(m, ("velune.council",)) for m, _ in imports_of(path, strict=False)), (
            path.name
        )


# ── light import ────────────────────────────────────────────────────────────

_LIGHT_IMPORT = """
import sys
import velune.council
import velune.council.domain, velune.council.profiles, velune.council.contracts
import velune.council.request, velune.council.results, velune.council.stages
import velune.council.state, velune.council.runner, velune.council.ports
import velune.council.serialization, velune.council.trace
heavy = [
    "velune.providers", "velune.models", "velune.cli", "velune.permissions", "velune.tools",
    "velune.execution", "velune.mcp", "velune.kernel", "velune.cognition.council",
    "velune.cognition.orchestrator", "velune.core.trace", "velune.council.adapters",
    "rich", "prompt_toolkit", "httpx",
]
loaded = sorted(m for m in sys.modules if any(m == h or m.startswith(h + ".") for h in heavy))
print("LOADED=" + ",".join(loaded))
"""


def test_importing_the_core_loads_no_provider_cli_or_authority_modules():
    run = subprocess.run(
        [sys.executable, "-c", _LIGHT_IMPORT],
        capture_output=True,
        text=True,
        cwd=ROOT,
        timeout=120,
    )
    assert run.returncode == 0, run.stderr
    loaded = run.stdout.strip().split("LOADED=", 1)[1]
    assert loaded == "", f"core import pulled in: {loaded}"


def test_importing_the_adapters_is_what_loads_the_runtime_stack():
    code = (
        "import sys, velune.council.adapters.legacy;"
        "print(any(m.startswith('velune.providers') for m in sys.modules))"
    )
    run = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, cwd=ROOT, timeout=120
    )
    assert run.returncode == 0, run.stderr
    assert run.stdout.strip() == "False"  # the pure mapper stays light too


# ── domain neutrality ───────────────────────────────────────────────────────

CODING_WORDS = {"coder", "repository", "repo", "patch", "diff", "git", "file", "files"}
NEUTRAL_FILES = [p for p in CORE_FILES if p.name != "profiles.py"]


def words(text: str) -> set[str]:
    spaced = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", text)
    return {w.lower() for w in re.findall(r"[A-Za-z]+", spaced)}


def docstring_nodes(tree: ast.AST) -> set[int]:
    found: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                found.add(id(body[0].value))
    return found


@pytest.mark.parametrize("path", NEUTRAL_FILES, ids=lambda p: p.name)
def test_core_code_uses_no_coding_vocabulary_outside_profile_data(path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    skip = docstring_nodes(tree)
    names: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            names.append((node.id, node.lineno))
        elif isinstance(node, ast.Attribute):
            names.append((node.attr, node.lineno))
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.append((node.name, node.lineno))
        elif isinstance(node, ast.arg):
            names.append((node.arg, node.lineno))
        elif isinstance(node, ast.keyword) and node.arg:
            names.append((node.arg, node.value.lineno))
        elif (
            isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in skip
        ):
            names.append((node.value, node.lineno))
    for text, line in names:
        hit = words(text) & CODING_WORDS
        assert not hit, f"{path.name}:{line} uses {sorted(hit)} in {text!r}"


def test_no_core_module_branches_on_the_domain():
    for path in CORE_FILES:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Compare):
                text = ast.unparse(node)
                assert "CouncilDomain" not in text and ".domain" not in text, (
                    f"{path.name}:{node.lineno} compares on the domain: {text}"
                )


def test_the_legacy_mapper_does_not_branch_on_the_domain_either():
    tree = ast.parse((ADAPTERS / "legacy.py").read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Compare):
            assert "domain" not in ast.unparse(node).lower()


# ── execution authority stays outside ───────────────────────────────────────


def test_the_core_names_nothing_that_could_execute_or_grant_permission():
    forbidden = {"permission", "approve", "authorize", "execute", "subprocess", "sandbox"}
    for path in CORE_FILES:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        skip = docstring_nodes(tree)
        for node in ast.walk(tree):
            texts = []
            if isinstance(node, ast.Name):
                texts.append(node.id)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                texts.append(node.name)
            elif isinstance(node, ast.arg):
                texts.append(node.arg)
            elif (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and id(node) not in skip
            ):
                texts.append(node.value)
            for text in texts:
                assert not words(text) & forbidden, f"{path.name}:{node.lineno} {text!r}"


def test_no_prompt_text_lives_in_the_core():
    from velune.council.profiles import BUILTIN_PROFILES

    for profile in BUILTIN_PROFILES:
        for seat in profile.all_seats:
            assert seat.prompt_key is None or seat.prompt_key.startswith("council.general.")
            assert seat.prompt_key is None or len(seat.prompt_key) < 64  # a key, never text
            assert len(seat.objective) <= 200
    prompts_dir = COUNCIL / "prompts"
    assert not prompts_dir.exists()


def test_public_surface_is_importable_and_matches_all():
    import velune.council as core

    assert all(hasattr(core, name) for name in core.__all__)
    assert "RuntimeSeatInvoker" not in core.__all__
    assert core.ArbitrationResult.__name__ == "ArbitrationResult"
