# syntax=docker/dockerfile:1
#
# One Dockerfile, two images:
#
#   docker build -t gateway .                                 # the gateway (default)
#   docker build -t dummy-upstream --target dummy-upstream .  # the test backend
#
# The dummy upstream is its own target so the test backend never ships
# inside the gateway image.

# ---- dependencies -------------------------------------------------------------
FROM python:3.13-slim AS deps

ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
# A virtualenv, so the runtime stages can copy one folder and get every
# dependency, without pip or build leftovers.
RUN python -m venv /venv
# requirements.txt alone first: this layer is reused until the dependencies
# change, so editing code doesn't reinstall them.
COPY requirements.txt .
RUN /venv/bin/pip install -r requirements.txt

# ---- shared runtime base ------------------------------------------------------
FROM python:3.13-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH=/venv/bin:$PATH
COPY --from=deps /venv /venv
RUN useradd --system --uid 10001 --no-create-home --shell /usr/sbin/nologin app
WORKDIR /app
# Code is owned by root and only readable by "app": a compromised process
# can't rewrite the code it runs.
USER app

# ---- dummy upstream -----------------------------------------------------------
FROM runtime AS dummy-upstream

COPY dummy_upstream ./dummy_upstream
EXPOSE 8001
HEALTHCHECK --interval=5s --timeout=3s --start-period=5s --retries=5 \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8001/', timeout=2)"]
CMD ["uvicorn", "dummy_upstream.main:app", "--host", "0.0.0.0", "--port", "8001", "--no-access-log"]

# ---- gateway (last, so it's the default target) -------------------------------
FROM runtime AS gateway

COPY app ./app
# A default config so the image runs on its own. docker-compose and
# Kubernetes mount their own over /app/config.
COPY config.yaml ./config/config.yaml

ENV GATEWAY_HOST=0.0.0.0 \
    GATEWAY_PORT=8000 \
    GATEWAY_METRICS_PORT=9100 \
    GATEWAY_CONFIG=/app/config/config.yaml
# 8000: API (public). 9100: Prometheus metrics (keep internal).
EXPOSE 8000 9100

# Liveness, using the same /health Kubernetes will use. Python's urllib,
# since the slim image has no curl.
HEALTHCHECK --interval=5s --timeout=3s --start-period=10s --retries=5 \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=2)"]

# Exec form: python is PID 1 and gets SIGTERM directly, so `docker stop`
# triggers the graceful drain. A shell wrapper would swallow the signal.
CMD ["python", "-m", "app"]
