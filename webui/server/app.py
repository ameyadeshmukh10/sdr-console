#!/usr/bin/env python3
"""Local MVP web UI backend for the SDR outbound pipeline.

Zero-dependency (Python stdlib only). Serves a small JSON API + the built React
frontend from a single process.

  python3 webui/server/app.py [--port 8787] [--static webui/frontend/dist]

Endpoints (all under /api, all JSON):
  GET  /api/status                 pipeline summary + persona rollup
  GET  /api/batches?status=&limit= batches with per-batch status counts
  GET  /api/orchestration/config   live pipeline config for the Orchestration view
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

import base64
import concurrent.futures
import hashlib
import hmac
import json
import os
import re
import secrets
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
HUBSPOT_LISTS = SCRIPTS / "sdr-pipeline" / "scripts" / "hubspot_lists.py"
HUBSPOT_ACTIVITY_SYNC = SCRIPTS / "sdr-pipeline" / "scripts" / "hubspot_activity_sync.py"
HEYREACH_ACTIVITY = SCRIPTS / "sdr-pipeline" / "scripts" / "heyreach_activity.py"
AISDR_SYNC = SCRIPTS / "sdr-pipeline" / "scripts" / "aisdr_attribution_sync.py"
HUBSPOT_ACTIVITY_AUDIT = SCRIPTS / "sdr-pipeline" / "scripts" / "hubspot_activity_audit.py"
UNENROLL_CHECK = SCRIPTS / "sdr-pipeline" / "scripts" / "unenrollment_check.py"
UNENROLL_STATUS_PATH = DATA / "outreach" / ".unenroll_status.json"
MAX_WEBHOOK_BODY = 1_048_576  # 1 MB cap on a webhook body (LinkedIn events are tiny) — DoS guard
SOURCE_CONTACTS = SCRIPTS / "sdr-pipeline" / "scripts" / "source_contacts.py"
CLAY_ENRICH = SCRIPTS / "sdr-pipeline" / "scripts" / "clay_enrich.py"
PIPELINE_SCRIPTS = SCRIPTS / "sdr-pipeline" / "scripts"
SOURCE_JOBS_DIR = DATA / "outreach" / "source-jobs"
CLASSIFY_REPLIES = SCRIPTS / "email-bison" / "scripts" / "classify_replies.py"
CLASSIFY_LI_REPLIES = SCRIPTS / "email-bison" / "scripts" / "classify_li_replies.py"
DRAFT_FOLLOWUPS = SCRIPTS / "email-bison" / "scripts" / "draft_followups.py"
FOLLOWUP_DRAFTS = DATA / "interested-replies" / "followup_drafts.json"
SENT_FOLLOWUPS = DATA / "interested-replies" / "sent_followups.json"
# Per-lead console state (dismissed / tagged / reclassified / agent choice). Keyed by
# lead identity — NOT reply_id — because scans rewrite the queue files and every new
# inbound reply gets a fresh reply_id; this file is what makes those actions durable.
REPLY_STATE = DATA / "interested-replies" / "reply_state.json"
AUTOSYNC_STATUS_PATH = DATA / "interested-replies" / ".autosync_status.json"
BUILD_PLAY = SCRIPTS / "signal-playbook" / "scripts" / "build_play.py"
SIGNAL_PLAYS_DIR = DATA / "signal-plays"
ANALYZE = SCRIPTS / "interested-trends" / "scripts"
TRENDS_DIR = DATA / "interested-replies" / "analysis"
REPLIES_LAST_RUN = DATA / "interested-replies" / "last_run.json"
REVIEW_QUEUE = DATA / "interested-replies" / "review_queue.json"
LI_REVIEW_QUEUE = DATA / "interested-replies" / "li_review_queue.json"
BATCH_JOBS_DIR = DATA / "outreach" / "batch-jobs"

# In-process pipeline-DB access for the HeyReach webhook path. The server's own
# db_connect() is read-only (mode=ro); persisting webhook events needs writes, so that
# one path uses batch_db.connect() (read-write WAL). heyreach_activity supplies the
# normalize_event/dedup_key helpers used to index events on receipt.
sys.path.insert(0, str(PIPELINE_SCRIPTS))
import batch_db as pipeline_db        # noqa: E402
import heyreach_activity              # noqa: E402
import mongo_store                    # noqa: E402  (lazy pymongo — safe without it)
import orchestration_config           # noqa: E402  (no I/O at import; parses on request)

PERSONA_ENV = {
    "sales-leadership": "BISON_CAMPAIGN_SALES_LEADERSHIP",
    "revops": "BISON_CAMPAIGN_REVOPS",
    "partnerships": "BISON_CAMPAIGN_PARTNERSHIPS",
    "sdr-bdr": "BISON_CAMPAIGN_SDR_BDR",
}
# Per-instruction-variant Bison campaigns. Enrollment routes by variant FIRST and
# only falls back to the persona campaign (then the default BISON_CAMPAIGN_ID), so
# both sets of campaigns can be live at once — see enroll.py / sdr_batches.cmd_enroll.
VARIANT_ORDER = ["value-give", "earn", "show"]
VARIANT_ENV = {
    "value-give": "BISON_CAMPAIGN_VALUE_GIVE",
    "earn": "BISON_CAMPAIGN_EARN",
    "show": "BISON_CAMPAIGN_SHOW",
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
    """Config the web UI reads (campaign IDs, HeyReach config, …). Merges the .env file
    (local dev) with the process environment (Railway/Docker, where there is NO .env file —
    config is injected as real env vars). The process environment wins, so host-provided
    config is always honored even when ENV_PATH is absent."""
    import os
    env = {}
    if ENV_PATH.is_file():
        for line in ENV_PATH.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            # strip inline comments + surrounding quotes/whitespace
            val = val.split("#", 1)[0].strip().strip('"').strip("'")
            env[key.strip()] = val
    env.update(os.environ)
    return env


# ----------------------------------------------------------------------------
# Authentication — a simple email+password gate over the whole /api surface.
#
# Stateless, HMAC-signed bearer tokens (stdlib only, no extra deps). The accepted
# users are stored as salted PBKDF2-HMAC-SHA256 hashes (never plaintext). The
# token-signing secret comes from AUTH_SECRET_KEY; if it is not set a random one
# is generated at startup, which means every restart invalidates outstanding
# tokens (everyone simply logs in again) — set AUTH_SECRET_KEY in production so
# sessions survive redeploys.
# ----------------------------------------------------------------------------
_AUTH_ITERATIONS = 240000
_AUTH_TOKEN_TTL = 7 * 24 * 3600  # 7 days

# email (lowercased) -> (salt_hex, pbkdf2_sha256_hex). Hashes only — no plaintext.
_USERS = {
    "ameya.deshmukh@everworker.ai": ("5101ada9dcb0404b5c6dcc1429de8223", "4971b8475fa88f188700a9504b891cad6277cd98fd338897004c7bba0978d85c"),
    "lucas.cowell@everworker.ai": ("e6d20cfb46294a3ae4d2b5246e1965c3", "4bdc195d7ad3c18ec8a7ddb29dabb77ea6a979083b2924c08b10b65018755f5b"),
    "alex.purtell@everworker.ai": ("7c9f90584312041c8bfe5bf0c226c080", "3d3c65fadd0e5635ab6c9b02544e6fdf49fc68f7bda6d6f665acdc57d40e94fe"),
    "demo@everworker.ai": ("67451f9a6a3505c9b880939b1c10ec19", "0bcf72b83a13b44146dc644e5d151e9024b6e137cd8e807d61d645fd01b69452"),
    "sales@everworker.ai": ("09ec9dd545d4f62d23614cb869866130", "5d846a99028b8c1a5a31dcea0cb4713624dd722ff3fdb649360e53d42ccda611"),
}

# Per-method exact-match auth exemptions (NOT prefix — that would leak siblings
# like /api/clay/oauth/start). External/non-browser callers that have no bearer:
#   /api/health             — public liveness probe (Railway healthcheck)
#   /api/clay/oauth/callback — browser redirect back from Clay's OAuth consent
#   /api/login              — the sign-in endpoint itself
#   /api/heyreach/webhook   — HeyReach posts here; secured by its own HMAC secret
_EXEMPT_GET = {"/api/health", "/api/clay/oauth/callback"}
_EXEMPT_POST = {"/api/login", "/api/heyreach/webhook"}


def _auth_secret():
    """The token-signing secret, read once at startup. Prefer AUTH_SECRET_KEY from
    the environment (or .env); otherwise generate an ephemeral one and warn."""
    secret = read_env().get("AUTH_SECRET_KEY", "").strip()
    if not secret:
        secret = secrets.token_hex(32)
        sys.stderr.write(
            "[auth] WARNING: AUTH_SECRET_KEY is not set — using an ephemeral "
            "signing secret; all sessions reset on restart. Set AUTH_SECRET_KEY "
            "in production so logins survive redeploys.\n")
    return secret.encode("utf-8")


_AUTH_SECRET = _auth_secret()


def verify_credentials(email, password):
    """True iff (email, password) matches an accepted user. Email is matched
    case-insensitively; the password is not. Constant-time hash comparison."""
    rec = _USERS.get((email or "").strip().lower())
    if not rec:
        return False
    salt_hex, hash_hex = rec
    dk = hashlib.pbkdf2_hmac(
        "sha256", (password or "").encode("utf-8"),
        bytes.fromhex(salt_hex), _AUTH_ITERATIONS)
    return hmac.compare_digest(dk.hex(), hash_hex)


def make_token(email):
    """Mint a signed bearer token: base64url(email|expiry).hex(hmac_sha256)."""
    payload = f"{email.strip().lower()}|{int(time.time()) + _AUTH_TOKEN_TTL}"
    body = base64.urlsafe_b64encode(payload.encode("utf-8")).decode("ascii")
    sig = hmac.new(_AUTH_SECRET, body.encode("ascii"), hashlib.sha256).hexdigest()
    return f"{body}.{sig}"


def verify_token(token):
    """Return the email for a valid, unexpired token, else None. Verifies the
    signature (constant-time) BEFORE decoding the untrusted payload."""
    if not token or "." not in token:
        return None
    body, _, sig = token.partition(".")
    expected = hmac.new(_AUTH_SECRET, body.encode("ascii"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expected):
        return None
    try:
        payload = base64.urlsafe_b64decode(body.encode("ascii")).decode("utf-8")
        email, _, exp = payload.partition("|")
        if not exp or int(exp) < int(time.time()):
            return None
        return email
    except (ValueError, UnicodeDecodeError):
        return None


def bearer_from_headers(headers):
    """Extract the bearer token from an Authorization header, or None."""
    raw = headers.get("Authorization", "") or ""
    return raw[7:].strip() if raw.startswith("Bearer ") else None


def _campaign_int(raw):
    raw = (raw or "").strip()
    return int(raw) if raw.isdigit() else None


def persona_campaign_map():
    env = read_env()
    return {persona: _campaign_int(env.get(var, "")) for persona, var in PERSONA_ENV.items()}


def variant_campaign_map():
    env = read_env()
    return {variant: _campaign_int(env.get(var, "")) for variant, var in VARIANT_ENV.items()}


def default_campaign_id():
    """The catch-all BISON_CAMPAIGN_ID — last fallback in enroll.py's routing."""
    return _campaign_int(read_env().get("BISON_CAMPAIGN_ID", ""))


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
    """contact_id -> full contact record from the DB (authoritative for ALL contacts,
    incl. ones sourced via Clay that never went through contacts.jsonl)."""
    out = {}
    with db_connect() as conn:
        for r in conn.execute(
            "SELECT contact_id, first_name, last_name, email, title, company, linkedin_url, "
            "persona, domain, variant, status, error, batch_id FROM contacts"):
            out[r["contact_id"]] = dict(r)
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
                    # contacts.jsonl first, DB fallback (sourced contacts are DB-only)
                    def meta(k):
                        return cmeta.get(k) or dbm.get(k) or ""
                    rows.append({
                        "contact_id": cid,
                        "first_name": meta("first_name"),
                        "last_name": meta("last_name"),
                        "email": meta("email"),
                        "title": meta("title"),
                        "company": meta("company"),
                        "persona": asset.get("persona") or meta("persona"),
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
    jm = INDEX._load_contacts_jsonl().get(str(contact_id), {})
    dbm = db_contact_meta().get(str(contact_id), {})

    def meta(k):  # contacts.jsonl first, DB fallback (sourced contacts are DB-only)
        return jm.get(k) or dbm.get(k) or ""
    return {
        "contact": {
            "contact_id": str(contact_id),
            "first_name": meta("first_name"),
            "last_name": meta("last_name"),
            "email": meta("email"),
            "title": meta("title"),
            "company": meta("company"),
            "linkedin_url": meta("linkedin_url"),
            "buyer_role": meta("buyer_role"),
            "persona": asset.get("persona") or meta("persona"),
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

    rate = lambda num, den: round(100 * num / den, 2) if den else None

    steps_by_campaign = {}
    for s in steps:
        # Interested rate = interested ÷ replies. Recomputed from the raw counts
        # so snapshots cached by an older fetch (which divided by contacted)
        # display the current metric without waiting for a refresh.
        s["interested_rate_pct"] = rate(s.get("interested") or 0, s.get("unique_replies") or 0)
        steps_by_campaign.setdefault(s.get("campaign_id"), []).append(s)
    for c in campaigns:
        c["interested_rate_pct"] = rate(c.get("interested") or 0, c.get("unique_replies") or 0)
        c["steps"] = sorted(steps_by_campaign.get(c.get("campaign_id"), []),
                            key=lambda s: s.get("step_number", 0))

    total_contacted = sum(c.get("total_leads_contacted") or 0 for c in campaigns)
    total_interested = sum(c.get("interested") or 0 for c in campaigns)
    total_replies = sum(c.get("unique_replies") or 0 for c in campaigns)
    total_leads = sum(c.get("total_leads") or 0 for c in campaigns)

    return {
        "fetched_at": last_run.get("fetched_at"),
        "campaigns": campaigns,
        "totals": {
            "total_leads": total_leads,
            "total_contacted": total_contacted,
            "total_replies": total_replies,
            "total_interested": total_interested,
            "overall_reply_rate_pct": rate(total_replies, total_contacted),
            "overall_interested_rate_pct": rate(total_interested, total_replies),
        },
        "errors": last_run.get("errors", []),
    }


def linkedin_analytics_payload():
    """Live HeyReach (LinkedIn) analytics for the configured campaign: the lead
    funnel (GetById progressStats) + connection/message/reply metrics
    (GetOverallStats). Degrades to {error}/{configured:false} so the Analytics
    page never breaks on a HeyReach hiccup."""
    env = read_env()
    raw = (env.get("HEYREACH_CAMPAIGN_ID") or "").strip()
    cid = int(raw) if raw.isdigit() else None
    if cid is None:
        return {"configured": False}
    try:
        sys.path.insert(0, str(SCRIPTS / "sdr-pipeline" / "scripts"))
        from heyreach_client import HeyReachClient
        hr = HeyReachClient()
        camp = hr.get_campaign(cid) or {}
        stats = (hr.get_overall_stats([cid]) or {}).get("overallStats") or {}
    except Exception as e:  # noqa: BLE001
        return {"configured": True, "campaign_id": cid, "error": str(e)[:200]}
    return {
        "configured": True, "campaign_id": cid,
        "campaign_name": camp.get("name"), "status": camp.get("status"),
        "funnel": camp.get("progressStats") or {}, "stats": stats,
        "fetched_at": now_iso(),
    }


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


def run_script_streaming(args, on_stderr_line=None, timeout=3600):
    """Like run_script but streams stderr line-by-line to a callback as it arrives
    (subprocess.run buffers until exit). stdout is still captured whole for the
    final JSON summary. Returns the same {returncode, stdout, stderr} shape."""
    proc = subprocess.Popen(
        [sys.executable, *args], cwd=str(PROJECT_ROOT),
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1)
    out_chunks, err_chunks = [], []

    def pump(stream, sink, cb):
        for line in iter(stream.readline, ""):
            sink.append(line)
            if cb:
                try:
                    cb(line.rstrip("\n"))
                except Exception:
                    pass
        stream.close()

    threads = [
        threading.Thread(target=pump, args=(proc.stdout, out_chunks, None), daemon=True),
        threading.Thread(target=pump, args=(proc.stderr, err_chunks, on_stderr_line), daemon=True),
    ]
    for t in threads:
        t.start()
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
    for t in threads:
        t.join(timeout=5)
    return {"returncode": proc.returncode, "stdout": "".join(out_chunks),
            "stderr": "".join(err_chunks)}


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


def do_hubspot_lists(query, list_type=None):
    """Search HubSpot lists by name. list_type in {contact, company} or None (both)."""
    args = [str(HUBSPOT_LISTS), "search", query or ""]
    if list_type in ("contact", "company"):
        args += ["--type", list_type]
    res = run_script(args, timeout=60)
    if res["returncode"] != 0:
        return {"ok": False, "error": (res["stderr"] or res["stdout"]).strip()[:300]}
    try:
        return {"ok": True, "lists": json.loads(res["stdout"] or "[]")}
    except json.JSONDecodeError:
        return {"ok": False, "error": "could not parse list search output"}


# One HubSpot activity sync at a time: the hourly autosync loop and the API path
# share this lock so two concurrent runs can't race the ledger's check-then-log.
HS_SYNC_LOCK = threading.Lock()
AUTOSYNC_STATUS = {}


def _autosync_status():
    """Last autosync outcome for the UI — from memory, falling back to the file
    mirror so a restart doesn't blank the indicator until the next cycle."""
    return AUTOSYNC_STATUS or (_read_json(AUTOSYNC_STATUS_PATH) or {})


def _record_autosync(ok, mode, summary):
    # Reassign (never mutate in place): request threads read this concurrently.
    global AUTOSYNC_STATUS
    status = {"ok": bool(ok), "mode": mode, "at": now_iso(),
              "summary": str(summary or "")[:300]}
    AUTOSYNC_STATUS = status
    try:
        AUTOSYNC_STATUS_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = AUTOSYNC_STATUS_PATH.with_name(AUTOSYNC_STATUS_PATH.name + ".tmp")
        tmp.write_text(json.dumps(status, indent=2))
        os.replace(tmp, AUTOSYNC_STATUS_PATH)
    except OSError:
        pass


def do_hubspot_activity_sync(since_days=None, limit=None, dry_run=False, contact_id=None,
                             event_types=None, replies_only=False, refresh_leads=False):
    """Shell out to the HubSpot activity-sync reconcile script and return its JSON
    summary. All logging logic lives in that script; this never touches HubSpot — and a
    sync failure is isolated here, it cannot affect sends/enrollment/scans."""
    args = [str(HUBSPOT_ACTIVITY_SYNC), "--json"]
    if dry_run:
        args.append("--dry-run")
    if replies_only:
        args.append("--replies-only")
    if refresh_leads:
        args.append("--refresh-leads")
    if since_days is not None:
        args += ["--since-days", str(int(since_days))]
    if limit is not None:
        args += ["--limit", str(int(limit))]
    if contact_id:
        args += ["--contact-id", str(contact_id)]
    for et in (event_types or []):
        if et in ("outbound", "inbound", "our_reply"):
            args += ["--event-type", et]
    with HS_SYNC_LOCK:
        res = run_script(args, timeout=3600)
    if res["returncode"] != 0:
        return {"ok": False, "error": (res["stderr"] or res["stdout"]).strip()[:500]}
    try:  # the script prints progress lines, then the JSON summary on the last line
        last = [ln for ln in (res["stdout"] or "").splitlines() if ln.strip()][-1]
        return json.loads(last)
    except (json.JSONDecodeError, IndexError):
        return {"ok": False, "error": "could not parse activity-sync output",
                "stdout": (res["stdout"] or "")[-500:]}


def system_status_payload():
    """Deploy/durability status: Railway volume attachment + the entrypoint's boot
    marker. volume_suspect flags a data dir that looks non-durable — either the
    seed ran more than once on the same marker, or we're on Railway with no volume
    mounted at /app/data (Railway injects RAILWAY_VOLUME_MOUNT_PATH when one is)."""
    marker = _read_json(DATA / ".boot-marker.json") or {}
    mount = (os.environ.get("RAILWAY_VOLUME_MOUNT_PATH") or "").rstrip("/")
    on_railway = bool(os.environ.get("RAILWAY_ENVIRONMENT")
                      or os.environ.get("RAILWAY_PROJECT_ID"))
    volume_attached = mount == "/app/data"
    suspect = (marker.get("seed_count") or 0) > 1 or (on_railway and not volume_attached)
    return {"ok": True, "boot_marker": marker, "on_railway": on_railway,
            "railway_volume_mount": mount or None,
            "volume_attached": volume_attached if on_railway else None,
            "volume_suspect": suspect,
            "hubspot_autosync": _autosync_status()}


def hubspot_activity_status_payload():
    """Read-only rollup of the activity ledger for the UI. Safe if the sync has never
    run (the table may not exist yet)."""
    try:
        conn = db_connect()
    except sqlite3.Error:
        return {"available": False, "logged": 0, "failed": 0, "by_type": {}}
    try:
        by_status = {r["status"]: r["n"] for r in conn.execute(
            "SELECT status, COUNT(*) n FROM hubspot_activity_log GROUP BY status")}
        by_type = {r["event_type"]: r["n"] for r in conn.execute(
            "SELECT event_type, COUNT(*) n FROM hubspot_activity_log "
            "WHERE status='logged' GROUP BY event_type")}
        last = conn.execute("SELECT MAX(created_at) m FROM hubspot_activity_log").fetchone()
        return {"available": True,
                "logged": by_status.get("logged", 0),
                "failed": by_status.get("failed", 0),
                "skipped_no_contact": by_status.get("skipped_no_contact", 0),
                "by_type": by_type,
                "last_logged_at": last["m"] if last else None}
    except sqlite3.Error:
        return {"available": False, "logged": 0, "failed": 0, "by_type": {}}
    finally:
        conn.close()


# ----------------------------------------------------------------------------
# AI SDR deal attribution (nightly HubSpot -> MongoDB sync). Reads are in-process
# via mongo_store; the sync itself shells out to aisdr_attribution_sync.py in a
# background thread. Everything degrades cleanly when MONGO_URL isn't wired yet.
# ----------------------------------------------------------------------------
_AISDR_SYNC_LOCK = threading.Lock()
_AISDR_SYNC_STATE = {"started_at": None, "last_result": None}


def aisdr_analytics_payload():
    """Aggregates for the Analytics tiles. Never raises: unconfigured -> a
    {"configured": false} hint, unreachable -> {"configured": true, "error": ...}
    (same degradation contract as linkedin_analytics_payload)."""
    if not mongo_store.mongo_configured():
        return {"configured": False}
    try:
        return mongo_store.aisdr_analytics(mongo_store.get_db())
    except Exception as e:  # noqa: BLE001 — tiles show the error, page keeps working
        return {"configured": True, "error": f"{type(e).__name__}: {e}"[:200]}


def aisdr_sync_status_payload():
    """Sync-run state for the UI: is one running now + the last run's summary."""
    out = {"configured": mongo_store.mongo_configured(),
           "running": _AISDR_SYNC_LOCK.locked(),
           "started_at": _AISDR_SYNC_STATE["started_at"],
           "last_result": _AISDR_SYNC_STATE["last_result"]}
    if out["configured"]:
        try:
            out.update(mongo_store.get_sync_state(mongo_store.get_db()) or {})
        except Exception as e:  # noqa: BLE001
            out["error"] = f"{type(e).__name__}: {e}"[:200]
    return out


def do_aisdr_sync(full=False, dry_run=False):
    """Kick the attribution sync in a background thread (the seed run takes a couple
    of minutes — too long for a request). 409 when one is already running. Returns
    (payload, http_code)."""
    if not mongo_store.mongo_configured():
        return ({"ok": False, "error": "MONGO_URL is not set — connect the Railway "
                                       "MongoDB service to sdr-console first"}, 400)
    if not _AISDR_SYNC_LOCK.acquire(blocking=False):
        return ({"ok": False, "error": "a sync is already running",
                 "started_at": _AISDR_SYNC_STATE["started_at"]}, 409)

    args = [str(AISDR_SYNC), "--json"]
    if full:
        args.append("--full")
    if dry_run:
        args.append("--dry-run")

    def _run():
        try:
            res = run_script(args, timeout=1800)
            lines = [ln for ln in (res.get("stdout") or "").splitlines() if ln.strip()]
            summary = lines[-1] if lines else (res.get("stderr") or "")[:300]
            _AISDR_SYNC_STATE["last_result"] = summary[:500]
            print(f"[aisdr-sync] done: {summary}", flush=True)
        except Exception as e:  # noqa: BLE001 — never leak into the server
            _AISDR_SYNC_STATE["last_result"] = f"{type(e).__name__}: {e}"[:500]
            print(f"[aisdr-sync] error: {type(e).__name__}: {e}", flush=True)
        finally:
            _AISDR_SYNC_LOCK.release()

    _AISDR_SYNC_STATE["started_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    try:
        threading.Thread(target=_run, daemon=True).start()
    except Exception as e:  # noqa: BLE001 — don't leak the lock if the thread can't start
        _AISDR_SYNC_LOCK.release()
        return ({"ok": False, "error": f"could not start sync thread: {e}"}, 500)
    return ({"ok": True, "started": True, "full": full, "dry_run": dry_run}, 202)


# ----------------------------------------------------------------------------
# Unenrollment checker (everworker_tag suppression). A 30-minute daemon loop and
# POST /api/unenroll/run share do_unenrollment_check(); the sweep itself lives in
# unenrollment_check.py (shelled out). Status = lock state + the unenrollment_log
# ledger (read-only) + the last run's summary (file-mirrored across restarts).
# ----------------------------------------------------------------------------
_UNENROLL_LOCK = threading.Lock()
_UNENROLL_STATE = {"started_at": None, "last_result": None, "progress": None}
UNENROLL_STATUS = {}


def _unenroll_status():
    """Last unenrollment-run outcome — memory first, file mirror after a restart."""
    return UNENROLL_STATUS or (_read_json(UNENROLL_STATUS_PATH) or {})


def _record_unenroll(ok, summary):
    # Reassign (never mutate in place): request threads read this concurrently.
    global UNENROLL_STATUS
    status = {"ok": bool(ok), "at": now_iso(), "summary": summary}
    UNENROLL_STATUS = status
    try:
        UNENROLL_STATUS_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = UNENROLL_STATUS_PATH.with_name(UNENROLL_STATUS_PATH.name + ".tmp")
        tmp.write_text(json.dumps(status, indent=2))
        os.replace(tmp, UNENROLL_STATUS_PATH)
    except OSError:
        pass


def unenrollment_status_payload():
    """Status for the Orchestration view. rules[] is the extensibility contract —
    each suppression rule the console runs appends one entry (everworker_tag is
    the first). Read-only; safe before the first run (table may not exist)."""
    env = read_env()
    enabled = (env.get("UNENROLL_CHECK_ENABLED", "1") or "1").strip().lower() \
        not in ("0", "false", "no")
    try:
        interval = max(5, int(env.get("UNENROLL_CHECK_MINUTES", "30") or 30))
    except ValueError:
        interval = 30
    counts = {"available": False}
    try:
        conn = db_connect()
        try:
            counts = pipeline_db.unenrollment_counts(conn, rule="everworker_tag")
            counts["available"] = True
        finally:
            conn.close()
    except sqlite3.Error:
        pass
    last = _unenroll_status()
    return {
        "enabled": enabled,
        "interval_minutes": interval,
        "running": _UNENROLL_LOCK.locked(),
        "started_at": _UNENROLL_STATE["started_at"],
        # Most recent run's parsed summary INCLUDING dry runs (which deliberately
        # never touch last_run) — this is how the UI shows dry-run results.
        "last_result": _UNENROLL_STATE["last_result"],
        # Latest progress line from an in-flight sweep (None when idle) — the UI
        # shows this instead of a silent spinner during long runs.
        "progress": _UNENROLL_STATE.get("progress"),
        "rules": [{
            "id": "everworker_tag",
            "name": "EverWorker tag suppression",
            "description": "everworker_tag=false in HubSpot: never enroll; stop "
                           "active Email Bison + HeyReach sequences.",
            "enabled": enabled,
            "channels": {
                "bison": {"configured": bool(env.get("EMAILBISON_API_KEY"))},
                "heyreach": {"configured": bool(env.get("HEYREACH_API_KEY"))},
            },
            "last_run": last or None,
            "counts": counts,
        }],
    }


def do_unenrollment_check(dry_run=False):
    """Kick one unenrollment sweep in a background thread. 409 when one is already
    running (the 30-min loop and the UI button share this). Returns (payload, code)."""
    if not read_env().get("HUBSPOT_ACCESS_TOKEN"):
        return ({"ok": False, "error": "HUBSPOT_ACCESS_TOKEN is not set — the checker "
                                       "needs HubSpot to find flagged contacts"}, 400)
    if not _UNENROLL_LOCK.acquire(blocking=False):
        return ({"ok": False, "error": "an unenrollment check is already running",
                 "started_at": _UNENROLL_STATE["started_at"]}, 409)

    args = [str(UNENROLL_CHECK), "--json"]
    if dry_run:
        args.append("--dry-run")

    def _progress(line):
        # The script's stderr progress lines, live: into Railway logs AND the
        # status payload, so a long sweep is never a silent spinner.
        _UNENROLL_STATE["progress"] = line[:300]
        print(line, flush=True)

    def _run():
        # dry_run comes from the closure, not the parsed output — a dry run must
        # never overwrite the persisted real-run status, even when it crashes or
        # its output is unparseable.
        try:
            res = run_script_streaming(args, on_stderr_line=_progress, timeout=3600)
            lines = [ln for ln in (res.get("stdout") or "").splitlines() if ln.strip()]
            summary = lines[-1] if lines else (res.get("stderr") or "")[:300]
            try:
                parsed = json.loads(summary)
            except (json.JSONDecodeError, TypeError):
                parsed = summary[:500]
            _UNENROLL_STATE["last_result"] = parsed
            ok = parsed.get("ok") if isinstance(parsed, dict) else res["returncode"] == 0
            if not dry_run:
                _record_unenroll(ok, parsed)
            print(f"[unenroll] done: {str(summary)[:300]}", flush=True)
        except Exception as e:  # noqa: BLE001 — never leak into the server
            _UNENROLL_STATE["last_result"] = f"{type(e).__name__}: {e}"[:500]
            if not dry_run:
                _record_unenroll(False, f"{type(e).__name__}: {e}"[:300])
            print(f"[unenroll] error: {type(e).__name__}: {e}", flush=True)
        finally:
            _UNENROLL_STATE["progress"] = None
            _UNENROLL_LOCK.release()

    _UNENROLL_STATE["started_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    try:
        threading.Thread(target=_run, daemon=True).start()
    except Exception as e:  # noqa: BLE001 — don't leak the lock if the thread can't start
        _UNENROLL_LOCK.release()
        return ({"ok": False, "error": f"could not start unenrollment thread: {e}"}, 500)
    return ({"ok": True, "started": True, "dry_run": dry_run}, 202)


def heyreach_activity_status_payload():
    """Read-only rollup of the HeyReach webhook inbox + the LinkedIn slice of the activity
    ledger. Safe before the first webhook (tables may not exist yet)."""
    try:
        conn = db_connect()
    except sqlite3.Error:
        return {"available": False, "inbox": {}, "logged": 0, "by_type": {}}
    try:
        inbox = {r["status"]: r["n"] for r in conn.execute(
            "SELECT status, COUNT(*) n FROM heyreach_events GROUP BY status")}
        by_type = {r["event_type"]: r["n"] for r in conn.execute(
            "SELECT event_type, COUNT(*) n FROM hubspot_activity_log "
            "WHERE status='logged' AND channel='linkedin' GROUP BY event_type")}
        by_status = {r["status"]: r["n"] for r in conn.execute(
            "SELECT status, COUNT(*) n FROM hubspot_activity_log "
            "WHERE channel='linkedin' GROUP BY status")}
        last = conn.execute("SELECT MAX(received_at) m FROM heyreach_events").fetchone()
        return {"available": True, "inbox": inbox,
                "logged": by_status.get("logged", 0),
                "failed": by_status.get("failed", 0),
                "skipped_no_contact": by_status.get("skipped_no_contact", 0),
                "by_type": by_type,
                "last_event_at": last["m"] if last else None}
    except sqlite3.Error:
        return {"available": False, "inbox": {}, "logged": 0, "by_type": {}}
    finally:
        conn.close()


# ----------------------------------------------------------------------------
# Clay MCP OAuth (connect once, backend auto-refreshes) — see clay_oauth.py.
# ----------------------------------------------------------------------------
def _clay_oauth():
    if str(PIPELINE_SCRIPTS) not in sys.path:
        sys.path.insert(0, str(PIPELINE_SCRIPTS))
    import clay_oauth  # noqa: E402
    return clay_oauth


def do_clay_status():
    try:
        return {"ok": True, "status": _clay_oauth().status()}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "status": "disconnected", "error": f"{type(e).__name__}: {e}"}


def do_clay_oauth_start(redirect_uri=None):
    try:
        return {"ok": True,
                "authorize_url": _clay_oauth().start_authorization(redirect_uri)}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


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
# matches: "  [dry] email [persona] -> bison campaign 14 (8 vars)"
#          "  [dry] email [persona] -> bison campaign 14 (8 vars) +LinkedIn"
#          "  [skip] email [persona] -> campaign 14: <reason>"
# The optional "bison " word and the trailing " +LinkedIn" tag were added to the
# dry-run output when HeyReach was wired into cmd_enroll; both are tolerated here
# so the preview keeps parsing (older "-> campaign N" output still matches too).
ENROLL_LINE = re.compile(
    r"^\s*\[(dry|skip)\]\s+(\S+)\s+\[([^\]]+)\]\s+->\s+(?:bison\s+)?campaign\s+(\S+)"
    r"(?:\s+\((\d+)\s+vars\))?(?:\s*\+LinkedIn)?(?::\s*(.*))?$")
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


def _run_generate_job(job_id, batch_id, variant="value-give"):
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
        log(f"starting batch {batch_id} [{variant}]")
        summary = G.generate_batch(batch_id, progress_cb=progress_cb, cancel_event=job["cancel"],
                                   variant=variant)
        job["summary"] = {"total": summary["total"], "linted": summary["linted"],
                          "failed": summary["failed"]}
        log(f"generation done: {summary['linted']} linted, {summary['failed']} failed; ingesting…")
        ing = run_script([str(SDR_BATCHES), "ingest", str(batch_id)], timeout=240)
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


VALID_VARIANTS = {"value-give", "earn", "show"}


def _clean_variant(v):
    return v if v in VALID_VARIANTS else "value-give"


def start_generate_job(batch_id, variant="value-give"):
    global ACTIVE_GEN_JOB
    with JOB_LOCK:
        if ACTIVE_GEN_JOB and JOBS.get(ACTIVE_GEN_JOB, {}).get("status") == "running":
            return {"ok": False, "error": "a generation job is already running",
                    "job_id": ACTIVE_GEN_JOB}, 409
        job_id = None
    job_id = _new_job_id()
    job = {
        "job_id": job_id, "kind": "generate", "batch_id": batch_id, "variant": variant,
        "status": "running", "started_at": now_iso(), "finished_at": None,
        "contacts": {}, "log": [], "cancel": threading.Event(),
        "summary": {"total": 0, "linted": 0, "failed": 0}, "error": None,
    }
    with JOB_LOCK:
        JOBS[job_id] = job
        ACTIVE_GEN_JOB = job_id
    threading.Thread(target=_run_generate_job, args=(job_id, batch_id, variant), daemon=True).start()
    return {"ok": True, "job_id": job_id}, 200


def now_iso():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


# ----------------------------------------------------------------------------
# Clay sourcing jobs — backend-driven Clay enrichment of a company list's buying
# group, then the deterministic HubSpot+pipeline path (source_contacts.py). Long
# running, so run as a background job with status polling (in-memory, like the
# generate job). mode "end-to-end" commits immediately; "review" pauses with the
# candidate rows until a /api/source/confirm.
# ----------------------------------------------------------------------------
SOURCE_JOBS = {}            # job_id -> job dict
_SOURCE_SEQ = [0]


def _new_source_job_id():
    with JOB_LOCK:
        _SOURCE_SEQ[0] += 1
        return f"src-{_SOURCE_SEQ[0]}"


def _source_job_public(job):
    if not job:
        return None
    return {k: v for k, v in job.items() if k != "thread"}


def _source_progress_path(list_id):
    return SOURCE_JOBS_DIR / f"progress-{list_id}.json"


def read_source_progress(list_id):
    """How many companies in this list have already been enriched."""
    p = _source_progress_path(list_id)
    if not p.is_file():
        return {"enriched": 0}
    try:
        data = json.loads(p.read_text())
        return {"enriched": int(data.get("count", len(data.get("enriched", []))))}
    except (ValueError, OSError):
        return {"enriched": 0}


def start_source_job(list_id, list_name=None, cap=25, mode="end-to-end",
                     per_company_cap=0, concurrency=8, titles="", locations="",
                     reset=False, whole_list=False):
    clay = _clay_oauth()
    if clay.status() != "connected":
        return {"ok": False, "error": "Clay is not connected — connect Clay first"}, 409
    job_id = _new_source_job_id()
    whole_list = bool(whole_list)
    # Whole-list runs are unattended, so they always commit (end-to-end); `cap` is
    # the per-batch size and the job loops until the list is exhausted.
    mode = "end-to-end" if whole_list else (mode if mode in ("end-to-end", "review") else "end-to-end")
    SOURCE_JOBS[job_id] = {
        "job_id": job_id, "kind": "source", "status": "running",
        "list_id": str(list_id), "list_name": list_name,
        "cap": int(cap), "mode": mode, "whole_list": whole_list,
        # Optional enrichment knobs surfaced from the UI's Advanced section.
        "per_company_cap": max(0, int(per_company_cap or 0)),
        "concurrency": max(1, int(concurrency or 8)),
        "titles": (titles or "").strip(),
        "locations": (locations or "").strip(),
        # Auto-advance cursor: persisted per list so each run takes the next batch.
        "progress_file": str(_source_progress_path(list_id)),
        "reset": bool(reset),
        "candidates_path": str(SOURCE_JOBS_DIR / f"{job_id}-candidates.json"),
        "candidates": None, "enrich": None, "source": None,
        "started_at": now_iso(), "finished_at": None, "error": None,
    }
    threading.Thread(target=_run_source_job, args=(job_id,), daemon=True).start()
    return {"ok": True, "job_id": job_id}, 200


def _enrich_args(job, out_path, reset):
    args = [str(CLAY_ENRICH), "--list-id", job["list_id"],
            "--cap", str(job["cap"]),
            "--concurrency", str(job.get("concurrency", 8)),
            "--out", out_path]
    if job.get("per_company_cap"):
        args += ["--per-company-cap", str(job["per_company_cap"])]
    if job.get("titles"):
        args += ["--titles", job["titles"]]
    if job.get("locations"):
        args += ["--locations", job["locations"]]
    if job.get("progress_file"):
        args += ["--progress-file", job["progress_file"]]
    if reset:
        args += ["--reset-progress"]
    return args


def _make_progress_cb(job):
    """Parse the enrich script's streamed stderr into job['progress'] live."""
    def on_line(line):
        p = job["progress"]
        m = re.search(r"this run takes (\d+)", line)
        if m:
            p["total"], p["phase"] = int(m.group(1)), "firing"
        m = re.search(r"firing (\d+)/(\d+) tasks", line)
        if m:
            p["fired"], p["total"], p["phase"] = int(m.group(1)), int(m.group(2)), "firing"
        m = re.search(r"\[(\d+)/(\d+)\]\s+\S+\s+done", line)
        if m:
            p["completed"], p["total"], p["phase"] = int(m.group(1)), int(m.group(2)), "polling"
        m = re.search(r"resolved:\s+(\d+)\s+contacts", line)
        if m:
            p["contacts"] += int(m.group(1))
    return on_line


def _run_enrich_batch(job, out_path, reset):
    """Run one enrich batch into out_path. Returns (enrich_result, summary_dict)."""
    job["progress"] = {"total": job["cap"], "completed": 0, "fired": 0, "contacts": 0,
                       "phase": "starting", "batch": (job.get("whole") or {}).get("batch")}
    enrich = run_script_streaming(_enrich_args(job, out_path, reset),
                                  _make_progress_cb(job), timeout=3600)
    job["progress"]["phase"] = "done"
    job["enrich"] = enrich
    summary = {}
    if enrich["returncode"] == 0:
        try:
            summary = json.loads(enrich["stdout"])
        except json.JSONDecodeError:
            summary = {}
    return enrich, summary


def _commit_candidates(candidates_path, list_name):
    """Run source_contacts.py on a candidates file: dedup, create in HubSpot,
    make/reuse the static list, ingest into the pipeline. Returns a result dict."""
    args = [str(SOURCE_CONTACTS), candidates_path]
    if list_name:
        args += ["--list-name", list_name]
    res = run_script(args, timeout=1800)
    if res["returncode"] != 0:
        return {"ok": False, "error": (res["stderr"] or res["stdout"]).strip()[:500], "raw": res}
    try:
        stats = json.loads(res["stdout"])
    except json.JSONDecodeError:
        stats = {}
    return {"ok": True, "raw": res, **stats}


def _run_source_job(job_id):
    job = SOURCE_JOBS[job_id]
    try:
        SOURCE_JOBS_DIR.mkdir(parents=True, exist_ok=True)
        if job.get("whole_list"):
            _run_whole_list_job(job)
        else:
            _run_single_batch_job(job)
    except Exception as e:  # noqa: BLE001
        job["status"] = "error"
        job["error"] = f"{type(e).__name__}: {e}"
    finally:
        if job["status"] in ("done", "error"):
            job["finished_at"] = now_iso()


def _run_single_batch_job(job):
    enrich, _ = _run_enrich_batch(job, job["candidates_path"], job.get("reset"))
    if enrich["returncode"] != 0:
        job["status"] = "error"
        job["error"] = (enrich["stderr"] or enrich["stdout"]).strip()[:500]
        return
    candidates = _read_json(Path(job["candidates_path"])) or []
    job["candidates"] = candidates
    if not candidates:
        job["status"] = "done"
        job["source"] = {"note": "no candidates with a work email were found"}
        return
    if job["mode"] == "review":
        job["status"] = "awaiting_review"
        return
    _commit_source_job(job)


def _run_whole_list_job(job):
    """Auto-continue: enrich + commit batches back-to-back until the list is
    exhausted. Each batch advances the cursor and commits, so a failure only
    costs the current batch — re-running resumes from where it stopped."""
    job["whole"] = {"batch": 0, "companies_total": None, "companies_processed": 0,
                    "contacts_created": 0, "remaining": None, "batches": []}
    MAX_BATCHES = 400  # safety stop; cap*400 dwarfs any real list
    for n in range(1, MAX_BATCHES + 1):
        job["whole"]["batch"] = n
        out_path = str(SOURCE_JOBS_DIR / f"{job['job_id']}-batch{n}-candidates.json")
        job["candidates_path"] = out_path
        reset = bool(job.get("reset")) and n == 1  # reset cursor only on the first batch
        enrich, summary = _run_enrich_batch(job, out_path, reset)
        if enrich["returncode"] != 0:
            job["status"] = "error"
            job["error"] = f"batch {n} enrich failed: {(enrich['stderr'] or enrich['stdout']).strip()[:300]}"
            return
        companies = int(summary.get("companies", 0))
        remaining = summary.get("remaining_after")
        if job["whole"]["companies_total"] is None:
            job["whole"]["companies_total"] = companies + int(remaining or 0)
        if companies == 0:  # nothing left to enrich — list exhausted
            break
        candidates = _read_json(Path(out_path)) or []
        job["candidates"] = candidates
        if candidates:
            commit = _commit_candidates(out_path, job.get("list_name"))
            if not commit.get("ok"):
                job["status"] = "error"
                job["error"] = f"batch {n} commit failed: {commit.get('error', '')[:300]}"
                return
            job["stats"] = {k: v for k, v in commit.items() if k not in ("ok", "raw")}
            job["whole"]["contacts_created"] += int(commit.get("created", 0))
            job["whole"]["batches"].append({
                "batch": n, "companies": companies, "candidates": len(candidates),
                "created": int(commit.get("created", 0)), "list_id": commit.get("hubspot_list_id")})
            INDEX.build()
        else:
            job["whole"]["batches"].append({"batch": n, "companies": companies,
                                            "candidates": 0, "created": 0})
        job["whole"]["companies_processed"] += companies
        job["whole"]["remaining"] = remaining
        if remaining is not None and int(remaining) <= 0:  # cursor reached the end
            break
    job["status"] = "done"


def _commit_source_job(job):
    """Commit a single batch's candidates (end-to-end / review-approve path)."""
    result = _commit_candidates(job["candidates_path"], job.get("list_name"))
    job["source"] = result.get("raw")
    if not result.get("ok"):
        job["status"] = "error"
        job["error"] = result.get("error")
        return
    job["stats"] = {k: v for k, v in result.items() if k not in ("ok", "raw")}
    INDEX.build()
    job["status"] = "done"


def confirm_source_job(job_id):
    job = SOURCE_JOBS.get(job_id)
    if not job:
        return {"ok": False, "error": f"no source job {job_id}"}, 404
    if job["status"] != "awaiting_review":
        return {"ok": False, "error": f"job is {job['status']}, not awaiting review"}, 409
    job["status"] = "running"
    threading.Thread(target=_confirm_source_thread, args=(job_id,), daemon=True).start()
    return {"ok": True, "job_id": job_id}, 200


def _confirm_source_thread(job_id):
    job = SOURCE_JOBS[job_id]
    try:
        _commit_source_job(job)
    except Exception as e:  # noqa: BLE001
        job["status"] = "error"
        job["error"] = f"{type(e).__name__}: {e}"
    finally:
        job["finished_at"] = now_iso()


# ----------------------------------------------------------------------------
# Message Batches API jobs — async (submit -> poll -> retrieve), persisted to a
# JSON file per job so they survive restart, with a daemon poller per job.
# ----------------------------------------------------------------------------
BATCH_POLL_SECONDS = 30
BATCH_RETRY_WORKERS = 6   # concurrency for re-generating lint-failed copy
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


def _normalize_split(split):
    """Validate a {variant: percent} dict. Returns a clean dict of positive
    percentages over known variants, or None if unusable (caller falls back to a
    single variant)."""
    if not isinstance(split, dict):
        return None
    clean = {}
    for k, v in split.items():
        if k in VALID_VARIANTS:
            try:
                pct = float(v)
            except (TypeError, ValueError):
                continue
            if pct > 0:
                clean[k] = pct
    return clean or None


def _assign_variant_split(contacts, split):
    """Assign each contact a variant per the % split: largest-remainder for exact
    counts, then spread evenly (ratio-deficit greedy) so variants aren't clustered
    in the domain-sorted order. Mutates contacts in place; returns the counts."""
    n = len(contacts)
    total = sum(split.values())
    variants = [v for v in ("value-give", "earn", "show") if split.get(v, 0) > 0]
    raw = {v: n * split[v] / total for v in variants}
    counts = {v: int(raw[v]) for v in variants}
    leftover = n - sum(counts.values())
    for v in sorted(variants, key=lambda v: raw[v] - counts[v], reverse=True):
        if leftover <= 0:
            break
        counts[v] += 1
        leftover -= 1
    remaining = dict(counts)
    for c in contacts:
        pick = max(variants, key=lambda v: (remaining[v] / counts[v]) if counts[v] else -1)
        c["variant"] = pick
        remaining[pick] -= 1
    return counts


def start_batch_job(limit=None, batch_ids=None, variant="value-give", split=None):
    """Bundle N pending pipeline batches into one Anthropic Message Batch.

    If `split` ({variant: percent}) is given, variants are assigned across the
    selected contacts by those proportions (overriding any per-contact variant);
    otherwise the single `variant` applies."""
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

    split = _normalize_split(split)
    split_counts = _assign_variant_split(contacts, split) if split else None

    knowledge = G.load_knowledge()
    requests, manifest = G.prepare_batch_requests(contacts, knowledge, variant=variant)
    client = G.AnthropicClient()
    batch = client.create_batch(requests)

    job_id = f"batch-{batch['id'][-10:]}"
    n_cached = sum(1 for m in manifest.values() if not m["was_combined"])
    job = {
        "job_id": job_id, "anthropic_batch_id": batch["id"], "variant": variant,
        "split": split_counts, "pipeline_batch_ids": selected, "status": "processing",
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
            # Retry tail for lint failures / errored requests — run concurrently.
            # A large batch can have hundreds of lint failures; doing these one at
            # a time (each a fresh web-search generation) turned into hours. Each
            # generate_one writes its own per-contact file, so they parallelize
            # cleanly (the AnthropicClient is stateless across calls).
            knowledge = G.load_knowledge()
            retry_client = G.AnthropicClient()
            retry_variant = job.get("variant", "value-give")
            retry_contacts = [c for c in retries if c]

            def _retry_one(contact):
                try:
                    G.generate_one(contact, knowledge, retry_client,
                                   variant=contact.get("variant") or retry_variant)
                except Exception:  # noqa: BLE001
                    pass

            if retry_contacts:
                with concurrent.futures.ThreadPoolExecutor(max_workers=BATCH_RETRY_WORKERS) as ex:
                    list(ex.map(_retry_one, retry_contacts))
            # record results via the canonical ingest path, mark batches done
            for bid in job["pipeline_batch_ids"]:
                run_script([str(SDR_BATCHES), "ingest", str(bid)], timeout=300)
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
            # Fire-and-forget technographic + hiring scans for this batch's
            # companies — cache-aware and best-effort, so the batch's "done"
            # status is never delayed and a detector failure only logs.
            # (Detection is deliberately NOT done inside process_batch_result:
            # that loop is serial over potentially hundreds of results.
            # Separate threads so one detector's failure never blocks the other.)
            tail_domains = sorted({(m.get("domain") or "") for m in manifest.values()} - {""})
            if tail_domains and (os.environ.get("TECH_DETECT_ENABLED") or "1").strip().lower() not in ("0", "false", "no", "off"):
                def _tech_tail():
                    try:
                        import tech_signals as T  # noqa: E402
                        s = T.backfill(domains=tail_domains, workers=3)
                        sys.stderr.write(f"[webui] tech backfill for batch job {job_id}: {s}\n")
                    except Exception as e:  # noqa: BLE001
                        sys.stderr.write(f"[webui] tech backfill skipped ({job_id}): {e}\n")
                threading.Thread(target=_tech_tail, daemon=True).start()
            if tail_domains and (os.environ.get("HIRING_DETECT_ENABLED") or "1").strip().lower() not in ("0", "false", "no", "off"):
                def _hiring_tail():
                    try:
                        import hiring_signals as H  # noqa: E402
                        if not H.hiring_available()[0]:
                            return
                        s = H.backfill(domains=tail_domains, workers=3)
                        sys.stderr.write(f"[webui] hiring backfill for batch job {job_id}: {s}\n")
                    except Exception as e:  # noqa: BLE001
                        sys.stderr.write(f"[webui] hiring backfill skipped ({job_id}): {e}\n")
                threading.Thread(target=_hiring_tail, daemon=True).start()
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


def _annotate_enrollment(jobs):
    """Add contact-status counts + `enrolled_live` to done jobs, in place.

    A done job whose pipeline batches have no contacts left in `generated`
    and at least one `enrolled`/`skipped` has been through a live enroll —
    the UI collapses those rows. Best-effort: any DB problem leaves the
    jobs unannotated (they stay visible)."""
    done = [j for j in jobs if j and j.get("status") == "done"
            and j.get("pipeline_batch_ids")]
    if not done:
        return
    try:
        ids = sorted({int(b) for j in done for b in j["pipeline_batch_ids"]})
        marks = ",".join("?" * len(ids))
        with db_connect() as conn:
            rows = conn.execute(
                f"SELECT batch_id, status, COUNT(*) n FROM contacts "
                f"WHERE batch_id IN ({marks}) GROUP BY batch_id, status", ids).fetchall()
        by_batch = {}
        for r in rows:
            by_batch.setdefault(r["batch_id"], {})[r["status"]] = r["n"]
        for j in done:
            counts = {}
            for bid in j["pipeline_batch_ids"]:
                for status, n in by_batch.get(int(bid), {}).items():
                    counts[status] = counts.get(status, 0) + n
            j["contact_counts"] = counts
            j["enrolled_live"] = (counts.get("generated", 0) == 0
                                  and counts.get("enrolled", 0) + counts.get("skipped", 0) > 0)
    except Exception:
        pass


def batch_job_status(job_id):
    job = _read_batch_job(job_id)
    if not job:
        return None
    # ensure a poller is running if it's still processing (e.g. after restart)
    if job.get("status") == "processing":
        _start_batch_poller(job_id)
    pub = _batch_job_public(job)
    _annotate_enrollment([pub])
    return pub


def batch_jobs_list():
    jobs = []
    if BATCH_JOBS_DIR.is_dir():
        for fp in sorted(BATCH_JOBS_DIR.glob("*.json"), reverse=True):
            j = _read_json(fp)
            if j:
                jobs.append(_batch_job_public(j))
    _annotate_enrollment(jobs)
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


def _heyreach():
    sys.path.insert(0, str(SCRIPTS / "sdr-pipeline" / "scripts"))
    from heyreach_client import HeyReachClient  # noqa: E402
    return HeyReachClient()


def interested_tag_id():
    raw = read_env().get("BISON_INTERESTED_TAG_ID", "11")
    return int(raw) if str(raw).isdigit() else 11


def _merged_queue_items():
    """All review-queue items across channels, each stamped with its channel.
    Email items (review_queue.json) default to channel='email'; LinkedIn items
    (li_review_queue.json) already carry channel='linkedin'."""
    items = []
    for default_channel, path in (("email", REVIEW_QUEUE), ("linkedin", LI_REVIEW_QUEUE)):
        data = _read_json(path) or {}
        for it in (data.get("items") or []):
            it.setdefault("channel", default_channel)
            items.append(it)
    return items


def do_scan_replies(campaign_id=None, lookback_days=14):
    # Email: auto-apply high-confidence unsubscribe / "close the file" replies
    # (unsubscribed + blacklisted in Bison, kept out of the review queue).
    args = [str(CLASSIFY_REPLIES), "--lookback", str(lookback_days), "--apply-unsubscribes"]
    if campaign_id:
        args += ["--campaign", str(campaign_id)]
    res = run_script(args, timeout=600)
    # LinkedIn: HeyReach conversations aren't scoped by a Bison campaign id, so the
    # campaign filter narrows only the email side — the LinkedIn inbox is always
    # refreshed (the classifier no-ops cleanly if HeyReach isn't configured).
    li_res = run_script([str(CLASSIFY_LI_REPLIES), "--lookback", str(lookback_days)], timeout=600)
    payload = review_queue_payload()
    payload["scan"] = res
    payload["li_scan"] = li_res
    payload["ok"] = res["returncode"] == 0 and li_res["returncode"] == 0
    return payload


def _load_sent():
    """Set of reply_ids whose follow-up has already been sent (so the card clears)."""
    data = _read_json(SENT_FOLLOWUPS) or {}
    return set((data.get("sent") or {}).keys())


# One writer at a time on the sent-followups ledger — approve/send and the
# section-move endpoint both mutate it from request threads.
SENT_LOCK = threading.Lock()


def _sent_records():
    return (_read_json(SENT_FOLLOWUPS) or {}).get("sent") or {}


def _mark_sent(reply_id, meta):
    with SENT_LOCK:
        data = _read_json(SENT_FOLLOWUPS) or {}
        data.setdefault("sent", {})[str(reply_id)] = meta
        _write_json_atomic(SENT_FOLLOWUPS, data)


def _patch_sent(reply_id, patch):
    """Merge a patch into one ledger record (a None value deletes that key).
    Returns the record, or None if the reply was never sent/parked."""
    with SENT_LOCK:
        data = _read_json(SENT_FOLLOWUPS) or {}
        rec = (data.get("sent") or {}).get(str(reply_id))
        if rec is None:
            return None
        for k, v in patch.items():
            if v is None:
                rec.pop(k, None)
            else:
                rec[k] = v
        _write_json_atomic(SENT_FOLLOWUPS, data)
        return rec


def _stamp_handled(items):
    sent = _load_sent()
    for it in items or []:
        it["handled"] = str(it.get("reply_id")) in sent
    return items


# ---- Durable per-lead console state --------------------------------------------
# Scans regenerate review_queue.json / li_review_queue.json from scratch, so any
# state the console itself creates (dismissed, tagged, reclassified, agent choice)
# lives in reply_state.json keyed by lead identity and is merged back at read time.
REPLY_STATE_LOCK = threading.Lock()


def _lead_key(item):
    """Stable per-lead identity across scans. reply_ids change with every new
    inbound reply, so per-lead state keys off the lead itself: the lead's email
    for the email channel, the HeyReach conversation id for LinkedIn."""
    if (item.get("channel") or "email") == "linkedin":
        cid = item.get("conversation_id") or item.get("reply_id")
        return f"li:{cid}" if cid else None
    email_addr = (item.get("lead_email") or item.get("from_email") or "").strip().lower()
    return email_addr or None


def _reply_state():
    data = _read_json(REPLY_STATE) or {}
    leads = data.get("leads")
    return {"leads": leads if isinstance(leads, dict) else {}}


def _reply_state_update_many(patches):
    """Merge {lead_key: patch} into the state in ONE locked read+atomic write
    (a None value in a patch deletes that field)."""
    with REPLY_STATE_LOCK:
        data = _reply_state()
        for key, patch in patches.items():
            cur = data["leads"].setdefault(key, {})
            for k, v in patch.items():
                if v is None:
                    cur.pop(k, None)
                else:
                    cur[k] = v
            if not cur:
                data["leads"].pop(key, None)
        REPLY_STATE.parent.mkdir(parents=True, exist_ok=True)
        tmp = REPLY_STATE.with_name(REPLY_STATE.name + ".tmp")
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False))
        os.replace(tmp, REPLY_STATE)
        return data


def _reply_state_update(key, patch):
    return _reply_state_update_many({key: patch})["leads"].get(key)


def _norm_ts(d):
    """Comparable timestamp key across the mixed formats the channels emit
    ('YYYY-MM-DD HH:MM:SS' vs ISO 'T'-separated)."""
    return (d or "").replace(" ", "T")[:19]


def _force_interested(item):
    """Apply the manual 'reclassified as interested' override to one item,
    keeping the model's verdict in classifier_original for audit."""
    cl = item.get("classifier") or {}
    if not cl.get("interested"):
        item.setdefault("classifier_original", dict(cl))
        cl.update({"interested": True,
                   "confidence": max(float(cl.get("confidence") or 0), 0.51),
                   "reason": "manually reclassified as interested"})
        item["classifier"] = cl
    item["already_interested"] = True
    return item


def _apply_reply_state(items):
    """Overlay per-lead console state onto queue items. Dismissal hides a lead only
    for the replies seen up to dismiss time — a NEWER reply (date_received past the
    snapshot) resurfaces them. A manual reclassify keeps the model's verdict in
    classifier_original for audit."""
    leads = _reply_state()["leads"]
    for it in items or []:
        key = _lead_key(it)
        if key:
            it["lead_key"] = key
        st = leads.get(key) if key else None
        if not st:
            continue
        if st.get("tagged_interested"):
            it["already_interested"] = True
        if st.get("agent"):
            it["agent"] = st["agent"]
        rec = st.get("reclassified")
        if rec and rec.get("interested"):
            _force_interested(it)   # a rescan re-runs the model — re-apply the override
            it["reclassified"] = rec
        dis = st.get("dismissed")
        if dis:
            # Hidden while it's the same reply that was dismissed, or an older one.
            # A dateless item (either side) is NOT assumed old — a new reply must
            # never stay buried just because its timestamp is missing.
            same_reply = str(it.get("reply_id")) == str(dis.get("reply_id") or "")
            d_recv, last = _norm_ts(it.get("date_received")), _norm_ts(dis.get("last_reply_at"))
            if same_reply or (d_recv and last and d_recv <= last):
                it["dismissed"] = dis
    return items


def _sent_lead_key(meta):
    """Lead identity for a sent-ledger record. New records store lead_key;
    legacy ones derive it (LinkedIn: the conversation id; email: to_email)."""
    if meta.get("lead_key"):
        return meta["lead_key"]
    channel = meta.get("channel") or ("linkedin" if meta.get("conversation_id") else "email")
    if channel == "linkedin":
        cid = meta.get("conversation_id")
        return f"li:{cid}" if cid else None
    to_email = (meta.get("to_email") or "").strip().lower()
    return to_email or None


def _stamp_sent_state(items):
    """Stamp handled / parked / post_followup from the sent-followups ledger.

    handled — the item's own reply has a live (non-resurfaced) send/park record;
        it sits in "Follow up" until the lead answers. Email reply_ids are
        per-message so membership is exact. LinkedIn reply_id IS the conversation
        id, so a strictly newer inbound message (date_received past the record's
        marker) un-handles the card — the lead replied since our send.
    parked — handled via a manual "Move to Follow up" record (nothing was sent).
    post_followup — not handled, and the lead replied AFTER our last send/park
        for that lead; these route to "Possible interested" for review — unless
        the SDR re-tagged/reclassified the lead since that send (re_engaged),
        which releases them back to the normal interested buckets.

    Timestamps: last_reply_at is channel-native clock (compare to date_received);
    sent_at/parked_at are server UTC (compare to reply_state's tagged_at /
    reclassified.at). Records with no comparable timestamps keep today's
    behavior: stay handled, never stamp post_followup.
    """
    sent = _sent_records()
    leads = _reply_state()["leads"]
    lead_marker = {}   # lead_key -> newest send marker (channel-native clock)
    lead_wall = {}     # lead_key -> newest send/park time (server UTC clock)
    for meta in sent.values():
        key = _sent_lead_key(meta)
        if not key:
            continue
        marker = _norm_ts(meta.get("last_reply_at") or meta.get("sent_at"))
        wall = _norm_ts(meta.get("sent_at") or meta.get("parked_at"))
        if marker:
            lead_marker[key] = max(lead_marker.get(key, ""), marker)
        if wall:
            lead_wall[key] = max(lead_wall.get(key, ""), wall)
    for it in items or []:
        meta = sent.get(str(it.get("reply_id")))
        d_recv = _norm_ts(it.get("date_received"))
        handled = False
        if meta and not meta.get("resurfaced"):
            if (it.get("channel") or "email") == "linkedin":
                marker = _norm_ts(meta.get("last_reply_at") or meta.get("sent_at"))
                handled = not (d_recv and marker and d_recv > marker)
            else:
                handled = True
        it["handled"] = handled
        it["parked"] = bool(handled and meta and meta.get("manual"))
        key = _lead_key(it)
        st = (leads.get(key) or {}) if key else {}
        re_engaged_at = max(_norm_ts(st.get("tagged_at")),
                            _norm_ts((st.get("reclassified") or {}).get("at")))
        # "~" sorts after any timestamp — a lead with no send record on the
        # server clock can never count as re-engaged.
        re_engaged = re_engaged_at > lead_wall.get(key or "", "~")
        it["post_followup"] = bool(
            not handled and key and lead_marker.get(key) and d_recv
            and d_recv > lead_marker[key] and not re_engaged)
    return items


def _attach_threads(items):
    """Attach a chronological `thread` to each item: the outbound sequence we sent,
    the prospect's reply, and any follow-ups sent from the console. Bison's
    sent-emails endpoint returns only sequence sends, so console follow-ups are
    merged from the drafts/sent records — matched by lead (lead_id / lead email /
    conversation id), never by reply_id, which changes with each inbound reply."""
    drafts = (_read_json(FOLLOWUP_DRAFTS) or {}).get("items") or []
    sent_meta = (_read_json(SENT_FOLLOWUPS) or {}).get("sent") or {}
    sent_drafts = [d for d in drafts
                   if d.get("status") == "sent" and (d.get("sent_message") or d.get("draft"))]
    # Index once — the match below would otherwise be O(items x sent_drafts) on
    # every queue fetch, which the UI re-issues after each action.
    by_lead, by_conv, by_email = {}, {}, {}
    for d in sent_drafts:
        if d.get("lead_id") is not None:
            by_lead.setdefault(d["lead_id"], []).append(d)
        if d.get("conversation_id"):
            by_conv.setdefault(d["conversation_id"], []).append(d)
        if d.get("from_email"):
            by_email.setdefault(d["from_email"].lower(), []).append(d)
    # Same-lead sibling replies (email reply_ids are per-message, so one lead can
    # have several queue items) — each item's thread shows the whole exchange.
    by_lead_inbound = {}
    for it in items or []:
        key = it.get("lead_key") or _lead_key(it)
        if key:
            by_lead_inbound.setdefault(key, []).append(it)
    # Follow-ups sent BEFORE a "Move to Interested" re-opened the draft live on
    # in the ledger's prior_sends — the draft record itself is back to "drafted".
    prior_by_lead = {}
    for meta in sent_meta.values():
        key = _sent_lead_key(meta)
        if key:
            for p in meta.get("prior_sends") or []:
                prior_by_lead.setdefault(key, []).append(p)

    for it in items or []:
        thread = []
        for m in reversed(it.get("sent_emails") or []):    # stored newest-first
            thread.append({"dir": "out", "kind": "sequence", "subject": m.get("subject"),
                           "date": m.get("date"),
                           "from": m.get("from_email") or it.get("sending_email"),
                           "text": m.get("text") or ""})
        thread.append({"dir": "in", "kind": "reply", "subject": it.get("subject"),
                       "date": it.get("date_received"),
                       "from": it.get("from_email") or it.get("from_name"),
                       "text": it.get("text_body") or ""})
        lead_key = it.get("lead_key") or _lead_key(it)
        for sib in (by_lead_inbound.get(lead_key) or []) if lead_key else []:
            if str(sib.get("reply_id")) == str(it.get("reply_id")):
                continue
            thread.append({"dir": "in", "kind": "reply", "subject": sib.get("subject"),
                           "date": sib.get("date_received"),
                           "from": sib.get("from_email") or sib.get("from_name"),
                           "text": sib.get("text_body") or ""})
        for p in (prior_by_lead.get(lead_key) or []) if lead_key else []:
            thread.append({"dir": "out", "kind": "followup",
                           "subject": f"Re: {p.get('subject')}" if p.get("subject") else None,
                           "date": p.get("sent_at"),
                           "from": it.get("sending_email"),
                           "agent": p.get("agent"),
                           "text": p.get("text") or ""})
        lead_id = it.get("lead_id")
        conv_id = it.get("conversation_id")
        lead_emails = {e for e in ((it.get("lead_email") or "").lower(),
                                   (it.get("from_email") or "").lower()) if e}
        matched, seen = [], set()
        for d in ((by_lead.get(lead_id) or []) if lead_id is not None else []) \
                + ((by_conv.get(conv_id) or []) if conv_id else []) \
                + [d for e in lead_emails for d in (by_email.get(e) or [])]:
            if id(d) not in seen:
                seen.add(id(d))
                matched.append(d)
        for d in matched:
            meta = sent_meta.get(str(d.get("reply_id"))) or {}
            thread.append({"dir": "out", "kind": "followup",
                           "subject": f"Re: {d.get('subject')}" if d.get("subject") else None,
                           "date": d.get("sent_at") or meta.get("sent_at"),
                           "from": it.get("sending_email"),
                           "agent": d.get("agent"),
                           "text": d.get("sent_message") or d.get("draft") or ""})
        thread.sort(key=lambda m: _norm_ts(m.get("date")))
        it["thread"] = thread
    return items


def review_queue_payload():
    """Unified review queue: email (Bison) + LinkedIn (HeyReach) replies in one
    list, each item carrying a `channel`. Counts are summed across channels.
    Per-lead console state and the merged conversation thread are attached at
    read time so they survive scans rewriting the queue files."""
    email = _read_json(REVIEW_QUEUE) or {}
    li = _read_json(LI_REVIEW_QUEUE) or {}
    if not email and not li:
        return {"available": False, "items": []}
    for it in (email.get("items") or []):
        it.setdefault("channel", "email")
    items = list(email.get("items") or []) + list(li.get("items") or [])
    _stamp_sent_state(items)
    _apply_reply_state(items)
    _attach_threads(items)
    # An item we already replied to / parked is superseded once a strictly newer
    # reply from the same lead is in the queue: the conversation continues in the
    # newer card (whose thread carries this one's inbound text via the sibling
    # merge), so the stale card is dropped instead of lingering in Follow up.
    sent = _sent_records()
    latest = {}
    for it in items:
        k = it.get("lead_key")
        if k:
            cand = (_norm_ts(it.get("date_received")), str(it.get("reply_id")))
            if cand > latest.get(k, ("", "")):
                latest[k] = cand

    def _superseded(it):
        k = it.get("lead_key")
        if not k or str(it.get("reply_id")) not in sent:
            return False
        newest = latest.get(k)
        return bool(newest and newest[1] != str(it.get("reply_id"))
                    and _norm_ts(it.get("date_received")) < newest[0])

    items = [it for it in items if not _superseded(it)]
    active = [it for it in items if not it.get("dismissed")]
    dismissed = [it for it in items if it.get("dismissed")]
    ec, lc = (email.get("counts") or {}), (li.get("counts") or {})
    counts = {
        "scanned": (ec.get("scanned") or 0) + (lc.get("scanned") or 0),
        "flagged": (ec.get("flagged") or 0) + (lc.get("flagged") or 0),
        "already": ec.get("already") or 0,
        "unsubscribed": ec.get("unsubscribed") or 0,
        "filtered": (ec.get("filtered") or 0) + (lc.get("filtered") or 0),
        "dismissed": len(dismissed),
        "email_scanned": ec.get("scanned") or 0,
        "linkedin_scanned": lc.get("scanned") or 0,
    }
    return {
        "available": True,
        "items": active,
        "dismissed": dismissed,
        "counts": counts,
        "scanned_at": email.get("scanned_at") or li.get("scanned_at"),
        "lookback_days": email.get("lookback_days") or li.get("lookback_days"),
        "linkedin": {"configured": bool(li.get("configured")), "error": li.get("error")},
        "hubspot_autosync": _autosync_status(),
    }


# Queue files are rewritten by request threads (reclassify/tag) and by the scan
# scripts; one writer at a time + atomic replace so readers never see torn JSON.
QUEUE_WRITE_LOCK = threading.Lock()


def _write_json_atomic(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    os.replace(tmp, path)


def _find_queue_item(reply_id):
    return next((it for it in _apply_reply_state(_stamp_sent_state(_merged_queue_items()))
                 if str(it.get("reply_id")) == str(reply_id)), None)


def do_dismiss_reply(reply_id, reason=None):
    """Clear a lead out of the queue without sending anything — e.g. the SDR already
    replied or booked them from the CRM. Keyed per lead and windowed to the replies
    seen so far, so the lead stays gone across rescans but a NEW reply resurfaces."""
    it = _find_queue_item(reply_id)
    if it is None:
        return {"ok": False, "error": f"no queue item {reply_id}"}, 404
    key = _lead_key(it)
    if not key:
        return {"ok": False, "error": "item has no lead identity to key dismissal on"}, 409
    _reply_state_update(key, {"dismissed": {
        "reason": (reason or "handled")[:120], "at": now_iso(),
        "reply_id": str(reply_id),               # covers items with no usable date
        "last_reply_at": it.get("date_received") or "",
    }})
    return {"ok": True, "reply_id": reply_id, "lead_key": key}, 200


def do_undismiss_reply(reply_id):
    it = _find_queue_item(reply_id)
    key = _lead_key(it) if it else None
    if not key:
        return {"ok": False, "error": f"no queue item {reply_id}"}, 404
    _reply_state_update(key, {"dismissed": None})
    return {"ok": True, "reply_id": reply_id, "lead_key": key}, 200


def do_reclassify_reply(reply_id):
    """Promote a misclassified 'other' reply to interested: enrich it on demand
    (non-interested items are never enriched at scan time), flip the classifier
    verdict in the queue file (so drafting picks it up), record the manual override
    per lead (so it survives rescans), then run the standard tag-interested flow."""
    item, channel = None, None
    for path, ch in ((REVIEW_QUEUE, "email"), (LI_REVIEW_QUEUE, "linkedin")):
        for it in (_read_json(path) or {}).get("items") or []:
            if str(it.get("reply_id")) == str(reply_id):
                it.setdefault("channel", ch)
                item, channel = it, ch
                break
        if item:
            break
    if item is None:
        return {"ok": False, "error": f"no queue item {reply_id}"}, 404
    # Enrich OUTSIDE the lock — it's a network round-trip.
    if channel == "email" and not item.get("enriched") and item.get("lead_id"):
        try:
            sys.path.insert(0, str(SCRIPTS / "email-bison" / "scripts"))
            from classify_replies import enrich_item  # noqa: E402
            enrich_item(_bison(), item)
        except Exception as e:  # noqa: BLE001 — enrichment is context, not a gate
            item["enrich_error"] = str(e)[:200]
    _force_interested(item)
    # Re-read + mutate + atomic-write under the lock, so a scan or another request
    # finishing in between can't be clobbered with our stale copy of the file.
    qpath = REVIEW_QUEUE if channel == "email" else LI_REVIEW_QUEUE
    with QUEUE_WRITE_LOCK:
        queue = _read_json(qpath) or {}
        for fresh in queue.get("items") or []:
            if str(fresh.get("reply_id")) == str(reply_id):
                fresh.update(item)
                _write_json_atomic(qpath, queue)
                break
    key = _lead_key(item)
    if key:
        _reply_state_update(key, {"reclassified": {"interested": True, "at": now_iso()},
                                  "dismissed": None})
    tag = do_tag_replies([reply_id])
    return {"ok": bool(tag.get("ok")), "item": item, "tag": tag}, 200


def do_move_reply(reply_id, to):
    """Manual section moves.

    to='interested' — un-park a Follow up item back to Interested (draft again):
    the ledger record is kept but flagged resurfaced, the sent draft re-opens
    (status back to 'drafted', which draft_followups.py otherwise refuses to
    touch), the sent text is preserved in the record's prior_sends so the thread
    keeps showing it, then the standard reclassify flow force-interests the item.
    A later Approve & send overwrites the ledger meta, clearing the flag.

    to='followup' — park a lead in Follow up (used from Dismissed: "I'm on it /
    handled outside the console"). Manual parks carry parked_at and never
    sent_at, so hubspot_activity_sync's our-replies pass ignores them."""
    it = _find_queue_item(reply_id)
    if it is None:
        return {"ok": False, "error": f"no queue item {reply_id}"}, 404
    key = _lead_key(it)
    if not key:
        return {"ok": False, "error": "item has no lead identity"}, 409

    if to == "interested":
        rec = _sent_records().get(str(reply_id))
        if rec is None:
            return {"ok": False, "error": "nothing was sent or parked for this reply"}, 409
        patch = {"resurfaced": True, "resurfaced_at": now_iso()}
        drafts = _read_json(FOLLOWUP_DRAFTS) or {"items": []}
        d = next((x for x in drafts.get("items") or []
                  if str(x.get("reply_id")) == str(reply_id)), None)
        if d and d.get("status") == "sent":
            prior = {"text": d.get("sent_message") or d.get("draft") or "",
                     "sent_at": d.get("sent_at"), "agent": d.get("agent"),
                     "subject": d.get("subject")}
            if prior["text"]:
                patch["prior_sends"] = (rec.get("prior_sends") or []) + [prior]
            d["status"] = "drafted"
            _write_json_atomic(FOLLOWUP_DRAFTS, drafts)
        _patch_sent(reply_id, patch)
        payload, code = do_reclassify_reply(reply_id)
        payload["moved"] = "interested"
        return payload, code

    # to == "followup"
    rec = _sent_records().get(str(reply_id))
    if rec is not None:
        _patch_sent(reply_id, {"resurfaced": None, "resurfaced_at": None, "lead_key": key,
                               "last_reply_at": it.get("date_received")
                                                or rec.get("last_reply_at") or ""})
    else:
        meta = {"manual": True, "parked_at": now_iso(),
                "channel": it.get("channel") or "email",
                "lead_key": key, "last_reply_at": it.get("date_received") or ""}
        if (it.get("channel") or "email") == "linkedin":
            meta["conversation_id"] = it.get("conversation_id") or it.get("reply_id")
        else:
            meta["to_email"] = (it.get("from_email") or it.get("lead_email") or "").strip().lower()
        _mark_sent(reply_id, meta)
    _reply_state_update(key, {"dismissed": None})
    return {"ok": True, "reply_id": reply_id, "lead_key": key, "moved": "followup"}, 200


def do_tag_replies(reply_ids):
    """Mark replies interested. Email (Bison): mark-as-interested + attach the
    Interested tag to the lead. LinkedIn: there's no Bison lead, so 'interested'
    is a local state flip in the LinkedIn queue that advances the card to draft —
    HeyReach interested replies otherwise skip straight past this step."""
    email_q = _read_json(REVIEW_QUEUE) or {}
    li_q = _read_json(LI_REVIEW_QUEUE) or {}
    email_by_id = {str(it.get("reply_id")): it for it in email_q.get("items", [])}
    li_by_id = {str(it.get("reply_id")): it for it in li_q.get("items", [])}
    tag_id = interested_tag_id()
    bison = None
    results, tagged, failed = [], 0, 0
    tagged_ids, state_patches = set(), {}
    for rid in reply_ids:
        li_item = li_by_id.get(str(rid))
        if li_item is not None:                          # LinkedIn — local flip only
            key = _lead_key(li_item)
            if key:  # durable: the queue file is rewritten on every rescan
                state_patches[key] = {"tagged_interested": True, "tagged_at": now_iso()}
            tagged_ids.add(str(rid))
            results.append({"reply_id": rid, "channel": "linkedin", "ok": True})
            tagged += 1
            continue
        item = email_by_id.get(str(rid), {})
        lead_id = item.get("lead_id")
        try:
            bison = bison or _bison()
            bison.mark_reply_interested(rid)
            if lead_id:
                bison.attach_tags_to_leads([tag_id], [lead_id])
            results.append({"reply_id": rid, "lead_id": lead_id, "ok": True})
            tagged += 1
            tagged_ids.add(str(rid))
            key = _lead_key(item)
            if key:
                state_patches[key] = {"tagged_interested": True, "tagged_at": now_iso()}
        except Exception as e:  # noqa: BLE001 - one bad reply must not abort the rest
            results.append({"reply_id": rid, "lead_id": lead_id, "ok": False, "error": str(e)[:200]})
            failed += 1
    if state_patches:
        _reply_state_update_many(state_patches)
    if tagged_ids:
        # Re-read + atomic-write under the lock (the Bison calls above take long
        # enough for a scan to have rewritten the file since we read it).
        with QUEUE_WRITE_LOCK:
            for qpath in (REVIEW_QUEUE, LI_REVIEW_QUEUE):
                queue = _read_json(qpath) or {}
                changed = False
                for it in queue.get("items") or []:
                    if str(it.get("reply_id")) in tagged_ids:
                        it["already_interested"] = True
                        changed = True
                if changed:
                    _write_json_atomic(qpath, queue)
    return {"ok": failed == 0, "tagged": tagged, "failed": failed, "results": results}


# ---- Interested follow-up: draft -> approve (push to campaign + send) ----------
def do_draft_followups():
    """Generate follow-up drafts for the interested replies in the review queue."""
    res = run_script([str(DRAFT_FOLLOWUPS)], timeout=600)
    payload = _read_json(FOLLOWUP_DRAFTS) or {"items": []}
    payload["ok"] = res["returncode"] == 0
    payload["scan"] = res
    return payload


def followup_drafts_payload():
    payload = _read_json(FOLLOWUP_DRAFTS) or {"items": []}
    payload["available"] = FOLLOWUP_DRAFTS.is_file()
    _stamp_handled(payload.get("items"))
    return payload


def _mark_draft_sent(drafts, draft, body_text):
    """Record a sent draft in followup_drafts.json (shared by both channels)."""
    if draft:
        draft["status"] = "sent"
        draft["sent_at"] = now_iso()
        draft["sent_message"] = body_text
        FOLLOWUP_DRAFTS.write_text(json.dumps(drafts, indent=2, ensure_ascii=False))


def do_approve_followup(reply_id, message):
    """Approve a drafted follow-up and send the (edited) reply in the prospect's
    own thread, then mark it handled so the card clears. The enriched review-queue
    item is the source of truth for the recipient + sender; the draft supplies the
    body. Email replies send in the Bison thread; LinkedIn replies send via
    HeyReach (SendMessage) from the right LinkedIn sender to the right person."""
    qitem = next((it for it in _merged_queue_items() if str(it.get("reply_id")) == str(reply_id)), {})
    drafts = _read_json(FOLLOWUP_DRAFTS) or {"items": []}
    draft = next((d for d in drafts.get("items", []) if str(d.get("reply_id")) == str(reply_id)), {})
    src = qitem or draft
    channel = src.get("channel") or draft.get("channel") or "email"
    body_text = message or draft.get("draft") or ""
    if not body_text.strip():
        return {"ok": False, "error": "no follow-up draft to send — click Draft follow-up first"}, 409

    if channel == "linkedin":
        conv_id = src.get("conversation_id") or src.get("reply_id") or draft.get("conversation_id")
        acct_id = src.get("linkedin_account_id") or draft.get("linkedin_account_id")
        if not (conv_id and acct_id):
            return {"ok": False, "error": "missing conversation or LinkedIn sender for this reply"}, 409
        try:
            hr = _heyreach()
            hr.send_message(conv_id, acct_id, body_text)
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": str(e)[:300]}, 502
        try:  # best-effort: mark the conversation seen; never fail a sent message on this
            hr.set_seen(conv_id, acct_id, True)
        except Exception:  # noqa: BLE001
            pass
        _mark_sent(reply_id, {"sent_at": now_iso(), "channel": "linkedin",
                              "conversation_id": conv_id, "linkedin_account_id": acct_id,
                              "lead_key": _lead_key(src),
                              "last_reply_at": src.get("date_received") or ""})
        _mark_draft_sent(drafts, draft, body_text)
        return {"ok": True, "reply_id": reply_id, "channel": "linkedin",
                "to_name": src.get("from_name")}, 200

    # ---- email (Bison) -------------------------------------------------------
    lead_id = src.get("lead_id")
    to_email = src.get("from_email") or src.get("lead_email")
    sender_email_id = src.get("sender_email_id")
    if not (to_email and sender_email_id):
        return {"ok": False, "error": "missing recipient or sending inbox for this reply"}, 409
    bison = _bison()
    body_html = body_text.replace("\n", "<br>")
    try:
        if lead_id:  # idempotent — covers approve on an item tagged out-of-band
            bison.mark_reply_interested(reply_id)
            bison.attach_tags_to_leads([interested_tag_id()], [lead_id])
        bison.send_reply(reply_id, body_html, sender_email_id,
                         [{"email_address": to_email, "name": src.get("from_name")}],
                         content_type="html", inject_previous=True)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)[:300]}, 502
    _mark_sent(reply_id, {"sent_at": now_iso(), "channel": "email", "to_email": to_email,
                          "sender_email_id": sender_email_id, "lead_key": _lead_key(src),
                          "last_reply_at": src.get("date_received") or ""})
    _mark_draft_sent(drafts, draft, body_text)
    return {"ok": True, "reply_id": reply_id, "channel": "email", "to_email": to_email}, 200


# ----------------------------------------------------------------------------
# Reply agents: registry + per-lead selection + regenerate. The standard agent
# drafts synchronously via draft_followups.py; the signal-playbook agent runs the
# async build job below and its draft lands in the same followup_drafts.json.
# ----------------------------------------------------------------------------
def _reply_agents_mod():
    p = str(SCRIPTS / "email-bison" / "scripts")
    if p not in sys.path:   # called per request — don't grow sys.path unboundedly
        sys.path.insert(0, p)
    import reply_agents  # noqa: E402
    return reply_agents


def reply_agents_payload():
    ra = _reply_agents_mod()
    return {"ok": True, "agents": ra.AGENTS, "default": ra.DEFAULT_AGENT}


def do_set_reply_agent(reply_id, agent):
    ra = _reply_agents_mod()
    if not ra.get(agent):
        return {"ok": False, "error": f"unknown agent {agent!r}"}, 400
    it = _find_queue_item(reply_id)
    if it is None:
        return {"ok": False, "error": f"no queue item {reply_id}"}, 404
    key = _lead_key(it)
    if not key:
        return {"ok": False, "error": "item has no lead identity"}, 409
    # the default agent is represented as no stored state
    _reply_state_update(key, {"agent": None if agent == ra.DEFAULT_AGENT else agent})
    return {"ok": True, "reply_id": reply_id, "agent": agent}, 200


def do_regenerate_followup(reply_id, agent=None, company_domain=None):
    """Re-draft one follow-up with the chosen agent. Standard = synchronous; the
    signal-playbook agent kicks off the async build job and the UI polls its
    status until the draft lands in followup_drafts.json. `company_domain` is an
    optional hand-typed override for leads whose account can't be auto-resolved."""
    it = _find_queue_item(reply_id)
    if it is None:
        return {"ok": False, "error": f"no queue item {reply_id}"}, 404
    if it.get("handled"):   # never clobber the record of an already-sent follow-up
        return {"ok": False, "error": "a follow-up was already sent for this reply"}, 409
    ra = _reply_agents_mod()
    agent = agent or it.get("agent") or ra.DEFAULT_AGENT
    spec = ra.get(agent)
    if not spec:
        return {"ok": False, "error": f"unknown agent {agent!r}"}, 400
    if spec.get("kind") == "pipeline":   # registry-driven: async build agents
        return start_play_job(reply_id, company_domain=company_domain)
    res = run_script([str(DRAFT_FOLLOWUPS), "--reply-id", str(reply_id)], timeout=600)
    drafts = _read_json(FOLLOWUP_DRAFTS) or {"items": []}
    draft = next((d for d in drafts.get("items", [])
                  if str(d.get("reply_id")) == str(reply_id)), None)
    # exit code 0 + a record is not success: draft_one records API errors in-place
    if (res["returncode"] != 0 or not draft or draft.get("error")
            or not (draft.get("draft") or "").strip()):
        err = ((draft or {}).get("error")
               or (res["stderr"] or res["stdout"]).strip()[-500:] or "draft failed")
        return {"ok": False, "error": err}, 502
    return {"ok": True, "async": False, "agent": agent, "draft": draft}, 200


# ---- Signal Playbook build job (async; clones the SOURCE_JOBS pattern) ----------
PLAY_JOBS = {}
PLAY_LOCK = threading.Lock()   # guards the one-build-per-lead check-then-insert
_PLAY_SEQ = [0]
PLAY_STAGES = ["research", "deck-data", "render", "publish", "draft"]


def _new_play_job_id():
    with JOB_LOCK:
        _PLAY_SEQ[0] += 1
        return f"play-{_PLAY_SEQ[0]}"


def _normalize_domain(raw):
    """Best-effort clean a user-typed or derived value into a bare host — tolerant
    of a full URL ('https://www.IBM.com/careers'), a 'www.' prefix, or even a whole
    email ('ronnie@ibm.com') -> 'ibm.com'. Returns '' if nothing domain-shaped."""
    d = (raw or "").strip().lower()
    if not d:
        return ""
    d = re.sub(r"^[a-z][a-z0-9+.-]*://", "", d)   # strip scheme
    d = d.split("/", 1)[0].split("?", 1)[0]        # strip path / query
    d = d.rsplit("@", 1)[-1]                        # tolerate a full email address
    if d.startswith("www."):
        d = d[4:]
    d = d.strip(".")
    return d if ("." in d and " " not in d) else ""


def _play_contact_inputs(item, override_domain=None):
    """Resolve the contact profile the play pipeline needs from the queue item.
    Domain resolution, most trusted first: a caller-supplied override (the user
    typed it in), the contacts table matched by email, the lead's own email
    domain, then — for email-less leads like LinkedIn — any contact we've already
    pulled at the same company. `_normalize_domain` cleans whatever we land on."""
    email_addr = (item.get("lead_email") or item.get("from_email") or "").strip().lower()
    parts = (item.get("from_name") or "").split()
    first = item.get("first_name") or (parts[0] if parts else "")
    last = item.get("last_name") or (" ".join(parts[1:]) if len(parts) > 1 else "")
    company = item.get("company")
    domain = _normalize_domain(override_domain)   # a user-supplied domain always wins
    if not domain and email_addr and "@" in email_addr:
        try:
            conn = db_connect()
            row = conn.execute(
                "SELECT company, domain FROM contacts WHERE lower(email)=? LIMIT 1",
                (email_addr,)).fetchone()
            conn.close()
            if row:
                company = company or row["company"]
                domain = row["domain"]
        except sqlite3.Error:
            pass
        domain = domain or email_addr.rsplit("@", 1)[-1]
    # No email to key off (typical for LinkedIn leads) — recover the account's
    # domain from any contact we've already pulled at the same company.
    if not domain and company:
        try:
            conn = db_connect()
            row = conn.execute(
                "SELECT domain FROM contacts WHERE lower(company)=? "
                "AND domain IS NOT NULL AND domain != '' LIMIT 1",
                (company.strip().lower(),)).fetchone()
            conn.close()
            if row:
                domain = row["domain"]
        except sqlite3.Error:
            pass
    domain = _normalize_domain(domain)
    return {"firstName": first, "lastName": last, "jobTitle": item.get("title") or "",
            "businessEmail": email_addr, "companyName": company or (domain or "").split(".")[0],
            "companyDomain": domain or ""}


def start_play_job(reply_id, company_domain=None):
    it = _find_queue_item(reply_id)
    if it is None:
        return {"ok": False, "error": f"no queue item {reply_id}"}, 404
    if not BUILD_PLAY.is_file():
        return {"ok": False, "error": "signal-playbook skill not installed"}, 501
    key = _lead_key(it) or str(reply_id)
    # Check-then-insert must be atomic or two concurrent regenerates for the same
    # lead both pass the check and spawn two full build pipelines.
    with PLAY_LOCK:
        running = next((j for j in PLAY_JOBS.values()
                        if j["lead_key"] == key and j["status"] == "running"), None)
        if running:  # one build per lead at a time — return the in-flight job
            return {"ok": True, "async": True, "agent": "signal-playbook",
                    "job_id": running["job_id"], "existing": True}, 200
        contact = _play_contact_inputs(it, override_domain=company_domain)
        if not contact.get("companyDomain"):
            # need_domain lets the UI prompt for a hand-typed domain instead of just
            # surfacing a dead-end error (leads with no email + no known account).
            # Status 200 (NOT 4xx) is load-bearing: the frontend fetch wrapper throws
            # on any non-2xx and drops the JSON body, so a 409 would strip need_domain
            # and the UI would fall through to a generic error banner. This is a soft
            # "need more input" result, so 200 with ok:false is the right shape here.
            return {"ok": False, "need_domain": True,
                    "error": "could not resolve a company domain for this lead"}, 200
        job_id = _new_play_job_id()
        PLAY_JOBS[job_id] = {
            "job_id": job_id, "reply_id": reply_id, "lead_key": key, "status": "running",
            "agent": "signal-playbook", "contact": contact,
            "stage": "research", "pct": 2, "stages": PLAY_STAGES, "log": [],
            "play": None, "draft": None, "fallback": None, "error": None,
            "started_at": now_iso(), "finished_at": None,
        }
    threading.Thread(target=_run_play_job, args=(job_id,), daemon=True).start()
    return {"ok": True, "async": True, "agent": "signal-playbook", "job_id": job_id}, 200


def _play_progress_cb(job):
    """build_play.py announces each stage as 'stage: <name>' on stderr; everything
    else is free-text progress kept for the job's log tail."""
    order = {s: i for i, s in enumerate(PLAY_STAGES)}
    def on_line(line):
        job["log"] = (job["log"] + [line[:300]])[-40:]
        m = re.match(r"stage:\s*([a-z-]+)", line.strip())
        if m and m.group(1) in order:
            job["stage"] = m.group(1)
            job["pct"] = min(95, int(order[m.group(1)] / len(PLAY_STAGES) * 100) + 5)
    return on_line


def _run_play_job(job_id):
    job = PLAY_JOBS[job_id]
    try:
        args = [str(BUILD_PLAY), "--reply-id", str(job["reply_id"]),
                "--contact-json", json.dumps(job["contact"])]
        res = run_script_streaming(args, _play_progress_cb(job), timeout=3600)
        out = {}
        try:  # progress goes to stderr; the JSON result is stdout's last line
            last = [ln for ln in (res.get("stdout") or "").splitlines() if ln.strip()][-1]
            out = json.loads(last)
        except (json.JSONDecodeError, IndexError):
            pass
        if not out:
            job["status"] = "error"
            job["error"] = (res.get("stderr") or "").strip()[-500:] or "signal play build failed"
            return
        job["play"] = out.get("play")
        job["draft"] = out.get("draft")
        job["fallback"] = out.get("fallback")
        job["error"] = out.get("error")
        job["status"] = "done" if (out.get("ok") or out.get("draft")) else "error"
        if job["status"] == "done":
            job["stage"], job["pct"] = "done", 100
    except Exception as e:  # noqa: BLE001
        job["status"] = "error"
        job["error"] = f"{type(e).__name__}: {e}"
    finally:
        job["finished_at"] = now_iso()


def _play_job_public(job):
    if not job:
        return None
    return {k: v for k, v in job.items() if k != "contact"}


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


def _tech_status():
    """(available, reason) for technographic detection. Mirrors the Mongo
    'configured' degrade: import lazily, never raise, never block boot — the
    server must come up without dnspython installed."""
    try:
        import tech_signals as T  # noqa: E402  (PIPELINE_SCRIPTS is on sys.path)
        return T.tech_available()
    except Exception as e:  # noqa: BLE001
        return False, f"tech_signals unavailable: {e}"


def _hiring_status():
    """(available, reason) for hiring detection. Same degrade contract as
    _tech_status — without PROSPEO_API_KEY the feature reports unavailable and
    the server keeps running."""
    try:
        import hiring_signals as H  # noqa: E402  (PIPELINE_SCRIPTS is on sys.path)
        return H.hiring_available()
    except Exception as e:  # noqa: BLE001
        return False, f"hiring_signals unavailable: {e}"


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
        r.pop("tech_detail", None)  # structured detections stay in the DB — heavy for a list
        r["tech_age_days"] = _age_days(r.get("tech_checked_at"))
        r["has_tech"] = bool(r.get("tech_signals"))
        r.pop("hiring_detail", None)  # full title lists stay in the DB — heavy for a list
        r["hiring_age_days"] = _age_days(r.get("hiring_checked_at"))
        r["has_hiring"] = bool(r.get("hiring_signals"))
    available, reason = _tech_status()
    h_available, h_reason = _hiring_status()
    return {"signals": rows, "count": len(rows),
            "tech_available": available, "tech_reason": reason,
            "hiring_available": h_available, "hiring_reason": h_reason}


def signals_detail(domain):
    """One domain's full cache row for the Signals drawer: the untruncated signal
    + parsed tech_detail (per-vendor detections, evidence, scan metadata, HubSpot
    write-back outcome) + the contacts that reuse this row. Read-only; degrades
    gracefully, never 500."""
    domain = (domain or "").strip().lower()
    if not domain:
        return {"ok": False, "error": "domain required", "signal": None}
    row, contacts = None, []
    with db_connect() as conn:
        try:
            r = conn.execute("SELECT * FROM account_signals WHERE domain=?", (domain,)).fetchone()
            row = dict(r) if r else None
        except sqlite3.Error:
            row = None
        try:
            contacts = [dict(c) for c in conn.execute(
                "SELECT contact_id, first_name, last_name, title, persona, status, batch_id "
                "FROM contacts WHERE domain=? ORDER BY status, last_name, first_name", (domain,))]
        except sqlite3.Error:
            contacts = []
    if row is None:
        return {"ok": False, "error": f"no signal cached for {domain}", "signal": None,
                "domain": domain, "contacts": contacts}
    row["age_days"] = _age_days(row.get("researched_at"))
    row["fresh"] = row["age_days"] is not None and row["age_days"] < 90
    row["tech_age_days"] = _age_days(row.get("tech_checked_at"))
    row["has_tech"] = bool(row.get("tech_signals"))
    try:
        row["tech_detail"] = json.loads(row.get("tech_detail") or "null")
    except (ValueError, TypeError):
        row["tech_detail"] = None
    row["hiring_age_days"] = _age_days(row.get("hiring_checked_at"))
    row["has_hiring"] = bool(row.get("hiring_signals"))
    try:
        row["hiring_detail"] = json.loads(row.get("hiring_detail") or "null")
    except (ValueError, TypeError):
        row["hiring_detail"] = None
    available, reason = _tech_status()
    h_available, h_reason = _hiring_status()
    return {"ok": True, "domain": domain, "signal": row, "contacts": contacts,
            "tech_available": available, "tech_reason": reason,
            "hiring_available": h_available, "hiring_reason": h_reason}


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


def do_detect_tech(domain, force=False):
    """Technographic scan for one domain (per-row UI button). In-process like
    do_refresh_signal — tech_signals writes via batch_db's own read-write
    connection. Returns (payload, status)."""
    domain = (domain or "").strip().lower()
    if not domain:
        return {"ok": False, "error": "domain required"}, 400
    available, reason = _tech_status()
    if not available:
        return {"ok": False, "error": f"technographic detection unavailable: {reason}"}, 501
    company = None
    with db_connect() as conn:
        try:
            row = conn.execute(
                "SELECT company FROM contacts WHERE domain=? AND company IS NOT NULL AND company!='' LIMIT 1",
                (domain,)).fetchone()
            company = row["company"] if row else None
        except sqlite3.Error:
            company = None
    import tech_signals as T  # noqa: E402
    try:
        res = T.detect_and_store(domain, company=company, force=force)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)[:300]}, 502
    payload = signals_payload()
    payload["ok"] = True
    payload["detected"] = res
    return payload, 200


# ---- bulk technographic backfill (async in-process job; one at a time) ----------
TECH_JOBS = {}
TECH_LOCK = threading.Lock()   # guards the one-running-job check-then-insert
_TECH_SEQ = [0]


def start_tech_backfill(limit=None, stale_days=None, force=False):
    """Scan every account_signals domain with no tech scan yet (the UI 'Detect
    missing' button / prod backfill). Returns (payload, status)."""
    available, reason = _tech_status()
    if not available:
        return {"ok": False, "error": f"technographic detection unavailable: {reason}"}, 501
    import tech_signals as T  # noqa: E402
    with TECH_LOCK:
        if any(j["status"] == "running" for j in TECH_JOBS.values()):
            return {"ok": False, "error": "a tech backfill is already running"}, 409
        _TECH_SEQ[0] += 1
        job_id = f"tech-{_TECH_SEQ[0]}"
        job = {"job_id": job_id, "status": "running", "total": 0, "done": 0,
               "detected": 0, "skipped": 0, "errors": 0, "hubspot_ok": 0,
               "hubspot_missing": 0, "current": None, "log": [], "error": None,
               "started_at": now_iso(), "finished_at": None}
        TECH_JOBS[job_id] = job
    # queue size up front so the UI can show progress before the first result
    with db_connect() as conn:
        try:
            job["total"] = conn.execute(
                "SELECT COUNT(*) FROM account_signals WHERE tech_checked_at IS NULL").fetchone()[0]
        except sqlite3.Error:
            pass

    def _progress(done, total, domain, res):
        job["done"], job["total"], job["current"] = done, total, domain
        status = ("error" if (res.get("error_exc") or res.get("tech_error"))
                  else "skip" if res.get("skipped") else "ok")
        job["log"] = (job["log"] + [f"{domain}: {status}"])[-40:]

    def _run():
        try:
            summary = T.backfill(stale_days=stale_days, limit=limit, force=force,
                                 workers=3, progress=_progress)
            job.update(summary)  # total/detected/skipped/errors/hubspot_ok/hubspot_missing
            job["status"] = "done"
        except Exception as e:  # noqa: BLE001
            job["status"] = "error"
            job["error"] = str(e)[:300]
        finally:
            job["current"] = None
            job["finished_at"] = now_iso()

    threading.Thread(target=_run, daemon=True).start()
    return {"ok": True, "job_id": job_id, "total": job["total"]}, 200


def do_detect_hiring(domain, force=False):
    """Hiring scan for one domain (Signals drawer button). In-process like
    do_detect_tech — hiring_signals writes via batch_db's own read-write
    connection. Returns (payload, status)."""
    domain = (domain or "").strip().lower()
    if not domain:
        return {"ok": False, "error": "domain required"}, 400
    available, reason = _hiring_status()
    if not available:
        return {"ok": False, "error": f"hiring detection unavailable: {reason}"}, 501
    company = None
    with db_connect() as conn:
        try:
            row = conn.execute(
                "SELECT company FROM contacts WHERE domain=? AND company IS NOT NULL AND company!='' LIMIT 1",
                (domain,)).fetchone()
            company = row["company"] if row else None
        except sqlite3.Error:
            company = None
    import hiring_signals as H  # noqa: E402
    try:
        res = H.detect_and_store(domain, company=company, force=force)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)[:300]}, 502
    payload = signals_payload()
    payload["ok"] = True
    payload["detected"] = res
    return payload, 200


# ---- bulk hiring backfill (async in-process job; one at a time) -----------------
HIRING_JOBS = {}
HIRING_LOCK = threading.Lock()   # guards the one-running-job check-then-insert
_HIRING_SEQ = [0]


def start_hiring_backfill(limit=None, stale_days=None, force=False):
    """Scan every account_signals domain with no hiring scan yet (the UI 'Detect
    hiring' button / prod backfill). Independent of the tech job registry — the
    two backfills may run side by side. Every non-skipped scan costs a Prospeo
    credit, so prefer a `limit` on first runs. Returns (payload, status)."""
    available, reason = _hiring_status()
    if not available:
        return {"ok": False, "error": f"hiring detection unavailable: {reason}"}, 501
    import hiring_signals as H  # noqa: E402
    with HIRING_LOCK:
        if any(j["status"] == "running" for j in HIRING_JOBS.values()):
            return {"ok": False, "error": "a hiring backfill is already running"}, 409
        _HIRING_SEQ[0] += 1
        job_id = f"hiring-{_HIRING_SEQ[0]}"
        job = {"job_id": job_id, "status": "running", "total": 0, "done": 0,
               "detected": 0, "skipped": 0, "errors": 0, "hubspot_ok": 0,
               "hubspot_missing": 0, "current": None, "log": [], "error": None,
               "started_at": now_iso(), "finished_at": None}
        HIRING_JOBS[job_id] = job
    # queue size up front so the UI can show progress before the first result
    with db_connect() as conn:
        try:
            job["total"] = conn.execute(
                "SELECT COUNT(*) FROM account_signals WHERE hiring_checked_at IS NULL").fetchone()[0]
        except sqlite3.Error:
            pass

    def _progress(done, total, domain, res):
        job["done"], job["total"], job["current"] = done, total, domain
        status = ("error" if (res.get("error_exc") or res.get("hiring_error"))
                  else "skip" if res.get("skipped") else "ok")
        job["log"] = (job["log"] + [f"{domain}: {status}"])[-40:]

    def _run():
        try:
            summary = H.backfill(stale_days=stale_days, limit=limit, force=force,
                                 workers=3, progress=_progress)
            job.update(summary)  # total/detected/skipped/errors/hubspot_ok/hubspot_missing
            job["status"] = "done"
        except Exception as e:  # noqa: BLE001
            job["status"] = "error"
            job["error"] = str(e)[:300]
        finally:
            job["current"] = None
            job["finished_at"] = now_iso()

    threading.Thread(target=_run, daemon=True).start()
    return {"ok": True, "job_id": job_id, "total": job["total"]}, 200


# ----------------------------------------------------------------------------
# A/B by instruction variant + "show the product" sample fulfillment.
# ----------------------------------------------------------------------------
SAMPLES_DIR = DATA / "outreach" / "samples"


def _interested_emails():
    """Lowercased lead emails marked interested (fetched dataset + approved review queue)."""
    emails = set()
    for row in read_jsonl(DATA / "interested-replies" / "dataset.jsonl"):
        e = ((row.get("lead") or {}).get("email") or "").strip().lower()
        if e:
            emails.add(e)
    for it in _merged_queue_items():
        if it.get("already_interested") or (it.get("classifier") or {}).get("interested"):
            e = (it.get("from_email") or "").strip().lower()
            if e:
                emails.add(e)
    return emails


def variant_breakdown():
    """Interested rate per instruction variant. NULL/'' variant counts as the value-give baseline."""
    interested = _interested_emails()
    with db_connect() as conn:
        rows = conn.execute(
            "SELECT COALESCE(NULLIF(variant,''),'value-give') v, status, email FROM contacts").fetchall()
    agg = {}
    for r in rows:
        a = agg.setdefault(r["v"], {"total": 0, "enrolled": 0, "interested": 0})
        a["total"] += 1
        if r["status"] == "enrolled":
            a["enrolled"] += 1
        if (r["email"] or "").strip().lower() in interested:
            a["interested"] += 1
    order = ["value-give", "earn", "show"]
    out = []
    for v in order + [k for k in agg if k not in order]:
        if v not in agg:
            continue
        a = agg[v]
        den = a["enrolled"] or a["total"]
        out.append({"variant": v, **a,
                    "interested_rate_pct": round(100 * a["interested"] / den, 3) if den else None})
    return {"variants": out, "interested_total": len(interested)}


def do_generate_samples(company=None, domain=None, from_email=None):
    """Draft 3 sample outbound emails our AI would write for a lead's company (show-arm demo)."""
    if from_email and not company:
        with db_connect() as conn:
            r = conn.execute("SELECT company, domain FROM contacts WHERE lower(email)=? LIMIT 1",
                             ((from_email or "").strip().lower(),)).fetchone()
        if r:
            company = company or r["company"]
            domain = domain or r["domain"]
        if not domain and "@" in (from_email or ""):
            domain = from_email.split("@")[-1].lower()
    if not (company or domain):
        return {"ok": False, "error": "company, domain, or from_email required"}
    G = _gen_mod()
    result = G.generate_samples(company or domain, domain or "")
    SAMPLES_DIR.mkdir(parents=True, exist_ok=True)
    key = re.sub(r"[^a-z0-9]+", "-", (company or domain).lower()).strip("-")[:50] or "company"
    result["ok"] = True
    result["generated_at"] = now_iso()
    (SAMPLES_DIR / f"{key}.json").write_text(json.dumps(result, indent=2))
    return result


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
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")

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

    def _public_base(self):
        """The scheme+host the browser used to reach us, honoring the reverse
        proxy's X-Forwarded-* headers (Railway terminates TLS upstream, so the
        socket is plain HTTP but the public URL is HTTPS). Returns e.g.
        'https://sdr-console.up.railway.app' or 'http://localhost:8787', or None
        if no host can be determined. Used to build the Clay OAuth redirect so it
        always points back to wherever the console is actually served."""
        h = self.headers
        proto = (h.get("X-Forwarded-Proto") or "").split(",")[0].strip() or "http"
        host = (h.get("X-Forwarded-Host") or h.get("Host") or "").split(",")[0].strip()
        return f"{proto}://{host}" if host else None

    def _read_body(self):
        length = int(self.headers.get("Content-Length", 0) or 0)
        if not length:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return {}

    def _html(self, body, code=200):
        raw = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _clay_callback_html(self, code, state, err):
        """Finish the Clay OAuth flow and render a tiny close-this-tab page."""
        if err:
            return self._html(f"<h2>Clay authorization failed</h2><p>{err}</p>", code=400)
        try:
            _clay_oauth().handle_callback(code, state)
            return self._html(
                "<h2>Clay connected ✓</h2><p>You can close this tab and return to the SDR Console.</p>")
        except Exception as e:  # noqa: BLE001
            return self._html(f"<h2>Clay authorization failed</h2><p>{type(e).__name__}: {e}</p>", code=400)

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    # -- routing ----------------------------------------------------------
    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        params = parse_qs(parsed.query)
        # Auth gate: every /api/* read except the exempt few needs a valid token.
        # Non-/api/ paths (static assets + SPA fallback) are always served so the
        # login page itself can load. Reads headers only — never the body.
        if (path.startswith("/api/") and path not in _EXEMPT_GET
                and not verify_token(bearer_from_headers(self.headers))):
            return self._error(401, "authentication required")
        try:
            if path == "/api/health":
                return self._json({"ok": True})
            if path == "/api/status":
                return self._json(db_status())
            if path == "/api/system/status":
                return self._json(system_status_payload())
            if path == "/api/batches":
                status = (params.get("status", [""])[0] or None)
                limit = (params.get("limit", [""])[0] or None)
                return self._json(db_batches(status=status, limit=limit))
            if path == "/api/orchestration/config":
                return self._json(orchestration_config.orchestration_config_payload())
            if path == "/api/analytics":
                return self._json(analytics_payload())
            if path == "/api/analytics/linkedin":
                return self._json(linkedin_analytics_payload())
            if path == "/api/analytics/aisdr":
                return self._json(aisdr_analytics_payload())
            if path == "/api/hubspot/aisdr/status":
                return self._json(aisdr_sync_status_payload())
            if path == "/api/unenroll/status":
                return self._json(unenrollment_status_payload())
            if path == "/api/progress":
                return self._json(progress_payload())
            if path == "/api/trends":
                return self._json(trends_payload())
            if path == "/api/replies/queue":
                return self._json(review_queue_payload())
            if path == "/api/replies/followup/drafts":
                return self._json(followup_drafts_payload())
            if path == "/api/replies/agents":
                return self._json(reply_agents_payload())
            if path.startswith("/api/replies/playbook/status/"):
                job_id = path[len("/api/replies/playbook/status/"):]
                job = PLAY_JOBS.get(job_id)
                if not job:
                    return self._error(404, f"no play job {job_id}")
                return self._json(_play_job_public(job))
            if path.startswith("/api/plays/") and path.endswith("/html"):
                slug = path[len("/api/plays/"):-len("/html")]
                if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,80}", slug or ""):
                    return self._error(400, "bad slug")
                d = SIGNAL_PLAYS_DIR / slug
                html = next(iter(sorted(d.glob("*.html"))), None) if d.is_dir() else None
                if not html:
                    return self._error(404, f"no play html for {slug}")
                body = html.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if path == "/api/signals":
                return self._json(signals_payload())
            if path == "/api/signals/detail":
                return self._json(signals_detail(params.get("domain", [""])[0]))
            if path.startswith("/api/signals/tech/status/"):
                job_id = path[len("/api/signals/tech/status/"):]
                job = TECH_JOBS.get(job_id)
                if not job:
                    return self._error(404, f"no tech job {job_id}")
                return self._json(dict(job))
            if path.startswith("/api/signals/hiring/status/"):
                job_id = path[len("/api/signals/hiring/status/"):]
                job = HIRING_JOBS.get(job_id)
                if not job:
                    return self._error(404, f"no hiring job {job_id}")
                return self._json(dict(job))
            if path == "/api/variants":
                return self._json(variant_breakdown())
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
            if path == "/api/hubspot/lists":
                q = params.get("q", [""])[0]
                list_type = params.get("type", [None])[0]
                return self._json(do_hubspot_lists(q, list_type))
            if path == "/api/hubspot/activity/status":
                return self._json(hubspot_activity_status_payload())
            if path == "/api/hubspot/activity/audit":
                # read-only duplicate diagnosis — see hubspot_activity_audit.py
                args = [str(HUBSPOT_ACTIVITY_AUDIT), "--json"]
                for cid in params.get("contact_id", []):
                    args += ["--contact-id", cid]
                sample = (params.get("sample", [""])[0] or "").strip()
                if sample.isdigit():
                    args += ["--sample", sample]
                elif not params.get("contact_id"):
                    args += ["--sample", "5"]
                res = run_script(args, timeout=600)
                if res["returncode"] != 0:
                    return self._error(502, (res["stderr"] or res["stdout"]).strip()[:400])
                try:
                    last = [ln for ln in res["stdout"].splitlines() if ln.strip()][-1]
                    return self._json(json.loads(last))
                except (json.JSONDecodeError, IndexError):
                    return self._error(502, "could not parse audit output")
            if path == "/api/heyreach/activity/status":
                return self._json(heyreach_activity_status_payload())
            if path == "/api/clay/status":
                return self._json(do_clay_status())
            if path == "/api/clay/oauth/start":
                base = self._public_base()
                redirect = f"{base}/api/clay/oauth/callback" if base else None
                return self._json(do_clay_oauth_start(redirect))
            if path == "/api/clay/oauth/callback":
                code = params.get("code", [None])[0]
                state = params.get("state", [None])[0]
                err = params.get("error", [None])[0]
                return self._clay_callback_html(code, state, err)
            if path.startswith("/api/source/status/"):
                job_id = path[len("/api/source/status/"):]
                job = SOURCE_JOBS.get(job_id)
                if not job:
                    return self._error(404, f"no source job {job_id}")
                return self._json(_source_job_public(job))
            if path == "/api/source/progress":
                list_id = (params.get("list_id", [""])[0]).strip()
                if not list_id:
                    return self._error(400, "list_id required")
                return self._json({"ok": True, "list_id": list_id,
                                   **read_source_progress(list_id)})
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
        # Auth gate (see do_GET). /api/login and the HeyReach webhook are exempt.
        if (path.startswith("/api/") and path not in _EXEMPT_POST
                and not verify_token(bearer_from_headers(self.headers))):
            return self._error(401, "authentication required")
        try:
            if path == "/api/login":
                body = self._read_body()
                email = str(body.get("email", "")).strip().lower()
                password = str(body.get("password", ""))
                if not verify_credentials(email, password):
                    time.sleep(0.5)  # blunt online password guessing
                    return self._error(401, "invalid credentials")
                return self._json({"ok": True, "token": make_token(email), "email": email})
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
                payload, code = start_generate_job(int(batch_id), variant=_clean_variant(body.get("variant")))
                return self._json(payload, code=code)
            if path == "/api/generate/batch":
                body = self._read_body()
                payload, code = start_batch_job(limit=body.get("limit"),
                                                batch_ids=body.get("batch_ids"),
                                                variant=_clean_variant(body.get("variant")),
                                                split=body.get("split"))
                return self._json(payload, code=code)
            if path == "/api/source/enrich":
                body = self._read_body()
                list_id = str(body.get("list_id", "")).strip()
                if not list_id:
                    return self._error(400, "list_id required")
                payload, code = start_source_job(
                    list_id, list_name=(body.get("list_name") or None),
                    cap=int(body.get("cap", 25)), mode=(body.get("mode") or "end-to-end"),
                    per_company_cap=body.get("per_company_cap", 0),
                    concurrency=body.get("concurrency", 8),
                    titles=body.get("titles", ""), locations=body.get("locations", ""),
                    reset=bool(body.get("reset", False)),
                    whole_list=bool(body.get("whole_list", False)))
                return self._json(payload, code=code)
            if path.startswith("/api/source/confirm/"):
                job_id = path[len("/api/source/confirm/"):]
                payload, code = confirm_source_job(job_id)
                return self._json(payload, code=code)
            if path == "/api/source/progress/reset":
                body = self._read_body()
                list_id = str(body.get("list_id", "")).strip()
                if not list_id:
                    return self._error(400, "list_id required")
                p = _source_progress_path(list_id)
                try:
                    p.unlink()
                except FileNotFoundError:
                    pass
                return self._json({"ok": True, "list_id": list_id, "enriched": 0})
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
            if path == "/api/signals/tech/detect":
                body = self._read_body()
                payload, code = do_detect_tech(body.get("domain"), force=bool(body.get("force")))
                return self._json(payload, code)
            if path == "/api/signals/hiring/detect":
                body = self._read_body()
                payload, code = do_detect_hiring(body.get("domain"), force=bool(body.get("force")))
                return self._json(payload, code)
            if path == "/api/signals/hiring/backfill":
                body = self._read_body()
                payload, code = start_hiring_backfill(
                    limit=body.get("limit"), stale_days=body.get("stale_days"),
                    force=bool(body.get("force")))
                return self._json(payload, code)
            if path == "/api/signals/tech/backfill":
                body = self._read_body()
                payload, code = start_tech_backfill(
                    limit=body.get("limit"), stale_days=body.get("stale_days"),
                    force=bool(body.get("force")))
                return self._json(payload, code)
            if path == "/api/replies/scan":
                body = self._read_body()
                return self._json(do_scan_replies(
                    campaign_id=body.get("campaign_id"),
                    lookback_days=int(body.get("lookback_days", 14)),
                ))
            if path == "/api/samples":
                body = self._read_body()
                return self._json(do_generate_samples(
                    company=body.get("company"), domain=body.get("domain"),
                    from_email=body.get("from_email"),
                ))
            if path == "/api/replies/tag":
                body = self._read_body()
                if body.get("confirm") is not True:
                    return self._error(400, "tagging requires confirm=true")
                reply_ids = body.get("reply_ids") or []
                if not reply_ids:
                    return self._error(400, "reply_ids required")
                return self._json(do_tag_replies(reply_ids))
            if path == "/api/replies/agent":
                body = self._read_body()
                if not (body.get("reply_id") and body.get("agent")):
                    return self._error(400, "reply_id and agent required")
                payload, code = do_set_reply_agent(body.get("reply_id"), body.get("agent"))
                return self._json(payload, code=code)
            if path == "/api/replies/followup/regenerate":
                body = self._read_body()
                if not body.get("reply_id"):
                    return self._error(400, "reply_id required")
                payload, code = do_regenerate_followup(
                    body.get("reply_id"), body.get("agent"), body.get("company_domain"))
                return self._json(payload, code=code)
            if path == "/api/replies/dismiss":
                body = self._read_body()
                if not body.get("reply_id"):
                    return self._error(400, "reply_id required")
                payload, code = do_dismiss_reply(body.get("reply_id"), body.get("reason"))
                return self._json(payload, code=code)
            if path == "/api/replies/undismiss":
                body = self._read_body()
                if not body.get("reply_id"):
                    return self._error(400, "reply_id required")
                payload, code = do_undismiss_reply(body.get("reply_id"))
                return self._json(payload, code=code)
            if path == "/api/replies/reclassify":
                body = self._read_body()
                if not body.get("reply_id"):
                    return self._error(400, "reply_id required")
                payload, code = do_reclassify_reply(body.get("reply_id"))
                return self._json(payload, code=code)
            if path == "/api/replies/followup/move":
                body = self._read_body()
                if not body.get("reply_id"):
                    return self._error(400, "reply_id required")
                if body.get("to") not in ("interested", "followup"):
                    return self._error(400, "to must be 'interested' or 'followup'")
                payload, code = do_move_reply(body.get("reply_id"), body.get("to"))
                return self._json(payload, code=code)
            if path == "/api/replies/followup/draft":
                return self._json(do_draft_followups())
            if path == "/api/replies/followup/approve":
                body = self._read_body()
                if body.get("confirm") is not True:
                    return self._error(400, "sending requires confirm=true")
                if not body.get("reply_id"):
                    return self._error(400, "reply_id required")
                payload, code = do_approve_followup(body.get("reply_id"), body.get("message"))
                return self._json(payload, code=code)
            if path == "/api/hubspot/activity/sync":
                body = self._read_body()
                return self._json(do_hubspot_activity_sync(
                    since_days=body.get("since_days"),
                    limit=body.get("limit"),
                    dry_run=bool(body.get("dry_run", False)),
                    contact_id=body.get("contact_id"),
                    event_types=body.get("event_types"),
                    replies_only=bool(body.get("replies_only", False)),
                    refresh_leads=bool(body.get("refresh_leads", False)),
                ))
            if path == "/api/hubspot/aisdr/sync":
                body = self._read_body()
                payload, code = do_aisdr_sync(full=bool(body.get("full")),
                                              dry_run=bool(body.get("dry_run")))
                return self._json(payload, code=code)
            if path == "/api/unenroll/run":
                body = self._read_body()
                payload, code = do_unenrollment_check(dry_run=bool(body.get("dry_run")))
                return self._json(payload, code=code)
            if path == "/api/heyreach/webhook":
                return self._heyreach_webhook(parsed)
            if path == "/api/reindex":
                n = INDEX.build()
                return self._json({"indexed": n, "built_at": INDEX.built_at})
            return self._error(404, "unknown endpoint")
        except subprocess.TimeoutExpired:
            return self._error(504, "subprocess timed out")
        except Exception as e:  # noqa: BLE001
            return self._error(500, f"{type(e).__name__}: {e}")

    def _heyreach_webhook(self, parsed):
        """Receive a HeyReach (LinkedIn) webhook: validate the shared secret, persist the
        RAW payload to the durable inbox, ACK 200 immediately, then kick a background
        drain. We never require valid JSON to ACK (durability first) and never let an
        error block the 200, so HeyReach won't enter its 24h retry storm over our hiccups.
        The reconcile (resolve contact -> log a HubSpot LinkedIn Communication) happens
        out-of-band in heyreach_activity.py, idempotently."""
        import os
        import hmac
        secret = os.environ.get("HEYREACH_WEBHOOK_SECRET")
        given = (parse_qs(parsed.query).get("secret", [None])[0]
                 or self.headers.get("X-Webhook-Secret"))
        if secret:
            if not given or not hmac.compare_digest(given, secret):  # constant-time
                return self._error(401, "bad or missing webhook secret")
        else:
            # Fail closed off-box: never accept an unauthenticated webhook from a remote
            # host (we bind 0.0.0.0 on Railway). Loopback is allowed for local dev.
            client_ip = self.client_address[0] if self.client_address else ""
            if client_ip not in ("127.0.0.1", "::1"):
                return self._error(503, "webhook secret not configured")
        try:
            length = int(self.headers.get("Content-Length", 0) or 0)
        except (TypeError, ValueError):
            length = 0
        if length > MAX_WEBHOOK_BODY:
            return self._error(413, "payload too large")
        raw = self.rfile.read(length).decode("utf-8", "replace") if length else ""
        event_id = event_type = dedup = None
        try:  # best-effort indexing for idempotent ACK; the drain re-parses authoritatively
            obj = json.loads(raw) if raw else {}
            ev = heyreach_activity.normalize_event(obj) if isinstance(obj, dict) else None
            if ev:
                event_id, event_type = ev.get("event_id"), ev.get("raw_type")
                dedup = heyreach_activity.dedup_key(ev)
        except Exception:  # noqa: BLE001 — parsing must never block the ACK
            pass
        # Persist-before-process: the inbox row is the ONLY durable copy of the payload, so
        # a failed write must NOT be ACKed as success — return 5xx and let HeyReach retry
        # (it retries up to 5x over 24h). Only ACK 200 once the row is durably stored
        # (a duplicate returning None is still a successful persist).
        persisted = False
        try:
            conn = pipeline_db.connect()
            try:
                pipeline_db.enqueue_heyreach_event(conn, raw, event_id=event_id,
                                                   event_type=event_type, dedup_key=dedup)
                persisted = True
            finally:
                conn.close()
        except Exception as e:  # noqa: BLE001
            sys.stderr.write(f"[heyreach] enqueue failed: {type(e).__name__}: {e}\n")
        if not persisted:
            return self._error(503, "could not persist event; retry")
        self._json({"ok": True}, code=200)
        _kick_heyreach_drain()
        return

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


_hr_drain_lock = threading.Lock()


def _kick_heyreach_drain():
    """Spawn at most one background drain of the HeyReach webhook inbox (single-flight:
    a running drain naturally picks up rows queued during its run). Best-effort and fully
    isolated — it shells out to the reconcile script and can never raise into the request
    path. Called right after a webhook is persisted for near-real-time logging."""
    if not _hr_drain_lock.acquire(blocking=False):
        return  # a drain is already running; it will sweep the newly-queued rows
    def _pending_count():
        try:
            conn = pipeline_db.connect()
            try:
                return conn.execute(
                    "SELECT COUNT(*) FROM heyreach_events WHERE status='pending'").fetchone()[0]
            finally:
                conn.close()
        except Exception:  # noqa: BLE001
            return 0
    def _run():
        rearm = False
        try:
            # Bounded loop: drain, then mop up rows that arrived DURING the drain (the
            # drain's SELECT is a snapshot). We re-loop only while genuinely-new 'pending'
            # rows remain — transient 'failed' rows are left for the hourly sweep so a
            # flaky HubSpot can't make this tight-loop.
            for _ in range(10):
                res = run_script([str(HEYREACH_ACTIVITY), "--drain", "--json"], timeout=900)
                lines = [ln for ln in (res.get("stdout") or "").splitlines() if ln.strip()]
                print(f"[heyreach-sync] drain: "
                      f"{lines[-1] if lines else (res.get('stderr') or '')[:200]}", flush=True)
                if not _pending_count():
                    break
        except Exception as e:  # noqa: BLE001 — a drain must never crash the server
            print(f"[heyreach-sync] drain error: {type(e).__name__}: {e}", flush=True)
        finally:
            _hr_drain_lock.release()
            # Re-arm: an event enqueued after our last in-loop check (while we still held
            # the lock) would otherwise wait for the hourly sweep. Re-kick once if so.
            rearm = _pending_count() > 0
        if rearm:
            _kick_heyreach_drain()
    try:
        threading.Thread(target=_run, daemon=True).start()
    except Exception as e:  # noqa: BLE001 — don't leak the lock if the thread can't start
        _hr_drain_lock.release()
        print(f"[heyreach-sync] could not start drain thread: {type(e).__name__}: {e}", flush=True)


def _activity_autosync_loop():
    """Background auto-sync: periodically log new email activity to HubSpot. Best-effort
    and fully isolated — it shells out to the reconcile script and can never raise into,
    block, or slow the request path. Every cycle logs replies (cheap); the heavier outbound
    sweep (sent sequence emails) is included on every HUBSPOT_ACTIVITY_AUTOSYNC_FULL_EVERY-th
    cycle. The outbound sweep is ON by default (HUBSPOT_ACTIVITY_AUTOSYNC_FULL=1) so sent
    emails log without any extra configuration; set it to 0 to opt out (replies only).
    Disable the whole loop with HUBSPOT_ACTIVITY_AUTOSYNC=0.
    """
    import os
    if (os.environ.get("HUBSPOT_ACTIVITY_AUTOSYNC", "1") or "1").strip().lower() in ("0", "false", "no"):
        print("[activity-sync] auto-sync disabled (HUBSPOT_ACTIVITY_AUTOSYNC=0)", flush=True)
        return
    interval = max(5, int(os.environ.get("HUBSPOT_ACTIVITY_AUTOSYNC_MINUTES", "60") or 60)) * 60
    full_on = (os.environ.get("HUBSPOT_ACTIVITY_AUTOSYNC_FULL", "1") or "1").strip().lower() in ("1", "true", "yes")
    full_every = max(1, int(os.environ.get("HUBSPOT_ACTIVITY_AUTOSYNC_FULL_EVERY", "12") or 12))
    time.sleep(120)  # let the server settle before the first run
    tick = 0
    while True:
        try:
            full = full_on and (tick % full_every == 0)
            args = [str(HUBSPOT_ACTIVITY_SYNC), "--json", "--since-days", "60"]
            if (os.environ.get("HUBSPOT_ACTIVITY_ALLOW_BACKFILL", "") or "").strip().lower() \
                    in ("1", "true", "yes"):
                args.append("--allow-backfill")     # intentional first backfill opt-in
            if full:
                args += ["--sleep", "0.1"]          # throttle the heavier outbound sweep
            else:
                args.append("--replies-only")       # cheap: inbound + our replies only
            with HS_SYNC_LOCK:
                res = run_script(args, timeout=5400)
            lines = [ln for ln in (res.get("stdout") or "").splitlines() if ln.strip()]
            summary = lines[-1] if lines else (res.get("stderr") or "")[:200]
            # The script exits 0 even when the run failed or the fresh-ledger guard
            # refused — health must come from the JSON summary, not the exit code.
            parsed = None
            try:
                parsed = json.loads(summary)
            except (json.JSONDecodeError, TypeError):
                pass
            ok = bool(parsed.get("ok")) if isinstance(parsed, dict) else res["returncode"] == 0
            if isinstance(parsed, dict) and parsed.get("guard"):
                summary = (f"refused: empty dedup ledger, {parsed.get('would_log')} events would "
                           f"log (> {parsed.get('threshold')}). Recover with "
                           f"--reconcile-from-hubspot, or set HUBSPOT_ACTIVITY_ALLOW_BACKFILL=1 "
                           f"for an intentional first backfill.")
            _record_autosync(ok, "full" if full else "replies", summary)
            print(f"[activity-sync] {'full' if full else 'replies'}: {summary}", flush=True)
        except Exception as e:  # noqa: BLE001 - auto-sync must never crash the server
            _record_autosync(False, "error", f"{type(e).__name__}: {e}")
            print(f"[activity-sync] error: {type(e).__name__}: {e}", flush=True)
        # Safety net for LinkedIn: re-drain any HeyReach webhook events left pending/failed
        # (HubSpot was down, the contact wasn't in the table yet, an inline drain raced).
        # The webhook itself logs in near-real-time; this just guarantees eventual delivery.
        if (os.environ.get("HEYREACH_ACTIVITY_AUTOSYNC", "1") or "1").strip().lower() not in ("0", "false", "no"):
            try:
                hr = run_script([str(HEYREACH_ACTIVITY), "--drain", "--json", "--retry-skipped"], timeout=1800)
                hlines = [ln for ln in (hr.get("stdout") or "").splitlines() if ln.strip()]
                print(f"[heyreach-sync] sweep: "
                      f"{hlines[-1] if hlines else (hr.get('stderr') or '')[:200]}", flush=True)
            except Exception as e:  # noqa: BLE001
                print(f"[heyreach-sync] sweep error: {type(e).__name__}: {e}", flush=True)
        tick += 1
        time.sleep(interval)


def _unenrollment_loop():
    """Unenrollment sweeps every UNENROLL_CHECK_MINUTES (default 30): stop Bison +
    HeyReach outreach for contacts RevOps tagged everworker_tag=false in HubSpot.
    Shares do_unenrollment_check() with POST /api/unenroll/run — a 409 here just
    means a manual run is already in flight, which counts as this cycle's sweep.
    Best-effort and isolated: it can never raise into the request path. Disable
    with UNENROLL_CHECK_ENABLED=0 (the manual endpoint keeps working)."""
    env = read_env()  # .env + process env, same source as the status endpoint
    if (env.get("UNENROLL_CHECK_ENABLED", "1") or "1").strip().lower() in ("0", "false", "no"):
        print("[unenroll] sweeper disabled (UNENROLL_CHECK_ENABLED=0)", flush=True)
        return
    if not env.get("HUBSPOT_ACCESS_TOKEN"):
        print("[unenroll] HUBSPOT_ACCESS_TOKEN not set — sweeper disabled", flush=True)
        return
    try:
        interval = max(5, int(env.get("UNENROLL_CHECK_MINUTES", "30") or 30)) * 60
    except ValueError:
        interval = 30 * 60
    print(f"[unenroll] sweeper enabled every {interval // 60} min", flush=True)
    time.sleep(120)  # let the server settle before the first sweep
    while True:
        try:
            payload, code = do_unenrollment_check()
            print(f"[unenroll] sweep trigger -> {code} {payload}", flush=True)
        except Exception as e:  # noqa: BLE001 - the sweeper must never crash the server
            print(f"[unenroll] sweep error: {type(e).__name__}: {e}", flush=True)
        time.sleep(interval)


def _aisdr_sync_loop():
    """Nightly AI SDR deal-attribution sync at AISDR_SYNC_HOUR (default 0 = midnight)
    US Eastern time. DST-safe: each iteration recomputes the next wall-clock target
    with zoneinfo instead of adding a fixed 24h. The first run against an empty
    MongoDB is the seed; later runs are incremental (the script keeps a watermark).
    Disable with AISDR_SYNC_ENABLED=0; requires MONGO_URL (Railway MongoDB service)."""
    import os
    from datetime import datetime, time as dtime, timedelta
    from zoneinfo import ZoneInfo
    if (os.environ.get("AISDR_SYNC_ENABLED", "1") or "1").strip().lower() in ("0", "false", "no"):
        print("[aisdr-sync] nightly sync disabled (AISDR_SYNC_ENABLED=0)", flush=True)
        return
    if not mongo_store.mongo_configured():
        print("[aisdr-sync] MONGO_URL not set — nightly sync disabled "
              "(connect the Railway MongoDB service to enable)", flush=True)
        return
    hour = min(23, max(0, int(os.environ.get("AISDR_SYNC_HOUR", "0") or 0)))
    tz = ZoneInfo("America/New_York")
    print(f"[aisdr-sync] nightly sync enabled at {hour:02d}:00 America/New_York", flush=True)
    while True:
        now = datetime.now(tz)
        target_date = now.date() if now.time() < dtime(hour) else now.date() + timedelta(days=1)
        target = datetime.combine(target_date, dtime(hour), tzinfo=tz)
        time.sleep(max(60, (target - now).total_seconds()))
        payload, code = do_aisdr_sync()
        print(f"[aisdr-sync] nightly trigger -> {code} {payload}", flush=True)
        time.sleep(120)  # step past the target minute before recomputing tomorrow's


def main():
    import os
    # $PORT is injected by hosting platforms (Railway); --port overrides for local runs.
    port = int(os.environ.get("PORT", "8787"))
    # Bind localhost for local dev (safe), but all interfaces when a platform sets $PORT
    # (Railway) or HOST is given — otherwise HeyReach's webhook can never reach us.
    host = os.environ.get("HOST") or ("0.0.0.0" if os.environ.get("PORT") else "127.0.0.1")
    static_dir = PROJECT_ROOT / "webui" / "frontend" / "dist"
    args = sys.argv[1:]
    for i, a in enumerate(args):
        if a == "--port" and i + 1 < len(args):
            port = int(args[i + 1])
        if a == "--static" and i + 1 < len(args):
            static_dir = Path(args[i + 1]).resolve()
        if a == "--host" and i + 1 < len(args):
            host = args[i + 1]

    Handler.static_dir = static_dir if static_dir.is_dir() else None

    print(f"[webui] project root: {PROJECT_ROOT}")
    # Ensure the pipeline DB schema (incl. the heyreach_events webhook inbox) exists, so a
    # webhook arriving on a fresh deploy before any sync runs has a table to land in.
    try:
        _c = pipeline_db.connect()
        pipeline_db.init_schema(_c)
        _c.close()
    except Exception as e:  # noqa: BLE001
        print(f"[webui] schema init warning: {type(e).__name__}: {e}")
    print(f"[webui] building outreach index ...", flush=True)
    n = INDEX.build()
    print(f"[webui] indexed {n} generated outreach files")
    resumed = resume_batch_jobs()
    if resumed:
        print(f"[webui] resumed {resumed} in-flight batch job(s)")
    threading.Thread(target=_activity_autosync_loop, daemon=True).start()
    threading.Thread(target=_aisdr_sync_loop, daemon=True).start()
    threading.Thread(target=_unenrollment_loop, daemon=True).start()
    if Handler.static_dir:
        print(f"[webui] serving frontend from {Handler.static_dir}")
    else:
        print(f"[webui] frontend dist not found; API-only (use Vite dev server)")
    print(f"[webui] listening on http://{host}:{port}")
    ThreadingHTTPServer((host, port), Handler).serve_forever()


if __name__ == "__main__":
    main()
