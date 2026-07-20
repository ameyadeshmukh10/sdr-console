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
import re
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
                        "Gives: signal play, run-rate + signal-set estimate, signal-mapping session, "
                        "pipeline gap analysis, pilot playbook (breakup).",
    "revops": "Frame the pain as signal-to-action latency, data hygiene, measurable lift. "
              "Gives: signal-mapping session, run-rate + signal-set estimate, pipeline gap analysis, "
              "signal play, outbound teardown.",
    "partnerships": "Frame the pain as co-sell / partner-sourced pipeline coverage at scale. "
                    "Gives: signal play (partner ecosystem), co-sell pilot playbook, run-rate + "
                    "signal-set estimate, signal-mapping session, 3 personalized drafts.",
    "sdr-bdr": "Frame the pain as follow-up volume, ramp time, response speed. "
               "Gives: 3 personalized drafts, run-rate + signal-set estimate, signal play, "
               "signal-mapping session, outbound teardown.",
}

# Shared research block — all variants research the same way; only the writing (Step 3) differs.
RESEARCH_BLOCK = """\
# Today's date
Today is {today}. The current year is {year}. Judge recency strictly against this date.

# Step 0 — the email domain is ground truth for WHO the company is
The contact block gives the company name from our CRM and the contact's email domain. CRM
names go stale (acquisitions, rebrands, mergers, junk values), but the domain is where the
email actually lands — trust it. If the stated name does not match the company operating that
domain today, research and write for the DOMAIN's company, using its current name (mention the
legacy brand only where it genuinely helps, e.g. "VictorOps, now Splunk On-Call"). Exception:
a personal/free email domain (gmail.com and the like) says nothing about the employer — keep
the stated company then.

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
"""

# ---- Variant write rules (Step 3). Choose one per run to A/B test. -----------------------------
WRITE_RULES = {
    # The original value-anchored give + meeting-ask sequence (the current baseline).
    "value-give": """\
# Step 3 — write the sequence
Write a 4-touch cold EMAIL sequence plus 3 LinkedIn touches, following every rule in the knowledge
above: 70-110 words per email (aim 80-95); three short paragraphs separated by a blank line; no em or
en dashes; NO sign-off or trailing name; step 1 opens on the signal (or the company-specific anchor
in the fallback case); each CTA leads with a deliverable give AND asks for a meeting; at least one
concrete metric across the sequence; step 4 is a breakup; never put pricing in a cold email.
""",

    # "Earn the reply": shorter, relevance-first, question CTAs, meeting deferred. The email itself
    # is the proof our AI writes like a sharp human.
    "earn": """\
# Step 3 — write the sequence ("earn the reply")
You are writing to a sharp, busy GTM leader who gets dozens of AI-generated cold emails a week. Your
job is to NOT sound like those: we sell an AI SDR, so this email must prove, by being visibly better,
that our AI writes like a thoughtful human peer. The goal of each early email is a REPLY, not a booked
meeting.

- 45-75 words per email. One or two short paragraphs. ONE idea per email.
- Touch 1 opens on the signal (or company anchor), then makes ONE specific, true observation about
  THEIR situation (their motion, stage, or the tension the signal implies), and ends with a SINGLE
  soft, open question. No meeting ask, no "give", no product pitch in touch 1.
- Touch 2: one new specific angle (if a hiring-signal line is provided in the contact block, that IS
  the angle for email 2: open on it). At most ONE proof point in the WHOLE sequence, told as a one-line
  human story (never a stack of numbers). End on a soft question or a light, low-friction offer. If a
  sequencing play is provided in the contact block, add one plain reassurance line (own email and
  LinkedIn infrastructure, nothing about their tools or process changes) and let the light offer be to
  size their current run rate and what the AI SDR would add on top.
- Touch 3: now you may ask for a short conversation, framed as a quick look at their signal sources
  and which high-yield ones are going unworked. If the sequence's ONE proof point lands here, tell it
  as the Memgraph signal-activation story in one human line (their intent tools and product telemetry
  were surfacing more in-market accounts than the team could prospect; pointing the AI SDR at the full
  signal set drove $2.7M in 90 days), tied to THEIR signals when a play above flags them. Keep the ask
  light (a quick chat / short call), not "worth 15 minutes to walk you through it".
- Touch 4: a genuine one-line breakup. No guilt, leave the door open.
- No em or en dashes. NO sign-off or trailing name. No hype words (revolutionary, game-changing,
  cutting-edge, supercharge, unlock, transform). Do not cram metrics. Never put pricing in a cold
  email. Vary how each email opens; do not start them all the same way.
""",

    # "Show the product": earn-the-reply PLUS a concrete async give that IS a demo of the product.
    "show": """\
# Step 3 — write the sequence ("show the product")
Same as the "earn the reply" style, with ONE addition: this email is a live demo of the product. In
TOUCH 2, make a concrete, low-friction, ASYNC offer that proves the product without a meeting: offer
to send a small real sample our AI would produce for THEM, e.g. "I had our AI draft 3 opening lines
(or 3 short emails) to {company}'s top 3 accounts, want me to send them over? No call." Delivered by
email, no meeting required. If a hiring-signal line is provided in the contact block, open email 2 on
it before making the sample offer.

- 45-75 words per email. One or two short paragraphs. ONE idea per email.
- Touch 1: open on the signal (or anchor) + ONE specific observation about their situation + a SINGLE
  soft, open question. No meeting ask in touch 1.
- Touch 2 carries the async sample offer above. At most ONE proof point in the whole sequence, as a
  one-line human story. A sequencing play from the contact block joins touch 2 only when there is NO
  hiring signal (one reassurance line: own sending infrastructure, their tools and process untouched).
- Touch 3: a soft meeting ask framed as signal activation: the variant's single proof point as one
  Memgraph line (their intent tools and product telemetry surfaced more in-market accounts than the
  team could prospect; the AI SDR was pointed at the full signal set), plus an offer to take a quick
  look at THEIR signal sources together.
- Touch 4: a genuine one-line breakup.
- No em or en dashes. NO sign-off or trailing name. No hype words. Do not cram metrics. Never put
  pricing in a cold email. Vary how each email opens.
""",
}
DEFAULT_VARIANT = "value-give"

OUTPUT_SCHEMA = """\
# Output
Return ONLY a single JSON object, no prose, no markdown code fences, matching EXACTLY this shape
(use \\n\\n for paragraph breaks inside bodies):

{
  "company": "<the company name you verified for the email domain (echo the given company if you did not research)>",
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


# Write-only preamble: the signal is provided (from the cache), so no web search. The chosen variant's
# write rules (above) are appended after this.
WRITE_PREAMBLE = """\
# Your task
You are GIVEN the company's current signal in the contact block below. Do NOT search the web. Write the
sequence using ONLY the provided signal. If the signal begins with "no recent signal -", treat the text
after it as the company's product / ICP / GTM anchor and personalize around that (do not invent news).
"""

# Research-only task (UI force-refresh): just find/refresh the signal, return only the signal JSON.
RESEARCH_ONLY_TASK = """\
Today is {today}. The current year is {year}. Judge recency strictly against this date.

The company name we have may be stale — the DOMAIN is ground truth. If the given name does not
match the company operating the domain today (acquisition, rebrand, merger, junk CRM data),
research the domain's company instead and use ITS current name. (Personal/free email domains
are the exception — keep the given name.)

Find ONE recent signal for the company, in priority order: (1) a {year} funding round the company raised,
(2) a {year} key leadership hire, (3) a {year} product launch / partnership / new market entry. A signal
qualifies ONLY if it happened in {year}; verify the date; ignore anything from {prev_year} or earlier. Use
at most 3 web searches. If none qualifies, set has_recent_signal=false and put a one-line product/ICP/GTM
anchor in `signal`, prefixed with "no recent signal - ".

Return ONLY this JSON, no prose:
{{"company": "<the company name you verified for the domain>",
  "signal": "<signal with its month/date, or 'no recent signal - <anchor>'>", "has_recent_signal": true|false}}
"""


def load_knowledge():
    parts = []
    for fname in ("offer.md", "cta-offers.md", "icp-email.md"):
        parts.append((KNOWLEDGE_DIR / fname).read_text())
    if EXAMPLE.is_file():
        parts.append("# Reference sequence (emulate the shape, never copy specifics)\n\n"
                     + EXAMPLE.read_text())
    return "\n\n---\n\n".join(parts)


def build_system(knowledge, variant=DEFAULT_VARIANT, mode="research"):
    rules = WRITE_RULES.get(variant, WRITE_RULES[DEFAULT_VARIANT])
    if mode == "write":
        body = WRITE_PREAMBLE + "\n\n" + rules
    else:
        body = RESEARCH_BLOCK.format(**_today()) + "\n\n" + rules
    return (
        "You are an expert B2B SDR copywriter for EverWorker's SDR AI Worker. Ground every claim in "
        "the knowledge base below; never invent product claims, numbers, or proof not in it.\n\n"
        + knowledge + "\n\n---\n\n" + body + "\n\n" + OUTPUT_SCHEMA
    )


def build_user(contact, cached_signal=None, prior_issues=None, tech_signals=None,
               tech_playbook=None, hiring_signals=None):
    persona = contact.get("persona", "sales-leadership")
    framing = PERSONA_FRAMING.get(persona, PERSONA_FRAMING["sales-leadership"])
    domain = contact.get("domain") or db.email_domain(contact.get("email"))
    base = (
        f"Contact:\n"
        f"- name: {contact.get('first_name','')} {contact.get('last_name','')}\n"
        f"- title: {contact.get('title','')}\n"
        f"- company: {contact.get('company','')}\n"
        + (f"- email domain: {domain}\n" if domain else "")
        + f"- persona: {persona}\n"
        f"- linkedin: {contact.get('linkedin_url','')}\n\n"
        f"Persona framing: {framing}\n\n"
    )
    if tech_signals:
        base += (
            f"Company tech stack (deterministic scan of their website/DNS; reliable): {tech_signals}\n"
            "Background only unless a play below says otherwise: reference a specific tool ONLY when "
            "it sharpens one line's relevance (e.g. their CRM). Never list the stack, never mention "
            "scanning, never present it as news. Never mention chat, scheduling, or website-chat "
            "tools (e.g. Qualified, Drift, Intercom, Chili Piper, Calendly) at all.\n\n"
        )
        pb = tech_playbook or {}
        seq, intent, ads = pb.get("sequencing") or [], pb.get("intent_abm") or [], pb.get("ads") or []
        if seq:
            base += (
                f"Sequencing play for EMAIL 2 (they run {seq[0]}): acknowledge in one line that the "
                "team already runs sequences (you may name the tool once, naturally) and make the "
                "no-disruption point: our AI SDR ships its own built-in email and LinkedIn "
                "deliverability infrastructure and sending capacity, so nothing about their current "
                "tools or process changes. Reps stay on follow-up and deal progression while it "
                "generates meetings and interested leads on autopilot, adding 2-5x more meetings on "
                "top of the current run rate. Close email 2 on the run-rate CTA: 15 minutes to "
                "calculate their current run rate, map their signal set, and estimate how many "
                "additional meetings the AI SDR would add. If a hiring signal is provided below, the "
                "hiring signal stays email 2's opener and this play shrinks to one supporting line "
                "before that CTA.\n\n"
            )
        if intent:
            base += (
                f"Signal-activation play for EMAIL 3 (they run {intent[0]}): name that one tool "
                "naturally (never as news, never implying we scanned them). In-market and intent "
                "signals are already flowing into their stack and most go unworked; our AI SDR "
                "activates automatically against exactly those signals, so they get worked the moment "
                "they appear. Tie this into email 3's Memgraph signal-activation proof and close on "
                "the signal-mapping CTA (both in the knowledge above).\n\n"
            )
        elif ads:
            base += (
                "Signal-activation play for EMAIL 3 (their site runs ad pixels): they are investing "
                "in ads, so their spend is already generating demand and inbound signals. Reference "
                "that ad investment in general terms only, never name pixel vendors and never imply "
                "we looked at their site: our AI SDR acts on the signals their ads already produce, "
                "turning existing investment into more meetings and pipeline. Tie this into email 3's "
                "Memgraph signal-activation proof and close on the signal-mapping CTA (both in the "
                "knowledge above).\n\n"
            )
    if hiring_signals:
        base += (
            f"Company hiring signal (live job-postings scan; reliable): {hiring_signals}\n"
            "Use it in EMAIL 2 ONLY: open email 2 on the hiring signal (open-role count plus one or two "
            "sales roles, e.g. 'hiring AEs and an SDR') and tie it to covering more pipeline while the "
            "new reps ramp. Never use it in email 1. If email 1's signal already covers their hiring, "
            "skip this angle entirely, do not double-hit. Never mention the data source, never list all "
            "the titles, do not claim the postings are new, and do not reference hiring anywhere else "
            "in the sequence. If a sequencing play is provided above, hiring still opens email 2 and "
            "the sequencing point shrinks to one supporting line.\n\n"
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


# --- relaxed linters for the new test variants (shorter, question-led, no forced give/meeting/metric) ---
_HYPE = re.compile(r"\b(revolutioniz\w*|revolutionary|game[- ]?chang\w*|cutting[- ]?edge|"
                   r"supercharg\w*|unlock\w*|transformati\w*|best[- ]in[- ]class|world[- ]class|"
                   r"seamless\w*|paradigm|synerg\w*)\b", re.I)


def _wc(body):
    return len(re.findall(r"[A-Za-z0-9']+", body or ""))


def _lint_short(email, lo=28, hi=100, require_give=None):
    """Shared rules for the earn/show variants: short, no dashes/sign-off/pricing/hype,
    a soft question in touch 1, a genuine breakup in touch 4. Word-count band is
    intentionally wide — it's a style nudge, not worth an expensive re-gen."""
    steps = []
    for i in range(1, 5):
        subj, body = email.get(f"subject{i}", ""), email.get(f"body{i}", "")
        if not subj or not body:
            return [f"missing subject{i}/body{i}"]
        steps.append({"n": i, "subject": subj, "body": body})
    issues = []
    for s in steps:
        b = s["body"]
        wc = _wc(b)
        if not (lo <= wc <= hi):
            issues.append(f"step{s['n']}: word count {wc} (need {lo}-{hi})")
        if L.DASH.search(b):
            issues.append(f"step{s['n']}: em/en dash present (use commas or periods)")
        lines = [ln.strip() for ln in b.splitlines() if ln.strip()]
        if lines and L.SIGNOFF_LINE.match(lines[-1]):
            issues.append(f"step{s['n']}: trailing sign-off/name (end on the line, no sign-off)")
        if L.PRICING.search(b):
            issues.append(f"step{s['n']}: pricing/plan language in a cold step")
        if _HYPE.search(b):
            issues.append(f"step{s['n']}: hype word (write like a sharp human, no buzzwords)")
    if "?" not in steps[0]["body"]:
        issues.append("step1: no soft question (touch 1 should end on one open question)")
    if not L.BREAKUP.search(steps[-1]["body"]):
        issues.append("step4: final step is not a breakup")
    if require_give and not require_give.search(" ".join(s["body"] for s in steps)):
        issues.append("missing the async sample offer (touch 2 should offer to send a real sample, no call)")
    return issues


# a lenient detector for the "show" variant's async sample offer
_SHOW_GIVE = re.compile(r"\bsample\b|drafted?\s+(3|three)|\b(3|three)\s+(sample|opening|short|"
                        r"personalized|tailored)\b|want me to send|send (you |them |over )|no call", re.I)


def lint_earn(email):
    return _lint_short(email)


def lint_show(email):
    return _lint_short(email, require_give=_SHOW_GIVE)


LINTERS = {"value-give": lint_email, "earn": lint_earn, "show": lint_show}


def lint_assets(asset):
    """Variant-aware lint used by both generation and ingest (reads asset['variant'])."""
    fn = LINTERS.get(asset.get("variant", DEFAULT_VARIANT), lint_email)
    return fn(asset.get("email", {}))


def _atomic_write(contact_id, asset):
    db.GEN_DIR.mkdir(parents=True, exist_ok=True)
    path = db.GEN_DIR / f"{contact_id}.json"
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(asset, indent=2, ensure_ascii=False))
    os.replace(tmp, path)


def generate_contact(contact, knowledge, client, write=True, cached_signal=None,
                     variant=DEFAULT_VARIANT, tech_signals=None, tech_playbook=None,
                     hiring_signals=None):
    """Generate + validate one contact. Returns a result dict.

    cached_signal (when provided): use it as the company signal and DO NOT search
    the web — much cheaper. Otherwise research the signal with web search.
    tech_signals (when provided): the company's detected tech-stack line, passed
    to the prompt as background context.
    tech_playbook (when provided): the {ads, intent_abm, sequencing} groups from
    _cached_tech — steers the email-2 sequencing play and email-3
    signal-activation play in the prompt.
    hiring_signals (when provided): the company's open-sales-roles line, passed
    to the prompt with the email-2-only placement instruction.
    variant: which instruction set / linter to use (value-give | earn | show).
    write=False skips the file write (used by --contact-test); the asset is
    still returned under result["asset"].
    """
    cid, persona = contact["contact_id"], contact.get("persona", "sales-leadership")
    mode = "write" if cached_signal else "research"
    use_search = cached_signal is None
    linter = LINTERS.get(variant, lint_email)
    issues, last_asset, web_searches = ["no output"], None, 0
    cache_read = cache_write = 0
    verified_company = ""  # the model's domain-verified company name (research mode)

    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            res = client.complete(
                build_system(knowledge, variant=variant, mode=mode),
                build_user(contact, cached_signal=cached_signal,
                           prior_issues=None if attempt == 1 else issues,
                           tech_signals=tech_signals, tech_playbook=tech_playbook,
                           hiring_signals=hiring_signals),
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

        verified_company = (data.get("company") or "").strip() or verified_company
        last_asset = {
            "contact_id": cid,
            "persona": persona,
            "variant": variant,
            # trust the cached signal verbatim; otherwise take the model's
            "signal": (cached_signal or data.get("signal") or "").strip(),
            "email": data.get("email", {}) or {},
            "linkedin": data.get("linkedin", {}) or {},
        }
        issues = linter(last_asset["email"])
        if not issues:
            if write:
                _atomic_write(cid, last_asset)
            return {"status": "linted", "signal": last_asset["signal"], "asset": last_asset,
                    "company": verified_company,
                    "web_searches": web_searches, "attempts": attempt, "issues": [],
                    "cache_read": cache_read, "cache_write": cache_write}

    # all attempts failed: write the last asset (if any) so ingest records it as failed with reason
    if write and last_asset is not None:
        _atomic_write(cid, last_asset)
    return {"status": "failed", "asset": last_asset,
            "signal": (last_asset or {}).get("signal", ""),
            "company": verified_company,
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


def _maybe_detect_tech(domain, company=None):
    """Best-effort technographic scan alongside signal research (cache-aware, a
    few seconds; TECH_DETECT_ENABLED=0 turns the inline hook off). Failures only
    log to stderr — generation must never break on the detector."""
    if not domain:
        return
    if (os.environ.get("TECH_DETECT_ENABLED") or "1").strip().lower() in ("0", "false", "no", "off"):
        return
    try:
        import tech_signals as _tech  # lazy: pulls in dnspython + vendored package
        _tech.detect_and_store(domain, company=company)
    except Exception as e:  # noqa: BLE001
        sys.stderr.write(f"[generate] tech detect skipped for {domain}: {e}\n")


def _cached_tech(domain):
    """(line, playbook) for prompt use — the stored tech-stack line plus the
    copy playbook groups ({ads, intent_abm, sequencing}) classified from stored
    tech_detail. (None, None) when there is nothing to say (no row, or the 'No
    signals detected' literal); legacy rows with no parseable detail give
    (line, None) = the old line-only background behavior. Classification is
    best-effort and never breaks generation."""
    if not domain:
        return None, None
    conn = db.connect()
    try:
        row = db.get_signal(conn, domain)
    finally:
        conn.close()
    tech = (row or {}).get("tech_signals")
    if not tech or tech == "No signals detected":
        return None, None
    playbook = None
    try:
        import tech_signals as _tech  # lazy: stdlib-only at import, mirrors the boot rule
        playbook = _tech.playbook_from_detail(row.get("tech_detail"))
    except Exception as e:  # noqa: BLE001
        sys.stderr.write(f"[generate] tech playbook skipped for {domain}: {e}\n")
    return tech, playbook


def _maybe_detect_hiring(domain, company=None):
    """Best-effort hiring scan alongside signal research (cache-aware, one
    Prospeo credit per company per HIRING_REFRESH_DAYS; HIRING_DETECT_ENABLED=0
    turns the inline hook off, and without PROSPEO_API_KEY it is a no-op).
    Failures only log to stderr — generation must never break on the detector."""
    if not domain:
        return
    if (os.environ.get("HIRING_DETECT_ENABLED") or "1").strip().lower() in ("0", "false", "no", "off"):
        return
    try:
        import hiring_signals as _hiring  # lazy (stdlib-only, but mirror the boot rule)
        if not _hiring.hiring_available()[0]:
            return
        _hiring.detect_and_store(domain, company=company)
    except Exception as e:  # noqa: BLE001
        sys.stderr.write(f"[generate] hiring detect skipped for {domain}: {e}\n")


def _cached_hiring(domain):
    """A compact hiring line for prompt use, or None. Fires ONLY when the scan
    found open sales/GTM roles (from hiring_detail.sales_titles) — a plain
    open-roles count with no sales roles is a weak angle for this pitch, and
    the 'No open roles detected' literal means nothing to say."""
    if not domain:
        return None
    conn = db.connect()
    try:
        row = db.get_signal(conn, domain)
    finally:
        conn.close()
    line = (row or {}).get("hiring_signals")
    if not line or line == "No open roles detected":
        return None
    try:
        detail = json.loads(row.get("hiring_detail") or "{}")
    except (ValueError, TypeError):
        return None
    sales = [t for t in (detail.get("sales_titles") or []) if t]
    if not sales:
        return None
    count = detail.get("active_count") or len(sales)
    return (f"{count} open roles, {len(sales)} in sales/GTM "
            f"(e.g. {', '.join(sales[:2])})")


def generate_one(contact, knowledge, client, write=True, variant=DEFAULT_VARIANT):
    """Cache-aware single-contact generation.

    Reuse a fresh per-company signal (write-only, no web search). On a cache miss
    research once per company under a per-domain lock, then store the signal so the
    rest of that company (and future runs within 90 days) skip the search.
    """
    domain = (contact.get("domain") or db.email_domain(contact.get("email")))

    cached = _fresh_cached_signal(domain)
    if cached:
        tech_line, tech_playbook = _cached_tech(domain)
        r = generate_contact(contact, knowledge, client, write=write, cached_signal=cached, variant=variant,
                             tech_signals=tech_line, tech_playbook=tech_playbook,
                             hiring_signals=_cached_hiring(domain))
        r["used_cache"] = True
        return r

    lock = _domain_lock(domain) if domain else None
    if lock:
        lock.acquire()
    try:
        if domain:  # another thread may have cached it while we waited
            cached = _fresh_cached_signal(domain)
            if cached:
                tech_line, tech_playbook = _cached_tech(domain)
                r = generate_contact(contact, knowledge, client, write=write, cached_signal=cached, variant=variant,
                                     tech_signals=tech_line, tech_playbook=tech_playbook,
                                     hiring_signals=_cached_hiring(domain))
                r["used_cache"] = True
                return r
        # cache miss = this thread researches the company: scan its tech + hiring
        # first (cache-aware, seconds) so this contact's copy can already use them
        _maybe_detect_tech(domain, contact.get("company", ""))
        _maybe_detect_hiring(domain, contact.get("company", ""))
        tech_line, tech_playbook = _cached_tech(domain)
        r = generate_contact(contact, knowledge, client, write=write, variant=variant,  # search + write
                             tech_signals=tech_line, tech_playbook=tech_playbook,
                             hiring_signals=_cached_hiring(domain))
        r["used_cache"] = False
        sig = (r.get("signal") or "").strip()
        if domain and sig:
            has_recent = not sig.lower().startswith("no recent signal")
            conn = db.connect()
            try:
                # prefer the model's domain-verified company name over the (possibly
                # stale) CRM value so the cache row can't say "VictorOps" for splunk.com
                db.upsert_signal(conn, domain, r.get("company") or contact.get("company", ""),
                                 sig, has_recent, client.model)
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
    # the model reconciles the CRM name against the domain; its verified name wins
    company_out = (data.get("company") or "").strip() or (company or "")
    conn = db.connect()
    try:
        db.upsert_signal(conn, domain, company_out, signal, has_recent, client.model)
    finally:
        conn.close()
    # a signal refresh also freshens the tech + hiring scans (skip-if-fresh keeps it cheap)
    _maybe_detect_tech(domain, company_out)
    _maybe_detect_hiring(domain, company_out)
    return {"domain": domain, "company_name": company_out, "signal": signal,
            "has_recent": 1 if has_recent else 0, "web_searches": res.get("web_search_count", 0)}


# ----------------------------------------------------------------------------
# "Show the product" fulfillment: when a show-arm lead says "yes, send them",
# draft 3 sample outbound emails our AI would write for THEIR outbound — i.e. to
# 3 accounts that fit the lead's company's ICP. This is the deliverable demo.
# ----------------------------------------------------------------------------
SAMPLES_TASK = """\
Today is {today}.

You are the AI SDR working FOR {company}. Show {company}'s GTM leader exactly what your outbound looks
like for THEIR business.

1. Research {company}: what they sell and, crucially, WHO they sell to (their ICP — the kind of
   companies and roles {company} prospects).
2. Pick THREE realistic target accounts that {company} would actually prospect (real companies that fit
   their ICP, not {company} itself). For each, find ONE recent ({year}) signal if you can; if not, use a
   specific true fact about that account.
3. For each, write ONE short, sharp, personalized opening line (1-2 sentences) that {company}'s AI SDR
   would send to that account, grounded in the signal/fact. Sound like a sharp human, no hype, no em
   dashes, do not invent funding numbers.

Return ONLY this JSON, no prose:
{{"company": "{company}",
  "icp_summary": "<one line: who {company} sells to>",
  "samples": [
    {{"account": "<target company>", "signal": "<recent signal or specific fact>", "opener": "<1-2 sentence opener>"}},
    {{"account": "...", "signal": "...", "opener": "..."}},
    {{"account": "...", "signal": "...", "opener": "..."}}
  ]}}
"""


def generate_samples(company, domain="", client=None):
    """Draft 3 sample outbound emails our AI would write for {company}'s own outbound."""
    client = client or AnthropicClient()
    system = ("You are an expert B2B SDR copywriter. Be specific, concrete, and human. Ground claims in "
              "what you can actually find; never invent funding figures or fake metrics.")
    user = SAMPLES_TASK.format(company=(company or domain or "the company"), **_today())
    res = client.complete(system, user, use_web_search=True, max_web_searches=4, max_tokens=2048)
    data = extract_json(res["text"])
    return {
        "company": company, "domain": domain,
        "icp_summary": (data.get("icp_summary") or "").strip(),
        "samples": data.get("samples", []) or [],
        "web_searches": res.get("web_search_count", 0),
    }


# ----------------------------------------------------------------------------
# Message Batches API path: build requests, then process results. Same prompts
# and cache logic as the real-time path, just packaged for async batch submit.
# ----------------------------------------------------------------------------
def build_request_params(contact, knowledge, client, cached_signal=None, variant=DEFAULT_VARIANT,
                         tech_signals=None, tech_playbook=None, hiring_signals=None):
    """The Messages `params` for one contact (write-only if a cached signal is
    given, else a combined research+write request with web search). 1h cache."""
    mode = "write" if cached_signal else "research"
    return client.build_body(
        build_system(knowledge, variant=variant, mode=mode),
        build_user(contact, cached_signal=cached_signal, tech_signals=tech_signals,
                   tech_playbook=tech_playbook, hiring_signals=hiring_signals),
        use_web_search=cached_signal is None, max_web_searches=3,
        max_tokens=4096, cache_ttl="1h",
    )


def prepare_batch_requests(contacts, knowledge, client=None, variant=DEFAULT_VARIANT):
    """Build {custom_id, params} for each contact + a manifest for result handling.
    Already-cached companies become cheap write-only requests (no web search)."""
    client = client or AnthropicClient()
    requests, manifest = [], {}
    for c in contacts:
        cid = str(c["contact_id"])
        domain = c.get("domain") or db.email_domain(c.get("email"))
        cached = _fresh_cached_signal(domain)
        cvariant = c.get("variant") or variant  # per-contact split wins over run-level
        tech_line, tech_playbook = _cached_tech(domain)
        requests.append({"custom_id": cid,
                         "params": build_request_params(c, knowledge, client, cached_signal=cached,
                                                        variant=cvariant, tech_signals=tech_line,
                                                        tech_playbook=tech_playbook,
                                                        hiring_signals=_cached_hiring(domain))})
        manifest[cid] = {"contact": c, "domain": domain, "variant": cvariant,
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
    variant = entry.get("variant", DEFAULT_VARIANT)

    if result.get("type") != "succeeded":
        return {"status": "retry", "issues": [f"batch result: {result.get('type')}"], "contact": contact}

    parsed = parse_message(result.get("message", {}))
    try:
        data = extract_json(parsed["text"])
    except AnthropicJSONError:
        return {"status": "retry", "issues": ["invalid JSON from batch"], "contact": contact}

    asset = {
        "contact_id": cid, "persona": persona, "variant": variant,
        "signal": (cached_signal or data.get("signal") or "").strip(),
        "email": data.get("email", {}) or {}, "linkedin": data.get("linkedin", {}) or {},
    }
    # cache the researched signal (combined requests) even if the copy fails lint
    if entry["was_combined"] and domain and asset["signal"]:
        has_recent = not asset["signal"].lower().startswith("no recent signal")
        conn = db.connect()
        try:
            db.upsert_signal(conn, domain,
                             (data.get("company") or "").strip() or contact.get("company", ""),
                             asset["signal"], has_recent)
        finally:
            conn.close()

    issues = lint_assets(asset)
    if issues:
        return {"status": "retry", "issues": issues, "contact": contact, "signal": asset["signal"]}
    _atomic_write(cid, asset)
    return {"status": "linted", "signal": asset["signal"],
            "web_searches": parsed["web_search_count"], "usage": parsed["usage"],
            "used_cache": cached_signal is not None}


def generate_batch(batch_id, progress_cb=None, cancel_event=None, max_workers=MAX_WORKERS,
                   variant=DEFAULT_VARIANT):
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
            # a pre-assigned per-contact variant (e.g. a 3-way-split sourced list) wins;
            # otherwise use the run-level variant the caller chose.
            cvariant = contact.get("variant") or variant
            r = generate_one(contact, knowledge, client, variant=cvariant)
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

    usage: generate_batch.py --contact-test [Company] [Title] [persona] [FirstName] [variant]
           variant in {value-give, earn, show} (default value-give)
    """
    args = sys.argv[2:]
    variant = args[4] if len(args) > 4 and args[4] in WRITE_RULES else DEFAULT_VARIANT
    contact = {
        "contact_id": "TEST",
        "first_name": args[3] if len(args) > 3 else "Jordan",
        "last_name": "Test",
        "title": args[1] if len(args) > 1 else "VP of Sales",
        "company": args[0] if len(args) > 0 else "Ramp",
        "persona": args[2] if len(args) > 2 else "sales-leadership",
        "linkedin_url": "",
    }
    print(f"generating [{variant}] test copy for {contact['first_name']} @ {contact['company']} "
          f"({contact['persona']})...\n")
    client = AnthropicClient()
    r = generate_contact(contact, load_knowledge(), client, write=False, variant=variant)
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
