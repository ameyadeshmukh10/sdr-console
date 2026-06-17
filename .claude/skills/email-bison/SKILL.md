---
name: email-bison
description: Integrate with the Email Bison sales-sequencing API to extract and analyze master-inbox replies. Use when the user wants to pull interested replies, conversation threads, leads, campaigns, or other Email Bison data, or extend this skill with new Email Bison use cases.
---

# Email Bison

Tools for working with an Email Bison instance (a sales email sequencing tool)
through its REST API. The full API spec is in `openapi.json` at the project root.

## API basics

- **Base URL:** `https://send.growthtoday.co` (override with `EMAILBISON_BASE_URL`)
- **Auth:** `Authorization: Bearer <EMAILBISON_API_KEY>`
- **Key location:** `.env` at the project root (`EMAILBISON_API_KEY=...`). The key
  is read by `scripts/bison_client.py`; never hardcode it in scripts.
- **Pagination:** Laravel style — responses have `data`, `links.next`, `meta`.
  `bison_client.get_paginated()` handles this for you.

Key endpoints (see `openapi.json` for the rest):
- `GET /api/replies` — master inbox. Filters: `status=interested`, `tag_ids[]`, `folder`, `campaign_id`.
- `GET /api/replies/{id}/conversation-thread` — full thread (`older_messages`, `current_reply`, `newer_messages`).
- `GET /api/leads/{id}` — lead profile (name, company, title, notes, custom_variables).
- `GET /api/tags` — workspace tags (the `Interested` tag is a default tag).
- `GET /api/campaigns/{id}` — campaign details.

## Use case 1: extract interested replies

Pulls every reply marked **Interested** — matching **either** the built-in
`interested` status **or** the `Interested` tag (de-duped) — and captures the full
thread plus the lead profile, for later trend analysis of what generates interest.

Run:
```bash
python3 .claude/skills/email-bison/scripts/fetch_interested_replies.py
```
(no dependencies — pure Python stdlib)

Outputs to `data/interested-replies/`:
- `dataset.jsonl` — one JSON record per interested thread (canonical, for analysis)
- `threads/<reply_id>.md` — human-readable thread (outbound + inbound, labeled)
- `last_run.json` — run timestamp, match counts, and any per-reply errors

### `dataset.jsonl` record shape
```json
{
  "reply_id": 239,
  "reply_uuid": "…",
  "interested_via": ["status", "tag"],
  "campaign_id": 12,
  "campaign_name": "Q2 Outbound",
  "sender_email_id": 25065,
  "subject": "…",
  "date_received": "…",
  "lead": {
    "id": 326217, "first_name": "…", "last_name": "…", "email": "…",
    "title": "…", "company": "…", "notes": "…", "status": "…",
    "custom_variables": [{"name": "…", "value": "…"}]
  },
  "first_outbound_text": "the email we sent that prompted the reply",
  "interested_reply_text": "the lead's reply that got marked interested",
  "thread": [
    {"direction": "outbound", "from_name": "…", "from_email": "…",
     "to": ["…"], "subject": "…", "date": "…", "text": "…"}
  ],
  "fetched_at": "…"
}
```
`direction` is `inbound` when a message's from-address matches the lead's email,
otherwise `outbound` (what we sent).

## Use case 3: pull campaign denominators + per-step stats

Supplies the missing denominator for conversion analysis — how many leads each
campaign actually contacted — so interested-reply *rates* (not just counts) can be
computed, plus per-sequence-step stats.

Run:
```bash
python3 .claude/skills/email-bison/scripts/fetch_campaign_stats.py
```

Outputs to `data/campaign-stats/`:
- `campaigns.jsonl` — per campaign: `total_leads_contacted`, `interested`,
  `unique_replies`, `emails_sent`, plus computed `interested_rate_pct` / `reply_rate_pct`
- `step_stats.jsonl` — per campaign per sequence step: `leads_contacted`, `interested`,
  `unique_replies` and rates (lets you see which step truly converts best)
- `last_run.json` — run metadata + any per-campaign errors

Notes:
- Uses `GET /api/campaigns` (aggregate counts) and `POST /api/campaigns/{id}/stats`
  (per-step; **requires `start_date`/`end_date`** — the script passes a wide window).
- Bison's `interested` field counts the built-in **interested status**, not custom tags,
  so workspace totals (82) are smaller than the status-OR-tag set (171) from use case 1.
- The `interested-trends` skill's `analyze_conversion.py` consumes these files.

## Extending this skill

Each new Email Bison use case = a new script under `scripts/` that reuses
`bison_client.py`:

```python
from bison_client import BisonClient
client = BisonClient()                       # reads .env automatically
for reply in client.list_replies(folder="all"):
    ...
```

Add new endpoint wrappers to `BisonClient` rather than calling `urllib` directly,
so every use case shares auth, retries, and pagination.
