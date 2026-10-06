"""Prompt behaviour must not depend on which machine you are on.

The git-ignored premium layer used to override the committed baseline just by
existing on disk, so one release behaved differently on a developer machine, in
CI and from PyPI. It is now opt-in (VELUNE_PROMPT_LAYER=premium), visible in
doctor and in council traces, and its JSON contracts are checked.
"""

from __future__ import annotations

import importlib
import importlib.util
import logging
import re
import sys
import types

import pytest

import velune.cognition.prompts as prompts
from tests.council_fakes import make_orchestrator
from velune.cognition.council.messages import ChallengerMessage, PlannerMessage, ReviewerMessage
from velune.cognition.prompts import (
    CHAT_CONVERSATIONAL,
    CHAT_INTERACTIVE,
    COUNCIL_CHALLENGER,
    COUNCIL_CODER,
    COUNCIL_PLANNER,
    COUNCIL_REVIEWER,
    COUNCIL_SYNTHESIZER,
    PROMPT_LAYER_ENV,
    _baseline,
    active_layer,
    get_prompt,
    is_premium_active,
    reset_prompt_layer,
)

PREMIUM_MODULE = "velune.cognition.prompts._premium"
ALL_KEYS = (
    COUNCIL_PLANNER,
    COUNCIL_CODER,
    COUNCIL_REVIEWER,
    COUNCIL_CHALLENGER,
    COUNCIL_SYNTHESIZER,
    CHAT_INTERACTIVE,
    CHAT_CONVERSATIONAL,
)


@pytest.fixture(autouse=True)
def _clean_layer(monkeypatch):
    monkeypatch.delenv(PROMPT_LAYER_ENV, raising=False)
    reset_prompt_layer()
    yield
    monkeypatch.undo()
    reset_prompt_layer()


def _install_premium(monkeypatch, prompts_value):
    fake = types.ModuleType(PREMIUM_MODULE)
    fake.PROMPTS = prompts_value
    monkeypatch.setitem(sys.modules, PREMIUM_MODULE, fake)
    monkeypatch.setattr(prompts, "_premium", fake, raising=False)
    return fake


def _block_premium(monkeypatch):
    monkeypatch.setitem(sys.modules, PREMIUM_MODULE, None)  # makes the import fail
    monkeypatch.delattr(prompts, "_premium", raising=False)


# ── default: the same everywhere ─────────────────────────────────────────────


def test_a_premium_file_on_disk_changes_nothing_by_default(monkeypatch):
    _install_premium(monkeypatch, {COUNCIL_CODER: "PREMIUM CODER"})
    assert get_prompt(COUNCIL_CODER) == _baseline.PROMPTS[COUNCIL_CODER]
    info = active_layer()
    assert info.name == "baseline" and info.requested == "baseline"
    assert info.premium_available is True
    assert is_premium_active() is False


def test_baseline_is_the_default_without_any_premium_module(monkeypatch):
    _block_premium(monkeypatch)
    assert get_prompt(COUNCIL_PLANNER) == _baseline.PROMPTS[COUNCIL_PLANNER]
    assert active_layer().premium_available is False


# ── opt-in ───────────────────────────────────────────────────────────────────


def test_premium_applies_only_when_requested_and_falls_back_per_key(monkeypatch):
    _install_premium(monkeypatch, {COUNCIL_CODER: "PREMIUM CODER"})
    monkeypatch.setenv(PROMPT_LAYER_ENV, "premium")
    reset_prompt_layer()
    assert get_prompt(COUNCIL_CODER) == "PREMIUM CODER"
    assert get_prompt(COUNCIL_PLANNER) == _baseline.PROMPTS[COUNCIL_PLANNER]
    assert active_layer().name == "premium"
    assert is_premium_active() is True


def test_the_environment_value_is_case_and_space_insensitive(monkeypatch):
    _install_premium(monkeypatch, {COUNCIL_CODER: "PREMIUM CODER"})
    monkeypatch.setenv(PROMPT_LAYER_ENV, "  Premium ")
    reset_prompt_layer()
    assert get_prompt(COUNCIL_CODER) == "PREMIUM CODER"


def test_requested_premium_that_is_missing_falls_back_loudly(monkeypatch, caplog):
    _block_premium(monkeypatch)
    monkeypatch.setenv(PROMPT_LAYER_ENV, "premium")
    reset_prompt_layer()
    with caplog.at_level(logging.WARNING, logger="velune.cognition.prompts"):
        assert get_prompt(COUNCIL_CODER) == _baseline.PROMPTS[COUNCIL_CODER]
    assert "no usable premium prompts" in caplog.text
    info = active_layer()
    assert info.name == "baseline" and info.requested == "premium"


@pytest.mark.parametrize("bad", [None, "just a string", ["a"], {}, {1: "x"}, {COUNCIL_CODER: "  "}])
def test_requested_premium_that_is_malformed_falls_back_loudly(monkeypatch, caplog, bad):
    _install_premium(monkeypatch, bad)
    monkeypatch.setenv(PROMPT_LAYER_ENV, "premium")
    reset_prompt_layer()
    with caplog.at_level(logging.WARNING, logger="velune.cognition.prompts"):
        assert get_prompt(COUNCIL_CODER) == _baseline.PROMPTS[COUNCIL_CODER]
    assert "no usable premium prompts" in caplog.text


def test_malformed_entries_are_dropped_but_valid_ones_kept(monkeypatch):
    _install_premium(monkeypatch, {COUNCIL_CODER: "PREMIUM CODER", COUNCIL_PLANNER: 7, 3: "x"})
    monkeypatch.setenv(PROMPT_LAYER_ENV, "premium")
    reset_prompt_layer()
    assert get_prompt(COUNCIL_CODER) == "PREMIUM CODER"
    assert get_prompt(COUNCIL_PLANNER) == _baseline.PROMPTS[COUNCIL_PLANNER]


def test_an_unknown_layer_name_uses_the_baseline_and_warns(monkeypatch, caplog):
    _install_premium(monkeypatch, {COUNCIL_CODER: "PREMIUM CODER"})
    monkeypatch.setenv(PROMPT_LAYER_ENV, "bogus")
    reset_prompt_layer()
    with caplog.at_level(logging.WARNING, logger="velune.cognition.prompts"):
        assert get_prompt(COUNCIL_CODER) == _baseline.PROMPTS[COUNCIL_CODER]
    assert "Unknown VELUNE_PROMPT_LAYER" in caplog.text


# ── digest ───────────────────────────────────────────────────────────────────


def test_the_digest_is_stable_and_tells_layers_apart(monkeypatch):
    first = active_layer().digest
    assert first == active_layer().digest
    assert re.fullmatch(r"[0-9a-f]{12}", first)

    _install_premium(monkeypatch, {COUNCIL_CODER: "PREMIUM CODER"})
    monkeypatch.setenv(PROMPT_LAYER_ENV, "premium")
    reset_prompt_layer()
    assert active_layer().digest != first


def test_the_digest_changes_when_a_prompt_changes(monkeypatch):
    before = active_layer().digest
    changed = dict(_baseline.PROMPTS)
    changed[COUNCIL_CODER] += " (edited)"
    monkeypatch.setattr(_baseline, "PROMPTS", changed)
    assert active_layer().digest != before


# ── completeness and contracts ───────────────────────────────────────────────


def test_every_prompt_key_exists_in_the_baseline():
    for key in ALL_KEYS:
        assert _baseline.PROMPTS[key].strip()
        assert get_prompt(key) == _baseline.PROMPTS[key]


def test_unknown_keys_raise():
    with pytest.raises(KeyError):
        get_prompt("council.nope")


def _json_fields(text: str) -> set[str]:
    block = text.split("JSON Format:", 1)[1]
    return set(re.findall(r'"([A-Za-z_][A-Za-z0-9_]*)"\s*:', block))


@pytest.mark.parametrize(
    ("key", "message"),
    [
        (COUNCIL_PLANNER, PlannerMessage),
        (COUNCIL_REVIEWER, ReviewerMessage),
        (COUNCIL_CHALLENGER, ChallengerMessage),
    ],
)
def test_baseline_json_contracts_cover_the_typed_messages(key, message):
    expected = set(message.model_fields) - {"parse_error", "status"}
    assert expected <= _json_fields(_baseline.PROMPTS[key])


def _real_premium():
    if importlib.util.find_spec(PREMIUM_MODULE) is None:
        pytest.skip("no local premium prompt layer on this machine")
    return importlib.import_module(PREMIUM_MODULE)


@pytest.mark.parametrize("key", [COUNCIL_PLANNER, COUNCIL_REVIEWER, COUNCIL_CHALLENGER])
def test_a_real_premium_layer_keeps_the_json_field_names(key):
    premium = _real_premium()
    if key not in premium.PROMPTS:
        pytest.skip(f"premium does not override {key}")
    assert _json_fields(premium.PROMPTS[key]) == _json_fields(_baseline.PROMPTS[key])


def test_a_real_premium_layer_only_overrides_known_keys():
    premium = _real_premium()
    assert set(premium.PROMPTS) <= set(_baseline.PROMPTS)


# ── surfaces ─────────────────────────────────────────────────────────────────


def test_doctor_shows_the_baseline_and_hints_at_a_local_premium_layer(monkeypatch):
    from velune.cli.commands.doctor import _check_prompt_layer

    _install_premium(monkeypatch, {COUNCIL_CODER: "PREMIUM CODER"})
    result = _check_prompt_layer()
    assert result["status"] == "ok"
    assert result["message"].startswith(f"baseline ({active_layer().digest})")
    assert f"{PROMPT_LAYER_ENV}=premium" in result["message"]


def test_doctor_warns_when_premium_was_requested_but_is_unavailable(monkeypatch):
    from velune.cli.commands.doctor import _check_prompt_layer

    _block_premium(monkeypatch)
    monkeypatch.setenv(PROMPT_LAYER_ENV, "premium")
    reset_prompt_layer()
    result = _check_prompt_layer()
    assert result["status"] == "warn"
    assert "premium was requested" in result["message"]


def test_doctor_reports_premium_when_it_is_active(monkeypatch):
    from velune.cli.commands.doctor import _check_prompt_layer

    _install_premium(monkeypatch, {COUNCIL_CODER: "PREMIUM CODER"})
    monkeypatch.setenv(PROMPT_LAYER_ENV, "premium")
    reset_prompt_layer()
    result = _check_prompt_layer()
    assert result["status"] == "ok" and result["message"].startswith("premium (")


async def test_council_traces_record_the_prompt_layer(monkeypatch):
    orch, _ = make_orchestrator(monkeypatch)
    result = await orch.execute_task("explain it", "ctx", council_tier="standard")
    trace = result["execution_trace"]
    assert trace["prompt_layer"] == f"baseline {active_layer().digest}"
