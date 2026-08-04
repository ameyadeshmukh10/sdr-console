"""Pull a HubSpot list, filter to US B2B-tech ICP, assign a persona, write contacts.jsonl.

Run:  python3 .claude/skills/sdr-pipeline/scripts/hubspot_pull.py [list_id] [--limit N]
(list_id defaults to HUBSPOT_LIST_ID from .env; no --limit means the whole list)

`--limit N` caps how many contacts this run adds, counting only ones the pipeline
does NOT already hold. Counting NEW contacts rather than list members is what makes
the cap repeatable: "pull 50 more" run twice pulls a hundred different people, where
a cap on members read would return the same first 50 every time.

Output: data/outreach/contacts.jsonl — one ICP contact per line, with `persona` and
`linkedin_url`, ready for the per-persona subagents to generate copy.
"""

import argparse
import json
import os
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(SCRIPTS.parents[1] / "ai-sdr" / "scripts"))  # buyer_group
from hubspot_client import HubSpotClient, HubSpotError  # noqa: E402
from buyer_group import persona_for_title, buyer_role  # noqa: E402
import batch_db  # noqa: E402
from batch_db import classify_motion  # noqa: E402

PROJECT_ROOT = SCRIPTS.parents[3]
OUT_DIR = PROJECT_ROOT / "data" / "outreach"

# Lightweight B2B software/tech heuristic (industry/company/website).
TECH_HINTS = ("software", "saas", "technology", "tech", "information technology", "internet",
              "computer", "it services", "platform", "cloud", "ai", "data", "cyber", "dev")


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


def already_held():
    """Contact ids the pipeline already has, so --limit counts only NEW people.

    Best-effort: a missing or unreadable DB just means nothing is held yet, which
    degrades the cap to "the first N ICP contacts" rather than failing the pull."""
    try:
        conn = batch_db.connect()
        try:
            return {str(r["contact_id"]) for r in
                    conn.execute("SELECT contact_id FROM contacts")}
        finally:
            conn.close()
    except Exception:  # noqa: BLE001
        return set()


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("list_id", nargs="?", help="HubSpot list id (default: HUBSPOT_LIST_ID)")
    ap.add_argument("--limit", type=int, default=None,
                    help="cap how many NEW contacts to add; omit for the whole list")
    args = ap.parse_args()
    limit = args.limit if (args.limit or 0) > 0 else None

    list_id = args.list_id or os.environ.get("HUBSPOT_LIST_ID")
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
    tag_prop = os.environ.get("HUBSPOT_EVERWORKER_TAG_PROPERTY", "everworker_tag")
    props = ["firstname", "lastname", "email", "jobtitle", "company",
             # Direct dial + mobile: the call list recommends a phone touch for the
             # top of the book, and a call recommendation without a number is a
             # to-do item rather than an action.
             "phone", "mobilephone",
             "country", "hs_country_region_code", "industry", "website", "domain",
             # Provenance: how the contact came to exist. Without these the pipeline
             # cannot tell a cold contact from someone who came in through a form,
             # which is what makes outbound attribution defensible downstream.
             "hs_analytics_source", "hs_latest_source", "lifecyclestage",
             linkedin_prop, tag_prop]

    try:
        client = HubSpotClient()
        ids = list(client.get_list_members(list_id))
        contacts = client.batch_read_contacts(ids, props)
    except HubSpotError as e:
        print(f"ERROR pulling HubSpot list {list_id}: {e}")
        return 1

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    rows = []
    skipped = {"non_icp": 0, "non_us": 0, "non_tech": 0, "no_email": 0, "suppressed": 0,
               "over_limit": 0}
    kept_unknown_country = 0
    has_industry = any((c.get("properties", {}).get("industry")) for c in contacts)
    # Only consulted when a cap is set — an uncapped pull should not pay for the read.
    held = already_held() if limit else set()
    capped = 0

    for c in contacts:
        p = c.get("properties", {})
        # Suppression: RevOps tagged this contact do-not-contact — never enters the
        # pipeline. (The enroll gate re-checks live; this just avoids wasted copy.)
        if str(p.get(tag_prop) or "").strip().lower() == "false":
            skipped["suppressed"] += 1
            continue
        title = p.get("jobtitle") or ""
        persona = persona_for_title(title)
        if not persona:
            skipped["non_icp"] += 1
            continue
        if not p.get("email"):
            skipped["no_email"] += 1
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
        # The cap. Contacts we already hold pass through free — re-writing a row we
        # have costs nothing and keeps the file a faithful slice of the list — so
        # the budget is spent only on people the pipeline has never seen.
        if limit is not None and str(c.get("id")) not in held:
            if capped >= limit:
                skipped["over_limit"] += 1
                continue
            capped += 1
        rows.append({
            "contact_id": c.get("id"),
            "first_name": p.get("firstname") or "",
            "last_name": p.get("lastname") or "",
            "email": p.get("email"),
            "title": title,
            "company": p.get("company") or "",
            "linkedin_url": p.get(linkedin_prop) or "",
            "phone": p.get("phone") or "",
            "mobile_phone": p.get("mobilephone") or "",
            "buyer_role": buyer_role(title)[0],
            "persona": persona,
            "source": p.get("hs_analytics_source") or "",
            "latest_source": p.get("hs_latest_source") or "",
            "lifecycle_stage": p.get("lifecyclestage") or "",
            "motion": classify_motion(p.get("hs_analytics_source"),
                                      p.get("hs_latest_source"),
                                      p.get("lifecyclestage")),
        })

    out = OUT_DIR / "contacts.jsonl"
    with out.open("w") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    from collections import Counter
    by_persona = Counter(r["persona"] for r in rows)
    by_motion = Counter(r["motion"] for r in rows)
    print(f"Pulled {len(ids)} list members → {len(contacts)} read → {len(rows)} ICP contacts.")
    if limit is not None:
        print(f"Limit {limit} new contacts: took {capped}, "
              f"held back {skipped['over_limit']} more that qualify.")
    print("By persona:", dict(by_persona))
    print("By motion:", dict(by_motion),
          "(inbound = HubSpot original-source or lifecycle says they came to us)")
    print("Skipped:", skipped)
    print(f"Kept {kept_unknown_country} contacts with unknown country (trusting the list).")
    if not has_industry:
        print("NOTE: no industry data on contacts — tech filter skipped (trusting the list).")
    print(f"Wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
