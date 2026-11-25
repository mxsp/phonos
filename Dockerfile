# syntax=docker/dockerfile:1
# ==============================================================================
# Phonos Dual-Mode Runtime (Playground Sandbox :5040 + Production Lab :5041)
# ==============================================================================
FROM python:3.12-slim AS runtime
WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PHONOS_IN_DOCKER=1 \
    PHONOS_BASE_DATA=/data \
    PHONOS_HOST=0.0.0.0 \
    PHONOS_NO_BROWSER=1 \
    LOCAL_LAB_MODE=true \
    ENABLE_2FA=false

# Shared X11/render libs required by RDKit 2D SVG/PNG molecule drawing on slim images
RUN apt-get update && apt-get install -y --no-install-recommends \
      libxrender1 \
      libxext6 \
      libexpat1 \
      ca-certificates \
  && rm -rf /var/lib/apt/lists/*

COPY requirements.txt pyproject.toml LICENSE DISCLAIMER.md THIRD_PARTY_LICENSES.md ./
RUN pip install --no-cache-dir --upgrade pip \
 && pip install --no-cache-dir -r requirements.txt

COPY . .
RUN pip install --no-cache-dir --no-deps . \
 && useradd --create-home --uid 10001 phonos \
 && mkdir -p /data/playground /data/production /app/logs /app/job_outputs /app/production/user_uploads \
 && chown -R phonos:phonos /data /app

USER phonos
VOLUME ["/data"]
EXPOSE 5040 5041 5555 5556

HEALTHCHECK --interval=30s --timeout=5s --start-period=25s --retries=3 \
  CMD python -c "import os, urllib.request; p = os.environ.get('PHONOS_PORT') or ('5041' if os.environ.get('PHONOS_MODE') == 'production' else '5040'); urllib.request.urlopen(f'http://127.0.0.1:{p}/api/health', timeout=3)" || exit 1

ENTRYPOINT ["python", "-m", "phonos_app.launcher"]
CMD ["--mode", "playground"]
