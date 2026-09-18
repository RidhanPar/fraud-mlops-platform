"""Prediction log and label store.

Every scored transaction is written here with its inputs, score and model
version. The drift monitor reads it, and Phase 4 builds the audit trail on it.
Postgres in docker compose, SQLite in tests.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence

import pandas as pd
from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    Column,
    DateTime,
    Float,
    Integer,
    MetaData,
    String,
    Table,
    create_engine,
    insert,
    select,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB

metadata = MetaData()
_id = BigInteger().with_variant(Integer, "sqlite")
_json = JSON().with_variant(JSONB, "postgresql")

predictions = Table(
    "predictions",
    metadata,
    Column("id", _id, primary_key=True, autoincrement=True),
    Column("request_id", String(36), nullable=False, index=True),
    Column("transaction_id", String(64), index=True),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("model_version", String(32), nullable=False),
    Column("threshold", Float, nullable=False),
    Column("score", Float, nullable=False),
    Column("is_fraud", Boolean, nullable=False),
    Column("features", _json, nullable=False),
)

labels = Table(
    "labels",
    metadata,
    Column("id", _id, primary_key=True, autoincrement=True),
    Column("transaction_id", String(64), nullable=False, index=True),
    Column("is_fraud", Boolean, nullable=False),
    Column("labelled_at", DateTime(timezone=True), nullable=False),
)


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


class PredictionStore:
    def __init__(self, url: str) -> None:
        self.engine = create_engine(url, pool_pre_ping=True, pool_size=5, max_overflow=5) \
            if url.startswith("postgresql") else create_engine(url)
        with self.engine.begin() as conn:
            if self.engine.dialect.name == "postgresql":
                # Several uvicorn workers start at once; serialise table creation.
                conn.execute(text("SELECT pg_advisory_xact_lock(7331)"))
            metadata.create_all(conn)

    def record_predictions(
        self,
        request_id: str,
        model_version: str,
        threshold: float,
        rows: Sequence[dict],
        transaction_ids: Sequence[str | None],
        scores: Sequence[float],
    ) -> None:
        now = _now()
        records = [
            {
                "request_id": request_id,
                "transaction_id": tx,
                "created_at": now,
                "model_version": model_version,
                "threshold": threshold,
                "score": float(s),
                "is_fraud": bool(s >= threshold),
                "features": row,
            }
            for row, tx, s in zip(rows, transaction_ids, scores)
        ]
        with self.engine.begin() as conn:
            conn.execute(insert(predictions), records)

    def record_labels(self, items: Sequence[tuple[str, bool]]) -> None:
        now = _now()
        with self.engine.begin() as conn:
            conn.execute(
                insert(labels),
                [{"transaction_id": t, "is_fraud": y, "labelled_at": now} for t, y in items],
            )

    def recent_predictions(self, limit: int) -> pd.DataFrame:
        q = (
            select(predictions.c.score, predictions.c.is_fraud, predictions.c.features)
            .order_by(predictions.c.id.desc())
            .limit(limit)
        )
        with self.engine.connect() as conn:
            rows = conn.execute(q).all()
        if not rows:
            return pd.DataFrame()
        feats = pd.DataFrame.from_records([r.features for r in rows])
        feats["score"] = [r.score for r in rows]
        feats["is_fraud"] = [r.is_fraud for r in rows]
        return feats

    def recent_labelled(self, limit: int) -> pd.DataFrame:
        """Latest labels joined to the prediction that was made for that transaction."""
        q = (
            select(predictions.c.score, predictions.c.is_fraud, labels.c.is_fraud.label("label"))
            .join(labels, labels.c.transaction_id == predictions.c.transaction_id)
            .order_by(labels.c.id.desc())
            .limit(limit)
        )
        with self.engine.connect() as conn:
            return pd.DataFrame(conn.execute(q).all(), columns=["score", "is_fraud", "label"])
