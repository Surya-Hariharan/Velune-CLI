"""The input to a council run: the question, the context and the budgets.

The core never reads files, repositories or the network. ``context`` is opaque text a caller has
already assembled, and ``evidence`` is whatever it chose to hand over.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field, StringConstraints, field_validator

from velune.council.domain import Depth
from velune.council.serialization import SCHEMA_VERSION, Contract

Identifier = Annotated[str, StringConstraints(min_length=1, max_length=64, pattern=r"^[\w.:-]+$")]


class EvidenceItem(Contract):
    id: Identifier
    text: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=4000)]
    source: Annotated[str, StringConstraints(max_length=200)] = ""


class ResponseRequirements(Contract):
    language: Annotated[str, StringConstraints(max_length=32)] = ""
    format: Annotated[str, StringConstraints(max_length=64)] = ""
    length: Annotated[str, StringConstraints(max_length=64)] = ""
    audience: Annotated[str, StringConstraints(max_length=64)] = ""


class CouncilSettings(Contract):
    """Budgets as data. No provider or model names live here."""

    wall_budget_s: float = Field(default=600.0, gt=0)
    seat_timeout_s: float = Field(default=120.0, gt=0)
    max_concurrency: int = Field(default=1, ge=1, le=16)
    seed: int | None = None


class CouncilRequest(Contract):
    schema_version: Literal[1] = SCHEMA_VERSION
    request_id: Identifier
    question: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=8000)
    ]
    profile_id: Identifier = "general"
    depth: Depth = Depth.STANDARD
    context: Annotated[str, StringConstraints(max_length=16000)] = ""
    evidence: tuple[EvidenceItem, ...] = ()
    response: ResponseRequirements = ResponseRequirements()
    settings: CouncilSettings = CouncilSettings()
    metadata: tuple[tuple[str, str], ...] = ()

    @field_validator("evidence")
    @classmethod
    def _unique_evidence_ids(cls, items: tuple[EvidenceItem, ...]) -> tuple[EvidenceItem, ...]:
        ids = [item.id for item in items]
        if len(set(ids)) != len(ids):
            raise ValueError("evidence ids must be unique")
        return items

    @field_validator("metadata")
    @classmethod
    def _sorted_metadata(cls, pairs: tuple[tuple[str, str], ...]) -> tuple[tuple[str, str], ...]:
        return tuple(sorted(pairs))
