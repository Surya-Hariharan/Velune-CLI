"""Every production tool schema survives each provider's tool-definition translation.

The tool loop builds OpenAI-format definitions once; Anthropic, Gemini and Cohere
adapters translate them. A schema a provider cannot represent would fail the whole
request (HTTP 400) and silently demote the model to plain chat for the session.
"""

from __future__ import annotations

import re
from types import SimpleNamespace

import pytest

from tests._fs_registry import production_registry
from velune.orchestration.tool_loop import ToolLoopRunner
from velune.providers.adapters.anthropic import AnthropicProvider
from velune.providers.adapters.cohere import _to_cohere_tools
from velune.providers.adapters.google import _to_gemini_tools

FILESYSTEM_TOOLS = {
    "read_file",
    "read_directory",
    "write_file",
    "edit_file",
    "create_file",
    "delete_file",
    "create_directory",
    "delete_directory",
    "move_path",
    "grep_files",
    "find_files",
}
_JSON_TYPES = {"string", "integer", "number", "boolean", "array", "object"}


@pytest.fixture
def definitions(tmp_path):
    provider = SimpleNamespace(provider_id="test")
    runner = ToolLoopRunner(provider, production_registry(tmp_path))  # type: ignore[arg-type]
    return runner.build_tool_definitions()


def _walk(schema: dict):
    yield schema
    for sub in (schema.get("properties") or {}).values():
        yield from _walk(sub)
    if isinstance(schema.get("items"), dict):
        yield from _walk(schema["items"])


def test_filesystem_tools_are_all_exposed(definitions):
    names = {d["function"]["name"] for d in definitions}
    assert names >= FILESYSTEM_TOOLS


def test_definitions_are_well_formed_openai_functions(definitions):
    for d in definitions:
        fn = d["function"]
        assert d["type"] == "function"
        assert re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", fn["name"])
        assert fn["description"].strip(), fn["name"]
        params = fn["parameters"]
        assert params["type"] == "object"
        assert set(params.get("required", [])) <= set(params.get("properties", {})), fn["name"]
        for node in _walk(params):
            assert node.get("type") in _JSON_TYPES, (fn["name"], node)


def test_filesystem_schemas_avoid_keywords_providers_reject(definitions):
    # Gemini's function-declaration schema is an OpenAPI subset; keep the
    # filesystem tools to plain type/description/required/minimum.
    for d in definitions:
        if d["function"]["name"] not in FILESYSTEM_TOOLS:
            continue
        for node in _walk(d["function"]["parameters"]):
            assert "default" not in node, d["function"]["name"]


def test_anthropic_translation(definitions):
    for d in definitions:
        tool = AnthropicProvider._translate_tool(d)
        assert tool["name"] == d["function"]["name"]
        assert tool["input_schema"] == d["function"]["parameters"]


def test_gemini_translation(definitions):
    translated = _to_gemini_tools(definitions)
    assert translated is not None
    declarations = translated[0]["functionDeclarations"]
    assert [x["name"] for x in declarations] == [d["function"]["name"] for d in definitions]
    edit = next(x for x in declarations if x["name"] == "edit_file")
    assert edit["parameters"]["required"] == ["file_path", "old_string", "new_string"]


def test_cohere_translation_keeps_required_flags(definitions):
    translated = {t["name"]: t for t in _to_cohere_tools(definitions) or []}
    edit = translated["edit_file"]["parameter_definitions"]
    assert edit["old_string"]["required"] is True
    assert edit["replace_all"]["required"] is False
    assert edit["replace_all"]["type"] == "bool"
