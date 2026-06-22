"""Print the domains of companies in a HubSpot company list (for Clay sourcing).

  python3 hubspot_company_domains.py <company_list_id>

Output: JSON [{company_id, name, domain}, ...]. Companies without a domain are
included with domain=null (the skill skips/logs those).
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from hubspot_client import HubSpotClient  # noqa: E402


def main():
    if len(sys.argv) < 2:
        print("usage: hubspot_company_domains.py <company_list_id>", file=sys.stderr)
        return 2
    hub = HubSpotClient()
    ids = hub.read_list_records(sys.argv[1])
    companies = hub.batch_read_companies(ids, properties=("domain", "name", "website"))
    out = []
    for c in companies:
        p = c.get("properties", {})
        out.append({"company_id": c.get("id"), "name": p.get("name"),
                    "domain": (p.get("domain") or "").strip().lower() or None})
    print(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
