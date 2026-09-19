"""Operator commands for the AWS deployment (boto3, no AWS CLI needed).

    python scripts/aws.py push                 # build and push both images, print the tag
    python scripts/aws.py loadtest             # run the load generator as a Fargate task in the VPC
    python scripts/aws.py alarms --since 2026-09-19T10:00:00Z
    python scripts/aws.py empty-seals          # remove Object Lock copies before terraform destroy
    python scripts/aws.py cost --start 2026-09-19 --end 2026-09-21

Uses the AWS profile in AWS_PROFILE (default: fraud-mlops) and reads resource
names from `terraform output`.
"""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import boto3

ROOT = Path(__file__).resolve().parents[1]
TF_DIR = ROOT / "infra" / "terraform"
PROFILE = os.environ.get("AWS_PROFILE", "fraud-mlops")
REGION = "eu-north-1"


def session() -> boto3.Session:
    return boto3.Session(profile_name=PROFILE, region_name=REGION)


def tf_outputs() -> dict:
    raw = subprocess.check_output(["terraform", "output", "-json"], cwd=TF_DIR, text=True)
    return {k: v["value"] for k, v in json.loads(raw).items()}


def run(cmd: list[str], **kw) -> None:
    print("+", " ".join(cmd), flush=True)
    subprocess.run(cmd, check=True, **kw)


def git_tag() -> str:
    sha = subprocess.check_output(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, text=True).strip()
    dirty = subprocess.check_output(["git", "status", "--porcelain", "--", "fraud_mlops", "Dockerfile",
                                     "requirements-serve.txt"], cwd=ROOT, text=True).strip()
    # Tags are immutable in ECR; uncommitted code must never reuse a commit's tag.
    return f"{sha}-dirty-{int(time.time())}" if dirty else sha


def cmd_push(args) -> None:
    out = tf_outputs()
    ecr = session().client("ecr")
    auth = ecr.get_authorization_token()["authorizationData"][0]
    user, password = base64.b64decode(auth["authorizationToken"]).decode().split(":", 1)
    registry = auth["proxyEndpoint"].removeprefix("https://")
    subprocess.run(["docker", "login", "--username", user, "--password-stdin", registry],
                   input=password, text=True, check=True, capture_output=True)

    tag = args.tag or git_tag()
    api = f"{out['ecr_api_repo']}:{tag}"
    run(["docker", "build", "-q", "-t", api, "."], cwd=ROOT)
    run(["docker", "push", "-q", api])
    loadgen = f"{out['ecr_loadgen_repo']}:latest"
    run(["docker", "build", "-q", "-t", loadgen, "loadtest"], cwd=ROOT)
    run(["docker", "push", "-q", loadgen])
    print(f"\npushed image tag: {tag}\nnext: terraform apply -var image_tag={tag}")


def cmd_loadtest(args) -> None:
    out = tf_outputs()
    ecs, logs = session().client("ecs"), session().client("logs")
    command = ["--url", out["loadtest_url"], "--payloads", "/lt/payloads.json",
               "--procs", str(args.procs), "--duration", str(args.duration), "--print-json",
               "--header", f"X-Loadtest-Token:{out['loadtest_token']}",
               "--note", args.note]
    task = ecs.run_task(
        cluster=out["cluster"], taskDefinition=out["loadgen_task_definition"], launchType="FARGATE",
        networkConfiguration={"awsvpcConfiguration": {
            "subnets": out["subnets"], "securityGroups": [out["task_security_group"]],
            "assignPublicIp": "ENABLED"}},
        overrides={"containerOverrides": [{"name": "loadgen", "command": command}]},
    )["tasks"][0]
    arn = task["taskArn"]
    task_id = arn.rsplit("/", 1)[-1]
    print(f"started load generator task {task_id}", flush=True)
    while True:
        t = ecs.describe_tasks(cluster=out["cluster"], tasks=[arn])["tasks"][0]
        if t["lastStatus"] == "STOPPED":
            break
        time.sleep(15)
    container = t["containers"][0]
    print(f"task stopped: exit {container.get('exitCode')} {t.get('stoppedReason', '')}")

    events = logs.get_log_events(logGroupName="/ecs/fraud-mlops", logStreamName=f"loadgen/loadgen/{task_id}",
                                 startFromHead=True)["events"]
    result = None
    for e in events:
        msg = e["message"]
        if msg.startswith("RESULT_JSON "):
            result = json.loads(msg.removeprefix("RESULT_JSON "))
        else:
            print(msg)
    if result is None:
        sys.exit("no RESULT_JSON in the task log")
    result["fargate_task"] = {"cpu": t.get("cpu"), "memory": t.get("memory"), "id": task_id}
    target = ROOT / "loadtest/results" / args.out
    target.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(f"saved {target}")


def cmd_alarms(args) -> None:
    cw = session().client("cloudwatch")
    since = dt.datetime.fromisoformat(args.since.replace("Z", "+00:00"))
    items = []
    for page in cw.get_paginator("describe_alarm_history").paginate(
            AlarmNamePrefix="fraud-mlops-", HistoryItemType="StateUpdate", StartDate=since,
            EndDate=dt.datetime.now(dt.timezone.utc), ScanBy="TimestampAscending"):
        for h in page["AlarmHistoryItems"]:
            data = json.loads(h["HistoryData"])
            items.append({
                "time": h["Timestamp"].isoformat(),
                "alarm": h["AlarmName"].removeprefix("fraud-mlops-"),
                "from": data["oldState"]["stateValue"],
                "to": data["newState"]["stateValue"],
                "reason": data["newState"].get("stateReason", "")[:200],
            })
    for i in items:
        print(f"{i['time'][11:19]}  {i['alarm']:<20} {i['from']:>17} -> {i['to']:<17}")
    if args.out:
        Path(args.out).write_text(json.dumps(items, indent=2), encoding="utf-8")


def cmd_empty_seals(args) -> None:
    """Governance mode Object Lock: only a principal with s3:BypassGovernanceRetention
    (the deployer, not the auditor) can remove locked seal copies."""
    bucket = tf_outputs()["seal_bucket"]
    s3 = session().client("s3")
    deleted = 0
    for page in s3.get_paginator("list_object_versions").paginate(Bucket=bucket):
        objs = [{"Key": v["Key"], "VersionId": v["VersionId"]}
                for v in page.get("Versions", []) + page.get("DeleteMarkers", [])]
        if objs:
            s3.delete_objects(Bucket=bucket, Delete={"Objects": objs}, BypassGovernanceRetention=True)
            deleted += len(objs)
    print(f"removed {deleted} object versions from {bucket}")


def cmd_cost(args) -> None:
    ce = boto3.Session(profile_name=PROFILE).client("ce", region_name="us-east-1")
    r = ce.get_cost_and_usage(
        TimePeriod={"Start": args.start, "End": args.end}, Granularity="DAILY",
        Metrics=["UnblendedCost"], GroupBy=[{"Type": "DIMENSION", "Key": "SERVICE"}])
    total = 0.0
    for day in r["ResultsByTime"]:
        for g in day["Groups"]:
            amount = float(g["Metrics"]["UnblendedCost"]["Amount"])
            if amount >= 0.001:
                total += amount
                print(f"{day['TimePeriod']['Start']}  {g['Keys'][0]:<45} ${amount:.3f}")
    print(f"total ${total:.2f}")


def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("push")
    p.add_argument("--tag")
    p.set_defaults(fn=cmd_push)
    p = sub.add_parser("loadtest")
    p.add_argument("--duration", type=float, default=20)
    p.add_argument("--procs", type=int, default=4)
    p.add_argument("--note", default="AWS: generator is a Fargate task in the same VPC, through the ALB")
    p.add_argument("--out", default="aws_fargate.json")
    p.set_defaults(fn=cmd_loadtest)
    p = sub.add_parser("alarms")
    p.add_argument("--since", required=True)
    p.add_argument("--out")
    p.set_defaults(fn=cmd_alarms)
    p = sub.add_parser("empty-seals")
    p.set_defaults(fn=cmd_empty_seals)
    p = sub.add_parser("cost")
    p.add_argument("--start", required=True)
    p.add_argument("--end", required=True)
    p.set_defaults(fn=cmd_cost)
    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
