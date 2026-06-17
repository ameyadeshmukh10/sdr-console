---
name: interested-trends
description: Analyze the corpus of Email Bison interested replies to uncover what's working to generate interested replies — trends in ICP/firmographic targeting, persona targeting, SDR email construction, subject lines, and CTAs. Use when the user wants to find patterns, trends, or "what's working" across their interested replies, or refresh the trends report.
---

# Interested-Reply Trends

Analyzes `data/interested-replies/dataset.jsonl` (produced by the `email-bison`
skill) to answer: **what's working to generate interested replies?** Across four
lenses — **ICP targeting, persona targeting, SDR email construction, and CTA** —
plus subject lines, reply intent, and timing.

## Two layers: descriptive + causal

1. **Descriptive** (`analyze_interested.py`) — profiles the people who replied
   interested and what the winning messages look like. No denominator, so it cannot
   say "X converts better than Y."
2. **Causal / conversion** (`analyze_conversion.py`) — joins per-campaign denominators
   pulled from Bison (`data/campaign-stats/`) to compute true interested **rates** by
   campaign, offer type, geo, and sequence step. This is what tells you what actually
   *works*, not just what's common among repliers.

Always run both and reconcile: descriptive volume can be a pure send-volume artifact
that the rate analysis corrects (e.g. PDFs are most *common* among replies but Demo
requests *convert* far better per lead contacted).

**Numerator note:** conversion rates use Bison's built-in `interested` **status** count,
which is smaller (82 workspace-wide) than the status-OR-tag set (171) the descriptive
layer uses. Same source on both sides of each ratio = a valid rate; just don't mix the
two numerators.

## Workflow

### 1. Ensure the inputs exist
If missing/stale, pull from Bison first:
```bash
python3 .claude/skills/email-bison/scripts/fetch_interested_replies.py   # dataset.jsonl
python3 .claude/skills/email-bison/scripts/fetch_campaign_stats.py        # campaign-stats/ (denominators)
```

### 2. Run the analyzers
```bash
python3 .claude/skills/interested-trends/scripts/analyze_interested.py    # descriptive
python3 .claude/skills/interested-trends/scripts/analyze_conversion.py    # causal rates
python3 .claude/skills/interested-trends/scripts/analyze_cohorts.py       # cohort + qualitative prep
```
- `analyze_conversion.py` → `conversion.json` + rate CSVs — true interested rates by
  campaign, offer, geo, and step.
- `analyze_cohorts.py` → `cohorts.json` (per-cohort quantitative) + `cohort_evidence.jsonl`
  (per-reply winning email + reply + personalization/offer/CTA tags). Cohorts = job
  function: **Marketing / Sales / CEO-Founder / Other** (`assign_cohort`). These are
  shares **among repliers**, not conversion rates (job function is unknown for the
  contacted population).

### 2b. Deep-dive one cohort for human review (verbatim copy)
```bash
python3 .claude/skills/interested-trends/scripts/cohort_deepdive.py Sales
# Cohort ∈ {Sales, Marketing, "CEO/Founder", Other}  (default: Sales)
```
Writes `<cohort>-cohort-deepdive.md` (+ `.json`) — an **evidence book** for reading the
actual copy:
- **Part 1 — Signature galleries:** for every personalization type / offer / CTA, the
  exact matched sentence(s) pulled verbatim from the sent emails, each with the lead's
  reply. Lets you scan "show me all the metric-specificity copy" at a glance.
- **Part 2 — Full emails:** the complete verbatim opener + winning email + cleaned reply
  for every record in the cohort, headed by its tagged signatures.
Use this when a human wants to judge what is / isn't working from the raw words.
This writes to `data/interested-replies/analysis/`:
- `features.jsonl` — one feature record per reply (persona, sequence construction
  features for each of the 4 steps, winning step/CTA, reply intent, cleaned reply text)
- `summary.json` — aggregate distributions, cross-tabs, and a `caveats` block
- `by_campaign.csv`, `by_persona.csv`, `by_cta.csv`, `by_reply_intent.csv`

The script handles all counting so the report never relies on eyeballing 171 records.
It also separates **auto-replies/OOO** from genuine interest.

### 3. LLM synthesis layer (you, the agent)
Read `summary.json` (the numbers) and `features.jsonl` (the per-record detail). Then:
- **Reclassify** records the heuristics left `seniority=Unknown` or `function=Other`
  by reading their `title`.
- **Infer industry clusters** (no industry field exists) from `company` names and
  `email_domain` — group the 78 companies into a handful of industries for the ICP lens.
- **Read the winning emails and replies** for the dominant patterns. For any cited
  reply, confirm it against `data/interested-replies/threads/<reply_id>.md` —
  `interested_reply_text` is occasionally the SDR's own message, not the lead's.
- Quantify from `summary.json`; illustrate with concrete cited examples.

### 4. Write the reports
- `trends-report.md` — descriptive (template below).
- `conversion-report.md` — causal rates (from `conversion.json`).
- `cohort-playbook.md` — **the AI SDR deliverable**: per-cohort qualitative synthesis.
  Read `cohort_evidence.jsonl` grouped by `cohort`; for each cohort extract **what kind
  of personalization worked (type + where + the actual content), what offer/positioning
  worked, what CTA worked, and structure**, with cited `reply_id` snippets confirmed
  against `threads/<id>.md`. End each cohort with an explicit "AI SDR instructions" block
  so the patterns can be fed straight into the sequence generator. Numbers from
  `cohorts.json`; qualitative read from the winning emails.

## Report template

```
# What's Working: Interested-Reply Trends
_Generated <date> from N interested replies (M genuine, K auto-replies excluded)._

## TL;DR — top 5 things working
- ... (each backed by a number + a one-line why)

## Conversion rates (causal — from conversion.json)
- Interested RATE by offer type, geo, campaign, and step. Lead with this when
  campaign-stats are available; it overrides volume-based descriptive findings.

## ICP / firmographic trends
- Offer types and campaigns that dominate by VOLUME; geo split; inferred industry
  clusters. Flag where volume ≠ rate (e.g. PDFs common but low-converting).

## Persona trends
- Seniority × function of repliers; which personas respond to which offer.

## SDR email construction
- Winning email length (avg/range), personalized-opening & social-proof usage,
  subject-line patterns, and which sequence step (1–4) tends to win.

## CTA analysis
- CTA mix of winning emails; CTA × reply-intent (which asks produce meeting accepts
  vs info requests vs referrals).

## Reply intent & timing
- Intent breakdown; day-of-week / hour patterns.

## Data caveats & confidence
- Descriptive only (no denominator). Auto-replies excluded (K). Titles/companies
  normalized heuristically. interested_reply_text occasionally = SDR side.

## Recommended experiments
- Concrete A/B hypotheses to test next (each tied to a finding above).
```

**Rules for the report:**
- Every quantitative claim cites a count/% from `summary.json`.
- Every qualitative claim cites ≥1 `reply_id` with a short verbatim snippet.
- Be honest about confidence; flag thin segments (small n).

## Extending
Add new analysis cuts as functions in `analyze_interested.py` (it's pure stdlib).
For true conversion lift, a future script would pull total-contacted per campaign
from Bison via `email-bison/scripts/bison_client.py` and divide.
