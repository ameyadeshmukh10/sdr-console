# syntax=docker/dockerfile:1
#
# SDR Console — single-image deploy (Railway).
# The stack is a Vite/React frontend + a Python-stdlib backend that serves BOTH the
# JSON API and the built SPA from one process. No pip dependencies (stdlib only), so
# the runtime stage just needs python3. The committed data/ ships as a first-boot seed
# for the Railway Volume mounted at /app/data (see docker-entrypoint.sh).

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
