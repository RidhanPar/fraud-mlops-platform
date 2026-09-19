"""Verify the audit trail end to end.

1. Every row's stored hash matches a hash recomputed from its current values.
2. Every seal's digest matches the rows now in its id range (catches edits,
   deletions and insertions, even if the attacker also rewrote record_hash).
3. The seal chain links unbroken from the genesis value.

    python -m fraud_mlops.audit.verify
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field

from fraud_mlops.api.store import PredictionStore
from fraud_mlops.audit.hashing import GENESIS, record_hash, rows_digest, seal_hash
from fraud_mlops.dburl import app_database_url


@dataclass
class VerifyReport:
    rows_checked: int = 0
    seals_checked: int = 0
    unsealed_rows: int = 0
    problems: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems


def verify(store: PredictionStore, max_problems: int = 50) -> VerifyReport:
    report = VerifyReport()
    seals = store.seals()

    prev = GENESIS
    for s in seals:
        if s["prev_seal_hash"] != prev:
            report.problems.append(f"seal {s['id']}: chain broken (prev hash does not match seal {s['id'] - 1})")
        if seal_hash(s["prev_seal_hash"], s["first_id"], s["last_id"], s["rows_digest"]) != s["seal_hash"]:
            report.problems.append(f"seal {s['id']}: seal hash does not match its contents")
        prev = s["seal_hash"]
    report.seals_checked = len(seals)

    si = 0
    current: list[str] = []

    def close_seal(s: dict) -> None:
        if len(current) != s["row_count"]:
            report.problems.append(
                f"seal {s['id']}: expected {s['row_count']} rows in ids {s['first_id']}..{s['last_id']}, found {len(current)}")
        elif rows_digest(current) != s["rows_digest"]:
            report.problems.append(f"seal {s['id']}: rows in ids {s['first_id']}..{s['last_id']} changed since sealing")

    for row in store.iter_predictions():
        report.rows_checked += 1
        recomputed = record_hash(row)
        if recomputed != row["record_hash"] and len(report.problems) < max_problems:
            report.problems.append(f"prediction {row['id']}: content does not match its record hash")

        while si < len(seals) and row["id"] > seals[si]["last_id"]:
            close_seal(seals[si])
            current = []
            si += 1
        if si >= len(seals):
            report.unsealed_rows += 1
        elif row["id"] >= seals[si]["first_id"]:
            current.append(recomputed)
        else:
            report.problems.append(f"prediction {row['id']}: sits between sealed ranges, added after sealing")

    while si < len(seals):
        close_seal(seals[si])
        current = []
        si += 1
    return report


def main() -> None:
    store = PredictionStore(app_database_url())
    r = verify(store)
    bucket = os.environ.get("SEAL_ANCHOR_BUCKET")
    if bucket:
        from fraud_mlops.audit.anchor import SealAnchor

        r.problems.extend(SealAnchor(bucket).compare(store.seals()))
        print(f"anchor bucket  : s3://{bucket} (write once copies compared)")
    print(f"rows checked   : {r.rows_checked}")
    print(f"seals checked  : {r.seals_checked}")
    print(f"unsealed tail  : {r.unsealed_rows} (newer than the last seal)")
    if r.ok:
        print("result         : OK, audit trail intact")
        return
    print(f"result         : FAILED, {len(r.problems)} problem(s)")
    for p in r.problems:
        print(f"  - {p}")
    sys.exit(1)


if __name__ == "__main__":
    main()
