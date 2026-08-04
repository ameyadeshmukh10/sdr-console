"""Synthetic trends data with PLANTED, KNOWN effects — for building and validating
the Trends charts before real volume exists.

Why this exists: the real corpus has 82 status-interested replies spread over 9
campaigns and 4 sequence steps. Most cells are 0-2 replies, so a chart that renders
"flat" is ambiguous — no effect, or a broken join? This generator plants effects we
can check the charts actually surface, plus the degenerate cases that break naive
visualisations:

  * a high-rate / low-volume offer      (should read as "scale this")
  * a low-rate / high-volume offer      (should read as "kill this")
  * a tiny-n offer with a flattering %  (MUST be visibly low-confidence, not a winner)
  * a step-3 trough and step-4 spike    (the sequence-shape finding)
  * a mid-period offer-mix shift        (the trend the time axis has to show)
  * a flat cumulative rate hiding a real swing in the differenced window rate

Everything is deterministic (fixed seed) so screenshots and tests are stable, and
the planted truth is written to ground_truth.json so a chart can be checked against
what it is supposed to reveal.

Aggregation deliberately reuses the real helpers from analyze_interested.py — the
demo exercises the same code path the live analysis does, only the input is fake.

Writes the replies/trends slice of a demo PROFILE
(data/demo/<profile>/interested-replies/):
  last_run.json
  analysis/summary.json  analysis/conversion.json  analysis/cohorts.json
  analysis/ground_truth.json

A profile is the whole synthetic data tree the console can be pointed at; see
sdr-pipeline/scripts/make_demo_profile.py, which builds the rest of it (contacts,
signals, generated copy, campaign stats) and calls this script.

Run:  python3 .claude/skills/interested-trends/scripts/make_demo_data.py [--profile generic]
"""

import argparse
import json
import random
from datetime import date, timedelta
from pathlib import Path

import analyze_interested as AI

PROJECT_ROOT = Path(__file__).resolve().parents[4]


def profile_dirs(profile):
    demo = PROJECT_ROOT / "data" / "demo" / profile / "interested-replies"
    return demo, demo / "analysis"

SEED = 20260803
MONTHS = ["2026-01", "2026-02", "2026-03", "2026-04", "2026-05", "2026-06"]

# --- planted offer economics -------------------------------------------------
# rate_pct is the ground truth; `interested` is derived so the two always agree.
# `mix` is the share of this offer's replies landing in each month of MONTHS —
# that is what produces the mid-period shift.
OFFERS = [
    {"offer_type": "Demo request", "geo": "Americas", "contacted": 620,
     "rate_pct": 2.4, "reply_rate_pct": 7.4, "campaigns": 2,
     "mix": [0.30, 0.28, 0.22, 0.12, 0.05, 0.03],
     "label": "high-rate / low-volume — starved winner"},
    {"offer_type": "Lead magnet (PDF)", "geo": "Global", "contacted": 9100,
     "rate_pct": 0.62, "reply_rate_pct": 7.3, "campaigns": 3,
     "mix": [0.02, 0.05, 0.13, 0.30, 0.28, 0.22],
     "label": "workhorse — mid rate, most volume"},
    {"offer_type": "Event", "geo": "Global", "contacted": 2400,
     "rate_pct": 0.50, "reply_rate_pct": 5.4, "campaigns": 2,
     "mix": [0.05, 0.10, 0.20, 0.25, 0.22, 0.18],
     "label": "middling — no strong signal"},
    {"offer_type": "Persona-targeted", "geo": "Americas", "contacted": 5200,
     "rate_pct": 0.10, "reply_rate_pct": 2.7, "campaigns": 2,
     "mix": [0.0, 0.0, 0.05, 0.20, 0.35, 0.40],
     "label": "low-rate / high-volume — kill or rewrite"},
    {"offer_type": "Webinar (pilot)", "geo": "Global", "contacted": 180,
     "rate_pct": 3.3, "reply_rate_pct": 9.0, "campaigns": 1,
     "mix": [0.0, 0.0, 0.0, 0.20, 0.40, 0.40],
     "label": "small-n, flattering rate — low confidence, not yet a winner"},
    {"offer_type": "Newsletter reply", "geo": "Unknown", "contacted": 40,
     "rate_pct": 2.5, "reply_rate_pct": 10.0, "campaigns": 1,
     "mix": [0.0, 0.0, 0.0, 0.0, 1.0, 0.0],
     "label": "n=1 — must render as insufficient data, never as a top performer"},
]

# --- planted sequence shape --------------------------------------------------
# Sends decay as leads reply/unsubscribe; step 3 is the trough, step 4 the spike.
STEPS = [
    {"step": 1, "contacted": 17500, "rate_pct": 0.150, "reply_rate_pct": 2.5},
    {"step": 2, "contacted": 11800, "rate_pct": 0.190, "reply_rate_pct": 2.0},
    {"step": 3, "contacted": 10200, "rate_pct": 0.059, "reply_rate_pct": 1.3},
    {"step": 4, "contacted": 8400, "rate_pct": 0.450, "reply_rate_pct": 2.4},
]

# --- planted snapshot history ------------------------------------------------
# The cumulative rate barely moves while the windowed rate dips then recovers —
# the whole argument for differencing snapshots.
SNAPSHOT_WINDOWS = [
    {"at": "2026-01-31T00:00:00Z", "new_contacted": 1800, "window_rate_pct": 0.94},
    {"at": "2026-02-28T00:00:00Z", "new_contacted": 2400, "window_rate_pct": 0.83},
    {"at": "2026-03-31T00:00:00Z", "new_contacted": 3600, "window_rate_pct": 0.36},
    {"at": "2026-04-30T00:00:00Z", "new_contacted": 4200, "window_rate_pct": 0.31},
    {"at": "2026-05-31T00:00:00Z", "new_contacted": 3800, "window_rate_pct": 0.79},
    {"at": "2026-06-30T00:00:00Z", "new_contacted": 3740, "window_rate_pct": 0.96},
]

# --- planted persona / construction leanings ---------------------------------
# Weighted draws, not hard rules, so the charts see noise like real data.
SENIORITY = ["C-Level", "Head/Director", "Manager", "IC", "Founder/Owner"]
FUNCTIONS = ["Sales", "Marketing", "RevOps", "Executive/General", "Other"]
CTAS = ["Open question", "Soft permission question", "Demo offer",
        "Specific-time offer", "Resource offer", "None/unclear"]
INTENTS = ["Meeting/demo accept", "Question", "Info request", "Referral/forward",
           "Pricing request", "Positive-later", "Other"]

# offer_type -> per-dimension weights (index-aligned with the lists above)
LEANING = {
    "Demo request":     {"cta": [2, 2, 6, 3, 1, 1], "fn": [6, 2, 2, 3, 1],
                         "sen": [4, 3, 2, 1, 3], "intent": [7, 3, 2, 1, 1, 1, 3]},
    "Lead magnet (PDF)": {"cta": [6, 4, 1, 1, 3, 3], "fn": [3, 6, 2, 3, 2],
                          "sen": [3, 4, 4, 4, 2], "intent": [3, 5, 4, 2, 1, 1, 6]},
    "Event":            {"cta": [4, 3, 2, 2, 2, 3], "fn": [3, 4, 2, 3, 2],
                         "sen": [3, 3, 3, 3, 2], "intent": [4, 4, 3, 2, 1, 1, 5]},
    "Persona-targeted": {"cta": [3, 5, 1, 1, 1, 4], "fn": [2, 2, 6, 2, 1],
                         "sen": [2, 4, 4, 2, 1], "intent": [2, 5, 3, 1, 1, 1, 5]},
    "Webinar (pilot)":  {"cta": [3, 4, 2, 2, 2, 1], "fn": [4, 3, 3, 2, 1],
                         "sen": [3, 4, 2, 1, 2], "intent": [6, 3, 2, 1, 1, 1, 2]},
    "Newsletter reply": {"cta": [2, 2, 1, 1, 2, 2], "fn": [2, 2, 2, 2, 2],
                         "sen": [2, 2, 2, 2, 2], "intent": [2, 2, 2, 1, 1, 1, 2]},
}
# Winning step follows the planted sequence shape: mostly 4, trough at 3.
STEP_WEIGHTS = [15, 19, 6, 45]


def rate(num, den):
    return round(100.0 * num / den, 3) if den else None


def allocate(total, weights):
    """Split `total` across `weights` preserving the sum exactly (largest remainder)."""
    s = sum(weights)
    if s <= 0 or total <= 0:
        return [0] * len(weights)
    raw = [total * w / s for w in weights]
    out = [int(x) for x in raw]
    for i in sorted(range(len(raw)), key=lambda i: raw[i] - out[i], reverse=True):
        if sum(out) >= total:
            break
        out[i] += 1
    return out


def month_days(month):
    y, m = int(month[:4]), int(month[5:7])
    nxt = date(y + 1, 1, 1) if m == 12 else date(y, m + 1, 1)
    return (nxt - date(y, m, 1)).days


def make_reply(rng, rid, offer, day):
    lean = LEANING[offer["offer_type"]]
    step = rng.choices([1, 2, 3, 4], weights=STEP_WEIGHTS)[0]
    cta = rng.choices(CTAS, weights=lean["cta"])[0]
    return {
        "reply_id": rid,
        "lead_name": f"Demo Lead {rid}",
        "company": f"Demo Co {rid % 97}",
        "email_domain": f"demo{rid % 97}.example",
        "title": "Demo title",
        "campaign_name": f"{offer['geo']} {offer['offer_type']}",
        "geo": offer["geo"],
        "offer_type": offer["offer_type"],
        "seniority": rng.choices(SENIORITY, weights=lean["sen"])[0],
        "function": rng.choices(FUNCTIONS, weights=lean["fn"])[0],
        "winning_step": step,
        "winning_cta": cta,
        "reply_intent": rng.choices(INTENTS, weights=lean["intent"])[0],
        "is_auto_reply": rng.random() < 0.02,
        "reply_word_count": rng.randint(8, 90),
        "reply_clean": "[synthetic demo reply — not real correspondence]",
        "reply_dow": AI.DOW[day.weekday()],
        "reply_hour": rng.choices(range(6, 20), weights=[1, 2, 4, 6, 7, 6, 5, 4, 5, 6, 5, 3, 2, 1])[0],
        "reply_date": day.isoformat(),
        "reply_month": day.strftime("%Y-%m"),
        "reply_week": (day - timedelta(days=day.weekday())).isoformat(),
        "interested_via": ["status", "tag"],
        "has_linkedin": rng.random() < 0.4,
        "has_location": rng.random() < 0.3,
        "untracked": False,
        "steps": {f"step{i}": {
            "subject": f"Demo subject {i}",
            "subject_word_count": rng.randint(4, 9),
            "subject_is_question": rng.random() < 0.3,
            "subject_personalized": rng.random() < 0.35,
            "body_word_count": rng.randint(45, 140),
            "num_questions": rng.randint(0, 2),
            "has_personalized_opening": rng.random() < 0.5,
            "has_social_proof": rng.random() < 0.6,
            "has_ps": rng.random() < 0.25,
            "cta_type": cta if i == step else rng.choice(CTAS),
        } for i in (1, 2, 3, 4)},
    }


def build_replies(rng):
    feats, rid = [], 900000
    for offer in OFFERS:
        n = max(1, round(offer["contacted"] * offer["rate_pct"] / 100.0))
        offer["_interested"] = n
        for month, count in zip(MONTHS, allocate(n, offer["mix"])):
            for _ in range(count):
                rid += 1
                day = date(int(month[:4]), int(month[5:7]),
                           rng.randint(1, month_days(month)))
                feats.append(make_reply(rng, rid, offer, day))
    feats.sort(key=lambda f: f["reply_date"])
    return feats


def build_summary(feats):
    genuine = [f for f in feats if not f["is_auto_reply"]]
    win_wc = [f["steps"][f"step{f['winning_step']}"]["body_word_count"] for f in genuine]
    return {
        "generated_from": "demo profile (make_demo_data.py)",
        "demo": True,
        "total_replies": len(feats),
        "auto_reply_count": sum(1 for f in feats if f["is_auto_reply"]),
        "genuine_count": len(genuine),
        "untracked_count": 0,
        "by_campaign": AI.dist(f["campaign_name"] for f in feats),
        "by_geo": AI.dist(f["geo"] for f in feats),
        "by_offer_type": AI.dist(f["offer_type"] for f in feats),
        "by_seniority": AI.dist(f["seniority"] for f in feats),
        "by_function": AI.dist(f["function"] for f in feats),
        "by_winning_step": AI.dist(str(f["winning_step"]) for f in genuine),
        "by_winning_cta": AI.dist(f["winning_cta"] for f in genuine),
        "by_reply_intent": AI.dist(f["reply_intent"] for f in genuine),
        "by_reply_dow": AI.dist(f["reply_dow"] for f in genuine),
        "by_interested_via": AI.dist("status,tag" for _ in feats),
        "winning_email_word_count": {
            "min": min(win_wc), "max": max(win_wc),
            "avg": round(sum(win_wc) / len(win_wc), 1), "n": len(win_wc),
        },
        "timeseries": AI.build_timeseries(feats),
        "crosstabs": {
            "offer_type_x_seniority": AI._crosstab(genuine, "offer_type", "seniority"),
            "cta_x_reply_intent": AI._crosstab(genuine, "winning_cta", "reply_intent"),
            "seniority_x_function": AI._crosstab(feats, "seniority", "function"),
        },
        "caveats": [
            "Demo profile dataset — see ground_truth.json for the planted effects "
            "each chart should reveal.",
            "DESCRIPTIVE ONLY: these distributions are the population of interested "
            "repliers, not all leads contacted. Read them as composition, not lift.",
        ],
    }


def build_conversion():
    by_offer, by_campaign = {}, []
    for o in OFFERS:
        n = o["_interested"]
        replies = max(n, round(o["contacted"] * o["reply_rate_pct"] / 100.0))
        by_offer[o["offer_type"]] = {
            "interested": n, "contacted": o["contacted"], "unique_replies": replies,
            "campaigns": o["campaigns"],
            "interested_rate_pct": rate(n, o["contacted"]),
            "reply_rate_pct": rate(replies, o["contacted"]),
            "demo_label": o["label"],
        }
        # Split each offer across its campaigns so the campaign table has rows too.
        for i, (c, ni) in enumerate(zip(allocate(o["contacted"], [1] * o["campaigns"]),
                                        allocate(n, [1] * o["campaigns"])), start=1):
            suffix = "" if o["campaigns"] == 1 else f" {i}"
            by_campaign.append({
                "campaign_name": f"{o['geo']} {o['offer_type']}{suffix}",
                "offer_type": o["offer_type"], "geo": o["geo"],
                "contacted": c, "interested": ni,
                "interested_rate_pct": rate(ni, c),
                "reply_rate_pct": rate(round(c * o["reply_rate_pct"] / 100.0), c),
            })
    by_campaign.sort(key=lambda c: c["interested_rate_pct"] or 0, reverse=True)

    by_geo = {}
    for o in OFFERS:
        g = by_geo.setdefault(o["geo"], {"interested": 0, "contacted": 0,
                                         "unique_replies": 0, "campaigns": 0})
        g["interested"] += o["_interested"]
        g["contacted"] += o["contacted"]
        g["unique_replies"] += round(o["contacted"] * o["reply_rate_pct"] / 100.0)
        g["campaigns"] += o["campaigns"]
    for g in by_geo.values():
        g["interested_rate_pct"] = rate(g["interested"], g["contacted"])
        g["reply_rate_pct"] = rate(g["unique_replies"], g["contacted"])

    by_step = {}
    for s in STEPS:
        n = round(s["contacted"] * s["rate_pct"] / 100.0)
        by_step[str(s["step"])] = {
            "interested": n, "contacted": s["contacted"],
            "unique_replies": round(s["contacted"] * s["reply_rate_pct"] / 100.0),
            "interested_rate_pct": rate(n, s["contacted"]),
            "reply_rate_pct": s["reply_rate_pct"],
        }

    tot_i = sum(o["_interested"] for o in OFFERS)
    tot_c = sum(o["contacted"] for o in OFFERS)

    # Snapshot history: cumulative totals reconstructed from the planted windows.
    points, cum_c, cum_i, cum_r = [], 0, 0, 0
    for i, w in enumerate(SNAPSHOT_WINDOWS):
        d_i = round(w["new_contacted"] * w["window_rate_pct"] / 100.0)
        d_r = round(w["new_contacted"] * 0.06)
        cum_c += w["new_contacted"]; cum_i += d_i; cum_r += d_r
        pt = {"fetched_at": w["at"], "cum_interested": cum_i, "cum_contacted": cum_c,
              "cum_interested_rate_pct": rate(cum_i, cum_c)}
        if i:
            pt.update({"new_contacted": w["new_contacted"], "new_interested": d_i,
                       "new_replies": d_r,
                       "window_interested_rate_pct": rate(d_i, w["new_contacted"]),
                       "window_reply_rate_pct": rate(d_r, w["new_contacted"])})
        points.append(pt)

    return {
        "source": "demo profile (make_demo_data.py)",
        "demo": True,
        "note": "Rates use the same numerator on both sides of the ratio.",
        "overall": {"interested": tot_i, "contacted": tot_c,
                    "interested_rate_pct": rate(tot_i, tot_c)},
        "by_campaign": by_campaign,
        "by_offer_type": dict(sorted(by_offer.items(),
                                     key=lambda kv: kv[1]["interested_rate_pct"] or 0,
                                     reverse=True)),
        "by_geo": by_geo,
        "by_step": by_step,
        "rate_series": {
            "available": True, "snapshots": len(points), "points": points,
            "note": "window_* rates are differenced between consecutive snapshots. "
                    "Note how flat cum_* looks next to them — that is the point.",
        },
    }


def build_cohorts(feats):
    """Cohorts keyed by function, mirroring analyze_cohorts.py's output shape."""
    out, sizes = {}, {}
    genuine = [f for f in feats if not f["is_auto_reply"]]
    for fn in FUNCTIONS:
        rows = [f for f in genuine if f["function"] == fn]
        if not rows:
            continue
        sizes[fn] = len(rows)

        def share(key):
            c = AI.dist(f[key] for f in rows)
            return {k: {"count": v["count"],
                        "pct_of_cohort": round(100.0 * v["count"] / len(rows), 1)}
                    for k, v in c.items()}

        out[fn] = {
            "n": len(rows),
            "winning_cta": share("winning_cta"),
            "offer_type": share("offer_type"),
            "winning_step": {k: {"count": v["count"],
                                 "pct_of_cohort": round(100.0 * v["count"] / len(rows), 1)}
                             for k, v in AI.dist(str(f["winning_step"]) for f in rows).items()},
        }
    return {
        "caveat": "Cohort shares are directional — small cohorts move a lot on a "
                  "single reply.",
        "cohort_sizes": sizes,
        "cohorts": out,
    }


def build_ground_truth():
    return {
        "seed": SEED,
        "what_each_chart_must_reveal": [
            {"chart": "Offer opportunity scatter",
             "expect": "Demo request (2.4% on 620) and Webinar pilot (3.3% on 180) sit "
                       "above the baseline line; Persona-targeted (0.10% on 5,200) sits "
                       "far right and bottom. Newsletter reply (n=1) must be visibly "
                       "marked insufficient-data and must NOT read as the top performer."},
            {"chart": "Sequence funnel + rate overlay",
             "expect": "Sends decay 17,500 -> 8,400 while the rate line dips to 0.059% at "
                       "step 3 and spikes to 0.450% at step 4."},
            {"chart": "Replies over time / mix",
             "expect": "Offer mix rotates from Demo-request-dominant in Jan-Feb to "
                       "Lead-magnet + Persona-targeted by May-Jun."},
            {"chart": "Rate over time (differenced snapshots)",
             "expect": "Window rate falls 0.94 -> 0.31 then recovers to 0.96, while the "
                       "cumulative line stays nearly flat around 0.6."},
        ],
        "planted_offers": [{"offer_type": o["offer_type"], "contacted": o["contacted"],
                            "rate_pct": o["rate_pct"], "interested": o["_interested"],
                            "label": o["label"]} for o in OFFERS],
        "planted_steps": STEPS,
        "planted_windows": SNAPSHOT_WINDOWS,
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--profile", default="generic",
                    help="demo profile id to write into (data/demo/<profile>/)")
    args = ap.parse_args()
    DEMO_DIR, OUT_DIR = profile_dirs(args.profile)

    rng = random.Random(SEED)
    feats = build_replies(rng)
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    summary = build_summary(feats)
    conversion = build_conversion()

    with (OUT_DIR / "features.jsonl").open("w") as f:
        for ft in feats:
            f.write(json.dumps(ft, ensure_ascii=False) + "\n")
    (OUT_DIR / "summary.json").write_text(json.dumps(summary, indent=2))
    (OUT_DIR / "conversion.json").write_text(json.dumps(conversion, indent=2))
    (OUT_DIR / "cohorts.json").write_text(json.dumps(build_cohorts(feats), indent=2))
    (OUT_DIR / "ground_truth.json").write_text(json.dumps(build_ground_truth(), indent=2))
    (DEMO_DIR / "last_run.json").write_text(json.dumps({
        "demo": True,
        "fetched_at": f"{MONTHS[-1]}-30T00:00:00Z",
        "total_interested": len(feats),
        "errors": [],
    }, indent=2))

    print(f"Demo data written to {DEMO_DIR}")
    print(f"  {len(feats)} synthetic replies over {len(MONTHS)} months, "
          f"{len(OFFERS)} offers, {conversion['overall']['contacted']} contacted")
    print(f"  overall planted rate: {conversion['overall']['interested_rate_pct']}%")
    for k, v in conversion["by_offer_type"].items():
        print(f"    {k:20s} {v['interested_rate_pct']:>6}%  "
              f"({v['interested']}/{v['contacted']})  {v['demo_label']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
