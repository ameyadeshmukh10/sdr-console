"""Write the researched signal + email touch 1 into the HubSpot CONTACT
property `sdr_signal_notes` (multi-line text, already exists in the portal).

Called after every batch ingest (sdr_batches.py cmd_ingest) and by the
one-time `sdr_batches.py notes-backfill` catch-up. Best-effort: every entry
point catches, logs to stderr, and never raises — a HubSpot failure must
never fail an ingest. Kill switch: SDR_NOTES_HUBSPOT_WRITEBACK=0 (default on).
Latest generation wins (the property mirrors generated/<cid>.json); an asset
with no email touch 1 yields no update, so an existing note is never blanked.
"""

import os
import sys
import threading

NOTES_PROPERTY = "sdr_signal_notes"
NOTES_PROPERTY_LABEL = "SDR Signal Notes"
WRITEBACK_FLAG = "SDR_NOTES_HUBSPOT_WRITEBACK"
MAX_LEN = 65000  # HubSpot multi-line text caps values at 65,536 chars


def log(msg):
    sys.stderr.write(f"[notes] {msg}\n")
    sys.stderr.flush()


def _flag(name, default=True):
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() not in ("0", "false", "no", "off")


def enabled():
    return _flag(WRITEBACK_FLAG, True)


def format_note(asset):
    """Plain-text note: the signal under a "Signal:" label, then the email 1
    body verbatim — no subject line, no body label (user-approved format).
    Returns None when the body is missing/empty so callers skip the contact
    instead of blanking an existing note."""
    body = ((asset.get("email") or {}).get("body1") or "").strip()
    if not body:
        return None
    signal = (asset.get("signal") or "").strip()
    parts = []
    if signal:
        parts.append(f"Signal:\n{signal}")
    parts.append(body)
    return "\n\n".join(parts)[:MAX_LEN]


def note_update(contact_id, asset):
    """Build one batch_update input, or None when there is nothing to write."""
    note = format_note(asset)
    if not note:
        return None
    return {"id": str(contact_id), "properties": {NOTES_PROPERTY: note}}


# ---- HubSpot write (best-effort; never raises) --------------------------------
_HS_LOCK = threading.Lock()
_HS_CLIENT = None
_PROPERTY_READY = False


def sync_contacts(updates, progress=None):
    """Batch-PATCH sdr_signal_notes onto contacts. Returns
    {"ok", "synced", "failed", ...}; never raises. Chunks of 100; one bad id
    (deleted/merged contact) 4xx-fails a whole HubSpot batch call, so a failed
    chunk retries one-by-one to isolate it."""
    global _HS_CLIENT, _PROPERTY_READY
    try:
        if not updates:
            return {"ok": True, "synced": 0, "failed": 0}
        if not os.environ.get("HUBSPOT_ACCESS_TOKEN"):
            return {"ok": False, "reason": "no_token", "synced": 0, "failed": len(updates)}
        import hubspot_client as hc
        with _HS_LOCK:
            if _HS_CLIENT is None:
                _HS_CLIENT = hc.HubSpotClient()
            client = _HS_CLIENT
            if not _PROPERTY_READY:
                try:  # property already exists in the portal; a token without
                    #   schema scopes must not block the actual updates
                    client.ensure_contact_property(NOTES_PROPERTY, NOTES_PROPERTY_LABEL)
                except Exception as exc:  # noqa: BLE001
                    log(f"ensure property skipped: {str(exc)[:160]}")
                _PROPERTY_READY = True
        synced = failed = 0
        for i in range(0, len(updates), 100):
            chunk = updates[i:i + 100]
            try:
                client.batch_update("contacts", chunk)
                synced += len(chunk)
            except Exception as exc:  # noqa: BLE001
                log(f"chunk of {len(chunk)} failed ({str(exc)[:120]}); retrying singly")
                for u in chunk:
                    try:
                        client.batch_update("contacts", [u])
                        synced += 1
                    except Exception as exc1:  # noqa: BLE001
                        failed += 1
                        log(f"contact {u['id']} failed: {str(exc1)[:160]}")
            if progress:
                progress(synced + failed, len(updates))
        return {"ok": failed == 0, "synced": synced, "failed": failed}
    except Exception as exc:  # noqa: BLE001 — write-back must never fail the caller
        log(f"sync failed: {exc}")
        return {"ok": False, "error": str(exc)[:300], "synced": 0, "failed": len(updates)}
