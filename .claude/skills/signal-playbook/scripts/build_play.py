"""Signal Playbook Reply Agent — build a personalized signal play for an
interested reply's lead and draft the follow-up email around it.

Stages (each announced as "stage: <name>" on stderr so the webui job can track
progress; the final JSON result is stdout's last line):

  research   Web-search research on the lead's company -> research.md
             (the company-researcher agent spec, run as one Messages API call)
  deck-data  research.md -> schema-valid deck-data.json (+ one repair round-trip
             against the node validator)
  render     deck-data.json -> single-file HTML + 4-page PDF via the vendored
             deck-renderer (serialized: the renderer has one shared fill file)
  upload     PDF -> HubSpot File Manager (public URL; flags url_domain_ok=false
             when the portal serves it from a non-everworker.ai host)
  draft      A contextualized reply email embedding the URL, merged into
             followup_drafts.json for the normal edit-before-send approval flow

Any render/upload failure still produces a draft (fallback="standard", no link),
so the SDR always gets something to edit and send.

  python3 build_play.py --reply-id <id> --contact-json '{"firstName":...}'
"""

import argparse
import fcntl
import json
import re
import shutil
import subprocess
import sys
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
- For technographic signals, reason from public evidence (job postings, site markup, docs,
  partner pages) — no detector tool is available here.
- Wrap the punchiest metric/phrase of offering bullets and outreach drafts in ==double-equals==.
- Respect every length ceiling noted in the template — the deck has fixed-size cards.

Produce ONE markdown document following the template below EXACTLY — same headings, same
order. Return ONLY the markdown document, no preamble.
"""

def stage_research(client, contact):
    log("stage: research")
    template = TEMPLATE.read_text()
    user = (
        f"Input contact profile:\n"
        f"- Name: {contact.get('firstName','')} {contact.get('lastName','')}\n"
        f"- Job title: {contact.get('jobTitle','')}\n"
        f"- Business email: {contact.get('businessEmail','')}\n"
        f"- Prospect company: {contact.get('companyName','')}\n"
        f"- Company domain: {contact.get('companyDomain','')}\n"
        f"- Today's date: {datetime.now(timezone.utc).strftime('%Y-%m-%d')}\n\n"
        f"Research the prospect and produce the completed research file."
    )
    res = client.complete(RESEARCH_SYSTEM + "\n# TEMPLATE\n\n" + template, user,
                          use_web_search=True, max_web_searches=10,
                          max_tokens=8000, timeout=1500)
    text = (res.get("text") or "").strip()
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
- You cannot count characters precisely — target roughly 85-90% of every maxLength budget so
  you land safely under, and prefer cutting a whole clause over trimming single words.
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


def _clamp_string(s, limit):
    """Shorten to <= limit at a word boundary (never below ~60% of the budget),
    keeping ==highlight== markers balanced."""
    while True:
        cut = s[:limit]
        sp = cut.rfind(" ")
        if sp >= int(limit * 0.6):
            cut = cut[:sp]
        cut = cut.rstrip(" ,;:-—").rstrip()
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
    data = extract_json(res["text"])
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
        data = extract_json(repair["text"])
        ok, report, data = _validate_with_clamp(data, data_path)
        if not ok:
            raise RuntimeError(f"deck-data failed validation after repair: {report[:500]}")
    return data


# ---------------------------------------------------------------------------
# Stage 3 — render (serialized: the renderer has ONE shared fill file)
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
        pdf_out = out_dir / f"{safe}-AI-SDR-Playbook.pdf"
        shutil.copyfile(EXPORT_DIR / "Bites-AI-SDR-Playbook.html", html_out)
        shutil.copyfile(EXPORT_DIR / "Bites-AI-SDR-Playbook.pdf", pdf_out)
    log(f"rendered {pdf_out.name}")
    return html_out, pdf_out


# ---------------------------------------------------------------------------
# Stage 4 — upload to HubSpot File Manager
# ---------------------------------------------------------------------------
def stage_upload(pdf_path):
    log("stage: upload")
    from hubspot_client import HubSpotClient  # noqa: E402 — needs HUBSPOT_ACCESS_TOKEN
    hs = HubSpotClient()
    up = hs.upload_file(pdf_path, folder_path="signal-plays", access="PUBLIC_INDEXABLE")
    url = up.get("url") or ""
    host = re.sub(r"^https?://", "", url).split("/")[0].lower()
    domain_ok = host == REQUIRED_URL_SUFFIX or host.endswith("." + REQUIRED_URL_SUFFIX)
    if not domain_ok:
        log(f"warning: file URL host {host!r} is not on {REQUIRED_URL_SUFFIX} — connect a "
            f"file-hosting domain in HubSpot (Settings -> Content -> Domains & URLs)")
    return {"file_id": up.get("id"), "pdf_url": url, "url_domain_ok": domain_ok}


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


def stage_draft(client, item, contact, deck_data, upload, fallback):
    log("stage: draft")
    system = DRAFT_SYSTEM + "\n\n# Product knowledge + playbook\n\n" + load_knowledge()
    name = (item or {}).get("from_name") or contact.get("firstName") or "there"
    url_line = ""
    if upload and upload.get("pdf_url"):
        url_line = f"\nThe hosted play (include this exact URL): {upload['pdf_url']}"
    note = f"\n\n{FALLBACK_NOTE}" if fallback else ""
    user = (f"Prospect: {name}, {contact.get('jobTitle','')} at {contact.get('companyName','')}\n\n"
            f"Conversation so far:\n\"\"\"\n{thread_context(item)}\n\"\"\"\n\n"
            f"The signal play we built for them:\n{play_summary(deck_data)}{url_line}{note}\n\n"
            f"Draft the reply. Return ONLY the JSON.")
    res = client.complete(system, user, use_web_search=False, max_tokens=900, timeout=300)
    data = extract_json(res["text"])
    return {"draft": str(data.get("draft", "")).strip(),
            "rationale": str(data.get("rationale", ""))[:300]}


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
    ap.add_argument("--skip-upload", action="store_true",
                    help="build + draft without the HubSpot upload (testing)")
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
    deck_data, upload, fallback, error = None, None, None, None
    html_out = pdf_out = None

    try:
        research_md = stage_research(client, contact)
        (out_dir / "research.md").write_text(research_md)
        deck_data = stage_deck_data(client, research_md, out_dir)
        html_out, pdf_out = stage_render(out_dir / "deck-data.json",
                                         contact.get("companyName"), out_dir)
        if args.skip_upload:
            upload = {"file_id": None, "pdf_url": "", "url_domain_ok": False,
                      "skipped": True}
        else:
            upload = stage_upload(pdf_out)
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
        "pdf_url": (upload or {}).get("pdf_url"),
        "file_id": (upload or {}).get("file_id"),
        "url_domain_ok": (upload or {}).get("url_domain_ok"),
        "html_path": str(html_out) if html_out else None,
        "pdf_path": str(pdf_out) if pdf_out else None,
    } if (upload or html_out) else None

    try:
        draft = stage_draft(client, item, contact, deck_data,
                            None if fallback else upload, bool(fallback))
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
