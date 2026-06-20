"""Classify inbound Bison replies as interested / not, for the review queue.

Bison's own interested detection misses replies, so we run each recent inbound
reply through Claude (grounded in a definition + few-shot examples from the
ground-truth dataset) and write a review queue the UI surfaces for approval.
Tagging itself is gated and happens elsewhere (mark-as-interested + Interested tag).

  python3 classify_replies.py [--lookback 14] [--campaign 10] [--max 300]

Writes data/interested-replies/review_queue.json.
"""

import argparse
import json
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent                     # email-bison/scripts
SKILLS = HERE.parents[1]                                    # .claude/skills
PROJECT_ROOT = HERE.parents[3]
sys.path.insert(0, str(HERE))                              # bison_client, fetch_interested_replies
sys.path.insert(0, str(SKILLS / "ai-sdr" / "scripts"))     # anthropic_client

from bison_client import BisonClient, BisonError            # noqa: E402
from fetch_interested_replies import message_text           # noqa: E402
from anthropic_client import (                              # noqa: E402
    AnthropicClient, extract_json, AnthropicError, AnthropicJSONError,
)

OUT = PROJECT_ROOT / "data" / "interested-replies" / "review_queue.json"
DATASET = PROJECT_ROOT / "data" / "interested-replies" / "dataset.jsonl"

DEFINITION = """\
You label B2B sales email replies by whether the sender shows INTEREST in eventually meeting/buying.

INTERESTED (interested=true): ANY positive buying signal toward a conversation, INCLUDING
future-dated ones. Wants a call/demo/meeting now OR later; shares a calendar link or availability;
says the outreach is good/relevant and wants to talk; asks to learn more / for materials; asks about
pricing or how it works with positive intent; loops in a colleague to evaluate. CRUCIAL: a warm
deferral that still expresses interest IS interested, e.g. "love to set up a time but we're not
ready yet, let's talk in July", "circle back after our launch", "reach out to me in Q3", "great
emails, ping me next quarter". These are positive-later leads (intent=positive_later); tag them
interested.

NOT INTERESTED (interested=false): out-of-office / auto-reply; unsubscribe / remove me; a FLAT
decline with no positive sentiment and no future commitment ("not interested", "no budget", "not a
fit", "wrong person", a vague "not now / maybe someday" brush-off); pure bounce / delivery notices;
hostile or spam complaints; marketing newsletters. A referral that just says "talk to X" with no
buying intent is intent=referral, interested=false.

The line: positive sentiment + a real forward intent (even a future date) = interested. A flat or
vague brush-off with no warmth = not interested.

Judge ONLY the sender's new message text (quoted history is stripped)."""

SCHEMA = """\
Return ONLY this JSON, no prose:
{"interested": true|false, "confidence": 0.0-1.0, "reason": "<one short sentence>",
 "intent": "meeting_request|info_request|pricing|positive_later|positive_other|referral|not_interested|auto_reply|unsubscribe"}"""

NEG_EXAMPLES = [
    # positive-later: warm intent + a future meeting date IS interested
    ("These emails are great. I'd love to set up a time but we're not in a place to move yet. Let's talk again mid-July.",
     '{"interested": true, "confidence": 0.85, "reason": "Positive sentiment and a future meeting ask (mid-July).", "intent": "positive_later"}'),
    ("I'm currently out of office returning Monday. For urgent matters contact ops@acme.com.",
     '{"interested": false, "confidence": 0.97, "reason": "Automated out-of-office reply.", "intent": "auto_reply"}'),
    ("Please remove me from your list and do not contact me again.",
     '{"interested": false, "confidence": 0.98, "reason": "Explicit unsubscribe request.", "intent": "unsubscribe"}'),
    ("Not interested, this isn't a fit for us.",
     '{"interested": false, "confidence": 0.9, "reason": "Flat decline with no positive intent.", "intent": "not_interested"}'),
]

_QUOTE_MARKERS = re.compile(
    r"(?im)^(on .+ wrote:|from:\s|sent from|get outlook|_{5,}|-{5,}|>{1,}|"
    r"on .+,.+<.+@.+> wrote)")


def strip_quoted(text):
    """Keep only the sender's new text: cut at the first quoted-history marker."""
    if not text:
        return ""
    lines = text.splitlines()
    kept = []
    for ln in lines:
        if _QUOTE_MARKERS.match(ln.strip()):
            # keep a trailing 'Get Outlook' style footer out; stop here
            break
        kept.append(ln)
    out = "\n".join(kept).strip()
    return out or text.strip()


def load_fewshot():
    pos = []
    if DATASET.is_file():
        for line in DATASET.read_text().splitlines()[:6]:
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            txt = (rec.get("interested_reply_text") or "").strip()
            if txt:
                pos.append((strip_quoted(txt)[:600],
                            '{"interested": true, "confidence": 0.95, "reason": "Wants to talk / shares availability.", "intent": "meeting_request"}'))
            if len(pos) >= 3:
                break
    examples = pos + NEG_EXAMPLES
    blocks = []
    for text, label in examples:
        blocks.append(f"REPLY:\n{text}\n\nLABEL:\n{label}")
    return "\n\n---\n\n".join(blocks)


def build_system(fewshot):
    return f"{DEFINITION}\n\n{SCHEMA}\n\n# Examples\n\n{fewshot}"


def classify_one(client, system, text):
    try:
        res = client.complete(system, f"REPLY:\n{text}\n\nLABEL:", use_web_search=False,
                              max_tokens=300)
        data = extract_json(res["text"])
        return {
            "interested": bool(data.get("interested")),
            "confidence": float(data.get("confidence", 0)),
            "reason": str(data.get("reason", ""))[:300],
            "intent": str(data.get("intent", "")),
        }
    except (AnthropicError, AnthropicJSONError, ValueError) as e:
        return {"interested": False, "confidence": 0.0, "reason": f"classify error: {e}"[:200],
                "intent": "error"}


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--lookback", type=int, default=14)
    ap.add_argument("--campaign", type=int, default=None)
    ap.add_argument("--max", type=int, default=300)
    args = ap.parse_args()

    client = AnthropicClient()
    bison = BisonClient()
    system = build_system(load_fewshot())
    cutoff = datetime.now(timezone.utc) - timedelta(days=args.lookback)

    # gather candidate inbound replies
    candidates = []
    for reply in bison.list_replies(folder="inbox"):
        if len(candidates) >= args.max:
            break
        if reply.get("automated_reply"):
            continue
        if args.campaign and reply.get("campaign_id") != args.campaign:
            continue
        dr = reply.get("date_received")
        if dr:
            try:
                when = datetime.fromisoformat(dr.replace("Z", "+00:00"))
                if when < cutoff:
                    continue
            except ValueError:
                pass
        candidates.append(reply)

    def work(reply):
        text = strip_quoted(message_text(reply))
        cls = classify_one(client, system, text[:4000])
        return {
            "reply_id": reply.get("id"),
            "lead_id": reply.get("lead_id"),
            "from_name": reply.get("from_name"),
            "from_email": reply.get("from_email_address"),
            "subject": reply.get("subject"),
            "campaign_id": reply.get("campaign_id"),
            "date_received": reply.get("date_received"),
            "text_body": text[:1500],
            "already_interested": bool(reply.get("interested")),
            "classifier": cls,
        }

    items = []
    if candidates:
        with ThreadPoolExecutor(max_workers=4) as ex:
            futures = [ex.submit(work, r) for r in candidates]
            for fut in as_completed(futures):
                items.append(fut.result())

    items.sort(key=lambda it: (not it["classifier"]["interested"],
                               -it["classifier"]["confidence"]))
    flagged = sum(1 for it in items if it["classifier"]["interested"] and not it["already_interested"])
    payload = {
        "scanned_at": now_iso(),
        "lookback_days": args.lookback,
        "campaign_id": args.campaign,
        "counts": {"scanned": len(items), "flagged": flagged,
                   "already": sum(1 for it in items if it["already_interested"])},
        "items": items,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, indent=2, ensure_ascii=False))
    print(f"scanned {len(items)} replies, flagged {flagged} interested (not yet tagged) -> {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
