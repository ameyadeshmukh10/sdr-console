# ICP Email Generation Rules (email channel, GTM-leadership buyers)

How to write a 4-touch outbound email sequence for an ICP lead. Grounded in `offer.md`
(product/proof) + `cta-offers.md` (the CTA) + the empirical winning patterns from our own replies
(`analysis/sales-cohort-deepdive.md`, `cohort-playbook.md`). Scope: **email only, ICP buyer group
only** (verify with `scripts/buyer_group.py`).

## Inputs the generator needs
- Lead: first name, **title** (must pass `is_icp_buyer`), company.
- A **real, recent company/GTM signal** (funding, hire, product/GTM launch, expansion, tech move).
  No signal → don't fabricate; use a role-level pain hypothesis instead and flag for research.
- Primary **give** chosen from `cta-offers.md` (Tier A by default).

## The 4-touch structure
| Step | Job | CTA |
|---|---|---|
| 1 | Personalized opener + the strongest value-first give | Tier-A give (account list / drafts) |
| 2 | New angle, not a nag — when a hiring signal is provided, open email 2 on it (open-role count + 1-2 sales roles, tied to covering pipeline while the new reps ramp; skip if step 1's signal already covered hiring). When a sequencing play is flagged, one no-disruption line: own email + LinkedIn infra and capacity, their tools and process untouched, reps stay on follow-up and closing, 2-5x more meetings on top of the run rate (a single supporting line when hiring opened the email) | Run-rate + signal-set estimate (15 min) |
| 3 | The Memgraph signal-activation proof: signal-rich (reo.dev, 6sense, product telemetry), more in-market accounts than the team could prospect, the AI SDR activated the full set. Name ONE detected intent/ABM tool when flagged; when only ad pixels are flagged, reference their ad investment generically (never name pixels); else tell the signal-set story on its own | Signal-mapping session (15 min) |
| 4 | Breakup + soft give | "Should I close your file? Happy to leave {give} either way." |

## Per-email recipe — write in 3 short paragraphs separated by BLANK LINES
1. **Subject:** outcome-led, ~4–6 words, lowercase ok, no clickbait. E.g. *"pipeline {company} is leaving on the table"*, *"a signal play for {company}"*.
2. **Paragraph 1 — opener (1–2 sentences):** name the **specific recent signal** — *"Saw {company} just {signal}."* Highest-leverage line (personalized openers = +30.5%).
3. **Paragraph 2 — pain + value (2 sentences):** tie the signal to **scaling pipeline without adding headcount** (role-tuned), then one concrete deck-true metric (*"3–5x meetings per rep, no new hires"*). Include a metric.
4. **Paragraph 3 — CTA (1–2 sentences):** a **give + a meeting ask** from `cta-offers.md`. Lead with the deliverable give, but **the give is delivered ON the meeting** — every CTA must ask for a quick call / 15 minutes / "walk you through it." Never "send it over, no call," and never a bare meeting ask with no give.

## Formatting (critical — the old output got these wrong)
- **Separate the 3 paragraphs with a blank line** (`\n\n` in the JSON). Do NOT write one dense block.
- **NO sign-off and NO name at the end.** Do not end with "{first_name}", "Best,", "Thanks", or any
  closer; the Email Bison campaign appends the sender's signature automatically. End on the CTA.
- **NEVER use em dashes (—) or en dashes (–).** Use commas, periods, or colons instead. (Regular
  hyphens in words like "tech-stack" or "3-5x" are fine.)
- Plain text only. No markdown, no bullet lists.

## Hard guardrails (enforced by `lint_sequence.py`)
- **70–110 words** per email (sweet spot ~75–95). **One** ask. Paragraph breaks required.
- Step-1 opener references the company/signal by name (personalized).
- ≥1 concrete metric somewhere in the sequence.
- CTA **asks for a meeting AND anchors it on a deliverable give** (no "send it over, no call"; no
  bare "got 15 min"; no "send your visitors / 25 in-market accounts" — see `cta-offers.md`).
- Step 4 is a **breakup**.
- **No pricing/discount language in cold steps** (pricing only in replies, on a direct ask).
- **No trailing first-name sign-off.** Every product claim traceable to `offer.md`.

## Tone
Peer-to-peer, concise, confident, zero fluff. Emulate the verbatim winners in
`analysis/sales-cohort-deepdive.md` (e.g. *"I noticed Storylane shipped Storylane 2.0… balancing
high-volume outreach with personalized demo follow-up"*) — **emulate the shape, never copy text or
reuse another company's specifics.**

## Output format (so the linter can read it)
Markdown, one block per step:
```
## Step N — Subject: <subject>
<body>
```
