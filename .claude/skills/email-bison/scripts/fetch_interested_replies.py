"""Use case 1: extract every "Interested" reply from the Email Bison master inbox.

Matches replies that are EITHER marked with the built-in `interested` status OR
carry the default `Interested` tag, de-dupes them, then for each one pulls the
full conversation thread (what we sent + what the lead replied) and the lead's
profile. Output is written for later trend analysis:

    data/interested-replies/dataset.jsonl   one JSON record per thread
    data/interested-replies/threads/<id>.md  human-readable thread
    data/interested-replies/last_run.json    run metadata

Run:  python3 .claude/skills/email-bison/scripts/fetch_interested_replies.py
"""

import html
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

from bison_client import BisonClient, BisonError

# Output lives at the project root (4 levels up from this script:
# scripts/ -> email-bison/ -> skills/ -> .claude/ -> project root).
PROJECT_ROOT = Path(__file__).resolve().parents[4]
OUT_DIR = PROJECT_ROOT / "data" / "interested-replies"
THREADS_DIR = OUT_DIR / "threads"

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"[ \t]*\n[ \t]*")


def html_to_text(value):
    if not value:
        return ""
    text = _TAG_RE.sub("\n", value)
    text = html.unescape(text)
    text = _WS_RE.sub("\n", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def message_text(msg):
    if msg.get("text_body"):
        return msg["text_body"].strip()
    return html_to_text(msg.get("html_body"))


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def collect_interested(client):
    """Return {reply_id: {"reply": <reply obj>, "via": set()}} unioned."""
    found = {}

    def add(reply, via):
        rid = reply.get("id")
        if rid is None:
            return
        entry = found.setdefault(rid, {"reply": reply, "via": set()})
        entry["via"].add(via)
        # Prefer the richest copy of the reply object we have seen.
        if len(reply) > len(entry["reply"]):
            entry["reply"] = reply

    via_status = 0
    for reply in client.list_replies(status="interested", folder="all"):
        add(reply, "status")
        via_status += 1

    interested_tag = next(
        (t for t in client.list_tags()
         if (t.get("name") or "").strip().lower() == "interested"),
        None,
    )
    via_tag = 0
    if interested_tag:
        for reply in client.list_replies(
            folder="all", **{"tag_ids[]": interested_tag["id"]}
        ):
            add(reply, "tag")
            via_tag += 1

    return found, via_status, via_tag, interested_tag


def lead_id_from(reply, thread):
    if reply.get("lead_id"):
        return reply["lead_id"]
    for bucket in ("older_messages", "newer_messages"):
        for msg in thread.get(bucket, []) or []:
            if msg.get("lead_id"):
                return msg["lead_id"]
    current = thread.get("current_reply") or {}
    return current.get("lead_id")


def ordered_messages(thread):
    older = thread.get("older_messages") or []
    current = thread.get("current_reply")
    newer = thread.get("newer_messages") or []
    msgs = list(older)
    if current:
        msgs.append(current)
    msgs.extend(newer)
    return msgs


def normalize_message(msg, lead_email):
    from_email = (msg.get("from_email_address") or "").strip().lower()
    direction = "inbound" if lead_email and from_email == lead_email else "outbound"
    return {
        "direction": direction,
        "from_name": msg.get("from_name"),
        "from_email": msg.get("from_email_address"),
        "to": [r.get("address") for r in (msg.get("to") or []) if r.get("address")],
        "subject": msg.get("subject"),
        "date": msg.get("date_received"),
        "text": message_text(msg),
    }


def build_record(client, reply, via, fetched_at):
    reply_id = reply["id"]
    thread = client.get_conversation_thread(reply_id)

    lid = lead_id_from(reply, thread)
    lead = {}
    if lid:
        try:
            lead = client.get_lead(lid)
        except BisonError as e:
            print(f"  ! lead {lid} fetch failed: {e}", file=sys.stderr)
    lead_email = (lead.get("email") or reply.get("from_email_address") or "").strip().lower()

    messages = [normalize_message(m, lead_email) for m in ordered_messages(thread)]

    first_outbound = next((m["text"] for m in messages if m["direction"] == "outbound"), "")
    interested_reply_text = message_text(reply) or next(
        (m["text"] for m in reversed(messages) if m["direction"] == "inbound"), ""
    )

    campaign_id = reply.get("campaign_id")
    campaign_name = None
    if campaign_id:
        campaign_name = (client.get_campaign(campaign_id) or {}).get("name")

    lead_record = {
        "id": lead.get("id") or lid,
        "first_name": lead.get("first_name"),
        "last_name": lead.get("last_name"),
        "email": lead.get("email") or reply.get("from_email_address"),
        "title": lead.get("title"),
        "company": lead.get("company"),
        "notes": lead.get("notes"),
        "status": lead.get("status"),
        "custom_variables": lead.get("custom_variables") or [],
    }

    return {
        "reply_id": reply_id,
        "reply_uuid": reply.get("uuid"),
        "interested_via": sorted(via),
        "campaign_id": campaign_id,
        "campaign_name": campaign_name,
        "sender_email_id": reply.get("sender_email_id"),
        "subject": reply.get("subject"),
        "date_received": reply.get("date_received"),
        "lead": lead_record,
        "first_outbound_text": first_outbound,
        "interested_reply_text": interested_reply_text,
        "thread": messages,
        "fetched_at": fetched_at,
    }


def render_markdown(rec):
    lead = rec["lead"]
    name = " ".join(p for p in [lead.get("first_name"), lead.get("last_name")] if p) or "(unknown lead)"
    lines = [
        f"# Interested reply — {name}",
        "",
        f"- **Lead:** {name}",
        f"- **Company:** {lead.get('company') or '—'}",
        f"- **Title:** {lead.get('title') or '—'}",
        f"- **Email:** {lead.get('email') or '—'}",
        f"- **Lead status:** {lead.get('status') or '—'}",
        f"- **Interested via:** {', '.join(rec['interested_via'])}",
        f"- **Campaign:** {rec.get('campaign_name') or rec.get('campaign_id') or '—'}",
        f"- **Subject:** {rec.get('subject') or '—'}",
        f"- **Reply received:** {rec.get('date_received') or '—'}",
    ]
    if lead.get("notes"):
        lines.append(f"- **Notes:** {lead['notes']}")
    if lead.get("custom_variables"):
        cvs = ", ".join(
            f"{c.get('name')}={c.get('value')}" for c in lead["custom_variables"]
        )
        lines.append(f"- **Custom fields:** {cvs}")
    lines += ["", "---", "", "## Thread (chronological)", ""]

    for i, m in enumerate(rec["thread"], 1):
        label = "OUTBOUND (us)" if m["direction"] == "outbound" else "INBOUND (lead)"
        sender = m.get("from_name") or m.get("from_email") or "?"
        lines += [
            f"### {i}. {label} — {sender}",
            f"*{m.get('date') or ''}* · subject: {m.get('subject') or '—'}",
            "",
            m.get("text") or "(no text body)",
            "",
        ]
    return "\n".join(lines)


def main():
    try:
        client = BisonClient()
    except BisonError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1

    fetched_at = now_iso()
    print("Collecting interested replies (status + tag)...")
    try:
        found, via_status, via_tag, interested_tag = collect_interested(client)
    except BisonError as e:
        print(f"ERROR while listing replies: {e}", file=sys.stderr)
        return 1

    tag_note = f"tag '{interested_tag['name']}' (id {interested_tag['id']})" if interested_tag else "no Interested tag found"
    print(f"  status=interested matches: {via_status}")
    print(f"  {tag_note} matches: {via_tag}")
    print(f"  unique interested replies: {len(found)}")

    THREADS_DIR.mkdir(parents=True, exist_ok=True)
    records = []
    errors = []
    for rid, entry in sorted(found.items()):
        try:
            rec = build_record(client, entry["reply"], entry["via"], fetched_at)
        except BisonError as e:
            errors.append({"reply_id": rid, "error": str(e)})
            print(f"  ! reply {rid} failed: {e}", file=sys.stderr)
            continue
        records.append(rec)
        (THREADS_DIR / f"{rid}.md").write_text(render_markdown(rec))

    dataset_path = OUT_DIR / "dataset.jsonl"
    with dataset_path.open("w") as f:
        for rec in records:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    last_run = {
        "fetched_at": fetched_at,
        "total_interested": len(records),
        "via_status": via_status,
        "via_tag": via_tag,
        "interested_tag": interested_tag,
        "errors": errors,
    }
    (OUT_DIR / "last_run.json").write_text(json.dumps(last_run, indent=2))

    print(f"\nWrote {len(records)} records to {dataset_path}")
    print(f"Per-thread markdown in {THREADS_DIR}")
    if errors:
        print(f"{len(errors)} reply(ies) errored — see last_run.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
