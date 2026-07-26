# AlexG5 live service image.
# CI / dry-run: works on Linux (MetaTrader5 not installed).
# Live trading on the Windows host: prefer BOREX_DEPLOY_MODE=native in
# restart-live.ps1 (MT5 IPC requires Windows Python + local terminal64.exe).
# Docker mode is for packaged restarts when the host already has MT5 + a
# Windows container runtime, or for dry-run containers.

FROM python:3.11-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    BOREX_MAIN_ROOT=/app \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends gcc libpq-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /app/requirements.txt
COPY deploy/requirements-ci.txt /app/deploy/requirements-ci.txt
COPY deploy/borex_live/requirements.txt /app/deploy/borex_live/requirements.txt

# Install strategy + live deps; skip MetaTrader5 on non-Windows.
RUN pip install -r /app/requirements.txt -r /app/deploy/requirements-ci.txt \
    && grep -v -i '^MetaTrader5' /app/deploy/borex_live/requirements.txt \
       > /tmp/live-reqs.txt \
    && pip install -r /tmp/live-reqs.txt \
    && rm /tmp/live-reqs.txt

COPY borex /app/borex
COPY deploy/borex_live /app/deploy/borex_live

WORKDIR /app/deploy/borex_live

EXPOSE 8790

# Default: dry-run smoke. Override CMD on the deploy host for --demo live.
CMD ["python", "mt5service.py", "--dry-run", "--strategy", "alexg5", \
     "--leverage", "5000", "--rr-factor", "2.5", "--no-ui"]
