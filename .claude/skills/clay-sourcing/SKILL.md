---
name: clay-sourcing
description: Source net-new GTM-leadership contacts (CRO, VP Sales, SDR/BDR Manager, Director of RevOps, RevOps, Sales Ops) at a HubSpot company list via the Clay MCP, dedup against HubSpot, create them in HubSpot + a static list, and feed them into the SDR pipeline split evenly across the 3 instruction-set variants. Use when the user wants to grow the outbound list from a set of target companies.
---

# Clay sourcing → HubSpot → pipeline (3-variant split)

Clay is **MCP-only** (no REST key), so the contact-finding runs through the Clay MCP and must be
driven by you (Claude). HubSpot writes + the pipeline are handled by Python scripts in
`.claude/skills/sdr-pipeline/scripts/`. Run from the project root.

## Inputs
- A **HubSpot company list ID** (its members are companies), OR a list of company domains directly.
- Optional: a name for the new HubSpot list, and how many companies to process this run.

## Steps

1. **Get company domains.** If given a HubSpot company list ID:
   `python3 .claude/skills/sdr-pipeline/scripts/hubspot_company_domains.py <list_id>`
   → JSON `[{company_id, name, domain}]`. Drop entries with `domain: null` (log them). Clay needs
   domains, not names. **Cap** at ~25 companies per run unless the user asks for more (Clay credits).

2. **Find contacts via the Clay MCP — one call per domain.** Use
   `mcp__48bc72f4-64a3-448c-bf77-4f473c7b118a__find-and-enrich-contacts-at-company` with:
   - `companyIdentifier`: the domain (e.g. `"acme.com"`).
   - `contactFilters.job_title_keywords`: `["CRO","Chief Revenue Officer","VP of Sales","Head of Sales","Director of Sales","SDR Manager","BDR Manager","Sales Development Manager","Director of Revenue Operations","Revenue Operations","RevOps","Sales Operations","Sales Ops"]` (values OR together).
   - `contactFilters.locations`: `["United States"]`.
   - `contactFilters.job_title_exclude_keywords`: `["Intern","Assistant"]`.
   - `dataPoints.contactDataPoints`: `[{ "type": "Email" }]` (we need work emails to enroll).
   The tool is **async**: it returns a `taskId`. Poll with
   `mcp__48bc72f4-64a3-448c-bf77-4f473c7b118a__get-task-context` until the contacts + emails are
   resolved before reading results. Collect each contact's name, title, email, company, domain, and
   LinkedIn URL.

3. **Assemble candidates** into a JSON file, e.g. `/tmp/clay-candidates.json` — a flat list of
   `{first_name,last_name,title,email,company,domain,linkedin_url}` objects (the script also accepts
   `firstName`/`jobTitle`/`linkedin` key variants).

4. **Dedup + create + list + ingest** (one command, deterministic):
   `python3 .claude/skills/sdr-pipeline/scripts/source_contacts.py /tmp/clay-candidates.json --list-name "AI SDR Sourced <date>"`
   This: drops no-email + duplicate rows, keeps only ICP titles (via `buyer_group`, assigning persona),
   **dedups against HubSpot by email** (drops anyone already there), assigns each net-new contact a
   **variant round-robin** (value-give / earn / show), **creates** the net-new contacts in HubSpot,
   makes a **static HubSpot list** and adds them, writes `data/outreach/sourced-<list>.jsonl`, and runs
   `sdr_batches init` so they land in the pipeline as pending batches with their variant set.
   (Add `--no-ingest` to stop before the pipeline, or `--no-hubspot` to dry-run the jsonl only.)

5. **Report** the summary it prints: candidates, ICP, already-in-HubSpot, net-new, per-variant counts,
   the new HubSpot list id, and the new pending batches. Tell the user to open the **Pipeline** tab and
   **Generate copy** (the per-contact variant is honored automatically — each contact is written with
   its assigned instruction set) then **enroll** (each routes to its variant's Bison campaign 14/15/16).

## Notes
- Idempotent: re-running on the same companies creates no duplicates (dedup catches contacts now in HubSpot).
- Clay enrichment costs credits per company + per email; keep the company cap sensible and tell the
  user roughly how many companies you're about to enrich before a large run.
- Contacts whose email can't be resolved by Clay are dropped (can't be enrolled).
