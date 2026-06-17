"""Guardrail linter for generated ICP email sequences (the "reviewed vs guardrails" step).

Parses a markdown sequence file with blocks:
    ## Step N — Subject: <subject>
    <body...>
and checks each email against the rules in knowledge/icp-email.md + cta-offers.md.

Checks per email: word count 70–100, personalized opener, ≥1 metric in the sequence, a single
clear CTA, **value-first CTA (no bare time-ask)**, breakup in the final step, and **no pricing in
cold steps**. Exit code 0 iff every email passes.

Usage:  python3 .claude/skills/ai-sdr/scripts/lint_sequence.py <sequence.md>
"""

import re
import sys
from pathlib import Path

STEP_RE = re.compile(r"^##\s*Step\s*(\d+)\s*[—\-:]+\s*Subject:\s*(.*)$", re.I)
SENT_SPLIT = re.compile(r"(?<=[.!?])\s+|\n+")

# Deliverable value-first gives (see cta-offers.md).
GIVE = re.compile(
    r"signal play|pipeline (model|gap)|walk you through|"
    r"\bsent over\b|\bsend (it|that|those|them|you|over|the)\b|can i send|i can send|"
    r"happy to send|want me to send|want (that|it|those|the|to see)\b|"
    r"leave .*(playbook|one-?pager|teardown)|"
    r"3 (personalized|tailored) (emails|drafts)|personalized (emails|drafts)|"
    r"one-?page(r)?|\bteardown\b|\bbenchmark\b|\bplaybook\b|plays? scoped|ai-?sdr plays?", re.I)
# Undeliverable / banned gives — we CANNOT hand a prospect their own visitors or in-market lists.
FORBIDDEN_CTA = re.compile(
    r"de-?anonymized|companies that visited|visited .*(site|website)|"
    r"\d+ (in-?market )?accounts (already )?showing|accounts (already )?showing (buying )?signals|"
    r"the list of companies|in-market account list|list of (your )?(site |website )?visitors", re.I)
# Trailing sign-off / name (the campaign appends the signature; the body must end on the CTA).
SIGNOFF_LINE = re.compile(r"^\s*[—\-]?\s*[A-Z][a-z]+\s*$|"
                          r"^\s*(best|thanks|cheers|regards|warmly|sincerely|talk soon)\b", re.I)
# Em dash / en dash — never use; replace with commas or periods.
DASH = re.compile(r"[—–]")
# A MEETING ask is now REQUIRED in every CTA (the give is delivered on the call).
MEETING = re.compile(
    r"walk you through|walk (you|them) through|hop on|jump on|"
    r"grab (15|20|30|a |some )?(min|minute|time)|grab time|"
    r"\b\d{1,2}[-\s]?(min|minute)s?\b|worth (15|20|a quick|a few|\d)|"
    r"on a (quick )?(call|chat)|quick (call|chat)|book (a )?(call|time|meeting|slot)|"
    r"calendar|calendly|\bdemo\b|open to (a )?(call|chat|conversation|meeting)|"
    r"\d{1,2}\s?(am|pm)|set up a call|put (15|time) on|chat live|trade notes", re.I)
BREAKUP = re.compile(
    r"close (your|the) file|closing the loop|last (note|email|one)|final note|should i close|"
    r"off your radar|move on|i'?ll stop|stop (reaching out|crowding|cluttering|emailing|bugging|"
    r"nudging|chasing)|won'?t (chase|bug|nudge|keep)|circle back later|leave (you|it)", re.I)
METRIC = re.compile(r"\d+\s?%|\b\d+\s?[-–]?\s?\d*\s?x\b|3[-–]5x|\$\d|\d{2,}\s?(replies|meetings|accounts|deals|contacts|leads)|\bbant\b", re.I)
# Pricing in cold steps = OUR monthly pricing, NOT funding/proof dollar amounts ($12M, $2.7M).
PRICING = re.compile(r"/mo\b|per month|\bpricing\b|\bopt-?out\b|\bmonthly (price|fee|cost|rate)\b|"
                     r"\$\s?\d[\d.,]*\s?k?\s*/\s*mo|\b\d(\.\d)?\s?k\s*(/mo|per month)|"
                     r"\bcost(s|ing)?\s+\$\d", re.I)
SIGNAL_OPENER = re.compile(r"\b(saw|noticed|congrats|read|caught|came across|spotted|loved|"
                           r"following|saw that|just saw)\b", re.I)


def sentences(text):
    return [s.strip() for s in SENT_SPLIT.split(text or "") if s.strip()]


def parse_steps(md):
    steps, cur = [], None
    for line in md.splitlines():
        m = STEP_RE.match(line.strip())
        if m:
            if cur:
                steps.append(cur)
            cur = {"n": int(m.group(1)), "subject": m.group(2).strip(), "body": []}
        elif cur is not None:
            if line.strip().startswith("## "):  # next non-step heading ends the block
                steps.append(cur)
            cur["body"].append(line)
    if cur:
        steps.append(cur)
    for s in steps:
        s["body"] = "\n".join(s["body"]).strip()
    return steps


def cta_region(body):
    """The ask region = last few sentences, ignoring one-word sign-offs (e.g. 'Mike')."""
    sents = [s for s in sentences(body) if len(s.split()) > 2]
    return " ".join(sents[-3:])


def lint_email(step, is_last, is_first=False):
    body = step["body"]
    wc = len(re.findall(r"[A-Za-z0-9']+", body))
    issues = []

    if not (70 <= wc <= 110):
        issues.append(f"word count {wc} (need 70–110)")

    # Paragraph breaks: body should be a few short paragraphs, not one block.
    if "\n\n" not in body:
        issues.append("no paragraph breaks (separate opener / value / CTA with blank lines)")

    # No trailing sign-off or name — the campaign appends the signature.
    body_lines = [ln.strip() for ln in body.splitlines() if ln.strip()]
    if body_lines and SIGNOFF_LINE.match(body_lines[-1]):
        issues.append(f"trailing sign-off/name '{body_lines[-1]}' (end on the CTA, no sign-off)")

    if FORBIDDEN_CTA.search(body):
        issues.append("CTA promises an undeliverable give (de-anon visitors / in-market account list)")

    if DASH.search(body):
        issues.append("em/en dash present (never use — or –; use commas or periods)")

    # Personalized opener is only required on the COLD step 1; follow-ups/breakups
    # legitimately open with just the first name.
    if is_first:
        opener = " ".join(sentences(body)[:1])
        proper = re.findall(r"\b[A-Z][a-zA-Z0-9.&]+\b", opener)
        if not (SIGNAL_OPENER.search(opener) or len(proper) >= 2):
            issues.append("step-1 opener not personalized (no signal verb / named company)")

    cta = cta_region(body)
    n_q = body.count("?")
    if n_q == 0:
        issues.append("no clear ask (no question CTA)")
    elif n_q > 3:  # allow rhetorical questions; flag only clear over-asking
        issues.append(f"{n_q} questions (too many asks)")

    # Every CTA must ASK FOR A MEETING and ANCHOR it on a deliverable give.
    give = bool(GIVE.search(cta) or GIVE.search(body))
    meeting = bool(MEETING.search(cta))
    if not meeting:
        issues.append("CTA must ask for a meeting (the give is delivered on the call)")
    if not give:
        issues.append("CTA has no value hook (anchor the meeting on a give)")

    if PRICING.search(body):
        issues.append("pricing/plan language in a cold step (move to replies only)")

    if is_last and not BREAKUP.search(body):
        issues.append("final step is not a breakup")

    return wc, issues


def main():
    if len(sys.argv) < 2:
        print("usage: lint_sequence.py <sequence.md>")
        return 2
    path = Path(sys.argv[1])
    if not path.is_file():
        print(f"ERROR: {path} not found")
        return 2

    md = path.read_text()
    steps = parse_steps(md)
    if not steps:
        print("ERROR: no '## Step N — Subject: …' blocks found")
        return 2

    full_text = " ".join(s["body"] for s in steps)
    seq_has_metric = bool(METRIC.search(full_text))

    all_pass = True
    print(f"Linting {path.name} — {len(steps)} emails\n")
    for i, step in enumerate(steps):
        is_last = i == len(steps) - 1
        wc, issues = lint_email(step, is_last, is_first=(i == 0))
        status = "PASS" if not issues else "FAIL"
        if issues:
            all_pass = False
        print(f"  Step {step['n']} [{status}] ({wc}w) — {step['subject']}")
        for it in issues:
            print(f"      ✗ {it}")

    if not seq_has_metric:
        all_pass = False
        print("\n  Sequence-level ✗ no concrete metric anywhere in the sequence")
    print()
    print("ALL PASS ✅" if all_pass else "FAILURES ❌")
    return 0 if all_pass else 1


if __name__ == "__main__":
    raise SystemExit(main())
