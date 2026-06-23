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

    def read_list_records(self, list_id):
        """Record IDs for any v3 list (contacts OR companies). Alias of get_list_members."""
        return list(self.get_list_members(list_id))

    def search_lists(self, query="", object_type_id=None, count=50):
        """Search HubSpot lists by name. Returns a list of normalized dicts:
        {list_id, name, object_type_id, processing_type, size}.
        object_type_id '0-1'=contacts, '0-2'=companies; pass it to constrain results.
        """
        body = {"query": query or "", "count": int(count), "offset": 0,
                "additionalProperties": ["hs_list_size"]}
        if object_type_id:
            body["objectTypeIds"] = [object_type_id]
        payload = self._request("POST", "/crm/v3/lists/search", body=body)
        out = []
        for lst in payload.get("lists", []):
            extra = lst.get("additionalProperties") or {}
            out.append({
                "list_id": str(lst.get("listId")),
                "name": lst.get("name"),
                "object_type_id": lst.get("objectTypeId"),
                "processing_type": lst.get("processingType"),
                "size": extra.get("hs_list_size"),
            })
        return out

    def create_list(self, name, object_type_id="0-1"):
        """Create a static (MANUAL) list, or reuse an existing one of the same name.

        HubSpot rejects duplicate list names (400 ILS.DUPLICATE_LIST_NAMES); when
        that happens we look the list up by name and return its id, so the call is
        idempotent and re-running a sourcing batch doesn't blow up. Returns listId.
        """
        try:
            payload = self._request("POST", "/crm/v3/lists",
                                    body={"name": name, "objectTypeId": object_type_id,
                                          "processingType": "MANUAL"})
            return str((payload.get("list") or {}).get("listId") or payload.get("listId"))
        except HubSpotError as e:
            if "DUPLICATE_LIST_NAMES" not in str(e) and "already exist" not in str(e):
                raise
            for lst in self.search_lists(query=name, object_type_id=object_type_id):
                if (lst.get("name") or "").strip() == name.strip():
                    return str(lst["list_id"])
            raise

    def add_contacts_to_list(self, list_id, record_ids):
        """Add record IDs to a static list (chunks of 100). Body is a JSON array of ids."""
        ids = [str(i) for i in record_ids]
        for i in range(0, len(ids), 100):
            self._request("PUT", f"/crm/v3/lists/{list_id}/memberships/add", body=ids[i:i + 100])
        return len(ids)

    # ---- companies ------------------------------------------------------
    def batch_read_companies(self, ids, properties=("domain", "name", "website")):
        """Read properties for company ids (chunks of 100)."""
        ids = [str(i) for i in ids]
        out = []
        for i in range(0, len(ids), 100):
            payload = self._request(
                "POST", "/crm/v3/objects/companies/batch/read",
                body={"inputs": [{"id": cid} for cid in ids[i:i + 100]], "properties": list(properties)},
            )
            out.extend(payload.get("results", []))
        return out

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

    def find_existing_email_ids(self, emails):
        """Map {email_lower: contact_id} for emails already present in HubSpot."""
        emails = [e.strip().lower() for e in emails if e and e.strip()]
        found = {}
        for i in range(0, len(emails), 100):  # search `IN` accepts up to 100 values
            chunk = emails[i:i + 100]
            after = None
            while True:
                body = {
                    "filterGroups": [{"filters": [{"propertyName": "email", "operator": "IN", "values": chunk}]}],
                    "properties": ["email"], "limit": 100,
                }
                if after:
                    body["after"] = after
                payload = self._request("POST", "/crm/v3/objects/contacts/search", body=body)
                for r in payload.get("results", []):
                    e = (r.get("properties") or {}).get("email")
                    if e:
                        found[e.strip().lower()] = str(r.get("id"))
                after = (payload.get("paging") or {}).get("next", {}).get("after")
                if not after:
                    break
        return found

    def find_existing_emails(self, emails):
        """Subset of `emails` already present in HubSpot (lowercased), for dedup."""
        return set(self.find_existing_email_ids(emails))

    def create_contact(self, properties):
        """Create one contact. Returns the new contact id (str). Raises on failure."""
        payload = self._request("POST", "/crm/v3/objects/contacts", body={"properties": properties})
        return str(payload.get("id"))

    def batch_create_contacts(self, records):
        """Create contacts (chunks of 100). `records` = list of property dicts. Returns created
        objects (with `id`), aligned by email. Skips a whole chunk only on hard error."""
        out = []
        for i in range(0, len(records), 100):
            chunk = records[i:i + 100]
            payload = self._request(
                "POST", "/crm/v3/objects/contacts/batch/create",
                body={"inputs": [{"properties": r} for r in chunk]},
            )
            out.extend(payload.get("results", []))
        return out

    def delete_contact(self, contact_id):
        self._request("DELETE", f"/crm/v3/objects/contacts/{contact_id}")
