"""Campaign endpoints for the console: read payloads, write handlers, copy suggestion.

Split out of app.py, which already carries the whole API surface. Follows the two
conventions that file establishes:

  * READ payloads degrade, never 500. A pipeline DB that predates the campaign
    tables (an old demo profile, a stale volume) reports an empty console rather
    than an error page.
  * WRITES go through batch_db.connect() (read-write WAL), the same escape hatch the
    HeyReach webhook uses — app.db_connect() is deliberately mode=ro. Every campaign
    write is a POST, so demo mode's blanket POST guard already refuses them all
    before dispatch; nothing here needs its own demo check.

`import campaigns` / `import batch_db` resolve because app.py puts the pipeline
scripts dir on sys.path before importing this module.
"""

import json
import os
import re
import sys
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import batch_db as db
import campaigns as C
import audiences as A
import capacity as CAP
import crm_sync
import demo_actions
import campaign_brief
import contact_import
import crm_signals

# Discovery job registry. Discovery makes network calls and can run for minutes, so
# it is a background job the UI polls, not a blocking request. Its own registry
# (rather than sharing TECH_JOBS) keeps "is a campaign discovering?" answerable
# without colliding with the Signals view's own tech/hiring backfills.
DISCOVERY_JOBS = {}
_DISCOVERY_LOCK = threading.Lock()
_DISCOVERY_SEQ = [0]

# Clay enrichment runs on the same registry shape but is kept separate: it spends
# money, so "is this campaign spending Clay credits right now?" has to be answerable
# without untangling it from the free DNS/HTTP scans.
ENRICH_JOBS = {}
_ENRICH_LOCK = threading.Lock()
_ENRICH_SEQ = [0]


def _empty(reason=None):
    return {"campaigns": [], "ctas": [], "available": False, "error": reason}


def _tables_present(conn):
    return bool(conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='campaigns'").fetchone())


# ---- reads -----------------------------------------------------------------
def campaigns_payload(conn):
    """The campaign list + the offer library, for the Campaigns index."""
    try:
        if not _tables_present(conn):
            return _empty("campaign tables not present in this dataset")
        rows = db.list_campaigns(conn)
        for r in rows:
            r["counts"] = db.campaign_counts(conn, r["campaign_id"])
            r["steps"] = len(db.get_steps(conn, r["campaign_id"]))
            r["window_days_left"] = _days_left(r.get("window_end"))
        return {"campaigns": rows, "ctas": db.list_ctas(conn), "available": True,
                "signal_counts": db.signal_event_counts(conn, days=30)}
    except Exception as e:  # noqa: BLE001
        return _empty(f"{type(e).__name__}: {e}")


def _days_left(window_end):
    if not window_end:
        return None
    try:
        end = datetime.strptime(window_end[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None
    return (end - datetime.now(timezone.utc)).days


def campaign_detail_payload(conn, campaign_id, member_limit=500):
    """One campaign: definition, the step->CTA sequence, membership, and a preview
    of what its signal query currently matches."""
    try:
        if not _tables_present(conn):
            return {"error": "campaign tables not present in this dataset"}
        camp = db.get_campaign(conn, campaign_id)
        if not camp:
            return {"error": "not found"}
        out = {
            "campaign": camp,
            "steps": C.step_plan(conn, campaign_id),
            "counts": db.campaign_counts(conn, campaign_id),
            "members": C.attach_money(
                db.campaign_members(conn, campaign_id, limit=member_limit)),
            "ctas": db.list_ctas(conn),
            "window_days_left": _days_left(camp.get("window_end")),
            "plan_prompt": C.render_plan_prompt(C.step_plan(conn, campaign_id)),
        }
        # What the definition matches RIGHT NOW, so the window/filter edits have
        # visible consequences before anything is committed. Never fatal.
        try:
            out["match_preview"] = C.qualify(conn, camp, commit=False,
                                             audience_crm=_audience_crm())
        except Exception as e:  # noqa: BLE001
            out["match_preview"] = {"error": f"{type(e).__name__}: {e}"}
        # How many in-scope accounts have never been scanned — the size of the
        # "find accounts" opportunity, which is the number that explains an empty
        # campaign better than "0 matches" does.
        try:
            scope = C.discovery_scope(conn, camp, limit=2000)
            out["discovery"] = {
                "unscanned_accounts": len(scope),
                "sample": scope[:10],
                "last_run_at": camp.get("last_discovery_at"),
                "interval_days": camp.get("discovery_interval_days"),
                "due": C.discovery_due(camp),
                "running": any(j["status"] == "running" and j["campaign_id"] == campaign_id
                               for j in DISCOVERY_JOBS.values()),
            }
        except Exception as e:  # noqa: BLE001
            out["discovery"] = {"error": f"{type(e).__name__}: {e}"}
        # Enrichment scope: where the buyer group is thinnest, and what filling it
        # would cost. Never fatal — Clay being down must not blank the campaign.
        try:
            out["enrichment"] = {
                **C.enrich_estimate(conn, camp, limit=25),
                "last_run_at": camp.get("last_enrich_at"),
                "running": any(j["status"] == "running" and j["campaign_id"] == campaign_id
                               for j in ENRICH_JOBS.values()),
            }
        except Exception as e:  # noqa: BLE001
            out["enrichment"] = {"error": f"{type(e).__name__}: {e}"}
        try:
            out["audience_desc"] = A.describe(camp.get("audience"))
        except Exception:  # noqa: BLE001
            out["audience_desc"] = None
        try:
            out["capacity"] = CAP.status(conn)
            ads = CAP.ad_audience(out["members"])
            out["ad_audience"] = ads[:25]
            # What an ad channel would actually reach if it were switched on: the
            # accounts with a mapped committee. An ads checkbox with no number
            # beside it is decoration.
            out["ad_reach"] = {
                "accounts": len(ads),
                "contacts": sum(a.get("contacts", 0) or 0 for a in ads),
            }
        except Exception:  # noqa: BLE001
            out["capacity"], out["ad_audience"] = None, []
            out["ad_reach"] = None
        out["channel_keys"] = list(CHANNEL_KEYS)
        out["roadmap_channels"] = list(ROADMAP_CHANNELS)
        return out
    except Exception as e:  # noqa: BLE001
        return {"error": f"{type(e).__name__}: {e}"}


def signal_events_payload(conn, days=90, limit=300):
    """Recent signal observations — what a campaign window would actually catch.

    This is the view account_signals could not provide: its rows are the latest
    value per domain with a 'when we checked' timestamp, not a history of what
    fired when."""
    try:
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                            "AND name='signal_events'").fetchone():
            return {"events": [], "counts": {}, "available": False}
        start = (datetime.now(timezone.utc) - timedelta(days=int(days))).strftime(
            "%Y-%m-%dT%H:%M:%SZ")
        events = db.signal_events_in_window(conn, start=start)
        return {"events": events[:limit], "total": len(events), "available": True,
                "counts": db.signal_event_counts(conn, days=days),
                "by_week": _by_week(events, conn)}
    except Exception as e:  # noqa: BLE001
        return {"events": [], "counts": {}, "available": False, "error": str(e)}


def _by_week(events, conn=None):
    """[{week, <kind>: n, ...}] oldest-first, for the window picker chart.

    Buckets are keyed off the LIVE signal registry rather than a hardcoded three,
    so a kind this deployment defined charts itself."""
    zero = {k: 0 for k in C.signal_registry(conn)}
    buckets = {}
    for ev in events:
        wk = (ev.get("observed_at") or "")[:10]
        if not wk:
            continue
        try:
            d = datetime.strptime(wk, "%Y-%m-%d")
        except ValueError:
            continue
        key = (d - timedelta(days=d.weekday())).strftime("%Y-%m-%d")
        b = buckets.setdefault(key, {"week": key, **zero})
        if ev["kind"] in b:
            b[ev["kind"]] += 1
    return [buckets[k] for k in sorted(buckets)]


# ---- writes ----------------------------------------------------------------
# Set by app.py at import: () -> Path|None, the active demo profile's own DB.
# Injected rather than imported so this module stays usable from a CLI context.
demo_db_path = lambda: None       # noqa: E731
demo_dir_path = lambda: None      # noqa: E731


def _demo_paths():
    """(profile dir, profile db) for the active demo, or (None, None) when live."""
    return demo_dir_path(), demo_db_path()


# The channels a campaign may declare. `ads` is DECLARED, not wired: the
# advertising agent is roadmap, so checking it records the intent (and sizes the
# audience from the buying groups we already mapped) without pretending anything is
# bought. Saying so in one place keeps the UI from having to guess.
CHANNEL_KEYS = ("email", "linkedin", "ads")
ROADMAP_CHANNELS = ("ads",)


def _validate_channels(raw):
    if raw in (None, ""):
        return None
    if not isinstance(raw, dict):
        raise ValueError("channels must be an object")
    unknown = set(raw) - set(CHANNEL_KEYS)
    if unknown:
        raise ValueError(f"unknown channels: {', '.join(sorted(unknown))}")
    return {k: bool(raw.get(k)) for k in CHANNEL_KEYS}


def _audience_crm():
    """Where a hubspot_list / crm_query audience resolves from for THIS request.

    None (live) means the real portal. Under a demo it is the profile's simulated
    CRM — otherwise both of those audience types answer with "HUBSPOT_ACCESS_TOKEN
    is not set" on the first step of building a campaign, which is precisely the
    not-configured notice a demo must never show."""
    demo_dir, demo_db = _demo_paths()
    return demo_actions.DemoCRM(demo_dir, demo_db) if demo_dir else None


def _write_conn():
    """Read-write connection for campaign mutations.

    Routed to the DEMO profile's own pipeline.db when one is active, so a demo can
    build and run a campaign without any of it reaching live data. Per-call rather
    than a global path swap — the server is threaded and a global would leak one
    request's profile into another's writes."""
    conn = db.connect(demo_db_path())
    db.init_schema(conn)
    return conn


def create_campaign(body):
    """Create + seed the default 4-touch cadence, so a new campaign already has its
    step->CTA links populated instead of an empty sequence."""
    name = str(body.get("name") or "").strip()
    if not name:
        raise ValueError("name is required")
    aud = A.validate_audience(body.get("audience"))
    # REJECT an unknown type rather than coercing it to outbound. Coercion is the
    # dangerous default here: a typo'd "inbound" would silently produce an outbound
    # campaign, and the copy would cold-open at people who raised their hand.
    ctype = str(body.get("campaign_type") or "outbound")
    if ctype not in C.CAMPAIGN_TYPES:
        raise ValueError(f"campaign_type must be one of {list(C.CAMPAIGN_TYPES)}")
    ever_days = (_int_or_none(body.get("evergreen_interval_days"))
                 or C.DEFAULT_EVERGREEN_INTERVAL_DAYS)
    conn = _write_conn()
    try:
        # Validated with a connection so a custom signal kind is accepted.
        sq = C.validate_signal_query(body.get("signal_query"), conn)
        camp = db.create_campaign(
            conn, name,
            description=body.get("description"),
            brief=_str_or_none(body.get("brief")),
            campaign_type=ctype,
            status=body.get("status") or "draft",
            audience=aud,
            channels=_validate_channels(body.get("channels")),
            window_start=_date(body.get("window_start")),
            window_end=_date(body.get("window_end")),
            signal_query=sq,
            membership_mode=body.get("membership_mode") or "rolling",
            variant=body.get("variant"),
            bison_campaign_id=_str_or_none(body.get("bison_campaign_id")),
            heyreach_campaign_id=_str_or_none(body.get("heyreach_campaign_id")),
            target_accounts=_int_or_none(body.get("target_accounts")),
            discovery_interval_days=_int_or_none(body.get("discovery_interval_days")),
            evergreen=1 if body.get("evergreen") else 0,
            evergreen_interval_days=ever_days,
            review_due_at=(datetime.now(timezone.utc)
                           + timedelta(days=ever_days)).strftime("%Y-%m-%d")
                          if body.get("evergreen") else None)
        if body.get("seed_sequence", True):
            db.seed_default_sequence(conn, camp["campaign_id"])
        return {"campaign": camp, "steps": C.step_plan(conn, camp["campaign_id"])}
    finally:
        conn.close()


def update_campaign(campaign_id, body):
    """Patch semantics: only the keys present in the body are written."""
    fields = {}
    for k in ("name", "description", "brief", "membership_mode", "variant"):
        if k in body:
            fields[k] = body[k]
    if "campaign_type" in body:
        t = str(body["campaign_type"] or "outbound")
        if t not in C.CAMPAIGN_TYPES:
            raise ValueError(f"campaign_type must be one of {list(C.CAMPAIGN_TYPES)}")
        fields["campaign_type"] = t
    if "evergreen" in body:
        fields["evergreen"] = 1 if body["evergreen"] else 0
        # Turning evergreen ON schedules the first review; turning it off clears
        # the schedule so a paused review can't strand the campaign.
        if fields["evergreen"]:
            days = _int_or_none(body.get("evergreen_interval_days")) \
                or C.DEFAULT_EVERGREEN_INTERVAL_DAYS
            fields.setdefault("review_due_at", _date(body.get("review_due_at"))
                              or (datetime.now(timezone.utc)
                                  + timedelta(days=int(days))).strftime("%Y-%m-%d"))
        else:
            fields["review_state"] = None
            fields["review_due_at"] = None
    if "evergreen_interval_days" in body:
        fields["evergreen_interval_days"] = _int_or_none(body["evergreen_interval_days"])
    for k in ("window_start", "window_end"):
        if k in body:
            fields[k] = _date(body[k])
    for k in ("bison_campaign_id", "heyreach_campaign_id"):
        if k in body:
            fields[k] = _str_or_none(body[k])
    if "target_accounts" in body:
        fields["target_accounts"] = _int_or_none(body["target_accounts"])
    if "discovery_interval_days" in body:
        fields["discovery_interval_days"] = _int_or_none(body["discovery_interval_days"])
    if "signal_query" in body:
        fields["signal_query"] = C.validate_signal_query(body["signal_query"], conn)
    if "audience" in body:
        fields["audience"] = A.validate_audience(body["audience"])
    if "channels" in body:
        fields["channels"] = _validate_channels(body["channels"])
    if "status" in body:
        status = str(body["status"])
        if status not in db.CAMPAIGN_STATUSES:
            raise ValueError(f"status must be one of {list(db.CAMPAIGN_STATUSES)}")
        fields["status"] = status
        # Stamp the lifecycle transitions rather than making the UI send them.
        if status == "active":
            fields["launched_at"] = db.now()
        elif status == "completed":
            fields["completed_at"] = db.now()
    conn = _write_conn()
    try:
        if not db.get_campaign(conn, campaign_id):
            raise LookupError("campaign not found")
        camp = db.update_campaign(conn, campaign_id, **fields)
        return {"campaign": camp}
    finally:
        conn.close()


def delete_campaign(campaign_id):
    conn = _write_conn()
    try:
        return {"deleted": db.delete_campaign(conn, campaign_id)}
    finally:
        conn.close()


def upsert_step(campaign_id, body):
    """Create or patch one sequence step. cta_key is validated against the library —
    a step pointing at a nonexistent offer would silently generate an unanchored CTA."""
    try:
        step_no = int(body.get("step_no"))
    except (TypeError, ValueError):
        raise ValueError("step_no must be an integer")
    channel = str(body.get("channel") or "email")
    if channel not in ("email", "linkedin"):
        raise ValueError("channel must be email or linkedin")
    fields = {}
    conn = _write_conn()
    try:
        if not db.get_campaign(conn, campaign_id):
            raise LookupError("campaign not found")
        if "cta_key" in body:
            key = _str_or_none(body["cta_key"])
            if key and not conn.execute("SELECT 1 FROM campaign_ctas WHERE cta_key=?",
                                        (key,)).fetchone():
                raise ValueError(f"unknown cta_key {key!r}")
            fields["cta_key"] = key
        if "copy_mode" in body:
            cm = str(body["copy_mode"])
            if cm not in ("generated", "manual"):
                raise ValueError("copy_mode must be generated or manual")
            fields["copy_mode"] = cm
        for k in ("angle", "subject", "body"):
            if k in body:
                fields[k] = body[k]
        if "day_offset" in body:
            fields["day_offset"] = _int_or_none(body["day_offset"])
        db.upsert_step(conn, campaign_id, step_no, channel, **fields)
        return {"steps": C.step_plan(conn, campaign_id)}
    finally:
        conn.close()


def delete_step(campaign_id, body):
    step_no = _int_or_none(body.get("step_no"))
    channel = str(body.get("channel") or "email")
    if step_no is None:
        raise ValueError("step_no is required")
    conn = _write_conn()
    try:
        db.delete_step(conn, campaign_id, step_no, channel)
        return {"steps": C.step_plan(conn, campaign_id)}
    finally:
        conn.close()


def discover(campaign_id, body):
    """Find accounts: scan in-scope, un-scanned accounts for signal.

    dry_run lists the candidates and scans nothing — the honest preview, because the
    real run spends a Prospeo credit per domain on the hiring detector. The live run
    is a background job; the caller polls `discover_status`."""
    conn = _write_conn()
    try:
        camp = db.get_campaign(conn, campaign_id)
        if not camp:
            raise LookupError("campaign not found")
        kinds = body.get("kinds") or None
        limit = _int_or_none(body.get("limit"))
        limit = 25 if limit is None else max(1, min(limit, 500))
        if body.get("dry_run"):
            cands = C.discovery_scope(conn, camp, limit=limit, kinds=kinds)
            return {"dry_run": True, "candidates": cands, "count": len(cands),
                    "costs_credits": "hiring" in (kinds or
                                                  C.validate_signal_query(
                                                      camp.get("signal_query"),
                                                      conn)["kinds"])}
    finally:
        conn.close()

    with _DISCOVERY_LOCK:
        if any(j["status"] == "running" and j["campaign_id"] == campaign_id
               for j in DISCOVERY_JOBS.values()):
            raise Discovering("discovery is already running for this campaign")
        _DISCOVERY_SEQ[0] += 1
        job_id = f"disc-{_DISCOVERY_SEQ[0]}"
        job = {"job_id": job_id, "campaign_id": campaign_id, "status": "running",
               "done": 0, "total": 0, "current": None, "scanned": 0,
               "detected": {}, "errors": [], "unavailable": {}, "qualified": None,
               "results": [], "found_accounts": 0,
               "error": None, "started_at": db.now(), "finished_at": None}
        DISCOVERY_JOBS[job_id] = job

    def _progress(done, total, domain):
        job["done"], job["total"], job["current"] = done, total, domain

    # Captured NOW, in the request thread. The worker has no thread-local, so a
    # demo job that read it there would silently write to live data.
    demo_dir, demo_db = _demo_paths()

    def _run():
        # Its own connection: this runs off-thread and sqlite connections are not
        # shareable across threads.
        c2 = db.connect(demo_db)
        try:
            if demo_dir:
                res = demo_actions.simulate_discovery(
                    demo_dir, demo_db, campaign_id, limit=limit, progress=_progress)
                job.update({k: res[k] for k in
                            ("scanned", "detected", "errors", "unavailable",
                             "results", "found_accounts") if k in res})
                job["qualified"] = res.get("qualified")
                job["status"] = "done"
                return
            camp2 = db.get_campaign(c2, campaign_id)
            res = C.discover(c2, camp2, limit=limit, kinds=kinds, progress=_progress)
            job.update({k: res[k] for k in
                        ("scanned", "detected", "errors", "unavailable",
                         "results", "found_accounts") if k in res})
            job["qualified"] = res.get("qualified")
            job["status"] = "done"
        except Exception as e:  # noqa: BLE001
            job["status"] = "error"
            job["error"] = f"{type(e).__name__}: {e}"[:300]
        finally:
            job["finished_at"] = db.now()
            c2.close()

    threading.Thread(target=_run, daemon=True).start()
    return {"job_id": job_id, "status": "running", "limit": limit}


class Discovering(Exception):
    """Raised when a discovery run is already in flight — surfaced as a 409."""


def discover_status(job_id):
    job = DISCOVERY_JOBS.get(job_id)
    if not job:
        raise LookupError(f"no discovery job {job_id}")
    out = dict(job)
    out["errors"] = out["errors"][:20]   # the UI shows a sample, not every failure
    return out


def enrich(campaign_id, body):
    """Find the REST of the buyer group at this campaign's accounts, via Clay.

    Always cost-first: dry_run returns the account list and a credit floor and spends
    nothing. The live run is a background job because Clay is async per company and a
    25-account run takes minutes."""
    conn = _write_conn()
    try:
        camp = db.get_campaign(conn, campaign_id)
        if not camp:
            raise LookupError("campaign not found")
        limit = _int_or_none(body.get("limit"))
        limit = 25 if limit is None else max(1, min(limit, 200))
        if body.get("dry_run"):
            return {"dry_run": True, **C.enrich_estimate(conn, camp, limit=limit)}
    finally:
        conn.close()

    with _ENRICH_LOCK:
        if any(j["status"] == "running" and j["campaign_id"] == campaign_id
               for j in ENRICH_JOBS.values()):
            raise Discovering("enrichment is already running for this campaign")
        _ENRICH_SEQ[0] += 1
        job_id = f"enr-{_ENRICH_SEQ[0]}"
        job = {"job_id": job_id, "campaign_id": campaign_id, "status": "running",
               "done": 0, "total": 0, "current": None, "accounts": 0, "found": 0,
               "created": 0, "added_to_campaign": 0, "credits": 0.0, "errors": [],
               "unavailable": None, "error": None,
               "started_at": db.now(), "finished_at": None}
        ENRICH_JOBS[job_id] = job

    per_company = _int_or_none(body.get("per_company_cap")) or 3
    add = bool(body.get("add_to_campaign"))

    def _progress(done, total, domain):
        job["done"], job["total"], job["current"] = done, total, domain

    demo_dir, demo_db = _demo_paths()

    def _run():
        c2 = db.connect(demo_db)
        try:
            if demo_dir:
                res = demo_actions.simulate_enrich(
                    demo_dir, demo_db, campaign_id, limit=limit,
                    per_company_cap=per_company, add_to_campaign=add,
                    progress=_progress)
                job.update({k: res[k] for k in
                            ("accounts", "found", "created", "added_to_campaign",
                             "credits", "errors", "unavailable") if k in res})
                job["note"] = res.get("note")
                job["status"] = "done"
                return
            camp2 = db.get_campaign(c2, campaign_id)
            res = C.enrich(c2, camp2, limit=limit, per_company_cap=per_company,
                           progress=_progress, add_to_campaign=add)
            job.update({k: res[k] for k in
                        ("accounts", "found", "created", "added_to_campaign",
                         "credits", "errors", "unavailable") if k in res})
            job["note"] = res.get("note")
            job["status"] = "done"
        except Exception as e:  # noqa: BLE001
            job["status"] = "error"
            job["error"] = f"{type(e).__name__}: {e}"[:300]
        finally:
            job["finished_at"] = db.now()
            c2.close()

    threading.Thread(target=_run, daemon=True).start()
    return {"job_id": job_id, "status": "running", "limit": limit}


def enrich_status(job_id):
    job = ENRICH_JOBS.get(job_id)
    if not job:
        raise LookupError(f"no enrichment job {job_id}")
    out = dict(job)
    out["errors"] = out["errors"][:20]
    return out


# ---- audiences, capacity, CRM field map ------------------------------------
def audiences_payload():
    """The audience + signal vocabulary a campaign can be built from.

    Signal kinds come straight from campaigns.SIGNAL_REGISTRY, so adding a kind
    there makes it appear in the builder with no UI change."""
    conn = _write_conn()
    try:
        reg = C.signal_registry(conn)
    finally:
        conn.close()
    return {
        "types": list(A.AUDIENCE_TYPES),
        # The LIVE registry, so a signal this deployment defined for itself is
        # selectable in the campaign builder the moment it is saved.
        "signal_kinds": [{"id": k, **v} for k, v in reg.items() if v.get("active", True)],
        "presets": [{"id": k, "label": v["label"], "description": v["description"],
                     "default_days": v["default_days"]}
                    for k, v in A.CRM_PRESETS.items()],
    }


def audience_preview(body):
    """Resolve an audience without saving it — how many contacts and accounts it
    actually reaches, including how many the CRM knows about that we have not pulled.

    Goes through `_write_conn()` for the connection, not a bare `db.connect()`: the
    latter always opened the LIVE pipeline.db, so "Preview reach" inside a demo
    reported the real contact pool."""
    conn = _write_conn()
    try:
        aud = A.validate_audience(body.get("audience"))
        res = A.resolve(conn, aud, limit=None, crm=_audience_crm())
        res["domains"] = res["domains"][:50]
        res["contact_ids"] = res["contact_ids"][:50]
        return res
    finally:
        conn.close()


def import_preview(body, project_root):
    """What dropping this file would do — writes nothing.

    Held as its own call so the column mapping can be corrected before anything is
    created. An email column mapped to the wrong header imports a list of nobody,
    and there is no undo for contacts created in someone's CRM."""
    conn = _write_conn()
    try:
        return contact_import.preview(
            conn, body.get("filename"), body.get("content_b64"),
            body.get("mapping"), project_root)
    finally:
        conn.close()


def import_commit(body, project_root, scripts_dir):
    """Import for real: create in the CRM + pipeline, record the import, and hand
    back an `upload` audience pointing at it.

    In a demo the CRM leg is skipped — a demo may write to its own dataset and
    nothing else — so contacts land in the profile's pipeline only. Same response
    shape either way, so the UI cannot tell, and the demo can show an event list
    being turned into a campaign without touching a real portal."""
    demo_dir, _demo_db = _demo_paths()
    conn = _write_conn()
    try:
        if demo_dir:
            return demo_actions.simulate_file_import(
                conn, body.get("filename"), body.get("content_b64"),
                body.get("mapping"), body.get("label"), project_root)
        return contact_import.commit(
            conn, body.get("filename"), body.get("content_b64"),
            body.get("mapping"), label=body.get("label"),
            project_root=project_root, scripts_dir=scripts_dir)
    finally:
        conn.close()


# ---- signal definitions ------------------------------------------------------
def signal_defs_payload(conn):
    """Every signal kind this deployment recognises, plus the rule vocabulary.

    What counts as a signal is configuration, so this is a read of the table rather
    than a dump of a constant. `usage` says how many events each kind has actually
    produced — a definition nobody's data ever matches is the thing worth seeing."""
    try:
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                            "AND name='signal_defs'").fetchone():
            return {"signals": [], "available": False}
        rows = db.signal_defs(conn)
        counts = {}
        try:
            counts = {r["kind"]: r["n"] for r in conn.execute(
                "SELECT kind, COUNT(*) n FROM signal_events GROUP BY kind")}
        except Exception:  # noqa: BLE001
            pass
        for r in rows:
            r["events"] = counts.get(r["kind"], 0)
            if r.get("rule"):
                r["rule_text"] = crm_signals.describe_rule(r["rule"])
            try:
                r["last_run_detail"] = json.loads(r.get("last_run_detail") or "null")
            except (ValueError, TypeError):
                r["last_run_detail"] = None
        return {"signals": rows, "available": True,
                "vocabulary": crm_signals.vocabulary(),
                # Rules that need the CRM can be BUILT with no token — they simply
                # cannot be previewed or run, and saying which is which up front
                # beats a failure at preview time.
                "crm_available": bool((os.environ.get("HUBSPOT_ACCESS_TOKEN") or "").strip())
                                 or bool(_demo_paths()[0])}
    except Exception as e:  # noqa: BLE001
        return {"signals": [], "available": False, "error": str(e)}


def save_signal_def(body):
    """Create or retune one signal kind. Validates the rule before storing it."""
    kind = crm_signals.validate_kind(body.get("kind"))
    conn = _write_conn()
    try:
        existing = db.get_signal_def(conn, kind)
        fields = {}
        for k in ("label", "description"):
            if k in body:
                fields[k] = _str_or_none(body[k])
        if "strength" in body:
            try:
                fields["strength"] = max(0.0, min(50.0, float(body["strength"])))
            except (TypeError, ValueError):
                raise ValueError("strength must be a number between 0 and 50")
        if "decay_scale" in body:
            try:
                fields["decay_scale"] = max(0.1, min(10.0, float(body["decay_scale"])))
            except (TypeError, ValueError):
                raise ValueError("decay must be a number between 0.1 and 10")
        if "active" in body:
            fields["active"] = 1 if body["active"] else 0
        if "rule" in body:
            rule = body["rule"]
            if rule in (None, {}, ""):
                # Only a user-defined kind can drop its rule; a builtin never had
                # one, and clearing it on a rule-backed custom kind would leave a
                # signal that can never fire again.
                if existing and not existing.get("builtin") and existing.get("rule"):
                    raise ValueError("a custom signal needs a rule — delete it instead")
                fields["rule"] = None
            else:
                fields["rule"] = crm_signals.validate_rule(rule)
                fields.setdefault("detector", "rule")
        if not existing:
            if not fields.get("rule"):
                raise ValueError("a new signal needs a rule that defines when it fires")
            fields.setdefault("label", kind.replace("_", " ").title())
        return {"signal": db.upsert_signal_def(conn, kind, **fields)}
    finally:
        conn.close()


def delete_signal_def(kind):
    conn = _write_conn()
    try:
        if not db.delete_signal_def(conn, kind):
            raise ValueError("builtin signals can't be deleted — deactivate it instead")
        return {"ok": True, "deleted": kind}
    finally:
        conn.close()


def preview_signal_rule(body):
    """What a rule would catch right now. Writes nothing."""
    rule = crm_signals.validate_rule(body.get("rule"))
    limit = _int_or_none(body.get("limit")) or 300
    conn = _write_conn()
    try:
        return crm_signals.preview(conn, str(body.get("kind") or "preview"), rule,
                                   limit=max(1, min(limit, 2000)),
                                   label=_str_or_none(body.get("label")),
                                   crm=_audience_crm())
    finally:
        conn.close()


def run_signal_rule(kind, body):
    """Evaluate one definition for real and record the matches as signal_events."""
    conn = _write_conn()
    try:
        d = db.get_signal_def(conn, kind)
        if not d:
            raise LookupError(f"no signal definition {kind}")
        limit = _int_or_none(body.get("limit")) or crm_signals.DEFAULT_LIMIT
        return crm_signals.evaluate(conn, d, limit=max(1, min(limit, 5000)),
                                    commit=not body.get("dry_run"),
                                    crm=_audience_crm())
    finally:
        conn.close()


# ---- customer proof ----------------------------------------------------------
def references_payload(conn):
    """The proof library + which CTA cites what.

    Separate from the CTA library because one story backs several offers, and
    because `nameable` is a fact about the CUSTOMER — whether we may say their name
    out loud — not about the offer that happens to reference them."""
    try:
        refs = db.customer_references(conn, active_only=False)
        ctas = db.list_ctas(conn, active_only=False)
        return {"references": refs, "available": True,
                "ctas": [{"cta_key": c["cta_key"], "label": c["label"],
                          "tier": c.get("tier"),
                          "content": c.get("content") or []} for c in ctas]}
    except Exception as e:  # noqa: BLE001
        return {"references": [], "ctas": [], "available": False, "error": str(e)}


def save_reference(body):
    ref_key = str(body.get("ref_key") or "").strip().lower().replace(" ", "-")
    if not re.match(r"^[a-z0-9][a-z0-9-]{1,48}$", ref_key or ""):
        raise ValueError("id must be lowercase letters, numbers and hyphens")
    customer = _str_or_none(body.get("customer"))
    story = _str_or_none(body.get("story"))
    if not customer or not story:
        raise ValueError("a reference needs a customer and a story")
    nameable = 1 if body.get("nameable") else 0
    anonymous = _str_or_none(body.get("anonymous"))
    if not nameable and not anonymous and not _str_or_none(body.get("industry")):
        raise ValueError("if we may not name them, say how to describe them instead")
    # The URL is rendered as a real anchor in the UI, so the scheme is checked here.
    # javascript:/data: would be a script-injection vector dressed up as a case-study
    # link, and rejecting it at the boundary is cheaper than trusting every renderer.
    url = _str_or_none(body.get("url"))
    if url and not re.match(r"^https?://", url, re.I):
        raise ValueError("the link must start with http:// or https://")
    kind = str(body.get("kind") or "proof")
    if kind not in ("proof", "asset", "doc"):
        raise ValueError("kind must be proof, asset or doc")
    conn = _write_conn()
    try:
        return {"reference": db.upsert_customer_reference(
            conn, ref_key, customer=customer, story=story, nameable=nameable,
            anonymous=anonymous, industry=_str_or_none(body.get("industry")),
            metric=_str_or_none(body.get("metric")),
            quote=_str_or_none(body.get("quote")),
            source=_str_or_none(body.get("source")),
            url=url, kind=kind,
            active=1 if body.get("active", True) else 0)}
    finally:
        conn.close()


def set_cta_reference(body):
    """Add or remove one piece of content from an offer. Explicitly two-way — the
    UI has an Add and a Remove and this is what both call."""
    cta_key = _str_or_none(body.get("cta_key"))
    ref_key = _str_or_none(body.get("reference_key"))
    if not cta_key or not ref_key:
        raise ValueError("cta_key and reference_key are required")
    detach = bool(body.get("detach"))
    conn = _write_conn()
    try:
        if not detach and not any(
                r["ref_key"] == ref_key
                for r in db.customer_references(conn, active_only=False)):
            raise ValueError(f"no such content {ref_key}")
        cta = (db.detach_cta_content(conn, cta_key, ref_key) if detach
               else db.attach_cta_content(conn, cta_key, ref_key))
        if not cta:
            raise LookupError(f"no such offer {cta_key}")
        return {"cta": cta}
    finally:
        conn.close()


def reviews_payload(conn):
    """Evergreen campaigns waiting on a human before their next cycle opens."""
    try:
        if not _tables_present(conn):
            return {"reviews": [], "available": False}
        return {"reviews": C.pending_reviews(conn), "available": True}
    except Exception as e:  # noqa: BLE001
        return {"reviews": [], "available": False, "error": str(e)}


def relaunch(campaign_id, body):
    """Approve a review and open the next cycle. The only route past a review —
    there is deliberately no 'relaunch without looking'."""
    conn = _write_conn()
    try:
        camp = db.get_campaign(conn, campaign_id)
        if not camp:
            raise LookupError("campaign not found")
        if not camp.get("evergreen"):
            raise ValueError("this campaign is not evergreen")
        brief = body.get("brief")
        return C.relaunch(conn, camp,
                          brief=brief if brief is not None else None,
                          window_days=_int_or_none(body.get("window_days")),
                          note=_str_or_none(body.get("note")))
    finally:
        conn.close()


def imports_payload():
    conn = _write_conn()
    try:
        return {"imports": contact_import.list_imports(conn)}
    finally:
        conn.close()


def brief(body, project_root):
    """Configure a campaign from a description or a dropped spec. Writes nothing.

    Returns a PATCH for the builder form plus any clarifying questions; the user
    still presses Create. Held here rather than called straight from app.py so the
    proposer gets a connection and can offer the real CTA library as vocabulary."""
    conn = _write_conn()
    try:
        return campaign_brief.propose(project_root, body, conn)
    finally:
        conn.close()


def capacity_payload(conn, days=30):
    """Sending capacity + enrichment spend. Report-only.

    Two different clocks on purpose: LinkedIn is a DAILY allowance (and the one that
    actually bites), email a monthly one."""
    try:
        return {"available": True, "capacity": CAP.status(conn),
                "spend": CAP.spend(conn, days=days)}
    except Exception as e:  # noqa: BLE001
        return {"available": False, "error": f"{type(e).__name__}: {e}"}


def hot_list_payload(conn, path, size=None, refresh=False):
    """The daily hot-target report.

    Served from the persisted snapshot so it is STABLE for a working day — a target
    list that reshuffles between page loads is not something anyone can plan around.
    `path` comes from the caller so demo mode can serve a profile's own snapshot.
    A missing snapshot is computed live rather than rendering empty."""
    try:
        if not _tables_present(conn):
            return {"accounts": [], "available": False}
        p = Path(path)
        if not refresh and p.is_file():
            try:
                snap = json.loads(p.read_text())
                snap["available"] = True
                snap["stale"] = C.hot_list_stale(p)
                return snap
            except (json.JSONDecodeError, OSError):
                pass
        snap = C.hot_target_list(conn, size=size or C.HOT_LIST_SIZE)
        snap.update({"available": True, "stale": False, "computed_live": True})
        return snap
    except Exception as e:  # noqa: BLE001
        return {"accounts": [], "available": False, "error": f"{type(e).__name__}: {e}"}


def refresh_hot_list():
    """Rebuild the daily snapshot.

    C.refresh_hot_list writes to its module-level LIVE path, so under a demo the
    snapshot is written to the profile's own file here instead — otherwise a demo
    action would overwrite production's hot list."""
    conn = _write_conn()
    try:
        snap = C.hot_target_list(conn)
        demo_db = demo_db_path()
        if demo_db:
            out = Path(demo_db).parent / "hot-list.json"
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(json.dumps(snap, ensure_ascii=False))
            snap["demo"] = True
            return snap
        return C.refresh_hot_list(conn)
    finally:
        conn.close()


def campaign_analytics_payload(conn, days=30):
    """Console-campaign performance, for Analytics and Trends.

    The existing Analytics view reports on BISON campaigns — the sending containers.
    This reports on console campaigns, which is a different question: not "how did
    campaign 14 perform" but "did the way we defined and prioritised this audience
    actually work". The join between them is 1:1 (`bison_campaign_id`), so both can
    sit on one page without double-counting.
    """
    try:
        if not _tables_present(conn):
            return {"available": False, "campaigns": []}
        rows = db.list_campaigns(conn)
        out = []
        for c in rows:
            cid = c["campaign_id"]
            counts = db.campaign_counts(conn, cid)
            by_state = counts["by_state"]
            enrolled = by_state.get("enrolled", 0) + by_state.get("replied", 0)
            replied = by_state.get("replied", 0)
            spend = CAP.spend(conn, days=3650, campaign_id=cid)
            out.append({
                "campaign_id": cid, "name": c["name"], "key": c["key"],
                "status": c["status"],
                "audience": (c.get("audience") or {}).get("type") or "all_contacts",
                "membership_mode": c.get("membership_mode"),
                "bison_campaign_id": c.get("bison_campaign_id"),
                "members": counts["members"], "accounts": counts["accounts"],
                "enrolled": enrolled, "replied": replied,
                # Reply rate is per ENROLLED, not per member: a qualified contact we
                # never sent to cannot have replied, and folding them in would make
                # a campaign look worse the better it is at finding people.
                "reply_rate_pct": round(100 * replied / enrolled, 2) if enrolled else None,
                "by_band": counts.get("by_band") or {},
                "avg_score": counts.get("avg_score"),
                "credits": spend.get("credits") or 0,
            })
        # Does priority actually predict replies? The single most important question
        # about a scoring model, and the only honest way to answer it is to compare
        # reply rate ACROSS bands rather than assert the score works.
        band_perf = {}
        for r in conn.execute("""
            SELECT score_band, COUNT(*) n,
                   SUM(CASE WHEN state='replied' THEN 1 ELSE 0 END) replied,
                   SUM(CASE WHEN state IN ('enrolled','replied') THEN 1 ELSE 0 END) enrolled
            FROM campaign_members WHERE score_band IS NOT NULL GROUP BY score_band
        """):
            en = r["enrolled"] or 0
            band_perf[r["score_band"]] = {
                "members": r["n"], "enrolled": en, "replied": r["replied"] or 0,
                "reply_rate_pct": round(100 * (r["replied"] or 0) / en, 2) if en else None,
            }
        # Same question for the channel recommendation and for momentum.
        chan, mom = {}, {"warming": 0, "cooling": 0, "flat": 0}
        for m in db.campaign_members(conn, order="score"):
            for k, v in ((m.get("channels") or {}).get("channels") or {}).items():
                if v:
                    slot = chan.setdefault(k, {"members": 0, "replied": 0})
                    slot["members"] += 1
                    if m["state"] == "replied":
                        slot["replied"] += 1
            mv = m.get("momentum")
            mom["warming" if (mv or 0) > 0 else "cooling" if (mv or 0) < 0 else "flat"] += 1
        return {"available": True, "campaigns": out, "by_band": band_perf,
                "by_channel": chan, "momentum": mom,
                "overlap": C.overlap_summary(conn)}
    except Exception as e:  # noqa: BLE001
        return {"available": False, "campaigns": [], "error": f"{type(e).__name__}: {e}"}


def funnel_payload(conn, analytics=None):
    """The end-to-end funnel, following the system's actual causal chain.

        audience -> signal fires -> qualified -> scored -> enrolled -> contacted
        -> replied -> interested

    Analytics used to treat the BISON campaign as the unit of analysis, which could
    only ever see the last three stages — everything upstream (who we chose, why they
    ranked, which channel) was invisible, so a bad number had no diagnosable cause.
    This walks the whole chain, joining console campaigns to Bison stats 1:1 through
    `bison_campaign_id`, so a drop-off can be attributed to the step that caused it.

    Stages before `contacted` come from our own tables and are exact. `contacted`
    onward come from Bison's snapshot, so they are only as fresh as the last stats
    refresh — reported as `source` per stage rather than blended silently.
    """
    try:
        if not _tables_present(conn):
            return {"available": False, "stages": []}
        bison = {}
        for c in ((analytics or {}).get("campaigns") or []):
            bison[str(c.get("campaign_id"))] = c

        rows, totals = [], {"qualified": 0, "enrolled": 0, "contacted": 0,
                            "replied": 0, "interested": 0, "accounts": 0}
        for camp in db.list_campaigns(conn):
            cid = camp["campaign_id"]
            counts = db.campaign_counts(conn, cid)
            st = counts["by_state"]
            qualified = counts["members"]
            enrolled = st.get("enrolled", 0) + st.get("replied", 0)
            b = bison.get(str(camp.get("bison_campaign_id") or "")) or {}
            contacted = b.get("total_leads_contacted") or 0
            replied = b.get("unique_replies") or 0
            interested = b.get("interested") or 0
            # Our own reply state is the fallback when Bison stats aren't joined —
            # better a smaller true number than a zero that reads as "nothing works".
            if not replied:
                replied = st.get("replied", 0)
            rows.append({
                "campaign_id": cid, "name": camp["name"], "status": camp["status"],
                "bison_campaign_id": camp.get("bison_campaign_id"),
                "joined": bool(b),
                "accounts": counts["accounts"], "qualified": qualified,
                "enrolled": enrolled, "contacted": contacted,
                "replied": replied, "interested": interested,
                "by_band": counts.get("by_band") or {},
                "avg_score": counts.get("avg_score"),
            })
            totals["accounts"] += counts["accounts"]
            for k in ("qualified", "enrolled", "contacted", "replied", "interested"):
                totals[k] += rows[-1][k]

        def rate(n, d):
            return round(100 * n / d, 1) if d else None

        # A Bison campaign holds every lead ever put into it, including ones enrolled
        # by the pre-campaign path. So its `contacted` covers a WIDER population than
        # the console campaign's `enrolled`, and dividing one by the other produced a
        # 14,602% conversion rate. There is no per-lead attribution in the Bison
        # snapshot to net that out, so the honest move is to detect the mismatch and
        # decline to state a rate rather than print a fake one.
        mixed = totals["contacted"] > totals["enrolled"]
        stages = [
            {"id": "qualified", "label": "Qualified", "n": totals["qualified"],
             "source": "console", "note": "matched the audience and signal query"},
            {"id": "enrolled", "label": "Enrolled", "n": totals["enrolled"],
             "source": "console", "of_prev": rate(totals["enrolled"], totals["qualified"]),
             "note": "sequenced into Bison / HeyReach"},
            {"id": "contacted", "label": "Contacted", "n": totals["contacted"],
             "source": "bison", "mixed": mixed,
             "of_prev": None if mixed else rate(totals["contacted"], totals["enrolled"]),
             "note": ("includes leads enrolled outside these campaigns — not comparable "
                      "to Enrolled above" if mixed else "at least one touch actually sent")},
            {"id": "replied", "label": "Replied", "n": totals["replied"],
             "source": "bison", "of_prev": rate(totals["replied"], totals["contacted"])},
            {"id": "interested", "label": "Interested", "n": totals["interested"],
             "source": "bison", "of_prev": rate(totals["interested"], totals["replied"])},
        ]
        # The biggest proportional drop is the one worth naming — it is the answer to
        # "where is this losing people", which is the whole reason to draw a funnel.
        # Stages with no comparable predecessor are excluded, not treated as 0%.
        worst = None
        for s in stages[1:]:
            if s.get("of_prev") is not None and (worst is None or s["of_prev"] < worst["of_prev"]):
                worst = s
        return {"available": True, "stages": stages, "campaigns": rows,
                "totals": totals, "biggest_drop": worst, "mixed_population": mixed,
                "unjoined": [r["name"] for r in rows if not r["joined"]]}
    except Exception as e:  # noqa: BLE001
        return {"available": False, "stages": [], "error": f"{type(e).__name__}: {e}"}


def crm_fields_payload(conn):
    """The field map + what each local key means, for the Setup section."""
    try:
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                            "AND name='crm_field_map'").fetchone():
            return {"fields": [], "available": False}
        return {
            "available": True,
            "fields": db.crm_fields(conn),
            "local_fields": [{"key": k, "object_type": v[0], "means": v[1]}
                             for k, v in crm_sync.LOCAL_FIELDS.items()],
            "enabled": crm_sync.writeback_enabled(),
            "directions": ["push", "pull", "both", "off"],
        }
    except Exception as e:  # noqa: BLE001
        return {"fields": [], "available": False, "error": str(e)}


def update_crm_field(body):
    key = _str_or_none(body.get("local_key"))
    if not key:
        raise ValueError("local_key is required")
    fields = {}
    if "property_name" in body:
        prop = _str_or_none(body["property_name"])
        if not prop:
            raise ValueError("property_name cannot be blank")
        fields["property_name"] = prop
    if "direction" in body:
        d = str(body["direction"])
        if d not in ("push", "pull", "both", "off"):
            raise ValueError("direction must be push, pull, both or off")
        fields["direction"] = d
    for k in ("label", "field_type"):
        if k in body:
            fields[k] = _str_or_none(body[k])
    for k in ("enabled", "auto_create"):
        if k in body:
            fields[k] = 1 if body[k] else 0
    conn = _write_conn()
    try:
        row = db.update_crm_field(conn, key, **fields)
        if not row:
            raise LookupError(f"no mapped field {key!r}")
        return {"field": row}
    finally:
        conn.close()


def crm_sync_run(body):
    """Push computed values to the CRM, pull CRM values back, or provision the
    properties. Pull is the half that makes the CRM authoritative."""
    action = str(body.get("action") or "push")
    dry = bool(body.get("dry_run"))
    conn = _write_conn()
    try:
        if action == "ensure":
            return crm_sync.ensure_properties(conn, dry_run=dry)
        if action == "pull":
            return crm_sync.pull(conn, limit=_int_or_none(body.get("limit")) or 500,
                                 dry_run=dry)
        if action == "push":
            return crm_sync.push(conn, campaign_id=_int_or_none(body.get("campaign_id")),
                                 dry_run=dry, limit=_int_or_none(body.get("limit")))
        raise ValueError("action must be push, pull or ensure")
    finally:
        conn.close()


def rescore(campaign_id, body):
    """Recompute member priorities against the signals visible now."""
    conn = _write_conn()
    try:
        camp = db.get_campaign(conn, campaign_id)
        if not camp:
            raise LookupError("campaign not found")
        res = C.rescore(conn, camp, commit=not body.get("dry_run"))
        res["counts"] = db.campaign_counts(conn, campaign_id)
        return res
    finally:
        conn.close()


def call_list_payload(conn, campaign_id=None, limit=100, state="qualified"):
    """The SDR call list: contacts to work, strongest signal first.

    Cross-campaign when campaign_id is None — a rep works one list, not one list per
    campaign.

    `total` is the count BEFORE the limit. The console filters and sorts the rows it
    holds, so it has to be able to say whether it is holding all of them — a sort
    over a truncated page silently answers "the weakest on the list" with "the
    weakest of the strongest 300"."""
    try:
        if not _tables_present(conn):
            return {"contacts": [], "available": False}
        rows = C.attach_money(db.campaign_members(conn, campaign_id, state=state or None,
                                                  limit=limit, order="priority"))
        return {"contacts": rows, "available": True, "count": len(rows),
                "total": _member_total(conn, campaign_id, state)}
    except Exception as e:  # noqa: BLE001
        return {"contacts": [], "available": False, "error": str(e)}


def _member_total(conn, campaign_id=None, state=None):
    where, params = [], []
    if campaign_id is not None:
        where.append("campaign_id=?")
        params.append(campaign_id)
    if state:
        where.append("state=?")
        params.append(state)
    sql = "SELECT COUNT(*) FROM campaign_members"
    if where:
        sql += " WHERE " + " AND ".join(where)
    return conn.execute(sql, params).fetchone()[0]


# ---- working the list ---------------------------------------------------------
# Two levels, deliberately separate, because they are different decisions:
#
#   CAMPAIGN level   "not a fit for THIS campaign", "call them next week", "put them
#                    top of my list" — scoped to one membership row.
#   PERSON level     "pause all outreach", "do not contact" — applies everywhere and
#                    is ENFORCED at qualification and at the enroll gate.
#
# Collapsing them would mean a rep dismissing someone from one campaign quietly
# burned them for every future one.
MEMBER_OUTCOMES = ("worked", "no_answer", "not_a_fit", "later")


def update_member(body):
    """Campaign-level action on one contact. Never touches the person's global state."""
    cid = _int_or_none(body.get("campaign_id"))
    contact_id = _str_or_none(body.get("contact_id"))
    if cid is None or not contact_id:
        raise ValueError("campaign_id and contact_id are required")
    fields = {}
    action = str(body.get("action") or "").strip()

    if action == "worked":
        outcome = str(body.get("outcome") or "worked")
        if outcome not in MEMBER_OUTCOMES:
            raise ValueError(f"outcome must be one of {list(MEMBER_OUTCOMES)}")
        fields["outcome"] = outcome
        fields["worked_at"] = db.now()
        # "Not a fit" is the one outcome that changes membership: they stop being on
        # the list. Everything else is a record of a touch, not a removal.
        if outcome == "not_a_fit":
            fields["state"] = "removed"
    elif action == "snooze":
        days = _int_or_none(body.get("days")) or 7
        days = max(1, min(days, 365))
        fields["snoozed_until"] = (datetime.now(timezone.utc)
                                   + timedelta(days=days)).strftime("%Y-%m-%d")
    elif action == "unsnooze":
        fields["snoozed_until"] = None
    elif action == "priority":
        v = body.get("manual_priority")
        if v in (None, ""):
            fields["manual_priority"] = None      # back to the computed score
        else:
            try:
                fields["manual_priority"] = max(0.0, min(100.0, float(v)))
            except (TypeError, ValueError):
                raise ValueError("manual_priority must be a number 0-100")
    elif action == "restore":
        fields["state"] = "qualified"
        fields["outcome"] = None
    elif action == "note":
        fields["note"] = _str_or_none(body.get("note"))
    else:
        raise ValueError("action must be worked, snooze, unsnooze, priority, "
                         "restore or note")
    if "note" in body and action != "note":
        fields["note"] = _str_or_none(body.get("note"))

    conn = _write_conn()
    try:
        if not db.get_member(conn, cid, contact_id):
            raise LookupError("that contact is not in this campaign")
        row = db.update_member(conn, cid, contact_id, **fields)
        return {"member": row, "counts": db.campaign_counts(conn, cid)}
    finally:
        conn.close()


def update_engagement(body):
    """PERSON-level action: applies to every campaign, now and future.

    Enforced, not cosmetic — `batch_db.suppressed_contact_ids` folds this into the
    same set the enroll gate and qualification already consult."""
    contact_id = _str_or_none(body.get("contact_id"))
    if not contact_id:
        raise ValueError("contact_id is required")
    state = str(body.get("engagement_state") or "active").strip().lower()
    until = None
    if state == "paused":
        days = _int_or_none(body.get("days")) or 30
        days = max(1, min(days, 3650))
        until = (datetime.now(timezone.utc) + timedelta(days=days)).strftime("%Y-%m-%d")
    conn = _write_conn()
    try:
        row = db.set_engagement(conn, contact_id, state, paused_until=until,
                                note=_str_or_none(body.get("note")))
        if not row:
            raise LookupError("no such contact")
        # What this actually stops, stated back: a switch whose blast radius is
        # invisible is one nobody trusts.
        affected = [dict(r) for r in conn.execute(
            "SELECT m.campaign_id, m.state, c.name FROM campaign_members m "
            "JOIN campaigns c USING (campaign_id) WHERE m.contact_id=?",
            (contact_id,))]
        return {"engagement": row, "campaigns": affected}
    finally:
        conn.close()


def qualify(campaign_id, body):
    """Run the campaign's definition. dry_run previews without writing members."""
    dry = bool(body.get("dry_run"))
    conn = _write_conn()
    try:
        camp = db.get_campaign(conn, campaign_id)
        if not camp:
            raise LookupError("campaign not found")
        res = C.qualify(conn, camp, commit=not dry, audience_crm=_audience_crm())
        res["counts"] = db.campaign_counts(conn, campaign_id)
        return res
    finally:
        conn.close()


# ---- copy suggestion -------------------------------------------------------
SUGGEST_SYSTEM = """You write B2B cold outreach for EverWorker's SDR AI Worker.

You are writing ONE step of an existing sequence. You are given the campaign, the
whole sequence so it reads as a progression rather than four unrelated emails, and
the specific offer this step must carry. Ground everything in the knowledge base;
never invent product claims, numbers, or proof that is not in it.

Hard rules for the step you write:
- The CTA must be the assigned offer's give plus a meeting ask. Never a bare "got 15
  minutes?", never "I'll send it over, no call needed".
- 55-130 words. Short paragraphs. No sign-off, no em dashes, no hype adjectives.
- Ask exactly one question.
- This is a TEMPLATE for the whole campaign, not one contact: use the merge variables
  {{first_name}}, {{company}} where a name or company belongs. Do not invent a
  specific account's news — that personalization is added per contact at generation.

Return ONLY this JSON:
{"subject": "<subject line, lowercase, under 8 words>", "body": "<the email body>"}"""


def suggest_step_copy(campaign_id, body, project_root):
    """Draft manual copy for one step, inside the frame that step already declares.

    Deliberately NOT a free-form writer: the CTA comes from the step's cta_key, so a
    suggestion cannot drift off the offer the campaign assigned to that touch. The
    result is returned for review — nothing is saved until the UI posts it back."""
    step_no = _int_or_none(body.get("step_no"))
    channel = str(body.get("channel") or "email")
    if step_no is None:
        raise ValueError("step_no is required")
    if not (os.environ.get("ANTHROPIC_API_KEY") or "").strip():
        raise RuntimeError("ANTHROPIC_API_KEY is not set")

    conn = db.connect()
    try:
        camp = db.get_campaign(conn, campaign_id)
        if not camp:
            raise LookupError("campaign not found")
        plan = C.step_plan(conn, campaign_id)
    finally:
        conn.close()
    step = next((s for s in plan if s["step_no"] == step_no and s["channel"] == channel), None)
    if not step:
        raise LookupError(f"no {channel} step {step_no} on this campaign")

    knowledge = _knowledge(project_root)
    cta = step.get("cta") or {}
    user = (
        f"{knowledge}\n\n---\n\n"
        f"CAMPAIGN: {camp['name']}\n"
        + (f"WHAT DEFINES IT: {camp.get('description')}\n" if camp.get("description") else "")
        + f"\nFULL SEQUENCE (for progression — do not rewrite these):\n"
        + C.render_plan_prompt(plan)
        + f"\n\nWRITE {channel.upper()} STEP {step_no} ONLY.\n"
        + (f"This step's job: {step['angle']}\n" if step.get("angle") else "")
        + (f"This step's offer — {cta['label']}: anchor the meeting on {cta['give']}. "
           f"Meeting ask: \"{cta['ask']}\".\nLibrary example of this offer (do not copy "
           f"verbatim): {cta.get('example')}\n" if cta else
           "This step has no assigned offer; close on the strongest give the knowledge "
           "base supports for this position in the cadence.\n")
        + (f"\nExtra direction from the user: {body['instruction']}\n"
           if body.get("instruction") else "")
        + "\nReturn only the JSON object."
    )
    client_mod = _anthropic(project_root)
    try:
        client = client_mod.AnthropicClient()
    except Exception as e:  # noqa: BLE001 — surfaced as a 501 upstream
        raise RuntimeError(str(e))
    res = client.complete(SUGGEST_SYSTEM, user, max_tokens=2000, timeout=180)
    try:
        parsed = client_mod.extract_json(res["text"])
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(f"the model did not return usable JSON: {e}")
    return {"step_no": step_no, "channel": channel,
            "cta_key": step.get("cta_key"),
            "subject": (parsed.get("subject") or "").strip(),
            "body": (parsed.get("body") or "").strip()}


def _knowledge(project_root):
    """The same offer/ICP knowledge the pipeline generator reads, so a suggestion
    made here and copy generated later are grounded in one source."""
    kd = Path(project_root) / ".claude" / "skills" / "ai-sdr" / "knowledge"
    parts = []
    for fname in ("offer.md", "cta-offers.md", "icp-email.md"):
        f = kd / fname
        if f.is_file():
            parts.append(f.read_text())
    return "\n\n---\n\n".join(parts)


def _anthropic(project_root):
    """Lazy import — the server must boot with no API key and no client present."""
    scripts = Path(project_root) / ".claude" / "skills" / "ai-sdr" / "scripts"
    if str(scripts) not in sys.path:
        sys.path.insert(0, str(scripts))
    import anthropic_client
    return anthropic_client


# ---- coercion helpers ------------------------------------------------------
def _str_or_none(v):
    s = str(v).strip() if v is not None else ""
    return s or None


def _int_or_none(v):
    if v is None or v == "":
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _date(v):
    """Accept YYYY-MM-DD or a full ISO timestamp; store as given, reject garbage."""
    s = _str_or_none(v)
    if not s:
        return None
    try:
        datetime.strptime(s[:10], "%Y-%m-%d")
    except ValueError:
        raise ValueError(f"invalid date {s!r} (expected YYYY-MM-DD)")
    return s


# ---- buyer group -----------------------------------------------------------
def buyer_group_payload(conn):
    """The buyer-group ruleset + what it currently resolves to.

    ORDER IS THE LOGIC here (first match wins), so the payload keeps `sort_order`
    explicit rather than relying on array position — the UI has to be able to show
    and change priority, since that is what decides whether "Sales Operations
    Manager" is RevOps or generic Sales."""
    try:
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                            "AND name='buyer_group_roles'").fetchone():
            return {"available": False, "roles": []}
        import buyer_group_config as BG
        out = BG.summary(conn)
        out["available"] = True
        return out
    except Exception as e:  # noqa: BLE001
        return {"available": False, "roles": [], "error": f"{type(e).__name__}: {e}"}


def update_buyer_group(body):
    """Patch, add, delete or test a buyer-group rule."""
    import buyer_group_config as BG
    action = str(body.get("action") or "update")
    conn = _write_conn()
    try:
        if action == "test":
            titles = body.get("titles") or []
            return {"results": [{"title": t, **BG.classify(conn, t)} for t in titles]}
        key = _str_or_none(body.get("role_key"))
        if not key:
            raise ValueError("role_key is required")
        if action == "delete":
            if not db.delete_buyer_role(conn, key):
                raise LookupError(f"no role {key!r}")
            BG.invalidate()
            return {"deleted": True, "roles": db.buyer_group_roles(conn, active_only=False)}
        if action == "add":
            label = _str_or_none(body.get("label"))
            if not label:
                raise ValueError("label is required")
            db.add_buyer_role(conn, key, label,
                              seniority=body.get("seniority"),
                              match_patterns=body.get("match_patterns"),
                              clay_titles=body.get("clay_titles"),
                              persona=_str_or_none(body.get("persona")),
                              is_icp=body.get("is_icp", True),
                              worth_calling=body.get("worth_calling"),
                              sort_order=body.get("sort_order"))
            BG.invalidate()
            return {"roles": db.buyer_group_roles(conn, active_only=False)}
        fields = {}
        for k in ("label", "seniority", "persona"):
            if k in body:
                fields[k] = _str_or_none(body[k])
        for k in ("is_icp", "worth_calling", "active"):
            if k in body:
                fields[k] = 1 if body[k] else 0
        if "sort_order" in body:
            fields["sort_order"] = _int_or_none(body["sort_order"]) or 0
        for k in ("match_patterns", "clay_titles"):
            if k in body:
                vals = [str(v).strip() for v in (body[k] or []) if str(v).strip()]
                if k == "match_patterns":
                    # A bad regex here silently stops matching for that rule, so it
                    # is rejected at the door rather than discovered in a sweep.
                    import re as _re
                    for v in vals:
                        try:
                            _re.compile(v)
                        except _re.error as e:
                            raise ValueError(f"invalid pattern {v!r}: {e}")
                fields[k] = vals
        row = db.update_buyer_role(conn, key, **fields)
        if not row:
            raise LookupError(f"no role {key!r}")
        BG.invalidate()
        return {"roles": db.buyer_group_roles(conn, active_only=False)}
    finally:
        conn.close()
