"""
Feature engineering for the confidence calibration model.

Input: a row from the ``decisions`` table joined to ``incidents``.
Output: a 1-D numpy array of numeric features ready for scikit-learn.

Feature vector (8 dimensions, all in [0, 1]):
  0  llm_confidence      – LLM self-reported certainty
  1  grounding_score     – fraction of RCA claims backed by real evidence
  2  runbook_score       – max cosine-similarity to a matching runbook entry
  3  confidence          – current hand-tuned weighted composite (0.5*llm + 0.3*grnd + 0.2*run)
  4  risk_score          – policy risk score for the matched rule
  5  is_production       – 1 if environment == "production"/"prod", else 0
  6  is_statefulset      – 1 if target_kind == "StatefulSet", else 0
  7  outcome_is_ready    – 1 if the decision outcome was READY (auto-execute), else 0

Label (binary):
  ``correct`` = 1 if the decision outcome was validated by the incident's final state.
  Rules:
    - outcome READY   → correct iff state ∈ {RESOLVED, VERIFYING}
    - outcome ESCALATED → correct iff state ∈ {ESCALATED, FAILED}
    - outcome AWAITING_APPROVAL → always treated as "correct" (human in the loop)
"""

from __future__ import annotations

import csv
from pathlib import Path
from typing import NamedTuple

import numpy as np

# States that indicate a READY decision was validated (agent took action, things went well)
_READY_SUCCESS_STATES = {"RESOLVED", "VERIFYING", "EXECUTING"}
# States that indicate an ESCALATED decision was correct (the problem really was unsolvable)
_ESCALATED_SUCCESS_STATES = {"ESCALATED", "FAILED"}


class DecisionRow(NamedTuple):
    """A single decision record as read from the CSV export."""

    llm_confidence: float
    grounding_score: float
    runbook_score: float
    confidence: float
    risk_score: float
    outcome: str           # READY | ESCALATED | AWAITING_APPROVAL
    state: str             # final incident state
    environment: str       # dev | production | staging
    target_kind: str       # Deployment | StatefulSet | …


def _to_float(value: str, fallback: float = 0.0) -> float:
    try:
        return float(value)
    except (ValueError, TypeError):
        return fallback


def _is_production(environment: str) -> float:
    return 1.0 if environment.strip().lower() in {"prod", "production"} else 0.0


def _is_statefulset(target_kind: str) -> float:
    return 1.0 if target_kind.strip().lower() == "statefulset" else 0.0


def _is_ready(outcome: str) -> float:
    return 1.0 if outcome.strip().upper() == "READY" else 0.0


def _label(outcome: str, state: str) -> int:
    """
    1 = decision was correct given what actually happened.
    0 = decision was wrong.

    AWAITING_APPROVAL decisions always count as correct (human review is
    the right policy, regardless of how the incident ended).
    """
    outcome = outcome.strip().upper()
    state = state.strip().upper()
    if outcome == "AWAITING_APPROVAL":
        return 1
    if outcome == "READY":
        return 1 if state in _READY_SUCCESS_STATES else 0
    if outcome == "ESCALATED":
        return 1 if state in _ESCALATED_SUCCESS_STATES else 0
    return 0  # unknown outcome


def row_to_features(row: DecisionRow) -> np.ndarray:
    """Return an (8,) float32 feature vector from a DecisionRow."""
    return np.array(
        [
            row.llm_confidence,
            row.grounding_score,
            row.runbook_score,
            row.confidence,
            row.risk_score,
            _is_production(row.environment),
            _is_statefulset(row.target_kind),
            _is_ready(row.outcome),
        ],
        dtype=np.float32,
    )


FEATURE_NAMES = [
    "llm_confidence",
    "grounding_score",
    "runbook_score",
    "confidence",
    "risk_score",
    "is_production",
    "is_statefulset",
    "outcome_is_ready",
]


def load_csv(path: Path) -> tuple[np.ndarray, np.ndarray]:
    """
    Load ``decisions_raw.csv`` (exported from Postgres) and return
    ``(X, y)`` where X has shape ``(n_samples, 8)`` and y has shape ``(n_samples,)``.
    """
    rows: list[DecisionRow] = []
    labels: list[int] = []

    with path.open(newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        for record in reader:
            dr = DecisionRow(
                llm_confidence=_to_float(record.get("llm_confidence", "0")),
                grounding_score=_to_float(record.get("grounding_score", "0")),
                runbook_score=_to_float(record.get("runbook_score", "0")),
                confidence=_to_float(record.get("confidence", "0")),
                risk_score=_to_float(record.get("risk_score", "0")),
                outcome=record.get("outcome", ""),
                state=record.get("state", ""),
                environment=record.get("environment", ""),
                target_kind=record.get("target_kind", ""),
            )
            rows.append(dr)
            labels.append(_label(dr.outcome, dr.state))

    X = np.vstack([row_to_features(r) for r in rows]).astype(np.float32)
    y = np.array(labels, dtype=np.int32)
    return X, y
