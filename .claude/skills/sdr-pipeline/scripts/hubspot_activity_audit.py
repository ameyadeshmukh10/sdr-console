"""Audit HubSpot email/LinkedIn engagements for duplicates — READ-ONLY.

Diagnoses the "same email logged 4-5 times" complaint: for each audited contact it
pulls every email engagement (and LinkedIn communication) on the record, clusters
them by timestamp proximity + normalized subject, and labels each engagement's
source — `ours` (its engagement id is in our hubspot_activity_log ledger) vs
`other` (HubSpot native inbox/BCC logging, HeyReach's native sync, a human, or a
past run whose ledger was lost).

Reading the report:
- clusters with ours+other at ~the same minute  -> overlapping loggers (disable one:
  the native inbox logging for SDR mailboxes, or HEYREACH_ACTIVITY_AUTOSYNC=0 for
  LinkedIn, or rely on ours and turn the native one off).
- clusters that are all `other` with createdates days apart -> historical re-log
  storms from a wiped ledger (scars; recoverable by deleting the extra engagements).
- clusters that are all `ours` -> a ledger/dedup bug (report it).

  python3 hubspot_activity_audit.py --contact-id 123 [--contact-id 456 ...]
  python3 hubspot_activity_audit.py --sample 5 [--window-secs 90] [--json]
"""

import argparse
import json
import re
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))

import batch_db as db  # noqa: E402
from hubspot_client import HubSpotClient, HubSpotError  # noqa: E402

EMAIL_PROPS = ["hs_timestamp", "hs_email_subject", "hs_email_direction", "hs_createdate"]
COMM_PROPS = ["hs_timestamp", "hs_communication_channel_type", "hs_createdate", "hs_body_preview"]


def norm_subject(s):
    s = (s or "").lower().strip()
    return re.sub(r"^(re|fwd?):\s*", "", s).strip()


def to_ms(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        pass
    from hubspot_client import to_ms_epoch
    return to_ms_epoch(v) or 0


def cluster(engagements, window_ms):
    """Group engagements on one contact by (normalized subject, timestamp bucket)."""
    engagements = sorted(engagements, key=lambda e: e["ts"])
    clusters = []
    for e in engagements:
        placed = False
        for c in clusters:
            if c["subject"] == e["subject"] and abs(e["ts"] - c["members"][-1]["ts"]) <= window_ms:
                c["members"].append(e)
                placed = True
                break
        if not placed:
            clusters.append({"subject": e["subject"], "members": [e]})
    return clusters


def audit_contact(hs, ours_ids, contact_id, window_ms):
    rows = []
    for r in hs.iter_contact_engagements("emails", contact_id, EMAIL_PROPS):
        p = r.get("properties") or {}
        rows.append({
            "id": str(r.get("id")), "kind": "email",
            "ts": to_ms(p.get("hs_timestamp")),
            "subject": norm_subject(p.get("hs_email_subject")),
            "direction": p.get("hs_email_direction"),
            "created": p.get("hs_createdate"),
            "source": "ours" if str(r.get("id")) in ours_ids else "other",
        })
    try:
        for r in hs.iter_contact_engagements("communications", contact_id, COMM_PROPS):
            p = r.get("properties") or {}
            rows.append({
                "id": str(r.get("id")), "kind": "communication",
                "ts": to_ms(p.get("hs_timestamp")),
                "subject": norm_subject((p.get("hs_body_preview") or "")[:60]),
                "direction": p.get("hs_communication_channel_type"),
                "created": p.get("hs_createdate"),
                "source": "ours" if str(r.get("id")) in ours_ids else "other",
            })
    except HubSpotError:
        pass  # portal may not expose communications search — email audit still stands

    dupes = []
    for c in cluster(rows, window_ms):
        if len(c["members"]) < 2:
            continue
        sources = sorted({m["source"] for m in c["members"]})
        created = sorted({(m["created"] or "")[:10] for m in c["members"]})
        verdict = ("overlapping_loggers" if sources == ["other", "ours"]
                   else "ledger_loss_or_manual" if sources == ["other"]
                   else "dedup_bug")
        dupes.append({
            "subject": c["subject"] or "(no subject)",
            "count": len(c["members"]),
            "sources": sources,
            "created_days": created,
            "verdict": verdict,
            "engagements": [{k: m[k] for k in ("id", "kind", "direction", "source", "created")}
                            for m in c["members"]],
        })
    return {"contact_id": str(contact_id), "engagements": len(rows), "duplicate_clusters": dupes}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--contact-id", action="append", default=[])
    ap.add_argument("--sample", type=int, default=0,
                    help="audit N contacts sampled from the activity ledger")
    ap.add_argument("--window-secs", type=int, default=60)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    hs = HubSpotClient()
    conn = db.connect()
    db.init_schema(conn)
    ours_ids = {str(r[0]) for r in conn.execute(
        "SELECT engagement_id FROM hubspot_activity_log "
        "WHERE engagement_id IS NOT NULL AND status='logged'")}

    contact_ids = list(args.contact_id)
    if args.sample and not contact_ids:
        contact_ids = [str(r[0]) for r in conn.execute(
            "SELECT DISTINCT contact_id FROM hubspot_activity_log "
            "WHERE contact_id IS NOT NULL AND status='logged' "
            "ORDER BY created_at DESC LIMIT ?", (args.sample,))]
    if not contact_ids:
        print("no contacts to audit — pass --contact-id or --sample N", file=sys.stderr)
        return 1

    report = {"ok": True, "ledger_engagements": len(ours_ids),
              "window_secs": args.window_secs, "contacts": []}
    for cid in contact_ids:
        try:
            report["contacts"].append(
                audit_contact(hs, ours_ids, cid, args.window_secs * 1000))
        except HubSpotError as e:
            report["contacts"].append({"contact_id": str(cid), "error": str(e)[:300]})

    total_dupes = sum(len(c.get("duplicate_clusters") or []) for c in report["contacts"])
    report["duplicate_clusters_total"] = total_dupes
    if args.json:
        print(json.dumps(report))
        return 0
    for c in report["contacts"]:
        if c.get("error"):
            print(f"contact {c['contact_id']}: ERROR {c['error']}")
            continue
        print(f"contact {c['contact_id']}: {c['engagements']} engagements, "
              f"{len(c['duplicate_clusters'])} duplicate cluster(s)")
        for d in c["duplicate_clusters"]:
            print(f"  x{d['count']}  {d['subject'][:60]!r}  sources={'+'.join(d['sources'])} "
                  f"created={','.join(d['created_days'])}  -> {d['verdict']}")
    print(f"\ntotal duplicate clusters: {total_dupes}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
