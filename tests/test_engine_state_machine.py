"""RunState, the transition table, the retry cap, terminal states, cancellation and serialization."""

from __future__ import annotations

import itertools

import pytest
from pydantic import ValidationError

from velune.core.types.agent import AgentRole
from velune.council.domain import ArtifactKind
from velune.council.serialization import digest
from velune.orchestration.engine import (
    TRANSITIONS,
    IllegalTransitionError,
    InvalidArtifactError,
    RetryLimitExceeded,
    TerminalStateError,
    advance,
    allowed_from,
    can_transition,
    cancel,
    fail,
    record_artifact,
)
from velune.orchestration.state import (
    TERMINAL_PHASES,
    RunArtifact,
    RunFailure,
    RunPhase,
    RunState,
    RunStatus,
)

P = RunPhase
WORKING = [P.PLAN, P.EXECUTE, P.OBSERVE, P.REFLECT, P.RETRY, P.VALIDATE]


def at(phase: RunPhase, *, max_attempts: int = 3) -> RunState:
    """A state parked at ``phase``, built by walking the legal path (never by forging fields)."""
    state = RunState.start("run-1", max_attempts=max_attempts)
    path = {
        P.PLAN: [],
        P.EXECUTE: [P.EXECUTE],
        P.OBSERVE: [P.EXECUTE, P.OBSERVE],
        P.REFLECT: [P.EXECUTE, P.OBSERVE, P.REFLECT],
        P.VALIDATE: [P.EXECUTE, P.OBSERVE, P.REFLECT, P.VALIDATE],
        P.FINISH: [P.EXECUTE, P.OBSERVE, P.REFLECT, P.VALIDATE, P.FINISH],
    }
    if phase is P.RETRY:
        path[P.RETRY] = [P.EXECUTE, P.OBSERVE, P.REFLECT, P.RETRY]
    for step in path[phase]:
        state = advance(state, step)
    return state


def artifact(state: RunState, name: str = "a1", kind=ArtifactKind.ARTIFACT) -> RunArtifact:
    return RunArtifact(
        artifact_id=name,
        kind=kind,
        author=AgentRole.PLANNER,
        phase=state.phase,
        attempt=state.attempt,
        digest=digest({"name": name}),
    )


# ── the happy path ─────────────────────────────────────────────────────────


def test_a_run_starts_in_plan_running_on_attempt_one():
    state = RunState.start("run-1")
    assert (state.phase, state.status, state.attempt, state.trail) == (
        P.PLAN,
        RunStatus.RUNNING,
        1,
        (P.PLAN,),
    )
    assert not state.is_terminal and state.failure is None and state.artifacts == ()


def test_the_straight_path_reaches_finish_succeeded():
    state = RunState.start("run-1")
    for step in (P.EXECUTE, P.OBSERVE, P.REFLECT, P.VALIDATE, P.FINISH):
        state = advance(state, step)
    assert state.phase is P.FINISH and state.status is RunStatus.SUCCEEDED and state.is_terminal
    assert state.trail == (P.PLAN, P.EXECUTE, P.OBSERVE, P.REFLECT, P.VALIDATE, P.FINISH)


def test_transitions_are_values_the_input_state_is_never_mutated():
    before = RunState.start("run-1")
    after = advance(before, P.EXECUTE)
    assert before.phase is P.PLAN and after.phase is P.EXECUTE and before is not after


# ── the table: every pair, legal or not ────────────────────────────────────

LEGAL = {
    (P.PLAN, P.EXECUTE),
    (P.EXECUTE, P.OBSERVE),
    (P.OBSERVE, P.REFLECT),
    (P.REFLECT, P.RETRY),
    (P.REFLECT, P.VALIDATE),
    (P.RETRY, P.EXECUTE),
    (P.VALIDATE, P.FINISH),
} | {(p, t) for p in WORKING for t in (P.FAILED, P.CANCELLED)}


@pytest.mark.parametrize("source, target", list(itertools.product(P, P)))
def test_the_transition_table_allows_exactly_the_legal_pairs(source, target):
    assert can_transition(source, target) is ((source, target) in LEGAL)


def test_terminal_phases_have_no_exits_and_working_phases_can_abort():
    assert TERMINAL_PHASES == {P.FINISH, P.FAILED, P.CANCELLED}
    for phase in TERMINAL_PHASES:
        assert allowed_from(phase) == ()
    for phase in WORKING:
        assert P.FAILED in TRANSITIONS[phase] and P.CANCELLED in TRANSITIONS[phase]


def test_the_table_is_read_only():
    with pytest.raises(TypeError):
        TRANSITIONS[P.PLAN] = frozenset()  # type: ignore[index]


@pytest.mark.parametrize(
    "phase, target",
    [
        (P.PLAN, P.OBSERVE),
        (P.PLAN, P.FINISH),
        (P.EXECUTE, P.VALIDATE),
        (P.OBSERVE, P.EXECUTE),
        (P.REFLECT, P.FINISH),
        (P.RETRY, P.VALIDATE),
        (P.VALIDATE, P.EXECUTE),
        (P.VALIDATE, P.RETRY),
    ],
)
def test_an_illegal_advance_raises_a_typed_error_that_names_the_options(phase, target):
    state = at(phase)
    with pytest.raises(IllegalTransitionError) as caught:
        advance(state, target)
    assert not isinstance(caught.value, TerminalStateError)
    assert caught.value.source == phase.value and caught.value.target == target.value
    assert set(caught.value.allowed) == {t.value for t in TRANSITIONS[phase]}
    assert state.phase is phase  # nothing changed


def test_advance_refuses_to_abort_use_fail_or_cancel():
    with pytest.raises(ValueError, match="fail\\(\\) or cancel\\(\\)"):
        advance(RunState.start("run-1"), P.FAILED)
    with pytest.raises(ValueError, match="fail\\(\\) or cancel\\(\\)"):
        advance(RunState.start("run-1"), P.CANCELLED)


# ── retries ────────────────────────────────────────────────────────────────


def test_retry_spends_an_attempt_and_goes_back_to_execute():
    state = at(P.REFLECT)
    state = advance(state, P.RETRY)
    assert (state.phase, state.attempt) == (P.RETRY, 2)
    state = advance(state, P.EXECUTE)
    assert (state.phase, state.attempt) == (P.EXECUTE, 2)


def test_retries_are_bounded_by_max_attempts():
    state = RunState.start("run-1", max_attempts=3)
    for expected_attempt in (2, 3):
        for step in (P.EXECUTE, P.OBSERVE, P.REFLECT, P.RETRY):
            state = advance(state, step)
        assert state.attempt == expected_attempt
    for step in (P.EXECUTE, P.OBSERVE, P.REFLECT):
        state = advance(state, step)
    with pytest.raises(RetryLimitExceeded) as caught:
        advance(state, P.RETRY)
    assert (caught.value.attempt, caught.value.max_attempts) == (3, 3)
    assert state.attempt == 3 and state.phase is P.REFLECT
    # The run can still end honestly after the cap.
    assert advance(state, P.VALIDATE).phase is P.VALIDATE


def test_max_attempts_one_allows_no_retry_at_all():
    state = at(P.REFLECT, max_attempts=1)
    with pytest.raises(RetryLimitExceeded):
        advance(state, P.RETRY)


@pytest.mark.parametrize("bad", [0, -1, 11, 1000])
def test_max_attempts_is_validated(bad):
    with pytest.raises(ValidationError):
        RunState.start("run-1", max_attempts=bad)


# ── terminal states ────────────────────────────────────────────────────────


def terminal_states() -> list[RunState]:
    return [at(P.FINISH), fail(at(P.EXECUTE), kind="boom"), cancel(at(P.OBSERVE))]


@pytest.mark.parametrize("state", terminal_states(), ids=lambda s: s.phase.value)
def test_terminal_states_cannot_transition_any_way(state):
    for target in P:
        with pytest.raises(TerminalStateError):
            advance(state, target)
    with pytest.raises(TerminalStateError):
        fail(state, kind="again")
    with pytest.raises(TerminalStateError):
        cancel(state)
    with pytest.raises(TerminalStateError):
        record_artifact(state, artifact(state))
    assert isinstance(TerminalStateError("finish", "plan"), IllegalTransitionError)


def test_a_terminal_error_message_says_the_run_already_ended():
    with pytest.raises(TerminalStateError, match="already ended in finish"):
        advance(at(P.FINISH), P.PLAN)


# ── failure and cancellation ───────────────────────────────────────────────


@pytest.mark.parametrize("phase", WORKING)
def test_a_run_can_fail_from_every_working_phase(phase):
    state = fail(at(phase), kind="provider_error", message="x" * 400)
    assert state.phase is P.FAILED and state.status is RunStatus.FAILED
    assert state.failure == RunFailure(kind="provider_error", message="x" * 300)
    assert state.trail[-1] is P.FAILED


@pytest.mark.parametrize("phase", WORKING)
def test_a_run_can_be_cancelled_from_every_working_phase(phase):
    state = cancel(at(phase))
    assert state.phase is P.CANCELLED and state.status is RunStatus.CANCELLED
    assert state.failure is None and state.is_terminal


def test_cancel_keeps_what_the_run_had_produced():
    state = at(P.EXECUTE)
    state = record_artifact(state, artifact(state))
    cancelled = cancel(state)
    assert cancelled.artifacts == state.artifacts and cancelled.attempt == state.attempt


def test_a_failure_needs_a_valid_kind():
    with pytest.raises(ValidationError):
        fail(at(P.PLAN), kind="Not A Slug")


# ── artifacts ──────────────────────────────────────────────────────────────


def test_artifacts_attach_to_the_current_phase_and_attempt():
    state = at(P.EXECUTE)
    state = record_artifact(state, artifact(state, "a1"))
    state = advance(state, P.OBSERVE)
    state = record_artifact(state, artifact(state, "a2", ArtifactKind.CRITIQUE))
    assert [a.artifact_id for a in state.artifacts] == ["a1", "a2"]
    assert state.artifacts[1].phase is P.OBSERVE


def test_a_stale_or_duplicate_artifact_is_rejected():
    state = at(P.EXECUTE)
    stale = artifact(at(P.PLAN), "old")
    with pytest.raises(InvalidArtifactError, match="was made in plan"):
        record_artifact(state, stale)
    state = record_artifact(state, artifact(state, "a1"))
    with pytest.raises(InvalidArtifactError, match="already recorded"):
        record_artifact(state, artifact(state, "a1"))


def test_the_artifact_count_is_bounded():
    from velune.orchestration.state import MAX_ARTIFACTS

    state = at(P.EXECUTE)
    for i in range(MAX_ARTIFACTS):
        state = record_artifact(state, artifact(state, f"a{i}"))
    with pytest.raises(InvalidArtifactError, match="at most"):
        record_artifact(state, artifact(state, "overflow"))


def test_an_artifact_reference_carries_a_digest_not_content():
    fields = set(RunArtifact.model_fields)
    assert "digest" in fields and not fields & {"text", "content", "payload", "body"}
    with pytest.raises(ValidationError):
        RunArtifact(
            artifact_id="a",
            kind=ArtifactKind.ARTIFACT,
            author=AgentRole.CODER,
            phase=P.EXECUTE,
            attempt=1,
            digest="not-a-digest",
        )


# ── invariants a hand-built state cannot break ─────────────────────────────


def test_runstate_rejects_inconsistent_hand_built_values():
    with pytest.raises(ValidationError, match="must have status"):
        RunState(run_id="r", phase=P.FINISH, trail=(P.FINISH,))
    with pytest.raises(ValidationError, match="failed run carries a failure"):
        RunState(run_id="r", phase=P.FAILED, status=RunStatus.FAILED, trail=(P.FAILED,))
    with pytest.raises(ValidationError, match="failed run carries a failure"):
        RunState(run_id="r", failure=RunFailure(kind="x"))
    with pytest.raises(ValidationError, match="trail must end"):
        RunState(run_id="r", phase=P.EXECUTE, trail=(P.PLAN,))
    with pytest.raises(ValidationError, match="past a terminal"):
        RunState(
            run_id="r",
            phase=P.EXECUTE,
            trail=(P.PLAN, P.FINISH, P.EXECUTE),
        )
    with pytest.raises(ValidationError, match="exceed max_attempts"):
        RunState(run_id="r", attempt=4, max_attempts=3)
    with pytest.raises(ValidationError):
        RunState(run_id="r", scratch={"anything": 1})  # type: ignore[call-arg]
    with pytest.raises(ValidationError):
        RunState(run_id="bad id!")


def test_runstate_is_frozen():
    state = RunState.start("run-1")
    with pytest.raises(ValidationError):
        state.phase = P.FINISH  # type: ignore[misc]


# ── serialization ──────────────────────────────────────────────────────────


def test_every_reachable_state_round_trips_through_json():
    states = [at(p) for p in (P.PLAN, P.EXECUTE, P.OBSERVE, P.REFLECT, P.VALIDATE, P.FINISH)]
    retried = at(P.REFLECT)
    retried = advance(retried, P.RETRY)
    exe = advance(retried, P.EXECUTE)
    states += [retried, exe, record_artifact(exe, artifact(exe)), fail(exe, kind="timeout")]
    states.append(cancel(exe))
    for state in states:
        assert RunState.model_validate_json(state.model_dump_json()) == state
        assert RunState.model_validate(state.model_dump(mode="json")) == state


def test_json_is_plain_data():
    import json

    state = record_artifact(at(P.EXECUTE), artifact(at(P.EXECUTE)))
    data = json.loads(state.model_dump_json())
    assert data["phase"] == "execute" and data["status"] == "running"
    assert data["trail"] == ["plan", "execute"]
    assert data["artifacts"][0]["kind"] == "artifact"
    assert data["schema_version"] == 1


def test_json_with_an_unknown_field_or_phase_is_rejected():
    data = RunState.start("r").model_dump(mode="json")
    with pytest.raises(ValidationError):
        RunState.model_validate({**data, "extra": 1})
    with pytest.raises(ValidationError):
        RunState.model_validate({**data, "phase": "teleport"})
