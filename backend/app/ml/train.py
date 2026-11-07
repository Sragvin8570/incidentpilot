"""
Train the confidence calibration model.

Steps
-----
1. Load ``ml/data/decisions_raw.csv``  (exported from Postgres).
2. Build feature matrix X and binary label y.
3. Fit a LogisticRegression on all data (small dataset → no train/test split,
   but we report leave-one-out accuracy for honest evaluation).
4. Run 1 000 bootstrap resamplings to build the prediction interval ensemble.
5. Save the fitted ConfidencePredictor to ``ml/models/confidence_model.pkl``.
6. Print a quick evaluation report.

Run from repo root:
    python backend/app/ml/train.py
Or with a custom CSV:
    python backend/app/ml/train.py --data path/to/decisions.csv
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
from sklearn.linear_model import LogisticRegression
from sklearn.model_selection import LeaveOneOut, cross_val_score
from sklearn.metrics import brier_score_loss, log_loss

# Allow running as a script without installing the package.
_REPO_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(_REPO_ROOT / "backend"))

from app.ml.features import FEATURE_NAMES, load_csv  # noqa: E402
from app.ml.predictor import ConfidencePredictor, _N_BOOTSTRAP  # noqa: E402

_DEFAULT_CSV = _REPO_ROOT / "ml" / "data" / "decisions_raw.csv"
_RNG_SEED = 42


def _build_model() -> LogisticRegression:
    return LogisticRegression(
        solver="lbfgs",
        max_iter=1_000,
        C=1.0,                 # mild L2 regularisation — good for small datasets
        class_weight="balanced",
    )


def train(csv_path: Path, verbose: bool = True) -> ConfidencePredictor:
    """Train and return a fitted ConfidencePredictor."""
    if verbose:
        print(f"Loading data from {csv_path} …")

    X, y = load_csv(csv_path)
    n_samples, n_features = X.shape
    n_positive = int(y.sum())

    if verbose:
        print(f"  Samples : {n_samples}")
        print(f"  Features: {n_features}  ({', '.join(FEATURE_NAMES)})")
        print(f"  Correct (y=1): {n_positive}  ({n_positive/n_samples*100:.1f}%)")
        print(f"  Wrong   (y=0): {n_samples - n_positive}  ({(n_samples-n_positive)/n_samples*100:.1f}%)")
        print()

    # ------------------------------------------------------------------ #
    # Primary model (trained on all data)                                 #
    # ------------------------------------------------------------------ #
    model = _build_model()
    model.fit(X, y)

    # ------------------------------------------------------------------ #
    # Leave-one-out accuracy (honest evaluation on small dataset)         #
    # ------------------------------------------------------------------ #
    loo = LeaveOneOut()
    loo_scores = cross_val_score(_build_model(), X, y, cv=loo, scoring="accuracy")
    loo_probs = np.zeros(n_samples)
    for train_idx, test_idx in loo.split(X):
        m = _build_model().fit(X[train_idx], y[train_idx])
        loo_probs[test_idx] = m.predict_proba(X[test_idx])[:, 1]

    loo_brier = brier_score_loss(y, loo_probs)
    loo_logloss = log_loss(y, loo_probs)

    if verbose:
        print("-- Leave-One-Out evaluation ------------------------------")
        print(f"  Accuracy   : {loo_scores.mean():.3f} ± {loo_scores.std():.3f}")
        print(f"  Brier score: {loo_brier:.4f}  (0 = perfect, lower is better)")
        print(f"  Log-loss   : {loo_logloss:.4f}  (lower is better)")
        print()

    # ------------------------------------------------------------------ #
    # Feature importance (coefficients)                                   #
    # ------------------------------------------------------------------ #
    if verbose:
        print("-- Feature coefficients (logistic regression) ------------")
        coefs = model.coef_[0]
        for name, coef in sorted(zip(FEATURE_NAMES, coefs), key=lambda t: -abs(t[1])):
            bar = "█" * int(abs(coef) * 10)
            sign = "+" if coef >= 0 else "-"
            print(f"  {name:<22} {sign}{abs(coef):.4f}  {bar}")
        print()

    # ------------------------------------------------------------------ #
    # Bootstrap ensemble (1 000 resamplings for prediction intervals)     #
    # ------------------------------------------------------------------ #
    if verbose:
        print(f"Building bootstrap ensemble ({_N_BOOTSTRAP} resamplings) …", end="", flush=True)

    rng = np.random.default_rng(_RNG_SEED)
    bootstrap_models: list[LogisticRegression] = []
    for _ in range(_N_BOOTSTRAP):
        indices = rng.integers(0, n_samples, size=n_samples)
        X_boot, y_boot = X[indices], y[indices]
        # Need at least one sample of each class
        if len(np.unique(y_boot)) < 2:
            bootstrap_models.append(model)   # fall back to primary
            continue
        m = _build_model()
        m.fit(X_boot, y_boot)
        bootstrap_models.append(m)

    if verbose:
        print(" done.")
        print()

    # ------------------------------------------------------------------ #
    # Sample predictions with intervals                                   #
    # ------------------------------------------------------------------ #
    predictor = ConfidencePredictor(model=model, bootstrap_models=bootstrap_models)

    if verbose:
        print("-- Sample predictions on training data -------------------")
        scores, lo, hi = predictor.predict_batch(X)
        print(f"  {'Score':>6}  {'Lo':>6}  {'Hi':>6}  {'Width':>6}  {'True':>5}  {'outcome'}")
        shown = min(10, n_samples)
        for i in range(shown):
            print(
                f"  {scores[i]:.4f}  {lo[i]:.4f}  {hi[i]:.4f}  "
                f"{hi[i]-lo[i]:.4f}  {int(y[i]):>5}"
            )
        if n_samples > shown:
            print(f"  … ({n_samples - shown} more rows)")
        print()

    return predictor


def main() -> int:
    parser = argparse.ArgumentParser(description="Train the IncidentPilot confidence model.")
    parser.add_argument(
        "--data",
        type=Path,
        default=_DEFAULT_CSV,
        help="Path to decisions_raw.csv (default: ml/data/decisions_raw.csv)",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Where to save the model pkl (default: ml/models/confidence_model.pkl)",
    )
    args = parser.parse_args()

    if not args.data.exists():
        print(f"ERROR: Data file not found: {args.data}", file=sys.stderr)
        print("Export from Postgres first:", file=sys.stderr)
        print(
            "  docker exec <postgres-container> psql -U incidentpilot -d incidentpilot "
            "-c \"\\COPY (SELECT d.*, i.state, i.environment, i.target_kind FROM decisions d "
            "JOIN incidents i ON i.id=d.incident_id) TO '/tmp/decisions_raw.csv' WITH CSV HEADER\"",
            file=sys.stderr,
        )
        return 1

    predictor = train(args.data)
    save_path = args.output or (_REPO_ROOT / "ml" / "models" / "confidence_model.pkl")
    predictor.save(save_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
