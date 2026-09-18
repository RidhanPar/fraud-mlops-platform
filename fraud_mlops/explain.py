"""Per decision explanations with exact TreeSHAP.

Uses XGBoost's built in `pred_contribs`, which runs the same TreeSHAP
algorithm as the `shap` library (a test checks they agree exactly) without
adding shap and numba to the serving image.

Contributions are in log odds. They add up exactly:
    base_value + sum(contributions) = log(p / (1 - p))
so the explanation accounts for the whole score, not an approximation of it.
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd
import xgboost as xgb

from fraud_mlops.features import RAW_COLUMNS


def explain(pipeline, row: dict[str, float], top: int = 10) -> dict:
    features = pipeline.named_steps["features"].transform(pd.DataFrame([row], columns=RAW_COLUMNS))
    booster = pipeline.named_steps["xgb"].get_booster()
    contribs = booster.predict(xgb.DMatrix(features), pred_contribs=True)[0]
    base, values = float(contribs[-1]), contribs[:-1]
    margin = base + float(values.sum())
    score = 1.0 / (1.0 + math.exp(-margin))

    order = np.argsort(-np.abs(values))
    ranked = [
        {
            "feature": features.columns[i],
            "value": float(features.iloc[0, i]),
            "contribution": float(values[i]),
            "direction": "towards fraud" if values[i] > 0 else "towards legitimate",
        }
        for i in order
    ]
    return {
        "method": "TreeSHAP (xgboost pred_contribs), log odds",
        "base_value": base,
        "score_from_contributions": score,
        "top_contributions": ranked[:top],
        "other_features_total": float(sum(r["contribution"] for r in ranked[top:])),
    }
