#!/usr/bin/env python3
"""Audience resolution — WHICH accounts and contacts are in scope for a campaign.

A campaign has two independent filters, and keeping them separate is what makes the
workflow legible:

    audience      which accounts/contacts are in the pool at all
    signal_query  which of those are worth working right now

"Everyone on HubSpot list 2198" is an audience. "Every contact on a deal we lost in
the last 30 days" is an audience. "Showed a funding signal in the last 14 days" is a
signal query. You want both: re-engage last month's closed-lost, but lead with
whichever of them just raised.

Audience types
--------------
all_contacts   everything already pulled into the pipeline (the default, and what
               campaigns did before audiences existed)
hubspot_list   a HubSpot list, static or dynamic — membership resolves CRM-side
upload         a list that arrived as a FILE — an event export, badge scans — and
               was imported through contact_import.py. Resolves LOCALLY against the
               recorded import, so the audience keeps meaning "the people from that
               list" after the file is long gone
crm_query      a CRM segment computed live. Presets today:
                 closed_lost      contacts on deals lost in the last N days
                 closed_won       contacts on deals won in the last N days
                 no_deal          contacts with no associated deal at all
                 lifecycle        contacts at a given lifecycle stage

Everything resolves to a set of CONTACT ids that already exist locally, plus the set
of DOMAINS in scope (which is what enrichment then goes and finds new contacts at).

CLI:
    python3 audiences.py presets [--json]
    python3 audiences.py resolve '<audience json>' [--json] [--limit N]
"""

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone

import batch_db as db

AUDIENCE_TYPES = ("all_contacts", "hubspot_list", "crm_query", "upload")

# CRM segment presets. Each is a recipe over deals/contacts that the console can run
# against HubSpot without the user writing a filter. `needs_deals` marks the ones
# that have to walk deal -> contact associations.
CRM_PRESETS = {
    "closed_lost": {
        "label": "Closed-lost recently",
        "description": "Every contact on a deal that closed lost in the window. "
                       "The highest-intent cold audience there is — they evaluated "
                       "you and something changed.",
        "needs_deals": True, "won": False, "default_days": 30,
    },
    "closed_won": {
        "label": "Closed-won recently",
        "description": "Contacts on recently won deals — for expansion or referral "
                       "motions, not cold outbound.",
        "needs_deals": True, "won": True, "default_days": 90,
    },
    "no_deal": {
        "label": "Never had a deal",
        "description": "Contacts with no associated deal at all — pure cold.",
        "needs_deals": False, "default_days": None,
    },
    "lifecycle": {
        "label": "At a lifecycle stage",
        "description": "Contacts sitting at a given HubSpot lifecycle stage.",
        "needs_deals": False, "default_days": None,
    },
}

# Deal-stage matching. HubSpot stage VALUES are pipeline-specific internal ids, so
# the stage must be resolved from its label at runtime; matching on the label text is
# the only portable way across portals and pipelines.
_LOST_HINTS = ("closed lost", "closedlost", "lost")
_WON_HINTS = ("closed won", "closedwon", "won")


def validate_audience(a):
    """Normalize, or raise ValueError. None/{} means all_contacts."""
    a = dict(a or {})
    if not a:
        return {"type": "all_contacts"}
    t = a.get("type") or "all_contacts"
    if t not in AUDIENCE_TYPES:
        raise ValueError(f"audience type must be one of {list(AUDIENCE_TYPES)}")
    out = {"type": t}
    if t == "hubspot_list":
        lid = str(a.get("list_id") or "").strip()
        if not lid:
            raise ValueError("hubspot_list audience needs a list_id")
        out["list_id"] = lid
        out["list_name"] = a.get("list_name")
    elif t == "upload":
        try:
            out["import_id"] = int(a.get("import_id"))
        except (TypeError, ValueError):
            raise ValueError("upload audience needs an import_id")
        out["label"] = a.get("label")
    elif t == "crm_query":
        preset = a.get("preset")
        if preset not in CRM_PRESETS:
            raise ValueError(f"preset must be one of {sorted(CRM_PRESETS)}")
        out["preset"] = preset
        spec = CRM_PRESETS[preset]
        if spec["default_days"] is not None:
            days = a.get("days", spec["default_days"])
            try:
                days = int(days)
            except (TypeError, ValueError):
                raise ValueError("days must be an integer")
            if not 1 <= days <= 3650:
                raise ValueError("days must be between 1 and 3650")
            out["days"] = days
        if preset == "lifecycle":
            stage = str(a.get("lifecycle_stage") or "").strip()
            if not stage:
                raise ValueError("lifecycle audience needs a lifecycle_stage")
            out["lifecycle_stage"] = stage
    return out


def describe(a):
    """One human sentence for the audience — used in the UI and the campaign header."""
    a = validate_audience(a)
    t = a["type"]
    if t == "all_contacts":
        return "All contacts in the pipeline"
    if t == "hubspot_list":
        return f"HubSpot list {a.get('list_name') or a['list_id']}"
    if t == "upload":
        return "Imported list: " + (a.get("label") or f"import #{a['import_id']}")
    spec = CRM_PRESETS[a["preset"]]
    if a.get("days"):
        return f"{spec['label']} (last {a['days']} days)"
    if a.get("lifecycle_stage"):
        return f"Lifecycle stage: {a['lifecycle_stage']}"
    return spec["label"]


# ---- resolution ------------------------------------------------------------
class LiveCRM:
    """The real portal — the default source the two CRM-backed audience types read.

    It exists as an OBJECT rather than a pair of module functions so a caller can
    substitute a stand-in (demo mode's simulated CRM) by passing it in. A
    module-level global would have been shorter and wrong: the server is threaded,
    so swapping one would leak one request's data source into another's.
    """

    def list_members(self, list_id):
        return _hubspot_list_members(list_id)

    def crm_query(self, a):
        return _crm_query_contacts(a)


LIVE = LiveCRM()


def resolve(conn, audience, limit=None, crm=None):
    """{contact_ids, domains, source, stats} for an audience.

    Only ever returns contacts that already exist LOCALLY. A HubSpot list or CRM
    segment naming people we have never pulled reports them under
    `stats.not_in_pipeline` rather than inventing rows — pulling them is a separate,
    explicit step (the Source tab), because it changes the contact pool.

    `crm` overrides where list membership and CRM segments are read from; it must
    expose LiveCRM's two methods. Demo mode passes its own so an audience resolves
    against the profile instead of reporting a missing HubSpot token.
    """
    crm = crm or LIVE
    a = validate_audience(audience)
    t = a["type"]
    if t == "all_contacts":
        rows = [dict(r) for r in conn.execute(
            "SELECT contact_id, domain FROM contacts WHERE domain IS NOT NULL AND domain != ''")]
        return _pack(rows, a, {})

    if t == "upload":
        # Resolved locally: an imported list IS a recorded set of contacts, so it
        # needs no CRM round-trip and stays resolvable in a demo, offline, or after
        # the source file is gone.
        ids, stats = _import_members(conn, a["import_id"])
    elif t == "hubspot_list":
        ids, stats = crm.list_members(a["list_id"])
    else:
        ids, stats = crm.crm_query(a)

    if not ids:
        return _pack([], a, stats)
    # intersect with what we actually hold
    placeholders = ",".join("?" * len(ids))
    rows = [dict(r) for r in conn.execute(
        f"SELECT contact_id, domain FROM contacts WHERE contact_id IN ({placeholders})",
        [str(i) for i in ids])]
    stats["from_crm"] = len(ids)
    stats["not_in_pipeline"] = len(ids) - len(rows)
    return _pack(rows, a, stats, limit=limit)


def _pack(rows, audience, stats, limit=None):
    if limit:
        rows = rows[:int(limit)]
    return {
        "audience": audience,
        "description": describe(audience),
        "contact_ids": [r["contact_id"] for r in rows],
        "domains": sorted({r["domain"] for r in rows if r.get("domain")}),
        "contacts": len(rows),
        "stats": stats,
    }


def _import_members(conn, import_id):
    """Contact ids recorded against a file import. An import that no longer exists
    yields nothing WITH an error, never everyone — the same rule every other
    audience failure follows, because silently widening a campaign to the whole
    pipeline is the one outcome worth guarding hardest against."""
    try:
        rows = [str(r["contact_id"]) for r in conn.execute(
            "SELECT contact_id FROM contact_import_members WHERE import_id=?",
            (int(import_id),))]
    except Exception as e:  # noqa: BLE001 — table missing on an older DB
        return [], {"error": f"{type(e).__name__}: {e}"}
    if not rows:
        return [], {"error": f"import #{import_id} has no contacts on record"}
    return rows, {"imported": len(rows)}


def _client():
    import hubspot_client
    return hubspot_client.HubSpotClient()


def _hubspot_list_members(list_id):
    try:
        return list(_client().get_list_members(list_id)), {}
    except Exception as e:  # noqa: BLE001 — surfaced, never fatal
        return [], {"error": f"{type(e).__name__}: {e}"}


def _stage_ids(client, won):
    """Internal dealstage ids whose LABEL reads as closed won/lost.

    Stage values are pipeline-specific ids, so the label is the only portable
    handle. Reading the property definition's enum options needs no extra scope —
    the same trick the attribution sync uses for owner names.
    """
    hints = _WON_HINTS if won else _LOST_HINTS
    options = (client.get_property("deals", "dealstage") or {}).get("options") or []
    out = []
    for o in options:
        label = str(o.get("label") or "").lower()
        if any(h in label for h in hints):
            out.append(str(o.get("value")))
    return out


def _crm_query_contacts(a):
    """Contact ids for a CRM preset. Returns (ids, stats)."""
    preset = a["preset"]
    spec = CRM_PRESETS[preset]
    try:
        client = _client()
    except Exception as e:  # noqa: BLE001
        return [], {"error": f"{type(e).__name__}: {e}"}

    if not spec["needs_deals"]:
        return _contact_only_query(client, a)

    try:
        stages = _stage_ids(client, spec["won"])
        if not stages:
            return [], {"error": "no closed-"
                        + ("won" if spec["won"] else "lost")
                        + " stage found in this portal's deal pipelines"}
        cutoff = datetime.now(timezone.utc) - timedelta(days=int(a["days"]))
        cutoff_ms = str(int(cutoff.timestamp() * 1000))
        deal_ids, after = [], None
        while True:
            results, after, _total = client.search_page(
                "deals",
                filters=[{"propertyName": "dealstage", "operator": "IN", "values": stages},
                         {"propertyName": "closedate", "operator": "GTE", "value": cutoff_ms}],
                properties=["dealname", "dealstage", "closedate", "amount"],
                sorts=[{"propertyName": "hs_object_id", "direction": "ASCENDING"}],
                after=after)
            deal_ids += [r["id"] for r in results]
            if not after or len(deal_ids) >= 9800:
                break
        if not deal_ids:
            return [], {"deals": 0}
        assoc = client.batch_read_associations("deals", "contacts", deal_ids)
        ids = sorted({c for lst in assoc.values() for c in lst})
        return ids, {"deals": len(deal_ids), "stages_matched": len(stages)}
    except Exception as e:  # noqa: BLE001
        return [], {"error": f"{type(e).__name__}: {e}"}


def _contact_only_query(client, a):
    preset = a["preset"]
    try:
        if preset == "lifecycle":
            filters = [{"propertyName": "lifecyclestage", "operator": "EQ",
                        "value": a["lifecycle_stage"]}]
        else:  # no_deal — HubSpot has no "has no association" filter, so invert
            return _no_deal_contacts(client)
        ids, after = [], None
        while True:
            results, after, _total = client.search_page(
                "contacts", filters=filters, properties=["email"],
                sorts=[{"propertyName": "hs_object_id", "direction": "ASCENDING"}],
                after=after)
            ids += [r["id"] for r in results]
            if not after or len(ids) >= 9800:
                break
        return ids, {}
    except Exception as e:  # noqa: BLE001
        return [], {"error": f"{type(e).__name__}: {e}"}


def _no_deal_contacts(client):
    """Contacts with no associated deal.

    Computed locally by subtraction: HubSpot search cannot express "has no
    association", so we take the contacts we hold and remove any that HubSpot
    reports a deal for. Bounded by the local pool, not the portal.
    """
    conn = db.connect()
    try:
        local = [r["contact_id"] for r in conn.execute("SELECT contact_id FROM contacts")]
    finally:
        conn.close()
    if not local:
        return [], {}
    with_deal = set()
    for i in range(0, len(local), 100):
        chunk = local[i:i + 100]
        assoc = client.batch_read_associations("contacts", "deals", chunk)
        with_deal |= {cid for cid, deals in assoc.items() if deals}
    ids = [c for c in local if c not in with_deal]
    return ids, {"checked": len(local), "with_deal": len(with_deal)}


# ---- CLI -------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("presets").add_argument("--json", action="store_true")
    p = sub.add_parser("resolve")
    p.add_argument("audience", help="JSON, e.g. '{\"type\":\"crm_query\",\"preset\":\"closed_lost\",\"days\":30}'")
    p.add_argument("--limit", type=int)
    p.add_argument("--json", action="store_true")
    args = ap.parse_args()

    if args.cmd == "presets":
        out = [{"id": k, **{kk: vv for kk, vv in v.items() if kk != "needs_deals"}}
               for k, v in CRM_PRESETS.items()]
        print(json.dumps({"presets": out, "types": list(AUDIENCE_TYPES)}, indent=2))
        return 0

    conn = db.connect()
    db.init_schema(conn)
    try:
        res = resolve(conn, json.loads(args.audience), limit=args.limit)
    except ValueError as e:
        print(f"invalid audience: {e}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(res, ensure_ascii=False))
    else:
        print(f"{res['description']}: {res['contacts']} contacts, "
              f"{len(res['domains'])} accounts")
        for k, v in (res["stats"] or {}).items():
            print(f"  {k}: {v}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
