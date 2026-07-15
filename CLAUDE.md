# CLAUDE.md — project context for Claude sessions

Read this first. It captures the architecture, deployment topology, and operational
facts that aren't obvious from the code. Companion docs: `README.md` (overview),
`USAGE.md` (CLI runbook), `webui/README.md` (console), `.env.example` (every env var,
documented), `.claude/skills/*/SKILL.md` (pipeline logic).

## What this is

Autonomous outbound pipeline + web console for EverWorker's SDR AI Worker. It pulls ICP
contacts from HubSpot, generates persona-targeted outreach with Claude sub-agents,
enrolls into Email Bison (email) + HeyReach (LinkedIn), logs all activity back to
HubSpot, and reports on results — including nightly AI SDR **deal attribution**.

## Architecture (verified, don't re-derive)

- **One deployed process.** `webui/server/app.py` (~2,800 lines, Python 3.12) serves BOTH
  the JSON API and the built React SPA via `ThreadingHTTPServer`. Routing is a manual
  `if path == "/api/...":` dispatch in `do_GET` / `do_POST`. Every `/api/*` route is
  auth-gated (Bearer token from `/api/login`) unless listed in `_EXEMPT_GET`/`_EXEMPT_POST`.
- **Backend deps:** stdlib only, EXCEPT `requirements.txt` (`pymongo` for the attribution
  store, `dnspython` for technographic detection). Anything importing either must do it
  lazily (see `mongo_store.py`, `tech_signals.py`) so the server still boots without them.
- **Frontend:** React 18 + Vite 5 + Recharts in `webui/frontend/`. API wrappers live in
  `src/api.js`; shared stat tiles / spinners in `src/components/ui.jsx`.
- **Convention:** heavy/write work shells out via `run_script()` to
  `.claude/skills/*/scripts/*.py` (each script prints progress lines, then a JSON summary
  as the LAST stdout line — callers parse that). Read endpoints are in-process
  `*_payload()` functions that must degrade gracefully, never 500.
- **Pipeline scripts** live in `.claude/skills/sdr-pipeline/scripts/`. `hubspot_client.py`
  is the shared stdlib HubSpot client (retry/backoff, `.env` autoload) — extend it, don't
  write new HTTP code.

## Deployment (Railway)

- Railway project has the **sdr-console service** (Docker build from this repo's
  `Dockerfile`; deploys on push to `main`) and a **MongoDB service**.
- **Railway Volume** mounted at `/app/data` = the LIVE data dir. The committed `data/` is
  only a first-boot seed (`docker-entrypoint.sh` copies it in when `pipeline.db` is
  absent). **Prod data on the volume is far ahead of the repo snapshot** — never assume
  repo `data/` reflects production.
- **MongoDB** is wired via `MONGO_URL=${{MongoDB.MONGO_URL}}` (reference variable on the
  sdr-console service, private networking, same project/environment). Database `aisdr`.
- The **Railway CLI is NOT available in cloud Claude sessions** (it's on the user's local
  machine). For Railway dashboard changes (variables, services), either give the user
  click-by-click instructions or ask for a project token. Code changes ship via GitHub →
  merge to `main` → Railway auto-deploys.
- Env vars live in Railway service variables in prod, `.env` locally. `.env.example`
  documents all of them.

## Data stores

| Store | Where | What |
|---|---|---|
| SQLite `data/outreach/pipeline.db` | volume | contacts, batches, signals cache (`account_signals`, incl. the technographic `tech_*` and hiring `hiring_signals/hiring_detail/hiring_checked_at/hiring_error` columns), `hubspot_activity_log` (engagement-logging ledger), `bison_lead_map`, `heyreach_events` inbox. Schema: `scripts/batch_db.py`. The web server opens it READ-ONLY; writes go through pipeline scripts (and their in-process module calls, e.g. signal refresh / tech + hiring detect). |
| JSONL under `data/` | volume | campaign stats, generated outreach copy, interested-reply threads/analysis. |
| MongoDB db `aisdr` | Railway MongoDB service | AI SDR deal attribution: `emails`, `contacts`, `deals`, `sync_state` (see below). Accessed only through `scripts/mongo_store.py`. |

## AI SDR deal attribution (added 2026-07, PR #18)

Nightly job answering "which HubSpot deals did the AI SDR create, and what are they worth?"

- **Flow:** the pipeline logs every AI SDR email to HubSpot as an `emails` engagement
  with header From = `HUBSPOT_AISDR_FROM_EMAIL` (`ai-sdr@everworker.ai`). The sync
  (`scripts/aisdr_attribution_sync.py`) searches those engagements (subject/body/date in
  the search response), resolves email→contact and contact→deal associations in batches,
  snapshots everything into Mongo, computes attribution, and writes
  `ai_sdr_deal_created=true` back to HubSpot deals + contacts (only where not already
  true; never un-sets).
- **Attribution rule (user-approved):** a deal counts iff it's associated with an
  AI-SDR-emailed contact AND `deal.createdate >= that contact's first AI SDR email`.
  "Total pipeline" = sum of `amount` over ALL flagged deals incl. closed lost.
- **Scheduling:** `_aisdr_sync_loop()` daemon thread in `app.py`, fires at
  `AISDR_SYNC_HOUR` (default 0 = midnight) America/New_York, DST-safe. First run against
  empty Mongo = full seed; after that incremental via `sync_state.watermark_ms`
  (10-min overlap; upserts by engagement id = idempotent). Contact→deal associations and
  deal snapshots are re-swept EVERY run so late-created deals get attributed and
  stages/amounts stay fresh.
- **Endpoints:** `GET /api/analytics/aisdr` (tiles), `GET /api/hubspot/aisdr/status`,
  `POST /api/hubspot/aisdr/sync` (`{full?, dry_run?}`, background thread, 409 if running).
  UI: top of AnalyticsPage — two accent tiles + "Sync attribution" button.
- **CLI:** `python3 .claude/skills/sdr-pipeline/scripts/aisdr_attribution_sync.py
  [--json] [--dry-run] [--full] [--limit N]`.
- **Operational facts (from the 2026-07-09 seed):** 18,971 AI SDR email engagements,
  5,039 contacts emailed, 9 deals / $282,000 attributed. The seed takes ~15 min at this
  volume; incremental runs take seconds. HubSpot's raw `ai_sdr_deal_created=true` deal
  count (47) is HIGHER than the tiles (9) because ~38 deals carried pre-existing manual
  flags — the tiles show the strict computed attribution from Mongo; the sync preserves
  the manual flags.
- **Gotchas:** reading email engagements requires the **`sales-email-read`** scope on the
  HubSpot private app token (preflight surfaces a clear `missing_scope` error to the UI).
  HubSpot CRM search caps at 10,000 results — the sync windows past it by restarting the
  `hs_timestamp GT` filter (do the same in any new search-based code). `amount` is
  portal-currency, assumed single-currency (USD formatting).

## Technographic detection (added 2026-07)

Deterministic scan of which GTM tech an account runs (CRM / ad pixels / martech /
salestech) — no LLM, no third-party API.

- **Engine:** vendored at root `technographics/` (from the `technographic-signals` repo —
  provenance + re-sync in `technographics/VENDORED.md`). DNS fingerprinting (dnspython,
  resolvers 1.1.1.1/8.8.8.8: MX/TXT/NS/A/SOA + CNAME subdomain probes) + static-HTML
  fingerprinting, matched against a Wappalyzer-derived catalogue (7.5k vendors), scoped by
  `TECH_SELECTION_FILE` (default: curated ~65 marketing/sales vendors).
- **Runner:** `.claude/skills/sdr-pipeline/scripts/tech_signals.py` (module + CLI). Fetcher
  is stdlib urllib (NO requests/bs4); **NO Playwright in the Railway image** — `--rendered`
  exists for Claude sessions only (Chromium preinstalled there).
- **When it runs:** (1) inline in `generate_batch.py` on a research cache miss (under the
  per-domain lock, before copy is written) and after a UI signal refresh; (2) Signals view
  per-row "Detect" + bulk "Detect missing" (`POST /api/signals/tech/detect`,
  `POST /api/signals/tech/backfill` + `GET /api/signals/tech/status/<id>`); (3) a
  fire-and-forget tail after a Message-Batches job completes; (4) `build_play.py` scans the
  prospect pre-research and the play TARGET post-research (appended as a
  "6c-verified" block in research.md).
- **Storage:** `account_signals.tech_signals` (formatted line, or the literal
  `"No signals detected"`; NULL = scan itself failed), `tech_detail` (detections JSON),
  `tech_checked_at` (reused for `TECH_REFRESH_DAYS`, default 90), `tech_error`.
- **Consumers:** generation prompts get the line as background-only context (reference ONE
  relevant tool max, never list the stack); persona/batch-runner agents carry matching
  instructions; HubSpot write-back PATCHes the `technographic_signals` company property
  (best-effort — company matched by `domain`; `TECH_HUBSPOT_WRITEBACK=0` kills it; needs
  company read/write + schema scopes on the token, otherwise it logs and moves on).
- **Gotchas:** every import of `tech_signals`/dnspython must stay lazy (boot rule above).
  A scan only counts as failed when BOTH channels died (fetch error AND zero DNS records) —
  never store a network-dead run as "No signals detected". `--self-test` runs offline
  against vendored fixtures (works without dnspython/network).

## Hiring signals (added 2026-07)

Per-account job-postings scan — is the company hiring, and specifically for sales/GTM
roles? Ported from the `hubspot-hiring-signals` repo as a **stdlib rewrite** (urllib +
hand-rolled retry; NO requests/tenacity/pyyaml — zero new pip deps).

- **Engine:** `.claude/skills/sdr-pipeline/scripts/hiring_signals.py` (module + CLI).
  One Prospeo `enrich-company` call per domain (`PROSPEO_API_KEY`, **one credit per
  non-cached scan**) → `job_postings.active_count/active_titles`; the sales/GTM subset
  comes from the regex taxonomy ported verbatim from that repo's `config.yaml`
  (exclude beats include).
- **Storage** (`account_signals.hiring_*`, semantics mirror tech): `hiring_signals` =
  formatted line (`"14 open roles · 4 sales: SDR; AE; VP Sales"`) or the literal
  `"No open roles detected"`; NULL + `hiring_error` = the scan itself failed (retries
  next touch). Prospeo's definitive non-matches (NO_MATCH/NO_RESULT/NO_IDENTIFIER/
  INVALID_DATAPOINT) store the literal + `error_code` in `hiring_detail` — a credit
  guard so unmatchable domains aren't re-billed every touch. `hiring_checked_at` drives
  `HIRING_REFRESH_DAYS` (default 90).
- **Rate-limit gotcha (load-bearing):** Prospeo signals throttling as **HTTP 200 +
  `{"error":true,"error_code":"Rate limit exceeded"}`** — `_prospeo_request` retries it
  like a 429 (Retry-After honored, capped 16s; 5 attempts, expo backoff) and must never
  store it as a result. Unknown error codes classify as transport failures (retry).
- **When it runs:** same three hooks as tech — (1) inline in `generate_batch.py` on a
  research cache miss + after a UI signal refresh; (2) Signals view (drawer "⚑ Detect
  hiring", bulk "Detect hiring": `POST /api/signals/hiring/detect`,
  `POST /api/signals/hiring/backfill` + `GET /api/signals/hiring/status/<id>`, separate
  `HIRING_JOBS` registry); (3) fire-and-forget tail after Message-Batches (separate
  thread from the tech tail).
- **Copy consumer (the rule both engines carry):** when the sales subset is non-empty,
  `_cached_hiring()` feeds a compact line into the prompt and **email 2 opens on it**
  (count + 1-2 roles, tied to covering pipeline while the new reps ramp); email 1 keeps
  the researched news signal; skip if email 1 already covers hiring; never name the data
  source or claim postings are new. Matching wording lives in: `generate_batch.py`
  (`build_user` + earn/show WRITE_RULES), `icp-email.md` 4-touch table, `cta-offers.md`
  cadence, `sdr-batch-runner.md`, and the 4 persona agents — `grep -rn "email 2\|EMAIL 2"`
  over those files is the drift checklist. Counts with NO sales roles are not a hook.
- **HubSpot write-back:** refreshes the SAME three company properties the
  hubspot-hiring-signals job maintains — `open_roles_count` (str int),
  `hiring_signals_job_titles` (`<br>`-joined HTML, ALL titles), `hiring_signals`
  (`"; "`-joined **sales subset** — NOT the display line). Matched-with-0-postings
  clears stale values; non-matches skip. Best-effort by domain;
  `HIRING_HUBSPOT_WRITEBACK=0` kills it.
- **Gotchas:** keep every `import hiring_signals` lazy (boot rule); the server must boot
  with `PROSPEO_API_KEY` unset (endpoints return `hiring_available:false`, detect → 501).
  Run a first bulk backfill with `--limit` — every non-skipped scan is a credit.
  `--self-test` is offline (no key/network/DB).

## Background jobs (daemon threads started in `app.py main()`)

1. `_activity_autosync_loop` — hourly: logs new email/LinkedIn activity to HubSpot.
2. `_aisdr_sync_loop` — nightly midnight ET: deal attribution (above).
3. HeyReach webhook drain — near-real-time LinkedIn activity logging.

## HubSpot notes

- The app's own token (`HUBSPOT_ACCESS_TOKEN`, private app, EU portal 144358290 — but API
  host is always `api.hubapi.com`) is the source of truth for API capability.
- The HubSpot **MCP** connector available in Claude sessions has narrower scopes: it can
  read/search deals & contacts but CANNOT read email engagements or run SQL
  (`reporting-base-read` missing). Use it for spot-checks; use the app's scripts for real
  work.
- Custom properties in the portal: deals `ai_sdr_deal_created` (bool); contacts
  `ai_sdr_deal_created`, `ai_sdr_meeting_booked`, `ai_sdr_reply_generated` (+
  `ai_sdr_status`, `ai_sdr_errors`). Contact LinkedIn URL property =
  `HUBSPOT_LINKEDIN_PROPERTY` (default `hs_linkedin_url`). Owner/created-by id→name maps
  come free from the `hubspot_owner_id` / `hs_created_by_user_id` property definitions'
  enum options — no extra scope needed.

## Dev / verification quickies

```bash
python3 -m py_compile webui/server/app.py .claude/skills/sdr-pipeline/scripts/*.py
env -u PORT python3 webui/server/app.py --port 8787   # boots WITHOUT pymongo/MONGO_URL/dnspython
cd webui/frontend && npm ci && npm run build           # SPA build (Dockerfile stage 1)
python3 .claude/skills/sdr-pipeline/scripts/tech_signals.py --self-test   # offline detector check
python3 .claude/skills/sdr-pipeline/scripts/hiring_signals.py --self-test # offline hiring-classifier check
```
The server must always boot with `MONGO_URL` unset (aisdr endpoints return
`{"configured": false}`, nightly loop self-disables) — preserve that when touching
anything Mongo-related.
