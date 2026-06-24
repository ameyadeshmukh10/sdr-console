"""HeyReach public API client (stdlib) — validate key + enroll LinkedIn leads.

Reads HEYREACH_API_KEY and HEYREACH_BASE_URL from env (.env auto-loaded).
Auth header is X-API-KEY. Base: https://api.heyreach.io/api/public.
"""

import json
import os
import time
import urllib.error
import urllib.parse
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

    # ---- LinkedIn sender accounts ---------------------------------------
    def list_accounts(self, offset=0, limit=100):
        """POST /li_account/GetAll — the LinkedIn sender accounts on the workspace.
        Returns {totalCount, items:[{id, emailAddress, firstName, lastName,
        profileUrl, ...}]}."""
        return self._request("POST", "/li_account/GetAll",
                             {"offset": int(offset), "limit": int(limit)})

    # ---- Inbox (LinkedIn conversations) ---------------------------------
    def list_conversations(self, campaign_ids=None, account_ids=None, seen=None,
                           search=None, offset=0, limit=100):
        """POST /inbox/GetConversationsV2 — paginated LinkedIn conversations.

        Returns {totalCount, items:[conversation]} where each conversation has
        id (the conversationId), read, lastMessageAt, lastMessageText,
        lastMessageSender ('ME'|'CORRESPONDENT'), totalMessages, linkedInAccountId,
        correspondentProfile {firstName, lastName, profileUrl, position, companyName,
        emailAddress, ...}, linkedInAccount {id, firstName, lastName, emailAddress},
        and messages:[{createdAt, body, subject, isInMail, sender}]."""
        filters = {}
        if campaign_ids:
            filters["campaignIds"] = [int(c) for c in campaign_ids]
        if account_ids:
            filters["linkedInAccountIds"] = [int(a) for a in account_ids]
        if search:
            filters["searchString"] = search
        if seen is not None:
            filters["seen"] = bool(seen)
        return self._request("POST", "/inbox/GetConversationsV2",
                             {"offset": int(offset), "limit": int(limit), "filters": filters})

    def iter_conversations(self, campaign_ids=None, account_ids=None, seen=None,
                           search=None, page_size=100, max_items=1000):
        """Yield every conversation across pages (GetConversationsV2 is offset based,
        max 100/page)."""
        offset, fetched = 0, 0
        while fetched < max_items:
            page = self.list_conversations(campaign_ids=campaign_ids, account_ids=account_ids,
                                           seen=seen, search=search, offset=offset, limit=page_size)
            items = (page or {}).get("items") or []
            for it in items:
                yield it
                fetched += 1
                if fetched >= max_items:
                    return
            total = (page or {}).get("totalCount") or 0
            offset += page_size
            if offset >= total or not items:
                return

    def get_chatroom(self, account_id, conversation_id):
        """GET /inbox/GetChatroom/{accountId}/{conversationId} — one conversation with
        its full message list (same shape as a GetConversationsV2 item)."""
        path = "/inbox/GetChatroom/%s/%s" % (
            urllib.parse.quote(str(int(account_id))),
            urllib.parse.quote(str(conversation_id), safe=""))
        return self._request("GET", path)

    def send_message(self, conversation_id, linkedin_account_id, message, subject=None):
        """POST /inbox/SendMessage — send a LinkedIn message in an existing
        conversation, from the given sender account to the correspondent."""
        body = {"conversationId": conversation_id,
                "linkedInAccountId": int(linkedin_account_id),
                "message": message}
        if subject:
            body["subject"] = subject
        return self._request("POST", "/inbox/SendMessage", body)

    def set_seen(self, conversation_id, linkedin_account_id, seen=True):
        """POST /inbox/SetSeenStatus — mark a conversation seen/unseen."""
        return self._request("POST", "/inbox/SetSeenStatus", {
            "conversationId": conversation_id,
            "linkedInAccountId": int(linkedin_account_id), "seen": bool(seen)})

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
