"""Prompt behaviour must not depend on which machine you are on.

The git-ignored premium layer used to override the committed baseline just by
existing on disk, so one release behaved differently on a developer machine, in
CI and from PyPI. It is now opt-in (VELUNE_PROMPT_LAYER=premium), visible in
doctor.
"""

from __future__ import annotations

import logging
import re
import sys
import types

import pytest

import velune.cognition.prompts as prompts
from velune.cognition.prompts import (
    CHAT_CONVERSATIONAL,
    CHAT_INTERACTIVE,
    PROMPT_LAYER_ENV,
    _baseline,
    active_layer,
    get_prompt,
    is_premium_active,
    reset_prompt_layer,
)

PREMIUM_MODULE = "velune.cognition.prompts._premium"
ALL_KEYS = (CHAT_INTERACTIVE, CHAT_CONVERSATIONAL)


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
    _install_premium(monkeypatch, {CHAT_INTERACTIVE: "PREMIUM CHAT"})
    assert get_prompt(CHAT_INTERACTIVE) == _baseline.PROMPTS[CHAT_INTERACTIVE]
    info = active_layer()
    assert info.name == "baseline" and info.requested == "baseline"
    assert info.premium_available is True
    assert is_premium_active() is False


def test_baseline_is_the_default_without_any_premium_module(monkeypatch):
    _block_premium(monkeypatch)
    assert get_prompt(CHAT_CONVERSATIONAL) == _baseline.PROMPTS[CHAT_CONVERSATIONAL]
    assert active_layer().premium_available is False


# ── opt-in ───────────────────────────────────────────────────────────────────


def test_premium_applies_only_when_requested_and_falls_back_per_key(monkeypatch):
    _install_premium(monkeypatch, {CHAT_INTERACTIVE: "PREMIUM CHAT"})
    monkeypatch.setenv(PROMPT_LAYER_ENV, "premium")
    reset_prompt_layer()
    assert get_prompt(CHAT_INTERACTIVE) == "PREMIUM CHAT"
    assert get_prompt(CHAT_CONVERSATIONAL) == _baseline.PROMPTS[CHAT_CONVERSATIONAL]
    assert active_layer().name == "premium"
    assert is_premium_active() is True


def test_the_environment_value_is_case_and_space_insensitive(monkeypatch):
    _install_premium(monkeypatch, {CHAT_INTERACTIVE: "PREMIUM CHAT"})
    monkeypatch.setenv(PROMPT_LAYER_ENV, "  Premium ")
    reset_prompt_layer()
    assert get_prompt(CHAT_INTERACTIVE) == "PREMIUM CHAT"


def test_requested_premium_that_is_missing_falls_back_loudly(monkeypatch, caplog):
    _block_premium(monkeypatch)
    monkeypatch.setenv(PROMPT_LAYER_ENV, "premium")
    reset_prompt_layer()
    with caplog.at_level(logging.WARNING, logger="velune.cognition.prompts"):
        assert get_prompt(CHAT_INTERACTIVE) == _baseline.PROMPTS[CHAT_INTERACTIVE]
    assert "no usable premium prompts" in caplog.text
    info = active_layer()
    assert info.name == "baseline" and info.requested == "premium"


@pytest.mark.parametrize(
    "bad", [None, "just a string", ["a"], {}, {1: "x"}, {CHAT_INTERACTIVE: "  "}]
)
def test_requested_premium_that_is_malformed_falls_back_loudly(monkeypatch, caplog, bad):
    _install_premium(monkeypatch, bad)
    monkeypatch.setenv(PROMPT_LAYER_ENV, "premium")
    reset_prompt_layer()
    with caplog.at_level(logging.WARNING, logger="velune.cognition.prompts"):
        assert get_prompt(CHAT_INTERACTIVE) == _baseline.PROMPTS[CHAT_INTERACTIVE]
    assert "no usable premium prompts" in caplog.text


def test_malformed_entries_are_dropped_but_valid_ones_kept(monkeypatch):
    _install_premium(
        monkeypatch, {CHAT_INTERACTIVE: "PREMIUM CHAT", CHAT_CONVERSATIONAL: 7, 3: "x"}
    )
    monkeypatch.setenv(PROMPT_LAYER_ENV, "premium")
    reset_prompt_layer()
    assert get_prompt(CHAT_INTERACTIVE) == "PREMIUM CHAT"
    assert get_prompt(CHAT_CONVERSATIONAL) == _baseline.PROMPTS[CHAT_CONVERSATIONAL]


def test_an_unknown_layer_name_uses_the_baseline_and_warns(monkeypatch, caplog):
    _install_premium(monkeypatch, {CHAT_INTERACTIVE: "PREMIUM CHAT"})
    monkeypatch.setenv(PROMPT_LAYER_ENV, "bogus")
    reset_prompt_layer()
    with caplog.at_level(logging.WARNING, logger="velune.cognition.prompts"):
        assert get_prompt(CHAT_INTERACTIVE) == _baseline.PROMPTS[CHAT_INTERACTIVE]
    assert "Unknown VELUNE_PROMPT_LAYER" in caplog.text


# ── digest ───────────────────────────────────────────────────────────────────


def test_the_digest_is_stable_and_tells_layers_apart(monkeypatch):
    first = active_layer().digest
    assert first == active_layer().digest
    assert re.fullmatch(r"[0-9a-f]{12}", first)

    _install_premium(monkeypatch, {CHAT_INTERACTIVE: "PREMIUM CHAT"})
    monkeypatch.setenv(PROMPT_LAYER_ENV, "premium")
    reset_prompt_layer()
    assert active_layer().digest != first


def test_the_digest_changes_when_a_prompt_changes(monkeypatch):
    before = active_layer().digest
    changed = dict(_baseline.PROMPTS)
    changed[CHAT_INTERACTIVE] += " (edited)"
    monkeypatch.setattr(_baseline, "PROMPTS", changed)
    assert active_layer().digest != before


# ── completeness and contracts ───────────────────────────────────────────────


def test_every_prompt_key_exists_in_the_baseline():
    for key in ALL_KEYS:
        assert _baseline.PROMPTS[key].strip()
        assert get_prompt(key) == _baseline.PROMPTS[key]


def test_unknown_keys_raise():
    with pytest.raises(KeyError):
        get_prompt("chat.nope")


# ── surfaces ─────────────────────────────────────────────────────────────────


def test_doctor_shows_the_baseline_and_hints_at_a_local_premium_layer(monkeypatch):
    from velune.cli.commands.doctor import _check_prompt_layer

    _install_premium(monkeypatch, {CHAT_INTERACTIVE: "PREMIUM CHAT"})
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

    _install_premium(monkeypatch, {CHAT_INTERACTIVE: "PREMIUM CHAT"})
    monkeypatch.setenv(PROMPT_LAYER_ENV, "premium")
    reset_prompt_layer()
    result = _check_prompt_layer()
    assert result["status"] == "ok" and result["message"].startswith("premium (")
