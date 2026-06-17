# Value-Anchored CTA / Offer Library

The core of "offers testing." An **offer** here = the **CTA inside the email**. We lead with a
valuable give, but **the give is delivered ON a meeting** — our SDR team does nothing until a
meeting is booked. So every CTA **anchors a meeting ask on the value**: the give is the reason to
take the call, not a freebie sent cold.

**Hard rules:**
- **Every CTA must ask for a meeting** (15 min, quick call, "walk you through"). No "send it over,
  no call" — we don't deliver anything until we're talking.
- **Anchor the meeting on a real, deliverable give** (something we can actually show on the call).
- Do NOT promise a prospect their own "de-anonymized visitors" or "25 in-market accounts" — fake.

## Why value-anchored meeting asks win
GTM leaders ignore "got 15 minutes?" because it gives them nothing. They take a call when there's
something specific and useful waiting on the other side. Lead with the give, make the meeting the
way to get it.

## The library (give + meeting ask)

### Tier A — product-as-the-give
1. **Signal play (hiring + technographic)** — *"I built a personalized **signal play** for {company}
   off your hiring and tech-stack signals — accounts showing them progress ~**4.4x** faster. Worth
   **15 minutes** for me to walk you through it?"*
2. **Pipeline gap analysis** — *"Want to **grab 15 minutes**? I'll walk you through our **pipeline
   model** and show exactly how many meetings {company} needs to hit target this quarter."*
3. **Personalized drafts** — *"I had our AI SDR draft **3 personalized emails to your top 3
   accounts**. Want to **hop on a quick call** and I'll walk you through them?"*

### Tier B — analysis / teardown gives
4. **Outbound teardown** — *"**Worth 15 minutes?** I'll walk you through a **teardown of your current
   outbound** — 3 things I'd change. Our best practices alone usually lift response rates 50–70%."*
5. **Peer benchmark** — *"Want to **grab time** so I can walk you through how {company}'s reply rate
   compares to other {seed/Series-A} startups?"*
6. **Pilot playbook** — *"Before I close your file — **worth 15 minutes** to walk through a one-page
   **playbook of 3 AI-SDR plays** for your {team/motion}?"* (the breakup-step meeting ask)

### Anti-patterns — DO NOT USE
- ✗ "Want me to send it over? Yours to keep, no call needed." (we deliver only on a meeting)
- ✗ "Got 15 minutes for a quick call?" (a meeting ask with NO value hook — anchor it on a give)
- ✗ "Want 25 in-market accounts / your de-anonymized visitors?" (undeliverable / fake)

## Cadence placement (4-touch email)
- **Step 1 (opener):** Tier-A give + meeting ask — signal play or pipeline gap.
- **Step 2:** a new give + meeting ask — teardown or 3 drafts.
- **Step 3:** proof point (Memgraph) + benchmark give + meeting ask.
- **Step 4 (breakup):** *"Before I close your file, worth 15 minutes to walk through the playbook?"*

## Iteration loop
1. Generate a sequence with a chosen primary give (`SKILL.md` → generate).
2. Generate **N CTA variants** for the same lead (`examples/cta-variants.md`).
3. Ship; on the next `analyze_icp_cta.py`, watch which give+meeting framings earn replies and
   promote winners.

## Guardrails for any CTA
- Must **ask for a meeting** AND **anchor it on a deliverable give**.
- One ask per email. Keep the meeting ask low-friction (15 min, a quick call).
