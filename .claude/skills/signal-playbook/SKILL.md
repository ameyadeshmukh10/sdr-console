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
   validated with `node deck-renderer/scripts/validate-deck-data.mjs`. All generated
   text is Unicode-sanitized (web-scraped thin/no-break spaces and soft hyphens render
   as gaps inside words on the slides). Length/count overruns (the model can't count
   characters) are clamped mechanically — at the last complete sentence when possible,
   else at a word boundary with dangling connectors stripped — `==highlight==` markers
   kept balanced, each fix surfaced in the job log; one LLM repair round-trip handles
   structural issues only. Outreach copy is prompted SHORT (email 200-280 chars,
   LinkedIn 150-250, always ending on a question or offer) — budgets are ceilings.
3. **render** — `npm --prefix deck-renderer run deck` (Vite single-file HTML; no PDF —
   the interactive page renders in the viewer's own browser), serialized via
   `deck-renderer/.deck.lock` → `data/signal-plays/<slug>/<Company>-AI-SDR-Playbook.html`.
4. **publish** — the HTML is baked into a per-play coded template
   (`templates/signal-plays/<slug>.html` via the CMS source-code API, content wrapped in
   `{% raw %}` with HubSpot's standard include tags added), then a website page is
   created/updated at slug `signal-plays/<slug>-ai-sdr-playbook` and pushed LIVE
   (POST /cms/v3/pages/site-pages/{id}/draft/push-live — live within seconds). Rebuilds
   republish the same URL. Requires the **content scope** on HUBSPOT_ACCESS_TOKEN.
   `SIGNAL_PLAY_DOMAIN` overrides the publish domain; `SIGNAL_PLAY_NOINDEX=0` allows
   indexing. Flags `url_domain_ok=false` if the live URL isn't on everworker.ai.
5. **draft** — contextualized reply grounded in the ai-sdr knowledge + the thread + the
   play, embedding the live page URL → merged into
   `data/interested-replies/followup_drafts.json` (`agent: "signal-playbook"`) for the
   normal edit-before-send approval flow.

Render/publish failures fall back to a standard-style draft (no link) so the SDR always
gets something to send.

## Usage

```bash
python3 .claude/skills/signal-playbook/scripts/build_play.py \
  --reply-id 12345 \
  --contact-json '{"firstName":"Mike","lastName":"Bush","jobTitle":"Director, Strategic Sales","businessEmail":"mike.bush@datadynamicsinc.com","companyName":"Data Dynamics","companyDomain":"datadynamicsinc.com"}' \
  [--skip-publish]
```

Progress stages stream on stderr (`stage: research|deck-data|render|publish|draft`);
the final JSON result is stdout's last line. The webui triggers this via
`POST /api/replies/followup/regenerate {reply_id, agent: "signal-playbook"}` and polls
`GET /api/replies/playbook/status/<job_id>`.

Env: `ANTHROPIC_API_KEY` (research/data/draft), `HUBSPOT_ACCESS_TOKEN` with the content
scope (page publish), `SIGNAL_PLAY_DOMAIN` / `SIGNAL_PLAY_NOINDEX` (optional).
