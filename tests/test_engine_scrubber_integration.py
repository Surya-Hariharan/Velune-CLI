"""Phase 4E-5A: one credential-aware scrubber across the whole acting path.

Every secret here is a made-up canary. Fake providers only; no network, no real credential store.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import pytest

from tests.council_core_fakes import CounterIds
from tests.test_engine_acting_reliability import CANARY
from tests.test_engine_acting_runner import PROMPTS, acting_spec, agent_input, control
from tests.test_engine_provider_integration import MODEL, ScriptedProvider, read_call, reply
from tests.test_engine_tool_executor import ApprovePort, Registry, SpyTool
from velune.core.errors.provider import InferenceError
from velune.orchestration.engine.toolexec import (
    MAX_OUTPUT_CHARS,
    SCRUB_FAILED_TEXT,
    bound_output,
)
from velune.orchestration.engine_adapters import secrets
from velune.orchestration.engine_adapters.acting_stack import build_acting_stack
from velune.orchestration.engine_adapters.secrets import (
    ScrubberUnavailable,
    SecretScrubber,
    credential_scrubber,
    default_scrubber,
    static_source,
)


def raising_source():
    def read():
        raise RuntimeError(f"store locked {CANARY}")

    return read


def stack_for(provider, tool=None, **kw):
    tool = tool or SpyTool(result=f"file says {CANARY}")
    stack = build_acting_stack(
        provider,
        MODEL,
        Registry(tool),
        kw.pop("workspace", None) or Path("."),
        PROMPTS,
        CounterIds(),
        approvals=ApprovePort(),
        **kw,
    )
    return stack, tool


async def run(stack):
    return await stack.runner.run_acting(acting_spec(), agent_input(), control=control())


@pytest.fixture
def store_has_canary(monkeypatch):
    """The credential store holds the canary; the provider environment holds nothing."""
    monkeypatch.setattr(secrets, "credential_store_source", lambda: static_source(CANARY))
    monkeypatch.setattr(secrets, "provider_environment_source", lambda: static_source())


# ── credential-manager and environment secrets ────────────────────────────────


def test_a_credential_store_secret_is_masked(store_has_canary):
    scrub = credential_scrubber()
    assert CANARY not in scrub(f"key={CANARY}")
    assert CANARY not in scrub(f'{{"k": "{CANARY}"}}')


def test_a_provider_environment_secret_is_masked(monkeypatch):
    monkeypatch.setattr(secrets, "credential_store_source", lambda: static_source())
    monkeypatch.setenv("GROQ_API_KEY", CANARY)
    assert CANARY not in credential_scrubber()(f"token {CANARY} end")


# ── fail safely ───────────────────────────────────────────────────────────────


def test_an_unreadable_store_refuses_initialization_without_leaking(monkeypatch):
    monkeypatch.setattr(secrets, "credential_store_source", raising_source)
    monkeypatch.setattr(secrets, "provider_environment_source", lambda: static_source())
    with pytest.raises(ScrubberUnavailable) as info:
        credential_scrubber()
    assert CANARY not in str(info.value) and CANARY not in repr(info.value)
    assert info.value.__cause__ is None  # the underlying error text is not chained


def test_building_the_stack_with_an_unavailable_scrubber_builds_nothing(monkeypatch):
    monkeypatch.setattr(secrets, "credential_store_source", raising_source)
    monkeypatch.setattr(secrets, "provider_environment_source", lambda: static_source())
    from velune.orchestration.engine_adapters import acting_stack

    monkeypatch.setattr(acting_stack, "credential_scrubber", secrets.credential_scrubber)
    provider = ScriptedProvider(reply("never", stop="stop"))
    with pytest.raises(ScrubberUnavailable):
        stack_for(provider)
    assert provider.requests == []


def test_the_default_non_strict_scrubber_still_skips_a_broken_source():
    scrubber = SecretScrubber(raising_source(), static_source(CANARY))
    assert CANARY not in scrubber(f"x {CANARY}")  # unchanged legacy behaviour
    assert default_scrubber() is not None


async def test_a_source_that_fails_later_withholds_output_and_refuses_the_request():
    state = {"broken": False}

    def flaky():
        if state["broken"]:
            raise RuntimeError("store locked")
        return [CANARY]

    provider = ScriptedProvider(reply("", [read_call("a.txt")]), reply("final", stop="stop"))
    stack, _ = stack_for(provider, scrub=SecretScrubber(flaky, strict=True))
    state["broken"] = True  # the store becomes unreadable after the stack was built
    outcome = await run(stack)
    assert provider.requests == []  # the provider-bound text could not be scrubbed: nothing sent
    assert CANARY not in repr(outcome)
    text, _t, _n = bound_output("data", SecretScrubber(flaky, strict=True))
    assert text == SCRUB_FAILED_TEXT


# ── the assembled path ────────────────────────────────────────────────────────


async def test_a_secret_in_tool_output_never_reaches_the_provider_or_the_outcome(store_has_canary):
    provider = ScriptedProvider(
        reply("", [read_call("a.txt")]), reply(f"summary {CANARY}", stop="stop")
    )
    stack, tool = stack_for(provider)
    outcome = await run(stack)
    assert tool.runs == 1 and len(provider.requests) == 2
    for request in provider.requests:
        assert CANARY not in str(request.messages)  # provider-bound, including the tool result
    assert CANARY not in repr(outcome)  # the model echoed it; the draft is scrubbed


async def test_the_same_scrubber_instance_reaches_all_three_components(store_has_canary):
    stack, _ = stack_for(ScriptedProvider())
    assert stack.invoker._scrub is stack.scrub
    assert stack.executor._scrub is stack.scrub
    assert stack.runner._scrub is stack.scrub


async def test_an_injected_scrubber_is_used_instead_of_building_one():
    mine = SecretScrubber(static_source(CANARY))
    stack, _ = stack_for(ScriptedProvider(), scrub=mine)
    assert stack.scrub is mine


async def test_provider_errors_expose_only_fixed_messages_and_log_no_secret(
    store_has_canary, caplog
):
    caplog.set_level(logging.DEBUG)
    provider = ScriptedProvider(InferenceError(f"upstream said {CANARY}"))
    stack, _ = stack_for(provider)
    outcome = await run(stack)
    assert CANARY not in repr(outcome) and CANARY not in caplog.text


def test_a_secret_cannot_survive_by_straddling_the_clip():
    scrub = SecretScrubber(static_source(CANARY))
    raw = "x" * (MAX_OUTPUT_CHARS - 10) + CANARY + "tail"
    text, truncated, _n = bound_output(raw, scrub)
    assert CANARY[:8] not in text and len(text) <= MAX_OUTPUT_CHARS and truncated


async def test_the_approval_display_is_scrubbed_with_the_same_scrubber(store_has_canary):
    from velune.orchestration.engine import (
        ProposedToolCall,
        ResolvedToolPolicy,
        descriptors_from_registry,
        validate_tool_call,
    )

    seen: list[Any] = []

    class Spy:
        async def request(self, invocation):
            seen.append(invocation)
            return None

    registry = Registry(SpyTool())
    stack = build_acting_stack(
        ScriptedProvider(), MODEL, registry, Path("."), PROMPTS, CounterIds(), approvals=Spy()
    )
    policy = ResolvedToolPolicy(allowed=frozenset({"read_file"}))
    call = validate_tool_call(
        ProposedToolCall(call_id="c1", name="read_file", arguments={"file_path": f"{CANARY}.txt"}),
        policy,
        descriptors_from_registry(registry),
    )
    await stack.executor.execute(call, policy, run_id="run-1")  # bypasses the adapter's own check
    assert len(seen) == 1 and seen[0].display and CANARY not in seen[0].display
