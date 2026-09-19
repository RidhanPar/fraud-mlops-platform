"""Explain a logged decision made by ANY model version, not just the one serving.

Loads the model version named in the audit record from the MLflow registry,
checks the file's SHA-256 matches the hash logged with the decision (proving
these are the exact bytes that made it), then explains the logged input.

    DATABASE_URL=... python -m fraud_mlops.audit.explain_cli --transaction-id sim-1-42
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

import cloudpickle
import mlflow

from fraud_mlops.api.store import PredictionStore
from fraud_mlops.audit.hashing import file_sha256, record_hash
from fraud_mlops.config import load_params
from fraud_mlops.explain import explain
from fraud_mlops.train import tracking_uri
from fraud_mlops.dburl import app_database_url


def explain_logged(store: PredictionStore, transaction_id: str, top: int = 10) -> dict:
    records = store.predictions_for_transaction(transaction_id)
    if not records:
        raise SystemExit(f"no logged prediction for transaction {transaction_id}")
    rec = records[-1]
    if record_hash(rec) != rec["record_hash"]:
        raise SystemExit(f"prediction {rec['id']} fails its integrity check, refusing to explain it")

    mlflow.set_tracking_uri(tracking_uri())
    name = load_params()["registry"]["model_name"]
    model_dir = Path(mlflow.artifacts.download_artifacts(f"models:/{name}/{rec['model_version']}"))
    sha = file_sha256(model_dir / "model.pkl")
    if sha != rec["model_sha256"]:
        raise SystemExit(f"registry v{rec['model_version']} sha {sha[:12]} does not match the logged "
                         f"sha {rec['model_sha256'][:12]}; cannot prove it is the same model")
    with open(model_dir / "model.pkl", "rb") as f:
        pipeline = cloudpickle.load(f)

    out = explain(pipeline, rec["features"], top=top)
    out.update(
        transaction_id=transaction_id,
        prediction_id=rec["id"],
        decided_at=rec["created_at"].isoformat(),
        model_version=rec["model_version"],
        model_sha256=rec["model_sha256"],
        logged_score=rec["score"],
        threshold=rec["threshold"],
        decision="fraud" if rec["is_fraud"] else "legitimate",
    )
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--transaction-id", required=True)
    ap.add_argument("--top", type=int, default=10)
    args = ap.parse_args()
    store = PredictionStore(app_database_url())
    print(json.dumps(explain_logged(store, args.transaction_id, args.top), indent=2))


if __name__ == "__main__":
    main()
