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
import concurrent.futures
import json
import sys
from collections import defaultdict
from pathlib import Path

ENROLL_WORKERS = 8   # concurrent Bison create_lead calls

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

    # Idempotent (INSERT OR IGNORE; batches only WHERE batch_id IS NULL), so a
    # "database is locked" loss to the server's background writers just retries
    # on a fresh connection.
    def _run():
        conn = db.connect()
        try:
            db.init_schema(conn)
            added = db.upsert_contacts(conn, rows)
            made = db.assign_batches(conn, args.batch_size)
            return added, made, db.counts(conn)
        finally:
            conn.close()

    added, made, c = db.retry_locked(_run)
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
    import generate_batch as G  # variant-aware linter (reads asset['variant'])
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
            issues = G.lint_assets(asset)
            conn.execute("UPDATE contacts SET variant=? WHERE contact_id=?",
                         (asset.get("variant", "value-give"), r["contact_id"]))
            conn.commit()
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


def _upsert_env(updates):
    """Insert or replace KEY=value lines in the project .env (preserves the rest)."""
    env_path = E.PROJECT_ROOT / ".env"
    lines = env_path.read_text().splitlines() if env_path.is_file() else []
    out, seen = [], set()
    for ln in lines:
        key = ln.split("=", 1)[0].strip() if ("=" in ln and not ln.strip().startswith("#")) else None
        if key in updates:
            out.append(f"{key}={updates[key]}")
            seen.add(key)
        else:
            out.append(ln)
    for k, v in updates.items():
        if k not in seen:
            out.append(f"{k}={v}")
    env_path.write_text("\n".join(out) + "\n")


VARIANTS_ORDER = ["value-give", "earn", "show"]


def cmd_setup_variant_campaigns(args):
    """Create one dedicated Bison campaign per instruction variant by duplicating a
    template campaign, then write the ids to .env. Idempotent (skips configured ones)."""
    import os
    E._load_dotenv()
    from bison_client import BisonClient  # noqa: E402
    bison = BisonClient()
    template = args.template or os.environ.get("BISON_CAMPAIGN_SALES_LEADERSHIP") or os.environ.get("BISON_CAMPAIGN_ID")
    if not template:
        print("no template campaign id — pass --template N (a campaign with the 4-step sequence)")
        return 1
    updates, mapping = {}, {}
    for v in VARIANTS_ORDER:
        envkey = E.VARIANT_CAMPAIGN_ENV[v]
        existing = os.environ.get(envkey)
        if existing and not args.force:
            print(f"  {v:11} already configured -> campaign {existing} (skip; --force to recreate)")
            mapping[v] = existing
            continue
        new = bison.duplicate_campaign(template)
        cid = new.get("id")
        name = f"AI SDR Test - {v}"
        try:
            bison.rename_campaign(cid, name)
        except Exception as e:  # noqa: BLE001 - naming is cosmetic, don't fail setup
            name = new.get("name") + f" (rename failed: {str(e)[:60]})"
        mapping[v] = cid
        updates[envkey] = cid
        print(f"  {v:11} duplicated template {template} -> campaign {cid} "
              f"(name '{name}', status {new.get('status')})")
    if updates:
        _upsert_env(updates)
        print(f"\nwrote {len(updates)} campaign id(s) to .env: {updates}")
    print("\nNEXT in Bison: rename each 'Copy of…' campaign to its variant, verify sender emails + "
          "schedule, then activate. Enrollment now routes by variant automatically.")
    print("variant -> campaign:", mapping)
    return 0


def cmd_enroll(args):
    import os
    conn = db.connect()
    rows = db.contacts_by_status(conn, "generated")
    if not rows:
        print("nothing to enroll (no 'generated' contacts).")
        return 0
    # HeyReach (LinkedIn) is a single campaign for everyone; enabled when both env
    # vars are set. Bison is the email channel (per-variant campaigns).
    hr_campaign = os.environ.get("HEYREACH_CAMPAIGN_ID")
    hr_accounts = E.heyreach_accounts()
    hr_enabled = bool(hr_campaign and hr_accounts)
    bison = heyreach = None
    if not args.dry_run:
        from bison_client import BisonClient  # noqa: E402
        bison = BisonClient()
        if hr_enabled:
            heyreach = E.HeyReachClient()
    counts = {"enrolled": 0, "no_campaign": 0, "missing_file": 0, "skipped": 0,
              "suppressed": 0, "linkedin": 0, "no_li": 0, "heyreach_failed": 0}

    # Suppression gate: contacts RevOps tagged everworker_tag=false must never
    # enroll. Live HubSpot check first, local unenrollment ledger as fallback.
    import unenrollment_check as U  # noqa: E402 (stdlib-only module import)
    suppressed, _live_ok = U.suppressed_set([r["contact_id"] for r in rows], conn=conn)
    if suppressed:
        kept = []
        for r in rows:
            if str(r["contact_id"]) in suppressed:
                counts["suppressed"] += 1
                if args.dry_run:
                    print(f"  [suppressed] {r['email']} — everworker_tag=false, will not enroll")
                else:
                    db.set_contact_status(conn, r["contact_id"], "skipped",
                                          error="suppressed: everworker_tag=false")
                    print(f"  [suppressed] {r['email']} — everworker_tag=false, skipped")
            else:
                kept.append(r)
        rows = kept
        if not rows:
            print(f"enroll: all contacts suppressed. {counts}")
            return 0

    # Resolve campaign + copy per contact (local, fast). Skip missing-file /
    # no-campaign here so the network phase only sees real work. Build the
    # HeyReach pair now too (local) — it's batch-added after a successful enroll.
    tasks = []         # (row, campaign, cvars)
    hr_pairs = {}      # contact_id -> HeyReach lead pair (li_url present + hr_enabled)
    for r in rows:
        p = db.GEN_DIR / f"{r['contact_id']}.json"
        if not p.is_file():
            counts["missing_file"] += 1
            continue
        asset = json.loads(p.read_text())
        # route by instruction variant (one campaign per variant); persona is the fallback
        variant = asset.get("variant") or r["variant"] or "value-give"
        campaign = E.bison_campaign_for_variant(variant) or E.bison_campaign_for(r["persona"])
        if not campaign:
            counts["no_campaign"] += 1
            continue
        tasks.append((r, campaign, E.bison_custom_vars(asset["email"])))
        if hr_enabled:
            if r["linkedin_url"]:
                cf = {k: (asset.get("linkedin") or {}).get(k, "") for k in E.LI_KEYS}
                hr_pairs[r["contact_id"]] = E.HeyReachClient.build_pair(
                    E.heyreach_account_for(hr_accounts, r["contact_id"]),
                    r["first_name"], r["last_name"], r["linkedin_url"],
                    company=r["company"], position=r["title"], email=r["email"], custom_fields=cf)
            else:
                counts["no_li"] += 1

    if args.dry_run:
        for r, campaign, cvars in tasks:
            li = " +LinkedIn" if hr_pairs.get(r["contact_id"]) else ""
            print(f"  [dry] {r['email']} [{r['persona']}] -> bison campaign {campaign} ({len(cvars)} vars){li}")
        counts["enrolled"], counts["linkedin"] = len(tasks), len(hr_pairs)
        print(f"  [dry] HEYREACH would add {len(hr_pairs)} leads -> campaign {hr_campaign}" if hr_enabled
              else "  [dry] HEYREACH disabled (set HEYREACH_CAMPAIGN_ID + HEYREACH_LINKEDIN_ACCOUNT_ID)")
        print(f"enroll (dry-run): {counts}")
        return 0

    # Phase 1 — create leads concurrently (network-bound; BisonClient is stateless
    # per call and retries on 429/5xx). Workers do NOT touch the sqlite conn — they
    # just return results; status writes + attaches happen single-threaded below.
    def create(task):
        r, campaign, cvars = task
        try:
            lead_id = bison.create_lead(first_name=r["first_name"], last_name=r["last_name"],
                                        email=r["email"], title=r["title"], company=r["company"],
                                        custom_variables=cvars)
            return ("ok", r, campaign, lead_id)
        except Exception as e:  # benign per-lead rejection (already enrolled, bounced…)
            return ("err", r, campaign, str(e))

    with concurrent.futures.ThreadPoolExecutor(max_workers=ENROLL_WORKERS) as ex:
        results = list(ex.map(create, tasks))

    # Phase 2 — batch the attach: one attach_leads_to_campaign call per campaign
    # instead of one per lead. Mark 'enrolled' only after a successful attach.
    by_campaign = defaultdict(list)  # campaign -> [(row, lead_id)]
    for status, r, campaign, payload in results:
        if status == "err":
            db.set_contact_status(conn, r["contact_id"], "skipped", error=payload[:500])
            counts["skipped"] += 1
            print(f"  [skip] {r['email']} [{r['persona']}] -> campaign {campaign}: {payload[:120]}")
        else:
            by_campaign[campaign].append((r, payload))

    hr_batch = []  # HeyReach pairs for contacts that successfully enrolled in Bison
    for campaign, items in by_campaign.items():
        lead_ids = [lid for _, lid in items]
        try:
            bison.attach_leads_to_campaign(campaign, lead_ids)
        except Exception as e:  # whole-batch attach failure: leads created, not attached
            for r, _ in items:
                db.set_contact_status(conn, r["contact_id"], "skipped", error=f"attach: {str(e)[:480]}")
                counts["skipped"] += 1
            print(f"  [skip] campaign {campaign} attach failed ({len(lead_ids)} leads): {str(e)[:120]}")
            continue
        for r, _ in items:
            db.set_contact_status(conn, r["contact_id"], "enrolled")
            counts["enrolled"] += 1
            if r["contact_id"] in hr_pairs:
                hr_batch.append(hr_pairs[r["contact_id"]])
        print(f"  attached {len(lead_ids)} leads -> bison campaign {campaign}")

    # Phase 3 — HeyReach: batch-add the LinkedIn leads to the HeyReach campaign.
    # A HeyReach failure does NOT undo the Bison enrollment — it's a second channel,
    # so we just log + count it.
    if heyreach and hr_batch:
        added = 0
        for i in range(0, len(hr_batch), E.HR_CHUNK):
            chunk = hr_batch[i:i + E.HR_CHUNK]
            try:
                heyreach.add_leads_to_campaign(hr_campaign, chunk)
                added += len(chunk)
            except Exception as e:
                counts["heyreach_failed"] += len(chunk)
                print(f"  [skip] HEYREACH add of {len(chunk)} leads failed: {str(e)[:120]}")
        counts["linkedin"] = added
        print(f"  HEYREACH added {added} leads -> campaign {hr_campaign}")
    elif not hr_enabled:
        print("  HEYREACH disabled (set HEYREACH_CAMPAIGN_ID + HEYREACH_LINKEDIN_ACCOUNT_ID to enable LinkedIn)")

    print(f"enroll: {counts}")
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


def cmd_heyreach_backfill(args):
    """Push already-enrolled contacts (Bison-only) into the HeyReach campaign.

    For catching up contacts enrolled before HeyReach was wired in. Idempotent
    via data/outreach/heyreach_state.json so re-runs skip ones already added.
    Scope with --job <batch_job_id> to limit to that batch's contacts.
    """
    import os
    hr_campaign = os.environ.get("HEYREACH_CAMPAIGN_ID")
    hr_accounts = E.heyreach_accounts()
    if not (hr_campaign and hr_accounts):
        print("HeyReach not configured — set HEYREACH_CAMPAIGN_ID + HEYREACH_LINKEDIN_ACCOUNT_ID in .env")
        return 1

    # Preflight: AddLeadsToCampaignV2 only accepts leads when the campaign is live
    # (ACTIVE/IN_PROGRESS) with senders assigned. Check once up front so we fail
    # with a clear message instead of erroring on every lead.
    try:
        camp = E.HeyReachClient().get_campaign(hr_campaign)
    except Exception as e:  # noqa: BLE001
        print(f"could not read HeyReach campaign {hr_campaign}: {str(e)[:160]}")
        return 1
    status = (camp.get("status") or "").upper()
    if status not in ("ACTIVE", "IN_PROGRESS"):
        print(f"HeyReach campaign {hr_campaign} ({camp.get('name')!r}) is {status or 'UNKNOWN'} — "
              f"activate it in HeyReach (ACTIVE/IN_PROGRESS) before adding leads.")
        return 1
    assigned = set(str(a) for a in (camp.get("campaignAccountIds") or []))
    missing = [a for a in hr_accounts if a not in assigned]
    if missing:
        print(f"warning: sender account(s) {missing} are not assigned to campaign {hr_campaign} "
              f"(assigned: {sorted(assigned)}); their leads may be rejected.")

    out_dir = db.GEN_DIR.parent
    conn = db.connect()
    cols = "contact_id, first_name, last_name, email, title, company, linkedin_url, variant"
    where = "status='enrolled' AND linkedin_url IS NOT NULL AND linkedin_url != ''"
    params = []
    if args.job:
        jp = out_dir / "batch-jobs" / f"{args.job}.json"
        if not jp.is_file():
            print(f"no batch job file: {jp}")
            return 1
        batch_ids = json.loads(jp.read_text()).get("pipeline_batch_ids") or []
        if not batch_ids:
            print(f"batch job {args.job} has no pipeline_batch_ids")
            return 1
        where += f" AND batch_id IN ({','.join('?' * len(batch_ids))})"
        params = batch_ids
    rows = [dict(r) for r in conn.execute(f"SELECT {cols} FROM contacts WHERE {where}", params)]

    state_path = out_dir / "heyreach_state.json"
    done = set()
    if state_path.is_file():
        try:
            done = set(json.loads(state_path.read_text()).get("added", []))
        except (ValueError, OSError):
            done = set()

    pairs, skipped_done, missing_file = [], 0, 0
    for r in rows:
        cid = r["contact_id"]
        if cid in done:
            skipped_done += 1
            continue
        p = db.GEN_DIR / f"{cid}.json"
        if not p.is_file():
            missing_file += 1
            continue
        asset = json.loads(p.read_text())
        cf = {k: (asset.get("linkedin") or {}).get(k, "") for k in E.LI_KEYS}
        pairs.append((cid, E.HeyReachClient.build_pair(
            E.heyreach_account_for(hr_accounts, cid),
            r["first_name"], r["last_name"], r["linkedin_url"],
            company=r["company"], position=r["title"], email=r["email"], custom_fields=cf)))

    scope = f"job {args.job}" if args.job else "all enrolled"
    print(f"heyreach-backfill [{scope}]: {len(rows)} enrolled w/ LinkedIn, "
          f"{len(pairs)} to add, {skipped_done} already done, {missing_file} missing copy")
    if args.dry_run:
        print(f"  [dry] would add {len(pairs)} leads -> HeyReach campaign {hr_campaign}")
        return 0
    if not pairs:
        return 0

    heyreach = E.HeyReachClient()
    added, failed = 0, 0
    for i in range(0, len(pairs), E.HR_CHUNK):
        chunk = pairs[i:i + E.HR_CHUNK]
        try:
            heyreach.add_leads_to_campaign(hr_campaign, [pr for _, pr in chunk])
            added += len(chunk)
            done.update(cid for cid, _ in chunk)
            state_path.write_text(json.dumps({"added": sorted(done)}, indent=2))  # persist per chunk
        except Exception as e:  # noqa: BLE001
            failed += len(chunk)
            print(f"  [skip] HEYREACH add of {len(chunk)} leads failed: {str(e)[:140]}")
    print(f"heyreach-backfill: added={added} failed={failed} skipped_done={skipped_done} missing_file={missing_file}")
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
    p = sub.add_parser("heyreach-backfill"); p.add_argument("--job"); p.add_argument("--dry-run", action="store_true"); p.set_defaults(func=cmd_heyreach_backfill)
    p = sub.add_parser("setup-variant-campaigns"); p.add_argument("--template", type=int); p.add_argument("--force", action="store_true"); p.set_defaults(func=cmd_setup_variant_campaigns)
    p = sub.add_parser("reset-batch"); p.add_argument("batch_id", type=int); p.set_defaults(func=cmd_reset_batch)
    args = ap.parse_args()
    E._load_dotenv()  # make .env config (campaign ids, keys) available to all subcommands
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
