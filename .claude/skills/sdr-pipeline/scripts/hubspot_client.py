"""HubSpot CRM v3 client (stdlib) — list pulls + contact reads.

Reads HUBSPOT_ACCESS_TOKEN (private-app `pat-eu1-…`) and HUBSPOT_BASE_URL from env
(.env auto-loaded). EU token → base https://api.eu1.hubapi.com.
"""

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

# HubSpot serves the public REST API from api.hubapi.com for ALL regions, including EU.
# The `eu1` in a `pat-eu1-…` token is data residency only; there is no api.eu1.hubapi.com.
DEFAULT_BASE_URL = "https://api.hubapi.com"


def _load_dotenv():
    here = Path(__file__).resolve()
    for parent in [here.parent, *here.parents]:
        env_path = parent / ".env"
        if env_path.is_file():
            for raw in env_path.read_text().splitlines():
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                os.environ.setdefault(k.strip(), v.split(" #")[0].strip().strip('"').strip("'"))
            return


class HubSpotError(RuntimeError):
    pass


class HubSpotClient:
    def __init__(self, token=None, base_url=None):
        _load_dotenv()
        self.token = token or os.environ.get("HUBSPOT_ACCESS_TOKEN")
        if not self.token:
            raise HubSpotError("HUBSPOT_ACCESS_TOKEN is not set (.env or environment).")
        self.base_url = (base_url or os.environ.get("HUBSPOT_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")

    def _request(self, method, path, params=None, body=None):
        url = self.base_url + path
        if params:
            url += "?" + urllib.parse.urlencode(params, doseq=True)
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Authorization", f"Bearer {self.token}")
        req.add_header("Accept", "application/json")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        last = None
        for attempt in range(5):
            try:
                with urllib.request.urlopen(req, timeout=60) as resp:
                    raw = resp.read().decode("utf-8")
                return json.loads(raw) if raw else {}
            except urllib.error.HTTPError as e:
                detail = e.read().decode("utf-8", "replace")[:300]
                if e.code == 429 or (500 <= e.code < 600):
                    if attempt < 4:
                        time.sleep(2 ** attempt)
                        last = HubSpotError(f"HTTP {e.code} for {url}: {detail}")
                        continue
                raise HubSpotError(f"HTTP {e.code} for {url}: {detail}") from e
            except urllib.error.URLError as e:
                if attempt < 4:
                    time.sleep(2 ** attempt)
                    last = HubSpotError(f"Network error for {url}: {e.reason}")
                    continue
                raise HubSpotError(f"Network error for {url}: {e.reason}") from e
        if last:
            raise last

    # ---- lists ----------------------------------------------------------
    def get_list_members(self, list_id):
        """Yield contact record IDs (strings) for an ILS list, all pages."""
        after = None
        while True:
            params = {"limit": 250}
            if after:
                params["after"] = after
            payload = self._request("GET", f"/crm/v3/lists/{list_id}/memberships/join-order", params)
            for r in payload.get("results", []):
                yield str(r.get("recordId"))
            after = (payload.get("paging") or {}).get("next", {}).get("after")
            if not after:
                return

    # ---- contacts -------------------------------------------------------
    def batch_read_contacts(self, ids, properties):
        """Read properties for up to many contact ids (chunks of 100)."""
        ids = [str(i) for i in ids]
        out = []
        for i in range(0, len(ids), 100):
            chunk = ids[i:i + 100]
            payload = self._request(
                "POST", "/crm/v3/objects/contacts/batch/read",
                body={"inputs": [{"id": cid} for cid in chunk], "properties": list(properties)},
            )
            out.extend(payload.get("results", []))
        return out
