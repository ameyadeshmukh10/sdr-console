---
name: sdr-pipeline
description: Autonomous outbound pipeline — pull contacts from a HubSpot list, route each to a job-title persona agent that researches and writes value-first copy, then enroll into an Email Bison email campaign (subject1-4/body1-4 custom variables) and a HeyReach LinkedIn campaign. Sells EverWorker's SDR AI Worker to US B2B-tech GTM leadership. Use to run or operate this pipeline.
---

# SDR Pipeline — HubSpot → per-title agents → Email Bison + HeyReach

Operationalizes the `ai-sdr` engine end to end. Pull a HubSpot list → route each ICP contact to a
**persona subagent** → it researches the account and writes value-first email + LinkedIn copy →
**auto-enroll** into one Email Bison campaign and one HeyReach campaign. Product: **SDR AI Worker
only**. Audience: **US B2B software/tech GTM leadership** (`buyer_group.py` enforces this).

Bison instance: **`send.everworker.ai`** (token in `.env`).

## One-time setup
1. **`.env`:** set `HUBSPOT_LIST_ID` (the list to pull). The per-persona Bison campaigns are
   pre-filled (`BISON_CAMPAIGN_SALES_LEADERSHIP=10`, `…_REVOPS=11`, `…_PARTNERSHIPS=12`,
   `…_SDR_BDR=13`). `HUBSPOT_LINKEDIN_PROPERTY` defaults to `hs_linkedin_url`.
2. **Email Bison routing is per-persona:** sales-leadership→10, revops→11, partnerships→12,
   sdr-bdr→13. Each campaign must have 4 sequence steps whose subject/body reference the custom
   variables `{{subject1}}`/`{{body1}}` … `{{subject4}}`/`{{body4}}`. (Campaigns are currently
   `draft` — leads can be added; activate to start sending.)
3. **LinkedIn (HeyReach) is DEFERRED.** `HEYREACH_CAMPAIGN_ID` / `…_ACCOUNT_ID` are blank, so
   `enroll.py` skips LinkedIn. To enable later: set those, and the HeyReach campaign's message steps
   must reference custom fields `{{li_connect}}`, `{{li_msg1}}`, `{{li_msg2}}`.

## Run (autonomous)
1. **Pull + segment:**
   `python3 .claude/skills/sdr-pipeline/scripts/hubspot_pull.py` → `data/outreach/contacts.jsonl`
   (US/tech ICP only; each row has `persona` + `linkedin_url`; non-ICP dropped). Prints per-persona counts.
2. **Generate copy per contact:** for each row in `contacts.jsonl`, invoke the subagent named by its
   `persona` (Task tool) with the contact fields. Save the agent's JSON to
   `data/outreach/generated/<contact_id>.json`. Persona → agent:
   - `sales-leadership` → **sdr-sales-leadership**
   - `revops` → **sdr-revops**
   - `partnerships` → **sdr-partnerships**
   - `sdr-bdr` → **sdr-sdr-bdr-leadership**
   (For large lists, run this fan-out as a `Workflow` to pipeline contacts concurrently.)
3. **Enroll:** `python3 .claude/skills/sdr-pipeline/scripts/enroll.py`
   - **Dry-run first:** add `--dry-run` to print the exact Bison + HeyReach payloads without sending.
   - Bison: `create_lead` with `custom_variables` `subject1-4`/`body1-4` → `attach_leads_to_campaign(BISON_CAMPAIGN_ID)`.
   - HeyReach: `AddLeadsToCampaignV2(HEYREACH_CAMPAIGN_ID)` with `customUserFields {li_connect,li_msg1,li_msg2}`
     (skipped for contacts without a LinkedIn URL — email still sends).
   - **Idempotent** via `data/outreach/enroll_state.json`; re-runs skip already-enrolled contacts.
   - Refuses to enroll email copy that fails the guardrail linter (`--no-lint` to override).

## Generated-asset schema (`data/outreach/generated/<contact_id>.json`)
```json
{
  "contact_id": "12345", "persona": "sales-leadership", "signal": "raised $12M Series A",
  "email": {"subject1":"…","body1":"…","subject2":"…","body2":"…",
            "subject3":"…","body3":"…","subject4":"…","body4":"…"},
  "linkedin": {"li_connect":"…","li_msg1":"…","li_msg2":"…"}
}
```

## Guardrails (enforced)
- Only ICP GTM-leadership titles are written/enrolled (`persona_for_title` in `buyer_group.py`).
- Email copy must pass `lint_sequence.py` (70–100w, value-first CTA, breakup step 4, a metric, no
  pricing in cold steps) — `enroll.py` blocks failures.
- All product claims trace to `ai-sdr/knowledge/offer.md`; no fabricated signals/stats.

## Batch mode (scale, parallel, low-token) — `/sdr-batches`
For the full list, use the SQLite-backed batch system instead of hand-dispatching:
- **Slash command:** `/sdr-batches [N|all] [enroll]` — batches contacts (25/batch), dispatches
  `sdr-batch-runner` sub-agents in parallel to generate copy, then dry-runs (or live-runs) enrollment.
  The recipe is encoded in `.claude/commands/sdr-batches.md`, so invoking it costs almost no thinking.
- **Terminal CLI** (`scripts/sdr_batches.py`, state in `data/outreach/pipeline.db`):
  `init` (contacts.jsonl → batches of 25) · `status` · `pending-batches` · `get-batch <id>` ·
  `ingest <id>` (lint + mark) · `enroll [--dry-run]` (per-persona campaigns) · `reset-batch <id>`.
- **`sdr-batch-runner`** agent processes one batch of 25 end to end (research → write → save →
  `ingest`). Statuses per contact: pending → generated → enrolled (or failed, with the lint reason).
- New sub-agents/commands may need a Claude Code session reload before first use.

## Components
- Clients: `scripts/hubspot_client.py`, `scripts/heyreach_client.py`,
  `email-bison/scripts/bison_client.py` (`create_lead`, `attach_leads_to_campaign`).
- Router: `ai-sdr/scripts/buyer_group.py` (`persona_for_title`).
- Agents: `.claude/agents/sdr-*.md` (one per persona).

## Notes / current state
- **Bison instance:** `send.everworker.ai` with token `2|omRS…` (in `.env`) — verified. This replaced
  the old `send.growthtoday.co` instance everywhere (the read/extraction skills now point here too).
- HubSpot base is `https://api.hubapi.com` (EU `pat-eu1` tokens still use this host).
- LinkedIn (HeyReach) is deferred — email-only for now (per-persona Bison campaigns 10/11/12/13).
- Marketing-pipeline titles are ICP but have **no persona agent yet** → skipped & logged.
