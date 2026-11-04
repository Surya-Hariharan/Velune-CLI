"""Phase 4D-1: tool proposals, tool policy resolution and trusted argument validation.

Everything here is pure. The fake tools raise if ``execute`` is ever called, and the tests that
matter assert it never was.
"""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

from tests.council_core_fakes import CounterIds
from tests.engine_fakes import FakeReasoningInvoker
from velune.core.types.agent import AgentRole
from velune.council.domain import ArtifactKind
from velune.council.profiles import RoutingRole
from velune.council.results import SeatStatus
from velune.orchestration.engine import (
    EXCLUDED_TOOLS,
    READ_ONLY_TOOL_CEILING,
    ActingInvoker,
    ActingRequest,
    ActingResult,
    AgentKind,
    AgentSpec,
    ArtifactOrigin,
    ArtifactStore,
    ProposedToolCall,
    ReasoningInvoker,
    ReasoningResult,
    ResolvedToolPolicy,
    ToolCallRejected,
    ToolPolicy,
    ToolPolicyError,
    ToolRejection,
    descriptors_from_registry,
    resolve_tool_policy,
    validate_proposals,
    validate_tool_call,
)
from velune.orchestration.engine.artifacts import StoredArtifact
from velune.orchestration.engine.toolschema import (
    MAX_ARGUMENT_BYTES,
    MAX_DEPTH,
    ArgumentError,
    canonical_arguments,
    schema_problem,
)
from velune.orchestration.state import RunPhase

ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / "velune" / "orchestration" / "engine"
NEW_MODULES = ("proposals.py", "toolpolicy.py", "toolschema.py", "toolvalidate.py")

SECRET = "sk-ant-SECRETSECRETSECRETSECRET"


# ── fakes: tools that describe themselves and refuse to run ───────────────────


class FakeTool:
    executed = 0

    def __init__(self, name, schema, perms=("filesystem.read",)):
        self._name, self._schema, self._perms = name, schema, set(perms)

    def get_name(self):
        return self._name

    def get_schema(self):
        return self._schema

    def get_required_permissions(self):
        return self._perms

    async def execute(self, **kwargs):  # pragma: no cover - must never run
        FakeTool.executed += 1
        raise AssertionError("a tool was executed")


class FakeRegistry:
    def __init__(self, *tools):
        self._tools = {t.get_name(): t for t in tools}

    def list_tools(self):
        return list(self._tools)

    def get(self, name):
        return self._tools.get(name)


READ_FILE = {
    "type": "object",
    "properties": {
        "file_path": {"type": "string"},
        "start_line": {"type": "integer", "minimum": 1},
        "end_line": {"type": "integer", "minimum": 1},
    },
    "required": ["file_path"],
}
LIMIT_ONLY = {"type": "object", "properties": {"limit": {"type": "integer"}}}


def registry():
    return FakeRegistry(
        FakeTool("read_file", READ_FILE),
        FakeTool("git_log", LIMIT_ONLY, ("git.read",)),
        FakeTool(
            "grep_files",
            {
                "type": "object",
                "properties": {"pattern": {"type": "string"}},
                "required": ["pattern"],
            },
        ),
        FakeTool("write_file", READ_FILE, ("filesystem.write",)),
        FakeTool(
            "execute_command",
            {"type": "object", "properties": {"command": {"type": "string"}}},
            ("terminal.execute",),
        ),
        FakeTool(
            "web_fetch",
            {"type": "object", "properties": {"url": {"type": "string"}}},
            ("network.access",),
        ),
    )


@pytest.fixture(autouse=True)
def _no_tool_ever_runs():
    FakeTool.executed = 0
    yield
    assert FakeTool.executed == 0, "a fake tool was executed"


@pytest.fixture
def catalog():
    return descriptors_from_registry(registry())


def acting(*tools, role=AgentRole.CODER) -> AgentSpec:
    return AgentSpec(
        role=role,
        kind=AgentKind.ACTING,
        routing_role=RoutingRole.CODER,
        prompt_ref="engine.act",
        tool_policy=ToolPolicy(allowed_tools=tuple(tools)),
    )


def reasoning() -> AgentSpec:
    return AgentSpec(
        role=AgentRole.REVIEWER, routing_role=RoutingRole.REVIEWER, prompt_ref="engine.review"
    )


def proposal(name="read_file", arguments=None, call_id="c1", **kw) -> ProposedToolCall:
    return ProposedToolCall(
        call_id=call_id,
        name=name,
        arguments={"file_path": "a.py"} if arguments is None else arguments,
        **kw,
    )


def policy_for(catalog, *tools) -> ResolvedToolPolicy:
    return resolve_tool_policy(acting(*tools), catalog)


# ── proposal type ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "smuggled",
    [
        {"approved": True},
        {"permissions": ["filesystem.write"]},
        {"agent": "supervisor"},
        {"role": "supervisor"},
        {"author": "planner"},
        {"origin": "tool"},
        {"result": "done"},
        {"executor": "shell"},
        {"kind": "decision"},
    ],
)
def test_a_proposal_cannot_carry_trusted_fields(smuggled):
    with pytest.raises(ValidationError):
        ProposedToolCall(call_id="c1", name="read_file", arguments={}, **smuggled)


def test_a_proposal_is_frozen():
    item = proposal()
    with pytest.raises(ValidationError):
        item.name = "write_file"  # type: ignore[misc]


@pytest.mark.parametrize("bad", ["", "1tool", "a b", "a;b", "../x", "x" * 65, "read\nfile"])
def test_tool_names_are_simple_identifiers(bad):
    with pytest.raises(ValidationError):
        ProposedToolCall(call_id="c1", name=bad, arguments={})


@pytest.mark.parametrize("bad", ["", " ", "a b", "x" * 65, "-a"])
def test_call_ids_are_simple_identifiers(bad):
    with pytest.raises(ValidationError):
        ProposedToolCall(call_id=bad, name="read_file", arguments={})


def test_the_rationale_is_bounded_and_optional():
    assert proposal().rationale == ""
    with pytest.raises(ValidationError):
        proposal(rationale="x" * 301)


# ── the acting port ───────────────────────────────────────────────────────────


class FakeActingInvoker:
    async def propose(self, request: ActingRequest) -> ActingResult:
        return ActingResult.success("thinking", (proposal(),))


def test_reasoning_results_cannot_carry_proposals():
    with pytest.raises(ValidationError):
        ReasoningResult(status=SeatStatus.OK, text="x", proposals=(proposal(),))  # type: ignore[call-arg]
    assert "proposals" not in ReasoningResult.model_fields


def test_the_two_invoker_protocols_are_structurally_distinct():
    assert isinstance(FakeActingInvoker(), ActingInvoker)
    assert not isinstance(FakeActingInvoker(), ReasoningInvoker)
    assert isinstance(FakeReasoningInvoker(), ReasoningInvoker)
    assert not isinstance(FakeReasoningInvoker(), ActingInvoker)


def test_an_acting_result_has_no_slot_for_trusted_data():
    assert set(ActingResult.model_fields) == {
        "status",
        "text",
        "proposals",
        "error",
        "model",
        "attempts",
    }
    with pytest.raises(ValidationError):
        ActingResult(status=SeatStatus.OK, text="x", approved=True)  # type: ignore[call-arg]


def test_acting_result_invariants():
    assert ActingResult.success(proposals=(proposal(),)).ok
    with pytest.raises(ValidationError):
        ActingResult(status=SeatStatus.OK)  # nothing at all
    with pytest.raises(ValidationError):
        ActingResult(status=SeatStatus.PROVIDER_ERROR, proposals=(proposal(),))
    with pytest.raises(ValidationError):
        ActingResult(status=SeatStatus.PROVIDER_ERROR, text="x")
    with pytest.raises(ValidationError):
        ActingResult.success(proposals=(proposal(call_id="a"), proposal(call_id="a")))
    with pytest.raises(ValidationError):
        ActingResult.success(proposals=tuple(proposal(call_id=f"c{i}") for i in range(17)))
    failed = ActingResult.failure(SeatStatus.TIMEOUT, "slow")
    assert not failed.ok and failed.proposals == () and failed.text is None


def test_an_acting_request_offers_names_only():
    request = ActingRequest(
        run_id="run-1",
        call_id="call-1",
        agent=AgentRole.CODER,
        routing_role=RoutingRole.CODER,
        attempt=1,
        messages=({"role": "user", "content": "go"},),  # type: ignore[arg-type]
        timeout_s=5,
        offered_tools=("read_file",),
    )
    assert request.offered_tools == ("read_file",)
    with pytest.raises(ValidationError):
        ActingRequest(**{**request.model_dump(), "offered_tools": ("bad name",)})


# ── tool policy resolution ────────────────────────────────────────────────────


def test_a_reasoning_agent_resolves_to_no_tools(catalog):
    policy = resolve_tool_policy(reasoning(), catalog)
    assert policy.allowed == frozenset()
    assert not policy.permits("read_file")


def test_reasoning_specs_still_cannot_be_given_tools():
    with pytest.raises(ValidationError):
        AgentSpec(
            role=AgentRole.REVIEWER,
            routing_role=RoutingRole.REVIEWER,
            prompt_ref="engine.review",
            tool_policy=ToolPolicy(allowed_tools=("read_file",)),
        )


def test_the_allowlist_is_intersected_with_catalog_and_ceiling(catalog):
    policy = policy_for(catalog, "read_file", "git_log")
    assert policy.allowed == frozenset({"read_file", "git_log"})
    # grep_files is registered and in the ceiling but the agent did not ask for it
    assert not policy.permits("grep_files")


def test_an_empty_allowlist_resolves_to_nothing_but_an_acting_agent_needs_a_tool():
    with pytest.raises(ValidationError):
        acting()  # AgentSpec itself requires at least one tool for an acting agent


def test_unknown_tools_are_refused(catalog):
    with pytest.raises(ToolPolicyError, match="not a registered tool"):
        policy_for(catalog, "read_file", "no_such_tool")


@pytest.mark.parametrize("tool", ["execute_command", "web_fetch", "write_file"])
def test_excluded_tools_are_refused_even_when_registered(catalog, tool):
    assert tool in catalog and tool in EXCLUDED_TOOLS
    with pytest.raises(ToolPolicyError, match="excluded"):
        policy_for(catalog, "read_file", tool)


def test_the_phase_exclusions_cover_the_approved_list():
    assert {
        "execute_command",
        "web_fetch",
        "write_file",
        "create_file",
        "delete_file",
        "move_path",
        "git_commit",
        "git_checkout",
    } <= EXCLUDED_TOOLS
    assert not EXCLUDED_TOOLS & READ_ONLY_TOOL_CEILING


def test_a_registered_tool_outside_the_driver_ceiling_is_refused():
    cat = descriptors_from_registry(
        FakeRegistry(FakeTool("read_file", READ_FILE), FakeTool("custom_reader", READ_FILE))
    )
    with pytest.raises(ToolPolicyError, match="ceiling"):
        policy_for(cat, "custom_reader")


def test_a_driver_can_narrow_the_ceiling(catalog):
    spec = acting("read_file", "grep_files")
    assert resolve_tool_policy(spec, catalog, ceiling={"read_file", "grep_files"}).allowed
    with pytest.raises(ToolPolicyError, match="ceiling"):
        resolve_tool_policy(spec, catalog, ceiling={"read_file"})


def test_a_ceiling_name_does_not_protect_a_tool_that_became_powerful():
    """If the registry swaps ``read_file`` for something that writes, the scope ceiling still refuses it."""
    cat = descriptors_from_registry(
        FakeRegistry(FakeTool("read_file", READ_FILE, ("filesystem.read", "filesystem.write")))
    )
    with pytest.raises(ToolPolicyError, match="beyond the ceiling"):
        policy_for(cat, "read_file")


def test_a_tool_with_no_declared_scope_is_refused():
    cat = descriptors_from_registry(FakeRegistry(FakeTool("read_file", READ_FILE, ())))
    with pytest.raises(ToolPolicyError, match="no permission scope"):
        policy_for(cat, "read_file")


def test_a_tool_whose_schema_cannot_be_enforced_is_refused():
    odd = {"type": "object", "properties": {"x": {"oneOf": [{"type": "string"}]}}}
    cat = descriptors_from_registry(FakeRegistry(FakeTool("read_file", odd)))
    with pytest.raises(ToolPolicyError, match="cannot be enforced"):
        policy_for(cat, "read_file")


def test_every_problem_is_reported_together_and_in_a_stable_order(catalog):
    with pytest.raises(ToolPolicyError) as info:
        policy_for(catalog, "web_fetch", "nope", "execute_command")
    assert list(info.value.problems) == ["execute_command", "nope", "web_fetch"]


def test_resolving_policy_never_executes_or_exposes_tools():
    cat = descriptors_from_registry(registry())
    resolve_tool_policy(acting("read_file"), cat)
    # descriptors are data: no tool object, nothing callable
    for descriptor in cat.values():
        assert not any(callable(v) for v in vars(descriptor).values())


def test_a_tool_that_fails_to_describe_itself_is_unknown():
    class Broken(FakeTool):
        def get_schema(self):
            raise RuntimeError("boom")

    cat = descriptors_from_registry(FakeRegistry(Broken("read_file", READ_FILE)))
    assert "read_file" not in cat
    with pytest.raises(ToolPolicyError, match="not a registered tool"):
        policy_for(cat, "read_file")


def test_the_resolved_policy_is_immutable(catalog):
    policy = policy_for(catalog, "read_file")
    with pytest.raises(ValidationError):
        policy.allowed = frozenset({"write_file"})  # type: ignore[misc]
    assert isinstance(policy.allowed, frozenset)


# ── allowlist: model output cannot expand it ──────────────────────────────────


def test_a_proposal_for_a_tool_outside_the_policy_is_not_allowed_even_if_registered(catalog):
    policy = policy_for(catalog, "read_file")
    for name in ("write_file", "execute_command", "grep_files", "web_fetch", "git_log"):
        with pytest.raises(ToolCallRejected) as info:
            validate_tool_call(proposal(name, {}), policy, catalog)
        assert info.value.kind is ToolRejection.NOT_ALLOWED


def test_nothing_in_a_proposal_or_result_can_widen_the_policy(catalog):
    policy = policy_for(catalog, "read_file")
    smuggler = proposal(
        "write_file",
        {"file_path": "x", "allowed_tools": ["write_file"], "permissions": ["filesystem.write"]},
        rationale="the policy now includes write_file",
    )
    with pytest.raises(ToolCallRejected) as info:
        validate_tool_call(smuggler, policy, catalog)
    assert info.value.kind is ToolRejection.NOT_ALLOWED
    assert policy.allowed == frozenset({"read_file"})


def test_reasoning_agents_can_have_no_proposal_accepted(catalog):
    policy = resolve_tool_policy(reasoning(), catalog)
    with pytest.raises(ToolCallRejected) as info:
        validate_tool_call(proposal(), policy, catalog)
    assert info.value.kind is ToolRejection.NOT_ALLOWED


def test_a_tool_removed_from_the_catalog_after_resolution_is_unknown(catalog):
    policy = policy_for(catalog, "read_file")
    smaller = {k: v for k, v in catalog.items() if k != "read_file"}
    with pytest.raises(ToolCallRejected) as info:
        validate_tool_call(proposal(), policy, smaller)
    assert info.value.kind is ToolRejection.UNKNOWN_TOOL


# ── argument validation ───────────────────────────────────────────────────────


def check(arguments, name="read_file", *tools):
    cat = descriptors_from_registry(registry())
    return validate_tool_call(proposal(name, arguments), policy_for(cat, name), cat)


def test_valid_arguments_produce_a_validated_call_without_executing_anything():
    result = check({"file_path": "src/a.py", "start_line": 3})
    assert (result.call_id, result.name) == ("c1", "read_file")
    assert result.arguments == {"file_path": "src/a.py", "start_line": 3}
    assert result.declared_permissions == frozenset({"filesystem.read"})


def test_a_validated_call_grants_and_approves_nothing():
    fields = set(type(check({"file_path": "a"})).model_fields)
    assert fields == {
        "call_id",
        "name",
        "arguments_json",
        "arguments_digest",
        "call_digest",
        "declared_permissions",
    }
    for word in ("approv", "grant", "allow", "authoriz", "permit", "result", "status"):
        assert not any(word in field for field in fields), word


def test_decoded_arguments_are_a_fresh_copy_each_time():
    result = check({"file_path": "a.py"})
    result.arguments["file_path"] = "/etc/passwd"
    assert result.arguments == {"file_path": "a.py"}


@pytest.mark.parametrize(
    "arguments",
    [
        {},  # missing required
        {"file_path": "a.py", "mode": "w"},  # unknown argument
        {"file_path": 3},  # wrong type
        {"file_path": None},  # null never accepted
        {"file_path": "a.py", "start_line": "3"},
        {"file_path": "a.py", "start_line": 3.0},  # not an integer
        {"file_path": "a.py", "start_line": True},  # bool is not a number
        {"file_path": "a.py", "start_line": 0},  # below minimum
        {"file_path": "a.py", "start_line": [1]},
        {"file_path": ["a.py"]},
        {"file_path": {"nested": "a.py"}},
    ],
)
def test_malformed_arguments_fail_closed(arguments):
    with pytest.raises(ToolCallRejected) as info:
        check(arguments)
    assert info.value.kind is ToolRejection.BAD_ARGUMENTS


def test_rejection_messages_never_echo_argument_values():
    for arguments in (
        {"file_path": 3, "extra": SECRET},
        {"file_path": SECRET, "start_line": SECRET},
        {SECRET.replace("-", "_"): 1, "file_path": "a"},
    ):
        with pytest.raises(ToolCallRejected) as info:
            check(arguments)
        assert SECRET not in str(info.value) and SECRET not in info.value.message


def test_unknown_argument_names_are_rejected_even_for_a_tool_that_takes_none():
    cat = descriptors_from_registry(FakeRegistry(FakeTool("git_status", {}, ("git.read",))))
    policy = policy_for(cat, "git_status")
    assert validate_tool_call(proposal("git_status", {}), policy, cat).arguments == {}
    with pytest.raises(ToolCallRejected):
        validate_tool_call(proposal("git_status", {"directory": "."}), policy, cat)


class Opaque:
    pass


@pytest.mark.parametrize(
    "value",
    [
        {1, 2},
        b"bytes",
        bytearray(b"x"),
        (1, 2),
        Path("a.py"),
        Opaque(),
        float("nan"),
        float("inf"),
        -float("inf"),
        2**60,
        complex(1, 2),
        lambda: 1,
        frozenset({1}),
    ],
)
def test_non_json_values_are_rejected(value):
    with pytest.raises(ToolCallRejected) as info:
        check({"file_path": value})
    assert info.value.kind in {ToolRejection.NOT_JSON, ToolRejection.BAD_ARGUMENTS}
    with pytest.raises(ArgumentError):
        canonical_arguments({"x": value})


def test_str_and_dict_subclasses_are_rejected():
    class Sneaky(str):
        pass

    class SneakyDict(dict):
        pass

    with pytest.raises(ArgumentError) as a:
        canonical_arguments({"x": Sneaky("a")})
    assert a.value.kind == "not_json"
    with pytest.raises(ArgumentError):
        canonical_arguments({"x": SneakyDict(y=1)})


@pytest.mark.parametrize("key", [1, None, ("a",), "has space", "1abc", "", "x" * 65, "a-b", "../x"])
def test_argument_names_must_be_simple_string_identifiers(key):
    with pytest.raises(ArgumentError):
        canonical_arguments({key: 1})


@pytest.mark.parametrize("text", ["a\x00b", "\ud800", "x\udfffy"])
def test_nul_and_invalid_unicode_are_rejected(text):
    with pytest.raises(ToolCallRejected) as info:
        check({"file_path": text})
    assert info.value.kind is ToolRejection.NOT_JSON


def test_oversized_values_are_rejected_deterministically():
    cases = [
        {"file_path": "x" * 4097},  # one long string
        {f"k{i}": 1 for i in range(33)},  # too many keys
        {"file_path": ["a"] * 65},  # too many items
        {f"k{i}": "x" * 4000 for i in range(6)},  # many strings, total bytes over the cap
    ]
    for arguments in cases:
        with pytest.raises(ToolCallRejected) as info:
            check(arguments)
        assert info.value.kind is ToolRejection.TOO_LARGE
    assert 6 * 4000 > MAX_ARGUMENT_BYTES


def test_a_string_exactly_at_the_limit_is_accepted_by_the_size_check():
    assert canonical_arguments({"x": "y" * 4096})


def test_too_many_values_overall_are_rejected():
    many = {f"k{i}": list(range(30)) for i in range(20)}
    with pytest.raises(ArgumentError) as info:
        canonical_arguments(many)
    assert info.value.kind == "too_large"


def test_deep_nesting_is_rejected():
    deep: object = "leaf"
    for _ in range(MAX_DEPTH + 1):
        deep = [deep]
    with pytest.raises(ToolCallRejected) as info:
        check({"file_path": deep})
    assert info.value.kind is ToolRejection.TOO_DEEP
    ok: object = "leaf"
    for _ in range(MAX_DEPTH - 2):
        ok = [ok]
    assert canonical_arguments({"x": ok})


def test_pathologically_deep_and_cyclic_structures_cannot_exhaust_the_stack():
    deep: object = 1
    for _ in range(50_000):
        deep = [deep]
    with pytest.raises(ToolCallRejected) as info:
        check({"file_path": deep})
    assert info.value.kind is ToolRejection.TOO_DEEP

    cyclic: dict = {}
    cyclic["self"] = [cyclic]
    with pytest.raises(ToolCallRejected) as info:
        check(cyclic)
    assert info.value.kind is ToolRejection.TOO_DEEP


# ── canonical digests ─────────────────────────────────────────────────────────


def test_the_digest_is_stable_across_key_order_and_equivalent_objects():
    a = check({"file_path": "a.py", "start_line": 1, "end_line": 9})
    b = check({"end_line": 9, "file_path": "a.py", "start_line": 1})
    assert (
        a.arguments_json == b.arguments_json == '{"end_line":9,"file_path":"a.py","start_line":1}'
    )
    assert a.arguments_digest == b.arguments_digest
    assert a.call_digest == b.call_digest
    assert len(a.call_digest) == 64


def test_the_digest_changes_when_arguments_change():
    base = check({"file_path": "a.py"})
    for other in (
        {"file_path": "b.py"},
        {"file_path": "a.py", "start_line": 1},
        {"file_path": "A.py"},
    ):
        changed = check(other)
        assert changed.arguments_digest != base.arguments_digest
        assert changed.call_digest != base.call_digest


def test_the_call_digest_also_binds_the_tool_name():
    cat = descriptors_from_registry(
        FakeRegistry(FakeTool("read_file", READ_FILE), FakeTool("grep_files", READ_FILE))
    )
    policy = policy_for(cat, "read_file", "grep_files")
    one = validate_tool_call(proposal("read_file", {"file_path": "a"}), policy, cat)
    two = validate_tool_call(proposal("grep_files", {"file_path": "a"}), policy, cat)
    assert one.arguments_digest == two.arguments_digest
    assert one.call_digest != two.call_digest


def test_one_and_one_point_zero_have_different_digests():
    number = {"type": "object", "properties": {"n": {"type": "number"}}}
    cat = descriptors_from_registry(FakeRegistry(FakeTool("read_file", number)))
    policy = policy_for(cat, "read_file")
    as_int = validate_tool_call(proposal("read_file", {"n": 1}), policy, cat)
    as_float = validate_tool_call(proposal("read_file", {"n": 1.0}), policy, cat)
    assert as_int.arguments_digest != as_float.arguments_digest


def test_unicode_is_not_normalised_away():
    assert check({"file_path": "é"}).arguments_digest != check({"file_path": "é"}).arguments_digest


def test_digests_do_not_depend_on_the_interpreter_hash_seed():
    code = (
        "from velune.orchestration.engine.toolschema import canonical_arguments\n"
        "from velune.orchestration.engine.toolvalidate import argument_digest\n"
        "print(argument_digest(canonical_arguments({'b': [3, 2, 1], 'a': {'z': 1, 'y': 2}})))\n"
    )
    outputs = set()
    for seed in ("0", "1", "12345"):
        result = subprocess.run(
            [sys.executable, "-W", "error", "-c", code],
            capture_output=True,
            text=True,
            cwd=ROOT,
            env={**__import__("os").environ, "PYTHONHASHSEED": seed},
            timeout=60,
        )
        assert result.returncode == 0, result.stderr
        outputs.add(result.stdout.strip())
    assert len(outputs) == 1


# ── whole responses ───────────────────────────────────────────────────────────


def test_a_response_is_validated_atomically(catalog):
    policy = policy_for(catalog, "read_file", "git_log")
    good = proposal(call_id="a")
    bad = proposal("git_log", {"limit": "ten"}, call_id="b")
    with pytest.raises(ToolCallRejected) as info:
        validate_proposals([good, bad], policy, catalog)
    assert info.value.call_id == "b"
    assert [v.call_id for v in validate_proposals([good], policy, catalog)] == ["a"]


def test_duplicate_call_ids_and_oversized_batches_are_rejected(catalog):
    policy = policy_for(catalog, "read_file")
    with pytest.raises(ToolCallRejected) as info:
        validate_proposals([proposal(call_id="a"), proposal(call_id="a")], policy, catalog)
    assert info.value.kind is ToolRejection.DUPLICATE_CALL_ID
    many = [proposal(call_id=f"c{i}") for i in range(17)]
    with pytest.raises(ToolCallRejected) as info:
        validate_proposals(many, policy, catalog)
    assert info.value.kind is ToolRejection.TOO_MANY_CALLS
    assert validate_proposals([], policy, catalog) == ()


# ── the schema subset ─────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "schema",
    [
        {"type": "object", "properties": {"x": {"oneOf": []}}},
        {"type": "object", "properties": {"x": {"type": "object"}}},
        {"type": "object", "properties": {"x": {"type": "array"}}},
        {"type": "object", "properties": {"x": {"type": "string", "pattern": ".*"}}},
        {"type": "object", "properties": {"x": {"type": "string"}}, "required": ["y"]},
        {"type": "object", "additionalProperties": True},
        {"type": "string"},
        {"anyOf": []},
        {"type": "object", "properties": {"bad name": {"type": "string"}}},
        {"type": "object", "properties": {"x": {"type": "null"}}},
        "not a schema",
    ],
)
def test_schemas_this_validator_cannot_enforce_are_reported(schema):
    assert schema_problem(schema)


def test_the_empty_schema_means_no_parameters_and_flat_schemas_are_supported():
    assert schema_problem({}) is None
    assert schema_problem(READ_FILE) is None
    assert (
        schema_problem(
            {
                "type": "object",
                "properties": {
                    "mode": {"type": "string", "enum": ["a", "b"], "minLength": 1, "maxLength": 3},
                    "tags": {"type": "array", "items": {"type": "string"}, "maxItems": 2},
                    "flag": {"type": "boolean"},
                    "ratio": {"type": "number", "minimum": 0, "maximum": 1},
                },
            }
        )
        is None
    )


def test_enum_array_and_range_rules_are_enforced():
    schema = {
        "type": "object",
        "properties": {
            "mode": {"type": "string", "enum": ["a", "b"]},
            "tags": {"type": "array", "items": {"type": "string"}, "maxItems": 2},
            "ratio": {"type": "number", "minimum": 0, "maximum": 1},
            "name": {"type": "string", "minLength": 2, "maxLength": 3},
        },
    }
    cat = descriptors_from_registry(FakeRegistry(FakeTool("read_file", schema)))
    policy = policy_for(cat, "read_file")

    def run(arguments):
        return validate_tool_call(proposal("read_file", arguments), policy, cat)

    assert run({"mode": "a", "tags": ["x"], "ratio": 0.5, "name": "ab"})
    for bad in (
        {"mode": "c"},
        {"tags": ["x", "y", "z"]},
        {"tags": [1]},
        {"ratio": 2},
        {"ratio": True},
        {"name": "a"},
        {"name": "abcd"},
    ):
        with pytest.raises(ToolCallRejected) as info:
            run(bad)
        assert info.value.kind is ToolRejection.BAD_ARGUMENTS


# ── the real read-only tools' own schemas (metadata only; nothing is run) ──────


def test_the_real_read_only_tools_are_enforceable_and_inside_the_ceiling(monkeypatch, tmp_path):
    from velune.tools import (
        FindFiles,
        GitBlame,
        GitBranch,
        GitDiff,
        GitLog,
        GitStatus,
        GrepFiles,
        ReadDirectory,
        ReadFile,
    )
    from velune.tools.base.tool import BaseTool

    async def boom(self, **kwargs):  # pragma: no cover - must never run
        raise AssertionError("a real tool was executed")

    monkeypatch.setattr(BaseTool, "execute", boom, raising=True)
    classes = (
        ReadFile, ReadDirectory, GrepFiles, FindFiles, GitLog, GitDiff, GitBlame, GitStatus, GitBranch,
    )  # fmt: skip
    tools = [cls(workspace=tmp_path) for cls in classes]
    for tool in tools:
        monkeypatch.setattr(type(tool), "execute", boom, raising=True)
    cat = descriptors_from_registry(FakeRegistry(*tools))
    assert set(cat) == READ_ONLY_TOOL_CEILING
    spec = acting(*sorted(READ_ONLY_TOOL_CEILING))
    policy = resolve_tool_policy(spec, cat)
    assert policy.allowed == READ_ONLY_TOOL_CEILING

    ok = validate_tool_call(
        proposal("read_file", {"file_path": "a.py", "start_line": 1, "end_line": 5}), policy, cat
    )
    assert ok.declared_permissions == frozenset({"filesystem.read"})
    assert validate_tool_call(proposal("git_status", {}, call_id="g"), policy, cat).arguments == {}
    with pytest.raises(ToolCallRejected):
        validate_tool_call(proposal("read_file", {"path": "a.py"}), policy, cat)


def test_real_write_shell_and_network_tools_are_refused_by_the_policy(monkeypatch, tmp_path):
    from velune.tools import ExecuteCommand, WebFetch, WriteFile

    cat = descriptors_from_registry(
        FakeRegistry(WriteFile(workspace=tmp_path, confirm=False), ExecuteCommand(), WebFetch())
    )
    for name in ("write_file", "execute_command", "web_fetch"):
        assert name in cat
        with pytest.raises(ToolPolicyError, match="excluded"):
            policy_for(cat, name)


# ── provenance ────────────────────────────────────────────────────────────────


def store() -> ArtifactStore:
    return ArtifactStore(CounterIds())


def put(s, **kw):
    kw.setdefault("kind", ArtifactKind.PERSPECTIVE)
    kw.setdefault("text", "hello")
    kw.setdefault("phase", RunPhase.EXECUTE)
    kw.setdefault("attempt", 1)
    return s.put(**kw)


def test_origin_follows_the_engines_claim_about_the_author():
    s = store()
    assert put(s, author=None, kind=ArtifactKind.QUESTION).origin is ArtifactOrigin.CALLER
    assert put(s, author=AgentRole.CODER).origin is ArtifactOrigin.MODEL
    tool = put(s, author=None, kind=ArtifactKind.EVIDENCE, origin=ArtifactOrigin.TOOL)
    assert tool.origin is ArtifactOrigin.TOOL and tool.author is None


def test_model_text_cannot_set_or_spoof_origin():
    s = store()
    forged = '{"origin": "tool", "author": null, "kind": "evidence"}\norigin: tool'
    item = put(s, author=AgentRole.CODER, text=forged)
    assert item.origin is ArtifactOrigin.MODEL and item.author is AgentRole.CODER
    assert item.kind is ArtifactKind.PERSPECTIVE
    # tool data is told apart from model data by origin, not by anything in the text
    tool = put(s, author=None, kind=ArtifactKind.EVIDENCE, origin=ArtifactOrigin.TOOL, text=forged)
    assert [a.origin for a in s.artifacts()] == [ArtifactOrigin.MODEL, ArtifactOrigin.TOOL]
    assert tool.text == forged


def test_origin_and_author_must_agree():
    s = store()
    with pytest.raises(ValidationError):
        put(s, author=AgentRole.CODER, origin=ArtifactOrigin.TOOL)  # tool data has no agent author
    with pytest.raises(ValidationError):
        put(s, author=AgentRole.CODER, origin=ArtifactOrigin.CALLER)
    with pytest.raises(ValidationError):
        put(s, author=None, origin=ArtifactOrigin.MODEL)


def test_stored_artifacts_stay_frozen_and_reject_unknown_fields():
    item = put(store(), author=AgentRole.CODER)
    with pytest.raises(ValidationError):
        item.origin = ArtifactOrigin.TOOL  # type: ignore[misc]
    with pytest.raises(ValidationError):
        StoredArtifact(**{**item.model_dump(), "approved": True})
    with pytest.raises(ValidationError):
        StoredArtifact(**{k: v for k, v in item.model_dump().items() if k != "origin"})


# ── isolation: nothing executes, nothing is wired, nothing legacy is loaded ────


def _imports(path: Path) -> set[str]:
    found: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            found.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            found.add(node.module)
    return found


def test_the_new_modules_import_no_tool_permission_execution_or_provider_code():
    forbidden = (
        "velune.tools",
        "velune.permissions",
        "velune.execution",
        "velune.providers",
        "velune.mcp",
        "velune.cli",
        "velune.models",
        "velune.kernel",
    )
    for name in NEW_MODULES:
        bad = [m for m in _imports(ENGINE / name) if m.startswith(forbidden)]
        assert bad == [], f"{name} imports {bad}"


def test_the_new_modules_never_call_the_permission_or_execution_machinery():
    banned = {
        "authorize_and_execute",
        "guarded_execute",
        "PermissionGate",
        "Boundary",
        "subprocess",
    }
    for name in NEW_MODULES:
        names = {
            node.attr if isinstance(node, ast.Attribute) else node.id
            for node in ast.walk(ast.parse((ENGINE / name).read_text(encoding="utf-8")))
            if isinstance(node, (ast.Attribute, ast.Name))
        }
        assert not names & banned, f"{name}: {names & banned}"
        calls = {
            n.func.attr
            for n in ast.walk(ast.parse((ENGINE / name).read_text(encoding="utf-8")))
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
        }
        assert "execute" not in calls, name


def test_importing_the_engine_still_loads_no_tools_permissions_providers_or_cli():
    code = (
        "import sys\n"
        "import velune.orchestration.engine\n"
        "bad = ('velune.tools','velune.permissions','velune.execution','velune.providers',"
        "'velune.cli','velune.mcp','velune.models','velune.cognition.orchestrator',"
        "'velune.cognition.council','velune.runtime')\n"
        "print('LOADED', sorted(m for m in sys.modules if m.startswith(bad)))\n"
    )
    result = subprocess.run(
        [sys.executable, "-W", "error", "-c", code],
        capture_output=True,
        text=True,
        cwd=ROOT,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "LOADED []", result.stdout


def test_the_engine_flag_is_still_off_and_unread_by_the_new_modules(monkeypatch):
    from velune.kernel.config import VeluneConfig
    from velune.orchestration.engine import engine_enabled

    monkeypatch.delenv("VELUNE_ORCHESTRATION__ENGINE", raising=False)
    assert not engine_enabled(VeluneConfig())
    for name in NEW_MODULES:
        source = (ENGINE / name).read_text(encoding="utf-8")
        assert "engine_enabled" not in source and "ENGINE_ON" not in source


def test_the_driver_and_reasoning_path_are_unchanged_by_the_tool_types():
    from velune.orchestration.engine import PlanReviewDriver, ReasoningAgentRunner

    assert "propose" not in dir(ReasoningAgentRunner) and "propose" not in dir(PlanReviewDriver)
    assert "proposals" not in ReasoningResult.model_fields


def test_a_reasoning_spec_that_somehow_holds_tools_still_resolves_to_none(catalog):
    """Defence in depth: ``model_construct`` skips the spec's own validation, the resolver must not."""
    sneaky = AgentSpec.model_construct(
        role=AgentRole.REVIEWER,
        kind=AgentKind.REASONING,
        routing_role=RoutingRole.REVIEWER,
        prompt_ref="engine.review",
        read_scope=(),
        tool_policy=ToolPolicy(allowed_tools=("read_file", "write_file")),
        budget=reasoning().budget,
    )
    assert sneaky.tool_policy.grants_tools
    assert resolve_tool_policy(sneaky, catalog).allowed == frozenset()
