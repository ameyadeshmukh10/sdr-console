# USAGE — SDR Pipeline & Analysis Commands

Quick reference for running everything yourself from the terminal. All commands assume you are in the
project root:

```bash
cd "/Users/ameyadeshmukh/Documents/sdraiworker/reply management"
```

Everything is pure Python standard library — **no installs needed**. Secrets/config live in `.env`.

---

## 0. One-time config (`.env`)

| Variable | What it is | Status |
|---|---|---|
| `EMAILBISON_API_KEY` / `EMAILBISON_BASE_URL` | Bison token + instance (`send.everworker.ai`) | ✅ set |
| `HUBSPOT_ACCESS_TOKEN` / `HUBSPOT_BASE_URL` | HubSpot private-app token (`api.hubapi.com`) | ✅ set |
| `HUBSPOT_LIST_ID` | The HubSpot list to pull | ✅ `2198` |
| `HUBSPOT_LINKEDIN_PROPERTY` | Contact property holding the LinkedIn URL | `hs_linkedin_url` |
| `BISON_CAMPAIGN_SALES_LEADERSHIP/_REVOPS/_PARTNERSHIPS/_SDR_BDR` | Per-persona email campaigns | ✅ `10 / 11 / 12 / 13` |
| `HEYREACH_API_KEY` / `HEYREACH_BASE_URL` | HeyReach (LinkedIn) | ✅ set |
| `HEYREACH_CAMPAIGN_ID` / `HEYREACH_LINKEDIN_ACCOUNT_ID` | LinkedIn campaign + sender | ⬜ blank (LinkedIn deferred) |

**Persona → Bison campaign:** sales-leadership→**10**, revops→**11**, partnerships→**12**, sdr-bdr→**13**.

---

## 1. THE MAIN FLOW — batch outbound (recommended)

This batches your HubSpot contacts (25/batch), generates copy with parallel sub-agents, and enrolls
into Bison. State lives in SQLite (`data/outreach/pipeline.db`), so it's resumable.

### Easiest: the slash command (inside Claude Code)
```
/sdr-batches 2            # process 2 batches (50 contacts), DRY-RUN enroll (no writes)
/sdr-batches 2 enroll     # process 2 batches and LIVE-enroll into Bison
/sdr-batches all enroll   # process every pending batch and live-enroll
```
The slash command runs init, dispatches the `sdr-batch-runner` agents in parallel, then enrolls.

### Manual, step by step (terminal)
```bash
P=.claude/skills/sdr-pipeline/scripts

# (a) Refresh contacts from HubSpot list 2198  ->  data/outreach/contacts.jsonl
python3 $P/hubspot_pull.py

# (b) Load contacts into the batch DB (idempotent); makes 25-contact batches
python3 $P/sdr_batches.py init

# (c) See where things stand
python3 $P/sdr_batches.py status
python3 $P/sdr_batches.py pending-batches            # list pending batch ids

# (d) GENERATION happens via Claude sub-agents — use the slash command for this part,
#     or inspect a batch yourself:
python3 $P/sdr_batches.py get-batch 1                 # the 25 contacts in batch 1
#     (a sub-agent writes data/outreach/generated/<contact_id>.json, then:)
python3 $P/sdr_batches.py ingest 1                    # lint files + mark generated/failed

# (e) Enroll everything marked "generated" into Bison (per-persona campaigns)
python3 $P/sdr_batches.py enroll --dry-run            # preview payloads, no writes
python3 $P/sdr_batches.py enroll                      # LIVE: create leads + attach to 10/11/12/13

# Fix-ups
python3 $P/sdr_batches.py reset-batch 7               # set batch 7 + its contacts back to pending
```

**Contact status:** `pending → generated → enrolled` (or `failed` with the lint reason).
Re-running only picks up unfinished work.

---

## 2. Single-shot / file-based outbound (no DB)

For a one-off or small set without the batch DB.
```bash
P=.claude/skills/sdr-pipeline/scripts

python3 $P/hubspot_pull.py            # pull list -> data/outreach/contacts.jsonl (US/tech ICP only)
# (generate copy per contact via the persona agents -> data/outreach/generated/<id>.json)
python3 $P/enroll.py --dry-run        # preview Bison payloads (per-persona routing)
python3 $P/enroll.py                  # live enroll; idempotent via data/outreach/enroll_state.json
```

**Lint any sequence markdown** (the guardrail check):
```bash
python3 .claude/skills/ai-sdr/scripts/lint_sequence.py .claude/skills/ai-sdr/examples/icp-email-sequence.md
```
Checks: 70–110 words, paragraph breaks, no sign-off, no em dashes, value-anchored **meeting** CTA,
step-4 breakup, a metric, no pricing, no undeliverable gives.

**Classify a job title** (ICP gate + persona routing):
```bash
echo "VP of Sales" | python3 .claude/skills/ai-sdr/scripts/buyer_group.py
```

---

## 3. Email Bison — pull data

```bash
B=.claude/skills/email-bison/scripts

python3 $B/fetch_interested_replies.py   # all "Interested" replies -> data/interested-replies/
python3 $B/fetch_campaign_stats.py       # campaign + per-step stats -> data/campaign-stats/
```

---

## 4. Analysis (reads the pulled data)

```bash
T=.claude/skills/interested-trends/scripts

python3 $T/analyze_interested.py     # descriptive features -> analysis/ (summary.json + CSVs)
python3 $T/analyze_conversion.py     # TRUE conversion rates by campaign/offer/geo/step
python3 $T/analyze_cohorts.py        # cohort (Marketing/Sales/CEO-Founder/Other) + qualitative prep
python3 $T/analyze_icp_cta.py        # ICP buyer-group filter + value-first-vs-time-ask CTA baseline
python3 $T/cohort_deepdive.py Sales  # verbatim evidence book for one cohort (Sales|Marketing|"CEO/Founder"|Other)
```
Outputs land in `data/interested-replies/analysis/` (reports: `trends-report.md`,
`conversion-report.md`, `cohort-playbook.md`, `sales-cohort-deepdive.md`, `icp-cta-report.md`).

---

## Where things live

```
skills/                         <- visible shortcut into .claude/skills (browse the code/knowledge)
data/
├─ interested-replies/          <- pulled replies + analysis/ reports
├─ campaign-stats/              <- campaign + step stats
└─ outreach/
   ├─ contacts.jsonl            <- pulled ICP contacts
   ├─ generated/<id>.json       <- generated copy per contact
   ├─ pipeline.db               <- SQLite batch state (status per contact/batch)
   └─ enroll_state.json         <- file-flow idempotency
.env                            <- keys + config
```
The product knowledge the agents write from: `skills/ai-sdr/knowledge/` (`offer.md`, `cta-offers.md`,
`icp-email.md`).

---

## Troubleshooting

- **`pending-batches` is empty but you expected batches** — run `sdr_batches.py init` (loads new
  contacts into batches). Re-run `hubspot_pull.py` first if the list changed.
- **`enroll` says `no_campaign`** — the per-persona `BISON_CAMPAIGN_*` ids aren't set in `.env`.
- **A batch had failures** — `status` shows `failed` counts; the per-contact `error` (lint reason) is
  in the DB. Re-generate those, then `ingest <id>` again, or `reset-batch <id>` to redo the whole batch.
- **New `/sdr-batches` command or `sdr-batch-runner` agent not found** — reload the Claude Code session.
- **Blank line before the email signature** — set this in Bison (prepend a blank line to the sender
  signature); Bison strips trailing whitespace from the lead body, so it can't come from the copy.
- **Bison/HubSpot/HeyReach 401** — token/instance mismatch; check the matching `*_API_KEY` and
  `*_BASE_URL` in `.env`.
- **Always dry-run before a live enroll** (`enroll --dry-run`) to eyeball routing + payloads.
```
