---
name: ai-sdr
description: Generate and iterate on outbound for the EverWorker SDR AI Worker, sold exclusively to a tight ICP (GTM leadership at B2B tech startups) via email. Use to write value-first cold email sequences, brainstorm/test CTA offers, or draft replies (incl. pricing) to interested leads. Grounded in the product knowledge base.
---

# AI SDR — value-first outbound engine (ICP + email)

Turns our analysis of what generates interested replies into **action**: writes outbound and
handles replies for the **EverWorker SDR AI Worker**, the only thing we sell, to the only buyers we
target. Everything is grounded in the knowledge base — **never invent product claims, numbers, or
signals.**

## Scope (ruthlessly narrow — by design)
- **Product:** the SDR AI Worker packages only. **Channel:** email only (LinkedIn is a follow-on).
- **ICP:** B2B **tech startups** scaling pipeline **without adding headcount**.
- **Buyer group only:** CRO/Sales chiefs; VP/Head/Dir Sales·BD·GTM·Revenue; Sales Mgr/Analyst/Ops;
  SDR/BDR Mgr/Lead; Head of Marketing *who owns pipeline*. Verify any lead with
  `scripts/buyer_group.py` — if `is_icp_buyer` is False, **don't write to them.**
- **"Offers" = the CTA inside the email.** Default to **value-first** gives, never bare time-asks.

## Knowledge base (read before generating)
- `knowledge/offer.md` — product, positioning, proof, ICP/buyer group, objections, **pricing
  matrix**, style rules. The single source of truth.
- `knowledge/cta-offers.md` — the **value-first CTA/offer library** (the iteration substrate).
- `knowledge/icp-email.md` — the email generation recipe + guardrails.
- Empirical winners to emulate (shape, not text): `data/interested-replies/analysis/`
  `sales-cohort-deepdive.md`, `cohort-playbook.md`; baseline CTA mix: `icp-cta-report.md`.

## Workflows

### A. Generate an email sequence
1. Confirm the lead is ICP: `echo "<title>" | python3 .claude/skills/ai-sdr/scripts/buyer_group.py`.
2. Gather a **real, recent company signal** (don't fabricate). Pick a primary **give** from
   `cta-offers.md` (Tier A default).
3. Read `offer.md` + `cta-offers.md` + `icp-email.md`, then write a **4-touch** sequence
   (opener+give → new-angle give → proof+re-offer → breakup+soft give) in the output format below.
4. **Lint and revise to pass:**
   `python3 .claude/skills/ai-sdr/scripts/lint_sequence.py <file.md>` (70–100w, personalized opener,
   ≥1 metric, single value-first CTA, breakup in step 4, no pricing in cold steps).
5. Template + a passing reference: `examples/icp-email-sequence.md`.

### B. Test CTA offers (the iteration loop)
Generate **N value-first CTA variants** for one lead (`examples/cta-variants.md`), ship across
comparable ICP segments, then re-run `interested-trends/scripts/analyze_icp_cta.py` after the next
`fetch_interested_replies.py` to watch the **value-first reply share** rise and see which gives won.
Promote winners to Tier A in `cta-offers.md`; retire losers.

### C. Handle an interested reply
1. Classify intent (meeting-accept / info-request / pricing / objection / referral).
2. Draft a grounded reply — **deliver the give first**, then the next step. Examples:
   `examples/reply-handling.md`.
3. **Pricing only on a direct pricing ask:** use the matrix in `offer.md` — anchor on **Scale
   ($5.5k/mo)**, note **3-month opt-out** + included GTM engineer, frame vs a fully-loaded SDR.
   **Never lead with price in cold outbound.**

## Output format (lint-readable)
```
## Step N — Subject: <subject>
<body 70–100 words, one value-first ask>
```

## Guardrails (non-negotiable)
- ICP buyer group only; email only; SDR AI Worker only.
- Value-first CTA by default; never a bare "got 15 minutes?".
- Every claim traceable to `offer.md`; no fabricated signals/stats; no pricing in cold steps.

## Follow-ons (out of scope now)
LinkedIn channel; other audiences; pushing sequences into Bison sequence-steps via the API;
closed-loop promotion of winning CTAs back into `cta-offers.md`.
