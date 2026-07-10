"""Log SDR email activity onto HubSpot contact records (CRM v3 `emails` engagements).

Every contact this system touches is a HubSpot contact whose record id we already
store as `contact_id`. This script reconciles three kinds of email activity onto
those records, idempotently:

  outbound   — each sequence email Email Bison actually sent the contact   (EMAIL / SENT)
  inbound    — each reply the contact sent back                            (INCOMING_EMAIL / RECEIVED)
  our_reply  — each follow-up reply we sent in-thread                      (EMAIL / SENT)

The "AI SDR" attribution: 100s of rotating Bison aliases physically send the mail,
but every logged activity sets the email header From to a single "AI SDR" identity
(HUBSPOT_AISDR_FROM_*), with the real rotating alias recorded as the header Sender —
so the contact timeline reads as one consistent AI SDR sender. Optionally also sets a
HubSpot owner (HUBSPOT_AISDR_OWNER_ID); left unset by default so the AI's volume does
not pollute any human rep's activity reporting.

Idempotency + resumability: a ledger table (hubspot_activity_log) keyed by a stable
per-event dedup key means re-runs never double-log. The Bison email->lead_id map is
cached (bison_lead_map) because the bulk enroll path discards lead_id; per-lead sweeps
self-limit to leads whose sequence is still active.

This script is read-only against the SDR send/enroll/scan paths — it never sends mail,
never enrolls, never mutates Bison/HeyReach. Safe to run on a schedule or on demand.

  python3 hubspot_activity_sync.py [--dry-run] [--json] [--since-days N]
      [--event-type outbound|inbound|our_reply ...] [--replies-only] [--outbound-only]
      [--contact-id ID] [--limit N] [--refresh-leads] [--resolve-owner EMAIL]
"""

import argparse
import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))                                    # batch_db, hubspot_client
sys.path.insert(0, str(SCRIPTS.parents[1] / "email-bison" / "scripts"))  # bison_client, helpers

import batch_db as db  # noqa: E402
from hubspot_client import HubSpotClient, HubSpotError, to_ms_epoch  # noqa: E402
from bison_client import BisonClient, BisonError  # noqa: E402
from fetch_interested_replies import message_text  # noqa: E402

PROJECT_ROOT = SCRIPTS.parents[3]
REPLIES_DIR = PROJECT_ROOT / "data" / "interested-replies"
SENT_FOLLOWUPS = REPLIES_DIR / "sent_followups.json"
FOLLOWUP_DRAFTS = REPLIES_DIR / "followup_drafts.json"
REVIEW_QUEUE = REPLIES_DIR / "review_queue.json"

BODY_CAP = 60000  # keep individual email bodies well under HubSpot property limits

# Senders that are never a real contact reply — system/alert mail, bounces.
JUNK_SENDER_SUBSTR = ("google-workspace-alerts", "workspace-alerts-noreply",
                      "calendar-notification@google", "mailer-daemon", "postmaster@",
                      "no-reply@google", "noreply@google")


def now():
    return datetime.now(timezone.utc)


def iso_z(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def is_junk_sender(email):
    e = (email or "").lower()
    if not e or "@" not in e:
        return True
    return any(s in e for s in JUNK_SENDER_SUBSTR)


def split_name(name):
    """Best-effort first/last split for an email-header display name."""
    parts = (name or "").strip().split()
    if not parts:
        return None, None
    return parts[0], (" ".join(parts[1:]) or None)


def read_json(path, default):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return default


class Logger:
    """Holds config + the HubSpot client and turns one resolved event into a ledger
    row + (unless dry-run) a HubSpot email engagement. Centralises dedup, dry-run,
    throttling, and error capture so the three event loops stay simple."""

    def __init__(self, conn, hs, *, ai_from, owner_id, dry_run, sleep, target_cid,
                 reconcile=False):
        self.conn, self.hs = conn, hs
        self.ai_from = ai_from
        self.owner_id = owner_id
        self.dry_run = dry_run
        self.sleep = sleep
        self.target_cid = target_cid
        self.reconcile = reconcile
        self.counts = {"logged": 0, "skipped_dupe": 0, "skipped_no_contact": 0, "failed": 0,
                       "reconciled": 0}
        self.by_type = {"outbound": 0, "inbound": 0, "our_reply": 0}
        self.errors = []

    # header builders -----------------------------------------------------
    def outbound_headers(self, alias_email, to_email, to_name):
        h = {"from": dict(self.ai_from)}
        if alias_email:
            h["sender"] = {"email": alias_email}  # the real rotating Bison alias
        to = {"email": to_email}
        fn, ln = split_name(to_name)
        if fn:
            to["firstName"] = fn
        if ln:
            to["lastName"] = ln
        h["to"] = [to]
        h["cc"], h["bcc"] = [], []
        return h

    def inbound_headers(self, from_email, from_name, to_inbox):
        frm = {"email": from_email}
        fn, ln = split_name(from_name)
        if fn:
            frm["firstName"] = fn
        if ln:
            frm["lastName"] = ln
        # The contact replied to the rotating alias inbox; when that's known use it,
        # else fall back to the AI SDR identity so the "to" is never blank.
        to = {"email": to_inbox} if to_inbox else dict(self.ai_from)
        return {"from": frm, "to": [to], "cc": [], "bcc": []}

    # the one place an event becomes a HubSpot activity -------------------
    def log(self, *, dedup_key, event_type, contact_id, direction, status, timestamp,
            subject=None, text=None, html=None, headers=None):
        # --contact-id is a debug/scope filter: anything other than the target is simply
        # out of scope — filter it FIRST so it never touches counts or the ledger. (A full
        # run without a target still records every unresolved event as skipped_no_contact.)
        if self.target_cid and str(contact_id) != str(self.target_cid):
            return
        if db.activity_logged(self.conn, dedup_key):
            self.counts["skipped_dupe"] += 1
            return
        if not contact_id:
            self.counts["skipped_no_contact"] += 1
            if not self.dry_run:
                db.record_activity(self.conn, dedup_key, event_type, "skipped_no_contact",
                                   event_ts=timestamp)
            return
        if self.dry_run:
            self.counts["logged"] += 1
            self.by_type[event_type] += 1
            print(f"  [dry] {event_type:9s} -> contact {contact_id}  {status:8s} "
                  f"{(subject or '')[:60]!r}")
            return
        # Reconcile mode (ledger recovery): if HubSpot already has an engagement on
        # this contact at ~this timestamp, adopt it into the ledger instead of
        # creating a duplicate. Only truly-missing events fall through and log.
        if self.reconcile:
            ms = to_ms_epoch(timestamp)
            existing = None
            if ms:
                try:
                    existing = self.hs.find_email_engagement(
                        contact_id, ms, direction=direction, subject=subject)
                except HubSpotError:
                    existing = None
            if existing:
                db.record_activity(self.conn, dedup_key, event_type, "logged",
                                   contact_id=contact_id, engagement_id=existing.get("id"),
                                   event_ts=timestamp)
                self.counts["reconciled"] += 1
                return
        try:
            eng_id = self.hs.log_email(
                contact_id, timestamp=timestamp, direction=direction, status=status,
                subject=subject, text=(text or None) and text[:BODY_CAP],
                html=(html or None) and html[:BODY_CAP], headers=headers, owner_id=self.owner_id)
            db.record_activity(self.conn, dedup_key, event_type, "logged",
                               contact_id=contact_id, engagement_id=eng_id, event_ts=timestamp)
            self.counts["logged"] += 1
            self.by_type[event_type] += 1
            if self.sleep:
                time.sleep(self.sleep)
        except HubSpotError as e:
            self.counts["failed"] += 1
            db.record_activity(self.conn, dedup_key, event_type, "failed",
                               contact_id=contact_id, event_ts=timestamp, error=str(e)[:300])
            if len(self.errors) < 5:
                self.errors.append(str(e)[:300])


# ---- contact-id resolution -----------------------------------------------
def load_email_to_cid(conn):
    """Lowercased contact email -> HubSpot contact_id, from our local contacts table."""
    out = {}
    for r in conn.execute("SELECT contact_id, lower(email) e FROM contacts "
                          "WHERE email IS NOT NULL AND email != ''"):
        out[r["e"]] = r["contact_id"]
    return out


def lead_to_cid_map(conn):
    out = {}
    for r in conn.execute("SELECT lead_id, contact_id FROM bison_lead_map "
                          "WHERE lead_id IS NOT NULL AND contact_id IS NOT NULL"):
        out[str(r["lead_id"])] = r["contact_id"]
    return out


def resolve_contact(lead_id, emails, *, email_to_cid, lead_cid, bison, hs, hs_cache, conn):
    """Map (lead_id, candidate emails) -> HubSpot contact_id. Always prefers the enrolled
    contact via lead_id, so every activity in a thread lands on the SAME record even when
    the contact replies from a different address than the one we enrolled."""
    if lead_id is not None and str(lead_id) in lead_cid:
        return lead_cid[str(lead_id)]
    cand = [e.strip().lower() for e in emails if e and e.strip()]
    for e in cand:
        if e in email_to_cid:
            return email_to_cid[e]
    # One lead fetch to recover the enrolled email (often differs from the reply-from addr).
    lead_email = None
    if lead_id is not None and bison is not None:
        try:
            lead_email = ((bison.get_lead(lead_id) or {}).get("email") or "").strip().lower()
        except BisonError:
            lead_email = None
        if lead_email and lead_email in email_to_cid:
            cid = email_to_cid[lead_email]
            db.upsert_lead_map(conn, lead_email, lead_id, cid)
            conn.commit()
            return cid
    for e in ([lead_email] + cand):
        if not e:
            continue
        if e not in hs_cache:
            try:
                hs_cache.update(hs.find_existing_email_ids([e]))
            except HubSpotError:
                pass
            hs_cache.setdefault(e, None)
        if hs_cache.get(e):
            return hs_cache[e]
    return None


# ---- outbound: actually-sent sequence emails -----------------------------
FULL_SWEEP_MARKER = "__full_sweep__"  # sentinel row; freshness tracks the last FULL sweep,
# not incidental per-reply upserts (those would make a partial map look fresh and skip leads).


def refresh_lead_map(conn, bison, email_to_cid, *, hs, force, max_age_hours, log_fn):
    """(Re)build the Bison email->lead_id cache by paginating /api/leads. Refreshes only
    when forced or the last full sweep is stale, since it's ~300 paginated GETs."""
    row = conn.execute("SELECT updated_at FROM bison_lead_map WHERE email=?",
                       (FULL_SWEEP_MARKER,)).fetchone()
    if not force and row and row[0]:
        try:
            age_h = (now() - datetime.strptime(row[0], "%Y-%m-%dT%H:%M:%SZ")
                     .replace(tzinfo=timezone.utc)).total_seconds() / 3600
            if age_h < max_age_hours:
                return 0
        except (ValueError, TypeError):
            pass
    log_fn("refreshing Bison lead map (paginating /api/leads)…")
    cnt = 0
    for lead in bison.get_paginated("/api/leads"):
        email = (lead.get("email") or "").strip().lower()
        if not email:
            continue
        db.upsert_lead_map(conn, email, lead.get("id"), email_to_cid.get(email))
        cnt += 1
        if cnt % 500 == 0:
            conn.commit()
    conn.execute("INSERT INTO bison_lead_map (email, updated_at) VALUES (?,?) "
                 "ON CONFLICT(email) DO UPDATE SET updated_at=excluded.updated_at",
                 (FULL_SWEEP_MARKER, db.now()))
    conn.commit()
    log_fn(f"lead map: {cnt} Bison leads mapped")
    resolve_unmatched_against_hubspot(conn, hs, log_fn=log_fn)
    return cnt


def resolve_unmatched_against_hubspot(conn, hs, *, log_fn):
    """Fill contact_id for lead-map rows the local contacts table couldn't resolve.

    The pagination above only resolves contact_id for leads whose email is in our LOCAL
    contacts table — but that table is a snapshot of the latest list pull (contacts.jsonl is
    overwritten each pull), so leads from earlier batches drop out of it. Those rows keep a
    NULL contact_id and run_outbound (WHERE contact_id IS NOT NULL) silently skips them, so
    their sent emails never log. Resolve them directly against HubSpot by email — the same
    live lookup the inbound/our_reply paths already use — so every emailed lead that is a real
    HubSpot contact gets covered. COALESCE in upsert_lead_map means an already-resolved id is
    never clobbered; once resolved a row is skipped here on future runs, so this shrinks to
    nothing in steady state. ~1 search call per 100 unmatched, only on full sweeps."""
    if hs is None:
        return 0
    unresolved = [dict(r) for r in conn.execute(
        "SELECT email, lead_id FROM bison_lead_map "
        "WHERE contact_id IS NULL AND email != ? AND coalesce(email,'') != ''",
        (FULL_SWEEP_MARKER,))]
    if not unresolved:
        return 0
    log_fn(f"lead map: resolving {len(unresolved)} unmatched leads against HubSpot…")
    try:
        found = hs.find_existing_email_ids([r["email"] for r in unresolved])
    except HubSpotError as e:
        log_fn(f"lead map: HubSpot resolve failed: {str(e)[:160]}")
        return 0
    resolved = 0
    for r in unresolved:
        cid = found.get((r["email"] or "").strip().lower())
        if cid:
            # pass the existing lead_id back so upsert_lead_map doesn't null it out
            db.upsert_lead_map(conn, r["email"], r["lead_id"], cid)
            resolved += 1
    conn.commit()
    log_fn(f"lead map: resolved {resolved}/{len(unresolved)} unmatched via HubSpot")
    return resolved


def run_outbound(conn, bison, logger, *, since_ms, settle_days, limit, lead_alias, log_fn):
    settle_cutoff = iso_z(now() - timedelta(days=settle_days))
    where = ("lead_id IS NOT NULL AND contact_id IS NOT NULL "
             "AND NOT (last_fetch_at IS NOT NULL AND last_sent_at IS NOT NULL AND last_sent_at < ?)")
    params = [settle_cutoff]
    if logger.target_cid:
        where += " AND contact_id = ?"
        params.append(str(logger.target_cid))
    rows = [dict(r) for r in conn.execute(
        f"SELECT email, lead_id, contact_id FROM bison_lead_map WHERE {where} ORDER BY rowid", params)]
    if limit:
        rows = rows[:limit]
    log_fn(f"outbound: sweeping {len(rows)} active leads for sent emails")
    contact_names = {r["contact_id"]: f"{r['first_name'] or ''} {r['last_name'] or ''}".strip()
                     for r in conn.execute("SELECT contact_id, first_name, last_name FROM contacts")}
    for row in rows:
        lead_id, contact_id, to_email = row["lead_id"], row["contact_id"], row["email"]
        try:
            sent = bison.get_lead_sent_emails(lead_id) or []
        except BisonError as e:
            if len(logger.errors) < 5:
                logger.errors.append(f"sent-emails lead {lead_id}: {str(e)[:120]}")
            continue
        sent = [m for m in sent if m.get("sent_at")]  # exclude not-yet-sent scheduled steps
        newest = max((m.get("sent_at") for m in sent), default=None)
        if not logger.dry_run:
            db.mark_lead_fetched(conn, to_email, newest)
        for m in sent:
            se = m.get("sender_email") or {}
            alias = se.get("email") if isinstance(se, dict) else se
            if alias:
                lead_alias.setdefault(lead_id, alias)
            ms = to_ms_epoch(m.get("sent_at"))
            if since_ms and ms and ms < since_ms:
                continue
            html = m.get("email_body")
            logger.log(
                dedup_key=f"outbound:{lead_id}:{m.get('id')}",
                event_type="outbound", contact_id=contact_id,
                direction="EMAIL", status="SENT", timestamp=m.get("sent_at"),
                subject=m.get("email_subject"),
                text=_html_to_text(html), html=html,
                headers=logger.outbound_headers(alias, to_email, contact_names.get(contact_id)))


def _html_to_text(html_value):
    try:
        from fetch_interested_replies import html_to_text
        return html_to_text(html_value)
    except Exception:
        return html_value or ""


# ---- inbound: replies the contact sent us --------------------------------
def run_inbound(conn, bison, logger, *, since_ms, max_replies, include_auto,
                email_to_cid, lead_cid, lead_alias, log_fn):
    hs_cache = {}
    seen = 0
    for reply in bison.list_replies(folder="inbox"):
        if seen >= max_replies:
            break
        if not reply.get("lead_id"):
            continue
        if reply.get("automated_reply") and not include_auto:
            continue
        if is_junk_sender(reply.get("from_email_address")):
            continue
        dr = reply.get("date_received")
        if not dr:
            continue  # can't log an activity without a timestamp
        ms = to_ms_epoch(dr)
        if since_ms and ms and ms < since_ms:
            continue
        seen += 1
        key = f"inbound:email:{reply.get('id')}"
        if db.activity_logged(conn, key):
            logger.counts["skipped_dupe"] += 1
            continue
        contact_id = resolve_contact(
            reply.get("lead_id"), [reply.get("from_email_address")],
            email_to_cid=email_to_cid, lead_cid=lead_cid, bison=bison,
            hs=logger.hs, hs_cache=hs_cache, conn=conn)
        to_inbox = lead_alias.get(reply.get("lead_id"))
        body = message_text(reply)
        logger.log(
            dedup_key=key, event_type="inbound", contact_id=contact_id,
            # INCOMING_EMAIL is what marks it received on the timeline; HubSpot's
            # hs_email_status enum has no RECEIVED, so SENT is the valid "delivered" state.
            direction="INCOMING_EMAIL", status="SENT", timestamp=dr,
            subject=reply.get("subject"), text=body,
            headers=logger.inbound_headers(reply.get("from_email_address"),
                                           reply.get("from_name"), to_inbox))
    log_fn(f"inbound: considered {seen} replies")


# ---- our_reply: follow-ups we sent in-thread -----------------------------
def run_our_replies(conn, bison, logger, *, since_ms, email_to_cid, lead_cid, log_fn):
    sent = read_json(SENT_FOLLOWUPS, {}).get("sent", {})
    drafts = {str(d.get("reply_id")): d for d in read_json(FOLLOWUP_DRAFTS, {}).get("items", [])}
    # sender_email_id -> alias address, harvested from the review queue (has both).
    sender_addr = {}
    for it in read_json(REVIEW_QUEUE, {}).get("items", []):
        if it.get("sender_email_id") and it.get("sending_email"):
            sender_addr[it["sender_email_id"]] = it["sending_email"]
    hs_cache = {}
    n = 0
    for reply_id, snt in sent.items():
        if (snt.get("channel") or "email") == "linkedin":
            continue  # email only — LinkedIn is handled by HeyReach's native HubSpot sync
        sent_at = snt.get("sent_at")
        if not sent_at:
            continue  # only actually-sent replies (with a timestamp) are logged
        ms = to_ms_epoch(sent_at)
        if since_ms and ms and ms < since_ms:
            continue
        n += 1
        key = f"our_reply:{reply_id}:{sent_at}"
        if db.activity_logged(conn, key):
            logger.counts["skipped_dupe"] += 1
            continue
        to_email = (snt.get("to_email") or "").strip()
        d = drafts.get(str(reply_id), {})
        # Resolve via lead_id first (same as inbound) so our reply lands on the enrolled
        # contact, not a duplicate record for the address the contact replied from.
        contact_id = resolve_contact(
            d.get("lead_id"), [to_email], email_to_cid=email_to_cid, lead_cid=lead_cid,
            bison=bison, hs=logger.hs, hs_cache=hs_cache, conn=conn)
        body = d.get("sent_message") or d.get("draft") or ""
        alias = sender_addr.get(snt.get("sender_email_id"))
        logger.log(
            dedup_key=key, event_type="our_reply", contact_id=contact_id,
            direction="EMAIL", status="SENT", timestamp=sent_at,
            subject=d.get("subject"), text=body,
            html=(body.replace("\n", "<br>") if body else None),
            headers=logger.outbound_headers(alias, to_email, d.get("from_name")))
    log_fn(f"our_reply: considered {n} sent follow-ups")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="no HubSpot writes, no ledger writes")
    ap.add_argument("--json", action="store_true", help="print the summary as JSON on the last line")
    ap.add_argument("--since-days", type=int, default=None, help="only log events newer than N days")
    ap.add_argument("--event-type", action="append", choices=["outbound", "inbound", "our_reply"],
                    help="repeatable; default = all three")
    ap.add_argument("--replies-only", action="store_true", help="inbound + our_reply (skip the lead sweep)")
    ap.add_argument("--outbound-only", action="store_true")
    ap.add_argument("--contact-id", default=None, help="limit to one HubSpot contact id")
    ap.add_argument("--limit", type=int, default=None, help="cap leads swept / items per type (smoke test)")
    ap.add_argument("--refresh-leads", action="store_true", help="force re-pull the Bison lead map")
    ap.add_argument("--settle-days", type=int, default=21,
                    help="skip leads whose last send is older than this (sequence finished)")
    ap.add_argument("--max-replies", type=int, default=1000, help="cap inbound reply pagination")
    ap.add_argument("--leads-max-age-hours", type=float, default=6.0,
                    help="reuse the cached lead map if refreshed within this many hours")
    ap.add_argument("--sleep", type=float, default=0.0, help="seconds to sleep between HubSpot writes")
    ap.add_argument("--include-auto-replies", action="store_true")
    ap.add_argument("--resolve-owner", default=None, metavar="EMAIL",
                    help="print the HubSpot owner id for EMAIL and exit (for HUBSPOT_AISDR_OWNER_ID)")
    ap.add_argument("--allow-backfill", action="store_true",
                    help="bypass the fresh-ledger guard (intentional first-run/mass backfill)")
    ap.add_argument("--reconcile-from-hubspot", action="store_true",
                    help="ledger recovery: adopt existing HubSpot engagements (matched by "
                         "contact + timestamp) into the ledger instead of re-creating them; "
                         "only truly-missing events get logged")
    args = ap.parse_args()

    import os
    hs = HubSpotClient()  # also loads .env so the config vars below are populated

    if args.resolve_owner:
        oid = hs.find_owner_id(args.resolve_owner)
        print(oid or f"(no owner found for {args.resolve_owner})")
        return

    # event-type selection
    types = set(args.event_type or [])
    if args.replies_only:
        types |= {"inbound", "our_reply"}
    if args.outbound_only:
        types |= {"outbound"}
    if not types:
        types = {"outbound", "inbound", "our_reply"}

    quiet = args.json
    def log_fn(msg):
        if not quiet:
            print(msg, flush=True)

    fn_first, fn_last = split_name(os.environ.get("HUBSPOT_AISDR_FROM_NAME", "AI SDR"))
    ai_from = {"email": os.environ.get("HUBSPOT_AISDR_FROM_EMAIL", "ai-sdr@everworker.ai"),
               "firstName": fn_first or "AI", "lastName": fn_last or "SDR"}
    owner_id = os.environ.get("HUBSPOT_AISDR_OWNER_ID") or None

    since_ms = (int((now() - timedelta(days=args.since_days)).timestamp() * 1000)
                if args.since_days else None)

    conn = db.connect()
    db.init_schema(conn)
    email_to_cid = load_email_to_cid(conn)
    bison = BisonClient()  # cheap (just loads env); used for sweeps + lead-id resolution

    def run_all(logger):
        lead_alias = {}  # lead_id -> alias address, shared so inbound can fill the "to" header
        lead_cid = lead_to_cid_map(conn)  # whatever email->lead->contact mappings are cached
        if "outbound" in types:
            refresh_lead_map(conn, bison, email_to_cid, hs=hs, force=args.refresh_leads,
                             max_age_hours=args.leads_max_age_hours, log_fn=log_fn)
            run_outbound(conn, bison, logger, since_ms=since_ms, settle_days=args.settle_days,
                         limit=args.limit, lead_alias=lead_alias, log_fn=log_fn)
            lead_cid = lead_to_cid_map(conn)  # refreshed map now resolves more threads
        if "inbound" in types:
            run_inbound(conn, bison, logger, since_ms=since_ms, max_replies=args.max_replies,
                        include_auto=args.include_auto_replies, email_to_cid=email_to_cid,
                        lead_cid=lead_cid, lead_alias=lead_alias, log_fn=log_fn)
        if "our_reply" in types:
            run_our_replies(conn, bison, logger, since_ms=since_ms, email_to_cid=email_to_cid,
                            lead_cid=lead_cid, log_fn=log_fn)

    def make_logger(dry_run):
        return Logger(conn, hs, ai_from=ai_from, owner_id=owner_id, dry_run=dry_run,
                      sleep=args.sleep, target_cid=args.contact_id,
                      reconcile=args.reconcile_from_hubspot)

    # Fresh-ledger guard: an EMPTY ledger + a live run is exactly the shape of the
    # "wiped volume re-logs everything as duplicates" failure. Probe with a dry-run
    # first; a would-log volume above the threshold refuses to write unless the
    # caller explicitly opts in (--allow-backfill) or reconciles (--reconcile-from-hubspot).
    fresh_max = int(os.environ.get("HUBSPOT_ACTIVITY_FRESH_MAX", "50") or 50)
    ledger_logged = conn.execute(
        "SELECT COUNT(*) FROM hubspot_activity_log WHERE status='logged'").fetchone()[0]
    guard_active = (ledger_logged == 0 and not args.dry_run
                    and not args.allow_backfill and not args.reconcile_from_hubspot)
    if guard_active:
        log_fn(f"fresh ledger (0 logged rows) — probing with a dry-run first "
               f"(guard threshold {fresh_max})")
        probe = make_logger(dry_run=True)
        run_all(probe)
        if probe.counts["logged"] > fresh_max:
            summary = {
                "ok": False, "guard": "fresh_ledger", "would_log": probe.counts["logged"],
                "threshold": fresh_max, "scope": sorted(types),
                "hint": "ledger is empty but a mass backfill was about to run — if the data "
                        "volume was wiped, run with --reconcile-from-hubspot to adopt the "
                        "existing engagements; for an intentional first backfill use "
                        "--allow-backfill",
            }
            if not quiet:
                print(f"\nGUARD: would log {probe.counts['logged']} engagements onto an "
                      f"empty ledger (> {fresh_max}) — refusing. {summary['hint']}")
            if args.json:
                print(json.dumps(summary))
            return
        log_fn(f"guard passed ({probe.counts['logged']} <= {fresh_max}) — running live")

    logger = make_logger(dry_run=args.dry_run)
    run_all(logger)

    summary = {
        "ok": logger.counts["failed"] == 0,
        "dry_run": args.dry_run,
        "scope": sorted(types),
        "owner_set": bool(owner_id),
        **logger.counts,
        "by_type": logger.by_type,
        "errors": logger.errors,
    }
    if not quiet:
        print(f"\nactivity sync: {logger.counts} by_type={logger.by_type} "
              f"{'(dry-run)' if args.dry_run else ''}")
        if logger.errors:
            print("first errors:")
            for e in logger.errors:
                print("  -", e)
    if args.json:
        print(json.dumps(summary))


if __name__ == "__main__":
    main()
