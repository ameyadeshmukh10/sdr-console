#!/usr/bin/env python3
"""The buyer group, as configuration.

Who we sell to used to be stated in four separate hardcoded places that could drift
apart without anyone noticing:

    ai-sdr/scripts/buyer_group.py   regexes -> role label + is_icp
    clay_enrich.JOB_TITLE_KEYWORDS  what Clay is asked to search for
    clay_enrich._IC_TITLE_RE        what we throw away when it comes back
    capacity.SENIOR_ROLES           who is worth a rep's phone call

They are one definition. This module is that definition, read from
`buyer_group_roles`, and everything above now asks it instead of holding its own copy.
Editing a role in the console therefore changes what Clay searches for, what survives
the gate, which persona writes the copy, and which channel the score recommends — in
one place, which is what "define the buyer group" has to mean to be worth anything.

ORDER IS THE LOGIC. Rules are evaluated by `sort_order`, first match wins:
  * the exclusion rule sits at 0, so "Account Executive" is rejected before any
    seniority rule can claim it;
  * RevOps sits above generic Sales, so "Sales Operations Manager" is RevOps.
Reordering rules changes classifications — that is the intended power and the main
way to get it wrong, so the console shows the order explicitly.

Falls back to the original `buyer_group.py` if the table is empty or unreadable, so
a partially-migrated DB classifies exactly as it did before.

CLI:
    python3 buyer_group_config.py list [--json]
    python3 buyer_group_config.py test "VP of Sales" "Account Executive" ...
    python3 buyer_group_config.py clay-terms [--json]
"""

import argparse
import json
import re
import sys
import threading

import batch_db as db

# Compiled-pattern cache. classify() runs per contact inside qualification loops, so
# recompiling the ruleset each call is not free. Invalidated by a cheap stamp rather
# than a TTL: an edit should take effect on the next call, not in 60 seconds.
_CACHE = {"stamp": None, "rules": None}
_LOCK = threading.Lock()


def _stamp(conn):
    row = conn.execute(
        "SELECT COUNT(*), COALESCE(MAX(updated_at),'') FROM buyer_group_roles").fetchone()
    return (row[0], row[1])


def _rules(conn):
    """Ordered (role, [compiled patterns]) — cached until the table changes."""
    try:
        stamp = _stamp(conn)
    except Exception:  # noqa: BLE001 — table missing on an old DB
        return None
    with _LOCK:
        if _CACHE["stamp"] == stamp and _CACHE["rules"] is not None:
            return _CACHE["rules"]
        rules = []
        for role in db.buyer_group_roles(conn, active_only=True):
            pats = []
            for p in role.get("match_patterns") or []:
                try:
                    pats.append(re.compile(p, re.I))
                except re.error:
                    # A bad regex typed in the console must not break classification
                    # for every other rule — skip it and keep going.
                    continue
            if pats:
                rules.append((role, pats))
        _CACHE["stamp"], _CACHE["rules"] = stamp, rules
        return rules


def invalidate():
    with _LOCK:
        _CACHE["stamp"], _CACHE["rules"] = None, None


def classify(conn, title):
    """{role_key, label, is_icp, seniority, persona, worth_calling} for a job title.

    Returns the NOT-ICP shape (is_icp False, label None) when nothing matches, which
    is the conservative default: an unrecognised title is not silently treated as a
    buyer."""
    t = " ".join((title or "").lower().split())
    if not t:
        return _miss()
    rules = _rules(conn)
    if rules is None:
        return _legacy(title)
    for role, pats in rules:
        if any(p.search(t) for p in pats):
            return {
                "role_key": role["role_key"],
                "label": role["label"] if role["is_icp"] else None,
                "is_icp": bool(role["is_icp"]),
                "seniority": role.get("seniority"),
                "persona": role.get("persona"),
                "worth_calling": bool(role.get("worth_calling")),
                "matched": role["label"],
            }
    return _miss()


def _miss():
    return {"role_key": None, "label": None, "is_icp": False, "seniority": None,
            "persona": None, "worth_calling": False, "matched": None}


def _legacy(title):
    """Pre-config behaviour, for a DB that predates the table."""
    try:
        import buyer_group
        label, is_icp = buyer_group.buyer_role(title)
        senior = label in ("CRO / Sales Chief", "VP/Head/Dir Sales-GTM", "Founder/CEO")
        return {"role_key": None, "label": label if is_icp else None,
                "is_icp": bool(is_icp), "seniority": None, "persona": None,
                "worth_calling": bool(is_icp and senior), "matched": label}
    except Exception:  # noqa: BLE001
        return _miss()


def role_label(conn, title):
    """Just the label, or None — the shape campaigns._buyer_role wants."""
    return classify(conn, title)["label"]


def is_icp(conn, title):
    return classify(conn, title)["is_icp"]


def worth_calling(conn, role_label_or_title, by_label=True):
    """Whether this role justifies a rep's call. Accepts a role LABEL (what
    campaign_members stores) or a raw title."""
    if not role_label_or_title:
        return False
    if by_label:
        for role in db.buyer_group_roles(conn, active_only=True):
            if role["label"] == role_label_or_title:
                return bool(role.get("worth_calling"))
        return False
    return classify(conn, role_label_or_title)["worth_calling"]


def senior_labels(conn):
    """Labels flagged worth_calling — what capacity.SENIOR_ROLES used to hardcode."""
    try:
        return tuple(r["label"] for r in db.buyer_group_roles(conn, active_only=True)
                     if r.get("worth_calling"))
    except Exception:  # noqa: BLE001
        return ("CRO / Sales Chief", "VP/Head/Dir Sales-GTM", "Founder/CEO")


def clay_search_terms(conn):
    """(include_titles, exclude_titles) for a Clay contact search.

    Include comes from every ICP role's `clay_titles`; exclude from the rules marked
    not-ICP. Clay's keyword match is fuzzy, so this is the first pass — `classify()`
    is still applied to everything that comes back, which is the actual guarantee.
    """
    include, exclude = [], []
    try:
        roles = db.buyer_group_roles(conn, active_only=True)
    except Exception:  # noqa: BLE001
        return ([], [])
    for r in roles:
        (include if r["is_icp"] else exclude).extend(r.get("clay_titles") or [])
    return (list(dict.fromkeys(include)), list(dict.fromkeys(exclude)))


def summary(conn):
    """The whole ruleset plus what it would do — for the console."""
    roles = db.buyer_group_roles(conn, active_only=False)
    inc, exc = clay_search_terms(conn)
    return {"roles": roles, "clay_include": inc, "clay_exclude": exc,
            "seniorities": ["decision-maker", "champion", "influencer", "excluded"],
            "personas": ["sales-leadership", "revops", "partnerships", "sdr-bdr"]}


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list").add_argument("--json", action="store_true")
    p = sub.add_parser("test", help="classify one or more titles")
    p.add_argument("titles", nargs="+")
    p.add_argument("--json", action="store_true")
    sub.add_parser("clay-terms").add_argument("--json", action="store_true")
    args = ap.parse_args()

    conn = db.connect()
    db.init_schema(conn)
    if args.cmd == "list":
        rows = db.buyer_group_roles(conn, active_only=False)
        if args.json:
            print(json.dumps({"roles": rows}, ensure_ascii=False))
        else:
            for r in rows:
                flags = ("icp" if r["is_icp"] else "EXCLUDED") + \
                        (" · call" if r["worth_calling"] else "") + \
                        ("" if r["active"] else " · off")
                print(f"{r['sort_order']:>4}  {r['label']:<26} {flags:<20} "
                      f"{r.get('persona') or '-'}")
        return 0
    if args.cmd == "test":
        for t in args.titles:
            c = classify(conn, t)
            print(f"  {t:<38} -> {c['label'] or 'NOT-ICP':<24} "
                  f"{c['seniority'] or '-':<15} "
                  f"{'call' if c['worth_calling'] else ''}")
        return 0
    inc, exc = clay_search_terms(conn)
    print(json.dumps({"include": inc, "exclude": exc}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
