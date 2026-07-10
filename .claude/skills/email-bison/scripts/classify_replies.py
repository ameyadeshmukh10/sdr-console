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
from fetch_interested_replies import message_text, html_to_text  # noqa: E402
from anthropic_client import (                              # noqa: E402
    AnthropicClient, extract_json, AnthropicError, AnthropicJSONError,
)

OUT = PROJECT_ROOT / "data" / "interested-replies" / "review_queue.json"
DATASET = PROJECT_ROOT / "data" / "interested-replies" / "dataset.jsonl"

# Senders that are never real leads — system/alert mail + known non-prospect domains.
JUNK_SENDER_DOMAINS = {"thepreachtruck.com"}
JUNK_SENDER_SUBSTR = ("google-workspace-alerts", "workspace-alerts-noreply",
                      "calendar-notification@google", "mailer-daemon", "postmaster@",
                      "no-reply@google", "noreply@google")
# Subjects that are our own infra/test mail, not prospect replies.
SKIP_SUBJECT = re.compile(r"everworker connection test", re.I)
# Confidence at/above which an unsubscribe/"close the file" reply is auto-suppressed.
UNSUB_CONFIDENCE = 0.80
# Only interested/referral replies above this confidence are surfaced + enriched.
INTERESTED_MIN_CONFIDENCE = 0.50


def enrich_item(bison, item):
    """Attach the prospect's profile (name/title), the outbound emails we sent, and
    the sending inbox to a candidate item — so the Replies card has full context.
    Degrades silently on any lookup failure (one bad lead/thread won't fail a scan)."""
    try:
        lead = bison.get_lead(item["lead_id"]) or {}
    except BisonError:
        lead = {}
    item["first_name"] = lead.get("first_name")
    item["last_name"] = lead.get("last_name")
    item["title"] = lead.get("title")
    item["lead_email"] = lead.get("email")
    # The emails WE sent this lead (the outbound sequence) come from the dedicated
    # sent-emails endpoint — the conversation-thread often omits them.
    sent, sending = [], None
    try:
        rows = bison.get_lead_sent_emails(item["lead_id"]) or []
    except BisonError:
        rows = []
    rows = sorted(rows, key=lambda m: m.get("sent_at") or "", reverse=True)[:10]
    for m in rows:
        se = m.get("sender_email")
        addr = (se.get("email") if isinstance(se, dict) else se) or None
        sending = sending or addr
        sent.append({"subject": m.get("email_subject"), "date": m.get("sent_at"),
                     "from_email": addr, "text": html_to_text(m.get("email_body"))[:10000]})
    item["sending_email"] = sending
    item["sent_emails"] = sent  # newest first, the outbound sequence we sent
    item["enriched"] = True
    return item


def is_junk_sender(email):
    e = (email or "").lower()
    if not e or "@" not in e:
        return True
    if any(s in e for s in JUNK_SENDER_SUBSTR):
        return True
    return e.rsplit("@", 1)[-1] in JUNK_SENDER_DOMAINS

DEFINITION = """\
You label B2B sales email replies by whether the sender shows INTEREST in eventually meeting/buying.

INTERESTED (interested=true): ANY positive buying signal toward a conversation, INCLUDING
future-dated ones. Wants a call/demo/meeting now OR later; shares a calendar link or availability;
says the outreach is good/relevant and wants to talk; asks to learn more / for materials; asks about
pricing or how it works with positive intent; loops in a colleague to evaluate. CRUCIAL: a warm
deferral that still expresses interest IS interested, e.g. "love to set up a time but we're not
ready yet, let's talk in July", "circle back after our launch", "reach out to me in Q3", "great
emails, ping me next quarter". These are positive-later leads (intent=positive_later); tag them
interested. A REFERRAL that hands you to the right person — "reach out to Jane, she owns this",
"talk to our VP of Sales", "forwarding to our RevOps lead" — is interested=true, intent=referral:
it's an actionable warm handoff, treat it as a positive outcome.

NOT INTERESTED (interested=false): out-of-office / auto-reply; a FLAT decline with no positive
sentiment and no future commitment ("not interested", "no budget", "not a fit", "wrong person", a
vague "not now / maybe someday" brush-off); pure bounce / delivery notices; marketing newsletters.

UNSUBSCRIBE (interested=false, intent=unsubscribe): an explicit ask to stop / opt out / be removed,
INCLUDING "please close the file", "close us out", "stop contacting me", "remove me from your list",
"do not reach out again", or a hostile spam complaint. These get suppressed automatically.

The line: positive sentiment OR an actionable handoff = interested. A flat brush-off = not
interested. An explicit "stop / close the file" = unsubscribe.

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
    ("We're not moving forward on this — please close the file and stop reaching out.",
     '{"interested": false, "confidence": 0.95, "reason": "Explicit ask to close the file / stop contact.", "intent": "unsubscribe"}'),
    ("I'm not the right person for this — reach out to Jenna, our VP of Sales, she owns this.",
     '{"interested": true, "confidence": 0.88, "reason": "Actionable referral to the VP of Sales.", "intent": "referral"}'),
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
        intent = str(data.get("intent", ""))
        interested = bool(data.get("interested"))
        if intent == "referral":  # a referral is an actionable handoff — always interested
            interested = True
        return {
            "interested": interested,
            "confidence": float(data.get("confidence", 0)),
            "reason": str(data.get("reason", ""))[:300],
            "intent": intent,
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
    ap.add_argument("--apply-unsubscribes", action="store_true",
                    help="actually unsubscribe + blacklist high-confidence unsubscribe replies "
                         "in Bison (default: dry-run, just preview them)")
    args = ap.parse_args()

    client = AnthropicClient()
    bison = BisonClient()
    system = build_system(load_fewshot())
    cutoff = datetime.now(timezone.utc) - timedelta(days=args.lookback)

    # gather candidate inbound replies, dropping ones that should never surface:
    # auto-replies, out-of-window, non-lead senders, junk/system senders, infra test mail.
    candidates = []
    filtered = {"automated": 0, "non_lead": 0, "junk_sender": 0, "skip_subject": 0}
    for reply in bison.list_replies(folder="inbox"):
        if len(candidates) >= args.max:
            break
        if reply.get("automated_reply"):
            filtered["automated"] += 1
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
        if not reply.get("lead_id"):                       # not a known lead (e.g. random sender)
            filtered["non_lead"] += 1
            continue
        if is_junk_sender(reply.get("from_email_address")):  # system/alert mail, junk domains
            filtered["junk_sender"] += 1
            continue
        if SKIP_SUBJECT.search(reply.get("subject") or ""):  # our own connection-test mail
            filtered["skip_subject"] += 1
            continue
        candidates.append(reply)

    def work(reply):
        text = strip_quoted(message_text(reply))
        cls = classify_one(client, system, text[:4000])
        return {
            "reply_id": reply.get("id"),
            "lead_id": reply.get("lead_id"),
            "sender_email_id": reply.get("sender_email_id"),  # Bison inbox to reply FROM
            "from_name": reply.get("from_name"),
            "from_email": reply.get("from_email_address"),
            "subject": reply.get("subject"),
            "campaign_id": reply.get("campaign_id"),
            "date_received": reply.get("date_received"),
            "text_body": text[:10000],
            "already_interested": bool(reply.get("interested")),
            "classifier": cls,
        }

    classified = []
    if candidates:
        with ThreadPoolExecutor(max_workers=4) as ex:
            futures = [ex.submit(work, r) for r in candidates]
            for fut in as_completed(futures):
                classified.append(fut.result())

    # Partition off high-confidence unsubscribe / "close the file" replies — these
    # get suppressed in Bison and never shown in the review queue.
    def is_unsub(it):
        c = it["classifier"]
        return c["intent"] == "unsubscribe" and c["confidence"] >= UNSUB_CONFIDENCE and it["lead_id"]

    unsub = [it for it in classified if is_unsub(it)]
    items = [it for it in classified if not is_unsub(it)]

    # Apply (or preview) the unsubscribe + blacklist in Bison.
    unsub_done, unsub_errors = [], []
    for it in unsub:
        rec = {"reply_id": it["reply_id"], "lead_id": it["lead_id"],
               "from_email": it["from_email"], "reason": it["classifier"]["reason"]}
        if args.apply_unsubscribes:
            try:
                bison.unsubscribe_lead(it["lead_id"])
                bison.blacklist_lead(it["lead_id"])
                unsub_done.append(rec)
            except BisonError as e:
                unsub_errors.append({**rec, "error": str(e)[:200]})
        else:
            unsub_done.append({**rec, "dry_run": True})

    # Enrich only the candidates the UI surfaces (interested/referral > 0.50) with
    # the lead profile + the outbound emails we sent + the sending inbox. Bounded
    # so the 2 Bison calls/candidate stay proportional to what's shown.
    to_enrich = [it for it in items
                 if it["classifier"]["interested"] and it["classifier"]["confidence"] > INTERESTED_MIN_CONFIDENCE
                 and it.get("lead_id")]
    if to_enrich:
        with ThreadPoolExecutor(max_workers=4) as ex:
            list(ex.map(lambda it: enrich_item(bison, it), to_enrich))

    items.sort(key=lambda it: (not it["classifier"]["interested"],
                               -it["classifier"]["confidence"]))
    flagged = sum(1 for it in items if it["classifier"]["interested"] and not it["already_interested"])
    payload = {
        "scanned_at": now_iso(),
        "lookback_days": args.lookback,
        "campaign_id": args.campaign,
        "counts": {"scanned": len(items), "flagged": flagged,
                   "already": sum(1 for it in items if it["already_interested"]),
                   "unsubscribed": len(unsub), "filtered": sum(filtered.values())},
        "filtered": filtered,
        "unsubscribed": {"applied": args.apply_unsubscribes, "items": unsub_done,
                         "errors": unsub_errors},
        "items": items,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, indent=2, ensure_ascii=False))
    mode = "unsubscribed" if args.apply_unsubscribes else "would unsubscribe (dry-run)"
    print(f"scanned {len(items)} replies, flagged {flagged} interested, "
          f"{mode} {len(unsub)}, filtered {sum(filtered.values())} -> {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
