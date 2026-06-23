"""Drive Clay's MCP server from the backend (no Claude Code) to source a company
list's buying group, then emit candidate contacts for source_contacts.py.

This replaces the *interactive* Clay step of the clay-sourcing skill. It uses the
Anthropic Messages API MCP connector with a cheap model (Haiku by default) to
operate Clay's async `find-and-enrich-contacts-at-company` + `get-task-context`
tools, and orchestrates the polling cadence here in Python.

  python3 clay_enrich.py --list-id <company_list_id> [--cap 25] --out candidates.json

Output file: a JSON list of {first_name,last_name,title,email,company,domain,
linkedin_url} — exactly the shape source_contacts.py:normalize() accepts. A short
summary JSON is printed to stdout; per-domain progress goes to stderr.

Env: ANTHROPIC_API_KEY, HUBSPOT_ACCESS_TOKEN, plus a connected Clay OAuth session
(see clay_oauth.py). Model override: CLAUDE_SOURCING_MODEL (default claude-haiku-4-5).
"""

import argparse
import concurrent.futures
import json
import os
import re
import sys
import time
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
from hubspot_client import HubSpotClient  # noqa: E402
from clay_mcp import ClayMCP  # noqa: E402
import clay_oauth  # noqa: E402

JOB_TITLE_KEYWORDS = [
    "CRO", "Chief Revenue Officer", "VP of Sales", "Head of Sales", "Director of Sales",
    "SDR Manager", "BDR Manager", "Sales Development Manager",
    "Director of Revenue Operations", "Revenue Operations", "RevOps",
    "Sales Operations", "Sales Ops",
]

# Sent to Clay to keep individual-contributor / junior titles out of the result set.
# (Clay's keyword match is fuzzy, so this is a first pass, not a guarantee.)
JOB_TITLE_EXCLUDE_KEYWORDS = [
    "Intern", "Assistant", "Representative", "Coordinator",
    "Specialist", "Associate", "Account Executive", "Junior",
]

# Deterministic title gate applied to every contact Clay returns — the real
# guarantee that ICs don't slip through fuzzy matching. A contact is kept only
# if its title carries a leadership/ops marker AND no IC/junior marker.
_TARGET_TITLE_RE = re.compile(
    r"\b(chief|cro|vp|vice president|head of|director|manager|lead"
    r"|revenue operations|revops|sales operations|sales ops)\b", re.I)
_IC_TITLE_RE = re.compile(
    r"\b(intern|assistant|representative|coordinator|specialist"
    r"|associate|account executive|junior|entry)\b", re.I)


def is_target_title(title):
    """True only for GTM-leadership / RevOps-Sales-Ops titles (ICs excluded)."""
    t = title or ""
    if _IC_TITLE_RE.search(t):
        return False
    return bool(_TARGET_TITLE_RE.search(t))

POLL_INTERVAL_SECONDS = 15
MAX_POLLS_PER_DOMAIN = 20  # ~5 min ceiling per company
DEFAULT_CONCURRENCY = 8    # companies enriched in parallel (override with --concurrency)


def load_progress(path):
    """Set of company_ids already enriched for this list (empty if no file)."""
    if not path or not Path(path).is_file():
        return set()
    try:
        return set(str(x) for x in json.loads(Path(path).read_text()).get("enriched", []))
    except (ValueError, OSError):
        return set()


def save_progress(path, ids):
    """Persist the set of enriched company_ids so the next run advances past them."""
    if not path:
        return
    Path(path).write_text(json.dumps({"enriched": sorted(ids), "count": len(ids)}, indent=2))


def company_domains(list_id, cap=None, exclude_ids=None):
    """Select the next `cap` companies (with a domain) NOT already enriched.

    Returns (selected, stats); stats reports the cursor position across the list.
    """
    exclude_ids = exclude_ids or set()
    hub = HubSpotClient()
    ids = hub.read_list_records(list_id)
    companies = hub.batch_read_companies(ids, properties=("domain", "name", "website"))
    eligible = []
    for c in companies:
        p = c.get("properties", {})
        domain = (p.get("domain") or "").strip().lower()
        if domain:
            eligible.append({"company_id": c.get("id"), "name": p.get("name"), "domain": domain})
    remaining = [c for c in eligible if str(c["company_id"]) not in exclude_ids]
    selected = remaining[: int(cap)] if cap else remaining
    stats = {
        "total_with_domain": len(eligible),
        "already_enriched": len(eligible) - len(remaining),
        "remaining_before": len(remaining),
    }
    return selected, stats


def _find_args(domain, titles, locations):
    return {
        "companyIdentifier": domain,
        "contactFilters": {
            "job_title_keywords": titles or JOB_TITLE_KEYWORDS,
            "locations": locations or ["United States"],
            "job_title_exclude_keywords": JOB_TITLE_EXCLUDE_KEYWORDS,
        },
        "dataPoints": {"contactDataPoints": [{"type": "Email"}]},
    }


def fire_find_task(mcp, domain, titles=None, locations=None):
    """Kick off Clay's async find-and-enrich for one domain; return its taskId.

    Only *starts* the enrichment (Clay returns a taskId immediately) — it does
    NOT wait. Firing every company's task up front lets Clay enrich them all in
    parallel; the wait happens once, in the polling phase.
    """
    found = mcp.call_tool("find-and-enrich-contacts-at-company",
                          _find_args(domain, titles, locations))
    return found.get("taskId") or found.get("task_id")


def _work_email(contact):
    """The contact's resolved work email, if its Email enrichment has a value."""
    for e in contact.get("enrichments") or []:
        if (e.get("name") or "").lower() == "email" and e.get("value"):
            return str(e["value"]).strip()
    return None


def _email_settled(contact):
    """True once the Email enrichment is no longer in-flight (resolved or failed)."""
    for e in contact.get("enrichments") or []:
        if (e.get("name") or "").lower() == "email":
            if e.get("value"):
                return True
            state = (e.get("state") or "").lower()
            return not any(w in state for w in
                           ("progress", "pending", "queue", "run", "start", "wait"))
    return True  # no email enrichment to wait on


def _to_contact(raw, domain, company_name):
    """Map a Clay task contact into the shape source_contacts.py expects."""
    name = (raw.get("name") or "").strip()
    first, _, last = name.partition(" ")
    return {
        "first_name": first, "last_name": last,
        "title": raw.get("latest_experience_title") or "",
        "email": _work_email(raw),
        "company": raw.get("latest_experience_company") or company_name or domain,
        "domain": raw.get("domain") or domain,
        "linkedin_url": raw.get("url") or "",
    }


def poll_task(mcp, task_id, domain, company_name):
    """One get-task-context poll. Returns (resolved, contacts).

    Clay returns the people immediately; only their Email enrichment is async.
    The task is 'resolved' once every contact's email has settled; we then keep
    the contacts that actually have a work email.
    """
    data = mcp.call_tool("get-task-context", {"taskId": task_id})
    contacts = data.get("contacts") or []
    if not all(_email_settled(c) for c in contacts):
        return False, []
    out = [_to_contact(c, domain, company_name) for c in contacts]
    return True, [c for c in out if c["email"]]


def _csv(value, default):
    """Parse a comma-separated CLI string into a list; fall back to default."""
    if not value:
        return list(default)
    items = [s.strip() for s in value.split(",") if s.strip()]
    return items or list(default)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--list-id", required=True, help="HubSpot company list id")
    ap.add_argument("--cap", type=int, default=25, help="max companies this run (Clay credits)")
    ap.add_argument("--concurrency", type=int, default=DEFAULT_CONCURRENCY,
                    help="companies enriched in parallel (default %(default)s)")
    ap.add_argument("--per-company-cap", type=int, default=0,
                    help="max contacts kept per company (0 = no limit)")
    ap.add_argument("--titles", default="",
                    help="comma-separated target job-title keywords (default: built-in ICP list)")
    ap.add_argument("--locations", default="",
                    help="comma-separated locations (default: United States)")
    ap.add_argument("--progress-file", default="",
                    help="JSON file tracking already-enriched company ids (auto-advance cursor)")
    ap.add_argument("--reset-progress", action="store_true",
                    help="ignore + overwrite existing progress (start the list over)")
    ap.add_argument("--out", required=True, help="path to write the candidate contacts JSON")
    args = ap.parse_args()

    titles = _csv(args.titles, JOB_TITLE_KEYWORDS)
    locations = _csv(args.locations, ["United States"])
    per_company_cap = max(0, args.per_company_cap)

    # Auto-advance cursor: skip companies enriched in prior runs (unless resetting).
    progress = set() if args.reset_progress else load_progress(args.progress_file)
    companies, cur = company_domains(args.list_id, cap=args.cap, exclude_ids=progress)
    sys.stderr.write(
        f"cursor: {cur['already_enriched']}/{cur['total_with_domain']} already enriched, "
        f"{cur['remaining_before']} remaining; this run takes {len(companies)}\n")
    if not companies:
        exhausted = cur["total_with_domain"] > 0 and cur["remaining_before"] == 0
        note = ("all companies in this list have been enriched — reset to start over"
                if exhausted else "no companies with a domain in that list")
        sys.stderr.write(note + "\n")
        Path(args.out).write_text("[]")
        print(json.dumps({"companies": 0, "candidates": 0, "exhausted": exhausted,
                          "note": note, **cur, "out": args.out}))
        return 0

    mcp = ClayMCP()  # one direct MCP session, shared across the thread pool

    # Fan-out / fan-in: fire EVERY company's Clay find task up front, then poll
    # them all together. Clay enriches all companies in parallel server-side, so
    # wall-clock is ~the slowest single company instead of ceil(N/workers) waves.
    workers = max(1, min(args.concurrency, len(companies)))
    cap_note = f", ≤{per_company_cap}/company" if per_company_cap else ""
    sys.stderr.write(f"enriching {len(companies)} companies, {workers} at a time{cap_note}\n")
    sys.stderr.write(f"  titles: {titles}\n  locations: {locations}\n")
    raw_results = []
    total = len(companies)
    done = 0

    def mark_done(domain):
        nonlocal done
        done += 1
        sys.stderr.write(f"[{done}/{total}] {domain} done\n")

    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        # Phase 1 — fire all find tasks concurrently (fast; just returns taskIds).
        fired = {pool.submit(fire_find_task, mcp,
                             co["domain"], titles, locations): co for co in companies}
        pending = []  # [{co, task_id}] still being enriched at Clay
        fired_n = 0
        for fut in concurrent.futures.as_completed(fired):
            co = fired[fut]
            try:
                task_id = fut.result()
            except Exception as exc:
                sys.stderr.write(f"  [{co['domain']}] find error: {exc}\n")
                task_id = None
            fired_n += 1
            sys.stderr.write(f"firing {fired_n}/{total} tasks\n")  # phase-1 progress
            if task_id:
                pending.append({"co": co, "task_id": task_id})
            else:  # no task to wait on — count it done now so progress completes
                sys.stderr.write(f"  [{co['domain']}] no taskId returned; skipping\n")
                mark_done(co["domain"])
        sys.stderr.write(f"fired {len(pending)}/{total} find tasks; polling together…\n")

        # Phase 2 — poll all pending tasks together, round by round, until each
        # resolves or we hit the round ceiling.
        for rnd in range(MAX_POLLS_PER_DOMAIN):
            if not pending:
                break
            polls = {pool.submit(poll_task, mcp, t["task_id"],
                                 t["co"]["domain"], t["co"].get("name")): t for t in pending}
            still = []
            for fut in concurrent.futures.as_completed(polls):
                t = polls[fut]
                try:
                    resolved, contacts = fut.result()
                except Exception as exc:
                    sys.stderr.write(f"  [{t['co']['domain']}] poll error: {exc}\n")
                    resolved, contacts = False, []
                if resolved:
                    raw_results.extend(contacts)
                    sys.stderr.write(f"  [{t['co']['domain']}] resolved: {len(contacts)} contacts\n")
                    mark_done(t["co"]["domain"])
                else:
                    still.append(t)
            pending = still
            if pending and rnd < MAX_POLLS_PER_DOMAIN - 1:
                sys.stderr.write(f"  {len(pending)} still pending (round {rnd + 1}/{MAX_POLLS_PER_DOMAIN})…\n")
                time.sleep(POLL_INTERVAL_SECONDS)

        for t in pending:  # timed out — count done so the run + progress complete
            sys.stderr.write(f"  [{t['co']['domain']}] timed out after {MAX_POLLS_PER_DOMAIN} polls; skipping\n")
            mark_done(t["co"]["domain"])

    # Dedup + title-gate once, after the parallel gather (avoids a shared set
    # across threads). The gate is the deterministic backstop to Clay's fuzzy
    # keyword match — an IC title (e.g. "Sales Development Representative") is
    # dropped here even if Clay returned it.
    candidates, seen_emails = [], set()
    per_domain = {}
    dropped_titles = 0
    dropped_cap = 0
    for raw in raw_results:
        email = (raw.get("email") or "").strip().lower()
        if not email or email in seen_emails:
            continue
        if not is_target_title(raw.get("title", "")):
            dropped_titles += 1
            sys.stderr.write(f"  dropped non-target title: {raw.get('title')!r} "
                             f"<{email}>\n")
            continue
        dom = (raw.get("domain") or "").strip().lower()
        if per_company_cap and per_domain.get(dom, 0) >= per_company_cap:
            dropped_cap += 1
            continue
        per_domain[dom] = per_domain.get(dom, 0) + 1
        seen_emails.add(email)
        candidates.append(raw)
    if dropped_titles:
        sys.stderr.write(f"title gate dropped {dropped_titles} IC/non-target contact(s)\n")
    if dropped_cap:
        sys.stderr.write(f"per-company cap dropped {dropped_cap} extra contact(s)\n")

    # Advance the cursor: mark every company attempted this run as enriched
    # (including 0-yield / timeouts), so future runs move on through the list.
    attempted_ids = {str(co["company_id"]) for co in companies}
    progress = progress | attempted_ids
    save_progress(args.progress_file, progress)
    enriched_total = len(progress)
    remaining_after = max(0, cur["total_with_domain"] - enriched_total)
    sys.stderr.write(
        f"cursor advanced: {enriched_total}/{cur['total_with_domain']} enriched, "
        f"{remaining_after} remaining\n")

    Path(args.out).write_text(json.dumps(candidates, indent=2))
    print(json.dumps({"companies": len(companies), "candidates": len(candidates),
                      "dropped_titles": dropped_titles, "dropped_cap": dropped_cap,
                      "total_with_domain": cur["total_with_domain"],
                      "enriched_total": enriched_total, "remaining_after": remaining_after,
                      "out": args.out}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
