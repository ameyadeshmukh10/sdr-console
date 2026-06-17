# SDR Console

Autonomous outbound pipeline + a local web console for **EverWorker's SDR AI Worker**.

Pull an ICP contact list from HubSpot, route each contact to a job-title persona agent
that researches a recent company signal and writes value-first email + LinkedIn copy, then
enroll into Email Bison campaigns by persona — all observable from a local React UI.

## Layout

| Path | What |
|------|------|
| `.claude/skills/` | The pipeline logic: persona sub-agents, copy linter, HubSpot/Bison/HeyReach clients, the `sdr-pipeline` orchestrator and `sdr-batches` batch runner. |
| `webui/` | Local web console (React + Vite frontend, zero-dependency Python stdlib backend). See [`webui/README.md`](webui/README.md). |
| `USAGE.md` | How to run the pipeline scripts from the CLI. |
| `openapi.json` | Email Bison API reference. |

## Quick start

```bash
cp .env.example .env          # fill in your HubSpot / Bison / HeyReach keys
./webui/run.sh                # build + serve the console at http://localhost:8787
#   or: ./webui/run.sh dev    # hot-reload dev (UI :5173, API :8787)
```

## The console (6 pages)

**Use** (ingest a HubSpot list) · **Pipeline** (live batch progress + enroll with a dry-run
gate) · **Orchestration** (the agent→campaign diagram) · **Analytics** (campaign reply /
interested rates) · **Trends** (what's working across interested replies) · **Outreach**
(browse every generated sequence by persona / CTA / signal / status).

## Not in this repo

`.env` (API keys) and `data/` (live prospect PII, generated outreach, reply threads, the
pipeline SQLite DB) are gitignored. Provide your own `.env` and run the pipeline to populate
`data/` locally.
