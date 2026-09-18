"""Feature contract shared by training and serving.

Keeping this in one module means the service can never compute a feature
differently from the training pipeline (training/serving skew).
"""

from __future__ import annotations

import pandas as pd

PCA_COLUMNS = [f"V{i}" for i in range(1, 29)]
RAW_COLUMNS = ["Time", *PCA_COLUMNS, "Amount"]
TARGET = "Class"
MODEL_COLUMNS = [*PCA_COLUMNS, "Amount", "hour_of_day"]

SECONDS_PER_DAY = 86_400


def add_time_features(df: pd.DataFrame) -> pd.DataFrame:
    """Replace raw `Time` with hour of day.

    In the source data `Time` is seconds since the first transaction in the
    capture window. A live transaction has no such value, and a time ordered
    holdout would sit entirely outside the training range. Hour of day is
    available at scoring time and keeps the daily fraud pattern.
    """
    out = df[RAW_COLUMNS].copy()
    out["hour_of_day"] = (out["Time"] % SECONDS_PER_DAY) / 3600.0
    return out[MODEL_COLUMNS]
