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
| SQLite `data/outreach/pipeline.db` | volume | contacts, batches, signals cache (`account_signals`, incl. the technographic `tech_*` and hiring `hiring_signals/hiring_detail/hiring_checked_at/hiring_error` columns), `signal_events` (append-only observation log — the time dimension campaigns qualify against), `campaigns` / `campaign_steps` / `campaign_ctas` / `campaign_members`, `hubspot_activity_log` (engagement-logging ledger), `bison_lead_map`, `heyreach_events` inbox, `unenrollment_log` (suppression ledger). Schema: `scripts/batch_db.py`. The web server opens it READ-ONLY; writes go through pipeline scripts (and their in-process module calls, e.g. signal refresh / tech + hiring detect). |
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
- **Consumers:** generation prompts get the line as background context (reference ONE
  relevant tool max, never list the stack; chat/scheduling tools — Qualified, Drift,
  Intercom, Chili Piper, Calendly — are NEVER mentioned) plus **playbook plays** classified
  from `tech_detail` by `tech_signals.playbook_groups()` (`PLAYBOOK_*` sets; also in the
  scan CLI/API JSON as `playbook`): sequencing tools (Outreach/Salesloft/Apollo) → EMAIL 2
  no-disruption angle (own email+LinkedIn infra, 2-5x on top of the run rate) + run-rate
  CTA; intent/ABM tools (name ONE) or ad pixels (generic, never name pixels) → EMAIL 3
  Memgraph signal-activation story + signal-mapping CTA. `_cached_tech()` returns
  `(line, playbook)`; legacy rows without parseable detail degrade to line-only.
  Persona/batch-runner agents carry matching instructions; HubSpot write-back PATCHes the
  `technographic_signals` company property (best-effort — company matched by `domain`;
  `TECH_HUBSPOT_WRITEBACK=0` kills it; needs company read/write + schema scopes on the
  token, otherwise it logs and moves on).
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
  source or claim postings are new. When a tech sequencing play is also present, hiring
  still opens email 2 and the sequencing point shrinks to one supporting line. Matching
  wording lives in: `generate_batch.py` (`build_user` + earn/show WRITE_RULES),
  `icp-email.md` 4-touch table, `cta-offers.md` cadence + Tier lists, `offer.md` (Memgraph
  signal-activation story + 2-5x infra claim), the gold example
  (`examples/icp-email-sequence.md`), `sdr-batch-runner.md`, and the 4 persona agents —
  `grep -rn "email 2\|EMAIL 2\|email 3\|EMAIL 3\|signal-mapping\|run[- ]rate"` over those
  files is the drift checklist. Counts with NO sales roles are not a hook.
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

## Unenrollment checker (added 2026-07)

Suppression sweeps: contacts RevOps tagged with the HubSpot contact property
`everworker_tag = "false"` (enumeration, values `"true"`/`"false"`) must never be touched
by the AI SDR again — they booked a meeting, became an opportunity, etc.

- **Engine:** `.claude/skills/sdr-pipeline/scripts/unenrollment_check.py` (module + CLI:
  `--json --dry-run --limit --force --contact-id`). Per sweep: search contacts where
  `everworker_tag EQ "false"` (10k re-window via `hs_object_id GT`), then per contact —
  **Bison**: email → lead (`bison_lead_map`, live fallback) → `lead_scheduled_emails` →
  `stop_future_emails` in each campaign with steps still queued (`scheduled`/`sending
  paused`); **HeyReach**: linkedin url → `get_campaigns_for_lead` → `stop_lead_in_campaign`
  per non-finished campaign. One HubSpot timeline note per actually-stopped contact
  (`UNENROLL_HUBSPOT_NOTE=0` kills it; never for never-enrolled contacts).
- **Ledger:** `unenrollment_log` in pipeline.db, dedup key `<rule>:<channel>:<contact_id>`
  — sweeps are idempotent; `failed` retries every sweep; `skipped_unconfigured` re-arms
  automatically once the channel's API key lands; `done` rows re-verify after
  `UNENROLL_RECHECK_HOURS` (default 24) so a contact re-enrolled while still flagged is
  re-stopped within a day; `--force` re-checks everything now.
  **One-way**: flipping the tag back to `"true"` only re-permits future enrollment (the
  gate's live check wins over the ledger) — stopped sequences stay stopped.
- **Gates:** `hubspot_pull.py` drops flagged contacts at pull time (`skipped.suppressed`);
  `sdr_batches.py cmd_enroll` + `enroll.py main()` call
  `unenrollment_check.suppressed_set()` (live tag check, ledger fallback, fail-open —
  the sweeper is the backstop) and skip suppressed contacts with a `suppressed` count.
- **Server:** `_unenrollment_loop()` every `UNENROLL_CHECK_MINUTES` (default 30,
  `UNENROLL_CHECK_ENABLED=0` disables); `GET /api/unenroll/status` (rules[]-shaped —
  future suppression rules append entries), `POST /api/unenroll/run` (`{dry_run?}`,
  409 if running). UI: Orchestration view — a dashed "safety gate" bar in the diagram +
  an "Unenrollment & suppression rules" card section (Run now / Dry run buttons).
- **Gotchas:** HeyReach lead/campaign endpoints all live under the `/campaign/`
  controller (`/campaign/GetCampaignsForLead`, NOT `/lead/…` — the first live run
  404'd on that; paths are cross-checked against the bcharleson/heyreach-cli client).
  Keep `import unenrollment_check` cheap (its module imports are stdlib + batch_db
  only; clients import lazily inside functions — boot rule).

## Inbound vs outbound delineation (added 2026-08)

The console is an OUTBOUND worker, but ICP lists contain inbound-sourced contacts
(the live campaign names show it: "Webinars", "Academy Registered", "Behavioral Lead
Score" are all outbound sends to people who arrived inbound). Without provenance the
attribution silently claims inbound pipeline as outbound — the fastest way to get the
whole number challenged.

- **Provenance at pull time:** `hubspot_pull.py` reads `hs_analytics_source`,
  `hs_latest_source`, `lifecyclestage` and stores them on `contacts` along with a
  derived `motion` column. Additive migration + `idx_contacts_motion` in
  `batch_db.init_schema` (which the server runs on boot, so prod migrates itself).
- **`batch_db.classify_motion()`** is the single definition of inbound, used by the
  pull, the attribution sync and the demo generator so they cannot disagree. It is
  deliberately **conservative: unknown reads as `outbound`.** Over-claiming inbound
  would quietly shrink the outbound numbers; the reverse error is the one this whole
  distinction exists to prevent. Sets: `MOTION_INBOUND_SOURCES`, `MOTION_INBOUND_STAGES`.
- **Attribution is split, not narrowed.** `compute_attribution()` returns
  `(first, attributed, flagged, originated, influenced)`. `attributed` is unchanged
  (same union, same HubSpot write-back); the split is additive:
  - `originated` — EVERY qualifying contact came in cold → the AI SDR created it
  - `influenced` — at least one qualifying contact was inbound-sourced → real
    pipeline we touched, but not pipeline we created
  Persisted per deal as `ai_sdr_attribution`, rolled up by
  `mongo_store.aisdr_analytics()` into `by_attribution`, rendered on Analytics as two
  tiles plus a Motion column on the deal table. Deals synced before the split have no
  `ai_sdr_attribution` and report as `unclassified` until the next sweep.
- **Gotcha:** the rollup must reconcile with `total_pipeline` — verified
  ($370k originated + $116k influenced = $486k). If a third bucket is added, keep it
  in the same aggregation so the parts always sum to the whole.
- **Still not delineated** (deliberate, not oversight): campaign-level motion tags
  (Bison carries `type`, all `"outbound"` today), Trends split by motion — which will
  move the headline rates, since re-engaging a webinar attendee converts far better
  than cold and blending them flatters cold outbound — and the Replies queue, which
  is correct for a pure-outbound world.

## Demo mode / demo profiles (added 2026-08)

Point the WHOLE console at a synthetic dataset instead of live data — for demos,
screenshots, and building charts before real volume exists.

- **A profile** is a mirror of the live `data/` tree at `data/demo/<id>/` plus a
  `profile.json` manifest (`label`, `description`, `customer`, `covers[]`,
  `generated_at`). Shadows `outreach/pipeline.db`, `outreach/contacts.jsonl`,
  `outreach/generated/*.json`, `campaign-stats/*.jsonl`, `interested-replies/**`.
- **How the switch travels:** the client sends `X-Demo-Profile: <id>` on EVERY
  request (injected once in `api.js` `authHeaders()`); the handler validates it and
  stashes it in a **thread-local** (`webui/server/demo_mode.py`) for that request
  only. `ThreadingHTTPServer` = one thread per request, so profiles never bleed
  across requests **and background daemon threads never see one** — they default to
  None (live), which matters because they write to HubSpot/Bison.
- **Read sites go through `R(path)`** in `app.py`, which remaps a path under `DATA`
  into the active profile. Writers and the startup index deliberately keep the bare
  constants. `OutreachIndex` is instantiated **per profile** (`index()`), since it's
  a process-wide cache. New read endpoints must wrap their paths in `R()` or they
  will leak live data into a demo.
- **Missing files are NOT backfilled from live data** — a thin profile shows empty
  states (read endpoints already degrade gracefully). `covers[]` lets the UI say
  "not part of this profile" instead of "no data".
- **A demo must look like a WORKING system.** No failed connectors, no "unavailable"
  capability notices, no synthetic-data disclaimers anywhere in the UI. That means
  every capability probe is **declared, not probed** when a profile is active —
  `_tech_status`, `_hiring_status`, `orchestration_config(demo=True)`, the
  suppression rule's channel chips, and `connectors_payload` (a provider the profile
  omits is HIDDEN, never rendered as not-configured). Anything that would shell out
  or call a live API is served from a profile fixture instead:
  `linkedin.json`, `aisdr.json`, `aisdr_status.json`, `hubspot_lists.json` — the last
  one matters because `do_hubspot_lists` otherwise dumps the script's traceback into
  the Use view. **Fixture field names must match what the page reads** (HeyReach's
  camelCase `connectionsSent`, `deals_created`, `campaign_name`, `last_sync_at`) or
  the tiles silently render "—".
  When adding a view or a probe, check it in demo mode before shipping.
- **One carve-out to the read-only rule:** `_DEMO_FIXTURE_POSTS` in `app.py` is a
  short allowlist of POSTs a demo answers ENTIRELY from its own fixtures —
  `/api/replies/followup/draft` and `/api/replies/followup/regenerate`, so a demo can
  show the agent drafting a reply (the moment the product is about). They are
  intercepted at the top of `do_POST` before any dispatch: no script runs, nothing is
  written, nothing external is called. Regenerated drafts live in `_DEMO_DRAFTS`
  (process memory, keyed by profile+reply_id) and are applied on read in
  `followup_drafts_payload()`, because the UI refetches after a regenerate and demo
  mode must not write. **Never add an irreversible action to this allowlist** —
  approve/send, enroll, sync and config-apply stay refused.
- **Demo mode: A DEMO MAY WRITE TO ITS OWN DATASET AND NOTHING ELSE** (changed
  2026-08; it used to be "every POST 409s"). That was the right instinct expressed as
  the wrong invariant — it also made the demo unable to show the product's central
  act, building and running a campaign. What must never happen is an OUTWARD EFFECT:
  mailing a prospect, writing to the customer's CRM, spending a Clay/Prospeo credit,
  calling the model. An allowed action must satisfy all three: (1) every write lands
  in `data/demo/<id>/…`, (2) no HTTP call leaves the process, (3) no credit or send
  is actually consumed.
  - `demo_mode.writable(path)` is the allowlist — exact-match or regex, never a
    prefix, so a new sibling endpoint is refused by default. It covers campaign
    CRUD/steps/qualify/rescore/discover/enrich, `/api/ingest`, and hotlist refresh.
  - Writes route to the profile's own DB via `batch_db.connect(path)` — passed
    per call, NOT by mutating `DB_PATH`, because the server is threaded and a global
    swap would leak one request's profile into another's writes. `app.py` injects
    `campaigns_api.demo_db_path` / `demo_dir_path`.
  - Background jobs capture the profile paths in the REQUEST thread
    (`_demo_paths()`); the worker has no thread-local and would otherwise write live.
  - `webui/server/demo_actions.py` holds the simulated sources: `simulate_crm_pull`
    (from `crm_pool.json`), `simulate_enrich` (Clay, from `clay_pool.json`),
    `simulate_discovery`. Each returns the SAME response shape as the real path, so
    nothing in the frontend knows a demo is running — a shape mismatch then shows up
    as a broken demo rather than a plausible lie. They sleep at roughly real cadence
    (a Clay run that returned instantly would demo a product that doesn't exist),
    meter credits into the profile's own ledger, and use seeded RNG so the same demo
    twice gives the same result.
  - Still refused (verified): enroll live/dry-run, crm/sync, hubspot/aisdr/sync,
    unenroll/run, config/apply, analytics/refresh, trends/refresh, source/enrich,
    signals/*/backfill, replies/followup/approve, generate.
  - An unknown profile id is a **400**, never a silent fallback to live.
- **Endpoints:** `GET /api/demo/profiles`. UI: switcher pinned under Setup in the
  sidebar (`components/DemoSwitcher.jsx`, state in `DemoContext.jsx`), amber banner
  + top hairline while active, and `<Routes key={profileId}>` remounts every view on
  switch (pages fetch on mount).
- **Generate:** `python3 .claude/skills/sdr-pipeline/scripts/make_demo_profile.py
  [--profile generic] [--label …] [--customer …] [--contacts 120]` — builds the DB,
  copy, signals and campaign stats, then calls
  `interested-trends/scripts/make_demo_data.py --profile <id>` for the replies/trends
  slice (planted effects + `ground_truth.json`). Deterministic (fixed seed).
- **Gotchas:** generated copy must match `generate_batch.py`'s nested
  `email`/`linkedin` object shape or `derive_cta()` 500s the Outreach view. All
  companies are fictional `.example` domains on purpose. `docker-entrypoint.sh`
  refreshes `data/demo/` on EVERY boot (unlike the one-time seed, which is skipped
  once `pipeline.db` exists) — without that, production's already-seeded volume
  would never receive profiles and the switcher would stay hidden.

## Campaigns (added 2026-08)

A **campaign** = a defined set of accounts **showing signal over a target window**,
worked through an ordered **sequence** where **every step declares the CTA/offer it
carries**. It is the first entity that makes "which offer does touch 3 make?"
answerable from data instead of from prose.

- **Membership is derived, never typed.** `signal_query` + `[window_start,
  window_end]` is the definition; `campaign_members` is the materialized result.
  `membership_mode='rolling'` (the default) re-derives on every sweep, so an account
  that first fires on day 9 of a 30-day window joins on day 9; `'snapshot'` freezes
  at launch. **Qualification never REMOVES a member** — a contact mid-sequence whose
  signal ages out of the window would otherwise be stranded half-sent.
- **`signal_events` is the substrate, and it had to be added.** `account_signals`
  holds one MUTABLE latest row per domain and its `*_checked_at` columns record when
  we LOOKED, not what fired — "which accounts showed signal between X and Y" was not
  expressible before. The new table is append-only, written by all three
  `upsert_*_signals` functions, deduped by a **fingerprint of the signal VALUE** so a
  re-scan of an unchanged finding is not a new event (`observed_at` = when the signal
  appeared to us). Negative results (`"No signals detected"`, `"No open roles
  detected"`, the `"no recent signal - …"` research fallback) are never events, so
  they can never qualify an account. `init_schema` back-seeds it once from
  `account_signals` using the scan timestamps.
- **Step → CTA is the point.** `campaign_steps.cta_key` references `campaign_ctas`,
  the offer library from `ai-sdr/knowledge/cta-offers.md` **as data** (8 builtin rows,
  seeded idempotently from `batch_db.CTA_LIBRARY` — keep the two in sync). This
  INVERTS the old flow: the offer used to live only as prose in the prompt and was
  reverse-engineered afterwards by `app.derive_cta()` over the finished copy.
  `campaigns.render_plan_prompt()` turns the plan into prompt text that
  `generate_batch.build_user()` injects as authoritative over the knowledge-base
  cadence, and the asset records the `campaign_id` it was written against.
- **Copy per step is `generated` or `manual`.** Generated = the persona agent writes
  per contact inside the frame the step declares (cta_key + angle). Manual =
  `subject`/`body` on the row are used verbatim. `POST …/suggest` drafts manual copy
  **inside the step's assigned offer**, so a suggestion cannot drift off the CTA the
  campaign assigned to that touch; it returns a draft for review and saves nothing.
  At enrollment `sdr_batches._apply_manual_copy()` overlays manual steps onto the
  generated `subject{n}/body{n}` before they become Bison custom variables, filling
  `{{first_name}}` / `{{company}}` (the only two merge vars the UI advertises). A
  step flagged manual but left blank keeps the generated copy rather than sending an
  empty email. Only steps 1-4 apply — Bison carries `subject1-4`/`body1-4` and
  nothing more. The legacy `enroll.py` path does NOT do this overlay.
- **1:1 with the sender.** One console campaign owns one Bison campaign
  (`bison_campaign_id`) + one HeyReach campaign, which is what lets Bison stats roll
  straight up. `sdr_batches.cmd_enroll` routes by campaign FIRST and only then falls
  back to the old variant → persona → `BISON_CAMPAIGN_ID` chain, and now records
  `campaign_members.state='enrolled'` + `bison_lead_id` — previously the campaign and
  lead ids were discarded and only `contacts.status='enrolled'` survived.
- **Discovery ("Find accounts") is the other half of qualification.** Qualification
  only READS signals already observed, so a new campaign starts empty with no obvious
  next step. `campaigns.discover()` scans in-scope accounts (persona/motion filters,
  minus existing members) that have never been scanned, then re-qualifies —
  turning "0 accounts match" into "0 match, 478 never scanned". Only the two
  DETERMINISTIC detectors run: the research signal comes from an LLM web search at
  copy-generation time, and discovery says so rather than pretending to cover it.
  **The hiring detector spends one Prospeo credit per domain**, so: dry-run previews
  scan nothing and state the cost, the UI default is 25, and the background sweep is
  capped by `CAMPAIGN_DISCOVERY_LIMIT` (default 25/campaign/sweep, 0 = manual only)
  and only fires on each campaign's `discovery_interval_days` (default 7).
- **Priority scoring drives the SDR call list.** `campaigns.score_member()` returns
  0-100 + a hot/warm/cool band, stored on `campaign_members` at qualification and
  **frozen there** — a score that drifted as signals aged would make yesterday's call
  list unreproducible, and "why was this person top of my list on Monday" is a real
  question. `POST …/rescore` is the explicit way to move them. Components:
  `signal_strength` 0-50 (kind × a recency DECAY multiplier — recency multiplies
  rather than adds, or a trivial "they run HubSpot" detection reaches warm purely for
  being scanned today), `stacking` 0-25 (+12.5 per additional signal family — news +
  hiring + a tech play gives the rep three different things to say), `persona_fit`
  0-25. Every component is returned and shown on hover; a priority number nobody can
  interrogate gets ignored.
- **The call list is ACCOUNT-DIVERSE, not a raw score sort.** Signal is an
  account-level property, so a strict sort puts all 49 buyers at one funded company
  above every other account. `batch_db.campaign_members(order='priority')` ranks
  within account (`ROW_NUMBER() OVER (PARTITION BY domain …)`) then interleaves, so
  every account's best contact precedes any account's second. `order='score'` gives
  the raw ranking.
- **Endpoints:** `GET /api/campaigns`, `GET /api/campaigns/<id>`,
  `GET /api/campaigns/calllist?campaign_id=&limit=&state=`,
  `GET /api/campaigns/discover/status/<job>`, `GET /api/signals/events?days=`;
  `POST /api/campaigns` (create, seeds the default cadence), `POST /api/campaigns/<id>`
  (patch), `…/delete`, `…/steps`, `…/steps/delete`, `…/qualify` (`{dry_run?}`),
  `…/discover` (`{limit?, kinds?, dry_run?}` — background job, 409 if one is already
  running for that campaign), `…/rescore`, `…/suggest`.
- **UI lives inside the Use view**, not on its own nav item — starting a campaign IS
  how you put the worker to work. `pages/UsePage.jsx` has three tabs
  (Campaigns / Call list / Source contacts); `/campaigns` and `/calllist` are routes
  onto the same page with a preselected tab, so older links and Home's widget still
  resolve. Components: `CampaignsPanel`, `CampaignDetail`, `CampaignForm`,
  `SequenceEditor`, `DiscoveryPanel`, `CallList`, and `campaignShared.jsx`.
  **`campaignShared.jsx` exists to break an import cycle** — `CampaignDetail` needs
  `StatusBadge`/`windowLabel`, and importing them back out of the page module created
  page → detail → page. Rollup hoisted through it, so it only broke under the Vite
  dev server. Keep shared campaign presentational helpers there.
- **Home widget:** the `campaigns` section of `home_payload()` renders as "Work next" —
  a miniature of the call list, linking to `/calllist`. Deliberately not a campaign
  dashboard: "who do I call next" is the only campaign question worth putting on Home.
- **Background:** `_campaign_sweep_loop()` every `CAMPAIGN_SWEEP_MINUTES` (default 60,
  `CAMPAIGN_SWEEP_ENABLED=0` disables) re-qualifies rolling campaigns and auto-completes
  ones whose window has closed. Qualification is local-only; discovery inside the sweep
  DOES reach out, hence the two rate limits above. Like every daemon thread it ignores
  demo profiles.
- **Audiences decide WHO is in the pool; signal_query decides who is worth working
  NOW.** `campaigns.audience` (JSON) is resolved by `audiences.py`:
  `all_contacts`, `hubspot_list`, or `crm_query` with a preset — `closed_lost`
  (contacts on deals lost in N days), `closed_won`, `no_deal`, `lifecycle`. Closed-
  lost resolves stage ids by matching the LABEL of `dealstage`'s enum options,
  because stage values are pipeline-specific. A resolution FAILURE yields nothing
  rather than silently widening the campaign to everyone. Contacts the CRM matches
  but we've never pulled are reported as `not_in_pipeline`, never invented — pulling
  them changes the contact pool and stays a separate step.
- **Enrichment finds the REST of the buyer group.** Discovery scans accounts we hold
  contacts at; `campaigns.enrich()` runs Clay at those same accounts to find the
  buyers we don't, thinnest committee first. Reuses `source_contacts.py` for
  dedup/create/list rather than re-implementing it. Every Clay call is metered on
  FIRE (an empty search still bills).
- **Spend and capacity share one ledger** (`usage_ledger`, `capacity.py`). Credits
  (Clay/Prospeo, real money) and sends (LinkedIn ~20/connected account/DAY, email
  15,000/MONTH) are the same question — what did this consume. **Report-only: nothing
  blocks.** Different clocks on purpose; showing the LinkedIn daily allowance
  monthly would hide the constraint that actually bites. Metered at
  `sdr_batches.cmd_enroll` (sends), `hiring_signals.detect_and_store` and
  `campaigns.enrich` (credits).
- **Channels: the score says WHERE to spend, not just who is best.**
  `capacity.recommend_channels()` → call / linkedin / email / ads per contact, gated
  on score and `SENIOR_ROLES` (reusing `buyer_group.buyer_role()`'s taxonomy, not a
  parallel one). A full LinkedIn allowance downgrades the recommendation rather than
  promising a send that can't happen. `ad_audience()` is ACCOUNT-level — ads justify
  themselves by reaching the whole committee, so an account qualifies on committee
  depth, not any one person.
- **Score momentum.** A contact scored in an earlier campaign has a baseline;
  `apply_momentum()` stores `previous_score`, `momentum` and `rank_score`
  (= score + a ±15-bounded adjustment). The call list orders by `rank_score` while
  `priority_score` stays the pure comparable strength — an account warming up
  outranks a statically-equal one going cold. On a rescore the baseline is the
  member's own current score; a prior score in another campaign only seeds the first.
- **Overlapping campaigns are SEQUENCED TOGETHER.** A contact in several campaigns
  would otherwise get both cadences stacked. `campaigns.touch_plan()` merges every
  step of every campaign they're in into ONE timeline spaced by
  `MIN_TOUCH_GAP_DAYS` (2), max one touch a day — **spacing wins over the campaign
  window**; a window says when an account may ENTER, not that we may talk over
  ourselves to finish inside it. `render_context_prompt()` feeds the other
  campaign's touches into the copy prompt so each writes as one conversation instead
  of two cold opens. Membership lives on the PERSON: `contact_campaign_tags()`
  attaches every campaign to every member row, and the CRM `ai_sdr_campaigns`
  property carries all of them "; "-joined.
- **CRM is the source of truth** (`crm_sync.py`, `crm_field_map` table). We PUSH what
  we compute and READ mapped values BACK as authoritative, so a human editing a
  property in HubSpot wins instead of being reverted next scan. The mapping is DATA,
  so a different portal (or Salesforce) is config, not a deploy. A pulled value is
  also logged as a `signal_event` — a human-authored signal is still a signal. Blank
  values are never pushed over a human's text. `hubspot_client.ensure_property()`
  generalizes the old company-only helper to contacts/deals and maps `number`/`bool`
  properly (a score written as a string is un-sortable in HubSpot).
- **Hot target list** — the daily top-20 ACCOUNT report across active campaigns
  (`campaigns.hot_target_list`, persisted to `data/outreach/hot-list.json`, rebuilt
  once per 24h by the sweep). Account-level because a rep plans a day around
  accounts. **Served from the snapshot, not computed per request** — a list that
  reshuffles between page loads can't be planned against. The rollup dedupes to one
  row per contact first: a person in two active campaigns was counted twice and
  double-weighted their own momentum.
- **Signal kinds are a registry, not a constant.** `campaigns.SIGNAL_REGISTRY` holds
  every kind (research / hiring / tech / website_visit / prior_score / crm_field)
  with its label, base strength, detector and optional `decay_scale`. Adding an entry
  there makes the kind selectable in the campaign builder, scorable, filterable in the
  Signals feed and chartable — nothing else changes. `decay_scale` lets a kind age
  faster or slower than the default (a page view three weeks old is not intent;
  a warm prior score still counts).
- **Signals view has two halves, and they answer different questions.** The cache
  table is `account_signals` — one mutable row per domain, latest value only. The
  `SignalFeed` below it is `signal_events` — append-only, every kind, ordered by when
  the signal appeared. Only the second can answer "what fired this week", which is
  also exactly what a campaign window qualifies against.
- **Call-list columns are configurable** (`ColumnPicker`), and the options include
  every enabled row of `crm_field_map` — so wiring a CRM property makes it available
  as a column with no code change. Stored per-browser in localStorage: it is a view
  preference, and persisting it server-side would make one rep's layout everyone's.
- **Analytics and Trends are organised by QUESTION, not by data source.**
  Analytics has three tabs following the causal chain — Outcome (attributed
  pipeline), Funnel (the end-to-end chain), Channels (email/LinkedIn/Bison detail).
  Trends has two — Targeting (does the score predict?) and Messaging (the interested
  reply analysis). The score/channel/momentum work was originally appended as a final
  section on each, which buried the question under answers to a different one.
- **The funnel is the spine** (`campaigns_api.funnel_payload`, `Funnel.jsx`):
  qualified → enrolled → contacted → replied → interested, joining console campaigns
  to Bison stats 1:1 via `bison_campaign_id`. Analytics previously started at
  *contacted*, because the Bison campaign was the unit of analysis — which made every
  upstream decision invisible and left a bad number with no diagnosable cause.
  - Stage `source` is labelled (`console` = exact, `bison` = last stats refresh)
    rather than blended; mixing a live count with a day-old one invents rates.
  - **A Bison campaign holds every lead ever put into it**, including pre-campaign
    enrollments, so its `contacted` covers a WIDER population than a console
    campaign's `enrolled` — dividing them produced a 14,602% conversion. There is no
    per-lead attribution in the snapshot to net it out, so when
    `contacted > enrolled` the stage is flagged `mixed` and the rate is withheld.
    Don't "fix" this by clamping to 100%; the number is not comparable, not too big.
  - Demo console campaigns bind to Bison ids read from the profile's OWN
    `campaigns.jsonl` (`_demo_bison_ids`), not the live portal's 14/15/16 — a
    binding to an id the profile lacks makes every stage past Enrolled read zero.
- **Analytics/Trends ask whether the targeting model works.** `CampaignAnalytics`
  (Analytics) and `ScoreTrends` (Trends) both read `GET /api/analytics/campaigns`,
  comparing reply rate ACROSS priority bands rather than asserting the score is good.
  A cool band at 0% is the STRONGEST separation, not a missing comparison — guard any
  lift ratio against divide-by-zero or a perfect model reports as a broken one.
  Demo replies are drawn weighted by band for the same reason: random replies made a
  working model render as broken.
- **CLI:** `python3 .claude/skills/sdr-pipeline/scripts/campaigns.py
  {list|show|plan|qualify|discover|enrich|rescore|calllist|hotlist|sweep}`;
  `audiences.py {presets|resolve}`; `capacity.py {status|spend}`;
  `crm_sync.py {fields|push|pull|ensure}`. All take `--json` / `--dry-run`.
- **Gotchas:** campaign WRITES need a read-write connection — `app.db_connect()` is
  `mode=ro`, so `campaigns_api` uses `batch_db.connect()` (the same escape hatch the
  HeyReach webhook uses); every campaign write is a POST, so demo mode's blanket POST
  guard already refuses them. `signal_query` validation REJECTS unknown keys rather
  than ignoring them — a typo'd filter that silently matches everything would enroll
  the wrong accounts. `motion` defaults to `outbound` for contacts with a NULL motion,
  matching `classify_motion`'s conservative default. The account cap counts
  ACCOUNTS, not contacts. The `idx_cmembers_score` index is created AFTER the ALTER
  migrations, not inside the schema script — on a DB predating scoring the column
  does not exist yet and `CREATE TABLE IF NOT EXISTS` is a no-op there.
  `campaigns._playbook()` must always return a dict: `playbook_from_detail` returns
  None for a legacy row with no parseable detail, and indexing that None failed a
  whole sweep. **Demo mode:** `make_demo_profile.build_campaigns()` writes through
  the REAL helpers (`qualify`, `record_signal_event`, `record_usage`) so the demo
  can't drift from the build — same scorer, same momentum rule, same hot-list query.
  Copy suggestions in a demo come from `campaign_copy.json` via
  `_DEMO_FIXTURE_POST_PATTERNS` (a regex allowlist, since the path carries an id),
  because "needs an API key" is exactly the not-configured notice a demo must never
  show when the agent writing copy IS the product.


## Campaign brief, file import, evergreen, connectors (added 2026-08)

Four additions that share one theme: the things that used to require a file edit,
an env var, or a redeploy are now done from the console — and all four work in
demo mode without touching anything real.

- **Campaign brief configurator** (`webui/server/campaign_brief.py`,
  `components/CampaignBrief.jsx`). Describe a campaign, or drop a spec file, and
  the builder form fills itself in. The model emits a constrained CONFIG PATCH
  (ids from `SIGNAL_REGISTRY` / `CRM_PRESETS` / the CTA library), validated by
  `validate_config` before it touches the form; an unknown value is **dropped with
  a warning shown to the user, never coerced** — a filter silently changed to
  something adjacent targets the wrong accounts. It PROPOSES only: fields visibly
  change, marked `✦`, and the user still presses Create. Underdetermined specs come
  back as a QUESTION whose options each carry their own config overlay, so
  answering visibly moves other fields. Demo: `_demo_campaign_brief` +
  `data/demo/<id>/campaign_brief.json` — keyword-matched recipes, `clarify.decides`
  names the fields held back until the question is answered, and the fixture still
  goes through `validate_config` so a demo can't put an un-creatable value on screen.
  - **`campaigns.brief`** (new column) is the agreed DIRECTION in prose — distinct
    from `description` (a label) and from the structured fields (what to DO).
    `render_brief_prompt()` prepends it to the step plan in `generate_batch`, so
    what was decided in the room reaches the copy instead of stopping at the form.
- **Drop a CSV/Excel list** (`webui/server/contact_import.py`,
  `components/ContactUpload.jsx`; audience type `upload`). Event exports and badge
  scans are the freshest audience there is and the only one the CRM has never seen.
  **XLSX is parsed with `zipfile` + `ElementTree`** (an xlsx is a zip of XML) to
  keep the stdlib-only rule — no openpyxl. Two steps: preview counts and column
  mapping write nothing; import delegates to `source_contacts.py`, which already
  does HubSpot dedup, the ICP gate, contact creation and pipeline ingest
  idempotently, so CRM creation comes for free. Recorded in `contact_imports` /
  `contact_import_members` so `{"type":"upload","import_id":N}` keeps meaning "the
  people from that list" after the file is gone. Demo: `simulate_file_import`
  parses for real, writes to the profile DB, skips the CRM leg.
- **Evergreen campaigns.** `evergreen`, `evergreen_interval_days`, `review_due_at`,
  `review_state`, `cycle`, `relaunched_at` on `campaigns`. **The interval is how
  often somebody is ASKED, not how often it relaunches** — what decays in an
  always-on campaign is the message, not the targeting, so the sweep
  (`review_due()` → `raise_review()`) PAUSES the campaign and raises a review
  instead of auto-completing it; `relaunch()` is the only path to the next cycle.
  Checked BEFORE the window-closed branch in `sweep()`, or an evergreen campaign
  whose window ran out would be completed and never asked. UI:
  `EvergreenReview.jsx` above the tabs on the campaign, plus a Home strip
  (`sections.reviews`) above the outcome tiles — a campaign in review is silently
  not sending. `GET /api/campaigns/reviews`, `POST …/<id>/relaunch`.
- **Connectors are wirable from Setup** (`webui/server/connector_store.py`).
  Credentials entered in the console are stored at **`<DATA>/connectors/credentials.json`
  on the Railway volume** — the only writable location that outlives a deploy, since
  the app cannot write its own service variables. **Stored wins over the
  environment** (the reverse would let someone rotate a key in the UI and change
  nothing); `apply_to_environ()` runs FIRST in `main()` so a console-set key is live
  on the next boot without touching Railway. `_INJECTED` tracks keys this module put
  into `os.environ`, or every stored key would read back as "also set in the
  environment". **Secrets never leave**: `describe()` returns presence, source and a
  mask (`tes…3456`) only. Saving immediately runs `connectors.test_connection()` — a
  cheap authenticated READ per provider (Prospeo deliberately does NOT scan: every
  scan is a paid credit). `POST /api/connectors/<id>` / `…/test` / `…/disconnect`,
  all refused in demo mode by the blanket POST guard, and demo payloads carry
  `writable:false` with no `fields` at all.
- **The money scale** (`campaigns.money_rating`, `Money` in `campaignShared.jsx`).
  Aggregate signal as `$`–`$$$$$` on the call list, campaign members and hot
  targets. **Two independent axes**: the COUNT is how much is there (rank score),
  the COLOUR is how ready they are (`hot` inbound / `warming` / `open` / `cold`).
  `$$$$$` in the cool "open" hue is a big unworked opportunity; `$` in hot is a
  small inbound one — the two disagreeing is the informative case. Derived on read
  from already-frozen fields, never stored, so it cannot disagree with the score
  beside it. Clicking prints a receipt itemising the components; `base` (the sum of
  the components) is the TOTAL and `score` is shown separately as RANKED AT, or the
  receipt fails to add up.
- **Contact pull count.** `hubspot_pull.py --limit N` caps how many **NEW** contacts
  a pull adds (counting new, not list members, is what makes "pull 50 more" work
  twice). `/api/ingest` takes `limit`; `_ingest_limit()` maps absent/0/"max" to
  uncapped. Source tab has presets + Custom + **Maximum**.
- **Demo-mode fixes this shipped with:** `KNOWN_AREAS` was missing `"campaigns"`, so
  `covers` filtered it out and the panel claimed campaigns weren't part of the
  profile; `audience_preview` used a bare `db.connect()` and reported the LIVE
  contact pool inside a demo; `hubspot_list` / `crm_query` audiences leaked
  "HUBSPOT_ACCESS_TOKEN is not set" (now `demo_actions.DemoCRM`, injected via
  `audiences.resolve(crm=…)` / `qualify(audience_crm=…)` — passed explicitly, never
  a global, because the server is threaded); `simulate_discovery` recorded events
  but never stamped `*_checked_at`, so scanned accounts never left the unscanned
  queue; and every demo account was pre-scanned, so "Find accounts" was permanently
  greyed out (`UNSCANNED_SHARE` + `NEW_COMPANIES` in `make_demo_profile.py`).

## Configurable signals + channel plan (added 2026-08)

- **What counts as a signal is DATA, not code.** `signal_defs` (kind, label,
  strength, decay_scale, detector, rule, active, builtin) replaces the hardcoded
  registry. `batch_db.SIGNAL_DEF_SEED` is the single source for the builtins and
  `campaigns.SIGNAL_REGISTRY` is derived from it, so a kind cannot mean one thing to
  the scorer and another to the builder. **`campaigns.signal_registry(conn)` is the
  live read**; the constant is the no-connection fallback.
  - `validate_signal_query(q, conn)` takes an optional conn and widens the accepted
    kinds to whatever the deployment defined. Without a conn only builtins validate —
    the safe direction: a caller with no DB rejects a custom kind rather than
    accepting a typo. Every real caller (create/patch/qualify/discover/brief) passes
    one.
  - `score_member(..., registry=)` and `_kind_strength(ev, registry)` read the live
    strengths/decays. `qualify()` and `rescore()` read the registry ONCE per run.
  - Builtins can be retuned or deactivated but **never deleted** — stored events
    reference the id. Deactivating stops it being offered and stops its rule running;
    it deliberately does NOT invalidate campaigns already qualifying on it.
- **CRM-derived rules** (`.claude/skills/sdr-pipeline/scripts/crm_signals.py`). A kind
  with a `rule` is self-executing. Three sources, each with a CLOSED field list
  declared in `SOURCES` (the reports.py discipline — a property name is never
  interpolated from user input): `local_field` (our own contacts row — free, instant,
  works with no CRM), `contact_property` (HubSpot batch reads; where the activity
  counters live), `deal` (contact→deal associations, won/lost resolved from stage
  LABELS like audiences.py). `TEMPLATES` are the starting points people actually mean
  — prior activity, prior deal, closed-lost, opens-but-never-replies, page views,
  lifecycle. Events are written at DOMAIN level (that's what campaigns qualify
  against) and deduped by `record_signal_event`'s value fingerprint, so re-running an
  unchanged rule does not manufacture a fresh observation every sweep.
  - `preview()` is `evaluate(commit=False)` — the count is the whole reason the step
    exists. The sweep calls `run_all()` BEFORE qualification (rules can create the
    observations qualification reads), skipping CRM-backed rules when no token is set.
  - UI: `SignalDefinitions.jsx` on the Signals view — strength slider per kind, decay
    as plain language, run/turn-off/delete, and a rule editor that previews before it
    can save. `GET /api/signals/definitions`, `POST …` / `…/preview` / `…/<kind>/run`
    / `…/<kind>/delete`, dispatched by `Handler._signals_post` (shared by the live and
    demo POST branches — the demo branch fell through to `_campaigns_post` at first).
  - Demo: `DemoCRM.contact_properties()` / `.contact_deals()` generate per-contact
    values deterministically from the contact id, so a rule catching 12 accounts keeps
    catching those 12. `make_demo_profile` ships one custom `prior_activity` kind, or
    the demo would only ever show the six builtins and never the feature.
- **Channel plan on the campaign** (`campaigns.channels` JSON, `ChannelPlan` /
  `ChannelChips`). Email / LinkedIn / Advertising as a declaration of intent: the same
  accounts get worked differently depending on the play — LinkedIn only for a senior
  committee, email for volume, ads across the account to build familiarity underneath
  both. **`ads` is roadmap and says so**: ticking it records the intent and sizes the
  audience from `capacity.ad_audience()` (surfaced as `ad_reach`), and buys nothing.
  `_validate_channels` rejects unknown keys.
- **Easter eggs.** The `$` receipt ends with "marketing so easy sales can do it".
  Creating a campaign fires `MoneyRain.jsx` — falling `$` and "Dollar, dollar, bills
  y'all", self-clearing, pointer-events-none except the card, skipped under
  `prefers-reduced-motion`. Drawn, not embedded: the CSP blocks remote assets and
  shipping a copyrighted clip into a customer-facing bundle is not on.

## Working the call list + Use↔Analytics links (added 2026-08)

- **Two levels of action on a contact, deliberately separate.** "Not a fit for this
  campaign" and "stop contacting this person" feel adjacent while working a list and
  are wildly different decisions — the second burns a contact for every campaign you
  will ever run. `ContactActions.jsx` renders them as two sections with different
  headings and colour, and the second states its blast radius.
  - CAMPAIGN level → `campaign_members`: `manual_priority`, `snoozed_until`,
    `worked_at`, `outcome` (worked/no_answer/not_a_fit/later), `note`.
    `POST /api/calllist/member` with an `action`. `manual_priority` OVERRIDES the sort
    (`COALESCE(manual_priority, rank_score, priority_score)`) while `priority_score`
    is left untouched — the override stays visible as an override. A snoozed member is
    still a member (`hide_snoozed` filters the view, nothing is removed). Only
    `not_a_fit` changes `state`.
  - PERSON level → `contacts`: `engagement_state` (active|paused|suppressed),
    `paused_until`, `engagement_note`. `POST /api/calllist/engagement`; the response
    lists which campaigns it affects, because a switch with an invisible blast radius
    is one nobody trusts.
- **Person-level suppression is ENFORCED, not cosmetic.**
  `batch_db.suppressed_contact_ids()` now unions the unenrollment ledger with
  `engagement_state`, so it flows into BOTH existing gates: `campaigns.qualify()`
  (verified: candidates 78→77, `skipped.suppressed` 0→1) and
  `unenrollment_check.suppressed_set()` (the enroll gate). An EXPIRED pause is not
  suppression — filtered by date, so a pause lifts itself with no job.
  - Local suppression is checked against EVERY id, not only CRM-shaped ones. The
    digit filter in `suppressed_set` exists for HubSpot's batch read; applying it to
    the local check too meant a contact with a non-numeric id (file import,
    enrichment, any demo profile) could be marked do-not-contact and still enroll.
    Local suppression also holds when HubSpot is unreachable, and the live tag read
    can only ADD to it — a CRM tag flipped back to "true" re-permits a CRM-driven
    suppression, not a decision a human made in the console.
  - Suppressing an EXISTING member does not remove them (qualification never removes
    members). They stay listed, dimmed and badged; the send gate is what stops them.
- **Use ↔ Analytics deep links.** `/campaigns?open=<id>` opens straight into a
  campaign (`CampaignsPanel` reads the param and clears it on back), so the funnel,
  Home and the hot list can point AT a campaign rather than at the list containing
  it. Funnel rows link back; a campaign links forward to
  `/analytics?tab=funnel&campaign=<id>`, which selects the tab and highlights the row
  (`tr.row-focus`). The call list links to the funnel — "is this converting?" is the
  question the list raises and Analytics answers.
  - Gotcha: `Funnel` already took `compact`; a deep-link prop was added ALONGSIDE it.
    Replacing the signature dropped `compact`, which is still referenced inside and
    would have thrown at render. HomePage's `<Funnel counts= total=>` is a different,
    local component.
- **Transcripts in the campaign generator.** `campaign_brief.read_attachment()`
  accepts text or base64 (one decode path; `.docx` via zipfile + ElementTree, same
  stdlib trick as xlsx). Transcripts are DETECTED, normalized (WEBVTT headers, cue
  numbers, `-->` ranges and leading `[00:14:02]` clocks stripped; consecutive turns by
  one speaker merged) and labelled as transcripts so the prompt tells the model to
  find the decision inside a conversation.
  - Detection discriminates a transcript from a `key: value` spec by SPEAKER
    REPETITION — `Name:` lines alone match "Target: closed lost / Window: 90 days".
    A conversation has few speakers taking many turns; a spec has one line per key.
    Turn counting runs on the timestamp-STRIPPED line, or the commonest export shape
    in the world (`[00:14:02] Dana:`) scores zero turns and reads as a spec.
  - A pasted transcript gets the same treatment as an uploaded one.

## CTA plays carry content (added 2026-08)

- **A CTA is a promise; content is the evidence behind it.** Proof used to live only
  as prose in `offer.md`, so you could not see which proof a given touch leaned on,
  or swap it without editing markdown. `customer_references` holds it as data and
  `cta_content` joins it to offers — a JOIN, not a column, because "add and remove
  content from a play" is inherently many-to-many: one case study backs several
  offers, and an offer often carries a story AND an asset.
- **Most proof already lives somewhere**, so a reference is usually a LINK (`url`)
  plus enough summary for the writer. The console points at the source of truth
  rather than forking it, and the link is rendered and opens. The URL scheme is
  validated server-side (`^https?://`) because it becomes a real anchor —
  `javascript:` dressed up as a case-study link is the obvious attack.
- **`nameable` is the load-bearing field.** Naming a customer who has not agreed is
  a relationship and legal problem, and it is a fact about the CUSTOMER, not the CTA
  citing them. When it is 0, `campaigns._render_reference()` **never sends the name
  or the quote to the model** — not "sends them with an instruction not to use
  them", because the reliable way to stop a model naming a customer is for it never
  to see the name. It gets `anonymous` (or "a <industry> company") instead. Verified:
  name and quote both absent from the rendered prompt. Off by default; the API
  refuses an un-nameable reference that has no description to use instead.
- **UI:** `PlayContent.jsx` under each step's CTA in the sequence editor — the
  attached content as chips with a visible × , an `+ Add content` picker over the
  library, and a New-content drawer whose nameable checkbox explains its own
  consequence. `GET /api/references`, `POST /api/references`,
  `POST /api/references/attach` (`{detach:true}` removes — one endpoint, both
  directions, because Add and Remove are the same decision).
- Seeded from `offer.md` § Proof (the two Memgraph stories) and attached to
  signal-play / signal-mapping / run-rate. Attachment is `INSERT OR IGNORE` gated on
  the join table being empty, so content detached by hand stays detached across a
  redeploy.

- **Content repositories are a connector category.** `CATEGORIES` gains `content`
  ("Content & enablement") — separate from `platform` because "where does our
  content come from?" is asked by a different person than "what is this deployed
  on". `hubspot-files` (HubSpot File Manager) is genuinely INTEGRATED: it detects
  off the same private-app token and `hubspot_client.upload_file` already hosts
  generated assets on a public URL. Drive / Notion / SharePoint / Confluence /
  Highspot / Seismic / Gong are catalogue-only roadmap entries.
  - `connectors.content_repositories()` is read by BOTH Setup and the CTA content
    picker, so the two can never disagree about what is connected. It adds
    `browsable` — a narrower question than "configured": **linking works from any
    source at all, and connecting a repository only changes whether the console can
    browse it for you.** The picker says exactly that, so an empty library never
    reads as "nothing is set up".
  - Demo gotcha this shipped with: an INTEGRATED provider missing from a profile's
    `connectors.json` is HIDDEN, so the demo showed zero content sources —
    precisely the not-configured impression a demo must never give.
    `make_demo_profile` now names `hubspot-files` in the connected list. Anything the
    demo tells a story about has to be on that list.

## Inbound campaigns, web de-anon, and the fit gate (added 2026-08)

- **Inbound is a campaign TYPE** (`campaigns.campaign_type`), not a motion filter.
  `signal_query.motion` decides who gets PICKED; the type decides how they are
  WRITTEN TO. `render_type_prompt()` prepends an inbound framing block ahead of the
  brief and the step plan: never cold-open, reference what they actually did, ask
  sooner and shorter. Cold-opening at someone who filled in a form last Tuesday is
  the most damaging thing the agent can do, so this could not stay a filter.
  - An unknown type is **rejected, not coerced**. Coercion was the first
    implementation and is the dangerous default here: a typo'd "inbound" would
    silently produce an outbound campaign and cold-open at people who raised a hand.
- **`web_deanon` — identified website visitors.** Distinct from `website_visit` (a
  known contact returning): this is a de-anonymisation vendor putting a NAME to
  anonymous traffic, i.e. an account researching you that you never knew was there.
  Strength 46, `decay_scale` 4.0 — the strongest behavioural signal and the fastest
  to go stale. Vendors added to the connector catalogue (RB2B, Vector, Warmly,
  Dealfront, Factors.ai, 6sense); none wired yet, so they are roadmap entries that
  the `web_deanon` kind is ready to receive.
- **The fit gate: `min_score` + `require_senior`.** The signal query says the
  ACCOUNT is worth working; the gate says whether this PERSON at it is. Without it a
  qualifying account sweeps in everyone held there and a campaign quietly becomes a
  blast. Applied AFTER scoring on purpose — the score already blends signal
  strength, stacking and persona fit, so it IS the fit measure rather than a second
  opinion about one. Skips are counted as `below_fit` / `not_senior` so the preview
  says why. `_is_senior()` reuses `capacity.SENIOR_ROLES` (not a parallel list) and
  fails OPEN, since a missing taxonomy must not silently empty a campaign.
  Verified: 78 candidates → 72 at score ≥ 45 → 32 at score ≥ 70.

## Campaign Outreach + Replies tabs, and the Find-accounts result (added 2026-08)

- **A campaign now has all five stages on it**: Audience → Find accounts → Sequence
  & offers → Call list → **Outreach** → **Replies**. The copy and the replies always
  existed app-wide, but standing on a campaign and asking "what did we say to these
  people, and what came back?" meant leaving it and filtering by hand.
  `campaign_outreach_payload()` / `campaign_replies_payload()` live in `app.py`
  because they join two things only it holds — campaign membership and the
  generated-copy index / reply queue.
  - Outreach lists members WITHOUT copy too. "Which of these has the agent written
    to?" is the question, and dropping the un-written ones answers it wrongly by
    omission — it would look complete when it was partial.
  - Replies match on EMAIL. A reply arrives from a mailbox and carries no campaign
    id, so the member's address is the only join available; it is exact.
    Deliberately READ-ONLY — drafting and sending stay on the Replies view where the
    guardrails are; this answers "did it land?" and hands over.
  - **Gotcha:** `OutreachIndex` is lazy PER PROFILE and `.rows` is empty until built.
    `query()` calls `maybe_rebuild()`; reading `.rows` directly does not — the first
    request in a demo reported 0 of 70 written when the true answer was 38. Any new
    reader of the index must call `maybe_rebuild()` first.
- **The campaign Outreach table shows campaigns and the MERGED sequence.**
  `campaign_outreach_payload` hand-builds its rows, and the first version dropped
  `all_campaigns` / `overlapping` — which `db.campaign_members()` already returns —
  so a tab where 60 of 70 contacts were in two campaigns showed none of it.
  - A contact in two campaigns does not get two cadences: `touch_plan()` folds every
    campaign's steps into ONE de-conflicted schedule. So "which sequence is this?"
    has a genuinely different answer for them (`merged · 14` vs `single · 7`), and
    the send progress underneath is progress against the merged plan. Computed only
    for OVERLAPPING contacts and capped at 200 — for everyone else the answer is
    this campaign's own step count and touch_plan would be a query per row for a
    known answer.
  - **`tableLayout: fixed` was missing.** Percentage widths are advisory without it,
    so the long signal text stretched its column and skewed the rest — that was the
    "wonky" table, not the data.
- **Outreach is CONTACT-centric, and the links carry their filter.** Membership
  lives on the person, so `outreach_detail` returns every campaign the contact is in
  (`contact_campaign_tags`) plus their engagement state — someone worked by three
  campaigns looks unrelated on each screen unless all three travel with them, which
  is also how a prospect quietly gets triple-touched. The drawer links each campaign
  back to `/campaigns?open=<id>`.
  - `OutreachIndex.query()` gained `contact` and `campaign` filters, so a link FROM a
    campaign lands on the already-filtered slice rather than the whole list plus
    instructions. A bad campaign id resolves to an EMPTY set, not an unfiltered one
    — fail closed.
  - `/outreach?campaign=` and `/replies?campaign=` / `?reply=` both show a removable
    chip saying what they are scoped to. A filtered list that doesn't say it is
    filtered is how someone concludes the copy is missing. Replies scopes client-side
    (the queue is already loaded and the scope is a VIEW, not a different dataset).
- **Find accounts shows what it FOUND, not what it will read.** The scan button no
  longer carries a predicted count (it was a prediction about input; the number that
  matters is the output). After a run, `ScanResult` lists the accounts with signal
  and what was found at each, with the nothing-detected ones collapsed behind a
  toggle — "we looked and there was nothing" is why they won't be re-scanned, so it
  is worth seeing once.
  - `discover()` now returns `results: [{domain, company, found: {kind: line}, any}]`
    via `_scan_result()`, which READS BACK `account_signals` rather than inferring
    from "the runner didn't raise".
  - That fixed a real reporting bug: `detected[kind]` was incremented per
    non-raising call, so twelve accounts with no hiring reported as "12 hiring". It
    now counts real findings, filtered against `NEGATIVE_RESULTS` (the detectors'
    "No signals detected" / "No open roles detected" literals).

**Where the ICP engine lives** (answers the open question in the ICP auto-build
work): `~/Documents/Sales Motion Master` — `.claude/agents/company-researcher.md` is
Agent 1 of its deck pipeline and already does exactly the needed job: from a contact
+ domain it web-researches the company, derives its **ICP** (summary, size band,
industries, geography, why-now triggers) and its **buying group** table
(Industry | Champion | Decision Maker | Economic Buyer), against the fixed shape in
`engine/deck-builder/templates/research.template.md`. It is a Claude Code agent, not
a library, so porting it means re-expressing that prompt through
`anthropic_client.complete(use_web_search=True)` and writing the result into
`buyer_group_roles` — proposing, never auto-applying, like every other configurator
here.

## Filtering + sorting the Use tab's index views (added 2026-08)

The three index views in Use — Campaigns, Hot targets, Call list — are universal
across campaigns by design (a rep works one list, not one per campaign). Shared
controls in `components/tableTools.jsx` narrow and re-order them without giving up
that default: `useSort`/`sortRows`/`SortTh`, `Search`/`Pick`/`Toggle`,
`FilterCount`, plus `facet()` (option lists with counts) and `matcher()` (all-terms
free text). Styling is `.filterbar` / `.f-toggle` / `th.th-sort` in `styles.css`.

- **"No column sort" is a REACHABLE STATE, not the absence of one.** Each list's
  default order is a property of its query that no column can reproduce — the call
  list's account-diverse interleave, `list_campaigns`' work-first ordering, the hot
  list's `fit` ranking. So `useSort` cycles preferred → reversed → **back to
  natural**, and every reset returns there. The Score header says outright that
  sorting by it gives the raw ranking, which clusters one big account at the top.
- **Nulls sort LAST in both directions** (`compare()`), matching what
  `campaign_members` already does in SQL: ascending-by-score otherwise opens on a
  wall of never-scored rows, which is not what "show me the weakest" means.
- **Hot targets stamps the snapshot rank onto the row** before any filter or sort,
  and `#` renders that, not the row position. Re-sorting by buyers must not
  renumber the list — "we're #3 on your list" has to stay true all day.
- **Sorting is client-side, so a capped fetch has to say so.**
  `call_list_payload` returns `total` (count before the limit); the call list fetches
  300, shows "N of M loaded" with a load-all, and marks the band tiles "of N loaded".
  Sorting a truncated page silently answers "the weakest on the list" with "the
  weakest of the strongest 300".
- **Fixed on the way through:** the call list's state filter ran in the BROWSER over
  a response the server had already filtered to `qualified`, so "Enrolled" was always
  empty. It is now a server param — and the every-state value is the sentinel
  `state=all`, because `parse_qs` drops blanks and `state=` arrives indistinguishable
  from an absent param (which means `qualified`). The state tile relabels itself from
  the server count so it stays true in every state.
- **Facets are computed over the unfiltered set** so options never vanish as you
  narrow, and the shown/total count with its reset is always rendered — a quietly
  filtered list is how someone concludes the data is missing.

## Cross-highlighting on Analytics + Trends (added 2026-08)

Both pages stack widgets that are cuts of the SAME rows — a stage bar, a motion
tile and the attributed-deal table are three views of one deal set; a campaign is
a funnel row, a campaign-table row and a chart bar. Reading across them was left
to the eye. `components/crossHighlight.jsx` (`useHighlight` / `SelectionBar` /
`rowProps`, classes `.xh-*` in styles.css) makes picking a value in any widget mark
the matching part of the others.

- **HIGHLIGHT, NEVER FILTER.** A row contributing nothing to the selection is
  itself the finding; dropping it hides that. No number changes — the totals keep
  describing the whole population, which is what makes the marked share readable
  AS a share.
- **A widget only reacts to dimensions it can express** (`on(isMatch, reflects)`).
  Without that, picking a campaign dimmed the funnel's stage totals — page-level
  figures with nothing to say about one campaign, reading as "none of these match".
- **Only clickable where it links somewhere.** On Trends only "By offer type" is
  selectable; seniority/function/intent have no counterpart on the page, and a row
  that highlights only itself teaches people the interaction does nothing.
- **Summaries come from the SERVER aggregate**, never from summing visible rows —
  `aisdr.deals` is capped at the 25 largest, so a row sum silently under-reports.
  The deal table states when it is showing a subset.
- Wired: Outcome (motion ↔ stage ↔ deal, 3-way), Funnel (campaign across both
  tables; a funnel STAGE tints the column it decomposes into via `.col-on`),
  Channels (chart bar ↔ table row), Trends Messaging (offer across the scatter,
  the offer mix and the conversion table). Esc or the strip's Clear exits.

Alignment work that shipped with it: `table { font-variant-numeric: tabular-nums }`
globally plus a `.num` class right-aligning numeric th+td (headers AND cells, or
the label floats off its column); `table.tight` + `.panel-scroll` for tables inside
side-by-side panels — "By priority band" is five columns in ~316px and was
overflowing its panel by 60px, clipped by its neighbour; one `when()` timestamp
format, and the two sync stamps are now NAMED ("Email stats synced" vs "Attribution
synced") because two unlabelled ones read as a contradiction.

## Packaging, buyer group, and ad-hoc reports (added 2026-08)

- **Tier marks** (`webui/server/tiers.py`, `components/Addon.jsx`). Three rungs:
  `core` → `scale` (the **Scale package**: technographic + hiring signal agents,
  plus contact phone reveal) → `advanced` (enrichment credits, scoring, analytics,
  CRM sync). **"Custom setup" is
  a PROPERTY (`custom_setup: True`), not a fourth tier** — several Advanced items
  connect to a customer's own provider/CRM, and modelling that as a flag keeps the
  ladder three ascending rungs.
  Badges are deliberately understated: a small pill by the feature's heading, detail
  in the tooltip. **Never a modal, lock or upsell** — nothing here gates anything.
  `ADVANCED_CREDITS_PER_MONTH` (15,000, env-overridable) is the number the Capacity
  view meters enrichment spend against, so the allowance has one answer in one place.
- **The buyer group is configuration** (`buyer_group_roles`, `buyer_group_config.py`).
  Who we sell to was stated in FOUR hardcoded places that could drift:
  `buyer_group.py`'s regexes, `clay_enrich.JOB_TITLE_KEYWORDS`, its `_IC_TITLE_RE`
  gate, and `capacity.SENIOR_ROLES`. All four now read this ruleset, so editing a
  role changes what enrichment searches for, what survives the gate, which persona
  writes, and who is worth a call — together.
  - **ORDER IS THE LOGIC**: rules run by `sort_order`, first match wins. RevOps sits
    above generic Sales so "Sales Operations" is RevOps; **exclusion is not a special
    case, it is simply rule 0 mapping to `is_icp=0`**.
  - Seeded to reproduce the old classifier exactly — verified 17/17 identical on a
    title corpus. Falls back to `buyer_group.py` when the table is absent.
  - A bad regex typed in the console is rejected at the API, and `_rules()` skips an
    uncompilable pattern rather than failing classification for every other rule.
- **Ad-hoc reports** (`webui/server/reports.py`, `components/Reports.jsx`) — a shared
  "Raw data" tab on BOTH Analytics and Trends: pick a dataset, configure columns, or
  describe the report in English.
  - **The model never writes SQL.** It emits a constrained SPEC (dataset id, column
    ids, operators from an allowlist) validated against the `DATASETS` registry
    before anything runs; the SQL is then built from registry-owned expressions with
    values bound. Unknown dataset/column/operator is **rejected, not sanitised** — a
    filter silently dropped would give a confidently wrong answer. Verified against
    four injection attempts (`sqlite_master`, `;DROP TABLE`, operator injection,
    column injection): all rejected, table intact.
  - Adding a dataset is a dict entry + its column list; it becomes describable,
    filterable and column-configurable with no other change.
  - Demo mode answers `describe` from `report_recipes.json` by keyword and then
    **executes the matched spec against the profile's own data** — computed, not
    canned, so it changes when the demo data does.

- **Contacts link to the CRM everywhere** (`components/ContactLink.jsx`). A contact
  id in this console IS the HubSpot record id, so the only missing piece was the
  portal — `HUBSPOT_PORTAL_ID` produces a URL template served on `/api/tiers` (both
  are app-wide config the client needs exactly once). Without it names simply render
  as text; nothing breaks. **Phone is Scale-tier and reveal-on-click** — a grid of
  phone numbers is not something to leave on a shared screen, and the click is where
  the tier mark belongs. `contacts.phone` / `mobile_phone` are pulled by
  `hubspot_pull.py`; a call recommendation without a number is a to-do, not an action.
- **Home's Recent signals widget is full width and last** (`.home-widget.wide`), with
  a per-account contact drill-down: name → CRM, score, phone. A signal you cannot act
  on from where you read it is a notification, not a work surface.
  **Gotcha:** the drill-down query must `GROUP BY c.contact_id` — joining
  `campaign_members` straight through listed anyone in two campaigns twice.

## Home view (`GET /api/home`)

Widget dashboard: outcome first, then what needs a human, then whether it's improving.

- **One assembled endpoint, not eight parallel calls.** `home_payload()` builds five
  sections (`outcome`, `queue`, `trend`, `pipeline`, `attention`) each inside its own
  try — a broken source nulls that widget and reports under `errors[<section>]`;
  Home always renders.
- **Every widget owns exactly one destination.** A widget with no link belongs on its
  own page instead.
- **Zero states are content, not absences.** "Nothing waiting" renders as a calm green
  result. Distinguish *empty* from *complete*: `signals_total` exists alongside
  `signals_missing` precisely so "no accounts cached yet" can't render as "all
  accounts covered".
- **`attention` renders only when non-empty** and moves to the TOP of the page — a
  dashboard with a permanent health panel trains people to ignore it. It stays honest
  in live mode even though demo mode suppresses failure states elsewhere.
- **Deltas** come from `campaigns_history.jsonl` (`_home_deltas`): newest snapshot vs
  the most recent one ≥6 days older, `None` when there isn't enough history rather
  than inventing a comparison.
- **`_home_headline`** computes the one finding worth surfacing (step trough vs best,
  else offer spread) using the SAME confidence gate as `TrendsCharts.jsx`
  (MIN_INTERESTED=5 / MIN_CONTACTED=200). Keep those thresholds in sync.

## Setup view — connectors + config sections

- **Connectors** (`webui/server/connectors.py`, `GET /api/connectors`): two tiers kept
  deliberately distinct. `integrated: true` = code exists and status is **detected**
  from config (env key present / Clay OAuth `status()` / `built_in` for in-process
  engines); `integrated: false` = catalogue-only roadmap entries (Salesforce,
  ZoomInfo, Apollo, PitchBook, …) with nothing wired to them. Never render the two
  the same way — it would imply a provider is one toggle away.
  In demo mode statuses are **declared** by the profile
  (`data/demo/<id>/connectors.json` → `{"connected": [...]}`, default HubSpot+Clay),
  never probed, so a demo can't leak which credentials the host holds. `built_in`
  stays `built_in` even in demo. Logos are monogram tiles, not brand assets (CSP
  forbids remote assets); drop `src/assets/connectors/<id>.svg` in to replace them.
- **Under the hood is a TAB STRIP**, not accordions (`uth-tabs` in `DiagramPage.jsx`):
  one section rendered at a time, `tab` state, and diagram-node clicks call
  `openSection(id)` which selects the tab and scrolls the strip into view.
- **Chat-driven config editing** (`webui/server/config_edit.py`,
  `components/ConfigChat.jsx`): describe a change or attach a text file → Claude
  proposes a full rewrite of whitelisted files → the UI shows a real unified diff →
  approve writes it. Endpoints: `GET /api/config/scopes`, `GET /api/config/file`,
  `POST /api/config/propose|apply|revert`.
  - **`SCOPES` is the whitelist** — the model never picks the file. `_safe_path()`
    accepts a path only if it exactly matches one of the scope's entries, and it is
    re-checked at write time. Only `knowledge` (offer.md) is `editable: True`;
    `sequencing` is medium-risk and `icp`/`guardrails` are high-risk (they change WHO
    gets contacted) and stay locked, rendering an explanation instead.
  - `propose()` writes nothing. `apply_proposal()` snapshots the previous content to
    `data/config-history/<proposal-id>/*.before` (the volume, so a revert survives a
    restart), appends to `audit.jsonl`, and emits `patch.diff`.
  - Applied edits are **NOT durable across a redeploy** — `.claude/` lives in the
    image. `persistence_note()` says so and the UI shows it; the patch is offered so
    the change can be committed. Making this durable means moving those files to the
    volume or adding a commit flow.
  - Guards verified: no `ANTHROPIC_API_KEY` → 501; non-editable scope → 400; demo mode
    → 409 (the blanket POST guard); expired proposal id → 400; a proposal naming a
    path outside the scope is dropped with a warning, not written.
- **`SectionFrame`** (`OrchestrationSections.jsx`) gives every "under the hood"
  section the same shape: *Controls:* one-liner → body → *To change this:* footer,
  with click-to-copy source-path chips. Section metadata (`controls`, `sources`,
  `editNote`) lives in the `sections` array in `DiagramPage.jsx`. Editing is
  deliberately NOT offered inline — this config lives in versioned files the pipeline
  agents also read, so a form here would fork the truth.
- **Signal config** renders both engines through one `SignalBlock` (status → settings
  → detection scope → effect on copy). Settings come from
  `orchestration_config._signal_settings()`, which reports the effective value and
  whether it came from the environment or a default.
- **Gotcha:** `_signal_settings` is served to the browser, so `_SECRET_ENV_RE`
  (KEY|TOKEN|SECRET|PASSWORD|URL$) forces credential-shaped vars to report
  `set`/`not set` only. Never echo an env value into this payload without checking
  that regex.
- **Gotcha:** `GET /api/demo/profiles` is the ONE endpoint that tolerates an unknown
  `X-Demo-Profile` (returns 200 with the header ignored). It's the call the client
  uses to discover its stored profile is gone; 400-ing it would wedge the console on
  a deleted profile with no route back through the UI. Every other endpoint 400s.

## Background jobs (daemon threads started in `app.py main()`)

1. `_activity_autosync_loop` — hourly: logs new email/LinkedIn activity to HubSpot.
2. `_aisdr_sync_loop` — nightly midnight ET: deal attribution (above).
3. HeyReach webhook drain — near-real-time LinkedIn activity logging.
4. `_unenrollment_loop` — every 30 min: everworker_tag suppression sweeps (above).
5. `_campaign_sweep_loop` — hourly: rolling campaign re-qualification (above). Local
   writes only; never calls an external service.

## HubSpot notes

- The app's own token (`HUBSPOT_ACCESS_TOKEN`, private app, EU portal 144358290 — but API
  host is always `api.hubapi.com`) is the source of truth for API capability.
- The HubSpot **MCP** connector available in Claude sessions has narrower scopes: it can
  read/search deals & contacts but CANNOT read email engagements or run SQL
  (`reporting-base-read` missing). Use it for spot-checks; use the app's scripts for real
  work.
- Custom properties in the portal: deals `ai_sdr_deal_created` (bool); contacts
  `ai_sdr_deal_created`, `ai_sdr_meeting_booked`, `ai_sdr_reply_generated` (+
  `ai_sdr_status`, `ai_sdr_errors`), and `everworker_tag` (RevOps-maintained
  enumeration `"true"`/`"false"`; `"false"` = do-not-contact, drives the
  unenrollment checker). Contact LinkedIn URL property =
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
