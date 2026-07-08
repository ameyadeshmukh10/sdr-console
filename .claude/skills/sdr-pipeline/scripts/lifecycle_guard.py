"""Lifecycle guard — stop AI-SDR outreach to contacts who have moved to a gated
HubSpot lifecycle stage (by default `opportunity` or `customer`), across both
Email Bison (email) and HeyReach (LinkedIn).

Why this exists: the pipeline enrolls a contact once (while they're a `lead`) and
never re-checks their lifecycle stage, so a contact keeps getting emailed after
they become an Opportunity or Customer. This guard reconciles the enrolled set
against HubSpot and stops anyone who has advanced.

Two uses:
  • Immediate / one-off cleanup of the already-enrolled set  (run with --once).
  • Daily pre-send-window reconcile, scheduled from webui/server/app.py.

For each enrolled contact now in a gated stage:
  • Email:  find the Bison lead by email → stop it (unsubscribe | blacklist | remove).
  • LinkedIn: stop the lead in the HeyReach campaign (only when HeyReach is
              configured at runtime; otherwise a no-op).
  • Mark the contact `gated` in pipeline.db so no enroll path re-enrolls it.

Safety:
  • HubSpot is only READ (batch_read_contacts). The only writes are the Bison /
    HeyReach per-lead stops and the local DB status.
  • It NEVER pauses a whole campaign — every action is scoped to one lead, so other
    outreach is untouched.
  • --dry-run does the HubSpot lookup and prints exactly what it would stop, touching
    nothing external.
  • `unsubscribe` (the default) is reversible and keeps the lead record.

Run:
  python3 .claude/skills/sdr-pipeline/scripts/lifecycle_guard.py --once [--dry-run]
      [--mode unsubscribe|blacklist|remove]
      [--stages opportunity,customer]
      [--statuses enrolled]
      [--only-emails a@b.com,c@d.com]
      [--json]

Env (all optional):
  GATED_LIFECYCLE_STAGES   default "opportunity,customer"
  LIFECYCLE_GUARD_MODE     default "unsubscribe"
"""

import argparse
import json
import os
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))                                   # hubspot_client, heyreach_client, batch_db
sys.path.insert(0, str(SCRIPTS.parents[1] / "email-bison" / "scripts"))  # bison_client

import batch_db as db                                             # noqa: E402
from hubspot_client import HubSpotClient, HubSpotError            # noqa: E402
from heyreach_client import HeyReachClient, HeyReachError         # noqa: E402


def _load_dotenv():
    """Load .env walking up from this script so config vars resolve in dry-run too.
    Real environment variables always win (setdefault)."""
    for parent in [SCRIPTS, *SCRIPTS.parents]:
        env_path = parent / ".env"
        if env_path.is_file():
            for raw in env_path.read_text().splitlines():
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                os.environ.setdefault(k.strip(), v.split(" #")[0].strip().strip('"').strip("'"))
            return


def gated_stages():
    raw = os.environ.get("GATED_LIFECYCLE_STAGES", "opportunity,customer")
    return {s.strip().lower() for s in raw.split(",") if s.strip()}


def _lifecycle_by_id(hub, contact_ids):
    """Read lifecyclestage for many contact ids. Returns {contact_id: stage_lower}.
    Reuses batch_read_contacts (chunks of 100). Ids HubSpot can't resolve are omitted."""
    out = {}
    for rec in hub.batch_read_contacts(contact_ids, ["lifecyclestage", "email"]):
        stage = ((rec.get("properties") or {}).get("lifecyclestage") or "").strip().lower()
        out[str(rec.get("id"))] = stage
    return out


def _stop_email(bison, email, mode, log):
    """Stop a single lead's email in Bison. Returns an outcome string.
    `unsubscribe` (default) and `blacklist` need only the lead id; `remove` needs a
    campaign, so it falls back to unsubscribe when the campaign can't be resolved
    (so the lead is never left still sending)."""
    lead = bison.find_lead_by_email(email)
    if not lead:
        return "not_in_bison"
    lead_id = lead.get("id")
    if mode == "blacklist":
        bison.unsubscribe_lead(lead_id)      # stop scheduled sends first
        bison.blacklist_lead(lead_id)        # then global suppression
        return "blacklisted"
    if mode == "remove":
        campaigns = lead.get("campaigns") or lead.get("campaign_ids") or []
        cids = [c.get("id") if isinstance(c, dict) else c for c in campaigns]
        cids = [c for c in cids if c]
        if cids:
            for cid in cids:
                bison.remove_leads_from_campaign(cid, [lead_id])
            return f"removed_from_{len(cids)}_campaign(s)"
        bison.unsubscribe_lead(lead_id)      # no campaign resolvable → guarantee a stop
        log(f"      (no campaign on lead {lead_id}; fell back to unsubscribe)")
        return "unsubscribed_fallback"
    bison.unsubscribe_lead(lead_id)          # default: reversible
    return "unsubscribed"


def _stop_linkedin(heyreach, hr_campaign, profile_url, log):
    """Best-effort LinkedIn stop for one lead in the HeyReach campaign."""
    try:
        heyreach.stop_lead_in_campaign(hr_campaign, profile_url=profile_url)
        return "stopped"
    except HeyReachError as e:
        log(f"      ! HeyReach stop failed for {profile_url}: {str(e)[:120]}")
        return "hr_error"


def run(args):
    _load_dotenv()
    stages = {s.strip().lower() for s in (args.stages.split(",") if args.stages else [])} or gated_stages()
    mode = (args.mode or os.environ.get("LIFECYCLE_GUARD_MODE") or "unsubscribe").strip().lower()
    if mode not in ("unsubscribe", "blacklist", "remove"):
        print(f"ERROR: invalid --mode {mode!r} (use unsubscribe|blacklist|remove)")
        return 2
    statuses = [s.strip() for s in (args.statuses or "enrolled").split(",") if s.strip()]
    only_emails = {e.strip().lower() for e in (args.only_emails or "").split(",") if e.strip()}

    logs = []
    def log(msg):
        logs.append(msg)
        if not args.json:
            print(msg)

    # 1. Candidate set: contacts currently in active outreach (default status=enrolled).
    conn = db.connect(); db.init_schema(conn)
    candidates = []
    for st in statuses:
        candidates += db.contacts_by_status(conn, st)
    if only_emails:
        candidates = [c for c in candidates if (c.get("email") or "").lower() in only_emails]
    log(f"guard: {len(candidates)} candidate contact(s) (status in {statuses}), "
        f"gated stages={sorted(stages)}, mode={mode}{' [DRY-RUN]' if args.dry_run else ''}")
    if not candidates:
        summary = {"candidates": 0, "gated": 0, "stopped_email": 0, "stopped_linkedin": 0}
        if args.json:
            print(json.dumps({"summary": summary, "affected": []}))
        return 0

    # 2. Read current lifecycle stage from HubSpot (READ-ONLY).
    try:
        hub = HubSpotClient()
        stage_by_id = _lifecycle_by_id(hub, [c["contact_id"] for c in candidates])
    except HubSpotError as e:
        print(f"ERROR reading HubSpot lifecycle: {e}")
        return 1

    affected = [c for c in candidates if stage_by_id.get(str(c["contact_id"]), "") in stages]
    log(f"guard: {len(affected)} contact(s) have advanced to a gated stage.")
    if not affected:
        summary = {"candidates": len(candidates), "gated": 0, "stopped_email": 0, "stopped_linkedin": 0}
        if args.json:
            print(json.dumps({"summary": summary, "affected": []}))
        return 0

    # 3. Clients only when actually acting (dry-run stays read-only).
    hr_campaign = os.environ.get("HEYREACH_CAMPAIGN_ID")
    hr_enabled = bool(hr_campaign)
    bison = heyreach = None
    if not args.dry_run:
        from bison_client import BisonClient, BisonError  # noqa: E402
        try:
            bison = BisonClient()
        except BisonError as e:
            print(f"ERROR: cannot reach Email Bison to stop leads: {e}")
            return 1
        if hr_enabled:
            try:
                heyreach = HeyReachClient()
            except HeyReachError as e:
                log(f"  ! HeyReach configured but client init failed ({str(e)[:80]}); "
                    f"skipping LinkedIn stops.")
                hr_enabled = False

    # 4. Stop each affected lead (email + optional LinkedIn), then mark it gated.
    counts = {"candidates": len(candidates), "gated": len(affected),
              "stopped_email": 0, "stopped_linkedin": 0, "email_not_in_bison": 0, "errors": 0}
    report = []
    for c in affected:
        email = c.get("email") or ""
        stage = stage_by_id.get(str(c["contact_id"]), "")
        li = (c.get("linkedin_url") or "").strip()
        row = {"contact_id": c["contact_id"], "email": email, "persona": c.get("persona"),
               "lifecyclestage": stage, "email_action": None, "linkedin_action": None}
        prefix = f"  • {email} [{c.get('persona')}] now '{stage}':"

        # Email
        if args.dry_run:
            row["email_action"] = f"would {mode}"
            log(f"{prefix} would {mode} in Bison" + (f" + stop LinkedIn" if (hr_enabled and li) else ""))
        else:
            try:
                outcome = _stop_email(bison, email, mode, log)
                row["email_action"] = outcome
                if outcome == "not_in_bison":
                    counts["email_not_in_bison"] += 1
                    log(f"{prefix} not found in Bison (nothing to stop by email)")
                else:
                    counts["stopped_email"] += 1
                    log(f"{prefix} email {outcome}")
            except Exception as e:  # noqa: BLE001 — one bad lead must not abort the sweep
                counts["errors"] += 1
                row["email_action"] = f"error: {str(e)[:120]}"
                log(f"{prefix} EMAIL STOP FAILED: {str(e)[:150]}")

        # LinkedIn (best-effort, only when HeyReach is live and we have a profile url)
        if hr_enabled and li:
            if args.dry_run:
                row["linkedin_action"] = "would stop"
            else:
                res = _stop_linkedin(heyreach, hr_campaign, li, log)
                row["linkedin_action"] = res
                if res == "stopped":
                    counts["stopped_linkedin"] += 1

        # Local state — never re-enroll this contact
        if not args.dry_run:
            db.set_contact_status(conn, c["contact_id"], "gated",
                                  error=f"lifecycle={stage}; email={row['email_action']}")
        report.append(row)

    log(f"guard done: {json.dumps(counts)}")
    if args.json:
        print(json.dumps({"summary": counts, "affected": report}))
    return 0


def main():
    ap = argparse.ArgumentParser(description="Stop outreach to contacts now in a gated lifecycle stage.")
    ap.add_argument("--once", action="store_true", help="run a single reconcile pass now (default behavior)")
    ap.add_argument("--dry-run", action="store_true", help="identify + print, but stop nothing")
    ap.add_argument("--mode", default=None, help="unsubscribe (default) | blacklist | remove")
    ap.add_argument("--stages", default=None, help="comma list, default env GATED_LIFECYCLE_STAGES or opportunity,customer")
    ap.add_argument("--statuses", default="enrolled", help="pipeline.db statuses to reconcile (default: enrolled)")
    ap.add_argument("--only-emails", default=None, help="restrict to these emails (comma list) — e.g. a first targeted run")
    ap.add_argument("--json", action="store_true", help="emit a JSON summary line (for the scheduler)")
    args = ap.parse_args()
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
