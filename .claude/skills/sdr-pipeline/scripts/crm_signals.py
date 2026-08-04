#!/usr/bin/env python3
"""Signals DERIVED from the CRM — defined as data, evaluated on a schedule.

The signal kinds this pipeline shipped with are the ones it can go and detect:
account news, hiring, tech stack. But the strongest buying trigger a team has is
usually already sitting in their CRM and is specific to them — "they've opened
fourteen emails and never replied", "we lost a deal to them in Q1 and they've gone
quiet", "they're a marketing-qualified lead nobody has called". Those cannot be
shipped as constants because they are different at every customer.

So a signal kind can carry a RULE, and this module is what runs it: evaluate the
rule against CRM and pipeline data, and write a `signal_event` for every account
that matches. From that point the new kind is indistinguishable from a builtin —
campaigns qualify on it, the scorer weighs it by its configured strength, the
Signals feed lists it, the money scale counts it.

Three rule sources, each with a CLOSED field list
-------------------------------------------------
    local_field       something already on our `contacts` row. Free, instant, and
                      the only source that works with no CRM connection at all.
    contact_property  a HubSpot contact property, read in batches. This is where
                      the activity counters live.
    deal              association to a deal, optionally filtered by state and a
                      time window.

The field lists are declared here (see SOURCES) and the rule is validated against
them BEFORE anything runs — the same discipline reports.py uses for datasets. A
property name is never interpolated from user input into a query it wasn't declared
for, and an unknown field is rejected rather than sent to HubSpot to see what
happens.

Cost and blast radius
---------------------
`contact_property` and `deal` rules make API calls, so evaluation is bounded by
`limit`, batched 100 at a time, and reports what it skipped. `preview()` runs the
same evaluation with `commit=False`: it is the honest way to answer "how many
accounts would this actually catch", which is the only question that makes a rule
worth saving. Nothing is written until it is answered.

Stdlib only; the HubSpot client is imported lazily so the module is importable with
no token (the boot rule).
"""

import argparse
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
import batch_db as db  # noqa: E402

DEFAULT_LIMIT = 500
BATCH = 100

# Operators, and how each compares. Kept tiny on purpose: every one has to be
# meaningful for both a number and a string, or explicitly numeric.
OPERATORS = {
    "gte": {"label": "is at least", "numeric": True},
    "lte": {"label": "is at most", "numeric": True},
    "eq": {"label": "is", "numeric": False},
    "neq": {"label": "is not", "numeric": False},
    "contains": {"label": "contains", "numeric": False},
    "exists": {"label": "has any value", "numeric": False, "valueless": True},
    "not_exists": {"label": "is empty", "numeric": False, "valueless": True},
}

# What a rule may read. Each field is declared with its type and a human label, so
# the UI can render the right control and validation can reject anything else.
#
# The `contact_property` list is deliberately the ACTIVITY and LIFECYCLE surface of
# a HubSpot contact — the fields that answer "how much has this person already done
# with us", which is what "total prior activity" means in practice. Adding a field
# here makes it selectable everywhere with no other change.
SOURCES = {
    "local_field": {
        "label": "Something we already hold",
        "note": "Read straight from the pipeline. No API call, no credit, instant.",
        "needs_crm": False,
        "fields": [
            {"key": "lifecycle_stage", "label": "Lifecycle stage", "type": "string"},
            {"key": "source", "label": "Original source", "type": "string"},
            {"key": "latest_source", "label": "Latest source", "type": "string"},
            {"key": "motion", "label": "Motion (inbound/outbound)", "type": "string"},
            {"key": "persona", "label": "Persona", "type": "string"},
            {"key": "title", "label": "Job title", "type": "string"},
            {"key": "status", "label": "Pipeline status", "type": "string"},
        ],
    },
    "contact_property": {
        "label": "A CRM contact property",
        "note": "Read from HubSpot in batches of 100. Costs API calls, not credits.",
        "needs_crm": True,
        "fields": [
            {"key": "num_notes", "label": "Notes logged", "type": "number"},
            {"key": "num_contacted_notes", "label": "Times contacted", "type": "number"},
            {"key": "hs_analytics_num_page_views", "label": "Page views", "type": "number"},
            {"key": "hs_analytics_num_visits", "label": "Site visits", "type": "number"},
            {"key": "hs_email_open", "label": "Emails opened", "type": "number"},
            {"key": "hs_email_click", "label": "Emails clicked", "type": "number"},
            {"key": "hs_analytics_num_event_completions", "label": "Form submissions",
             "type": "number"},
            {"key": "hs_sales_email_last_replied", "label": "Last replied to an email",
             "type": "date"},
            {"key": "notes_last_contacted", "label": "Last contacted", "type": "date"},
            {"key": "hs_last_sales_activity_timestamp", "label": "Last sales activity",
             "type": "date"},
            {"key": "lifecyclestage", "label": "Lifecycle stage", "type": "string"},
            {"key": "hs_lead_status", "label": "Lead status", "type": "string"},
            {"key": "hubspot_owner_id", "label": "Owner", "type": "string"},
            {"key": "hs_analytics_source", "label": "Original source", "type": "string"},
        ],
    },
    "deal": {
        "label": "A deal they were on",
        "note": "Walks contact→deal associations. One batched read per 100 contacts.",
        "needs_crm": True,
        # `field` here selects WHICH deals count, not a property to compare.
        "fields": [
            {"key": "any", "label": "Any deal", "type": "deal_state"},
            {"key": "won", "label": "A deal we won", "type": "deal_state"},
            {"key": "lost", "label": "A deal we lost", "type": "deal_state"},
            {"key": "open", "label": "An open deal", "type": "deal_state"},
        ],
    },
}

# Starting points, because "define a signal from a CRM field" is too abstract to
# begin from. These are the ones people actually mean, with the fields prefilled;
# CRM_PRESETS does the same job for audiences and it is why that screen is usable.
TEMPLATES = [
    {
        "id": "total_prior_activity",
        "label": "Lots of prior activity",
        "hint": "They have engaged with us repeatedly — a warm account nobody is working.",
        "kind": "prior_activity", "signal_label": "Prior activity",
        "strength": 34, "decay_scale": 0.5,
        "rule": {"source": "contact_property", "field": "num_contacted_notes",
                 "op": "gte", "value": 5},
    },
    {
        "id": "prior_deal",
        "label": "Was on a deal before",
        "hint": "Any past opportunity. They know us, and someone already qualified them.",
        "kind": "prior_deal", "signal_label": "Prior deal",
        "strength": 38, "decay_scale": 0.4,
        "rule": {"source": "deal", "field": "any", "op": "exists", "window_days": 365},
    },
    {
        "id": "closed_lost_deal",
        "label": "We lost a deal to them",
        "hint": "The highest-intent cold audience there is — they evaluated us and "
                "something changed.",
        "kind": "closed_lost", "signal_label": "Closed-lost",
        "strength": 42, "decay_scale": 0.5,
        "rule": {"source": "deal", "field": "lost", "op": "exists", "window_days": 365},
    },
    {
        "id": "email_engaged",
        "label": "Opens but never replies",
        "hint": "Reading everything and saying nothing. A reason to change channel, "
                "not to send again.",
        "kind": "email_engaged", "signal_label": "Email engagement",
        "strength": 30, "decay_scale": 2.0,
        "rule": {"source": "contact_property", "field": "hs_email_open",
                 "op": "gte", "value": 8},
    },
    {
        "id": "page_views",
        "label": "Browsing the site",
        "hint": "Behavioural intent. Ages fast — a page view three weeks ago is not intent.",
        "kind": "page_views", "signal_label": "Site activity",
        "strength": 40, "decay_scale": 3.0,
        "rule": {"source": "contact_property", "field": "hs_analytics_num_page_views",
                 "op": "gte", "value": 5},
    },
    {
        "id": "mql_uncalled",
        "label": "At a lifecycle stage",
        "hint": "Marketing says they're qualified. Worth checking nobody has called.",
        "kind": "lifecycle_flag", "signal_label": "Lifecycle stage",
        "strength": 32, "decay_scale": 1.0,
        "rule": {"source": "local_field", "field": "lifecycle_stage",
                 "op": "eq", "value": "marketingqualifiedlead"},
    },
]

_LOST_HINTS = ("closed lost", "closedlost", "lost")
_WON_HINTS = ("closed won", "closedwon", "won")
_KIND_RE = re.compile(r"^[a-z][a-z0-9_]{1,38}$")


class RuleError(ValueError):
    """A rule the console will not run. Surfaced as a 400, never a 500."""


# ---- validation ------------------------------------------------------------
def validate_rule(rule):
    """Normalize, or raise RuleError.

    Rejects rather than repairs, and rejects on the WHOLE rule: a filter with a
    silently-dropped clause would match far more accounts than the person who wrote
    it intended, and this rule decides who gets contacted."""
    if not isinstance(rule, dict):
        raise RuleError("a rule must be an object")
    src = rule.get("source")
    spec = SOURCES.get(src)
    if not spec:
        raise RuleError(f"source must be one of {sorted(SOURCES)}")
    field = str(rule.get("field") or "").strip()
    known = {f["key"]: f for f in spec["fields"]}
    if field not in known:
        raise RuleError(f"{src} field must be one of {sorted(known)}")
    op = str(rule.get("op") or "").strip()
    if op not in OPERATORS:
        raise RuleError(f"op must be one of {sorted(OPERATORS)}")
    ftype = known[field]["type"]
    out = {"source": src, "field": field, "op": op}

    if not OPERATORS[op].get("valueless"):
        val = rule.get("value")
        if val is None or val == "":
            raise RuleError(f"'{OPERATORS[op]['label']}' needs a value")
        if OPERATORS[op]["numeric"] or ftype == "number":
            try:
                out["value"] = float(val)
            except (TypeError, ValueError):
                raise RuleError(f"{field} is numeric — '{val}' is not a number")
        else:
            out["value"] = str(val).strip()[:200]
    if src == "deal":
        # A deal rule is inherently existence-shaped: which deals count is the
        # `field`, so anything but exists/not_exists would be meaningless.
        if op not in ("exists", "not_exists"):
            raise RuleError("a deal rule can only test 'has any value' or 'is empty'")
    days = rule.get("window_days")
    if days not in (None, ""):
        try:
            days = int(days)
        except (TypeError, ValueError):
            raise RuleError("window_days must be a whole number of days")
        if not 1 <= days <= 3650:
            raise RuleError("window_days must be between 1 and 3650")
        out["window_days"] = days
    return out


def validate_kind(kind):
    kind = str(kind or "").strip().lower().replace("-", "_")
    if not _KIND_RE.match(kind):
        raise RuleError("id must be lowercase letters, numbers and underscores "
                        "(2-39 characters)")
    return kind


def describe_rule(rule):
    """One human sentence, for the list view and the signal_event summary."""
    try:
        r = validate_rule(rule)
    except RuleError:
        return "invalid rule"
    spec = SOURCES[r["source"]]
    label = next(f["label"] for f in spec["fields"] if f["key"] == r["field"])
    op = OPERATORS[r["op"]]["label"]
    if r["source"] == "deal":
        base = f"{label}" if r["op"] == "exists" else f"No {label.lower()}"
    elif OPERATORS[r["op"]].get("valueless"):
        base = f"{label} {op}"
    else:
        val = r["value"]
        val = int(val) if isinstance(val, float) and val.is_integer() else val
        base = f"{label} {op} {val}"
    if r.get("window_days"):
        base += f", in the last {r['window_days']} days"
    return base


# ---- evaluation ------------------------------------------------------------
def _client():
    import hubspot_client
    return hubspot_client.HubSpotClient()


def _compare(raw, op, want, numeric):
    present = raw not in (None, "", [])
    if op == "exists":
        return present
    if op == "not_exists":
        return not present
    if not present:
        return False
    if numeric:
        try:
            return (float(raw) >= want) if op == "gte" else (float(raw) <= want)
        except (TypeError, ValueError):
            return False
    a, b = str(raw).strip().lower(), str(want).strip().lower()
    return {"eq": a == b, "neq": a != b, "contains": b in a}.get(op, False)


def _local_matches(conn, rule, limit):
    """Rows matching a local_field rule. No API call — this is why local rules are
    the ones a demo (or an unconnected deployment) can still run."""
    col = rule["field"]
    rows = [dict(r) for r in conn.execute(
        f"SELECT contact_id, domain, company, first_name, last_name, {col} AS val "
        "FROM contacts WHERE domain IS NOT NULL AND domain != '' LIMIT ?",
        (int(limit),))]
    numeric = OPERATORS[rule["op"]]["numeric"]
    want = rule.get("value")
    return [r for r in rows if _compare(r["val"], rule["op"], want, numeric)], len(rows)


def _property_matches(conn, rule, limit, crm=None):
    """Rows matching a contact_property rule, read from HubSpot in batches."""
    rows = [dict(r) for r in conn.execute(
        "SELECT contact_id, domain, company, first_name, last_name FROM contacts "
        "WHERE domain IS NOT NULL AND domain != '' LIMIT ?", (int(limit),))]
    if not rows:
        return [], 0
    prop = rule["field"]
    numeric = OPERATORS[rule["op"]]["numeric"]
    want = rule.get("value")
    values = (crm.contact_properties([r["contact_id"] for r in rows], [prop])
              if crm else _fetch_properties([r["contact_id"] for r in rows], [prop]))
    hits = []
    for r in rows:
        raw = (values.get(str(r["contact_id"])) or {}).get(prop)
        if _compare(raw, rule["op"], want, numeric):
            hits.append({**r, "val": raw})
    return hits, len(rows)


def _fetch_properties(ids, props):
    client = _client()
    out = {}
    for i in range(0, len(ids), BATCH):
        chunk = [str(x) for x in ids[i:i + BATCH]]
        for rec in client.batch_read_contacts(chunk, props) or []:
            out[str(rec.get("id"))] = rec.get("properties") or {}
    return out


def _deal_matches(conn, rule, limit, crm=None):
    """Rows matching a deal rule, via contact→deal associations."""
    rows = [dict(r) for r in conn.execute(
        "SELECT contact_id, domain, company, first_name, last_name FROM contacts "
        "WHERE domain IS NOT NULL AND domain != '' LIMIT ?", (int(limit),))]
    if not rows:
        return [], 0
    ids = [str(r["contact_id"]) for r in rows]
    if crm:
        by_contact = crm.contact_deals(ids, rule["field"], rule.get("window_days"))
    else:
        by_contact = _fetch_deals(ids, rule["field"], rule.get("window_days"))
    hits = []
    for r in rows:
        deals = by_contact.get(str(r["contact_id"])) or []
        got = bool(deals)
        if (got and rule["op"] == "exists") or (not got and rule["op"] == "not_exists"):
            hits.append({**r, "val": f"{len(deals)} deal(s)" if got else "none"})
    return hits, len(rows)


def _fetch_deals(ids, state, window_days):
    """{contact_id: [deal_id]} for deals matching the state + window.

    Stage ids are pipeline-specific, so won/lost are resolved from the dealstage
    property's option LABELS — the same portable trick audiences.py uses."""
    client = _client()
    assoc = {}
    for i in range(0, len(ids), BATCH):
        assoc.update(client.batch_read_associations("contacts", "deals",
                                                    ids[i:i + BATCH]) or {})
    wanted_ids = sorted({d for lst in assoc.values() for d in lst})
    if not wanted_ids:
        return {}
    keep = set(wanted_ids)
    if state != "any" or window_days:
        stages = set()
        if state in ("won", "lost"):
            hints = _WON_HINTS if state == "won" else _LOST_HINTS
            options = (client.get_property("deals", "dealstage") or {}).get("options") or []
            stages = {str(o.get("value")) for o in options
                      if any(h in str(o.get("label") or "").lower() for h in hints)}
            if not stages:
                return {}
        cutoff = None
        if window_days:
            cutoff = (datetime.now(timezone.utc)
                      - timedelta(days=int(window_days))).timestamp() * 1000
        keep = set()
        for i in range(0, len(wanted_ids), BATCH):
            chunk = wanted_ids[i:i + BATCH]
            for rec in client.batch_read_objects(
                    "deals", chunk, ["dealstage", "closedate", "createdate"]) or []:
                p = rec.get("properties") or {}
                stage = str(p.get("dealstage") or "")
                if state in ("won", "lost") and stage not in stages:
                    continue
                if state == "open" and stage in stages:
                    continue
                if cutoff:
                    ts = p.get("closedate") or p.get("createdate")
                    try:
                        when = datetime.strptime(str(ts)[:10], "%Y-%m-%d").replace(
                            tzinfo=timezone.utc).timestamp() * 1000
                    except (ValueError, TypeError):
                        when = None
                    if when is not None and when < cutoff:
                        continue
                keep.add(str(rec.get("id")))
    return {cid: [d for d in lst if str(d) in keep]
            for cid, lst in assoc.items() if any(str(d) in keep for d in lst)}


def evaluate(conn, definition, limit=DEFAULT_LIMIT, commit=False, crm=None):
    """Run one rule. Returns what it matched; writes events only when `commit`.

    Events are recorded at the DOMAIN level because that is what a campaign
    qualifies against — a contact-level match makes their whole account visible,
    with the matching person named in the summary so the reason is legible.
    """
    kind = definition["kind"]
    rule = definition.get("rule")
    if not rule:
        raise RuleError(f"{kind} has no rule to evaluate")
    rule = validate_rule(rule)
    res = {"kind": kind, "rule": describe_rule(rule), "scanned": 0, "matched": 0,
           "accounts": 0, "recorded": 0, "committed": bool(commit),
           "sample": [], "error": None}
    try:
        if rule["source"] == "local_field":
            hits, scanned = _local_matches(conn, rule, limit)
        elif rule["source"] == "contact_property":
            hits, scanned = _property_matches(conn, rule, limit, crm)
        else:
            hits, scanned = _deal_matches(conn, rule, limit, crm)
    except Exception as e:  # noqa: BLE001 — a failed rule is a result, not a crash
        res["error"] = f"{type(e).__name__}: {e}"[:300]
        return res

    res["scanned"] = scanned
    res["matched"] = len(hits)
    by_domain = {}
    for h in hits:
        by_domain.setdefault(h["domain"], []).append(h)
    res["accounts"] = len(by_domain)
    res["sample"] = [{
        "domain": d, "company": rows[0].get("company"),
        "contacts": len(rows),
        "who": f"{rows[0].get('first_name') or ''} {rows[0].get('last_name') or ''}".strip(),
        "value": rows[0].get("val"),
    } for d, rows in list(by_domain.items())[:10]]

    if commit:
        label = definition.get("label") or kind
        for dom, rows in by_domain.items():
            who = f"{rows[0].get('first_name') or ''} {rows[0].get('last_name') or ''}".strip()
            val = rows[0].get("val")
            summary = f"{label}: {res['rule']}"
            if who:
                summary += f" — {who}"
                if len(rows) > 1:
                    summary += f" +{len(rows) - 1} more"
            if val not in (None, ""):
                summary += f" ({val})"
            # record_signal_event dedups on the VALUE fingerprint, so re-running an
            # unchanged rule does not manufacture a fresh observation every sweep —
            # which would make every matching account look permanently hot.
            if db.record_signal_event(conn, dom, kind, summary,
                                      detail=json.dumps({"rule": rule,
                                                         "contacts": len(rows)})):
                res["recorded"] += 1
        db.upsert_signal_def(conn, kind, last_run_at=db.now(),
                             last_run_detail=json.dumps({
                                 "accounts": res["accounts"], "matched": res["matched"],
                                 "recorded": res["recorded"], "scanned": scanned}))
    return res


def preview(conn, kind, rule, limit=200, label=None, crm=None):
    """What a rule WOULD catch, having written nothing.

    The count is the whole reason this step exists: a rule is abstract until you can
    see that it matches 3 accounts, or 900."""
    return evaluate(conn, {"kind": kind, "label": label or kind,
                           "rule": validate_rule(rule)},
                    limit=limit, commit=False, crm=crm)


def run_all(conn, limit=DEFAULT_LIMIT, crm=None, include_crm=True):
    """Evaluate every ACTIVE rule-backed definition. Used by the hourly sweep.

    `include_crm=False` runs only the free local rules — what the sweep does when
    no CRM is configured, so a deployment without HubSpot still gets the signals it
    can compute for itself."""
    out = []
    for d in db.signal_defs(conn, active_only=True, with_rules=True):
        rule = d.get("rule") or {}
        if not include_crm and SOURCES.get(rule.get("source"), {}).get("needs_crm"):
            continue
        try:
            out.append(evaluate(conn, d, limit=limit, commit=True, crm=crm))
        except RuleError as e:
            out.append({"kind": d["kind"], "error": str(e)})
    return {"evaluated": len(out), "results": out}


def vocabulary():
    """Everything the configurator may choose from — sources, fields, operators and
    the starting templates. Generated from the declarations above, so adding a field
    to SOURCES makes it selectable with no UI change."""
    return {
        "sources": [{"id": k, "label": v["label"], "note": v["note"],
                     "needs_crm": v["needs_crm"], "fields": v["fields"]}
                    for k, v in SOURCES.items()],
        "operators": [{"id": k, **v} for k, v in OPERATORS.items()],
        "templates": TEMPLATES,
    }


# ---- CLI -------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list").add_argument("--json", action="store_true")
    sub.add_parser("vocab")
    p = sub.add_parser("preview")
    p.add_argument("kind")
    p.add_argument("rule", help='JSON, e.g. \'{"source":"local_field",'
                                '"field":"lifecycle_stage","op":"eq","value":"lead"}\'')
    p.add_argument("--limit", type=int, default=200)
    r = sub.add_parser("run")
    r.add_argument("--kind", default=None, help="one kind, or all active rules")
    r.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    r.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    conn = db.connect()
    db.init_schema(conn)
    try:
        if args.cmd == "vocab":
            print(json.dumps(vocabulary(), indent=2))
            return 0
        if args.cmd == "list":
            rows = db.signal_defs(conn)
            for d in rows:
                mark = "•" if d.get("rule") else " "
                state = "" if d.get("active") else "  (inactive)"
                print(f"{mark} {d['kind']:<20} {d['strength']:>5}  {d['label']}{state}")
                if d.get("rule"):
                    print(f"    {describe_rule(d['rule'])}")
            return 0
        if args.cmd == "preview":
            res = preview(conn, args.kind, json.loads(args.rule), limit=args.limit)
            print(json.dumps(res, indent=2))
            return 0
        if args.cmd == "run":
            if args.kind:
                d = db.get_signal_def(conn, args.kind)
                if not d:
                    print(f"no signal definition {args.kind!r}", file=sys.stderr)
                    return 1
                res = evaluate(conn, d, limit=args.limit, commit=not args.dry_run)
            else:
                res = run_all(conn, limit=args.limit)
            print(json.dumps(res, indent=2))
            return 0
    except RuleError as e:
        print(f"invalid rule: {e}", file=sys.stderr)
        return 1
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
