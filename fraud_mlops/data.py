"""Load, validate, fingerprint and split the transaction data."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

from fraud_mlops.features import RAW_COLUMNS, TARGET


@dataclass
class Splits:
    train: pd.DataFrame
    valid: pd.DataFrame
    holdout: pd.DataFrame


def file_sha256(path: str | Path) -> str:
    """Fingerprint of the exact bytes a model was trained on."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def validate(df: pd.DataFrame) -> pd.DataFrame:
    missing = set(RAW_COLUMNS + [TARGET]) - set(df.columns)
    if missing:
        raise ValueError(f"Missing columns: {sorted(missing)}")
    if df[RAW_COLUMNS].isna().any().any():
        raise ValueError("Null values in feature columns")
    if not set(df[TARGET].unique()) <= {0, 1}:
        raise ValueError("Target must be binary 0/1")
    if (df["Amount"] < 0).any():
        raise ValueError("Negative transaction amount")
    return df


def load(path: str | Path) -> pd.DataFrame:
    df = pd.read_csv(path)
    validate(df)
    # Duplicates would let the same transaction appear in train and holdout.
    return df.drop_duplicates().reset_index(drop=True)


def time_split(df: pd.DataFrame, train_frac: float, valid_frac: float) -> Splits:
    """Split by time, never randomly: the model is judged on the future."""
    df = df.sort_values("Time", kind="mergesort").reset_index(drop=True)
    n = len(df)
    a = int(n * train_frac)
    b = int(n * (train_frac + valid_frac))
    return Splits(train=df.iloc[:a], valid=df.iloc[a:b], holdout=df.iloc[b:])
