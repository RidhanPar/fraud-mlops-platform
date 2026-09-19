"""Race test for the sealer on real Postgres.

16 writer threads insert predictions with a random delay between the INSERT
(when the id is drawn) and the COMMIT (when the row becomes visible), while a
sealer seals every 100 ms. Afterwards the verifier checks that no row landed
inside an already sealed range.

    python scripts/seal_race_test.py --mode barrier   # the fix
    python scripts/seal_race_test.py --mode time      # the old 10 s time cut off, for comparison

Uses the local docker compose database (append only: rows and seals stay).
"""

from __future__ import annotations

import argparse
import datetime as dt
import random
import sys
import threading
import time
import uuid
from pathlib import Path

from sqlalchemy import create_engine, insert, select, text

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fraud_mlops.api.store import SEAL_BARRIER, PredictionStore, predictions, utcnow  # noqa: E402
from fraud_mlops.audit import seal as seal_mod  # noqa: E402
from fraud_mlops.audit.hashing import GENESIS, record_hash, rows_digest, seal_hash  # noqa: E402
from fraud_mlops.audit.verify import verify  # noqa: E402
from fraud_mlops.features import RAW_COLUMNS  # noqa: E402

URL = "postgresql+psycopg://fraud_app:app_local_only@127.0.0.1:5434/fraud"


def writer(store: PredictionStore, barrier: bool, stop: threading.Event, max_delay: float) -> None:
    rng = random.Random()
    while not stop.is_set():
        rec = {"request_id": str(uuid.uuid4()), "transaction_id": None, "created_at": utcnow(),
               "caller": "race-test", "model_version": "race", "model_sha256": "0" * 64,
               "threshold": 0.5, "score": rng.random(), "features": {c: rng.random() for c in RAW_COLUMNS}}
        rec["is_fraud"] = rec["score"] >= 0.5
        rec["record_hash"] = record_hash(rec)
        with store.engine.begin() as c:
            if barrier:
                c.execute(text("SELECT pg_advisory_xact_lock_shared(:k)"), {"k": SEAL_BARRIER})
            c.execute(insert(predictions), [rec])
            c.execute(text("SELECT pg_sleep(:s)"), {"s": rng.uniform(0, max_delay)})  # slow commit


def seal_by_time(store: PredictionStore, settle: float) -> None:
    """The original algorithm: seal rows stamped more than `settle` seconds ago."""
    seals = store.seals()
    after = seals[-1]["last_id"] if seals else 0
    prev = seals[-1]["seal_hash"] if seals else GENESIS
    until = utcnow() - dt.timedelta(seconds=settle)
    with store.engine.connect() as c:
        rows = c.execute(select(predictions.c.id, predictions.c.record_hash)
                         .where(predictions.c.id > after, predictions.c.created_at < until)
                         .order_by(predictions.c.id)).all()
    if rows:
        d = rows_digest([r.record_hash for r in rows])
        store.append_seal({"first_id": rows[0].id, "last_id": rows[-1].id, "row_count": len(rows),
                           "rows_digest": d, "prev_seal_hash": prev,
                           "seal_hash": seal_hash(prev, rows[0].id, rows[-1].id, d), "sealed_at": utcnow()})


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["barrier", "time"], required=True)
    ap.add_argument("--seconds", type=float, default=20)
    ap.add_argument("--writers", type=int, default=16)
    ap.add_argument("--max-commit-delay", type=float, default=0.3)
    ap.add_argument("--settle", type=float, default=0.1, help="time mode only; scaled down from 10 s")
    args = ap.parse_args()

    store = PredictionStore(URL)
    # One connection per writer plus headroom, so the sealer is never starved of a
    # connection (that would test the pool, not the sealing logic).
    store.engine = create_engine(URL, pool_size=args.writers + 4, max_overflow=0)
    seal_mod.seal_once(store)  # start from a fully sealed log
    before = set(verify(store).problems)  # problems that predate this run
    start_seals = len(store.seals())
    stop = threading.Event()
    threads = [threading.Thread(target=writer, args=(store, args.mode == "barrier", stop, args.max_commit_delay),
                                daemon=True) for _ in range(args.writers)]
    for t in threads:
        t.start()
    end = time.time() + args.seconds
    try:
        while time.time() < end:
            seal_mod.seal_once(store) if args.mode == "barrier" else seal_by_time(store, args.settle)
            time.sleep(0.1)
    finally:
        stop.set()
    for t in threads:
        t.join()

    r = verify(store)
    new = [p for p in r.problems if p not in before]
    print(f"mode={args.mode} writers={args.writers} seals made={len(store.seals()) - start_seals} "
          f"rows checked={r.rows_checked} unsealed tail={r.unsealed_rows}")
    print(f"new verification problems: {len(new)}")
    for p in new[:5]:
        print("  -", p)


if __name__ == "__main__":
    main()
