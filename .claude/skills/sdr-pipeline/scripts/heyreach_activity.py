"""Log HeyReach (LinkedIn) activity onto HubSpot contact records as Communications.

The LinkedIn counterpart of hubspot_activity_sync.py. HeyReach pushes six event types
to the console's webhook; each is persisted raw to the `heyreach_events` inbox and then
reconciled here onto the matching HubSpot contact as a CRM v3 `communications` object
(LinkedIn channel), idempotently via the shared `hubspot_activity_log` ledger:

  li_connection_sent       — connection request sent to the lead          (outbound)
  li_connection_accepted   — the lead accepted our connection request     (inbound-ish)
  li_message_sent          — LinkedIn DM we sent                          (outbound)
  li_inmail_sent           — InMail we sent                               (outbound)
  li_message_replied       — DM reply the lead sent back                  (inbound)
  li_inmail_replied        — InMail reply the lead sent back              (inbound)

Communications carry no inbound/outbound direction property (unlike emails), so the
direction/intent is written into the body text. Attribution mirrors the email path: no
HubSpot owner by default (so the AI's volume doesn't pollute a human rep's reporting),
overridable with HUBSPOT_AISDR_OWNER_ID.

Contact resolution (mirrors the email resolve_contact): normalized LinkedIn profile URL
-> local contacts -> email -> local contacts -> live HubSpot search. The exact HeyReach
webhook payload field names are read defensively from a flattened view of the payload, so
a schema surprise is a one-line patch to the candidate-key lists rather than a redesign.

  python3 heyreach_activity.py --drain [--dry-run] [--json] [--limit N] [--contact-id ID]
                               [--retry-skipped] [--sleep S]
  python3 heyreach_activity.py --backfill [--lookback 30] [--max 1000] [--dry-run] [--json]

This script never sends LinkedIn messages, never enrolls, never mutates HeyReach. It only
reads the durable inbox + HeyReach conversations (for --backfill) and writes to HubSpot.
"""

import argparse
import fcntl
import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))                                   # batch_db, hubspot_client, heyreach_client

import batch_db as db  # noqa: E402
from hubspot_client import HubSpotClient, HubSpotError, to_ms_epoch  # noqa: E402

BODY_CAP = 8000  # LinkedIn messages are short; keep bodies well under HubSpot limits

# Known HeyReach webhook event labels (for reference; classification is keyword-based in
# _canonical_type so it survives label variants):
#   "Connection Request Sent", "Connection Request Accepted", "Message Sent",
#   "InMail Sent", and a (possibly unified) reply event such as
#   "Every Message/InMail Reply Received". For the reply + sent families the isInMail
#   flag — not the label — decides DM vs InMail, because a unified label mentions both.
CANON_TYPES = {"li_connection_sent", "li_connection_accepted", "li_message_sent",
               "li_inmail_sent", "li_message_replied", "li_inmail_replied"}


# ---- LinkedIn URL normalization ------------------------------------------
def normalize_linkedin(url):
    """Canonical /in/<slug> key for a LinkedIn profile URL, lowercased. Absorbs
    http/https, www, locale prefixes, trailing slash, and query/fragment. Accepts a
    full URL, a bare '/in/slug', or a bare slug. Returns None when there's no handle."""
    s = (url or "").strip()
    if not s:
        return None
    m = re.search(r"/in/([^/?#]+)", s, re.I)
    if m:
        return m.group(1).strip().lower().rstrip("/")
    # bare handle (no scheme/slash, not an email/domain)
    if "/" not in s and "@" not in s and "." not in s:
        return s.lower()
    return None


# ---- defensive payload parsing -------------------------------------------
def _flatten(obj):
    """Breadth-first flatten of a payload to {lowercased_leaf_key: scalar}. Shallowest
    occurrence of a key wins, so top-level fields take precedence over nested ones."""
    flat, queue = {}, [obj]
    while queue:
        cur = queue.pop(0)
        if isinstance(cur, dict):
            for k, v in cur.items():
                if isinstance(v, (dict, list)):
                    queue.append(v)
                else:
                    lk = str(k).lower()
                    if lk not in flat and v is not None and v != "":
                        flat[lk] = v
        elif isinstance(cur, list):
            queue.extend(x for x in cur if isinstance(x, (dict, list)))
    return flat


def _g(flat, *names):
    for n in names:
        v = flat.get(n)
        if v is not None and v != "":
            return v
    return None


def _truthy(v):
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in ("1", "true", "yes", "y", "t")


def _canonical_type(raw_type, is_inmail, has_inmail_flag):
    """Map a raw HeyReach event label to a canonical type by keyword, so we survive label
    variants. The isInMail FLAG (when present) is authoritative for DM-vs-InMail; only
    when the flag is absent do we fall back to the label — and then only if the label is
    unambiguously InMail-only ('inmail' present, 'message' absent), since a unified reply
    label mentions both."""
    s = re.sub(r"[^a-z0-9]+", " ", (raw_type or "").lower()).strip()
    if "connection" in s or "connect" in s or "invitation" in s:
        return "li_connection_accepted" if "accept" in s else "li_connection_sent"
    if "accept" in s:
        return "li_connection_accepted"
    inmail = is_inmail if has_inmail_flag else ("inmail" in s and "message" not in s)
    if "repl" in s or "received" in s or "incoming" in s:
        return "li_inmail_replied" if inmail else "li_message_replied"
    if "sent" in s or "message" in s or "inmail" in s:
        return "li_inmail_sent" if inmail else "li_message_sent"
    return None


def normalize_event(raw_obj):
    """Turn a raw HeyReach webhook payload into a canonical event dict, or None if no
    event type can be determined. Reads every field from a flattened view via candidate
    key names (HeyReach's exact schema is unconfirmed; patch the lists here once a real
    payload is captured)."""
    if not isinstance(raw_obj, dict) or not raw_obj:
        return None
    flat = _flatten(raw_obj)
    raw_type = _g(flat, "eventtype", "event_type", "type", "event", "webhooktype",
                  "eventname", "action", "name")
    inmail_raw = _g(flat, "isinmail", "is_inmail", "inmail")
    is_inmail = _truthy(inmail_raw)
    canon = _canonical_type(raw_type, is_inmail, has_inmail_flag=inmail_raw is not None)
    if canon not in CANON_TYPES:
        return None
    ev = {
        "canon_type": canon,
        "raw_type": raw_type,
        "is_inmail": is_inmail,
        "profile_url": _g(flat, "profileurl", "linkedinprofileurl", "linkedinurl",
                          "profile_url", "leadprofileurl", "publicprofileurl",
                          "correspondentprofileurl"),
        "email": (_g(flat, "emailaddress", "email", "enrichedemailaddress",
                     "lead_email", "correspondentemail") or ""),
        "first_name": _g(flat, "firstname", "first_name"),
        "last_name": _g(flat, "lastname", "last_name"),
        "timestamp": _g(flat, "time", "timestamp", "createdat", "eventtime", "sentat",
                        "occurredat", "datetime", "date"),
        "body": _g(flat, "message", "messagetext", "text", "body", "lastmessagetext",
                   "content", "note") or "",
        "subject": _g(flat, "subject", "messagesubject", "inmailsubject") or "",
        "event_id": _g(flat, "eventid", "webhookid", "messageid", "id"),
        "campaign_id": _g(flat, "campaignid", "campaign_id"),
    }
    return ev


# ---- dedup + rendering ----------------------------------------------------
def dedup_key(ev):
    """Stable per-event key for the ledger + inbox unique index. Prefers HeyReach's own
    event id; otherwise a hash of (profile/email, type, coarse timestamp, body head)."""
    if not ev:
        return None
    if ev.get("event_id"):
        return f"li:{ev['canon_type']}:{ev['event_id']}"
    ident = normalize_linkedin(ev.get("profile_url")) or (ev.get("email") or "").strip().lower()
    basis = "|".join([ident or "", ev.get("canon_type") or "",
                      str(to_ms_epoch(ev.get("timestamp")) or ""),
                      (ev.get("body") or "")[:200]])
    return "li:" + hashlib.sha1(basis.encode("utf-8")).hexdigest()


def _name(ev):
    n = " ".join(p for p in (ev.get("first_name"), ev.get("last_name")) if p).strip()
    return n or "the lead"


def render_body(ev, sdr_name="AI SDR"):
    """The hs_communication_body text. Since Communications has no direction field, the
    sent/received intent is stated explicitly here."""
    name = _name(ev)
    body = (ev.get("body") or "").strip()[:BODY_CAP]
    subj = (ev.get("subject") or "").strip()
    t = ev["canon_type"]
    if t == "li_connection_sent":
        s = f"LinkedIn connection request sent to {name} by {sdr_name}."
        return s + (f"\n\nNote:\n{body}" if body else "")
    if t == "li_connection_accepted":
        return f"{name} accepted the LinkedIn connection request from {sdr_name}."
    if t == "li_message_sent":
        return f"LinkedIn message sent to {name} by {sdr_name}:\n\n{body}"
    if t == "li_inmail_sent":
        head = f"LinkedIn InMail sent to {name} by {sdr_name}" + (f" — {subj}" if subj else "")
        return f"{head}:\n\n{body}"
    if t == "li_message_replied":
        return f"LinkedIn reply received from {name}:\n\n{body}"
    if t == "li_inmail_replied":
        head = f"LinkedIn InMail reply received from {name}" + (f" — {subj}" if subj else "")
        return f"{head}:\n\n{body}"
    return f"LinkedIn activity from {name}:\n\n{body}"  # unreachable (canon validated)


# ---- contact resolution ---------------------------------------------------
def load_linkedin_to_cid(conn):
    """Normalized LinkedIn slug -> HubSpot contact_id, from the local contacts table."""
    out = {}
    for r in conn.execute("SELECT contact_id, linkedin_url FROM contacts "
                          "WHERE linkedin_url IS NOT NULL AND linkedin_url != ''"):
        key = normalize_linkedin(r["linkedin_url"])
        if key:
            out.setdefault(key, r["contact_id"])
    return out


def load_email_to_cid(conn):
    """Lowercased contact email -> HubSpot contact_id, from the local contacts table."""
    out = {}
    for r in conn.execute("SELECT contact_id, lower(email) e FROM contacts "
                          "WHERE email IS NOT NULL AND email != ''"):
        out[r["e"]] = r["contact_id"]
    return out


def resolve_contact(ev, *, li_to_cid, email_to_cid, hs, hs_cache):
    """Map an event to a HubSpot contact_id: local LinkedIn map -> local email map ->
    live HubSpot LinkedIn search -> live HubSpot email search. None if unresolved."""
    key = normalize_linkedin(ev.get("profile_url"))
    if key and key in li_to_cid:
        return li_to_cid[key]
    e = (ev.get("email") or "").strip().lower()
    if e and e in email_to_cid:
        return email_to_cid[e]
    if ev.get("profile_url"):
        try:
            cid = hs.find_contact_by_linkedin(ev["profile_url"])
        except HubSpotError:
            cid = None
        if cid:
            return cid
    if e:
        if e not in hs_cache:
            try:
                hs_cache.update(hs.find_existing_email_ids([e]))
            except HubSpotError:
                pass
            hs_cache.setdefault(e, None)
        if hs_cache.get(e):
            return hs_cache[e]
    return None


# ---- logger ---------------------------------------------------------------
class LiLogger:
    """Turns one resolved event into a ledger row + (unless dry-run) a HubSpot LinkedIn
    Communication. Centralises dedup, dry-run, throttling, and error capture."""

    def __init__(self, conn, hs, *, dry_run, owner_id, sleep, target_cid, sdr_name):
        self.conn, self.hs = conn, hs
        self.dry_run = dry_run
        self.owner_id = owner_id
        self.sleep = sleep
        self.target_cid = target_cid
        self.sdr_name = sdr_name
        self.counts = {"logged": 0, "skipped_dupe": 0, "skipped_no_contact": 0, "failed": 0}
        self.by_type = {t: 0 for t in CANON_TYPES}
        self.errors = []
        self.last_error = None  # the error from the most recent failed log() (per-row)

    def log(self, *, ev, contact_id):
        """Returns one of: 'out_of_scope' | 'dupe' | 'no_contact' | 'dry' | 'logged' | 'failed'."""
        if self.target_cid and str(contact_id) != str(self.target_cid):
            return "out_of_scope"
        key = dedup_key(ev)
        if db.activity_logged(self.conn, key):
            self.counts["skipped_dupe"] += 1
            return "dupe"
        if not contact_id:
            self.counts["skipped_no_contact"] += 1
            if not self.dry_run:
                db.record_activity(self.conn, key, ev["canon_type"], "skipped_no_contact",
                                   channel="linkedin", event_ts=ev.get("timestamp"))
            return "no_contact"
        if self.dry_run:
            self.counts["logged"] += 1
            self.by_type[ev["canon_type"]] += 1
            print(f"  [dry] {ev['canon_type']:22s} -> contact {contact_id}  "
                  f"{render_body(ev, self.sdr_name)[:70]!r}")
            return "dry"
        try:
            comm_id = self.hs.log_communication(
                contact_id, timestamp=ev.get("timestamp"),
                body=render_body(ev, self.sdr_name), owner_id=self.owner_id)
            db.record_activity(self.conn, key, ev["canon_type"], "logged",
                               contact_id=contact_id, engagement_id=comm_id,
                               channel="linkedin", event_ts=ev.get("timestamp"))
            self.counts["logged"] += 1
            self.by_type[ev["canon_type"]] += 1
            if self.sleep:
                time.sleep(self.sleep)
            return "logged"
        except HubSpotError as e:
            self.counts["failed"] += 1
            self.last_error = str(e)[:300]
            db.record_activity(self.conn, key, ev["canon_type"], "failed",
                               contact_id=contact_id, channel="linkedin",
                               event_ts=ev.get("timestamp"), error=self.last_error)
            if len(self.errors) < 5:
                self.errors.append(self.last_error)
            return "failed"

    def summary(self, **extra):
        return {"ok": self.counts["failed"] == 0, "dry_run": self.dry_run,
                "owner_set": bool(self.owner_id), **self.counts,
                "by_type": {k: v for k, v in self.by_type.items() if v},
                "errors": self.errors, **extra}


def _make_logger(conn, hs, args):
    owner_id = os.environ.get("HUBSPOT_AISDR_OWNER_ID") or None
    sdr_name = os.environ.get("HUBSPOT_AISDR_FROM_NAME", "AI SDR")
    return LiLogger(conn, hs, dry_run=args.dry_run, owner_id=owner_id, sleep=args.sleep,
                    target_cid=args.contact_id, sdr_name=sdr_name)


# ---- drain the webhook inbox ----------------------------------------------
def drain_pending(conn, hs, logger, *, limit, log_fn):
    """Process pending/failed heyreach_events rows into HubSpot Communications. Inbox
    rows are marked done (logged/dupe), skipped (no contact / unmappable), or failed
    (transient error — retried next run). Dry-run leaves inbox status untouched."""
    li_to_cid = load_linkedin_to_cid(conn)
    email_to_cid = load_email_to_cid(conn)
    hs_cache = {}
    rows = db.pending_heyreach_events(conn, limit=limit)
    log_fn(f"drain: {len(rows)} pending LinkedIn events")
    processed = 0
    for row in rows:
        processed += 1
        try:
            ev = normalize_event(json.loads(row["raw"]))
        except (ValueError, TypeError):
            ev = None
        if ev is None:
            if not logger.dry_run:
                db.mark_heyreach_event(conn, row["id"], "skipped", error="unparseable/unmapped")
            continue
        # A missing/unparseable event timestamp is a permanent defect, not a transient
        # HubSpot error — fall back to when we received the webhook so the activity can
        # still be logged (and dated sensibly) instead of failing-then-abandoning forever.
        if to_ms_epoch(ev.get("timestamp")) is None:
            ev["timestamp"] = row.get("received_at")
        cid = resolve_contact(ev, li_to_cid=li_to_cid, email_to_cid=email_to_cid,
                              hs=hs, hs_cache=hs_cache)
        outcome = logger.log(ev=ev, contact_id=cid)
        if logger.dry_run:
            continue
        if outcome in ("logged", "dupe"):
            db.mark_heyreach_event(conn, row["id"], "done")
        elif outcome == "no_contact":
            db.mark_heyreach_event(conn, row["id"], "skipped", error="no matching contact")
        elif outcome == "failed":
            db.mark_heyreach_event(conn, row["id"], "failed", error=(logger.last_error or "hubspot error"))
        # out_of_scope (contact-id filter): leave the row as-is for a full run
    return logger.summary(processed=processed)


# ---- optional one-time backfill from existing conversations ---------------
def backfill_from_conversations(conn, hs, logger, *, lookback_days, max_items, log_fn):
    """Synthesize message/reply events (the 4 message-family types) from HeyReach
    conversations and log them. Connection sent/accepted are NOT backfillable — no API
    source. Idempotent via the same ledger, so it's safe to re-run."""
    from datetime import datetime, timezone, timedelta
    try:
        from heyreach_client import HeyReachClient, HeyReachError
    except ImportError as e:
        log_fn(f"backfill unavailable: {e}")
        return logger.summary(processed=0)
    raw = (os.environ.get("HEYREACH_CAMPAIGN_ID") or "").strip()
    campaign_ids = [int(x) for x in raw.replace(" ", "").split(",") if x.isdigit()]
    try:
        hr = HeyReachClient()
    except HeyReachError as e:
        log_fn(f"backfill: HeyReach not configured ({e})")
        return logger.summary(processed=0)
    li_to_cid = load_linkedin_to_cid(conn)
    email_to_cid = load_email_to_cid(conn)
    hs_cache = {}
    cutoff = datetime.now(timezone.utc) - timedelta(days=lookback_days)
    processed = 0
    try:
        convos = hr.iter_conversations(campaign_ids=campaign_ids or None, max_items=max_items)
        for conv in convos:
            cp = conv.get("correspondentProfile") or {}
            base = {
                "profile_url": cp.get("profileUrl"),
                "email": cp.get("emailAddress") or cp.get("enrichedEmailAddress") or "",
                "first_name": cp.get("firstName"), "last_name": cp.get("lastName"),
                "campaign_id": None, "raw_type": "backfill",
            }
            conv_id = conv.get("id")
            for m in conv.get("messages") or []:
                created = m.get("createdAt")
                if to_ms_epoch(created) is None:
                    continue  # can't log an activity without a real timestamp
                try:
                    if datetime.fromisoformat(str(created).replace("Z", "+00:00")) < cutoff:
                        continue
                except ValueError:
                    pass
                is_inmail = bool(m.get("isInMail"))
                sender = m.get("sender")
                if sender == "ME":
                    canon = "li_inmail_sent" if is_inmail else "li_message_sent"
                elif sender == "CORRESPONDENT":
                    canon = "li_inmail_replied" if is_inmail else "li_message_replied"
                else:
                    continue
                # Stable per-message id (NOT the positional index, which shifts when a
                # message is inserted/reordered and would re-log on a later backfill).
                mid = m.get("id") or m.get("messageId")
                ev_id = (f"{conv_id}:{mid}" if mid else "%s:%s" % (
                    conv_id, hashlib.sha1(
                        ("%s|%s|%s|%s" % (conv_id, created, sender, (m.get("body") or "")[:120]))
                        .encode("utf-8")).hexdigest()[:16]))
                ev = {**base, "canon_type": canon, "is_inmail": is_inmail,
                      "timestamp": created, "body": m.get("body") or "",
                      "subject": m.get("subject") or "", "event_id": ev_id}
                cid = resolve_contact(ev, li_to_cid=li_to_cid, email_to_cid=email_to_cid,
                                      hs=hs, hs_cache=hs_cache)
                logger.log(ev=ev, contact_id=cid)
                processed += 1
    except HeyReachError as e:
        log_fn(f"backfill: HeyReach fetch failed: {e}")
    return logger.summary(processed=processed)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--drain", action="store_true", help="process the webhook inbox (default)")
    ap.add_argument("--backfill", action="store_true",
                    help="one-time: synthesize message/reply events from HeyReach conversations")
    ap.add_argument("--lookback", type=int, default=30, help="backfill: only messages newer than N days")
    ap.add_argument("--max", type=int, default=1000, help="backfill: max conversations to scan")
    ap.add_argument("--retry-skipped", action="store_true",
                    help="requeue previously 'skipped' inbox rows (e.g. after adding contacts)")
    ap.add_argument("--dry-run", action="store_true", help="no HubSpot writes, no ledger/inbox writes")
    ap.add_argument("--json", action="store_true", help="print the summary as JSON on the last line")
    ap.add_argument("--limit", type=int, default=500, help="max inbox rows to process this run")
    ap.add_argument("--contact-id", default=None, help="limit logging to one HubSpot contact id")
    ap.add_argument("--sleep", type=float, default=0.0, help="seconds to sleep between HubSpot writes")
    args = ap.parse_args()

    quiet = args.json
    def log_fn(msg):
        if not quiet:
            print(msg, flush=True)

    hs = HubSpotClient()  # also loads .env
    conn = db.connect()
    db.init_schema(conn)

    # Cross-process mutual exclusion: only one drain/backfill writes to HubSpot at a time,
    # machine-wide (the inline webhook drain, the hourly sweep, and any manual CLI run are
    # all separate processes, so an in-process lock can't serialize them). This keeps the
    # ledger check-then-write safe — without it two drainers could both pass activity_logged()
    # for the same event and create duplicate Communications. Exit cleanly if another holds it.
    lock_path = db.DB_PATH.parent / ".heyreach_drain.lock"
    lock_f = open(lock_path, "w")
    try:
        fcntl.flock(lock_f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        log_fn("another HeyReach drain is already running; exiting")
        if args.json:
            print(json.dumps({"ok": True, "skipped": "locked"}))
        return

    if args.retry_skipped and not args.dry_run:
        n = db.retry_skipped_heyreach_events(conn)
        log_fn(f"requeued {n} skipped events")

    logger = _make_logger(conn, hs, args)
    try:
        if args.backfill:
            summary = backfill_from_conversations(
                conn, hs, logger, lookback_days=args.lookback, max_items=args.max, log_fn=log_fn)
        else:
            summary = drain_pending(conn, hs, logger, limit=args.limit, log_fn=log_fn)
    finally:
        fcntl.flock(lock_f, fcntl.LOCK_UN)
        lock_f.close()

    if not quiet:
        print(f"\nheyreach activity: {logger.counts} by_type="
              f"{summary.get('by_type')} {'(dry-run)' if args.dry_run else ''}")
        for e in logger.errors:
            print("  -", e)
    if args.json:
        print(json.dumps(summary))


if __name__ == "__main__":
    main()
