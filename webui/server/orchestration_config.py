"""Live "under the hood" config for the Orchestration view.

Assembles GET /api/orchestration/config from the pipeline's REAL sources —
agent markdown, knowledge base, buyer_group.py, hiring/tech constant sets, the
technographic selection JSON — so the page always shows what the pipeline
actually runs (no hand-maintained copy to drift).

Contract: `orchestration_config_payload()` NEVER raises. Each section is built
independently; a failed section comes back null with its message under
`errors[<section>]` and the rest still render. All parsing is stdlib; the
pipeline modules it imports (tech_signals, hiring_signals, generate_batch,
buyer_group, lint_sequence) are stdlib-only at import time per the repo's boot
rule, and every import here is wrapped anyway.
"""

import json
import os
import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SKILLS = PROJECT_ROOT / ".claude" / "skills"
AGENTS_DIR = PROJECT_ROOT / ".claude" / "agents"
KNOWLEDGE_DIR = SKILLS / "ai-sdr" / "knowledge"
SIGNATURES_DIR = PROJECT_ROOT / "technographics" / "signatures"
PIPELINE_SCRIPTS = SKILLS / "sdr-pipeline" / "scripts"
AISDR_SCRIPTS = SKILLS / "ai-sdr" / "scripts"

for p in (str(PIPELINE_SCRIPTS), str(AISDR_SCRIPTS)):
    if p not in sys.path:
        sys.path.insert(0, p)

# persona id (contacts.persona / PERSONA_COLORS key) -> agent markdown file
PERSONA_AGENT_FILES = {
    "sales-leadership": "sdr-sales-leadership.md",
    "revops": "sdr-revops.md",
    "partnerships": "sdr-partnerships.md",
    "sdr-bdr": "sdr-sdr-bdr-leadership.md",
}

ACRONYMS = {"cro": "CRO", "cso": "CSO", "cco": "CCO", "cmo": "CMO", "ceo": "CEO",
            "vp": "VP", "evp": "EVP", "svp": "SVP", "sdr": "SDR", "bdr": "BDR",
            "ae": "AE", "gtm": "GTM", "smb": "SMB", "revops": "RevOps",
            "salesops": "SalesOps", "ic": "IC", "bd": "BD", "dir": "Dir"}


# ---- generic parsing helpers -------------------------------------------------
def _md_sections(text):
    """{heading-without-##: body} split on top-level '## ' headings."""
    sections, current, buf = {}, None, []
    for line in text.splitlines():
        if line.startswith("## "):
            if current is not None:
                sections[current] = "\n".join(buf).strip()
            current, buf = line[3:].strip(), []
        elif current is not None:
            buf.append(line)
    if current is not None:
        sections[current] = "\n".join(buf).strip()
    return sections


def _section(sections, prefix):
    for heading, body in sections.items():
        if heading.lower().startswith(prefix.lower()):
            return body
    return ""


def _strip_md(text):
    """Markdown inline markup -> plain text (bold/italic/backticks/links)."""
    text = re.sub(r"\*\*([^*]*)\*\*", r"\1", text)
    text = re.sub(r"\*([^*]*)\*", r"\1", text)
    text = text.replace("`", "")
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)
    return re.sub(r"[ \t]+", " ", text).strip()


def _bullets(body):
    """Top-level '- ' items with continuation lines joined."""
    items = []
    for line in body.splitlines():
        if re.match(r"^- ", line):
            items.append(line[2:].strip())
        elif items and re.match(r"^\s+\S", line) and not re.match(r"^\s+- ", line):
            items[-1] += " " + line.strip()
    return items


def _gfm_table(body):
    """First GFM table in body -> list of {header_lower: cell}."""
    rows, headers = [], None
    for line in body.splitlines():
        line = line.strip()
        if not line.startswith("|"):
            if headers:
                break
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if headers is None:
            headers = [c.lower() for c in cells]
        elif all(re.fullmatch(r":?-+:?", c) for c in cells):
            continue
        else:
            rows.append(dict(zip(headers, [_strip_md(c) for c in cells])))
    return rows


def _frontmatter(text):
    """Flat 'key: value' dict from a leading --- YAML block."""
    m = re.match(r"\A---\n(.*?)\n---\n", text, re.S)
    fm = {}
    if m:
        for line in m.group(1).splitlines():
            if ":" in line:
                k, _, v = line.partition(":")
                fm[k.strip()] = v.strip()
    return fm


def _humanize_regex(rx):
    """One readable chip per top-level alternative of a regex, plus the raw
    pattern. Best-effort cleanup for display; `raw` is always exact."""
    # split on top-level | (respect parens)
    parts, depth, buf = [], 0, ""
    for ch in rx:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch == "|" and depth == 0:
            parts.append(buf)
            buf = ""
        else:
            buf += ch
    parts.append(buf)

    chips = []
    for p in parts:
        p = p.replace(r"\b", "").replace("(?:", "(")
        # whitespace classes BEFORE the optional-s handling ("\s?" contains "s?")
        p = p.replace(r"[\s-]?", " ").replace(r"\s?", " ").replace(r"\s+", " ").replace(r"\s", " ")
        p = re.sub(r"\.\{0,\d+\}", " ", p)
        p = re.sub(r"\(([^()|]*\|[^()]*)\)", lambda m: m.group(1).replace("|", "/"), p)
        p = p.replace("(s)?", "(s)").replace("s?", "(s)")
        p = p.replace("-?", "-").replace("\\", "")
        words = []
        for w in re.split(r"\s+", p.strip()):
            if not w:
                continue
            core = w.strip("()")
            if "/" in w:
                words.append("/".join(ACRONYMS.get(x.lower(), x.capitalize()) for x in w.split("/")))
            elif core.lower() in ACRONYMS:
                words.append(w.replace(core, ACRONYMS[core.lower()]))
            else:
                words.append(w[0].upper() + w[1:] if w and w[0].isalpha() else w)
        chip = " ".join(words)
        # re-capitalize hyphenated words ("Mid-market" -> "Mid-Market")
        chip = re.sub(r"-([a-z])", lambda m: "-" + m.group(1).upper(), chip)
        if chip:
            chips.append(chip)
    return {"humanized": chips, "raw": rx}


def _prettify_vendor(vid):
    return " ".join(ACRONYMS.get(w, w.capitalize()) for w in vid.split("_"))


def _first_sentence(text, cap=300):
    m = re.search(r"\.(?:\s|$)", text)
    out = text[: m.end()].strip() if m else text
    return out[:cap].rstrip()


# ---- sections ----------------------------------------------------------------
def _pipeline_section(root):
    text = (root / ".claude" / "skills" / "sdr-pipeline" / "SKILL.md").read_text()
    body = _section(_md_sections(text), "Run (autonomous)")
    stages, agent_by_persona = [], {}
    current = None
    for line in body.splitlines():
        m = re.match(r"^(\d+)\.\s+\*\*(.+?):?\*\*:?\s*(.*)", line)
        if m:
            current = {"n": int(m.group(1)), "name": m.group(2).rstrip(":"),
                       "detail": _strip_md(m.group(3))}
            stages.append(current)
            continue
        pm = re.match(r"^\s+-\s+`([\w-]+)`\s+→\s+\*\*([\w-]+)\*\*", line)
        if pm:
            agent_by_persona[pm.group(1)] = pm.group(2)
            continue
        if current and re.match(r"^\s+\S", line):
            current["detail"] = (current["detail"] + " " + _strip_md(line)).strip()
    for s in stages:
        s["detail"] = _first_sentence(s["detail"], cap=240)
    return {"stages": stages, "agent_by_persona": agent_by_persona}


def _icp_filter_section(root):
    import buyer_group as bg
    persona = bg._PERSONA_BY_ROLE
    # mirrors buyer_role()'s 7-step precedence; regexes/personas read live
    steps = [
        ("CRO / Sales Chief", bg._CHIEF_REV, True, None),
        ("SDR/BDR", bg._SDR_BDR, True, None),
        ("RevOps/Sales Ops", bg._REVOPS, True, None),
        ("Partnerships", bg._PARTNERSHIPS, True, None),
        ("Sales / Revenue / GTM / BD", bg._SALES_FUNC, True,
         "leadership titles → VP/Head/Dir Sales-GTM; the rest → Sales/BD IC & Ops"),
        ("Marketing", bg._MKTG_FUNC, True,
         "ICP only at leadership level (owns pipeline/SDRs); no persona yet → skipped"),
        ("Founder/CEO", bg._FOUNDER, bool(bg.FOUNDERS_ARE_ICP),
         "not ICP by the buyer-group definition (GTM leadership only)"),
    ]
    role_persona = {
        "CRO / Sales Chief": persona.get("CRO / Sales Chief"),
        "SDR/BDR": persona.get("SDR/BDR"),
        "RevOps/Sales Ops": persona.get("RevOps/Sales Ops"),
        "Partnerships": persona.get("Partnerships"),
        "Sales / Revenue / GTM / BD": persona.get("VP/Head/Dir Sales-GTM"),
        "Marketing": persona.get("Marketing-pipeline"),
        "Founder/CEO": persona.get("Founder/CEO"),
    }
    doc = (bg.__doc__ or "").strip().split("\n\n")[1] if bg.__doc__ else ""
    return {
        "definition": re.sub(r"\s+", " ", doc).strip(),
        "founders_are_icp": bool(bg.FOUNDERS_ARE_ICP),
        "fallthrough": "any title matching none of these is NOT-ICP and is dropped at pull time",
        "roles": [{"order": i + 1, "role": role, "icp": icp,
                   "persona": role_persona.get(role), "note": note,
                   "patterns": _humanize_regex(rx.pattern)}
                  for i, (role, rx, icp, note) in enumerate(steps)],
    }


def _personas_section(root):
    agents_dir = root / ".claude" / "agents"
    out = []
    for pid, fname in PERSONA_AGENT_FILES.items():
        text = (agents_dir / fname).read_text()
        fm = _frontmatter(text)
        sections = _md_sections(re.sub(r"\A---\n.*?\n---\n", "", text, flags=re.S))
        framing_heading = next((h for h in sections if h.startswith("Persona framing")), "")
        name = framing_heading[len("Persona framing"):].strip(" ()") or pid
        fields = {"pain": "", "outcome": "", "ctas": "", "tone": ""}
        for item in _bullets(sections.get(framing_heading, "")):
            m = re.match(r"(Pain|Outcome to sell|Preferred CTAs|Tone)\b\s*(.*)", _strip_md(item))
            if not m:
                continue
            label, rest = m.group(1), m.group(2)
            rest = re.sub(r"^\([^)]*\)", "", rest).lstrip(" :")  # drop the CTAs parenthetical
            key = {"Pain": "pain", "Outcome to sell": "outcome",
                   "Preferred CTAs": "ctas", "Tone": "tone"}[label]
            fields[key] = rest
        out.append({"id": pid, "agent": fm.get("name", fname[:-3]), "name": name,
                    "description": fm.get("description", ""), **fields})
    return out


def _sequencing_section(root):
    icp = _md_sections((root / ".claude" / "skills" / "ai-sdr" / "knowledge" / "icp-email.md").read_text())
    cta = _md_sections((root / ".claude" / "skills" / "ai-sdr" / "knowledge" / "cta-offers.md").read_text())

    def _tier_items(body):
        items, current = [], None
        for line in body.splitlines():
            m = re.match(r"^(\d+)\.\s+\*\*(.+?)\*\*\s+—\s+(.*)", line)
            if m:
                current = {"n": int(m.group(1)), "name": m.group(2), "_rest": m.group(3)}
                items.append(current)
            elif current and re.match(r"^\s+\S", line):
                current["_rest"] += " " + line.strip()
        for it in items:
            rest = it.pop("_rest")
            q = re.search(r'\*"(.+?)"\*', rest)
            it["cta"] = _strip_md(q.group(1)) if q else _strip_md(rest)
            trail = rest[q.end():] if q else ""
            it["note"] = _strip_md(trail).strip(" ()") if trail.strip() else ""
        return items

    library_body = _section(cta, "The library")
    tier_a = tier_b = anti = ""
    for chunk in re.split(r"^### ", library_body + "\n### " + _section(cta, "zzz"), flags=re.M):
        if chunk.startswith("Tier A"):
            tier_a = chunk
        elif chunk.startswith("Tier B"):
            tier_b = chunk
        elif chunk.startswith("Anti-patterns"):
            anti = chunk

    linkedin = []
    try:
        import generate_batch as G
        for key, desc in re.findall(r'"(li_\w+)":\s*"([^"]*)"', G.OUTPUT_SCHEMA):
            linkedin.append({"key": key, "desc": desc})
        variants = {"default": G.DEFAULT_VARIANT, "list": []}
        src = (root / ".claude" / "skills" / "sdr-pipeline" / "scripts" / "generate_batch.py").read_text()
        for vid in G.WRITE_RULES:
            m = re.search(r"((?:^\s*#[^\n]*\n)+)\s*\"" + re.escape(vid) + r"\": \"\"\"", src, re.M)
            summary = " ".join(l.strip("# ").strip() for l in m.group(1).splitlines()) if m else \
                G.WRITE_RULES[vid].splitlines()[0].lstrip("# ").strip()
            variants["list"].append({"id": vid, "summary": summary})
    except Exception:
        variants = None

    return {
        "four_touch": [{"step": r.get("step", ""), "job": r.get("job", ""), "cta": r.get("cta", "")}
                       for r in _gfm_table(_section(icp, "The 4-touch structure"))],
        "linkedin_touches": linkedin,
        "cadence": [_strip_md(b) for b in _bullets(_section(cta, "Cadence placement"))],
        "cta_library": {"tier_a": _tier_items(tier_a), "tier_b": _tier_items(tier_b),
                        "anti_patterns": [_strip_md(b) for b in _bullets(anti)]},
        "variants": variants,
    }


def _knowledge_section(root):
    offer = _md_sections((root / ".claude" / "skills" / "ai-sdr" / "knowledge" / "offer.md").read_text())
    return {
        "one_liner": _strip_md(re.sub(r"\s+", " ", _section(offer, "One-liner"))),
        "proof": [_first_sentence(_strip_md(b)) for b in _bullets(_section(offer, "Proof"))],
        "files": ["offer.md", "cta-offers.md", "icp-email.md", "examples/icp-email-sequence.md"],
    }


LINT_LABELS = [
    ("GIVE", "CTA must lead with a deliverable give"),
    ("MEETING", "CTA must ask for a meeting"),
    ("FORBIDDEN_CTA", "banned undeliverable gives (de-anon visitors, in-market lists)"),
    ("DASH", "no em or en dashes"),
    ("SIGNOFF_LINE", "no trailing sign-off or name"),
    ("BREAKUP", "final step must be a breakup"),
    ("METRIC", "at least one concrete metric in the sequence"),
    ("PRICING", "no pricing in cold steps"),
]


def _guardrails_section(root):
    icp = _md_sections((root / ".claude" / "skills" / "ai-sdr" / "knowledge" / "icp-email.md").read_text())
    rules = [_strip_md(b) for b in
             _bullets(_section(icp, "Hard guardrails")) + _bullets(_section(icp, "Formatting"))]
    checks = []
    try:
        import lint_sequence as L
        for name, label in LINT_LABELS:
            rx = getattr(L, name, None)
            if rx is not None:
                checks.append({"id": name, "label": label, "raw": rx.pattern})
    except Exception:
        pass
    return {
        "rules": rules,
        "lint_checks": checks,
        "word_band": "70-110 words per email (aim 80-95)",
        "enforced_at": "every generated sequence is linted at ingest; failures are fixed or blocked before enrollment",
    }


PLAYBOOK_PLAYS = {
    "sequencing": "email 2: no-disruption angle + run-rate CTA",
    "intent_abm": "email 3: Memgraph signal-activation story + signal-mapping CTA (tool named)",
    "ads": "email 3: signal activation, ad investment referenced generically (pixels never named)",
    "never_mention": "never appears in copy (chat/scheduling tools)",
}


def _tech_signals_section(root):
    import tech_signals as T
    selection = json.loads((root / "technographics" / "signatures" / "selection.marketing_sales.json").read_text())
    try:
        vendors_meta = json.loads((root / "technographics" / "signatures" / "vendors.json").read_text())
    except Exception:
        vendors_meta = {}
    # curated vendors.json covers ~36 of the 65 selected ids; the vendored
    # master catalogue fills in category (hence bucket) for the rest
    if any(v not in vendors_meta for v in selection.get("selected", [])):
        try:
            master = json.loads((root / "technographics" / "signatures" / "master" / "vendors.json").read_text())
            for vid in selection.get("selected", []):
                if vid not in vendors_meta and vid in master:
                    vendors_meta[vid] = {"category": master[vid].get("category"),
                                         "vendor_name": master[vid].get("name")}
        except Exception:
            pass
    try:
        available, reason = T.tech_available()
    except Exception as e:  # noqa: BLE001
        available, reason = False, str(e)

    display = dict(T._CATEGORY_DISPLAY) | {"other": "Other"}
    buckets = {k: [] for k in display}
    playbook_vendors = {k: [] for k in PLAYBOOK_PLAYS}
    for vid in selection.get("selected", []):
        meta = vendors_meta.get(vid) or {}
        name = meta.get("vendor_name") or _prettify_vendor(vid)
        bucket = T.bucket_for(vid, meta.get("category")) or "other"
        if vid in T.PLAYBOOK_SEQUENCING:
            play = "sequencing"
        elif vid in T.PLAYBOOK_INTENT_ABM:
            play = "intent_abm"
        elif vid in T.PLAYBOOK_NEVER_MENTION:
            play = "never_mention"
        elif bucket == "ad_pixel" and vid not in T.PLAYBOOK_ADS_EXCLUDE:
            play = "ads"
        else:
            play = None
        buckets[bucket].append({"id": vid, "name": name, "playbook": play})
        if play:
            playbook_vendors[play].append(name)
    return {
        "available": available, "reason": None if available else reason,
        "refresh_days": int(float(os.environ.get("TECH_REFRESH_DAYS") or 90)),
        "buckets": [{"id": k, "name": display[k], "vendors": sorted(buckets[k], key=lambda v: v["name"])}
                    for k in display if buckets[k]],
        "playbooks": [{"id": k, "play": PLAYBOOK_PLAYS[k], "vendors": sorted(v)}
                      for k, v in playbook_vendors.items() if v],
    }


def _hiring_signals_section(root):
    import hiring_signals as H
    try:
        available, reason = H.hiring_available()
    except Exception as e:  # noqa: BLE001
        available, reason = False, str(e)

    # the include-group labels live only in source comments — slice them out;
    # fall back to one flat group from the imported tuple
    groups = []
    try:
        src = (root / ".claude" / "skills" / "sdr-pipeline" / "scripts" / "hiring_signals.py").read_text()
        block = src.split("SALES_INCLUDE_PATTERNS = (", 1)[1].split("\n)", 1)[0]
        current = None
        for line in block.splitlines():
            line = line.strip()
            cm = re.match(r"^#\s*(.+)$", line)
            if cm:
                current = {"group": cm.group(1), "chips": []}
                groups.append(current)
                continue
            for pat in re.findall(r'r"((?:[^"\\]|\\.)*)"', line):
                if current is None:
                    current = {"group": "Sales / GTM roles", "chips": []}
                    groups.append(current)
                h = _humanize_regex(pat)
                current["chips"].append({"label": h["humanized"][0] if h["humanized"] else pat,
                                         "raw": pat})
        if not any(g["chips"] for g in groups):
            raise ValueError("no chips parsed")
    except Exception:
        groups = [{"group": "Sales / GTM roles",
                   "chips": [{"label": (_humanize_regex(p)["humanized"] or [p])[0], "raw": p}
                             for p in H.SALES_INCLUDE_PATTERNS]}]

    exclude = [{"label": (_humanize_regex(p)["humanized"] or [p])[0], "raw": p}
               for p in H.SALES_EXCLUDE_PATTERNS]
    return {
        "available": available, "reason": None if available else reason,
        "refresh_days": int(float(os.environ.get("HIRING_REFRESH_DAYS") or 90)),
        "exclude_beats_include": True,
        "include": [g for g in groups if g["chips"]],
        "exclude": exclude,
    }


# ---- payload -----------------------------------------------------------------
def orchestration_config_payload(root=None):
    root = Path(root) if root else PROJECT_ROOT
    payload, errors = {"ok": True}, {}

    def _safe(key, fn):
        try:
            payload[key] = fn()
        except Exception as e:  # noqa: BLE001
            payload[key] = None
            errors[key] = str(e)

    _safe("pipeline", lambda: _pipeline_section(root))
    _safe("icp_filter", lambda: _icp_filter_section(root))
    _safe("personas", lambda: _personas_section(root))
    _safe("sequencing", lambda: _sequencing_section(root))
    _safe("knowledge", lambda: _knowledge_section(root))
    _safe("guardrails", lambda: _guardrails_section(root))

    signals = {}
    try:
        signals["tech"] = _tech_signals_section(root)
    except Exception as e:  # noqa: BLE001
        signals["tech"] = None
        errors["signals.tech"] = str(e)
    try:
        signals["hiring"] = _hiring_signals_section(root)
    except Exception as e:  # noqa: BLE001
        signals["hiring"] = None
        errors["signals.hiring"] = str(e)
    payload["signals"] = signals

    payload["errors"] = errors
    return payload


if __name__ == "__main__":
    print(json.dumps(orchestration_config_payload(), indent=2, ensure_ascii=False))
