"""HeyReach public API client (stdlib) — validate key + enroll LinkedIn leads.

Reads HEYREACH_API_KEY and HEYREACH_BASE_URL from env (.env auto-loaded).
Auth header is X-API-KEY. Base: https://api.heyreach.io/api/public.
"""

import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path

DEFAULT_BASE_URL = "https://api.heyreach.io/api/public"


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


class HeyReachError(RuntimeError):
    pass


class HeyReachClient:
    def __init__(self, api_key=None, base_url=None):
        _load_dotenv()
        self.api_key = api_key or os.environ.get("HEYREACH_API_KEY")
        if not self.api_key:
            raise HeyReachError("HEYREACH_API_KEY is not set (.env or environment).")
        self.base_url = (base_url or os.environ.get("HEYREACH_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")

    def _request(self, method, path, body=None):
        url = self.base_url + path
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("X-API-KEY", self.api_key)
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
                        last = HeyReachError(f"HTTP {e.code} for {url}: {detail}")
                        continue
                raise HeyReachError(f"HTTP {e.code} for {url}: {detail}") from e
            except urllib.error.URLError as e:
                if attempt < 4:
                    time.sleep(2 ** attempt)
                    last = HeyReachError(f"Network error for {url}: {e.reason}")
                    continue
                raise HeyReachError(f"Network error for {url}: {e.reason}") from e
        if last:
            raise last

    def check_key(self):
        """GET /auth/CheckApiKey — returns True if the key is valid."""
        self._request("GET", "/auth/CheckApiKey")
        return True

    def get_campaign(self, campaign_id):
        """GET /campaign/GetById — campaign details (status, campaignAccountIds, …)."""
        return self._request("GET", f"/campaign/GetById?campaignId={int(campaign_id)}")

    def get_overall_stats(self, campaign_ids, account_ids=None):
        """POST /stats/GetOverallStats — LinkedIn metrics for the given campaign(s):
        connectionsSent/Accepted, messagesSent, totalMessageReplies, reply +
        acceptance rates, uniqueLeadsContacted, autoTaggedInterested. accountIds
        is required by the API ([] = all accounts on the campaign)."""
        return self._request("POST", "/stats/GetOverallStats", {
            "campaignIds": [int(c) for c in campaign_ids],
            "accountIds": [int(a) for a in (account_ids or [])],
        })

    def add_leads_to_campaign(self, campaign_id, account_lead_pairs):
        """POST /campaign/AddLeadsToCampaignV2.

        account_lead_pairs: [{"linkedInAccountId": int, "lead": {firstName, lastName,
        profileUrl, companyName, position, emailAddress, customUserFields: {...}}}]
        """
        return self._request("POST", "/campaign/AddLeadsToCampaignV2",
                             {"campaignId": int(campaign_id), "accountLeadPairs": account_lead_pairs})

    @staticmethod
    def build_pair(linkedin_account_id, first_name, last_name, profile_url,
                   company=None, position=None, email=None, custom_fields=None):
        lead = {"firstName": first_name or "", "lastName": last_name or "", "profileUrl": profile_url}
        if company:
            lead["companyName"] = company
        if position:
            lead["position"] = position
        if email:
            lead["emailAddress"] = email
        if custom_fields:
            # HeyReach expects an array of {name, value}, not a dict.
            lead["customUserFields"] = (
                custom_fields if isinstance(custom_fields, list)
                else [{"name": k, "value": v} for k, v in custom_fields.items()])
        return {"linkedInAccountId": int(linkedin_account_id), "lead": lead}
