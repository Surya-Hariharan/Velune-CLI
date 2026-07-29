"""Confidence-calibration log for classifier claims.

docs/repository-intelligence-baseline.md's v1-vs-v2 discussion (referenced
in this repo's architecture review) singles out log-odds/Bayesian evidence
fusion as more principled than a flat weighted sum, but harder to
calibrate — and flags that this codebase has no labeled ground truth to
calibrate it against. Tuning fusion weights without that data is a guess
dressed up as engineering.

This is the prerequisite substrate: an append-only log correlating a claim
(what ``ClaimAccumulator`` produced, and which signals fed it) with its
eventual outcome (was it actually right?), so a future confidence model can
be tuned against real history instead of hand-picked weights. Nothing in
this codebase yet calls ``record_outcome`` from a real feedback signal (a
human correction, an agent action's downstream success/failure) — no such
signal exists yet to wire it to. This ships the recording/query surface
ahead of that integration rather than fabricating one to look complete.
``record_claim`` *is* wired in — see ``RepositoryCognitionService._run_pipeline``,
which logs every full-index run's tech-stack claims.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path

from velune.repository.schemas import Claim

logger = logging.getLogger("velune.repository.calibration")


@dataclass
class SignalCalibration:
    """Empirical calibration stats for one named signal, across every claim
    it has ever contributed to that also has a recorded outcome."""

    signal: str
    claims: int = 0
    outcomes: int = 0
    confirmed: int = 0

    @property
    def confirmation_rate(self) -> float | None:
        """Fraction of outcome-having claims this signal contributed to that
        were confirmed. None when there are no outcomes yet to divide by."""
        return self.confirmed / self.outcomes if self.outcomes else None


class ConfidenceCalibrationLog:
    """Append-only JSONL log of claims and their eventual outcomes.

    Append-only by design: unlike ``index_state.json`` (a read-modify-write
    cache that needed a lock — see ``index_state.index_state_lock``), every
    write here is a pure append, so concurrent writers can't lose each
    other's records the same way.
    """

    def __init__(self, log_path: Path) -> None:
        self.log_path = log_path

    def record_claim(self, claim_id: str, claim: Claim, context: str = "") -> None:
        """Append a claim record. Never raises — a logging failure must not
        interrupt indexing."""
        self._append(
            {
                "kind": "claim",
                "claim_id": claim_id,
                "value": claim.value,
                "confidence": claim.confidence,
                "source_signals": list(claim.source_signals),
                "context": context,
                "recorded_at": time.time(),
            }
        )

    def record_outcome(self, claim_id: str, confirmed: bool, note: str = "") -> None:
        """Append an outcome record for a previously-logged ``claim_id``.

        Not currently called anywhere in this codebase — see module
        docstring. Ready for a future integration (a human correction to a
        capability description, an agent action whose success/failure can
        be traced back to a claim it relied on).
        """
        self._append(
            {
                "kind": "outcome",
                "claim_id": claim_id,
                "confirmed": confirmed,
                "note": note,
                "recorded_at": time.time(),
            }
        )

    def summarize(self) -> dict[str, SignalCalibration]:
        """Empirical confirmation rate per source signal, from claims with a
        known outcome.

        Returns ``{}`` when the log doesn't exist yet or has no outcomes
        recorded — there's nothing to calibrate against yet, which is
        itself a meaningful (not an error) state.
        """
        claims_by_id: dict[str, dict] = {}
        outcomes_by_id: dict[str, bool] = {}

        for record in self._read_records():
            kind = record.get("kind")
            claim_id = record.get("claim_id")
            if not claim_id:
                continue
            if kind == "claim":
                claims_by_id[claim_id] = record
            elif kind == "outcome":
                outcomes_by_id[claim_id] = bool(record.get("confirmed"))

        per_signal: dict[str, SignalCalibration] = {}
        for claim_id, claim_record in claims_by_id.items():
            outcome = outcomes_by_id.get(claim_id)
            for signal in claim_record.get("source_signals", []):
                stats = per_signal.setdefault(signal, SignalCalibration(signal=signal))
                stats.claims += 1
                if outcome is not None:
                    stats.outcomes += 1
                    if outcome:
                        stats.confirmed += 1

        return per_signal

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _append(self, record: dict) -> None:
        try:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.log_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(record) + "\n")
        except OSError as exc:
            logger.debug("Could not append to confidence calibration log: %s", exc)

    def _read_records(self) -> list[dict]:
        if not self.log_path.exists():
            return []
        records: list[dict] = []
        try:
            with open(self.log_path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        records.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue  # skip a corrupt line rather than fail the whole read
        except OSError as exc:
            logger.debug("Could not read confidence calibration log: %s", exc)
        return records
