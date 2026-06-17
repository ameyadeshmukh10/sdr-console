"""CLI for the SQLite-backed SDR batch pipeline (the terminal entrypoint).

Subcommands:
  init [--from contacts.jsonl] [--batch-size 25]   load contacts → batches of N (idempotent)
  status                                            counts by status
  pending-batches                                   space-separated pending batch ids
  get-batch <id>                                    JSON of a batch's contacts (for a sub-agent)
  ingest <id>                                       lint generated/<cid>.json for the batch → mark
                                                    generated/failed, batch done
  enroll [--dry-run]                                enroll all 'generated' contacts into Bison
  reset-batch <id>                                  set a batch + its contacts back to pending

Generated copy is written by sub-agents to data/outreach/generated/<contact_id>.json.
Run all parts deterministically from the terminal; the slash command /sdr-batches dispatches the
generation sub-agents in parallel between `init` and `enroll`.
"""

import argparse
import json
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
import batch_db as db                       # noqa: E402
import enroll as E                          # reuse lint + bison helpers  # noqa: E402

DEFAULT_CONTACTS = db.PROJECT_ROOT / "data" / "outreach" / "contacts.jsonl"


def cmd_init(args):
    src = Path(args.src) if args.src else DEFAULT_CONTACTS
    if not src.is_file():
        print(f"ERROR: {src} not found. Run hubspot_pull.py first.")
        return 1
    rows = [json.loads(l) for l in src.open() if l.strip()]
    conn = db.connect(); db.init_schema(conn)
    added = db.upsert_contacts(conn, rows)
    made = db.assign_batches(conn, args.batch_size)
    c = db.counts(conn)
    print(f"init: +{added} new contacts, +{made} new batches (size {args.batch_size})")
    print(f"total contacts: {c['total_contacts']} | batches: {c['batches_by_status']}")
    return 0


def cmd_status(args):
    conn = db.connect(); db.init_schema(conn)
    c = db.counts(conn)
    print(json.dumps(c, indent=2))
    return 0


def cmd_pending_batches(args):
    conn = db.connect(); db.init_schema(conn)
    ids = db.pending_batches(conn)
    if args.limit:
        ids = ids[:args.limit]
    print(" ".join(str(i) for i in ids))
    return 0


def cmd_get_batch(args):
    conn = db.connect()
    print(json.dumps(db.get_batch(conn, args.batch_id), ensure_ascii=False))
    return 0


def cmd_ingest(args):
    conn = db.connect()
    rows = db.get_batch(conn, args.batch_id)
    if not rows:
        print(f"batch {args.batch_id}: no contacts")
        return 1
    gen = bad = 0
    for r in rows:
        p = db.GEN_DIR / f"{r['contact_id']}.json"
        if not p.is_file():
            db.set_contact_status(conn, r["contact_id"], "failed", "no generated file")
            bad += 1
            continue
        try:
            asset = json.loads(p.read_text())
            issues = E.lint_email_assets(asset.get("email", {}))
        except Exception as e:  # noqa: BLE001
            issues = [f"unreadable json: {e}"]
        if issues:
            db.set_contact_status(conn, r["contact_id"], "failed", "; ".join(issues)[:400])
            bad += 1
        else:
            db.set_contact_status(conn, r["contact_id"], "generated")
            gen += 1
    db.set_batch_status(conn, args.batch_id, "done")
    print(f"batch {args.batch_id} ingested: {gen} generated, {bad} failed")
    return 0


def cmd_enroll(args):
    import os
    conn = db.connect()
    rows = db.contacts_by_status(conn, "generated")
    if not rows:
        print("nothing to enroll (no 'generated' contacts).")
        return 0
    bison = None
    if not args.dry_run:
        from bison_client import BisonClient  # noqa: E402
        bison = BisonClient()
    counts = {"enrolled": 0, "no_campaign": 0, "missing_file": 0, "skipped": 0}
    for r in rows:
        p = db.GEN_DIR / f"{r['contact_id']}.json"
        if not p.is_file():
            counts["missing_file"] += 1
            continue
        asset = json.loads(p.read_text())
        campaign = E.bison_campaign_for(r["persona"])
        if not campaign:
            counts["no_campaign"] += 1
            continue
        cvars = E.bison_custom_vars(asset["email"])
        if args.dry_run:
            print(f"  [dry] {r['email']} [{r['persona']}] -> campaign {campaign} ({len(cvars)} vars)")
        else:
            try:
                lead_id = bison.create_lead(first_name=r["first_name"], last_name=r["last_name"],
                                            email=r["email"], title=r["title"], company=r["company"],
                                            custom_variables=cvars)
                bison.attach_leads_to_campaign(campaign, [lead_id])
            except Exception as e:
                # Benign per-lead rejections (already in a sequence, bounced, unsubscribed)
                # should not abort the whole run — record and continue.
                db.set_contact_status(conn, r["contact_id"], "skipped", error=str(e)[:500])
                counts["skipped"] += 1
                print(f"  [skip] {r['email']} [{r['persona']}] -> campaign {campaign}: {str(e)[:120]}")
                continue
            db.set_contact_status(conn, r["contact_id"], "enrolled")
            counts["enrolled"] += 1
    print(f"enroll{' (dry-run)' if args.dry_run else ''}: {counts}")
    return 0


def cmd_reset_batch(args):
    conn = db.connect()
    for r in db.get_batch(conn, args.batch_id):
        db.set_contact_status(conn, r["contact_id"], "pending")
    conn.execute("UPDATE batches SET status='pending', claimed_at=NULL, completed_at=NULL WHERE batch_id=?",
                 (args.batch_id,))
    conn.commit()
    print(f"batch {args.batch_id} reset to pending")
    return 0


def main():
    ap = argparse.ArgumentParser(description="SDR batch pipeline")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("init"); p.add_argument("--from", dest="src"); p.add_argument("--batch-size", type=int, default=25); p.set_defaults(func=cmd_init)
    sub.add_parser("status").set_defaults(func=cmd_status)
    p = sub.add_parser("pending-batches"); p.add_argument("--limit", type=int, default=0); p.set_defaults(func=cmd_pending_batches)
    p = sub.add_parser("get-batch"); p.add_argument("batch_id", type=int); p.set_defaults(func=cmd_get_batch)
    p = sub.add_parser("ingest"); p.add_argument("batch_id", type=int); p.set_defaults(func=cmd_ingest)
    p = sub.add_parser("enroll"); p.add_argument("--dry-run", action="store_true"); p.set_defaults(func=cmd_enroll)
    p = sub.add_parser("reset-batch"); p.add_argument("batch_id", type=int); p.set_defaults(func=cmd_reset_batch)
    args = ap.parse_args()
    E._load_dotenv()  # make .env config (campaign ids, keys) available to all subcommands
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
