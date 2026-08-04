#!/usr/bin/env python3
"""Spend and capacity — what enrichment costs, and how much sending we can actually do.

Two different scarce things, deliberately measured through one ledger
(`batch_db.usage_ledger`) because the question is the same shape for both:

  CREDITS  Clay and Prospeo bill per call. Money, unbounded, spent by background
           jobs without a human watching.
  SENDS    LinkedIn allows ~20 connection/message actions per connected account per
           DAY before the account is at risk. Email is capped by the sending plan,
           15,000 per MONTH by default. Finite, resets, and blowing through it
           damages deliverability or gets a LinkedIn account restricted.

Report-only: nothing here blocks an action. It makes the number visible and tells
the channel recommender how much room is left, which is the input that matters —
"who should we call instead" is only answerable if you know sending is full.

Configured via env (see .env.example):
  LINKEDIN_SENDS_PER_ACCOUNT_DAY   default 20
  LINKEDIN_CONNECTED_ACCOUNTS      default from HEYREACH_LINKEDIN_ACCOUNT_ID count
  EMAIL_SENDS_PER_MONTH            default 15000

CLI:
    python3 capacity.py status [--json]
    python3 capacity.py spend [--days 30] [--json]
"""

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone

import batch_db as db

DEFAULT_LI_PER_ACCOUNT_DAY = 20
DEFAULT_EMAIL_PER_MONTH = 15000

# What each metered operation costs, for estimating BEFORE spending. Clay bills per
# company searched plus per email revealed; the per-company number is the one we can
# predict, so estimates are stated as a floor and labelled as such.
CREDIT_COSTS = {
    ("clay", "find-contacts"): 1.0,      # per company searched
    ("clay", "reveal-email"): 1.0,       # per contact with an email returned
    ("prospeo", "enrich-company"): 1.0,  # per non-cached domain
}


def _int_env(name, default):
    raw = (os.environ.get(name) or "").strip()
    try:
        return int(raw) if raw else default
    except ValueError:
        return default


def _connected_li_accounts():
    """How many LinkedIn sender accounts are wired. HeyReach takes a comma-separated
    list of account ids; the count of those is the sender pool."""
    explicit = _int_env("LINKEDIN_CONNECTED_ACCOUNTS", 0)
    if explicit:
        return explicit
    raw = (os.environ.get("HEYREACH_LINKEDIN_ACCOUNT_ID") or "").strip()
    n = len([x for x in raw.replace(";", ",").split(",") if x.strip()])
    return n or 1


def limits():
    """The configured ceilings, with where each number came from."""
    accounts = _connected_li_accounts()
    per_day = _int_env("LINKEDIN_SENDS_PER_ACCOUNT_DAY", DEFAULT_LI_PER_ACCOUNT_DAY)
    return {
        # limits() is the static ceilings only — it takes no connection, so usage
        # (which needs one) belongs in status() and nowhere else.
        "credits": {"budget": credits_budget(), "window": "month", "tier": "advanced"},
        "linkedin": {
            "accounts": accounts,
            "per_account_day": per_day,
            "per_day": accounts * per_day,
            "window": "day",
            "note": f"{accounts} connected account(s) x {per_day}/day. "
                    "Going past this risks the LinkedIn accounts, not just deliverability.",
        },
        "email": {
            "per_month": _int_env("EMAIL_SENDS_PER_MONTH", DEFAULT_EMAIL_PER_MONTH),
            "window": "month",
            "note": "Sending-plan ceiling for the month.",
        },
    }


def _day_start():
    n = datetime.now(timezone.utc)
    return n.replace(hour=0, minute=0, second=0, microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


def _month_start():
    n = datetime.now(timezone.utc)
    return n.replace(day=1, hour=0, minute=0, second=0,
                     microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


def credits_budget():
    """Monthly enrichment credit allowance (the Advanced tier number).

    Read from the environment so a bespoke contract shows the customer's figure
    rather than the list price. 0 disables the meter for a deployment that buys
    credits directly from the provider."""
    raw = (os.environ.get("ADVANCED_CREDITS_PER_MONTH") or "").strip()
    try:
        return int(raw) if raw else 15000
    except ValueError:
        return 15000


def status(conn):
    """Capacity used vs available, right now.

    LinkedIn is a DAILY window and email a MONTHLY one, so they are counted against
    different clocks — reporting both as "this month" would hide the constraint that
    actually bites (today's LinkedIn allowance)."""
    lim = limits()
    li_used = db.usage_sum(conn, provider="heyreach", unit_kind="sends", since=_day_start())
    em_used = db.usage_sum(conn, provider="bison", unit_kind="sends", since=_month_start())
    li_cap = lim["linkedin"]["per_day"]
    em_cap = lim["email"]["per_month"]
    cred_cap = credits_budget()
    cred_used = db.usage_sum(conn, unit_kind="credits", since=_month_start())
    return {
        # Enrichment credits are the Advanced tier's metered allowance. Monthly
        # window like email, and counted across every provider — a credit is a
        # credit whoever billed it.
        "credits": {
            "budget": cred_cap, "used": cred_used,
            "remaining": max(0, cred_cap - cred_used),
            "pct": round(100 * cred_used / cred_cap, 1) if cred_cap else None,
            "window": "month", "resets": "monthly", "tier": "advanced",
            "note": "Included with the Advanced tier.",
        },
        "linkedin": {
            **lim["linkedin"],
            "used": li_used, "remaining": max(0, li_cap - li_used),
            "pct": round(100 * li_used / li_cap, 1) if li_cap else None,
            "resets": "daily",
        },
        "email": {
            **lim["email"],
            "used": em_used, "remaining": max(0, em_cap - em_used),
            "pct": round(100 * em_used / em_cap, 1) if em_cap else None,
            "resets": "monthly",
        },
    }


def spend(conn, days=30, campaign_id=None):
    """Credit spend over a window, by provider and operation."""
    since = (datetime.now(timezone.utc) - timedelta(days=int(days))).strftime(
        "%Y-%m-%dT%H:%M:%SZ")
    totals = db.usage_totals(conn, since=since, campaign_id=campaign_id)
    credits = sum(r["units"] for r in totals["by_provider"] if r["unit_kind"] == "credits")
    return {"days": days, "since": since, "credits": credits, **totals,
            "recent": db.usage_recent(conn, limit=25)}


def estimate(operations):
    """Predicted credit cost for planned work.

    operations: [(provider, operation, count), ...]
    Returned as a FLOOR, not a quote — Clay bills per company searched plus per email
    revealed, and the reveal count is unknown until the search returns.
    """
    total, lines = 0.0, []
    for provider, op, count in operations:
        unit = CREDIT_COSTS.get((provider, op), 1.0)
        cost = unit * int(count)
        total += cost
        lines.append({"provider": provider, "operation": op,
                      "count": int(count), "unit": unit, "credits": cost})
    return {"credits_floor": total, "lines": lines,
            "note": "Floor only — Clay also bills per email revealed, which is not "
                    "known until the search returns."}


# ---- channel recommendation ------------------------------------------------
# Priority says WHO. Capacity says HOW MUCH. Together they say WHERE to spend:
# a hot decision-maker is worth a human call and a LinkedIn touch, a cool influencer
# is worth an ad impression and nothing more. Sending capacity is finite, so a score
# that ranks people without saying which channel to use leaves the scarce resource
# (LinkedIn actions, rep time) allocated by whoever scrolls first.

# Roles senior enough to justify a rep's time on the phone. These are the labels
# `buyer_group.buyer_role()` produces — the project's existing taxonomy, reused so
# there is one definition of who counts as a decision-maker.
SENIOR_ROLES = ("CRO / Sales Chief", "VP/Head/Dir Sales-GTM", "Founder/CEO")

CHANNEL_RULES = {
    "call":     {"min_score": 70, "roles": SENIOR_ROLES,
                 "why": "Hot signal on a buyer who can sign — worth a rep's time."},
    "linkedin": {"min_score": 45, "roles": None,
                 "why": "Warm enough to justify a limited daily LinkedIn action."},
    "email":    {"min_score": 0, "roles": None,
                 "why": "Email is the cheapest channel — everyone qualified gets it."},
    "ads":      {"min_score": 0, "roles": None,
                 "why": "Cheap reach across the whole buyer group, including the "
                        "people not worth a direct touch."},
}


def _senior_roles(conn=None):
    """Role labels worth a rep's call, from the configured buyer group.

    SENIOR_ROLES below is the fallback for a DB predating the ruleset. Reading the
    config means flipping "worth calling" on a role in the console immediately
    changes the channel recommendation."""
    try:
        import buyer_group_config as _bg
        own = conn is None
        c = conn or db.connect()
        try:
            return _bg.senior_labels(c) or SENIOR_ROLES
        finally:
            if own:
                c.close()
    except Exception:  # noqa: BLE001
        return SENIOR_ROLES


def recommend_channels(score, buyer_role=None, li_remaining=None, email_remaining=None,
                       senior_roles=None):
    """{channel: bool} + reasons, for one contact.

    li_remaining/email_remaining let a full allowance downgrade the recommendation
    rather than promising a send that cannot happen today: when LinkedIn is out of
    room, only the very top of the list keeps it.
    """
    score = score or 0
    role = buyer_role or ""
    senior = senior_roles if senior_roles is not None else _senior_roles()
    out, why = {}, {}
    for ch, rule in CHANNEL_RULES.items():
        ok = score >= rule["min_score"]
        allowed = senior if rule["roles"] else None
        if ok and allowed and role and role not in allowed:
            ok = False
        out[ch], why[ch] = ok, rule["why"]

    # Ads are for the buyer group as a whole, so they stay on for everyone — that is
    # the point of the channel. Direct channels tighten when capacity is short.
    if li_remaining is not None and li_remaining <= 0:
        if out["linkedin"] and score < 70:
            out["linkedin"] = False
            why["linkedin"] = "Daily LinkedIn allowance is used up — reserved for hot only."
    if email_remaining is not None and email_remaining <= 0:
        out["email"] = False
        why["email"] = "Monthly email allowance is used up."
    return {"channels": out, "why": why,
            "primary": next((c for c in ("call", "linkedin", "email", "ads") if out.get(c)), None)}


def ad_audience(members, min_members=2):
    """Accounts worth running ads at, from a scored member list.

    Ads are an ACCOUNT play, not a contact play: the case for spending on them is
    that they reach the whole buyer group cheaply, including the influencers who
    aren't worth a direct touch. So an account qualifies on the strength of its
    buying committee, not any one person — which is why `min_members` exists.
    """
    by_domain = {}
    for m in members:
        d = m.get("domain")
        if not d:
            continue
        slot = by_domain.setdefault(d, {"domain": d, "company": m.get("company"),
                                        "contacts": 0, "best_score": 0, "roles": set()})
        slot["contacts"] += 1
        slot["best_score"] = max(slot["best_score"], m.get("priority_score") or 0)
        if m.get("buyer_role"):
            slot["roles"].add(m["buyer_role"])
    out = []
    for d in by_domain.values():
        if d["contacts"] < min_members:
            continue
        d["roles"] = sorted(d["roles"])
        d["reason"] = (f"{d['contacts']} mapped buyers, best signal {round(d['best_score'])}")
        out.append(d)
    out.sort(key=lambda x: (-x["best_score"], -x["contacts"]))
    return out


# ---- CLI -------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status").add_argument("--json", action="store_true")
    p = sub.add_parser("spend")
    p.add_argument("--days", type=int, default=30)
    p.add_argument("--json", action="store_true")
    args = ap.parse_args()

    conn = db.connect()
    db.init_schema(conn)
    if args.cmd == "status":
        st = status(conn)
        if args.json:
            print(json.dumps(st, ensure_ascii=False))
        else:
            li, em = st["linkedin"], st["email"]
            print(f"LinkedIn  {li['used']:.0f}/{li['per_day']} today "
                  f"({li['accounts']} account(s) x {li['per_account_day']})")
            print(f"Email     {em['used']:.0f}/{em['per_month']} this month")
        return 0
    res = spend(conn, days=args.days)
    print(json.dumps(res, ensure_ascii=False, indent=2 if not args.json else None))
    return 0


if __name__ == "__main__":
    sys.exit(main())
