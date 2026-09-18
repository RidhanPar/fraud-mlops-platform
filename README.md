# Fraud Detection MLOps Platform

Production lifecycle around the PayGuard XGBoost fraud model: registry, reproducible
training, promotion gate, serving, monitoring, drift, audit and infrastructure.

Status: Phases 1 to 4 complete (registry and gate, serving, monitoring and drift, audit and governance).
The full README with architecture and measured numbers lands in Phase 5.

## Phase 1 quick start

```bash
pip install -r requirements-dev.txt
# place the Kaggle creditcard.csv at data/raw/creditcard.csv
python -m fraud_mlops.train                      # train, register, run the gate
python -m fraud_mlops.train --set model.max_depth=2   # override any param in params.yaml
mlflow ui --backend-store-uri sqlite:///mlflow.db    # browse runs and the registry
pytest
```

## Phase 2 quick start

```bash
python -m fraud_mlops.export_model               # pull the champion into serving_model/
docker build -t fraud-detector:v1 .
docker run -d --name fraud-api -p 8000:8000 fraud-detector:v1
curl http://127.0.0.1:8000/model                 # which version is serving, with lineage
# Load test (generator in its own container, see docs/LOAD_TEST.md)
python -m loadtest.run --export-payloads loadtest/payloads.json
```

Endpoints: `POST /predict`, `POST /predict/batch` (max 1000 rows), `GET /health`,
`GET /ready`, `GET /model`, and interactive docs at `/docs`.

## Phase 3 quick start

```bash
python -m fraud_mlops.export_model      # model + training reference + calibrated drift thresholds
docker compose up -d --build            # API, Postgres, monitor, Prometheus, Alertmanager, Grafana
python scripts/simulate_drift.py        # 12 min replay with an injected upstream incident
# The audit tables are append only, so reset the database with: docker compose down -v
python scripts/report_drift_event.py    # chart and summary from Prometheus
```

Grafana http://localhost:3000, Prometheus http://localhost:9090, Alertmanager http://localhost:9093.
Design and alert policy: [docs/MONITORING.md](docs/MONITORING.md).
The drift event write up: [docs/drift_event/DRIFT_EVENT.md](docs/drift_event/DRIFT_EVENT.md).

## Phase 4 quick start

```bash
docker compose up -d --build                     # now includes the migration job and the auditor
curl http://127.0.0.1:8000/audit/transactions/<id>             # every decision for a transaction
curl -H "X-Client-ID: analyst-1" http://127.0.0.1:8000/explain/<id>   # exact SHAP for a logged decision
export DATABASE_URL=postgresql+psycopg://fraud_app:app_local_only@127.0.0.1:5434/fraud
python -m fraud_mlops.audit.verify               # check row hashes and the seal chain
python -m fraud_mlops.audit.explain_cli --transaction-id <id>  # decisions by older model versions
python scripts/audit_tamper_demo.py              # try to rewrite a decision three ways
python -m fraud_mlops.retrain_policy --window 2m # what should we do right now, and why
```

Design and evidence: [docs/AUDIT.md](docs/AUDIT.md), [docs/RETRAINING_POLICY.md](docs/RETRAINING_POLICY.md).
