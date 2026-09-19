"""Copy seals to S3 with Object Lock, outside the database admin's reach.

The seal chain in Postgres catches edits to rows, but an admin with full
database control could rewrite every seal after a forgery. A copy of each seal
in a write once bucket (Object Lock, default retention set on the bucket)
cannot be changed or deleted by that admin, so a rewritten chain no longer
matches its anchors.
"""

from __future__ import annotations

import json
from typing import Any

import boto3


def _key(seal: dict[str, Any]) -> str:
    return f"seals/{int(seal['first_id']):012d}-{int(seal['last_id']):012d}.json"


class SealAnchor:
    def __init__(self, bucket: str, client=None) -> None:
        self.bucket = bucket
        self.s3 = client or boto3.client("s3")

    def anchored(self) -> dict[str, str]:
        """key -> seal_hash for every anchored seal."""
        out: dict[str, str] = {}
        for page in self.s3.get_paginator("list_objects_v2").paginate(Bucket=self.bucket, Prefix="seals/"):
            for obj in page.get("Contents", []):
                body = self.s3.get_object(Bucket=self.bucket, Key=obj["Key"])["Body"].read()
                out[obj["Key"]] = json.loads(body)["seal_hash"]
        return out

    def put(self, seal: dict[str, Any]) -> str:
        record = {k: (v.isoformat() if hasattr(v, "isoformat") else v)
                  for k, v in seal.items() if k != "id"}
        key = _key(seal)
        # Object Lock requires an integrity checksum on the upload.
        self.s3.put_object(Bucket=self.bucket, Key=key, Body=json.dumps(record).encode(),
                           ContentType="application/json", ChecksumAlgorithm="SHA256")
        return key

    def anchor_missing(self, seals: list[dict[str, Any]], known: set[str]) -> list[str]:
        written = []
        for seal in seals:
            key = _key(seal)
            if key not in known:
                self.put(seal)
                known.add(key)
                written.append(key)
        return written

    def compare(self, seals: list[dict[str, Any]]) -> list[str]:
        """Problems where the database chain disagrees with the write once copies."""
        anchors = self.anchored()
        problems = []
        for seal in seals:
            key = _key(seal)
            if key in anchors and anchors[key] != seal["seal_hash"]:
                problems.append(f"seal {seal['id']}: differs from its write once copy in S3 ({key})")
        db_keys = {_key(s) for s in seals}
        for key in anchors:
            if key not in db_keys:
                problems.append(f"S3 holds seal {key} that is missing from the database")
        return problems
