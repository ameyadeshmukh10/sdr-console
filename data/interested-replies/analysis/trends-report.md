# What's Working: Interested-Reply Trends

_Generated 2026-06-03 from **171 interested replies** (170 genuine, 1 auto-reply excluded). Source: `data/interested-replies/dataset.jsonl`. Numbers from `summary.json`; examples cite `reply_id` and were confirmed against `threads/<id>.md`._

> ⚠️ **Read this first — descriptive, not causal.** This is the set of people who *replied interested*, not everyone you contacted. There is **no denominator**, so nothing here proves "X converts better than Y" — it shows **who replies and what the winning messages look like**. Treat it as a profile of success, then test the hypotheses at the bottom to get real lift.
>
> 📊 **The denominator now exists — see [`conversion-report.md`](./conversion-report.md) for the causal view.** It overrides the volume-based findings below: e.g. PDFs *dominate replies* here but Demo-request asks *convert ~3.7× better per lead contacted*, and the persona-targeted CRO/CMO campaigns have the **worst** rate despite landing C-level. The step-4 breakup finding is **confirmed causally**.

---

## TL;DR — top 5 things working

1. **The "breakup" follow-up is your single biggest reply driver.** Step 4 ("Last note…", "Closing the loop", "Should I close your file?") generated **53 of 141 matched replies (38%)** — more than the personalized opener (step 1: 45). The polite final-nudge is doing heavy lifting (e.g. `3539488` "Should I close your file?" → *"I'd like to learn more…"*; `3505237` "Closing the loop" → *"sure send it over"*).
2. **Hyper-personalized openers win the early reply.** Step 1 wins 45 times and **82.9% of step-1 openers reference the prospect's company/LinkedIn** ("I looked at Confluence Local Marketing and your LinkedIn…"). When it lands, it lands fast (`3295145`, `3435236`).
3. **Short, plain-text, question-led emails.** Winning emails average **71 words** (range 37–131), subjects average **5.7 words**, ~**0.8 questions per email**, and **0% use a P.S.** Brevity + one clear ask is the pattern.
4. **The offer that pulls is the AI/“scale-without-hiring” angle, and it pulls across many industries** — marketing agencies, financial services, tech/SaaS, pharma, CPG. Targeting is horizontal, not one vertical.
5. **Senior buyers are replying.** **C-Level (22.8%) + Founder/Owner (14%) + Head/Director (21.1%) = ~58%** of repliers. You're reaching decision-makers, not just ICs.

---

## ICP / firmographic trends

- **Offer type (campaign-encoded):** Lead-magnet PDFs dominate replies — **116 (67.8%)**, vs Demo request 23 (13.5%), Event 19 (11.1%), Persona-targeted CRO/CMO 13 (7.6%). Caveat: PDFs are likely also your largest *send* volume, so this reflects mix as much as effectiveness.
- **Geo:** Global **66%** / Americas **34%**. The two persona-targeted plays (CRO/CMO USA) are Americas-only and small but punch above weight on seniority (see below).
- **Inferred industries** (no industry field — clustered from company/domain): the repliers span **marketing & creative agencies** (Confluence Local Marketing, All The Right Movies, RB Yacht Marketing, Early Marketing), **financial services/fintech** (The CFO Centre UK, Offa, Starling Bank), **tech/SaaS** (Zillion Games, Honeycomb Software, Blueprint Technologies, Zeendoc), **pharma/life-sciences** (Takeda, Axtria, NABU Sciences), and **CPG/manufacturing** (Migros, Siniora Food, Flora Food Group, Vagabond). **Takeaway:** the "AI worker / automate-without-headcount" message resonates horizontally; agencies, financial services and tech are the densest clusters.

## Persona trends

- **Seniority of repliers:** C-Level 39 (22.8%), Head/Director 36 (21.1%), Manager 31 (18.1%), IC 28 (16.4%), Founder/Owner 24 (14%), VP 5.
- **Function:** Marketing 37 (21.6%), Sales/Revenue 34 (19.9%), Finance 25 (14.6%), Exec/General 19, then Consulting, Eng/IT/Product, HR/Talent. (Function "Other" 34 — mostly niche/headline titles the heuristic couldn't bucket.)
- **Offer × persona:** the **persona-targeted CRO play landed 13/13 on C-Level** — when you target a title explicitly, you get that title (e.g. `3617826`, `3618708`, David Van Wert, **Chief Revenue Officer**: *"send me an invite for next Thursday… focused on making outbound more efficient without adding headcount"*). PDFs spread across all seniorities (C-Level 21, Head/Dir 29, Manager 24, IC 22). **Takeaway:** title-targeted subject lines ("Double meetings without more headcount" → CROs) are the cleanest lever for persona control.

## SDR email construction

- **Which step wins:** Step 4 **53** > Step 1 **45** > Step 2 **31** > Step 3 **12** (29 unmatched/untracked). A barbell: the personalized opener and the breakup close do the work; the middle steps under-perform.
- **Length & shape:** winning emails avg **71 words** (37–131); subjects avg **5.7 words**; **23%** of winning subjects are questions; avg **0.8** questions in the body. Plain text, no P.S.
- **Personalization:** **82.9%** of openers reference the specific company/LinkedIn; **31.8%** include light social proof ("agencies I talk with…", "teams we work with…"). The winning-email personalization rate is lower (27.7%) precisely *because* so many winners are step-4 breakups, which are short and generic.
- **Subject-line patterns that won:** outcome-led and specific — "Scale local marketing without hiring", "Cut manual QA and coaching time", "Double meetings without more headcount", and breakup framings "Last note - can I leave a resource?", "Should I close your file?", "Closing the loop".

## CTA analysis

- **CTA mix of winning emails:** Open question **56 (39.7%)**, Soft-permission question **33 (23.4%)**, None/unclear 23, Demo offer 22, Specific-time offer 5, Resource offer 2.
- **CTA × reply intent** (what each ask produces):
  - **Soft-permission questions** ("would you be open to…", "can I leave a resource?") convert most cleanly to meetings — **17 of 33 → meeting/demo accept (52%)**, the best ratio of any CTA.
  - **Demo offers** → 11/22 meeting accepts (50%) but also draw more clarifying **questions** (7).
  - **Open questions** are highest-volume but more diffuse: 18 meetings, 18 "other", 11 questions, and the few pricing asks.
  - **Specific-time offers** are rare (5) and didn't outperform — surprisingly, "pick a slot" hard-asks are underused here.
- **Takeaway:** the **soft-permission ask** ("would you be open to…", "ok if I send…") is the highest-yield CTA in this corpus; hard calendar asks are underused and worth testing.

## Reply intent & timing

- **Intent mix (genuine):** Meeting/demo accept **60 (35.3%)**, Question 37 (21.8%), Referral/forward 13 (7.6%), Info request 7, Pricing 4, Negative/opt-out 2 — plus 46 "Other" (short/ambiguous or SDR-side messages). **~35% of interested replies are an outright meeting/demo yes.**
- **Day of week (reply received):** **Tuesday 26.5%** is the clear peak, then Mon/Fri ~21% each; Wed dips to 10.6%; weekends negligible. Early-week and Friday are when interest surfaces.

## Data caveats & confidence

- **Descriptive only / no denominator** — cannot compute true conversion lift (see top banner).
- **1 auto-reply/OOO** excluded from messaging/intent aggregates.
- **Heuristic personas:** seniority Unknown 8/171, function "Other" 34/171 — title text is messy (some are LinkedIn headlines). Directional, not exact.
- **No industry field** — industry clusters above are inferred from company name/domain.
- **`interested_reply_text` is occasionally the SDR's own reply** (latest message in thread), which inflates "Other"/"Question" intents. Cited examples were checked against the thread files; segment-level intent counts carry this noise.
- **Winning-step matching** resolved for 141/171 (subject match); 29 unmatched are mostly the 24 untracked single-message threads.

## Recommended experiments (to turn "what replied" into "what converts")

1. **Pull the denominator.** Add per-campaign total-contacted from Bison and recompute reply-rate by offer, persona, and step. This is the one change that turns this report causal. (Extend `email-bison/scripts/bison_client.py`.)
2. **Double down on the breakup.** Step 4 wins most — A/B two breakup variants ("Should I close your file?" vs "Closing the loop") and measure which earns the meeting, not just the reply.
3. **CTA test: soft-permission vs hard calendar ask.** Soft-permission had the best meeting ratio (52%) and specific-time asks are barely used (5). Test "would you be open to 15 min?" against "Tue 10a or Wed 2p?".
4. **Lean into title-targeted subjects.** The CRO play hit 13/13 C-Level. Build parallel persona campaigns (CFO, CMO, Head of Ops) with outcome-in-subject lines and compare reply quality.
5. **Send/cadence timing.** Interest peaks Tue + Fri — test concentrating step-1 and step-4 sends just before those days.
6. **Trim the middle.** Steps 2–3 under-deliver (31 + 12). Test cutting to a 3-touch sequence (personalized opener → value → breakup) and watch reply rate.
