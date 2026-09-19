"""Tamper demo against the AWS deployment (RDS + S3 Object Lock).

Escalates one step beyond the local demo: after forging a decision, the
database admin also rewrites every seal from that point on, so the database
chain is internally consistent again. Only the write once copies in S3 can
catch that. Then the admin tries to remove the S3 copy.

Everything is restored at the end so the demo database stays usable.

    AWS_PROFILE=default python scripts/aws_tamper_demo.py
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import quote_plus

import boto3
import botocore
from sqlalchemy import create_engine, text

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from fraud_mlops.api.store import PredictionStore  # noqa: E402
from fraud_mlops.audit.anchor import SealAnchor, _key  # noqa: E402
from fraud_mlops.audit.hashing import record_hash, rows_digest, seal_hash  # noqa: E402

OUT = ROOT / "docs/aws/tamper_demo.json"
PRED_ON = "ALTER TABLE predictions {} TRIGGER predictions_append_only"
SEAL_ON = "ALTER TABLE audit_seals {} TRIGGER audit_seals_append_only"


def attempt(engine, statements: list[tuple[str, dict]]) -> str:
    try:
        with engine.begin() as c:
            for sql, params in statements:
                c.execute(text(sql), params)
        return "succeeded"
    except Exception as exc:
        return "refused: " + str(getattr(exc, "orig", exc)).splitlines()[0]


def verify_in_region(session, out: dict, anchors: bool) -> dict:
    """Run the verifier as a one off Fargate task next to the database (the auditor's
    task definition and role, which can read the seal bucket). Returns its report."""
    ecs, logs = session.client("ecs"), session.client("logs")
    env = [] if anchors else [{"name": "SEAL_ANCHOR_BUCKET", "value": ""}]
    task = ecs.run_task(
        cluster=out["cluster"], taskDefinition="fraud-mlops-auditor", launchType="FARGATE",
        networkConfiguration={"awsvpcConfiguration": {"subnets": out["subnets"],
                              "securityGroups": [out["task_security_group"]], "assignPublicIp": "ENABLED"}},
        overrides={"cpu": "1024", "memory": "2048", "containerOverrides": [{
            "name": "auditor", "environment": env,
            "command": ["sh", "-c", "unset PROMETHEUS_MULTIPROC_DIR && exec python -m fraud_mlops.audit.verify"]}]},
    )["tasks"][0]
    arn = task["taskArn"]
    while ecs.describe_tasks(cluster=out["cluster"], tasks=[arn])["tasks"][0]["lastStatus"] != "STOPPED":
        time.sleep(10)
    task_id = arn.rsplit("/", 1)[-1]
    lines = [e["message"] for e in logs.get_log_events(
        logGroupName="/ecs/fraud-mlops", logStreamName=f"auditor/auditor/{task_id}", startFromHead=True)["events"]]
    report = {"ok": any("OK, audit trail intact" in line for line in lines),
              "problems": [line.strip()[2:] for line in lines if line.strip().startswith("- ")]}
    for line in lines:
        if line.startswith("rows checked"):
            report["rows_checked"] = int(line.split(":")[1])
        if line.startswith("seals checked"):
            report["seals_checked"] = int(line.split(":")[1])
    return report


def full_verify(session, out) -> dict:
    return {"database_chain_only": verify_in_region(session, out, anchors=False),
            "with_s3_anchors": verify_in_region(session, out, anchors=True)}


def main() -> None:
    out = {k: v["value"] for k, v in json.loads(subprocess.check_output(
        ["terraform", "output", "-json"], cwd=ROOT / "infra/terraform", text=True)).items()}
    session = boto3.Session(profile_name=os.environ.get("AWS_PROFILE", "default"), region_name="eu-north-1")
    sm = session.client("secretsmanager")
    admin = json.loads(sm.get_secret_value(SecretId=out["admin_db_secret_arn"])["SecretString"])
    app_pw = sm.get_secret_value(SecretId=out["app_db_secret_arn"])["SecretString"]
    host = out["db_host"]
    url = lambda u, p: f"postgresql+psycopg://{quote_plus(u)}:{quote_plus(p)}@{host}:5432/fraud?sslmode=require"  # noqa: E731
    store = PredictionStore(url("fraud_app", app_pw))
    admin_engine = create_engine(url(admin["username"], admin["password"]))
    anchor = SealAnchor(out["seal_bucket"], client=session.client("s3"))

    results: dict = {"verify_before": full_verify(session, out)}

    seals = store.seals()
    anchored = anchor.anchored()
    # Target: a fraud decision inside the newest seal that already has an S3 copy.
    target_seal = next(s for s in reversed(seals) if _key(s) in anchored)
    with store.engine.connect() as c:
        target_id = c.execute(text(
            "SELECT id FROM predictions WHERE is_fraud AND id BETWEEN :a AND :b ORDER BY id DESC LIMIT 1"),
            {"a": target_seal["first_id"], "b": target_seal["last_id"]}).scalar_one()
    original = next(r for r in store.iter_predictions(after_id=target_id - 1) if r["id"] == target_id)
    results["target"] = {"prediction_id": target_id, "transaction_id": original["transaction_id"],
                         "score": original["score"], "seal_id": target_seal["id"],
                         "seals_after_it": len([s for s in seals if s["id"] > target_seal["id"]])}

    edit = ("UPDATE predictions SET score = 0.01, is_fraud = false WHERE id = :id", {"id": target_id})
    results["1_app_role_update"] = attempt(store.engine, [edit])
    results["2_admin_update"] = attempt(admin_engine, [edit])

    forged = dict(original, score=0.01, is_fraud=False)
    results["3_admin_forges_row_and_row_hash"] = attempt(admin_engine, [
        (PRED_ON.format("DISABLE"), {}),
        ("UPDATE predictions SET score = 0.01, is_fraud = false, record_hash = :h WHERE id = :id",
         {"h": record_hash(forged), "id": target_id}),
        (PRED_ON.format("ENABLE"), {}),
    ])
    results["verify_after_row_forgery"] = full_verify(session, out)

    # 4. Rewrite the seal covering the row and every seal after it, so the database
    #    chain is consistent again.
    rows = {r["id"]: r for r in store.iter_predictions(after_id=target_seal["first_id"] - 1)}
    rewrites, prev = [], next((s["seal_hash"] for s in seals if s["id"] == target_seal["id"] - 1), None)
    from fraud_mlops.audit.hashing import GENESIS
    prev = prev or GENESIS
    for s in [s for s in seals if s["id"] >= target_seal["id"]]:
        digest = rows_digest([rows[i]["record_hash"] for i in sorted(rows) if s["first_id"] <= i <= s["last_id"]])
        new_hash = seal_hash(prev, s["first_id"], s["last_id"], digest)
        rewrites.append(("UPDATE audit_seals SET rows_digest = :d, prev_seal_hash = :p, seal_hash = :h WHERE id = :id",
                         {"d": digest, "p": prev, "h": new_hash, "id": s["id"]}))
        prev = new_hash
    results["4_admin_rewrites_seal_chain"] = attempt(admin_engine, [
        (SEAL_ON.format("DISABLE"), {}), *rewrites, (SEAL_ON.format("ENABLE"), {})])
    results["4_seals_rewritten"] = len(rewrites)
    results["verify_after_chain_rewrite"] = full_verify(session, out)

    # 5. Try to remove the S3 copy that exposes the forgery, without the bypass permission.
    s3 = session.client("s3")
    key = _key(target_seal)
    version = s3.list_object_versions(Bucket=out["seal_bucket"], Prefix=key)["Versions"][0]["VersionId"]
    try:
        s3.delete_object(Bucket=out["seal_bucket"], Key=key, VersionId=version)
        results["5_delete_s3_copy"] = "succeeded"
    except botocore.exceptions.ClientError as e:
        results["5_delete_s3_copy"] = f"refused: {e.response['Error']['Code']}: {e.response['Error']['Message']}"

    # Restore the original row and seals.
    restore = [(PRED_ON.format("DISABLE"), {}),
               ("UPDATE predictions SET score = :s, is_fraud = true, record_hash = :h WHERE id = :id",
                {"s": original["score"], "h": original["record_hash"], "id": target_id}),
               (PRED_ON.format("ENABLE"), {}), (SEAL_ON.format("DISABLE"), {})]
    restore += [("UPDATE audit_seals SET rows_digest = :d, prev_seal_hash = :p, seal_hash = :h WHERE id = :id",
                 {"d": s["rows_digest"], "p": s["prev_seal_hash"], "h": s["seal_hash"], "id": s["id"]})
                for s in seals if s["id"] >= target_seal["id"]]
    restore.append((SEAL_ON.format("ENABLE"), {}))
    results["restore"] = attempt(admin_engine, restore)
    results["verify_after_restore"] = full_verify(session, out)

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(results, indent=2, default=str), encoding="utf-8")
    print(json.dumps(results, indent=2, default=str))


if __name__ == "__main__":
    main()
