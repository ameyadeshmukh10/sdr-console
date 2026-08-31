# SDR Console

**A production AI GTM system I designed, built, and operate end to end** — an autonomous
outbound pipeline plus a full web console for an **SDR AI Worker**, live on Railway.

It sources and researches ICP contacts, generates persona-targeted email + LinkedIn
outreach with Claude agents, enrolls across two sequencing channels, triages and answers
replies, mirrors every touch into HubSpot, enforces do-not-contact suppression, and
closes the loop with **deal attribution** — which deals the AI SDR created and what
they're worth. Humans gate only the outward writes; everything else runs itself.

This repo is my GTM engineering portfolio in working-code form: outbound systems design,
signal engineering, multi-agent AI orchestration, token-cost economics, RevOps
compliance, and closed-loop revenue attribution — all in one deployed system, built solo.

Repo: <https://github.com/ameyadeshmukh10/sdr-console> (private) · Built by
[Ameya Deshmukh](https://github.com/ameyadeshmukh10)

## The system by the numbers

| | |
|---|---|
| **2,000+** | researched, linted, persona-targeted outreach sequences generated (bundled snapshot) |
| **~19,000** | AI SDR email engagements logged to HubSpot; **5,000+** contacts emailed |
| **$282k** | pipeline attributed to the AI SDR by the nightly attribution engine (9 deals, July 2026 seed — strict computed attribution, not self-reported flags) |
| **~23,000** | contact do-not-contact backlog drained by the suppression sweeper without missing a mid-sequence contact |
| **7,500** | vendor fingerprints in the technographic detection catalogue (deterministic — no LLM, no third-party API) |
| **50%** | generation cost cut via Anthropic's Message Batches API, on top of a 90-day signal cache that eliminates repeat research |
| **8** | console views · **~70** API routes · **4** autonomous background loops · **1** deployed process |

## What it does, end to end

1. **Source** — pull an ICP contact list from HubSpot (searchable by name), or grow the
   list from a *company* list: a Clay-powered buying-group enrichment sources
   GTM-leadership contacts per company, dedups against HubSpot, applies the ICP/persona
   filter, and creates them straight into the pipeline. No manual list building.
2. **Research** — every account gets three signal layers, each cached per domain for 90
   days: a researched recent news signal (web search), a **technographic scan** (which
   CRM / ad pixels / martech / salestech the company runs, via DNS + website
   fingerprinting), and a **hiring scan** (open roles with a sales/GTM-role classifier).
3. **Generate** — each contact routes by job title to one of **4 persona agents** (sales
   leadership, RevOps, partnerships, SDR/BDR leadership) that write a value-first
   4-touch email sequence + LinkedIn copy. Signals steer the copy mechanically: an open
   sales-hiring signal opens email 2; a detected sequencing tool (Outreach/Salesloft/
   Apollo) triggers a no-disruption angle; intent/ABM tooling triggers a
   signal-activation story in email 3. Every sequence must pass a **deterministic
   linter** — word counts, paragraph shape, value-anchored meeting CTA, a metric,
   step-4 breakup, no pricing — before it counts as generated.
4. **Enroll** — generated contacts enroll into per-persona Email Bison campaigns and a
   HeyReach LinkedIn campaign, always through a dry-run preview → confirm gate.
5. **Manage replies** — a unified two-pane inbox classifies inbound email + LinkedIn
   replies with Claude, drafts channel-aware follow-ups for interested leads (approve to
   send in-thread), and can build a **personalized signal-play web page** for the lead's
   company — researched, rendered, and published live to the CMS — with the link
   embedded in the reply.
6. **Log + attribute** — background loops mirror all activity into HubSpot (email and
   LinkedIn engagements, per-contact signal notes, technographic + hiring company
   properties), and a nightly engine computes deal attribution with an explicit,
   auditable rule: a deal counts only if it's associated with an AI-SDR-emailed contact
   **and** was created after that contact's first AI SDR email.
7. **Suppress** — contacts RevOps flags as do-not-contact are dropped at pull time,
   blocked at enroll time, and actively **unenrolled from in-flight sequences on both
   channels** by a recurring sweep with a durable, idempotent ledger.

## GTM engineering highlights

The parts I'm proudest of — each a real problem in running AI outbound at volume, solved
in code:

- **Signal engineering, three layers deep.** News, technographics, and hiring are
  separate engines with shared semantics (formatted display line, raw detail JSON,
  checked-at timestamp, error state), a shared 90-day cache, and *rules for how copy
  consumes them* — reference one relevant tool max, never recite the stack, never
  mention chat/scheduling tools, never claim job postings are new. Signal data is only
  as good as the discipline in using it.
- **Deterministic where it should be deterministic.** Tech detection is DNS + static-HTML
  fingerprinting against a Wappalyzer-derived catalogue — no LLM in the loop, offline
  self-tests, and a strict rule that a network-dead scan is never stored as "no signals."
  The LLM is reserved for what actually needs it: research and writing.
- **Token-cost economics as a first-class design constraint.** Web-search tokens dominate
  generation cost, so: same-company contacts are batched together, cache hits become
  write-only calls (zero searches), bulk generation rides the Message Batches API at
  half price with 1-hour prompt caching, and the job log marks every contact `[cached]`
  vs `[searched]` so cost is observable, not vibes.
- **Attribution that survives scrutiny.** The nightly sync snapshots emails, contacts,
  and deals into MongoDB and computes attribution from data, not opinions — it found
  that HubSpot's raw flag count overstated AI-sourced deals by 5x (47 flagged vs 9
  computed) because of pre-existing manual flags. The tiles show the defensible number.
- **Compliance engineered for the worst case.** When RevOps bulk-flagged ~23k contacts,
  the naive sweep starved the contacts who mattered most (newly flagged, mid-sequence).
  I rebuilt it: never-checked contacts first, newest first; paced API calls under
  channel rate limits; per-channel circuit breakers; ledger writes that survive lock
  contention. Suppression is one-way — flipping the flag back re-permits future sends,
  but stopped sequences stay stopped.
- **Experimentation built into the pipe.** Three instruction-set variants (value-give /
  earn / show) with an exact percentage split — largest-remainder allocation plus
  interleaving so variants don't cluster — flowing through generation, enrollment, and
  analytics so A/B results stay attributable.
- **An analysis loop, not just a send loop.** Interested replies feed a trends engine:
  conversion by campaign, seniority, function, CTA, offer type, and reply cohorts, with
  verbatim evidence books per cohort. The system tells me what's working.
- **Reply-to-microsite in one click.** The Signal Playbook agent researches the lead's
  company, builds a personalized play deck as a single-file HTML page, publishes it live
  on the company website via the HubSpot CMS API (stable URL across rebuilds), and
  drafts the reply around the link — a bespoke asset per interested lead, on demand.
- **Agent-driven sourcing over MCP.** The Clay enrichment is orchestrated by a
  cheap model driving Clay's MCP server through the Anthropic API's MCP connector, with
  auth via full OAuth 2.1 — discovery, dynamic client registration, PKCE, token
  refresh — implemented from scratch in stdlib Python.

## The console

React 18 + Vite + Recharts SPA, served by the same process as the API, behind a login
gate (signed bearer-token sessions; every `/api/*` route auth-gated). Eight views:

| View | What it does |
|------|--------------|
| **Use** | Search HubSpot lists by name (contact or company), ingest into the pipeline, or run Clay buying-group enrichment (end-to-end or review-then-commit). |
| **Pipeline** | Live batch progress; one-click generation (real-time or 50%-off Batch API); variant % split; enrollment with dry-run gate + confirm modal. |
| **Orchestration** | Live diagram of HubSpot → persona agents → campaigns with contact counts, plus the suppression safety gate with run-now / dry-run controls. |
| **Analytics** | Campaign reply / interested rates and the deal-attribution tiles (deals created, total pipeline $) with on-demand sync. |
| **Trends** | What's working across interested replies — seniority, function, winning CTA, offer type, conversion by campaign, cohorts. |
| **Replies** | Cross-channel triage inbox: Claude classification, gated Bison tagging, draft → approve → send in-thread, dismiss/reclassify with persistent state, Signal Playbook agent. |
| **Signals** | The per-company research cache: signal, tech line, hiring line, age; per-row and bulk re-detect. |
| **Outreach** | Search/filter/group all generated sequences by persona, CTA play, signal, status; click through to the full copy. |

## Autonomous operations

Four background loops inside the one deployed process — no cron service, no worker fleet:

- **Activity autosync** (hourly) — logs outbound/inbound email + LinkedIn activity to
  HubSpot with a dedup ledger, a duplicate-forensics audit CLI, and a reconcile mode
  that adopts pre-existing engagements instead of re-creating them.
- **Deal attribution** (nightly, midnight ET, DST-safe) — incremental via watermark with
  idempotent upserts; deal associations re-swept every run so late-created deals still
  attribute. Full seed over ~19k engagements: ~15 minutes. Nightly increment: seconds.
- **HeyReach webhook drain** — near-real-time LinkedIn activity logging.
- **Unenrollment sweeps** (every 30 min) — the do-not-contact enforcement described
  above, across both channels, with HubSpot timeline notes on every stop.

Everything that writes outward is either human-gated in the UI (enroll, tag, send) or
best-effort with full logging — a HubSpot write-back never fails a pipeline run.

## Architecture

Deliberately boring where it can be, sophisticated where it counts:

- **One deployed process.** `webui/server/app.py` (Python 3.12, `ThreadingHTTPServer`,
  ~4,000 lines) serves the JSON API (~70 routes) and the built SPA. The backend is
  **Python stdlib only** except `pymongo` and `dnspython` — both imported lazily, so the
  server boots and degrades gracefully with any dependency, key, or database absent.
- **Write/read separation.** The web server opens SQLite **read-only**; all writes go
  through pipeline scripts the server shells out to (progress lines + a final JSON
  summary line as the contract). Read endpoints degrade gracefully rather than 500.
- **Anthropic API without an SDK.** Generation, classification, and MCP-driven
  enrichment all ride stdlib `urllib`: Message Batches, forced JSON schemas, prompt
  caching, the server-side web-search tool, and the MCP connector.
- **Resumable by design.** Batch state is a SQLite state machine
  (`pending → generated → enrolled`, `failed` carries the lint reason); batch jobs
  persist to disk and survive restarts (the poller resumes); enrollment and sweeps are
  idempotent via ledgers.

### Data stores

| Store | What |
|---|---|
| SQLite (`data/outreach/pipeline.db`) | Contact/batch state machine, per-account signal cache (research + tech + hiring), activity ledger, enrollment lead map, unenrollment ledger. |
| JSONL under `data/` | Generated sequences, campaign stats, interested-reply threads + analysis. |
| MongoDB (db `aisdr`) | Deal-attribution snapshots: emails, contacts, deals, sync watermark. |

## Deployment (Railway)

One Docker service + a MongoDB service:

- **Multi-stage image**: Node 20 builds the Vite SPA; a `python:3.12-slim` runtime
  serves it (Node kept in the runtime only for the deck renderer). Push to `main` →
  auto-deploy, `/api/health` healthcheck, on-failure restarts (`railway.json`).
- **Railway Volume at `/app/data`** is the live data dir; the committed `data/` is a
  first-boot seed the entrypoint copies onto an empty volume. The app verifies
  durability at boot and the UI warns if the data dir looks ephemeral.
- **MongoDB** wired via a Railway reference variable over private networking. Unset it
  and attribution reports `configured: false`, the nightly loop self-disables, and the
  rest of the console keeps working.
- Config lives in Railway service variables (locally: `.env`); `.env.example` documents
  every variable.

## Stack & integrations

Python 3.12 (stdlib-first backend) · React 18 + Vite + Recharts · SQLite + MongoDB ·
Anthropic API (Messages, Batches, web search, prompt caching, MCP connector) · HubSpot
(CRM, engagements, properties, CMS pages) · Email Bison · HeyReach · Clay (MCP, OAuth
2.1) · Prospeo · DNS-level technographics · Docker · Railway

## Layout

| Path | What |
|------|------|
| `.claude/skills/` | The pipeline logic: persona sub-agents, copy linter, channel clients, signal engines, attribution sync, suppression sweeper, orchestrator + batch runner. |
| `webui/` | The console (React frontend, stdlib Python backend). See [`webui/README.md`](webui/README.md). |
| `technographics/` | Vendored deterministic technographic-detection engine. |
| `deck-renderer/` | Vite single-file HTML renderer for signal-play pages. |
| `data/` | A real pipeline snapshot: ICP contacts, 2,000+ generated sequences, reply threads + analysis, campaign stats, the SQLite DB. First-boot seed in prod. |
| `USAGE.md` | CLI runbook for every pipeline script. |
| `CLAUDE.md` | Architecture + operational context for Claude sessions. |
| `openapi.json` | Email Bison API reference. |

## Quick start

```bash
git clone https://github.com/ameyadeshmukh10/sdr-console.git
cd sdr-console
cp .env.example .env          # fill in your HubSpot / Bison / HeyReach / Anthropic keys
./webui/run.sh                # build + serve the console at http://localhost:8787
#   or: ./webui/run.sh dev    # hot-reload dev (UI :5173, API :8787)
```

The console opens against the bundled `data/` snapshot, so every page has real content
on first run — no pipeline run required to explore it.

## Running the pipeline yourself

With a valid `.env`, from Claude Code:

```bash
/sdr-batches <N> enroll        # process N pending batches and enroll into Bison
```

Or entirely from the console: **Use** → ingest a list → **Pipeline** → Generate copy (or
Batch API) → dry-run → enroll. See [`USAGE.md`](USAGE.md) for the underlying scripts.

## Secrets

`.env` (live API keys) and the Clay OAuth token file are the only things gitignored —
copy `.env.example` and fill it in (including `AUTH_SECRET_KEY`, which signs console
login sessions). Everything else, including the `data/` snapshot, is in the repo. The
repo is private; treat the prospect data in `data/` accordingly.
