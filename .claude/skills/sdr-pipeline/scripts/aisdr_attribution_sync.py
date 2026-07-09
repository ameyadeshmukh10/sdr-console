"""Nightly AI SDR deal attribution: HubSpot -> MongoDB sync + property write-back.

The pipeline logs every AI SDR outreach email to HubSpot as an `emails` engagement
whose header From is the single AI SDR identity (ai-sdr@everworker.ai). This script
closes the loop:

  1. pulls every email engagement sent by HUBSPOT_AISDR_FROM_EMAIL (subject, body,
     timestamp fetched IN the search response — no per-object re-reads),
  2. resolves associations in batches: email -> contacts, email -> deals, and
     contact -> deals (the logged emails are associated to contacts only; HubSpot
     merely displays them on deal timelines, so contact -> deal is the real join),
  3. snapshots those contacts and deals into MongoDB (Railway MongoDB service),
     including owner/created-by numeric ids AND their display names,
  4. attributes deals: a deal counts as AI-SDR-created iff it is associated with a
     contact the AI SDR emailed AND deal.createdate >= that contact's FIRST AI SDR
     email — pre-existing deals are never credited,
  5. writes ai_sdr_deal_created=true back to HubSpot (deals + contacts, batched),
     only where the value isn't already true; never writes false.

First run (empty Mongo / no sync_state) seeds everything; later runs are
incremental via a hs_timestamp watermark (with a 10-minute overlap) and upserts
keyed by engagement id, so re-runs are idempotent. Contact/deal snapshots and the
contact->deal association sweep are refreshed EVERY run — a deal created months
after the emails is exactly what we're looking for.

Read-only against HubSpot except the two ai_sdr_deal_created batch updates.

  python3 aisdr_attribution_sync.py [--json] [--dry-run] [--full] [--limit N]
      [--sleep SECS]
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))  # hubspot_client, mongo_store

from hubspot_client import HubSpotClient, HubSpotError, to_ms_epoch  # noqa: E402
import mongo_store  # noqa: E402

BODY_CAP = 60000        # same body cap convention as hubspot_activity_sync.py
WINDOW_SOFT_CAP = 9800  # re-window before HubSpot's 10k search-result ceiling
OVERLAP_MS = 10 * 60 * 1000  # re-scan 10 min behind the watermark each night

EMAIL_PROPS = ["hs_timestamp", "hs_email_subject", "hs_email_text",
               "hs_email_direction", "hs_email_status"]
DEAL_PROPS = ["dealname", "createdate", "dealstage", "amount",
              "hubspot_owner_id", "hs_created_by_user_id", "ai_sdr_deal_created"]

MISSING_SCOPE_MSG = ("HubSpot token cannot read email engagements (HTTP 403) — add the "
                     "sales-email-read scope to the private app in HubSpot (Settings > "
                     "Integrations > Private Apps), regenerate the token, and update "
                     "HUBSPOT_ACCESS_TOKEN on Railway.")


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def hs_bool(v):
    return str(v).strip().lower() == "true"


def contact_props():
    linkedin = os.environ.get("HUBSPOT_LINKEDIN_PROPERTY", "hs_linkedin_url")
    return ["firstname", "lastname", "email", "jobtitle", linkedin, "company",
            "ai_sdr_deal_created", "ai_sdr_meeting_booked", "ai_sdr_reply_generated"], linkedin


# --------------------------------------------------------------------------
# HubSpot fetch steps
# --------------------------------------------------------------------------

def fetch_label_maps(client):
    """{value: label} maps for dealstage, deal owner and created-by user ids.
    Enumeration options carry the display names — no extra API scopes needed."""
    maps = {}
    for prop in ("dealstage", "hubspot_owner_id", "hs_created_by_user_id"):
        options = (client.get_property("deals", prop) or {}).get("options") or []
        maps[prop] = {str(o.get("value")): o.get("label") for o in options}
    return maps


def fetch_emails(client, from_email, start_ms, *, sleep, limit=None):
    """All AI SDR email engagements with hs_timestamp > start_ms, ascending, as
    normalized dicts. Windows past HubSpot's 10k search cap by restarting the
    GT filter at the max timestamp seen. Yields (docs, max_ts)."""
    sorts = [{"propertyName": "hs_timestamp", "direction": "ASCENDING"}]
    docs, window_start, max_ts = {}, int(start_ms), int(start_ms)
    while True:
        filters = [
            {"propertyName": "hs_email_from_email", "operator": "EQ", "value": from_email},
            {"propertyName": "hs_timestamp", "operator": "GT", "value": str(window_start)},
        ]
        after, window_count = None, 0
        while True:
            results, after, _total = client.search_page(
                "emails", filters=filters, properties=EMAIL_PROPS, after=after,
                sorts=sorts, limit=100)
            for r in results:
                p = r.get("properties") or {}
                ms = to_ms_epoch(p.get("hs_timestamp"))
                if ms is None:
                    continue
                max_ts = max(max_ts, ms)
                docs[str(r.get("id"))] = {
                    "_id": str(r.get("id")),
                    "timestamp": ms,
                    "subject": p.get("hs_email_subject"),
                    "text": (p.get("hs_email_text") or "")[:BODY_CAP],
                    "direction": p.get("hs_email_direction"),
                    "status": p.get("hs_email_status"),
                }
            window_count += len(results)
            if limit and len(docs) >= limit:
                return list(docs.values()), max_ts
            if not after:
                return list(docs.values()), max_ts
            if window_count >= WINDOW_SOFT_CAP:
                break  # restart pagination with a later GT bound
            time.sleep(sleep)
        if max_ts <= window_start:
            # >9.8k results sharing one millisecond — impossible in practice, but
            # bail rather than loop forever.
            print(f"[warn] window made no timestamp progress at {window_start}; stopping",
                  flush=True)
            return list(docs.values()), max_ts
        window_start = max_ts


# --------------------------------------------------------------------------
# Attribution (pure — unit-testable with plain dicts)
# --------------------------------------------------------------------------

def compute_attribution(emails, deals):
    """emails: [{timestamp, contact_ids: [...]}]; deals: [{_id, createdate,
    contact_ids: [...]}]. Returns (first_email_by_contact, attributed_deal_ids,
    flagged_contact_ids). A deal is attributed iff ANY associated contact's first
    AI SDR email is at or before the deal's create date; a contact is flagged iff
    at least one of its deals is attributed."""
    first = {}
    for e in emails:
        ts = e.get("timestamp")
        if ts is None:
            continue
        for cid in e.get("contact_ids") or []:
            if cid not in first or ts < first[cid]:
                first[cid] = ts
    attributed, flagged = set(), set()
    for d in deals:
        created = d.get("createdate")
        if created is None:
            continue
        for cid in d.get("contact_ids") or []:
            ts = first.get(cid)
            if ts is not None and created >= ts:
                attributed.add(d["_id"])
                flagged.add(cid)
    # flag every contact on an attributed deal only if IT was emailed and predates
    # the deal — already guaranteed by the loop above (cid added per qualifying hit).
    return first, attributed, flagged


# --------------------------------------------------------------------------
# The sync run
# --------------------------------------------------------------------------

def run_sync(client, db, *, full=False, dry_run=False, limit=None, sleep=0.25):
    from pymongo import UpdateOne  # lazy: script only runs where pymongo exists

    from_email = os.environ.get("HUBSPOT_AISDR_FROM_EMAIL", "ai-sdr@everworker.ai")
    counts = {"emails_new": 0, "contacts": 0, "deals": 0, "deals_attributed": 0,
              "contacts_flagged": 0, "hubspot_deal_updates": 0,
              "hubspot_contact_updates": 0}
    started = now_iso()
    mongo_store.ensure_indexes(db)
    state = mongo_store.get_sync_state(db)

    def finalize(ok, error=None, watermark=None):
        update = {"last_run_at": now_iso(), "last_run_started_at": started,
                  "last_run_ok": ok, "last_error": error, "counts": counts}
        if ok:
            update["seeded"] = True
        if watermark is not None:
            update["watermark_ms"] = watermark
        db.sync_state.update_one({"_id": mongo_store.STATE_ID},
                                 {"$set": update}, upsert=True)

    # 1. Preflight: can this token read email engagements at all?
    try:
        client.search_page("emails", filters=[
            {"propertyName": "hs_email_from_email", "operator": "EQ", "value": from_email},
        ], properties=["hs_timestamp"], limit=1)
    except HubSpotError as e:
        if "HTTP 403" in str(e):
            finalize(False, MISSING_SCOPE_MSG)
            return {"ok": False, "error_kind": "missing_scope", "error": MISSING_SCOPE_MSG}
        raise

    # 2. Label maps for deal stage / owner / created-by display names.
    labels = fetch_label_maps(client)

    # 3-4. Fetch new emails since the watermark and upsert them.
    if full or not state or not state.get("watermark_ms"):
        start_ms = 0
        print(f"[aisdr] seed run: fetching ALL emails from {from_email}", flush=True)
    else:
        start_ms = max(0, int(state["watermark_ms"]) - OVERLAP_MS)
        print(f"[aisdr] incremental run: emails after {start_ms}", flush=True)
    email_docs, max_ts = fetch_emails(client, from_email, start_ms, sleep=sleep, limit=limit)
    counts["emails_new"] = len(email_docs)
    print(f"[aisdr] fetched {len(email_docs)} email engagements", flush=True)
    if email_docs:
        ops = [UpdateOne({"_id": d["_id"]},
                         {"$set": {**d, "synced_at": now_iso()}}, upsert=True)
               for d in email_docs]
        for i in range(0, len(ops), 500):
            db.emails.bulk_write(ops[i:i + 500], ordered=False)

    # 5. Associations for the new emails: contacts (primary) + deals (supplementary).
    new_ids = [d["_id"] for d in email_docs]
    email_contacts = client.batch_read_associations("emails", "contacts", new_ids) if new_ids else {}
    email_deals = client.batch_read_associations("emails", "deals", new_ids) if new_ids else {}
    if new_ids:
        ops = [UpdateOne({"_id": eid},
                         {"$set": {"contact_ids": email_contacts.get(eid, []),
                                   "deal_ids": email_deals.get(eid, [])}})
               for eid in new_ids]
        for i in range(0, len(ops), 500):
            db.emails.bulk_write(ops[i:i + 500], ordered=False)

    # 6. Contact -> deal sweep over ALL known contacts (not just this delta):
    # deals created long after the emails are precisely what attribution is for.
    all_contact_ids = sorted({cid for ids in db.emails.distinct("contact_ids") or []
                              for cid in ([ids] if isinstance(ids, str) else ids)})
    contact_deals = client.batch_read_associations("contacts", "deals", all_contact_ids) \
        if all_contact_ids else {}
    all_deal_ids = sorted(
        {did for ids in contact_deals.values() for did in ids}
        | {did for ids in db.emails.distinct("deal_ids") or []
           for did in ([ids] if isinstance(ids, str) else ids)})
    print(f"[aisdr] {len(all_contact_ids)} contacts, {len(all_deal_ids)} candidate deals",
          flush=True)

    # 7. Snapshot contacts + deals (fresh every run so stages/amounts/flags stay live).
    cprops, linkedin_prop = contact_props()
    deal_to_contacts = {}
    for cid, dids in contact_deals.items():
        for did in dids:
            deal_to_contacts.setdefault(did, set()).add(cid)
    # Direct email->deal links inherit the email's contacts as the join anchor —
    # merged from ALL stored emails (not just this delta) so recomputing a deal's
    # contact_ids never drops anchors discovered in earlier runs.
    for e in db.emails.find({"deal_ids": {"$exists": True, "$ne": []}},
                            {"contact_ids": 1, "deal_ids": 1}):
        for did in e.get("deal_ids") or []:
            deal_to_contacts.setdefault(did, set()).update(e.get("contact_ids") or [])

    contact_rows = client.batch_read_contacts(all_contact_ids, cprops) if all_contact_ids else []
    ops = []
    for r in contact_rows:
        p = r.get("properties") or {}
        cid = str(r.get("id"))
        ops.append(UpdateOne({"_id": cid}, {"$set": {
            "firstname": p.get("firstname"), "lastname": p.get("lastname"),
            "name": " ".join(x for x in (p.get("firstname"), p.get("lastname")) if x) or None,
            "email": p.get("email"), "jobtitle": p.get("jobtitle"),
            "linkedin_url": p.get(linkedin_prop), "company": p.get("company"),
            "ai_sdr_meeting_booked": hs_bool(p.get("ai_sdr_meeting_booked")),
            "ai_sdr_reply_generated": hs_bool(p.get("ai_sdr_reply_generated")),
            "hs_ai_sdr_deal_created": hs_bool(p.get("ai_sdr_deal_created")),
            "deal_ids": contact_deals.get(cid, []),
            "synced_at": now_iso(),
        }}, upsert=True))
    if ops:
        db.contacts.bulk_write(ops, ordered=False)
    counts["contacts"] = len(contact_rows)

    deal_rows = client.batch_read_objects("deals", all_deal_ids, DEAL_PROPS) if all_deal_ids else []
    ops = []
    for r in deal_rows:
        p = r.get("properties") or {}
        did = str(r.get("id"))
        owner = p.get("hubspot_owner_id")
        creator = p.get("hs_created_by_user_id")
        ops.append(UpdateOne({"_id": did}, {"$set": {
            "dealname": p.get("dealname"),
            "createdate": to_ms_epoch(p.get("createdate")),
            "dealstage": p.get("dealstage"),
            "dealstage_label": labels["dealstage"].get(str(p.get("dealstage"))) or p.get("dealstage"),
            # amount is portal-currency; assumes a single-currency portal (upgrade
            # path for multi-currency: read amount_in_home_currency instead).
            "amount": float(p["amount"]) if p.get("amount") not in (None, "") else None,
            "hubspot_owner_id": owner,
            "owner_name": labels["hubspot_owner_id"].get(str(owner)) if owner else None,
            "hs_created_by_user_id": creator,
            "creator_name": labels["hs_created_by_user_id"].get(str(creator)) if creator else None,
            "hs_ai_sdr_deal_created": hs_bool(p.get("ai_sdr_deal_created")),
            "contact_ids": sorted(deal_to_contacts.get(did, set())),
            "synced_at": now_iso(),
        }}, upsert=True))
    if ops:
        db.deals.bulk_write(ops, ordered=False)
    counts["deals"] = len(deal_rows)

    # 8. Attribution over the WHOLE store (emails may predate this run's delta).
    emails_all = list(db.emails.find({}, {"timestamp": 1, "contact_ids": 1}))
    deals_all = list(db.deals.find({}, {"createdate": 1, "contact_ids": 1,
                                        "hs_ai_sdr_deal_created": 1}))
    first, attributed, flagged = compute_attribution(emails_all, deals_all)
    counts["deals_attributed"] = len(attributed)
    counts["contacts_flagged"] = len(flagged)
    print(f"[aisdr] attribution: {len(attributed)} deals, {len(flagged)} contacts",
          flush=True)

    contact_first = [UpdateOne({"_id": cid}, {"$set": {"first_aisdr_email_at": ts}})
                     for cid, ts in first.items()]
    if contact_first:
        db.contacts.bulk_write(contact_first, ordered=False)
    deal_flags = [UpdateOne({"_id": d["_id"]},
                            {"$set": {"ai_sdr_deal_created": d["_id"] in attributed}})
                  for d in deals_all]
    if deal_flags:
        db.deals.bulk_write(deal_flags, ordered=False)
    contact_flags = [UpdateOne({"_id": c["_id"]},
                               {"$set": {"ai_sdr_deal_created": c["_id"] in flagged}})
                     for c in db.contacts.find({}, {"_id": 1})]
    if contact_flags:
        db.contacts.bulk_write(contact_flags, ordered=False)

    # 9. Write ai_sdr_deal_created=true back to HubSpot where it isn't already.
    deal_updates = [{"id": d["_id"], "properties": {"ai_sdr_deal_created": "true"}}
                    for d in deals_all
                    if d["_id"] in attributed and not d.get("hs_ai_sdr_deal_created")]
    contact_updates = [{"id": c["_id"], "properties": {"ai_sdr_deal_created": "true"}}
                       for c in db.contacts.find({"ai_sdr_deal_created": True,
                                                  "hs_ai_sdr_deal_created": {"$ne": True}},
                                                 {"_id": 1})]
    if dry_run:
        print(f"[aisdr] dry-run: would update {len(deal_updates)} deals, "
              f"{len(contact_updates)} contacts in HubSpot", flush=True)
    else:
        if deal_updates:
            counts["hubspot_deal_updates"] = client.batch_update("deals", deal_updates)
            db.deals.bulk_write([UpdateOne({"_id": u["id"]},
                                           {"$set": {"hs_ai_sdr_deal_created": True}})
                                 for u in deal_updates], ordered=False)
        if contact_updates:
            counts["hubspot_contact_updates"] = client.batch_update("contacts", contact_updates)
            db.contacts.bulk_write([UpdateOne({"_id": u["id"]},
                                              {"$set": {"hs_ai_sdr_deal_created": True}})
                                    for u in contact_updates], ordered=False)
        print(f"[aisdr] HubSpot updated: {counts['hubspot_deal_updates']} deals, "
              f"{counts['hubspot_contact_updates']} contacts", flush=True)

    # 10. Advance the watermark and record the run.
    prev = int(state.get("watermark_ms") or 0) if state else 0
    finalize(True, None, watermark=max(prev, max_ts))
    return {"ok": True, "dry_run": dry_run, **counts}


def main(argv=None):
    ap = argparse.ArgumentParser(description="AI SDR deal attribution sync (HubSpot -> MongoDB)")
    ap.add_argument("--json", action="store_true", help="print the summary as JSON (last line)")
    ap.add_argument("--dry-run", action="store_true", help="no HubSpot write-backs")
    ap.add_argument("--full", action="store_true", help="ignore the watermark; re-scan everything")
    ap.add_argument("--limit", type=int, default=None, help="cap emails fetched (smoke test)")
    ap.add_argument("--sleep", type=float, default=0.25,
                    help="pause between search pages (default 0.25s ~= 4 req/s)")
    args = ap.parse_args(argv)

    try:
        client = HubSpotClient()
        db = mongo_store.get_db()
        summary = run_sync(client, db, full=args.full, dry_run=args.dry_run,
                           limit=args.limit, sleep=args.sleep)
    except Exception as e:  # noqa: BLE001 — surface everything as a clean summary
        summary = {"ok": False, "error": str(e)[:500]}
        try:
            db = mongo_store.get_db()
            db.sync_state.update_one(
                {"_id": mongo_store.STATE_ID},
                {"$set": {"last_run_at": now_iso(), "last_run_ok": False,
                          "last_error": str(e)[:500]}}, upsert=True)
        except Exception:  # noqa: BLE001 — Mongo itself may be the failure
            pass
    print(json.dumps(summary) if args.json else summary)
    return 0 if summary.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
