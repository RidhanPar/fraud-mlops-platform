"""Try to rewrite a logged fraud decision, three ways, against the running stack.

1. As the application role     -> refused by privileges (no UPDATE grant)
2. As the database admin       -> refused by the append only trigger
3. As an admin who disables the trigger and also recomputes the row hash
                               -> the edit lands, and the verifier catches it via the seal chain

The original values are then restored (the demo database stays usable), and the
verifier passes again, which shows the hashes cover content, not timestamps of edits.

    python scripts/audit_tamper_demo.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from sqlalchemy import create_engine, text

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fraud_mlops.api.store import PredictionStore  # noqa: E402
from fraud_mlops.audit.hashing import record_hash  # noqa: E402
from fraud_mlops.audit.verify import verify  # noqa: E402

APP = "postgresql+psycopg://fraud_app:app_local_only@127.0.0.1:5434/fraud"
ADMIN = "postgresql+psycopg://fraud:fraud@127.0.0.1:5434/fraud"
OUT = Path(__file__).resolve().parents[1] / "docs/audit/tamper_demo.json"


DISABLE = "ALTER TABLE predictions DISABLE TRIGGER predictions_append_only"
ENABLE = "ALTER TABLE predictions ENABLE TRIGGER predictions_append_only"


def attempt(url: str, statements: list[str], params: dict) -> str:
    """Run the statements in one transaction; report success or the database's refusal."""
    try:
        with create_engine(url).begin() as c:
            for sql in statements:
                c.execute(text(sql), params)
        return "succeeded"
    except Exception as exc:
        return "refused: " + str(getattr(exc, "orig", exc)).splitlines()[0]


def main() -> None:
    store = PredictionStore(APP)
    results: dict = {}

    before = verify(store)
    results["verify_before"] = {"ok": before.ok, "rows": before.rows_checked, "seals": before.seals_checked}

    last_sealed = store.seals()[-1]["last_id"]
    with store.engine.connect() as c:
        target_id = c.execute(text(
            "SELECT id FROM predictions WHERE is_fraud AND id <= :m ORDER BY id DESC LIMIT 1"),
            {"m": last_sealed}).scalar_one()
    original = next(r for r in store.iter_predictions(after_id=target_id - 1) if r["id"] == target_id)
    results["target"] = {"prediction_id": target_id, "transaction_id": original["transaction_id"],
                         "score": original["score"], "decision": "fraud"}

    edit = "UPDATE predictions SET score = 0.01, is_fraud = false WHERE id = :id"
    results["1_app_role_update"] = attempt(APP, [edit], {"id": target_id})
    results["2_admin_update"] = attempt(ADMIN, [edit], {"id": target_id})

    forged = dict(original, score=0.01, is_fraud=False)
    forged_hash = record_hash(forged)
    results["3_admin_disables_trigger_and_forges_hash"] = attempt(ADMIN, [
        DISABLE,
        "UPDATE predictions SET score = 0.01, is_fraud = false, record_hash = :h WHERE id = :id",
        ENABLE,
    ], {"h": forged_hash, "id": target_id})

    after = verify(store)
    results["verify_after_forgery"] = {"ok": after.ok, "problems": after.problems}

    attempt(ADMIN, [
        DISABLE,
        "UPDATE predictions SET score = :s, is_fraud = true, record_hash = :h WHERE id = :id",
        ENABLE,
    ], {"s": original["score"], "h": original["record_hash"], "id": target_id})
    restored = verify(store)
    results["verify_after_restore"] = {"ok": restored.ok, "problems": restored.problems}

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(results, indent=2), encoding="utf-8")
    print(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()
