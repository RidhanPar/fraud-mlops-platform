"""Pull the drift event out of Prometheus and the alert log into a chart and summary.

    python scripts/report_drift_event.py
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import httpx
import matplotlib

matplotlib.use("Agg")
import matplotlib.dates as mdates  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docs/drift_event"
PROM = "http://127.0.0.1:9090"


def ts(iso: str) -> float:
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()


def series(query: str, start: float, end: float) -> dict[str, list[tuple[float, float]]]:
    r = httpx.get(f"{PROM}/api/v1/query_range",
                  params={"query": query, "start": start, "end": end, "step": 5}).json()
    out = {}
    for s in r["data"]["result"]:
        key = s["metric"].get("alertname") or s["metric"].get("feature") or query
        out[key] = [(float(t), float(v)) for t, v in s["values"]]
    return out


def main() -> None:
    timeline = json.loads((OUT / "timeline.json").read_text())
    phases = {p["phase"]: (ts(p["start"]), ts(p["end"])) for p in timeline["phases"]}
    start, end = phases["baseline"][0] + 90, phases["recovery"][1] + 60
    inc_start, inc_end = phases["incident"]

    v14 = series('fraud_feature_psi{feature="V14"}', start, end)
    flag = series("fraud_flag_rate", start, end)
    ref_flag = series("fraud_reference_flag_rate", start, end)
    recall = series("fraud_labelled_recall", start, end)
    score_psi = series("fraud_score_psi", start, end)
    firing = series('ALERTS{alertstate="firing"}', start, end)

    # Detection and resolution times per alert, relative to incident start.
    alerts = {}
    for name, pts in firing.items():
        times = [t for t, _ in pts]
        alerts[name] = {
            "first_firing_after_incident_start_s": round(min(times) - inc_start),
            "last_firing_after_incident_end_s": round(max(times) - inc_end),
        }
    baseline_alerts = [n for n, pts in firing.items() if any(t < inc_start for t, _ in pts)]

    # Skip the first 90 s of baseline: the monitor needs time to fill its windows, and
    # until then Prometheus carries forward whatever the gauges held before the run.
    warm = (phases["baseline"][0] + 90, phases["baseline"][1])

    def val_at(s, t0, t1, fn):
        vals = [v for pts in s.values() for t, v in pts if t0 <= t <= t1 and v == v]
        return round(fn(vals), 4) if vals else None

    summary = {
        "incident": timeline["incident"],
        "rows_sent": timeline["rows_sent"],
        "phases": timeline["phases"],
        "alerts": alerts,
        "alerts_firing_during_baseline": baseline_alerts,
        "v14_psi": {"baseline_max": val_at(v14, *warm, max),
                    "incident_max": val_at(v14, *phases["incident"], max)},
        "flag_rate": {"baseline_mean": val_at(flag, *warm, lambda v: sum(v) / len(v)),
                      "incident_min": val_at(flag, *phases["incident"], min)},
        "labelled_recall": {"baseline_mean": val_at(recall, *warm, lambda v: sum(v) / len(v)),
                            "incident_min": val_at(recall, *phases["incident"], min)},
        "score_psi": {"baseline_max": val_at(score_psi, *warm, max),
                      "incident_max": val_at(score_psi, *phases["incident"], max)},
    }
    sink = ROOT / "monitoring/alert_log/alerts.jsonl"
    if sink.exists():
        summary["notifications"] = [json.loads(line) for line in sink.read_text().splitlines()]
    (OUT / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")

    # Chart
    dt = lambda t: datetime.fromtimestamp(t, tz=timezone.utc)  # noqa: E731
    fig, axes = plt.subplots(4, 1, figsize=(11, 10), sharex=True)
    panels = [
        (axes[0], v14, "V14 PSI vs training", None),
        (axes[1], {**flag, **{"reference": v for v in ref_flag.values()}}, "Fraud flag rate", None),
        (axes[2], recall, "Recall on labelled traffic", None),
        (axes[3], score_psi, "Score PSI (prediction drift)", 0.1),
    ]
    for ax, s, title, line in panels:
        for name, pts in s.items():
            ax.plot([dt(t) for t, _ in pts], [v for _, v in pts],
                    label="live" if name != "reference" else "training reference")
        ax.axvspan(dt(inc_start), dt(inc_end), color="tab:red", alpha=0.12)
        if line:
            ax.axhline(line, ls="--", color="grey", lw=1)
        ax.set_title(title, loc="left", fontsize=10)
        ax.grid(alpha=0.3)
    axes[1].legend(fontsize=8)
    for name, info in alerts.items():
        t = inc_start + info["first_firing_after_incident_start_s"]
        for ax in axes:
            ax.axvline(dt(t), color="black", lw=0.8, ls=":")
        axes[0].annotate(name, (dt(t), axes[0].get_ylim()[1]), rotation=90, fontsize=7,
                         va="top", ha="right")
    axes[-1].xaxis.set_major_formatter(mdates.DateFormatter("%H:%M", tz=timezone.utc))
    axes[-1].set_xlabel("UTC")
    fig.suptitle("Drift event: V14 zero filled upstream (red band). Dotted lines: alerts firing.", fontsize=11)
    fig.tight_layout()
    fig.savefig(OUT / "drift_event.png", dpi=120)
    print(json.dumps({k: summary[k] for k in ("alerts", "alerts_firing_during_baseline", "v14_psi",
                                              "flag_rate", "labelled_recall", "score_psi")}, indent=2))


if __name__ == "__main__":
    sys.exit(main())
