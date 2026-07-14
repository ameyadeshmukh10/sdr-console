<!--
  RESEARCH FILE TEMPLATE — Agent 1 (company-researcher) output.
  Fill every section. Keep the headings and order EXACTLY as below: Agent 2 parses
  this file to build deck-data.json, and the deck has fixed-size cards.

  TWO COMPANIES, never conflated:
    • PROSPECT = the company the input contact works at (EverWorker's buyer).
      → drives the Cover + "The Research" slide (offering / ICP / buying group).
    • TARGET   = a real account that fits the PROSPECT's ICP, that EverWorker's AI SDR
      would prospect into on the prospect's behalf.
      → drives "The Play" + "Outreach" slides (signals / contacts / messages).

  Cite sources inline as [n] and list them under SOURCES. Mark anything not directly
  confirmed as (inferred). Wrap punchy metrics/phrases in ==double-equals== in the
  offering bullets and outreach drafts — that becomes the deck's emerald highlight.
-->

# AI SDR Playbook Research — {{Prospect Company}}

**Input contact:** {{First Last}} · {{Job Title}} · {{business email}}
**Prospect company:** {{Company}} · {{domain}}
**Prepared:** {{YYYY-MM-DD}}

---

## 1 · PROSPECT — what they sell  *(→ Cover wordmark + Research slide · Offering)*

- **Prospect wordmark (cover):** {{short brand name, ≤24 chars}}
- **Cover tagline:** AI SDR Customized to your GTM  *(default; only change with reason)*
- **Offering headline (≤70):** {{one line on what the prospect sells}}
- **Proof bullets (2–3, each ≤52, ==highlight== the number):**
  - {{e.g. Built in ==under 14 minutes==}}
  - {{...}}
  - {{...}}
- **Flagship product:**
  - Name (≤24): {{...}}
  - Description (≤110): {{...}}
  - Tags (1–2, ≤22 each): {{...}}, {{...}}
  - Trusted by (≤44): {{2–3 marquee customers}}

## 2 · PROSPECT — ICP  *(→ Research slide · ICP)*

- **ICP summary (≤95):** {{one line on who they sell to}}
- **Size (≤56):** {{employee count / multi-site / revenue band}}
- **Industries (2–4, ≤16 each):** {{...}}
- **Geography:** {{regions}}
- **Why now (2–3, ≤52 each):** {{in-market triggers}}

## 3 · PROSPECT — case studies / customer stories

<!-- Find under Resources / Customers / footer nav. One row per study. If none found,
     say "No public case studies found" and reason the buying group from first principles. -->

| Solution purchased | Customer | Outcome | Problem solved | Quoted contact title(s) |
| --- | --- | --- | --- | --- |
| {{...}} | {{...}} | {{...}} | {{...}} | {{...}} |

## 4 · PROSPECT — buying group  *(→ Research slide · Buying Group)*

<!-- Ground in the quoted titles above where possible; else reason it. Exactly 3 tiers. -->

| Industry | Champion | Decision Maker | Economic Buyer |
| --- | --- | --- | --- |
| {{...}} | {{title(s)}} | {{title(s)}} | {{title(s)}} |

- **Champion** — owns the outcome: {{titles, ' · ' separated, ≤72}}
- **Decision Maker** — owns the org: {{titles, ≤72}}
- **Economic Buyer** — owns the budget: {{titles, ≤72}}

## 5 · SIGNAL BRAINSTORM — what "in-market" looks like for this ICP

<!-- Brainstorm before searching. These guide the target hunt in §6. -->

- **News:** {{events that signal fit/intent}}
- **Hiring:** {{roles whose opening signals fit/intent}}
- **Technographic:** {{tools whose presence/absence signals fit/intent}}

---

## 6 · TARGET ACCOUNT — {{Target Company}}  *(→ The Play slide)*

<!-- Pick ONE real company that fits the PROSPECT's ICP and is in-market now. -->

- **Name (≤40):** {{...}}
- **Ticker (≤14, optional):** {{e.g. NYSE: XXX — omit if private}}
- **Blurb (≤62):** {{industry · category · HQ}}
- **Domain:** {{target.com}}
- **Three headline stats:** {{value}} {{label}} | {{value}} {{label}} | {{value}} {{label}}
- **Why it cleared the ICP gate (≤220):** {{...}}

### 6a · News signals  *(2026 only, within 90 days of 2026-06-30; cite + date each)*
- {{label ≤24}} — {{detail ≤100}} [n]

### 6b · Hiring signals  *(cite the job posting/source)*
- {{label ≤24}} — {{detail ≤100}} [n]

### 6c · Technographic signals  *(TARGET: inferred from public evidence · PROSPECT: detector line in your input)*
<!-- The input contact profile carries a "Detected tech stack (prospect; …)" line produced by
     .claude/skills/sdr-pipeline/scripts/tech_signals.py (deterministic website/DNS scan —
     trust it verbatim). Use it for prospect-side context (offering, ICP, outreach angles).
     For the TARGET, infer tech here from public evidence (job postings, site markup, docs,
     partner pages); after this file is produced, the pipeline appends a verified
     "6c-verified · Technographic scan (TARGET)" block — deck-data prefers that over
     inferences when both exist. -->
- {{label e.g. "Technographic · <Tool>" ≤24}} — {{detail ≤100, tie the tool to the prospect's wedge}}

> Prospect detector output (copy the input line):
> ```
> {{the provided "Detected tech stack" line, or "unavailable"}}
> ```

**Signal stack for the deck (3–5 total, mix of the above), each tagged with a kind
[expansion | hiring | tech | program | news]:**
1. ({{kind}}) {{label}} — {{detail}}
2. ...

## 7 · TARGET — buyer contacts  *(→ The Play slide · resolved, first 2 also get outreach)*

<!-- 2 (ideal) to 3 real people in the buying group AT THE TARGET. Web-search names +
     LinkedIn. Email: published or company-pattern (mark (inferred) if pattern-derived). -->

| # | Name | Title | LinkedIn URL | Email | Source / confidence |
| --- | --- | --- | --- | --- | --- |
| 1 | {{...}} | {{...}} | {{https://...}} | {{...}} | {{...}} |
| 2 | {{...}} | {{...}} | {{https://...}} | {{...}} | {{...}} |

## 8 · OUTREACH DRAFTS  *(→ Outreach slide — one email + one LinkedIn per contact 1 & 2)*

<!-- Ground every claim in a §6 signal. ==highlight== the punchy phrases. -->

### Contact 1 — {{Name}}
- **LinkedIn (≤340):** {{...}}
- **Email subject (≤44, lowercase):** {{...}}
- **Email body (≤430):** {{...}}

### Contact 2 — {{Name}}
- **LinkedIn (≤340):** {{...}}
- **Email subject (≤44):** {{...}}
- **Email body (≤430):** {{...}}

---

## SOURCES
1. {{url}}
2. {{url}}
