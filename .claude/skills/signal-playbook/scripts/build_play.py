"""Signal Playbook Reply Agent — build a personalized signal play for an
interested reply's lead and draft the follow-up email around it.

Stages (each announced as "stage: <name>" on stderr so the webui job can track
progress; the final JSON result is stdout's last line):

  research   Web-search research on the lead's company -> research.md
             (the company-researcher agent spec, run as one Messages API call)
  deck-data  research.md -> schema-valid deck-data.json (+ one repair round-trip
             against the node validator)
  render     deck-data.json -> single-file interactive HTML via the vendored
             deck-renderer (serialized: the renderer has one shared fill file)
  publish    HTML -> LIVE HubSpot website page at
             everworker.ai/signal-plays/<company>-ai-sdr-playbook (per-play coded
             template via the source-code API, then create/update page draft +
             POST /draft/push-live = live within seconds; same URL on rebuilds)
  draft      A contextualized reply email embedding the page URL, merged into
             followup_drafts.json for the normal edit-before-send approval flow

Any render/publish failure still produces a draft (fallback="standard", no link),
so the SDR always gets something to edit and send.

  python3 build_play.py --reply-id <id> --contact-json '{"firstName":...}'
"""

import argparse
import fcntl
import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent                     # signal-playbook/scripts
SKILLS = HERE.parents[1]                                    # .claude/skills
PROJECT_ROOT = HERE.parents[3]
sys.path.insert(0, str(SKILLS / "ai-sdr" / "scripts"))     # anthropic_client
sys.path.insert(0, str(SKILLS / "sdr-pipeline" / "scripts"))  # hubspot_client
sys.path.insert(0, str(SKILLS / "email-bison" / "scripts"))   # draft_followups

from anthropic_client import (                              # noqa: E402
    AnthropicClient, extract_json, AnthropicError, AnthropicJSONError,
)
# One knowledge loader for every reply agent — the standard drafter owns it, so
# a knowledge-base change can't silently diverge between agents.
from draft_followups import load_context as load_knowledge  # noqa: E402

TEMPLATE = HERE.parent / "templates" / "research.template.md"
SCHEMA = PROJECT_ROOT / "schemas" / "deck-data.schema.json"
RENDERER = PROJECT_ROOT / "deck-renderer"
RENDER_LOCK = RENDERER / ".deck.lock"
EXPORT_DIR = RENDERER / "export-deck-bites"
PLAYS_DIR = PROJECT_ROOT / "data" / "signal-plays"
REVIEW_QUEUE = PROJECT_ROOT / "data" / "interested-replies" / "review_queue.json"
LI_REVIEW_QUEUE = PROJECT_ROOT / "data" / "interested-replies" / "li_review_queue.json"
DRAFTS = PROJECT_ROOT / "data" / "interested-replies" / "followup_drafts.json"

REQUIRED_URL_SUFFIX = "everworker.ai"


def log(msg):
    print(msg, file=sys.stderr, flush=True)


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def slugify(name):
    s = re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")
    return s[:60] or "prospect"


# ---------------------------------------------------------------------------
# Unicode sanitation. Web-scraped content is full of typographic junk — thin /
# narrow no-break spaces, soft hyphens, zero-widths — and the model copies it
# verbatim into research/deck copy. On the deck these render as gaps INSIDE
# words ("pe tabyte", "G DPR"), and they silently eat the character budgets.
# ---------------------------------------------------------------------------
_INVISIBLE = re.compile("[\\u00ad\\u200b\\u200c\\u200d\\u200e\\u200f\\u2060\\ufeff]")
_EXOTIC_SP = "\\u00a0\\u1680\\u2000-\\u200a\\u202f\\u205f\\u3000"
# Two-letter words that legitimately follow a space — never glue these on.
_SMALL_WORDS = {"a", "i", "an", "as", "at", "be", "by", "do", "go", "if", "in",
                "is", "it", "my", "no", "of", "on", "or", "so", "to", "up", "us", "we"}


def sanitize_text(s):
    if not isinstance(s, str) or not s:
        return s
    s = _INVISIBLE.sub("", s)
    s = s.replace("\u2010", "-").replace("\u2011", "-")
    # exotic space hugging punctuation: drop the space ("file /object", "70– 90")
    s = re.sub(f"[{_EXOTIC_SP}]+(?=[^\\w\\s])", "", s)
    s = re.sub(f"(?<=[^\\w\\s])[{_EXOTIC_SP}]+", "", s)

    def fix(m):
        b, a = m.group(1), m.group(2)
        if b[-1].isupper() and len(a) >= 2 and a[0].isupper() and a[1].isupper():
            return b + a          # split acronym: "G DPR" -> "GDPR"
        if a[0].islower() and (len(b) <= 3
                               or (len(a) <= 2 and a.lower() not in _SMALL_WORDS)):
            return b + a          # hyphenation-point split: "pe tabyte", "cove ry"
        return b + " " + a        # genuine word gap: "Data Dynamics"

    prev = None
    while prev != s:              # chains like "uns truc tured" need repeated passes
        prev = s
        s = re.sub(f"([A-Za-z]+)[{_EXOTIC_SP}]+([A-Za-z]+)", fix, s)
    s = re.sub(f"[{_EXOTIC_SP}]+", " ", s)   # leftovers (around digits etc.)
    return re.sub(r" {2,}", " ", s)


def sanitize_deck(node, counter=None):
    """Recursively sanitize every string in the deck-data structure."""
    if isinstance(node, str):
        clean = sanitize_text(node)
        if counter is not None and clean != node:
            counter[0] += 1
        return clean
    if isinstance(node, list):
        return [sanitize_deck(x, counter) for x in node]
    if isinstance(node, dict):
        return {k: sanitize_deck(v, counter) for k, v in node.items()}
    return node


def load_queue_item(reply_id):
    for path, channel in ((REVIEW_QUEUE, "email"), (LI_REVIEW_QUEUE, "linkedin")):
        if not path.is_file():
            continue
        try:
            data = json.loads(path.read_text())
        except (ValueError, OSError):
            continue
        for it in data.get("items") or []:
            if str(it.get("reply_id")) == str(reply_id):
                it.setdefault("channel", channel)
                return it
    return None


def thread_context(item):
    """The conversation so far, oldest-first, for grounding the reply draft."""
    if not item:
        return ""
    parts = []
    for m in reversed(item.get("sent_emails") or []):
        parts.append(f"[WE SENT · {m.get('date') or ''}] {m.get('subject') or ''}\n"
                     f"{(m.get('text') or '')[:1200]}")
    parts.append(f"[THEY REPLIED · {item.get('date_received') or ''}] "
                 f"{item.get('subject') or ''}\n{(item.get('text_body') or '')[:2500]}")
    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# Stage 1 — research
# ---------------------------------------------------------------------------
RESEARCH_SYSTEM = """\
You are the company researcher in EverWorker's AI SDR playbook pipeline. EverWorker sells an
AI SDR. The playbook you feed is a "we already did your outbound for you" demo for a sales
leader at a PROSPECT company: show you understand their business, then find them a real
TARGET account, surface buying signals, resolve the buyers, and draft the outreach our AI SDR
would send on their behalf.

TWO COMPANIES, never conflated:
- PROSPECT = the company the input contact works at (EverWorker's buyer). Drives Cover +
  "The Research" (their offering, ICP, buying group).
- TARGET = a real account that fits the PROSPECT's ICP and is in-market now — the company our
  AI SDR would prospect into FOR the prospect. Drives "The Play" + "Outreach".

Rules:
- Web research only: search and read the prospect's real website, case studies, and news.
  Do not invent facts; mark anything unconfirmed as (inferred). Cite sources inline as [n]
  and list URLs under SOURCES.
- Target news/hiring signals must be recent (within ~90 days of today). Older = not a "now"
  signal.
- Technographic signals: the PROSPECT's detected tech stack is provided in the input — it
  comes from a deterministic website/DNS scanner, so trust it verbatim (no need to re-verify).
  For the TARGET, infer tech from public evidence (job postings, site markup, docs, partner
  pages); a verified scan of the target is appended to this file by the pipeline afterwards.
- Wrap the punchiest metric/phrase of offering bullets and outreach drafts in ==double-equals==.
- Respect every length ceiling noted in the template — the deck has fixed-size cards. The
  ceilings are hard LIMITS, not targets.
- Outreach drafts: SHORT WINS. Target 200-280 characters per email body and 150-250 per
  LinkedIn message. One idea per message, grounded in a named signal, complete sentences
  only, and ALWAYS end on a single clear, low-friction question or concrete offer.
- Type only plain ASCII spaces — never thin/narrow/no-break spaces or soft hyphens (web
  pages are full of them; do NOT copy them into your text).

Produce ONE markdown document following the template below EXACTLY — same headings, same
order. Return ONLY the markdown document, no preamble.
"""

def detect_prospect_tech(contact):
    """Cache-aware technographic scan of the PROSPECT's domain (tech_signals.py,
    sdr-pipeline scripts dir — already on sys.path). Returns the formatted line
    or "". Best-effort: any failure only logs, the build never depends on it."""
    domain = (contact.get("companyDomain") or "").strip()
    if not domain:
        return ""
    try:
        import tech_signals  # noqa: E402  (lazy: dnspython + vendored package)
        res = tech_signals.detect_and_store(domain, company=contact.get("companyName"))
        return (res or {}).get("tech_signals") or ""
    except Exception as e:  # noqa: BLE001
        log(f"prospect tech scan skipped: {e}")
        return ""


def augment_target_tech(research_md):
    """Post-research: pull the TARGET's domain out of the research file, run the
    deterministic scanner on it (pure detect, no store — targets are not outreach
    accounts), and append a clearly-labeled verified block for stage_deck_data to
    use. Best-effort; returns the file unchanged on any miss/failure."""
    try:
        m = re.search(r"\*\*Domain:\*\*\s*([A-Za-z0-9][A-Za-z0-9.-]*\.[A-Za-z]{2,})", research_md)
        if not m:
            return research_md
        target_domain = m.group(1).strip().rstrip(".")
        import tech_signals  # noqa: E402
        res = tech_signals.detect_domain(target_domain)
        line = res.get("formatted") or f"unavailable ({res.get('error') or 'scan failed'})"
        log(f"target tech scan ({target_domain}): {line[:120]}")
        return (research_md.rstrip() + "\n\n"
                "### 6c-verified · Technographic scan (TARGET)\n"
                f"Deterministic DNS + website scan of {target_domain} run by the pipeline — "
                "trust verbatim over inferred tech above:\n"
                "```\n" + line + "\n```\n")
    except Exception as e:  # noqa: BLE001
        log(f"target tech scan skipped: {e}")
        return research_md


def stage_research(client, contact):
    log("stage: research")
    template = TEMPLATE.read_text()
    tech_line = detect_prospect_tech(contact)
    user = (
        f"Input contact profile:\n"
        f"- Name: {contact.get('firstName','')} {contact.get('lastName','')}\n"
        f"- Job title: {contact.get('jobTitle','')}\n"
        f"- Business email: {contact.get('businessEmail','')}\n"
        f"- Prospect company: {contact.get('companyName','')}\n"
        f"- Company domain: {contact.get('companyDomain','')}\n"
        f"- Detected tech stack (prospect; deterministic website/DNS scan): {tech_line or 'unavailable'}\n"
        f"- Today's date: {datetime.now(timezone.utc).strftime('%Y-%m-%d')}\n\n"
        f"Research the prospect and produce the completed research file."
    )
    res = client.complete(RESEARCH_SYSTEM + "\n# TEMPLATE\n\n" + template, user,
                          use_web_search=True, max_web_searches=10,
                          max_tokens=8000, timeout=1500)
    text = sanitize_text((res.get("text") or "").strip())
    if len(text) < 500 or "TARGET" not in text.upper():
        raise RuntimeError("research output too thin to build a play from")
    return text


# ---------------------------------------------------------------------------
# Stage 2 — deck-data (+ one repair round-trip against the validator)
# ---------------------------------------------------------------------------
DATA_SYSTEM = """\
You turn an AI SDR research markdown file into the JSON that fills a fixed-layout 4-slide
deck. You do NO research and invent NOTHING — only restructure and tighten what's in the
research file.

Hard rules:
- Respect every maxLength/maxItems in the schema; condense copy to fit (the validator rejects
  violations). The ==double-equals== highlight markers count toward length budgets.
- maxLength budgets are CEILINGS, not targets — and you cannot count characters precisely,
  so land WELL under them. When condensing, drop a whole middle sentence rather than
  trimming words off the end.
- Outreach copy: SHORT WINS. Target 200-280 chars per email body and 150-250 per LinkedIn
  message. One idea per message, grounded in a named signal, complete sentences only, and
  every message MUST end with a single clear, low-friction question or concrete offer as
  its final sentence. Never trail off mid-thought.
- target.gate and icp.summary must read as complete thoughts — no dangling conjunctions.
- Type only plain ASCII spaces — never thin/narrow/no-break spaces or soft hyphens (do not
  copy them from the research file).
- Exact counts: buyingGroup = 3 (Champion, Decision Maker, Economic Buyer, in that order),
  target.stats = 3, outreach.linkedin = 2, outreach.email = 2 (matching the first two
  target.contacts, same order).
- signals[].kind is one of: expansion | hiring | tech | program | news.
- ticker only if the target is public; else omit or "".
- prospect.logoDataUri = "".
- Wrap 1-4 punchy phrases per outreach body / offering bullet in ==double-equals==.

Return ONLY the JSON object, no prose, no markdown fences.
"""

def run_validator(data_path):
    proc = subprocess.run(
        ["node", str(RENDERER / "scripts" / "validate-deck-data.mjs"), str(data_path)],
        capture_output=True, text=True, timeout=120)
    return proc.returncode == 0, (proc.stdout + proc.stderr).strip()


# The model cannot count characters, so pure length/count overruns are fixed
# mechanically instead of hoping another LLM round lands under budget (it
# routinely misses by a few chars and used to fail the whole build).
_ERR_TOO_LONG = re.compile(r"deck\.([^\s:]+): too long — \d+/(\d+) chars")
_ERR_TOO_MANY = re.compile(r"deck\.([^\s:]+): allows at most (\d+) items")
_PATH_SEG = re.compile(r"([^.\[\]]+)|\[(\d+)\]")


def parse_validator_errors(report):
    """Validator report -> [{kind: 'too_long'|'too_many', path: [seg,...], limit: N}].
    Path segments are dict keys (str) and list indexes (int); leading 'deck.' stripped."""
    out = []
    for line in (report or "").splitlines():
        m = _ERR_TOO_LONG.search(line)
        kind = "too_long" if m else None
        if not m:
            m = _ERR_TOO_MANY.search(line)
            kind = "too_many" if m else None
        if not m:
            continue
        path = [seg if seg else int(idx)
                for seg, idx in _PATH_SEG.findall(m.group(1)) if seg or idx]
        out.append({"kind": kind, "path": path, "limit": int(m.group(2))})
    return out


def _balance_highlights(s):
    """Truncation can cut inside a ==highlight==; an odd marker count would leak
    literal '==' onto the slide. Close the last open marker if its text survived,
    else drop it."""
    if s.count("==") % 2 == 0:
        return s
    i = s.rfind("==")
    tail = s[i + 2:].strip()
    if tail:            # highlight text survived the cut — close it (adds 2 chars;
        return s + "==" # the caller's loop re-trims if that pushes past the budget)
    return (s[:i] + s[i + 2:]).rstrip()


# Trailing connector words/symbols left behind by a word-boundary cut — stripped
# repeatedly so a clamp never ends "...needing governance &" or "...for a".
_DANGLING = re.compile(
    r"(?:\s+(?:&|and|or|but|with|for|to|the|an?|of|on|in|at|by|as|that|its?|their|your|our)"
    r"|[\s,;:&—–-])+$", re.IGNORECASE)
_SENT_END = re.compile(r"[.!?](?=\s)")


def _clamp_string(s, limit):
    """Shorten to <= limit, preferring the last COMPLETE SENTENCE at or above
    half the budget (a mid-sentence cut reads as gibberish on the slide); else
    cut at a word boundary and strip dangling connectors. Keeps ==highlight==
    markers balanced."""
    while True:
        cut = s[:limit]
        sent_end = None
        if cut.rstrip()[-1:] in (".", "!", "?"):
            sent_end = len(cut.rstrip())
        else:
            ends = [m.end() for m in _SENT_END.finditer(cut)]
            if ends:
                sent_end = ends[-1]
        if sent_end and sent_end >= int(limit * 0.5):
            cut = cut[:sent_end].rstrip()
        else:
            sp = cut.rfind(" ")
            if sp >= int(limit * 0.6):
                cut = cut[:sp]
            cut = _DANGLING.sub("", cut).rstrip()
        cut = _balance_highlights(cut)
        if len(cut) <= limit:
            return cut
        limit -= 2  # balancing re-added a marker; trim a little further


def clamp_deck_data(data, errors):
    """Apply mechanical fixes for length/count overruns in place. Returns the
    number of fields clamped (other error classes are left for the LLM repair)."""
    fixed = 0
    for err in errors:
        node = data
        try:
            for seg in err["path"][:-1]:
                node = node[seg]
            leaf = err["path"][-1]
            val = node[leaf]
        except (KeyError, IndexError, TypeError):
            continue
        if err["kind"] == "too_long" and isinstance(val, str):
            clamped = _clamp_string(val, err["limit"])
            node[leaf] = clamped
            log(f"clamped deck.{'.'.join(map(str, err['path']))}: "
                f"{len(val)} -> {len(clamped)} chars (budget {err['limit']})")
            fixed += 1
        elif err["kind"] == "too_many" and isinstance(val, list):
            node[leaf] = val[:err["limit"]]
            log(f"clamped deck.{'.'.join(map(str, err['path']))}: "
                f"{len(val)} -> {err['limit']} items")
            fixed += 1
    return fixed


def _validate_with_clamp(data, data_path):
    """Validate; mechanically fix any length/count overruns and re-validate.
    Returns (ok, report, data)."""
    data_path.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    ok, report = run_validator(data_path)
    if ok:
        return True, report, data
    if clamp_deck_data(data, parse_validator_errors(report)):
        data_path.write_text(json.dumps(data, indent=2, ensure_ascii=False))
        ok, report = run_validator(data_path)
    return ok, report, data


def stage_deck_data(client, research_md, out_dir):
    log("stage: deck-data")
    schema = SCHEMA.read_text()
    system = DATA_SYSTEM + "\n# SCHEMA (authoritative)\n\n" + schema
    user = "# RESEARCH FILE\n\n" + research_md + "\n\nProduce the deck-data JSON."
    res = client.complete(system, user, use_web_search=False, max_tokens=8000, timeout=600)
    data = _sanitized(extract_json(res["text"]))
    data_path = out_dir / "deck-data.json"
    ok, report, data = _validate_with_clamp(data, data_path)
    if not ok:
        # Structural problems (missing fields, bad enums) — the one LLM repair
        # round is for these; leftover length overruns get clamped again after.
        log("deck-data invalid; one repair round-trip")
        repair = client.complete(
            system,
            user + "\n\nYour previous attempt:\n" + json.dumps(data, ensure_ascii=False)
            + "\n\nThe validator rejected it with:\n" + report[:3000]
            + "\n\nFix every reported issue and return the corrected FULL JSON.",
            use_web_search=False, max_tokens=8000, timeout=600)
        data = _sanitized(extract_json(repair["text"]))
        ok, report, data = _validate_with_clamp(data, data_path)
        if not ok:
            raise RuntimeError(f"deck-data failed validation after repair: {report[:500]}")
    return data


def _sanitized(data):
    counter = [0]
    data = sanitize_deck(data, counter)
    if counter[0]:
        log(f"sanitized exotic whitespace/invisible chars in {counter[0]} deck field(s)")
    return data


# ---------------------------------------------------------------------------
# Stage 3 — render the single-file HTML (serialized: ONE shared fill file).
# No PDF: the interactive HTML *is* the deliverable — it renders in the
# viewer's own browser, so container font quirks can't touch it.
# ---------------------------------------------------------------------------
def stage_render(data_path, company, out_dir):
    log("stage: render")
    safe = re.sub(r"[^A-Za-z0-9]+", "-", company or "Prospect").strip("-") or "Prospect"
    with open(RENDER_LOCK, "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        shutil.copyfile(data_path, RENDERER / "deck-data.json")
        proc = subprocess.run(["npm", "--prefix", str(RENDERER), "run", "deck"],
                              capture_output=True, text=True, timeout=1200,
                              cwd=str(PROJECT_ROOT))
        if proc.returncode != 0:
            raise RuntimeError("deck build failed: "
                               + (proc.stderr or proc.stdout).strip()[-600:])
        html_out = out_dir / f"{safe}-AI-SDR-Playbook.html"
        shutil.copyfile(EXPORT_DIR / "Bites-AI-SDR-Playbook.html", html_out)
    log(f"rendered {html_out.name}")
    return html_out


# ---------------------------------------------------------------------------
# Stage 4 — publish as a LIVE HubSpot website page.
# The single-file deck is baked into a per-play coded template (no page-widget
# API gymnastics), then the 2-step instant-publish flow runs: create/update the
# page draft -> POST /draft/push-live. Live within seconds, same URL on rebuild.
# ---------------------------------------------------------------------------
_HEAD_ASSET = re.compile(
    r"<style\b.*?</style>|<script\b.*?</script>|<link\b[^>]*>", re.S | re.I)


def to_cms_template(html, title):
    """Single-file deck HTML -> HubSpot coded page template. Extracts the built
    <head> assets + <body> content, wraps them in {% raw %} (the React bundle is
    full of braces HubL would parse), and adds HubSpot's required include tags."""
    head_m = re.search(r"<head\b[^>]*>(.*?)</head>", html, re.S | re.I)
    body_m = re.search(r"<body\b[^>]*>(.*?)</body>", html, re.S | re.I)
    if not body_m:
        raise RuntimeError("built deck HTML has no <body> — cannot publish")
    head_assets = "\n".join(m.group(0) for m in _HEAD_ASSET.finditer(head_m.group(1))) if head_m else ""
    for guard in ("{% raw %}", "{% endraw %}"):
        if guard in head_assets or guard in body_m.group(1):
            raise RuntimeError("deck HTML contains a raw-guard token — refusing to publish")
    noindex = (os.environ.get("SIGNAL_PLAY_NOINDEX", "1") or "1").strip().lower() \
        not in ("0", "false", "no")
    robots = '<meta name="robots" content="noindex">\n    ' if noindex else ""
    return f"""<!--
    templateType: page
    isAvailableForNewContent: false
    label: Signal play — {title}
-->
<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>{title}</title>
    {robots}{{{{ standard_header_includes }}}}
    {{% raw %}}{head_assets}{{% endraw %}}
  </head>
  <body>
    {{% raw %}}{body_m.group(1)}{{% endraw %}}
    {{{{ standard_footer_includes }}}}
  </body>
</html>
"""


def stage_publish(html_path, company, slug):
    log("stage: publish")
    from hubspot_client import HubSpotClient, HubSpotError  # noqa: E402
    hs = HubSpotClient()
    title = f"{company} — AI SDR Playbook"
    template_path = f"templates/signal-plays/{slug}.html"
    page_slug = f"signal-plays/{slug}-ai-sdr-playbook"

    try:
        hs.upsert_source_file(template_path, to_cms_template(html_path.read_text(), title))
    except HubSpotError as e:
        if "403" in str(e) or "MISSING_SCOPES" in str(e).upper():
            raise RuntimeError("HubSpot token lacks the `content` scope (CMS pages) — add it "
                               "to the private app, then regenerate") from e
        raise
    log(f"template published: {template_path}")

    # htmlTitle is the PUBLIC page title — publish validation refuses a SITE_PAGE
    # without one (CONTENT_TITLE_MISSING); `name` alone is only the dashboard name.
    body = {"name": title, "htmlTitle": title, "slug": page_slug,
            "templatePath": template_path, "state": "DRAFT"}
    domain = (os.environ.get("SIGNAL_PLAY_DOMAIN") or "").strip()
    if domain:
        body["domain"] = domain
    existing = hs.get_site_page_by_slug(page_slug)
    was_published = False
    if existing:
        page_id = existing.get("id")
        was_published = "PUBLISHED" in str(existing.get("currentState")
                                           or existing.get("state") or "").upper()
        hs.update_site_page_draft(page_id, body)
        log(f"page draft updated: id {page_id} (currently "
            f"{'published' if was_published else 'unpublished'})")
    else:
        created = hs.create_site_page(body)
        page_id = created.get("id")
        log(f"page created: id {page_id}")

    # Preflight: publish validation requires a public title + template — verify
    # they took before attempting to publish, so failures are self-explanatory.
    check = hs.get_site_page(page_id) or {}
    if not (check.get("htmlTitle") or "").strip():
        raise RuntimeError(f"page {page_id} still has no htmlTitle after update — "
                           f"HubSpot will refuse to publish it")

    if was_published:
        # already live: fold the fresh draft into the live page
        hs.push_site_page_live(page_id)
        log("republished: draft pushed live")
    else:
        # never published: v3's schedule-with-now IS the publish action
        # (/draft/push-live does NOT publish a draft page)
        hs.publish_site_page_now(page_id)

    # Verify it actually went live — a silent Draft must never happen again.
    page, state = check, ""
    for attempt in range(5):
        page = hs.get_site_page(page_id) or {}
        state = str(page.get("currentState") or page.get("state") or "").upper()
        log(f"page state: {state or 'unknown'}")
        if "PUBLISHED" in state or "SCHEDULED" in state:
            break
        time.sleep(3)
    if "PUBLISHED" not in state and "SCHEDULED" not in state:
        raise RuntimeError(f"page {page_id} did not publish (state {state or 'unknown'}) — "
                           f"check the token's `content` scope and the portal's page limits")

    page_url = page.get("url") or ""
    if not page_url:
        dom = page.get("domain") or domain
        page_url = f"https://{dom}/{page_slug}" if dom else ""
    host = re.sub(r"^https?://", "", page_url).split("/")[0].lower()
    domain_ok = host == REQUIRED_URL_SUFFIX or host.endswith("." + REQUIRED_URL_SUFFIX)
    if not domain_ok:
        log(f"warning: page host {host!r} is not on {REQUIRED_URL_SUFFIX} — set "
            f"SIGNAL_PLAY_DOMAIN or check the portal's primary website domain")
    log(f"live: {page_url} ({state})")
    return {"page_id": page_id, "page_url": page_url, "url_domain_ok": domain_ok}


# ---------------------------------------------------------------------------
# Stage 5 — draft the reply embedding the play
# ---------------------------------------------------------------------------
DRAFT_SYSTEM = """\
You are an SDR replying to an INTERESTED B2B prospect who answered a cold sequence for
EverWorker's SDR AI Worker. You have just built them a personalized "signal play": a short
playbook showing a real in-market target account (in THEIR ICP), the buying signals on it,
the resolved buyer contacts, and the outreach our AI SDR would send on their behalf.

Rules (from the playbook):
- Deliver the give FIRST: the play link is the value — lead with it, in the first two lines.
- Reference 1-2 specifics from the play (the target account, the sharpest signal) so it
  reads hand-built, not templated.
- Match the prospect's energy: short, warm, specific, no corporate filler, no em dashes,
  no hype. One idea per reply. End on a single clear, low-friction ask (a short call to
  walk the play + what running this at scale would look like).
- Ground every claim in the product knowledge; never invent numbers or customers.
- Pricing ONLY on a direct pricing ask.

Return ONLY this JSON, no prose:
{"draft": "<the reply body, ready to send, containing the play URL>",
 "rationale": "<one sentence on the approach>"}"""

FALLBACK_NOTE = """\
NOTE: The play artifact could not be built/hosted this time, so DO NOT include or promise a
link. Draft the same style of reply grounded in the thread + product knowledge, offering to
walk them through the personalized play live instead."""


def play_summary(deck_data):
    if not deck_data:
        return ""
    t = deck_data.get("target") or {}
    signals = "; ".join(f"{s.get('label')}: {s.get('detail')}"
                        for s in (t.get("signals") or [])[:3])
    contacts = ", ".join(f"{c.get('name')} ({c.get('title')})"
                         for c in (t.get("contacts") or [])[:2])
    return (f"Target account: {t.get('name')} — {t.get('blurb')}\n"
            f"Why it cleared their ICP gate: {t.get('gate')}\n"
            f"Signals: {signals}\nResolved buyers: {contacts}")


def stage_draft(client, item, contact, deck_data, publish, fallback):
    log("stage: draft")
    system = DRAFT_SYSTEM + "\n\n# Product knowledge + playbook\n\n" + load_knowledge()
    name = (item or {}).get("from_name") or contact.get("firstName") or "there"
    url_line = ""
    if publish and publish.get("page_url"):
        url_line = f"\nThe live play page (include this exact URL): {publish['page_url']}"
    note = f"\n\n{FALLBACK_NOTE}" if fallback else ""
    user = (f"Prospect: {name}, {contact.get('jobTitle','')} at {contact.get('companyName','')}\n\n"
            f"Conversation so far:\n\"\"\"\n{thread_context(item)}\n\"\"\"\n\n"
            f"The signal play we built for them:\n{play_summary(deck_data)}{url_line}{note}\n\n"
            f"Draft the reply. Return ONLY the JSON.")
    res = client.complete(system, user, use_web_search=False, max_tokens=900, timeout=300)
    data = extract_json(res["text"])
    return {"draft": sanitize_text(str(data.get("draft", "")).strip()),
            "rationale": sanitize_text(str(data.get("rationale", ""))[:300])}


def merge_draft(item, reply_id, draft, play_meta, fallback, error):
    """Insert/replace this reply's draft in followup_drafts.json (the shared store
    the approval UI reads). Same record shape as draft_followups.py."""
    payload = {"generated_at": now_iso(), "items": []}
    if DRAFTS.is_file():
        try:
            payload = json.loads(DRAFTS.read_text())
        except (ValueError, OSError):
            pass
    items = [d for d in payload.get("items") or []
             if str(d.get("reply_id")) != str(reply_id)]
    it = item or {}
    # keep reply_id the same type as queue/draft records (int) — a str-typed copy
    # would escape the str()-based dedup and collide with a later standard draft
    rid = it.get("reply_id")
    if rid is None:
        rid = int(reply_id) if str(reply_id).isdigit() else reply_id
    items.append({
        "reply_id": rid, "lead_id": it.get("lead_id"),
        "channel": it.get("channel", "email"),
        "sender_email_id": it.get("sender_email_id"),
        "linkedin_account_id": it.get("linkedin_account_id"),
        "conversation_id": it.get("conversation_id"),
        "from_name": it.get("from_name"), "from_email": it.get("from_email"),
        "subject": it.get("subject"), "campaign_id": it.get("campaign_id"),
        "original_reply": (it.get("text_body") or "")[:4000],
        "intent": (it.get("classifier") or {}).get("intent") or "",
        "draft": draft.get("draft", ""), "rationale": draft.get("rationale", ""),
        "error": error, "status": "drafted", "drafted_at": now_iso(),
        "agent": "signal-playbook", "fallback": fallback, "play": play_meta,
    })
    payload["items"] = items
    payload["generated_at"] = now_iso()
    DRAFTS.parent.mkdir(parents=True, exist_ok=True)
    tmp = DRAFTS.with_name(DRAFTS.name + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False))
    tmp.replace(DRAFTS)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--reply-id", required=True)
    ap.add_argument("--contact-json", required=True,
                    help='{"firstName","lastName","jobTitle","businessEmail",'
                         '"companyName","companyDomain"}')
    ap.add_argument("--skip-publish", action="store_true",
                    help="build + draft without publishing the HubSpot page (testing)")
    args = ap.parse_args()

    contact = json.loads(args.contact_json)
    item = load_queue_item(args.reply_id)
    slug = slugify(contact.get("companyName") or contact.get("companyDomain"))
    out_dir = PLAYS_DIR / slug
    out_dir.mkdir(parents=True, exist_ok=True)

    try:
        client = AnthropicClient()
    except AnthropicError as e:  # missing/invalid key — emit a clean result, not a stack
        print(json.dumps({"ok": False, "error": str(e)[:400]}))
        return 1
    deck_data, publish, fallback, error = None, None, None, None
    html_out = None

    try:
        research_md = stage_research(client, contact)
        research_md = augment_target_tech(research_md)
        (out_dir / "research.md").write_text(research_md)
        deck_data = stage_deck_data(client, research_md, out_dir)
        html_out = stage_render(out_dir / "deck-data.json",
                                contact.get("companyName"), out_dir)
        if args.skip_publish:
            publish = {"page_id": None, "page_url": "", "url_domain_ok": False,
                       "skipped": True}
        else:
            publish = stage_publish(html_out, contact.get("companyName") or slug, slug)
    except (AnthropicError, AnthropicJSONError, RuntimeError, ValueError,
            subprocess.TimeoutExpired, OSError) as e:
        fallback = "standard"
        error = f"{type(e).__name__}: {e}"[:400]
        log(f"play build failed ({error}); falling back to a standard draft")
    except Exception as e:  # noqa: BLE001 — never die without emitting a result
        fallback = "standard"
        error = f"{type(e).__name__}: {e}"[:400]
        log(f"play build failed unexpectedly ({error}); falling back")

    play_meta = {
        "slug": slug,
        "page_url": (publish or {}).get("page_url"),
        "page_id": (publish or {}).get("page_id"),
        "url_domain_ok": (publish or {}).get("url_domain_ok"),
        "html_path": str(html_out) if html_out else None,
    } if (publish or html_out) else None

    try:
        draft = stage_draft(client, item, contact, deck_data,
                            None if fallback else publish, bool(fallback))
    except (AnthropicError, AnthropicJSONError, ValueError) as e:
        print(json.dumps({"ok": False, "error": f"draft failed: {e}"[:400],
                          "fallback": fallback, "play": play_meta}))
        return 1

    merge_draft(item, args.reply_id, draft, play_meta, fallback, error)
    print(json.dumps({"ok": fallback is None, "reply_id": args.reply_id,
                      "play": play_meta, "draft": draft,
                      "fallback": fallback, "error": error}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
