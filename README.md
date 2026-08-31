# SDR Console

Autonomous outbound pipeline + a web console for ** SDR AI Worker**.

Pull an ICP contact list from HubSpot, route each contact to a job-title persona agent
that researches a recent company signal and writes value-first email + LinkedIn copy, then
enroll into Email Bison campaigns by persona — all observable from a local React UI.

Repo: <https://github.com/ameyadeshmukh10/sdr-console> (private)

## Layout

| Path | What |
|------|------|
| `.claude/skills/` | The pipeline logic: persona sub-agents, copy linter, HubSpot/Bison/HeyReach clients, the `sdr-pipeline` orchestrator and `sdr-batches` batch runner. |
| `webui/` | Local web console (React + Vite frontend, zero-dependency Python stdlib backend). See [`webui/README.md`](webui/README.md). |
| `data/` | A real pipeline snapshot: ICP contacts, 2,000+ generated outreach sequences, interested-reply threads + analysis, campaign stats, and the SQLite pipeline DB. |
| `USAGE.md` | How to run the pipeline scripts from the CLI. |
| `CLAUDE.md` | Project context for Claude sessions: architecture, Railway deployment topology, MongoDB attribution store, operational gotchas. |
| `openapi.json` | Email Bison API reference. |

## Quick start

```bash
git clone https://github.com/ameyadeshmukh10/sdr-console.git
cd sdr-console
cp .env.example .env          # fill in your HubSpot / Bison / HeyReach keys
./webui/run.sh                # build + serve the console at http://localhost:8787
#   or: ./webui/run.sh dev    # hot-reload dev (UI :5173, API :8787)
```

The console opens against the bundled `data/` snapshot, so every page has real content on
first run — no pipeline run required to explore it.

## The console (6 pages)

**Use** (ingest a HubSpot list) · **Pipeline** (live batch progress + enroll with a dry-run
gate) · **Orchestration** (the agent→campaign diagram) · **Analytics** (campaign reply /
interested rates + AI SDR deal attribution: deals created + total pipeline, synced nightly
from HubSpot into MongoDB) · **Trends** (what's working across interested replies) ·
**Outreach** (browse every generated sequence by persona / CTA / signal / status).

## Deployment

Deployed on **Railway** as a single service (Docker; deploys on push to `main`), with a
Railway Volume at `/app/data` (live pipeline data — the committed `data/` is only a
first-boot seed) and a MongoDB service (`MONGO_URL`) backing the AI SDR deal-attribution
analytics. See `CLAUDE.md` for the full topology.

## Running the pipeline yourself

With a valid `.env`, generate and enroll a fresh batch from Claude Code:

```bash
/sdr-batches <N> enroll        # process N pending batches and enroll into Bison
```

See [`USAGE.md`](USAGE.md) for the underlying `sdr_batches.py` / `hubspot_pull.py` /
`fetch_*` scripts.

## Secrets

`.env` (live HubSpot / Bison / HeyReach API keys) is the **only** thing gitignored — copy
`.env.example` and fill it in. Everything else, including the `data/` snapshot, is in the
repo. The repo is private; treat the prospect data in `data/` accordingly.
