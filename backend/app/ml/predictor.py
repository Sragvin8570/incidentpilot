"""
Confidence calibration model with bootstrap prediction intervals.

Model: Logistic Regression trained on the 8 decision features.
Interval: 1000-sample bootstrap gives a 95% confidence interval around P(correct).

Usage
-----
    from app.ml.predictor import ConfidencePredictor

    predictor = ConfidencePredictor.load()          # loads from models/confidence_model.pkl
    score, lo, hi = predictor.predict(
        llm_confidence=0.95,
        grounding_score=1.0,
        runbook_score=0.73,
        confidence=0.906,                           # current composite score
        risk_score=0.45,
        environment="dev",
        target_kind="Deployment",
        outcome="READY",
    )
    # score = 0.88, lo = 0.72, hi = 0.97  (95% CI from bootstrap)
"""

from __future__ import annotations

import pickle
from dataclasses import dataclass
from pathlib import Path
from typing import NamedTuple

import numpy as np
from sklearn.linear_model import LogisticRegression

from app.ml.features import FEATURE_NAMES, DecisionRow, load_csv, row_to_features

_MODEL_PATH = Path(__file__).resolve().parents[3] / "ml" / "models" / "confidence_model.pkl"
_N_BOOTSTRAP = 1_000
_CI_ALPHA = 0.05  # 95% interval


class PredictionInterval(NamedTuple):
    """Point estimate + bootstrap confidence interval for P(decision is correct)."""

    score: float   # mean predicted probability from bootstrap
    lo: float      # lower bound (alpha/2 percentile)
    hi: float      # upper bound (1 - alpha/2 percentile)
    width: float   # hi - lo  (narrower = model is more certain)


@dataclass
class ConfidencePredictor:
    """
    Wraps a fitted scikit-learn LogisticRegression and bootstrap ensemble
    so callers get a point estimate plus a 95% prediction interval.

    Attributes
    ----------
    model : LogisticRegression
        Primary model trained on the full dataset.
    bootstrap_models : list[LogisticRegression]
        ``_N_BOOTSTRAP`` models, each fitted on a resampled version of the
        training data.  Used to derive the interval.
    """

    model: LogisticRegression
    bootstrap_models: list[LogisticRegression]

    # ------------------------------------------------------------------
    # Serialisation
    # ------------------------------------------------------------------

    def save(self, path: Path = _MODEL_PATH) -> None:
        """Persist the predictor to disk."""
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("wb") as fh:
            pickle.dump(self, fh)
        print(f"Model saved → {path}")

    @classmethod
    def load(cls, path: Path = _MODEL_PATH) -> "ConfidencePredictor":
        """Load a previously saved predictor."""
        if not path.exists():
            raise FileNotFoundError(
                f"No trained model found at {path}. "
                "Run `python backend/app/ml/train.py` first."
            )
        with path.open("rb") as fh:
            obj = pickle.load(fh)
        if not isinstance(obj, cls):
            raise TypeError(f"Expected {cls.__name__}, got {type(obj).__name__}")
        return obj

    # ------------------------------------------------------------------
    # Prediction
    # ------------------------------------------------------------------

    def predict(
        self,
        *,
        llm_confidence: float,
        grounding_score: float,
        runbook_score: float,
        confidence: float,
        risk_score: float,
        environment: str,
        target_kind: str,
        outcome: str,
    ) -> PredictionInterval:
        """
        Return a PredictionInterval for the given decision features.

        Parameters mirror the Decision DB columns so callers in policy.py
        can pass them without any additional transformation.
        """
        dr = DecisionRow(
            llm_confidence=llm_confidence,
            grounding_score=grounding_score,
            runbook_score=runbook_score,
            confidence=confidence,
            risk_score=risk_score,
            outcome=outcome,
            state="",          # state is unknown at prediction time
            environment=environment,
            target_kind=target_kind,
        )
        x = row_to_features(dr).reshape(1, -1)

        # Bootstrap distribution of predicted probabilities
        boot_probs = np.array(
            [m.predict_proba(x)[0, 1] for m in self.bootstrap_models]
        )
        lo = float(np.percentile(boot_probs, 100 * _CI_ALPHA / 2))
        hi = float(np.percentile(boot_probs, 100 * (1 - _CI_ALPHA / 2)))
        score = float(self.model.predict_proba(x)[0, 1])

        return PredictionInterval(score=score, lo=lo, hi=hi, width=hi - lo)

    def predict_batch(self, X: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        """
        Batch prediction for evaluation / plotting.

        Returns (scores, lo, hi) each of shape (n_samples,).
        """
        scores = self.model.predict_proba(X)[:, 1]
        boot_matrix = np.column_stack(
            [m.predict_proba(X)[:, 1] for m in self.bootstrap_models]
        )
        lo = np.percentile(boot_matrix, 100 * _CI_ALPHA / 2, axis=1)
        hi = np.percentile(boot_matrix, 100 * (1 - _CI_ALPHA / 2), axis=1)
        return scores, lo, hi
