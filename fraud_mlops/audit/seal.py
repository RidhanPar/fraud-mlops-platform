"""Auditor: seal new prediction log rows into a hash chain.

Each seal covers a contiguous id range and stores
    rows_digest = sha256(record hashes of those rows, in id order)
    seal_hash   = sha256(previous seal_hash | first_id | last_id | rows_digest)

Editing, deleting or inserting a row inside a sealed range changes its digest,
and rewriting a seal breaks every seal after it. Sealing runs off the request
path, so API workers never contend for a global chain lock.

In production each seal_hash would also be written somewhere the database
admin cannot change (S3 Object Lock, a transparency log). Here it is kept in
the audit_seals table, which is itself append only.

    python -m fraud_mlops.audit.seal            # one pass
    python -m fraud_mlops.audit.seal --loop     # auditor service
"""

from __future__ import annotations

import argparse
import datetime as dt
import logging
import os
import time

from fraud_mlops.api.store import PredictionStore, utcnow
from fraud_mlops.audit.hashing import GENESIS, rows_digest, seal_hash

log = logging.getLogger("auditor")

# Rows younger than this are left for the next pass, so a slow commit with a
# lower id cannot land inside a range that has already been sealed.
SETTLE_SECONDS = 10


def seal_once(store: PredictionStore, max_rows: int = 50_000,
              settle_seconds: float = SETTLE_SECONDS) -> dict | None:
    seals = store.seals()
    after = seals[-1]["last_id"] if seals else 0
    prev = seals[-1]["seal_hash"] if seals else GENESIS
    until = utcnow() - dt.timedelta(seconds=settle_seconds)

    ids, hashes = [], []
    for row in store.iter_predictions(after_id=after, until=until):
        ids.append(row["id"])
        hashes.append(row["record_hash"])
        if len(ids) >= max_rows:
            break
    if not ids:
        return None

    digest = rows_digest(hashes)
    seal = {
        "first_id": ids[0],
        "last_id": ids[-1],
        "row_count": len(ids),
        "rows_digest": digest,
        "prev_seal_hash": prev,
        "seal_hash": seal_hash(prev, ids[0], ids[-1], digest),
        "sealed_at": utcnow(),
    }
    store.append_seal(seal)
    return seal


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--loop", action="store_true")
    ap.add_argument("--interval", type=float, default=float(os.environ.get("SEAL_INTERVAL_SECONDS", "30")))
    args = ap.parse_args()
    store = PredictionStore(os.environ["DATABASE_URL"])
    max_rows = 50_000
    while True:
        try:
            # Keep going only while chunks come back full (a backlog). Otherwise the
            # pass would chase rows as they age past the settle window, one at a time.
            while (seal := seal_once(store, max_rows=max_rows)) is not None:
                log.info("sealed ids %s..%s (%s rows) %s", seal["first_id"], seal["last_id"],
                         seal["row_count"], seal["seal_hash"][:16])
                if seal["row_count"] < max_rows:
                    break
        except Exception:
            log.exception("seal pass failed")
        if not args.loop:
            return
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
