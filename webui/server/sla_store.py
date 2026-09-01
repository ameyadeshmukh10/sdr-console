"""SLAs — automatic enrollment rules for the Use view (added 2026-08-18).

An SLA says: "every <interval>, look in HubSpot for contacts that match <criteria>
and enroll them" — the second way (next to picking a list by hand) to feed the AI
SDR. Criteria are any mix of

  * a HubSpot FORM — new submissions since the last run trigger the run
    (submitters are the candidates; property criteria then filter them),
  * up to 10 COMPANY (account) property filters (operator + value),
  * up to 10 CONTACT property filters (operator + value),

with at least one criterion overall. Each SLA maintains its own HubSpot static
list ("AI SDR SLA · <name>"): a run adds the new matches to that list and then
runs the standard pull + init on it (scripts/sla_run.py + app.do_ingest), so ICP
gating and suppression apply exactly as for a hand-picked list.

Schedule (user-facing picklist): every 5 | 15 minutes, 1 | 5 | 10 hours, or
3 | 7 | 14 days — day schedules always fire at 22:00 UTC.

Storage: data/outreach/slas.json (runtime state on the volume; gitignored).
stdlib only; no I/O at import.
"""

import json
import os
import re
import secrets
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

ALLOWED_EVERY = {"minutes": (5, 15), "hours": (1, 5, 10), "days": (3, 7, 14)}
DAY_HOUR_UTC = 22
MAX_FILTERS = 10
MAX_NAME = 100
OBJECT_TYPES = ("companies", "contacts")
OPERATORS = ("EQ", "NEQ", "CONTAINS_TOKEN", "NOT_CONTAINS_TOKEN", "HAS_PROPERTY", "NOT_HAS_PROPERTY",
             "IN", "NOT_IN", "GT", "GTE", "LT", "LTE", "BETWEEN")
NO_VALUE_OPS = ("HAS_PROPERTY", "NOT_HAS_PROPERTY")
LIST_OPS = ("IN", "NOT_IN")

_LOCK = threading.Lock()


class SlaError(ValueError):
    def __init__(self, msg, code=400):
        super().__init__(msg)
        self.code = code


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_iso(s):
    if not s:
        return None
    try:
        return datetime.strptime(str(s)[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _new_id():
    return "sla-" + secrets.token_hex(4)


# --------------------------------------------------------------------------
# validation
# --------------------------------------------------------------------------

def validate_schedule(sched):
    if not isinstance(sched, dict):
        raise SlaError("schedule is required")
    unit = str(sched.get("unit") or "").strip().lower()
    if unit not in ALLOWED_EVERY:
        raise SlaError("schedule unit must be minutes, hours or days")
    try:
        every = int(sched.get("every"))
    except (TypeError, ValueError):
        raise SlaError("schedule every must be a number")
    if every not in ALLOWED_EVERY[unit]:
        raise SlaError(f"every {every} {unit} is not an option (allowed: {', '.join(map(str, ALLOWED_EVERY[unit]))})")
    return {"every": every, "unit": unit}


def validate_filters(filters, object_type):
    if filters is None:
        return []
    if not isinstance(filters, list):
        raise SlaError(f"{object_type} filters must be a list")
    if len(filters) > MAX_FILTERS:
        raise SlaError(f"at most {MAX_FILTERS} {object_type} properties")
    out = []
    for f in filters:
        if not isinstance(f, dict):
            continue
        prop = str(f.get("property") or "").strip()
        if not re.fullmatch(r"[A-Za-z0-9_.\-]+", prop or ""):
            raise SlaError(f"{object_type}: property name is required")
        op = str(f.get("operator") or "EQ").strip().upper()
        if op not in OPERATORS:
            raise SlaError(f"{object_type} {prop}: unknown operator {op}")
        entry = {"property": prop, "operator": op, "label": str(f.get("label") or prop)[:120],
                 "type": str(f.get("type") or "")[:40], "field_type": str(f.get("field_type") or "")[:40]}
        if op in NO_VALUE_OPS:
            pass
        elif op in LIST_OPS:
            vals = f.get("values")
            if isinstance(vals, str):
                vals = [v.strip() for v in vals.split(",")]
            vals = [str(v).strip() for v in (vals or []) if str(v).strip()]
            if not vals:
                raise SlaError(f"{object_type} {prop}: {op} needs at least one value")
            entry["values"] = vals[:200]
        elif op == "BETWEEN":
            lo, hi = str(f.get("value") or "").strip(), str(f.get("high_value") or "").strip()
            if not lo or not hi:
                raise SlaError(f"{object_type} {prop}: BETWEEN needs a low and a high value")
            entry["value"], entry["high_value"] = lo, hi
        else:
            val = str(f.get("value") if f.get("value") is not None else "").strip()
            if val == "":
                raise SlaError(f"{object_type} {prop}: a value is required for {op}")
            entry["value"] = val
        out.append(entry)
    return out


def validate_sla(spec, existing=None):
    """Return a clean SLA dict from a wizard payload (create or edit)."""
    if not isinstance(spec, dict):
        raise SlaError("invalid SLA")
    name = str(spec.get("name") or "").strip()
    if not name:
        raise SlaError("a name is required")
    if len(name) >= MAX_NAME:
        raise SlaError(f"the name must be under {MAX_NAME} characters")
    schedule = validate_schedule(spec.get("schedule"))
    form = spec.get("form") if isinstance(spec.get("form"), dict) else None
    if form:
        fid = str(form.get("id") or "").strip()
        if not fid:
            form = None
        else:
            form = {"id": fid, "name": str(form.get("name") or fid)[:160]}
    company_filters = validate_filters(spec.get("company_filters"), "company")
    contact_filters = validate_filters(spec.get("contact_filters"), "contact")
    if not form and not company_filters and not contact_filters:
        raise SlaError("pick a form or add at least one company / contact property")
    base = dict(existing or {})
    base.update({
        "name": name, "schedule": schedule, "form": form,
        "company_filters": company_filters, "contact_filters": contact_filters,
        "enabled": bool(spec.get("enabled", base.get("enabled", True))),
    })
    return base


# --------------------------------------------------------------------------
# schedule math
# --------------------------------------------------------------------------

def next_run_after(sched, from_dt=None):
    """The next fire time after `from_dt` (default now). Minutes/hours: from + N.
    Days: the next 22:00 UTC that is >= N days after `from_dt`'s date when
    from_dt is a previous run; for a fresh SLA (from_dt None) the next 22:00 UTC."""
    now = datetime.now(timezone.utc)
    unit, every = sched["unit"], int(sched["every"])
    if unit == "minutes":
        return (from_dt or now) + timedelta(minutes=every)
    if unit == "hours":
        return (from_dt or now) + timedelta(hours=every)
    # days — 22:00 UTC boundaries
    if from_dt is None:
        cand = now.replace(hour=DAY_HOUR_UTC, minute=0, second=0, microsecond=0)
        if cand <= now:
            cand += timedelta(days=1)
        return cand
    cand = (from_dt + timedelta(days=every)).replace(hour=DAY_HOUR_UTC, minute=0, second=0, microsecond=0)
    while cand <= now:
        cand += timedelta(days=every)
    return cand


def schedule_label(sched):
    unit, every = sched.get("unit"), sched.get("every")
    if unit == "days":
        return f"every {every} day{'s' if every != 1 else ''} at 22:00 UTC"
    return f"every {every} {unit if every != 1 else unit[:-1]}"


# --------------------------------------------------------------------------
# store
# --------------------------------------------------------------------------

class SlaStore:
    def __init__(self, path):
        self.path = Path(path)

    def _load(self):
        try:
            data = json.loads(self.path.read_text("utf-8"))
            if isinstance(data, dict) and isinstance(data.get("slas"), list):
                return data
        except (OSError, ValueError):
            pass
        return {"version": 1, "slas": []}

    def _save(self, data):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, indent=2, sort_keys=True), "utf-8")
        os.replace(tmp, self.path)

    def list(self):
        return [self._public(s) for s in self._load()["slas"]]

    def get(self, sla_id):
        for s in self._load()["slas"]:
            if s.get("id") == sla_id:
                return s
        return None

    def upsert(self, spec, by):
        with _LOCK:
            data = self._load()
            sla_id = str(spec.get("id") or "").strip() or None
            existing = None
            if sla_id:
                existing = next((s for s in data["slas"] if s.get("id") == sla_id), None)
                if existing is None:
                    raise SlaError("no such SLA", 404)
            clean = validate_sla(spec, existing)
            ts = now_iso()
            if existing is None:
                clean.update({"id": _new_id(), "created_at": ts, "created_by": by,
                              "hubspot_list_id": None, "hubspot_list_name": None,
                              "last_run_at": None, "last_run": None, "runs": [],
                              "totals": {"matched": 0, "added": 0, "runs": 0}})
                clean["next_run_at"] = next_run_after(clean["schedule"]).strftime("%Y-%m-%dT%H:%M:%SZ")
                data["slas"].append(clean)
            else:
                # schedule changed → recompute from the last run (or now)
                if existing.get("schedule") != clean["schedule"] or not existing.get("next_run_at"):
                    clean["next_run_at"] = next_run_after(clean["schedule"], _parse_iso(existing.get("last_run_at"))).strftime("%Y-%m-%dT%H:%M:%SZ")
                data["slas"] = [clean if s.get("id") == sla_id else s for s in data["slas"]]
            clean.update({"updated_at": ts, "updated_by": by})
            self._save(data)
            return self._public(clean)

    def delete(self, sla_id):
        with _LOCK:
            data = self._load()
            before = len(data["slas"])
            data["slas"] = [s for s in data["slas"] if s.get("id") != sla_id]
            if len(data["slas"]) == before:
                raise SlaError("no such SLA", 404)
            self._save(data)
        return True

    def set_enabled(self, sla_id, on, by):
        with _LOCK:
            data = self._load()
            s = next((x for x in data["slas"] if x.get("id") == sla_id), None)
            if s is None:
                raise SlaError("no such SLA", 404)
            s["enabled"] = bool(on)
            s["updated_at"], s["updated_by"] = now_iso(), by
            if on and not s.get("next_run_at"):
                s["next_run_at"] = next_run_after(s["schedule"], _parse_iso(s.get("last_run_at"))).strftime("%Y-%m-%dT%H:%M:%SZ")
            self._save(data)
            return self._public(s)

    def patch(self, sla_id, **fields):
        """Internal: persist run-time facts (list id, run records) without
        re-validating the spec."""
        with _LOCK:
            data = self._load()
            s = next((x for x in data["slas"] if x.get("id") == sla_id), None)
            if s is None:
                return None
            s.update(fields)
            self._save(data)
            return s

    def record_run(self, sla_id, run):
        """Append a run record, roll totals, and schedule the next fire."""
        with _LOCK:
            data = self._load()
            s = next((x for x in data["slas"] if x.get("id") == sla_id), None)
            if s is None:
                return None
            ts = run.get("at") or now_iso()
            s["last_run_at"] = ts
            s["last_run"] = run
            s["runs"] = ([run] + list(s.get("runs") or []))[:20]
            tot = s.setdefault("totals", {"matched": 0, "added": 0, "runs": 0})
            tot["runs"] = int(tot.get("runs") or 0) + 1
            tot["matched"] = int(tot.get("matched") or 0) + int(run.get("matched") or 0)
            tot["added"] = int(tot.get("added") or 0) + int(run.get("added") or 0)
            s["next_run_at"] = next_run_after(s["schedule"], _parse_iso(ts)).strftime("%Y-%m-%dT%H:%M:%SZ")
            self._save(data)
            return s

    def due(self, now=None):
        now = now or datetime.now(timezone.utc)
        out = []
        for s in self._load()["slas"]:
            if not s.get("enabled"):
                continue
            nxt = _parse_iso(s.get("next_run_at"))
            if nxt is None or nxt <= now:
                out.append(s)
        return out

    @staticmethod
    def _public(s):
        out = dict(s)
        out["schedule_label"] = schedule_label(s.get("schedule") or {})
        out["criteria_summary"] = criteria_summary(s)
        return out


def criteria_summary(s):
    parts = []
    if s.get("form"):
        parts.append(f"form “{s['form'].get('name') or s['form'].get('id')}”")
    n_c, n_k = len(s.get("company_filters") or []), len(s.get("contact_filters") or [])
    if n_c:
        parts.append(f"{n_c} account propert{'y' if n_c == 1 else 'ies'}")
    if n_k:
        parts.append(f"{n_k} contact propert{'y' if n_k == 1 else 'ies'}")
    return " + ".join(parts) or "—"


def default_path(data_dir):
    override = (os.environ.get("SLA_STORE_PATH") or "").strip()
    return Path(override) if override else Path(data_dir) / "outreach" / "slas.json"
