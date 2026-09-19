FROM python:3.11-slim

# XGBoost needs the OpenMP runtime. Security updates are applied at build time;
# curl is deliberately not installed (ECR scanning flagged 2 critical CVEs in it,
# and the health check below needs nothing beyond Python).
RUN apt-get update && apt-get upgrade -y && apt-get install -y --no-install-recommends libgomp1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements-serve.txt .
RUN pip install --no-cache-dir -r requirements-serve.txt

COPY fraud_mlops/ fraud_mlops/
# One image = one model version. Rollback = redeploy the previous image tag.
COPY serving_model/ serving_model/

RUN useradd --create-home --uid 1000 app
USER app

ENV MODEL_DIR=/app/serving_model \
    WORKERS=2 \
    PYTHONUNBUFFERED=1 \
    PROMETHEUS_MULTIPROC_DIR=/tmp/prometheus
EXPOSE 8000

HEALTHCHECK --interval=15s --timeout=3s --start-period=20s \
    CMD python -c "import urllib.request,sys; sys.exit(urllib.request.urlopen('http://127.0.0.1:8000/ready', timeout=2).status != 200)"

# Metric files from a previous run would be merged into the new one, so clear them first.
CMD ["sh", "-c", "rm -rf $PROMETHEUS_MULTIPROC_DIR && mkdir -p $PROMETHEUS_MULTIPROC_DIR && exec uvicorn fraud_mlops.api.main:app --host 0.0.0.0 --port 8000 --workers ${WORKERS}"]
