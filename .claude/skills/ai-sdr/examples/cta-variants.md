# CTA / Offer Variants for one lead (the "offers testing" substrate)

Same lead as the gold sequence (Maya Chen, VP Revenue, Driftwave — Series-A, just raised $12M,
hiring AEs). Below are **deliverable, value-first CTA variants for the Step-1 opener** to A/B test.
Each gives value without requiring a meeting — and each is something we can actually send/show.
Rotate one per segment and measure the value-first reply share on the next `analyze_icp_cta.py` run.

| # | CTA (verbatim) | Give type | Why it pays the buyer first |
|---|---|---|---|
| V1 | "I had our SDR AI build a **personalized signal play** for Driftwave off your hiring and tech-stack signals — accounts showing them progress ~4.4x faster. Want me to send it over?" | signal play | A tailored play they keep |
| V2 | "Do you know how many meetings Driftwave needs to hit pipeline target this quarter? I can **walk you through our pipeline model** and show exactly how to close the gap." | pipeline gap analysis | The meeting math for their number |
| V3 | "I had our AI SDR write **3 personalized emails to your top 3 target accounts**. Want to see them?" | 3 drafts | Ready copy + a quality bar |
| V4 | "Happy to send a **2-minute teardown of Driftwave's current outbound** — 3 things I'd change. Our best practices alone usually lift response rates 50–70%." | teardown | Free expert review |
| V5 | "Want a **benchmark of Driftwave's reply rate vs other Series-A startups**?" | peer benchmark | A number they'll want to know |
| V6 | "Can I send a **one-page playbook with 3 AI-SDR plays** scoped to a Series-A motion?" | pilot playbook | A concrete plan (great breakup give) |

**Anti-patterns (do NOT ship):**
- ✗ "Want 25 in-market accounts showing buying signals? Yours to keep." (can't deliver cold)
- ✗ "Want the list of companies that visited your site this week?" (we don't have their visitor data)
- ✗ "Got 15 minutes for a quick demo?" (bare time-ask, gives nothing)

**How to test:** ship V1–V3 to comparable segments, hold V4–V6 for follow-ups/breakups, then on the
next pull check which gives earned replies (`icp_cta.jsonl`) and promote winners in `cta-offers.md`.
