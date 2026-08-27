"""Unenrollment checker — the `everworker_tag` suppression rule.

RevOps maintains the HubSpot CONTACT property `everworker_tag` (enumeration,
"true"/"false"). When it is "false" — the contact booked a meeting, became an
opportunity, or must otherwise be left alone — the AI SDR stops touching them:

  1. sweep: search HubSpot for contacts where everworker_tag = "false" (only the
     explicit "false" matches; unset/true contacts are untouched). Tens of
     thousands of contacts can be flagged at once (a RevOps bulk workflow), so
     the sweep queues NEVER-CHECKED contacts first, newest first — a contact
     flagged yesterday is the one still mid-sequence — then rotates through
     failed/re-check retries oldest-first, capped per sweep
     (UNENROLL_FRESH_LIMIT / UNENROLL_RETRY_LIMIT) so a sweep always finishes
     inside the server's 1h timeout instead of being killed mid-backlog,
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
     automatically once the channel's API key lands, 'failed' rows and stale
     re-checks rotate through the capped retry queue oldest-first, and --force
     (implied by --contact-id) re-checks everything now, uncapped.

Resilience (each of these failure modes previously killed or starved sweeps):
ledger writes retry SQLite lock contention and NEVER abort the sweep; per-channel
API calls are paced (UNENROLL_PACE_S, default 0.25s — HeyReach caps at 300/min);
and a channel that fails UNENROLL_CIRCUIT_THRESHOLD (default 8) times in a row is
deferred for the rest of the sweep WITHOUT ledger rows, so those contacts are
retried at full priority next sweep instead of minting thousands of failed rows.

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
import sqlite3
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


def _env_int(name, default):
    try:
        return max(0, int(os.environ.get(name, "") or default))
    except ValueError:
        return default


def _env_float(name, default):
    try:
        return max(0.0, float(os.environ.get(name, "") or default))
    except ValueError:
        return default


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
    # Digits only: one malformed id would 400 the whole HubSpot batch read and
    # needlessly degrade every other contact's live check to the ledger.
    ids = [str(c) for c in contact_ids if c and str(c).isdigit()]
    if not ids:
        return set(), True
    tag = tag_property()
    try:
        from hubspot_client import HubSpotClient  # noqa: E402 (lazy — boot rule)
        client = HubSpotClient()
        rows = client.batch_read_contacts(ids, [tag])
        return {str(r.get("id")) for r in rows
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
        return {i for i in ids if i in ledger}, False
    except Exception as e:  # noqa: BLE001
        print(f"[unenroll-gate] ledger fallback failed ({type(e).__name__}: "
              f"{str(e)[:120]}) — gate open", flush=True)
        return set(), False


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
                sorts=[{"propertyName": "hs_object_id", "direction": "ASCENDING"}],
                limit=200)  # search max page size — tens of thousands can be flagged
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
            try:  # cache write is best-effort — a locked DB must not fail the stop
                db.upsert_lead_map(conn, email, lead["id"], contact_id)
                conn.commit()
            except sqlite3.Error:
                try:
                    conn.rollback()
                except sqlite3.Error:
                    pass
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


def _ledger_snapshot(conn):
    """The rule's whole ledger in ONE read: dedup_key -> {action, status, created_at}.
    Even at tens of thousands of contacts this fits trivially in memory, and it
    replaces two SELECTs per contact — during the sweep the DB is only touched to
    write outcomes, keeping lock windows tiny."""
    return {r["dedup_key"]: {"action": r["action"], "status": r["status"],
                             "created_at": r["created_at"]}
            for r in conn.execute(
                "SELECT dedup_key, action, status, created_at "
                "FROM unenrollment_log WHERE rule=?", (RULE,))}


def _needs_work(row, configured):
    """Does this ledger row (None = never attempted) need attention this sweep?
    A 'done' row is skipped — except action='skipped_unconfigured', which re-arms
    once the channel's API key is configured, and any row older than
    UNENROLL_RECHECK_HOURS (default 24), which is re-verified so a contact
    re-enrolled while still flagged (tag flip-flop, manual enrollment outside the
    console) is re-stopped within a day instead of never. 0 = re-verify every
    sweep."""
    if row is None or row["status"] != "done":
        return True
    if configured and row["action"] == "skipped_unconfigured":
        return True
    hours = _recheck_hours()
    try:
        age = datetime.now(timezone.utc) - datetime.strptime(
            row["created_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return True  # unparseable timestamp — re-verify rather than trust it
    return age.total_seconds() >= hours * 3600


def _safe_record(conn, dedup_key, *args, **kwargs):
    """record_unenrollment that never kills the sweep. Retries SQLite write-slot
    contention with backoff (another writer — the hourly activity sync, the
    webhook drain — can hold the lock past busy_timeout); anything else gives up
    after one try. Returns True when the row landed. The channel stop has already
    happened by the time this runs, and one 'database is locked' propagating from
    here used to abort ENTIRE sweeps — the sweep must outlive ledger hiccups."""
    last = None
    for attempt in range(4):
        try:
            db.record_unenrollment(conn, dedup_key, *args, **kwargs)
            return True
        except sqlite3.OperationalError as e:
            last = e
            if "locked" not in str(e).lower() or attempt >= 3:
                break
            time.sleep(min(1.0 * (2 ** attempt), 8.0))
        except sqlite3.Error as e:
            last = e
            break
    try:
        conn.rollback()  # clear any half-open transaction so later writes start clean
    except sqlite3.Error:
        pass
    print(f"[unenroll] ledger write failed for {dedup_key}: "
          f"{type(last).__name__}: {str(last)[:160]}", file=sys.stderr, flush=True)
    return False


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

    configured = {"bison": bison is not None, "heyreach": heyreach is not None}
    ledger = {} if force else _ledger_snapshot(conn)

    # Partition into two queues. `fresh` = at least one configured channel has NO
    # ledger row: the contact was never even attempted — processed FIRST, newest
    # id first, because a contact RevOps just flagged is exactly the one still
    # mid-sequence (the bug this ordering fixes: with 20k+ flagged and a wall of
    # retrying failures ahead of them in ascending-id order, newly flagged
    # contacts were never reached before the sweep died). `retries` = failed rows
    # and stale-done re-checks — rotated oldest ledger entry first (every write
    # refreshes created_at, so the backlog cycles) and capped per sweep so the
    # sweep finishes inside the server's 1h timeout.
    fresh, retries, skipped_ledger = [], [], 0
    for c in contacts:
        cid = str(c.get("id"))
        rows = {ch: ledger.get(f"{RULE}:{ch}:{cid}") for ch in CHANNELS}
        todo = [ch for ch in CHANNELS if _needs_work(rows[ch], configured[ch])]
        if not todo:
            skipped_ledger += 1
            continue
        if any(rows[ch] is None for ch in todo):
            fresh.append((c, todo))
        else:
            retries.append((c, todo, min((rows[ch]["created_at"] or "") for ch in todo)))
    fresh.sort(key=lambda it: int(it[0]["id"]) if str(it[0].get("id", "")).isdigit() else 0,
               reverse=True)
    retries.sort(key=lambda it: it[2])

    fresh_cap = 0 if force else _env_int("UNENROLL_FRESH_LIMIT", 1000)
    retry_cap = 0 if force else _env_int("UNENROLL_RETRY_LIMIT", 500)
    fresh_work = fresh[:fresh_cap] if fresh_cap else fresh
    retry_work = retries[:retry_cap] if retry_cap else retries
    deferred_fresh = len(fresh) - len(fresh_work)
    deferred_retry = len(retries) - len(retry_work)
    work = fresh_work + [(c, todo) for c, todo, _ in retry_work]
    print(f"[unenroll] queue: {len(fresh)} never-checked (doing {len(fresh_work)}, "
          f"newest first), {len(retries)} retries/re-checks (doing {len(retry_work)}, "
          f"oldest first), {skipped_ledger} already handled",
          file=sys.stderr, flush=True)

    counts = {ch: {"stopped": 0, "not_active": 0, "not_found": 0, "no_identifier": 0,
                   "failed": 0, "skipped_unconfigured": 0, "deferred": 0}
              for ch in CHANNELS}
    checked = notes_logged = ledger_errors = 0
    errors = []
    # Circuit breaker: after N consecutive failures on a channel (a HeyReach 429
    # storm, Bison down) stop calling it for the rest of the sweep. Deferred
    # contacts get NO ledger row, so the next sweep — with a fresh circuit —
    # picks them right back up; without this, one outage minted thousands of
    # 'failed' rows that every later sweep burned its whole budget retrying.
    threshold = _env_int("UNENROLL_CIRCUIT_THRESHOLD", 8)
    # Pacing: minimum gap between calls per channel (HeyReach caps at 300/min —
    # unpaced per-contact lookups turned big sweeps into guaranteed 429 storms).
    pace = _env_float("UNENROLL_PACE_S", 0.25)
    consec = {ch: 0 for ch in CHANNELS}
    tripped = set()
    last_call = {ch: 0.0 for ch in CHANNELS}

    for c, todo in work:
        cid = str(c.get("id"))
        props = c.get("properties") or {}
        email = (props.get("email") or "").strip().lower()
        linkedin_url = (props.get(li) or "").strip()
        keys = {ch: f"{RULE}:{ch}:{cid}" for ch in CHANNELS}
        checked += 1

        results = {}
        for ch in todo:
            if not configured[ch]:
                results[ch] = ("skipped_unconfigured", None)
                counts[ch]["skipped_unconfigured"] += 1
                if not dry_run and not _safe_record(
                        conn, keys[ch], RULE, ch, cid, "skipped_unconfigured", "done",
                        email=email, linkedin_url=linkedin_url):
                    ledger_errors += 1
                continue
            if ch in tripped:
                results[ch] = ("deferred", None)
                counts[ch]["deferred"] += 1
                continue
            if pace > 0:
                wait = last_call[ch] + pace - time.time()
                if wait > 0:
                    time.sleep(wait)
            last_call[ch] = time.time()
            try:
                if ch == "bison":
                    action, campaign_ids = stop_bison(conn, bison, cid, email,
                                                      dry_run=dry_run)
                else:
                    action, campaign_ids = stop_heyreach(heyreach, linkedin_url, email,
                                                         dry_run=dry_run)
                consec[ch] = 0
                results[ch] = (action, campaign_ids)
                counts[ch][action] += 1
                if not dry_run and not _safe_record(
                        conn, keys[ch], RULE, ch, cid, action, "done",
                        email=email, linkedin_url=linkedin_url,
                        campaign_ids=campaign_ids):
                    ledger_errors += 1
            except Exception as e:  # noqa: BLE001 - one contact never aborts the sweep
                results[ch] = ("failed", None)
                counts[ch]["failed"] += 1
                consec[ch] += 1
                err = f"{type(e).__name__}: {str(e)[:300]}"
                errors.append(f"{ch} {email or cid}: {err}")
                if not dry_run and not _safe_record(
                        conn, keys[ch], RULE, ch, cid, "failed", "failed",
                        email=email, linkedin_url=linkedin_url, error=err):
                    ledger_errors += 1
                if threshold and consec[ch] >= threshold and ch not in tripped:
                    tripped.add(ch)
                    msg = (f"{ch}: {threshold} consecutive failures — circuit open, "
                           f"deferring remaining {ch} checks to the next sweep")
                    errors.append(msg)
                    print(f"[unenroll] {msg}", file=sys.stderr, flush=True)

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
    return {"ok": failed_total == 0 and ledger_errors == 0, "rule": RULE,
            "dry_run": dry_run, "flagged": len(contacts), "checked": checked,
            "skipped_ledger": skipped_ledger,
            "queue": {"fresh": len(fresh), "retries": len(retries),
                      "deferred_fresh": deferred_fresh,
                      "deferred_retry": deferred_retry},
            "bison": counts["bison"], "heyreach": counts["heyreach"],
            "notes_logged": notes_logged, "ledger_errors": ledger_errors,
            "errors": errors[:20],
            "duration_s": round(time.time() - started, 1), "at": now_iso()}


def main(argv=None):
    ap = argparse.ArgumentParser(description="Stop outreach for everworker_tag=false contacts")
    ap.add_argument("--json", action="store_true", help="print the summary as JSON (last line)")
    ap.add_argument("--dry-run", action="store_true",
                    help="report what would be stopped; no API writes, no ledger rows")
    ap.add_argument("--limit", type=int, default=None, help="max flagged contacts to process")
    ap.add_argument("--force", action="store_true",
                    help="re-process contacts already handled in the ledger")
    ap.add_argument("--contact-id", default=None,
                    help="check a single HubSpot contact id now (implies --force)")
    args = ap.parse_args(argv)

    _load_dotenv()
    try:
        summary = run_check(dry_run=args.dry_run, limit=args.limit,
                            force=args.force or bool(args.contact_id),
                            contact_id=args.contact_id)
    except Exception as e:  # noqa: BLE001 - the summary line must always print
        summary = {"ok": False, "rule": RULE, "dry_run": args.dry_run,
                   "error": f"{type(e).__name__}: {str(e)[:500]}", "at": now_iso()}
    print(json.dumps(summary) if args.json else summary)
    return 0 if summary.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
