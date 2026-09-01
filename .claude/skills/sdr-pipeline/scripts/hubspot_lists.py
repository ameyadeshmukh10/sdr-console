"""Search HubSpot lists by name (for the Use view's list picker).

  python3 hubspot_lists.py search "<query>" [--type contact|company]

Output: JSON [{list_id, name, object_type_id, processing_type, size, created_at,
updated_at}, ...], newest-created first (the API has no sort; we pull one wide page).
object_type_id "0-1" = contact list, "0-2" = company list.
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from hubspot_client import HubSpotClient  # noqa: E402

# Friendly --type -> HubSpot objectTypeId.
TYPE_TO_OBJECT_ID = {"contact": "0-1", "company": "0-2"}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("command", choices=["search"])
    ap.add_argument("query", nargs="?", default="")
    ap.add_argument("--type", choices=["contact", "company"], default=None)
    ap.add_argument("--count", type=int, default=500)
    args = ap.parse_args()

    object_type_id = TYPE_TO_OBJECT_ID.get(args.type)
    hub = HubSpotClient()
    results = hub.search_lists(args.query, object_type_id=object_type_id, count=args.count)
    print(json.dumps(results, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
