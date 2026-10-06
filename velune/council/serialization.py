"""Strict contract base class and deterministic serialization.

Every contract is frozen and rejects unknown fields, so a ``reasoning`` or ``thoughts`` key cannot
be smuggled into an artifact. Canonical JSON is independent of dict order, interpreter hash seed
and wall-clock fields: the same logical value always yields the same bytes and the same digest.
"""

from __future__ import annotations

import enum
import hashlib
import json
from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict

SCHEMA_VERSION = 1
DIGEST_LENGTH = 12
FLOAT_PLACES = 4


class Contract(BaseModel):
    """Frozen, strict base for everything the core defines."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    # Field names that vary between otherwise identical runs (timings). Excluded from the
    # canonical form and the digest; kept by ``full_json`` for audit.
    volatile_fields: ClassVar[frozenset[str]] = frozenset()


def round_float(value: float) -> float:
    return round(float(value), FLOAT_PLACES)


def _plain(value: Any, *, keep_volatile: bool) -> Any:
    if isinstance(value, Contract):
        skip = frozenset() if keep_volatile else type(value).volatile_fields
        return {
            name: _plain(getattr(value, name), keep_volatile=keep_volatile)
            for name in type(value).model_fields
            if name not in skip
        }
    if isinstance(value, BaseModel):
        return {
            name: _plain(getattr(value, name), keep_volatile=keep_volatile)
            for name in type(value).model_fields
        }
    if isinstance(value, enum.Enum):
        return value.value
    if isinstance(value, (tuple, list)):
        return [_plain(item, keep_volatile=keep_volatile) for item in value]
    if isinstance(value, (set, frozenset)):
        raise TypeError("sets are not canonically ordered; use a sorted tuple")
    if isinstance(value, dict):
        return {str(k): _plain(v, keep_volatile=keep_volatile) for k, v in value.items()}
    if isinstance(value, float):
        return round_float(value)
    return value


def _dumps(plain: Any) -> str:
    return json.dumps(plain, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def canonical_json(value: Any) -> str:
    """Deterministic JSON without volatile fields. This is what digests are computed over."""
    return _dumps(_plain(value, keep_volatile=False))


def full_json(value: Any) -> str:
    """Same ordering rules as :func:`canonical_json`, but keeps volatile fields (for audit)."""
    return _dumps(_plain(value, keep_volatile=True))


def digest(value: Any) -> str:
    """First 12 hex characters of the SHA-256 of the canonical form."""
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()[:DIGEST_LENGTH]
