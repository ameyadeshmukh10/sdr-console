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

import config_edit
import connectors
import demo_mode

# webui/server/app.py -> webui/server -> webui -> project root
PROJECT_ROOT = Path(__file__).resolve().parents[2]
DATA = PROJECT_ROOT / "data"
DB_PATH = DATA / "outreach" / "pipeline.db"
GEN_DIR = DATA / "outreach" / "generated"
CONTACTS_JSONL = DATA / "outreach" / "contacts.jsonl"
CAMPAIGN_STATS = DATA / "campaign-stats"
ENV_PATH = PROJECT_ROOT / ".env"


def R(path):
    """Resolve a data path for READING, honoring this request's demo profile.

    Live data by default. When a demo profile is active for the current request
    thread, the path is remapped into `data/demo/<profile>/…` so the whole console
    reads from that synthetic tree. Apply this at read sites ONLY — writers and
    background jobs must keep using the bare constants so they always act on
    reality (see demo_mode's module docstring).
    """
    return demo_mode.resolve(DATA, demo_mode.active(), path)


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
# Config-edit audit log + pre-change snapshots. On the data volume so a revert
# survives a restart, and never demo-scoped — config edits are always for real.
CONFIG_HISTORY = DATA / "config-history"
ANALYZE = SCRIPTS / "interested-trends" / "scripts"
TRENDS_DIR = DATA / "interested-replies" / "analysis"
REPLIES_LAST_RUN = DATA / "interested-replies" / "last_run.json"
REVIEW_QUEUE = DATA / "interested-replies" / "review_queue.json"
LI_REVIEW_QUEUE = DATA / "interested-replies" / "li_review_queue.json"
BATCH_JOBS_DIR = DATA / "outreach" / "batch-jobs"
# Daily hot-target snapshot. A file, not a live query, so the report is stable for a
# working day; the campaign sweep rebuilds it once every 24h.
HOT_LIST_PATH = DATA / "outreach" / "hot-list.json"

# In-process pipeline-DB access for the HeyReach webhook path. The server's own
# db_connect() is read-only (mode=ro); persisting webhook events needs writes, so that
# one path uses batch_db.connect() (read-write WAL). heyreach_activity supplies the
# normalize_event/dedup_key helpers used to index events on receipt.
sys.path.insert(0, str(PIPELINE_SCRIPTS))
import batch_db as pipeline_db        # noqa: E402
import heyreach_activity              # noqa: E402
import mongo_store                    # noqa: E402  (lazy pymongo — safe without it)
import orchestration_config           # noqa: E402  (no I/O at import; parses on request)
import campaigns_api                  # noqa: E402  (imports batch_db + campaigns; stdlib only)
import demo_actions                   # noqa: E402  (simulated CRM/Clay sources for demo mode)
import tiers                          # noqa: E402  (static packaging registry)
import reports                        # noqa: E402  (ad-hoc report builder)
import campaign_brief                 # noqa: E402  (lazy Anthropic client — safe with no key)
import connector_store                # noqa: E402  (console-set credentials on the volume)


def _demo_db():
    """The active demo profile's own pipeline.db, or None when live."""
    return demo_mode.db_path(DATA, demo_mode.active())


def _demo_dir():
    p = demo_mode.active()
    return (DATA / demo_mode.DEMO_SUBDIR / p) if p else None


# Campaign writes route to the demo profile's DB when one is active. Injected here
# rather than imported inside campaigns_api so that module stays CLI-usable.
campaigns_api.demo_db_path = _demo_db
campaigns_api.demo_dir_path = _demo_dir

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
def crm_link_config():
    """How to deep-link a contact into the CRM.

    A contact id in this console is the HubSpot record id, so the only missing piece
    is the portal. Without a link, "call this person" means copying a name into
    another tab — which is where a prioritised call list stops being used.
    """
    import os
    env = read_env()
    portal = (env.get("HUBSPOT_PORTAL_ID") or "").strip()
    if not portal:
        return {"available": False, "reason": "HUBSPOT_PORTAL_ID not set"}
    return {
        "available": True, "portal_id": portal, "provider": "hubspot",
        # {id} is substituted client-side; kept as a template so a different CRM is
        # a config change rather than a frontend edit.
        "contact_url": f"https://app.hubspot.com/contacts/{portal}/contact/{{id}}",
        "company_url": f"https://app.hubspot.com/contacts/{portal}/company/{{id}}",
    }


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
    uri = f"file:{R(DB_PATH)}?mode=ro"
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


# The 4-email + 3-LinkedIn template every sequence follows. Used to turn "N touches
# logged" into "step N of 4", so the UI can say which messages have actually gone out.
EMAIL_STEPS = 4
LINKEDIN_STEPS = 3


def db_sequence_progress():
    """{contact_id: {email_sent, li_sent, replied, last_sent_at}} from the activity ledger.

    There is no per-STEP send record anywhere — HubSpot activity rows carry a channel
    and a direction, not a step number. But sends happen in template order, so a count
    of logged outbound touches maps to "steps 1..N sent, the rest still staged". That
    inference is stated wherever it surfaces; it is not a per-message confirmation.

    Only rows that were actually logged count ('logged' status) — a failed log attempt
    means we don't know whether the send happened, and guessing would be worse.
    """
    out = {}
    try:
        with db_connect() as conn:
            for r in conn.execute(
                "SELECT contact_id, channel, event_type, COUNT(*) n, MAX(event_ts) last_ts "
                "FROM hubspot_activity_log "
                "WHERE contact_id IS NOT NULL AND status = 'logged' "
                "GROUP BY contact_id, channel, event_type"):
                cid = str(r["contact_id"])
                row = out.setdefault(cid, {"email_sent": 0, "li_sent": 0,
                                           "replied": False, "last_sent_at": None})
                if r["event_type"] == "outbound":
                    if (r["channel"] or "email") == "linkedin":
                        row["li_sent"] += r["n"]
                    else:
                        row["email_sent"] += r["n"]
                    if r["last_ts"] and (row["last_sent_at"] or "") < r["last_ts"]:
                        row["last_sent_at"] = r["last_ts"]
                elif r["event_type"] == "inbound":
                    row["replied"] = True
    except sqlite3.Error:
        return {}
    return out


def db_contact_meta():
    """contact_id -> full contact record from the DB (authoritative for ALL contacts,
    incl. ones sourced via Clay that never went through contacts.jsonl)."""
    out = {}
    with db_connect() as conn:
        for r in conn.execute(
            "SELECT contact_id, first_name, last_name, email, title, company, linkedin_url, "
            "persona, domain, variant, status, error, batch_id, updated_at FROM contacts"):
            out[r["contact_id"]] = dict(r)
    return out


# ----------------------------------------------------------------------------
# In-memory outreach index. Built once; rebuilt on demand (POST /api/reindex)
# or automatically when the generated dir grows newer than the last build.
# ----------------------------------------------------------------------------
class OutreachIndex:
    def __init__(self, profile=None):
        # One index per demo profile (None = live). Bound at construction rather
        # than read per-call so a build started on one thread can't have its paths
        # swapped underneath it by a differently-scoped request.
        self.profile = profile
        self.rows = []
        self.built_at = 0.0
        self.dir_mtime = 0.0
        self.lock = threading.Lock()

    def _p(self, path):
        return demo_mode.resolve(DATA, self.profile, path)

    def _load_contacts_jsonl(self):
        meta = {}
        contacts_jsonl = self._p(CONTACTS_JSONL)
        if contacts_jsonl.is_file():
            for line in contacts_jsonl.read_text().splitlines():
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
            progress = db_sequence_progress()
            rows = []
            gen_dir = self._p(GEN_DIR)
            if gen_dir.is_dir():
                for fp in gen_dir.glob("*.json"):
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
                        "updated_at": dbm.get("updated_at") or "",
                        # Sequence progress — how far through the 4+3 template this
                        # contact actually is (see db_sequence_progress).
                        "seq": progress.get(cid) or {
                            "email_sent": 0, "li_sent": 0, "replied": False,
                            "last_sent_at": None},
                    })
            # sort populated companies first (blanks last), then by name
            rows.sort(key=lambda r: (r["company"].strip() == "", r["company"].lower(), r["last_name"].lower()))
            self.rows = rows
            self.built_at = time.time()
            self.dir_mtime = self._current_dir_mtime()
        return len(rows)

    def _current_dir_mtime(self):
        try:
            return self._p(GEN_DIR).stat().st_mtime
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
        # Explicit id filters, so a link FROM a campaign or a contact lands on the
        # already-filtered slice rather than the whole list plus instructions.
        contact = get("contact")
        # on | off — how else would you ever find the person you excluded a year
        # ago? An exclusion you cannot list is an exclusion you cannot undo.
        outreach_state = get("outreach")
        off_ids, off_rows = None, {}
        if outreach_state in ("on", "off"):
            try:
                with db_connect() as _c:
                    # The full contact record, not just the id: this index only holds
                    # people with GENERATED COPY, and most of the pipeline has none.
                    # Filtering the index alone would have shown an empty exclusion
                    # list while people really were excluded — the exact failure this
                    # screen exists to prevent.
                    for r in _c.execute(
                            "SELECT contact_id, first_name, last_name, email, title, "
                            "       company, persona, status, engagement_state, "
                            "       paused_until, engagement_note, engagement_updated_at "
                            "FROM contacts WHERE engagement_state IS NOT NULL "
                            "  AND engagement_state != 'active'"):
                        off_rows[str(r["contact_id"])] = dict(r)
                off_ids = set(off_rows)
            except sqlite3.Error:
                off_ids, off_rows = set(), {}
        contact_ids = None
        campaign = get("campaign")
        if campaign:
            try:
                with db_connect() as _c:
                    contact_ids = {str(m["contact_id"]) for m in
                                   pipeline_db.campaign_members(_c, int(campaign),
                                                                limit=5000)}
            except (ValueError, sqlite3.Error):
                contact_ids = set()

        def matches(r):
            if contact and str(r["contact_id"]) != contact:
                return False
            if off_ids is not None:
                is_off = str(r["contact_id"]) in off_ids
                if (outreach_state == "off") != is_off:
                    return False
            if contact_ids is not None and str(r["contact_id"]) not in contact_ids:
                return False
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
        # Viewing the excluded: include people who have no generated copy. They are
        # still contacts, still excluded, and still the ones you came here to find.
        if outreach_state == "off":
            have = {str(r["contact_id"]) for r in filtered}
            for cid, row in off_rows.items():
                if cid in have:
                    continue
                filtered.append({
                    "contact_id": cid,
                    "first_name": row.get("first_name") or "",
                    "last_name": row.get("last_name") or "",
                    "email": row.get("email") or "", "title": row.get("title") or "",
                    "company": row.get("company") or "", "signal": "",
                    "persona": row.get("persona") or "", "cta_type": "",
                    "status": row.get("status") or "", "batch_id": None,
                    "updated_at": "", "seq": None, "no_copy": True,
                })
            filtered.sort(key=lambda r: ((r.get("company") or "").strip() == "",
                                         (r.get("company") or "").lower(),
                                         (r.get("last_name") or "").lower()))

        start = (page - 1) * page_size
        items = filtered[start:start + page_size]

        # Which campaigns each of these people is in. Attached to the PAGE SLICE at
        # query time rather than baked into the index: membership changes constantly
        # while the copy does not, so an index rebuilt on file mtime would serve
        # stale campaign tags. One query for 50 rows is cheap.
        #
        # This is what makes the list contact-centric: the same person appears once,
        # carrying every campaign working them, so an overlap is visible here rather
        # than only from inside whichever campaign you happened to open.
        if items:
            try:
                ids = [str(r["contact_id"]) for r in items]
                with db_connect() as conn:
                    tags = pipeline_db.contact_campaign_tags(conn, ids)
                    # Whether outreach is switched on for this person. Same field the
                    # enroll gate reads, so the toggle on this list is the real
                    # thing rather than a display state.
                    ph = ",".join("?" * len(ids))
                    eng = {str(x["contact_id"]): dict(x) for x in conn.execute(
                        f"SELECT contact_id, engagement_state, paused_until, "
                        f"engagement_note FROM contacts WHERE contact_id IN ({ph})",
                        ids)}
                for r in items:
                    r["campaigns"] = tags.get(str(r["contact_id"]), [])
                    r["overlapping"] = len(r["campaigns"]) > 1
                    e = eng.get(str(r["contact_id"])) or {}
                    r["engagement_state"] = e.get("engagement_state") or "active"
                    r["paused_until"] = e.get("paused_until")
                    r["engagement_note"] = e.get("engagement_note")
            except sqlite3.Error:
                for r in items:
                    r["campaigns"], r["overlapping"] = [], False
                    r["engagement_state"] = "active"

        return {
            "total": len(filtered),
            "page": page,
            "page_size": page_size,
            "facets": facets,
            "groups": groups,
            "items": items,
        }


# The live index. Writers and the startup build always use this one directly;
# reads go through index() so a demo request sees its own profile's index.
INDEX = OutreachIndex()
_INDEXES = {None: INDEX}
_INDEXES_LOCK = threading.Lock()


def index():
    """The outreach index for this request's demo profile (live by default)."""
    profile = demo_mode.active()
    if profile is None:
        return INDEX
    with _INDEXES_LOCK:
        idx = _INDEXES.get(profile)
        if idx is None:
            idx = _INDEXES[profile] = OutreachIndex(profile)
    return idx


def outreach_detail(contact_id):
    fp = R(GEN_DIR) / f"{contact_id}.json"
    if not fp.is_file():
        return None
    try:
        asset = json.loads(fp.read_text())
    except (json.JSONDecodeError, OSError):
        return None
    jm = index()._load_contacts_jsonl().get(str(contact_id), {})
    dbm = db_contact_meta().get(str(contact_id), {})
    # Every campaign this person is in, not just the one you came from. Membership
    # lives on the PERSON: a contact worked by three campaigns looks unrelated on
    # each screen unless all three travel with them, and nobody notices they are
    # being triple-touched. Never fatal — copy must render without the campaign
    # tables (an older profile, a stale volume).
    campaigns, engagement = [], None
    try:
        with db_connect() as conn:
            campaigns = pipeline_db.contact_campaign_tags(
                conn, [str(contact_id)]).get(str(contact_id), [])
            row = conn.execute(
                "SELECT engagement_state, paused_until, engagement_note "
                "FROM contacts WHERE contact_id=?", (str(contact_id),)).fetchone()
            if row and (row["engagement_state"] or "active") != "active":
                engagement = dict(row)
    except sqlite3.Error:
        pass

    def meta(k):  # contacts.jsonl first, DB fallback (sourced contacts are DB-only)
        return jm.get(k) or dbm.get(k) or ""
    return {
        "campaigns": campaigns,
        "engagement": engagement,
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
        "sequence": _message_states(str(contact_id), dbm.get("status", "")),
    }


def _message_states(contact_id, contact_status):
    """Per-message state for one contact: sent | staged | draft.

    Three states, because "not sent" hides a real distinction:
      draft  — copy exists but the contact was never enrolled, so nothing is queued
      staged — enrolled, so the step is queued in Bison/HeyReach but hasn't gone yet
      sent   — a logged outbound touch on that channel covers this step
    Step-level attribution is inferred from the COUNT of logged touches (sends run in
    template order); `inferred: true` says so rather than implying per-message proof.
    """
    prog = db_sequence_progress().get(contact_id) or {}
    email_sent = int(prog.get("email_sent") or 0)
    li_sent = int(prog.get("li_sent") or 0)
    enrolled = contact_status == "enrolled"

    def state(n, sent_count):
        if n <= sent_count:
            return "sent"
        return "staged" if enrolled else "draft"

    return {
        "enrolled": enrolled,
        "replied": bool(prog.get("replied")),
        "last_sent_at": prog.get("last_sent_at"),
        "inferred": True,
        "email": [{"step": n, "key": f"body{n}", "state": state(n, email_sent)}
                  for n in range(1, EMAIL_STEPS + 1)],
        "linkedin": [{"step": n, "key": k, "state": state(n, li_sent)}
                     for n, k in enumerate(("li_connect", "li_msg1", "li_msg2"), start=1)],
        "email_sent": email_sent, "email_total": EMAIL_STEPS,
        "li_sent": li_sent, "li_total": LINKEDIN_STEPS,
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
    campaigns = read_jsonl(R(CAMPAIGN_STATS) / "campaigns.jsonl")
    steps = read_jsonl(R(CAMPAIGN_STATS) / "step_stats.jsonl")
    last_run = {}
    lr = R(CAMPAIGN_STATS) / "last_run.json"
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
    page never breaks on a HeyReach hiccup.

    In demo mode this comes from the profile's linkedin.json fixture — never from
    the live HeyReach account, which a demo must not touch or reveal."""
    if demo_mode.is_demo():
        fx = _read_json(R(DATA / "linkedin.json"))
        return fx if fx else {"configured": False}
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


def _ingest_limit(body):
    """How many new contacts a pull may add. None = the whole list ("Maximum").

    Absent, null, 0, "max"/"all" and anything unparseable all mean uncapped — a
    malformed cap must not silently pull one contact and read as an empty list."""
    raw = body.get("limit")
    if raw is None or (isinstance(raw, str) and raw.strip().lower() in
                       ("", "max", "maximum", "all")):
        return None
    try:
        n = int(raw)
    except (TypeError, ValueError):
        return None
    return n if n > 0 else None


def do_ingest(list_id, limit=None):
    """Pull a HubSpot list into the pipeline and batch it.

    `limit` caps how many NEW contacts the pull adds; None means the whole list
    (what the Source tab calls "Maximum"). The cap lives in hubspot_pull.py so the
    CLI and the console agree on what a limit counts."""
    pre = db_status()
    args = [str(HUBSPOT_PULL), str(list_id)]
    if limit:
        args += ["--limit", str(int(limit))]
    pull = run_script(args, timeout=600)
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
    # How many more the list still holds under a cap, so "run it again" is an
    # informed choice rather than a guess. Absent when the pull was uncapped.
    held = re.search(r"held back (\d+) more that qualify", pull["stdout"])
    return {
        "ok": True,
        "pull": pull, "init": init,
        "new_contacts": new_contacts, "new_batches": new_batches,
        "limit": limit,
        "remaining_in_crm": int(held.group(1)) if held else None,
        "pending_batches": db_batches(status="pending")["batches"],
        "status": post,
    }


def do_hubspot_lists(query, list_type=None):
    """Search HubSpot lists by name. list_type in {contact, company} or None (both).

    In demo mode this is served from the profile's hubspot_lists.json: shelling out
    would hit the real portal (which a demo must not touch) and, with no token, dump
    the script's traceback straight into the Use view.
    """
    if demo_mode.is_demo():
        fx = _read_json(R(DATA / "hubspot_lists.json")) or {}
        rows = fx.get("lists") or []
        q = (query or "").strip().lower()
        if list_type in ("contact", "company"):
            want = "0-1" if list_type == "contact" else "0-2"
            rows = [r for r in rows if r.get("object_type_id") == want]
        if q:
            rows = [r for r in rows if q in (r.get("name") or "").lower()]
        return {"ok": True, "lists": rows}
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
    mirror so a restart doesn't blank the indicator until the next cycle.

    Profile-scoped in demo mode: the in-memory status belongs to the live process
    and its background threads, so surfacing it in a demo would show the host's
    HubSpot health (often a red "sync issue") inside a dataset that has nothing to
    do with HubSpot.
    """
    if demo_mode.is_demo():
        return _read_json(R(AUTOSYNC_STATUS_PATH)) or {}
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
    (same degradation contract as linkedin_analytics_payload).

    Attribution lives in Mongo, not under data/, so R() cannot scope it to a demo
    profile — and these are REAL deal names and amounts. Report it as absent in demo
    mode rather than letting live pipeline value show up under a banner that says
    everything on screen is synthetic.

    A demo profile may ship an aisdr.json fixture; it is served instead of touching
    Mongo, so the tiles tell the attribution story without exposing real deals."""
    if demo_mode.is_demo():
        fx = _read_json(R(DATA / "aisdr.json"))
        return fx if fx else {"configured": False}
    if not mongo_store.mongo_configured():
        return {"configured": False}
    try:
        return mongo_store.aisdr_analytics(mongo_store.get_db())
    except Exception as e:  # noqa: BLE001 — tiles show the error, page keeps working
        return {"configured": True, "error": f"{type(e).__name__}: {e}"[:200]}


def aisdr_sync_status_payload():
    """Sync-run state for the UI: is one running now + the last run's summary."""
    if demo_mode.is_demo():   # see aisdr_analytics_payload — served from the profile
        fx = _read_json(R(DATA / "aisdr_status.json"))
        return fx if fx else {"configured": False, "running": False,
                              "started_at": None, "last_result": None}
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
            # Declared, not probed, in demo mode — a profile represents a working
            # deployment, so the host's absent keys must not render as an
            # unconfigured safety gate. Same rule as _tech_status/_hiring_status.
            "channels": {
                "bison": {"configured": demo_mode.is_demo()
                          or bool(env.get("EMAILBISON_API_KEY"))},
                "heyreach": {"configured": demo_mode.is_demo()
                             or bool(env.get("HEYREACH_API_KEY"))},
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
    idx = index()
    idx.maybe_rebuild()
    by_email = {r["email"].lower(): r for r in idx.rows if r.get("email")}
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


def connectors_payload():
    """Setup → Connectors. Demo statuses are declared by the profile, never probed
    from the host, so a demo shows a deliberate configuration and never reveals
    which credentials the machine happens to hold."""
    profile = demo_mode.active()
    declared = None
    if profile:
        decl = _read_json(R(DATA / "connectors.json")) or {}
        if isinstance(decl.get("connected"), list):
            declared = decl["connected"]
    out = connectors.connectors_payload(read_env(), PROJECT_ROOT,
                                        demo_profile=profile, demo_connected=declared)
    # What each connector needs in order to be wired up from here. Field METADATA
    # and presence only — connector_store.describe never returns a secret, and in a
    # demo the store is not consulted at all, so a demo can neither read nor reveal
    # a real credential.
    env = {} if profile else read_env()
    for item in out["connectors"]:
        cid = item["id"]
        if not item["integrated"]:
            continue
        if connector_store.configurable(cid):
            item["fields"] = ([] if profile
                              else connector_store.describe(DATA, env, cid))
            item["configurable"] = True
        else:
            item["configurable"] = False
            item["connect_via"] = connector_store.NO_FIELDS.get(cid)
    out["writable"] = not profile
    out["storage_note"] = (
        "Credentials entered here are stored on the persistent volume "
        "(data/connectors) and take effect immediately, without a redeploy. They "
        "override the matching environment variable."
    )
    return out


def _home_deltas():
    """Week-over-week movement from the append-only campaign snapshots.

    Bison reports lifetime-to-date counts, so a delta needs two snapshots. Picks the
    newest snapshot and the most recent one at least 6 days older; returns None when
    there isn't enough history rather than inventing a comparison.
    """
    path = R(CAMPAIGN_STATS) / "campaigns_history.jsonl"
    if not path.is_file():
        return None
    by_snap = {}
    try:
        for line in path.open():
            if not line.strip():
                continue
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            at = r.get("fetched_at")
            if not at:
                continue
            agg = by_snap.setdefault(at, {"contacted": 0, "interested": 0, "replies": 0})
            agg["contacted"] += r.get("total_leads_contacted") or 0
            agg["interested"] += r.get("interested") or 0
            agg["replies"] += r.get("unique_replies") or 0
    except OSError:
        return None
    snaps = sorted(by_snap.items())
    if len(snaps) < 2:
        return None
    latest_at, latest = snaps[-1]
    # Walk back for a snapshot at least ~a week older; fall back to the previous one.
    prev_at, prev = snaps[-2]
    for at, agg in reversed(snaps[:-1]):
        if (latest_at[:10] > at[:10]) and (latest_at[:10][:7] != at[:10][:7]
                                           or int(latest_at[8:10]) - int(at[8:10]) >= 6):
            prev_at, prev = at, agg
            break
    return {
        "since": prev_at, "until": latest_at,
        "contacted": latest["contacted"] - prev["contacted"],
        "interested": latest["interested"] - prev["interested"],
        "replies": latest["replies"] - prev["replies"],
    }


def home_payload():
    """One assembled summary for the Home view.

    Deliberately one endpoint rather than eight parallel calls from the page: fewer
    round trips, deltas need cross-source maths anyway, and every section degrades
    independently — a broken source nulls its own widget instead of erroring Home.
    """
    out = {"sections": {}, "errors": {}}

    def section(name, fn):
        try:
            out["sections"][name] = fn()
        except Exception as e:  # noqa: BLE001 — Home must always render
            out["sections"][name] = None
            out["errors"][name] = f"{type(e).__name__}: {e}"[:160]

    # --- row 1: what the worker produced ---------------------------------
    def outcome():
        an = analytics_payload() or {}
        totals = an.get("totals") or {}
        ai = aisdr_analytics_payload() or {}
        return {
            "pipeline_attributed": ai.get("total_pipeline") if ai.get("configured") is not False else None,
            "deals_created": ai.get("deals_created") if ai.get("configured") is not False else None,
            "interested": totals.get("total_interested"),
            "contacted": totals.get("total_contacted"),
            "replies": totals.get("total_replies"),
            "leads": totals.get("total_leads"),
            "interested_rate_pct": totals.get("overall_interested_rate_pct"),
            "reply_rate_pct": totals.get("overall_reply_rate_pct"),
            "deltas": _home_deltas(),
            "last_synced": an.get("fetched_at"),
        }

    # --- row 2: what needs a human --------------------------------------
    def queue():
        items = []
        for path, channel in ((R(REVIEW_QUEUE), "email"), (R(LI_REVIEW_QUEUE), "linkedin")):
            q = _read_json(path) or {}
            for it in (q.get("items") or []):
                items.append({
                    "reply_id": it.get("reply_id"),
                    "channel": it.get("channel") or channel,
                    "from_name": it.get("from_name"),
                    "company": (it.get("from_email") or "").split("@")[-1],
                    "intent": (it.get("classifier") or {}).get("intent"),
                    "date_received": it.get("date_received"),
                })
        items.sort(key=lambda i: i.get("date_received") or "", reverse=True)

        prog = progress_payload() or {}
        pending_batches = sum(1 for b in (prog.get("active_batches") or [])
                              if b.get("status") == "pending")
        # Total matters as well as missing: "all accounts covered" and "no accounts
        # cached yet" are different states and must not render the same way.
        # Coverage alone says little once every account is scanned ("12/12" three
        # times over). What matters for the copy is how many accounts yielded a
        # USABLE HOOK: a researched news angle (email 1), non-zero sales roles
        # (email 2 opens on it), and a detected GTM stack (the no-disruption /
        # signal-activation plays). Accounts with none of the three fall back to
        # generic copy — that is the number worth acting on.
        #
        # Derived from the stored display lines, which have the same format whether
        # the row came from a live scan or a demo profile: hiring_signals reads
        # "14 open roles · 4 sales: SDR; AE" (the " sales:" only appears when the
        # subset is non-empty), and a failed/empty tech scan stores the literal
        # "No signals detected".
        sig = None
        try:
            with db_connect() as conn:
                row = conn.execute(
                    "SELECT COUNT(*) total,"
                    " SUM(signal IS NOT NULL AND TRIM(signal) != '') research,"
                    " SUM(tech_signals IS NOT NULL"
                    "     AND tech_signals != 'No signals detected') tech_hook,"
                    " SUM(hiring_signals LIKE '% sales:%') hiring_hook,"
                    " SUM(tech_signals IS NULL OR hiring_signals IS NULL) unscanned,"
                    " SUM((signal IS NULL OR TRIM(signal) = '')"
                    "     AND (tech_signals IS NULL"
                    "          OR tech_signals = 'No signals detected')"
                    "     AND hiring_signals NOT LIKE '% sales:%') no_hook"
                    " FROM account_signals").fetchone()
                sig = {k: (row[k] or 0) for k in
                       ("total", "research", "tech_hook", "hiring_hook",
                        "unscanned", "no_hook")}
                # The most recently researched accounts, with the contact count so
                # the widget can link straight to that account's slice of Outreach.
                # This is the story the coverage bars alone never told: WHICH
                # accounts we just learned something about.
                sig["recent"] = [{
                    "company": r["company_name"] or r["domain"],
                    "domain": r["domain"],
                    "signal": r["signal"],
                    "tech": None if r["tech_signals"] == "No signals detected"
                            else r["tech_signals"],
                    # Only a NON-EMPTY sales subset is a hook (same rule the copy
                    # uses) — "3 open roles · 0 sales" is not something to show.
                    "hiring": (r["hiring_signals"]
                               if (r["hiring_signals"] or "").find(" sales:") > -1
                               else None),
                    "checked_at": r["researched_at"],
                    "contacts": r["n_contacts"] or 0,
                } for r in conn.execute(
                    "SELECT s.domain, s.company_name, s.signal, s.tech_signals,"
                    "       s.hiring_signals, s.researched_at,"
                    "       (SELECT COUNT(*) FROM contacts c WHERE c.domain = s.domain)"
                    "         AS n_contacts"
                    " FROM account_signals s"
                    " WHERE s.signal IS NOT NULL AND TRIM(s.signal) != ''"
                    " ORDER BY COALESCE(s.researched_at, s.updated_at) DESC"
                    " LIMIT 8")]
                # Drill-down: the actual people at each of those accounts, best
                # first. A signal you cannot act on from where you read it is a
                # notification, not a work surface — so the widget carries the
                # contact, their score, their number and a link into the CRM.
                doms = [r["domain"] for r in sig["recent"]]
                if doms:
                    ph = ",".join("?" * len(doms))
                    by_dom = {}
                    # One row per CONTACT. A person in two campaigns has two
                    # membership rows, and joining straight through listed them
                    # twice — so the best-scoring membership is picked per contact.
                    for c in conn.execute(
                            "SELECT c.contact_id, c.first_name, c.last_name, c.title,"
                            "       c.domain, c.phone, c.mobile_phone, c.persona,"
                            "       MAX(m.priority_score) AS priority_score,"
                            "       MAX(m.score_band) AS score_band,"
                            "       MAX(m.state) AS state"
                            "  FROM contacts c"
                            "  LEFT JOIN campaign_members m ON m.contact_id = c.contact_id"
                            f" WHERE c.domain IN ({ph})"
                            "  GROUP BY c.contact_id"
                            "  ORDER BY priority_score IS NULL, priority_score DESC,"
                            "           c.last_name", doms):
                        by_dom.setdefault(c["domain"], [])
                        if len(by_dom[c["domain"]]) < 4:
                            by_dom[c["domain"]].append(dict(c))
                    for r in sig["recent"]:
                        r["people"] = by_dom.get(r["domain"], [])
        except sqlite3.Error:
            pass
        return {
            "replies_waiting": len(items),
            "replies_top": items[:3],
            "generated_ready": prog.get("generated_ready"),
            "pending_batches": pending_batches,
            "signals_missing": (sig or {}).get("unscanned"),
            "signals": sig,
        }

    # --- row 3: is it improving -----------------------------------------
    def trend():
        t = trends_payload() or {}
        conv = t.get("conversion") or {}
        rs = conv.get("rate_series") or {}
        points = [{"at": (p.get("fetched_at") or "")[:10],
                   "rate": p.get("window_interested_rate_pct")}
                  for p in (rs.get("points") or [])
                  if p.get("window_interested_rate_pct") is not None]
        # Per-step numbers, not a generated sentence: Home should be a miniature of
        # the Trends view, using the same figures the sequence chart plots.
        steps = [{"step": k,
                  "contacted": v.get("contacted"),
                  "interested": v.get("interested"),
                  "rate": v.get("interested_rate_pct")}
                 for k, v in sorted((conv.get("by_step") or {}).items(),
                                    key=lambda kv: str(kv[0]))]
        return {
            "available": len(points) >= 2,
            "points": points,
            "latest": points[-1]["rate"] if points else None,
            "previous": points[-2]["rate"] if len(points) >= 2 else None,
            "overall_rate": (conv.get("overall") or {}).get("interested_rate_pct"),
            "steps": steps,
            "total_interested": t.get("total_interested"),
        }

    # --- row 4: pipeline state ------------------------------------------
    def pipeline():
        st = db_status() or {}
        prog = progress_payload() or {}
        return {
            "contacts_by_status": st.get("contacts_by_status") or {},
            "total_contacts": st.get("total_contacts"),
            "by_persona": st.get("by_persona") or {},
            "batches_by_status": st.get("batches_by_status") or {},
            "active_batches": len(prog.get("active_batches") or []),
        }

    # --- row 5: only what actually needs attention -----------------------
    def attention():
        alerts = []
        try:
            conn_state = connectors_payload() or {}
            for cid in (conn_state.get("summary") or {}).get("needs_attention") or []:
                alerts.append({"level": "error", "link": "/diagram",
                               "text": f"{cid} needs reconnecting — the stored "
                                       "authorization can no longer be refreshed."})
        except Exception:  # noqa: BLE001
            pass
        try:
            un = unenrollment_status_payload() or {}
            for r in (un.get("rules") or []):
                c = r.get("counts") or {}
                if c.get("available") and (c.get("failed") or 0) > 0:
                    alerts.append({"level": "warn", "link": "/pipeline",
                                   "text": f"{c['failed']} suppression action(s) failed "
                                           f"on {r.get('name')} — they retry next sweep."})
            if un.get("enabled") is False:
                alerts.append({"level": "warn", "link": "/pipeline",
                               "text": "The suppression sweeper is disabled — flagged "
                                       "contacts are not being stopped automatically."})
        except Exception:  # noqa: BLE001
            pass
        try:
            if (system_status_payload() or {}).get("volume_suspect"):
                alerts.append({"level": "error", "link": "/diagram",
                               "text": "The data directory looks non-persistent — "
                                       "everything recorded here resets on redeploy."})
        except Exception:  # noqa: BLE001
            pass
        return {"alerts": alerts}

    # --- the call list: who to work next, strongest signal first -----------
    def campaigns():
        """Active campaigns + the top of the priority-ordered call list.

        The widget's one destination is the Use view's Campaigns tab. It answers
        "who do I call next", which is the only campaign question worth putting on
        a dashboard — everything else about a campaign belongs on the campaign."""
        with db_connect() as conn:
            cp = campaigns_api.campaigns_payload(conn)
            if not cp.get("available"):
                return None
            rows = cp.get("campaigns") or []
            active = [c for c in rows if c.get("status") == "active"]
            cl = campaigns_api.call_list_payload(conn, limit=6)
        waiting = sum((c.get("counts") or {}).get("by_state", {}).get("qualified", 0)
                      for c in rows)
        bands = {}
        for c in rows:
            for band, n in ((c.get("counts") or {}).get("by_band") or {}).items():
                bands[band] = bands.get(band, 0) + n
        return {
            "total": len(rows),
            "active": len(active),
            "waiting": waiting,          # qualified but not yet sent to
            "bands": bands,
            "signals_30d": cp.get("signal_counts") or {},
            "call_list": [{
                "contact_id": m.get("contact_id"),
                "name": f"{m.get('first_name') or ''} {m.get('last_name') or ''}".strip(),
                "title": m.get("title"),
                "company": m.get("company") or m.get("domain"),
                "persona": m.get("persona"),
                "score": m.get("priority_score"),
                "band": m.get("score_band"),
                "signal": (m.get("signal_snapshot") or {}).get("summary"),
                "campaign": m.get("campaign_name"),
            } for m in (cl.get("contacts") or [])],
        }

    # --- evergreen campaigns waiting on a decision -------------------------
    def reviews():
        """Evergreen cycles that have ended and are asking before they run again.

        On Home because it is the one campaign state where NOTHING happens until a
        human acts — an evergreen campaign sitting in review is silently not
        sending, and a queue nobody is shown is a queue nobody works. Returns None
        when empty so the widget disappears rather than sitting there empty."""
        with db_connect() as conn:
            rp = campaigns_api.reviews_payload(conn)
        items = rp.get("reviews") or []
        return {"reviews": items} if items else None

    section("outcome", outcome)
    section("queue", queue)
    section("trend", trend)
    section("pipeline", pipeline)
    section("campaigns", campaigns)
    section("reviews", reviews)
    section("attention", attention)
    return out


def campaign_outreach_payload(campaign_id, limit=500):
    """The written copy for this campaign's members.

    Lives here rather than in campaigns_api because it joins two things that only
    app.py holds: the campaign's membership and the generated-copy index.

    Members WITHOUT copy are included, not filtered out. "Which of these has the
    agent actually written to?" is the question this tab exists to answer, and a
    list that silently dropped the un-written ones would answer it wrongly by
    omission — it would look complete when it was partial."""
    try:
        with db_connect() as conn:
            members = pipeline_db.campaign_members(conn, campaign_id, limit=limit)
    except sqlite3.Error as e:
        return {"available": False, "error": str(e), "contacts": []}
    # maybe_rebuild(), not a bare .rows read: the index is lazy PER PROFILE, so the
    # first request in a demo (or after new copy lands) would otherwise see an empty
    # index and report every contact as un-written. `query()` does this for the
    # app-wide view; reading .rows directly skipped it.
    idx = index()
    idx.maybe_rebuild()
    written = {str(r["contact_id"]): r for r in idx.rows}

    # The SEQUENCE this person is actually on. A contact in two campaigns does not
    # get two cadences — touch_plan merges every campaign's steps into one
    # de-conflicted timeline (see the overlap note in campaigns.py), so "which
    # sequence is this?" has a different answer for them than for a single-campaign
    # contact, and the screen has to say which.
    #
    # Only computed for OVERLAPPING contacts: for everyone else the answer is just
    # this campaign's own step count, and touch_plan per row would be a query per
    # contact for an answer already known.
    try:
        with db_connect() as conn:
            own_steps = len(pipeline_db.get_steps(conn, campaign_id))
    except sqlite3.Error:
        own_steps = 0
    plans = {}
    overlapped = [m for m in members if m.get("overlapping")][:200]
    if overlapped:
        try:
            import campaigns as _camp
            with db_connect() as conn:
                for m in overlapped:
                    try:
                        plans[str(m["contact_id"])] = _camp.touch_plan(
                            conn, str(m["contact_id"]))
                    except Exception:  # noqa: BLE001 — one bad row must not blank the tab
                        pass
        except Exception:  # noqa: BLE001
            pass

    rows = []
    for m in members:
        cid = str(m["contact_id"])
        w = written.get(cid)
        tp = plans.get(cid)
        camps = m.get("all_campaigns") or []
        rows.append({
            "contact_id": cid,
            "name": f"{m.get('first_name') or ''} {m.get('last_name') or ''}".strip(),
            "title": m.get("title"), "company": m.get("company") or m.get("domain"),
            "persona": m.get("persona"), "state": m.get("state"),
            "priority_score": m.get("priority_score"), "score_band": m.get("score_band"),
            "has_copy": bool(w),
            "signal": (w or {}).get("signal") or (m.get("signal_snapshot") or {}).get("summary"),
            "cta_type": (w or {}).get("cta_type"),
            "seq": (w or {}).get("seq"),
            # Every campaign working this person, so an overlap is visible on the row
            # rather than only after opening them.
            "campaigns": camps,
            "campaign_count": len(camps) or 1,
            "sequence": {
                "merged": bool(tp and tp.get("overlapping")),
                "touches": len(tp["touches"]) if tp else own_steps,
                "campaigns": len(tp["campaigns"]) if tp else 1,
                "conflicts": tp.get("conflicts") if tp else 0,
                "span_days": tp.get("span_days") if tp else None,
            },
        })
    merged = sum(1 for r in rows if r["sequence"]["merged"])
    return {
        "available": True,
        "contacts": rows,
        "written": sum(1 for r in rows if r["has_copy"]),
        "total": len(rows),
        "merged": merged,
        "own_steps": own_steps,
    }


def campaign_excluded_payload(campaign_id, limit=300):
    """Who is out of this campaign, and why — with enough to put them back.

    Two different exclusions, kept apart because undoing them is different:

      LOCAL   dropped from THIS campaign ("not a fit here"). Restoring is a
              campaign-level action and affects nothing else.
      GLOBAL  outreach switched off for the person everywhere. They still MATCH —
              they show in the campaign's target count — they are simply never
              added. Restoring is a person-level decision.

    The whole point is that both are reversible and findable a year later, when the
    reason they were excluded may no longer hold.
    """
    out = {"available": True, "local": [], "global": []}
    try:
        with db_connect() as conn:
            for r in conn.execute(
                    "SELECT m.contact_id, m.outcome, m.note, m.updated_at, "
                    "       c.first_name, c.last_name, c.title, c.company, c.domain "
                    "FROM campaign_members m LEFT JOIN contacts c USING (contact_id) "
                    "WHERE m.campaign_id=? AND m.state='removed' "
                    "ORDER BY m.updated_at DESC LIMIT ?", (campaign_id, limit)):
                d = dict(r)
                d["name"] = f"{d.pop('first_name', '') or ''} {d.pop('last_name', '') or ''}".strip()
                out["local"].append(d)
            # Globally-off people who are IN this campaign, or would match it. The
            # membership ones are the actionable list; the rest surface through the
            # match preview's off_targets.
            for r in conn.execute(
                    "SELECT c.contact_id, c.first_name, c.last_name, c.title, "
                    "       c.company, c.domain, c.engagement_state, c.paused_until, "
                    "       c.engagement_note, c.engagement_updated_at "
                    "FROM contacts c JOIN campaign_members m USING (contact_id) "
                    "WHERE m.campaign_id=? AND c.engagement_state IS NOT NULL "
                    "  AND c.engagement_state != 'active' LIMIT ?", (campaign_id, limit)):
                d = dict(r)
                d["name"] = f"{d.pop('first_name', '') or ''} {d.pop('last_name', '') or ''}".strip()
                out["global"].append(d)
    except sqlite3.Error as e:
        return {"available": False, "error": str(e), "local": [], "global": []}
    return out


def campaign_replies_payload(campaign_id, limit=500):
    """Replies from this campaign's contacts.

    Matched on EMAIL, because a reply arrives from a mailbox and carries no campaign
    id — Bison's reply record knows the address, not which console campaign put the
    contact there. Matching on the member's email is the only join available, and it
    is exact: an address is either one of this campaign's members or it isn't.
    """
    try:
        with db_connect() as conn:
            members = pipeline_db.campaign_members(conn, campaign_id, limit=limit)
    except sqlite3.Error as e:
        return {"available": False, "error": str(e), "replies": []}
    by_email = {(m.get("email") or "").strip().lower(): m for m in members if m.get("email")}
    if not by_email:
        return {"available": True, "replies": [], "total": 0}

    queue = review_queue_payload() or {}
    drafts = {str(d.get("reply_id")): d
              for d in ((followup_drafts_payload() or {}).get("items") or [])}
    out = []
    for it in (queue.get("items") or []):
        addr = (it.get("from_email") or "").strip().lower()
        m = by_email.get(addr)
        if not m:
            continue
        d = drafts.get(str(it.get("reply_id"))) or {}
        out.append({
            "reply_id": it.get("reply_id"),
            "contact_id": m.get("contact_id"),
            "channel": it.get("channel") or "email",
            "from_name": it.get("from_name") or
                         f"{m.get('first_name') or ''} {m.get('last_name') or ''}".strip(),
            "from_email": it.get("from_email"),
            "company": m.get("company") or m.get("domain"),
            "intent": (it.get("classifier") or {}).get("intent"),
            "date_received": it.get("date_received"),
            "snippet": (it.get("body") or it.get("snippet") or "")[:400],
            "draft_status": d.get("status"),
        })
    out.sort(key=lambda r: r.get("date_received") or "", reverse=True)
    return {"available": True, "replies": out, "total": len(out),
            "members_with_email": len(by_email)}


def _demo_draft_followups(handler):
    """Serve the profile's pre-written follow-up drafts as if they were just drafted.

    Reads only `interested-replies/followup_drafts.json` from the active profile — no
    model call, no file write, no outbound request. This is what lets a demo show the
    agent drafting a reply, which is the moment the product is actually about.
    """
    payload = _read_json(R(FOLLOWUP_DRAFTS)) or {"items": []}
    items = payload.get("items") or []
    return {"ok": True, "drafted": len(items), "kept": 0,
            "stdout": f"drafted {len(items)} follow-ups from the demo profile",
            "returncode": 0}


# Regenerated demo drafts, held in PROCESS MEMORY only — keyed (profile, reply_id).
# The UI refetches the drafts file after a regenerate, and demo mode must not write,
# so the override is applied on read instead. Nothing persists past a restart, which
# is the right lifetime for a demo.
_DEMO_DRAFTS = {}
_DEMO_DRAFTS_LOCK = threading.Lock()


def _demo_regenerate_draft(handler):
    """Swap in the next pre-written alternate for one reply, so 'regenerate' shows a
    genuinely different message instead of appearing to do nothing."""
    body = handler._read_body()
    rid = str(body.get("reply_id") or "")
    profile = demo_mode.active()
    payload = _read_json(R(FOLLOWUP_DRAFTS)) or {}
    for it in (payload.get("items") or []):
        if str(it.get("reply_id")) != rid:
            continue
        variants = [it.get("draft") or ""] + list(it.get("demo_alternates") or [])
        variants = [v for v in variants if v]
        if not variants:
            return {"ok": False, "error": "that reply has no draft in this profile"}
        key = (profile, rid)
        with _DEMO_DRAFTS_LOCK:
            nxt = (_DEMO_DRAFTS.get(key, {}).get("idx", 0) + 1) % len(variants)
            _DEMO_DRAFTS[key] = {"idx": nxt, "draft": variants[nxt]}
        return {"ok": True, "reply_id": rid, "draft": variants[nxt],
                "agent": body.get("agent") or it.get("agent") or "standard",
                "intent": it.get("intent"), "regenerated": True}
    return {"ok": False, "error": "no draft for that reply in this demo profile"}


def _demo_suggest_step_copy(handler):
    """Demo answer for the campaign copy suggester.

    A demo has no ANTHROPIC_API_KEY, and "copy suggestions need an API key" is
    exactly the kind of not-configured notice a demo must never show — the product
    being demonstrated IS the agent writing copy. So the draft comes from the
    profile's own fixture and nothing is generated, called or written.

    Fixture: data/demo/<id>/campaign_copy.json
      {"suggestions": {"email:2": {"subject": "...", "body": "..."}},
       "default": {"subject": "...", "body": "..."}}
    """
    body = handler._read_body()
    step = body.get("step_no")
    channel = str(body.get("channel") or "email")
    fx = _read_json(R(DATA / "campaign_copy.json")) or {}
    key = f"{channel}:{step}"
    draft = (fx.get("suggestions") or {}).get(key) or fx.get("default")
    if not draft:
        return {"ok": False, "error": "no sample copy for that step in this demo profile"}
    return {"step_no": step, "channel": channel, "cta_key": body.get("cta_key"),
            "subject": draft.get("subject", ""), "body": draft.get("body", ""),
            "demo": True}


def _demo_describe_report(handler):
    """Demo answer for report-by-description.

    A demo has no ANTHROPIC_API_KEY, and "needs an API key" is exactly the
    not-configured notice a demo must never show. Matches the request against
    the profile's `report_recipes.json` by keyword and runs the matching SPEC —
    so the table shown is genuinely computed from the profile's data, not a
    canned screenshot. An unmatched request says so rather than inventing one."""
    body = handler._read_body()
    desc = str(body.get("description") or "").strip().lower()
    fx = _read_json(R(DATA / "report_recipes.json")) or {}
    best, score = None, 0
    for r in (fx.get("recipes") or []):
        hits = sum(1 for k in (r.get("keywords") or []) if k in desc)
        if hits > score:
            best, score = r, hits
    if not best:
        return {"ok": False, "error": (
            "This demo answers a set of example questions — try asking about hot "
            "contacts, signals by type, campaign performance, or credit spend.")}
    with db_connect() as conn:
        out = reports.run(conn, best["spec"])
    out["demo"] = True
    out["matched"] = best.get("label")
    return out


def _demo_campaign_brief(handler):
    """Demo answer for the campaign brief configurator.

    A demo has no ANTHROPIC_API_KEY, and an agent that configures a campaign from a
    meeting note is the product — "needs an API key" is the one notice a demo must
    never show there. So the profile answers it from `campaign_brief.json`:

      {"clarify": {...one question, with per-option config overlays...},
       "recipes": [{"keywords": [...], "summary": ..., "config": {...}, "notes": []}],
       "default": {"summary": ..., "config": {...}}}

    It behaves like the real thing in the way that matters: the FIRST call comes
    back with a clarifying question and only a partial configuration, and the
    answer to that question changes the rest of the form. Answering is what
    completes the config — a fixture that returned everything at once would demo a
    form-filler rather than a configurator.

    Every field still goes through campaign_brief.validate_config, so a fixture
    cannot put a value on the form that the create endpoint would then reject.
    """
    body = handler._read_body()
    text = " ".join([str(body.get("text") or "")]
                    + [str(a.get("name") or "") + " " + str(a.get("text") or "")[:2000]
                       for a in (body.get("attachments") or [])]).lower()
    answers = body.get("answers") or {}
    fx = _read_json(R(DATA / "campaign_brief.json")) or {}

    best, score = None, 0
    for r in (fx.get("recipes") or []):
        hits = sum(1 for k in (r.get("keywords") or []) if k in text)
        if hits > score:
            best, score = r, hits
    best = best or fx.get("default")
    if not best:
        return {"ok": False, "error": (
            "This demo configures a set of example campaigns — try describing a "
            "closed-lost re-engagement, a funding-and-hiring push, or a "
            "competitive-displacement campaign.")}

    raw = dict(best.get("config") or {})
    notes = list(best.get("notes") or [])
    clarify = best.get("clarify") or fx.get("clarify") or {}
    questions = []
    if clarify and clarify.get("id") not in answers:
        # Hold back the fields this question decides, so the form visibly completes
        # when it is answered rather than the answer being cosmetic.
        for k in (clarify.get("decides") or []):
            raw.pop(k, None)
        questions = [clarify]
    else:
        chosen = answers.get((clarify or {}).get("id"))
        for o in (clarify.get("options") or []):
            if o.get("label") == chosen:
                raw = _merge_config(raw, o.get("config") or {})
                if o.get("note"):
                    notes.append(o["note"])
                break

    conn = db_connect()
    try:
        config, warnings = campaign_brief.validate_config(raw, conn)
        qs = campaign_brief.validate_questions(questions, conn)
    finally:
        conn.close()
    return {
        "summary": best.get("summary", ""),
        "config": config, "questions": qs,
        "notes": notes[:6], "warnings": warnings, "demo": True,
    }


def _merge_config(base, overlay):
    """Overlay wins, one level deep so a partial signal_query does not wipe the rest."""
    out = dict(base)
    for k, v in (overlay or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = {**out[k], **v}
        else:
            out[k] = v
    return out


# POST paths a demo may answer from its own fixtures (see the guard in do_POST).
# Everything irreversible is deliberately absent.
_DEMO_FIXTURE_POSTS = {
    "/api/replies/followup/draft": _demo_draft_followups,
    "/api/replies/followup/regenerate": _demo_regenerate_draft,
    "/api/campaigns/brief": _demo_campaign_brief,
}

# Same contract, for paths that carry an id. Checked after the exact map.
_DEMO_FIXTURE_POST_PATTERNS = [
    (re.compile(r"^/api/campaigns/\d+/suggest$"), _demo_suggest_step_copy),
]


def _demo_fixture_handler(path):
    fn = _DEMO_FIXTURE_POSTS.get(path)
    if fn:
        return fn
    for rx, handler in _DEMO_FIXTURE_POST_PATTERNS:
        if rx.match(path):
            return handler
    return None


def demo_profiles_payload():
    """Demo profiles available on disk, for the sidebar switcher.

    Unauthenticated-safe in content (labels only), but still behind the auth gate
    like every other read. `active` is whatever this request asked for, echoed back
    so the client can detect a profile that vanished under it.
    """
    return {
        "profiles": demo_mode.list_profiles(DATA),
        "active": demo_mode.active(),
        "areas": list(demo_mode.KNOWN_AREAS),
    }


def trends_payload():
    """Trends artifacts, from live data or the active demo profile (see R())."""
    src = R(TRENDS_DIR)
    summary = _read_json(src / "summary.json")
    conversion = _read_json(src / "conversion.json")
    cohorts = _read_json(src / "cohorts.json")
    last_run = _read_json(R(REPLIES_LAST_RUN)) or {}
    return {
        "available": summary is not None,
        "demo": demo_mode.active(),
        # Planted-effect documentation, present only in synthetic profiles.
        "ground_truth": _read_json(src / "ground_truth.json"),
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
    email = _read_json(R(REVIEW_QUEUE)) or {}
    li = _read_json(R(LI_REVIEW_QUEUE)) or {}
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
    path = R(FOLLOWUP_DRAFTS)          # profile-scoped: demo must not read live drafts
    payload = _read_json(path) or {"items": []}
    payload["available"] = path.is_file()
    profile = demo_mode.active()
    if profile:
        with _DEMO_DRAFTS_LOCK:
            overrides = {rid: v["draft"] for (prof, rid), v in _DEMO_DRAFTS.items()
                         if prof == profile}
        for it in (payload.get("items") or []):
            alt = overrides.get(str(it.get("reply_id")))
            if alt:
                it["draft"] = alt
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
    server must come up without dnspython installed.

    In demo mode capability is DECLARED, not probed. A demo profile represents a
    fully-configured deployment, so it must not report the host's missing optional
    dependencies or absent API keys as broken features."""
    if demo_mode.is_demo():
        return True, None
    try:
        import tech_signals as T  # noqa: E402  (PIPELINE_SCRIPTS is on sys.path)
        return T.tech_available()
    except Exception as e:  # noqa: BLE001
        return False, f"tech_signals unavailable: {e}"


def _hiring_status():
    """(available, reason) for hiring detection. Same degrade contract as
    _tech_status — without PROSPEO_API_KEY the feature reports unavailable and
    the server keeps running. Declared, not probed, in demo mode (see _tech_status)."""
    if demo_mode.is_demo():
        return True, None
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

    def _demo_header(self):
        """The requested demo profile id, or None. Untrusted client input."""
        v = (self.headers.get("X-Demo-Profile") or "").strip()
        return v or None

    def _enter_demo_scope(self):
        """Bind this request's thread to the requested demo profile.

        False (and no binding) if the client named a profile that doesn't exist —
        better a clear 400 than silently serving live data to something that
        believes it is in a demo. Always paired with demo_mode.clear() in a finally.
        """
        requested = self._demo_header()
        demo_mode.clear()
        if requested is None:
            return True
        if not demo_mode.profile_exists(DATA, requested):
            # The profile listing must never fail on a stale id: it is the very
            # call the client uses to discover its selection is gone and fall back
            # to live. 400-ing it would leave the console permanently wedged on a
            # deleted profile with no way back through the UI.
            return urlparse(self.path).path == "/api/demo/profiles"
        demo_mode.set_active(requested)
        return True

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
        if not self._enter_demo_scope():
            return self._error(400, "unknown demo profile")
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
                return self._json(orchestration_config.orchestration_config_payload(
                    demo=demo_mode.is_demo()))
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
            if path == "/api/demo/profiles":
                return self._json(demo_profiles_payload())
            if path == "/api/connectors":
                return self._json(connectors_payload())
            if path == "/api/home":
                return self._json(home_payload())
            if path == "/api/config/scopes":
                return self._json(config_edit.scopes_payload(PROJECT_ROOT, CONFIG_HISTORY))
            if path == "/api/config/file":
                scope = (params.get("scope", [""])[0] or "").strip()
                if scope not in config_edit.SCOPES:
                    return self._error(404, "unknown config scope")
                return self._json({"scope": scope,
                                   "files": config_edit.read_scope(PROJECT_ROOT, scope)})
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
                d = R(SIGNAL_PLAYS_DIR) / slug
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
            if path == "/api/signals/events":
                days = (params.get("days", ["90"])[0] or "90")
                with db_connect() as conn:
                    return self._json(campaigns_api.signal_events_payload(
                        conn, days=int(days) if days.isdigit() else 90))
            if path == "/api/campaigns":
                with db_connect() as conn:
                    return self._json(campaigns_api.campaigns_payload(conn))
            if path == "/api/campaigns/calllist":
                cid = (params.get("campaign_id", [""])[0] or "").strip()
                limit = (params.get("limit", ["100"])[0] or "100")
                # 'all' is the every-state sentinel. It can't be spelled as an empty
                # value: parse_qs drops blanks, so `state=` arrives indistinguishable
                # from an absent param and would silently fall back to 'qualified'.
                state = params.get("state", ["qualified"])[0]
                # Clamped: the client can ask for the whole list so its sort covers
                # everything, but not for an unbounded response.
                with db_connect() as conn:
                    return self._json(campaigns_api.call_list_payload(
                        conn, campaign_id=int(cid) if cid.isdigit() else None,
                        limit=min(int(limit), 5000) if limit.isdigit() else 100,
                        state="" if state == "all" else state))
            if path.startswith("/api/campaigns/discover/status/"):
                job_id = path[len("/api/campaigns/discover/status/"):]
                try:
                    return self._json(campaigns_api.discover_status(job_id))
                except LookupError as e:
                    return self._error(404, str(e))
            if path.startswith("/api/campaigns/enrich/status/"):
                job_id = path[len("/api/campaigns/enrich/status/"):]
                try:
                    return self._json(campaigns_api.enrich_status(job_id))
                except LookupError as e:
                    return self._error(404, str(e))
            if path == "/api/campaigns/audiences":
                return self._json(campaigns_api.audiences_payload())
            if path == "/api/campaigns/imports":
                return self._json(campaigns_api.imports_payload())
            if path == "/api/signals/definitions":
                with db_connect() as conn:
                    return self._json(campaigns_api.signal_defs_payload(conn))
            if path == "/api/references":
                with db_connect() as conn:
                    out = campaigns_api.references_payload(conn)
                # Where content can come from, from the SAME inventory Setup renders,
                # so the two surfaces cannot disagree about what is connected.
                profile = demo_mode.active()
                declared = None
                if profile:
                    decl = _read_json(R(DATA / "connectors.json")) or {}
                    if isinstance(decl.get("connected"), list):
                        declared = decl["connected"]
                out["repositories"] = connectors.content_repositories(
                    {} if profile else read_env(), PROJECT_ROOT,
                    demo_profile=profile, demo_connected=declared)
                return self._json(out)
            if path.startswith("/api/campaigns/") and path.endswith("/outreach"):
                cid = path[len("/api/campaigns/"):-len("/outreach")]
                if cid.isdigit():
                    return self._json(campaign_outreach_payload(int(cid)))
            if path.startswith("/api/campaigns/") and path.endswith("/replies"):
                cid = path[len("/api/campaigns/"):-len("/replies")]
                if cid.isdigit():
                    return self._json(campaign_replies_payload(int(cid)))
            if path.startswith("/api/campaigns/") and path.endswith("/excluded"):
                cid = path[len("/api/campaigns/"):-len("/excluded")]
                if cid.isdigit():
                    return self._json(campaign_excluded_payload(int(cid)))
            if path == "/api/campaigns/reviews":
                with db_connect() as conn:
                    return self._json(campaigns_api.reviews_payload(conn))
            if path == "/api/campaigns/hotlist":
                # R() so a demo profile serves its own snapshot, not live targets.
                with db_connect() as conn:
                    return self._json(campaigns_api.hot_list_payload(
                        conn, R(HOT_LIST_PATH)))
            if path == "/api/capacity":
                days = (params.get("days", ["30"])[0] or "30")
                with db_connect() as conn:
                    return self._json(campaigns_api.capacity_payload(
                        conn, days=int(days) if days.isdigit() else 30))
            if path == "/api/crm/fields":
                with db_connect() as conn:
                    return self._json(campaigns_api.crm_fields_payload(conn))
            if path == "/api/reports/schema":
                return self._json(reports.schema_payload())
            if path == "/api/tiers":
                # Static packaging registry — which features are separately-sold
                # agents/add-ons, for the tier badges. CRM linking rides along
                # because both are app-wide config the client needs exactly once,
                # and a second app-boot request for one field isn't worth it.
                return self._json({**tiers.payload(), "crm": crm_link_config()})
            if path == "/api/buyer-group":
                with db_connect() as conn:
                    return self._json(campaigns_api.buyer_group_payload(conn))
            if path == "/api/analytics/campaigns":
                with db_connect() as conn:
                    return self._json(campaigns_api.campaign_analytics_payload(conn))
            if path == "/api/analytics/funnel":
                # Joined to the Bison snapshot so the funnel spans our own stages
                # AND the send-side ones in a single chain.
                with db_connect() as conn:
                    return self._json(campaigns_api.funnel_payload(
                        conn, analytics=analytics_payload()))
            if path.startswith("/api/campaigns/"):
                rest = path[len("/api/campaigns/"):]
                if rest.isdigit():
                    with db_connect() as conn:
                        payload = campaigns_api.campaign_detail_payload(conn, int(rest))
                    if payload.get("error") == "not found":
                        return self._error(404, f"no campaign {rest}")
                    return self._json(payload)
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
                return self._json(index().query(params))
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
        finally:
            demo_mode.clear()

    def _demo_ingest(self):
        """Simulated 'pull a list from HubSpot' — reads the profile's own CRM pool.

        Same response shape as do_ingest() so the Source tab is byte-identical in a
        demo; nothing leaves the process and nothing lands outside the profile."""
        body = self._read_body()
        return demo_actions.simulate_crm_pull(
            _demo_dir(), _demo_db(), str(body.get("list_id", "")).strip(),
            limit=_ingest_limit(body))

    def _signals_post(self, path):
        """Signal definitions — what counts as a signal in this deployment.

        Configuration, not code (see crm_signals.py). Preview writes nothing; save
        validates the rule before it can ever run, so a definition that reaches the
        table is one the evaluator will accept."""
        try:
            if path == "/api/signals/definitions":
                return self._json(campaigns_api.save_signal_def(self._read_body()))
            if path == "/api/signals/definitions/preview":
                return self._json(campaigns_api.preview_signal_rule(self._read_body()))
            rest = path[len("/api/signals/definitions/"):]
            kind, _, action = rest.partition("/")
            if action == "delete":
                return self._json(campaigns_api.delete_signal_def(kind))
            if action == "run":
                return self._json(campaigns_api.run_signal_rule(kind, self._read_body()))
            return self._error(404, f"no signal route {path}")
        except LookupError as e:
            return self._error(404, str(e))
        except ValueError as e:
            return self._error(400, str(e))

    def _campaigns_post(self, path):
        """Dispatch the campaign write surface.

        POST /api/campaigns                    create (seeds the default cadence)
        POST /api/campaigns/<id>               patch the definition
        POST /api/campaigns/<id>/delete
        POST /api/campaigns/<id>/steps         upsert one sequence step
        POST /api/campaigns/<id>/steps/delete
        POST /api/campaigns/<id>/qualify       {dry_run?} apply the signal query
        POST /api/campaigns/<id>/suggest       draft copy for one step
        POST /api/campaigns/brief              configure a campaign from a description
        """
        body = self._read_body()
        rest = path[len("/api/campaigns"):].strip("/")
        try:
            if rest == "audience/preview":
                return self._json(campaigns_api.audience_preview(body))
            if rest == "brief":
                return self._json(campaigns_api.brief(body, PROJECT_ROOT))
            if rest == "audience/upload":
                return self._json(campaigns_api.import_preview(body, PROJECT_ROOT))
            if rest == "audience/import":
                return self._json(campaigns_api.import_commit(
                    body, PROJECT_ROOT, SCRIPTS / "sdr-pipeline" / "scripts"))
            if rest == "hotlist/refresh":
                return self._json(campaigns_api.refresh_hot_list())
            if not rest:
                return self._json(campaigns_api.create_campaign(body))
            head, _, action = rest.partition("/")
            if not head.isdigit():
                return self._error(404, f"no campaign route {path}")
            cid = int(head)
            if not action:
                return self._json(campaigns_api.update_campaign(cid, body))
            if action == "delete":
                return self._json(campaigns_api.delete_campaign(cid))
            if action == "steps":
                return self._json(campaigns_api.upsert_step(cid, body))
            if action == "steps/delete":
                return self._json(campaigns_api.delete_step(cid, body))
            if action == "qualify":
                return self._json(campaigns_api.qualify(cid, body))
            if action == "discover":
                return self._json(campaigns_api.discover(cid, body))
            if action == "enrich":
                return self._json(campaigns_api.enrich(cid, body))
            if action == "rescore":
                return self._json(campaigns_api.rescore(cid, body))
            if action == "relaunch":
                return self._json(campaigns_api.relaunch(cid, body))
            if action == "suggest":
                return self._json(campaigns_api.suggest_step_copy(cid, body, PROJECT_ROOT))
            return self._error(404, f"no campaign route {path}")
        except campaigns_api.Discovering as e:
            return self._error(409, str(e))
        except LookupError as e:
            return self._error(404, str(e))
        except ValueError as e:
            return self._error(400, str(e))
        except RuntimeError as e:
            # no API key / model returned nothing usable — same contract as
            # /api/config/propose, which 501s when the key is absent
            msg = str(e)
            return self._error(501 if "ANTHROPIC_API_KEY" in msg else 502, msg)

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path
        # Auth gate (see do_GET). /api/login and the HeyReach webhook are exempt.
        if (path.startswith("/api/") and path not in _EXEMPT_POST
                and not verify_token(bearer_from_headers(self.headers))):
            return self._error(401, "authentication required")
        # Demo mode is read-only. Every POST here either mutates local state or
        # reaches OUT — enrolling leads, writing to HubSpot, sending via
        # Bison/HeyReach. Refusing the whole verb is the guarantee that a demo can
        # never touch a real prospect.
        #
        # The one carve-out: a short allowlist of actions a demo answers ENTIRELY
        # FROM ITS OWN FIXTURES. These are intercepted here, before any dispatch, so
        # no script runs, nothing is written and no external service is called — the
        # handler can only read the profile. Anything irreversible (approve/send,
        # enroll, sync, config apply) stays refused.
        # Demo mode: A DEMO MAY WRITE TO ITS OWN DATASET AND NOTHING ELSE.
        #
        # This used to refuse every POST. That was the right instinct expressed as
        # the wrong invariant — it also made the demo unable to show the product's
        # central act, building and running a campaign. What must never happen is an
        # OUTWARD EFFECT: mailing a prospect, writing to the customer's CRM, spending
        # a credit, calling the model. Writing to the profile's own synthetic sqlite
        # has none of those properties.
        #
        # Two allowlists, both exact-match or regex so a new sibling endpoint is
        # refused by default: `_demo_fixture_handler` answers purely from fixtures,
        # `demo_mode.writable` permits a write scoped to the profile's own DB (with
        # simulated stand-ins for HubSpot/Clay/Prospeo — see demo_actions.py).
        if path.startswith("/api/") and path != "/api/login" and self._demo_header():
            _fixture = _demo_fixture_handler(path)
            if _fixture and self._enter_demo_scope():
                try:
                    return self._json(_fixture(self))
                finally:
                    demo_mode.clear()
            if demo_mode.writable(path) and self._enter_demo_scope():
                try:
                    if path == "/api/ingest":
                        return self._json(self._demo_ingest())
                    if path == "/api/buyer-group":
                        return self._json(campaigns_api.update_buyer_group(self._read_body()))
                    if path == "/api/reports/run":
                        # SELECT-only against the profile's own DB.
                        with db_connect() as conn:
                            return self._json(reports.run(conn, self._read_body().get("spec")))
                    if path == "/api/reports/describe":
                        return self._json(_demo_describe_report(self))
                    # Signal definitions are local config; the rules read the
                    # profile's own contacts and its simulated CRM.
                    if path.startswith("/api/signals/definitions"):
                        return self._signals_post(path)
                    if path == "/api/references":
                        return self._json(campaigns_api.save_reference(self._read_body()))
                    if path == "/api/references/attach":
                        return self._json(campaigns_api.set_cta_reference(self._read_body()))
                    if path == "/api/calllist/member":
                        return self._json(campaigns_api.update_member(self._read_body()))
                    if path == "/api/calllist/engagement":
                        return self._json(campaigns_api.update_engagement(self._read_body()))
                    return self._campaigns_post(path)
                except campaigns_api.Discovering as e:
                    return self._error(409, str(e))
                except LookupError as e:
                    return self._error(404, str(e))
                except ValueError as e:
                    return self._error(400, str(e))
                finally:
                    demo_mode.clear()
            return self._error(
                409, "not available in demo mode — this would reach a real prospect, "
                     "CRM or paid service. Switch back to live data to run it.")
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
                return self._json(do_ingest(list_id, _ingest_limit(body)))
            if path == "/api/analytics/refresh":
                return self._json(do_refresh())
            # -- customer proof attached to offers ---------------------------
            if path == "/api/references":
                return self._json(campaigns_api.save_reference(self._read_body()))
            if path == "/api/references/attach":
                return self._json(campaigns_api.set_cta_reference(self._read_body()))
            # -- working the call list ---------------------------------------
            # Campaign-level vs person-level, kept as separate endpoints because
            # they are separate decisions with different blast radii.
            if path == "/api/calllist/member":
                return self._json(campaigns_api.update_member(self._read_body()))
            if path == "/api/calllist/engagement":
                return self._json(campaigns_api.update_engagement(self._read_body()))
            # -- signal definitions: what counts as a signal here ------------
            if path.startswith("/api/signals/definitions"):
                return self._signals_post(path)
            # -- connectors: wire a system up from Setup --------------------
            # Writes land on the volume and take effect in-process immediately.
            # Refused in demo mode by the blanket POST guard above, which is the
            # point: a demo must never learn or store a real credential.
            if path.startswith("/api/connectors/"):
                rest = path[len("/api/connectors/"):]
                cid, _, action = rest.partition("/")
                if not connectors.is_integrated(cid):
                    return self._error(404, f"unknown connector {cid}")
                if action == "test":
                    return self._json(connectors.test_connection(
                        cid, read_env(), PROJECT_ROOT))
                if not connector_store.configurable(cid):
                    return self._error(400, f"{cid} is not configured with keys")
                body = self._read_body()
                if action == "disconnect":
                    keys = [f["key"] for f in connector_store.FIELDS[cid]]
                    connector_store.clear(DATA, keys)
                    return self._json({"ok": True, "cleared": keys})
                if action:
                    return self._error(404, f"no connector route {path}")
                allowed = {f["key"] for f in connector_store.FIELDS[cid]}
                values = {k: v for k, v in (body.get("values") or {}).items()
                          if k in allowed}
                if not values:
                    return self._error(400, "no recognised fields for this connector")
                written = connector_store.save(DATA, values)
                # Verify straight away: a saved key that doesn't work looks exactly
                # like one that does, and finding out later is the whole problem
                # this screen exists to fix.
                test = connectors.test_connection(cid, read_env(), PROJECT_ROOT)
                return self._json({"ok": True, "saved": written, "test": test})
            # -- campaigns ------------------------------------------------
            # All campaign writes land here; demo mode's blanket POST guard above
            # has already refused them, so a demo can never create or launch one.
            if path == "/api/campaigns" or path.startswith("/api/campaigns/"):
                return self._campaigns_post(path)
            # -- CRM field wiring ------------------------------------------
            # The CRM is the source of truth: push writes what we compute, pull
            # reads the CRM value back as authoritative.
            if path == "/api/crm/fields":
                try:
                    return self._json(campaigns_api.update_crm_field(self._read_body()))
                except LookupError as e:
                    return self._error(404, str(e))
                except ValueError as e:
                    return self._error(400, str(e))
            if path == "/api/buyer-group":
                try:
                    return self._json(campaigns_api.update_buyer_group(self._read_body()))
                except LookupError as e:
                    return self._error(404, str(e))
                except ValueError as e:
                    return self._error(400, str(e))
            if path == "/api/reports/run":
                with db_connect() as conn:
                    return self._json(reports.run(conn, self._read_body().get("spec")))
            if path == "/api/reports/describe":
                body = self._read_body()
                try:
                    spec = reports.describe(body.get("description"), PROJECT_ROOT)
                except reports.SpecError as e:
                    return self._error(400, str(e))
                except RuntimeError as e:
                    msg = str(e)
                    return self._error(501 if "ANTHROPIC_API_KEY" in msg else 502, msg)
                with db_connect() as conn:
                    return self._json(reports.run(conn, spec))
            if path == "/api/crm/sync":
                try:
                    return self._json(campaigns_api.crm_sync_run(self._read_body()))
                except ValueError as e:
                    return self._error(400, str(e))
            if path == "/api/enroll/dry-run":
                return self._json(do_enroll(live=False))
            if path == "/api/enroll/live":
                body = self._read_body()
                if body.get("confirm") is not True:
                    return self._error(400, "live enrollment requires confirm=true")
                return self._json(do_enroll(live=True))
            if path == "/api/config/propose":
                # Read-only against the repo: computes a diff, writes nothing.
                body = self._read_body()
                try:
                    prop = config_edit.propose(
                        PROJECT_ROOT,
                        str(body.get("scope") or ""),
                        str(body.get("instruction") or ""),
                        body.get("attachments") or [])
                except ValueError as e:
                    return self._error(400, str(e))
                except RuntimeError as e:
                    # No API key / unusable model output — not the caller's fault.
                    return self._error(501, str(e))
                return self._json(prop)
            if path == "/api/config/apply":
                body = self._read_body()
                try:
                    return self._json(config_edit.apply_proposal(
                        PROJECT_ROOT, CONFIG_HISTORY,
                        str(body.get("proposal_id") or ""),
                        actor=verify_token(bearer_from_headers(self.headers))))
                except ValueError as e:
                    return self._error(400, str(e))
            if path == "/api/config/revert":
                body = self._read_body()
                try:
                    return self._json(config_edit.revert(
                        PROJECT_ROOT, CONFIG_HISTORY,
                        str(body.get("entry_id") or ""),
                        actor=verify_token(bearer_from_headers(self.headers))))
                except ValueError as e:
                    return self._error(400, str(e))
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


def _campaign_sweep_loop():
    """Re-qualify active ROLLING campaigns every CAMPAIGN_SWEEP_MINUTES (default 60).

    This is what makes membership rolling rather than a list frozen at launch: an
    account that first shows signal on day 9 of a 30-day window joins on day 9. The
    sweep also closes campaigns whose window has passed, so the console and the
    sweeper agree about which are still live.

    Local only — it writes campaign_members and never calls out to HubSpot, Bison or
    HeyReach, so it is safe to run unattended (enrollment stays a separate,
    explicitly triggered step). Disable with CAMPAIGN_SWEEP_ENABLED=0.

    Deliberately reads no demo profile: background threads always act on live data
    (see demo_mode's module docstring)."""
    env = read_env()
    if (env.get("CAMPAIGN_SWEEP_ENABLED", "1") or "1").strip().lower() in ("0", "false", "no"):
        print("[campaigns] rolling sweep disabled (CAMPAIGN_SWEEP_ENABLED=0)", flush=True)
        return
    try:
        interval = max(5, int(env.get("CAMPAIGN_SWEEP_MINUTES", "60") or 60)) * 60
    except ValueError:
        interval = 3600
    # Discovery inside the sweep DOES reach out (DNS, HTTP, and Prospeo credits for
    # the hiring detector), so it is capped per campaign per sweep and only fires on
    # each campaign's own cadence (discovery_interval_days, default weekly). 0 makes
    # discovery a purely manual action.
    try:
        disc_limit = max(0, int(env.get("CAMPAIGN_DISCOVERY_LIMIT", "25") or 25))
    except ValueError:
        disc_limit = 25
    print(f"[campaigns] rolling sweep every {interval // 60} min "
          f"(discovery {'off' if not disc_limit else f'≤{disc_limit} accounts/campaign'})",
          flush=True)
    time.sleep(90)  # let the server settle before the first sweep
    while True:
        try:
            import campaigns as _camp
            conn = pipeline_db.connect()
            try:
                res = _camp.sweep(conn, commit=True, discovery_limit=disc_limit)
            finally:
                conn.close()
            added = sum(r.get("added", 0) or 0 for r in res.get("results", []))
            scanned = sum((r.get("discovered") or {}).get("scanned", 0)
                          for r in res.get("results", []))
            if res.get("swept"):
                print(f"[campaigns] swept {res['swept']} campaign(s), "
                      f"{added} member(s) added, {scanned} account(s) scanned", flush=True)
        except Exception as e:  # noqa: BLE001 — the sweeper must never crash the server
            print(f"[campaigns] sweep error: {type(e).__name__}: {e}", flush=True)
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
    # Credentials wired up from the Setup view live on the volume, not in the
    # service variables. Load them into the environment FIRST: every client below
    # reads os.environ, and a connector configured in the console has to be live on
    # the next boot without anyone touching Railway.
    try:
        n = connector_store.apply_to_environ(DATA)
        if n:
            print(f"[connectors] loaded {n} console-configured credential(s) "
                  f"from the data volume", flush=True)
    except Exception as e:  # noqa: BLE001 — never block boot on the store
        print(f"[connectors] credential store unavailable: {e}", flush=True)
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
    threading.Thread(target=_campaign_sweep_loop, daemon=True).start()
    if Handler.static_dir:
        print(f"[webui] serving frontend from {Handler.static_dir}")
    else:
        print(f"[webui] frontend dist not found; API-only (use Vite dev server)")
    print(f"[webui] listening on http://{host}:{port}")
    ThreadingHTTPServer((host, port), Handler).serve_forever()


if __name__ == "__main__":
    main()
