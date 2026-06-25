#!/bin/sh
# Seed the live data dir from the baked-in snapshot on first boot, then start the server.
#
# /app/data is where the app reads/writes pipeline.db (the HubSpot activity ledger + the
# heyreach_events webhook inbox) and the generated outreach copy. In production a Railway
# Volume is mounted there; on its first boot the Volume is empty, so we copy the committed
# snapshot (stashed at /app/seed-data in the image) into it. We seed ONLY when pipeline.db
# is absent, so later boots never clobber data written at runtime.
set -e

SEED=/app/seed-data
DATA=/app/data

if [ ! -f "$DATA/outreach/pipeline.db" ] && [ -d "$SEED" ]; then
  echo "[entrypoint] empty data dir — seeding $DATA from baked-in snapshot ..."
  mkdir -p "$DATA"
  cp -a "$SEED/." "$DATA/"
fi

echo "[entrypoint] starting SDR Console ..."
exec python3 webui/server/app.py
