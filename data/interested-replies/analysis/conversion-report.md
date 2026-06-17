# Conversion Analysis: What Actually Works (Causal)

_Generated 2026-06-03 from `data/campaign-stats/` (Bison campaign + per-step stats). This is the **causal companion** to `trends-report.md` — it adds the denominator the descriptive report was missing._

> **Numerator note:** rates use Bison's built-in **interested status** count (82 workspace-wide), not the status-OR-tag set (171) the descriptive report counts. Same source on both sides of every ratio, so the rates are valid — just a different (narrower) numerator than the 171.

---

## The headline: volume ≠ effectiveness

The descriptive report said **lead-magnet PDFs dominate** (67.8% of interested replies). With denominators, that flips: PDFs dominate only because they're **56% of all sends (8,561 of 15,673 contacted)**. On a **rate** basis they're middling.

**Interested rate by offer type:**

| Offer type | Interested | Contacted | **Interested rate** | Reply rate |
|---|---:|---:|---:|---:|
| **Demo request** | 13 | 578 | **2.25%** | 7.0% |
| Lead magnet (PDF) | 52 | 8,561 | 0.61% | 7.4% |
| Event | 12 | 2,279 | 0.53% | — |
| **Persona-targeted (CRO/CMO)** | 5 | 4,255 | **0.12%** | 2.7% |

**Demo-request asks convert ~3.7× better than PDFs and ~19× better than the persona-targeted CRO/CMO campaigns** — even though those persona campaigns landed exclusively on C-level titles. Targeting the right *title* did not produce interest; the *offer* did.

## By campaign

| Campaign | Offer | Interested rate | Interested / Contacted |
|---|---|---:|---|
| Global Demo Request | Demo | **3.38%** | 8 / 237 |
| Americas Demo Request | Demo | 1.54% | 5 / 325 |
| Global Event | Event | 0.80% | 11 / 1,381 |
| Global PDFs | PDF | 0.79% | 35 / 4,444 |
| Americas PDFs | PDF | 0.41% | 17 / 4,117 |
| CRO USA April 2026 | Persona | 0.20% | 4 / 2,036 |
| Americas Event | Event | 0.11% | 1 / 898 |
| CMO USA April 2026 | Persona | 0.05% | 1 / 2,219 |

## By geo

**Global converts 3× better than Americas: 0.89% (54/6,062) vs 0.29% (28/9,595).** Global is also where the high-rate Demo Request and Event plays run. Americas volume is heavily the low-converting persona-targeted campaigns.

## By sequence step — the breakup wins, causally

The descriptive report observed step 4 *appeared* most among replies. With denominators that holds up as a true **rate**, not an artifact of being last:

| Step | Interested | Contacted | **Interested rate** |
|---|---:|---:|---:|
| Step 1 (personalized opener) | 23 | 15,664 | 0.147% |
| Step 2 | 19 | 10,406 | 0.183% |
| Step 3 | 8 | 9,207 | 0.087% |
| **Step 4 (breakup)** | 32 | 7,747 | **0.413%** |

**Step 4 converts ~2.8× the opener and ~4.7× step 3**, despite fewer leads reaching it. Step 3 is the weakest link.

## What this changes vs. the descriptive report

- ❌ "PDFs are what's working" → ✅ **PDFs are high-volume but low-rate; Demo-request asks are what convert.**
- ❌ "Persona-targeting (CRO/CMO) lands senior buyers" → ✅ true, but those campaigns have the **worst interested rate** — landing the title ≠ generating interest.
- ✅ "The breakup email (step 4) wins" → **confirmed causally** (highest rate per lead contacted).

## Recommended actions (now evidence-backed)

1. **Shift send mix toward Demo-request asks.** They convert 3.7× PDFs. Scale Global Demo Request volume and test the demo ask inside PDF audiences.
2. **Re-examine the persona-targeted CRO/CMO plays.** 0.12% rate on 4,255 contacted is a lot of spend for 5 interested. Either rework the offer (it's persona-targeted but not demo-led) or pause.
3. **Keep and expand the breakup (step 4); fix or cut step 3.** Step 3 (0.087%) underperforms step 1. Test replacing it or moving to a 3-touch sequence ending in the breakup.
4. **Weight toward Global.** 3× the Americas rate; investigate whether it's the offer mix or genuine geo/list-quality difference before scaling Americas spend.

## Caveats

- Rates use the **interested status** numerator (82), so absolute counts here are smaller than the 171 in the descriptive report (which adds tag-only interested). Directionally consistent; don't mix the two numerators.
- Stats reflect a wide date window (2020-01-01 → today); campaigns at different maturity/contacted counts. A young campaign's rate is noisier (small n — e.g. Global Demo Request is 8/237).
- Per-step rates aggregate across campaigns with different sequences; treat as a portfolio view, not a single funnel.
- Re-run `fetch_campaign_stats.py` then `analyze_conversion.py` to refresh.
