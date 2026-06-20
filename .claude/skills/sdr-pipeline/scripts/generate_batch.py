"""Generate SDR outbound copy for one batch via the Anthropic API.

Replicates what the `sdr-batch-runner` Claude Code sub-agent does, but as a
direct API call (stdlib urllib) so it can be triggered from the web UI. Per
contact: research a recent signal (server-side web_search), write the 4-touch
email + LinkedIn copy as strict JSON, validate against the SAME linter the
`ingest` step uses, retrying with the lint errors fed back.

Two entry points:
  - CLI:  python3 generate_batch.py <batch_id>     (generates, then runs ingest)
  - lib:  generate_batch(batch_id, progress_cb, cancel_event)  (used by the webui
          job worker; the caller runs `sdr_batches.py ingest` afterwards)

DB writes go through the existing `ingest`, so the read-only web API sees them.
"""

import json
import os
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent                      # sdr-pipeline/scripts
SKILLS = HERE.parents[1]                                     # .claude/skills
sys.path.insert(0, str(HERE))                               # batch_db
sys.path.insert(0, str(SKILLS / "ai-sdr" / "scripts"))      # anthropic_client, lint_sequence

import batch_db as db                                        # noqa: E402
import lint_sequence as L                                    # noqa: E402
from anthropic_client import (                               # noqa: E402
    AnthropicClient, extract_json, parse_message, AnthropicError, AnthropicJSONError,
)

KNOWLEDGE_DIR = SKILLS / "ai-sdr" / "knowledge"
EXAMPLE = SKILLS / "ai-sdr" / "examples" / "icp-email-sequence.md"
SDR_BATCHES = HERE / "sdr_batches.py"
MAX_ATTEMPTS = 3
MAX_WORKERS = 4

PERSONA_FRAMING = {
    "sales-leadership": "Frame the pain as coverage/quota: more pipeline per rep without hiring. "
                        "Gives: signal play, pipeline gap analysis, peer benchmark, pilot playbook (breakup).",
    "revops": "Frame the pain as signal-to-action latency, data hygiene, measurable lift. "
              "Gives: pipeline gap analysis, signal play, outbound teardown, peer benchmark.",
    "partnerships": "Frame the pain as co-sell / partner-sourced pipeline coverage at scale. "
                    "Gives: signal play (partner ecosystem), co-sell pilot playbook, pipeline gap analysis, 3 personalized drafts.",
    "sdr-bdr": "Frame the pain as follow-up volume, ramp time, response speed. "
               "Gives: 3 personalized drafts, signal play, outbound teardown, peer benchmark.",
}

TASK_TEMPLATE = """\
# Today's date
Today is {today}. The current year is {year}. Judge recency strictly against this date.

# Step 1 — find a RECENT signal (be efficient, do not waste searches)
Search the web for ONE recent signal about the contact's company, in this PRIORITY ORDER:
1. A funding round the company RAISED in {year} (the more recent the better, within the last month
   or two is ideal).
2. A key LEADERSHIP HIRE (CRO, CEO, VP / Head / Director of Sales or GTM) announced in {year}.
3. A PRODUCT LAUNCH, PARTNERSHIP, or NEW MARKET ENTRY in {year}.

RECENCY IS MANDATORY. A signal only qualifies if it happened in {year}, and the closer to today the
better. BEFORE you use any item, verify its date. If the most recent thing you can find is from
{prev_year} or earlier (e.g. 2024, 2025), it does NOT qualify: ignore it and do NOT present old news
as recent. Aim your queries at the current year (include "{year}" and recent-time terms).

Use AT MOST 3 web searches. If you do not find a qualifying {year} signal, STOP searching and use the
fallback below, do not keep burning searches.

# Step 2 (fallback) — when there is NO recent {year} signal
Do NOT invent or imply recent news, and do NOT use a generic pain hypothesis. Instead research the
company's PRODUCT / OFFERING, their ICP (who they sell to), and their GO-TO-MARKET motion, and
personalize the whole sequence around how our AI SDR fits THAT specific business. The opener must
reference something concrete and true about what they build or who they sell to (not invented news).

# The `signal` field must be auditable
- If you found a recent signal: set `signal` to it AND include its month/date, e.g.
  "Raised a $20M Series B in May {year}".
- If you used the fallback: prefix with "no recent signal - " and describe the anchor, e.g.
  "no recent signal - anchored on Acme's PLG motion selling to mid-market RevOps teams".

# Step 3 — write the sequence
Write a 4-touch cold EMAIL sequence plus 3 LinkedIn touches, following every rule in the knowledge
above: 70-110 words per email (aim 80-95); three short paragraphs separated by a blank line; no em or
en dashes; NO sign-off or trailing name; step 1 opens on the signal (or the company-specific anchor
in the fallback case); each CTA leads with a deliverable give AND asks for a meeting; at least one
concrete metric across the sequence; step 4 is a breakup; never put pricing in a cold email.
"""

OUTPUT_SCHEMA = """\
# Output
Return ONLY a single JSON object, no prose, no markdown code fences, matching EXACTLY this shape
(use \\n\\n for paragraph breaks inside bodies):

{
  "signal": "<the recent signal with its month/date, or 'no recent signal - <anchor>'>",
  "email": {
    "subject1": "...", "body1": "...",
    "subject2": "...", "body2": "...",
    "subject3": "...", "body3": "...",
    "subject4": "...", "body4": "..."
  },
  "linkedin": {
    "li_connect": "<=280 char connection note referencing the signal/anchor, no pitch",
    "li_msg1": "value-first give after connection",
    "li_msg2": "soft follow-up nudge + give"
  }
}
"""


def _today():
    now = datetime.now()
    return {"today": now.strftime("%B %d, %Y"), "year": now.year, "prev_year": now.year - 1}


# Write-only task: the company signal is provided (from the cache), so no web search.
WRITE_TASK = """\
# Your task
You are GIVEN the company's current signal in the contact block below. Do NOT search the web. Write the
sequence using ONLY the provided signal.

If the signal begins with "no recent signal -", treat the text after it as the company's product / ICP /
GTM anchor and personalize around that (do not imply or invent recent news).

# Write the sequence
Write a 4-touch cold EMAIL sequence plus 3 LinkedIn touches, following every rule in the knowledge above:
70-110 words per email (aim 80-95); three short paragraphs separated by a blank line; no em or en dashes;
NO sign-off or trailing name; step 1 opens on the signal (or the anchor); each CTA leads with a deliverable
give AND asks for a meeting; at least one concrete metric across the sequence; step 4 is a breakup; never
put pricing in a cold email.
"""

# Research-only task (UI force-refresh): just find/refresh the signal, return only the signal JSON.
RESEARCH_ONLY_TASK = """\
Today is {today}. The current year is {year}. Judge recency strictly against this date.

Find ONE recent signal for the company, in priority order: (1) a {year} funding round the company raised,
(2) a {year} key leadership hire, (3) a {year} product launch / partnership / new market entry. A signal
qualifies ONLY if it happened in {year}; verify the date; ignore anything from {prev_year} or earlier. Use
at most 3 web searches. If none qualifies, set has_recent_signal=false and put a one-line product/ICP/GTM
anchor in `signal`, prefixed with "no recent signal - ".

Return ONLY this JSON, no prose:
{{"signal": "<signal with its month/date, or 'no recent signal - <anchor>'>", "has_recent_signal": true|false}}
"""


def load_knowledge():
    parts = []
    for fname in ("offer.md", "cta-offers.md", "icp-email.md"):
        parts.append((KNOWLEDGE_DIR / fname).read_text())
    if EXAMPLE.is_file():
        parts.append("# Reference sequence (emulate the shape, never copy specifics)\n\n"
                     + EXAMPLE.read_text())
    return "\n\n---\n\n".join(parts)


def build_system(knowledge, mode="research"):
    task = WRITE_TASK if mode == "write" else TASK_TEMPLATE.format(**_today())
    return (
        "You are an expert B2B SDR copywriter for EverWorker's SDR AI Worker. Ground every claim in "
        "the knowledge base below; never invent product claims, numbers, or proof not in it.\n\n"
        + knowledge + "\n\n---\n\n" + task + "\n\n" + OUTPUT_SCHEMA
    )


def build_user(contact, cached_signal=None, prior_issues=None):
    persona = contact.get("persona", "sales-leadership")
    framing = PERSONA_FRAMING.get(persona, PERSONA_FRAMING["sales-leadership"])
    base = (
        f"Contact:\n"
        f"- name: {contact.get('first_name','')} {contact.get('last_name','')}\n"
        f"- title: {contact.get('title','')}\n"
        f"- company: {contact.get('company','')}\n"
        f"- persona: {persona}\n"
        f"- linkedin: {contact.get('linkedin_url','')}\n\n"
        f"Persona framing: {framing}\n\n"
    )
    if cached_signal:
        base += (f"Company signal (use this, do NOT search the web): {cached_signal}\n\n"
                 f"Use the contact's first name in the copy. Write the sequence. Return only the JSON object.")
    else:
        base += (f"Use the contact's first name in the copy. Research "
                 f"{contact.get('company','the company')} now and write the sequence. "
                 f"Return only the JSON object.")
    if prior_issues:
        base += ("\n\nYour previous attempt FAILED these checks:\n- "
                 + "\n- ".join(prior_issues)
                 + "\nFix ALL of them. Return only the corrected JSON object.")
    return base


def lint_email(email):
    """Identical checks to enroll.lint_email_assets (the ingest source of truth)."""
    issues, steps = [], []
    for i in range(1, 5):
        subj, body = email.get(f"subject{i}", ""), email.get(f"body{i}", "")
        if not subj or not body:
            issues.append(f"missing subject{i}/body{i}")
        steps.append({"n": i, "subject": subj, "body": body})
    if issues:
        return issues
    full = " ".join(s["body"] for s in steps)
    if not L.METRIC.search(full):
        issues.append("no concrete metric in the sequence")
    for idx, step in enumerate(steps):
        _, step_issues = L.lint_email(step, is_last=(idx == len(steps) - 1), is_first=(idx == 0))
        issues += [f"step{step['n']}: {it}" for it in step_issues]
    return issues


def _atomic_write(contact_id, asset):
    db.GEN_DIR.mkdir(parents=True, exist_ok=True)
    path = db.GEN_DIR / f"{contact_id}.json"
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(asset, indent=2, ensure_ascii=False))
    os.replace(tmp, path)


def generate_contact(contact, knowledge, client, write=True, cached_signal=None):
    """Generate + validate one contact. Returns a result dict.

    cached_signal (when provided): use it as the company signal and DO NOT search
    the web — much cheaper. Otherwise research the signal with web search.
    write=False skips the file write (used by --contact-test); the asset is
    still returned under result["asset"].
    """
    cid, persona = contact["contact_id"], contact.get("persona", "sales-leadership")
    mode = "write" if cached_signal else "research"
    use_search = cached_signal is None
    issues, last_asset, web_searches = ["no output"], None, 0
    cache_read = cache_write = 0

    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            res = client.complete(
                build_system(knowledge, mode=mode),
                build_user(contact, cached_signal=cached_signal,
                           prior_issues=None if attempt == 1 else issues),
                use_web_search=use_search, max_web_searches=3, max_tokens=4096,
            )
            web_searches = res.get("web_search_count", 0)
            u = res.get("usage", {})
            cache_read += u.get("cache_read_input_tokens", 0) or 0
            cache_write += u.get("cache_creation_input_tokens", 0) or 0
        except AnthropicError as e:
            issues = [f"api error: {e}"]
            continue
        try:
            data = extract_json(res["text"])
        except AnthropicJSONError:
            issues = ["model did not return valid JSON"]
            continue

        last_asset = {
            "contact_id": cid,
            "persona": persona,
            # trust the cached signal verbatim; otherwise take the model's
            "signal": (cached_signal or data.get("signal") or "").strip(),
            "email": data.get("email", {}) or {},
            "linkedin": data.get("linkedin", {}) or {},
        }
        issues = lint_email(last_asset["email"])
        if not issues:
            if write:
                _atomic_write(cid, last_asset)
            return {"status": "linted", "signal": last_asset["signal"], "asset": last_asset,
                    "web_searches": web_searches, "attempts": attempt, "issues": [],
                    "cache_read": cache_read, "cache_write": cache_write}

    # all attempts failed: write the last asset (if any) so ingest records it as failed with reason
    if write and last_asset is not None:
        _atomic_write(cid, last_asset)
    return {"status": "failed", "asset": last_asset,
            "signal": (last_asset or {}).get("signal", ""),
            "web_searches": web_searches, "attempts": MAX_ATTEMPTS, "issues": issues,
            "cache_read": cache_read, "cache_write": cache_write}


# Per-domain locks so a company is researched once even under the worker pool.
_DOMAIN_LOCKS = {}
_DOMAIN_LOCKS_GUARD = threading.Lock()


def _domain_lock(domain):
    with _DOMAIN_LOCKS_GUARD:
        lk = _DOMAIN_LOCKS.get(domain)
        if lk is None:
            lk = threading.Lock()
            _DOMAIN_LOCKS[domain] = lk
        return lk


def _fresh_cached_signal(domain):
    """Return a fresh (<90d) cached signal string for the domain, or None."""
    if not domain:
        return None
    conn = db.connect()
    try:
        row = db.get_signal(conn, domain)
    finally:
        conn.close()
    return row["signal"] if db.signal_fresh(row) else None


def generate_one(contact, knowledge, client, write=True):
    """Cache-aware single-contact generation.

    Reuse a fresh per-company signal (write-only, no web search). On a cache miss
    research once per company under a per-domain lock, then store the signal so the
    rest of that company (and future runs within 90 days) skip the search.
    """
    domain = (contact.get("domain") or db.email_domain(contact.get("email")))

    cached = _fresh_cached_signal(domain)
    if cached:
        r = generate_contact(contact, knowledge, client, write=write, cached_signal=cached)
        r["used_cache"] = True
        return r

    lock = _domain_lock(domain) if domain else None
    if lock:
        lock.acquire()
    try:
        if domain:  # another thread may have cached it while we waited
            cached = _fresh_cached_signal(domain)
            if cached:
                r = generate_contact(contact, knowledge, client, write=write, cached_signal=cached)
                r["used_cache"] = True
                return r
        r = generate_contact(contact, knowledge, client, write=write)  # combined: search + write
        r["used_cache"] = False
        sig = (r.get("signal") or "").strip()
        if domain and sig:
            has_recent = not sig.lower().startswith("no recent signal")
            conn = db.connect()
            try:
                db.upsert_signal(conn, domain, contact.get("company", ""), sig, has_recent, client.model)
            finally:
                conn.close()
        return r
    finally:
        if lock:
            lock.release()


def research_signal(domain, company, client=None):
    """Force a fresh signal search for one company and update the cache (UI refresh)."""
    client = client or AnthropicClient()
    system = ("You research a B2B company's single most recent GTM signal.\n\n"
              + RESEARCH_ONLY_TASK.format(**_today()))
    user = f"Company: {company or domain} (domain {domain}). Find the recent signal and return only the JSON."
    res = client.complete(system, user, use_web_search=True, max_web_searches=3, max_tokens=1024)
    data = extract_json(res["text"])
    signal = (data.get("signal") or "").strip()
    has_recent = bool(data.get("has_recent_signal")) and not signal.lower().startswith("no recent signal")
    conn = db.connect()
    try:
        db.upsert_signal(conn, domain, company or "", signal, has_recent, client.model)
    finally:
        conn.close()
    return {"domain": domain, "company_name": company, "signal": signal,
            "has_recent": 1 if has_recent else 0, "web_searches": res.get("web_search_count", 0)}


# ----------------------------------------------------------------------------
# Message Batches API path: build requests, then process results. Same prompts
# and cache logic as the real-time path, just packaged for async batch submit.
# ----------------------------------------------------------------------------
def build_request_params(contact, knowledge, client, cached_signal=None):
    """The Messages `params` for one contact (write-only if a cached signal is
    given, else a combined research+write request with web search). 1h cache."""
    mode = "write" if cached_signal else "research"
    return client.build_body(
        build_system(knowledge, mode=mode),
        build_user(contact, cached_signal=cached_signal),
        use_web_search=cached_signal is None, max_web_searches=3,
        max_tokens=4096, cache_ttl="1h",
    )


def prepare_batch_requests(contacts, knowledge, client=None):
    """Build {custom_id, params} for each contact + a manifest for result handling.
    Already-cached companies become cheap write-only requests (no web search)."""
    client = client or AnthropicClient()
    requests, manifest = [], {}
    for c in contacts:
        cid = str(c["contact_id"])
        domain = c.get("domain") or db.email_domain(c.get("email"))
        cached = _fresh_cached_signal(domain)
        requests.append({"custom_id": cid,
                         "params": build_request_params(c, knowledge, client, cached_signal=cached)})
        manifest[cid] = {"contact": c, "domain": domain,
                         "was_combined": cached is None, "cached_signal": cached}
    return requests, manifest


def process_batch_result(custom_id, result, manifest):
    """Handle one batch result. On success: lint + write the file + cache the
    researched signal. Returns status linted | retry | error (retry = caller
    should re-generate this contact synchronously)."""
    entry = manifest.get(str(custom_id))
    if not entry:
        return {"status": "error", "issues": ["unknown custom_id"]}
    contact = entry["contact"]
    cid, persona = contact["contact_id"], contact.get("persona", "sales-leadership")
    domain, cached_signal = entry["domain"], entry.get("cached_signal")

    if result.get("type") != "succeeded":
        return {"status": "retry", "issues": [f"batch result: {result.get('type')}"], "contact": contact}

    parsed = parse_message(result.get("message", {}))
    try:
        data = extract_json(parsed["text"])
    except AnthropicJSONError:
        return {"status": "retry", "issues": ["invalid JSON from batch"], "contact": contact}

    asset = {
        "contact_id": cid, "persona": persona,
        "signal": (cached_signal or data.get("signal") or "").strip(),
        "email": data.get("email", {}) or {}, "linkedin": data.get("linkedin", {}) or {},
    }
    # cache the researched signal (combined requests) even if the copy fails lint
    if entry["was_combined"] and domain and asset["signal"]:
        has_recent = not asset["signal"].lower().startswith("no recent signal")
        conn = db.connect()
        try:
            db.upsert_signal(conn, domain, contact.get("company", ""), asset["signal"], has_recent)
        finally:
            conn.close()

    issues = lint_email(asset["email"])
    if issues:
        return {"status": "retry", "issues": issues, "contact": contact, "signal": asset["signal"]}
    _atomic_write(cid, asset)
    return {"status": "linted", "signal": asset["signal"],
            "web_searches": parsed["web_search_count"], "usage": parsed["usage"],
            "used_cache": cached_signal is not None}


def generate_batch(batch_id, progress_cb=None, cancel_event=None, max_workers=MAX_WORKERS):
    """Generate all contacts in a batch concurrently. Does NOT touch the DB —
    the caller runs `sdr_batches.py ingest <batch_id>` to record results.

    progress_cb(contact_id, state, **extra) is called as each contact moves:
      state in {"researching","linted","failed","cancelled","error"}.
    """
    conn = db.connect()
    contacts = db.get_batch(conn, batch_id)
    conn.close()
    knowledge = load_knowledge()
    client = AnthropicClient()

    if progress_cb:
        for c in contacts:
            progress_cb(c["contact_id"], "queued",
                        name=f"{c.get('first_name','')} {c.get('last_name','')}".strip(),
                        company=c.get("company", ""), persona=c.get("persona", ""))

    results = {}

    def _run_one(contact):
        cid = contact["contact_id"]
        if cancel_event is not None and cancel_event.is_set():
            if progress_cb:
                progress_cb(cid, "cancelled")
            return {"status": "cancelled", "issues": ["cancelled"]}
        if progress_cb:
            progress_cb(cid, "researching")
        try:
            r = generate_one(contact, knowledge, client)
        except Exception as e:  # noqa: BLE001 - never let one contact kill the batch
            r = {"status": "error", "issues": [str(e)[:300]], "web_searches": 0,
                 "attempts": 0, "signal": "", "used_cache": False}
        if progress_cb:
            progress_cb(cid, r["status"], web_searches=r.get("web_searches", 0),
                        signal=r.get("signal", ""), issues=r.get("issues", []),
                        cache_read=r.get("cache_read", 0), cache_write=r.get("cache_write", 0),
                        used_cache=r.get("used_cache", False))
        return r

    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futures = {ex.submit(_run_one, c): c["contact_id"] for c in contacts}
        for fut in as_completed(futures):
            results[futures[fut]] = fut.result()

    linted = sum(1 for r in results.values() if r["status"] == "linted")
    failed = len(results) - linted
    return {"batch_id": batch_id, "total": len(contacts), "linted": linted,
            "failed": failed, "results": results}


def contact_test():
    """Generate ONE synthetic contact and print the result. No DB/Bison writes.

    usage: generate_batch.py --contact-test [Company] [Title] [persona] [FirstName]
    """
    args = sys.argv[2:]
    contact = {
        "contact_id": "TEST",
        "first_name": args[3] if len(args) > 3 else "Jordan",
        "last_name": "Test",
        "title": args[1] if len(args) > 1 else "VP of Sales",
        "company": args[0] if len(args) > 0 else "Ramp",
        "persona": args[2] if len(args) > 2 else "sales-leadership",
        "linkedin_url": "",
    }
    print(f"generating test copy for {contact['first_name']} @ {contact['company']} "
          f"({contact['persona']})...\n")
    client = AnthropicClient()
    r = generate_contact(contact, load_knowledge(), client, write=False)
    print(f"status: {r['status']}  web_searches: {r['web_searches']}  attempts: {r['attempts']}  "
          f"cache_read: {r.get('cache_read', 0)}  cache_write: {r.get('cache_write', 0)}")
    print(f"signal: {r['signal']}\n")
    if r.get("asset"):
        print(json.dumps(r["asset"], indent=2, ensure_ascii=False))
    if r["issues"]:
        print("\nlint issues:", r["issues"])
    return 0 if r["status"] == "linted" else 1


def main():
    if len(sys.argv) >= 2 and sys.argv[1] == "--contact-test":
        return contact_test()
    if len(sys.argv) < 2:
        print("usage: generate_batch.py <batch_id>  |  generate_batch.py --contact-test [company] [title] [persona]")
        return 2
    batch_id = int(sys.argv[1])

    def cb(cid, state, **extra):
        if state in ("researching", "queued"):
            return
        tag = "OK " if state == "linted" else state
        cache = extra.get("cache_read", 0)
        cinfo = f" cache:{cache/1000:.1f}k read" if cache else ""
        print(f"  [{tag}] {cid} {('('+str(extra.get('web_searches',0))+' searches)') if state=='linted' else ''}{cinfo}"
              + (f" -- {extra.get('issues')}" if extra.get("issues") else ""))

    summary = generate_batch(batch_id, progress_cb=cb)
    print(f"batch {batch_id}: {summary['linted']} linted, {summary['failed']} failed")
    # record results through the canonical ingest path
    proc = subprocess.run([sys.executable, str(SDR_BATCHES), "ingest", str(batch_id)],
                          cwd=str(SKILLS.parents[1]), capture_output=True, text=True)
    print(proc.stdout.strip() or proc.stderr.strip())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
