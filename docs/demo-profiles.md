# Demo profiles — the import contract

How to build a demo profile the SDR Console can be pointed at, and the file contract
another project (e.g. the context engine) exports to in order to drive one.

Read `CLAUDE.md` → "Demo mode / demo profiles" first for how profiles are resolved
and served. This document is only about **authoring** them.

---

## Why a file contract

The console and the context engine evolve separately. Rather than teaching the
console to read the engine's internal schema, the engine writes two files in the
shape below; the console's importer reads only those. Either side can be refactored
without breaking the other, and the files are diffable, reviewable and
version-controllable — which matters when a profile is going in front of a customer.

## The spec directory

Everything needed to build one profile lives in a spec directory:

```
demo-specs/<profile-id>/
  profile.yaml       identity, offers, performance shape   (hand-authored)
  targets.json       the accounts                          (context engine)
  signals.json       signals per account                   (context engine)
```

Build it with:

```bash
python3 .claude/skills/sdr-pipeline/scripts/make_demo_profile.py --spec demo-specs/<profile-id>
```

> **Status:** `--spec` is not implemented yet. Today the generator produces the
> fictional `generic` profile from hardcoded fixtures. This document defines the
> contract so the engine side can start exporting; wiring `--spec` is the first
> task of that work.

---

## `targets.json`

One entry per account. `name` and `domain` are required; everything else is optional
and improves how representative the demo looks.

```json
{
  "version": 1,
  "generated_at": "2026-08-03T12:00:00Z",
  "source": "context-engine",
  "contains_real_accounts": true,
  "targets": [
    {
      "name": "Northwind Analytics",
      "domain": "northwind-analytics.com",
      "sector": "Data infrastructure",
      "employee_count": 240,
      "hq": "Boston, MA",
      "icp_fit": "high",
      "contacts": [
        {
          "first_name": "Priya",
          "last_name": "Okafor",
          "title": "VP Sales",
          "persona": "sales-leadership",
          "email": "priya.okafor@northwind-analytics.com",
          "linkedin_url": "https://www.linkedin.com/in/example"
        }
      ]
    }
  ]
}
```

Notes that matter:

- **`contains_real_accounts`** is load-bearing, not cosmetic. When true the console's
  banner says the *companies are real but the engagement is simulated*, instead of
  claiming nothing on screen is real. Set it honestly — it is the difference between
  an accurate statement and a misleading one in front of an audience.
- **`persona`** must be one of `sales-leadership`, `revops`, `partnerships`,
  `sdr-bdr`. Omit it and the importer derives it from the title using the same
  classifier the live pipeline uses.
- **Contacts at real companies:** if `contains_real_accounts` is true, prefer
  invented people. A real named individual shown next to a fabricated reply they
  never sent is the one combination worth avoiding.
- **Emails** are only ever displayed, never sent — demo mode refuses every write.
  They still shouldn't be real deliverable addresses for real people.

## `signals.json`

Signals keyed by domain, mirroring the three kinds the live pipeline caches. Any
subset is fine; missing kinds render as "not detected".

```json
{
  "version": 1,
  "signals": {
    "northwind-analytics.com": {
      "research": {
        "signal": "Northwind Analytics announced a $24M Series B to expand GTM.",
        "has_recent": true,
        "source_url": "https://example.com/press",
        "researched_at": "2026-07-28T00:00:00Z"
      },
      "tech": {
        "line": "Salesforce · Outreach · 6sense",
        "detections": ["Salesforce", "Outreach", "6sense"],
        "playbook": ["sequencing", "intent"]
      },
      "hiring": {
        "line": "14 open roles · 4 sales: SDR; AE; VP Sales",
        "active_count": 14,
        "sales_count": 4,
        "sales_titles": ["SDR", "AE", "VP Sales"]
      }
    }
  }
}
```

`playbook` values come from `tech_signals.playbook_groups()` — `sequencing`, `intent`,
`ads`. They drive which play the generated copy leans on, so getting them right is
what makes the demo copy read like real output for that account.

## `profile.yaml`

Identity plus the performance shape you want the Trends charts to tell. The
generator derives campaign stats and the reply set from this, so the numbers
reconcile across every view.

```yaml
id: acme
label: Acme Corp demo
customer: Acme Corp
description: Tailored walkthrough for the Acme GTM team.

window:
  start: 2026-01-01
  end: 2026-06-30

contacts: 140

offers:
  - name: Demo request
    geo: Americas
    contacted: 620
    interested_rate_pct: 2.4      # the starved winner
  - name: Lead magnet (PDF)
    geo: Global
    contacted: 9100
    interested_rate_pct: 0.62     # the workhorse
  - name: Persona-targeted
    geo: Americas
    contacted: 5200
    interested_rate_pct: 0.10     # the one to retire

sequence:                          # step-3 trough, step-4 spike
  - { step: 1, reach_share: 1.00, interested_rate_pct: 0.150 }
  - { step: 2, reach_share: 0.67, interested_rate_pct: 0.190 }
  - { step: 3, reach_share: 0.58, interested_rate_pct: 0.059 }
  - { step: 4, reach_share: 0.48, interested_rate_pct: 0.450 }

snapshots:                         # differenced by the console into a rate trend
  - { at: 2026-01-31, new_contacted: 1800, interested_rate_pct: 0.94 }
  - { at: 2026-03-31, new_contacted: 3600, interested_rate_pct: 0.36 }
  - { at: 2026-06-30, new_contacted: 3740, interested_rate_pct: 0.96 }

copy:
  mode: generate                   # generate | template
```

### `copy.mode`

- **`generate`** (recommended) — run the real persona agents against these accounts
  **once, at authoring time**, and freeze the output into the profile. The demo then
  showcases the product's actual writing, stays deterministic at runtime, and costs
  nothing during the demo itself. Requires an API key at build time.
- **`template`** — fill the built-in templates. Instant and free, but reads
  templated once you scroll a few rows.

Live generation during a demo is deliberately not an option: too slow, non-deterministic,
and it would spend money on every walkthrough.

---

## Invariants any importer must hold

1. **Never write outside `data/demo/<id>/`.** A profile build must not touch live data.
2. **Keep it deterministic.** Same spec in, byte-identical profile out, so screenshots
   and rehearsed demos stay stable. Seed anything random from the profile id.
3. **Make the tree reconcile.** The accounts in `targets.json` are the accounts in the
   DB, in the signals, in the copy and in the replies. Totals in `campaign-stats/`
   must match the reply set — a demo where the numbers disagree is worse than none.
4. **Match the live schemas.** Generated copy uses the nested `email` / `linkedin`
   object shape from `generate_batch.py`; the DB uses `batch_db.init_schema`. Both are
   enforced by the console reading them — a flat copy asset 500s the Outreach view.
5. **Set `contains_real_accounts` truthfully.** It changes what the console tells the
   room.

## What still needs building

- `--spec` in `make_demo_profile.py`: read the three files, validate, emit the profile.
- The `copy.mode: generate` path: drive the persona agents at build time and freeze.
- Profile-scoped AI SDR attribution. Attribution lives in Mongo rather than under
  `data/`, so `R()` can't reach it; demo mode currently reports it as unavailable
  (`demo_unavailable: true`) rather than showing live deal values. Giving a profile
  its own attribution fixture would make the tiles demoable.
- Profile-scoped orchestration config. The Setup view reads pipeline *configuration*
  (persona → campaign routing) from `.claude/`, not customer data, so it shows the
  live wiring in a demo. Harmless today, but a customer-tailored profile would want
  its own persona and campaign names.
- Scripted playback, so a demo can *show* a batch running while writes stay refused.
