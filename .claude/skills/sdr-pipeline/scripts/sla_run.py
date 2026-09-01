#!/usr/bin/env python3
"""Evaluate one SLA against HubSpot and add its new matches to the SLA's static list.

    python3 sla_run.py --sla-file data/outreach/slas.json --sla-id sla-1a2b3c4d [--dry-run] [--json]

Steps (all read-only against HubSpot except the list create / add-members):
  1. candidates
       form set     → submitters of the form since the SLA's last run (form-integrations
                       v1 submissions API), resolved to contact ids by email; if property
                       criteria exist they FILTER these candidates
       no form      → contacts matching the criteria: company filters → company ids →
                       contacts associated with them (associations.company IN …), AND-ed
                       with the contact filters; contact filters alone → plain search
  2. minus contacts already in the pipeline DB (read-only) and ids already added by
     this SLA before (sla['added_ids'])
  3. ensure the SLA's HubSpot static list exists ("AI SDR SLA · <name>"), add the new
     ids (HubSpot ignores duplicates)
  4. print a JSON summary as the LAST stdout line: {ok, matched, new, added, list_id,
     list_created, form_submissions, errors[], dry_run}
The console (app.py run_sla) then runs the standard pull + init on that list.

Operators are HubSpot CRM-search operators; date values in YYYY-MM-DD are converted to
epoch-ms; enumeration IN/NOT_IN take `values`. HubSpot search caps at 10,000 results
per query — we window by hs_object_id GT like the other search-based scripts.
stdlib only (hubspot_client).
"""

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from hubspot_client import HubSpotClient, HubSpotError  # noqa: E402

PROJECT_ROOT = Path(__file__).resolve().parents[4]
DB_PATH = PROJECT_ROOT / "data" / "outreach" / "pipeline.db"
SEARCH_CAP = 9800
IN_CHUNK = 100


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _to_ms(v):
    """YYYY-MM-DD (or ISO) → epoch ms string; anything else passes through."""
    s = str(v).strip()
    for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%SZ"):
        try:
            return str(int(datetime.strptime(s[:19] if "T" in s else s, fmt).replace(tzinfo=timezone.utc).timestamp() * 1000))
        except ValueError:
            continue
    return s


def hs_filters(filters):
    """SLA filter dicts → HubSpot search filters."""
    out = []
    for f in filters or []:
        op = f["operator"]
        base = {"propertyName": f["property"], "operator": op}
        is_date = (f.get("type") in ("date", "datetime")) or f.get("field_type") == "date"
        conv = _to_ms if is_date else (lambda x: str(x))
        if op in ("HAS_PROPERTY", "NOT_HAS_PROPERTY"):
            pass
        elif op in ("IN", "NOT_IN"):
            base["values"] = [conv(v) for v in f.get("values") or []]
        elif op == "BETWEEN":
            base["value"], base["highValue"] = conv(f["value"]), conv(f["high_value"])
        else:
            base["value"] = conv(f["value"])
        out.append(base)
    return out


def search_all_ids(hs, object_type, filters, log, cap=SEARCH_CAP):
    """Every id matching `filters` (AND), windowing past HubSpot's 10k cap by
    hs_object_id GT <last>. Returns a list of id strings."""
    ids, last_id, rounds = [], None, 0
    while True:
        rounds += 1
        f = list(filters)
        if last_id is not None:
            f.append({"propertyName": "hs_object_id", "operator": "GT", "value": str(last_id)})
        after, got = None, 0
        while True:
            results, after, _total = hs.search_page(object_type, filters=f, properties=["hs_object_id"],
                                                     sorts=[{"propertyName": "hs_object_id", "direction": "ASCENDING"}],
                                                     after=after, limit=100)
            for r in results:
                ids.append(str(r["id"]))
                last_id = int(r["id"])
                got += 1
            if not after or got >= cap:
                break
        if got < cap or rounds > 50:
            break
        log(f"  {object_type}: windowing past {len(ids)} results…")
    return ids


def form_submitters(hs, form_id, since_iso, log, page_cap=200):
    """(emails, n_submissions) for submissions strictly newer than since_iso."""
    since_ms = 0
    if since_iso:
        try:
            since_ms = int(datetime.strptime(since_iso[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc).timestamp() * 1000)
        except ValueError:
            since_ms = 0
    emails, n, after, pages = [], 0, None, 0
    while True:
        pages += 1
        params = {"limit": 50}
        if after:
            params["after"] = after
        payload = hs._request("GET", f"/form-integrations/v1/submissions/forms/{form_id}", params=params)
        results = payload.get("results") or []
        stop = False
        for sub in results:
            ts = int(sub.get("submittedAt") or 0)
            if ts <= since_ms:
                stop = True
                break
            n += 1
            for kv in sub.get("values") or []:
                if str(kv.get("name") or "").lower() == "email" and kv.get("value"):
                    emails.append(str(kv["value"]).strip().lower())
        after = (payload.get("paging") or {}).get("next", {}).get("after")
        if stop or not after or pages >= page_cap:
            break
    return sorted(set(emails)), n


def in_pipeline_ids():
    """Contact ids already in the pipeline DB. Uses the pipeline scripts' own
    connection helper (WAL, busy timeout) — a plain mode=ro open fails on some
    hosts when the WAL sidecars don't exist yet."""
    try:
        import batch_db  # noqa: E402  (same scripts dir)
        conn = batch_db.connect()
        ids = {str(r[0]) for r in conn.execute("SELECT contact_id FROM contacts")}
        conn.close()
        return ids
    except Exception:  # noqa: BLE001 — never block an SLA on the local DB
        return set()


def evaluate(hs, sla, log):
    """→ (candidate_ids, meta) — the contacts this SLA matches right now."""
    meta = {"form_submissions": 0, "company_matches": 0}
    contact_f = hs_filters(sla.get("contact_filters"))
    company_f = hs_filters(sla.get("company_filters"))
    company_ids = None
    if company_f:
        company_ids = search_all_ids(hs, "companies", company_f, log)
        meta["company_matches"] = len(company_ids)
        log(f"  companies matching: {len(company_ids)}")
        if not company_ids:
            return [], meta

    def contacts_with(extra_filters):
        """Contacts matching contact filters + extra, restricted to company_ids
        when company filters exist (chunked IN on associations.company)."""
        if company_ids is None:
            return search_all_ids(hs, "contacts", contact_f + extra_filters, log)
        out = []
        for i in range(0, len(company_ids), IN_CHUNK):
            chunk = company_ids[i:i + IN_CHUNK]
            f = contact_f + extra_filters + [{"propertyName": "associations.company", "operator": "IN", "values": chunk}]
            out.extend(search_all_ids(hs, "contacts", f, log))
        return sorted(set(out))

    if sla.get("form"):
        emails, n = form_submitters(hs, sla["form"]["id"], sla.get("last_run_at"), log)
        meta["form_submissions"] = n
        log(f"  form submissions since last run: {n} ({len(emails)} unique emails)")
        if not emails:
            return [], meta
        found = hs.find_existing_email_ids(emails)
        cids = sorted({str(v) for v in found.values() if v})
        if not cids:
            return [], meta
        if not contact_f and company_ids is None:
            return cids, meta
        out = []
        for i in range(0, len(cids), IN_CHUNK):
            chunk = cids[i:i + IN_CHUNK]
            out.extend(contacts_with([{"propertyName": "hs_object_id", "operator": "IN", "values": chunk}]))
        return sorted(set(out)), meta
    if not contact_f and company_ids is None:
        return [], meta
    return contacts_with([]), meta


def main(argv=None):
    ap = argparse.ArgumentParser(description="Evaluate an SLA and add new matches to its HubSpot list")
    ap.add_argument("--sla-file", required=True)
    ap.add_argument("--sla-id", required=True)
    ap.add_argument("--dry-run", action="store_true", help="evaluate only; no list create / add")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    log = (lambda m: print(m, file=sys.stderr, flush=True)) if args.json else (lambda m: print(m, flush=True))

    data = json.loads(Path(args.sla_file).read_text("utf-8"))
    sla = next((s for s in data.get("slas") or [] if s.get("id") == args.sla_id), None)
    if not sla:
        print(json.dumps({"ok": False, "error": f"no SLA {args.sla_id}"}))
        return 2
    summary = {"ok": True, "sla_id": sla["id"], "dry_run": args.dry_run, "matched": 0, "new": 0, "added": 0,
               "list_id": sla.get("hubspot_list_id"), "list_created": False, "errors": [], "at": now_iso()}
    try:
        hs = HubSpotClient()
        log(f"[sla] {sla['name']}: evaluating…")
        cids, meta = evaluate(hs, sla, log)
        summary.update(meta)
        summary["matched"] = len(cids)
        already = set(str(x) for x in (sla.get("added_ids") or []))
        in_db = in_pipeline_ids()
        new = [c for c in cids if c not in already and c not in in_db]
        summary["new"] = len(new)
        summary["skipped_in_pipeline"] = sum(1 for c in cids if c in in_db)
        summary["skipped_already_added"] = sum(1 for c in cids if c in already and c not in in_db)
        log(f"[sla] matched {len(cids)}, new {len(new)}")
        if args.dry_run or not new:
            summary["new_ids"] = new[:5000]
        else:
            list_id = sla.get("hubspot_list_id")
            if not list_id:
                name = f"AI SDR SLA · {sla['name']}"[:250]
                list_id = hs.create_list(name, object_type_id="0-1")   # idempotent by name
                summary["list_created"] = True
                summary["list_name"] = name
                log(f"[sla] created HubSpot list {list_id!r} {name!r}")
            summary["list_id"] = list_id
            hs.add_contacts_to_list(list_id, new)   # chunks of 100 internally
            summary["added"] = len(new)
            summary["new_ids"] = new[:5000]
            log(f"[sla] added {len(new)} contacts to list {list_id}")
    except HubSpotError as e:
        summary["ok"] = False
        summary["errors"].append(str(e)[:400])
    except Exception as e:  # noqa: BLE001
        summary["ok"] = False
        summary["errors"].append(f"{type(e).__name__}: {e}"[:400])
    print(json.dumps(summary))
    return 0 if summary["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
