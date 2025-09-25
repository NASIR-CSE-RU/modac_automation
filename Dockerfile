# =============================================================================
# MDAC Automation API — Dockerfile (headed-in-container via Xvfb)
# =============================================================================
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    HEADLESS=0 \
    LOG_NETWORK=0 \
    RECORD_TRACE=1 \
    GATE_WAIT_SECONDS=60

# System libs + Xvfb + xauth (xauth fixes your previous xvfb-run error)
RUN apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates curl \
      xvfb xauth \
      libglib2.0-0 libnss3 libnspr4 \
      libatk1.0-0 libatk-bridge2.0-0 \
      libcups2 libdrm2 libxkbcommon0 \
      libxcomposite1 libxdamage1 libxfixes3 \
      libxrandr2 libgbm1 \
      libasound2 libatspi2.0-0 \
      fonts-liberation fonts-noto fonts-noto-cjk fonts-noto-color-emoji \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Python deps + Playwright (installs Chromium + its OS deps)
COPY requirements.txt /app/requirements.txt
RUN pip install -r requirements.txt \
 && python -m playwright install --with-deps chromium

# App code
COPY . /app

# Non-root user that can write to bind-mounted dirs
RUN useradd -m -u 10001 appuser \
 && mkdir -p /app/downloads /app/videos \
 && chown -R appuser:appuser /app /ms-playwright || true
USER appuser

EXPOSE 8072

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD curl -fsS http://localhost:8072/health || exit 1

# Run FastAPI under a virtual X server to support HEADLESS=0 (headed)
CMD bash -lc "xvfb-run -a --server-args='-screen 0 1920x1080x24 -nolisten tcp' \
  uvicorn main:app --host 0.0.0.0 --port 8072"
