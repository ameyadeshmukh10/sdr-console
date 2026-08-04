"""Simulated data sources for demo mode.

A demo has to be able to DO the things the product does — pull contacts from the
CRM, find new buyers via enrichment, scan accounts for signal — or it can only ever
show finished state, never the act of building a campaign. But it must do them
without touching HubSpot, Clay, Prospeo or a mailbox.

So each source has a stand-in here that:
  * reads candidates from a fixture pool in the profile (`crm_pool.json`,
    `clay_pool.json`),
  * writes results into the PROFILE'S OWN pipeline.db, never the live one,
  * meters credits into the profile's own ledger so the spend view moves,
  * and returns the same response shape as the real path, so the UI is identical.

The last point is the one that keeps this honest: nothing in the frontend knows a
demo is running. If the real endpoint's shape changes, these have to change too, and
a mismatch shows up as a broken demo rather than a plausible-looking lie.

Timing is deliberately not instant. The real Clay path takes minutes and the UI has
a progress bar for it; returning immediately would demo a product that does not
exist. Each simulated job advances on a timer at roughly the real cadence.
"""

import json
import random
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import batch_db as db
import campaigns as C

# Roughly how long the real thing takes per unit, so progress bars behave.
SECONDS_PER_CLAY_ACCOUNT = 0.35
SECONDS_PER_SCAN = 0.12


def _now():
    return datetime.now(timezone.utc)


def _iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _pool(profile_dir, name):
    p = Path(profile_dir) / name
    if not p.is_file():
        return []
    try:
        data = json.loads(p.read_text())
    except (json.JSONDecodeError, OSError):
        return []
    return data if isinstance(data, list) else (data.get("contacts") or [])


def _rng(seed_parts):
    """Deterministic per-call randomness — the same demo action twice gives the
    same result, which matters when someone is running the demo a second time in
    front of the same audience."""
    return random.Random("|".join(str(p) for p in seed_parts))


# ---- CRM pull --------------------------------------------------------------
def simulate_crm_pull(profile_dir, db_file, list_id, limit=None):
    """Stand-in for hubspot_pull.py + sdr_batches init.

    Pulls contacts that the profile's fake CRM holds but the pipeline does not yet,
    so "retrieve from the CRM" has something real to retrieve and the contact count
    actually moves. Idempotent: re-running pulls the NEXT unpulled slice rather than
    duplicating, which is also how the live path behaves.

    `limit` is a cap on NEW contacts, matching hubspot_pull.py --limit; None is the
    Source tab's "Maximum" and takes everything the demo CRM still holds.
    """
    pool = _pool(profile_dir, "crm_pool.json")
    conn = db.connect(db_file)
    try:
        db.init_schema(conn)
        have = {r["contact_id"] for r in conn.execute("SELECT contact_id FROM contacts")}
        fresh = [c for c in pool if c.get("contact_id") not in have]
        pool_left = len(fresh)
        if list_id:
            scoped = [c for c in fresh if str(c.get("list_id") or "") == str(list_id)]
            # A list id the pool doesn't know about still returns something — a demo
            # that answers "0 contacts" to a typed-in list id looks broken.
            fresh = scoped or fresh
        take = fresh[:int(limit)] if limit else fresh
        if not take:
            return {"ok": True, "new_contacts": 0, "new_batches": 0, "demo": True,
                    "note": "every contact in this demo CRM has already been pulled"}
        added = db.upsert_contacts(conn, [{
            "contact_id": c["contact_id"], "first_name": c.get("first_name"),
            "last_name": c.get("last_name"), "email": c.get("email"),
            "title": c.get("title"), "company": c.get("company"),
            "linkedin_url": c.get("linkedin_url"), "persona": c.get("persona"),
            "variant": c.get("variant"), "source": c.get("source"),
            "latest_source": c.get("latest_source"),
            "lifecycle_stage": c.get("lifecycle_stage"), "motion": c.get("motion"),
            "phone": c.get("phone"), "mobile_phone": c.get("mobile_phone"),
        } for c in take])
        batches = db.assign_batches(conn, batch_size=25)
        # New accounts arrive with signal already on file, the way a real pull lands
        # on companies the signal cache has seen before.
        rng = _rng([list_id, len(have)])
        for c in take:
            dom = db.email_domain(c.get("email"))
            if dom and rng.random() < 0.45:
                db.record_signal_event(
                    conn, dom, rng.choice(["research", "hiring", "website_visit"]),
                    c.get("signal") or f"Recent activity at {c.get('company')}",
                    has_recent=True,
                    observed_at=_iso(_now() - timedelta(days=rng.randint(0, 20))))
        return {"ok": True, "new_contacts": added, "new_batches": batches,
                "demo": True, "list_id": list_id,
                # in THIS list vs across the whole demo CRM — reporting only the
                # first as "remaining in CRM" read as "the CRM is empty" when three
                # other lists still had contacts
                "remaining_in_list": max(0, len(fresh) - len(take)),
                "remaining_in_crm": max(0, pool_left - len(take))}
    finally:
        conn.close()


# ---- Clay enrichment -------------------------------------------------------
def simulate_enrich(profile_dir, db_file, campaign_id, limit=25, per_company_cap=3,
                    add_to_campaign=False, progress=None):
    """Stand-in for campaigns.enrich() — finds the rest of the buyer group.

    Draws from `clay_pool.json`, which is keyed by domain with a generic fallback so
    any account can yield plausible buyers. Credits are metered into the profile's
    own ledger at the real rate (one per account searched, one per email revealed),
    so the Capacity & spend view responds to the action exactly as it would live.
    """
    pool = _pool(profile_dir, "clay_pool.json")
    by_domain, generic = {}, []
    for row in pool:
        if row.get("domain"):
            by_domain.setdefault(row["domain"], []).append(row)
        else:
            generic.append(row)
    conn = db.connect(db_file)
    try:
        db.init_schema(conn)
        camp = db.get_campaign(conn, campaign_id)
        if not camp:
            return {"error": "campaign not found"}
        scope = C.enrichment_scope(conn, camp, limit=limit)
        res = {"campaign_id": campaign_id, "accounts": len(scope), "found": 0,
               "created": 0, "added_to_campaign": 0, "credits": 0.0,
               "errors": [], "unavailable": None, "demo": True}
        if not scope:
            res["note"] = "no accounts in this campaign to enrich"
            return res

        rng = _rng([campaign_id, len(scope)])
        made = []
        for i, acct in enumerate(scope):
            dom = acct["domain"]
            time.sleep(SECONDS_PER_CLAY_ACCOUNT)
            # One credit per account SEARCHED — an empty search still bills, which
            # is exactly the property the live path has and the reason the estimate
            # is a floor.
            db.record_usage(conn, "clay", "find-contacts", 1, "credits",
                            campaign_id=campaign_id, ref=dom)
            res["credits"] += 1
            candidates = list(by_domain.get(dom) or [])
            if not candidates and generic:
                candidates = rng.sample(generic, min(len(generic), per_company_cap + 1))
            # Not every account yields — a demo where every search hits is a lie.
            if rng.random() < 0.18:
                candidates = []
            for c in candidates[:per_company_cap]:
                first = c.get("first_name") or rng.choice(["Dana", "Alex", "Priya", "Sam"])
                last = c.get("last_name") or rng.choice(["Whitlock", "Ferreira", "Osei", "Nyman"])
                cid = f"demo-enr-{dom.split('.')[0]}-{first}-{last}".lower()
                made.append({
                    "contact_id": cid, "first_name": first, "last_name": last,
                    "email": f"{first}.{last}@{dom}".lower(),
                    "title": c.get("title") or "VP Sales",
                    "company": acct.get("company") or dom,
                    "linkedin_url": f"https://www.linkedin.com/in/{cid}",
                    "persona": c.get("persona") or "sales-leadership",
                    "variant": "value-give", "source": "OFFLINE",
                    "latest_source": "OFFLINE", "lifecycle_stage": "lead",
                    "motion": "outbound",
                    "phone": f"+1 (555) {rng.randint(200, 989)}-{rng.randint(1000, 9999)}",
                })
            res["found"] += len(candidates[:per_company_cap])
            if progress:
                progress(i + 1, len(scope), dom)

        if made:
            res["created"] = db.upsert_contacts(conn, made)
            db.assign_batches(conn, batch_size=25)
            # Second charge: one per email actually revealed.
            db.record_usage(conn, "clay", "reveal-email", res["created"], "credits",
                            campaign_id=campaign_id, ref="batch")
            res["credits"] += res["created"]
        db.update_campaign(conn, campaign_id, last_enrich_at=db.now())

        if add_to_campaign:
            fresh = db.get_campaign(conn, campaign_id)
            q = C.qualify(conn, fresh, commit=True,
                          audience_crm=DemoCRM(profile_dir, db_file))
            res["added_to_campaign"] = q.get("added", 0)
            res["qualified"] = q
        else:
            res["note"] = ("created in the demo pipeline but not sequenced — "
                           "qualify the campaign when you've reviewed them")
        return res
    finally:
        conn.close()


# ---- signal discovery ------------------------------------------------------
def simulate_discovery(profile_dir, db_file, campaign_id, limit=25, progress=None):
    """Stand-in for campaigns.discover() — scans in-scope accounts for signal.

    Writes through the SAME upsert helpers the real detectors use
    (`upsert_hiring_signals` / `upsert_tech_signals`), which is what makes the
    simulation converge like the real thing: each one stamps `*_checked_at`, so a
    scanned account LEAVES the unscanned queue, and appends the signal_event the
    campaign then qualifies against. Recording only the event — as this did before —
    left every account permanently unscanned, so the same 25 were rescanned forever
    and the panel never stopped saying they had never been looked at.

    A miss stores the detectors' literal "nothing found" strings rather than
    nothing at all, for the same reason: "we looked and there was nothing" and "we
    never looked" are different states, and only the second is worth a credit.
    """
    conn = db.connect(db_file)
    try:
        db.init_schema(conn)
        camp = db.get_campaign(conn, campaign_id)
        if not camp:
            return {"error": "campaign not found"}
        scope = C.discovery_scope(conn, camp, limit=limit)
        res = {"campaign_id": campaign_id, "scanned": 0, "candidates": len(scope),
               "detected": {}, "errors": [], "unavailable": {}, "results": [],
               "demo": True}
        kinds = C.validate_signal_query(camp.get("signal_query"))["kinds"]
        if "research" in kinds:
            res["unavailable"]["research"] = (
                "researched at copy-generation time, not by discovery")
        rng = _rng([campaign_id, "disc", len(scope)])
        for i, cand in enumerate(scope):
            dom, company = cand["domain"], cand.get("company")
            time.sleep(SECONDS_PER_SCAN)
            if "hiring" in kinds:
                # One credit per domain, hit or miss — same as the live detector.
                db.record_usage(conn, "prospeo", "enrich-company", 1, "credits", ref=dom)
                # A scan finds something roughly half the time. Both outcomes have to
                # appear or the demo implies a hit rate the real detectors don't have.
                if rng.random() < 0.5:
                    n = rng.randint(1, 5)
                    titles = ["SDR", "Account Executive", "VP Sales", "Sales Manager",
                              "Enterprise AE"][:n]
                    total = rng.randint(n + 3, 26)
                    db.upsert_hiring_signals(
                        conn, dom,
                        f"{total} open roles · {n} sales: " + "; ".join(titles),
                        hiring_detail=json.dumps({"active_count": total,
                                                  "active_titles": titles,
                                                  "sales_titles": titles}),
                        company_name=company)
                else:
                    db.upsert_hiring_signals(conn, dom, "No open roles detected",
                                             company_name=company)
            if "tech" in kinds:
                if rng.random() < 0.4:
                    v = rng.choice([("outreach", "Outreach", "salestech"),
                                    ("6sense", "6sense", "intent"),
                                    ("hubspot", "HubSpot", "crm")])
                    db.upsert_tech_signals(
                        conn, dom, v[1],
                        tech_detail=json.dumps({"detections": [
                            {"vendor_id": v[0], "vendor_name": v[1], "bucket": v[2],
                             "confidence": 0.9}]}),
                        company_name=company)
                else:
                    db.upsert_tech_signals(conn, dom, "No signals detected",
                                           company_name=company)
            # Same read-back as the live path, so the list the demo shows is built
            # the way the real one is rather than assembled from the loop's guesses.
            item = C._scan_result(conn, dom, cand.get("company"), kinds)
            for kind in item["found"]:
                res["detected"][kind] = res["detected"].get(kind, 0) + 1
            item["contacts"] = cand.get("contacts")
            res["results"].append(item)
            res["scanned"] += 1
            if progress:
                progress(i + 1, len(scope), dom)
        res["found_accounts"] = sum(1 for r in res["results"] if r["any"])
        db.update_campaign(conn, campaign_id, last_discovery_at=db.now())
        fresh = db.get_campaign(conn, campaign_id)
        res["qualified"] = C.qualify(conn, fresh, commit=True,
                                     audience_crm=DemoCRM(profile_dir, db_file))
        return res
    finally:
        conn.close()


# ---- file import -----------------------------------------------------------
def simulate_file_import(conn, filename, content_b64, mapping, label, project_root):
    """A dropped CSV/XLSX, imported into the PROFILE's pipeline only.

    The file is genuinely parsed and genuinely imported — a demo where the upload
    was faked would not survive someone dropping their own event list on it, which
    is exactly what a demo of this feature invites. What is skipped is the CRM leg:
    the live path shells out to source_contacts.py, which creates contacts in
    HubSpot, and a demo must never reach a real portal.

    Same response shape as the live commit, so nothing in the UI knows.
    """
    import contact_import

    pv = contact_import.preview(conn, filename, content_b64, mapping, project_root)
    rows = contact_import.parse(filename, content_b64)[1]
    contacts = contact_import.normalize_rows(rows, pv["mapping"])
    persona_for = contact_import._persona_fn(project_root)

    seen, take = set(), []
    for c in contacts:
        if not c["email"] or "@" not in c["email"] or c["email"] in seen:
            continue
        seen.add(c["email"])
        persona = persona_for(c["title"])
        if not persona:
            continue
        c["persona"] = persona
        take.append(c)
    if not take:
        raise contact_import.ImportError_(
            "nothing in this file passed the ICP filter — check the Title column is "
            "mapped, since the buyer-group rules read job titles")

    rng = _rng(["import", filename, len(take)])
    label = (label or filename or "Imported list").rsplit(".", 1)[0][:120]
    rows_out = []
    for i, c in enumerate(take):
        # Deterministic synthetic id in the demo's own namespace — a real CRM id
        # would imply this touched the customer's portal.
        cid = f"demo-imp-{abs(hash(c['email'])) % 10**9}"
        rows_out.append({
            "contact_id": cid, "first_name": c["first_name"], "last_name": c["last_name"],
            "email": c["email"], "title": c["title"],
            "company": c["company"] or (c["domain"] or "").split(".")[0].title(),
            "linkedin_url": c["linkedin_url"], "persona": c["persona"],
            "variant": ["value-give", "earn", "show"][i % 3],
            "source": "OFFLINE", "latest_source": "OFFLINE",
            "lifecycle_stage": "lead", "motion": "outbound",
            "phone": c.get("phone") or None,
        })
    created = db.upsert_contacts(conn, rows_out)
    db.assign_batches(conn, batch_size=25)
    time.sleep(min(3.0, 0.02 * len(rows_out)))   # the real path is not instant

    ids = [r["contact_id"] for r in rows_out]
    import_id = contact_import.record_import(
        conn, label, filename, source="file", rows=len(rows),
        matched=len(ids), contact_ids=ids,
        detail={"stats": pv["stats"], "demo": True})
    # A slice of the imported accounts arrive with signal already on file, the way a
    # real list lands on companies the signal cache has seen before. The rest stay
    # unscanned, which is what gives discovery something to do next.
    for r in rows_out:
        dom = db.email_domain(r["email"])
        if dom and rng.random() < 0.35:
            db.record_signal_event(
                conn, dom, "research",
                f"Met at the event this list came from ({label})",
                has_recent=True)
    return {
        "import_id": import_id, "label": label,
        "stats": pv["stats"], "source": {"created": created, "demo": True},
        "contacts": len(ids), "not_in_pipeline": 0, "demo": True,
        "audience": {"type": "upload", "import_id": import_id, "label": label},
    }


# ---- CRM audiences ---------------------------------------------------------
class DemoCRM:
    """Stand-in for the HubSpot side of audience resolution (audiences.LiveCRM).

    Without it, two of the three audience choices answer a demo with
    "HUBSPOT_ACCESS_TOKEN is not set" — the not-configured notice a demo must never
    show, on the very step where the campaign gets its scope. List membership and
    the CRM segments are computed from the profile's own contacts and its
    `crm_pool.json`, so an audience that says "closed-lost in the last 90 days"
    actually narrows to a subset the rest of the workflow then works.

    Returns (ids, stats) from both methods, exactly like the live one, so
    `not_in_pipeline` still counts people the demo CRM knows and the pipeline does
    not — the step's most useful number, and the one that motivates sourcing.
    """

    def __init__(self, profile_dir, db_file):
        self.profile_dir = profile_dir
        self.db_file = db_file

    def _pool(self):
        return _pool(self.profile_dir, "crm_pool.json")

    def _local(self):
        conn = db.connect(self.db_file)
        try:
            return [dict(r) for r in conn.execute(
                "SELECT contact_id, lifecycle_stage FROM contacts")]
        finally:
            conn.close()

    def list_members(self, list_id):
        """Everyone on a demo list: the pool rows tagged with it, plus the contacts
        already pulled from it. A list that resolved to only the un-pulled remainder
        would report a reach of zero on the list the demo was built from."""
        want = str(list_id)
        pool = [c["contact_id"] for c in self._pool()
                if str(c.get("list_id") or "") == want and c.get("contact_id")]
        local = [r["contact_id"] for r in self._local()]
        # An unknown id still resolves to something: a demo that answers "0 contacts"
        # to a typed-in list id looks broken, the same call the CRM pull makes.
        ids = sorted(set(pool) | set(local)) if pool else sorted(local)
        return ids, {"demo": True}

    # --- properties + deals, for CRM-derived signal rules -------------------
    # A rule over "times contacted" or "was on a deal we lost" has to resolve to
    # SOMETHING in a demo or the whole configurator is a dead form. These generate
    # per-contact values deterministically from the contact id, so the same demo
    # twice gives the same matches, and a rule that catches 12 accounts keeps
    # catching those 12 when you go back and look.
    def contact_properties(self, ids, props):
        out = {}
        for cid in ids:
            rng = _rng(["props", cid])
            vals = {}
            for p in props:
                if p in ("num_notes", "num_contacted_notes"):
                    vals[p] = str(rng.choice([0, 0, 1, 2, 3, 5, 8, 11, 14]))
                elif p in ("hs_analytics_num_page_views", "hs_analytics_num_visits"):
                    vals[p] = str(rng.choice([0, 0, 1, 3, 6, 9, 15, 24]))
                elif p in ("hs_email_open", "hs_email_click"):
                    vals[p] = str(rng.choice([0, 1, 2, 4, 7, 9, 12, 18]))
                elif p == "hs_analytics_num_event_completions":
                    vals[p] = str(rng.choice([0, 0, 0, 1, 2]))
                elif p in ("lifecyclestage",):
                    vals[p] = rng.choice(["lead", "lead", "marketingqualifiedlead",
                                          "salesqualifiedlead", "subscriber"])
                elif p == "hs_lead_status":
                    vals[p] = rng.choice(["", "NEW", "OPEN", "ATTEMPTED_TO_CONTACT"])
                elif p == "hs_analytics_source":
                    vals[p] = rng.choice(["OFFLINE", "ORGANIC_SEARCH", "DIRECT_TRAFFIC",
                                          "PAID_SEARCH"])
                elif p == "hubspot_owner_id":
                    vals[p] = rng.choice(["", "", "10011", "10042"])
                else:  # date-ish fields
                    vals[p] = ((_now() - timedelta(days=rng.randint(1, 240)))
                               .strftime("%Y-%m-%d") if rng.random() < 0.6 else "")
            out[str(cid)] = vals
        return out

    def contact_deals(self, ids, state, window_days=None):
        """{contact_id: [deal_id]} — a minority have deals, fewer are won.

        Roughly the real shape: most contacts were never on an opportunity, and of
        those that were, more were lost than won. A demo where everyone has a
        closed-lost deal would make the closed-lost signal look useless."""
        out = {}
        for cid in ids:
            rng = _rng(["deals", cid])
            if rng.random() > 0.28:          # 72% were never on a deal
                continue
            won = rng.random() < 0.3
            closed = rng.random() < 0.75
            if state == "won" and not (closed and won):
                continue
            if state == "lost" and not (closed and not won):
                continue
            if state == "open" and closed:
                continue
            if window_days and rng.randint(1, 730) > int(window_days):
                continue
            out[str(cid)] = [f"demo-deal-{abs(hash(cid)) % 10**7}"]
        return out

    def crm_query(self, a):
        preset = a["preset"]
        local = self._local()
        ids = [r["contact_id"] for r in local]
        if not ids:
            return [], {"demo": True}
        rng = _rng(["crm_query", preset, a.get("days"), len(ids)])
        if preset == "lifecycle":
            want = str(a.get("lifecycle_stage") or "").strip().lower()
            hit = [r["contact_id"] for r in local
                   if str(r.get("lifecycle_stage") or "").lower() == want]
            return sorted(hit), {"demo": True}
        if preset == "no_deal":
            # Most contacts have never had a deal; the complement is the same
            # population the closed-won/lost segments draw from.
            hit = [c for c in ids if rng.random() > 0.22]
            return sorted(hit), {"demo": True, "checked": len(ids),
                                 "with_deal": len(ids) - len(hit)}
        # closed_lost / closed_won: a stable slice of the pool, sized by the window
        # so widening the days visibly widens the audience.
        days = int(a.get("days") or 30)
        share = min(0.45, 0.06 + days / 400.0) * (1.0 if preset == "closed_lost" else 0.6)
        hit = sorted(c for c in ids if rng.random() < share)
        deals = max(1, int(len(hit) * 0.7)) if hit else 0
        return hit, {"demo": True, "deals": deals}
