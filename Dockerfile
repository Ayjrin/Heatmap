# This builds natively on Apple Silicon; Terraform defaults to Fargate ARM64.
FROM node:22-bookworm-slim AS node
FROM python:3.12-slim-bookworm
COPY --from=node /usr/local/bin/node /usr/local/bin/node
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONPATH=/app/src
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY src ./src
COPY scripts ./scripts
COPY web ./web
COPY config.yaml ./
RUN useradd --uid 10001 --create-home collector && mkdir -p /app/data && chown -R collector:collector /app
USER collector
CMD ["python", "-m", "proleague.pipeline", "--help"]
