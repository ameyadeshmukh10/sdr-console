"""Use case 2b: turn the descriptive trends CAUSAL by joining denominators.

Reads the campaign + per-step stats pulled by the email-bison skill
(data/campaign-stats/) and computes true interested-reply RATES:
  - by campaign
  - by offer type and geo (campaigns aggregated)
  - by sequence step number (does step 4 actually convert best, or just appear most?)

Numerator/denominator note: rates use Bison's own `interested` count, which reflects
the built-in *interested status* (82 across the workspace), not the broader
status-OR-tag set (171) used by the descriptive analysis. Same source on both sides
of the ratio = a valid rate.

Outputs (data/interested-replies/analysis/):
  conversion.json
  by_campaign_rate.csv  by_offer_rate.csv  by_step_rate.csv

Run:  python3 .claude/skills/interested-trends/scripts/analyze_conversion.py
"""

import csv
import json
from collections import defaultdict
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[4]
STATS_DIR = PROJECT_ROOT / "data" / "campaign-stats"
OUT_DIR = PROJECT_ROOT / "data" / "interested-replies" / "analysis"


# Kept in sync with analyze_interested.py (duplicated to avoid cross-skill imports).
def parse_geo(campaign):
    c = (campaign or "").lower()
    if "global" in c:
        return "Global"
    if "americas" in c or "usa" in c or "us " in c:
        return "Americas"
    return "Unknown"


def parse_offer_type(campaign):
    c = (campaign or "").lower()
    if "pdf" in c:
        return "Lead magnet (PDF)"
    if "demo" in c:
        return "Demo request"
    if "event" in c:
        return "Event"
    import re
    if re.search(r"\bcro\b|\bcmo\b|\bcfo\b", c):
        return "Persona-targeted"
    return "Other"


def rate(num, den):
    return round(100.0 * num / den, 3) if den else None


def write_csv(path, header, rows):
    with path.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)


def build_rate_series(hist_path):
    """True interested-rate over time, differenced from append-only snapshots.

    Bison only ever reports lifetime-to-date counts, so a cumulative rate barely
    moves once volume builds up and hides recent performance. Differencing
    consecutive snapshots gives the rate for the leads contacted *in that window*
    — the number that answers "is the AI SDR getting better?".

    With fewer than two snapshots there is nothing to difference; the payload says
    so explicitly rather than drawing a one-point line.
    """
    if not hist_path.exists():
        return {"available": False, "snapshots": 0,
                "note": "No snapshot history yet. Every fetch_campaign_stats.py run "
                        "appends one; two are needed before a rate trend exists."}
    by_snap = defaultdict(lambda: {"interested": 0, "contacted": 0, "unique_replies": 0})
    for line in hist_path.open():
        if not line.strip():
            continue
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        s = by_snap[r.get("fetched_at")]
        s["interested"] += r.get("interested") or 0
        s["contacted"] += r.get("total_leads_contacted") or 0
        s["unique_replies"] += r.get("unique_replies") or 0

    snaps = sorted(by_snap.items())
    points, prev = [], None
    for at, cur in snaps:
        pt = {"fetched_at": at, "cum_interested": cur["interested"],
              "cum_contacted": cur["contacted"],
              "cum_interested_rate_pct": rate(cur["interested"], cur["contacted"])}
        if prev:
            d_i = cur["interested"] - prev["interested"]
            d_c = cur["contacted"] - prev["contacted"]
            d_r = cur["unique_replies"] - prev["unique_replies"]
            # Counts can go backwards if a campaign is deleted or leads are purged;
            # a negative window is not a rate, so leave it null rather than invent one.
            pt.update({
                "new_contacted": d_c, "new_interested": d_i, "new_replies": d_r,
                "window_interested_rate_pct": rate(d_i, d_c) if d_c > 0 and d_i >= 0 else None,
                "window_reply_rate_pct": rate(d_r, d_c) if d_c > 0 and d_r >= 0 else None,
            })
        points.append(pt)
        prev = cur

    return {
        "available": len(points) >= 2,
        "snapshots": len(points),
        "points": points,
        "note": "window_* rates are differenced between consecutive snapshots = the "
                "rate for leads first contacted in that window. cum_* is "
                "lifetime-to-date and will look flat by comparison."
        if len(points) >= 2 else
        "Only one snapshot so far — showing it as a baseline. Run "
        "fetch_campaign_stats.py again later to get a trend.",
    }


def main():
    camp_path = STATS_DIR / "campaigns.jsonl"
    step_path = STATS_DIR / "step_stats.jsonl"
    if not camp_path.exists():
        print(f"ERROR: {camp_path} not found. Run "
              "email-bison/scripts/fetch_campaign_stats.py first.")
        return 1

    campaigns = [json.loads(l) for l in camp_path.open() if l.strip()]
    steps = [json.loads(l) for l in step_path.open() if l.strip()] if step_path.exists() else []

    # Only campaigns that actually contacted someone are meaningful.
    active = [c for c in campaigns if c["total_leads_contacted"] > 0]

    # By campaign (ranked by interested rate) ---------------------------------
    by_campaign = sorted(active, key=lambda c: (c["interested_rate_pct"] or 0), reverse=True)

    # By offer type / geo -----------------------------------------------------
    def grouped(keyfn):
        agg = defaultdict(lambda: {"interested": 0, "contacted": 0,
                                   "unique_replies": 0, "campaigns": 0})
        for c in active:
            g = agg[keyfn(c["campaign_name"])]
            g["interested"] += c["interested"]
            g["contacted"] += c["total_leads_contacted"]
            g["unique_replies"] += c["unique_replies"]
            g["campaigns"] += 1
        out = {}
        for k, g in agg.items():
            out[k] = {**g,
                      "interested_rate_pct": rate(g["interested"], g["contacted"]),
                      "reply_rate_pct": rate(g["unique_replies"], g["contacted"])}
        return dict(sorted(out.items(),
                           key=lambda kv: (kv[1]["interested_rate_pct"] or 0), reverse=True))

    by_offer = grouped(parse_offer_type)
    by_geo = grouped(parse_geo)

    # By sequence step number (causal version of "winning step") --------------
    step_agg = defaultdict(lambda: {"interested": 0, "contacted": 0, "unique_replies": 0})
    for s in steps:
        a = step_agg[s["step_number"]]
        a["interested"] += s["interested"]
        a["contacted"] += s["leads_contacted"]
        a["unique_replies"] += s["unique_replies"]
    by_step = {}
    for k in sorted(step_agg):
        a = step_agg[k]
        by_step[k] = {**a,
                      "interested_rate_pct": rate(a["interested"], a["contacted"]),
                      "reply_rate_pct": rate(a["unique_replies"], a["contacted"])}

    tot_i = sum(c["interested"] for c in active)
    tot_c = sum(c["total_leads_contacted"] for c in active)

    conversion = {
        "source": str(STATS_DIR),
        "note": "Rates use Bison's built-in `interested` status count as numerator "
                "(82 workspace-wide), not the status-OR-tag 171 from the descriptive "
                "analysis. Same source both sides of the ratio.",
        "overall": {"interested": tot_i, "contacted": tot_c,
                    "interested_rate_pct": rate(tot_i, tot_c)},
        "by_campaign": [
            {"campaign_name": c["campaign_name"],
             "offer_type": parse_offer_type(c["campaign_name"]),
             "geo": parse_geo(c["campaign_name"]),
             "contacted": c["total_leads_contacted"],
             "interested": c["interested"],
             "interested_rate_pct": c["interested_rate_pct"],
             "reply_rate_pct": c["reply_rate_pct"]}
            for c in by_campaign],
        "by_offer_type": by_offer,
        "by_geo": by_geo,
        "by_step": by_step,
        "rate_series": build_rate_series(STATS_DIR / "campaigns_history.jsonl"),
    }
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / "conversion.json").write_text(json.dumps(conversion, indent=2, ensure_ascii=False))

    write_csv(OUT_DIR / "by_campaign_rate.csv",
              ["campaign", "offer_type", "geo", "contacted", "interested",
               "interested_rate_pct", "reply_rate_pct"],
              [[c["campaign_name"], parse_offer_type(c["campaign_name"]),
                parse_geo(c["campaign_name"]), c["total_leads_contacted"], c["interested"],
                c["interested_rate_pct"], c["reply_rate_pct"]] for c in by_campaign])
    write_csv(OUT_DIR / "by_offer_rate.csv",
              ["offer_type", "campaigns", "contacted", "interested", "interested_rate_pct", "reply_rate_pct"],
              [[k, v["campaigns"], v["contacted"], v["interested"],
                v["interested_rate_pct"], v["reply_rate_pct"]] for k, v in by_offer.items()])
    write_csv(OUT_DIR / "by_step_rate.csv",
              ["step_number", "contacted", "interested", "interested_rate_pct", "reply_rate_pct"],
              [[k, v["contacted"], v["interested"], v["interested_rate_pct"], v["reply_rate_pct"]]
               for k, v in by_step.items()])

    # Console summary ---------------------------------------------------------
    print(f"Overall interested rate: {tot_i}/{tot_c} = {rate(tot_i, tot_c)}%\n")
    print("By offer type (interested rate):")
    for k, v in by_offer.items():
        print(f"  {k:20s} {v['interested_rate_pct']}%  ({v['interested']}/{v['contacted']})")
    print("\nBy sequence step (interested rate):")
    for k, v in by_step.items():
        print(f"  step{k}  {v['interested_rate_pct']}%  ({v['interested']}/{v['contacted']})")
    print("\nTop campaigns:")
    for c in by_campaign[:5]:
        print(f"  {c['campaign_name'][:28]:28s} {c['interested_rate_pct']}%  "
              f"({c['interested']}/{c['total_leads_contacted']})")
    print(f"\nWrote conversion.json + 3 CSVs to {OUT_DIR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
