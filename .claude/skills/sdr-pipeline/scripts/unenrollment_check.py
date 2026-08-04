"""Unenrollment checker — the `everworker_tag` suppression rule.

RevOps maintains the HubSpot CONTACT property `everworker_tag` (enumeration,
"true"/"false"). When it is "false" — the contact booked a meeting, became an
opportunity, or must otherwise be left alone — the AI SDR stops touching them:

  1. sweep: search HubSpot for contacts where everworker_tag = "false" (only the
     explicit "false" matches; unset/true contacts are untouched),
  2. Email Bison: resolve the lead by email (bison_lead_map cache, then a live
     lookup), read its scheduled emails, and stop-future-emails in every campaign
     that still has steps queued ('scheduled' / 'sending paused'),
  3. HeyReach: find the lead's campaigns by LinkedIn profile URL (email fallback)
     and StopLeadInCampaign in each one that isn't already finished,
  4. log one HubSpot timeline note per contact actually stopped (best-effort,
     UNENROLL_HUBSPOT_NOTE=0 disables),
  5. record every outcome in the unenrollment_log ledger (pipeline.db) — the
     sweeps are idempotent: a 'done' row is skipped until it is older than
     UNENROLL_RECHECK_HOURS (default 24 — so a contact re-enrolled while still
     flagged is re-stopped within a day), 'skipped_unconfigured' rows re-arm
     automatically once the channel's API key lands, a 'failed' row retries
     every sweep, and --force re-checks everything now.

The ledger is one-way: flipping the tag back to "true" only re-permits FUTURE
enrollment (the enroll gate's live check wins) — already-stopped sequences stay
stopped. This module also exports suppressed_set(), the pre-enrollment gate used
by enroll.py / sdr_batches.py: live HubSpot tag check first, ledger fallback when
HubSpot is unreachable (fail-open — the 30-min sweep is the backstop).

The web server runs this every UNENROLL_CHECK_MINUTES (default 30) and from
POST /api/unenroll/run. Sweep progress goes to STDERR (the server streams it
live into Railway logs + /api/unenroll/status); the LAST stdout line is the
JSON summary (with --json) — same summary contract as the other pipeline
scripts, streamed like the tech/hiring detectors.

  python3 unenrollment_check.py [--json] [--dry-run] [--limit N] [--force]
      [--contact-id ID]
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(SCRIPTS.parents[1] / "email-bison" / "scripts"))  # bison_client

import batch_db as db  # noqa: E402  (stdlib-only — keeps module import cheap)

RULE = "everworker_tag"
WINDOW_SOFT_CAP = 9800  # re-window before HubSpot's 10k search-result ceiling
# Bison scheduled-email statuses that mean "a step is still going to send".
ACTIVE_SCHEDULED = {"scheduled", "sending paused"}
# HeyReach campaign/lead states where stopping is pointless.
HEYREACH_INERT = {"FINISHED", "STOPPED", "CANCELED", "CANCELLED", "FAILED"}

CHANNELS = ("bison", "heyreach")


def _load_dotenv():
    for parent in [SCRIPTS, *SCRIPTS.parents]:
        env_path = parent / ".env"
        if env_path.is_file():
            for raw in env_path.read_text().splitlines():
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                os.environ.setdefault(k.strip(), v.split(" #")[0].strip().strip('"').strip("'"))
            return


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def tag_property():
    return os.environ.get("HUBSPOT_EVERWORKER_TAG_PROPERTY", "everworker_tag") or "everworker_tag"


def linkedin_property():
    return os.environ.get("HUBSPOT_LINKEDIN_PROPERTY", "hs_linkedin_url") or "hs_linkedin_url"


def _is_false(value):
    return str(value or "").strip().lower() == "false"


# --------------------------------------------------------------------------
# Pre-enrollment gate (imported by enroll.py / sdr_batches.py)
# --------------------------------------------------------------------------

def suppressed_set(contact_ids, conn=None):
    """Which of these HubSpot contact ids must NOT be enrolled?

    Returns (suppressed_ids: set[str], live_ok: bool). Live HubSpot check first —
    suppressed iff the tag is the literal "false", so a tag flipped back to "true"
    re-permits enrollment immediately. When HubSpot is unreachable the local
    unenrollment ledger is the fallback (live_ok=False); if that fails too the
    gate is open (fail-open — the 30-minute sweep is the backstop).
    """
    all_ids = [str(c) for c in contact_ids if c]
    if not all_ids:
        return set(), True

    # LOCAL suppression first, and unconditionally. Someone hitting "do not contact"
    # in the console expects that to hold whether or not HubSpot answers, and it is
    # a local fact — there is nothing to check remotely. The live tag read below can
    # only ADD to this set, never clear it: a CRM tag flipped back to "true"
    # re-permits a CRM-driven suppression, not a decision a human made here.
    #
    # Checked against EVERY id, not just the CRM-shaped ones. The digit filter below
    # exists for HubSpot's sake; applying it here too meant a contact whose id isn't
    # numeric (imported from a file, created by enrichment, any demo profile) could
    # be marked do-not-contact and still be enrolled.
    local = set()
    try:
        close = conn is None
        _c = db.connect() if close else conn
        try:
            local = {i for i in all_ids if i in db.suppressed_contact_ids(_c)}
        finally:
            if close:
                _c.close()
    except Exception as e:  # noqa: BLE001 — never block the gate on a read
        print(f"[unenroll-gate] local engagement check failed "
              f"({type(e).__name__}: {str(e)[:120]})", flush=True)

    # Digits only: one malformed id would 400 the whole HubSpot batch read and
    # needlessly degrade every other contact's live check to the ledger.
    ids = [i for i in all_ids if i.isdigit()]
    if not ids:
        return local, True

    tag = tag_property()
    try:
        from hubspot_client import HubSpotClient  # noqa: E402 (lazy — boot rule)
        client = HubSpotClient()
        rows = client.batch_read_contacts(ids, [tag])
        return local | {str(r.get("id")) for r in rows
                        if _is_false((r.get("properties") or {}).get(tag))}, True
    except Exception as e:  # noqa: BLE001 - any failure degrades to the ledger
        print(f"[unenroll-gate] live {tag} check unavailable "
              f"({type(e).__name__}: {str(e)[:120]}) — using the local ledger", flush=True)
    try:
        close = conn is None
        if conn is None:
            conn = db.connect()
        try:
            ledger = db.suppressed_contact_ids(conn, rule=RULE)
        finally:
            if close:
                conn.close()
        return local | {i for i in ids if i in ledger}, False
    except Exception as e:  # noqa: BLE001
        print(f"[unenroll-gate] ledger fallback failed ({type(e).__name__}: "
              f"{str(e)[:120]}) — gate open", flush=True)
        return local, False


# --------------------------------------------------------------------------
# Sweep: find flagged contacts
# --------------------------------------------------------------------------

def fetch_flagged_contacts(client, *, limit=None):
    """Every contact with everworker_tag = "false", ascending by hs_object_id.

    Pages via `after`; when a window nears the 10k search cap it restarts the
    query with hs_object_id GT <last seen> (same pattern as the attribution
    sync's fetch_emails)."""
    tag, li = tag_property(), linkedin_property()
    props = ["email", "firstname", "lastname", li, tag]
    out, window_start = [], 0
    while True:
        after, window_count = None, 0
        while True:
            results, after, _total = client.search_page(
                "contacts",
                filters=[{"propertyName": tag, "operator": "EQ", "value": "false"},
                         {"propertyName": "hs_object_id", "operator": "GT",
                          "value": str(window_start)}],
                properties=props,
                after=after,
                sorts=[{"propertyName": "hs_object_id", "direction": "ASCENDING"}])
            out.extend(results)
            if limit and len(out) >= limit:
                return out[:limit]
            window_count += len(results)
            if not after:
                return out
            if window_count >= WINDOW_SOFT_CAP:
                break  # restart the search past this window
        try:
            new_start = max(int(r["id"]) for r in out)
        except (ValueError, KeyError):
            return out
        if new_start <= window_start:  # no progress — bail rather than loop
            print(f"[unenroll] window restart made no progress at {window_start} — stopping",
                  file=sys.stderr, flush=True)
            return out
        window_start = new_start


# --------------------------------------------------------------------------
# Channel stops
# --------------------------------------------------------------------------

def _bison_lead_id(conn, bison, email, contact_id, *, dry_run=False):
    """email -> Bison lead id via the local cache, live lookup as fallback."""
    row = conn.execute("SELECT lead_id FROM bison_lead_map WHERE email=?",
                       (email,)).fetchone()
    if row and row["lead_id"]:
        return int(row["lead_id"])
    lead = bison.find_lead_by_email(email)
    if lead and lead.get("id") is not None:
        if not dry_run:  # dry runs write nothing, not even the benign cache
            db.upsert_lead_map(conn, email, lead["id"], contact_id)
            conn.commit()
        return int(lead["id"])
    return None


def stop_bison(conn, bison, contact_id, email, *, dry_run):
    """Stop future Bison emails in every campaign with steps still queued.
    Returns (action, campaign_ids | None)."""
    if not email:
        return "not_found", None
    lead_id = _bison_lead_id(conn, bison, email, contact_id, dry_run=dry_run)
    if lead_id is None:
        return "not_found", None
    rows = bison.lead_scheduled_emails(lead_id)
    campaign_ids = sorted({r.get("campaign_id") for r in rows
                           if r.get("campaign_id") is not None
                           and str(r.get("status") or "").lower() in ACTIVE_SCHEDULED})
    if not campaign_ids:
        return "not_active", None
    if not dry_run:
        for cid in campaign_ids:
            bison.stop_future_emails(cid, [lead_id])
    return "stopped", campaign_ids


def stop_heyreach(heyreach, linkedin_url, email, *, dry_run):
    """Stop the lead in every HeyReach campaign that could still message them.
    Returns (action, campaign_ids | None)."""
    if not (linkedin_url or email):
        return "not_found", None
    payload = heyreach.get_campaigns_for_lead(
        profile_url=linkedin_url or None, email=None if linkedin_url else email) or {}
    items = payload.get("items") or []
    if not items:
        return "not_found", None
    stopped, unidentifiable = [], False
    for item in items:
        if not isinstance(item, dict):
            continue
        campaign_id = item.get("campaignId", item.get("id"))
        if campaign_id is None:
            continue
        state = str(item.get("leadStatus") or item.get("status") or "").upper()
        if state in HEYREACH_INERT:
            continue
        # StopLeadInCampaign needs a lead identifier. With no LinkedIn URL (email
        # lookup) the campaign item may not carry one — that's terminal for this
        # contact, not a retryable failure (never raise into the failed/retry loop).
        member_id = item.get("leadMemberId")
        lead_url = linkedin_url or item.get("profileUrl") \
            or (item.get("lead") or {}).get("profileUrl")
        if not (member_id or lead_url):
            unidentifiable = True
            continue
        if not dry_run:
            heyreach.stop_lead_in_campaign(campaign_id, lead_member_id=member_id,
                                           lead_url=lead_url)
        stopped.append(campaign_id)
    if stopped:
        return "stopped", stopped
    return ("no_identifier", None) if unidentifiable else ("not_active", None)


# --------------------------------------------------------------------------
# The sweep itself
# --------------------------------------------------------------------------

def _recheck_hours():
    try:
        return max(0, float(os.environ.get("UNENROLL_RECHECK_HOURS", "24") or 24))
    except ValueError:
        return 24.0


def _handled(conn, dedup_key, configured):
    """True if this (rule, channel, contact) needs no work this sweep. A 'done'
    row is skipped — except action='skipped_unconfigured', which re-arms once the
    channel's API key is configured, and any row older than UNENROLL_RECHECK_HOURS
    (default 24), which is re-verified so a contact re-enrolled while still
    flagged (tag flip-flop, manual enrollment outside the console) is re-stopped
    within a day instead of never. 0 = re-verify every sweep."""
    row = conn.execute(
        "SELECT action, status, created_at FROM unenrollment_log WHERE dedup_key=?",
        (dedup_key,)).fetchone()
    if not row or row["status"] != "done":
        return False
    if configured and row["action"] == "skipped_unconfigured":
        return False
    hours = _recheck_hours()
    try:
        age = datetime.now(timezone.utc) - datetime.strptime(
            row["created_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        if age.total_seconds() >= hours * 3600:
            return False
    except (TypeError, ValueError):
        return False  # unparseable timestamp — re-verify rather than trust it
    return True


def _note_body(bison_result, heyreach_result):
    parts = ["AI SDR unenrollment: stopped outreach (everworker_tag = false)."]
    action, cids = bison_result
    if action == "stopped":
        parts.append(f"Email Bison: stopped in campaign(s) {', '.join(map(str, cids))}.")
    action, cids = heyreach_result
    if action == "stopped":
        parts.append(f"HeyReach (LinkedIn): stopped in campaign(s) {', '.join(map(str, cids))}.")
    return " ".join(parts)


def run_check(*, dry_run=False, limit=None, force=False, contact_id=None):
    from hubspot_client import HubSpotClient  # noqa: E402 (lazy — boot rule)
    client = HubSpotClient()  # raises with a clear message when the token is unset

    bison = heyreach = None
    if os.environ.get("EMAILBISON_API_KEY"):
        from bison_client import BisonClient  # noqa: E402
        bison = BisonClient()
    if os.environ.get("HEYREACH_API_KEY"):
        from heyreach_client import HeyReachClient  # noqa: E402
        heyreach = HeyReachClient()
    note_enabled = (os.environ.get("UNENROLL_HUBSPOT_NOTE", "1") or "1").strip().lower() \
        not in ("0", "false", "no")

    started = time.time()
    tag, li = tag_property(), linkedin_property()

    if contact_id:
        rows = client.batch_read_contacts([contact_id], ["email", "firstname", "lastname", li, tag])
        contacts = [r for r in rows if _is_false((r.get("properties") or {}).get(tag))]
        if not contacts:
            skipped = [r for r in rows]
            why = (f"{tag}={((skipped[0].get('properties') or {}).get(tag))!r}"
                   if skipped else "contact not found")
            print(f"[unenroll] contact {contact_id} not eligible ({why}) — nothing to do",
                  file=sys.stderr, flush=True)
    else:
        contacts = fetch_flagged_contacts(client, limit=limit)
    print(f"[unenroll] {len(contacts)} contact(s) flagged {tag}=false"
          + (" (dry-run)" if dry_run else ""), file=sys.stderr, flush=True)

    conn = db.connect()
    db.init_schema(conn)

    counts = {ch: {"stopped": 0, "not_active": 0, "not_found": 0, "no_identifier": 0,
                   "failed": 0, "skipped_unconfigured": 0} for ch in CHANNELS}
    checked = skipped_ledger = notes_logged = 0
    errors = []

    for c in contacts:
        cid = str(c.get("id"))
        props = c.get("properties") or {}
        email = (props.get("email") or "").strip().lower()
        linkedin_url = (props.get(li) or "").strip()
        keys = {ch: f"{RULE}:{ch}:{cid}" for ch in CHANNELS}
        configured = {"bison": bison is not None, "heyreach": heyreach is not None}
        todo = [ch for ch in CHANNELS
                if force or not _handled(conn, keys[ch], configured[ch])]
        if not todo:
            skipped_ledger += 1
            continue
        checked += 1

        results = {}
        for ch in todo:
            if not configured[ch]:
                results[ch] = ("skipped_unconfigured", None)
                counts[ch]["skipped_unconfigured"] += 1
                if not dry_run:
                    db.record_unenrollment(conn, keys[ch], RULE, ch, cid,
                                           "skipped_unconfigured", "done",
                                           email=email, linkedin_url=linkedin_url)
                continue
            try:
                if ch == "bison":
                    action, campaign_ids = stop_bison(conn, bison, cid, email,
                                                      dry_run=dry_run)
                else:
                    action, campaign_ids = stop_heyreach(heyreach, linkedin_url, email,
                                                         dry_run=dry_run)
                results[ch] = (action, campaign_ids)
                counts[ch][action] += 1
                if not dry_run:
                    db.record_unenrollment(conn, keys[ch], RULE, ch, cid, action, "done",
                                           email=email, linkedin_url=linkedin_url,
                                           campaign_ids=campaign_ids)
            except Exception as e:  # noqa: BLE001 - one contact never aborts the sweep
                results[ch] = ("failed", None)
                counts[ch]["failed"] += 1
                err = f"{type(e).__name__}: {str(e)[:300]}"
                errors.append(f"{ch} {email or cid}: {err}")
                if not dry_run:
                    db.record_unenrollment(conn, keys[ch], RULE, ch, cid, "failed",
                                           "failed", email=email,
                                           linkedin_url=linkedin_url, error=err)

        stops = {ch: r for ch, r in results.items() if r[0] == "stopped"}
        if stops and not dry_run and note_enabled:
            try:
                client.log_note(cid, timestamp=int(time.time() * 1000),
                                body=_note_body(results.get("bison", (None, None)),
                                                results.get("heyreach", (None, None))))
                notes_logged += 1
            except Exception as e:  # noqa: BLE001 - the note is best-effort
                errors.append(f"note {email or cid}: {type(e).__name__}: {str(e)[:200]}")
        line = " ".join(f"{ch}={r[0]}" + (f"{r[1]}" if r[1] else "")
                        for ch, r in results.items())
        print(f"[unenroll] {email or cid}: {line}", file=sys.stderr, flush=True)

    conn.close()
    failed_total = sum(counts[ch]["failed"] for ch in CHANNELS)
    return {"ok": failed_total == 0, "rule": RULE, "dry_run": dry_run,
            "flagged": len(contacts), "checked": checked,
            "skipped_ledger": skipped_ledger,
            "bison": counts["bison"], "heyreach": counts["heyreach"],
            "notes_logged": notes_logged, "errors": errors[:20],
            "duration_s": round(time.time() - started, 1), "at": now_iso()}


def main(argv=None):
    ap = argparse.ArgumentParser(description="Stop outreach for everworker_tag=false contacts")
    ap.add_argument("--json", action="store_true", help="print the summary as JSON (last line)")
    ap.add_argument("--dry-run", action="store_true",
                    help="report what would be stopped; no API writes, no ledger rows")
    ap.add_argument("--limit", type=int, default=None, help="max flagged contacts to process")
    ap.add_argument("--force", action="store_true",
                    help="re-process contacts already handled in the ledger")
    ap.add_argument("--contact-id", default=None, help="check a single HubSpot contact id")
    args = ap.parse_args(argv)

    _load_dotenv()
    try:
        summary = run_check(dry_run=args.dry_run, limit=args.limit,
                            force=args.force, contact_id=args.contact_id)
    except Exception as e:  # noqa: BLE001 - the summary line must always print
        summary = {"ok": False, "rule": RULE, "dry_run": args.dry_run,
                   "error": f"{type(e).__name__}: {str(e)[:500]}", "at": now_iso()}
    print(json.dumps(summary) if args.json else summary)
    return 0 if summary.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
