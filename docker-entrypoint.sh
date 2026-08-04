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

SEEDED=0
if [ ! -f "$DATA/outreach/pipeline.db" ] && [ -d "$SEED" ]; then
  echo "[entrypoint] empty data dir — seeding $DATA from baked-in snapshot ..."
  mkdir -p "$DATA"
  cp -a "$SEED/." "$DATA/"
  SEEDED=1
fi

# The synthetic Trends demo set is generated, static and read-only to the app, so
# unlike the seed above it is refreshed on EVERY boot — otherwise a volume that was
# already seeded (i.e. all of production) would never receive it and the console's
# "Demo data" toggle would silently stay hidden. Never touches real analysis dirs.
if [ -d "$SEED/demo" ]; then
  echo "[entrypoint] refreshing demo dataset in $DATA/demo ..."
  rm -rf "$DATA/demo"
  cp -a "$SEED/demo" "$DATA/demo"
fi

# Boot marker: boots/seed counters the app exposes at /api/system/status, so a
# non-durable /app/data (no Railway Volume attached) is visible in the UI.
python3 - "$DATA/.boot-marker.json" "$SEEDED" <<'PY'
import json, sys, time
from pathlib import Path
p, seeded = Path(sys.argv[1]), sys.argv[2] == "1"
d = {}
if p.is_file():
    try:
        d = json.loads(p.read_text())
    except ValueError:
        d = {}
now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
d.setdefault("first_boot_at", now)
d["boots"] = int(d.get("boots") or 0) + 1
d["seed_count"] = int(d.get("seed_count") or 0) + (1 if seeded else 0)
d["last_boot_at"] = now
d["seeded_this_boot"] = seeded
p.parent.mkdir(parents=True, exist_ok=True)
p.write_text(json.dumps(d, indent=2))
PY

echo "[entrypoint] starting SDR Console ..."
exec python3 webui/server/app.py
