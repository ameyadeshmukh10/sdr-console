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
