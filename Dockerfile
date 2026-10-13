# Fletcher Fortis: multi-stage image. Stage 1 builds the Eraser bulk-CCPA binary, stage 2 is Playwright (Chromium) + the app.
FROM docker.io/library/golang:1.26.9-bookworm AS eraser
WORKDIR /src
COPY eraser/go.mod eraser/go.sum ./
RUN go mod download
COPY eraser/ .
RUN CGO_ENABLED=0 go build -trimpath -ldflags="-s -w" -o /eraser ./cmd/eraser && /eraser --help >/dev/null

FROM mcr.microsoft.com/playwright/python:v1.63.0-noble
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright HOME=/tmp \
    DB_PATH=/data/optout.db SHOT_DIR=/data/shots AUTH_DIR=/data/auth HEALTH_FILE=/data/site_health.json BROKER_OUT=/data/brokers_merged.yaml \
    PROFILES_DIR=/config/profiles SKIP_FILE=/app/skip_ids.txt BROKERS_FILE=/app/brokers.yaml DRY_RUN=true
COPY requirements.txt /app/requirements.txt
RUN pip install --break-system-packages -r /app/requirements.txt 2>/dev/null || pip install -r /app/requirements.txt
COPY --from=eraser /eraser /usr/local/bin/eraser
COPY eraser/data/brokers.yaml /app/brokers.yaml
COPY config/skip_ids.txt /app/skip_ids.txt
COPY brokers/ /app/brokers/
COPY optout/ /app/optout/
# non-root user; /data and /config are the only writable paths (root filesystem can be mounted read-only)
RUN groupadd -g 10001 ff && useradd -u 10001 -g ff -M -d /tmp -s /usr/sbin/nologin ff \
 && mkdir -p /data /config/profiles \
 && { chown -R ff:ff /data /config && chmod 700 /data; } || chmod 1777 /data /config /config/profiles \
 && python -m compileall -q /app/optout && rm -rf /app/optout/__pycache__/*.pyc.* \
 && find /app -type f -exec chmod a-w {} +
WORKDIR /app/optout
USER 10001:10001
VOLUME ["/data"]
EXPOSE 8080
HEALTHCHECK --interval=60s --timeout=5s --start-period=30s --retries=3 CMD ["python", "-c", "import urllib.request,sys;sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/healthz',timeout=4).status==200 else 1)"]
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080", "--proxy-headers", "--forwarded-allow-ips", "*", "--no-server-header"]
