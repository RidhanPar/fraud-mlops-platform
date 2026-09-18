"""Retraining trigger policy, as code. See docs/RETRAINING_POLICY.md.

The same symptom (recall collapsing) can need opposite responses. If an
upstream fault is feeding bad inputs, retraining on that data would teach the
model to ignore a real signal, so the answer is to fix the data. If fraudsters
changed behaviour, the model is out of date and retraining is the fix.

`decide` is pure and unit tested. `gather_signals` reads Prometheus.

    python -m fraud_mlops.retrain_policy                       # now, 24h window
    python -m fraud_mlops.retrain_policy --window 3m --at 2026-09-18T08:00:00Z
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

import httpx

from fraud_mlops.config import ROOT


@dataclass
class Policy:
    recall_floor: float = 0.5          # release recall was 0.70
    min_labelled_frauds: int = 10      # below this, recall is noise
    max_reject_rate: float = 0.05
    max_model_age_days: int = 30       # scheduled refresh even when nothing fires


@dataclass
class Signals:
    window: str
    stuck_features: list[str] = field(default_factory=list)
    reject_rate: float | None = None
    recall_max: float | None = None          # best recall seen over the window
    labelled_frauds_min: float | None = None
    drifting_features: list[str] = field(default_factory=list)  # above threshold for the whole window
    score_psi_min: float | None = None
    flag_rate_ratio: float | None = None     # live flag rate / training flag rate
    model_age_days: float | None = None


@dataclass
class Decision:
    action: str
    reasons: list[str]
    next_steps: list[str]


def decide(s: Signals, p: Policy = Policy()) -> Decision:
    labels_ok = s.labelled_frauds_min is not None and s.labelled_frauds_min >= p.min_labelled_frauds
    recall_low = labels_ok and s.recall_max is not None and s.recall_max < p.recall_floor

    # 1. Data faults come first: never retrain on broken inputs.
    faults = []
    if s.stuck_features:
        faults.append(f"stuck values in {', '.join(s.stuck_features)} (upstream fill or default)")
    if s.reject_rate is not None and s.reject_rate > p.max_reject_rate:
        faults.append(f"{s.reject_rate:.1%} of requests rejected as invalid (input contract broken)")
    if faults:
        reasons = faults + ([f"recall {s.recall_max:.2f} is a symptom of the fault, not model decay"]
                            if recall_low else [])
        return Decision("FIX_DATA", reasons, [
            "Page the owner of the upstream feature service; do not retrain.",
            f"Mark the last {s.window} of prediction log as excluded from training data.",
            "Route affected decisions to fallback rules or manual review until inputs recover.",
            "After recovery, rescore logged transactions from the incident window.",
        ])

    # 2. Confirmed decay on clean inputs: the world changed, the model did not.
    if recall_low:
        return Decision("RETRAIN_NOW", [
            f"recall on labelled traffic stayed below {p.recall_floor} for {s.window} "
            f"(best {s.recall_max:.2f}) with no data fault detected",
        ], [
            "Build a training set including matured labels from the recent window.",
            "Run the training pipeline; the promotion gate still decides (Phase 1).",
            "Shadow the candidate on live traffic before switching the champion alias.",
        ])

    # 3. Sustained shift without label confirmation: prepare, let the gate decide.
    shift = []
    if s.drifting_features:
        shift.append(f"sustained drift in {', '.join(s.drifting_features)}")
    if s.score_psi_min is not None and s.score_psi_min > 0.1:
        shift.append(f"sustained prediction drift (score PSI >= {s.score_psi_min:.2f})")
    if s.flag_rate_ratio is not None and not 0.2 <= s.flag_rate_ratio <= 5:
        shift.append(f"flag rate {s.flag_rate_ratio:.1f}x the training rate")
    if shift:
        return Decision("RETRAIN_CANDIDATE", shift + (
            [] if labels_ok else ["not enough labelled fraud yet to confirm impact"]), [
            "Train a candidate on recent data; promote only if it beats the champion on held out data.",
            "Keep watching recall as labels mature.",
        ])

    # 4. Nothing wrong, but models go stale.
    if s.model_age_days is not None and s.model_age_days > p.max_model_age_days:
        return Decision("SCHEDULED_RETRAIN", [
            f"model is {s.model_age_days:.0f} days old (policy: {p.max_model_age_days})"],
            ["Routine retrain through the gate."])

    reasons = ["no data fault, no sustained drift, recall healthy or not yet measurable"]
    if not labels_ok:
        reasons.append("performance unconfirmed: fewer labelled frauds than the policy minimum")
    return Decision("NO_ACTION", reasons, [])


# ---- Prometheus ---------------------------------------------------------------

def _query(prom: str, expr: str, at: float) -> list[dict]:
    r = httpx.get(f"{prom}/api/v1/query", params={"query": expr, "time": at}, timeout=10).json()
    return r["data"]["result"]


def _scalar(prom: str, expr: str, at: float) -> float | None:
    res = _query(prom, expr, at)
    if not res:
        return None
    v = float(res[0]["value"][1])
    return None if v != v else v


def gather_signals(prom: str, window: str, at: float, model_dir: Path) -> Signals:
    w = window
    stuck = _query(prom, f"max_over_time(fraud_feature_mode_share[{w}]) > 0.5 "
                         f"and on(feature) fraud_feature_reference_mode_share < 0.1", at)
    drifting = _query(prom, f"min_over_time((fraud_feature_psi / on(feature) "
                            f"fraud_feature_psi_threshold)[{w}:15s]) > 1", at)
    meta = json.loads((model_dir / "metadata.json").read_text("utf-8"))
    age = None
    if meta.get("trained_at"):
        trained = dt.datetime.fromisoformat(meta["trained_at"]).timestamp()
        age = (at - trained) / 86400
    return Signals(
        window=w,
        stuck_features=sorted(r["metric"]["feature"] for r in stuck),
        reject_rate=_scalar(prom, f'sum(increase(fraud_http_requests_total{{status="422"}}[{w}])) / '
                                  f'sum(increase(fraud_http_requests_total{{route=~"/predict.*"}}[{w}]))', at),
        recall_max=_scalar(prom, f"max_over_time(fraud_labelled_recall[{w}])", at),
        labelled_frauds_min=_scalar(prom, f"min_over_time(fraud_labelled_frauds[{w}])", at),
        drifting_features=sorted(r["metric"]["feature"] for r in drifting),
        score_psi_min=_scalar(prom, f"min_over_time(fraud_score_psi[{w}])", at),
        flag_rate_ratio=_scalar(prom, f"avg_over_time(fraud_flag_rate[{w}]) / "
                                      f"avg_over_time(fraud_reference_flag_rate[{w}])", at),
        model_age_days=age,
    )


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--prometheus", default="http://127.0.0.1:9090")
    ap.add_argument("--window", default="24h", help="how long a signal must persist")
    ap.add_argument("--at", help="evaluate at this UTC time (ISO 8601), default now")
    ap.add_argument("--model-dir", default=str(ROOT / "serving_model"))
    args = ap.parse_args()
    at = (dt.datetime.fromisoformat(args.at.replace("Z", "+00:00")).timestamp()
          if args.at else dt.datetime.now(dt.timezone.utc).timestamp())
    signals = gather_signals(args.prometheus, args.window, at, Path(args.model_dir))
    d = decide(signals)
    print(json.dumps({"evaluated_at": dt.datetime.fromtimestamp(at, dt.timezone.utc).isoformat(),
                      "signals": asdict(signals), "decision": asdict(d)}, indent=2))


if __name__ == "__main__":
    main()
