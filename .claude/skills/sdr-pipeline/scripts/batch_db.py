"""SQLite-backed batch store for the SDR pipeline.

Durable, resumable queue so sub-agents can work batches of contacts in parallel.
DB lives at data/outreach/pipeline.db. Generated copy stays in
data/outreach/generated/<contact_id>.json (the DB tracks status + batching).

Statuses: contacts = pending|generated|enrolled|failed ; batches = pending|in_progress|done
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
        linkedin_url TEXT, persona TEXT, domain TEXT,
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
    CREATE INDEX IF NOT EXISTS idx_contacts_batch  ON contacts(batch_id);
    CREATE INDEX IF NOT EXISTS idx_contacts_status ON contacts(status);
    """)
    # migrate older DBs that predate the domain column, then backfill from email
    cols = [r["name"] for r in conn.execute("PRAGMA table_info(contacts)")]
    if "domain" not in cols:
        conn.execute("ALTER TABLE contacts ADD COLUMN domain TEXT")
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
          (contact_id, first_name, last_name, email, title, company, linkedin_url, persona, domain, status, updated_at)
        VALUES (:contact_id,:first_name,:last_name,:email,:title,:company,:linkedin_url,:persona,:domain,'pending',:ts)
    """, [{**r, "domain": email_domain(r.get("email")), "ts": now()} for r in rows])
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
        "SELECT contact_id, first_name, last_name, email, title, company, linkedin_url, persona, domain "
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
