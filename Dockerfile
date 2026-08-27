FROM python:3.12-slim

# WEB_CONCURRENCY=1 pins the single-process requirement (#161) instead of
# leaning on uvicorn's implicit default. Simulation run/stop state lives in
# per-process memory, so a second worker serves a control plane that cannot
# stop the first one's run loop. Stating it here makes the safe value part of
# the image rather than an absence, and a platform variable that overrides it
# to something above 1 is refused at startup by app/worker_guard.py.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    REGENGINE_DATA_DIR=/data \
    REGENGINE_REQUIRE_AUTH=1 \
    WEB_CONCURRENCY=1

WORKDIR /app
ENV PATH="/app/.venv/bin:$PATH"

RUN apt-get update \
    && apt-get install -y --no-install-recommends gosu \
    && rm -rf /var/lib/apt/lists/* \
    && adduser --disabled-password --gecos "" appuser \
    && mkdir -p /data \
    && chown -R appuser:appuser /data /app

COPY pyproject.toml uv.lock README.md ./
RUN pip install --no-cache-dir uv \
    && uv sync --frozen --no-dev --no-install-project \
    && chown -R appuser:appuser /app

COPY --chown=appuser:appuser app ./app
COPY --chown=appuser:appuser scripts ./scripts
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh

RUN chmod +x /usr/local/bin/docker-entrypoint.sh

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD python -c "import json, os, urllib.request; port=os.getenv('PORT', '8000'); json.load(urllib.request.urlopen(f'http://127.0.0.1:{port}/api/healthz', timeout=3))"

ENTRYPOINT ["docker-entrypoint.sh"]
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
