"""Enroll generated contacts into Email Bison (email) + HeyReach (LinkedIn).

Reads:
  data/outreach/contacts.jsonl              (from hubspot_pull.py)
  data/outreach/generated/<contact_id>.json (subagent output: email + linkedin assets)

For each contact:
  • Email Bison: create_lead with custom_variables subject1-4 / body1-4 → attach to BISON_CAMPAIGN_ID
  • HeyReach: AddLeadsToCampaignV2(HEYREACH_CAMPAIGN_ID) with customUserFields {li_connect,li_msg1,li_msg2}
              (skipped if the contact has no linkedin_url)

Idempotent via data/outreach/enroll_state.json. Refuses to enroll email copy that fails the
guardrail linter (unless --no-lint).

Run:  python3 .claude/skills/sdr-pipeline/scripts/enroll.py [--dry-run] [--no-lint]
"""

import concurrent.futures
import json
import os
import sys
from collections import defaultdict
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(SCRIPTS.parents[1] / "ai-sdr" / "scripts"))      # lint_sequence
sys.path.insert(0, str(SCRIPTS.parents[1] / "email-bison" / "scripts"))  # bison_client
from heyreach_client import HeyReachClient, HeyReachError  # noqa: E402
import lint_sequence as L  # noqa: E402

PROJECT_ROOT = SCRIPTS.parents[3]
OUT_DIR = PROJECT_ROOT / "data" / "outreach"
GEN_DIR = OUT_DIR / "generated"

ENROLL_WORKERS = 8   # concurrent lead create/update calls
HR_CHUNK = 100       # max HeyReach leads per add_leads_to_campaign call
STATE_FILE = OUT_DIR / "enroll_state.json"


def _load_dotenv():
    """Load .env so config vars (campaign ids) are available even in --dry-run."""
    for parent in [SCRIPTS, *SCRIPTS.parents]:
        env_path = parent / ".env"
        if env_path.is_file():
            for raw in env_path.read_text().splitlines():
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                os.environ.setdefault(k.strip(), v.split(" #")[0].strip().strip('"').strip("'"))
            return

EMAIL_KEYS = [f"{k}{i}" for i in range(1, 5) for k in ("subject", "body")]
LI_KEYS = ["li_connect", "li_msg1", "li_msg2"]

# Per-persona Bison campaign routing (legacy; kept as a fallback).
PERSONA_CAMPAIGN_ENV = {
    "sales-leadership": "BISON_CAMPAIGN_SALES_LEADERSHIP",
    "revops": "BISON_CAMPAIGN_REVOPS",
    "partnerships": "BISON_CAMPAIGN_PARTNERSHIPS",
    "sdr-bdr": "BISON_CAMPAIGN_SDR_BDR",
}

# Per-variant Bison campaign routing — one dedicated campaign per instruction set,
# for clean per-variant interested-rate reporting in Bison.
VARIANT_CAMPAIGN_ENV = {
    "value-give": "BISON_CAMPAIGN_VALUE_GIVE",
    "earn": "BISON_CAMPAIGN_EARN",
    "show": "BISON_CAMPAIGN_SHOW",
}


def bison_campaign_for_variant(variant):
    """The dedicated campaign for a variant, falling back to the persona/default
    routing only if the variant campaign isn't configured yet."""
    return os.environ.get(VARIANT_CAMPAIGN_ENV.get(variant or "value-give", ""))


def bison_campaign_for(persona):
    return (os.environ.get(PERSONA_CAMPAIGN_ENV.get(persona, ""))
            or os.environ.get("BISON_CAMPAIGN_ID"))


def load_state():
    if STATE_FILE.is_file():
        return json.loads(STATE_FILE.read_text())
    return {}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=2))


def lint_email_assets(email):
    """Return list of issues across the 4 emails (empty = pass)."""
    issues = []
    steps = []
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


def bison_custom_vars(email):
    # NOTE: Bison strips trailing whitespace from custom variables, so the blank line BEFORE the
    # campaign signature cannot be added here — it must be set in Bison (prepend a blank line to the
    # sender-email signature, or to the sequence-step template after {{body1}}). Internal paragraph
    # breaks (\n\n) within the body ARE preserved.
    return [{"name": k, "value": (email.get(k, "").rstrip() if k.startswith("body") else email.get(k, ""))}
            for k in EMAIL_KEYS]


def main():
    _load_dotenv()
    dry = "--dry-run" in sys.argv
    no_lint = "--no-lint" in sys.argv

    contacts_path = OUT_DIR / "contacts.jsonl"
    if not contacts_path.is_file():
        print(f"ERROR: {contacts_path} not found. Run hubspot_pull.py first.")
        return 1
    contacts = {c["contact_id"]: c for c in
                (json.loads(l) for l in contacts_path.open() if l.strip())}

    hr_campaign = os.environ.get("HEYREACH_CAMPAIGN_ID")
    hr_account = os.environ.get("HEYREACH_LINKEDIN_ACCOUNT_ID")

    # Lazy clients (only built when actually sending).
    bison = heyreach = None
    if not dry:
        from bison_client import BisonClient  # noqa: E402
        bison = BisonClient()
        if hr_campaign and hr_account:
            heyreach = HeyReachClient()

    state = load_state()
    gen_files = sorted(GEN_DIR.glob("*.json")) if GEN_DIR.is_dir() else []
    if not gen_files:
        print(f"No generated assets in {GEN_DIR}. Generate copy first (see SKILL.md).")
        return 1

    counts = {"email": 0, "linkedin": 0, "skipped_lint": 0, "skipped_done": 0, "no_li": 0}

    # Phase 1 — per-contact lint (local) + the lead create/update (network) run
    # concurrently. The Bison/HeyReach clients are stateless per call (and already
    # retry on 429/5xx), so this parallelizes cleanly. Workers only READ shared
    # state and return a result; state mutation + attaching happen single-threaded
    # below, so there's no lock to manage. The campaign attach is deferred and
    # batched (see phase 2) — one call per campaign instead of one per contact.
    def worker(gf):
        res = {"cid": None, "logs": [], "counts": {},
               "bison_new": None, "hr_pair": None}
        asset = json.loads(gf.read_text())
        cid = str(asset.get("contact_id"))
        res["cid"] = cid
        contact = contacts.get(cid) or contacts.get(asset.get("contact_id"))
        if not contact:
            res["logs"].append(f"  ! {gf.name}: no matching contact in contacts.jsonl — skipping")
            return res
        st = state.get(cid, {})
        email, linkedin = asset.get("email", {}), asset.get("linkedin", {})

        if not no_lint:
            issues = lint_email_assets(email)
            if issues:
                res["counts"]["skipped_lint"] = 1
                res["logs"].append(f"  ✗ {contact.get('email')} [{contact.get('persona')}] FAILED lint:")
                res["logs"] += [f"      - {it}" for it in issues[:6]]
                return res

        variant = asset.get("variant") or contact.get("variant") or "value-give"
        campaign = bison_campaign_for_variant(variant) or bison_campaign_for(contact.get("persona"))
        if st.get("bison"):
            lead_id = st["bison"]["lead_id"]
            cvars = bison_custom_vars(email)
            if dry:
                res["logs"].append(f"  [dry] BISON update lead {lead_id} ({contact.get('email')}) custom_variables refreshed")
            else:
                bison.update_lead(lead_id, email=contact.get("email"), custom_variables=cvars,
                                  first_name=contact.get("first_name"), last_name=contact.get("last_name"),
                                  title=contact.get("title"), company=contact.get("company"))
            res["counts"]["updated"] = 1
        elif not campaign:
            res["counts"]["no_campaign"] = 1
            res["logs"].append(f"  ! {contact.get('email')} [{contact.get('persona')}]: no Bison campaign configured — skipping email")
        else:
            cvars = bison_custom_vars(email)
            if dry:
                res["logs"].append(f"  [dry] BISON create_lead {contact.get('email')} [{contact.get('persona')}] "
                                   f"custom_variables={[c['name'] for c in cvars]} → attach campaign {campaign}")
            else:
                lead_id = bison.create_lead(
                    first_name=contact.get("first_name"), last_name=contact.get("last_name"),
                    email=contact.get("email"), title=contact.get("title"),
                    company=contact.get("company"), custom_variables=cvars)
                res["bison_new"] = (campaign, lead_id)  # attached in batch below
            res["counts"]["email"] = 1

        li_url = contact.get("linkedin_url")
        if not li_url:
            res["counts"]["no_li"] = 1
        elif st.get("heyreach"):
            pass
        elif not (hr_campaign and hr_account):
            if dry:
                res["logs"].append("  [dry] HEYREACH skipped (HEYREACH_CAMPAIGN_ID / LINKEDIN_ACCOUNT_ID not set)")
        else:
            custom_fields = {k: linkedin.get(k, "") for k in LI_KEYS}
            pair = HeyReachClient.build_pair(
                hr_account, contact.get("first_name"), contact.get("last_name"), li_url,
                company=contact.get("company"), position=contact.get("title"),
                email=contact.get("email"), custom_fields=custom_fields)
            if dry:
                res["logs"].append(f"  [dry] HEYREACH add lead {li_url} → campaign {hr_campaign} "
                                   f"customUserFields={list(custom_fields.keys())}")
            else:
                res["hr_pair"] = (hr_campaign, pair)
            res["counts"]["linkedin"] = 1
        return res

    with concurrent.futures.ThreadPoolExecutor(max_workers=ENROLL_WORKERS) as ex:
        results = list(ex.map(worker, gen_files))  # preserves gen_files order

    # Aggregate single-threaded: print logs, tally counts, collect batch work.
    bison_by_campaign, hr_by_campaign = defaultdict(list), defaultdict(list)
    for res in results:
        for line in res["logs"]:
            print(line)
        for k, v in res["counts"].items():
            counts[k] = counts.get(k, 0) + v
        if res["bison_new"]:
            campaign, lead_id = res["bison_new"]
            bison_by_campaign[campaign].append((res["cid"], lead_id))
        if res["hr_pair"]:
            hr_by_campaign[res["hr_pair"][0]].append((res["cid"], res["hr_pair"][1]))

    # Phase 2 — batch the campaign attaches: one call per campaign, not per lead.
    for campaign, items in bison_by_campaign.items():
        lead_ids = [lid for _, lid in items]
        if not dry:
            bison.attach_leads_to_campaign(campaign, lead_ids)
            for cid, lid in items:  # mark enrolled only after a successful attach
                state.setdefault(cid, {})["bison"] = {"lead_id": lid, "campaign_id": campaign}
        print(f"  BISON attached {len(lead_ids)} leads → campaign {campaign}")
    for campaign, items in hr_by_campaign.items():
        pairs = [p for _, p in items]
        if not dry:
            for i in range(0, len(pairs), HR_CHUNK):  # chunk large adds
                heyreach.add_leads_to_campaign(campaign, pairs[i:i + HR_CHUNK])
            for cid, _ in items:
                state.setdefault(cid, {})["heyreach"] = {"campaign_id": campaign}
        print(f"  HEYREACH added {len(pairs)} leads → campaign {campaign}")

    if not dry:
        save_state(state)
    print("\nSummary:", counts, "(dry-run)" if dry else "")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
