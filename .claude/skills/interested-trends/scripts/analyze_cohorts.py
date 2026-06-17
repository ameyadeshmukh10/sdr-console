"""Use case 2c: cohort winning sequences by job function + prep qualitative analysis.

Segments every interested reply into one of four job-function cohorts —
Marketing / Sales / CEO-Founder / Other — then tags the *kind* of personalization,
offer, and CTA used in the sequence that won the reply. The deterministic tagging +
per-cohort evidence bundle feeds the agent's qualitative synthesis (cohort-playbook.md),
which turns "what's working in each cohort" into AI SDR instructions.

Reuses the classifiers in analyze_interested.py (same skill, same dir).

IMPORTANT — descriptive, within repliers: we only know job function for the people who
replied, not for everyone contacted. So cohort numbers are SHARES AMONG REPLIERS, not
conversion rates. The qualitative patterns are the payload; the counts are context.

Outputs (data/interested-replies/analysis/):
  cohorts.json            per-cohort quantitative distributions
  cohort_evidence.jsonl   per-reply qualitative inputs (winning email + reply + tags)

Run:  python3 .claude/skills/interested-trends/scripts/analyze_cohorts.py
"""

import json
import re
from collections import Counter, defaultdict
from pathlib import Path

from analyze_interested import (
    DATASET, OUT_DIR, cv, words, clean_message, classify_cta,
    classify_seniority, classify_function, parse_offer_type, find_winning_step,
)

COHORTS = ["Marketing", "Sales", "CEO/Founder", "Other"]


def assign_cohort(title):
    t = (title or "").lower()
    seniority = classify_seniority(title)
    function = classify_function(title)
    # CEO/Founder wins first — top-exec generalists & owners (but NOT CMO/CRO/CFO).
    if seniority == "Founder/Owner" or re.search(
            r"\bceo\b|chief executive|\bfounder\b|\bowner\b|\bpresident\b|\bproprietor\b", t):
        return "CEO/Founder"
    if function == "Marketing":
        return "Marketing"
    if function == "Sales/Revenue":
        return "Sales"
    return "Other"


# ---- personalization taxonomy (detected in the opener + sequence) -----------
PERSONALIZATION = [
    ("company_research", r"i (looked at|reviewed|saw|checked|came across|read|explored|reviewed)|"
                         r"your (website|site|company|product|offering)|i noticed|i liked that|positions?\b"),
    ("role_persona", r"\bas (a |the |an )?(founder|co-?founder|ceo|owner|head|director|vp|"
                     r"manager|chief|president|consultant|specialist|cmo|cro|cfo)"),
    ("linkedin_tenure", r"your linkedin|\b\d+\+?\s*years?\b|over a decade|experience (building|in)"),
    ("recent_news", r"\brecent(ly)?\b|just (launched|announced|raised|acquired|closed)|"
                    r"acquisition|funding|authorisation|authorization|expansion|new (product|launch|round|role)|congrats"),
    ("pain_hypothesis", r"you('re| are) likely|you probably|likely (feel|felt|need|balancing|facing|juggling)|"
                        r"the squeeze|top pain|pain point|challenge|struggl|bottleneck|friction|without (inflating|adding)"),
    ("social_proof", r"agencies i talk|teams? (i|we) (work|talk)|companies like|clients (i|we)|"
                     r"customers|we help \w+ (leaders|teams)|others in"),
    ("metric_specificity", r"\d+%|\b\d+x\b|\$\d|\d+ hours|\d+ (reps|leads|meetings|locations)"),
]

OFFERS = [
    ("demo", r"\bdemo\b|walkthrough|show you|quick look|7-?minute|short demo"),
    ("audit", r"\baudit\b|assessment|review of your"),
    ("roi_sketch", r"roi (sketch|snapshot|example|estimate)|time-?to-?value|\bftes?\b|impact estimate"),
    ("resource", r"one-?pager|\bpdf\b|\bguide\b|checklist|case study|\bdeck\b|overview|template|\bresource"),
    ("pilot", r"\bpilot\b|\btrial\b|proof of concept|\bpoc\b"),
    ("call", r"\b\d{1,2}-?(min|minute)\b|quick (call|chat)|hop on a call|20 minutes|15 minutes"),
]


def detect_tags(text, table):
    t = text or ""
    return [label for label, pat in table if re.search(pat, t, re.IGNORECASE)]


def main():
    if not DATASET.exists():
        print(f"ERROR: {DATASET} not found. Run the email-bison fetch script first.")
        return 1
    records = [json.loads(l) for l in DATASET.open() if l.strip()]

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    evidence = []
    agg = {c: {
        "n": 0,
        "winning_step": Counter(),
        "winning_cta": Counter(),
        "offer": Counter(),
        "personalization": Counter(),
        "campaign": Counter(),
        "offer_type": Counter(),
        "seniority": Counter(),
        "win_word_counts": [],
    } for c in COHORTS}

    for r in records:
        lead = r["lead"]
        title = lead.get("title") or ""
        cohort = assign_cohort(title)

        opener = cv(r, "body1")
        win_step = find_winning_step(r)
        win_body = cv(r, f"body{win_step}") if win_step else opener
        win_subject = cv(r, f"subject{win_step}") if win_step else cv(r, "subject1")
        reply_clean = clean_message(r.get("interested_reply_text") or "")

        # Personalization across the whole sequence (richest signal), offer/CTA on the
        # email that actually won.
        seq_text = " ".join(cv(r, f"body{i}") for i in range(1, 5))
        personalization = detect_tags(seq_text, PERSONALIZATION)
        offer = detect_tags(win_body, OFFERS)
        cta = classify_cta(win_body)
        win_wc = len(words(win_body))

        a = agg[cohort]
        a["n"] += 1
        a["winning_step"][str(win_step)] += 1
        a["winning_cta"][cta] += 1
        for p in personalization:
            a["personalization"][p] += 1
        for o in offer:
            a["offer"][o] += 1
        a["campaign"][r.get("campaign_name")] += 1
        a["offer_type"][parse_offer_type(r.get("campaign_name"))] += 1
        a["seniority"][classify_seniority(title)] += 1
        a["win_word_counts"].append(win_wc)

        evidence.append({
            "reply_id": r["reply_id"],
            "cohort": cohort,
            "lead_name": " ".join(p for p in [lead.get("first_name"), lead.get("last_name")] if p),
            "title": title,
            "company": lead.get("company"),
            "campaign_name": r.get("campaign_name"),
            "offer_type": parse_offer_type(r.get("campaign_name")),
            "winning_step": win_step,
            "winning_subject": win_subject,
            "personalization_types": personalization,
            "offer_tags": offer,
            "winning_cta": cta,
            "opener_text": clean_message(opener),
            "winning_email_text": clean_message(win_body),
            "reply_text": reply_clean,
        })

    # Build JSON-friendly cohort summary.
    def topdist(counter, n):
        total = sum(counter.values())
        return {k: {"count": v, "pct_of_cohort": round(100 * v / n, 1) if n else 0}
                for k, v in counter.most_common()}

    cohorts_out = {
        "caveat": "Shares among REPLIERS, not conversion rates — job function is known "
                  "only for the 171 interested repliers, not the contacted population. "
                  "Use counts as context; the qualitative patterns are the payload.",
        "cohort_sizes": {c: agg[c]["n"] for c in COHORTS},
        "cohorts": {},
    }
    for c in COHORTS:
        a = agg[c]
        n = a["n"]
        wc = a["win_word_counts"]
        cohorts_out["cohorts"][c] = {
            "n": n,
            "personalization_types": topdist(a["personalization"], n),
            "offers": topdist(a["offer"], n),
            "winning_cta": topdist(a["winning_cta"], n),
            "winning_step": topdist(a["winning_step"], n),
            "offer_type": topdist(a["offer_type"], n),
            "seniority": topdist(a["seniority"], n),
            "avg_winning_word_count": round(sum(wc) / len(wc), 1) if wc else None,
            "top_campaigns": dict(a["campaign"].most_common(3)),
        }

    (OUT_DIR / "cohorts.json").write_text(json.dumps(cohorts_out, indent=2, ensure_ascii=False))
    with (OUT_DIR / "cohort_evidence.jsonl").open("w") as f:
        for e in evidence:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")

    # Console summary
    print("Cohort sizes (interested repliers):")
    for c in COHORTS:
        print(f"  {c:12s} {agg[c]['n']}")
    print()
    for c in COHORTS:
        a = agg[c]
        if not a["n"]:
            continue
        top_p = ", ".join(f"{k}({v})" for k, v in a["personalization"].most_common(4))
        top_o = ", ".join(f"{k}({v})" for k, v in a["offer"].most_common(3))
        top_c = ", ".join(f"{k}({v})" for k, v in a["winning_cta"].most_common(2))
        print(f"[{c}] n={a['n']}")
        print(f"   personalization: {top_p}")
        print(f"   offers: {top_o}")
        print(f"   winning CTA: {top_c}")
    print(f"\nWrote cohorts.json + cohort_evidence.jsonl to {OUT_DIR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
