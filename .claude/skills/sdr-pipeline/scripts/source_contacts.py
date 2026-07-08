"""Turn Clay-sourced candidate contacts into a HubSpot list + pipeline-ready jsonl.

The Clay step (find-and-enrich-contacts-at-company) is MCP-driven and run by Claude.
This script is the deterministic HubSpot + pipeline side: dedup against HubSpot,
ICP/persona filter, even 3-way variant split, create the net-new contacts in HubSpot,
make a static list, and write a sourced-<list>.jsonl that the pipeline ingests.

  python3 source_contacts.py <candidates.json> [--list-name NAME] [--no-ingest]

candidates.json = a JSON list of contact dicts from Clay. Flexible keys accepted:
  first_name/firstName, last_name/lastName, title/jobTitle, email, company, domain,
  linkedin_url/linkedin.
"""

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(SCRIPTS.parents[1] / "ai-sdr" / "scripts"))  # buyer_group
from hubspot_client import HubSpotClient, HubSpotError  # noqa: E402
from buyer_group import persona_for_title, buyer_role  # noqa: E402

PROJECT_ROOT = SCRIPTS.parents[3]
OUT_DIR = PROJECT_ROOT / "data" / "outreach"
VARIANTS = ["value-give", "earn", "show"]
LINKEDIN_PROP = os.environ.get("HUBSPOT_LINKEDIN_PROPERTY", "hs_linkedin_url")


def _g(d, *keys):
    for k in keys:
        v = d.get(k)
        if v:
            return str(v).strip()
    return ""


def normalize(raw):
    """Map a Clay candidate dict to our contact shape (best-effort key matching)."""
    name = _g(raw, "name", "full_name", "fullName")
    first = _g(raw, "first_name", "firstName", "firstname")
    last = _g(raw, "last_name", "lastName", "lastname")
    if not first and name:
        parts = name.split()
        first, last = parts[0], " ".join(parts[1:])
    return {
        "first_name": first, "last_name": last,
        "email": _g(raw, "email", "work_email", "workEmail").lower(),
        "title": _g(raw, "title", "jobTitle", "job_title", "headline"),
        "company": _g(raw, "company", "company_name", "companyName", "organization"),
        "domain": _g(raw, "domain", "company_domain", "companyDomain").lower(),
        "linkedin_url": _g(raw, "linkedin_url", "linkedin", "linkedinUrl", "linkedin_profile"),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("candidates", help="JSON file: list of Clay candidate contacts")
    ap.add_argument("--list-name", default=None)
    ap.add_argument("--no-hubspot", action="store_true", help="skip HubSpot writes (dry build of jsonl)")
    ap.add_argument("--no-ingest", action="store_true", help="don't run sdr_batches init afterwards")
    args = ap.parse_args()

    raw = json.loads(Path(args.candidates).read_text())
    if isinstance(raw, dict):  # accept {contacts:[...]} or {results:[...]}
        raw = raw.get("contacts") or raw.get("results") or raw.get("items") or []
    cands = [normalize(r) for r in raw]

    # 1. drop no-email, dedup within the candidate set
    seen, uniq = set(), []
    for c in cands:
        if not c["email"] or c["email"] in seen:
            continue
        seen.add(c["email"])
        uniq.append(c)

    # 2. ICP / persona filter (reuse buyer_group)
    icp = []
    for c in uniq:
        persona = persona_for_title(c["title"])
        if not persona:
            continue
        c["persona"] = persona
        c["buyer_role"] = buyer_role(c["title"])[0]
        icp.append(c)

    stats = {"candidates": len(cands), "with_email_unique": len(uniq), "icp": len(icp)}

    hub = None if args.no_hubspot else HubSpotClient()

    # 3. resolve against HubSpot by email: contacts already present keep their id,
    #    the rest are net-new. BOTH end up in the list + pipeline, so the batch
    #    fully lands and re-running the same candidates is idempotent.
    existing_ids = hub.find_existing_email_ids([c["email"] for c in icp]) if hub else {}

    # 3b. Lifecycle gate: drop contacts already in HubSpot who are in a gated stage
    #     (opportunity/customer) — never (re)source an active deal. Net-new contacts
    #     have no lifecycle yet, so this only affects the already-present set. Fails
    #     open on a HubSpot read error.
    if hub and existing_ids:
        gated = {s.strip().lower() for s in
                 os.environ.get("GATED_LIFECYCLE_STAGES", "opportunity,customer").split(",") if s.strip()}
        try:
            recs = hub.batch_read_contacts(list(existing_ids.values()), ["lifecyclestage"])
            gated_ids = {str(r.get("id")) for r in recs
                         if ((r.get("properties") or {}).get("lifecyclestage") or "").strip().lower() in gated}
        except HubSpotError as e:
            gated_ids = set()
            print(f"  ! lifecycle gate skipped (HubSpot read failed: {str(e)[:120]})", file=sys.stderr)
        if gated_ids:
            before = len(icp)
            icp = [c for c in icp if existing_ids.get(c["email"]) not in gated_ids]
            stats["gated_lifecycle"] = before - len(icp)
            print(f"  lifecycle gate: dropped {stats['gated_lifecycle']} already-in-HubSpot "
                  f"contact(s) in {sorted(gated)}")

    net_new = [c for c in icp if c["email"] not in existing_ids]
    existing = [c for c in icp if c["email"] in existing_ids]
    stats["already_in_hubspot"] = len(existing)
    stats["net_new"] = len(net_new)

    # 4. even 3-way variant split across the whole ICP set (stable across re-runs)
    for i, c in enumerate(icp):
        c["variant"] = VARIANTS[i % len(VARIANTS)]

    # 5. create the net-new contacts; reuse the existing ones' ids
    created, create_failed = [], 0
    if hub:
        for c in net_new:
            props = {"email": c["email"], "firstname": c["first_name"], "lastname": c["last_name"],
                     "jobtitle": c["title"], "company": c["company"]}
            if c["linkedin_url"]:
                props[LINKEDIN_PROP] = c["linkedin_url"]
            try:
                c["contact_id"] = hub.create_contact(props)
                created.append(c)
            except HubSpotError as e:
                create_failed += 1
                print(f"  ! create failed for {c['email']}: {str(e)[:120]}", file=sys.stderr)
        for c in existing:
            c["contact_id"] = existing_ids[c["email"]]
    else:
        for i, c in enumerate(icp):  # no-hubspot: synthetic ids so the jsonl is usable
            c["contact_id"] = f"clay-{i}"
            created.append(c)
    stats["created"] = len(created)
    stats["create_failed"] = create_failed

    # working set = everything we have an id for (newly created + already present)
    working = [c for c in icp if c.get("contact_id")]
    stats["in_list"] = len(working)

    # 6. create a static HubSpot list + add the new contacts
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M")
    list_name = args.list_name or f"AI SDR Sourced {ts}"
    list_id = None
    if hub and working:
        list_id = hub.create_list(list_name)
        hub.add_contacts_to_list(list_id, [c["contact_id"] for c in working])
    stats["hubspot_list_id"] = list_id
    stats["hubspot_list_name"] = list_name

    # 7. write the pipeline-ready jsonl (with variant + persona)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out_file = OUT_DIR / f"sourced-{list_id or ts}.jsonl"
    with out_file.open("w") as f:
        for c in working:
            f.write(json.dumps({
                "contact_id": c["contact_id"], "first_name": c["first_name"],
                "last_name": c["last_name"], "email": c["email"], "title": c["title"],
                "company": c["company"], "linkedin_url": c["linkedin_url"],
                "buyer_role": c["buyer_role"], "persona": c["persona"], "variant": c["variant"],
            }) + "\n")
    stats["jsonl"] = str(out_file)
    by_variant = {v: sum(1 for c in working if c["variant"] == v) for v in VARIANTS}
    stats["by_variant"] = by_variant

    # 8. ingest into the pipeline (upserts contacts with variant + assigns batches)
    if working and not args.no_ingest:
        proc = subprocess.run([sys.executable, str(SCRIPTS / "sdr_batches.py"), "init", "--from", str(out_file)],
                              cwd=str(PROJECT_ROOT), capture_output=True, text=True)
        stats["ingest"] = (proc.stdout or proc.stderr).strip()

    print(json.dumps(stats, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
