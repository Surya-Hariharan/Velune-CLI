"""Canonical JSON and digests are deterministic, order-independent and volatility-aware."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from typing import ClassVar

import pytest

from velune.council.contracts import Artifact, Claim, Perspective
from velune.council.request import CouncilRequest
from velune.council.serialization import (
    SCHEMA_VERSION,
    Contract,
    canonical_json,
    digest,
    full_json,
)


class Timed(Contract):
    name: str
    elapsed_ms: int = 0
    volatile_fields: ClassVar[frozenset[str]] = frozenset({"elapsed_ms"})


class Wrapper(Contract):
    items: tuple[Timed, ...]


def _perspective() -> Perspective:
    return Perspective(
        seat_id="analyst",
        position="p",
        claims=(Claim(id="AN-1", text="t", status="known", confidence=0.3333333),),
        confidence=0.66666666,
        rationale="r",
    )


def test_canonical_json_has_sorted_keys_and_no_whitespace():
    text = canonical_json(_perspective())
    assert ", " not in text and ": " not in text
    keys = list(json.loads(text))
    assert keys == sorted(keys)


def test_canonical_json_is_stable_across_equal_values():
    assert canonical_json(_perspective()) == canonical_json(_perspective())
    assert digest(_perspective()) == digest(_perspective())


def test_floats_are_rounded_in_the_canonical_form():
    assert '"confidence":0.6667' in canonical_json(_perspective())


def test_digest_is_twelve_hex_characters():
    value = digest(_perspective())
    assert len(value) == 12 and int(value, 16) >= 0


def test_digest_changes_when_content_changes():
    other = _perspective().model_copy(update={"position": "different"})
    assert digest(other) != digest(_perspective())


def test_volatile_fields_are_excluded_from_digest_but_kept_in_full_form():
    fast, slow = Timed(name="a", elapsed_ms=1), Timed(name="a", elapsed_ms=999)
    assert canonical_json(fast) == canonical_json(slow)
    assert digest(fast) == digest(slow)
    assert "elapsed_ms" not in canonical_json(fast)
    assert '"elapsed_ms":999' in full_json(slow)
    assert full_json(fast) != full_json(slow)


def test_volatile_fields_are_excluded_when_nested():
    one = Wrapper(items=(Timed(name="a", elapsed_ms=1), Timed(name="b", elapsed_ms=2)))
    two = Wrapper(items=(Timed(name="a", elapsed_ms=50), Timed(name="b", elapsed_ms=60)))
    assert digest(one) == digest(two)
    assert full_json(one) != full_json(two)


def test_metadata_order_does_not_change_the_digest():
    a = Artifact(kind="text", content="x", metadata=(("a", "1"), ("b", "2")))
    b = Artifact(kind="text", content="x", metadata=(("b", "2"), ("a", "1")))
    assert canonical_json(a) == canonical_json(b)
    r1 = CouncilRequest(request_id="r", question="q", metadata=(("k", "1"), ("a", "2")))
    r2 = CouncilRequest(request_id="r", question="q", metadata=(("a", "2"), ("k", "1")))
    assert digest(r1) == digest(r2)


def test_sets_are_refused():
    with pytest.raises(TypeError):
        canonical_json({"s": {1, 2}})


def test_non_ascii_text_is_preserved():
    art = Artifact(kind="text", content="café ✓")
    assert "café ✓" in canonical_json(art)


def test_schema_version_is_present_in_top_level_contracts():
    assert json.loads(canonical_json(_perspective()))["schema_version"] == SCHEMA_VERSION


_SCRIPT = """
from velune.council.contracts import Claim, Perspective
from velune.council.request import CouncilRequest
from velune.council.serialization import digest
p = Perspective(seat_id="analyst", position="p",
    claims=(Claim(id="AN-1", text="t", status="known", confidence=0.4),),
    confidence=0.5, rationale="r")
r = CouncilRequest(request_id="r", question="q", metadata=(("z", "1"), ("a", "2")))
print(digest(p), digest(r))
"""


def test_digest_is_identical_across_interpreter_hash_seeds():
    outputs = set()
    for seed in ("0", "1", "12345", "random"):
        env = {"PYTHONHASHSEED": seed, "PATH": "", "SYSTEMROOT": "C:\\Windows"}

        env = {**os.environ, "PYTHONHASHSEED": seed}
        run = subprocess.run(
            [sys.executable, "-c", _SCRIPT],
            capture_output=True,
            text=True,
            env=env,
            timeout=60,
        )
        assert run.returncode == 0, run.stderr
        outputs.add(run.stdout.strip())
    assert len(outputs) == 1
