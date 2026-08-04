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
from datetime import datetime, timezone
from pathlib import Path

# HubSpot serves the public REST API from api.hubapi.com for ALL regions, including EU.
# The `eu1` in a `pat-eu1-…` token is data residency only; there is no api.eu1.hubapi.com.
DEFAULT_BASE_URL = "https://api.hubapi.com"

# Standard HubSpot association type: email engagement -> contact (HUBSPOT_DEFINED).
EMAIL_TO_CONTACT_ASSOC = 198


def _linkedin_slug(url):
    """The /in/<handle> slug of a LinkedIn profile URL, lowercased, for token search.
    Returns None when there's no recognizable handle."""
    import re
    m = re.search(r"/in/([^/?#]+)", (url or "").strip(), re.I)
    return m.group(1).lower() if m else None


def to_ms_epoch(ts):
    """Normalise a timestamp to epoch-millis (str-able int) for hs_timestamp.

    Accepts epoch seconds/millis (int/str) or ISO-8601 like 2026-06-17T17:54:46.000000Z
    and 2026-06-24T17:27:46Z. Returns None if unparseable.
    """
    if ts is None:
        return None
    if isinstance(ts, (int, float)):
        v = int(ts)
        return v if v > 10 ** 12 else v * 1000
    s = str(ts).strip()
    if s.isdigit():
        v = int(s)
        return v if v > 10 ** 12 else v * 1000
    s = s.replace("Z", "+00:00")
    if "." in s:  # clamp fractional seconds to fromisoformat's 6-digit limit
        base, frac = s.split(".", 1)
        digits, rest = "", ""
        for i, ch in enumerate(frac):
            if ch.isdigit():
                digits += ch
            else:
                rest = frac[i:]
                break
        s = base + "." + digits[:6] + rest
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


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

    def find_company_id_by_domain(self, domain):
        """First company id whose `domain` property equals `domain`, or None
        (including on any API error — callers treat this as best-effort)."""
        domain = (domain or "").strip().lower()
        if not domain:
            return None
        try:
            results, _, _ = self.search_page(
                "companies",
                filters=[{"propertyName": "domain", "operator": "EQ", "value": domain}],
                properties=["domain"], limit=2)
        except HubSpotError:
            return None
        return str(results[0]["id"]) if results else None

    def update_company(self, company_id, properties):
        """PATCH properties onto one company."""
        return self._request("PATCH", f"/crm/v3/objects/companies/{company_id}",
                             body={"properties": dict(properties)})

    # Default property group per object type — HubSpot rejects a create without a
    # group that exists on that object.
    _PROPERTY_GROUPS = {
        "companies": "companyinformation",
        "contacts": "contactinformation",
        "deals": "dealinformation",
    }
    # fieldType -> the `type` HubSpot expects alongside it. Only textarea/text/html
    # are strings; a numeric or boolean property declared as a string silently
    # becomes un-sortable and un-filterable in HubSpot, which defeats the point of
    # writing a score there.
    _PROPERTY_TYPES = {
        "text": "string", "textarea": "string", "html": "string",
        "number": "number", "bool": "bool", "booleancheckbox": "bool",
        "date": "date", "select": "enumeration", "radio": "enumeration",
    }

    def ensure_property(self, object_type, name, label, field_type="textarea",
                        group_name=None, options=None):
        """Idempotently make sure a custom property exists on any object type.

        Reads it first; on 404 creates it (409/duplicate from a concurrent create
        counts as success). Other errors — e.g. a token missing the schema scopes —
        propagate as HubSpotError for the caller to handle.

        Generalizes the old company-only helper: a contact-level field map needs the
        same guarantee for contacts and deals, and needs numbers to be real numbers.
        """
        if object_type not in self._PROPERTY_GROUPS:
            raise ValueError(f"unsupported object type {object_type!r}")
        try:
            self._request("GET", f"/crm/v3/properties/{object_type}/{name}")
            return False  # already there
        except HubSpotError as exc:
            if "HTTP 404" not in str(exc):
                raise
        hs_field = "text" if field_type == "bool" else field_type
        body = {
            "name": name, "label": label,
            "type": self._PROPERTY_TYPES.get(field_type, "string"),
            "fieldType": "booleancheckbox" if field_type == "bool" else hs_field,
            "groupName": group_name or self._PROPERTY_GROUPS[object_type],
        }
        if options:
            body["options"] = options
        try:
            self._request("POST", f"/crm/v3/properties/{object_type}", body=body)
        except HubSpotError as exc:
            msg = str(exc)
            if "HTTP 409" not in msg and "already exists" not in msg.lower():
                raise
        return True

    def ensure_company_property(self, name, label, field_type="textarea",
                                group_name="companyinformation"):
        """Back-compat wrapper — tech_signals and hiring_signals call this."""
        return self.ensure_property("companies", name, label,
                                    field_type=field_type, group_name=group_name)

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

    # ---- email activity logging -----------------------------------------
    def log_email(self, contact_id, *, timestamp, direction, status, subject=None,
                  text=None, html=None, headers=None, owner_id=None):
        """Create one `emails` engagement and associate it to a contact record.

        Logs an email activity on the contact's timeline (CRM v3 emails object).

        contact_id : HubSpot contact record id (str).
        timestamp  : when the email occurred (epoch or ISO; normalised to ms).
        direction  : "EMAIL" (we sent it) or "INCOMING_EMAIL" (the contact sent it).
        status     : "SENT" | "RECEIVED" | "BOUNCED" | "FAILED".
        headers    : dict {"from":{email,firstName,lastName}, "sender":{email},
                     "to":[{email,...}]}. HubSpot derives the read-only
                     hs_email_from_email / hs_email_to_email props FROM this — never
                     set those directly. For alias sends, "from" is the identity that
                     shows on the card and "sender" is the actual sending address.
        owner_id   : hubspot_owner_id (optional). Omit to leave the activity ownerless
                     (logged by the integration) instead of attributing it to a person.
        Returns the new engagement id (str). Raises HubSpotError on failure.
        """
        ms = to_ms_epoch(timestamp)
        if ms is None:  # required by HubSpot — fail clearly rather than POST "None"
            raise HubSpotError(f"missing/unparseable hs_timestamp: {timestamp!r}")
        props = {
            "hs_timestamp": str(ms),
            "hs_email_direction": direction,
            "hs_email_status": status,
        }
        if subject is not None:
            props["hs_email_subject"] = subject
        if text is not None:
            props["hs_email_text"] = text
        if html is not None:
            props["hs_email_html"] = html
        if headers is not None:
            props["hs_email_headers"] = json.dumps(headers)
        if owner_id:
            props["hubspot_owner_id"] = str(owner_id)
        body = {
            "properties": props,
            "associations": [{
                "to": {"id": str(contact_id)},
                "types": [{"associationCategory": "HUBSPOT_DEFINED",
                           "associationTypeId": EMAIL_TO_CONTACT_ASSOC}],
            }],
        }
        payload = self._request("POST", "/crm/v3/objects/emails", body=body)
        return str(payload.get("id"))

    # ---- LinkedIn (HeyReach) activity logging ---------------------------
    def log_communication(self, contact_id, *, timestamp, body, channel_type="LINKEDIN_MESSAGE",
                          logged_from="CRM", owner_id=None):
        """Create one CRM v3 `communications` object (a logged LinkedIn message) and
        associate it to a contact record — the LinkedIn counterpart of log_email.

        contact_id   : HubSpot contact record id (str).
        timestamp    : when the event occurred (epoch or ISO; normalised to ms).
        body         : the activity text (hs_communication_body). Communications carry
                       no inbound/outbound direction property, so any "sent vs received"
                       intent must be conveyed in this text by the caller.
        channel_type : hs_communication_channel_type. LINKEDIN_MESSAGE for LinkedIn.
        owner_id     : hubspot_owner_id (optional). Omit to leave it ownerless (logged
                       by the integration) rather than attributing it to a person.
        Returns the new communication id (str). Raises HubSpotError on failure; if the
        association step fails the just-created object is best-effort deleted so no
        unassociated communication is orphaned (a retry then re-creates cleanly).
        """
        ms = to_ms_epoch(timestamp)
        if ms is None:  # required by HubSpot — fail clearly rather than POST "None"
            raise HubSpotError(f"missing/unparseable hs_timestamp: {timestamp!r}")
        props = {
            "hs_timestamp": str(ms),
            "hs_communication_channel_type": channel_type,
            "hs_communication_logged_from": logged_from,
            "hs_communication_body": body,
        }
        if owner_id:
            props["hubspot_owner_id"] = str(owner_id)
        payload = self._request("POST", "/crm/v3/objects/communications", body={"properties": props})
        comm_id = str(payload.get("id"))
        try:
            self.associate_communication_to_contact(comm_id, contact_id)
        except HubSpotError:
            try:
                self._request("DELETE", f"/crm/v3/objects/communications/{comm_id}")
            except HubSpotError:
                pass
            raise
        return comm_id

    def associate_communication_to_contact(self, comm_id, contact_id):
        """Associate a communication to a contact via the v4 default association — no
        hardcoded numeric type id needed (HubSpot fills in the standard label)."""
        self._request(
            "PUT",
            f"/crm/v4/objects/communications/{comm_id}/associations/default/contacts/{contact_id}")

    # ---- Notes (generic timeline entries) --------------------------------
    def log_note(self, contact_id, *, timestamp, body):
        """Create one CRM v3 `notes` object and associate it to a contact — a plain
        timeline entry for events that aren't an email or a LinkedIn message (e.g.
        "AI SDR stopped outreach"). Mirrors log_communication's contract: returns the
        new note id (str), raises HubSpotError on failure, and best-effort deletes the
        note if the association step fails so no orphan is left behind."""
        ms = to_ms_epoch(timestamp)
        if ms is None:  # required by HubSpot — fail clearly rather than POST "None"
            raise HubSpotError(f"missing/unparseable hs_timestamp: {timestamp!r}")
        payload = self._request("POST", "/crm/v3/objects/notes", body={
            "properties": {"hs_timestamp": str(ms), "hs_note_body": body}})
        note_id = str(payload.get("id"))
        try:
            self._request(
                "PUT",
                f"/crm/v4/objects/notes/{note_id}/associations/default/contacts/{contact_id}")
        except HubSpotError:
            try:
                self._request("DELETE", f"/crm/v3/objects/notes/{note_id}")
            except HubSpotError:
                pass
            raise
        return note_id

    def find_contact_by_linkedin(self, url):
        """Contact id whose LinkedIn-URL property matches `url`, else None. Searches the
        configured property (HUBSPOT_LINKEDIN_PROPERTY, default hs_linkedin_url) by the
        profile slug with CONTAINS_TOKEN so http/https/www/trailing-slash variants match."""
        slug = _linkedin_slug(url)
        if not slug:
            return None
        prop = os.environ.get("HUBSPOT_LINKEDIN_PROPERTY", "hs_linkedin_url")
        body = {
            "filterGroups": [{"filters": [
                {"propertyName": prop, "operator": "CONTAINS_TOKEN", "values": [slug]}]}],
            "properties": [prop], "limit": 1,
        }
        try:
            payload = self._request("POST", "/crm/v3/objects/contacts/search", body=body)
        except HubSpotError:
            return None
        results = payload.get("results") or []
        return str(results[0]["id"]) if results else None

    # ---- owners ---------------------------------------------------------
    def get_owners(self):
        """All HubSpot owners (users/seats), across pages. Owners are read-only."""
        out, after = [], None
        while True:
            params = {"limit": 100}
            if after:
                params["after"] = after
            payload = self._request("GET", "/crm/v3/owners", params)
            out.extend(payload.get("results", []))
            after = (payload.get("paging") or {}).get("next", {}).get("after")
            if not after:
                return out

    def find_owner_id(self, email):
        """Owner id (str) for a user's email, or None. Use to resolve the 'AI SDR' seat."""
        e = (email or "").strip().lower()
        for o in self.get_owners():
            if (o.get("email") or "").lower() == e:
                return str(o.get("id"))
        return None

    # ---- generic search / associations / batch ops -----------------------
    def search_page(self, object_type, *, filters, properties, after=None,
                    sorts=None, limit=100):
        """One page of POST /crm/v3/objects/{type}/search.

        filters    : list of filter dicts (single AND filterGroup).
        properties : property names returned on each hit (no per-object re-fetch).
        Returns (results, next_after, total). Search caps at 10,000 results per
        query — callers needing more must re-window the filters (e.g. by
        hs_timestamp GT <last seen>) and paginate again.
        """
        body = {"filterGroups": [{"filters": list(filters)}],
                "properties": list(properties), "limit": int(limit)}
        if sorts:
            body["sorts"] = sorts
        if after:
            body["after"] = after
        payload = self._request("POST", f"/crm/v3/objects/{object_type}/search", body=body)
        next_after = (payload.get("paging") or {}).get("next", {}).get("after")
        return payload.get("results", []), next_after, payload.get("total")

    def batch_read_associations(self, from_type, to_type, ids):
        """Map {from_id: [to_ids]} via POST /crm/v4/associations/{from}/{to}/batch/read
        (chunks of 100). Ids with no associations are absent from the map."""
        ids = [str(i) for i in ids]
        out = {}
        for i in range(0, len(ids), 100):
            payload = self._request(
                "POST", f"/crm/v4/associations/{from_type}/{to_type}/batch/read",
                body={"inputs": [{"id": x} for x in ids[i:i + 100]]},
            )
            for r in payload.get("results", []):
                frm = str((r.get("from") or {}).get("id"))
                bucket = out.setdefault(frm, [])
                for t in r.get("to", []):
                    tid = t.get("toObjectId") if isinstance(t, dict) else t
                    if tid is not None:
                        bucket.append(str(tid))
        return out

    def batch_read_objects(self, object_type, ids, properties):
        """Read properties for ids of any CRM object type (chunks of 100)."""
        ids = [str(i) for i in ids]
        out = []
        for i in range(0, len(ids), 100):
            payload = self._request(
                "POST", f"/crm/v3/objects/{object_type}/batch/read",
                body={"inputs": [{"id": x} for x in ids[i:i + 100]],
                      "properties": list(properties)},
            )
            out.extend(payload.get("results", []))
        return out

    def batch_update(self, object_type, updates):
        """Batch property updates (chunks of 100). `updates` items are
        {"id": <record id>, "properties": {...}}. Returns the count submitted."""
        n = 0
        for i in range(0, len(updates), 100):
            chunk = updates[i:i + 100]
            self._request("POST", f"/crm/v3/objects/{object_type}/batch/update",
                          body={"inputs": chunk})
            n += len(chunk)
        return n

    def get_property(self, object_type, name):
        """Property definition (GET /crm/v3/properties/{type}/{name}) — used for
        enumeration option -> label maps (deal stage, owner, created-by)."""
        return self._request("GET", f"/crm/v3/properties/{object_type}/{name}")

    # ---- File Manager (files/v3) ----------------------------------------
    def upload_file(self, file_path, folder_path="signal-plays", access="PUBLIC_INDEXABLE",
                    file_name=None):
        """Upload a file to HubSpot File Manager and return {"id", "url", "name"}.

        The returned url is the file's public URL — served from the portal's
        file-hosting domain (a connected domain like files.everworker.ai when one
        is configured in Settings -> Content -> Domains & URLs, otherwise the
        default *.hubspotusercontent* host). Requires the `files` scope on the
        private-app token.

        stdlib multipart POST /files/v3/files with parts: file, options, folderPath.
        """
        import mimetypes
        import uuid
        p = Path(file_path)
        data = p.read_bytes()
        name = file_name or p.name
        ctype = mimetypes.guess_type(name)[0] or "application/octet-stream"
        options = json.dumps({"access": access, "overwrite": True})
        boundary = f"----hs-{uuid.uuid4().hex}"

        def part(headers, payload):
            head = "".join(h + "\r\n" for h in headers)
            return (f"--{boundary}\r\n{head}\r\n").encode() + payload + b"\r\n"

        body = b"".join([
            part([f'Content-Disposition: form-data; name="file"; filename="{name}"',
                  f"Content-Type: {ctype}"], data),
            part(['Content-Disposition: form-data; name="options"',
                  "Content-Type: application/json"], options.encode()),
            part(['Content-Disposition: form-data; name="folderPath"'],
                 folder_path.encode()),
        ]) + f"--{boundary}--\r\n".encode()

        url = self.base_url + "/files/v3/files"
        req = urllib.request.Request(url, data=body, method="POST")
        req.add_header("Authorization", f"Bearer {self.token}")
        req.add_header("Accept", "application/json")
        req.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
        last = None
        for attempt in range(4):
            try:
                with urllib.request.urlopen(req, timeout=120) as resp:
                    payload = json.loads(resp.read().decode("utf-8") or "{}")
                return {"id": payload.get("id"), "url": payload.get("url"),
                        "name": payload.get("name"), "raw": payload}
            except urllib.error.HTTPError as e:
                detail = e.read().decode("utf-8", "replace")[:300]
                if (e.code == 429 or 500 <= e.code < 600) and attempt < 3:
                    time.sleep(2 ** attempt)
                    last = HubSpotError(f"HTTP {e.code} for {url}: {detail}")
                    continue
                raise HubSpotError(f"HTTP {e.code} for {url}: {detail}") from e
            except urllib.error.URLError as e:
                if attempt < 3:
                    time.sleep(2 ** attempt)
                    last = HubSpotError(f"Network error for {url}: {e.reason}")
                    continue
                raise HubSpotError(f"Network error for {url}: {e.reason}") from e
        raise last

    # ---- generic CRM search (engagement audit / reconcile) ---------------
    def search_objects(self, object_type, filters, properties, *, limit=100, after=None,
                       sorts=None):
        """One page of POST /crm/v3/objects/{type}/search. Returns the raw payload
        ({results, paging}). filters is one AND filter group."""
        body = {"filterGroups": [{"filters": filters}],
                "properties": list(properties), "limit": limit}
        if after:
            body["after"] = after
        if sorts:
            body["sorts"] = sorts
        return self._request("POST", f"/crm/v3/objects/{object_type}/search", body=body)

    def iter_contact_engagements(self, object_type, contact_id, properties, *, page_cap=20):
        """All engagements of one type associated with a contact (paginated search
        by the associations.contact pseudo-property)."""
        after = None
        for _ in range(page_cap):
            payload = self.search_objects(
                object_type,
                [{"propertyName": "associations.contact", "operator": "EQ",
                  "value": str(contact_id)}],
                properties, after=after)
            for r in payload.get("results", []):
                yield r
            after = ((payload.get("paging") or {}).get("next") or {}).get("after")
            if not after:
                return

    def find_email_engagement(self, contact_id, ts_ms, *, window_ms=120000,
                              direction=None, subject=None):
        """An existing email engagement on this contact within ±window of ts_ms —
        used to seed the dedup ledger instead of re-creating the engagement.
        direction/subject narrow the match so a nearby unrelated engagement (e.g.
        a native-logged reply a minute from our send) is never adopted."""
        filters = [
            {"propertyName": "associations.contact", "operator": "EQ", "value": str(contact_id)},
            {"propertyName": "hs_timestamp", "operator": "BETWEEN",
             "value": str(int(ts_ms) - window_ms), "highValue": str(int(ts_ms) + window_ms)},
        ]
        if direction:
            filters.append({"propertyName": "hs_email_direction", "operator": "EQ",
                            "value": direction})
        if subject:
            filters.append({"propertyName": "hs_email_subject", "operator": "EQ",
                            "value": str(subject)[:200]})
        payload = self.search_objects(
            "emails", filters,
            ["hs_timestamp", "hs_email_subject", "hs_email_direction"], limit=10)
        results = payload.get("results", [])
        return results[0] if results else None

    # ---- CMS: source files + site pages (signal-play publishing) ---------
    # All of these need the `content` scope on the private-app token.
    def upsert_source_file(self, path, content):
        """Create/overwrite a design-manager file in the PUBLISHED environment
        (multipart PUT /cms/v3/source-code/published/content/{path}). Publishing
        a template file re-renders the pages that use it."""
        import uuid
        boundary = f"----hs-{uuid.uuid4().hex}"
        name = path.rsplit("/", 1)[-1]
        payload = content.encode("utf-8") if isinstance(content, str) else content
        body = (f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="file"; filename="{name}"\r\n'
                f"Content-Type: text/html\r\n\r\n").encode() + payload + \
               f"\r\n--{boundary}--\r\n".encode()
        url = f"{self.base_url}/cms/v3/source-code/published/content/{urllib.parse.quote(path)}"
        req = urllib.request.Request(url, data=body, method="PUT")
        req.add_header("Authorization", f"Bearer {self.token}")
        req.add_header("Accept", "application/json")
        req.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
        last = None
        for attempt in range(4):
            try:
                with urllib.request.urlopen(req, timeout=120) as resp:
                    raw = resp.read().decode("utf-8")
                return json.loads(raw) if raw else {}
            except urllib.error.HTTPError as e:
                detail = e.read().decode("utf-8", "replace")[:300]
                if (e.code == 429 or 500 <= e.code < 600) and attempt < 3:
                    time.sleep(2 ** attempt)
                    last = HubSpotError(f"HTTP {e.code} for {url}: {detail}")
                    continue
                raise HubSpotError(f"HTTP {e.code} for {url}: {detail}") from e
            except urllib.error.URLError as e:
                if attempt < 3:
                    time.sleep(2 ** attempt)
                    last = HubSpotError(f"Network error for {url}: {e.reason}")
                    continue
                raise HubSpotError(f"Network error for {url}: {e.reason}") from e
        raise last

    def get_site_page_by_slug(self, slug):
        """The site page whose slug matches, or None. Tries the server-side slug
        filter first, then falls back to scanning the listing."""
        try:
            payload = self._request("GET", "/cms/v3/pages/site-pages",
                                    params={"slug": slug, "limit": 100})
            for r in payload.get("results", []):
                if (r.get("slug") or "").strip("/") == slug.strip("/"):
                    return r
            if payload.get("results") is not None and "paging" not in payload:
                return None
        except HubSpotError:
            pass
        after = None
        for _ in range(50):  # fallback scan (5k pages max)
            params = {"limit": 100}
            if after:
                params["after"] = after
            payload = self._request("GET", "/cms/v3/pages/site-pages", params=params)
            for r in payload.get("results", []):
                if (r.get("slug") or "").strip("/") == slug.strip("/"):
                    return r
            after = ((payload.get("paging") or {}).get("next") or {}).get("after")
            if not after:
                return None
        return None

    def create_site_page(self, body):
        return self._request("POST", "/cms/v3/pages/site-pages", body=body)

    def update_site_page_draft(self, page_id, body):
        return self._request("PATCH", f"/cms/v3/pages/site-pages/{page_id}/draft", body=body)

    def push_site_page_live(self, page_id):
        """Instant publish: promote the page's draft to live (the 2-step flow's
        second call). Returns the response payload (may be empty)."""
        return self._request("POST", f"/cms/v3/pages/site-pages/{page_id}/draft/push-live")

    def get_site_page(self, page_id):
        return self._request("GET", f"/cms/v3/pages/site-pages/{page_id}")

    def publish_site_page_now(self, page_id):
        """Publish a (draft) site page IMMEDIATELY. v3 has no separate publish-now
        endpoint — the schedule call with a now publishDate is the publish action
        (and in practice it publishes immediately). Falls back to the legacy v2
        publish-action if the v3 call is rejected; if BOTH fail, the raised error
        carries BOTH messages so the real validation problem is never masked."""
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        try:
            return self._request("POST", "/cms/v3/pages/site-pages/schedule",
                                 body={"id": str(page_id), "publishDate": now})
        except HubSpotError as v3_err:
            if not any(f"HTTP {c}" in str(v3_err) for c in (400, 403, 404, 405, 409, 422)):
                raise
            try:
                payload = self._request(
                    "POST", f"/content/api/v2/pages/{page_id}/publish-action",
                    body={"action": "schedule-publish"})
            except HubSpotError as v2_err:
                raise HubSpotError(
                    f"publish failed on both APIs. v3 schedule: {str(v3_err)[:300]} | "
                    f"v2 publish-action: {str(v2_err)[:300]}") from v2_err
            if isinstance(payload, dict):
                payload["_v3_error"] = str(v3_err)[:200]
            return payload
