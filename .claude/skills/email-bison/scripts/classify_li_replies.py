"""Classify inbound HeyReach (LinkedIn) replies for the unified review queue.

The LinkedIn counterpart of classify_replies.py. Pulls the configured HeyReach
campaign's conversations, keeps the ones where the lead messaged us last
(lastMessageSender == CORRESPONDENT — i.e. awaiting our reply), and runs each
inbound message through the SAME Claude classifier the email path uses. Each
surfaced reply carries the LinkedIn sender account + the lead profile + the
LinkedIn messages we sent, so the Replies card has full context.

Items are written channel="linkedin" in the same shape as the email queue, and
interested ones are marked already_interested=True so they land directly in the
draft+send section (there is no Bison "Interested tag" step for LinkedIn).

  python3 classify_li_replies.py [--lookback 14] [--max 200]

Writes data/interested-replies/li_review_queue.json. Degrades to an empty queue
(exit 0) when HeyReach isn't configured, so it never breaks a unified scan.
"""

import argparse
import json
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent                     # email-bison/scripts
SKILLS = HERE.parents[1]                                    # .claude/skills
PROJECT_ROOT = HERE.parents[3]
sys.path.insert(0, str(HERE))                              # classify_replies
sys.path.insert(0, str(SKILLS / "sdr-pipeline" / "scripts"))   # heyreach_client
sys.path.insert(0, str(SKILLS / "ai-sdr" / "scripts"))     # anthropic_client

from anthropic_client import AnthropicClient                # noqa: E402
# Reuse the email classifier verbatim — same definition, few-shots, schema.
from classify_replies import (                              # noqa: E402
    build_system, load_fewshot, classify_one, strip_quoted,
    now_iso, INTERESTED_MIN_CONFIDENCE,
)
from heyreach_client import HeyReachClient, HeyReachError   # noqa: E402

OUT = PROJECT_ROOT / "data" / "interested-replies" / "li_review_queue.json"


def heyreach_campaign_ids():
    import os
    raw = (os.environ.get("HEYREACH_CAMPAIGN_ID") or "").strip()
    return [int(x) for x in raw.replace(" ", "").split(",") if x.isdigit()]


def sender_display(conv):
    """Human label for the LinkedIn sender account that holds this conversation."""
    acct = conv.get("linkedInAccount") or {}
    name = " ".join(x for x in (acct.get("firstName"), acct.get("lastName")) if x).strip()
    return name or acct.get("emailAddress") or f"account {conv.get('linkedInAccountId')}"


def inbound_text(conv):
    """The lead's most recent message run — the trailing consecutive CORRESPONDENT
    messages we're replying to (newest-last), quoted history stripped."""
    msgs = conv.get("messages") or []
    run = []
    for m in reversed(msgs):
        if m.get("sender") == "CORRESPONDENT":
            run.append((m.get("body") or "").strip())
        else:
            break
    text = "\n\n".join(reversed([t for t in run if t]))
    if not text:  # fall back to the conversation summary field
        text = (conv.get("lastMessageText") or "").strip()
    return strip_quoted(text)


def sent_messages(conv):
    """The LinkedIn messages WE sent this lead (sender == ME), newest first, shaped
    like the email card's sent_emails so the UI component renders them as-is."""
    out = []
    for m in conv.get("messages") or []:
        if m.get("sender") == "ME":
            out.append({"subject": m.get("subject"), "date": m.get("createdAt"),
                        "text": (m.get("body") or "")[:1500]})
    out.sort(key=lambda x: x.get("date") or "", reverse=True)
    return out[:5]


def build_item(conv, cls):
    cp = conv.get("correspondentProfile") or {}
    name = " ".join(x for x in (cp.get("firstName"), cp.get("lastName")) if x).strip()
    # Only surface-confidence interested replies advance to draft+send; a low-confidence
    # "interested" lands in the collapsed "other replies" list (matches the UI partition,
    # and avoids an item that's interested-but-below-threshold falling through every bucket).
    interested = bool(cls.get("interested")) and float(cls.get("confidence") or 0) > INTERESTED_MIN_CONFIDENCE
    return {
        "channel": "linkedin",
        # conversationId is the universal reply key on the LinkedIn side.
        "reply_id": conv.get("id"),
        "conversation_id": conv.get("id"),
        "linkedin_account_id": conv.get("linkedInAccountId"),
        "profile_url": cp.get("profileUrl"),
        "first_name": cp.get("firstName"),
        "last_name": cp.get("lastName"),
        "from_name": name or None,
        "from_email": cp.get("emailAddress") or cp.get("enrichedEmailAddress") or "",
        "title": cp.get("position") or cp.get("headline"),
        "company": cp.get("companyName"),
        "subject": None,                                   # LinkedIn DMs have no subject
        "date_received": conv.get("lastMessageAt"),
        "text_body": inbound_text(conv)[:1500],
        "sending_email": sender_display(conv),             # the LinkedIn sender account
        "sent_emails": sent_messages(conv),                # outbound LI messages we sent
        "enriched": True,
        # interested LinkedIn replies skip the Bison "tag interested" gate and go
        # straight to draft+send, so mark them already_interested for the UI partition.
        "already_interested": interested,
        "classifier": cls,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--lookback", type=int, default=14)
    ap.add_argument("--max", type=int, default=200)
    args = ap.parse_args()

    empty = {"scanned_at": now_iso(), "channel": "linkedin", "lookback_days": args.lookback,
             "configured": False, "counts": {"scanned": 0, "flagged": 0}, "items": []}

    try:
        hr = HeyReachClient()                # constructing the client loads .env
    except HeyReachError as e:
        empty["error"] = str(e)[:200]
        OUT.parent.mkdir(parents=True, exist_ok=True)
        OUT.write_text(json.dumps(empty, indent=2, ensure_ascii=False))
        print(f"HeyReach not configured ({e}); wrote empty LinkedIn queue.")
        return 0
    campaign_ids = heyreach_campaign_ids()   # safe to read env now (.env loaded above)
    if not campaign_ids:
        OUT.parent.mkdir(parents=True, exist_ok=True)
        OUT.write_text(json.dumps(empty, indent=2, ensure_ascii=False))
        print("HEYREACH_CAMPAIGN_ID not set; wrote empty LinkedIn queue.")
        return 0

    cutoff = datetime.now(timezone.utc) - timedelta(days=args.lookback)

    # Candidate conversations: the lead messaged us last, in-window.
    candidates, filtered = [], {"we_replied_last": 0, "out_of_window": 0, "empty": 0}
    try:
        for conv in hr.iter_conversations(campaign_ids=campaign_ids, max_items=args.max):
            if conv.get("lastMessageSender") != "CORRESPONDENT":
                filtered["we_replied_last"] += 1
                continue
            la = conv.get("lastMessageAt")
            if la:
                try:
                    when = datetime.fromisoformat(la.replace("Z", "+00:00"))
                    if when < cutoff:
                        filtered["out_of_window"] += 1
                        continue
                except ValueError:
                    pass
            if not inbound_text(conv).strip():
                filtered["empty"] += 1
                continue
            candidates.append(conv)
    except HeyReachError as e:
        empty["configured"] = True
        empty["error"] = str(e)[:200]
        OUT.parent.mkdir(parents=True, exist_ok=True)
        OUT.write_text(json.dumps(empty, indent=2, ensure_ascii=False))
        print(f"HeyReach inbox fetch failed: {e}")
        return 1

    client = AnthropicClient()
    system = build_system(load_fewshot())

    def work(conv):
        cls = classify_one(client, system, inbound_text(conv)[:4000])
        return build_item(conv, cls)

    items = []
    if candidates:
        with ThreadPoolExecutor(max_workers=4) as ex:
            futures = [ex.submit(work, c) for c in candidates]
            for fut in as_completed(futures):
                items.append(fut.result())

    items.sort(key=lambda it: (not it["classifier"]["interested"],
                               -it["classifier"]["confidence"]))
    flagged = sum(1 for it in items
                  if it["classifier"]["interested"]
                  and it["classifier"]["confidence"] > INTERESTED_MIN_CONFIDENCE)
    payload = {
        "scanned_at": now_iso(), "channel": "linkedin", "lookback_days": args.lookback,
        "configured": True, "campaign_ids": campaign_ids,
        "counts": {"scanned": len(items), "flagged": flagged, "filtered": sum(filtered.values())},
        "filtered": filtered, "items": items,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, indent=2, ensure_ascii=False))
    print(f"scanned {len(items)} LinkedIn replies, flagged {flagged} interested, "
          f"filtered {sum(filtered.values())} -> {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
