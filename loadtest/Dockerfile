# Load generator image: runs next to the service on the Docker network,
# so the measurement skips the Windows host port proxy.
FROM python:3.11-slim
RUN pip install --no-cache-dir httpx==0.28.1 numpy==2.4.6
WORKDIR /lt
COPY run.py __init__.py loadtest/
# Real holdout transactions, exported with: python -m loadtest.run --export-payloads loadtest/payloads.json
COPY payloads.json /lt/payloads.json
ENTRYPOINT ["python", "-m", "loadtest.run"]
