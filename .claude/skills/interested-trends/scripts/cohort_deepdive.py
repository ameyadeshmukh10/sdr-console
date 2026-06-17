"""Use case 2d: human-review evidence book for ONE cohort (default: Sales).

For the chosen job-function cohort, this dumps the EXACT verbatim email copy and the
lead's reply for every differentiated signature surfaced by analyze_cohorts.py — so a
human can read the actual words and judge what is / isn't working.

It produces two views:
  Part 1 — Signature galleries: for each personalization type / offer / CTA, the exact
           matched sentence(s) pulled verbatim from the email, with reply_id + the reply.
  Part 2 — Full emails: the complete verbatim opener + winning email + full reply for
           every record in the cohort.

Usage:
  python3 .claude/skills/interested-trends/scripts/cohort_deepdive.py [Cohort]
    Cohort ∈ {Sales, Marketing, "CEO/Founder", Other}  (default: Sales)

Output (data/interested-replies/analysis/):
  <cohort>-cohort-deepdive.md   the evidence book
  <cohort>-cohort-deepdive.json the same data, structured
"""

import json
import re
import sys
from collections import defaultdict
from pathlib import Path

from analyze_interested import (
    DATASET, OUT_DIR, cv, clean_message, classify_cta, find_winning_step, words,
)
from analyze_cohorts import assign_cohort, PERSONALIZATION, OFFERS, detect_tags

SENT_SPLIT = re.compile(r"(?<=[.!?])\s+|\n+")


def slug(cohort):
    return re.sub(r"[^a-z0-9]+", "-", cohort.lower()).strip("-")


def sentences(text):
    return [s.strip() for s in SENT_SPLIT.split(text or "") if s.strip()]


def matching_sentences(text, pattern):
    """Return verbatim sentences from `text` that match `pattern`."""
    out = []
    for s in sentences(text):
        if re.search(pattern, s, re.IGNORECASE):
            out.append(s)
    return out


def first_line(text, n=160):
    t = " ".join((text or "").split())
    return t[:n] + ("…" if len(t) > n else "")


def main():
    cohort = sys.argv[1] if len(sys.argv) > 1 else "Sales"
    valid = {"Sales", "Marketing", "CEO/Founder", "Other"}
    if cohort not in valid:
        print(f"ERROR: cohort must be one of {sorted(valid)} (got {cohort!r})")
        return 1
    if not DATASET.exists():
        print(f"ERROR: {DATASET} not found. Run the email-bison fetch script first.")
        return 1

    records = [json.loads(l) for l in DATASET.open() if l.strip()]
    rows = []
    for r in records:
        title = r["lead"].get("title") or ""
        if assign_cohort(title) != cohort:
            continue
        win_step = find_winning_step(r)
        opener = cv(r, "body1")
        win_body = cv(r, f"body{win_step}") if win_step else opener
        seq_text = " ".join(cv(r, f"body{i}") for i in range(1, 5))
        reply = clean_message(r.get("interested_reply_text") or "")
        rows.append({
            "reply_id": r["reply_id"],
            "lead_name": " ".join(p for p in [r["lead"].get("first_name"), r["lead"].get("last_name")] if p),
            "title": title,
            "company": r["lead"].get("company"),
            "campaign_name": r.get("campaign_name"),
            "winning_step": win_step,
            "subjects": {i: cv(r, f"subject{i}") for i in range(1, 5)},
            "bodies": {i: cv(r, f"body{i}") for i in range(1, 5)},
            "opener": opener,
            "winning_body": win_body,
            "winning_cta": classify_cta(win_body),
            "personalization": detect_tags(seq_text, PERSONALIZATION),
            "offers": detect_tags(win_body, OFFERS),
            "reply": reply,
            "_seq_text": seq_text,
        })

    n = len(rows)
    if not n:
        print(f"No records in cohort {cohort!r}.")
        return 1

    # Build signature galleries (verbatim matched sentences). -----------------
    pers_gallery = defaultdict(list)   # signature -> [{reply_id, lead, sentences, reply}]
    for label, pat in PERSONALIZATION:
        for row in rows:
            sents = matching_sentences(row["_seq_text"], pat)
            if sents:
                pers_gallery[label].append({
                    "reply_id": row["reply_id"], "lead": row["lead_name"],
                    "title": row["title"], "company": row["company"],
                    "sentences": sents, "reply": row["reply"],
                })
    offer_gallery = defaultdict(list)
    for label, pat in OFFERS:
        for row in rows:
            sents = matching_sentences(row["winning_body"], pat)
            if sents:
                offer_gallery[label].append({
                    "reply_id": row["reply_id"], "lead": row["lead_name"],
                    "company": row["company"], "sentences": sents, "reply": row["reply"],
                })
    cta_gallery = defaultdict(list)
    for row in rows:
        cta_gallery[row["winning_cta"]].append({
            "reply_id": row["reply_id"], "lead": row["lead_name"],
            "company": row["company"], "winning_step": row["winning_step"],
            "winning_body": row["winning_body"], "reply": row["reply"],
        })

    # JSON dump ---------------------------------------------------------------
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    structured = {
        "cohort": cohort, "n": n,
        "records": [{k: v for k, v in row.items() if not k.startswith("_")} for row in rows],
        "personalization_gallery": {k: v for k, v in pers_gallery.items()},
        "offer_gallery": {k: v for k, v in offer_gallery.items()},
        "cta_gallery": {k: v for k, v in cta_gallery.items()},
    }
    (OUT_DIR / f"{slug(cohort)}-cohort-deepdive.json").write_text(
        json.dumps(structured, indent=2, ensure_ascii=False))

    # Markdown evidence book --------------------------------------------------
    L = []
    L.append(f"# {cohort} Cohort — Evidence Book (verbatim)")
    L.append("")
    L.append(f"_Generated from {n} interested replies in the **{cohort}** cohort. "
             "Every quote below is the exact copy from the sent sequence or the lead's "
             "reply — for human review of what is / isn't working._")
    L.append("")
    # signature presence counts
    L.append("## Signature presence in this cohort")
    L.append("| Signature | # of the cohort's emails |")
    L.append("|---|---:|")
    for label, _ in PERSONALIZATION:
        L.append(f"| personalization · {label} | {len(pers_gallery.get(label, []))} / {n} |")
    for label, _ in OFFERS:
        if offer_gallery.get(label):
            L.append(f"| offer · {label} | {len(offer_gallery[label])} / {n} |")
    for cta, items in sorted(cta_gallery.items(), key=lambda kv: -len(kv[1])):
        L.append(f"| CTA · {cta} | {len(items)} / {n} |")
    L.append("")

    L.append("---")
    L.append("## Part 1 — Signature galleries (exact words)")
    L.append("")
    L.append("### Personalization")
    for label, _ in PERSONALIZATION:
        items = pers_gallery.get(label, [])
        if not items:
            continue
        L.append(f"#### `{label}` — {len(items)} emails")
        for it in items:
            who = f"{it['lead']} · {it['title']} · {it['company']} (reply {it['reply_id']})"
            L.append(f"- **{who}**")
            for s in it["sentences"]:
                L.append(f"  - > {s}")
            if it["reply"]:
                L.append(f"  - ↳ _reply:_ {first_line(it['reply'])}")
        L.append("")
    L.append("### Offers (in the winning email)")
    for label, _ in OFFERS:
        items = offer_gallery.get(label, [])
        if not items:
            continue
        L.append(f"#### `{label}` — {len(items)} emails")
        for it in items:
            L.append(f"- **{it['lead']} · {it['company']} (reply {it['reply_id']})**")
            for s in it["sentences"]:
                L.append(f"  - > {s}")
        L.append("")
    L.append("### CTA (in the winning email)")
    for cta, items in sorted(cta_gallery.items(), key=lambda kv: -len(kv[1])):
        L.append(f"#### `{cta}` — {len(items)} emails")
        for it in items:
            cta_sent = sentences(it["winning_body"])
            tail = " ".join(cta_sent[-2:]) if cta_sent else ""
            L.append(f"- **{it['lead']} · {it['company']} (reply {it['reply_id']}, step {it['winning_step']})**")
            if tail:
                L.append(f"  - > …{first_line(tail, 220)}")
            if it["reply"]:
                L.append(f"  - ↳ _reply:_ {first_line(it['reply'])}")
        L.append("")

    L.append("---")
    L.append("## Part 2 — Full emails (verbatim, per reply)")
    L.append("")
    for row in rows:
        L.append(f"### Reply {row['reply_id']} — {row['lead_name']} · {row['title']} · {row['company']}")
        L.append(f"_Campaign: {row['campaign_name']} · winning step: {row['winning_step']} · "
                 f"CTA: {row['winning_cta']} · personalization: {', '.join(row['personalization']) or '—'} · "
                 f"offers: {', '.join(row['offers']) or '—'}_")
        L.append("")
        ws = row["winning_step"]
        # Opener (step 1) always shown; winning step too if different.
        L.append(f"**Opener — Subject: {row['subjects'].get(1) or '—'}**")
        L.append("```")
        L.append((row["bodies"].get(1) or "").strip())
        L.append("```")
        if ws and ws != 1:
            L.append(f"**Winning email (step {ws}) — Subject: {row['subjects'].get(ws) or '—'}**")
            L.append("```")
            L.append((row["bodies"].get(ws) or "").strip())
            L.append("```")
        L.append("**Lead's reply:**")
        L.append("```")
        L.append(row["reply"].strip() or "(reply text not isolated — see threads/%s.md)" % row["reply_id"])
        L.append("```")
        L.append("")

    out_md = OUT_DIR / f"{slug(cohort)}-cohort-deepdive.md"
    out_md.write_text("\n".join(L))

    print(f"[{cohort}] {n} records")
    print(f"Wrote {out_md}")
    print(f"Wrote {OUT_DIR / (slug(cohort) + '-cohort-deepdive.json')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
