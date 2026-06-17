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

### Enrollment safety
Live enrollment is the one outward write in the UI. It is gated three ways: a dry-run
preview is always shown first; the live button opens a confirm modal with an explicit
acknowledgement checkbox; and the backend rejects `POST /api/enroll/live` unless the body
carries `confirm: true`. Per-lead Bison rejections (already-in-sequence, bounced, etc.)
are surfaced as skips, mirroring the CLI.

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
