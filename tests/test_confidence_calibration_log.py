"""Unit tests for ConfidenceCalibrationLog and its wiring into RepositoryCognitionService.

This is the prerequisite data a future confidence-fusion model (log-odds/
Bayesian, per the v1-vs-v2 architecture discussion) would need before its
weights could be tuned against anything other than a guess — see
velune/repository/calibration.py's module docstring for why a flat
weighted sum (ClaimAccumulator) is used today instead of that fancier
model.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from velune.repository.calibration import ConfidenceCalibrationLog
from velune.repository.cognition import RepositoryCognitionService
from velune.repository.schemas import Claim


def test_record_claim_and_read_back(tmp_path):
    log = ConfidenceCalibrationLog(tmp_path / "calibration.jsonl")
    claim = Claim(value="Python", confidence=0.5, source_signals=["pyproject.toml present"])
    log.record_claim("language:Python", claim, context="tech_stack.language")

    records = log._read_records()
    assert len(records) == 1
    assert records[0]["value"] == "Python"
    assert records[0]["source_signals"] == ["pyproject.toml present"]


def test_summarize_with_no_outcomes_yet_reports_claims_but_no_confirmation_rate(tmp_path):
    log = ConfidenceCalibrationLog(tmp_path / "calibration.jsonl")
    log.record_claim("language:Python", Claim(value="Python", confidence=0.5, source_signals=["sig-a"]))

    summary = log.summarize()
    assert "sig-a" in summary
    assert summary["sig-a"].claims == 1
    assert summary["sig-a"].outcomes == 0
    assert summary["sig-a"].confirmation_rate is None


def test_summarize_computes_confirmation_rate_once_outcomes_exist(tmp_path):
    log = ConfidenceCalibrationLog(tmp_path / "calibration.jsonl")
    log.record_claim("a", Claim(value="X", confidence=0.5, source_signals=["sig"]))
    log.record_claim("b", Claim(value="Y", confidence=0.5, source_signals=["sig"]))
    log.record_outcome("a", confirmed=True)
    log.record_outcome("b", confirmed=False)

    summary = log.summarize()
    assert summary["sig"].claims == 2
    assert summary["sig"].outcomes == 2
    assert summary["sig"].confirmed == 1
    assert summary["sig"].confirmation_rate == 0.5


def test_missing_log_file_summarizes_to_empty(tmp_path):
    log = ConfidenceCalibrationLog(tmp_path / "does_not_exist.jsonl")
    assert log.summarize() == {}


def test_corrupt_line_is_skipped_not_fatal(tmp_path):
    log_path = tmp_path / "calibration.jsonl"
    log_path.write_text(
        '{"kind": "claim", "claim_id": "a", "value": "X", "confidence": 0.5, "source_signals": ["s"]}\n'
        "not valid json\n"
        '{"kind": "claim", "claim_id": "b", "value": "Y", "confidence": 0.5, "source_signals": ["s"]}\n',
        encoding="utf-8",
    )
    log = ConfidenceCalibrationLog(log_path)
    summary = log.summarize()
    assert summary["s"].claims == 2


def test_full_index_run_logs_tech_stack_claims():
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        (root / "pyproject.toml").write_text(
            '[project]\nname="x"\ndependencies=["fastapi"]\n', encoding="utf-8"
        )
        (root / "main.py").write_text("from fastapi import FastAPI\n", encoding="utf-8")

        RepositoryCognitionService(root).index(force=True)

        log = ConfidenceCalibrationLog(root / ".velune" / "confidence_calibration.jsonl")
        records = log._read_records()
        assert any(r["value"] == "Python" for r in records)
        assert any(r["value"] == "FastAPI" for r in records)
