"""M4: multi-language end-to-end benchmark.

Validates Velune's core behavior — language classification, symbol
extraction, project-type detection, and the full agentic tool loop
(grep → read → write a fix) — against generated fixture repositories in
Python, TypeScript, JavaScript, Go, Rust, Java, and C++.

Hermetic by design: a scripted provider stands in for the LLM (no API keys,
no network) and no language toolchain is invoked, so the suite runs on every
CI OS/Python combination. What *is* real: the parser, the scanner, the tool
registry, the path guard, the diff-preview write path, and the loop itself.
"""

from __future__ import annotations

import io
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace

import pytest
from rich.console import Console

from velune.core.types.inference import InferenceRequest, InferenceResponse, ToolCall
from velune.orchestration.tool_loop import ToolLoopRunner
from velune.repository.parser import RepositorySnapshotParser
from velune.repository.project_type import ProjectType, ProjectTypeDetector
from velune.repository.schemas import RepositoryLanguage, RepositorySymbolKind
from velune.tools.base.registry import ToolRegistry
from velune.tools.filesystem.read import ReadFile
from velune.tools.filesystem.search import GrepFiles
from velune.tools.filesystem.write import WriteFile

# ── Fixture projects ────────────────────────────────────────────────────────


@dataclass
class LangFixture:
    lang: str
    language: RepositoryLanguage
    manifest: dict[str, str]  # filename → content
    source_name: str  # buggy source file (relative)
    buggy_source: str  # contains add() returning a - b
    fixed_source: str  # corrected implementation
    expect_class: str  # a class/struct symbol the parser must find
    expect_function: str  # a function symbol the parser must find
    expect_import: bool  # parser must emit at least one import edge/symbol
    project_types: tuple[ProjectType, ...]  # acceptable detector outcomes
    extra: dict[str, str] = field(default_factory=dict)


FIXTURES: dict[str, LangFixture] = {
    "python": LangFixture(
        lang="python",
        language=RepositoryLanguage.PYTHON,
        manifest={"pyproject.toml": '[project]\nname = "calc"\nversion = "0.1.0"\n'},
        source_name="src/calc.py",
        buggy_source=(
            "import math\n\n\nclass Calculator:\n"
            "    def add(self, a, b):\n        return a - b\n"
            "\n\ndef area(r):\n    return math.pi * r * r\n"
        ),
        fixed_source=(
            "import math\n\n\nclass Calculator:\n"
            "    def add(self, a, b):\n        return a + b\n"
            "\n\ndef area(r):\n    return math.pi * r * r\n"
        ),
        expect_class="Calculator",
        expect_function="area",
        expect_import=True,
        project_types=(
            ProjectType.PYTHON_GENERIC,
            ProjectType.PYTHON_CLI,
        ),
    ),
    "typescript": LangFixture(
        lang="typescript",
        language=RepositoryLanguage.TYPESCRIPT,
        manifest={"package.json": '{"name": "calc", "version": "1.0.0"}\n'},
        source_name="src/calc.ts",
        buggy_source=(
            'import { log } from "./log";\n\n'
            "export class Calculator {\n"
            "  add(a: number, b: number): number { return a - b; }\n"
            "}\n\nexport function area(r: number): number { return Math.PI * r * r; }\n"
        ),
        fixed_source=(
            'import { log } from "./log";\n\n'
            "export class Calculator {\n"
            "  add(a: number, b: number): number { return a + b; }\n"
            "}\n\nexport function area(r: number): number { return Math.PI * r * r; }\n"
        ),
        expect_class="Calculator",
        expect_function="area",
        expect_import=True,
        project_types=(
            ProjectType.NODE_GENERIC,
            ProjectType.NODE_EXPRESS,
        ),
        extra={"src/log.ts": "export function log(msg: string): void { console.log(msg); }\n"},
    ),
    "javascript": LangFixture(
        lang="javascript",
        language=RepositoryLanguage.JAVASCRIPT,
        manifest={"package.json": '{"name": "calc", "version": "1.0.0"}\n'},
        source_name="src/calc.js",
        buggy_source=(
            'import { log } from "./log.js";\n\n'
            "export class Calculator {\n"
            "  add(a, b) { return a - b; }\n"
            "}\n\nexport function area(r) { return Math.PI * r * r; }\n"
        ),
        fixed_source=(
            'import { log } from "./log.js";\n\n'
            "export class Calculator {\n"
            "  add(a, b) { return a + b; }\n"
            "}\n\nexport function area(r) { return Math.PI * r * r; }\n"
        ),
        expect_class="Calculator",
        expect_function="area",
        expect_import=True,
        project_types=(ProjectType.NODE_GENERIC, ProjectType.NODE_EXPRESS),
        extra={"src/log.js": "export function log(msg) { console.log(msg); }\n"},
    ),
    "go": LangFixture(
        lang="go",
        language=RepositoryLanguage.GO,
        manifest={"go.mod": "module example.com/calc\n\ngo 1.22\n"},
        source_name="calc.go",
        buggy_source=(
            'package calc\n\nimport "fmt"\n\n'
            "type Calculator struct{}\n\n"
            "func Add(a, b int) int { return a - b }\n\n"
            'func Describe() { fmt.Println("calc") }\n'
        ),
        fixed_source=(
            'package calc\n\nimport "fmt"\n\n'
            "type Calculator struct{}\n\n"
            "func Add(a, b int) int { return a + b }\n\n"
            'func Describe() { fmt.Println("calc") }\n'
        ),
        expect_class="Calculator",
        expect_function="Add",
        expect_import=True,
        project_types=(ProjectType.GO,),
    ),
    "rust": LangFixture(
        lang="rust",
        language=RepositoryLanguage.RUST,
        manifest={"Cargo.toml": '[package]\nname = "calc"\nversion = "0.1.0"\nedition = "2021"\n'},
        source_name="src/lib.rs",
        buggy_source=(
            "use std::fmt;\n\npub struct Calculator;\n\n"
            "pub fn add(a: i64, b: i64) -> i64 { a - b }\n"
        ),
        fixed_source=(
            "use std::fmt;\n\npub struct Calculator;\n\n"
            "pub fn add(a: i64, b: i64) -> i64 { a + b }\n"
        ),
        expect_class="Calculator",
        expect_function="add",
        expect_import=True,
        project_types=(ProjectType.RUST,),
    ),
    "java": LangFixture(
        lang="java",
        language=RepositoryLanguage.JAVA,
        manifest={
            "pom.xml": (
                '<?xml version="1.0"?>\n<project><modelVersion>4.0.0</modelVersion>'
                "<groupId>com.example</groupId><artifactId>calc</artifactId>"
                "<version>1.0</version></project>\n"
            )
        },
        source_name="src/main/java/com/example/Calculator.java",
        buggy_source=(
            "package com.example;\n\nimport java.util.List;\n\n"
            "public class Calculator {\n"
            "    public int add(int a, int b) {\n        return a - b;\n    }\n"
            "}\n"
        ),
        fixed_source=(
            "package com.example;\n\nimport java.util.List;\n\n"
            "public class Calculator {\n"
            "    public int add(int a, int b) {\n        return a + b;\n    }\n"
            "}\n"
        ),
        expect_class="Calculator",
        expect_function="add",
        expect_import=True,
        project_types=(ProjectType.JAVA_GENERIC, ProjectType.JAVA_SPRING),
    ),
    "cpp": LangFixture(
        lang="cpp",
        language=RepositoryLanguage.CPP,
        manifest={"CMakeLists.txt": "cmake_minimum_required(VERSION 3.20)\nproject(calc)\n"},
        source_name="src/calc.cpp",
        buggy_source=(
            "#include <cmath>\n\nclass Calculator {\npublic:\n"
            "    int add(int a, int b) { return a - b; }\n};\n\n"
            "double area(double r) { return 3.14159 * r * r; }\n"
        ),
        fixed_source=(
            "#include <cmath>\n\nclass Calculator {\npublic:\n"
            "    int add(int a, int b) { return a + b; }\n};\n\n"
            "double area(double r) { return 3.14159 * r * r; }\n"
        ),
        expect_class="Calculator",
        expect_function="area",
        expect_import=True,
        # No C/C++ project type exists yet — the detector must still return a
        # coherent UNKNOWN profile rather than crash.
        project_types=(ProjectType.UNKNOWN,),
    ),
}

LANGS = sorted(FIXTURES)


def _materialize(fixture: LangFixture, root: Path) -> Path:
    for name, content in {**fixture.manifest, **fixture.extra}.items():
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    source = root / fixture.source_name
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text(fixture.buggy_source, encoding="utf-8")
    return root


# ── Language classification + symbol extraction ────────────────────────────


@pytest.mark.parametrize("lang", LANGS)
def test_parser_classifies_language(lang, tmp_path):
    fixture = FIXTURES[lang]
    root = _materialize(fixture, tmp_path)
    parser = RepositorySnapshotParser()
    detected = parser._detect_language(root / fixture.source_name)
    assert detected == fixture.language


@pytest.mark.parametrize("lang", LANGS)
def test_parser_extracts_symbols(lang, tmp_path):
    fixture = FIXTURES[lang]
    root = _materialize(fixture, tmp_path)
    parser = RepositorySnapshotParser()
    source = root / fixture.source_name
    symbols, edges = parser.parse_file(source, source.read_text(encoding="utf-8"))

    names_by_kind: dict[RepositorySymbolKind, set[str]] = {}
    for sym in symbols:
        names_by_kind.setdefault(sym.kind, set()).add(sym.name)

    assert fixture.expect_class in names_by_kind.get(RepositorySymbolKind.CLASS, set()), (
        f"{lang}: class {fixture.expect_class!r} not extracted; got {names_by_kind}"
    )
    found_functions = names_by_kind.get(RepositorySymbolKind.FUNCTION, set()) | names_by_kind.get(
        RepositorySymbolKind.METHOD, set()
    )
    assert fixture.expect_function in found_functions, (
        f"{lang}: function {fixture.expect_function!r} not extracted; got {names_by_kind}"
    )
    if fixture.expect_import:
        has_import = bool(names_by_kind.get(RepositorySymbolKind.IMPORT)) or bool(edges)
        assert has_import, f"{lang}: no import symbol/edge extracted"


# ── Project type detection ──────────────────────────────────────────────────


@pytest.mark.parametrize("lang", LANGS)
def test_project_type_detection(lang, tmp_path):
    fixture = FIXTURES[lang]
    root = _materialize(fixture, tmp_path)
    profile = ProjectTypeDetector().detect(root)
    assert profile.project_type in fixture.project_types, (
        f"{lang}: detected {profile.project_type}, expected one of {fixture.project_types}"
    )


# ── Full agentic fix loop over real tools ───────────────────────────────────


class ScriptedFixer:
    """Provider that greps for the bug, reads the file, writes the fix."""

    def __init__(self, fixture: LangFixture, root: Path) -> None:
        self._fixture = fixture
        self._root = root
        self._turn = 0

    def get_capabilities(self):
        return SimpleNamespace(supports_function_calling=True, supports_streaming=False)

    async def infer(self, request: InferenceRequest) -> InferenceResponse:
        self._turn += 1
        fx = self._fixture
        if self._turn == 1:
            calls = [
                ToolCall(
                    id="t1",
                    name="grep_files",
                    arguments={"pattern": "add", "directory": str(self._root)},
                )
            ]
        elif self._turn == 2:
            # The grep result (previous tool message) must mention the file.
            grep_output = request.messages[-1]["content"]
            assert fx.source_name.split("/")[-1] in grep_output, (
                f"grep did not locate the buggy file: {grep_output[:200]}"
            )
            calls = [ToolCall(id="t2", name="read_file", arguments={"file_path": fx.source_name})]
        elif self._turn == 3:
            read_output = request.messages[-1]["content"]
            assert "a - b" in read_output, "read_file did not return the buggy source"
            calls = [
                ToolCall(
                    id="t3",
                    name="write_file",
                    arguments={"file_path": fx.source_name, "content": fx.fixed_source},
                )
            ]
        else:
            write_output = request.messages[-1]["content"]
            assert "Successfully wrote" in write_output, write_output
            return InferenceResponse(
                content="Fixed the add() implementation.",
                model_id="scripted",
                finish_reason="stop",
                tokens_used=1,
                latency_ms=0.1,
            )
        return InferenceResponse(
            content="",
            model_id="scripted",
            finish_reason="tool_calls",
            tokens_used=1,
            latency_ms=0.1,
            tool_calls=calls,
        )


async def _allow_all(name, arguments, permissions) -> bool:
    return True


@pytest.mark.parametrize("lang", LANGS)
async def test_agentic_fix_loop(lang, tmp_path):
    from velune.execution.diff_preview import set_auto_accept

    fixture = FIXTURES[lang]
    root = _materialize(fixture, tmp_path)

    console = Console(file=io.StringIO(), force_terminal=False, width=100)
    registry = ToolRegistry()
    registry.register(ReadFile(workspace=root))
    registry.register(GrepFiles(workspace=root))
    registry.register(WriteFile(workspace=root, console=console))

    set_auto_accept(True)
    try:
        runner = ToolLoopRunner(
            ScriptedFixer(fixture, root), registry, approver=_allow_all, max_turns=6
        )
        result = await runner.run(
            InferenceRequest(
                model_id="scripted",
                messages=[{"role": "user", "content": f"Fix the add() bug in this {lang} repo"}],
            )
        )
    finally:
        set_auto_accept(False)

    assert result.stop_reason == "completed", result
    assert result.content == "Fixed the add() implementation."
    assert [inv.call.name for inv in result.invocations] == [
        "grep_files",
        "read_file",
        "write_file",
    ]
    assert not any(inv.error for inv in result.invocations), [
        (inv.call.name, inv.result) for inv in result.invocations
    ]
    # The fix must actually be on disk.
    on_disk = (root / fixture.source_name).read_text(encoding="utf-8")
    assert on_disk == fixture.fixed_source
    assert "a - b" not in on_disk
