"""Generate the Grafana dashboard JSON.

    python monitoring/grafana/build_dashboard.py
"""

import json
from pathlib import Path

DS = {"type": "prometheus", "uid": "prometheus"}
panels: list[dict] = []
_id = 0
_y = 0


def _next_id() -> int:
    global _id
    _id += 1
    return _id


def row(title: str) -> None:
    global _y
    panels.append({"type": "row", "title": title, "id": _next_id(), "collapsed": False,
                   "gridPos": {"x": 0, "y": _y, "w": 24, "h": 1}})
    _y += 1


def panel(kind, title, targets, x, w, h=8, unit=None, desc=None, extra=None, new_row=False):
    global _y
    p = {
        "type": kind,
        "title": title,
        "id": _next_id(),
        "datasource": DS,
        "gridPos": {"x": x, "y": _y, "w": w, "h": h},
        "targets": [
            {"datasource": DS, "expr": expr, "legendFormat": legend, "refId": chr(65 + i)}
            for i, (expr, legend) in enumerate(targets)
        ],
        "fieldConfig": {"defaults": {}, "overrides": []},
        "options": {},
    }
    if unit:
        p["fieldConfig"]["defaults"]["unit"] = unit
    if desc:
        p["description"] = desc
    if extra:
        for k, v in extra.items():
            p[k] = v if k != "fieldConfig" else {**p["fieldConfig"], **v}
    panels.append(p)
    if x + w >= 24:
        _y += h


def thresholds(*steps):
    return {"mode": "absolute", "steps": [{"color": c, "value": v} for v, c in steps]}


# Service health
row("Service health")
panel("stat", "Model version serving", [("max by (model_version) (fraud_model_info)", "v{{model_version}}")],
      0, 4, h=6, extra={"options": {"textMode": "name", "colorMode": "none"}})
panel("stat", "Requests / s", [("sum(rate(fraud_http_requests_total{route=~\"/predict.*\"}[1m]))", "req/s")],
      4, 4, h=6, extra={"options": {"colorMode": "none"}, "fieldConfig": {"defaults": {"unit": "reqps"}}})
panel("stat", "5xx error rate",
      [("sum(rate(fraud_http_requests_total{status=~\"5..\"}[1m])) / sum(rate(fraud_http_requests_total[1m])) or vector(0)", "5xx")],
      8, 4, h=6, unit="percentunit",
      extra={"fieldConfig": {"defaults": {"unit": "percentunit", "thresholds": thresholds((None, "green"), (0.01, "red"))}}})
panel("stat", "Rejected input (422) rate",
      [("sum(rate(fraud_http_requests_total{status=\"422\"}[1m])) / sum(rate(fraud_http_requests_total{route=~\"/predict.*\"}[1m])) or vector(0)", "422")],
      12, 4, h=6, unit="percentunit",
      extra={"fieldConfig": {"defaults": {"unit": "percentunit", "thresholds": thresholds((None, "green"), (0.05, "orange"))}}})
panel("stat", "Transactions scored / s", [("sum(rate(fraud_rows_scored_total[1m]))", "rows/s")], 16, 4, h=6,
      extra={"options": {"colorMode": "none"}})
panel("stat", "Firing alerts", [("count(ALERTS{alertstate=\"firing\"}) or vector(0)", "firing")], 20, 4, h=6,
      extra={"fieldConfig": {"defaults": {"thresholds": thresholds((None, "green"), (1, "red"))}}})

panel("timeseries", "/predict latency (budget 100 ms)", [
    (f"histogram_quantile({q}, sum by (le) (rate(fraud_http_request_duration_seconds_bucket{{route=\"/predict\"}}[1m])))", f"p{int(q*100)}")
    for q in (0.5, 0.95, 0.99)], 0, 12, unit="s")
panel("timeseries", "/predict/batch latency", [
    (f"histogram_quantile({q}, sum by (le) (rate(fraud_http_request_duration_seconds_bucket{{route=\"/predict/batch\"}}[1m])))", f"p{int(q*100)}")
    for q in (0.5, 0.95, 0.99)], 12, 12, unit="s")
panel("timeseries", "Requests by route and status", [
    ("sum by (route, status) (rate(fraud_http_requests_total[1m]))", "{{route}} {{status}}")], 0, 24, unit="reqps")

# Prediction behaviour
row("Predictions")
panel("timeseries", "Fraud flag rate: live vs training reference", [
    ("fraud_flag_rate", "live (last 20k rows)"),
    ("fraud_reference_flag_rate", "training reference"),
    ("0.2 * fraud_reference_flag_rate", "collapse alert line"),
], 0, 12, unit="percentunit",
    desc="Share of transactions the model flags. A collapse means the model may have gone blind.")
panel("bargauge", "Score distribution (last 5 min)", [
    ("sum by (le) (increase(fraud_score_bucket[5m]))", "{{le}}")], 12, 12,
    extra={"options": {"displayMode": "gradient", "orientation": "vertical"},
           "targets": [{"datasource": DS, "expr": "sum by (le) (increase(fraud_score_bucket[5m]))",
                        "legendFormat": "{{le}}", "refId": "A", "format": "heatmap"}]})

# Drift
row("Drift vs training data")
panel("stat", "Features drifting", [("fraud_features_drifting", "features")], 0, 4, h=8,
      extra={"fieldConfig": {"defaults": {"thresholds": thresholds((None, "green"), (1, "red"))}}})
panel("timeseries", "Feature PSI / calibrated threshold (>1 = alert)", [
    ("topk(5, fraud_feature_psi / on(feature) fraud_feature_psi_threshold)", "{{feature}}")], 4, 10,
    desc="Each feature's PSI divided by its own threshold, calibrated on known good traffic.")
panel("timeseries", "Score PSI (prediction drift)", [("fraud_score_psi", "score PSI")], 14, 10,
      extra={"fieldConfig": {"defaults": {"thresholds": thresholds((None, "green"), (0.1, "red")),
                                          "custom": {"thresholdsStyle": {"mode": "line"}}}}})
panel("bargauge", "Current PSI by feature", [("sort_desc(fraud_feature_psi)", "{{feature}}")], 0, 24, h=9,
      extra={"options": {"displayMode": "basic", "orientation": "horizontal"}})

# Performance
row("Model performance on labelled traffic (lagged)")
panel("timeseries", "Recall and precision at serving threshold", [
    ("fraud_labelled_recall", "recall"), ("fraud_labelled_precision", "precision"),
    ("fraud_labelled_pr_auc", "PR AUC")], 0, 16, unit="percentunit",
    desc="Computed only when at least 10 confirmed frauds are in the window. Labels arrive late.")
panel("timeseries", "Labelled window", [("fraud_labelled_frauds", "confirmed frauds"),
                                        ("fraud_labelled_rows / 1000", "labelled rows (k)")], 16, 8)

row("Alerts")
panel("table", "Firing and pending alerts (from Prometheus)", [], 0, 24, h=8,
      extra={"targets": [{"datasource": DS, "expr": "ALERTS", "refId": "A",
                          "format": "table", "instant": True}],
             "transformations": [{"id": "organize", "options": {
                 "excludeByName": {"Time": True, "Value": True, "__name__": True, "instance": True, "job": True}}}]})

dashboard = {
    "uid": "fraud-mlops",
    "title": "Fraud model: service, drift and performance",
    "tags": ["fraud", "mlops"],
    "timezone": "browser",
    "schemaVersion": 39,
    "refresh": "5s",
    "time": {"from": "now-30m", "to": "now"},
    "panels": panels,
}

out = Path(__file__).parent / "dashboards" / "fraud.json"
out.write_text(json.dumps(dashboard, indent=2), encoding="utf-8")
print(f"wrote {out} with {len(panels)} panels")
