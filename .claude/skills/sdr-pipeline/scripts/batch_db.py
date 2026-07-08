"""SQLite-backed batch store for the SDR pipeline.

Durable, resumable queue so sub-agents can work batches of contacts in parallel.
DB lives at data/outreach/pipeline.db. Generated copy stays in
data/outreach/generated/<contact_id>.json (the DB tracks status + batching).

Statuses: contacts = pending|generated|enrolled|failed|gated ; batches = pending|in_progress|done
  ('gated' = skipped/stopped because the contact is in a gated HubSpot lifecycle
   stage — opportunity/customer; set by the enroll-time gate and the daily guard.)
"""

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[4]  # scripts/ -> sdr-pipeline -> skills -> .claude -> project
DB_PATH = PROJECT_ROOT / "data" / "outreach" / "pipeline.db"
GEN_DIR = PROJECT_ROOT / "data" / "outreach" / "generated"


def now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def email_domain(email):
    """Company key for the signal cache: the lowercased email domain."""
    email = (email or "").strip().lower()
    return email.split("@")[-1] if "@" in email else ""


def connect():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")       # safe concurrent reads/writes
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def init_schema(conn):
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS contacts (
        contact_id  TEXT PRIMARY KEY,
        first_name  TEXT, last_name TEXT, email TEXT, title TEXT, company TEXT,
        linkedin_url TEXT, persona TEXT, domain TEXT, variant TEXT,
        batch_id    INTEGER,
        status      TEXT DEFAULT 'pending',
        error       TEXT,
        updated_at  TEXT
    );
    CREATE TABLE IF NOT EXISTS batches (
        batch_id     INTEGER PRIMARY KEY,
        status       TEXT DEFAULT 'pending',
        size         INTEGER,
        claimed_at   TEXT,
        completed_at TEXT
    );
    -- Per-company research cache, keyed by email domain. Reused for 90 days so a
    -- company is searched once instead of once per contact / per re-run.
    CREATE TABLE IF NOT EXISTS account_signals (
        domain        TEXT PRIMARY KEY,
        company_name  TEXT,
        signal        TEXT,
        has_recent    INTEGER,
        researched_at TEXT,
        model         TEXT,
        updated_at    TEXT
    );
    -- Idempotency ledger for HubSpot activity logging (hubspot_activity_sync.py).
    -- One row per logged email engagement; the dedup_key makes re-runs no-ops.
    CREATE TABLE IF NOT EXISTS hubspot_activity_log (
        dedup_key     TEXT PRIMARY KEY,
        event_type    TEXT NOT NULL,   -- outbound | inbound | our_reply
        channel       TEXT,            -- email (linkedin reserved; out of scope)
        contact_id    TEXT,            -- resolved HubSpot contact id (NULL if unresolved)
        engagement_id TEXT,            -- HubSpot email engagement id (NULL on failure)
        status        TEXT NOT NULL,   -- logged | failed | skipped_no_contact
        error         TEXT,
        event_ts      TEXT,            -- source timestamp (sent_at / date_received)
        created_at    TEXT
    );
    -- Cache of Bison email -> lead_id (the bulk enroll path discards lead_id, and the
    -- contacts table has none). Built by a paginated /api/leads sweep, then used to
    -- pull each lead's actually-sent emails. last_sent_at lets steady-state runs skip
    -- leads whose sequence has finished instead of re-fetching all ~4.6k every time.
    CREATE TABLE IF NOT EXISTS bison_lead_map (
        email         TEXT PRIMARY KEY,
        lead_id       INTEGER,
        contact_id    TEXT,
        last_sent_at  TEXT,
        last_fetch_at TEXT,
        updated_at    TEXT
    );
    -- Durable inbox for HeyReach (LinkedIn) webhook events. The webhook handler
    -- persists the raw payload here on receipt and ACKs immediately; a separate
    -- drain (heyreach_activity.py) processes pending rows into HubSpot LinkedIn
    -- Communications + the hubspot_activity_log ledger. dedup_key (when present)
    -- makes HeyReach retries no-ops; raw is the verbatim body, our source of truth.
    CREATE TABLE IF NOT EXISTS heyreach_events (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        event_id     TEXT,            -- HeyReach event id when the payload carries one
        event_type   TEXT,            -- raw HeyReach discriminator (best-effort parse)
        dedup_key    TEXT,            -- computed normalized key (NULL if unparseable)
        status       TEXT DEFAULT 'pending',  -- pending | done | skipped | failed
        attempts     INTEGER DEFAULT 0,
        error        TEXT,
        raw          TEXT NOT NULL,   -- verbatim request body
        received_at  TEXT,
        processed_at TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_contacts_batch  ON contacts(batch_id);
    CREATE INDEX IF NOT EXISTS idx_contacts_status ON contacts(status);
    CREATE INDEX IF NOT EXISTS idx_hsact_contact   ON hubspot_activity_log(contact_id);
    CREATE INDEX IF NOT EXISTS idx_hsact_status    ON hubspot_activity_log(status);
    CREATE INDEX IF NOT EXISTS idx_bisonmap_lead   ON bison_lead_map(lead_id);
    CREATE INDEX IF NOT EXISTS idx_hrev_status     ON heyreach_events(status);
    -- Partial unique index: dedups real ids, but never collapses NULL-keyed rows
    -- (an unparseable payload is still stored losslessly rather than dropped).
    CREATE UNIQUE INDEX IF NOT EXISTS idx_hrev_dedup ON heyreach_events(dedup_key)
        WHERE dedup_key IS NOT NULL;
    """)
    # migrate older DBs that predate the domain column, then backfill from email
    cols = [r["name"] for r in conn.execute("PRAGMA table_info(contacts)")]
    if "domain" not in cols:
        conn.execute("ALTER TABLE contacts ADD COLUMN domain TEXT")
    if "variant" not in cols:
        conn.execute("ALTER TABLE contacts ADD COLUMN variant TEXT")
    conn.execute("UPDATE contacts SET domain=lower(substr(email, instr(email,'@')+1)) "
                 "WHERE (domain IS NULL OR domain='') AND email LIKE '%@%'")
    # index after the column is guaranteed to exist
    conn.execute("CREATE INDEX IF NOT EXISTS idx_contacts_domain ON contacts(domain)")
    conn.commit()


def upsert_contacts(conn, rows):
    """Insert new contacts (ignore existing). Returns count of newly inserted."""
    before = conn.execute("SELECT COUNT(*) FROM contacts").fetchone()[0]
    conn.executemany("""
        INSERT OR IGNORE INTO contacts
          (contact_id, first_name, last_name, email, title, company, linkedin_url, persona, domain, variant, status, updated_at)
        VALUES (:contact_id,:first_name,:last_name,:email,:title,:company,:linkedin_url,:persona,:domain,:variant,'pending',:ts)
    """, [{"variant": None, **r, "domain": email_domain(r.get("email")), "ts": now()} for r in rows])
    conn.commit()
    return conn.execute("SELECT COUNT(*) FROM contacts").fetchone()[0] - before


def assign_batches(conn, batch_size=25):
    """Group any unbatched contacts into batches of `batch_size`. Returns new batch count.

    Ordered by domain so contacts from the same company land in the same batch —
    one web search then covers all of them (and feeds the signal cache).
    """
    unbatched = [r["contact_id"] for r in
                 conn.execute("SELECT contact_id FROM contacts WHERE batch_id IS NULL "
                              "ORDER BY domain, rowid")]
    next_bid = (conn.execute("SELECT COALESCE(MAX(batch_id),0) FROM batches").fetchone()[0]) + 1
    made = 0
    for i in range(0, len(unbatched), batch_size):
        chunk = unbatched[i:i + batch_size]
        bid = next_bid + made
        conn.execute("INSERT INTO batches (batch_id, status, size) VALUES (?, 'pending', ?)", (bid, len(chunk)))
        conn.executemany("UPDATE contacts SET batch_id=? WHERE contact_id=?", [(bid, c) for c in chunk])
        made += 1
    conn.commit()
    return made


def get_batch(conn, batch_id):
    return [dict(r) for r in conn.execute(
        "SELECT contact_id, first_name, last_name, email, title, company, linkedin_url, persona, domain, variant "
        "FROM contacts WHERE batch_id=? ORDER BY domain, rowid", (batch_id,))]


def pending_batches(conn):
    return [r["batch_id"] for r in
            conn.execute("SELECT batch_id FROM batches WHERE status='pending' ORDER BY batch_id")]


def set_batch_status(conn, batch_id, status):
    col = "completed_at" if status == "done" else "claimed_at"
    conn.execute(f"UPDATE batches SET status=?, {col}=? WHERE batch_id=?", (status, now(), batch_id))
    conn.commit()


def set_contact_status(conn, contact_id, status, error=None):
    conn.execute("UPDATE contacts SET status=?, error=?, updated_at=? WHERE contact_id=?",
                 (status, error, now(), contact_id))
    conn.commit()


def contacts_by_status(conn, status):
    return [dict(r) for r in conn.execute(
        "SELECT * FROM contacts WHERE status=? ORDER BY batch_id, rowid", (status,))]


def counts(conn):
    cstat = {r["status"]: r["n"] for r in
             conn.execute("SELECT status, COUNT(*) n FROM contacts GROUP BY status")}
    bstat = {r["status"]: r["n"] for r in
             conn.execute("SELECT status, COUNT(*) n FROM batches GROUP BY status")}
    total = conn.execute("SELECT COUNT(*) FROM contacts").fetchone()[0]
    return {"total_contacts": total, "contacts_by_status": cstat, "batches_by_status": bstat}


# ---- per-company signal cache --------------------------------------------
def get_signal(conn, domain):
    if not domain:
        return None
    r = conn.execute("SELECT * FROM account_signals WHERE domain=?", (domain,)).fetchone()
    return dict(r) if r else None


def signal_age_days(row):
    """Whole days since the signal was researched, or None if unparseable."""
    if not row or not row.get("researched_at"):
        return None
    try:
        ts = datetime.strptime(row["researched_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None
    return (datetime.now(timezone.utc) - ts).days


def signal_fresh(row, days=90):
    age = signal_age_days(row)
    return age is not None and age < days


def upsert_signal(conn, domain, company_name, signal, has_recent, model=None):
    conn.execute("""
        INSERT INTO account_signals (domain, company_name, signal, has_recent, researched_at, model, updated_at)
        VALUES (?,?,?,?,?,?,?)
        ON CONFLICT(domain) DO UPDATE SET
          company_name=excluded.company_name, signal=excluded.signal, has_recent=excluded.has_recent,
          researched_at=excluded.researched_at, model=excluded.model, updated_at=excluded.updated_at
    """, (domain, company_name, signal, 1 if has_recent else 0, now(), model, now()))
    conn.commit()


def all_signals(conn):
    return [dict(r) for r in conn.execute(
        "SELECT * FROM account_signals ORDER BY updated_at DESC")]


# ---- HubSpot activity ledger ---------------------------------------------
def activity_logged(conn, dedup_key):
    """True if this event was already logged successfully (skip on re-run)."""
    row = conn.execute(
        "SELECT 1 FROM hubspot_activity_log WHERE dedup_key=? AND status='logged'",
        (dedup_key,)).fetchone()
    return row is not None


def record_activity(conn, dedup_key, event_type, status, contact_id=None,
                    engagement_id=None, channel="email", event_ts=None, error=None):
    """Upsert a ledger row. A prior 'failed'/'skipped_no_contact' row is retried on
    the next run and flips to 'logged' once it succeeds; successes are never re-sent."""
    conn.execute("""
        INSERT INTO hubspot_activity_log
          (dedup_key, event_type, channel, contact_id, engagement_id, status, error, event_ts, created_at)
        VALUES (?,?,?,?,?,?,?,?,?)
        ON CONFLICT(dedup_key) DO UPDATE SET
          event_type=excluded.event_type, channel=excluded.channel, contact_id=excluded.contact_id,
          engagement_id=excluded.engagement_id, status=excluded.status, error=excluded.error,
          event_ts=excluded.event_ts, created_at=excluded.created_at
    """, (dedup_key, event_type, channel, contact_id, engagement_id, status, error, event_ts, now()))
    conn.commit()


def activity_counts(conn):
    """Summary of the ledger for status reporting."""
    by_status = {r["status"]: r["n"] for r in
                 conn.execute("SELECT status, COUNT(*) n FROM hubspot_activity_log GROUP BY status")}
    by_type = {r["event_type"]: r["n"] for r in
               conn.execute("SELECT event_type, COUNT(*) n FROM hubspot_activity_log "
                            "WHERE status='logged' GROUP BY event_type")}
    return {"by_status": by_status, "logged_by_type": by_type,
            "total": conn.execute("SELECT COUNT(*) FROM hubspot_activity_log").fetchone()[0]}


# ---- Bison email -> lead_id cache ----------------------------------------
def upsert_lead_map(conn, email, lead_id, contact_id=None):
    """Cache an email -> Bison lead_id mapping (from a /api/leads sweep)."""
    email = (email or "").strip().lower()
    if not email:
        return
    conn.execute("""
        INSERT INTO bison_lead_map (email, lead_id, contact_id, updated_at)
        VALUES (?,?,?,?)
        ON CONFLICT(email) DO UPDATE SET
          lead_id=excluded.lead_id,
          contact_id=COALESCE(excluded.contact_id, bison_lead_map.contact_id),
          updated_at=excluded.updated_at
    """, (email, int(lead_id) if lead_id is not None else None, contact_id, now()))


def mark_lead_fetched(conn, email, last_sent_at):
    """Record when a lead's sent-emails were last pulled + its newest send time."""
    conn.execute("UPDATE bison_lead_map SET last_fetch_at=?, last_sent_at=? WHERE email=?",
                 (now(), last_sent_at, (email or "").strip().lower()))
    conn.commit()


def lead_map_rows(conn):
    return [dict(r) for r in conn.execute("SELECT * FROM bison_lead_map")]


# ---- HeyReach (LinkedIn) webhook inbox -----------------------------------
def enqueue_heyreach_event(conn, raw, *, event_id=None, event_type=None, dedup_key=None):
    """Persist one raw HeyReach webhook payload. Idempotent on dedup_key (a HeyReach
    retry of an already-stored event is ignored). Returns the new row id, or None if
    the event was a duplicate. Commits. Never raises on a dupe — INSERT OR IGNORE."""
    cur = conn.execute("""
        INSERT OR IGNORE INTO heyreach_events
          (event_id, event_type, dedup_key, status, attempts, raw, received_at)
        VALUES (?,?,?,'pending',0,?,?)
    """, (event_id, event_type, dedup_key, raw, now()))
    conn.commit()
    return cur.lastrowid if cur.rowcount else None


def pending_heyreach_events(conn, limit=200, max_attempts=25):
    """Unprocessed inbox rows (pending or previously failed), oldest first. A poison
    row that keeps failing is dropped from the queue once it exceeds max_attempts so
    it can't be retried forever every tick (it stays in the table for inspection)."""
    return [dict(r) for r in conn.execute(
        "SELECT * FROM heyreach_events WHERE status IN ('pending','failed') "
        "AND attempts < ? ORDER BY id LIMIT ?", (int(max_attempts), int(limit)))]


def retry_skipped_heyreach_events(conn):
    """Flip previously 'skipped' rows (e.g. no matching contact at the time) back to
    'pending' so a later drain re-attempts them after contacts have been added.
    Returns the number requeued."""
    cur = conn.execute(
        "UPDATE heyreach_events SET status='pending', attempts=0 WHERE status='skipped'")
    conn.commit()
    return cur.rowcount


def mark_heyreach_event(conn, row_id, status, *, error=None):
    """Set the processing outcome for one inbox row (bumps attempts, stamps time)."""
    conn.execute(
        "UPDATE heyreach_events SET status=?, error=?, attempts=attempts+1, processed_at=? "
        "WHERE id=?", (status, error, now(), row_id))
    conn.commit()


def heyreach_event_counts(conn):
    """Inbox summary by status, for status reporting."""
    return {r["status"]: r["n"] for r in
            conn.execute("SELECT status, COUNT(*) n FROM heyreach_events GROUP BY status")}
