"""Integration tool definitions survive every provider's translation (VAIR A2).

What this proves, and what it does not: these tests take the *real* definitions the
tool loop builds for every shipped integration and run them through each adapter's
own tool-definition translator, asserting nothing is lost or malformed. They are
offline. They do **not** prove that a given provider accepts the payload — no live
provider was called — only that the shapes are portable and the adapters can
represent them.

Every provider in this repository speaks the same internal OpenAI function format;
the OpenAI-compatible adapters (OpenAI, Groq, OpenRouter, Ollama, LM Studio, vLLM and
the other compatible gateways) send it as-is, so the checks on the neutral format
cover them. Anthropic, Gemini and Cohere translate it, and are tested individually.
"""

from __future__ import annotations

import json
import re
from types import SimpleNamespace

import pytest

from tests._fs_registry import production_registry
from tests.integration_fakes import many_ops_app
from velune.core.task_registry import JobRegistry
from velune.core.types.inference import InferenceRequest
from velune.orchestration.tool_loop import ToolLoopRunner
from velune.providers.adapters._toolcalls import attach_openai_tools
from velune.providers.adapters.anthropic import AnthropicProvider
from velune.providers.adapters.cohere import _to_cohere_tools
from velune.providers.adapters.google import _to_gemini_tools
from velune.resources.manager import ResourceManager
from velune.resources.tools import IntegrationToolSource

ALLOWED_NODE_KEYS = {"type", "description", "enum", "items", "properties", "required"}
FORBIDDEN_KEYWORDS = {
    "default",
    "minimum",
    "maximum",
    "minLength",
    "maxLength",
    "format",
    "pattern",
    "additionalProperties",
    "$ref",
    "anyOf",
    "oneOf",
    "allOf",
    "nullable",
}
_NAME = re.compile(r"^[a-zA-Z][a-zA-Z0-9_-]{0,63}$")
PROVIDER = SimpleNamespace(provider_id="test")


def build(tmp_path, *, max_tools: int = 24, enable_all: bool = True):
    manager = ResourceManager(workspace=tmp_path, job_registry=JobRegistry())
    manager.register_defaults()
    source = IntegrationToolSource(manager, max_tools=max_tools)
    source.begin_run("start the database and test the api")
    if enable_all:  # availability is a runtime fact; exposure shape is what is under test
        for manifest in source.catalog.manifests():
            source.scope.enable(manifest)
    runner = ToolLoopRunner(PROVIDER, production_registry(tmp_path), dynamic_tools=source)  # type: ignore[arg-type]
    return runner, source


@pytest.fixture
def definitions(tmp_path):
    runner, _ = build(tmp_path)
    return runner.build_tool_definitions()


@pytest.fixture
def all_direct_definitions(tmp_path):
    """Every shipped operation as its own tool (a budget large enough for all of them)."""
    runner, source = build(tmp_path, max_tools=100)
    assert not source.scope.dispatched
    return runner.build_tool_definitions()


@pytest.fixture
def overflow_definitions(tmp_path):
    runner, source = build(tmp_path, max_tools=6)
    assert source.scope.dispatched, "expected an overflow so the dispatcher is present"
    return runner.build_tool_definitions()


def walk(schema):
    yield schema
    for sub in (schema.get("properties") or {}).values():
        yield from walk(sub)
    if isinstance(schema.get("items"), dict):
        yield from walk(schema["items"])


def integration_defs(defs):
    native = {t.get_name() for t in production_registry_tools()}
    return [d for d in defs if d["function"]["name"] not in native]


def production_registry_tools():
    import tempfile
    from pathlib import Path

    reg = production_registry(Path(tempfile.mkdtemp()))
    return [reg.get(n) for n in reg.list_tools()]


# ── The neutral (OpenAI-format) definitions ─────────────────────────────────


def test_definitions_are_well_formed_functions(definitions):
    names = [d["function"]["name"] for d in definitions]
    assert len(names) == len(set(names)), "tool names must be unique"
    for d in definitions:
        fn = d["function"]
        assert d["type"] == "function" and _NAME.match(fn["name"]), fn["name"]
        assert fn["description"].strip()
        params = fn["parameters"]
        assert params["type"] == "object"
        assert set(params.get("required", [])) <= set(params.get("properties", {}))


def test_integration_tools_use_only_the_portable_schema_subset(definitions):
    extra = integration_defs(definitions)
    assert len(extra) > 20  # the shipped integrations really are covered
    for d in extra:
        for node in walk(d["function"]["parameters"]):
            assert set(node) <= ALLOWED_NODE_KEYS, (d["function"]["name"], set(node))
            assert not (set(node) & FORBIDDEN_KEYWORDS), d["function"]["name"]


def test_no_integration_tool_takes_an_object_parameter(definitions, overflow_definitions):
    """Gemini wants OBJECT parameters to declare properties; none of ours need to."""
    for defs in (definitions, overflow_definitions):
        for d in integration_defs(defs):
            for node in walk(d["function"]["parameters"]):
                if node is not d["function"]["parameters"]:
                    assert node.get("type") != "object", d["function"]["name"]


def test_the_dispatcher_takes_its_arguments_as_json_text(overflow_definitions):
    dispatcher = next(
        d for d in overflow_definitions if d["function"]["name"] == "integration_call"
    )
    props = dispatcher["function"]["parameters"]["properties"]
    assert props["arguments"]["type"] == "string"
    assert dispatcher["function"]["parameters"]["required"] == ["integration", "operation"]


def test_discovery_tools_are_always_present_and_enum_constrained(tmp_path):
    runner, source = build(tmp_path, enable_all=False)
    defs = {d["function"]["name"]: d["function"] for d in runner.build_tool_definitions()}
    enable = defs["integrations_enable"]["parameters"]["properties"]["integration"]
    assert set(enable["enum"]) == {"docker", "postgres", "mysql", "supabase", "process", "http"}
    assert "integrations_list" in defs and "integrations_disable" not in defs


def test_tool_count_stays_within_the_repos_own_ceiling_at_default_settings(definitions):
    # 64 is the largest tool set the repo's native engine accepts (engine/spec.py MAX_TOOLS).
    assert len(definitions) <= 64


def test_ordering_is_stable_so_prompt_caches_survive(tmp_path):
    runner, source = build(tmp_path, enable_all=False)
    manifests = {m.id: m for m in source.catalog.manifests()}
    source.scope.enable(manifests["process"])
    first = [d["function"]["name"] for d in runner.build_tool_definitions()]
    assert first == [d["function"]["name"] for d in runner.build_tool_definitions()]
    source.scope.enable(manifests["docker"])
    second = [d["function"]["name"] for d in runner.build_tool_definitions()]
    # Everything that was offered before is still offered, in the same order
    # (the only change in the middle is the new "disable" tool appearing once).
    kept = [n for n in second if n in first]
    assert kept == first


# ── Each adapter's own translation ──────────────────────────────────────────


def test_openai_compatible_wire_format_is_the_neutral_format(definitions):
    request = InferenceRequest(model_id="m", messages=[], tools=definitions)
    payload: dict = {}
    attach_openai_tools(payload, request)
    assert payload["tools"] == definitions
    json.dumps(payload)  # serializable: what OpenAI, Groq, OpenRouter, Ollama, LM Studio send


def test_anthropic_translation(definitions):
    for d in definitions:
        tool = AnthropicProvider._translate_tool(d)
        assert tool["name"] == d["function"]["name"]
        assert tool["input_schema"] == d["function"]["parameters"]
        assert tool["input_schema"]["type"] == "object"


def test_gemini_translation_keeps_every_tool_and_all_declared_parameters(definitions):
    declarations = _to_gemini_tools(definitions)[0]["functionDeclarations"]
    assert [x["name"] for x in declarations] == [d["function"]["name"] for d in definitions]
    by_name = {d["function"]["name"]: d["function"] for d in definitions}
    for decl in declarations:
        original = by_name[decl["name"]]["parameters"]
        if original.get("properties"):
            assert decl["parameters"] == original
    logs = next(x for x in declarations if x["name"] == "docker__logs")
    assert logs["parameters"]["required"] == ["container"]


def test_gemini_omits_parameters_for_parameterless_functions(all_direct_definitions):
    definitions = all_direct_definitions
    declarations = {x["name"]: x for x in _to_gemini_tools(definitions)[0]["functionDeclarations"]}
    parameterless = [
        d["function"]["name"]
        for d in definitions
        if not d["function"]["parameters"].get("properties")
    ]
    assert "docker__images" in parameterless and "process__list" in parameterless
    for name in parameterless:
        assert "parameters" not in declarations[name]  # never an empty OBJECT schema
    assert "parameters" in declarations["docker__logs"]


def test_gemini_translation_of_odd_inputs():
    assert _to_gemini_tools(None) is None and _to_gemini_tools([]) is None
    out = _to_gemini_tools([{"name": "bare", "description": "d"}])
    assert out == [{"functionDeclarations": [{"name": "bare", "description": "d"}]}]
    out = _to_gemini_tools(
        [{"type": "function", "function": {"name": "n", "description": "d", "parameters": None}}]
    )
    assert "parameters" not in out[0]["functionDeclarations"][0]


def test_cohere_translation_keeps_types_and_required_flags(all_direct_definitions):
    definitions = all_direct_definitions
    translated = {t["name"]: t for t in _to_cohere_tools(definitions) or []}
    logs = translated["docker__logs"]["parameter_definitions"]
    assert logs["container"]["required"] is True and logs["container"]["type"] == "str"
    assert logs["tail"]["required"] is False and logs["tail"]["type"] == "int"
    query = translated["supabase__query"]["parameter_definitions"]
    assert query["service_role"]["type"] == "bool" and query["limit"]["type"] == "int"
    assert len(translated) == len(definitions)


# ── Token overhead (deterministic ceilings, with margin over measured values) ─


def size(defs) -> int:
    return len(json.dumps(defs, separators=(",", ":")))


def test_initial_exposure_is_small(tmp_path):
    """Measured ~1.2k characters (~300 tokens) for the two discovery tools."""
    runner, _ = build(tmp_path, enable_all=False)
    base = size(ToolLoopRunner(PROVIDER, production_registry(tmp_path)).build_tool_definitions())  # type: ignore[arg-type]
    added = size(runner.build_tool_definitions()) - base
    assert added < 1600


def test_per_tool_overhead_is_bounded(definitions):
    """Measured ~275 characters (~70 tokens) per operation tool."""
    extra = integration_defs(definitions)
    assert size(extra) / len(extra) < 450
    assert size(extra) < 12500  # every shipped integration, default budget (~3k tokens)


def test_one_integration_costs_what_it_should(tmp_path):
    """Measured ~13.4k characters (~3.3k tokens) for all 36 Docker operations as tool definitions."""
    runner, source = build(tmp_path, enable_all=False)
    docker = next(m for m in source.catalog.manifests() if m.id == "docker")
    source.scope.max_tools = 100  # enough for every operation to be its own tool
    source.scope.enable(docker)
    docker_defs = [
        d for d in runner.build_tool_definitions() if d["function"]["name"].startswith("docker__")
    ]
    assert len(docker_defs) == 36 and size(docker_defs) < 14500
    # Enabling just one category (what a task usually needs) is far cheaper.
    source.scope.reset()
    source.scope.enable(docker, ["inspect"])
    inspect_defs = [
        d for d in runner.build_tool_definitions() if d["function"]["name"].startswith("docker__")
    ]
    assert len(inspect_defs) == 12 and size(inspect_defs) < 4200


def test_a_larger_budget_exposes_more_a_smaller_one_less(tmp_path):
    small, s_src = build(tmp_path, max_tools=4)
    large, l_src = build(tmp_path, max_tools=64)
    assert len(s_src.scope.exposed) == 3 and len(l_src.scope.exposed) == 63  # the budget minus the dispatcher
    assert l_src.scope.dispatched  # the rest stay reachable through integration_call
    assert size(small.build_tool_definitions()) < size(large.build_tool_definitions())


def test_a_big_integration_never_loses_an_operation_to_the_budget(tmp_path):
    manager = ResourceManager(workspace=tmp_path)
    manager.register("bigapp", many_ops_app(40))
    source = IntegrationToolSource(manager, max_tools=8)
    source.scope.enable(source.catalog.manifest("bigapp"))
    runner = ToolLoopRunner(PROVIDER, None, dynamic_tools=source)  # type: ignore[arg-type]
    names = [d["function"]["name"] for d in runner.build_tool_definitions()]
    assert len([n for n in names if n.startswith("bigapp__")]) == 7 and "integration_call" in names
    reachable = {o.action for o in (*source.scope.exposed, *source.scope.dispatched)}
    assert len(reachable) == 40
