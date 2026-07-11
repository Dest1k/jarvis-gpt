# syntax=docker/dockerfile:1.7
FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV JARVIS_HOME=/runtime
ENV JARVIS_MODEL_ROOT=/runtime/data/models
ENV HOME=/home/jarvis
ENV CHROME_BIN=/usr/bin/chromium
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

WORKDIR /app

RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt/lists,sharing=locked \
    sed -i 's|http://deb.debian.org|https://deb.debian.org|g' \
      /etc/apt/sources.list.d/debian.sources \
    && printf '%s\n' \
      'Acquire::Retries "5";' \
      'Acquire::https::Timeout "120";' \
      > /etc/apt/apt.conf.d/80jarvis-retries \
    && rm -rf /var/lib/apt/lists/* \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
      ca-certificates \
      chromium \
      chromium-sandbox \
      fonts-liberation \
      gosu \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 jarvis \
    && useradd --create-home --uid 10001 --gid jarvis --shell /usr/sbin/nologin jarvis \
    && mkdir -p /runtime \
    && chown jarvis:jarvis /runtime

COPY requirements.txt /app/backend/requirements.txt
RUN pip install --no-cache-dir -r /app/backend/requirements.txt
RUN python -m playwright install --with-deps --only-shell chromium \
    && chmod -R a+rX /ms-playwright \
    && rm -rf /var/lib/apt/lists/*

COPY src /app/backend/src
COPY --chmod=755 docker-entrypoint.sh /usr/local/bin/jarvis-docker-entrypoint

RUN test -u /usr/lib/chromium/chrome-sandbox \
    && gosu jarvis chromium --version | grep -q '^Chromium ' \
    && gosu jarvis python -c "from playwright.sync_api import sync_playwright; p=sync_playwright().start(); b=p.chromium.launch(headless=True); b.close(); p.stop()"

ENV PYTHONPATH=/app/backend/src
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD gosu jarvis python -c "import os, shutil, urllib.request; from pathlib import Path; assert shutil.which('chromium'); assert list(Path(os.environ['PLAYWRIGHT_BROWSERS_PATH']).glob('chromium_headless_shell-*')); urllib.request.urlopen(f\"http://127.0.0.1:{os.getenv('JARVIS_API_PORT', '8000')}/health\", timeout=3).read()"

ENTRYPOINT ["jarvis-docker-entrypoint"]
CMD ["sh", "-c", "exec uvicorn jarvis_gpt.main:app --host \"${JARVIS_API_HOST:-0.0.0.0}\" --port \"${JARVIS_API_PORT:-8000}\""]
