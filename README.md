# Fraud Detection MLOps Platform

Production lifecycle around the PayGuard XGBoost fraud model: registry, reproducible
training, promotion gate, serving, monitoring, drift, audit and infrastructure.

Status: Phase 1 (registry, reproducible training, validation gate) complete.
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
