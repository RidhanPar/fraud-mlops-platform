"""Metrics used for model selection and for the promotion gate."""

from __future__ import annotations

import numpy as np
from sklearn.metrics import (
    average_precision_score,
    f1_score,
    precision_recall_curve,
    precision_score,
    recall_score,
    roc_auc_score,
)


def pick_threshold(y_true: np.ndarray, scores: np.ndarray) -> float:
    """Threshold that maximises F1 on the validation slice (never the holdout)."""
    precision, recall, thresholds = precision_recall_curve(y_true, scores)
    f1 = 2 * precision * recall / np.clip(precision + recall, 1e-12, None)
    # The last precision/recall pair has no threshold attached.
    return float(thresholds[int(np.argmax(f1[:-1]))])


def classification_metrics(
    y_true: np.ndarray, scores: np.ndarray, threshold: float
) -> dict[str, float]:
    y_pred = (scores >= threshold).astype(int)
    return {
        "pr_auc": float(average_precision_score(y_true, scores)),
        "roc_auc": float(roc_auc_score(y_true, scores)),
        "precision": float(precision_score(y_true, y_pred, zero_division=0)),
        "recall": float(recall_score(y_true, y_pred, zero_division=0)),
        "f1": float(f1_score(y_true, y_pred, zero_division=0)),
        "alert_rate": float(y_pred.mean()),
    }


def paired_bootstrap_delta(
    y_true: np.ndarray,
    cand_scores: np.ndarray,
    champ_scores: np.ndarray,
    rounds: int,
    seed: int,
) -> tuple[float, float]:
    """95% interval for (candidate PR AUC minus champion PR AUC).

    Both models are scored on the same resampled rows each round, so the
    interval reflects the difference between them, not the noise of each.
    With only ~100 frauds in the holdout, this is the honest way to say
    whether a small gain is real.
    """
    rng = np.random.default_rng(seed)
    n = len(y_true)
    deltas = []
    for _ in range(rounds):
        idx = rng.integers(0, n, n)
        yt = y_true[idx]
        if yt.sum() == 0:
            continue
        deltas.append(
            average_precision_score(yt, cand_scores[idx])
            - average_precision_score(yt, champ_scores[idx])
        )
    lo, hi = np.percentile(deltas, [2.5, 97.5])
    return float(lo), float(hi)
