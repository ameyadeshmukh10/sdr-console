"""Demo mode — serve the whole console from a synthetic profile instead of live data.

A "profile" is a self-contained mirror of the live `data/` tree living at
`data/demo/<profile-id>/`, plus a `profile.json` manifest describing itself. Point
the console at one and every read the API does resolves into that tree, so contacts,
signals, generated copy, replies, campaign stats and trends are all internally
consistent — a customer-tailored dataset rather than a single fake chart.

How the switch travels: the client sends `X-Demo-Profile: <id>` on every request;
the request handler validates it and stashes it in a thread-local for the duration
of that request only. `ThreadingHTTPServer` gives each request its own thread, so:

  * the active profile can never bleed between concurrent requests, and
  * background daemon threads (activity autosync, AI SDR attribution, unenrollment
    sweeps) never see a profile at all — they default to None, i.e. live data.
    That property is load-bearing: those threads WRITE to HubSpot and Bison, and
    must always act on reality.

Missing files inside a profile are deliberately NOT backfilled from live data. A
half-populated profile shows empty states — the read endpoints already degrade
gracefully — because silently mixing real customer data into a demo is worse than
showing nothing. `covers` in the manifest lets the UI label those empties as
"not part of this profile" instead of "no data".

Stdlib only, and cheap to import (the server must boot with this present but no
profiles on disk).
"""

import json
import re
import threading
from pathlib import Path

# Profiles live under data/demo/<id>/. Ids are filesystem names, so they are
# validated hard — this string arrives in an HTTP header from the client.
DEMO_SUBDIR = "demo"
_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")

# Areas a profile can cover. Used only for labelling in the UI; the resolver does
# not consult it, so a manifest that lies produces empty panels, not wrong data.
KNOWN_AREAS = ("pipeline", "signals", "outreach", "replies", "trends", "analytics",
               "campaigns")

_ctx = threading.local()


def _root(data_root):
    return Path(data_root) / DEMO_SUBDIR


def valid_id(profile_id):
    return bool(profile_id) and bool(_ID_RE.match(str(profile_id)))


def list_profiles(data_root):
    """Every profile on disk, newest-labelled first. Never raises."""
    out = []
    root = _root(data_root)
    if not root.is_dir():
        return out
    for entry in sorted(root.iterdir()):
        if not entry.is_dir() or not valid_id(entry.name):
            continue
        meta = {}
        manifest = entry / "profile.json"
        if manifest.is_file():
            try:
                loaded = json.loads(manifest.read_text())
                if isinstance(loaded, dict):
                    meta = loaded
            except (ValueError, OSError):
                meta = {}
        covers = [a for a in (meta.get("covers") or []) if a in KNOWN_AREAS]
        out.append({
            "id": entry.name,
            "label": meta.get("label") or entry.name,
            "description": meta.get("description") or "",
            "customer": meta.get("customer") or None,
            "covers": covers,
            "generated_at": meta.get("generated_at"),
            "has_manifest": manifest.is_file(),
            # True when the accounts are REAL companies (e.g. a customer's actual
            # target list) carrying synthetic engagement. The UI must then say so
            # precisely: the names are real, the replies and deals are not.
            "contains_real_accounts": bool(meta.get("contains_real_accounts")),
        })
    return out


def profile_exists(data_root, profile_id):
    return valid_id(profile_id) and (_root(data_root) / profile_id).is_dir()


def db_path(data_root, profile_id):
    """The profile's OWN pipeline.db, opened read-write for demo actions.

    See the writability note below: demo writes land here, never in live data.
    Returns None when no profile is active, so callers fall through to live.
    """
    if not profile_id:
        return None
    return _root(data_root) / profile_id / "outreach" / "pipeline.db"


# --- what a demo may actually DO --------------------------------------------
# The original rule was "demo mode is read-only: every POST 409s". That was the
# right instinct expressed as the wrong invariant. What must never happen is an
# OUTWARD EFFECT — mailing a real prospect, writing to the customer's CRM, spending
# Clay or Prospeo credits, calling the model. Writing to the profile's own synthetic
# sqlite file has none of those properties, and forbidding it made the demo unable
# to show the product's central act: building and running a campaign.
#
# So the invariant is now: A DEMO MAY WRITE TO ITS OWN DATASET AND NOTHING ELSE.
#
# Concretely, an allowed action must satisfy all three:
#   1. every write goes to data/demo/<id>/… (never the live tree)
#   2. no HTTP call leaves the process
#   3. no metered credit and no send is actually consumed
#
# Anything failing one of those stays refused. The list is exact-match or regex —
# never a prefix — so a new sibling endpoint is refused by default rather than
# silently inheriting permission.
_DEMO_WRITE_PATHS = {
    "/api/campaigns",                    # create a campaign
    "/api/campaigns/audience/preview",   # resolves against the profile's own contacts
    # A dropped CSV/XLSX. Parsing writes nothing; the import writes contacts into
    # the PROFILE's own pipeline and deliberately skips the CRM leg the live path
    # runs (see demo_actions.simulate_file_import) — no portal is touched.
    "/api/campaigns/audience/upload",
    "/api/campaigns/audience/import",
    "/api/campaigns/hotlist/refresh",    # recomputes from the profile's own members
    "/api/ingest",                       # SIMULATED pull from the demo CRM pool
    # Local configuration with no outward effect. A demo has to be able to show the
    # buyer group being edited — it is the definition the whole targeting story
    # rests on — and editing it writes one row in the profile's own DB.
    "/api/buyer-group",
    # Reports are SELECT-only over the profile's own DB; describe is fixture-backed.
    "/api/reports/run",
    "/api/reports/describe",
    # What counts as a signal here is local configuration, and a demo has to be able
    # to show it being defined — the rule reads the profile's own contacts and its
    # simulated CRM (demo_actions.DemoCRM), and writes events into the profile DB.
    "/api/signals/definitions",
    "/api/signals/definitions/preview",
    # Working the call list. Both write one row in the profile's own DB and reach
    # nothing outward — a demo has to be able to show a rep actually working a list.
    "/api/calllist/member",
    "/api/calllist/engagement",
    # The proof library is local config — one row in the profile's own DB.
    "/api/references",
    "/api/references/attach",
}
_DEMO_WRITE_RES = [
    re.compile(r"^/api/campaigns/\d+$"),                  # patch definition
    re.compile(r"^/api/campaigns/\d+/(delete|steps|steps/delete)$"),
    re.compile(r"^/api/campaigns/\d+/(qualify|rescore|relaunch)$"),
    re.compile(r"^/api/campaigns/\d+/(discover|enrich)$"),  # SIMULATED sources
    re.compile(r"^/api/campaigns/\d+/suggest$"),            # fixture-backed copy
    re.compile(r"^/api/signals/definitions/[a-z0-9_]+/(run|delete)$"),
]


def writable(path):
    """True if a demo profile may perform this POST against its own dataset."""
    return path in _DEMO_WRITE_PATHS or any(r.match(path) for r in _DEMO_WRITE_RES)


def resolve(data_root, profile_id, path):
    """Map a live path under `data_root` into `profile_id`'s tree.

    Paths outside `data_root` (scripts, the frontend bundle, temp dirs) pass
    through untouched — only data reads are redirected.
    """
    if not profile_id:
        return path
    p = Path(path)
    try:
        rel = p.relative_to(Path(data_root))
    except ValueError:
        return path
    return _root(data_root) / profile_id / rel


# --- per-request context ----------------------------------------------------

def set_active(profile_id):
    _ctx.profile = profile_id or None


def clear():
    _ctx.profile = None


def active():
    """The profile for THIS thread, or None for live data (the default)."""
    return getattr(_ctx, "profile", None)


def is_demo():
    return active() is not None
