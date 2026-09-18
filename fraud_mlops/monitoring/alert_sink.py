"""Stand in for PagerDuty or Slack: receives Alertmanager webhooks and appends
each alert to a JSON lines file, so fired alerts leave a durable record.

    python -m fraud_mlops.monitoring.alert_sink
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

LOG = Path(os.environ.get("ALERT_LOG", "/data/alerts.jsonl"))


class Handler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        received = datetime.now(timezone.utc).isoformat()
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with LOG.open("a", encoding="utf-8") as f:
            for alert in payload.get("alerts", []):
                record = {
                    "received_at": received,
                    "status": alert["status"],
                    "alertname": alert["labels"].get("alertname"),
                    "severity": alert["labels"].get("severity"),
                    "summary": alert["annotations"].get("summary"),
                    "starts_at": alert.get("startsAt"),
                    "ends_at": alert.get("endsAt"),
                }
                f.write(json.dumps(record) + "\n")
                print(json.dumps(record), flush=True)
        self.send_response(200)
        self.end_headers()

    def log_message(self, *args) -> None:
        pass


if __name__ == "__main__":
    HTTPServer(("0.0.0.0", int(os.environ.get("PORT", "9099"))), Handler).serve_forever()
