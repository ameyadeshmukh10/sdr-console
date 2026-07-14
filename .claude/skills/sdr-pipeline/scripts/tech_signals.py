"""Technographic detection runner — module + CLI.

Detects the marketing/sales tech a company runs (DNS + static-website
fingerprinting via the vendored `technographics/` package at the repo root) and
stores the result on the `account_signals` row for that domain:

    tech_signals    "CRM: HubSpot | Ad Pixels: Meta Pixel | Martech: Segment | ..."
                    (or the literal "No signals detected"; NULL only if the scan
                    itself failed on BOTH channels)
    tech_detail     JSON: structured detections + per-channel errors + timing
    tech_checked_at ISO-8601 Z; scans are reused for TECH_REFRESH_DAYS (90)
    tech_error      set only when the whole scan failed (fetch AND dns dead)

After a successful scan the formatted line is also PATCHed to the HubSpot
company property `technographic_signals` (best-effort; TECH_HUBSPOT_WRITEBACK=0
to disable, or no matching company by domain -> recorded, never fatal).

Import contract: this module is stdlib-only at import time. dnspython and the
vendored package are imported lazily inside functions, so webui/server/app.py
(which imports this in-process, like generate_batch) still boots with zero pip
deps — `tech_available()` reports why detection is off instead of crashing.

CLI (run_script conventions: progress lines on stderr, JSON summary as the
LAST stdout line):

    python3 tech_signals.py --domain acme.com [--company "Acme"] [--force]
                            [--rendered] [--no-hubspot]
    python3 tech_signals.py --missing [--stale-days N] [--limit N]
                            [--workers N] [--force] [--no-hubspot]
    python3 tech_signals.py --self-test        # offline; no network/dnspython

--rendered uses the vendored Playwright collector (Claude sessions have
Chromium preinstalled); the Railway image deliberately does not, so prod scans
are always DNS + static HTML.

The bucket-mapping / confidence / formatting logic is ported from the
technographic-signals repo's `src/detectors/category_map.py` and
`src/orchestrator.py` (see technographics/VENDORED.md for provenance).
"""

import argparse
import gzip
import http.cookiejar
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request
import zlib
from collections import defaultdict, namedtuple
from concurrent.futures import ThreadPoolExecutor
from html.parser import HTMLParser
from pathlib import Path

import batch_db as db
from hubspot_client import _load_dotenv

_load_dotenv()

PROJECT_ROOT = Path(__file__).resolve().parents[4]  # scripts/ -> sdr-pipeline -> skills -> .claude -> project
TECH_SRC = PROJECT_ROOT / "technographics" / "src"
SIGNATURES_DIR = PROJECT_ROOT / "technographics" / "signatures"
DEFAULT_SELECTION = SIGNATURES_DIR / "selection.marketing_sales.json"
FIXTURES_DIR = PROJECT_ROOT / "technographics" / "tests" / "fixtures"

TECH_PROPERTY = "technographic_signals"
TECH_PROPERTY_LABEL = "Technographic Signals"
NO_SIGNALS = "No signals detected"

_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36")


def log(msg):
    sys.stderr.write(f"[tech] {msg}\n")
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


# ---- availability / vendored-package plumbing ------------------------------
_AVAILABLE = None


def _ensure_path():
    if str(TECH_SRC) not in sys.path:
        sys.path.insert(0, str(TECH_SRC))


def tech_available():
    """(ok, reason). Cached. False when the vendored package or dnspython is
    missing — callers degrade instead of crashing (mongo_store pattern)."""
    global _AVAILABLE
    if _AVAILABLE is not None:
        return _AVAILABLE
    if not TECH_SRC.is_dir():
        _AVAILABLE = (False, f"vendored technographics package missing at {TECH_SRC}")
        return _AVAILABLE
    _ensure_path()
    try:
        import dns.resolver  # noqa: F401  (dnspython — requirements.txt)
    except Exception as exc:  # noqa: BLE001
        _AVAILABLE = (False, f"dnspython not installed ({exc.__class__.__name__}) — pip install -r requirements.txt")
        return _AVAILABLE
    _AVAILABLE = (True, "")
    return _AVAILABLE


# ---- ported from technographic-signals src/ (see VENDORED.md) ---------------
# _clean_domain: src/detectors/engine.py — reduce a website/domain field to a
# bare apex-ish host for DNS + fetching.
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


# Bucket maps: src/detectors/category_map.py. The library's own taxonomy is
# richer; the output string only has 4 buckets. Vendor override wins, then
# category map, else the detection is dropped from the formatted string
# (it stays in tech_detail).
CATEGORY_TO_BUCKET = {
    "crm": "crm",
    "marketing_automation": "martech",
    "cdp": "martech",
    "customer_data_platform": "martech",
    "analytics": "martech",
    "product_analytics": "martech",
    "a_b_testing": "martech",
    "ab_testing": "martech",
    "email": "martech",
    "email_productivity": "martech",
    "form_builders": "martech",
    "surveys": "martech",
    "tag_managers": "ad_pixel",
    "advertising": "ad_pixel",
    "retargeting": "ad_pixel",
    "live_chat": "salestech",
    "customer_support": "salestech",
    "appointment_scheduling": "salestech",
    "sales_engagement": "salestech",
}

VENDOR_BUCKET_OVERRIDE = {
    "facebook_pixel": "ad_pixel",
    "google_ads": "ad_pixel",
    "google_ads_conversion_tracking": "ad_pixel",
    "google_tag_manager": "ad_pixel",
    "linkedin_insight_tag": "ad_pixel",
    "linkedin_ads": "ad_pixel",
    "tiktok_pixel": "ad_pixel",
    "twitter_ads": "ad_pixel",
    "reddit_ads": "ad_pixel",
    "pinterest_conversion_tag": "ad_pixel",
    "microsoft_advertising": "ad_pixel",
    "snap_pixel": "ad_pixel",
    "quora_pixel": "ad_pixel",
    "6sense": "salestech",
    "demandbase": "salestech",
    "zoominfo": "salestech",
    "leadfeeder": "salestech",
    "clearbit_reveal": "salestech",
    "albacross": "salestech",
    "warmly": "salestech",
    "koala": "salestech",
    "gong": "salestech",
    "salesloft": "salestech",
    "apollo": "salestech",
    "outreach": "salestech",
    "qualified": "salestech",
    "drift": "salestech",
    "intercom": "salestech",
    "chili_piper": "salestech",
    "calendly": "salestech",
    "g2": "salestech",
    "factors_ai": "salestech",
    "reo": "salestech",
    "aisdr": "salestech",
}

_CATEGORY_DISPLAY = [
    ("crm", "CRM"),
    ("ad_pixel", "Ad Pixels"),
    ("martech", "Martech"),
    ("salestech", "Salestech"),
]

Hit = namedtuple("Hit", "name category confidence evidence")


def bucket_for(vendor_id, category):
    if vendor_id in VENDOR_BUCKET_OVERRIDE:
        return VENDOR_BUCKET_OVERRIDE[vendor_id]
    return CATEGORY_TO_BUCKET.get(category)


def confidence_bucket(confidence):
    if confidence >= 0.85:
        return "high"
    if confidence >= 0.6:
        return "medium"
    return "low"


# filter_low_confidence / format_signals: src/orchestrator.py — a low hit
# survives only when the same vendor is corroborated high/medium elsewhere;
# the formatted string uses a fixed bucket order, alphabetized vendors, empty
# buckets omitted, and the NO_SIGNALS literal so "we ran" is distinguishable
# from "we never ran".
def filter_low_confidence(hits):
    strong = {h.name for h in hits if h.confidence in ("high", "medium")}
    return [h for h in hits if h.confidence != "low" or h.name in strong]


def format_signals(hits):
    if not hits:
        return NO_SIGNALS
    by_cat = defaultdict(set)
    for h in hits:
        by_cat[h.category].add(h.name)
    parts = []
    for key, label in _CATEGORY_DISPLAY:
        vendors = sorted(by_cat.get(key, set()))
        if vendors:
            parts.append(f"{label}: {', '.join(vendors)}")
    return " | ".join(parts) if parts else NO_SIGNALS


# ---- engine singleton (matchers are read-only after construction) ----------
_ENGINE = None
_ENGINE_LOCK = threading.Lock()


class _Engine:
    def __init__(self):
        _ensure_path()
        from technographics.dns_matcher import DNSMatcher
        from technographics.loader import load_library, load_selection
        from technographics.web_matcher import WebMatcher

        sel_path = Path(os.environ.get("TECH_SELECTION_FILE") or DEFAULT_SELECTION)
        if not sel_path.is_absolute():
            sel_path = PROJECT_ROOT / sel_path
        selection = load_selection(sel_path) if sel_path.is_file() else None
        self.selection_name = sel_path.name if selection is not None else "full-catalogue"
        self.library = load_library(SIGNATURES_DIR, selection=selection)
        self.web_matcher = WebMatcher(self.library.web_signatures, self.library.vendors)
        self.dns_matcher = DNSMatcher(self.library.dns_signatures, self.library.vendors)
        # Union of every signature's CNAME probes (src/detectors/engine.py).
        self.subdomains = sorted({
            sub
            for sig in self.library.dns_signatures.values()
            for sub in sig.subdomains_to_probe
        })


def _engine():
    global _ENGINE
    if _ENGINE is None:
        with _ENGINE_LOCK:
            if _ENGINE is None:
                _ENGINE = _Engine()
    return _ENGINE


# ---- stdlib static fetcher --------------------------------------------------
# Port of src/site_fetcher.py's static mode (requests/bs4 -> urllib/html.parser
# to honor this repo's zero-pip-deps boot rule): same UA/Accept headers, same
# extracted surface (script srcs, cookie NAMES only, lowercased headers, first-
# wins meta map, js_globals empty — those need a JS engine).
class _PageParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.script_srcs = []
        self.meta_tags = {}

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "script":
            src = (a.get("src") or "").strip()
            if src:
                self.script_srcs.append(src)
        elif tag == "meta":
            name = a.get("name") or a.get("property")
            if name:
                key = name.strip().lower()
                if key not in self.meta_tags:
                    self.meta_tags[key] = (a.get("content") or "").strip()


def _decode_body(raw, headers):
    enc = (headers.get("content-encoding") or "").lower()
    try:
        if "gzip" in enc:
            raw = gzip.decompress(raw)
        elif "deflate" in enc:
            try:
                raw = zlib.decompress(raw)
            except zlib.error:
                raw = zlib.decompress(raw, -zlib.MAX_WBITS)
    except Exception:  # noqa: BLE001 — serve the bytes we got
        pass
    return raw


def fetch_static(host, timeout=None):
    """GET https://<host> and extract detection surface. Returns
    (PageData, fetch_error) — a 4xx/5xx still yields PageData (its headers and
    error page are legitimate match candidates); (None, error) only when no
    response was obtained at all."""
    _ensure_path()
    from technographics.web_matcher import PageData

    url = f"https://{host}"
    timeout = timeout or _env_float("TECH_FETCH_TIMEOUT", 10.0)
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    req = urllib.request.Request(url, headers={
        "User-Agent": _UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate",
    })

    resp, last_err = None, None
    for attempt in range(3):
        try:
            resp = opener.open(req, timeout=timeout)
            break
        except urllib.error.HTTPError as exc:
            resp = exc  # a real (4xx/5xx) response — inspect it
            break
        except Exception as exc:  # noqa: BLE001 — URLError, timeout, SSL, DNS
            last_err = f"{type(exc).__name__}: {exc}"
            if attempt < 2:
                time.sleep(1 + attempt)
    if resp is None:
        return None, last_err

    try:
        raw = resp.read(3_000_000)  # cap pathological pages
        status = getattr(resp, "status", None) or getattr(resp, "code", 0) or 0
        final_url = resp.geturl() or url
        headers = {k.lower(): v for k, v in resp.headers.items()}
        charset = resp.headers.get_content_charset() or "utf-8"
    except Exception as exc:  # noqa: BLE001
        return None, f"read failed: {type(exc).__name__}: {exc}"
    finally:
        try:
            resp.close()
        except Exception:  # noqa: BLE001
            pass

    html = _decode_body(raw, headers).decode(charset, "replace")
    parser = _PageParser()
    try:
        parser.feed(html)
        parser.close()
    except Exception:  # noqa: BLE001 — keep whatever parsed before the choke
        pass

    fetch_error = f"HTTP {status}" if status >= 400 else None
    page = PageData(
        final_url=final_url,
        js_globals=[],  # needs a JS engine — see --rendered
        script_srcs=parser.script_srcs,
        cookies={c.name: "" for c in jar},
        headers=headers,
        html=html,
        meta_tags=parser.meta_tags,
    )
    return page, fetch_error


# ---- detection ---------------------------------------------------------------
def detect_domain(domain, rendered=False):
    """Pure detection (no DB): fetch + DNS -> match -> fuse -> bucket/format.
    Returns {formatted, detections, fetch_error, dns_error, error, duration_ms,
    rendered}. `error` is set — and formatted is None — ONLY when both channels
    failed: a network-dead run must never be stored as "No signals detected"."""
    ok, reason = tech_available()
    if not ok:
        raise RuntimeError(f"technographic detection unavailable: {reason}")
    eng = _engine()

    host = _clean_domain(domain)
    if not host:
        return {"formatted": None, "detections": [], "fetch_error": None, "dns_error": None,
                "error": f"invalid domain: {domain!r}", "duration_ms": 0, "rendered": False}

    t0 = time.monotonic()
    page, fetch_error = None, None
    if rendered:
        from technographics.web_collector import collect_web  # lazy: playwright
        page = collect_web(host)
        if page.error:
            fetch_error = page.error
            if not (page.html or page.script_srcs):
                page = None
    else:
        page, fetch_error = fetch_static(host)
    web_dets = eng.web_matcher.match(page) if page is not None else []

    dns_dets, dns_error, dns_records = [], None, 0
    try:
        from technographics.dns_collector import collect_dns  # lazy: dnspython
        records = collect_dns(host, subdomains=eng.subdomains,
                              timeout=_env_float("TECH_DNS_TIMEOUT", 3.0))
        dns_error = records.error
        # The collector swallows per-lookup failures, so blocked egress (or a
        # dead domain) looks like "no records", not an error — count them.
        dns_records = (len(records.a) + len(records.mx) + len(records.txt)
                       + len(records.ns) + len(records.soa) + len(records.cname))
        dns_dets = eng.dns_matcher.match(records)
    except Exception as exc:  # noqa: BLE001 — DNS is best-effort
        dns_error = f"{type(exc).__name__}: {exc}"

    from technographics.fusion import fuse
    fused = fuse(dns_dets, web_dets)

    detections, hits = [], []
    for det in fused:
        bucket = bucket_for(det.vendor_id, det.category)
        detections.append({
            "vendor_id": det.vendor_id, "vendor_name": det.vendor_name,
            "category": det.category, "bucket": bucket, "source": det.source,
            "confidence": round(det.confidence, 4), "tier_signal": det.tier_signal,
            "evidence": list(det.evidence[:3]),
        })
        if bucket:
            hits.append(Hit(det.vendor_name, bucket, confidence_bucket(det.confidence),
                            list(det.evidence[:3])))

    formatted = format_signals(filter_low_confidence(hits))
    error = None
    # A live domain always answers SOMETHING (NS/SOA at minimum) — no response
    # on either channel means the scan failed (dead domain or blocked egress),
    # and must not be stored as a confident "No signals detected".
    if page is None and dns_error is None and dns_records == 0:
        dns_error = "no records (resolver unreachable or domain does not resolve)"
    if page is None and (dns_error is not None and not dns_dets):
        error = f"fetch: {fetch_error or 'failed'}; dns: {dns_error}"
        formatted = None

    return {"formatted": formatted, "detections": detections, "fetch_error": fetch_error,
            "dns_error": dns_error, "dns_records": dns_records, "error": error,
            "duration_ms": int((time.monotonic() - t0) * 1000), "rendered": bool(rendered)}


# ---- store + orchestrate -----------------------------------------------------
_SCHEMA_LOCK = threading.Lock()
_SCHEMA_READY = False


def _db():
    global _SCHEMA_READY
    conn = db.connect()
    if not _SCHEMA_READY:
        with _SCHEMA_LOCK:
            if not _SCHEMA_READY:
                db.init_schema(conn)  # guarantees the tech_* columns exist
                _SCHEMA_READY = True
    return conn


def detect_and_store(domain, company=None, force=False, hubspot=None, rendered=False):
    """Cache-aware detect for one domain: skip when the last SUCCESSFUL scan is
    younger than TECH_REFRESH_DAYS (failed scans always retry), else scan,
    upsert the account_signals row, and best-effort write the formatted line to
    HubSpot. hubspot=None follows TECH_HUBSPOT_WRITEBACK; True/False overrides."""
    ok, reason = tech_available()
    if not ok:
        raise RuntimeError(f"technographic detection unavailable: {reason}")
    host = _clean_domain(domain)
    if not host:
        raise ValueError(f"invalid domain: {domain!r}")

    conn = _db()
    try:
        row = db.get_signal(conn, host)
        refresh_days = _env_float("TECH_REFRESH_DAYS", 90)
        if (row and not force and row.get("tech_signals") is not None
                and db.tech_fresh(row, days=refresh_days)):
            return {"domain": host, "skipped": True,
                    "tech_signals": row.get("tech_signals"),
                    "tech_error": row.get("tech_error"),
                    "tech_checked_at": row.get("tech_checked_at"),
                    "detections": _detection_count(row.get("tech_detail")),
                    "hubspot": None}
    finally:
        conn.close()

    res = detect_domain(host, rendered=rendered)

    # HubSpot write-back BEFORE the upsert (no DB connection held across the
    # HTTP call) so its outcome can be persisted into tech_detail and shown in
    # the Signals drawer on a later page load — not just returned to the caller.
    hs = None
    if res["formatted"] is not None:
        enabled = _flag("TECH_HUBSPOT_WRITEBACK", True) if hubspot is None else bool(hubspot)
        hs = hubspot_writeback(host, res["formatted"]) if enabled else {"ok": False, "reason": "disabled"}

    detail = {
        "detections": res["detections"],
        "fetch_error": res["fetch_error"],
        "dns_error": res["dns_error"],
        "dns_records": res["dns_records"],
        "selection": _engine().selection_name,
        "duration_ms": res["duration_ms"],
        "rendered": res["rendered"],
        "hubspot": hs,
    }
    conn = _db()
    try:
        db.upsert_tech_signals(conn, host, res["formatted"],
                               tech_detail=json.dumps(detail, ensure_ascii=False),
                               tech_error=res["error"], company_name=company)
        stored = db.get_signal(conn, host) or {}
    finally:
        conn.close()

    return {"domain": host, "skipped": False, "tech_signals": res["formatted"],
            "tech_error": res["error"], "tech_checked_at": stored.get("tech_checked_at"),
            "detections": len(res["detections"]), "hubspot": hs}


def _detection_count(tech_detail):
    try:
        return len(json.loads(tech_detail or "{}").get("detections") or [])
    except (ValueError, TypeError):
        return 0


# ---- HubSpot write-back (best-effort; never raises) --------------------------
_HS_LOCK = threading.Lock()
_HS_CLIENT = None
_HS_PROPERTY_READY = False


def hubspot_writeback(domain, formatted):
    global _HS_CLIENT, _HS_PROPERTY_READY
    try:
        if not os.environ.get("HUBSPOT_ACCESS_TOKEN"):
            return {"ok": False, "reason": "no_token"}
        import hubspot_client as hc
        with _HS_LOCK:
            if _HS_CLIENT is None:
                _HS_CLIENT = hc.HubSpotClient()
            client = _HS_CLIENT
            if not _HS_PROPERTY_READY:
                client.ensure_company_property(TECH_PROPERTY, TECH_PROPERTY_LABEL)
                _HS_PROPERTY_READY = True
        company_id = client.find_company_id_by_domain(domain)
        if not company_id:
            return {"ok": False, "reason": "no_company"}
        client.update_company(company_id, {TECH_PROPERTY: formatted})
        return {"ok": True, "company_id": company_id}
    except Exception as exc:  # noqa: BLE001 — write-back must never fail a scan
        log(f"hubspot writeback failed for {domain}: {exc}")
        return {"ok": False, "error": str(exc)[:300]}


# ---- bulk ---------------------------------------------------------------------
def backfill(domains=None, stale_days=None, limit=None, workers=4, force=False,
             hubspot=None, progress=None):
    """Scan many domains (ThreadPoolExecutor). domains=None pulls the never-
    scanned (plus stale, when stale_days is set) from account_signals. Returns
    {total, detected, skipped, errors, hubspot_ok, hubspot_missing}."""
    ok, reason = tech_available()
    if not ok:
        raise RuntimeError(f"technographic detection unavailable: {reason}")

    if domains is None:
        conn = _db()
        try:
            rows = db.domains_missing_tech(conn, stale_days=stale_days, limit=limit)
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
    _engine()  # build the library once before the pool fans out

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
            if res.get("error_exc") or res.get("tech_error"):
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
def self_test():
    """Match the vendored fixtures (no network, no dnspython, no DB): the web
    fixture must yield Intercom + Segment, the DNS fixture Intercom + HubSpot,
    and the formatted line must bucket them correctly. Exit code 0/1."""
    _ensure_path()
    from technographics.dns_matcher import DNSRecords
    from technographics.fusion import fuse
    from technographics.web_matcher import PageData

    eng = _engine()
    page = PageData(**json.loads((FIXTURES_DIR / "sample_page_data.json").read_text()))
    records = DNSRecords(**json.loads((FIXTURES_DIR / "sample_dns_records.json").read_text()))

    web = eng.web_matcher.match(page)
    dns = eng.dns_matcher.match(records)
    fused = fuse(dns, web)
    hits = []
    for det in fused:
        bucket = bucket_for(det.vendor_id, det.category)
        if bucket:
            hits.append(Hit(det.vendor_name, bucket, confidence_bucket(det.confidence), []))
    formatted = format_signals(filter_low_confidence(hits))

    web_ids = {d.vendor_id for d in web}
    dns_ids = {d.vendor_id for d in dns}
    checks = [
        ("web detects intercom", "intercom" in web_ids),
        ("web detects segment", "segment" in web_ids),
        ("dns detects intercom", "intercom" in dns_ids),
        ("dns detects hubspot", "hubspot" in dns_ids),
        ("HubSpot lands in CRM bucket", "CRM:" in formatted and "HubSpot" in formatted),
        ("Segment lands in Martech bucket", "Martech:" in formatted and "Segment" in formatted),
        ("Intercom lands in Salestech bucket", "Salestech:" in formatted and "Intercom" in formatted),
    ]
    failed = [name for name, passed in checks if not passed]
    for name, passed in checks:
        log(f"{'PASS' if passed else 'FAIL'}  {name}")
    log(f"formatted: {formatted}")
    print(json.dumps({"ok": not failed, "failed": failed, "formatted": formatted,
                      "web_detections": len(web), "dns_detections": len(dns),
                      "vendors_loaded": len(eng.library.vendors)}))
    return 0 if not failed else 1


# ---- CLI ------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="Technographic detection (DNS + static web)")
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--domain", help="scan one domain (cache-aware; see --force)")
    mode.add_argument("--missing", action="store_true",
                      help="scan every account_signals domain with no tech scan yet")
    mode.add_argument("--self-test", action="store_true", dest="self_test",
                      help="offline fixture check (no network, no dnspython)")
    ap.add_argument("--company", help="company name to fill on a blank row (--domain only)")
    ap.add_argument("--force", action="store_true", help="re-scan even if fresh")
    ap.add_argument("--rendered", action="store_true",
                    help="use the Playwright collector (Claude sessions only; never prod)")
    ap.add_argument("--no-hubspot", action="store_true", help="skip the HubSpot write-back")
    ap.add_argument("--stale-days", type=int, default=None,
                    help="with --missing: also re-scan scans older than N days")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--workers", type=int, default=4)
    args = ap.parse_args()

    if args.self_test:
        sys.exit(self_test())

    hubspot = False if args.no_hubspot else None
    if args.domain:
        log(f"scanning {args.domain} ...")
        res = detect_and_store(args.domain, company=args.company, force=args.force,
                               hubspot=hubspot, rendered=args.rendered)
        log("skipped (fresh)" if res.get("skipped") else f"-> {res.get('tech_signals') or res.get('tech_error')}")
        print(json.dumps(res, ensure_ascii=False))
        return

    def progress(done, total, domain, res):
        status = ("error" if (res.get("error_exc") or res.get("tech_error"))
                  else "skip" if res.get("skipped") else "ok")
        log(f"[{done}/{total}] {domain}: {status}")

    summary = backfill(stale_days=args.stale_days, limit=args.limit, workers=args.workers,
                       force=args.force, hubspot=hubspot, progress=progress)
    log(f"done: {summary}")
    print(json.dumps(summary))


if __name__ == "__main__":
    main()
