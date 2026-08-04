"""Campaign brief — configure a campaign by describing it, or by dropping a spec.

The campaign builder asks eleven structured questions (window, signal kinds, hiring
threshold, tech plays, personas, motion, audience, variant, caps, cadence, sender
binding). Every one of them is a translation of a decision someone already made in
words: "we agreed to go back at everyone we lost this quarter, lead with the hiring
angle, and keep it to accounts actually building a sales team." This module does the
translation, so the meeting notes are the input and the form is the output.

Three properties make it safe to let a model drive a configuration screen:

  1. **It proposes, it does not apply.** Every call returns a PATCH for the form.
     The user sees the fields change and still presses Create. Nothing is written
     here — this module never touches the database.
  2. **The vocabulary is closed.** The model emits ids from a fixed vocabulary
     (signal kinds from campaigns.SIGNAL_REGISTRY, audience presets from
     audiences.CRM_PRESETS, CTA keys from the offer library). Anything outside it is
     DROPPED with a warning shown to the user, never coerced into the nearest match:
     a filter quietly changed to something adjacent would target the wrong accounts,
     and the whole point of the screen is that the targeting is inspectable.
  3. **It asks rather than guesses.** A spec that doesn't say which window, or which
     accounts count, comes back as a QUESTION with concrete options instead of a
     confident default. Answers are fed back in on the next call, and each one is
     allowed to move other fields — that is the point of a configurator over a form.

Input can be typed, a dropped spec, or a MEETING TRANSCRIPT — the artifact a
campaign decision actually leaves behind. Transcripts are detected (extension,
WebVTT/SRT markers, or a high density of `Name:` turns), stripped of timecodes and
cue numbers, and labelled as transcripts in the prompt so the model knows to hunt
for the decision inside a conversation rather than read a specification.

Demo mode has no API key, and "this needs an API key" is exactly the not-configured
notice a demo must never show when the thing being demonstrated IS an agent doing
setup. So a demo answers from the profile's `campaign_brief.json` (see
app._demo_campaign_brief): same response shape, one clarifying question first, then
a full configuration whose contents actually depend on the answer given.

Stdlib only apart from a lazy import of the shared Anthropic client (boot rule).
"""

import base64
import io
import json
import os
import re
import sys
import zipfile
from datetime import date, timedelta
from pathlib import Path
from xml.etree import ElementTree

MAX_TEXT = 6000
MAX_ATTACH_CHARS = 40000
MAX_ATTACHMENTS = 3

# Fields the configurator is allowed to set, and nothing else. A key outside this
# set is dropped with a warning rather than passed through — the campaign row has
# columns (status, bison_campaign_id) that must not be settable by a description.
TOP_FIELDS = ("name", "description", "brief", "window_start", "window_end",
              "membership_mode", "variant", "target_accounts",
              "discovery_interval_days")
VALID_VARIANTS = ("value-give", "earn", "show")
VALID_MEMBERSHIP = ("rolling", "snapshot")
VALID_INTERVALS = (0, 7, 14, 30)
# Personas are the four persona agents that can write for a campaign.
VALID_PERSONAS = ("sales-leadership", "revops", "partnerships", "sdr-bdr")

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# ---- transcripts -------------------------------------------------------------
# The most common way a campaign gets decided is a meeting, and the artifact of a
# meeting is a transcript. Pasting one in raw works badly for two reasons: it is
# mostly timecodes and crosstalk, and the model does not know it is reading a
# conversation rather than a spec. So transcripts are DETECTED, normalized, and
# labelled as transcripts in the prompt.
_VTT_TIME = re.compile(
    r"^\s*(\d{1,2}:)?\d{1,2}:\d{2}[.,]\d{1,3}\s*-->\s*"
    r"(\d{1,2}:)?\d{1,2}:\d{2}[.,]\d{1,3}.*$")
_CUE_NUM = re.compile(r"^\s*\d+\s*$")
# "[00:14:02] Alex:" / "00:14 Alex:" / "Alex (14:02):" — the shapes every exporter
# uses. The SPEAKER is kept (who said it is often the whole point: the CRO asking
# for something is different from an SDR musing) and only the clock is dropped.
_LEAD_TS = re.compile(r"^\s*[\[\(]?(?:\d{1,2}:)?\d{1,2}:\d{2}(?:[.,]\d{1,3})?[\]\)]?\s*")
_SPEAKER_TS = re.compile(r"^(?P<who>[^:\n]{1,40}?)\s*[\[\(](?:\d{1,2}:)?\d{1,2}:\d{2}[^\]\)]*[\]\)]\s*:")
_TRANSCRIPT_HINTS = re.compile(
    r"webvtt|-->|\btranscript\b|\bmeeting notes\b|\bspeaker \d", re.I)


def looks_like_transcript(name, text):
    """Cheap detection: extension, WebVTT/SRT markers, or many `Name:` turns.

    Errs toward NOT calling something a transcript — mislabelling a spec as a
    conversation would tell the model to go hunting for a decision that is stated
    plainly in front of it."""
    if re.search(r"\.(vtt|srt)$", name or "", re.I):
        return True
    head = (text or "")[:4000]
    if _TRANSCRIPT_HINTS.search(head):
        return True
    # Count turns on the TIMESTAMP-STRIPPED line. "[00:14:02] Dana: ..." is the
    # commonest export shape there is, and the leading clock contains colons — so
    # matching `Name:` against the raw line scores it as zero turns and the most
    # obvious transcript in the world reads as a spec.
    lines = []
    for l in head.splitlines():
        l = _LEAD_TS.sub("", l.strip())
        if l:
            lines.append(l)
    if len(lines) < 6:
        return False
    speakers = []
    for l in lines:
        m = re.match(r"^([^:]{1,40}):\s+\S", l)
        if m:
            speakers.append(m.group(1).strip().lower())
    if len(speakers) < 4:
        return False
    # `Name:` lines alone are not enough — "Target: closed lost / Window: 90 days"
    # is a spec, and reading it as a conversation would send the model looking for
    # a decision buried in dialogue that is instead stated plainly.
    #
    # What separates them is REPETITION: a conversation has a few speakers taking
    # many turns, a key/value document has one line per distinct key.
    return len(set(speakers)) <= max(2, len(speakers) / 2)


def normalize_transcript(text):
    """Strip the machinery, keep the conversation.

    Timecodes, cue numbers and WEBVTT headers carry no meaning for this task and
    are a large fraction of the tokens in a real export. Consecutive turns by the
    same speaker are merged so a sentence broken across four caption cues reads as
    one sentence."""
    out, last_who = [], None
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line or line.upper().startswith("WEBVTT"):
            continue
        if _VTT_TIME.match(line) or _CUE_NUM.match(line):
            continue
        m = _SPEAKER_TS.match(line)
        if m:
            line = f"{m.group('who').strip()}:{line[m.end():]}"
        else:
            line = _LEAD_TS.sub("", line)
        line = line.strip()
        if not line:
            continue
        who, sep, said = line.partition(":")
        if sep and len(who) <= 40 and said.strip():
            who, said = who.strip(), said.strip()
            if who == last_who and out:
                out[-1] += " " + said        # same speaker, next caption cue
                continue
            last_who = who
            out.append(f"{who}: {said}")
        else:
            if out and last_who:
                out[-1] += " " + line
            else:
                out.append(line)
                last_who = None
    return "\n".join(out)


def _docx_text(raw):
    """Plain text from a .docx — zip + XML, no python-docx (stdlib-only rule).

    Transcripts get mailed around as Word documents more often than anyone would
    like, and refusing them would push the user back to copy-paste."""
    with zipfile.ZipFile(io.BytesIO(raw)) as zf:
        if "word/document.xml" not in zf.namelist():
            raise ValueError("not a Word document")
        root = ElementTree.fromstring(zf.read("word/document.xml"))
    ns = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    paras = []
    for para in root.iter(f"{ns}p"):
        txt = "".join(t.text or "" for t in para.iter(f"{ns}t"))
        if txt.strip():
            paras.append(txt.strip())
    return "\n".join(paras)


def read_attachment(a):
    """{name, text, kind} from an attachment, whichever way it arrived.

    Accepts `text` (the browser read a text file, or the user pasted) or
    `content_b64` (anything binary, i.e. .docx). One decode path here rather than
    two in the client."""
    name = str(a.get("name") or "attachment")[:120]
    text = a.get("text")
    if not text and a.get("content_b64"):
        try:
            raw = base64.b64decode(a["content_b64"], validate=False)
        except Exception:  # noqa: BLE001
            raise ValueError(f"could not decode {name}")
        if re.search(r"\.docx$", name, re.I):
            try:
                text = _docx_text(raw)
            except Exception as e:  # noqa: BLE001
                raise ValueError(f"could not read {name} as a Word document: {e}")
        else:
            for enc in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
                try:
                    text = raw.decode(enc)
                    break
                except UnicodeDecodeError:
                    continue
            if text is None:
                raise ValueError(f"{name} isn't readable as text")
    text = str(text or "")
    if not text.strip():
        raise ValueError(f"{name} contained no readable text")
    kind = "spec"
    if looks_like_transcript(name, text):
        kind = "transcript"
        text = normalize_transcript(text)
    if len(text) > MAX_ATTACH_CHARS:
        # Keep the START: a meeting states what it is about before it wanders, and
        # the decision is usually made before the small talk at the end.
        text = text[:MAX_ATTACH_CHARS] + "\n…[truncated]"
    return {"name": name, "text": text, "kind": kind}


def available():
    return bool((os.environ.get("ANTHROPIC_API_KEY") or "").strip())


# ---- vocabulary ------------------------------------------------------------
def vocabulary(conn=None):
    """Everything the model may choose from, assembled from the real registries.

    Generated rather than written out, so a signal kind added to SIGNAL_REGISTRY or
    an offer added to the CTA library becomes describable with no change here."""
    import campaigns as C
    import audiences as A
    vocab = {
        # The LIVE registry: a kind this deployment defined is describable in a
        # brief the moment it exists, without touching this file.
        "signal_kinds": [{"id": k, "label": v["label"], "what": v.get("description")}
                         for k, v in C.signal_registry(conn).items()
                         if v.get("active", True)],
        "tech_playbook": [
            {"id": "sequencing", "what": "they run Outreach / Salesloft / Apollo"},
            {"id": "intent_abm", "what": "they run an intent or ABM platform"},
            {"id": "ads", "what": "ad pixels — they are spending on paid"},
        ],
        "personas": list(VALID_PERSONAS),
        "motions": list(C.VALID_MOTIONS),
        "membership_modes": list(VALID_MEMBERSHIP),
        "variants": list(VALID_VARIANTS),
        "audience_types": list(A.AUDIENCE_TYPES),
        "audience_presets": [{"id": k, "label": v["label"], "what": v["description"]}
                             for k, v in A.CRM_PRESETS.items()],
        "cta_keys": [],
    }
    if conn is not None:
        try:
            import batch_db as db
            vocab["cta_keys"] = [{"key": c["cta_key"], "label": c["label"],
                                  "give": c.get("give")}
                                 for c in db.list_ctas(conn, active_only=False)]
        except Exception:  # noqa: BLE001 — the offer library is optional context
            pass
    return vocab


SYSTEM = """You configure outbound campaigns for EverWorker's SDR AI Worker console.

You are given a description of a campaign — often the notes from a meeting, or a
spec document — and the exact vocabulary the console accepts. Your job is to turn it
into a concrete campaign configuration, and to ASK when the description leaves
something genuinely undecided.

Rules:
- Use ONLY ids from the supplied vocabulary. Never invent a signal kind, persona,
  audience preset or CTA key. If the description asks for something the vocabulary
  cannot express, leave the field out and say so in `notes`.
- Ask a question when a decision would materially change WHO gets contacted and the
  description does not settle it — the target window, which accounts count, whether
  inbound-sourced contacts are in scope. Do not ask about things you can reasonably
  infer, and do not ask more than 2 questions at once. Each option must carry the
  `config` it would apply, so choosing it visibly changes the form.
- Prefer NOT asking when a sensible default exists; say what you assumed in `notes`.
- `brief` is the part that has no field: the argument this campaign is making and
  the framing agreed for it, in 2-5 sentences, written as direction to the copy
  writer. Ground it in what the user said. Never invent product claims or numbers.
- `name` is short and human (under 60 chars). `description` is one line.
- Dates are ISO yyyy-mm-dd.

Return ONLY this JSON:
{
  "summary": "<2-3 sentences: what you configured and why>",
  "config": {
    "name": "...", "description": "...", "brief": "...",
    "window_start": "yyyy-mm-dd", "window_end": "yyyy-mm-dd",
    "membership_mode": "rolling|snapshot",
    "discovery_interval_days": 0|7|14|30,
    "variant": "value-give|earn|show",
    "target_accounts": <int or null>,
    "signal_query": {
      "kinds": ["..."], "require_recent": true|false,
      "hiring_sales_min": <int or null>, "tech_playbook": ["..."],
      "personas": ["..."], "motion": "outbound|inbound|any"
    },
    "audience": {"type": "all_contacts"}
                | {"type": "hubspot_list", "list_id": "..."}
                | {"type": "crm_query", "preset": "...", "days": <int>}
  },
  "questions": [
    {"id": "<short slug>", "question": "<one question>",
     "why": "<one line: what it changes>",
     "options": [{"label": "<short>", "detail": "<one line>",
                  "config": {<the same shape as config, partial>}}]}
  ],
  "notes": ["<assumption or unsupported request>"]
}
Every key in `config` is optional — omit what the description does not decide."""


# ---- validation ------------------------------------------------------------
def _clean_date(v, warnings, field):
    if v in (None, ""):
        return None
    s = str(v).strip()
    if not _DATE_RE.match(s):
        warnings.append(f"ignored {field}={v!r} — not an ISO yyyy-mm-dd date")
        return None
    return s


def _clean_int(v, warnings, field, allowed=None):
    if v in (None, ""):
        return None
    try:
        n = int(v)
    except (TypeError, ValueError):
        warnings.append(f"ignored {field}={v!r} — not a number")
        return None
    if allowed is not None and n not in allowed:
        warnings.append(f"ignored {field}={n} — must be one of {list(allowed)}")
        return None
    return n


def _clean_choice(v, allowed, warnings, field):
    if v in (None, ""):
        return None
    s = str(v).strip()
    if s not in allowed:
        warnings.append(f"ignored {field}={s!r} — not one of {list(allowed)}")
        return None
    return s


def _clean_subset(v, allowed, warnings, field):
    """Known ids kept, unknown ones dropped INDIVIDUALLY with a warning.

    Dropping the bad entry rather than the whole list keeps a mostly-right proposal
    usable; dropping it loudly is what stops a silently-narrowed filter."""
    if v is None:
        return None
    if not isinstance(v, list):
        warnings.append(f"ignored {field} — expected a list")
        return None
    out = []
    for item in v:
        s = str(item).strip()
        if s in allowed:
            out.append(s)
        else:
            warnings.append(f"ignored {field} value {s!r} — not a known option")
    return out


def validate_config(raw, conn=None):
    """(config, warnings) — the subset of `raw` the console will actually accept.

    Rejects rather than repairs. Every dropped value is reported so the UI can show
    what the model asked for and did not get; a configuration screen that silently
    discarded half a spec would be worse than one that refused it outright.
    """
    import campaigns as C
    import audiences as A

    raw = raw if isinstance(raw, dict) else {}
    warnings = []
    out = {}

    for k in ("name", "description", "brief"):
        v = raw.get(k)
        if isinstance(v, str) and v.strip():
            out[k] = v.strip()[:4000]

    for k in ("window_start", "window_end"):
        v = _clean_date(raw.get(k), warnings, k)
        if v:
            out[k] = v
    if out.get("window_start") and out.get("window_end") \
            and out["window_start"] > out["window_end"]:
        warnings.append("window_start was after window_end — the dates were swapped")
        out["window_start"], out["window_end"] = out["window_end"], out["window_start"]

    v = _clean_choice(raw.get("membership_mode"), VALID_MEMBERSHIP, warnings, "membership_mode")
    if v:
        out["membership_mode"] = v
    v = _clean_choice(raw.get("variant"), VALID_VARIANTS, warnings, "variant")
    if v:
        out["variant"] = v
    v = _clean_int(raw.get("discovery_interval_days"), warnings,
                   "discovery_interval_days", VALID_INTERVALS)
    if v is not None:
        out["discovery_interval_days"] = v
    v = _clean_int(raw.get("target_accounts"), warnings, "target_accounts")
    if v is not None and v > 0:
        out["target_accounts"] = v

    sq_raw = raw.get("signal_query")
    if isinstance(sq_raw, dict):
        sq = {}
        kinds = _clean_subset(sq_raw.get("kinds"), set(C.known_kinds(conn)), warnings,
                              "signal_query.kinds")
        if kinds:
            sq["kinds"] = kinds
        pb = _clean_subset(sq_raw.get("tech_playbook"), set(C.VALID_PLAYBOOK), warnings,
                           "signal_query.tech_playbook")
        if pb is not None:
            sq["tech_playbook"] = pb
        personas = _clean_subset(sq_raw.get("personas"), set(VALID_PERSONAS), warnings,
                                 "signal_query.personas")
        if personas is not None:
            sq["personas"] = personas
        motion = _clean_choice(sq_raw.get("motion"), C.VALID_MOTIONS, warnings,
                               "signal_query.motion")
        if motion:
            sq["motion"] = motion
        if "require_recent" in sq_raw:
            sq["require_recent"] = bool(sq_raw["require_recent"])
        hsm = _clean_int(sq_raw.get("hiring_sales_min"), warnings,
                         "signal_query.hiring_sales_min")
        if hsm is not None and hsm >= 0:
            sq["hiring_sales_min"] = hsm
        if sq:
            # Final gate: the same validator the API uses, so a config that survives
            # here cannot fail on submit.
            try:
                C.validate_signal_query(
                    {**sq, "kinds": sq.get("kinds") or list(C.known_kinds(conn))}, conn)
                out["signal_query"] = sq
            except ValueError as e:
                warnings.append(f"dropped the signal filter — {e}")

    aud_raw = raw.get("audience")
    if isinstance(aud_raw, dict):
        try:
            out["audience"] = A.validate_audience(aud_raw)
        except ValueError as e:
            warnings.append(f"dropped the audience — {e}")

    unknown = set(raw) - set(TOP_FIELDS) - {"signal_query", "audience"}
    for k in sorted(unknown):
        warnings.append(f"ignored {k!r} — not a field this screen sets")
    return out, warnings


def validate_questions(raw, conn=None):
    """Questions, each option carrying a validated config overlay.

    An option whose overlay is entirely invalid is dropped: an option that changes
    nothing when clicked is worse than one fewer option."""
    out = []
    for q in (raw or [])[:2]:
        if not isinstance(q, dict) or not str(q.get("question") or "").strip():
            continue
        options = []
        for o in (q.get("options") or [])[:4]:
            if not isinstance(o, dict):
                continue
            cfg, _w = validate_config(o.get("config"), conn)
            label = str(o.get("label") or "").strip()
            if not label:
                continue
            options.append({"label": label[:80],
                            "detail": str(o.get("detail") or "").strip()[:200],
                            "config": cfg})
        out.append({
            "id": str(q.get("id") or f"q{len(out) + 1}")[:40],
            "question": str(q["question"]).strip()[:300],
            "why": str(q.get("why") or "").strip()[:200],
            "options": options,
        })
    return out


# ---- the call --------------------------------------------------------------
def default_window():
    """A sane starting window when the description doesn't name one: the last 30
    days of signal, open for the next 30. Stated in `notes` when used."""
    today = date.today()
    return ((today - timedelta(days=30)).isoformat(),
            (today + timedelta(days=30)).isoformat())


def propose(project_root, body, conn=None):
    """Turn a description (+ optional spec files, + answers to prior questions) into
    a validated config patch and any remaining questions. Writes nothing.

    Raises ValueError for bad input and RuntimeError when the model is unavailable
    or unusable — surfaced as 400 and 501/502 respectively."""
    text = str(body.get("text") or "").strip()
    attachments = body.get("attachments") or []
    answers = body.get("answers") or {}
    current = body.get("current") or {}

    if not text and not attachments:
        raise ValueError("describe the campaign, or attach a spec to work from")
    if len(text) > MAX_TEXT:
        raise ValueError(f"description is too long (max {MAX_TEXT} chars)")
    if len(attachments) > MAX_ATTACHMENTS:
        raise ValueError(f"at most {MAX_ATTACHMENTS} attachments per request")
    if not available():
        raise RuntimeError("ANTHROPIC_API_KEY is not set")

    att_blocks, kinds = [], set()
    for a in attachments:
        item = read_attachment(a)
        kinds.add(item["kind"])
        tag = "transcript" if item["kind"] == "transcript" else "spec"
        att_blocks.append(f"<{tag} name=\"{item['name']}\">\n{item['text']}\n</{tag}>")
    # A pasted transcript arrives in `text`, not as a file, and reads identically to
    # the model — so it gets the same handling.
    pasted_transcript = looks_like_transcript("", text)
    if pasted_transcript:
        text = normalize_transcript(text)
        kinds.add("transcript")

    ws, we = default_window()
    transcript_note = ("\n<reading_a_transcript>\nSome of the input is a MEETING "
                       "TRANSCRIPT, not a spec. Extract the campaign decision from "
                       "the conversation and ignore everything else — scheduling, "
                       "greetings, tangents, anything discussed and then dropped. "
                       "Where the room disagreed, follow whoever actually decided, "
                       "and say in `notes` what you took as the decision. If the "
                       "conversation genuinely never settled something, ASK about "
                       "it rather than picking a side.\n</reading_a_transcript>\n"
                       if "transcript" in kinds else "")
    user = (
        f"<vocabulary>\n{json.dumps(vocabulary(conn), indent=1)}\n</vocabulary>\n\n"
        f"<today>{date.today().isoformat()}</today>\n"
        f"<default_window start=\"{ws}\" end=\"{we}\" />\n"
        + transcript_note + "\n"
        + ("\n\n".join(att_blocks) + "\n\n" if att_blocks else "")
        + (f"<current_form>\n{json.dumps(current, indent=1)}\n</current_form>\n\n"
           if current else "")
        + (f"<answers_to_your_questions>\n{json.dumps(answers, indent=1)}\n"
           "</answers_to_your_questions>\n\n" if answers else "")
        + f"<request>\n{text or 'Configure a campaign from the attached material.'}\n</request>"
    )

    client_mod = _anthropic(project_root)
    try:
        client = client_mod.AnthropicClient()
    except Exception as e:  # noqa: BLE001 — surfaced as a 501
        raise RuntimeError(str(e))
    res = client.complete(SYSTEM, user, max_tokens=4000, timeout=180)
    try:
        parsed = client_mod.extract_json(res["text"])
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(f"the model did not return usable JSON: {e}")

    config, warnings = validate_config(parsed.get("config"), conn)
    notes = [str(n).strip() for n in (parsed.get("notes") or []) if str(n).strip()]
    return {
        "summary": str(parsed.get("summary") or "").strip(),
        "config": config,
        "questions": validate_questions(parsed.get("questions"), conn),
        "notes": notes[:6],
        "warnings": warnings,
        "usage": res.get("usage", {}),
    }


def _anthropic(project_root):
    """Lazy import — the server must boot with no API key and no client present."""
    scripts = Path(project_root) / ".claude" / "skills" / "ai-sdr" / "scripts"
    if str(scripts) not in sys.path:
        sys.path.insert(0, str(scripts))
    import anthropic_client
    return anthropic_client
