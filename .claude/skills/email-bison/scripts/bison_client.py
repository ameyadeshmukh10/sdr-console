"""Shared Email Bison API client.

Pure stdlib (urllib) so it runs with no `pip install`. This is the seam every
Email Bison use case builds on — add new wrapper methods here and reuse them
from per-use-case scripts.
"""

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

DEFAULT_BASE_URL = "https://send.everworker.ai"


def _load_dotenv():
    """Load KEY=VALUE lines from a .env file walking up from this script.

    Real environment variables always win over .env values.
    """
    here = Path(__file__).resolve()
    for parent in [here.parent, *here.parents]:
        env_path = parent / ".env"
        if env_path.is_file():
            for raw in env_path.read_text().splitlines():
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key, value = key.strip(), value.split(" #")[0].strip().strip('"').strip("'")
                os.environ.setdefault(key, value)
            return


class BisonError(RuntimeError):
    pass


class BisonClient:
    def __init__(self, api_key=None, base_url=None):
        _load_dotenv()
        self.api_key = api_key or os.environ.get("EMAILBISON_API_KEY")
        if not self.api_key:
            raise BisonError(
                "EMAILBISON_API_KEY is not set. Add it to a .env file at the "
                "project root or export it in your shell."
            )
        self.base_url = (
            base_url
            or os.environ.get("EMAILBISON_BASE_URL")
            or DEFAULT_BASE_URL
        ).rstrip("/")
        self._campaign_cache = {}

    # ------------------------------------------------------------------ core
    def get(self, path, params=None):
        """Single authenticated GET returning parsed JSON."""
        url = self.base_url + path
        if params:
            url += "?" + urllib.parse.urlencode(params, doseq=True)
        req = urllib.request.Request(url, method="GET")
        req.add_header("Authorization", f"Bearer {self.api_key}")
        req.add_header("Accept", "application/json")

        last_err = None
        for attempt in range(4):
            try:
                with urllib.request.urlopen(req, timeout=60) as resp:
                    body = resp.read().decode("utf-8")
                return json.loads(body) if body else {}
            except urllib.error.HTTPError as e:
                detail = e.read().decode("utf-8", "replace")[:300]
                # Retry on rate limiting / transient server errors.
                if e.code in (429, 500, 502, 503, 504) and attempt < 3:
                    time.sleep(2 ** attempt)
                    last_err = BisonError(f"HTTP {e.code} for {url}: {detail}")
                    continue
                raise BisonError(f"HTTP {e.code} for {url}: {detail}") from e
            except urllib.error.URLError as e:
                if attempt < 3:
                    time.sleep(2 ** attempt)
                    last_err = BisonError(f"Network error for {url}: {e.reason}")
                    continue
                raise BisonError(f"Network error for {url}: {e.reason}") from e
        if last_err:
            raise last_err

    def get_paginated(self, path, params=None):
        """Yield every item in `data` across all pages (Laravel pagination)."""
        params = dict(params or {})
        params.setdefault("page", 1)
        while True:
            payload = self.get(path, params)
            data = payload.get("data", [])
            for item in data:
                yield item

            links = payload.get("links") or {}
            meta = payload.get("meta") or {}
            next_link = links.get("next")
            if not next_link:
                # Fall back to meta page counters when links are absent.
                current = meta.get("current_page")
                last = meta.get("last_page")
                if current and last and current < last:
                    params["page"] = current + 1
                    continue
                return
            params["page"] = params["page"] + 1

    def post(self, path, body=None):
        """Single authenticated POST returning parsed JSON."""
        url = self.base_url + path
        data = json.dumps(body or {}).encode("utf-8")
        req = urllib.request.Request(url, data=data, method="POST")
        req.add_header("Authorization", f"Bearer {self.api_key}")
        req.add_header("Accept", "application/json")
        req.add_header("Content-Type", "application/json")

        last_err = None
        for attempt in range(4):
            try:
                with urllib.request.urlopen(req, timeout=60) as resp:
                    payload = resp.read().decode("utf-8")
                return json.loads(payload) if payload else {}
            except urllib.error.HTTPError as e:
                detail = e.read().decode("utf-8", "replace")[:300]
                if e.code in (429, 500, 502, 503, 504) and attempt < 3:
                    time.sleep(2 ** attempt)
                    last_err = BisonError(f"HTTP {e.code} for {url}: {detail}")
                    continue
                raise BisonError(f"HTTP {e.code} for {url}: {detail}") from e
            except urllib.error.URLError as e:
                if attempt < 3:
                    time.sleep(2 ** attempt)
                    last_err = BisonError(f"Network error for {url}: {e.reason}")
                    continue
                raise BisonError(f"Network error for {url}: {e.reason}") from e
        if last_err:
            raise last_err

    def put(self, path, body=None):
        """Single authenticated PUT returning parsed JSON (mirrors patch)."""
        return self._write("PUT", path, body)

    def delete(self, path, body=None):
        """Single authenticated DELETE returning parsed JSON."""
        return self._write("DELETE", path, body)

    def patch(self, path, body=None):
        """Single authenticated PATCH returning parsed JSON (mirrors post)."""
        return self._write("PATCH", path, body)

    def _write(self, method, path, body=None):
        url = self.base_url + path
        data = json.dumps(body or {}).encode("utf-8")
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Authorization", f"Bearer {self.api_key}")
        req.add_header("Accept", "application/json")
        req.add_header("Content-Type", "application/json")

        last_err = None
        for attempt in range(4):
            try:
                with urllib.request.urlopen(req, timeout=60) as resp:
                    payload = resp.read().decode("utf-8")
                return json.loads(payload) if payload else {}
            except urllib.error.HTTPError as e:
                detail = e.read().decode("utf-8", "replace")[:300]
                if e.code in (429, 500, 502, 503, 504) and attempt < 3:
                    time.sleep(2 ** attempt)
                    last_err = BisonError(f"HTTP {e.code} for {url}: {detail}")
                    continue
                raise BisonError(f"HTTP {e.code} for {url}: {detail}") from e
            except urllib.error.URLError as e:
                if attempt < 3:
                    time.sleep(2 ** attempt)
                    last_err = BisonError(f"Network error for {url}: {e.reason}")
                    continue
                raise BisonError(f"Network error for {url}: {e.reason}") from e
        if last_err:
            raise last_err

    # -------------------------------------------------------------- wrappers
    def list_tags(self):
        return list(self.get_paginated("/api/tags"))

    def list_campaigns(self, **filters):
        """All campaigns with aggregate stats (total_leads_contacted, interested, …)."""
        return list(self.get_paginated("/api/campaigns", filters))

    def get_campaign_stats(self, campaign_id, body=None):
        """Campaign summary stats incl. per-sequence-step breakdown."""
        payload = self.post(f"/api/campaigns/{campaign_id}/stats", body)
        return payload.get("data", {})

    def duplicate_campaign(self, campaign_id):
        """Clone a campaign (copies the sequence template). Returns the new
        campaign dict {id, name, status:'Draft', ...}. New campaign starts paused."""
        payload = self.post(f"/api/campaigns/{campaign_id}/duplicate")
        return payload.get("data", payload) if isinstance(payload, dict) else payload

    def create_campaign(self, name, type="outbound"):
        """Create a campaign. type: 'outbound' | 'reply_followup'. Returns the new
        campaign dict {id, name, ...}."""
        payload = self.post("/api/campaigns", {"name": name, "type": type})
        return payload.get("data", payload) if isinstance(payload, dict) else payload

    def create_sequence_steps(self, campaign_id, sequence_steps, title="Sequence"):
        """Create the sequence steps for a campaign. Each step:
        {email_subject, email_body, wait_in_days, order, thread_reply?}."""
        return self.post(f"/api/campaigns/{campaign_id}/sequence-steps",
                         {"title": title, "sequence_steps": sequence_steps})

    def get_sequence_steps(self, campaign_id):
        return self.get(f"/api/campaigns/{campaign_id}/sequence-steps")

    def update_sequence_steps(self, sequence_id, sequence_steps, title="Sequence"):
        """Replace a campaign's sequence steps (PUT). sequence_id is the step/sequence id."""
        return self.put(f"/api/campaigns/sequence-steps/{sequence_id}",
                       {"title": title, "sequence_steps": sequence_steps})

    def remove_leads_from_campaign(self, campaign_id, lead_ids):
        """Remove leads from a campaign (DELETE /api/campaigns/{id}/leads)."""
        return self.delete(f"/api/campaigns/{campaign_id}/leads",
                          {"lead_ids": [int(x) for x in lead_ids]})

    def stop_future_emails(self, campaign_id, lead_ids):
        """Stop all future emails for leads within one campaign — the lead and its
        history stay in the campaign, only unsent steps are cancelled."""
        return self.post(f"/api/campaigns/{campaign_id}/leads/stop-future-emails",
                         {"lead_ids": [int(x) for x in lead_ids]})

    def lead_scheduled_emails(self, lead_id):
        """All scheduled emails for a lead across campaigns. Each row carries
        campaign_id and status ('scheduled', 'sending paused', 'stopped',
        'bounced', 'unsubscribed', 'replied', 'sent')."""
        return self.get_paginated(f"/api/leads/{lead_id}/scheduled-emails")

    def push_reply_to_followup_campaign(self, reply_id, campaign_id, force_add_reply=True):
        """Move a reply + its lead into a reply-followup campaign."""
        return self.post(f"/api/replies/{reply_id}/followup-campaign/push",
                         {"campaign_id": int(campaign_id), "force_add_reply": force_add_reply})

    def send_reply(self, reply_id, message, sender_email_id, to_emails,
                   content_type="html", inject_previous=True):
        """Send a reply in the thread of an existing reply (the drafted follow-up).

        to_emails accepts plain address strings or {email_address, name} dicts;
        the API requires the object shape, so strings are wrapped here."""
        recipients = [{"email_address": t} if isinstance(t, str) else t for t in to_emails]
        return self.post(f"/api/replies/{reply_id}/reply", {
            "message": message, "sender_email_id": sender_email_id,
            "to_emails": recipients, "content_type": content_type,
            "inject_previous_email_body": inject_previous})

    def rename_campaign(self, campaign_id, name):
        """Rename a campaign (partial settings update — only the name field)."""
        payload = self.patch(f"/api/campaigns/{campaign_id}/update", {"name": name})
        return payload.get("data", payload) if isinstance(payload, dict) else payload

    def list_replies(self, **filters):
        return self.get_paginated("/api/replies", filters)

    def get_conversation_thread(self, reply_id):
        payload = self.get(f"/api/replies/{reply_id}/conversation-thread")
        return payload.get("data", {})

    def get_lead(self, lead_id):
        payload = self.get(f"/api/leads/{lead_id}")
        return payload.get("data", {})

    def get_lead_sent_emails(self, lead_id):
        """Emails we actually sent this lead: [{email_subject, email_body, sent_at,
        sender_email, campaign_id, ...}] (the outbound sequence in the thread)."""
        payload = self.get(f"/api/leads/{lead_id}/sent-emails")
        return payload.get("data", payload) if isinstance(payload, dict) else (payload or [])

    def get_campaign(self, campaign_id):
        if campaign_id in self._campaign_cache:
            return self._campaign_cache[campaign_id]
        try:
            payload = self.get(f"/api/campaigns/{campaign_id}")
            campaign = payload.get("data", {})
        except BisonError:
            campaign = {}
        self._campaign_cache[campaign_id] = campaign
        return campaign

    # ---- enrollment (use case 5) ----------------------------------------
    def find_lead_by_email(self, email):
        """Return an existing lead dict by email, or None."""
        for lead in self.get_paginated("/api/leads", {"search": email}):
            if (lead.get("email") or "").lower() == email.lower():
                return lead
        return None

    def create_lead(self, first_name=None, last_name=None, email=None, title=None,
                    company=None, custom_variables=None, notes=None):
        """Create a lead with custom_variables; idempotent on duplicate email.

        custom_variables: list of {"name": str, "value": str}. Returns the lead id.
        """
        body = {
            "first_name": first_name or "",
            "last_name": last_name,
            "email": email,
            "title": title,
            "company": company,
            "notes": notes,
            "custom_variables": custom_variables or [],
        }
        body = {k: v for k, v in body.items() if v is not None}
        try:
            payload = self.post("/api/leads", body)
            data = payload.get("data", payload)
            return data.get("id")
        except BisonError as e:
            # Duplicate email → find the existing lead and reuse it.
            if "422" in str(e) and email:
                existing = self.find_lead_by_email(email)
                if existing:
                    return existing.get("id")
            raise

    def update_lead(self, lead_id, first_name=None, last_name=None, email=None, title=None,
                    company=None, custom_variables=None, behavior="patch"):
        """Update an existing lead (default: patch — only the fields passed)."""
        body = {
            "existing_lead_behavior": behavior,
            "first_name": first_name, "last_name": last_name, "email": email,
            "title": title, "company": company, "custom_variables": custom_variables,
        }
        body = {k: v for k, v in body.items() if v is not None}
        return self.post(f"/api/leads/create-or-update/{lead_id}", body)

    def attach_leads_to_campaign(self, campaign_id, lead_ids, allow_parallel_sending=True):
        """Enroll existing lead ids into a campaign."""
        return self.post(
            f"/api/campaigns/{campaign_id}/leads/attach-leads",
            {"lead_ids": list(lead_ids), "allow_parallel_sending": allow_parallel_sending},
        )

    # ---- interested-reply tagging --------------------------------------
    def mark_reply_interested(self, reply_id):
        """Set the built-in 'interested' status on a reply."""
        return self.patch(f"/api/replies/{reply_id}/mark-as-interested", {})

    def attach_tags_to_leads(self, tag_ids, lead_ids, skip_webhooks=True):
        """Attach tag ids to lead ids (tags are lead-centric in Bison)."""
        return self.post(
            "/api/tags/attach-to-leads",
            {"tag_ids": list(tag_ids), "lead_ids": list(lead_ids), "skip_webhooks": skip_webhooks},
        )

    def unsubscribe_lead(self, lead_id):
        """Unsubscribe a lead — stops all scheduled emails, keeps the record. Reversible."""
        return self.patch(f"/api/leads/{lead_id}/unsubscribe", {})

    def blacklist_lead(self, lead_id):
        """Add a lead to the global suppression blacklist (blocks across all campaigns)."""
        return self.post(f"/api/leads/{lead_id}/blacklist", {})
