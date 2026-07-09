"""MongoDB access for the AI SDR deal-attribution dataset (Railway MongoDB service).

Shared by aisdr_attribution_sync.py (writes) and the web server's read endpoints
(webui/server/app.py imports this in-process). All pymongo imports are lazy —
inside functions — so importing this module NEVER fails: the stdlib-only server
still boots when pymongo isn't installed or MONGO_URL isn't set, and the feature
degrades to {"configured": false} instead of crashing.

Database (default "aisdr", override AISDR_MONGO_DB), collections keyed by the
HubSpot record id as a string _id:

  emails      one doc per AI-SDR email engagement: timestamp (epoch ms), subject,
              text, direction, status, contact_ids[], deal_ids[], synced_at
  contacts    HubSpot contact snapshot + first_aisdr_email_at (the attribution
              anchor: min hs_timestamp of the AI SDR emails to that contact)
  deals       HubSpot deal snapshot incl. owner/creator ids AND display names,
              dealstage raw + label, ai_sdr_deal_created (our computed flag)
  sync_state  single doc _id="aisdr": watermark_ms, last_run_*, seeded, counts
"""

import os

STATE_ID = "aisdr"


def _load_dotenv():
    # Reuse the pipeline's .env loader so MONGO_URL works in local/dev runs too.
    from hubspot_client import _load_dotenv as load
    load()


def mongo_configured():
    """True when MONGO_URL is set (after .env auto-load)."""
    _load_dotenv()
    return bool(os.environ.get("MONGO_URL"))


def get_db():
    """Connected pymongo Database. Raises RuntimeError with a clear message when
    MONGO_URL is unset or pymongo is missing — callers surface, never crash."""
    _load_dotenv()
    url = os.environ.get("MONGO_URL")
    if not url:
        raise RuntimeError("MONGO_URL is not set — connect the Railway MongoDB service "
                           "(add MONGO_URL=${{MongoDB.MONGO_URL}} to the sdr-console service)")
    try:
        from pymongo import MongoClient
    except ImportError as e:
        raise RuntimeError("pymongo is not installed (pip install -r requirements.txt)") from e
    # 5s server selection keeps a misconfigured URL from hanging request threads.
    client = MongoClient(url, serverSelectionTimeoutMS=5000, appname="sdr-console-aisdr")
    return client[os.environ.get("AISDR_MONGO_DB", "aisdr")]


def ensure_indexes(db):
    """Idempotent index creation (create_index is a no-op when it already exists)."""
    db.emails.create_index("timestamp")
    db.emails.create_index("contact_ids")
    db.contacts.create_index("ai_sdr_deal_created")
    db.deals.create_index("ai_sdr_deal_created")
    db.deals.create_index("contact_ids")


def get_sync_state(db):
    """The sync_state doc (or None before the first run), _id stripped."""
    doc = db.sync_state.find_one({"_id": STATE_ID})
    if doc:
        doc.pop("_id", None)
    return doc


def aisdr_analytics(db):
    """Aggregates for the Analytics tiles. total_pipeline sums `amount` over ALL
    flagged deals — including closed lost, by explicit product decision."""
    flagged = {"ai_sdr_deal_created": True}
    # amount is stored as float-or-null at sync time, so a plain $sum suffices.
    total = list(db.deals.aggregate([
        {"$match": flagged},
        {"$group": {"_id": None, "total": {"$sum": {"$ifNull": ["$amount", 0]}}}},
    ]))
    state = get_sync_state(db) or {}
    return {
        "configured": True,
        "deals_created": db.deals.count_documents(flagged),
        "total_pipeline": (total[0]["total"] if total else 0),
        "contacts_emailed": db.contacts.count_documents({}),
        "emails_logged": db.emails.count_documents({}),
        "last_sync_at": state.get("last_run_at"),
        "last_sync_ok": state.get("last_run_ok"),
        "last_error": state.get("last_error"),
    }
