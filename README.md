# Fraud Detection MLOps Platform

Production lifecycle around the PayGuard XGBoost fraud model: registry, reproducible
training, promotion gate, serving, monitoring, drift, audit and infrastructure.

Status: Phase 1 (registry, training, gate) and Phase 2 (serving, load test) complete.
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
