# AI SDR Cohort Playbook — What's Working by Job Function

_Generated 2026-06-03 from `cohort_evidence.jsonl` + `cohorts.json` (171 interested replies). Qualitative patterns read from the actual winning sequences; counts from the deterministic tagger._

> **How to read this:** numbers are **shares among repliers within each cohort**, not conversion rates (we only know job function for the 171 who replied, not the full contacted list). The value is the **qualitative signature per cohort** — feed each cohort's "AI SDR instructions" block into the sequence generator. Pair with `conversion-report.md` for the cross-campaign rate truth (Demo asks and the step-4 breakup convert best overall).

## Cohort sizes
| Cohort | n | Avg winning email |
|---|---:|---:|
| Other (Finance, Eng, HR, Ops, Consulting…) | 71 | 78 words |
| Marketing | 37 | 69 words |
| Sales | 32 | 80 words |
| CEO/Founder | 31 | 78 words |

## Cross-cohort signature (the one-glance view)

| Lever | Marketing | Sales | CEO/Founder |
|---|---|---|---|
| **#1 personalization** | Company research **100%** + recent brand/campaign wins (51%) | **Metric specificity 88%** + pain hypothesis 81% | **Role-persona 81%** ("As Founder…") + LinkedIn tenure |
| **Pain framing** | Creative scale, campaign turnaround, brand consistency | Outreach volume vs. personalized follow-up, lead tracking | Headcount/bandwidth squeeze, "do more without hiring" |
| **Top offer** | Resource + call (43% each), some pilot | **Quick call (62%)** — metrics live in the copy, not a formal ROI offer | Call (52%) + **pilot (26%) / audit (16%)** (founders uniquely) |
| **Best CTA** | Open question | Open question (50%) | **Soft-permission tied with open** (38/38) |
| **Reply style** | Warm, appreciative, "send it through" | Action-oriented, names a concrete workflow need | Terse & decisive ("sure", "Send it over") |

---

## Marketing (n=37)

**What personalization worked — and where.** Every winning opener (100%) led with **specific company research**, and the highest-signal variant referenced a **recent brand/campaign win in the first sentence**:
- `3428866` (CMO, Zillion Games): *"Congrats on Zillion Games winning the EGR Europe award and on the recent partnership to expand affiliate reach…"*
- `3537438` (Group CMO, Flora Food Group): *"I saw Flora Food Group's recent work — Violife's collaboration with Bozoma and the Gordon Ramsay spot…"*

It's followed by a **marketing-specific pain hypothesis** (65%): creative scale, faster campaign turnarounds, consistent brand voice across markets (`3539138`, Head of Digital Marketing, Fieldfisher: *"balancing partner-level thought leadership with tighter timelines and compliance checks"*).

**Offer/positioning that worked.** Split between **a resource and a light call** (43% each); position EverWorker as helping the marketing *team* scale content/creative without adding headcount. Demo offers underperform here — marketers want to see an asset, not sit a demo.

**CTA.** Open questions win (43%); soft-permission ("ok if I send X?") second. Replies are warm and asset-hungry ("That sounds of interest, pls send").

**→ AI SDR instructions (Marketing):**
- Open with a *named recent campaign / award / brand moment* — not just the company name.
- Hypothesize a creative-scale or brand-consistency pain tied to their role.
- Offer a **resource/asset first** (not a demo), with a light call as the alternative.
- Close with a soft "ok if I send it over?" — they convert by receiving the asset.

---

## Sales (n=32)

**What personalization worked.** Sales leaders respond to **numbers and a sharp pain hypothesis**: **metric specificity 88%**, pain hypothesis 81% — the highest of any cohort. The winning openers cite a **GTM/product move** then a quantified follow-up pain:
- `3542130` (Founding SDR, Storylane): *"I noticed Storylane shipped Storylane 2.0 and an Outreach Marketplace app… balancing high-volume outreach with personalized demo follow-up across APAC."*
- `3545488` (BDR, Rover Engineering): tied the opener to a guide they downloaded + "SDR AI worker that handles inbound follow-up."

**Offer/positioning.** The ask is overwhelmingly a **quick call (62%)**; formal ROI-doc / resource offers are minor (≤16%). What's quantified is the **copy, not the offer** — the value prop is packed with metrics (88% metric specificity), e.g. `3545488`: *"AI workers like this can often double response rates and cut qualification time by ~30%."* Position around a concrete, measurable outcome (more meetings / faster follow-up / lead triage). Replies name the exact workflow they want automated (`3542130`: *"I'd like an AI workflow that can help me keep track of all the leads I need to follow up"*) — sales buyers self-qualify with a use case.

**CTA.** Open question (50%). Keep it outcome-led, not soft.

**→ AI SDR instructions (Sales):**
- Lead with a **quantified pain** ("cut follow-up time", "double meetings without headcount") — metrics over adjectives.
- Reference a recent **product/GTM move** as the hook.
- Pack the value prop with **concrete metrics** (double response rates, cut qualification time ~30%), then ask for a **quick call** — a formal ROI doc is optional.
- Expect (and invite) the prospect to name their own use case — make it easy to reply with one.

---

## CEO/Founder (n=31)

**What personalization worked.** Founders respond to **role-persona framing (81%)** plus **company research (77%)** — speak to them *as the owner carrying the load*:
- `3295145` (Founder, Confluence): *"…your LinkedIn noting 12+ years building local online visibility for SMBs — as Founder you've likely felt the squeeze between growing client demand and limited delivery bandwidth."*
- `3395888` (Co-Founder, RainmakerOS): *"I reviewed RainmakerOS.ai and liked that you position it as a done-for-you lead-gen platform… As Co-Founder you're likely balancing rapid pipeline growth with limited headcount."*

The pain is always **headcount/bandwidth** — "do more without hiring." This is the only cohort where **LinkedIn tenure** and **social proof** ("Agencies I talk with…") show up meaningfully.

**Offer/positioning.** A quick **call (52%)**, but uniquely **pilot (26%) and audit (16%)** land with founders — they'll take a low-commitment proof (formal ROI-doc offers are rare here). 

**CTA.** **Soft-permission and open questions tie (39% each)** — founders reward a respectful, low-friction ask and reply **tersely and decisively**: *"sure"*, *"Send it over."*

**→ AI SDR instructions (CEO/Founder):**
- Address them **as the owner** ("As Founder you've likely…") and reference tenure/journey.
- Frame the pain as **headcount/bandwidth** — scale without hiring.
- Offer a **pilot or audit** (low-commitment proof), with light social proof.
- Use a **soft-permission close**; keep it short — they reply in 3 words.

---

## Other (n=71 — Finance, Eng/IT, HR/Talent, Ops, Consulting)

Largest bucket and most heterogeneous. **Company research (70%) + recent news (59%)** lead; **social proof (39%)** is higher here than in Marketing/Sales (these buyers want proof others like them adopted it). Offers skew **call + demo (62% / 30%)** — more than other cohorts, this group will sit a demo. **Step 4 (breakup) is the top winner (22)** here.

**→ AI SDR guidance:** sub-segment later (Finance vs Eng vs HR behave differently). For now: company-research opener + a **named social-proof point** + offer a demo; the breakup email earns a disproportionate share of these replies, so keep it strong.

---

## How to operationalize

1. **Route by cohort at sequence-generation time.** Classify the lead's title into Marketing / Sales / CEO-Founder / Other (logic in `analyze_cohorts.py::assign_cohort`) and select the matching instruction block above.
2. **Always-on (every cohort):** named company research in sentence 1; a role-relevant pain hypothesis; a strong step-4 breakup; short (~70–80 words).
3. **Cohort dials:** Marketing → recent-campaign hook + resource offer; Sales → metric-packed copy + quick call; CEO/Founder → owner framing + pilot/audit + soft-permission close.
4. **Refresh:** re-run `analyze_cohorts.py` after each `fetch_interested_replies.py` pull to keep the playbook current as the corpus grows.

_Caveat: descriptive within repliers; not conversion lift. To make cohort claims causal you'd need job function on the contacted population (the full per-segment pull). Reply-text snippets confirmed against `threads/<reply_id>.md`._
