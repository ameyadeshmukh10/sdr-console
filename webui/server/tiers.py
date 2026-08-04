"""Packaging — which parts of the console are separately-sold agents and add-ons.

The console reads as one product, which is right for using it and wrong for selling
it: several of the most valuable pieces are individually-priced agents, and nothing
on screen said so. This is the registry that lets the UI mark them.

Deliberately understated. A badge that shouts turns the console into a pricing page
and gets tuned out by the people using it daily; a badge that is merely *present* is
something a rep can point at mid-demo and say "that's the Technographic agent, it's
on Advanced". So: a small tier pill next to the feature's own heading, with the
detail in the tooltip. Never a modal, never an upsell interstitial, never a lock.

Tiers
  core      included in the base SDR Worker
  scale     the Scale package — the technographic and hiring signal agents,
            and contact phone reveal
  advanced  enrichment credits, the enrichment connector, CRM sync, scoring and
            advanced analytics; the tier that carries the monthly credit budget

"Custom setup" is a PROPERTY, not a tier: several Advanced items connect to a
customer's own provider or CRM and are scoped per deployment. Modelling it as a flag
rather than a fourth tier keeps the ladder readable (three rungs, ascending) while
still letting a rep say "and that one is a custom setup".

The middle package is the "Scale" package.

`ADVANCED_CREDITS_PER_MONTH` is the number quoted on the Advanced tier, and it is
what the Capacity view reports enrichment spend against — so "how much of my
allowance have I used" has one answer in one place.
"""

import os

ADVANCED_CREDITS_PER_MONTH = 15000

TIERS = {
    "core": {
        "label": "Core",
        "rank": 0,
        "blurb": "Included with the SDR AI Worker.",
    },
    "scale": {
        "label": "Scale",
        "rank": 1,
        "blurb": "Scale package — the technographic and hiring signal agents, plus contact phone numbers.",
    },
    "advanced": {
        "label": "Advanced",
        "rank": 2,
        "blurb": f"Advanced tier — enrichment, scoring and analytics, including "
                 f"{ADVANCED_CREDITS_PER_MONTH:,} enrichment credits per month.",
    },
}
CUSTOM_SETUP_NOTE = ("Connects to your own provider or CRM account — scoped per "
                     "deployment.")

# One entry per separately-sold capability. `where` is documentation for us, not
# rendered: it records which surface carries the badge so this list and the UI can
# be reconciled by reading rather than by clicking through every page.
ADDONS = {
    "technographic-agent": {
        "name": "Technographic Signal Agent",
        "tier": "scale",
        "what": "Detects the GTM stack an account runs — CRM, ad pixels, intent and "
                "sequencing tools — from their website and DNS, with no third-party "
                "data fee. Drives the no-disruption and signal-activation plays.",
        "where": "Signals view, campaign Find accounts",
    },
    "hiring-agent": {
        "name": "Hiring Signal Agent",
        "tier": "scale",
        "what": "Reads live job postings per account and isolates the sales/GTM roles, "
                "which becomes the opener for touch 2.",
        "where": "Signals view, campaign Find accounts",
    },
    "enrichment-connector": {
        "name": "Enrichment Connector",
        "tier": "advanced", "custom_setup": True,
        "what": "Finds the rest of the buying committee at an account through your "
                "enrichment provider. Connected to your own account, so the credits "
                "and the data contract stay yours.",
        "where": "campaign Find accounts, Source contacts",
    },
    "lead-scoring": {
        "name": "Lead Scoring Agent",
        "tier": "advanced",
        "what": "Scores every qualified contact on signal strength, recency and buyer "
                "fit, tracks how that score moves over time, and turns it into a "
                "prioritised call list and a per-contact channel recommendation.",
        "where": "Call list, Hot targets, campaign Call list",
    },
    "advanced-analytics": {
        "name": "Advanced Analytics",
        "tier": "advanced",
        "what": "The end-to-end funnel, attribution to deals and revenue, and the "
                "targeting tests that show whether the scoring model actually "
                "predicts replies.",
        "where": "Analytics funnel + Trends targeting",
    },
    "crm-sync": {
        "name": "CRM Field Sync",
        "tier": "advanced", "custom_setup": True,
        "what": "Writes computed signals and scores into your CRM's own properties and "
                "reads them back as the source of truth. Field mapping is configured "
                "per CRM.",
        "where": "CRM fields",
    },
    "phone-reveal": {
        "name": "Contact Phone Numbers",
        "tier": "scale",
        "what": "Direct dial and mobile on the contact record, so a call the score "
                "recommends can actually be made without leaving the console.",
        "where": "Call list, Hot targets, Home signals drill-down",
    },
    "buyer-group": {
        "name": "Buyer Group Definition",
        "tier": "advanced",
        "what": "Defines which titles count as buyers, which persona writes to them, "
                "and who is worth a call — driving enrichment search, the ICP gate "
                "and channel choice from one place.",
        "where": "Buyer group",
    },
}


def payload():
    """The registry, for the UI. Static — no host state leaks through it."""
    return {
        "tiers": TIERS,
        "addons": [{"id": k, **v} for k, v in ADDONS.items()],
        "advanced_credits_per_month": credits_budget(),
        "custom_setup_note": CUSTOM_SETUP_NOTE,
    }


def credits_budget():
    """The Advanced tier's monthly enrichment credit allowance.

    Overridable per deployment, because a customer on a bespoke contract has a
    different number and the Capacity view must show theirs, not the list price."""
    raw = (os.environ.get("ADVANCED_CREDITS_PER_MONTH") or "").strip()
    try:
        return int(raw) if raw else ADVANCED_CREDITS_PER_MONTH
    except ValueError:
        return ADVANCED_CREDITS_PER_MONTH
