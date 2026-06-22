"""Drive Clay's MCP server from the backend (no Claude Code) to source a company
list's buying group, then emit candidate contacts for source_contacts.py.

This replaces the *interactive* Clay step of the clay-sourcing skill. It uses the
Anthropic Messages API MCP connector with a cheap model (Haiku by default) to
operate Clay's async `find-and-enrich-contacts-at-company` + `get-task-context`
tools, and orchestrates the polling cadence here in Python.

  python3 clay_enrich.py --list-id <company_list_id> [--cap 25] --out candidates.json

Output file: a JSON list of {first_name,last_name,title,email,company,domain,
linkedin_url} — exactly the shape source_contacts.py:normalize() accepts. A short
summary JSON is printed to stdout; per-domain progress goes to stderr.

Env: ANTHROPIC_API_KEY, HUBSPOT_ACCESS_TOKEN, plus a connected Clay OAuth session
(see clay_oauth.py). Model override: CLAUDE_SOURCING_MODEL (default claude-haiku-4-5).
"""

import argparse
import json
import os
import sys
import time
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(SCRIPTS.parents[1] / "ai-sdr" / "scripts"))  # anthropic_client
from hubspot_client import HubSpotClient  # noqa: E402
from anthropic_client import AnthropicClient, extract_json, AnthropicJSONError  # noqa: E402
import clay_oauth  # noqa: E402

DEFAULT_MODEL = os.environ.get("CLAUDE_SOURCING_MODEL") or "claude-haiku-4-5"
CLAY_SERVER_NAME = "clay"

JOB_TITLE_KEYWORDS = [
    "CRO", "Chief Revenue Officer", "VP of Sales", "Head of Sales", "Director of Sales",
    "SDR Manager", "BDR Manager", "Sales Development Manager",
    "Director of Revenue Operations", "Revenue Operations", "RevOps",
    "Sales Operations", "Sales Ops",
]

POLL_INTERVAL_SECONDS = 15
MAX_POLLS_PER_DOMAIN = 20  # ~5 min ceiling per company

SYSTEM = (
    "You operate Clay's MCP tools to source B2B sales-leadership contacts. "
    "Call the tools exactly as instructed and output ONLY compact JSON — no prose, "
    "no markdown fences."
)


def company_domains(list_id, cap=None):
    """[{company_id, name, domain}] for a HubSpot company list (domain != null)."""
    hub = HubSpotClient()
    ids = hub.read_list_records(list_id)
    companies = hub.batch_read_companies(ids, properties=("domain", "name", "website"))
    out = []
    for c in companies:
        p = c.get("properties", {})
        domain = (p.get("domain") or "").strip().lower()
        if domain:
            out.append({"company_id": c.get("id"), "name": p.get("name"), "domain": domain})
    if cap:
        out = out[: int(cap)]
    return out


def _mcp_servers():
    token = clay_oauth.get_access_token()  # raises ClayNotConnected if unusable
    return [{"type": "url", "url": clay_oauth.mcp_url(),
             "name": CLAY_SERVER_NAME, "authorization_token": token}]


def _ask_json(client, mcp_servers, prompt):
    res = client.complete_with_mcp(SYSTEM, prompt, mcp_servers,
                                   model=DEFAULT_MODEL, max_tokens=4096)
    try:
        return extract_json(res["text"])
    except AnthropicJSONError:
        return {}


def enrich_domain(client, mcp_servers, domain, company_name):
    """Find + enrich the buying group at one domain; return list of contact dicts."""
    find_prompt = (
        f'Call the Clay tool "find-and-enrich-contacts-at-company" with arguments:\n'
        f'  companyIdentifier: "{domain}"\n'
        f'  contactFilters.job_title_keywords: {json.dumps(JOB_TITLE_KEYWORDS)}\n'
        f'  contactFilters.locations: ["United States"]\n'
        f'  contactFilters.job_title_exclude_keywords: ["Intern", "Assistant"]\n'
        f'  dataPoints.contactDataPoints: [{{"type": "Email"}}]\n'
        f"The tool is async and returns a taskId. Return ONLY JSON: "
        f'{{"task_id": "<the taskId>"}}.'
    )
    found = _ask_json(client, mcp_servers, find_prompt)
    task_id = found.get("task_id") or found.get("taskId")
    if not task_id:
        sys.stderr.write(f"  [{domain}] no taskId returned; skipping\n")
        return []

    poll_prompt = (
        f'Call the Clay tool "get-task-context" for taskId "{task_id}". '
        f"If the enrichment is still running, return {{\"status\": \"pending\"}}. "
        f"Once it has resolved, return "
        f'{{"status": "resolved", "contacts": [{{"first_name","last_name","title",'
        f'"email","company","domain","linkedin_url"}}, ...]}} including ONLY contacts '
        f'that have a work email. Use "{company_name or domain}" as company and '
        f'"{domain}" as domain when missing. Output ONLY JSON.'
    )
    for attempt in range(MAX_POLLS_PER_DOMAIN):
        time.sleep(POLL_INTERVAL_SECONDS)
        res = _ask_json(client, mcp_servers, poll_prompt)
        if res.get("status") == "resolved":
            contacts = res.get("contacts") or []
            sys.stderr.write(f"  [{domain}] resolved: {len(contacts)} contacts\n")
            return contacts
        sys.stderr.write(f"  [{domain}] pending ({attempt + 1}/{MAX_POLLS_PER_DOMAIN})…\n")
    sys.stderr.write(f"  [{domain}] timed out after {MAX_POLLS_PER_DOMAIN} polls; skipping\n")
    return []


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--list-id", required=True, help="HubSpot company list id")
    ap.add_argument("--cap", type=int, default=25, help="max companies this run (Clay credits)")
    ap.add_argument("--out", required=True, help="path to write the candidate contacts JSON")
    args = ap.parse_args()

    companies = company_domains(args.list_id, cap=args.cap)
    if not companies:
        sys.stderr.write("no companies with a domain in that list\n")
        Path(args.out).write_text("[]")
        print(json.dumps({"companies": 0, "candidates": 0, "out": args.out}))
        return 0

    mcp_servers = _mcp_servers()
    client = AnthropicClient(model=DEFAULT_MODEL)

    candidates, seen_emails = [], set()
    for i, co in enumerate(companies):
        sys.stderr.write(f"[{i + 1}/{len(companies)}] {co['domain']}\n")
        for raw in enrich_domain(client, mcp_servers, co["domain"], co.get("name")):
            email = (raw.get("email") or "").strip().lower()
            if not email or email in seen_emails:
                continue
            seen_emails.add(email)
            candidates.append(raw)

    Path(args.out).write_text(json.dumps(candidates, indent=2))
    print(json.dumps({"companies": len(companies), "candidates": len(candidates),
                      "out": args.out}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
