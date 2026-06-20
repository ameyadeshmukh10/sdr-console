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

1. **Use** (`/`) — enter a HubSpot list ID → runs `hubspot_pull.py` + `sdr_batches.py init`,
   shows new contacts/batches + pending queue. Copy generation still happens via
   `/sdr-batches` in Claude Code (the UI does not generate copy).
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

7. **Replies** (`/replies`) — interested-reply detection. **Scan** the Bison inbox (lookback +
   optional campaign) → each inbound reply is classified by Claude (`classify_replies.py`) into a
   review queue → select the flagged ones → **Tag in Bison** (gated) applies `mark-as-interested`
   + the Interested tag (id 11) to each lead.
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

### New env vars (.env)
```
ANTHROPIC_API_KEY=sk-ant-...      # required for generation + reply classification
CLAUDE_MODEL=claude-opus-4-8      # model for both
BISON_INTERESTED_TAG_ID=11        # the "Interested" tag applied on approval
```

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
