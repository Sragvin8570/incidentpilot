"""
Unit tests for the ML confidence calibration module.

Tests are deliberately fast and do not require a trained model on disk.
They test:
  - Feature engineering (correct mapping, correct range)
  - Label generation (correct / wrong per outcome × state combos)
  - Predictor training on synthetic data
  - PredictionInterval properties (lo ≤ score ≤ hi, width ≥ 0)
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

# Allow importing the app package from tests/
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.ml.features import (
    FEATURE_NAMES,
    DecisionRow,
    _label,
    load_csv,
    row_to_features,
)
from app.ml.train import train


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_row(**kwargs) -> DecisionRow:
    defaults = dict(
        llm_confidence=0.9,
        grounding_score=1.0,
        runbook_score=0.8,
        confidence=0.92,
        risk_score=0.45,
        outcome="READY",
        state="RESOLVED",
        environment="dev",
        target_kind="Deployment",
    )
    defaults.update(kwargs)
    return DecisionRow(**defaults)


def _make_csv(tmp_path: Path, rows: list[dict]) -> Path:
    import csv

    fieldnames = [
        "llm_confidence", "grounding_score", "runbook_score", "confidence",
        "risk_score", "outcome", "state", "environment", "target_kind",
        # extra columns that load_csv should ignore
        "id", "incident_id", "risk_reason", "target_name", "namespace", "severity",
    ]
    path = tmp_path / "decisions_raw.csv"
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            full = {k: "" for k in fieldnames}
            full.update(row)
            writer.writerow(full)
    return path


# ---------------------------------------------------------------------------
# Feature engineering
# ---------------------------------------------------------------------------

class TestRowToFeatures:
    def test_shape(self):
        x = row_to_features(_make_row())
        assert x.shape == (len(FEATURE_NAMES),)

    def test_dtype(self):
        x = row_to_features(_make_row())
        assert x.dtype == np.float32

    def test_production_flag(self):
        dev_x = row_to_features(_make_row(environment="dev"))
        prod_x = row_to_features(_make_row(environment="production"))
        assert dev_x[5] == 0.0
        assert prod_x[5] == 1.0

    def test_prod_alias(self):
        x = row_to_features(_make_row(environment="prod"))
        assert x[5] == 1.0

    def test_statefulset_flag(self):
        dep_x = row_to_features(_make_row(target_kind="Deployment"))
        sts_x = row_to_features(_make_row(target_kind="StatefulSet"))
        assert dep_x[6] == 0.0
        assert sts_x[6] == 1.0

    def test_outcome_ready_flag(self):
        ready_x = row_to_features(_make_row(outcome="READY"))
        esc_x = row_to_features(_make_row(outcome="ESCALATED"))
        assert ready_x[7] == 1.0
        assert esc_x[7] == 0.0

    def test_all_values_in_range(self):
        x = row_to_features(_make_row())
        assert np.all(x >= 0.0) and np.all(x <= 1.0)


# ---------------------------------------------------------------------------
# Label generation
# ---------------------------------------------------------------------------

class TestLabel:
    @pytest.mark.parametrize("state", ["RESOLVED", "VERIFYING", "EXECUTING"])
    def test_ready_correct_states(self, state):
        assert _label("READY", state) == 1

    @pytest.mark.parametrize("state", ["ESCALATED", "FAILED", "AWAITING_APPROVAL"])
    def test_ready_wrong_states(self, state):
        assert _label("READY", state) == 0

    @pytest.mark.parametrize("state", ["ESCALATED", "FAILED"])
    def test_escalated_correct_states(self, state):
        assert _label("ESCALATED", state) == 1

    @pytest.mark.parametrize("state", ["RESOLVED", "READY", "VERIFYING"])
    def test_escalated_wrong_states(self, state):
        assert _label("ESCALATED", state) == 0

    @pytest.mark.parametrize("state", ["ESCALATED", "RESOLVED", "FAILED", "READY"])
    def test_awaiting_approval_always_correct(self, state):
        assert _label("AWAITING_APPROVAL", state) == 1

    def test_case_insensitive(self):
        assert _label("ready", "resolved") == 1
        assert _label("ESCALATED", "escalated") == 1


# ---------------------------------------------------------------------------
# CSV loading
# ---------------------------------------------------------------------------

class TestLoadCsv:
    def test_basic_load(self, tmp_path):
        csv_path = _make_csv(tmp_path, [
            {"llm_confidence": "0.9", "grounding_score": "1.0", "runbook_score": "0.8",
             "confidence": "0.92", "risk_score": "0.45", "outcome": "READY",
             "state": "RESOLVED", "environment": "dev", "target_kind": "Deployment"},
            {"llm_confidence": "0.5", "grounding_score": "0.5", "runbook_score": "0",
             "confidence": "0.4", "risk_score": "1.0", "outcome": "ESCALATED",
             "state": "ESCALATED", "environment": "production", "target_kind": "StatefulSet"},
        ])
        X, y = load_csv(csv_path)
        assert X.shape == (2, len(FEATURE_NAMES))
        assert y.shape == (2,)
        assert y[0] == 1   # READY → RESOLVED = correct
        assert y[1] == 1   # ESCALATED → ESCALATED = correct

    def test_empty_risk_score_defaults_to_zero(self, tmp_path):
        csv_path = _make_csv(tmp_path, [
            {"llm_confidence": "0.9", "grounding_score": "1.0", "runbook_score": "0.8",
             "confidence": "0.92", "risk_score": "", "outcome": "READY",
             "state": "RESOLVED", "environment": "dev", "target_kind": "Deployment"},
        ])
        X, _ = load_csv(csv_path)
        assert X[0, 4] == 0.0  # risk_score index


# ---------------------------------------------------------------------------
# End-to-end training on synthetic data
# ---------------------------------------------------------------------------

class TestTraining:
    @pytest.fixture()
    def synthetic_csv(self, tmp_path):
        """Create 30 synthetic rows with two classes."""
        rng = np.random.default_rng(0)
        rows = []
        for i in range(30):
            correct = i % 2 == 0
            rows.append({
                "llm_confidence": str(0.95 if correct else 0.4),
                "grounding_score": str(1.0 if correct else 0.5),
                "runbook_score": str(0.8 if correct else 0.1),
                "confidence": str(0.9 if correct else 0.4),
                "risk_score": "0.45",
                "outcome": "READY",
                "state": "RESOLVED" if correct else "ESCALATED",
                "environment": "dev",
                "target_kind": "Deployment",
            })
        return _make_csv(tmp_path, rows)

    def test_train_returns_predictor(self, synthetic_csv):
        from app.ml.predictor import ConfidencePredictor
        predictor = train(synthetic_csv, verbose=False)
        assert isinstance(predictor, ConfidencePredictor)

    def test_predict_returns_valid_interval(self, synthetic_csv):
        predictor = train(synthetic_csv, verbose=False)
        pi = predictor.predict(
            llm_confidence=0.95,
            grounding_score=1.0,
            runbook_score=0.8,
            confidence=0.92,
            risk_score=0.45,
            environment="dev",
            target_kind="Deployment",
            outcome="READY",
        )
        assert 0.0 <= pi.lo <= pi.score <= pi.hi <= 1.0
        assert pi.width >= 0.0

    def test_low_confidence_gives_lower_score(self, synthetic_csv):
        predictor = train(synthetic_csv, verbose=False)
        high = predictor.predict(
            llm_confidence=0.95, grounding_score=1.0, runbook_score=0.8,
            confidence=0.92, risk_score=0.45,
            environment="dev", target_kind="Deployment", outcome="READY",
        )
        low = predictor.predict(
            llm_confidence=0.3, grounding_score=0.3, runbook_score=0.0,
            confidence=0.3, risk_score=0.45,
            environment="dev", target_kind="Deployment", outcome="READY",
        )
        assert high.score > low.score

    def test_save_and_load(self, synthetic_csv, tmp_path):
        from app.ml.predictor import ConfidencePredictor
        predictor = train(synthetic_csv, verbose=False)
        pkl_path = tmp_path / "model.pkl"
        predictor.save(pkl_path)
        loaded = ConfidencePredictor.load(pkl_path)
        pi1 = predictor.predict(
            llm_confidence=0.9, grounding_score=1.0, runbook_score=0.8,
            confidence=0.9, risk_score=0.45,
            environment="dev", target_kind="Deployment", outcome="READY",
        )
        pi2 = loaded.predict(
            llm_confidence=0.9, grounding_score=1.0, runbook_score=0.8,
            confidence=0.9, risk_score=0.45,
            environment="dev", target_kind="Deployment", outcome="READY",
        )
        assert abs(pi1.score - pi2.score) < 1e-6

    def test_batch_predict_shape(self, synthetic_csv):
        from app.ml.features import load_csv
        predictor = train(synthetic_csv, verbose=False)
        X, _ = load_csv(synthetic_csv)
        scores, lo, hi = predictor.predict_batch(X)
        assert scores.shape == (30,)
        assert lo.shape == (30,)
        assert hi.shape == (30,)
        assert np.all(lo <= scores + 1e-6)
        assert np.all(scores <= hi + 1e-6)
