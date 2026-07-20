# Context Engine — EverWorker SDR AI Worker (the offer)

Single source of truth for the product, ICP, proof, objections, pricing, and style.
Every generated email / reply must be grounded here. Source: `EverWorker-SDR-Deck.pdf`
(page refs in parentheses). **Do not invent claims or numbers not in this file.**

## One-liner
An autonomous AI SDR that turns buying signals + inbound into booked meetings —
**3–5x more meetings per rep, without new headcount** (p1).

## What it is (solution)
Signal → booked meeting, with 0 human time per prospect (p2, p9):
- **Signal intelligence:** surfaces in-market accounts in real time — website de-anonymization,
  technographic, hiring, company news, intent/ABM, inbound (p3).
- **Buying-group enrichment:** account in → full buying group out, via the customer's B2B data
  providers (Clay, ZoomInfo, Apollo, Prospeo, People Data Labs, Lusha, Cognism) (p4).
- **Deep research + personal writing:** company + contact research → personalized, reviewed email
  (p5).
- **Multi-channel sequencing:** email + LinkedIn run by the worker; the human team adds calls.
  15 touches over ~3 weeks (p7, p10).
- **Outcome:** meetings booked, every email auto-logged to the CRM (Salesforce/HubSpot) (p6).
- **Done-for-you infrastructure:** email (authenticated domains, warmup pool, deliverability) and
  LinkedIn (proxies, sender rotation, human-pattern timing) are configured, integrated, managed —
  lands in the **primary inbox**, isolated from the customer's own domain (p6, p13, p14). Because
  the infrastructure and capacity are built in, it runs alongside a team's existing sequencing
  tools (Outreach, Salesloft, Apollo) without touching them — reps keep their process and stay on
  follow-up and deal progression while it books net-new meetings, adding **2–5x more meetings on
  top of the current run rate** (customer-confirmed, not on the deck page).
- **Multi-agent system + Context Engine:** personas · messaging · offers · proof · style, wired to
  the customer's CRM, email, LinkedIn, LLM endpoints, B2B data, scheduling (p8).
- **Live in 5 weeks**, built by a **forward-deployed GTM AI engineer**, running on the customer's
  stack (p15).

## ICP & buyer group (WHO WE SELL TO — tight)
**ICP:** B2B **technology startups** trying to **scale pipeline generation without scaling
headcount**.

**Buyer group (the only titles we target):** GTM leadership —
- Chief Revenue Officer (CRO) / Chief Sales/Commercial Officer
- VP / Head / Director of Sales · Business Development · GTM · Revenue
- Sales Manager / Sales Analyst / Sales Operations (RevOps)
- SDR / BDR Manager / Lead
- **Head of Marketing** *only when they own SDRs / pipeline generation*

Not in scope: founders/CEOs (per current definition), finance, eng, product, HR, consultants,
account/partnerships, junior/IC marketing. Classifier: `scripts/buyer_group.py`.

**Core pain we speak to:** more in-market accounts than the team can touch; can't hire SDRs fast
enough or affordably; reps burning time on research/writing/CRM instead of meetings (p3, p10, p12).

## Proof (use sparingly, only these — all deck-sourced)
- **Memgraph:** **$2.7M qualified pipeline, 600 replies, 60 BANT-qualified deals in 90 days**;
  45,000 contacts across 500 target accounts; live in 4 weeks; scaling to 100,000 next quarter
  (p12). Quote: *"We had more in-market accounts than the team could touch, and hiring enough SDRs
  to cover them wasn't realistic."* — Axel Goransson, Sales Intelligence Architect, Memgraph.
  **Signal-activation story** (customer-confirmed, not on the deck page): Memgraph came in
  signal-rich — reo.dev, 6sense, and product telemetry plus a rich marketing signal set were
  already surfacing more in-market accounts than the team could prospect into. The AI SDR was
  pointed at that full signal set and activated it automatically, which is why the pipeline came so
  fast. This is the email-3 framing: their signals, activated on autopilot (see `icp-email.md`).
- **Built-in best-practice lifts** (p11): multi-channel 287% more responses than single channel;
  +67% from tighter 75–100 word emails; +30.5% from personalized opening lines; 3x from 4–7 step
  sequences.
- **Trust:** SOC 2 Type II · ISO 27001 · GDPR (p1, p17).

## Objection → response
| Objection | Response (grounded) |
|---|---|
| "We already have SDRs." | It augments, doesn't replace — reps stop doing research/writing/CRM and spend time in meetings & closing; 3–5x output, same team (p10). |
| "AI outreach feels spammy / hurts our brand." | Deeply researched + personally written, every draft reviewed by an agent vs guardrails before send; lands in primary inbox, not spam (p5, p6). |
| "Deliverability / our domain." | Done-for-you email infra — authenticated domains, private warmup pool, dedicated IPs, sending isolated from your domain (p6, p13). |
| "How fast / how much lift to set up?" | Live in 5 weeks, forward-deployed GTM AI engineer builds it with you; runs on your stack (p15). |
| "Price?" | Frame vs a fully-loaded SDR: from $3.5k/mo, 3-month opt-out, infra + engineer included (see matrix). Only discuss on a direct pricing ask. |

## Pricing & packaging (p16) — all tiers 3-month opt-out
| Tier | Price | Adds on top |
|---|---|---|
| **Starter** | **$3,500/mo** | Multi-channel AI SDR (LinkedIn + Email); your B2B data providers + CRM |
| **Scale** | **$5,500/mo** | Everything in Starter + website de-anonymization (1,000 ICP accounts/mo) + technographic signals (5,000 accounts/mo) |
| **Advanced** | **$7,000/mo** | Everything in Scale + built-in API & agentic signal intelligence + de-anon (2,000/mo) + hiring signals (5,000/mo) + company+contact lead-gen (15,000 credits/mo) |

**Included in every package:** forward-deployed GTM AI engineer · LLM endpoints · email + LinkedIn
infrastructure · SDR AI agent config + CRM integration · single-tenant SaaS platform. Capacity
(email/LinkedIn infra, enrichment credits, LLM credits) is upgradable on demand at any tier.

**Pricing rules for outreach/replies:**
- **Never lead with price in cold outbound.** Lead with value/signal.
- On a direct pricing ask: anchor on **Scale ($5.5k/mo)**, note **3-month opt-out** and that the
  **GTM engineer + infra are included**, and frame against the cost of a fully-loaded SDR hire.
- Don't quote credit counts unless asked for tier detail.

## Style rules (apply to every generated asset)
- **75–100 words** per email (our data: ~70–90 is the sweet spot); plain text; **one** ask.
- Personalized opener naming a **real, recent company/GTM signal** (sentence 1).
- Map to the buyer's pain: **scale pipeline without headcount**. Use a concrete metric when apt.
- No hype, no superlatives, no fabricated stats — every claim traceable to this file.
- Outcome-led subject lines, ~4–6 words, no clickbait.
- Default CTA = **value-first** (see `cta-offers.md`), not "got 15 minutes?".
