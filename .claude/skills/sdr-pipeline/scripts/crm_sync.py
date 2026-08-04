#!/usr/bin/env python3
"""CRM field wiring — the CRM is the source of truth.

The console computes things (an account signal, a priority score, a recommended
channel mix) and those have to live somewhere durable and readable by everyone else:
the CRM. Two rules make that coherent rather than a sync mess:

  1. The console PUSHES what it computes into a mapped CRM property.
  2. The console READS the CRM value back as AUTHORITATIVE.

So if RevOps edits `ai_sdr_account_signal` in HubSpot, that edit wins — the next
scan does not silently revert it. That is what "CRM is the source of truth" has to
mean in practice; the alternative (console always overwrites) is exactly the failure
the AI SDR attribution work already hit, where pre-existing manual flags had to be
preserved by hand.

The mapping itself is DATA (`batch_db.crm_field_map`), not code, so a different
portal — or Salesforce, where the API names differ entirely — is a config change.
`local_key` names what the console computes; `object_type` + `property_name` name
where it lives over there.

Conflict handling for `direction='both'`: a pull writes the CRM value into the local
cache and marks it CRM-owned. A push only overwrites the CRM when our value is
non-empty AND differs; we never write a blank over a human's text.

CLI:
    python3 crm_sync.py fields [--json]
    python3 crm_sync.py push --campaign <id> [--dry-run] [--json]
    python3 crm_sync.py pull [--limit N] [--dry-run] [--json]
    python3 crm_sync.py ensure [--dry-run]      # create missing CRM properties
"""

import argparse
import json
import os
import sys

import batch_db as db

# What the console can compute, per object. The field map may point each of these at
# any CRM property; this dict is the contract for what a local_key MEANS.
LOCAL_FIELDS = {
    "tech_signals":        ("companies", "Detected GTM tech stack (formatted line)"),
    "hiring_signals":      ("companies", "Open sales roles, '; '-joined"),
    "hiring_roles_count":  ("companies", "Total open roles, as a string int"),
    "hiring_job_titles":   ("companies", "All open role titles, <br>-joined HTML"),
    "account_signal":      ("companies", "The researched account signal, in prose"),
    "priority_score":      ("contacts", "0-100 signal strength at qualification"),
    "priority_band":       ("contacts", "hot | warm | cool"),
    "campaign_name":       ("contacts", "Campaign this contact is being worked in"),
    "recommended_channels": ("contacts", "Comma-joined channel recommendation"),
    "buyer_role":          ("contacts", "decision-maker | champion | influencer | user"),
    "suppressed":          ("contacts", "RevOps do-not-contact tag (read-only)"),
}

BOOL_TRUE = ("1", "true", "yes", "on")


def _client():
    import hubspot_client
    return hubspot_client.HubSpotClient()


def writeback_enabled():
    return (os.environ.get("CRM_SYNC_ENABLED") or "1").strip().lower() not in ("0", "false", "no")


# ---- property provisioning -------------------------------------------------
def ensure_properties(conn, dry_run=False):
    """Create any mapped property that does not exist in the CRM yet.

    hubspot_client only shipped `ensure_company_property`; contacts and deals need
    the same thing for a contact-level field map to work at all, so this drives the
    generalized helper. `auto_create=0` fields (anything RevOps owns, like
    everworker_tag) are checked but never created — if it's missing that is a real
    configuration problem, not something to paper over."""
    if not writeback_enabled():
        return {"ok": False, "reason": "CRM_SYNC_ENABLED=0"}
    try:
        client = _client()
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
    created, existing, skipped, errors = [], [], [], []
    for f in db.crm_fields(conn, enabled_only=True):
        key, obj, prop = f["local_key"], f["object_type"], f["property_name"]
        try:
            client.get_property(obj, prop)
            existing.append(key)
            continue
        except Exception:  # noqa: BLE001 — 404 is the expected case here
            pass
        if not f.get("auto_create"):
            skipped.append({"local_key": key, "property": prop,
                            "reason": "auto_create off and the property is missing"})
            continue
        if dry_run:
            created.append({"local_key": key, "property": prop, "dry_run": True})
            continue
        try:
            client.ensure_property(obj, prop, f.get("label") or prop,
                                   field_type=f.get("field_type") or "text")
            created.append({"local_key": key, "property": prop})
        except Exception as e:  # noqa: BLE001
            errors.append({"local_key": key, "error": f"{type(e).__name__}: {e}"})
            db.bump_crm_field(conn, key, error=f"{type(e).__name__}: {e}")
    return {"ok": True, "created": created, "existing": existing,
            "skipped": skipped, "errors": errors, "dry_run": dry_run}


# ---- push: console -> CRM --------------------------------------------------
def _company_values(conn, domain):
    row = db.get_signal(conn, domain) or {}
    detail = {}
    try:
        detail = json.loads(row.get("hiring_detail") or "{}")
    except (json.JSONDecodeError, TypeError):
        detail = {}
    sales = [t for t in (detail.get("sales_titles") or []) if t]
    titles = [t for t in (detail.get("active_titles") or []) if t]
    return {
        "tech_signals": row.get("tech_signals"),
        "account_signal": row.get("signal"),
        "hiring_signals": "; ".join(sales) if sales else None,
        "hiring_roles_count": (str(detail["active_count"])
                               if detail.get("active_count") is not None else None),
        "hiring_job_titles": "<br>".join(titles) if titles else None,
    }


def _contact_values(member):
    ch = member.get("channels") or {}
    if isinstance(ch, str):
        try:
            ch = json.loads(ch)
        except json.JSONDecodeError:
            ch = {}
    on = [k for k, v in (ch.get("channels") or ch).items() if v is True]
    score = member.get("priority_score")
    # ALL campaigns, not just the one being pushed. Membership lives on the person,
    # so the CRM has to show a contact being worked by three campaigns as three —
    # otherwise the overlap is invisible to anyone outside this console.
    names = [c["name"] for c in (member.get("all_campaigns") or []) if c.get("name")]
    if not names and member.get("campaign_name"):
        names = [member["campaign_name"]]
    return {
        "priority_score": None if score is None else str(round(float(score), 1)),
        "priority_band": member.get("score_band"),
        "campaign_name": "; ".join(dict.fromkeys(names)) if names else None,
        "recommended_channels": ", ".join(sorted(on)) if on else None,
        "buyer_role": member.get("buyer_role"),
    }


def push(conn, campaign_id=None, dry_run=False, limit=None):
    """Write computed values into the mapped CRM properties.

    Best-effort throughout, matching every other write path in this codebase: one
    unmatched company or one rejected property never fails the run. Blank values are
    SKIPPED rather than written, so a field we happen not to have computed cannot
    erase something a human typed."""
    if not writeback_enabled():
        return {"ok": False, "reason": "CRM_SYNC_ENABLED=0"}
    fields = [f for f in db.crm_fields(conn, enabled_only=True, direction="push")]
    if not fields:
        return {"ok": True, "note": "no fields wired for push", "companies": 0, "contacts": 0}
    try:
        client = _client()
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    co_fields = [f for f in fields if f["object_type"] == "companies"]
    ct_fields = [f for f in fields if f["object_type"] == "contacts"]
    members = db.campaign_members(conn, campaign_id, limit=limit) if (
        campaign_id or ct_fields) else []
    res = {"ok": True, "dry_run": dry_run, "companies": 0, "contacts": 0,
           "no_company": 0, "errors": [], "by_field": {}}

    # --- companies, keyed by domain
    if co_fields:
        domains = sorted({m["domain"] for m in members if m.get("domain")}) if campaign_id \
            else [r["domain"] for r in db.all_signals(conn)]
        for dom in domains[:limit] if limit else domains:
            vals = _company_values(conn, dom)
            props = {f["property_name"]: vals.get(f["local_key"])
                     for f in co_fields if vals.get(f["local_key"])}
            if not props:
                continue
            if dry_run:
                res["companies"] += 1
                continue
            try:
                cid = client.find_company_id_by_domain(dom)
                if not cid:
                    res["no_company"] += 1
                    continue
                client.update_company(cid, props)
                res["companies"] += 1
                for f in co_fields:
                    if vals.get(f["local_key"]):
                        res["by_field"][f["local_key"]] = res["by_field"].get(f["local_key"], 0) + 1
            except Exception as e:  # noqa: BLE001
                res["errors"].append(f"{dom}: {type(e).__name__}: {e}")

    # --- contacts, keyed by HubSpot record id (already what contact_id is)
    if ct_fields and members:
        updates = []
        for m in members:
            vals = _contact_values(m)
            props = {f["property_name"]: vals.get(f["local_key"])
                     for f in ct_fields if vals.get(f["local_key"])}
            if props:
                updates.append({"id": m["contact_id"], "properties": props})
                for f in ct_fields:
                    if vals.get(f["local_key"]):
                        res["by_field"][f["local_key"]] = res["by_field"].get(f["local_key"], 0) + 1
        if updates and not dry_run:
            try:
                res["contacts"] = client.batch_update("contacts", updates)
            except Exception as e:  # noqa: BLE001
                res["errors"].append(f"contacts batch: {type(e).__name__}: {e}")
        elif updates:
            res["contacts"] = len(updates)

    if not dry_run:
        for f in fields:
            n = res["by_field"].get(f["local_key"], 0)
            if n:
                db.bump_crm_field(conn, f["local_key"], pushed=n,
                                  error=res["errors"][0][:200] if res["errors"] else None)
    return res


# ---- pull: CRM -> console (the CRM wins) -----------------------------------
def pull(conn, limit=500, dry_run=False):
    """Read mapped CRM values back and let them override the local cache.

    This is the half that makes the CRM authoritative. Today it covers the company
    signal fields, which are the ones a human plausibly edits by hand: if
    `ai_sdr_account_signal` reads differently in HubSpot than in our cache, HubSpot
    is right and the cache is updated to match.

    Contact-side fields are push-only by default (a priority score is ours to
    compute), except `suppressed`, which is pull-only and already enforced by the
    unenrollment checker's live tag read."""
    if not writeback_enabled():
        return {"ok": False, "reason": "CRM_SYNC_ENABLED=0"}
    fields = [f for f in db.crm_fields(conn, enabled_only=True, direction="pull")
              if f["object_type"] == "companies"]
    if not fields:
        return {"ok": True, "note": "no company fields wired for pull", "updated": 0}
    try:
        client = _client()
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    by_prop = {f["property_name"]: f for f in fields}
    rows = db.all_signals(conn)[:int(limit)]
    res = {"ok": True, "dry_run": dry_run, "checked": 0, "updated": 0,
           "overridden": [], "errors": []}
    for row in rows:
        dom = row["domain"]
        try:
            cid = client.find_company_id_by_domain(dom)
            if not cid:
                continue
            got = client.batch_read_objects("companies", [cid], list(by_prop)) or []
            props = (got[0].get("properties") if got else {}) or {}
        except Exception as e:  # noqa: BLE001
            res["errors"].append(f"{dom}: {type(e).__name__}: {e}")
            continue
        res["checked"] += 1
        for prop, f in by_prop.items():
            crm_val = (props.get(prop) or "").strip()
            if not crm_val:
                continue
            local_val = (row.get(_local_column(f["local_key"])) or "").strip()
            if crm_val == local_val:
                continue
            res["overridden"].append({"domain": dom, "local_key": f["local_key"],
                                      "crm": crm_val[:120], "local": local_val[:120]})
            if not dry_run:
                _write_local(conn, dom, f["local_key"], crm_val)
                res["updated"] += 1
                db.bump_crm_field(conn, f["local_key"], pulled=1)
    return res


# Which account_signals column backs each pullable local_key.
_LOCAL_COLUMNS = {
    "tech_signals": "tech_signals",
    "hiring_signals": "hiring_signals",
    "account_signal": "signal",
}


def _local_column(local_key):
    return _LOCAL_COLUMNS.get(local_key, local_key)


def _write_local(conn, domain, local_key, value):
    col = _local_column(local_key)
    if col not in ("tech_signals", "hiring_signals", "signal"):
        return
    conn.execute(f"UPDATE account_signals SET {col}=?, updated_at=? WHERE domain=?",
                 (value, db.now(), domain))
    # A human-authored signal is still a signal: log it as an observation so a
    # campaign window can qualify on it, exactly as a scanned one would.
    kind = {"signal": "research", "tech_signals": "tech",
            "hiring_signals": "hiring"}[col]
    db._insert_signal_event(conn, domain, kind, value)
    conn.commit()


# ---- CLI -------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("fields").add_argument("--json", action="store_true")
    p = sub.add_parser("push")
    p.add_argument("--campaign", type=int)
    p.add_argument("--limit", type=int)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("pull")
    p.add_argument("--limit", type=int, default=500)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--json", action="store_true")
    sub.add_parser("ensure").add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    conn = db.connect()
    db.init_schema(conn)
    if args.cmd == "fields":
        rows = db.crm_fields(conn)
        if getattr(args, "json", False):
            print(json.dumps({"fields": rows}, ensure_ascii=False))
        else:
            for f in rows:
                state = "on" if f["enabled"] else "off"
                print(f"  {f['local_key']:<22} {f['object_type']:<10} "
                      f"{f['property_name']:<30} {f['direction']:<5} {state}")
        return 0
    if args.cmd == "ensure":
        print(json.dumps(ensure_properties(conn, dry_run=args.dry_run), ensure_ascii=False))
        return 0
    if args.cmd == "push":
        print(json.dumps(push(conn, campaign_id=args.campaign, dry_run=args.dry_run,
                              limit=args.limit), ensure_ascii=False))
        return 0
    print(json.dumps(pull(conn, limit=args.limit, dry_run=args.dry_run), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
