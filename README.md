# SDR Console

An autonomous outbound sales pipeline and web console for an **SDR AI Worker**, deployed
as a single service on **Railway**. It sources and researches ICP contacts from HubSpot,
generates persona-targeted email + LinkedIn outreach with Claude agents, enrolls into
Email Bison (email) and HeyReach (LinkedIn), triages and answers replies, logs every
touch back to HubSpot, enforces do-not-contact suppression, and attributes created deals
and pipeline dollars back to the AI SDR — end to end, with humans gating only the
outward writes.

Repo: <https://github.com/ameyadeshmukh10/sdr-console> 

## What it does, end to end

1. **Source** — pull an ICP contact list from HubSpot (searchable by list name), or grow
   the list from a *company* list: a Clay MCP enrichment flow sources GTM-leadership
   contacts per company, dedups against HubSpot, and creates them straight into the
   pipeline — no manual list building.
2. **Research** — every account gets a researched recent signal (web search), a
   **technographic scan** (which CRM / ad pixels / martech / salestech the company runs,
   via deterministic DNS + website fingerprinting against a 7.5k-vendor catalogue), and a
   **hiring scan** (open roles, with a sales/GTM-role classifier). All three are cached
   per company domain for 90 days so a company is researched once, not once per contact.
3. **Generate** — each contact is routed by job title to one of **4 persona agents**
   (sales leadership, RevOps, partnerships, SDR/BDR leadership) that write a value-first
   4-touch email sequence + LinkedIn copy. Signals steer the copy: a hiring signal opens
   email 2; detected sequencing tools steer a no-disruption angle; intent/ABM tools steer
   a signal-activation story in email 3. Every sequence passes a deterministic linter
   (word counts, meeting CTA, metric present, breakup touch, no pricing…) before it
   counts as generated.
4. **Enroll** — generated contacts are enrolled into per-persona Email Bison campaigns
   and a HeyReach LinkedIn campaign, always through a dry-run preview → confirm gate.
5. **Manage replies** — a unified inbox classifies inbound email + LinkedIn replies with
   Claude, drafts channel-aware follow-ups for interested leads (approve to send), and
   can build a **personalized signal-play web page** for a lead's company and embed the
   live link in the reply.
6. **Log + attribute** — background loops mirror all activity into HubSpot (emails,
   LinkedIn touches, signal notes, technographic + hiring company properties) and a
   nightly job computes **deal attribution**: which HubSpot deals the AI SDR created and
   what they're worth, snapshotted into MongoDB and surfaced as Analytics tiles.
7. **Suppress** — contacts RevOps flags as do-not-contact (booked a meeting, became an
   opportunity) are dropped at pull time, blocked at enroll time, and actively
   **unenrolled from in-flight Bison/HeyReach sequences** by a recurring sweep with a
   durable idempotent ledger.

## The console

React SPA (React 18 + Vite + Recharts) served by the same process as the API. Every view
sits behind a login gate (signed bearer-token sessions; every `/api/*` route is
auth-gated). Eight views:

| View | What it does |
|------|--------------|
| **Use** | Search HubSpot lists by name (contact or company), ingest a contact list into the batch pipeline, or run the Clay buying-group enrichment on a company list (end-to-end or review-then-commit). |
| **Pipeline** | Live batch progress, one-click copy generation (real-time, or via Anthropic's Message Batches API at **50% cost**), A/B instruction-set variants with an exact percentage split, and enrollment with a dry-run gate + confirm modal. |
| **Orchestration** | Live diagram of HubSpot → persona agents → campaigns with contact counts, plus the suppression safety gate: unenrollment status, run-now / dry-run controls. |
| **Analytics** | Campaign leads / contacted / reply / interested rates, and the AI SDR **deal-attribution tiles** (deals created, total pipeline $) with an on-demand sync. |
| **Trends** | What's working across interested replies — seniority, function, winning CTA, offer type, conversion by campaign, reply cohorts. |
| **Replies** | Two-pane triage inbox across email + LinkedIn: scan-classify with Claude, tag interested in Bison (gated), draft → approve → send in-thread, dismiss/reclassify with persistent state, and the Signal Playbook reply agent. |
| **Signals** | The per-company research cache: signal, technographic line, hiring line, age; per-row and bulk re-detect for tech + hiring. |
| **Outreach** | Search/filter/group all generated sequences (2,000+ in the bundled snapshot) by persona, CTA play, signal, status; click through to the full 4-touch copy. |

## Autonomous operations

The deployed process runs four background loops — no cron, no worker fleet:

- **Activity autosync** (hourly) — logs new outbound/inbound email + LinkedIn activity to
  HubSpot contacts, with a dedup ledger, an audit CLI for duplicate forensics, and a
  reconcile mode that adopts pre-existing engagements instead of re-creating them.
- **Deal attribution** (nightly, midnight ET, DST-safe) — pulls every AI SDR email
  engagement from HubSpot, joins email → contact → deals, snapshots into MongoDB, and
  flags attributed deals/contacts in HubSpot. Incremental via a watermark (idempotent
  upserts); deal associations are re-swept every run so late-created deals still get
  attributed. A full seed over ~19k engagements takes ~15 minutes; nightly increments
  take seconds.
- **HeyReach webhook drain** — near-real-time LinkedIn activity logging.
- **Unenrollment sweeps** (every 30 min) — enforces the do-not-contact flag across both
  channels: stops queued Bison emails and live HeyReach sequences, writes a HubSpot
  timeline note, and records everything in an idempotent ledger. Built for scale: a
  ~23k-contact bulk flag is drained newest-first with pacing, per-channel circuit
  breakers, and retry rotation, so freshly flagged mid-sequence contacts are stopped
  first.

Everything that writes outward is either human-gated in the UI (enroll, tag, send) or
best-effort with full logging (HubSpot property write-backs never fail a pipeline run).

## Architecture

Deliberately boring where it can be, sophisticated where it counts:

- **One deployed process.** `webui/server/app.py` (Python 3.12, `ThreadingHTTPServer`)
  serves both the JSON API (~70 routes) and the built SPA. The backend is **Python
  stdlib only** except `pymongo` (attribution store) and `dnspython` (technographic
  DNS probes) — both imported lazily, so the server boots and degrades gracefully
  without them, without `MONGO_URL`, and without any API key configured.
- **Write/read separation.** The web server opens SQLite **read-only**; all writes go
  through the pipeline scripts in `.claude/skills/*/scripts/`, which the server shells
  out to (each prints progress lines + a final JSON summary). Read endpoints degrade
  gracefully rather than 500.
- **Claude integration without an SDK.** Copy generation, reply classification, and the
  Clay MCP enrichment all call the Anthropic API over stdlib `urllib` — including the
  Message Batches API (async, half price), forced JSON schemas, prompt caching, and the
  server-side web-search tool.
- **Cost engineering.** Web-search tokens dominate generation cost, so: same-company
  contacts are batched together, a 90-day per-domain signal cache turns re-runs into
  write-only calls, and bulk generation rides the Batches API at 50% off.

### Data stores

| Store | What |
|---|---|
| SQLite (`data/outreach/pipeline.db`) | Contact/batch state machine, per-account signals cache (research + tech + hiring), activity-log ledger, enrollment lead map, unenrollment ledger. |
| JSONL under `data/` | Generated sequences, campaign stats, interested-reply threads + analysis. |
| MongoDB (db `aisdr`) | Deal-attribution snapshots: emails, contacts, deals, sync watermark. |

## Deployment (Railway)

One Docker service + a MongoDB service:

- **Multi-stage image**: Node 20 builds the Vite SPA; a `python:3.12-slim` runtime serves
  it (Node is kept in the runtime only for the Signal Playbook deck renderer). Deploys on
  every push to `main`, with an `/api/health` healthcheck and on-failure restarts
  (`railway.json`).
- **Railway Volume at `/app/data`** is the live data dir. The committed `data/` snapshot
  is only a first-boot seed (the entrypoint copies it in when the volume is empty) —
  production data on the volume is far ahead of the repo. The app verifies durability at
  boot and the UI shows a warning banner if the data dir looks ephemeral.
- **MongoDB** is wired via a Railway reference variable (`MONGO_URL`) over private
  networking. Unset it and the attribution endpoints report `configured: false` and the
  nightly loop self-disables — the console keeps working.
- Config lives in Railway service variables (locally: `.env`); `.env.example` documents
  every variable.

## Layout

| Path | What |
|------|------|
| `.claude/skills/` | The pipeline logic: persona sub-agents, copy linter, HubSpot/Bison/HeyReach clients, signal engines (tech + hiring), attribution sync, suppression sweeper, orchestrator + batch runner. |
| `webui/` | The console (React frontend, stdlib Python backend). See [`webui/README.md`](webui/README.md). |
| `technographics/` | Vendored deterministic technographic-detection engine (DNS + HTML fingerprinting, Wappalyzer-derived catalogue). |
| `deck-renderer/` | Vite single-file HTML renderer for Signal Playbook play pages. |
| `data/` | A real pipeline snapshot: ICP contacts, 2,000+ generated sequences, reply threads + analysis, campaign stats, the SQLite DB. First-boot seed in prod. |
| `USAGE.md` | CLI runbook for every pipeline script. |
| `CLAUDE.md` | Architecture + operational context for Claude sessions: deployment topology, data semantics, gotchas. |
| `openapi.json` | Email Bison API reference. |

## Quick start

```bash
git clone https://github.com/ameyadeshmukh10/sdr-console.git
cd sdr-console
cp .env.example .env          # fill in your HubSpot / Bison / HeyReach / Anthropic keys
./webui/run.sh                # build + serve the console at http://localhost:8787
#   or: ./webui/run.sh dev    # hot-reload dev (UI :5173, API :8787)
```

The console opens against the bundled `data/` snapshot, so every page has real content on
first run — no pipeline run required to explore it.

## Running the pipeline yourself

With a valid `.env`, generate and enroll a fresh batch from Claude Code:

```bash
/sdr-batches <N> enroll        # process N pending batches and enroll into Bison
```

Or entirely from the console: **Use** → ingest a list → **Pipeline** → Generate copy (or
Batch API) → dry-run → enroll. See [`USAGE.md`](USAGE.md) for the underlying scripts.

## Secrets

`.env` (live API keys) and the Clay OAuth token file are the only things gitignored —
copy `.env.example` and fill it in (including `AUTH_SECRET_KEY`, which signs console
login sessions). Everything else, including the `data/` snapshot, is in the repo.

## UI 

Enroll contact or company lists from hubspot

<img width="3420" height="2240" alt="AISDR Use" src="https://github.com/user-attachments/assets/70495aba-becb-4938-80bf-3eb2b6ac19ae" />

Send batches to API for sequence generation. Use overnight batch processing to reduce token costs by 50% or immediate processing when speed to lead is essential.
The system uses anthropic prompt caching, intelligent batch grouping (by account), and stateful time bound database storage so it is very token efficient. You can sequence 15,000 people a month
and spend only $250 to $500 in Anthropic API consumption even while using Opus 4.8. 

<img width="3420" height="2154" alt="AISDR Enrollment Pipeline" src="https://github.com/user-attachments/assets/ccfc3751-7956-4be2-a6a7-6de6702c62bd" />

Review the generated sequence and signal for any contact prior to sending it out for activation. 

<img width="3420" height="3032" alt="AI SDR Sequence" src="https://github.com/user-attachments/assets/53b7d6ad-c5f5-48fe-9a0a-809427b174b6" />

Replies are automatically identified, classifed, and surfaced in a centralized inbox. (The AI SDR uses hundreds of email accounts and dozens of LinkedIn accounts to reach out.) 
The reply is classified as interested, follow-up, possible interested, or low confidence. Automated and not interested replies are not surfaced to the user. 

<img width="3420" height="2182" alt="AI SDR Reply Triage" src="https://github.com/user-attachments/assets/359d1ab1-4ac4-4f7b-af64-f18b5992de9a" />

Two reply agents are used a standard reply agent or a ABX reply agent. The agent is selected by the user and it drafts the resposne which is then human editable and sendable. The reply automatically comes from the linkedin account or email account the prospect is communicating with. Everything is also automatically logged to the CRM as email or linkedin activity on the contact record attributed to the AI SDR with a single alias for all of its accounts. 

<img width="3420" height="2182" alt="AISDR Reply Classification" src="https://github.com/user-attachments/assets/e8508799-1515-494f-888f-3b2c6a38916a" />

The ABX reply agent orchestrates a multi-agent system (MAS) which invokes subagents that research the prospect and company to determine ICP, buying group, positioning / messaging, and persona messaging. It then calls another agent to configure a temporary version of the AI SDR tuned to the client's company entirely, then runs the AI SDR automatically to find a real relevant company and buyer group contacts  that fits the prospects ICP and positioning showing real signal, then it creates sequencing, and then it calls a design agent to create a customized deck, and publishes it directly to hubspot as an interactive web experience with a dedicated URL, then it writes the reply email and places the link in the text box. This all happens completely autonomously. 

<img width="3420" height="2182" alt="AI SDR ABX Agent" src="https://github.com/user-attachments/assets/306c9a98-8897-4e19-bb3b-a8d5257291ec" />

The ABX Agent creates an interactive personalized web experience as shown below. This works very well to generate meetings. I've customized this for other companies as well so you can easily extend this to have multiple ABX experiences of any kind you can think of. 

<img width="1708" height="985" alt="abx-1:4" src="https://github.com/user-attachments/assets/ef8ed7f1-bf68-4af4-a194-27b7cee0b1f1" />

<img width="856" height="481" alt="abx 2:4" src="https://github.com/user-attachments/assets/43aae00e-03b3-49bb-a3ba-2b5aa2183204" />

<img width="1707" height="962" alt="abx 3:4" src="https://github.com/user-attachments/assets/57aedeb4-1864-44a0-b152-0a8fd18a25b4" />

<img width="1698" height="955" alt="abx 4:4" src="https://github.com/user-attachments/assets/1189bde9-4c5f-4e01-a158-a8cf1df1712a" />

The AI SDR Analytics view attributes deals to the AI SDR using time bound last touch attribution. (If the AI SDR communicated with the prospect within 7 days of a deal opening it attributes the deal as AI SDR influenced.) The AI SDR runs email and linkedin against lists in weekly sprints the same lists are provided to human SDRs who dial in parallel. Human SDRs monitor the reply inbox and use the reply agents to convert and manage interested replies to generate deals. 

I've used this version of the AI SDR to generate 42 deals and $1.3M in pipeline all 100% cold outbound signal based pipeline with 2 SDRs from June 28th 2026 to September 1st 2026. This system works very very well. It took me about 1.5-3 months to build and optimize this version. 

<img width="1416" height="989" alt="AISDR Analytics" src="https://github.com/user-attachments/assets/5d5c1587-186b-46a2-83e8-f4ebb1a66d4a" />

The AI SDR Signals View is a stateful database of signals found on account records. These may be used to power ABM segmentation and ad campaigns when I extend this in later releases into an ABM Advertising console. The signals are all stored in MongoDB as JSON and Markdown. 

<img width="1704" height="983" alt="AISDR Signals DB" src="https://github.com/user-attachments/assets/35e6e670-23ab-493d-8907-93707b8fd9a7" />

<img width="1705" height="983" alt="AI SDR Signals Example" src="https://github.com/user-attachments/assets/98472d9b-939d-422e-83ce-36213c31a436" />

<img width="1706" height="983" alt="AISDR Signals Example 2" src="https://github.com/user-attachments/assets/e5043deb-dcec-4112-88c8-33dd4bd9ffac" />

The AI SDR Outreach view shows the outreach its done. You can also search by whether the outreach resulted in an interested reply or a deal. This is stored in a MongoDB and SQLLite DB. 
You can use claude code and an agent eval system on a quarterly basis to identify trends in what is and isn't working on your outreach because all sequences are stored with detailed metadata (persona, seniority, company, industry, size, message that was replied to, the angle metadata classifer used in the messages, length of message, etc.) The intent here is to eventually create a self optimizing system. 

<img width="3420" height="6168" alt="AISDR Outreach DB" src="https://github.com/user-attachments/assets/965e6050-bd45-4d7d-ba40-007d364d0415" />

The AI SDR Orchestration view is where you can view settings. Currently all edits and configuration happens through actually making changes to source code files via claude code. But this could be extended into a UI layer. Which I am working on doing. 

<img width="3420" height="4114" alt="AISDR Orchestration" src="https://github.com/user-attachments/assets/5a9cbc58-68ad-43c6-982a-a72a19a99871" />






