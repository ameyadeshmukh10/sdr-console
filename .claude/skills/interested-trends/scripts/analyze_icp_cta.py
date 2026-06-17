"""Phase 0 (go-forward strategy): re-segment to the ICP buyer group and analyze CTAs
on a value-first <-> ask-for-time axis.

Old GTM optimized lead-source offer types (PDF/demo/event). Going forward we sell the
AI SDR exclusively, outbound-only, to a tight GTM-leadership buyer group, and we iterate
on the CTA *inside the email* — specifically toward offers that give value up front
instead of just asking for time. This script establishes the baseline:
  - filter the interested replies to is_icp_buyer()
  - classify the CTA in each winning email: value-give / soft-permission / open-question /
    time-ask / resource / none, and a boolean value_first
  - surface verbatim value-first CTAs that earned ICP replies (the iteration baseline)

Outputs (data/interested-replies/analysis/):
  icp_cta.jsonl        per ICP reply: role, winning CTA sentence, class, value_first, reply
  icp-cta-report.md    counts by role + value-first vs time-ask + verbatim examples

Run:  python3 .claude/skills/interested-trends/scripts/analyze_icp_cta.py
"""

import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

from analyze_interested import DATASET, OUT_DIR, cv, clean_message, find_winning_step

# buyer_group lives in the ai-sdr skill (canonical home of the go-forward ICP definition).
AI_SDR_SCRIPTS = (Path(__file__).resolve().parents[2] / "ai-sdr" / "scripts")
sys.path.insert(0, str(AI_SDR_SCRIPTS))
from buyer_group import buyer_role  # noqa: E402

SENT_SPLIT = re.compile(r"(?<=[.!?])\s+|\n+")

# CTA axis (classified on the CTA SENTENCE, not the whole body) ---------------
# A "give" = the ask is to send/leave the prospect something useful up front.
GIVE = re.compile(
    r"\bsent over\b|\bsend (it|that|those|them|you|over)\b|can i send|i can send|"
    r"happy to send|want me to send|want (that|it|those|the \w+)\b|"
    r"leave .*(playbook|brief|resource|one-?pager|teardown)|de-?anonymized|"
    r"accounts (already )?showing (buying )?signals|yours to keep|"
    r"prefer the (brief|list|teardown|playbook) by email|"
    r"one-?page(r)?|roi (estimate|snapshot|sketch)|\bteardown\b|\bbenchmark\b", re.I)
TIME_ASK = re.compile(
    r"\b\d{1,2}[-\s]?(min|minute)s?\b|quick (call|chat)|hop on|jump on|"
    r"book (a )?(call|time|meeting|slot)|grab (some )?time|calendar|calendly|"
    r"\bdemo\b|walk-?through|worth a (quick )?(call|chat)|"
    r"open to (a )?(call|chat|conversation|meeting)|\d{1,2}\s?(am|pm)|"
    r"set up a call|put time on", re.I)
SOFT_PERM = re.compile(r"would you be open|are you open|ok if i|mind if i|"
                       r"is it worth|does .* make sense|worth exploring", re.I)


def sentences(text):
    return [s.strip() for s in SENT_SPLIT.split(text or "") if s.strip()]


def cta_text(body):
    """Best-effort CTA = the question sentence nearest the end, else last 2 sentences."""
    sents = sentences(body)
    if not sents:
        return ""
    qs = [s for s in sents if s.endswith("?")]
    if qs:
        return qs[-1]
    return " ".join(sents[-2:])


def classify_cta(body):
    cta = cta_text(body)
    give = bool(GIVE.search(cta))
    time_ask = bool(TIME_ASK.search(cta))
    if give and not time_ask:
        return "value-give", True
    if give and time_ask:
        return "hybrid (give+time)", False  # offers value but still asks for time
    if time_ask:
        return "time-ask", False
    if SOFT_PERM.search(cta):
        return "soft-permission", False
    if cta.endswith("?"):
        return "open-question", False
    return "none/unclear", False


def main():
    if not DATASET.exists():
        print(f"ERROR: {DATASET} not found. Run the email-bison fetch script first.")
        return 1
    records = [json.loads(l) for l in DATASET.open() if l.strip()]

    rows = []
    for r in records:
        title = r["lead"].get("title") or ""
        role, icp = buyer_role(title)
        if not icp:
            continue
        win_step = find_winning_step(r)
        win_body = cv(r, f"body{win_step}") if win_step else cv(r, "body1")
        cls, value_first = classify_cta(win_body)
        rows.append({
            "reply_id": r["reply_id"],
            "role": role,
            "lead_name": " ".join(p for p in [r["lead"].get("first_name"), r["lead"].get("last_name")] if p),
            "title": title,
            "company": r["lead"].get("company"),
            "campaign_name": r.get("campaign_name"),
            "winning_step": win_step,
            "cta_class": cls,
            "value_first": value_first,
            "cta_sentence": cta_text(win_body),
            "reply_text": clean_message(r.get("interested_reply_text") or ""),
        })

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    with (OUT_DIR / "icp_cta.jsonl").open("w") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")

    n = len(rows)
    by_role = Counter(r["role"] for r in rows)
    by_cta = Counter(r["cta_class"] for r in rows)
    vf = sum(1 for r in rows if r["value_first"])

    # Report ------------------------------------------------------------------
    L = [
        "# ICP Buyer-Group CTA Analysis (Phase 0 — go-forward baseline)",
        "",
        f"_Filtered the {len(records)} interested replies to the ICP buyer group "
        f"(GTM leadership at B2B tech startups) → **{n} ICP replies**. CTA of each winning "
        "email classified on a value-first ↔ ask-for-time axis._",
        "",
        "> ⚠️ The underlying data is OLD GTM (many lead sources, mostly time-ask CTAs). This is "
        "the **baseline we iterate away from**, not the target. Value-first CTAs are expected to "
        "be sparse here — that's exactly the gap the new strategy fills.",
        "",
        "## ICP replies by buyer role",
        "| Role | ICP replies |",
        "|---|---:|",
    ]
    for role, c in by_role.most_common():
        L.append(f"| {role} | {c} |")
    L += [
        "",
        f"_(For reference: {len(records)} total replies, only {n} ({round(100*n/len(records))}%) "
        "were from the ICP buyer group under the old GTM. Founders/CEOs and non-GTM roles excluded.)_",
        "",
        "## CTA class of the winning email (ICP replies)",
        "| CTA class | count | value-first? |",
        "|---|---:|:--:|",
    ]
    vf_map = {"value-give": "✅", "hybrid (give+time)": "partial", "soft-permission": "—",
              "open-question": "—", "time-ask": "❌", "none/unclear": "—"}
    for cls, c in by_cta.most_common():
        L.append(f"| {cls} | {c} | {vf_map.get(cls, '—')} |")
    L += [
        "",
        f"**Value-first CTAs: {vf}/{n} ({round(100*vf/n) if n else 0}%).** "
        "The rest ask for time or are soft/open asks — the iteration opportunity.",
        "",
        "## Verbatim value-first CTAs that earned ICP replies (the seed examples)",
    ]
    vf_rows = [r for r in rows if r["value_first"]]
    if vf_rows:
        for r in vf_rows:
            L.append(f"- **{r['lead_name']} · {r['title']} · {r['company']} (reply {r['reply_id']})**")
            L.append(f"  - CTA: > {r['cta_sentence']}")
            if r["reply_text"]:
                L.append(f"  - ↳ reply: {' '.join(r['reply_text'].split())[:160]}")
    else:
        L.append("_None in the old data — every ICP reply came from a time-ask/soft/open CTA. "
                 "The value-first library in `ai-sdr/knowledge/cta-offers.md` is therefore "
                 "design-forward; this run is the before-picture._")
    L += [
        "",
        "## So what",
        "- Going forward, target only these roles; the 69% of old replies outside the ICP are noise.",
        "- Move CTAs from time-ask → value-give. Use `ai-sdr` to generate value-first sequences and",
        "  measure the value-first share climbing on the next pull.",
    ]
    (OUT_DIR / "icp-cta-report.md").write_text("\n".join(L))

    print(f"ICP replies: {n}/{len(records)}")
    print("By role:", dict(by_role))
    print("By CTA class:", dict(by_cta))
    print(f"Value-first CTAs: {vf}/{n}")
    print(f"Wrote icp_cta.jsonl + icp-cta-report.md to {OUT_DIR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
