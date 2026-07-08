"""Pull a HubSpot list, filter to US B2B-tech ICP, assign a persona, write contacts.jsonl.

Run:  python3 .claude/skills/sdr-pipeline/scripts/hubspot_pull.py [list_id]
(list_id defaults to HUBSPOT_LIST_ID from .env)

Output: data/outreach/contacts.jsonl — one ICP contact per line, with `persona` and
`linkedin_url`, ready for the per-persona subagents to generate copy.
"""

import json
import os
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(SCRIPTS.parents[1] / "ai-sdr" / "scripts"))  # buyer_group
from hubspot_client import HubSpotClient, HubSpotError  # noqa: E402
from buyer_group import persona_for_title, buyer_role  # noqa: E402

PROJECT_ROOT = SCRIPTS.parents[3]
OUT_DIR = PROJECT_ROOT / "data" / "outreach"

# Lightweight B2B software/tech heuristic (industry/company/website).
TECH_HINTS = ("software", "saas", "technology", "tech", "information technology", "internet",
              "computer", "it services", "platform", "cloud", "ai", "data", "cyber", "dev")


def gated_stages():
    """HubSpot lifecycle stages we must never enroll (deal in progress / closed won).
    Configurable via GATED_LIFECYCLE_STAGES; defaults to opportunity + customer."""
    raw = os.environ.get("GATED_LIFECYCLE_STAGES", "opportunity,customer")
    return {s.strip().lower() for s in raw.split(",") if s.strip()}


def country_is_us(props):
    """Tri-state: True = US, False = explicitly non-US, None = unknown (no data)."""
    code = (props.get("hs_country_region_code") or "").strip().upper()
    if code == "US":
        return True
    if code:
        return False  # an explicit non-US region code
    c = (props.get("country") or "").strip().lower()
    if not c:
        return None  # no country data → keep (trust the list)
    if "united states" in c or "usa" in c or c in {"us", "u.s.", "u.s.a.", "u.s"}:
        return True
    return False


def is_tech(props):
    blob = " ".join((props.get(k) or "") for k in ("industry", "company", "website", "domain")).lower()
    return any(h in blob for h in TECH_HINTS)


def main():
    list_id = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("HUBSPOT_LIST_ID")
    # .env auto-loads via the client; re-resolve list_id from env if needed.
    if not list_id:
        try:
            HubSpotClient()  # triggers .env load
        except HubSpotError:
            pass
        list_id = os.environ.get("HUBSPOT_LIST_ID")
    if not list_id:
        print("ERROR: no list id. Pass one as an arg or set HUBSPOT_LIST_ID in .env.")
        return 1

    linkedin_prop = os.environ.get("HUBSPOT_LINKEDIN_PROPERTY", "hs_linkedin_url")
    props = ["firstname", "lastname", "email", "jobtitle", "company",
             "country", "hs_country_region_code", "industry", "website", "domain",
             "lifecyclestage", linkedin_prop]

    try:
        client = HubSpotClient()
        ids = list(client.get_list_members(list_id))
        contacts = client.batch_read_contacts(ids, props)
    except HubSpotError as e:
        print(f"ERROR pulling HubSpot list {list_id}: {e}")
        return 1

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    rows = []
    skipped = {"non_icp": 0, "non_us": 0, "non_tech": 0, "no_email": 0, "gated_lifecycle": 0}
    kept_unknown_country = 0
    has_industry = any((c.get("properties", {}).get("industry")) for c in contacts)
    gated = gated_stages()

    for c in contacts:
        p = c.get("properties", {})
        title = p.get("jobtitle") or ""
        persona = persona_for_title(title)
        if not persona:
            skipped["non_icp"] += 1
            continue
        if not p.get("email"):
            skipped["no_email"] += 1
            continue
        # Lifecycle gate: never enroll a contact already in an active deal / closed won.
        if (p.get("lifecyclestage") or "").strip().lower() in gated:
            skipped["gated_lifecycle"] += 1
            continue
        # Geo: drop ONLY explicitly non-US; keep US and unknown-country (trust the list).
        us = country_is_us(p)
        if us is False:
            skipped["non_us"] += 1
            continue
        if us is None:
            kept_unknown_country += 1
        # Industry filter only when the data exists; else trust the list.
        if has_industry and not is_tech(p):
            skipped["non_tech"] += 1
            continue
        rows.append({
            "contact_id": c.get("id"),
            "first_name": p.get("firstname") or "",
            "last_name": p.get("lastname") or "",
            "email": p.get("email"),
            "title": title,
            "company": p.get("company") or "",
            "linkedin_url": p.get(linkedin_prop) or "",
            "buyer_role": buyer_role(title)[0],
            "persona": persona,
        })

    out = OUT_DIR / "contacts.jsonl"
    with out.open("w") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    from collections import Counter
    by_persona = Counter(r["persona"] for r in rows)
    print(f"Pulled {len(ids)} list members → {len(contacts)} read → {len(rows)} ICP contacts.")
    print("By persona:", dict(by_persona))
    print("Skipped:", skipped)
    print(f"Kept {kept_unknown_country} contacts with unknown country (trusting the list).")
    if not has_industry:
        print("NOTE: no industry data on contacts — tech filter skipped (trusting the list).")
    print(f"Wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
