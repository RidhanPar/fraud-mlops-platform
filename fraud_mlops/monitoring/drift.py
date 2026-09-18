"""Drift statistics: live window vs the training reference sample.

PSI (Population Stability Index) is the main signal. It is an effect size, so
unlike a KS p value it does not flag trivial differences just because the
window is large. Common reading: < 0.1 stable, 0.1 to 0.25 moderate, > 0.25 major.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
from scipy.stats import ks_2samp

from fraud_mlops.features import PCA_COLUMNS

# hour_of_day is left out on purpose: any short live window covers a few hours
# while the reference covers whole days, so it would "drift" all the time.
MONITORED_FEATURES = [*PCA_COLUMNS, "Amount"]
EPS = 1e-4


def _edges(reference: np.ndarray, bins: int) -> np.ndarray:
    qs = np.quantile(reference, np.linspace(0, 1, bins + 1)[1:-1])
    return np.concatenate([[-np.inf], np.unique(qs), [np.inf]])


def psi(reference: np.ndarray, current: np.ndarray, bins: int = 10) -> float:
    """Bins are reference deciles, so each bin holds ~10% of training data."""
    edges = _edges(reference, bins)
    ref = np.histogram(reference, edges)[0] / len(reference)
    cur = np.histogram(current, edges)[0] / max(len(current), 1)
    ref = np.clip(ref, EPS, None)
    cur = np.clip(cur, EPS, None)
    return float(np.sum((cur - ref) * np.log(cur / ref)))


def feature_drift(reference: pd.DataFrame, current: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for col in MONITORED_FEATURES:
        r, c = reference[col].to_numpy(), current[col].to_numpy()
        rows.append(
            {
                "feature": col,
                "psi": psi(r, c),
                "ks_stat": float(ks_2samp(r, c).statistic),
                "ref_mean": float(r.mean()),
                "cur_mean": float(c.mean()),
            }
        )
    return pd.DataFrame(rows).sort_values("psi", ascending=False, ignore_index=True)


def score_drift(ref_scores: np.ndarray, cur_scores: np.ndarray, threshold: float) -> dict[str, float]:
    # Scores pile up near 0, so compare on the log odds scale where deciles are distinct.
    def logit(p):
        p = np.clip(p, 1e-7, 1 - 1e-7)
        return np.log(p / (1 - p))

    return {
        "score_psi": psi(logit(ref_scores), logit(cur_scores)),
        "ref_alert_rate": float((ref_scores >= threshold).mean()),
        "cur_alert_rate": float((cur_scores >= threshold).mean()),
        "cur_mean_score": float(cur_scores.mean()),
    }


def mode_share(values: np.ndarray) -> float:
    """Share of rows holding the single most common exact value.

    Continuous features almost never repeat exactly, so a high share means a
    default or fill value is being sent: an upstream data fault, not drift.
    """
    if len(values) == 0:
        return float("nan")
    _, counts = np.unique(values, return_counts=True)
    return float(counts.max() / len(values))
