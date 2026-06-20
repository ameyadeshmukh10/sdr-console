#!/usr/bin/env python3
"""Local MVP web UI backend for the SDR outbound pipeline.

Zero-dependency (Python stdlib only). Serves a small JSON API + the built React
frontend from a single process.

  python3 webui/server/app.py [--port 8787] [--static webui/frontend/dist]

Endpoints (all under /api, all JSON):
  GET  /api/status                 pipeline summary + persona rollup
  GET  /api/batches?status=&limit= batches with per-batch status counts
  GET  /api/rollup                 persona -> campaign rollup for the diagram
  GET  /api/analytics              cached campaign stats (instant)
  POST /api/analytics/refresh      re-run fetch_campaign_stats.py (live Bison)
  GET  /api/outreach?...           paginated/filtered outreach index + facets
  GET  /api/outreach/<contact_id>  full generated copy for one contact
  POST /api/ingest {list_id}       run hubspot_pull.py + sdr_batches.py init
  POST /api/reindex                rebuild the in-memory outreach index

Design notes:
  - SQLite is opened read-only (mode=ro) so the API can never block or corrupt
    the live pipeline DB; it still sees committed WAL writes.
  - The 2,000+ generated/*.json files are read once into a compact in-memory
    index at startup (joined with contacts.jsonl + DB status, with a derived
    CTA type). Detail requests lazy-read a single file.
  - State-changing actions (ingest, analytics refresh) shell out to the existing
    pipeline scripts via subprocess; we never reimplement their logic.
"""

import json
import re
import sqlite3
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

# webui/server/app.py -> webui/server -> webui -> project root
PROJECT_ROOT = Path(__file__).resolve().parents[2]
DATA = PROJECT_ROOT / "data"
DB_PATH = DATA / "outreach" / "pipeline.db"
GEN_DIR = DATA / "outreach" / "generated"
CONTACTS_JSONL = DATA / "outreach" / "contacts.jsonl"
CAMPAIGN_STATS = DATA / "campaign-stats"
ENV_PATH = PROJECT_ROOT / ".env"

SCRIPTS = PROJECT_ROOT / ".claude" / "skills"
HUBSPOT_PULL = SCRIPTS / "sdr-pipeline" / "scripts" / "hubspot_pull.py"
SDR_BATCHES = SCRIPTS / "sdr-pipeline" / "scripts" / "sdr_batches.py"
FETCH_STATS = SCRIPTS / "email-bison" / "scripts" / "fetch_campaign_stats.py"
FETCH_REPLIES = SCRIPTS / "email-bison" / "scripts" / "fetch_interested_replies.py"
GENERATE_BATCH = SCRIPTS / "sdr-pipeline" / "scripts" / "generate_batch.py"
CLASSIFY_REPLIES = SCRIPTS / "email-bison" / "scripts" / "classify_replies.py"
ANALYZE = SCRIPTS / "interested-trends" / "scripts"
TRENDS_DIR = DATA / "interested-replies" / "analysis"
REPLIES_LAST_RUN = DATA / "interested-replies" / "last_run.json"
REVIEW_QUEUE = DATA / "interested-replies" / "review_queue.json"
BATCH_JOBS_DIR = DATA / "outreach" / "batch-jobs"

PERSONA_ORDER = ["sales-leadership", "revops", "partnerships", "sdr-bdr"]
PERSONA_ENV = {
    "sales-leadership": "BISON_CAMPAIGN_SALES_LEADERSHIP",
    "revops": "BISON_CAMPAIGN_REVOPS",
    "partnerships": "BISON_CAMPAIGN_PARTNERSHIPS",
    "sdr-bdr": "BISON_CAMPAIGN_SDR_BDR",
}

# ----------------------------------------------------------------------------
# CTA derivation. No explicit CTA field exists in the generated JSON, and the
# copy is heavily persona-templated: nearly every sequence opens with a "signal
# play" and closes on a "playbook", so a single give-phrase collapses every
# contact into one bucket. To get a discriminating facet we match the give
# phrases (mirrors the GIVE regex in ai-sdr/scripts/lint_sequence.py) in
# RAREST-FIRST order, so the label reflects the most *distinctive* play offered
# in the sequence rather than the universal boilerplate. Empirically this yields
# pipeline-model (~82%, sales-leadership default), outbound-teardown (~11%,
# mostly revops), personalized-drafts (~7%, mostly sdr-bdr), benchmark.
# ----------------------------------------------------------------------------
CTA_RULES = [
    ("personalized-drafts", re.compile(r"\d+ (personalized|tailored) (emails|drafts)|personalized (emails|drafts)", re.I)),
    ("outbound-teardown", re.compile(r"\bteardown\b", re.I)),
    ("pipeline-model", re.compile(r"pipeline (model|gap)", re.I)),
    ("benchmark", re.compile(r"\bbenchmark\b", re.I)),
    ("playbook", re.compile(r"\bplaybook\b|one-?page(r)?", re.I)),
    ("signal-play", re.compile(r"signal play|plays? scoped|ai-?sdr plays?", re.I)),
    ("send-asset", re.compile(r"\bsent over\b|can i send|i can send|happy to send|want me to send", re.I)),
]
MEETING_RE = re.compile(
    r"walk (you|them) through|hop on|jump on|grab (15|20|30|a |some )?(min|minute|time)|"
    r"grab time|\b\d{1,2}[-\s]?(min|minute)s?\b|on a (quick )?(call|chat)|quick (call|chat)|"
    r"book (a )?(call|time|meeting|slot)|calendar|calendly|\bdemo\b|"
    r"open to (a )?(call|chat|conversation|meeting)|set up a call", re.I)


def derive_cta(asset):
    """Classify an outreach asset into a CTA/offer category."""
    email = asset.get("email", {}) or {}
    li = asset.get("linkedin", {}) or {}
    blob = " ".join(str(v) for v in list(email.values()) + list(li.values()))
    for name, rx in CTA_RULES:
        if rx.search(blob):
            return name
    if MEETING_RE.search(blob):
        return "meeting-only"
    return "unknown"


# ----------------------------------------------------------------------------
# Tiny .env reader (no python-dotenv dependency).
# ----------------------------------------------------------------------------
def read_env():
    env = {}
    if not ENV_PATH.is_file():
        return env
    for line in ENV_PATH.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        # strip inline comments + surrounding quotes/whitespace
        val = val.split("#", 1)[0].strip().strip('"').strip("'")
        env[key.strip()] = val
    return env


def persona_campaign_map():
    env = read_env()
    out = {}
    for persona, var in PERSONA_ENV.items():
        raw = env.get(var, "").strip()
        out[persona] = int(raw) if raw.isdigit() else None
    return out


# ----------------------------------------------------------------------------
# Read-only SQLite access. mode=ro still sees committed WAL data.
# ----------------------------------------------------------------------------
def db_connect():
    uri = f"file:{DB_PATH}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=1")
    return conn


def db_status():
    with db_connect() as conn:
        cstat = {r["status"]: r["n"] for r in
                 conn.execute("SELECT status, COUNT(*) n FROM contacts GROUP BY status")}
        bstat = {r["status"]: r["n"] for r in
                 conn.execute("SELECT status, COUNT(*) n FROM batches GROUP BY status")}
        total = conn.execute("SELECT COUNT(*) FROM contacts").fetchone()[0]
        by_persona = {r["persona"]: r["n"] for r in
                      conn.execute("SELECT persona, COUNT(*) n FROM contacts "
                                   "WHERE persona IS NOT NULL GROUP BY persona")}
        # per-persona status breakdown for the diagram
        persona_status = {}
        for r in conn.execute("SELECT persona, status, COUNT(*) n FROM contacts "
                              "WHERE persona IS NOT NULL GROUP BY persona, status"):
            persona_status.setdefault(r["persona"], {})[r["status"]] = r["n"]
    return {
        "total_contacts": total,
        "contacts_by_status": cstat,
        "batches_by_status": bstat,
        "by_persona": by_persona,
        "persona_status": persona_status,
    }


def db_batches(status=None, limit=None):
    with db_connect() as conn:
        q = "SELECT batch_id, status, size, claimed_at, completed_at FROM batches"
        params = []
        if status:
            q += " WHERE status=?"
            params.append(status)
        q += " ORDER BY batch_id"
        if limit:
            q += " LIMIT ?"
            params.append(int(limit))
        batches = [dict(r) for r in conn.execute(q, params)]
        # per-batch contact status counts in one grouped query
        counts = {}
        for r in conn.execute("SELECT batch_id, status, COUNT(*) n FROM contacts "
                              "WHERE batch_id IS NOT NULL GROUP BY batch_id, status"):
            counts.setdefault(r["batch_id"], {})[r["status"]] = r["n"]
        for b in batches:
            b["counts"] = counts.get(b["batch_id"], {})
        total = conn.execute("SELECT COUNT(*) FROM batches"
                             + (" WHERE status=?" if status else ""),
                             ([status] if status else [])).fetchone()[0]
    return {"batches": batches, "total": total}


def db_contact_meta():
    """contact_id -> (status, error, batch_id, persona) from the DB."""
    out = {}
    with db_connect() as conn:
        for r in conn.execute("SELECT contact_id, status, error, batch_id, persona FROM contacts"):
            out[r["contact_id"]] = {
                "status": r["status"], "error": r["error"],
                "batch_id": r["batch_id"], "persona": r["persona"],
            }
    return out


# ----------------------------------------------------------------------------
# In-memory outreach index. Built once; rebuilt on demand (POST /api/reindex)
# or automatically when the generated dir grows newer than the last build.
# ----------------------------------------------------------------------------
class OutreachIndex:
    def __init__(self):
        self.rows = []
        self.built_at = 0.0
        self.dir_mtime = 0.0
        self.lock = threading.Lock()

    def _load_contacts_jsonl(self):
        meta = {}
        if CONTACTS_JSONL.is_file():
            for line in CONTACTS_JSONL.read_text().splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                cid = str(rec.get("contact_id"))
                meta[cid] = rec
        return meta

    def build(self):
        with self.lock:
            jsonl_meta = self._load_contacts_jsonl()
            db_meta = {}
            try:
                db_meta = db_contact_meta()
            except sqlite3.Error:
                db_meta = {}
            rows = []
            if GEN_DIR.is_dir():
                for fp in GEN_DIR.glob("*.json"):
                    try:
                        asset = json.loads(fp.read_text())
                    except (json.JSONDecodeError, OSError):
                        continue
                    cid = str(asset.get("contact_id") or fp.stem)
                    cmeta = jsonl_meta.get(cid, {})
                    dbm = db_meta.get(cid, {})
                    rows.append({
                        "contact_id": cid,
                        "first_name": cmeta.get("first_name", ""),
                        "last_name": cmeta.get("last_name", ""),
                        "email": cmeta.get("email", ""),
                        "title": cmeta.get("title", ""),
                        "company": cmeta.get("company", ""),
                        "persona": asset.get("persona") or cmeta.get("persona") or dbm.get("persona") or "",
                        "signal": asset.get("signal", ""),
                        "cta_type": derive_cta(asset),
                        "status": dbm.get("status", ""),
                        "batch_id": dbm.get("batch_id"),
                    })
            # sort populated companies first (blanks last), then by name
            rows.sort(key=lambda r: (r["company"].strip() == "", r["company"].lower(), r["last_name"].lower()))
            self.rows = rows
            self.built_at = time.time()
            self.dir_mtime = self._current_dir_mtime()
        return len(rows)

    def _current_dir_mtime(self):
        try:
            return GEN_DIR.stat().st_mtime
        except OSError:
            return 0.0

    def maybe_rebuild(self):
        if not self.rows or self._current_dir_mtime() > self.dir_mtime:
            self.build()

    def query(self, params):
        self.maybe_rebuild()
        rows = self.rows

        def get(name):
            v = params.get(name, [""])
            return (v[0] if v else "").strip()

        persona = get("persona")
        status = get("status")
        cta = get("cta")
        company = get("company").lower()
        signal = get("signal").lower()
        q = get("q").lower()
        group_by = get("group_by")

        def matches(r):
            if persona and r["persona"] != persona:
                return False
            if status and r["status"] != status:
                return False
            if cta and r["cta_type"] != cta:
                return False
            if company and company not in r["company"].lower():
                return False
            if signal and signal not in r["signal"].lower():
                return False
            if q:
                hay = " ".join([
                    r["first_name"], r["last_name"], r["email"],
                    r["company"], r["title"], r["signal"],
                ]).lower()
                if q not in hay:
                    return False
            return True

        filtered = [r for r in rows if matches(r)]

        # facets computed over the full index (simple + fast for ~2k rows)
        def facet(field):
            out = {}
            for r in rows:
                out[r[field]] = out.get(r[field], 0) + 1
            return dict(sorted(out.items(), key=lambda kv: -kv[1]))

        facets = {
            "persona": facet("persona"),
            "cta_type": facet("cta_type"),
            "status": facet("status"),
        }

        groups = None
        if group_by in ("persona", "cta_type", "status", "company"):
            g = {}
            for r in filtered:
                g[r[group_by]] = g.get(r[group_by], 0) + 1
            groups = dict(sorted(g.items(), key=lambda kv: -kv[1]))

        try:
            page = max(1, int(get("page") or 1))
        except ValueError:
            page = 1
        try:
            page_size = min(200, max(1, int(get("page_size") or 50)))
        except ValueError:
            page_size = 50
        start = (page - 1) * page_size
        items = filtered[start:start + page_size]

        return {
            "total": len(filtered),
            "page": page,
            "page_size": page_size,
            "facets": facets,
            "groups": groups,
            "items": items,
        }


INDEX = OutreachIndex()


def outreach_detail(contact_id):
    fp = GEN_DIR / f"{contact_id}.json"
    if not fp.is_file():
        return None
    try:
        asset = json.loads(fp.read_text())
    except (json.JSONDecodeError, OSError):
        return None
    jsonl_meta = INDEX._load_contacts_jsonl().get(str(contact_id), {})
    dbm = db_contact_meta().get(str(contact_id), {})
    return {
        "contact": {
            "contact_id": str(contact_id),
            "first_name": jsonl_meta.get("first_name", ""),
            "last_name": jsonl_meta.get("last_name", ""),
            "email": jsonl_meta.get("email", ""),
            "title": jsonl_meta.get("title", ""),
            "company": jsonl_meta.get("company", ""),
            "linkedin_url": jsonl_meta.get("linkedin_url", ""),
            "buyer_role": jsonl_meta.get("buyer_role", ""),
            "persona": asset.get("persona") or jsonl_meta.get("persona", ""),
            "status": dbm.get("status", ""),
            "error": dbm.get("error"),
            "batch_id": dbm.get("batch_id"),
        },
        "signal": asset.get("signal", ""),
        "cta_type": derive_cta(asset),
        "email": asset.get("email", {}),
        "linkedin": asset.get("linkedin", {}),
    }


# ----------------------------------------------------------------------------
# Analytics: read cached campaign-stats files; refresh via subprocess.
# ----------------------------------------------------------------------------
def read_jsonl(path):
    rows = []
    if path.is_file():
        for line in path.read_text().splitlines():
            line = line.strip()
            if line:
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    return rows


def analytics_payload():
    campaigns = read_jsonl(CAMPAIGN_STATS / "campaigns.jsonl")
    steps = read_jsonl(CAMPAIGN_STATS / "step_stats.jsonl")
    last_run = {}
    lr = CAMPAIGN_STATS / "last_run.json"
    if lr.is_file():
        try:
            last_run = json.loads(lr.read_text())
        except json.JSONDecodeError:
            last_run = {}

    steps_by_campaign = {}
    for s in steps:
        steps_by_campaign.setdefault(s.get("campaign_id"), []).append(s)
    for c in campaigns:
        c["steps"] = sorted(steps_by_campaign.get(c.get("campaign_id"), []),
                            key=lambda s: s.get("step_number", 0))

    total_contacted = sum(c.get("total_leads_contacted") or 0 for c in campaigns)
    total_interested = sum(c.get("interested") or 0 for c in campaigns)
    total_replies = sum(c.get("unique_replies") or 0 for c in campaigns)
    total_leads = sum(c.get("total_leads") or 0 for c in campaigns)
    rate = lambda num, den: round(100 * num / den, 2) if den else None

    return {
        "fetched_at": last_run.get("fetched_at"),
        "campaigns": campaigns,
        "totals": {
            "total_leads": total_leads,
            "total_contacted": total_contacted,
            "total_replies": total_replies,
            "total_interested": total_interested,
            "overall_reply_rate_pct": rate(total_replies, total_contacted),
            "overall_interested_rate_pct": rate(total_interested, total_contacted),
        },
        "errors": last_run.get("errors", []),
    }


def rollup_payload():
    st = db_status()
    cmap = persona_campaign_map()
    analytics = analytics_payload()
    by_campaign = {c.get("campaign_id"): c for c in analytics["campaigns"]}
    personas = []
    for p in PERSONA_ORDER:
        cid = cmap.get(p)
        stats = None
        if cid is not None and cid in by_campaign:
            c = by_campaign[cid]
            stats = {
                "total_leads": c.get("total_leads"),
                "total_leads_contacted": c.get("total_leads_contacted"),
                "unique_replies": c.get("unique_replies"),
                "interested": c.get("interested"),
                "reply_rate_pct": c.get("reply_rate_pct"),
                "interested_rate_pct": c.get("interested_rate_pct"),
            }
        personas.append({
            "persona": p,
            "campaign_id": cid,
            "contacts": st["by_persona"].get(p, 0),
            "by_status": st["persona_status"].get(p, {}),
            "campaign_stats": stats,
        })
    return {"personas": personas, "personas_order": PERSONA_ORDER}


# ----------------------------------------------------------------------------
# Subprocess helpers.
# ----------------------------------------------------------------------------
def run_script(args, timeout=600):
    proc = subprocess.run(
        [sys.executable, *args],
        cwd=str(PROJECT_ROOT),
        capture_output=True, text=True, timeout=timeout,
    )
    return {"returncode": proc.returncode, "stdout": proc.stdout, "stderr": proc.stderr}


def do_ingest(list_id):
    pre = db_status()
    pull = run_script([str(HUBSPOT_PULL), str(list_id)], timeout=600)
    if pull["returncode"] != 0:
        return {"ok": False, "stage": "pull", "pull": pull}
    init = run_script([str(SDR_BATCHES), "init"], timeout=600)
    if init["returncode"] != 0:
        return {"ok": False, "stage": "init", "pull": pull, "init": init}
    INDEX.build()
    post = db_status()
    # Prefer the explicit "+N new contacts, +M new batches" from init stdout.
    m = re.search(r"\+(\d+)\s+new contacts.*?\+(\d+)\s+new batches", init["stdout"])
    new_contacts = int(m.group(1)) if m else (post["total_contacts"] - pre["total_contacts"])
    new_batches = int(m.group(2)) if m else (
        sum(post["batches_by_status"].values()) - sum(pre["batches_by_status"].values()))
    return {
        "ok": True,
        "pull": pull, "init": init,
        "new_contacts": new_contacts, "new_batches": new_batches,
        "pending_batches": db_batches(status="pending")["batches"],
        "status": post,
    }


def do_refresh():
    res = run_script([str(FETCH_STATS)], timeout=300)
    payload = analytics_payload()
    payload["refresh"] = res
    payload["ok"] = res["returncode"] == 0
    return payload


# ----------------------------------------------------------------------------
# Enrollment. Wraps `sdr_batches.py enroll [--dry-run]`, parsing its line output
# into a structured preview / result. Live enrollment writes to Bison, so it is
# gated behind an explicit confirm in the UI and a `confirm` flag here.
# ----------------------------------------------------------------------------
# matches: "  [dry] email [persona] -> campaign 10 (8 vars)"
#          "  [skip] email [persona] -> campaign 10: <reason>"
ENROLL_LINE = re.compile(
    r"^\s*\[(dry|skip)\]\s+(\S+)\s+\[([^\]]+)\]\s+->\s+campaign\s+(\S+)"
    r"(?:\s+\((\d+)\s+vars\))?(?::\s*(.*))?$")
# matches: "enroll (dry-run): {'enrolled': 0, ...}" / "enroll: {...}"
ENROLL_COUNTS = re.compile(r"enroll(?:\s*\(dry-run\))?:\s*(\{.*\})")


def _generated_count():
    try:
        with db_connect() as conn:
            return conn.execute("SELECT COUNT(*) FROM contacts WHERE status='generated'").fetchone()[0]
    except sqlite3.Error:
        return 0


def _parse_enroll(stdout):
    rows, counts = [], None
    for line in stdout.splitlines():
        m = ENROLL_LINE.match(line)
        if m:
            kind, email, persona, campaign, nvars, reason = m.groups()
            rows.append({
                "email": email, "persona": persona, "campaign": campaign,
                "vars": int(nvars) if nvars else None,
                "skipped": kind == "skip", "reason": (reason or "").strip() or None,
            })
            continue
        cm = ENROLL_COUNTS.search(line)
        if cm:
            try:
                counts = json.loads(cm.group(1).replace("'", '"'))
            except json.JSONDecodeError:
                counts = None
    # per-campaign rollup of planned/attempted enrollments
    by_campaign = {}
    for r in rows:
        by_campaign[r["campaign"]] = by_campaign.get(r["campaign"], 0) + 1
    return rows, counts, by_campaign


def _enrich_enroll_rows(rows):
    """Attach contact_id/name/company/signal/cta_type to each row by email so the
    UI can show the actual copy under review (joins the in-memory outreach index)."""
    INDEX.maybe_rebuild()
    by_email = {r["email"].lower(): r for r in INDEX.rows if r.get("email")}
    for row in rows:
        m = by_email.get((row.get("email") or "").lower())
        if m:
            row["contact_id"] = m["contact_id"]
            row["first_name"] = m["first_name"]
            row["last_name"] = m["last_name"]
            row["company"] = m["company"]
            row["signal"] = m["signal"]
            row["cta_type"] = m["cta_type"]
    return rows


def do_enroll(live=False):
    args = [str(SDR_BATCHES), "enroll"] + ([] if live else ["--dry-run"])
    res = run_script(args, timeout=900)
    rows, counts, by_campaign = _parse_enroll(res.get("stdout", ""))
    _enrich_enroll_rows(rows)
    if live:
        INDEX.build()  # statuses changed: generated -> enrolled/skipped
    return {
        "ok": res["returncode"] == 0,
        "live": live,
        "rows": rows,
        "counts": counts,
        "by_campaign": by_campaign,
        "generated_remaining": _generated_count(),
        "stdout": res["stdout"], "stderr": res["stderr"], "returncode": res["returncode"],
    }


# ----------------------------------------------------------------------------
# Lightweight progress endpoint for polling while /sdr-batches runs in Claude
# Code. DB-only (no index rebuild) so it is cheap to hit every few seconds.
# ----------------------------------------------------------------------------
def progress_payload():
    with db_connect() as conn:
        cstat = {r["status"]: r["n"] for r in
                 conn.execute("SELECT status, COUNT(*) n FROM contacts GROUP BY status")}
        bstat = {r["status"]: r["n"] for r in
                 conn.execute("SELECT status, COUNT(*) n FROM batches GROUP BY status")}
        total_contacts = conn.execute("SELECT COUNT(*) FROM contacts").fetchone()[0]
        total_batches = conn.execute("SELECT COUNT(*) FROM batches").fetchone()[0]
        # only surface batches that are not yet done (the live work), plus recent done
        active = [dict(r) for r in conn.execute(
            "SELECT batch_id, status, size, claimed_at, completed_at FROM batches "
            "WHERE status != 'done' ORDER BY batch_id")]
        per_counts = {}
        if active:
            ids = [b["batch_id"] for b in active]
            qmarks = ",".join("?" * len(ids))
            for r in conn.execute(
                f"SELECT batch_id, status, COUNT(*) n FROM contacts "
                f"WHERE batch_id IN ({qmarks}) GROUP BY batch_id, status", ids):
                per_counts.setdefault(r["batch_id"], {})[r["status"]] = r["n"]
        for b in active:
            b["counts"] = per_counts.get(b["batch_id"], {})
    return {
        "contacts_by_status": cstat,
        "batches_by_status": bstat,
        "total_contacts": total_contacts,
        "total_batches": total_batches,
        "active_batches": active,
        "generated_ready": cstat.get("generated", 0),
    }


# ----------------------------------------------------------------------------
# Interested-trends deep dive. Reads the cached analysis artifacts; refresh
# re-runs the fetch + analyze chain.
# ----------------------------------------------------------------------------
def _read_json(path):
    if path.is_file():
        try:
            return json.loads(path.read_text())
        except json.JSONDecodeError:
            return None
    return None


def trends_payload():
    summary = _read_json(TRENDS_DIR / "summary.json")
    conversion = _read_json(TRENDS_DIR / "conversion.json")
    cohorts = _read_json(TRENDS_DIR / "cohorts.json")
    last_run = _read_json(REPLIES_LAST_RUN) or {}
    return {
        "available": summary is not None,
        "fetched_at": last_run.get("fetched_at"),
        "total_interested": last_run.get("total_interested"),
        "summary": summary,
        "conversion": conversion,
        "cohorts": cohorts,
    }


def do_trends_refresh():
    steps = []
    fetch = run_script([str(FETCH_REPLIES)], timeout=900)
    steps.append({"step": "fetch_interested_replies", **fetch})
    for name in ("analyze_interested.py", "analyze_conversion.py", "analyze_cohorts.py"):
        r = run_script([str(ANALYZE / name)], timeout=600)
        steps.append({"step": name, **r})
    payload = trends_payload()
    payload["refresh"] = steps
    payload["ok"] = all(s["returncode"] == 0 for s in steps)
    return payload


# ----------------------------------------------------------------------------
# Copy generation jobs. Generation (Opus + web search, ~25 contacts) is too slow
# to run inside an HTTP handler, so it runs on a daemon thread and the UI polls.
# One generation job at a time. DB writes go through `sdr_batches.py ingest`.
# ----------------------------------------------------------------------------
JOBS = {}                      # job_id -> job dict
JOB_LOCK = threading.Lock()
ACTIVE_GEN_JOB = None          # job_id of the running generation job, or None
_JOB_SEQ = [0]                 # monotonic counter (uuid is unavailable-free)


def _new_job_id():
    with JOB_LOCK:
        _JOB_SEQ[0] += 1
        n = _JOB_SEQ[0]
    return f"gen-{n}"


def _serialize_job(job):
    if not job:
        return None
    return {
        "job_id": job["job_id"], "kind": job["kind"], "batch_id": job["batch_id"],
        "status": job["status"], "started_at": job["started_at"],
        "finished_at": job["finished_at"], "summary": job["summary"],
        "error": job["error"], "cancel_requested": job["cancel"].is_set(),
        "contacts": job["contacts"],
        "log": list(job["log"]),
    }


def _run_generate_job(job_id, batch_id):
    global ACTIVE_GEN_JOB
    job = JOBS[job_id]

    def log(msg):
        job["log"].append(msg)
        del job["log"][:-200]  # keep last 200

    def progress_cb(cid, state, **extra):
        with JOB_LOCK:
            c = job["contacts"].setdefault(cid, {"contact_id": cid})
            c["state"] = state
            for k in ("name", "company", "persona", "web_searches", "signal", "issues",
                      "cache_read", "cache_write"):
                if k in extra:
                    c[k] = extra[k]
        if state == "linted":
            cr = extra.get("cache_read", 0)
            cinfo = f", cache {cr / 1000:.1f}k read" if cr else ""
            log(f"[done] {extra.get('name') or cid} ({extra.get('web_searches', 0)} searches{cinfo})")
        elif state in ("failed", "error"):
            log(f"[{state}] {extra.get('name') or cid}: {'; '.join(extra.get('issues', []))[:160]}")
        elif state == "researching":
            log(f"[run ] {job['contacts'].get(cid, {}).get('name') or cid}")

    try:
        # import lazily so app startup never depends on the Anthropic client
        sys.path.insert(0, str(GENERATE_BATCH.parent))
        import generate_batch as G  # noqa: E402
        log(f"starting batch {batch_id}")
        summary = G.generate_batch(batch_id, progress_cb=progress_cb, cancel_event=job["cancel"])
        job["summary"] = {"total": summary["total"], "linted": summary["linted"],
                          "failed": summary["failed"]}
        log(f"generation done: {summary['linted']} linted, {summary['failed']} failed; ingesting…")
        ing = run_script([str(SDR_BATCHES), "ingest", str(batch_id)], timeout=120)
        log((ing["stdout"] or ing["stderr"]).strip()[:200])
        INDEX.build()
        job["status"] = "cancelled" if job["cancel"].is_set() else "done"
    except Exception as e:  # noqa: BLE001
        job["status"] = "error"
        job["error"] = f"{type(e).__name__}: {e}"
        log(f"ERROR: {job['error']}")
    finally:
        job["finished_at"] = now_iso()
        with JOB_LOCK:
            ACTIVE_GEN_JOB = None


def start_generate_job(batch_id):
    global ACTIVE_GEN_JOB
    with JOB_LOCK:
        if ACTIVE_GEN_JOB and JOBS.get(ACTIVE_GEN_JOB, {}).get("status") == "running":
            return {"ok": False, "error": "a generation job is already running",
                    "job_id": ACTIVE_GEN_JOB}, 409
        job_id = None
    job_id = _new_job_id()
    job = {
        "job_id": job_id, "kind": "generate", "batch_id": batch_id,
        "status": "running", "started_at": now_iso(), "finished_at": None,
        "contacts": {}, "log": [], "cancel": threading.Event(),
        "summary": {"total": 0, "linted": 0, "failed": 0}, "error": None,
    }
    with JOB_LOCK:
        JOBS[job_id] = job
        ACTIVE_GEN_JOB = job_id
    threading.Thread(target=_run_generate_job, args=(job_id, batch_id), daemon=True).start()
    return {"ok": True, "job_id": job_id}, 200


def now_iso():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


# ----------------------------------------------------------------------------
# Message Batches API jobs — async (submit -> poll -> retrieve), persisted to a
# JSON file per job so they survive restart, with a daemon poller per job.
# ----------------------------------------------------------------------------
BATCH_POLL_SECONDS = 30
_BATCH_POLLERS = set()  # job_ids with a live poller (avoid duplicates on resume)
_BATCH_LOCK = threading.Lock()


def _batch_job_path(job_id):
    return BATCH_JOBS_DIR / f"{job_id}.json"


def _read_batch_job(job_id):
    return _read_json(_batch_job_path(job_id))


def _write_batch_job(job):
    BATCH_JOBS_DIR.mkdir(parents=True, exist_ok=True)
    _batch_job_path(job["job_id"]).write_text(json.dumps(job, indent=2))


def _gen_mod():
    sys.path.insert(0, str(GENERATE_BATCH.parent))
    import generate_batch as G  # noqa: E402
    return G


def start_batch_job(limit=None, batch_ids=None):
    """Bundle N pending pipeline batches into one Anthropic Message Batch."""
    G = _gen_mod()
    with db_connect() as conn:
        pending = [r["batch_id"] for r in conn.execute(
            "SELECT batch_id FROM batches WHERE status='pending' ORDER BY batch_id")]
    if batch_ids:
        selected = [b for b in pending if b in set(batch_ids)]
    else:
        selected = pending[: int(limit)] if limit else pending
    if not selected:
        return {"ok": False, "error": "no pending batches to submit"}, 400

    import batch_db as bdb  # writable connection for get_batch/status
    conn = bdb.connect()
    contacts = []
    for bid in selected:
        contacts.extend(bdb.get_batch(conn, bid))
    conn.close()
    if not contacts:
        return {"ok": False, "error": "selected batches have no contacts"}, 400

    knowledge = G.load_knowledge()
    requests, manifest = G.prepare_batch_requests(contacts, knowledge)
    client = G.AnthropicClient()
    batch = client.create_batch(requests)

    job_id = f"batch-{batch['id'][-10:]}"
    n_cached = sum(1 for m in manifest.values() if not m["was_combined"])
    job = {
        "job_id": job_id, "anthropic_batch_id": batch["id"],
        "pipeline_batch_ids": selected, "status": "processing",
        "submitted_at": now_iso(), "ended_at": None,
        "request_count": len(requests), "write_only": n_cached, "researched": len(requests) - n_cached,
        "counts": batch.get("request_counts", {}), "manifest": manifest,
        "summary": {"linted": 0, "failed": 0}, "error": None,
    }
    _write_batch_job(job)
    # mark the pipeline batches in_progress so they aren't double-submitted
    conn = bdb.connect()
    for bid in selected:
        bdb.set_batch_status(conn, bid, "in_progress")
    conn.close()

    _start_batch_poller(job_id)
    return {"ok": True, "job_id": job_id, "anthropic_batch_id": batch["id"],
            "request_count": len(requests), "write_only": n_cached}, 200


def _start_batch_poller(job_id):
    with _BATCH_LOCK:
        if job_id in _BATCH_POLLERS:
            return
        _BATCH_POLLERS.add(job_id)
    threading.Thread(target=_poll_batch_job, args=(job_id,), daemon=True).start()


def _poll_batch_job(job_id):
    G = _gen_mod()
    import batch_db as bdb
    try:
        client = G.AnthropicClient()
        while True:
            job = _read_batch_job(job_id)
            if not job or job.get("status") != "processing":
                return  # done / cancelled / error -> stop
            try:
                batch = client.get_batch(job["anthropic_batch_id"])
            except Exception as e:  # noqa: BLE001
                # transient poll failure: the Anthropic batch keeps running and
                # results stay available for 29 days, so never fail the job on a
                # poll hiccup — just wait and try again.
                sys.stderr.write(f"[webui] batch poll retry ({job_id}): {e}\n")
                time.sleep(BATCH_POLL_SECONDS)
                continue
            # re-read to honor a concurrent cancel before writing/processing
            job = _read_batch_job(job_id)
            if not job or job.get("status") != "processing":
                return
            job["counts"] = batch.get("request_counts", {})
            _write_batch_job(job)
            if batch.get("processing_status") != "ended":
                time.sleep(BATCH_POLL_SECONDS)
                continue

            # ended -> process results (re-check status didn't flip to cancelled)
            job = _read_batch_job(job_id)
            if not job or job.get("status") != "processing":
                return
            manifest = job["manifest"]
            linted, retries = 0, []
            for item in client.get_batch_results(batch["results_url"]):
                r = G.process_batch_result(item.get("custom_id"), item.get("result", {}), manifest)
                if r["status"] == "linted":
                    linted += 1
                elif r["status"] == "retry":
                    retries.append(r.get("contact"))
            # synchronous retry tail for lint failures / errored requests
            knowledge = G.load_knowledge()
            retry_client = G.AnthropicClient()
            for contact in [c for c in retries if c]:
                try:
                    G.generate_one(contact, knowledge, retry_client)
                except Exception:  # noqa: BLE001
                    pass
            # record results via the canonical ingest path, mark batches done
            for bid in job["pipeline_batch_ids"]:
                run_script([str(SDR_BATCHES), "ingest", str(bid)], timeout=180)
            INDEX.build()
            with db_connect() as conn:
                ids = job["pipeline_batch_ids"]
                qmarks = ",".join("?" * len(ids))
                gen = conn.execute(
                    f"SELECT COUNT(*) FROM contacts WHERE batch_id IN ({qmarks}) AND status='generated'",
                    ids).fetchone()[0]
                failed = conn.execute(
                    f"SELECT COUNT(*) FROM contacts WHERE batch_id IN ({qmarks}) AND status='failed'",
                    ids).fetchone()[0]
            job = _read_batch_job(job_id)  # re-read (cancel may have raced)
            job["status"] = "done"
            job["ended_at"] = now_iso()
            job["summary"] = {"linted": gen, "failed": failed}
            job["counts"] = batch.get("request_counts", {})
            _write_batch_job(job)
            return
    except Exception as e:  # noqa: BLE001
        job = _read_batch_job(job_id)
        if job:
            job["status"] = "error"
            job["error"] = f"{type(e).__name__}: {e}"
            _write_batch_job(job)
    finally:
        with _BATCH_LOCK:
            _BATCH_POLLERS.discard(job_id)


def _batch_job_public(job):
    """Strip the heavy manifest before sending to the UI."""
    if not job:
        return None
    return {k: v for k, v in job.items() if k != "manifest"}


def batch_job_status(job_id):
    job = _read_batch_job(job_id)
    if not job:
        return None
    # ensure a poller is running if it's still processing (e.g. after restart)
    if job.get("status") == "processing":
        _start_batch_poller(job_id)
    return _batch_job_public(job)


def batch_jobs_list():
    jobs = []
    if BATCH_JOBS_DIR.is_dir():
        for fp in sorted(BATCH_JOBS_DIR.glob("*.json"), reverse=True):
            j = _read_json(fp)
            if j:
                jobs.append(_batch_job_public(j))
    return {"jobs": jobs}


def cancel_batch_job(job_id):
    job = _read_batch_job(job_id)
    if not job:
        return {"ok": False, "error": f"no job {job_id}"}
    G = _gen_mod()
    try:
        G.AnthropicClient().cancel_batch(job["anthropic_batch_id"])
    except Exception as e:  # noqa: BLE001
        pass
    import batch_db as bdb
    conn = bdb.connect()
    for bid in job.get("pipeline_batch_ids", []):
        bdb.set_batch_status(conn, bid, "pending")
    conn.close()
    job["status"] = "cancelled"
    job["ended_at"] = now_iso()
    _write_batch_job(job)
    return {"ok": True, "job_id": job_id}


def resume_batch_jobs():
    """On startup, restart pollers for any batch jobs still processing."""
    if not BATCH_JOBS_DIR.is_dir():
        return 0
    n = 0
    for fp in BATCH_JOBS_DIR.glob("*.json"):
        j = _read_json(fp)
        if j and j.get("status") == "processing":
            _start_batch_poller(j["job_id"])
            n += 1
    return n


# ----------------------------------------------------------------------------
# Interested-reply tagging. Classifier runs as a subprocess (writes the review
# queue); the gated write applies mark-as-interested + the Interested tag.
# ----------------------------------------------------------------------------
def _bison():
    sys.path.insert(0, str(SCRIPTS / "email-bison" / "scripts"))
    from bison_client import BisonClient  # noqa: E402
    return BisonClient()


def interested_tag_id():
    raw = read_env().get("BISON_INTERESTED_TAG_ID", "11")
    return int(raw) if str(raw).isdigit() else 11


def do_scan_replies(campaign_id=None, lookback_days=14):
    args = [str(CLASSIFY_REPLIES), "--lookback", str(lookback_days)]
    if campaign_id:
        args += ["--campaign", str(campaign_id)]
    res = run_script(args, timeout=600)
    payload = _read_json(REVIEW_QUEUE) or {"available": False}
    if isinstance(payload, dict):
        payload["available"] = REVIEW_QUEUE.is_file()
    payload = {"available": REVIEW_QUEUE.is_file(), **(payload if isinstance(payload, dict) else {})}
    payload["scan"] = res
    payload["ok"] = res["returncode"] == 0
    return payload


def review_queue_payload():
    data = _read_json(REVIEW_QUEUE)
    if not data:
        return {"available": False, "items": []}
    data["available"] = True
    return data


def do_tag_replies(reply_ids):
    """For each reply: mark-as-interested + attach the Interested tag to its lead."""
    queue = _read_json(REVIEW_QUEUE) or {}
    by_id = {str(it.get("reply_id")): it for it in queue.get("items", [])}
    tag_id = interested_tag_id()
    bison = _bison()
    results, tagged, failed = [], 0, 0
    for rid in reply_ids:
        item = by_id.get(str(rid), {})
        lead_id = item.get("lead_id")
        try:
            bison.mark_reply_interested(rid)
            if lead_id:
                bison.attach_tags_to_leads([tag_id], [lead_id])
            results.append({"reply_id": rid, "lead_id": lead_id, "ok": True})
            tagged += 1
            item["already_interested"] = True  # update cache
        except Exception as e:  # noqa: BLE001 - one bad reply must not abort the rest
            results.append({"reply_id": rid, "lead_id": lead_id, "ok": False, "error": str(e)[:200]})
            failed += 1
    if queue:
        REVIEW_QUEUE.write_text(json.dumps(queue, indent=2))
    return {"ok": failed == 0, "tagged": tagged, "failed": failed, "results": results}


# ----------------------------------------------------------------------------
# Per-company signal cache (read + force-refresh).
# ----------------------------------------------------------------------------
def _age_days(researched_at):
    if not researched_at:
        return None
    try:
        ts = time.strptime(researched_at, "%Y-%m-%dT%H:%M:%SZ")
    except (ValueError, TypeError):
        return None
    return int((time.time() - time.mktime(ts) + time.timezone) // 86400)


def signals_payload():
    with db_connect() as conn:
        try:
            rows = [dict(r) for r in conn.execute(
                "SELECT * FROM account_signals ORDER BY updated_at DESC")]
        except sqlite3.Error:
            rows = []
    for r in rows:
        r["age_days"] = _age_days(r.get("researched_at"))
        r["fresh"] = r["age_days"] is not None and r["age_days"] < 90
    return {"signals": rows, "count": len(rows)}


def do_refresh_signal(domain, company=None):
    domain = (domain or "").strip().lower()
    if not domain:
        return {"ok": False, "error": "domain required"}
    if not company:
        with db_connect() as conn:
            row = conn.execute(
                "SELECT company FROM contacts WHERE domain=? AND company IS NOT NULL AND company!='' LIMIT 1",
                (domain,)).fetchone()
            company = row["company"] if row else ""
    sys.path.insert(0, str(GENERATE_BATCH.parent))
    import generate_batch as G  # noqa: E402
    res = G.research_signal(domain, company)
    payload = signals_payload()
    payload["ok"] = True
    payload["refreshed"] = res
    return payload


# ----------------------------------------------------------------------------
# HTTP handler.
# ----------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    server_version = "sdr-webui/0.1"
    static_dir = None  # set in main()

    def log_message(self, fmt, *args):
        sys.stderr.write("[webui] " + (fmt % args) + "\n")

    # -- helpers ----------------------------------------------------------
    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def _json(self, obj, code=200):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self._cors()
        self.end_headers()
        self.wfile.write(body)

    def _error(self, code, msg):
        self._json({"ok": False, "error": msg}, code=code)

    def _read_body(self):
        length = int(self.headers.get("Content-Length", 0) or 0)
        if not length:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return {}

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    # -- routing ----------------------------------------------------------
    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        params = parse_qs(parsed.query)
        try:
            if path == "/api/status":
                return self._json(db_status())
            if path == "/api/batches":
                status = (params.get("status", [""])[0] or None)
                limit = (params.get("limit", [""])[0] or None)
                return self._json(db_batches(status=status, limit=limit))
            if path == "/api/rollup":
                return self._json(rollup_payload())
            if path == "/api/analytics":
                return self._json(analytics_payload())
            if path == "/api/progress":
                return self._json(progress_payload())
            if path == "/api/trends":
                return self._json(trends_payload())
            if path == "/api/replies/queue":
                return self._json(review_queue_payload())
            if path == "/api/signals":
                return self._json(signals_payload())
            if path.startswith("/api/generate/status/"):
                job_id = path[len("/api/generate/status/"):]
                job = JOBS.get(job_id)
                if not job:
                    return self._error(404, f"no job {job_id}")
                return self._json(_serialize_job(job))
            if path == "/api/generate/batch/list":
                return self._json(batch_jobs_list())
            if path.startswith("/api/generate/batch/status/"):
                job_id = path[len("/api/generate/batch/status/"):]
                pub = batch_job_status(job_id)
                if pub is None:
                    return self._error(404, f"no batch job {job_id}")
                return self._json(pub)
            if path == "/api/outreach":
                return self._json(INDEX.query(params))
            if path.startswith("/api/outreach/"):
                cid = path[len("/api/outreach/"):]
                detail = outreach_detail(cid)
                if detail is None:
                    return self._error(404, f"no generated copy for {cid}")
                return self._json(detail)
            if path.startswith("/api/"):
                return self._error(404, "unknown endpoint")
            # static / SPA fallback
            return self._serve_static(path)
        except Exception as e:  # noqa: BLE001 - surface errors to the client
            return self._error(500, f"{type(e).__name__}: {e}")

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path
        try:
            if path == "/api/ingest":
                body = self._read_body()
                list_id = str(body.get("list_id", "")).strip()
                if not list_id:
                    return self._error(400, "list_id required")
                return self._json(do_ingest(list_id))
            if path == "/api/analytics/refresh":
                return self._json(do_refresh())
            if path == "/api/enroll/dry-run":
                return self._json(do_enroll(live=False))
            if path == "/api/enroll/live":
                body = self._read_body()
                if body.get("confirm") is not True:
                    return self._error(400, "live enrollment requires confirm=true")
                return self._json(do_enroll(live=True))
            if path == "/api/trends/refresh":
                return self._json(do_trends_refresh())
            if path == "/api/generate":
                body = self._read_body()
                batch_id = body.get("batch_id")
                if batch_id is None:
                    return self._error(400, "batch_id required")
                payload, code = start_generate_job(int(batch_id))
                return self._json(payload, code=code)
            if path == "/api/generate/batch":
                body = self._read_body()
                payload, code = start_batch_job(limit=body.get("limit"),
                                                batch_ids=body.get("batch_ids"))
                return self._json(payload, code=code)
            if path.startswith("/api/generate/batch/cancel/"):
                job_id = path[len("/api/generate/batch/cancel/"):]
                return self._json(cancel_batch_job(job_id))
            if path.startswith("/api/generate/cancel/"):
                job_id = path[len("/api/generate/cancel/"):]
                job = JOBS.get(job_id)
                if not job:
                    return self._error(404, f"no job {job_id}")
                job["cancel"].set()
                return self._json({"ok": True, "job_id": job_id})
            if path == "/api/signals/refresh":
                body = self._read_body()
                return self._json(do_refresh_signal(body.get("domain"), body.get("company")))
            if path == "/api/replies/scan":
                body = self._read_body()
                return self._json(do_scan_replies(
                    campaign_id=body.get("campaign_id"),
                    lookback_days=int(body.get("lookback_days", 14)),
                ))
            if path == "/api/replies/tag":
                body = self._read_body()
                if body.get("confirm") is not True:
                    return self._error(400, "tagging requires confirm=true")
                reply_ids = body.get("reply_ids") or []
                if not reply_ids:
                    return self._error(400, "reply_ids required")
                return self._json(do_tag_replies(reply_ids))
            if path == "/api/reindex":
                n = INDEX.build()
                return self._json({"indexed": n, "built_at": INDEX.built_at})
            return self._error(404, "unknown endpoint")
        except subprocess.TimeoutExpired:
            return self._error(504, "subprocess timed out")
        except Exception as e:  # noqa: BLE001
            return self._error(500, f"{type(e).__name__}: {e}")

    # -- static -----------------------------------------------------------
    def _serve_static(self, path):
        if not self.static_dir:
            return self._error(404, "frontend not built (run: cd webui/frontend && npm run build)")
        rel = path.lstrip("/") or "index.html"
        target = (self.static_dir / rel).resolve()
        # prevent path traversal outside static_dir; SPA fallback otherwise
        if self.static_dir not in target.parents and target != self.static_dir:
            target = self.static_dir / "index.html"
        if not target.is_file():
            target = self.static_dir / "index.html"
        if not target.is_file():
            return self._error(404, "not found")
        ctype = {
            ".html": "text/html", ".js": "application/javascript",
            ".css": "text/css", ".json": "application/json",
            ".svg": "image/svg+xml", ".ico": "image/x-icon",
            ".png": "image/png", ".woff2": "font/woff2",
        }.get(target.suffix, "application/octet-stream")
        data = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def main():
    port = 8787
    static_dir = PROJECT_ROOT / "webui" / "frontend" / "dist"
    args = sys.argv[1:]
    for i, a in enumerate(args):
        if a == "--port" and i + 1 < len(args):
            port = int(args[i + 1])
        if a == "--static" and i + 1 < len(args):
            static_dir = Path(args[i + 1]).resolve()

    Handler.static_dir = static_dir if static_dir.is_dir() else None

    print(f"[webui] project root: {PROJECT_ROOT}")
    print(f"[webui] building outreach index ...", flush=True)
    n = INDEX.build()
    print(f"[webui] indexed {n} generated outreach files")
    resumed = resume_batch_jobs()
    if resumed:
        print(f"[webui] resumed {resumed} in-flight batch job(s)")
    if Handler.static_dir:
        print(f"[webui] serving frontend from {Handler.static_dir}")
    else:
        print(f"[webui] frontend dist not found; API-only (use Vite dev server)")
    print(f"[webui] listening on http://localhost:{port}")
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()


if __name__ == "__main__":
    main()
