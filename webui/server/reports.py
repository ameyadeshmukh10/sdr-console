"""Ad-hoc reporting — raw data with configurable columns, and report-by-description.

Two things Analytics and Trends could not do: show you the underlying rows, and
answer a question nobody built a panel for. Both are the same feature underneath —
a constrained query over a small registry of datasets.

SAFETY MODEL, which is the whole design:

    The model NEVER writes SQL. It emits a SPEC — a dataset id, column ids, filters
    drawn from an operator allowlist, a group-by and a sort — and every part of that
    spec is validated against this registry before anything runs. Unknown dataset,
    unknown column, unknown operator: rejected, not sanitised. The SQL is then built
    here from validated identifiers with values bound as parameters.

That means the worst a bad or adversarial description can do is fail validation. It
cannot reach a table the registry doesn't list, a column it doesn't declare, or an
operator it doesn't permit — and it cannot mutate anything, because every query is a
SELECT built by this file.

Datasets are read-only views over what already exists; adding one is a dict entry
plus its column list, and it immediately becomes describable, filterable and
column-configurable with no other change.
"""

import json
import os
import re
from pathlib import Path

import batch_db as db

# A column: id -> (SQL expression, label, type). The expression is OURS — it never
# comes from input — so it is safe to interpolate; values never are.
_MEMBER_COLS = {
    "name": ("TRIM(COALESCE(c.first_name,'')||' '||COALESCE(c.last_name,''))", "Name", "text"),
    "email": ("c.email", "Email", "text"),
    "title": ("c.title", "Title", "text"),
    "company": ("COALESCE(c.company, m.domain)", "Company", "text"),
    "domain": ("m.domain", "Domain", "text"),
    "persona": ("c.persona", "Persona", "text"),
    "buyer_role": ("m.buyer_role", "Buyer role", "text"),
    "motion": ("c.motion", "Motion", "text"),
    "campaign": ("cam.name", "Campaign", "text"),
    "campaign_status": ("cam.status", "Campaign status", "text"),
    "state": ("m.state", "State", "text"),
    "score": ("m.priority_score", "Score", "number"),
    "band": ("m.score_band", "Priority band", "text"),
    "momentum": ("m.momentum", "Momentum", "number"),
    "rank_score": ("COALESCE(m.rank_score, m.priority_score)", "Rank score", "number"),
    "signal_kind": ("m.signal_kind", "Signal type", "text"),
    "origin": ("m.origin", "Source", "text"),
    "qualified_at": ("m.qualified_at", "Qualified", "date"),
    "enrolled_at": ("m.enrolled_at", "Enrolled", "date"),
}
_MEMBER_FROM = ("campaign_members m "
                "LEFT JOIN contacts c ON c.contact_id = m.contact_id "
                "LEFT JOIN campaigns cam ON cam.campaign_id = m.campaign_id")

_SIGNAL_COLS = {
    "domain": ("s.domain", "Domain", "text"),
    "kind": ("s.kind", "Signal type", "text"),
    "summary": ("s.summary", "What fired", "text"),
    "has_recent": ("s.has_recent", "Dated event", "number"),
    "observed_at": ("s.observed_at", "Observed", "date"),
}

_CAMPAIGN_COLS = {
    "name": ("cam.name", "Campaign", "text"),
    "status": ("cam.status", "Status", "text"),
    "membership_mode": ("cam.membership_mode", "Membership", "text"),
    "variant": ("cam.variant", "Variant", "text"),
    "window_start": ("cam.window_start", "Window start", "date"),
    "window_end": ("cam.window_end", "Window end", "date"),
    "bison_campaign_id": ("cam.bison_campaign_id", "Bison campaign", "text"),
    "members": ("(SELECT COUNT(*) FROM campaign_members x WHERE x.campaign_id=cam.campaign_id)",
                "Contacts", "number"),
    "accounts": ("(SELECT COUNT(DISTINCT x.domain) FROM campaign_members x "
                 "WHERE x.campaign_id=cam.campaign_id)", "Accounts", "number"),
    "enrolled": ("(SELECT COUNT(*) FROM campaign_members x WHERE x.campaign_id=cam.campaign_id "
                 "AND x.state IN ('enrolled','replied'))", "Enrolled", "number"),
    "replied": ("(SELECT COUNT(*) FROM campaign_members x WHERE x.campaign_id=cam.campaign_id "
                "AND x.state='replied')", "Replied", "number"),
    "avg_score": ("(SELECT ROUND(AVG(x.priority_score),1) FROM campaign_members x "
                  "WHERE x.campaign_id=cam.campaign_id)", "Average score", "number"),
}

_SPEND_COLS = {
    "provider": ("u.provider", "Provider", "text"),
    "operation": ("u.operation", "Operation", "text"),
    "units": ("u.units", "Units", "number"),
    "unit_kind": ("u.unit_kind", "Unit", "text"),
    "campaign": ("cam.name", "Campaign", "text"),
    "ref": ("u.ref", "Reference", "text"),
    "occurred_at": ("u.occurred_at", "When", "date"),
}

DATASETS = {
    "contacts": {
        "label": "Contacts in campaigns",
        "describe": "One row per contact per campaign — score, band, momentum, "
                    "channel, state, and why they qualified.",
        "from": _MEMBER_FROM, "cols": _MEMBER_COLS,
        "default": ["name", "company", "title", "campaign", "score", "band", "state"],
    },
    "signals": {
        "label": "Signal observations",
        "describe": "The append-only signal log — every observation, its kind and "
                    "when it fired.",
        "from": "signal_events s", "cols": _SIGNAL_COLS,
        "default": ["observed_at", "kind", "domain", "summary"],
    },
    "campaigns": {
        "label": "Campaigns",
        "describe": "One row per campaign with its definition and rolled-up counts.",
        "from": "campaigns cam", "cols": _CAMPAIGN_COLS,
        "default": ["name", "status", "accounts", "members", "enrolled", "replied"],
    },
    "spend": {
        "label": "Credit + send ledger",
        "describe": "Every metered event — enrichment credits and sends, by provider.",
        "from": "usage_ledger u LEFT JOIN campaigns cam ON cam.campaign_id = u.campaign_id",
        "cols": _SPEND_COLS,
        "default": ["occurred_at", "provider", "operation", "units", "unit_kind"],
    },
}

# Operators the spec may use. Mapped to SQL here so an input string is never a
# fragment — it only ever selects one of these.
OPERATORS = {
    "eq": "= ?", "ne": "!= ?", "gt": "> ?", "gte": ">= ?", "lt": "< ?", "lte": "<= ?",
    "contains": "LIKE ?", "starts": "LIKE ?", "is_null": "IS NULL",
    "not_null": "IS NOT NULL", "in": None,   # expanded below
}
AGGREGATES = {"count": "COUNT(*)", "sum": "SUM(%s)", "avg": "ROUND(AVG(%s),2)",
              "min": "MIN(%s)", "max": "MAX(%s)"}
MAX_ROWS = 2000


class SpecError(ValueError):
    """A spec that failed validation. Surfaced to the user, never auto-corrected."""


def schema_payload():
    """Everything a caller (or the model) may reference."""
    return {
        "datasets": [{
            "id": k, "label": v["label"], "describe": v["describe"],
            "default_columns": v["default"],
            "columns": [{"id": c, "label": m[1], "type": m[2]}
                        for c, m in v["cols"].items()],
        } for k, v in DATASETS.items()],
        "operators": sorted(OPERATORS),
        "aggregates": sorted(AGGREGATES),
        "max_rows": MAX_ROWS,
    }


def validate(spec):
    """Normalize a spec or raise SpecError. Rejects, never sanitises: a filter on a
    column that doesn't exist is a wrong report, and silently dropping it would give
    a confidently wrong answer."""
    spec = dict(spec or {})
    ds_id = spec.get("dataset")
    ds = DATASETS.get(ds_id)
    if not ds:
        raise SpecError(f"unknown dataset {ds_id!r} — pick one of {sorted(DATASETS)}")
    cols = [c for c in (spec.get("columns") or ds["default"])]
    bad = [c for c in cols if c not in ds["cols"]]
    if bad:
        raise SpecError(f"unknown column(s) for {ds_id}: {', '.join(bad)}")
    if not cols:
        raise SpecError("a report needs at least one column")

    filters = []
    for f in (spec.get("filters") or []):
        col, op = f.get("column"), f.get("op")
        if col not in ds["cols"]:
            raise SpecError(f"unknown filter column {col!r}")
        if op not in OPERATORS:
            raise SpecError(f"unknown operator {op!r}")
        if op in ("is_null", "not_null"):
            filters.append({"column": col, "op": op})
            continue
        if "value" not in f and op != "in":
            raise SpecError(f"filter on {col} needs a value")
        if op == "in":
            vals = f.get("values") or []
            if not isinstance(vals, list) or not vals:
                raise SpecError(f"'in' filter on {col} needs a non-empty values list")
            filters.append({"column": col, "op": op, "values": [str(v) for v in vals]})
        else:
            filters.append({"column": col, "op": op, "value": f["value"]})

    group_by = spec.get("group_by")
    if group_by and group_by not in ds["cols"]:
        raise SpecError(f"unknown group_by column {group_by!r}")
    aggs = []
    for a in (spec.get("aggregates") or []):
        fn = a.get("fn")
        if fn not in AGGREGATES:
            raise SpecError(f"unknown aggregate {fn!r}")
        acol = a.get("column")
        if fn != "count" and acol not in ds["cols"]:
            raise SpecError(f"aggregate {fn} needs a valid column")
        aggs.append({"fn": fn, "column": acol})
    if group_by and not aggs:
        aggs = [{"fn": "count", "column": None}]

    sort = spec.get("sort")
    if sort and sort.get("column") not in ds["cols"] and not group_by:
        raise SpecError(f"unknown sort column {sort.get('column')!r}")

    try:
        limit = int(spec.get("limit") or 200)
    except (TypeError, ValueError):
        limit = 200
    return {
        "dataset": ds_id, "columns": cols, "filters": filters,
        "group_by": group_by, "aggregates": aggs,
        "sort": sort, "limit": max(1, min(limit, MAX_ROWS)),
        "title": str(spec.get("title") or "").strip() or None,
    }


def build_sql(spec):
    """(sql, params) from a VALIDATED spec. Identifiers come from the registry;
    every value is bound."""
    ds = DATASETS[spec["dataset"]]
    cols = ds["cols"]
    where, params = [], []
    for f in spec["filters"]:
        expr = cols[f["column"]][0]
        op = f["op"]
        if op in ("is_null", "not_null"):
            where.append(f"{expr} {OPERATORS[op]}")
        elif op == "in":
            where.append(f"{expr} IN ({','.join('?' * len(f['values']))})")
            params += f["values"]
        elif op in ("contains", "starts"):
            where.append(f"{expr} LIKE ?")
            params.append(f"%{f['value']}%" if op == "contains" else f"{f['value']}%")
        else:
            where.append(f"{expr} {OPERATORS[op]}")
            params.append(f["value"])
    w = (" WHERE " + " AND ".join(where)) if where else ""

    if spec["group_by"]:
        g = cols[spec["group_by"]][0]
        sel = [f'{g} AS "{spec["group_by"]}"']
        for a in spec["aggregates"]:
            if a["fn"] == "count":
                sel.append('COUNT(*) AS "count"')
            else:
                sel.append(f'{AGGREGATES[a["fn"]] % cols[a["column"]][0]} '
                           f'AS "{a["fn"]}_{a["column"]}"')
        order = spec.get("sort") or {}
        oc = order.get("column")
        # A grouped report sorts by an OUTPUT name (the aggregate), which is not a
        # dataset column — so it is matched against the produced aliases instead.
        aliases = [s.rsplit(' AS ', 1)[1].strip('"') for s in sel]
        ob = oc if oc in aliases else aliases[-1]
        direction = "ASC" if (order.get("dir") or "desc").lower() == "asc" else "DESC"
        sql = (f'SELECT {", ".join(sel)} FROM {ds["from"]}{w} '
               f'GROUP BY {g} ORDER BY "{ob}" {direction} LIMIT ?')
        params.append(spec["limit"])
        return sql, params, aliases

    sel = [f'{cols[c][0]} AS "{c}"' for c in spec["columns"]]
    order = spec.get("sort") or {}
    if order.get("column") in cols:
        direction = "ASC" if (order.get("dir") or "desc").lower() == "asc" else "DESC"
        ob = f'{cols[order["column"]][0]} {direction}'
    else:
        ob = "1"
    sql = f'SELECT {", ".join(sel)} FROM {ds["from"]}{w} ORDER BY {ob} LIMIT ?'
    params.append(spec["limit"])
    return sql, params, spec["columns"]


def run(conn, spec):
    """Execute a spec. Degrades to an error payload, never raises into the handler."""
    try:
        spec = validate(spec)
    except SpecError as e:
        return {"ok": False, "error": str(e)}
    try:
        sql, params, out_cols = build_sql(spec)
        rows = [dict(r) for r in conn.execute(sql, params)]
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {e}", "spec": spec}
    ds = DATASETS[spec["dataset"]]
    labels = {c: (ds["cols"][c][1] if c in ds["cols"] else c.replace("_", " "))
              for c in out_cols}
    return {"ok": True, "spec": spec, "columns": out_cols, "labels": labels,
            "rows": rows, "count": len(rows),
            "truncated": len(rows) >= spec["limit"]}


# ---- description -> spec ----------------------------------------------------
SYSTEM = """You turn a plain-English request for a report into a strict JSON SPEC.

You never write SQL. You emit only a spec object, and it is validated against a
registry before it runs — an unknown dataset, column, or operator is rejected
outright, so guessing a name fails the request rather than producing a wrong report.

Return ONLY this JSON:
{"dataset": "<dataset id>",
 "title": "<short title for the report>",
 "columns": ["<column id>", ...],
 "filters": [{"column":"<id>","op":"<operator>","value":<v>}],
 "group_by": "<column id or null>",
 "aggregates": [{"fn":"count|sum|avg|min|max","column":"<id or null>"}],
 "sort": {"column":"<column id or aggregate alias>","dir":"asc|desc"},
 "limit": <int>}

Rules:
- Use ONLY dataset ids, column ids and operators from the schema given below.
- Pick the dataset that can actually answer the question. If several could, prefer
  the one whose columns need no aggregation.
- "top N" means sort desc + limit N. "by X" usually means group_by X.
- For a count-by-something report set group_by and leave columns as the default.
- Dates are ISO strings; use gte/lte for ranges.
- If the request cannot be answered from the schema, return
  {"error": "<one sentence saying what is missing>"} instead of guessing."""


def describe(description, project_root, schema=None):
    """Natural language -> a validated spec.

    A model failure is reported, never silently replaced with a default report: a
    plausible-looking table answering a different question is worse than an error.
    """
    if not (os.environ.get("ANTHROPIC_API_KEY") or "").strip():
        raise RuntimeError("ANTHROPIC_API_KEY is not set")
    desc = str(description or "").strip()
    if not desc:
        raise SpecError("describe what you want to see")
    import sys
    scripts = Path(project_root) / ".claude" / "skills" / "ai-sdr" / "scripts"
    if str(scripts) not in sys.path:
        sys.path.insert(0, str(scripts))
    import anthropic_client
    user = (f"SCHEMA:\n{json.dumps(schema or schema_payload(), indent=2)}\n\n"
            f"REQUEST:\n{desc}\n\nReturn only the JSON spec.")
    try:
        client = anthropic_client.AnthropicClient()
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(str(e))
    res = client.complete(SYSTEM, user, max_tokens=1500, timeout=120)
    try:
        parsed = anthropic_client.extract_json(res["text"])
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(f"the model did not return usable JSON: {e}")
    if parsed.get("error"):
        raise SpecError(parsed["error"])
    return validate(parsed)
