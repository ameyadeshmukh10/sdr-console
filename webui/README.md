# SDR Console — local web UI (MVP)

A local web UI over the existing SDR outbound pipeline. Read-only except for the
HubSpot ingest action. Reuses the existing Python pipeline scripts via subprocess;
the backend is Python stdlib only (no pip).

## Run

From the project root:

```bash
# One process, no Node at runtime: builds the frontend, then Python serves UI + API.
./webui/run.sh                 # -> http://localhost:8787

# Hot-reload dev: Python API on :8787 + Vite dev server on :5173.
./webui/run.sh dev             # -> http://localhost:5173
```

Manual equivalents:

```bash
python3 webui/server/app.py --port 8787          # API (+ static dist if built)
npm --prefix webui/frontend install              # once
npm --prefix webui/frontend run build            # produce dist/
npm --prefix webui/frontend run dev              # dev server with /api proxy
```

## Pages

1. **Use** (`/`) — **search HubSpot lists** (contact *or* company) and pick one, or type a
   list ID directly. A **contact list** runs `hubspot_pull.py` + `sdr_batches.py init` (shows
   new contacts/batches + pending queue). A **company list** opens the Clay enrichment panel
   (see *Clay buying-group enrichment* below). Copy generation happens on the Pipeline tab or
   via `/sdr-batches` in Claude Code.
2. **Pipeline** (`/pipeline`) — **real-time batch progress** (polls `/api/progress`
   every 2.5s with an auto-refresh toggle) while `/sdr-batches` runs in Claude Code,
   plus **enrollment with a dry-run gate**: preview the planned routing first, then a
   confirm modal before the live write to Bison (`sdr_batches.py enroll`).
3. **Orchestration** (`/diagram`) — SVG of HubSpot → 4 persona sub-agents → Bison
   campaigns 10–13, with live contact counts. Campaign stats overlay when a cached
   snapshot exists for that campaign id.
4. **Analytics** (`/analytics`) — campaign leads / contacted / reply rate / interested
   rate from cached `data/campaign-stats/`. The **Refresh** button re-runs
   `fetch_campaign_stats.py` to pull live from Bison.
5. **Trends** (`/trends`) — interested-reply deep dive (seniority, function, winning CTA,
   reply intent, offer type, conversion-by-campaign, reply cohorts) from the cached
   `interested-trends` analysis. **Refresh** re-runs `fetch_interested_replies.py` +
   the `analyze_*.py` chain.
6. **Outreach** (`/outreach`) — search/filter/group the generated sequences by persona,
   CTA play, status, company; click any lead for the full 4-touch email + LinkedIn copy.

7. **Replies** (`/replies`) — unified interested-reply triage across **email (Bison)** and
   **LinkedIn (HeyReach)**, with a channel badge on every card and a channel filter. **Scan**
   classifies each inbound reply with Claude — email via `classify_replies.py` (Bison master
   inbox), LinkedIn via `classify_li_replies.py` (HeyReach conversations where the lead messaged
   us last). Each card carries the lead, the sending account/inbox, and the messages we sent.
   - **Email:** review the possible interested → **Tag in Bison** (gated: `mark-as-interested`
     + the Interested tag, id 11) → draft → **Approve** sends the reply in the prospect's Bison
     thread.
   - **LinkedIn:** interested replies skip straight to draft (no Bison tag) → **Approve** sends
     via HeyReach `SendMessage` from the LinkedIn sender that got the reply, to that lead.
   Drafts are channel-aware (`draft_followups.py` writes shorter, DM-shaped copy for LinkedIn).
   On send the card clears (`sent_followups.json`); a fresh reply reappears on the next scan.
   The campaign filter narrows only the email side — LinkedIn always scans `HEYREACH_CAMPAIGN_ID`.
8. **Signals** (`/signals`) — the per-company research cache. Research is account-level, so it's
   cached by email domain in `account_signals` (pipeline.db) and reused for **90 days** — a company
   is web-searched once instead of once per contact / per re-run. Browse cached accounts (domain,
   signal, recent vs fallback, age in days) and **force-refresh** one to re-search on demand.

### Cost: Message Batches API (50% off, async)
The Pipeline tab also has a **Batch API** panel: submit N pending batches to Anthropic's Message
Batches API at **half price**. It's asynchronous — you submit and check back (most finish in
minutes, 24h cap). Each contact is one batched request (cache-aware: already-known companies are
cheap write-only requests, new ones research+write with web search; 1-hour prompt cache). Jobs are
persisted to `data/outreach/batch-jobs/<id>.json` and **survive a server restart** (the poller
resumes); on completion the results are linted, signals cached, lint-failures retried synchronously,
and the batches ingested. `POST /api/generate/batch {limit}` · status/list/cancel endpoints. Use the
real-time **Generate copy** button when you need it now; use the Batch API to save 50% when you can wait.

### Cost: per-company signal cache + company-grouped batching
Web-search result tokens dominate generation cost. To cut them: (a) `assign_batches` groups
same-company contacts into the same batch; (b) generation looks up a fresh cached signal first —
**cache hit** → write-only call, no search; **miss** → research once per company (per-domain lock),
store the signal. Re-runs/redos and overlapping pulls then hit the cache → zero searches for any
company researched in the last 90 days. The job log marks each contact `[cached]` vs `[searched]`.

### UI-triggered copy generation (Anthropic API)
The **Pipeline** page can generate a pending batch's copy without Claude Code: click **Generate
copy** on a pending batch → a background job (`generate_batch.py`) calls the Claude API per contact
with the server-side `web_search` tool + the ai-sdr knowledge base, forces the JSON schema, and
validates with the **same** linter `ingest` uses (retry ×2 on failure). The `GenerateJobPanel`
shows each contact move `researching → done/failed` live with web-search counts and a log; results
are recorded via `ingest`. One job at a time; cancellable. Model = `CLAUDE_MODEL` (default
`claude-opus-4-8`). **Everything is stdlib urllib — no `anthropic` SDK, no pip.**

### HubSpot list search (Use tab)
The Use tab can **search HubSpot lists by name** instead of needing a list ID. A
contact/company toggle scopes the search (`objectTypeId` `0-1` contacts, `0-2` companies).
Backend: `search_lists()` on `HubSpotClient` (`POST /crm/v3/lists/search`, requests
`hs_list_size`) → `hubspot_lists.py search "<q>" [--type contact|company]` →
`GET /api/hubspot/lists?q=&type=`. The manual list-ID input stays as a fallback.

### Clay buying-group enrichment (no Claude Code)
Select a **company list** → **Enrich buying group** sources GTM-leadership contacts at each
company via Clay, **without** running through Claude Code:
- The backend drives **Clay's MCP server** (`https://api.clay.com/v3/mcp`) through the
  **Anthropic Messages API MCP connector** (`mcp_servers` + `anthropic-beta: mcp-client-2025-04-04`)
  with a cheap model (`CLAUDE_SOURCING_MODEL`, default `claude-haiku-4-5`). `clay_enrich.py`
  resolves the list's company domains, calls `find-and-enrich-contacts-at-company` per domain,
  and **polls `get-task-context`** (Python-orchestrated cadence) until emails resolve.
- The resulting candidates flow through the existing **`source_contacts.py`** (dedup within +
  against HubSpot, ICP/persona filter, 3-way variant round-robin, create contacts + a static
  list, ingest into the pipeline) — fully reused, unchanged.
- **Clay auth is OAuth 2.1** (no static key). `clay_oauth.py` does discovery → dynamic client
  registration → PKCE authorize → token exchange/refresh; **Connect Clay** in the UI runs the
  one-time browser flow. Tokens are stored in `data/outreach/clay_oauth.json` (gitignored,
  `chmod 600`) and auto-refresh. Endpoints: `GET /api/clay/status`, `/api/clay/oauth/start`,
  `/api/clay/oauth/callback`.
- Runs as a **background job** (`POST /api/source/enrich {list_id,list_name,cap,mode}`,
  status at `/api/source/status/<id>`) with two modes: **end-to-end** (commit immediately) or
  **review** (pause on the candidate list, then `POST /api/source/confirm/<id>` to create).

### Instruction-set variant % split (Pipeline tab → Batch API)
The A/B panel has a **Single variant ↔ Split %** toggle. In split mode you set the percentage
per variant (value-give / earn / show, must total 100); on **Batch API** submit the selected
contacts are distributed by those proportions — largest-remainder for exact counts + even
interleaving so variants aren't clustered in the domain-sorted order. The per-contact variant
flows through `prepare_batch_requests` → `ingest` → enroll, so analytics and campaign routing
stay correct. `POST /api/generate/batch {limit, variant, split}`; omit `split` for the
single-variant behavior.

### New env vars (.env)
```
ANTHROPIC_API_KEY=sk-ant-...           # required for generation + reply classification + Clay sourcing
CLAUDE_MODEL=claude-opus-4-8           # model for generation + classification
CLAUDE_SOURCING_MODEL=claude-haiku-4-5 # cheap model that drives Clay MCP enrichment
CLAY_MCP_URL=https://api.clay.com/v3/mcp                 # Clay MCP endpoint (OAuth 2.1)
# CLAY_OAUTH_REDIRECT=...                # optional; auto-derived from the request host when unset
BISON_INTERESTED_TAG_ID=11             # the "Interested" tag applied on approval
```
Clay tokens are **not** in `.env` — they live in the gitignored `data/outreach/clay_oauth.json`,
populated by the one-time **Connect Clay** OAuth flow.

The **Clay OAuth callback URL** is derived per-request from the host the console is
served on (using the proxy's `X-Forwarded-Proto`/`X-Forwarded-Host`), so when the app
is deployed Clay redirects back to the deployed URL — not `localhost`. Set
`CLAY_OAUTH_REDIRECT` only to pin a fixed URL for local dev or non-standard setups.

### Network egress (running in a hosted/web environment)
The backend makes **direct** HTTPS calls to these hosts — they must be reachable (allowlisted in
a hosted environment; no issue when running locally):
- `api.anthropic.com` — generation, reply classification, and the Clay MCP connector. **The Clay
  *enrichment* calls ride on this host** (Anthropic's servers connect to Clay), so the backend
  itself does not need `api.clay.com` for enrichment.
- `api.hubapi.com` — all HubSpot calls (list search, pull, company domains, contact create).
- `api.clay.com` — **only** the Clay OAuth handshake/refresh (`clay_oauth.py`).

If a host is blocked the relevant endpoint fails with a clear JSON error (e.g. Clay OAuth start
returns `could not discover OAuth metadata …`). See
<https://code.claude.com/docs/en/claude-code-on-the-web> for editing a web environment's egress.

### Outward-write safety
Two outward writes, both gated the same way (dry-run/preview first → confirm modal with an
acknowledgement checkbox → backend requires `confirm: true`):
- **Enrollment** (`POST /api/enroll/live`) — push leads into Bison campaigns.
- **Reply tagging** (`POST /api/replies/tag`) — mark interested + apply the Interested tag.
Per-item Bison rejections are surfaced as skips/failures and never abort the rest. Copy generation
writes only local files + the pipeline DB (via `ingest`); it never sends anything.

## Notes

- **SQLite** is opened read-only (`mode=ro`); the UI never writes to `pipeline.db`.
- The ~2k generated files are indexed once in memory at startup; `POST /api/reindex`
  (or a generated-dir mtime change) rebuilds it.
- **CTA play** is derived (no explicit field): the copy is persona-templated and nearly
  every sequence uses a "signal play" opener + "playbook" close, so we label each
  sequence by its most *distinctive* (rarest) give — `pipeline-model` (sales-leadership
  default), `outbound-teardown` (revops), `personalized-drafts` (sdr-bdr), `benchmark`.
- ~19% of contacts have a blank company in `contacts.jsonl` (HubSpot source data);
  populated companies are sorted first.
- The cached campaign stats are from a Bison instance whose campaign ids don't all match
  the `.env` 10–13 mapping, so diagram stat overlays may be blank until a live refresh.
