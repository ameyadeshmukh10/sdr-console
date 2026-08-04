"""Hiring-signal detection runner — module + CLI.

Detects whether a company is hiring — and specifically for sales/GTM roles —
via the Prospeo company-enrichment API (job postings), and stores the result
on the `account_signals` row for that domain:

    hiring_signals    "14 open roles · 4 sales: SDR; AE; VP Sales"
                      (or "N open roles · no sales roles", or the literal
                      "No open roles detected"; NULL only if the scan itself
                      failed — network/HTTP error or retries exhausted)
    hiring_detail     JSON: {active_count, active_titles, sales_titles,
                      error_code, duration_ms, hubspot}
    hiring_checked_at ISO-8601 Z; scans are reused for HIRING_REFRESH_DAYS (90)
    hiring_error      set only when the scan failed (such rows retry next touch)

Semantics (ported from the hubspot-hiring-signals repo, adapted to the
tech_signals invariant "non-NULL = definitive answer"):
  - matched, postings > 0      -> formatted line; HubSpot write-back
  - matched, 0/no postings     -> NO_HIRING literal; write-back clears ("0","","")
  - NO_MATCH / NO_RESULT / NO_IDENTIFIER / INVALID_DATAPOINT -> Prospeo answered
    "unknown company": NO_HIRING literal + error_code in detail, NO write-back,
    NOT retried until the refresh window lapses (each scan costs a credit)
  - transport failure / unknown error_code -> NULL + hiring_error, retried on
    next touch
Prospeo signals rate limiting as HTTP 200 + {"error":true,"error_code":
"Rate limit exceeded"} — that is retried inside _prospeo_request exactly like
an HTTP 429 and must never be stored as a result.

After a matched scan the three HubSpot company properties from the
hubspot-hiring-signals job are refreshed (best-effort; company matched by
domain; HIRING_HUBSPOT_WRITEBACK=0 to disable): open_roles_count (str int),
hiring_signals_job_titles (<br>-joined HTML, ALL titles), hiring_signals
("; "-joined sales subset — NOT the display line above).

Import contract: this module is stdlib-only, at import time and at runtime
(urllib, no requests/tenacity/pyyaml — the source repo's deps are not in this
image). webui/server/app.py imports it in-process; without PROSPEO_API_KEY,
`hiring_available()` reports why detection is off instead of crashing.

CLI (run_script conventions: progress lines on stderr, JSON summary as the
LAST stdout line):

    python3 hiring_signals.py --domain acme.com [--company "Acme"] [--force]
                              [--no-hubspot]
    python3 hiring_signals.py --missing [--stale-days N] [--limit N]
                              [--workers N] [--force] [--no-hubspot]
    python3 hiring_signals.py --self-test      # offline; no network, no key

The sales-title taxonomy is ported verbatim from the hubspot-hiring-signals
repo's config.yaml (regexes below); the retry/rate-limit handling from its
prospeo_client.py. Each non-skipped scan spends one Prospeo credit.
"""

import argparse
import html
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache

import batch_db as db
from hubspot_client import _load_dotenv

_load_dotenv()

PROSPEO_ENRICH_URL = "https://api.prospeo.io/enrich-company"
RATE_LIMIT_CODE = "Rate limit exceeded"
MAX_RETRIES = 5          # attempts, matching the source repo's tenacity policy
BACKOFF_MIN = 1.0        # seconds
BACKOFF_MAX = 16.0       # also caps a hostile Retry-After header
SINGLE_SLEEP = 0.2       # per-call pacing (per worker thread)
REQUEST_TIMEOUT = 60

NO_HIRING = "No open roles detected"
# Prospeo answered but couldn't resolve the company — a definitive "no data",
# not a transport failure. Stored as NO_HIRING so we don't re-spend a credit
# on the same unmatchable domain every touch.
DEFINITIVE_CODES = {"NO_MATCH", "NO_RESULT", "NO_IDENTIFIER", "INVALID_DATAPOINT"}
MAX_TITLES_IN_LINE = 3   # display cap; full lists live in hiring_detail

# The three properties maintained by the hubspot-hiring-signals repo; they
# already exist in the portal, so ensure_company_property takes the GET path.
HIRING_PROPERTIES = (
    ("open_roles_count", "Open Roles Count", "text"),
    ("hiring_signals_job_titles", "Hiring Signals Job Titles", "html"),
    ("hiring_signals", "Hiring Signals", "text"),
)

# ---- sales-title taxonomy (ported verbatim from hubspot-hiring-signals -----
# config.yaml `sales_hiring_signals`; word-boundary-aware, case-insensitive).
SALES_INCLUDE_PATTERNS = (
    # Individual contributor sales
    r"account executive",
    r"\bae\b",
    r"sales rep",
    r"sales representative",
    r"sales development",
    r"\bsdr\b",
    r"business development rep",
    r"business development representative",
    r"\bbdr\b",
    r"inside sales",
    r"outside sales",
    r"field sales",
    r"enterprise sales",
    r"mid-?market sales",
    r"smb sales",
    r"commercial sales",
    r"strategic account",
    r"named account",
    r"key account",
    r"territory manager",
    r"sales engineer",
    r"solutions engineer",
    r"solutions consultant",
    r"pre-?sales",
    r"sales executive",
    r"client executive",
    r"client partner",
    r"account manager",
    # Sales leadership
    r"head of sales",
    r"vp.{0,5}sales",
    r"vice president.{0,10}sales",
    r"director.{0,10}sales",
    r"sales director",
    r"sales manager",
    r"regional sales",
    r"chief revenue officer",
    r"\bcro\b",
    r"chief sales officer",
    r"\bcso\b",
    r"head of revenue",
    r"vp.{0,5}revenue",
    # RevOps / SalesOps / GTM Ops
    r"revenue operations",
    r"\brevops\b",
    r"sales operations",
    r"\bsalesops\b",
    r"gtm operations",
    r"go-?to-?market operations",
    r"sales enablement",
    r"revenue enablement",
    r"deal desk",
    r"sales analyst",
    r"sales strategy",
)

# Hard excludes — even if a title matches above, drop it if it matches these.
SALES_EXCLUDE_PATTERNS = (
    r"customer success",
    r"customer support",
    r"implementation",
    r"onboarding specialist",
    r"renewals",
    r"marketing",
    r"recruiter",
    r"talent",
)


def log(msg):
    sys.stderr.write(f"[hiring] {msg}\n")
    sys.stderr.flush()


def _flag(name, default=True):
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() not in ("0", "false", "no", "off")


def _env_float(name, default):
    try:
        return float(os.environ.get(name) or default)
    except ValueError:
        return default


def hiring_available():
    """(ok, reason). False when PROSPEO_API_KEY is missing — callers degrade
    instead of crashing (tech_available pattern). Not cached: the check is a
    plain env read, and .env was already autoloaded at import."""
    if (os.environ.get("PROSPEO_API_KEY") or "").strip():
        return (True, "")
    return (False, "PROSPEO_API_KEY not set")


# _clean_domain: same normalization as tech_signals.py — reduce a website/
# domain field to a bare apex-ish host.
def _clean_domain(domain):
    if not domain:
        return None
    d = str(domain).strip().lower()
    for prefix in ("https://", "http://"):
        if d.startswith(prefix):
            d = d[len(prefix):]
    d = d.split("/", 1)[0].split("?", 1)[0]
    if d.startswith("www."):
        d = d[4:]
    return d or None


# ---- sales-title classifier --------------------------------------------------
@lru_cache(maxsize=1)
def _compiled_patterns():
    includes = [re.compile(p, re.IGNORECASE) for p in SALES_INCLUDE_PATTERNS]
    excludes = [re.compile(p, re.IGNORECASE) for p in SALES_EXCLUDE_PATTERNS]
    return includes, excludes


def filter_sales_titles(titles):
    """Subset of `titles` that represent sales-team hiring. Exclude beats
    include; original casing and order preserved."""
    if not titles:
        return []
    includes, excludes = _compiled_patterns()
    kept = []
    for title in titles:
        if not title:
            continue
        lowered = title.lower()
        if any(pat.search(lowered) for pat in excludes):
            continue
        if any(pat.search(lowered) for pat in includes):
            kept.append(title)
    return kept


# ---- Prospeo client (stdlib port of prospeo_client.py's retry semantics) ----
class ProspeoHTTPError(Exception):
    """Non-retryable HTTP error (e.g. 401/403/404). Carries the status code."""

    def __init__(self, status_code, message):
        super().__init__(message)
        self.status_code = status_code


def _honor_retry_after(headers):
    """Sleep the duration in Retry-After, capped at BACKOFF_MAX. Non-numeric
    (HTTP-date) or absent headers are skipped."""
    try:
        retry_after = headers.get("Retry-After") if headers is not None else None
        if retry_after:
            time.sleep(min(float(retry_after), BACKOFF_MAX))
    except (TypeError, ValueError):
        pass


def _is_body_rate_limited(body):
    return (isinstance(body, dict)
            and body.get("error") is True
            and body.get("error_code") == RATE_LIMIT_CODE)


def _prospeo_request(payload):
    """POST JSON to the enrich endpoint and return the parsed body.

    Retries (MAX_RETRIES attempts, expo backoff 1..16s, Retry-After honored):
    HTTP 429/5xx, network errors, and Prospeo's body-level rate limit —
    HTTP 200 + {"error":true,"error_code":"Rate limit exceeded"} — which must
    NEVER be returned as a result. Other non-2xx raise ProspeoHTTPError;
    a non-JSON 200 raises ValueError. Both land in the transport-failure
    bucket at the caller."""
    ok, reason = hiring_available()
    if not ok:
        raise RuntimeError(f"hiring detection unavailable: {reason}")
    data = json.dumps(payload).encode("utf-8")
    last_err = None
    for attempt in range(MAX_RETRIES):
        if attempt:
            time.sleep(min(BACKOFF_MAX, max(BACKOFF_MIN, 2.0 ** attempt)))
        req = urllib.request.Request(PROSPEO_ENRICH_URL, data=data, method="POST", headers={
            "X-KEY": (os.environ.get("PROSPEO_API_KEY") or "").strip(),
            "Content-Type": "application/json",
        })
        try:
            with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
                raw = resp.read().decode("utf-8", "replace")
                headers = resp.headers
        except urllib.error.HTTPError as exc:
            if exc.code == 429 or exc.code >= 500:
                _honor_retry_after(exc.headers)
                last_err = ProspeoHTTPError(exc.code, f"HTTP {exc.code} from Prospeo")
                continue
            try:
                snippet = exc.read().decode("utf-8", "replace")[:200]
            except Exception:  # noqa: BLE001
                snippet = ""
            raise ProspeoHTTPError(exc.code, f"HTTP {exc.code} from Prospeo: {snippet}")
        except Exception as exc:  # noqa: BLE001 — URLError, timeout, SSL
            last_err = exc
            continue
        body = json.loads(raw)  # ValueError -> transport failure at the caller
        if _is_body_rate_limited(body):
            _honor_retry_after(headers)
            last_err = RuntimeError("Prospeo body-level rate limit (HTTP 200)")
            continue
        return body
    raise last_err if last_err is not None else RuntimeError("prospeo request failed")


# ---- response classification (pure; the self-test target) --------------------
def _format_line(active_count, sales_titles):
    n = int(active_count or 0)
    if n <= 0:
        return NO_HIRING
    roles = f"{n} open role{'s' if n != 1 else ''}"
    if not sales_titles:
        return f"{roles} · no sales roles"
    shown = "; ".join(sales_titles[:MAX_TITLES_IN_LINE])
    return f"{roles} · {len(sales_titles)} sales: {shown}"


def _classify_response(body):
    """Map a parsed Prospeo body to the storage shape (semantics table in the
    module docstring): {formatted, active_count, active_titles, sales_titles,
    error_code, error}. `error` set (and formatted None) ONLY for transport-
    grade failures; the rate-limit code classifies as transport too (defense
    in depth — _prospeo_request retries it and never returns it)."""
    out = {"formatted": None, "active_count": None, "active_titles": [],
           "sales_titles": [], "error_code": None, "error": None}
    if not isinstance(body, dict):
        out["error"] = "unexpected Prospeo response (not a JSON object)"
        return out
    if body.get("error"):
        code = body.get("error_code")
        out["error_code"] = code
        if code in DEFINITIVE_CODES:
            out["formatted"] = NO_HIRING  # definitive "no data for this company"
        else:
            out["error"] = f"prospeo error: {code or 'unknown'}"
        return out
    postings = (body.get("company") or {}).get("job_postings") or {}
    try:
        count = int(postings.get("active_count") or 0)
    except (TypeError, ValueError):
        count = 0
    titles = [t for t in (postings.get("active_titles") or []) if t]
    sales = filter_sales_titles(titles)
    out.update({"formatted": _format_line(count, sales), "active_count": count,
                "active_titles": titles, "sales_titles": sales})
    return out


# ---- detection ---------------------------------------------------------------
def detect_domain(domain, company=None):
    """Pure detection (no DB): one Prospeo enrich call -> classify. Returns
    {formatted, active_count, active_titles, sales_titles, error_code, error,
    duration_ms}. `error` is set — and formatted is None — only when the
    request itself failed (retried transiently first)."""
    ok, reason = hiring_available()
    if not ok:
        raise RuntimeError(f"hiring detection unavailable: {reason}")
    host = _clean_domain(domain)
    if not host:
        return {"formatted": None, "active_count": None, "active_titles": [],
                "sales_titles": [], "error_code": None,
                "error": f"invalid domain: {domain!r}", "duration_ms": 0}

    t0 = time.monotonic()
    entry = {"company_website": host}
    if company:
        entry["company_name"] = str(company)
    try:
        body = _prospeo_request({"data": entry})
        res = _classify_response(body)
    except Exception as exc:  # noqa: BLE001 — transport failure, retried already
        res = {"formatted": None, "active_count": None, "active_titles": [],
               "sales_titles": [], "error_code": None, "error": str(exc)[:300]}
    time.sleep(SINGLE_SLEEP)  # pacing, per worker thread (source repo behavior)
    res["duration_ms"] = int((time.monotonic() - t0) * 1000)
    return res


# ---- store + orchestrate -----------------------------------------------------
_SCHEMA_LOCK = threading.Lock()
_SCHEMA_READY = False


def _db():
    global _SCHEMA_READY
    conn = db.connect()
    if not _SCHEMA_READY:
        with _SCHEMA_LOCK:
            if not _SCHEMA_READY:
                db.init_schema(conn)  # guarantees the hiring_* columns exist
                _SCHEMA_READY = True
    return conn


def _sales_count(hiring_detail):
    try:
        return len(json.loads(hiring_detail or "{}").get("sales_titles") or [])
    except (ValueError, TypeError):
        return 0


def detect_and_store(domain, company=None, force=False, hubspot=None):
    """Cache-aware detect for one domain: skip when the last DEFINITIVE scan is
    younger than HIRING_REFRESH_DAYS (failed scans always retry), else call
    Prospeo, upsert the account_signals row, and best-effort refresh the three
    HubSpot company properties. hubspot=None follows HIRING_HUBSPOT_WRITEBACK;
    True/False overrides."""
    ok, reason = hiring_available()
    if not ok:
        raise RuntimeError(f"hiring detection unavailable: {reason}")
    host = _clean_domain(domain)
    if not host:
        raise ValueError(f"invalid domain: {domain!r}")

    conn = _db()
    try:
        row = db.get_signal(conn, host)
        refresh_days = _env_float("HIRING_REFRESH_DAYS", 90)
        if (row and not force and row.get("hiring_signals") is not None
                and db.hiring_fresh(row, days=refresh_days)):
            return {"domain": host, "skipped": True,
                    "hiring_signals": row.get("hiring_signals"),
                    "hiring_error": row.get("hiring_error"),
                    "hiring_checked_at": row.get("hiring_checked_at"),
                    "sales_count": _sales_count(row.get("hiring_detail")),
                    "hubspot": None}
    finally:
        conn.close()

    res = detect_domain(host, company=company)

    # HubSpot write-back BEFORE the upsert (no DB connection held across the
    # HTTP call) so its outcome persists into hiring_detail for the drawer.
    # Only matched results write (a 0-postings match clears stale values, the
    # source repo's behavior); definitive non-matches record why they didn't.
    hs = None
    if res["error"] is None and res["error_code"] is None:
        enabled = _flag("HIRING_HUBSPOT_WRITEBACK", True) if hubspot is None else bool(hubspot)
        hs = (hubspot_writeback(host, res["active_count"], res["active_titles"],
                                res["sales_titles"])
              if enabled else {"ok": False, "reason": "disabled"})
    elif res["error"] is None:
        hs = {"ok": False, "reason": "not_matched"}

    detail = {
        "active_count": res["active_count"],
        "active_titles": res["active_titles"],
        "sales_titles": res["sales_titles"],
        "error_code": res["error_code"],
        "duration_ms": res["duration_ms"],
        "hubspot": hs,
    }
    conn = _db()
    try:
        db.upsert_hiring_signals(conn, host, res["formatted"],
                                 hiring_detail=json.dumps(detail, ensure_ascii=False),
                                 hiring_error=res["error"], company_name=company)
        # Meter the credit. We reached Prospeo (the cache-skip path returned long
        # before here), so this billed whatever the outcome was — including a
        # definitive NO_MATCH, which still costs.
        db.record_usage(conn, "prospeo", "enrich-company", 1, "credits", ref=host,
                        detail={"error_code": res["error_code"]} if res["error_code"] else None)
        stored = db.get_signal(conn, host) or {}
    finally:
        conn.close()

    return {"domain": host, "skipped": False, "hiring_signals": res["formatted"],
            "hiring_error": res["error"], "hiring_checked_at": stored.get("hiring_checked_at"),
            "sales_count": len(res["sales_titles"]), "hubspot": hs}


# ---- HubSpot write-back (best-effort; never raises) --------------------------
_HS_LOCK = threading.Lock()
_HS_CLIENT = None
_HS_PROPERTIES_READY = False


def _rich_text_titles(titles):
    """ALL titles for the rich-text property: HTML-escaped, <br>-joined."""
    return "<br>".join(html.escape(t) for t in titles) if titles else ""


def _single_line_titles(titles):
    """Sales subset for the single-line property: '; '-joined."""
    return "; ".join(titles) if titles else ""


def hubspot_writeback(domain, active_count, all_titles, sales_titles):
    global _HS_CLIENT, _HS_PROPERTIES_READY
    try:
        if not os.environ.get("HUBSPOT_ACCESS_TOKEN"):
            return {"ok": False, "reason": "no_token"}
        import hubspot_client as hc
        with _HS_LOCK:
            if _HS_CLIENT is None:
                _HS_CLIENT = hc.HubSpotClient()
            client = _HS_CLIENT
            if not _HS_PROPERTIES_READY:
                for name, label, field_type in HIRING_PROPERTIES:
                    client.ensure_company_property(name, label, field_type=field_type)
                _HS_PROPERTIES_READY = True
        company_id = client.find_company_id_by_domain(domain)
        if not company_id:
            return {"ok": False, "reason": "no_company"}
        client.update_company(company_id, {
            "open_roles_count": str(int(active_count or 0)),
            "hiring_signals_job_titles": _rich_text_titles(all_titles),
            "hiring_signals": _single_line_titles(sales_titles),
        })
        return {"ok": True, "company_id": company_id}
    except Exception as exc:  # noqa: BLE001 — write-back must never fail a scan
        log(f"hubspot writeback failed for {domain}: {exc}")
        return {"ok": False, "error": str(exc)[:300]}


# ---- bulk ---------------------------------------------------------------------
def backfill(domains=None, stale_days=None, limit=None, workers=3, force=False,
             hubspot=None, progress=None):
    """Scan many domains (ThreadPoolExecutor; keep workers modest — Prospeo is
    a metered, rate-limited API and every non-skipped scan costs a credit).
    domains=None pulls the never-scanned (plus stale, when stale_days is set)
    from account_signals. Returns {total, detected, skipped, errors,
    hubspot_ok, hubspot_missing}."""
    ok, reason = hiring_available()
    if not ok:
        raise RuntimeError(f"hiring detection unavailable: {reason}")

    if domains is None:
        conn = _db()
        try:
            rows = db.domains_missing_hiring(conn, stale_days=stale_days, limit=limit)
        finally:
            conn.close()
        items = [(r["domain"], r.get("company_name")) for r in rows]
    else:
        items = [(d, None) for d in (_clean_domain(x) for x in domains) if d]
        if limit:
            items = items[:int(limit)]

    summary = {"total": len(items), "detected": 0, "skipped": 0, "errors": 0,
               "hubspot_ok": 0, "hubspot_missing": 0}
    if not items:
        return summary

    def work(item):
        d, c = item
        try:
            return d, detect_and_store(d, company=c, force=force, hubspot=hubspot)
        except Exception as exc:  # noqa: BLE001 — one bad domain never kills the run
            return d, {"domain": d, "error_exc": str(exc)[:300]}

    done = 0
    with ThreadPoolExecutor(max_workers=max(1, int(workers))) as ex:
        for d, res in ex.map(work, items):
            done += 1
            if res.get("error_exc") or res.get("hiring_error"):
                summary["errors"] += 1
            elif res.get("skipped"):
                summary["skipped"] += 1
            else:
                summary["detected"] += 1
            hs = res.get("hubspot") or {}
            if hs.get("ok"):
                summary["hubspot_ok"] += 1
            elif hs.get("reason") == "no_company":
                summary["hubspot_missing"] += 1
            if progress:
                try:
                    progress(done, len(items), d, res)
                except Exception:  # noqa: BLE001 — progress is cosmetic
                    pass
    return summary


# ---- offline self-test ---------------------------------------------------------
_FIXTURE_MATCH = {
    "error": False,
    "company": {"job_postings": {"active_count": 11, "active_titles": [
        "Enterprise Account Executive",
        "Sales Development Representative",
        "VP of Sales",
        "Revenue Operations Manager",
        "Renewals Account Manager",     # exclude ("renewals") beats include ("account manager")
        "Aerospace Engineer",           # must NOT match \bae\b (word boundary)
        "Senior Software Engineer",
        "Customer Success Manager",
        "Technical Recruiter, Sales",   # exclude ("recruiter") beats include
        "Marketing Operations Manager",
        "AE, Mid-Market",
    ]}},
}
_FIXTURE_ZERO = {"error": False, "company": {"job_postings": {"active_count": 0, "active_titles": []}}}
_FIXTURE_NULL = {"error": False, "company": {}}
_FIXTURE_NO_MATCH = {"error": True, "error_code": "NO_MATCH"}
_FIXTURE_FAILED = {"error": True, "error_code": "REQUEST_FAILED"}
_FIXTURE_RATE_LIMIT = {"error": True, "error_code": "Rate limit exceeded"}


def self_test():
    """Offline check of the classifier + response classification (no network,
    no PROSPEO_API_KEY, no DB). Exit code 0/1."""
    expected_sales = ["Enterprise Account Executive", "Sales Development Representative",
                      "VP of Sales", "Revenue Operations Manager", "AE, Mid-Market"]

    sales = filter_sales_titles(_FIXTURE_MATCH["company"]["job_postings"]["active_titles"])
    m = _classify_response(_FIXTURE_MATCH)
    zero = _classify_response(_FIXTURE_ZERO)
    null = _classify_response(_FIXTURE_NULL)
    nomatch = _classify_response(_FIXTURE_NO_MATCH)
    failed = _classify_response(_FIXTURE_FAILED)
    ratelim = _classify_response(_FIXTURE_RATE_LIMIT)

    checks = [
        ("classifier keeps sales/GTM titles in order, original casing", sales == expected_sales),
        ("exclude beats include (Renewals Account Manager dropped)",
         "Renewals Account Manager" not in sales),
        ("\\bae\\b respects word boundaries (Aerospace Engineer dropped)",
         "Aerospace Engineer" not in sales),
        ("recruiter excluded even with 'Sales' in the title",
         "Technical Recruiter, Sales" not in sales),
        ("matched -> formatted line with count + sales cap",
         m["formatted"] == "11 open roles · 5 sales: Enterprise Account Executive; "
                           "Sales Development Representative; VP of Sales"),
        ("matched -> no error, full lists in detail fields",
         m["error"] is None and m["active_count"] == 11 and len(m["active_titles"]) == 11),
        ("0 postings -> NO_HIRING literal, no error",
         zero["formatted"] == NO_HIRING and zero["error"] is None),
        ("null job_postings -> NO_HIRING literal, no error",
         null["formatted"] == NO_HIRING and null["error"] is None),
        ("NO_MATCH -> definitive: NO_HIRING + error_code, no error",
         nomatch["formatted"] == NO_HIRING and nomatch["error_code"] == "NO_MATCH"
         and nomatch["error"] is None),
        ("REQUEST_FAILED -> transport failure: NULL + error",
         failed["formatted"] is None and failed["error"] is not None),
        ("rate-limit body detected", _is_body_rate_limited(_FIXTURE_RATE_LIMIT)),
        ("rate-limit body classifies as transport failure (never a result)",
         ratelim["formatted"] is None and ratelim["error"] is not None),
        ("_format_line singular", _format_line(1, []) == "1 open role · no sales roles"),
    ]
    failed_names = [name for name, passed in checks if not passed]
    for name, passed in checks:
        log(f"{'PASS' if passed else 'FAIL'}  {name}")
    log(f"formatted: {m['formatted']}")
    print(json.dumps({"ok": not failed_names, "failed": failed_names,
                      "formatted": m["formatted"], "sales_titles": sales}))
    return 0 if not failed_names else 1


# ---- CLI ------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="Hiring-signal detection (Prospeo job postings)")
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--domain", help="scan one domain (cache-aware; see --force)")
    mode.add_argument("--missing", action="store_true",
                      help="scan every account_signals domain with no hiring scan yet")
    mode.add_argument("--self-test", action="store_true", dest="self_test",
                      help="offline fixture check (no network, no PROSPEO_API_KEY)")
    ap.add_argument("--company", help="company name to fill on a blank row (--domain only)")
    ap.add_argument("--force", action="store_true", help="re-scan even if fresh")
    ap.add_argument("--no-hubspot", action="store_true", help="skip the HubSpot write-back")
    ap.add_argument("--stale-days", type=int, default=None,
                    help="with --missing: also re-scan scans older than N days")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--workers", type=int, default=3)
    args = ap.parse_args()

    if args.self_test:
        sys.exit(self_test())

    hubspot = False if args.no_hubspot else None
    if args.domain:
        log(f"scanning {args.domain} ...")
        res = detect_and_store(args.domain, company=args.company, force=args.force,
                               hubspot=hubspot)
        log("skipped (fresh)" if res.get("skipped") else f"-> {res.get('hiring_signals') or res.get('hiring_error')}")
        print(json.dumps(res, ensure_ascii=False))
        return

    def progress(done, total, domain, res):
        status = ("error" if (res.get("error_exc") or res.get("hiring_error"))
                  else "skip" if res.get("skipped") else "ok")
        log(f"[{done}/{total}] {domain}: {status}")

    summary = backfill(stale_days=args.stale_days, limit=args.limit, workers=args.workers,
                       force=args.force, hubspot=hubspot, progress=progress)
    log(f"done: {summary}")
    print(json.dumps(summary))


if __name__ == "__main__":
    main()
