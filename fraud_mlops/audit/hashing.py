"""Canonical hashing for audit records.

A record hash is computed at write time from the exact values stored. The
verifier recomputes it from what is in the database later, so any edit to a
logged decision (score, input, model version, time) changes the hash.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
from typing import Any

GENESIS = "0" * 64

HASHED_FIELDS = (
    "request_id",
    "transaction_id",
    "created_at",
    "caller",
    "model_version",
    "model_sha256",
    "threshold",
    "score",
    "is_fraud",
    "features",
)


def _utc_iso(value: dt.datetime) -> str:
    if value.tzinfo is None:  # SQLite drops the zone; values are always written in UTC
        value = value.replace(tzinfo=dt.timezone.utc)
    return value.astimezone(dt.timezone.utc).isoformat()


def canonical(record: dict[str, Any]) -> str:
    body = {k: record[k] for k in HASHED_FIELDS}
    body["created_at"] = _utc_iso(body["created_at"])
    body["score"] = float(body["score"])
    body["threshold"] = float(body["threshold"])
    body["is_fraud"] = bool(body["is_fraud"])
    return json.dumps(body, sort_keys=True, separators=(",", ":"))


def record_hash(record: dict[str, Any]) -> str:
    return hashlib.sha256(canonical(record).encode()).hexdigest()


def rows_digest(record_hashes: list[str]) -> str:
    return hashlib.sha256("".join(record_hashes).encode()).hexdigest()


def seal_hash(prev_seal: str, first_id: int, last_id: int, digest: str) -> str:
    return hashlib.sha256(f"{prev_seal}|{first_id}|{last_id}|{digest}".encode()).hexdigest()


def file_sha256(path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()
