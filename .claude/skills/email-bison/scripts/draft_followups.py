"""Draft a follow-up reply for each interested lead — grounded in the product
knowledge + the reply-handling playbook. Draft-for-review: writes
followup_drafts.json; the UI shows each draft for edit/approve, and only on
approval does the lead get pushed into the Interested Follow-up campaign and the
reply sent. Nothing here sends anything.

  python3 draft_followups.py [--max 50] [--refresh] [--reply-id ID]

Reads data/interested-replies/review_queue.json (the interested items) and writes
data/interested-replies/followup_drafts.json.
"""

import argparse
import json
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
SKILLS = HERE.parents[1]
PROJECT_ROOT = HERE.parents[3]
sys.path.insert(0, str(SKILLS / "ai-sdr" / "scripts"))      # anthropic_client

from anthropic_client import (                              # noqa: E402
    AnthropicClient, extract_json, AnthropicError, AnthropicJSONError,
)

KNOWLEDGE_DIR = SKILLS / "ai-sdr" / "knowledge"
PLAYBOOK = SKILLS / "ai-sdr" / "examples" / "reply-handling.md"
REVIEW_QUEUE = PROJECT_ROOT / "data" / "interested-replies" / "review_queue.json"
LI_REVIEW_QUEUE = PROJECT_ROOT / "data" / "interested-replies" / "li_review_queue.json"
DRAFTS = PROJECT_ROOT / "data" / "interested-replies" / "followup_drafts.json"
REPLY_STATE = PROJECT_ROOT / "data" / "interested-replies" / "reply_state.json"


# ---- per-lead console state (mirrors the webui overlay) -----------------------
# Scans rewrite the queue files, so manual reclassifies and dismissals live in
# reply_state.json keyed by lead. The bulk path must honor them: draft leads the
# SDR reclassified as interested, skip leads they dismissed as handled-in-CRM.
def _norm_ts(d):
    return (d or "").replace(" ", "T")[:19]


def lead_key(item):
    if (item.get("channel") or "email") == "linkedin":
        cid = item.get("conversation_id") or item.get("reply_id")
        return f"li:{cid}" if cid else None
    email = (item.get("lead_email") or item.get("from_email") or "").strip().lower()
    return email or None


def apply_overlay(items):
    try:
        leads = json.loads(REPLY_STATE.read_text()).get("leads") or {}
    except (OSError, ValueError):
        leads = {}
    if not isinstance(leads, dict):
        leads = {}
    out = []
    for it in items:
        st = leads.get(lead_key(it) or "") or {}
        if (st.get("reclassified") or {}).get("interested"):
            it["classifier"] = {**(it.get("classifier") or {}), "interested": True}
        dis = st.get("dismissed")
        if dis:
            same = str(it.get("reply_id")) == str(dis.get("reply_id") or "")
            d, last = _norm_ts(it.get("date_received")), _norm_ts(dis.get("last_reply_at"))
            if same or (d and last and d <= last):
                continue   # dismissed — don't spend a draft on a handled lead
        out.append(it)
    return out

# Extra steer for LinkedIn replies — same playbook voice, but a DM, not an email.
LINKEDIN_NOTE = (
    "CHANNEL = LinkedIn DM. Write a LinkedIn direct-message reply, NOT an email: no subject "
    "line, no email signature, no '[attached]' (you can't attach files in a DM — offer to send "
    "the artifact via a link or a quick call instead). Keep it tight — ideally under 600 "
    "characters — warm and conversational. One idea, one low-friction ask.")

DEFINITION = """\
You are an SDR drafting the follow-up reply to an INTERESTED B2B prospect who answered a cold
sequence for EverWorker's SDR AI Worker. Classify the follow-up intent, then draft ONE concise,
human reply that moves the conversation forward.

Rules (from the playbook):
- Deliver the give FIRST — the value earns the meeting. Make any next step effortless.
- Pricing ONLY on a direct pricing ask. Ground every claim in the product knowledge; never invent
  numbers or customers.
- Match the prospect's energy: short, warm, specific, no corporate filler, no em dashes, no hype.
- One idea per reply. End on a single clear, low-friction ask.
- intent is one of: meeting_request | info_request | pricing | objection | referral | positive_later.

Return ONLY this JSON, no prose:
{"intent": "<one of the above>", "draft": "<the reply body, ready to send>",
 "rationale": "<one sentence on the approach>"}"""


def load_context():
    parts = []
    for fname in ("offer.md", "cta-offers.md"):
        p = KNOWLEDGE_DIR / fname
        if p.is_file():
            parts.append(p.read_text())
    if PLAYBOOK.is_file():
        parts.append("# Reply-handling playbook (emulate the shape + voice)\n\n" + PLAYBOOK.read_text())
    return "\n\n---\n\n".join(parts)


def build_system():
    return f"{DEFINITION}\n\n# Product knowledge + playbook\n\n{load_context()}"


def draft_one(client, system, item):
    name = item.get("from_name") or "there"
    hint = (item.get("classifier") or {}).get("intent") or ""
    reply = (item.get("text_body") or "").strip()[:2500]
    channel_note = f"\n\n{LINKEDIN_NOTE}" if item.get("channel") == "linkedin" else ""
    user = (f"Prospect: {name}\nTheir interested reply:\n\"\"\"\n{reply}\n\"\"\"\n\n"
            f"(pre-classified as: {hint}).{channel_note} Draft the follow-up. Return ONLY the JSON.")
    try:
        res = client.complete(system, user, use_web_search=False, max_tokens=700)
        data = extract_json(res["text"])
        return {"intent": str(data.get("intent", hint)), "draft": str(data.get("draft", "")).strip(),
                "rationale": str(data.get("rationale", ""))[:300], "error": None}
    except (AnthropicError, AnthropicJSONError, ValueError) as e:
        return {"intent": hint, "draft": "", "rationale": "", "error": f"{e}"[:200]}


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--max", type=int, default=50)
    ap.add_argument("--refresh", action="store_true", help="re-draft even already-drafted replies")
    ap.add_argument("--reply-id", default=None,
                    help="draft ONLY this reply: forces a re-draft and bypasses the "
                         "interested filter (used for regenerate + manual reclassify)")
    args = ap.parse_args()

    def load_items(path, default_channel):
        if not path.is_file():
            return [], {}
        try:
            data = json.loads(path.read_text())
        except (ValueError, OSError):
            return [], {}
        items = []
        for it in (data.get("items") or []):
            it.setdefault("channel", default_channel)
            items.append(it)
        return items, data

    email_items, queue = load_items(REVIEW_QUEUE, "email")
    li_items, _ = load_items(LI_REVIEW_QUEUE, "linkedin")
    all_items = apply_overlay(email_items + li_items)

    def read_prior():
        if DRAFTS.is_file():
            try:
                return {it["reply_id"]: it for it in json.loads(DRAFTS.read_text()).get("items", [])}
            except (ValueError, OSError):
                pass
        return {}

    if args.reply_id:
        target = [it for it in all_items if str(it.get("reply_id")) == str(args.reply_id)]
        if not target:
            print(f"no queue item {args.reply_id}")
            return 1
        prior = read_prior()
        existing = prior.get(target[0].get("reply_id"))
        if existing and existing.get("status") == "sent":
            print(f"reply {args.reply_id} already has a SENT follow-up — refusing to overwrite "
                  f"its record (the thread + HubSpot sync read the sent body from it)")
            return 1
        prior.pop(target[0].get("reply_id"), None)     # force a fresh draft
        todo = target
    else:
        interested = [it for it in all_items if (it.get("classifier") or {}).get("interested")]
        if not interested:
            # leave followup_drafts.json untouched — it holds SENT records the
            # thread view and the HubSpot our-reply sync still need
            print("no interested replies to draft for.")
            return 0
        prior = read_prior()
        if args.refresh:   # re-draft drafted-but-unsent replies; never touch sent records
            prior = {rid: d for rid, d in prior.items() if d.get("status") == "sent"}
        todo = [it for it in interested if it.get("reply_id") not in prior][: args.max]
    client = AnthropicClient()
    system = build_system()

    drafted = []
    if todo:
        with ThreadPoolExecutor(max_workers=4) as ex:
            futs = {ex.submit(draft_one, client, system, it): it for it in todo}
            for fut in as_completed(futs):
                it = futs[fut]
                d = fut.result()
                drafted.append({
                    "reply_id": it.get("reply_id"), "lead_id": it.get("lead_id"),
                    "channel": it.get("channel", "email"),
                    "sender_email_id": it.get("sender_email_id"),
                    "linkedin_account_id": it.get("linkedin_account_id"),
                    "conversation_id": it.get("conversation_id"),
                    "from_name": it.get("from_name"), "from_email": it.get("from_email"),
                    "subject": it.get("subject"), "campaign_id": it.get("campaign_id"),
                    "original_reply": (it.get("text_body") or "")[:4000],
                    "intent": d["intent"], "draft": d["draft"], "rationale": d["rationale"],
                    "error": d["error"], "status": "drafted", "drafted_at": now_iso(),
                    "agent": "standard",
                })

    items = list(prior.values()) + drafted
    DRAFTS.parent.mkdir(parents=True, exist_ok=True)
    DRAFTS.write_text(json.dumps({"generated_at": now_iso(), "campaign_id": queue.get("campaign_id"),
                                  "items": items}, indent=2, ensure_ascii=False))
    ok = sum(1 for d in drafted if d["draft"] and not d["error"])
    print(f"drafted {ok}/{len(todo)} follow-ups ({len(prior)} kept) -> {DRAFTS}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
