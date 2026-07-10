# syntax=docker/dockerfile:1
#
# SDR Console — single-image deploy (Railway).
# The stack is a Vite/React frontend + a Python backend that serves BOTH the
# JSON API and the built SPA from one process. The backend is stdlib except for
# requirements.txt (pymongo, for the AI SDR attribution store). The committed data/
# ships as a first-boot seed for the Railway Volume mounted at /app/data (see
# docker-entrypoint.sh).

# ---- Stage 1: build the Vite/React frontend -> webui/frontend/dist ----
FROM node:20-slim AS frontend
WORKDIR /app/webui/frontend
# Install deps first (cached unless the lockfile changes), then build.
COPY webui/frontend/package.json webui/frontend/package-lock.json ./
RUN npm ci
COPY webui/frontend/ ./
RUN npm run build

# ---- Stage 2: runtime (Python stdlib serves API + built SPA) ----
FROM python:3.12-slim AS runtime
ENV PYTHONUNBUFFERED=1
WORKDIR /app
# Python deps first so the pip layer caches unless requirements.txt changes.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
# Node 20 + headless Chromium power the Signal Playbook deck renderer (Vite
# single-file build + Playwright PDF export). Browsers install to a fixed path
# OUTSIDE /app so the later COPY can't shadow them.
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates \
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*
# Renderer deps first (cached unless its lockfile changes), then the Chromium
# matching the pinned @playwright/test, with its system libraries.
COPY deck-renderer/package.json deck-renderer/package-lock.json ./deck-renderer/
RUN npm ci --prefix deck-renderer --no-audit --no-fund \
    && cd deck-renderer && npx playwright install --with-deps chromium \
    && rm -rf /var/lib/apt/lists/*
# Backend + .claude pipeline scripts + committed data + docs (see .dockerignore for exclusions).
COPY . .
# Bring in the built frontend from stage 1 (dist is gitignored, so it isn't in the context).
COPY --from=frontend /app/webui/frontend/dist ./webui/frontend/dist
# Stash the committed data as a one-time seed; /app/data is the live dir (a Railway Volume in
# prod). The entrypoint copies the seed into /app/data only when the Volume is empty.
RUN mv data seed-data && mkdir -p data && chmod +x docker-entrypoint.sh
# Documentation only — Railway injects $PORT and app.py binds 0.0.0.0:$PORT.
EXPOSE 8080
ENTRYPOINT ["./docker-entrypoint.sh"]
