"""Use case 2: feature extraction + aggregation over interested replies.

Reads data/interested-replies/dataset.jsonl (produced by the email-bison skill)
and emits deterministic, reproducible features so the analysis never depends on
an LLM miscounting 171 records. The qualitative synthesis (the trends report) is
done afterwards by the agent, reading these outputs.

Outputs (data/interested-replies/analysis/):
  features.jsonl        one feature record per reply
  summary.json          aggregate distributions, cross-tabs, caveats
  by_campaign.csv  by_persona.csv  by_cta.csv  by_reply_intent.csv

Run:  python3 .claude/skills/interested-trends/scripts/analyze_interested.py
"""

import csv
import json
import re
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[4]
DATA_DIR = PROJECT_ROOT / "data" / "interested-replies"
DATASET = DATA_DIR / "dataset.jsonl"
OUT_DIR = DATA_DIR / "analysis"

DOW = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

# --------------------------------------------------------------------- helpers

def cv(record, key):
    for c in record["lead"].get("custom_variables") or []:
        if c.get("name") == key:
            return c.get("value") or ""
    return ""


def words(text):
    return re.findall(r"[A-Za-z0-9']+", text or "")


_QUOTE_HDR = re.compile(
    r"^\s*(On .+wrote:|(From|Sent|To|Subject|Cc|Date):.*|_{5,}|-{5,}|>.*)$",
    re.IGNORECASE,
)
_SIG_MARKERS = [
    r"^\s*(best|regards|thanks|warmly|cheers|sincerely|kind regards|best regards)[,!.]?\s*$",
    r"book a meeting",
    r"calendly\.com",
    r"meetings.*hubspot\.com",
    r"this message is confidential",
    r"registered office",
    r"sent from my ",
    r"^\s*--\s*$",
]
_SIG_RE = re.compile("|".join(_SIG_MARKERS), re.IGNORECASE)


def strip_quoted(text):
    """Keep only the new content above any quoted/forwarded chain."""
    out = []
    for line in (text or "").splitlines():
        if line.lstrip().startswith(">"):
            break
        if _QUOTE_HDR.match(line):
            break
        out.append(line)
    return "\n".join(out).strip()


def strip_signature(text):
    """Cut the message at the first signature/disclaimer marker."""
    lines = (text or "").splitlines()
    for i, line in enumerate(lines):
        if _SIG_RE.search(line):
            return "\n".join(lines[:i]).strip()
    return (text or "").strip()


def clean_message(text):
    return strip_signature(strip_quoted(text))


AUTO_REPLY_RE = re.compile(
    r"automatic reply|out of office|on annual leave|on leave|"
    r"limited access to email|away from (the|my) office|i am currently out|"
    r"will be out of office|maternity leave|parental leave",
    re.IGNORECASE,
)


def is_auto_reply(text):
    return bool(AUTO_REPLY_RE.search(text or ""))


# ----------------------------------------------------------------- classifiers

def classify_seniority(title):
    t = (title or "").lower()
    if re.search(r"\bfounder\b|\bco-?founder\b|\bowner\b|proprietor", t):
        return "Founder/Owner"
    if re.search(r"\bceo\b|\bcfo\b|\bcto\b|\bcmo\b|\bcoo\b|\bcro\b|\bcio\b|\bcpo\b|"
                 r"chief\s+\w+\s+officer|chief executive|chief financial|chief marketing|"
                 r"chief technology|chief operating|chief revenue", t):
        return "C-Level"
    if re.search(r"\b(svp|evp|vp)\b|vice president", t):
        return "VP"
    if re.search(r"\bhead of\b|\bdirector\b|\bpartner\b|\bprincipal\b", t):
        return "Head/Director"
    if re.search(r"\bmanager\b|\blead\b|\bsupervisor\b", t):
        return "Manager"
    if re.search(r"\bspecialist\b|\banalyst\b|\bengineer\b|\bconsultant\b|\bexecutive\b|"
                 r"\bcoordinator\b|\bassociate\b|\brepresentative\b|\bagent\b", t):
        return "IC"
    return "Unknown"


def classify_function(title):
    t = (title or "").lower()
    pairs = [
        ("Finance", r"\bfinanc|\bcfo\b|\baccount|treasur|controller|\bfp&a\b"),
        ("Marketing", r"\bmarket|\bcmo\b|\bbrand\b|\bdemand gen|\bgrowth\b|\bseo\b|content|\bdigital\b"),
        ("Sales/Revenue", r"\bsales\b|\brevenue\b|\bcro\b|account exec|\bbdr\b|\bsdr\b|business development|partnership"),
        ("Operations", r"\boperation|\bcoo\b|\bops\b|supply chain|logistics"),
        ("Eng/IT/Product", r"\bengineer|\bcto\b|\bcio\b|\bit\b|developer|software|cloud|infrastructure|architect|\bproduct\b|\bdata\b"),
        ("HR/Talent", r"\bhr\b|human resources|talent|recruit|people\b"),
        ("Consulting/Advisory", r"\bconsult|advisor|\bcoach\b"),
        ("Executive/General", r"\bceo\b|chief executive|founder|owner|\bgeneral manager\b|\bgm\b|managing director"),
    ]
    for name, pat in pairs:
        if re.search(pat, t):
            return name
    return "Other"


def parse_geo(campaign):
    c = (campaign or "").lower()
    if "global" in c:
        return "Global"
    if "americas" in c or "usa" in c or "us " in c:
        return "Americas"
    return "Unknown"


def parse_offer_type(campaign):
    c = (campaign or "").lower()
    if "pdf" in c:
        return "Lead magnet (PDF)"
    if "demo" in c:
        return "Demo request"
    if "event" in c:
        return "Event"
    if re.search(r"\bcro\b|\bcmo\b|\bcfo\b", c):
        return "Persona-targeted"
    return "Other"


SPECIFIC_TIME_RE = re.compile(
    r"\b(mon|tues|wednes|thurs|fri|satur|sun)day\b.*\b(\d{1,2}\s?(am|pm|:\d)|noon)|"
    r"\b\d{1,2}\s?(am|pm)\b.*\b(mon|tue|wed|thu|fri)|"
    r"\bnext week\b.*\b\d|\bthis week\b.*\b\d", re.IGNORECASE,
)
CALENDLY_RE = re.compile(r"calendly\.com|meetings.*hubspot|book a meeting|grab time", re.IGNORECASE)
DEMO_RE = re.compile(r"\bdemo\b|\bwalkthrough\b|\bshow you\b|quick look", re.IGNORECASE)
RESOURCE_RE = re.compile(r"send (you )?(a |the )?(one-?pager|pdf|guide|audit|snapshot|case study|deck|checklist|example)", re.IGNORECASE)
SOFT_Q_RE = re.compile(r"would you be open|are you open|worth a|interested in|does .* make sense|can i|may i|happy to", re.IGNORECASE)


def classify_cta(body):
    b = body or ""
    if SPECIFIC_TIME_RE.search(b):
        return "Specific-time offer"
    if CALENDLY_RE.search(b):
        return "Calendly/booking link"
    if RESOURCE_RE.search(b):
        return "Resource offer"
    if DEMO_RE.search(b):
        return "Demo offer"
    if SOFT_Q_RE.search(b) and "?" in b:
        return "Soft permission question"
    if "?" in b:
        return "Open question"
    return "None/unclear"


def classify_reply_intent(clean_text, raw_text):
    # Use the lead's cleaned words; fall back to raw only if cleaning emptied it.
    t = (clean_text or "").lower().strip() or (raw_text or "").lower()
    if not t.strip():
        return "Other"
    if is_auto_reply(raw_text):
        return "Auto-reply/OOO"
    if re.search(r"\bunsubscrib|remove me|opt.?out|not interested|no thanks|"
                 r"stop emailing|please stop|take me off", t):
        return "Negative/opt-out"
    # Meeting acceptance is checked before referral so "grab time"/"booked" win.
    if re.search(r"grab time|grab something|booked a meeting|book a|calendar|schedule|"
                 r"let'?s (chat|talk|meet|connect)|set up a call|happy to (chat|meet|talk|hop)|"
                 r"works for me|sounds good|let'?s do it|what time|when works|"
                 r"feel free to grab|my (calendar|link)|sure[,.]?\s|yes[,.]?\s|"
                 r"@\s?\d{1,2}|i'?d like to (chat|talk|meet|see|automate|learn)", t):
        return "Meeting/demo accept"
    # Tight referral: explicit hand-off to another person.
    if re.search(r"forward (this|it|you|your|to)|not the (right|best) person|wrong person|"
                 r"loop (in|you in)|connect you (with|to)|reach out to [a-z]+\b|cc'?ing|"
                 r"my colleague|the right person|better (person|contact|fit)|"
                 r"speak (to|with) (my|our)|please contact [a-z]", t):
        return "Referral/forward"
    if re.search(r"\bpric|\bcost\b|\bbudget\b|how much|\bquote\b|pricing", t):
        return "Pricing request"
    if re.search(r"send (it|me|over|that|more)|share (the|more)|more (info|information|details)|"
                 r"learn more|tell me more|interested in (learning|seeing)|sounds interesting|"
                 r"send it over", t):
        return "Info request"
    if re.search(r"\bnext (month|quarter|year)\b|circle back|reconnect|"
                 r"not (right )?now|too busy|after (the|our)", t):
        return "Positive-later"
    if "?" in t:
        return "Question"
    return "Other"


def normalize_subject(s):
    s = (s or "").lower()
    s = re.sub(r"^\s*(re|fw|fwd)\s*:\s*", "", s)
    s = re.sub(r"^\s*(re|fw|fwd)\s*:\s*", "", s)  # twice for "RE: RE:"
    return re.sub(r"\s+", " ", s).strip()


def find_winning_step(record):
    """Which sequence step (1-4) prompted the reply, via subject match."""
    target = normalize_subject(record.get("subject"))
    if target:
        for i in range(1, 5):
            if normalize_subject(cv(record, f"subject{i}")) == target and target:
                return i
    # Fallback: fuzzy match the outbound text to a body.
    fo = (record.get("first_outbound_text") or "").strip()
    if fo:
        fo_head = " ".join(words(fo)[:25]).lower()
        best, best_i = 0.0, None
        for i in range(1, 5):
            body_head = " ".join(words(cv(record, f"body{i}"))[:25]).lower()
            if not body_head:
                continue
            a, b = set(fo_head.split()), set(body_head.split())
            if a and b:
                j = len(a & b) / len(a | b)
                if j > best:
                    best, best_i = j, i
        if best >= 0.4:
            return best_i
    return None


# ------------------------------------------------------------------- features

def sequence_features(record):
    steps = {}
    for i in range(1, 5):
        body = cv(record, f"body{i}")
        subj = cv(record, f"subject{i}")
        wc = len(words(body))
        steps[f"step{i}"] = {
            "subject": subj,
            "subject_word_count": len(words(subj)),
            "subject_is_question": subj.strip().endswith("?"),
            "subject_personalized": bool(
                record["lead"].get("company") and
                record["lead"]["company"].split()[0].lower() in subj.lower()
            ) if subj and record["lead"].get("company") else False,
            "body_word_count": wc,
            "num_questions": body.count("?"),
            "has_personalized_opening": bool(re.search(
                r"i (looked at|reviewed|saw|noticed|came across)|congrats|"
                r"your linkedin|noting|i read", body, re.IGNORECASE)),
            "has_social_proof": bool(re.search(
                r"\bclients?\b|companies like|teams? (i|we) (talk|work)|\d+%|"
                r"helped \d|customers", body, re.IGNORECASE)),
            "has_ps": bool(re.search(r"\bp\.?s\.?\b", body, re.IGNORECASE)),
            "cta_type": classify_cta(body),
        }
    return steps


def build_features(record):
    lead = record["lead"]
    title = lead.get("title") or ""
    reply_raw = record.get("interested_reply_text") or ""
    reply_clean = clean_message(reply_raw)
    auto = is_auto_reply(reply_raw)

    dt = None
    dr = record.get("date_received")
    if dr:
        try:
            dt = datetime.strptime(dr.replace("Z", "")[:19], "%Y-%m-%dT%H:%M:%S")
        except ValueError:
            dt = None

    winning = find_winning_step(record)
    steps = sequence_features(record)
    winning_cta = steps[f"step{winning}"]["cta_type"] if winning else None

    return {
        "reply_id": record["reply_id"],
        "lead_name": " ".join(p for p in [lead.get("first_name"), lead.get("last_name")] if p),
        "company": lead.get("company"),
        "email_domain": (lead.get("email") or "").split("@")[-1].lower(),
        "title": title,
        "campaign_name": record.get("campaign_name"),
        "geo": parse_geo(record.get("campaign_name")),
        "offer_type": parse_offer_type(record.get("campaign_name")),
        "seniority": classify_seniority(title),
        "function": classify_function(title),
        "winning_step": winning,
        "winning_cta": winning_cta,
        "reply_intent": classify_reply_intent(reply_clean, reply_raw),
        "is_auto_reply": auto,
        "reply_word_count": len(words(reply_clean)),
        "reply_clean": reply_clean,
        "reply_dow": DOW[dt.weekday()] if dt else None,
        "reply_hour": dt.hour if dt else None,
        # Kept so the console can put a time axis on any of these features. The
        # week key is the Monday of the ISO week.
        "reply_date": dt.date().isoformat() if dt else None,
        "reply_month": dt.strftime("%Y-%m") if dt else None,
        "reply_week": (dt.date() - timedelta(days=dt.weekday())).isoformat() if dt else None,
        "interested_via": record.get("interested_via"),
        "has_linkedin": bool(cv(record, "linkedin")),
        "has_location": bool(cv(record, "location")),
        "untracked": not bool((record.get("first_outbound_text") or "").strip()),
        "steps": steps,
    }


# ---------------------------------------------------------------- aggregation

def pct(n, total):
    return round(100.0 * n / total, 1) if total else 0.0


def dist(items):
    c = Counter(items)
    total = sum(c.values())
    return {k: {"count": v, "pct": pct(v, total)} for k, v in c.most_common()}


def write_csv(path, header, rows):
    with path.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)


def main():
    if not DATASET.exists():
        print(f"ERROR: {DATASET} not found. Run the email-bison fetch script first.")
        return 1

    records = [json.loads(l) for l in DATASET.open() if l.strip()]
    feats = [build_features(r) for r in records]

    # Genuine = exclude auto-replies from the messaging/intent findings.
    genuine = [f for f in feats if not f["is_auto_reply"]]
    n, ng = len(feats), len(genuine)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    with (OUT_DIR / "features.jsonl").open("w") as f:
        for ft in feats:
            f.write(json.dumps(ft, ensure_ascii=False) + "\n")

    # Winning-step body word counts (genuine only).
    win_wc = [f["steps"][f"step{f['winning_step']}"]["body_word_count"]
              for f in genuine if f["winning_step"]]

    summary = {
        "generated_from": str(DATASET),
        "total_replies": n,
        "auto_reply_count": sum(1 for f in feats if f["is_auto_reply"]),
        "genuine_count": ng,
        "untracked_count": sum(1 for f in feats if f["untracked"]),
        "by_campaign": dist(f["campaign_name"] for f in feats),
        "by_geo": dist(f["geo"] for f in feats),
        "by_offer_type": dist(f["offer_type"] for f in feats),
        "by_seniority": dist(f["seniority"] for f in feats),
        "by_function": dist(f["function"] for f in feats),
        "by_winning_step": dist(str(f["winning_step"]) for f in genuine),
        "by_winning_cta": dist(f["winning_cta"] for f in genuine if f["winning_cta"]),
        "by_reply_intent": dist(f["reply_intent"] for f in genuine),
        "by_reply_dow": dist(f["reply_dow"] for f in genuine if f["reply_dow"]),
        "by_interested_via": dist(
            ",".join(f["interested_via"]) if f["interested_via"] else "unknown" for f in feats),
        "winning_email_word_count": {
            "min": min(win_wc) if win_wc else None,
            "max": max(win_wc) if win_wc else None,
            "avg": round(sum(win_wc) / len(win_wc), 1) if win_wc else None,
            "n": len(win_wc),
        },
        "timeseries": build_timeseries(feats),
        "crosstabs": {
            "offer_type_x_seniority": _crosstab(genuine, "offer_type", "seniority"),
            "cta_x_reply_intent": _crosstab(
                [f for f in genuine if f["winning_cta"]], "winning_cta", "reply_intent"),
            "seniority_x_function": _crosstab(feats, "seniority", "function"),
        },
        "caveats": [
            "DESCRIPTIVE ONLY: this is the population of interested repliers, not all "
            "leads contacted. There is no denominator, so we cannot compute true "
            "conversion lift (which segment/message converts BETTER). Read every finding "
            "as 'what the people who replied look like', not 'what causes replies'.",
            f"{sum(1 for f in feats if f['is_auto_reply'])} interested-tagged replies are "
            "auto-replies/OOO and are excluded from messaging/intent aggregates.",
            "Titles and companies are messy free-text; seniority/function are heuristic and "
            "the agent should reclassify Unknown/Other records before reporting.",
            "No industry field exists; industry must be inferred from company name/domain.",
            "interested_reply_text is sometimes the SDR's own reply (the latest message in "
            "the thread), not the lead's. reply_intent is a heuristic on that text; for any "
            "cited example the agent should confirm against threads/<reply_id>.md.",
        ],
    }
    (OUT_DIR / "summary.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False))

    # CSVs ---------------------------------------------------------------
    write_csv(OUT_DIR / "by_campaign.csv", ["campaign", "count", "pct"],
              [[k, v["count"], v["pct"]] for k, v in summary["by_campaign"].items()])
    write_csv(OUT_DIR / "by_persona.csv", ["seniority", "function", "count"],
              _persona_rows(feats))
    write_csv(OUT_DIR / "by_cta.csv", ["winning_cta", "count", "pct"],
              [[k, v["count"], v["pct"]] for k, v in summary["by_winning_cta"].items()])
    write_csv(OUT_DIR / "by_reply_intent.csv", ["reply_intent", "count", "pct"],
              [[k, v["count"], v["pct"]] for k, v in summary["by_reply_intent"].items()])

    print(f"Analyzed {n} replies ({ng} genuine, "
          f"{summary['auto_reply_count']} auto-replies excluded).")
    print(f"Wrote features.jsonl, summary.json, and 4 CSVs to {OUT_DIR}")
    print("\nTop offers:", ", ".join(f"{k} ({v['count']})"
          for k, v in list(summary["by_offer_type"].items())))
    print("Top seniority:", ", ".join(f"{k} ({v['count']})"
          for k, v in list(summary["by_seniority"].items())[:5]))
    print("Reply intents:", ", ".join(f"{k} ({v['count']})"
          for k, v in list(summary["by_reply_intent"].items())))
    print("Winning CTA:", ", ".join(f"{k} ({v['count']})"
          for k, v in list(summary["by_winning_cta"].items())))
    return 0


def _crosstab(feats, a, b):
    table = defaultdict(Counter)
    for f in feats:
        table[f[a]][f[b]] += 1
    return {k: dict(v) for k, v in table.items()}


# ----------------------------------------------------------------- time series

# Dimensions whose mix we track period-over-period. A shift in any of these is a
# real trend even without a denominator: it says what the replies we win are
# increasingly made of.
MIX_DIMS = ("offer_type", "function", "seniority", "reply_intent", "winning_cta")


def _month_range(first, last):
    """Inclusive list of YYYY-MM keys, so gaps plot as zero instead of closing up."""
    out, y, m = [], int(first[:4]), int(first[5:7])
    ly, lm = int(last[:4]), int(last[5:7])
    while (y, m) <= (ly, lm):
        out.append(f"{y:04d}-{m:02d}")
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


def _week_range(first, last):
    out, d = [], datetime.strptime(first, "%Y-%m-%d").date()
    end = datetime.strptime(last, "%Y-%m-%d").date()
    while d <= end:
        out.append(d.isoformat())
        d += timedelta(days=7)
    return out


def _timeseries(feats, period_key, periods):
    buckets = defaultdict(list)
    for f in feats:
        if f.get(period_key):
            buckets[f[period_key]].append(f)
    rows = []
    for p in periods:
        group = buckets.get(p, [])
        genuine = [f for f in group if not f["is_auto_reply"]]
        row = {
            "period": p,
            "replies": len(group),
            "genuine": len(genuine),
            "auto": len(group) - len(genuine),
        }
        for dim in MIX_DIMS:
            row[f"by_{dim}"] = dict(Counter(
                f[dim] for f in genuine if f.get(dim)).most_common())
        row["by_winning_step"] = dict(Counter(
            str(f["winning_step"]) for f in genuine if f["winning_step"]).most_common())
        rows.append(row)
    return rows


def build_timeseries(feats):
    """Interested replies over time. NUMERATOR ONLY — see the note field.

    Bison reports lifetime-to-date campaign counts, so a true rate-over-time needs
    differenced snapshots from campaigns_history.jsonl (see analyze_conversion.py).
    Volume and mix shift are what this dataset alone can honestly support.
    """
    dated = [f for f in feats if f.get("reply_date")]
    if not dated:
        return {"available": False, "note": "No reply dates in the dataset."}
    dates = sorted(f["reply_date"] for f in dated)
    weeks = sorted({f["reply_week"] for f in dated})
    return {
        "available": True,
        "span": {"first": dates[0], "last": dates[-1], "dated_replies": len(dated),
                 "undated_replies": len(feats) - len(dated)},
        "months": _timeseries(dated, "reply_month",
                              _month_range(dates[0][:7], dates[-1][:7])),
        "weeks": _timeseries(dated, "reply_week", _week_range(weeks[0], weeks[-1])),
        "mix_dims": list(MIX_DIMS) + ["winning_step"],
        "note": "Interested-reply COUNTS and mix over time (numerator only). Not a "
                "conversion rate — sends per period are not in this dataset. Mix "
                "shares are of genuine (non-auto) replies in the period.",
    }


def _persona_rows(feats):
    c = Counter((f["seniority"], f["function"]) for f in feats)
    return [[s, fn, n] for (s, fn), n in c.most_common()]


if __name__ == "__main__":
    raise SystemExit(main())
