"""Prediction log (the audit trail), labels, seals and explanations.

Every scored transaction is written here with its inputs, score, decision,
model version and the SHA-256 of the exact model file. The drift monitor
reads it, and the auditor seals it into a hash chain.

Postgres in docker compose (schema owned by an admin role, the app role can
only INSERT and SELECT, and triggers block UPDATE/DELETE). SQLite in tests.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterator, Sequence
from typing import Any

import pandas as pd
from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    MetaData,
    String,
    Table,
    create_engine,
    func,
    insert,
    select,
)
from sqlalchemy.dialects.postgresql import JSONB

from fraud_mlops.audit.hashing import record_hash

metadata = MetaData()
_id = BigInteger().with_variant(Integer, "sqlite")
_json = JSON().with_variant(JSONB, "postgresql")

predictions = Table(
    "predictions",
    metadata,
    Column("id", _id, primary_key=True, autoincrement=True),
    Column("request_id", String(36), nullable=False, index=True),
    Column("transaction_id", String(64), index=True),
    Column("created_at", DateTime(timezone=True), nullable=False, index=True),
    Column("caller", String(64), nullable=False),
    Column("model_version", String(32), nullable=False),
    Column("model_sha256", String(64), nullable=False),
    Column("threshold", Float, nullable=False),
    Column("score", Float, nullable=False),
    Column("is_fraud", Boolean, nullable=False),
    Column("features", _json, nullable=False),
    Column("record_hash", String(64), nullable=False),
)

labels = Table(
    "labels",
    metadata,
    Column("id", _id, primary_key=True, autoincrement=True),
    Column("transaction_id", String(64), nullable=False, index=True),
    Column("is_fraud", Boolean, nullable=False),
    Column("labelled_at", DateTime(timezone=True), nullable=False),
)

audit_seals = Table(
    "audit_seals",
    metadata,
    Column("id", _id, primary_key=True, autoincrement=True),
    Column("first_id", BigInteger, nullable=False),
    Column("last_id", BigInteger, nullable=False),
    Column("row_count", Integer, nullable=False),
    Column("rows_digest", String(64), nullable=False),
    Column("prev_seal_hash", String(64), nullable=False),
    Column("seal_hash", String(64), nullable=False),
    Column("sealed_at", DateTime(timezone=True), nullable=False),
)

explanations = Table(
    "explanations",
    metadata,
    Column("id", _id, primary_key=True, autoincrement=True),
    Column("prediction_id", BigInteger, ForeignKey("predictions.id"), nullable=False, index=True),
    Column("requested_at", DateTime(timezone=True), nullable=False),
    Column("requested_by", String(64), nullable=False),
    Column("model_version", String(32), nullable=False),
    Column("explanation", _json, nullable=False),
)

APPEND_ONLY_TABLES = ("predictions", "labels", "audit_seals", "explanations")


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


class PredictionStore:
    def __init__(self, url: str) -> None:
        if url.startswith("postgresql"):
            self.engine = create_engine(url, pool_pre_ping=True, pool_size=5, max_overflow=5)
        else:
            self.engine = create_engine(url)

    def create_schema(self) -> None:
        """Only the migration job (Postgres) or tests (SQLite) call this. The app
        role in Postgres has no CREATE privilege."""
        metadata.create_all(self.engine)

    # ---- writes -----------------------------------------------------------

    def record_predictions(
        self,
        request_id: str,
        caller: str,
        model_version: str,
        model_sha256: str,
        threshold: float,
        rows: Sequence[dict],
        transaction_ids: Sequence[str | None],
        scores: Sequence[float],
    ) -> None:
        now = utcnow()
        records = []
        for row, tx, s in zip(rows, transaction_ids, scores):
            rec = {
                "request_id": request_id,
                "transaction_id": tx,
                "created_at": now,
                "caller": caller,
                "model_version": model_version,
                "model_sha256": model_sha256,
                "threshold": float(threshold),
                "score": float(s),
                "is_fraud": bool(s >= threshold),
                "features": row,
            }
            rec["record_hash"] = record_hash(rec)
            records.append(rec)
        with self.engine.begin() as conn:
            conn.execute(insert(predictions), records)

    def record_labels(self, items: Sequence[tuple[str, bool]]) -> None:
        now = utcnow()
        with self.engine.begin() as conn:
            conn.execute(
                insert(labels),
                [{"transaction_id": t, "is_fraud": y, "labelled_at": now} for t, y in items],
            )

    def record_explanation(self, prediction_id: int, requested_by: str, model_version: str,
                           explanation: dict) -> None:
        with self.engine.begin() as conn:
            conn.execute(insert(explanations), {
                "prediction_id": prediction_id, "requested_at": utcnow(),
                "requested_by": requested_by, "model_version": model_version,
                "explanation": explanation,
            })

    # ---- audit reads ------------------------------------------------------

    def predictions_for_transaction(self, transaction_id: str) -> list[dict[str, Any]]:
        q = (select(predictions).where(predictions.c.transaction_id == transaction_id)
             .order_by(predictions.c.id))
        with self.engine.connect() as conn:
            return [dict(r._mapping) for r in conn.execute(q)]

    def explanations_for_prediction(self, prediction_id: int) -> list[dict[str, Any]]:
        q = (select(explanations).where(explanations.c.prediction_id == prediction_id)
             .order_by(explanations.c.id))
        with self.engine.connect() as conn:
            return [dict(r._mapping) for r in conn.execute(q)]

    def iter_predictions(self, after_id: int = 0, until: dt.datetime | None = None,
                         batch: int = 5000) -> Iterator[dict[str, Any]]:
        """Stream rows in id order, for sealing and verification."""
        last = after_id
        while True:
            q = select(predictions).where(predictions.c.id > last)
            if until is not None:
                q = q.where(predictions.c.created_at < until)
            with self.engine.connect() as conn:
                rows = conn.execute(q.order_by(predictions.c.id).limit(batch)).all()
            if not rows:
                return
            for r in rows:
                yield dict(r._mapping)
            last = rows[-1].id

    def seals(self) -> list[dict[str, Any]]:
        with self.engine.connect() as conn:
            return [dict(r._mapping) for r in conn.execute(select(audit_seals).order_by(audit_seals.c.id))]

    def append_seal(self, seal: dict[str, Any]) -> None:
        with self.engine.begin() as conn:
            conn.execute(insert(audit_seals), seal)

    def prediction_count(self) -> int:
        with self.engine.connect() as conn:
            return int(conn.execute(select(func.count()).select_from(predictions)).scalar_one())

    # ---- monitoring reads -------------------------------------------------

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
