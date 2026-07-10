"""Use case 3: pull per-campaign denominators + per-step stats from Email Bison.

This supplies the missing denominator for the interested-trends analysis: how many
leads each campaign actually contacted, so interested-reply RATES (not just counts)
can be computed. It also pulls per-sequence-step stats so we can tell which step
truly converts best vs. merely appears most among repliers.

Outputs (data/campaign-stats/):
  campaigns.jsonl    one record per campaign (aggregate counts + rates)
  step_stats.jsonl   one record per campaign per sequence step
  last_run.json      run metadata

Run:  python3 .claude/skills/email-bison/scripts/fetch_campaign_stats.py
"""

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from bison_client import BisonClient, BisonError

PROJECT_ROOT = Path(__file__).resolve().parents[4]
OUT_DIR = PROJECT_ROOT / "data" / "campaign-stats"

# Per-step stats require a date window; use a wide one to capture all activity.
STATS_START_DATE = "2020-01-01"


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def to_int(v):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return 0


def rate(num, den):
    return round(100.0 * num / den, 2) if den else None


def main():
    try:
        client = BisonClient()
    except BisonError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1

    fetched_at = now_iso()
    stats_window = {
        "start_date": STATS_START_DATE,
        "end_date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
    }
    print("Listing campaigns...")
    try:
        campaigns = client.list_campaigns()
    except BisonError as e:
        print(f"ERROR listing campaigns: {e}", file=sys.stderr)
        return 1
    print(f"  {len(campaigns)} campaigns")

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    camp_records = []
    step_records = []
    errors = []

    for c in campaigns:
        cid = c.get("id")
        contacted = to_int(c.get("total_leads_contacted"))
        interested = to_int(c.get("interested"))
        unique_replies = to_int(c.get("unique_replies"))
        camp_records.append({
            "campaign_id": cid,
            "campaign_name": c.get("name"),
            "status": c.get("status"),
            "type": c.get("type"),
            "total_leads": to_int(c.get("total_leads")),
            "total_leads_contacted": contacted,
            "emails_sent": to_int(c.get("emails_sent")),
            "unique_replies": unique_replies,
            "interested": interested,
            "bounced": to_int(c.get("bounced")),
            "unsubscribed": to_int(c.get("unsubscribed")),
            "interested_rate_pct": rate(interested, unique_replies),
            "reply_rate_pct": rate(unique_replies, contacted),
            "fetched_at": fetched_at,
        })

        # Per-step stats (one extra POST per campaign).
        try:
            stats = client.get_campaign_stats(cid, stats_window)
        except BisonError as e:
            errors.append({"campaign_id": cid, "error": str(e)})
            print(f"  ! stats failed for campaign {cid}: {e}", file=sys.stderr)
            continue
        for step_idx, step in enumerate(stats.get("sequence_step_stats") or [], start=1):
            lc = to_int(step.get("leads_contacted"))
            si = to_int(step.get("interested"))
            sr = to_int(step.get("unique_replies"))
            step_records.append({
                "campaign_id": cid,
                "campaign_name": c.get("name"),
                "step_number": step_idx,
                "sequence_step_id": step.get("sequence_step_id"),
                "email_subject": step.get("email_subject"),
                "sent": to_int(step.get("sent")),
                "leads_contacted": lc,
                "unique_replies": sr,
                "interested": si,
                "interested_rate_pct": rate(si, sr),
                "reply_rate_pct": rate(sr, lc),
                "fetched_at": fetched_at,
            })

    with (OUT_DIR / "campaigns.jsonl").open("w") as f:
        for r in camp_records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    with (OUT_DIR / "step_stats.jsonl").open("w") as f:
        for r in step_records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    (OUT_DIR / "last_run.json").write_text(json.dumps({
        "fetched_at": fetched_at,
        "campaigns": len(camp_records),
        "step_rows": len(step_records),
        "total_contacted": sum(r["total_leads_contacted"] for r in camp_records),
        "total_interested": sum(r["interested"] for r in camp_records),
        "errors": errors,
    }, indent=2))

    tot_c = sum(r["total_leads_contacted"] for r in camp_records)
    tot_i = sum(r["interested"] for r in camp_records)
    print(f"\nWrote {len(camp_records)} campaigns, {len(step_records)} step rows to {OUT_DIR}")
    print(f"Overall: {tot_i} interested / {tot_c} contacted = {rate(tot_i, tot_c)}%")
    if errors:
        print(f"{len(errors)} campaign(s) errored on step stats — see last_run.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
