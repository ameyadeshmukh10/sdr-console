---
name: signal-playbook
description: Signal Playbook Reply Agent — build a personalized signal play (research → deck-data → HTML+PDF via the vendored deck-renderer), host the PDF publicly via HubSpot File Manager, and draft a contextualized follow-up reply embedding the link. Used by the Replies view's agent dropdown; runs as an async job in the webui.
---

# Signal Playbook Reply Agent

Turns an interested reply's lead into a personalized "signal play" and a ready-to-edit
follow-up email around it.

## Pipeline (scripts/build_play.py)

1. **research** — one Anthropic Messages API call with web search (adapted from the
   SDR-Playbook-Multi-Agent-System company-researcher agent) → `data/signal-plays/<slug>/research.md`
   following `templates/research.template.md`.
2. **deck-data** — research.md → `deck-data.json` per `schemas/deck-data.schema.json`,
   validated with `node deck-renderer/scripts/validate-deck-data.mjs` (one repair round-trip).
3. **render** — `npm --prefix deck-renderer run deck` (Vite single-file HTML + Playwright
   PDF), serialized via `deck-renderer/.deck.lock` → `data/signal-plays/<slug>/<Company>-AI-SDR-Playbook.{html,pdf}`.
4. **upload** — `HubSpotClient.upload_file` (files/v3, PUBLIC_INDEXABLE, folder
   `signal-plays`) → public URL. Requires the **files scope** on HUBSPOT_ACCESS_TOKEN.
   Flags `url_domain_ok=false` if the portal serves files from a non-everworker.ai host
   (fix: HubSpot Settings → Content → Domains & URLs → connect a file-hosting subdomain).
5. **draft** — contextualized reply grounded in the ai-sdr knowledge + the thread + the
   play, embedding the URL → merged into `data/interested-replies/followup_drafts.json`
   (`agent: "signal-playbook"`) for the normal edit-before-send approval flow.

Render/upload failures fall back to a standard-style draft (no link) so the SDR always
gets something to send.

## Usage

```bash
python3 .claude/skills/signal-playbook/scripts/build_play.py \
  --reply-id 12345 \
  --contact-json '{"firstName":"Mike","lastName":"Bush","jobTitle":"Director, Strategic Sales","businessEmail":"mike.bush@datadynamicsinc.com","companyName":"Data Dynamics","companyDomain":"datadynamicsinc.com"}' \
  [--skip-upload]
```

Progress stages stream on stderr (`stage: research|deck-data|render|upload|draft`);
the final JSON result is stdout's last line. The webui triggers this via
`POST /api/replies/followup/regenerate {reply_id, agent: "signal-playbook"}` and polls
`GET /api/replies/playbook/status/<job_id>`.

Env: `ANTHROPIC_API_KEY` (research/data/draft), `HUBSPOT_ACCESS_TOKEN` with files scope
(upload), `DECK_CHROMIUM_PATH` (optional Chromium override for the PDF export).
