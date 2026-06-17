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
        linkedin_url TEXT, persona TEXT,
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
    CREATE INDEX IF NOT EXISTS idx_contacts_batch  ON contacts(batch_id);
    CREATE INDEX IF NOT EXISTS idx_contacts_status ON contacts(status);
    """)
    conn.commit()


def upsert_contacts(conn, rows):
    """Insert new contacts (ignore existing). Returns count of newly inserted."""
    before = conn.execute("SELECT COUNT(*) FROM contacts").fetchone()[0]
    conn.executemany("""
        INSERT OR IGNORE INTO contacts
          (contact_id, first_name, last_name, email, title, company, linkedin_url, persona, status, updated_at)
        VALUES (:contact_id,:first_name,:last_name,:email,:title,:company,:linkedin_url,:persona,'pending',:ts)
    """, [{**r, "ts": now()} for r in rows])
    conn.commit()
    return conn.execute("SELECT COUNT(*) FROM contacts").fetchone()[0] - before


def assign_batches(conn, batch_size=25):
    """Group any unbatched contacts into batches of `batch_size`. Returns new batch count."""
    unbatched = [r["contact_id"] for r in
                 conn.execute("SELECT contact_id FROM contacts WHERE batch_id IS NULL ORDER BY rowid")]
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
        "SELECT contact_id, first_name, last_name, email, title, company, linkedin_url, persona "
        "FROM contacts WHERE batch_id=? ORDER BY rowid", (batch_id,))]


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
