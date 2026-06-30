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

import base64
import concurrent.futures
import hashlib
import hmac
import json
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

PERSONA_ORDER = ["sales-leadership", "revops", "partnerships", "sdr-bdr"]
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


def route_campaign(persona, variant, pmap=None, vmap=None, default=None):
    """The Bison campaign a contact actually enrolls into, mirroring enroll.py:
    variant campaign first, then persona campaign, then the default. Returns an
    int campaign id or None (unrouted)."""
    pmap = persona_campaign_map() if pmap is None else pmap
    vmap = variant_campaign_map() if vmap is None else vmap
    default = default_campaign_id() if default is None else default
    return vmap.get(variant) or pmap.get(persona) or default


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


# cid -> {"name": str|None, "leads": int|None}. The name is cached after the first
# success (it rarely changes); the lead count is refreshed live each call but the
# last-good value is retained so a transient HeyReach hiccup doesn't drop the diagram
# to 0.
_HEYREACH_CAMPAIGN_CACHE = {}


def linkedin_channel():
    """The HeyReach (LinkedIn) channel all personas feed into: campaign id/name +
    how many leads are in the campaign.

    The lead count comes from the LIVE HeyReach campaign (progressStats.totalUsers —
    the same figure the Analytics page shows as "Leads in campaign"), NOT from
    data/outreach/heyreach_state.json. That file is only written by the legacy
    `sdr_batches.py heyreach-backfill` path; the main `enroll` path adds leads to
    HeyReach without ever touching it, so in normal operation it is missing/stale and
    would make this node read 0 even when the campaign holds thousands of leads. The
    local file is kept only as a last-resort fallback when HeyReach is unreachable and
    we've never seen a live value."""
    env = read_env()
    raw = (env.get("HEYREACH_CAMPAIGN_ID") or "").strip()
    cid = int(raw) if raw.isdigit() else None
    if cid is None:
        return None
    cache = _HEYREACH_CAMPAIGN_CACHE.setdefault(cid, {"name": None, "leads": None})
    try:
        sys.path.insert(0, str(SCRIPTS / "sdr-pipeline" / "scripts"))
        from heyreach_client import HeyReachClient
        camp = HeyReachClient().get_campaign(cid) or {}
        if camp.get("name"):
            cache["name"] = camp["name"]
        total_users = (camp.get("progressStats") or {}).get("totalUsers")
        if isinstance(total_users, (int, float)):
            cache["leads"] = int(total_users)
    except Exception:  # noqa: BLE001 — never block the diagram on a HeyReach hiccup
        pass
    leads = cache["leads"]
    if leads is None:  # live unavailable and never seen — fall back to the local file
        leads = 0
        sp = DATA / "outreach" / "heyreach_state.json"
        if sp.is_file():
            try:
                leads = len(json.loads(sp.read_text()).get("added", []))
            except (ValueError, OSError):
                pass
    return {"campaign_id": cid, "campaign_name": cache["name"], "leads": leads}


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


def _campaign_stats(c):
    """The Bison campaign-wide totals slice (all leads ever added to the campaign,
    from any source — NOT just this pipeline's contacts), or None if uncached."""
    if not c:
        return None
    return {
        "total_leads": c.get("total_leads"),
        "total_leads_contacted": c.get("total_leads_contacted"),
        "unique_replies": c.get("unique_replies"),
        "interested": c.get("interested"),
        "reply_rate_pct": c.get("reply_rate_pct"),
        "interested_rate_pct": c.get("interested_rate_pct"),
    }


def _contacts_grouped():
    """[(persona, variant, status, n)] over all contacts. Falls back to a
    variant-less grouping on DBs that predate the variant column (the server opens
    read-only and can't run the batch_db migration)."""
    with db_connect() as conn:
        try:
            return [(r["persona"], r["variant"], r["status"], r["n"]) for r in conn.execute(
                "SELECT persona, variant, status, COUNT(*) n FROM contacts "
                "GROUP BY persona, variant, status")]
        except sqlite3.OperationalError:
            return [(r["persona"], None, r["status"], r["n"]) for r in conn.execute(
                "SELECT persona, status, COUNT(*) n FROM contacts GROUP BY persona, status")]


def rollup_payload():
    st = db_status()
    pmap = persona_campaign_map()
    vmap = variant_campaign_map()
    default_cid = default_campaign_id()
    analytics = analytics_payload()
    by_campaign = {c.get("campaign_id"): c for c in analytics["campaigns"]}

    # Left column — persona/agent nodes (contact counts straight from the DB).
    personas = []
    for p in PERSONA_ORDER:
        cid = pmap.get(p)
        c = by_campaign.get(cid) if cid is not None else None
        personas.append({
            "persona": p,
            "campaign_id": cid,
            "campaign_name": (c or {}).get("campaign_name"),
            "contacts": st["by_persona"].get(p, 0),
            "by_status": st["persona_status"].get(p, {}),
            "campaign_stats": _campaign_stats(c),
        })

    # Right column — EVERY Bison campaign contacts actually route into. Each
    # contact's destination is derived exactly as enrollment does it (variant
    # campaign first, persona campaign, then the default), so both the per-variant
    # (14/15/16) and the legacy per-persona (10-13) campaigns surface when in use.
    agg = {}    # cid(int|None) -> rollup of this pipeline's contacts
    edges = {}  # (persona, cid) -> count, for drawing the routing
    for persona, variant, status, n in _contacts_grouped():
        cid = route_campaign(persona, variant, pmap, vmap, default_cid)
        a = agg.setdefault(cid, {"contacts": 0, "enrolled": 0, "by_status": {},
                                 "personas": set(), "variants": set()})
        a["contacts"] += n
        if status == "enrolled":
            a["enrolled"] += n
        a["by_status"][status] = a["by_status"].get(status, 0) + n
        if persona:
            a["personas"].add(persona)
        if variant:
            a["variants"].add(variant)
        if persona is not None:
            edges[(persona, cid)] = edges.get((persona, cid), 0) + n

    variant_by_cid = {cid: v for v, cid in vmap.items() if cid is not None}
    persona_by_cid = {cid: p for p, cid in pmap.items() if cid is not None}
    campaigns = []
    for cid, a in agg.items():
        c = by_campaign.get(cid) or {}
        if cid is None:
            kind, label = "unrouted", "unrouted (no campaign configured)"
        elif cid in variant_by_cid:
            kind, label = "variant", variant_by_cid[cid]
        elif cid in persona_by_cid:
            kind, label = "persona", persona_by_cid[cid]
        else:
            kind, label = "campaign", (c.get("campaign_name") or f"#{cid}")
        campaigns.append({
            "campaign_id": cid,
            "campaign_name": c.get("campaign_name"),
            "kind": kind,
            "label": label,
            "pipeline_contacts": a["contacts"],
            "pipeline_enrolled": a["enrolled"],
            "by_status": a["by_status"],
            "personas": sorted(a["personas"]),
            "variants": sorted(a["variants"]),
            "stats": _campaign_stats(c),
        })
    # busiest first; unrouted bucket always last
    campaigns.sort(key=lambda x: (x["campaign_id"] is None, -x["pipeline_contacts"]))

    return {
        "personas": personas,
        "personas_order": PERSONA_ORDER,
        "campaigns": campaigns,
        "edges": [{"persona": p, "campaign_id": cid, "count": n}
                  for (p, cid), n in edges.items()],
        "linkedin": linkedin_channel(),
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
    res = run_script(args, timeout=3600)
    if res["returncode"] != 0:
        return {"ok": False, "error": (res["stderr"] or res["stdout"]).strip()[:500]}
    try:  # the script prints progress lines, then the JSON summary on the last line
        last = [ln for ln in (res["stdout"] or "").splitlines() if ln.strip()][-1]
        return json.loads(last)
    except (json.JSONDecodeError, IndexError):
        return {"ok": False, "error": "could not parse activity-sync output",
                "stdout": (res["stdout"] or "")[-500:]}


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


def _mark_sent(reply_id, meta):
    data = _read_json(SENT_FOLLOWUPS) or {}
    data.setdefault("sent", {})[str(reply_id)] = meta
    SENT_FOLLOWUPS.parent.mkdir(parents=True, exist_ok=True)
    SENT_FOLLOWUPS.write_text(json.dumps(data, indent=2))


def _stamp_handled(items):
    sent = _load_sent()
    for it in items or []:
        it["handled"] = str(it.get("reply_id")) in sent
    return items


def review_queue_payload():
    """Unified review queue: email (Bison) + LinkedIn (HeyReach) replies in one
    list, each item carrying a `channel`. Counts are summed across channels."""
    email = _read_json(REVIEW_QUEUE) or {}
    li = _read_json(LI_REVIEW_QUEUE) or {}
    if not email and not li:
        return {"available": False, "items": []}
    for it in (email.get("items") or []):
        it.setdefault("channel", "email")
    items = list(email.get("items") or []) + list(li.get("items") or [])
    _stamp_handled(items)
    ec, lc = (email.get("counts") or {}), (li.get("counts") or {})
    counts = {
        "scanned": (ec.get("scanned") or 0) + (lc.get("scanned") or 0),
        "flagged": (ec.get("flagged") or 0) + (lc.get("flagged") or 0),
        "already": ec.get("already") or 0,
        "unsubscribed": ec.get("unsubscribed") or 0,
        "filtered": (ec.get("filtered") or 0) + (lc.get("filtered") or 0),
        "email_scanned": ec.get("scanned") or 0,
        "linkedin_scanned": lc.get("scanned") or 0,
    }
    return {
        "available": True,
        "items": items,
        "counts": counts,
        "scanned_at": email.get("scanned_at") or li.get("scanned_at"),
        "lookback_days": email.get("lookback_days") or li.get("lookback_days"),
        "linkedin": {"configured": bool(li.get("configured")), "error": li.get("error")},
    }


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
    for rid in reply_ids:
        li_item = li_by_id.get(str(rid))
        if li_item is not None:                          # LinkedIn — local flip only
            li_item["already_interested"] = True
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
            item["already_interested"] = True  # update cache
        except Exception as e:  # noqa: BLE001 - one bad reply must not abort the rest
            results.append({"reply_id": rid, "lead_id": lead_id, "ok": False, "error": str(e)[:200]})
            failed += 1
    if email_q:
        REVIEW_QUEUE.write_text(json.dumps(email_q, indent=2))
    if li_q:
        LI_REVIEW_QUEUE.write_text(json.dumps(li_q, indent=2))
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
                              "conversation_id": conv_id, "linkedin_account_id": acct_id})
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
                          "sender_email_id": sender_email_id})
    _mark_draft_sent(drafts, draft, body_text)
    return {"ok": True, "reply_id": reply_id, "channel": "email", "to_email": to_email}, 200


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
            if path == "/api/batches":
                status = (params.get("status", [""])[0] or None)
                limit = (params.get("limit", [""])[0] or None)
                return self._json(db_batches(status=status, limit=limit))
            if path == "/api/rollup":
                return self._json(rollup_payload())
            if path == "/api/analytics":
                return self._json(analytics_payload())
            if path == "/api/analytics/linkedin":
                return self._json(linkedin_analytics_payload())
            if path == "/api/progress":
                return self._json(progress_payload())
            if path == "/api/trends":
                return self._json(trends_payload())
            if path == "/api/replies/queue":
                return self._json(review_queue_payload())
            if path == "/api/replies/followup/drafts":
                return self._json(followup_drafts_payload())
            if path == "/api/signals":
                return self._json(signals_payload())
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
            if full:
                args += ["--sleep", "0.1"]          # throttle the heavier outbound sweep
            else:
                args.append("--replies-only")       # cheap: inbound + our replies only
            res = run_script(args, timeout=5400)
            lines = [ln for ln in (res.get("stdout") or "").splitlines() if ln.strip()]
            print(f"[activity-sync] {'full' if full else 'replies'}: "
                  f"{lines[-1] if lines else (res.get('stderr') or '')[:200]}", flush=True)
        except Exception as e:  # noqa: BLE001 - auto-sync must never crash the server
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
    if Handler.static_dir:
        print(f"[webui] serving frontend from {Handler.static_dir}")
    else:
        print(f"[webui] frontend dist not found; API-only (use Vite dev server)")
    print(f"[webui] listening on http://{host}:{port}")
    ThreadingHTTPServer((host, port), Handler).serve_forever()


if __name__ == "__main__":
    main()
