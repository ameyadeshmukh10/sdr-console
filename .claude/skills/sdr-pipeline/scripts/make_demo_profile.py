"""Build a complete demo PROFILE — a synthetic mirror of the whole data tree that
the console can be pointed at from the sidebar.

A profile lives at `data/demo/<id>/` and shadows the live `data/` layout, so when
it is active EVERY read the API does (contacts, batches, signals, generated copy,
campaign stats, replies, trends) comes from one internally consistent dataset:

  data/demo/<id>/
    profile.json                        manifest: label, description, covers[]
    outreach/pipeline.db                contacts + batches + account_signals
    outreach/contacts.jsonl             the pull snapshot the console joins against
    outreach/generated/<contact>.json   per-contact email + LinkedIn copy
    campaign-stats/*.jsonl              Bison denominators (+ snapshot history)
    interested-replies/**               written by the interested-trends generator

Coherence is the whole point: the companies in the replies are the companies in the
DB, the signals are the ones the copy references, and the campaign names match the
offers in the trends analysis. A demo where the numbers don't reconcile is worse
than no demo.

Deterministic (fixed seed) so the same profile id always rebuilds byte-identically
and screenshots stay stable. Synthetic throughout — every company is fictional and
every reply is generated; nothing here is real correspondence.

Run:  python3 .claude/skills/sdr-pipeline/scripts/make_demo_profile.py \
          [--profile generic] [--label "Generic demo"] [--contacts 120]
"""

import argparse
import json
import random
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[4]
SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
from batch_db import classify_motion  # noqa: E402
TRENDS_GEN = (PROJECT_ROOT / ".claude" / "skills" / "interested-trends"
              / "scripts" / "make_demo_data.py")

SEED = 20260803

PERSONAS = ["sales-leadership", "revops", "partnerships", "sdr-bdr"]
VARIANTS = ["value-give", "earn", "show"]

# Fictional accounts. Names are invented on purpose — a demo must never imply a
# real company is a prospect or a customer.
COMPANIES = [
    ("Northwind Analytics", "northwind-analytics.example", "Data infrastructure"),
    ("Lumen Freight", "lumenfreight.example", "Logistics SaaS"),
    ("Kestrel Health", "kestrelhealth.example", "Healthcare SaaS"),
    ("Tandem Labs", "tandemlabs.example", "Developer tools"),
    ("Verity Pay", "veritypay.example", "Fintech"),
    ("Orchard Retail Cloud", "orchardretail.example", "Retail SaaS"),
    ("Sable Security", "sablesecurity.example", "Cybersecurity"),
    ("Aperture Robotics", "aperturerobotics.example", "Industrial automation"),
    ("Brightsend", "brightsend.example", "Marketing automation"),
    ("Cobalt Grid", "cobaltgrid.example", "Energy tech"),
    ("Meridian Talent", "meridiantalent.example", "HR tech"),
    ("Ferrous Supply", "ferroussupply.example", "Manufacturing SaaS"),
    ("Halyard Logistics", "halyardlogistics.example", "Supply chain"),
    ("Pinegrove Bank", "pinegrovebank.example", "Financial services"),
    ("Steelyard Data", "steelyarddata.example", "Analytics"),
    ("Juniper Clinical", "juniperclinical.example", "Life sciences"),
    ("Voltaic Systems", "voltaicsystems.example", "Hardware"),
    ("Redshift Media", "redshiftmedia.example", "AdTech"),
    ("Cobblestone HR", "cobblestonehr.example", "HR tech"),
    ("Arbor Insurance", "arborinsurance.example", "InsurTech"),
    ("Quarry Compute", "quarrycompute.example", "Cloud infrastructure"),
    ("Beacon Learning", "beaconlearning.example", "EdTech"),
    ("Tidewater Foods", "tidewaterfoods.example", "CPG"),
    ("Ironwood Legal", "ironwoodlegal.example", "LegalTech"),
    ("Selkie Travel", "selkietravel.example", "Travel"),
    ("Ampersand Retail", "ampersandretail.example", "Retail"),
    ("Northgate Energy", "northgateenergy.example", "Energy"),
    ("Peregrine Fintech", "peregrinefintech.example", "Fintech"),
    ("Lantern Health", "lanternhealth.example", "Healthcare"),
    ("Foxglove Design", "foxglovedesign.example", "Design tools"),
    ("Basalt Security", "basaltsecurity.example", "Cybersecurity"),
    ("Copperline Telecom", "copperlinetelecom.example", "Telecom"),
    ("Willowbrook Labs", "willowbrooklabs.example", "Biotech"),
    ("Sandpiper Games", "sandpipergames.example", "Gaming"),
    ("Harborview Realty", "harborviewrealty.example", "PropTech"),
    ("Kiln Manufacturing", "kilnmanufacturing.example", "Industrial"),
    ("Fernway Nonprofit", "fernwaynonprofit.example", "Nonprofit SaaS"),
]

# Accounts the demo's CRM holds but the pipeline does not — so pulling contacts
# brings back companies that are genuinely new, and the accounts arrive UNSCANNED.
# Without them a pull only ever added more people at companies already covered, and
# the discovery step had nothing to discover no matter how much you sourced.
NEW_COMPANIES = [
    ("Alderman Payments", "aldermanpayments.example", "Fintech"),
    ("Bluepeak Networks", "bluepeaknetworks.example", "Networking"),
    ("Cinder Logistics", "cinderlogistics.example", "Logistics"),
    ("Dovetail Studios", "dovetailstudios.example", "Creative SaaS"),
    ("Elmgrove Care", "elmgrovecare.example", "Healthcare"),
    ("Flintlock Data", "flintlockdata.example", "Analytics"),
    ("Gantry Robotics", "gantryrobotics.example", "Robotics"),
    ("Hollowmere Bank", "hollowmerebank.example", "Financial services"),
]

# Share of the seeded accounts left with no signal row, so "Find accounts" always
# has real work to offer on a fresh profile.
UNSCANNED_SHARE = 0.30

TITLES = {
    "sales-leadership": ["VP Sales", "Chief Revenue Officer", "Head of Sales",
                         "Director of Sales"],
    "revops": ["Director of Revenue Operations", "RevOps Manager",
               "Head of Sales Operations", "Sales Ops Lead"],
    "partnerships": ["Head of Partnerships", "Director of Alliances",
                     "VP Channel", "Partnerships Lead"],
    "sdr-bdr": ["SDR Manager", "BDR Manager", "Head of SDR", "Director of BDR"],
}
FIRST = ["Alex", "Priya", "Jordan", "Sam", "Mei", "Tomas", "Dana", "Kwame", "Iris",
         "Noah", "Lena", "Omar", "Ravi", "Sofia", "Elliot", "Nina"]
LAST = ["Reyes", "Okafor", "Lindqvist", "Moreau", "Tanaka", "Silva", "Novak",
        "Bennett", "Haddad", "Kaur", "Ferreira", "Walsh", "Ibrahim", "Costa"]

# Signal text templates. The generated copy references these, so the demo holds
# together when someone opens a contact and reads the email.
# Signals are picked PER CONTACT, not per company: two people at the same account
# get different angles, which is what the real pipeline produces (each contact is
# researched independently) and what stops the Outreach list reading as mail-merge.
NEWS = [
    "announced a $24M Series B to expand its go-to-market team",
    "opened a second office and is doubling the commercial org this year",
    "shipped a self-serve tier and is now selling upmarket at the same time",
    "named a new CRO from a category leader last quarter",
    "published a customer study claiming 3x faster onboarding",
    "moved upmarket with an enterprise tier announced at their user conference",
    "posted a 40% headcount increase across revenue roles this year",
    "consolidated three products into one platform SKU last quarter",
    "entered the UK market with a London-based sales team",
    "was named a leader in its category by an industry analyst",
    "acquired a smaller competitor and is merging the two customer bases",
    "launched a partner programme and is hiring channel managers",
    "reported that half of new revenue now comes from outbound",
    "replaced its self-serve funnel with a sales-assisted motion",
    "committed publicly to doubling ARR without doubling headcount",
]
TECH_STACKS = [
    ("Salesforce · Outreach · 6sense", ["sequencing", "intent"]),
    ("HubSpot · Apollo · Google Ads pixel", ["sequencing", "ads"]),
    ("Salesforce · Salesloft", ["sequencing"]),
    ("HubSpot · Demandbase", ["intent"]),
    ("Salesforce · LinkedIn Insight Tag", ["ads"]),
]
HIRING = [
    ("14 open roles · 4 sales: SDR; AE; VP Sales", 14, 4),
    ("6 open roles · 2 sales: Enterprise AE; SDR", 6, 2),
    ("22 open roles · 5 sales: BDR; AE; Sales Manager", 22, 5),
    ("3 open roles · 0 sales", 3, 0),
]

CAMPAIGNS = [
    (901, "Americas Demo request", "Demo request"),
    (902, "Global Lead magnet (PDF)", "Lead magnet (PDF)"),
    (903, "Global Event", "Event"),
    (904, "Americas Persona-targeted", "Persona-targeted"),
]


def now_iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


# Real pulls are lumpy: sales leadership dominates an ICP list, partnerships is a
# long tail. Perfectly even personas are the clearest tell that data was generated,
# so weight the draw (roughly mirroring the live 1685/209/81/56 shape).
PERSONA_WEIGHTS = [58, 22, 8, 12]        # index-aligned with PERSONAS


def _demo_provenance(rng):
    """Source / lifecycle / motion for one demo contact, classified by the real
    classifier so the demo and the live pipeline agree on what 'inbound' means."""
    if rng.random() < 0.22:
        src = rng.choice(["ORGANIC_SEARCH", "PAID_SEARCH", "DIRECT_TRAFFIC",
                          "REFERRALS", "ORGANIC_SOCIAL"])
        stage = rng.choice(["lead", "marketingqualifiedlead", "subscriber"])
    else:
        src = rng.choice(["OFFLINE", "OFFLINE", "OTHER"])
        stage = rng.choice(["lead", "subscriber", ""])
    return {"source": src, "latest_source": src, "lifecycle_stage": stage,
            "motion": classify_motion(src, src, stage)}


def build_contacts(rng, n):
    rows, used = [], set()
    # Uneven contacts-per-account, so some companies show a full buying group and
    # others a single champion.
    account_cycle = []
    for company, domain, sector in COMPANIES:
        account_cycle += [(company, domain, sector)] * rng.choice([1, 2, 2, 3, 4, 5])
    rng.shuffle(account_cycle)

    for i in range(n):
        company, domain, _ = account_cycle[i % len(account_cycle)]
        persona = rng.choices(PERSONAS, weights=PERSONA_WEIGHTS)[0]
        first, last = rng.choice(FIRST), rng.choice(LAST)
        local = f"{first}.{last}".lower()
        email = f"{local}@{domain}"
        if email in used:                       # keep emails unique
            email = f"{local}{i}@{domain}"
        used.add(email)
        rows.append({
            "contact_id": f"demo-{100000 + i}",
            "first_name": first, "last_name": last, "email": email,
            "title": rng.choice(TITLES[persona]),
            "company": company, "domain": domain, "persona": persona,
            "linkedin_url": f"https://www.linkedin.com/in/demo-{100000 + i}",
            # Fictional numbers in the reserved 555 exchange, so a demo can show the
            # phone reveal without anyone dialling a real person.
            "phone": f"+1 (555) {rng.randint(200, 989)}-{rng.randint(1000, 9999)}",
            "mobile_phone": (f"+1 (555) {rng.randint(200, 989)}-{rng.randint(1000, 9999)}"
                             if rng.random() < 0.6 else None),
            # Variants are assigned as batches run, so the arms are close but not
            # identical — an exact three-way split never happens in practice.
            "variant": rng.choices(VARIANTS, weights=[40, 33, 27])[0],
            # The researched angle is per CONTACT, so two people at one account get
            # different openers (see NEWS).
            "signal": rng.choice(NEWS),
            # Provenance: a realistic ICP list is mostly cold with a minority who had
            # already touched the website or a webinar.
            **_demo_provenance(rng),
        })
    return rows


def build_db(profile_dir, contacts, rng, batch_size=25):
    """Create the profile's pipeline.db using the real schema helpers."""
    sys.path.insert(0, str(SCRIPTS))
    import batch_db                                    # noqa: E402

    db_path = profile_dir / "outreach" / "pipeline.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    if db_path.exists():
        db_path.unlink()

    # batch_db.connect() is hard-wired to the live DB path, so open this one
    # directly and reuse only the schema/DDL from the module. Keeping the schema
    # in one place is what stops the demo DB drifting from the real one.
    import sqlite3
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    batch_db.init_schema(conn)

    now = datetime.now(timezone.utc)
    # Batch statuses: a couple done, one in flight, the rest pending — so the
    # Pipeline view has something to show in every column.
    n_batches = max(1, -(-len(contacts) // batch_size))
    for b in range(1, n_batches + 1):
        if b <= max(1, n_batches // 2):
            status = "done"
            claimed = now - timedelta(days=(n_batches - b) * 8 + 6)
            completed = now - timedelta(days=(n_batches - b) * 8 + 5)
        elif b == max(1, n_batches // 2) + 1:
            status, claimed, completed = "claimed", now - timedelta(hours=2), None
        else:
            status, claimed, completed = "pending", None, None
        size = min(batch_size, len(contacts) - (b - 1) * batch_size)
        conn.execute(
            "INSERT INTO batches (batch_id, status, size, claimed_at, completed_at) "
            "VALUES (?,?,?,?,?)",
            (b, status, size, now_iso(claimed) if claimed else None,
             now_iso(completed) if completed else None))

    for i, c in enumerate(contacts):
        batch_id = i // batch_size + 1
        brow = conn.execute("SELECT status FROM batches WHERE batch_id=?",
                            (batch_id,)).fetchone()
        bstatus = brow["status"] if brow else "pending"
        if bstatus == "done":
            status = rng.choices(["enrolled", "generated", "skipped"],
                                 weights=[8, 2, 1])[0]
        elif bstatus == "claimed":
            status = rng.choices(["generated", "pending"], weights=[3, 2])[0]
        else:
            status = "pending"
        # Work happened over weeks, not all at once. The spread is deliberately
        # compressed at the recent end so the NEWEST enrolled batch is only days old:
        # its sequence is still in flight, which is the only way the Outreach view
        # shows any "staged" messages rather than everything already sent.
        n_done = max(1, n_batches // 2)
        age_days = max(1, (n_done - batch_id) * 9 + 2 + rng.randint(0, 2))
        touched = now - timedelta(days=age_days, minutes=rng.randint(0, 600))
        c["updated_at"] = now_iso(touched)
        conn.execute(
            "INSERT INTO contacts (contact_id, first_name, last_name, email, title, "
            "company, linkedin_url, persona, domain, variant, source, latest_source, "
            "lifecycle_stage, motion, phone, mobile_phone, batch_id, status, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (c["contact_id"], c["first_name"], c["last_name"], c["email"], c["title"],
             c["company"], c["linkedin_url"], c["persona"], c["domain"], c["variant"],
             c["source"], c["latest_source"], c["lifecycle_stage"], c["motion"],
             c.get("phone"), c.get("mobile_phone"),
             batch_id, status, c["updated_at"]))
        c["status"] = status
        c["batch_id"] = batch_id

    # One account_signals row per company: research + technographic + hiring, i.e.
    # exactly what the Signals view renders.
    #
    # …except for UNSCANNED_SHARE of them, which get NO row at all. That gap is
    # load-bearing, not an omission: "Find accounts" only offers accounts nothing
    # has scanned yet, so a profile where every account was already scanned left
    # the discovery step permanently greyed out — a demo that could never show the
    # console going and looking for signal. These are the accounts it finds.
    unscanned = set(rng.sample(range(len(COMPANIES)),
                               k=max(1, round(len(COMPANIES) * UNSCANNED_SHARE))))
    for idx, (company, domain, _sector) in enumerate(COMPANIES):
        if idx in unscanned:
            continue
        news = NEWS[idx % len(NEWS)]
        tech_line, groups = TECH_STACKS[idx % len(TECH_STACKS)]
        hiring_line, total_roles, sales_roles = HIRING[idx % len(HIRING)]
        conn.execute(
            "INSERT INTO account_signals (domain, company_name, signal, has_recent, "
            "researched_at, model, updated_at, tech_signals, tech_detail, "
            "tech_checked_at, hiring_signals, hiring_detail, hiring_checked_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (domain, company, f"{company} {news}.", 1,
             now_iso(now - timedelta(days=rng.randint(1, 74))), "demo-profile",
             now_iso(now - timedelta(days=rng.randint(0, 20))),
             tech_line, json.dumps({"playbook": groups, "demo": True}),
             now_iso(now - timedelta(days=rng.randint(1, 74))),
             hiring_line,
             json.dumps({"active_count": total_roles, "sales_count": sales_roles,
                         "demo": True}),
             now_iso(now - timedelta(days=rng.randint(1, 74)))))

    # Activity ledger: this is what drives the Sequence column and the per-message
    # sent/staged chips. Enrolled contacts are partway through the 4+3 template —
    # older batches further along — so the view shows a sequence in flight rather
    # than everything at once.
    for c in contacts:
        if c["status"] != "enrolled":
            continue
        started = datetime.strptime(c["updated_at"], "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc)
        elapsed_days = max(0, (now - started).days)
        # ~1 email every 4 days, 1 LinkedIn touch every 7, capped at the template.
        n_email = min(4, 1 + elapsed_days // 4)
        n_li = min(3, elapsed_days // 7)
        for step in range(1, n_email + 1):
            ts = started + timedelta(days=(step - 1) * 4, hours=rng.randint(1, 9))
            if ts > now:
                break
            conn.execute(
                "INSERT OR IGNORE INTO hubspot_activity_log (dedup_key, event_type, "
                "channel, contact_id, engagement_id, status, event_ts, created_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (f"outbound:{c['contact_id']}:e{step}", "outbound", "email",
                 c["contact_id"], f"demo-eng-{c['contact_id']}-e{step}", "logged",
                 now_iso(ts), now_iso(ts)))
        for step in range(1, n_li + 1):
            ts = started + timedelta(days=step * 7, hours=rng.randint(1, 9))
            if ts > now:
                break
            conn.execute(
                "INSERT OR IGNORE INTO hubspot_activity_log (dedup_key, event_type, "
                "channel, contact_id, engagement_id, status, event_ts, created_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (f"outbound:{c['contact_id']}:li{step}", "outbound", "linkedin",
                 c["contact_id"], f"demo-eng-{c['contact_id']}-li{step}", "logged",
                 now_iso(ts), now_iso(ts)))
        c["_email_sent"], c["_li_sent"] = n_email, n_li

    conn.commit()
    conn.close()
    return db_path, n_batches


# Four CTA plays that classify DIFFERENTLY under the server's derive_cta() rules,
# so the Outreach view's "CTA play" filter has real facets instead of one value.
# The trigger phrase for each is load-bearing — see CTA_RULES in webui/server/app.py.
CTA_PLAYS = [
    {"id": "signal-play",
     "subject": "a signal play for {company}",
     "ask": "I built a signal play for {company} — want me to send it over?"},
    {"id": "personalized-drafts",
     "subject": "10 personalized drafts for your team",
     "ask": "Happy to run 10 personalized emails for your top accounts so you can "
            "judge the output before committing to anything."},
    {"id": "outbound-teardown",
     "subject": "a teardown of your current outbound",
     "ask": "I can do a teardown of your current sequence and show where the drop-off "
            "is. Worth 20 minutes?"},
    {"id": "pipeline-model",
     "subject": "your pipeline gap, modelled",
     "ask": "I can model the pipeline gap against your current run rate. Open to a "
            "quick call to walk through it?"},
]

# Persona-specific pain and outcome, so the copy reads differently per persona
# rather than being one body with the name swapped.
PERSONA_VOICE = {
    "sales-leadership": {
        "pain": "hitting the number without adding headcount",
        "line": "Most VPs we talk to are being asked to grow pipeline while the "
                "hiring plan is frozen.",
    },
    "revops": {
        "pain": "outbound data quality and attribution",
        "line": "The RevOps question is usually attribution — you cannot defend spend on a "
                "channel you cannot trace to pipeline.",
    },
    "partnerships": {
        "pain": "activating partner-sourced accounts",
        "line": "Partner-sourced accounts usually sit untouched because nobody owns "
                "the first touch.",
    },
    "sdr-bdr": {
        "pain": "ramp time and rep capacity",
        "line": "New reps take a quarter to ramp, and the pipeline gap shows up "
                "before they are productive.",
    },
}


def _demo_bison_ids(profile_dir):
    """Bison campaign ids that actually exist in this profile's stats fixture.

    Console campaigns must bind to THESE, not to the live portal's 14/15/16 — the
    funnel joins console campaigns to send stats through bison_campaign_id, and a
    binding that points at an id the profile doesn't have makes every stage past
    Enrolled read as zero.
    """
    f = profile_dir / "campaign-stats" / "campaigns.jsonl"
    ids = []
    if f.is_file():
        for line in f.read_text().splitlines():
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            # prefer campaigns that actually sent, so the funnel has real numbers
            if row.get("total_leads_contacted"):
                ids.append(str(row["campaign_id"]))
    return ids


def build_campaigns(profile_dir, contacts, rng):
    """Campaigns, signal history, scored membership, spend and the hot list.

    Everything is written through the REAL helpers (`campaigns.qualify`,
    `db.record_signal_event`, `db.record_usage`) rather than hand-rolled rows, so a
    demo cannot drift from how the build actually behaves: the scores you see are
    computed by the same scorer, the momentum by the same momentum rule, and the hot
    list by the same query. When those change, the demo changes with them.
    """
    sys.path.insert(0, str(SCRIPTS))
    sys.path.insert(0, str(PROJECT_ROOT / ".claude" / "skills" / "ai-sdr" / "scripts"))
    import batch_db as db
    import campaigns as C

    db_path = profile_dir / "outreach" / "pipeline.db"
    real_db, real_hot = db.DB_PATH, C.HOT_LIST_PATH
    db.DB_PATH = db_path
    C.HOT_LIST_PATH = profile_dir / "outreach" / "hot-list.json"
    conn = db.connect()
    try:
        db.init_schema(conn)
        now = datetime.now(timezone.utc)
        domains = sorted({c["domain"] for c in contacts})

        # --- signal history. Varied kind AND age, because that is what gives the
        # scores a spread: everything fresh scores everything hot, which looks fake.
        for i, dom in enumerate(domains):
            company = next(c["company"] for c in contacts if c["domain"] == dom)
            age = rng.choice([1, 2, 3, 5, 8, 12, 20, 35, 60])
            ts = now_iso(now - timedelta(days=age))
            roll = rng.random()
            if roll < 0.42:
                db.record_signal_event(conn, dom, "research",
                                       f"{rng.choice(NEWS)} ({company})",
                                       has_recent=True, observed_at=ts)
            if roll < 0.30 or 0.55 < roll < 0.80:
                n_sales = rng.choice([0, 1, 2, 3, 4, 5])
                titles = ["SDR", "Account Executive", "VP Sales",
                          "Enterprise AE", "Sales Manager"][:n_sales]
                if titles:
                    db.record_signal_event(
                        conn, dom, "hiring",
                        f"{rng.randint(4, 30)} open roles · {n_sales} sales: "
                        + "; ".join(titles),
                        detail=json.dumps({"active_count": rng.randint(4, 30),
                                           "active_titles": titles,
                                           "sales_titles": titles}),
                        observed_at=now_iso(now - timedelta(days=age + 1)))
            # Behavioural intent. Freshest signal there is and the fastest to age,
            # so demo visits are recent — a three-week-old page view is not intent.
            if rng.random() < 0.30:
                pages = rng.choice(["/pricing", "/pricing, /case-studies",
                                    "/product/ai-sdr", "/demo, /pricing"])
                db.record_signal_event(
                    conn, dom, "website_visit",
                    f"{rng.randint(2, 14)} visits in the last week ({pages})",
                    detail=json.dumps({"visits": rng.randint(2, 14), "pages": pages}),
                    observed_at=now_iso(now - timedelta(days=rng.choice([0, 1, 2, 4, 6]))))
            # Aggregate priority carried from earlier campaigns — a warm history.
            if rng.random() < 0.22:
                prior = rng.randint(38, 82)
                db.record_signal_event(
                    conn, dom, "prior_score",
                    f"Scored {prior} in an earlier campaign",
                    detail=json.dumps({"score": prior}),
                    observed_at=now_iso(now - timedelta(days=rng.randint(40, 120))))
            if roll > 0.62:
                vendors = rng.choice([
                    [("outreach", "Outreach", "salestech")],
                    [("salesloft", "Salesloft", "salestech"), ("6sense", "6sense", "intent")],
                    [("hubspot", "HubSpot", "crm")],
                    [("6sense", "6sense", "intent")],
                ])
                db.record_signal_event(
                    conn, dom, "tech",
                    ", ".join(v[1] for v in vendors),
                    detail=json.dumps({"detections": [
                        {"vendor_id": v[0], "vendor_name": v[1], "bucket": v[2],
                         "confidence": 0.9} for v in vendors]}),
                    observed_at=now_iso(now - timedelta(days=age + 3)))

        # --- a CRM-derived signal the customer defined for themselves. Ships in
        # the profile so the Signals view shows the configurator in USE rather than
        # only offering an empty "New signal" button — the point of the feature is
        # that a deployment adds its own kinds, and a demo with only the six
        # builtins does not show that at all.
        try:
            import crm_signals as CS
            d = db.upsert_signal_def(
                conn, "prior_activity", label="Prior activity",
                description="They have engaged with us repeatedly and nobody is "
                            "working them.",
                strength=34, decay_scale=0.5, detector="rule",
                rule={"source": "local_field", "field": "lifecycle_stage",
                      "op": "eq", "value": "marketingqualifiedlead"})
            CS.evaluate(conn, d, limit=500, commit=True)
        except Exception:  # noqa: BLE001 — the demo must build without it
            pass

        # --- campaigns. Three, deliberately different SHAPES so the view shows the
        # range the model supports: a CRM-segment re-engagement, a rolling signal
        # play, and a finished one.
        specs = [
            {"name": "Closed-lost re-engagement",
             "description": "Everyone we lost a deal to in the last 90 days, worked "
                            "again when something changes at the account.",
             "audience": {"type": "crm_query", "preset": "closed_lost", "days": 90},
             "signal_query": {"kinds": ["research", "hiring"], "motion": "outbound"},
             "brief": "These accounts already evaluated us and chose otherwise, so "
                      "nothing here opens like a first touch. Every email leads with "
                      "what has CHANGED at their end since the conversation ended, and "
                      "argues that the reason it did not land last time has moved. "
                      "Never reference the lost deal as a grievance.",
             "status": "active", "start": 45, "end": -30, "bison": "14"},
            {"name": "Q3 funding + hiring signals",
             "description": "Accounts that raised or opened sales roles this quarter.",
             "audience": {"type": "all_contacts"},
             "signal_query": {"kinds": ["research", "hiring"], "require_recent": True,
                              "motion": "outbound"},
             "brief": "Something measurable just happened at every account here — "
                      "money in, or a sales team being built. Lead on that specific "
                      "event, then make the rest of the email earn it rather than "
                      "restate it. Where the account is hiring reps, email 2 ties the "
                      "open roles to covering pipeline while those reps ramp.",
             "status": "active", "start": 30, "end": -21, "bison": "15"},
            {"name": "Sequencing-tool displacement",
             "description": "Accounts running Outreach or Salesloft — the "
                            "no-disruption play.",
             "audience": {"type": "all_contacts"},
             "signal_query": {"kinds": ["tech"], "tech_playbook": ["sequencing", "intent_abm"],
                              "motion": "outbound"},
             "status": "completed", "start": 90, "end": 10, "bison": "16"},
        ]
        bison_ids = _demo_bison_ids(profile_dir)
        made = []
        for idx, s in enumerate(specs):
            if bison_ids:
                s = {**s, "bison": bison_ids[idx % len(bison_ids)]}
            camp = db.create_campaign(
                conn, s["name"], description=s["description"],
                brief=s.get("brief"),
                status=s["status"],
                window_start=(now - timedelta(days=s["start"])).strftime("%Y-%m-%d"),
                window_end=(now - timedelta(days=s["end"])).strftime("%Y-%m-%d"),
                audience=s["audience"], signal_query=s["signal_query"],
                variant=rng.choice(VARIANTS), bison_campaign_id=s["bison"],
                heyreach_campaign_id="hr-demo-1", target_accounts=None,
                discovery_interval_days=7)
            db.seed_default_sequence(conn, camp["campaign_id"])
            # A campaign with every step on the library default reads as untouched.
            # Give one a hand-edited step so the step->CTA link looks used.
            if s["bison"] == "15":
                db.upsert_step(conn, camp["campaign_id"], 2, "email",
                               cta_key="outbound-teardown",
                               angle="Lead on the teardown instead of the run rate — "
                                     "this cohort responds to a critique.")
            made.append(camp)

        # --- membership. The audience resolver needs HubSpot for the closed-lost
        # campaign, which a demo has no token for, so that one is qualified with the
        # audience temporarily widened. The SCORES are still real: same scorer, same
        # signal events, same momentum rule.
        for camp in made:
            aud = camp.get("audience") or {}
            if aud.get("type") == "crm_query":
                db.update_campaign(conn, camp["campaign_id"],
                                   audience={"type": "all_contacts"})
                C.qualify(conn, db.get_campaign(conn, camp["campaign_id"]), commit=True)
                db.update_campaign(conn, camp["campaign_id"], audience=aud)
            else:
                C.qualify(conn, db.get_campaign(conn, camp["campaign_id"]), commit=True)

        # A second scoring pass on the oldest campaign produces real MOMENTUM: some
        # contacts warm, some cool, exactly as the rescore path would over time.
        # Perturb each contact INDIVIDUALLY — one shared delta gives every row the
        # same momentum, which is the clearest possible tell that it was generated.
        for m in db.campaign_members(conn, made[0]["campaign_id"]):
            if m.get("priority_score") is None:
                continue
            drift = rng.choice([-18, -12, -7, -4, 0, 3, 6, 11, 16])
            conn.execute("UPDATE campaign_members SET priority_score=? "
                         "WHERE campaign_id=? AND contact_id=?",
                         (max(5.0, min(100.0, m["priority_score"] - drift)),
                          made[0]["campaign_id"], m["contact_id"]))
        conn.commit()
        C.rescore(conn, db.get_campaign(conn, made[0]["campaign_id"]))

        # --- enrolled + replied state, so the funnel is not all 'qualified'.
        # Replies are drawn WEIGHTED BY SCORE rather than at random. Random replies
        # make hot contacts convert no better than cool ones, so the Analytics panel
        # that asks "does priority predict replies" renders a working model as
        # broken. A score that predicts is what the real thing is supposed to do —
        # the demo should show that, not contradict it.
        reply_odds = {"hot": 0.16, "warm": 0.07, "cool": 0.025}
        for camp in made:
            members = db.campaign_members(conn, camp["campaign_id"])
            take = int(len(members) * (0.9 if camp["status"] == "completed" else 0.45))
            for m in members[:take]:
                db.set_member_state(conn, camp["campaign_id"], m["contact_id"],
                                    "enrolled", bison_lead_id=rng.randint(1000, 9999))
                # Hold the invariant the LIVE enroll path holds: sdr_batches
                # cmd_enroll sets contacts.status AND campaign_members.state
                # together. Setting only the member state left contacts reading
                # "generated"/"skipped" while their membership said "enrolled",
                # so the Outreach list showed "2 enrolled" next to "not enrolled"
                # on the same row — a contradiction the demo must never display.
                db.set_contact_status(conn, m["contact_id"], "enrolled")
                if rng.random() < reply_odds.get(m.get("score_band"), 0.03):
                    db.set_member_state(conn, camp["campaign_id"], m["contact_id"],
                                        "replied")

        # --- spend + capacity. Metered the same way the real paths meter it, so the
        # Use view's numbers add up against the same ledger.
        for d in range(30, -1, -1):
            day = now - timedelta(days=d)
            if d > 0:
                db.record_usage(conn, "bison", "email-enroll", rng.randint(120, 480),
                                "sends", occurred_at=now_iso(day))
                # Past days only. Today's LinkedIn total is set once below —
                # stacking a loop entry on top of it pushed the demo to 130% of the
                # daily cap, which reads as a broken widget rather than a busy day.
                db.record_usage(conn, "heyreach", "li-enroll", rng.randint(6, 19),
                                "sends", occurred_at=now_iso(day))
            if rng.random() < 0.4:
                db.record_usage(conn, "clay", "find-contacts", rng.randint(10, 40),
                                "credits", campaign_id=rng.choice(made)["campaign_id"],
                                occurred_at=now_iso(day))
                db.record_usage(conn, "clay", "reveal-email", rng.randint(8, 30),
                                "credits", campaign_id=rng.choice(made)["campaign_id"],
                                occurred_at=now_iso(day))
            if rng.random() < 0.5:
                db.record_usage(conn, "prospeo", "enrich-company", rng.randint(5, 25),
                                "credits", occurred_at=now_iso(day))
        # Today's LinkedIn sends land mid-allowance — a demo that shows 0/20 makes
        # the capacity widget look decorative, and 20/20 makes it look broken.
        db.record_usage(conn, "heyreach", "li-enroll", 13, "sends")

        snap = C.hot_target_list(conn)
        (profile_dir / "outreach").mkdir(parents=True, exist_ok=True)
        (profile_dir / "outreach" / "hot-list.json").write_text(
            json.dumps(snap, ensure_ascii=False, indent=2))

        counts = {c["name"]: db.campaign_counts(conn, c["campaign_id"])["members"]
                  for c in made}
        overlap = C.overlap_summary(conn)["contacts"]
        return len(made), counts, overlap, len(snap["accounts"])
    finally:
        conn.close()
        db.DB_PATH, C.HOT_LIST_PATH = real_db, real_hot


def build_report_recipes(profile_dir):
    """Example report questions a demo can answer.

    Keyword-matched to a real SPEC that is then EXECUTED against the profile's own
    data — so the answer is computed, not canned, and changes when the demo data
    does. That is the difference between demonstrating the feature and faking it."""
    (profile_dir / "report_recipes.json").write_text(json.dumps({"recipes": [
        {"label": "Hot contacts not yet worked",
         "keywords": ["hot", "priority", "call", "worked", "top contacts"],
         "spec": {"dataset": "contacts", "title": "Hot contacts not yet worked",
                  "columns": ["name", "company", "title", "campaign", "score", "band"],
                  "filters": [{"column": "band", "op": "eq", "value": "hot"},
                              {"column": "state", "op": "eq", "value": "qualified"}],
                  "sort": {"column": "score", "dir": "desc"}, "limit": 50}},
        {"label": "Signals by type",
         "keywords": ["signal", "signals", "fired", "by type", "kind"],
         "spec": {"dataset": "signals", "title": "Signals by type",
                  "group_by": "kind",
                  "aggregates": [{"fn": "count", "column": None}],
                  "sort": {"column": "count", "dir": "desc"}, "limit": 20}},
        {"label": "Campaign performance",
         "keywords": ["campaign", "campaigns", "performance", "enrolled", "replied"],
         "spec": {"dataset": "campaigns", "title": "Campaign performance",
                  "columns": ["name", "status", "accounts", "members", "enrolled", "replied"],
                  "sort": {"column": "members", "dir": "desc"}, "limit": 25}},
        {"label": "Credit spend by provider",
         "keywords": ["credit", "credits", "spend", "cost", "provider", "budget"],
         "spec": {"dataset": "spend", "title": "Credit spend by provider",
                  "group_by": "provider",
                  "aggregates": [{"fn": "sum", "column": "units"}],
                  "sort": {"column": "sum_units", "dir": "desc"}, "limit": 20}},
        {"label": "Contacts by company",
         "keywords": ["company", "companies", "account", "accounts", "by company"],
         "spec": {"dataset": "contacts", "title": "Contacts by company",
                  "group_by": "company",
                  "aggregates": [{"fn": "count", "column": None},
                                 {"fn": "avg", "column": "score"}],
                  "sort": {"column": "count", "dir": "desc"}, "limit": 30}},
    ]}, indent=2))


def build_source_pools(profile_dir, contacts, rng):
    """The two fixture pools demo mode's simulated sources draw from.

    crm_pool.json   contacts the demo's fake CRM holds that the pipeline has NOT
                    pulled yet — so "retrieve from the CRM" has something real to
                    retrieve and the contact count actually moves.
    clay_pool.json  buyers enrichment can "find" at an account, keyed by domain with
                    a generic fallback so any account yields plausible people.

    Both are deliberately larger than one run consumes: the point is that a demo can
    be run several times, and each pull brings back the NEXT slice rather than the
    same names — which is also how the live path behaves.
    """
    existing_emails = {c["email"] for c in contacts}
    # Mostly the same company universe, so pulled contacts land on accounts the rest
    # of the demo already knows about — plus NEW_COMPANIES, which the pipeline has
    # never seen. Those are what make sourcing feed discovery: a pull that only ever
    # deepened existing accounts left "Find accounts" with nothing new to scan.
    crm, n = [], 0
    for company, domain, _sector in COMPANIES + NEW_COMPANIES:
        for _ in range(rng.choice([1, 2, 2, 3])):
            persona = rng.choices(PERSONAS, weights=PERSONA_WEIGHTS)[0]
            first, last = rng.choice(FIRST), rng.choice(LAST)
            email = f"{first}.{last}{n}@{domain}".lower()
            if email in existing_emails:
                continue
            n += 1
            crm.append({
                "contact_id": f"demo-crm-{200000 + n}",
                "first_name": first, "last_name": last, "email": email,
                "title": rng.choice(TITLES[persona]), "company": company,
                "persona": persona,
                "linkedin_url": f"https://www.linkedin.com/in/demo-crm-{200000 + n}",
                "phone": f"+1 (555) {rng.randint(200, 989)}-{rng.randint(1000, 9999)}",
                "variant": rng.choices(VARIANTS, weights=[40, 33, 27])[0],
                "signal": rng.choice(NEWS),
                # A couple of named lists so picking a list id in the UI is meaningful.
                "list_id": rng.choice(["2198", "2198", "3310", "4021"]),
                **_demo_provenance(rng),
            })
    (profile_dir / "crm_pool.json").write_text(json.dumps(crm, indent=2))

    # Enrichment finds the REST of the buying committee, so these skew to the roles
    # a thin account is usually missing rather than more of the same.
    clay = []
    for company, domain, _sector in COMPANIES:
        for _ in range(rng.choice([2, 3, 3, 4])):
            persona = rng.choices(PERSONAS, weights=[40, 26, 10, 24])[0]
            clay.append({
                "domain": domain, "company": company,
                "first_name": rng.choice(FIRST), "last_name": rng.choice(LAST),
                "title": rng.choice(TITLES[persona]), "persona": persona,
            })
    # Generic fallback for accounts the pool doesn't cover.
    for _ in range(24):
        persona = rng.choices(PERSONAS, weights=[40, 26, 10, 24])[0]
        clay.append({"domain": None, "first_name": rng.choice(FIRST),
                     "last_name": rng.choice(LAST),
                     "title": rng.choice(TITLES[persona]), "persona": persona})
    (profile_dir / "clay_pool.json").write_text(json.dumps(clay, indent=2))
    return len(crm), len(clay)


def build_campaign_brief_fixture(profile_dir):
    """What the demo's campaign configurator answers with.

    A demo has no ANTHROPIC_API_KEY, and an agent that turns a meeting note into a
    configured campaign is exactly the kind of thing a demo must be able to show
    rather than apologise for. Served by app._demo_campaign_brief.

    Two properties make it behave like the real one instead of like a canned
    screenshot:

      * it ASKS FIRST. The first response withholds the fields listed in `decides`
        and returns one clarifying question, so the demo shows the agent noticing
        something the note left open — which is the whole difference between a
        configurator and a form-filler.
      * the ANSWER MOVES OTHER FIELDS. Each option carries its own config overlay,
        so picking "the last quarter" visibly rewrites the window AND the audience,
        not just the sentence it was asked about.

    Values are ids from the real vocabulary and are re-validated on the way out
    (campaign_brief.validate_config), so a fixture can never put something on the
    form that Create would then reject.
    """
    def q(qid, question, why, decides, options):
        return {"id": qid, "question": question, "why": why,
                "decides": decides, "options": options}

    (profile_dir / "campaign_brief.json").write_text(json.dumps({
        "recipes": [
            {
                "keywords": ["closed lost", "closed-lost", "lost", "re-engage",
                             "reengage", "went dark", "last quarter"],
                "summary": "Set this up as a closed-lost re-engagement: everyone on "
                           "a deal we lost, worked again only when something has "
                           "actually changed at the account. Outbound-only, so the "
                           "numbers stay attributable.",
                "clarify": q(
                    "lost_window",
                    "How far back should the closed-lost list reach?",
                    "It sets both the audience and the signal window — a wider reach "
                    "means more accounts but colder memories of the last conversation.",
                    ["window_start", "window_end", "audience"],
                    [
                        {"label": "Last 30 days", "detail": "Tight and warm — they "
                         "remember the conversation.",
                         "config": {"window_start": "2026-07-05", "window_end": "2026-09-03",
                                    "audience": {"type": "crm_query",
                                                 "preset": "closed_lost", "days": 30}},
                         "note": "Audience set to deals lost in the last 30 days."},
                        {"label": "The last quarter", "detail": "The usual choice — "
                         "enough accounts to be worth a sequence.",
                         "config": {"window_start": "2026-05-06", "window_end": "2026-09-03",
                                    "audience": {"type": "crm_query",
                                                 "preset": "closed_lost", "days": 90}},
                         "note": "Audience set to deals lost in the last 90 days."},
                        {"label": "Everything this year", "detail": "Widest reach. "
                         "Expect some accounts to have changed buyer entirely.",
                         "config": {"window_start": "2026-01-01", "window_end": "2026-09-03",
                                    "audience": {"type": "crm_query",
                                                 "preset": "closed_lost", "days": 240},
                                    "target_accounts": 120},
                         "note": "Capped at 120 accounts — at this reach the list gets "
                                 "long enough to outrun sending capacity."},
                    ]),
                "config": {
                    "name": "Closed-lost re-engagement",
                    "description": "Deals we lost, worked again when something changes "
                                   "at the account.",
                    "brief": "These accounts already evaluated us and chose otherwise, "
                             "so nothing here opens like a first touch. Every email "
                             "leads with what has CHANGED at their end since the "
                             "conversation ended — new funding, a sales team being "
                             "rebuilt, tooling that just went in. The argument is that "
                             "the reason it did not land last time has moved, and it is "
                             "worth fifteen minutes to look again. Never reference the "
                             "lost deal as a grievance and never re-pitch the same "
                             "case; the change at their end is the whole reason we are "
                             "writing.",
                    "membership_mode": "rolling",
                    "variant": "value-give",
                    "discovery_interval_days": 7,
                    "signal_query": {"kinds": ["research", "hiring"],
                                     "require_recent": True, "motion": "outbound",
                                     "personas": ["sales-leadership", "revops"]},
                },
                "notes": [
                    "Signal is set to real dated events only — for a re-engagement the "
                    "whole premise is that something changed, so the fallback anchor "
                    "would undercut it.",
                    "Outbound-only, so inbound-sourced contacts don't get counted as "
                    "outbound pipeline.",
                ],
            },
            {
                "keywords": ["hiring", "hire", "ramp", "new reps", "headcount",
                             "growing the team", "scaling the team", "sales roles"],
                "summary": "Set this up around the hiring signal: accounts actively "
                           "building a sales team, pitched on covering pipeline while "
                           "the new reps ramp.",
                "clarify": q(
                    "hiring_bar",
                    "How many open sales roles should an account have to qualify?",
                    "This is the difference between 'they're hiring' and 'they're "
                    "building a team' — it moves the account count a lot.",
                    ["signal_query"],
                    [
                        {"label": "Any open sales role",
                         "detail": "Widest. Includes one-off backfills.",
                         "config": {"signal_query": {"kinds": ["hiring", "research"],
                                                     "hiring_sales_min": 1,
                                                     "motion": "outbound"}}},
                        {"label": "Three or more",
                         "detail": "A team being built, not a backfill. The usual bar.",
                         "config": {"signal_query": {"kinds": ["hiring", "research"],
                                                     "hiring_sales_min": 3,
                                                     "motion": "outbound"}},
                         "note": "Three open sales roles is the point where ramp "
                                 "coverage is a problem the buyer already has."},
                        {"label": "Five or more",
                         "detail": "Only serious build-outs. Far fewer accounts.",
                         "config": {"signal_query": {"kinds": ["hiring", "research"],
                                                     "hiring_sales_min": 5,
                                                     "motion": "outbound"}}},
                    ]),
                "config": {
                    "name": "Hiring push — ramp coverage",
                    "description": "Accounts actively building a sales team.",
                    "brief": "Every account here is hiring reps right now, which means "
                             "the buyer is already carrying a number they cannot cover "
                             "for the next two quarters while those reps ramp. Email 2 "
                             "opens on the hiring itself — the count and one or two of "
                             "the roles — and ties it to covering pipeline during the "
                             "ramp, never to replacing anyone. Do not claim the "
                             "postings are new and never name where the data came from.",
                    "window_start": "2026-07-05",
                    "window_end": "2026-09-03",
                    "membership_mode": "rolling",
                    "variant": "earn",
                    "discovery_interval_days": 7,
                    "signal_query": {"kinds": ["hiring", "research"],
                                     "motion": "outbound"},
                },
                "notes": ["Discovery re-scans weekly — hiring is the signal that turns "
                          "over fastest, and each scan costs one credit per account."],
            },
            {
                "keywords": ["outreach", "salesloft", "apollo", "sequencing",
                             "displacement", "stack", "tooling", "competitor",
                             "already using"],
                "summary": "Set this up as a tech-play campaign against accounts "
                           "running a sequencing tool, on the no-disruption angle.",
                "clarify": q(
                    "stack_scope",
                    "Which stack are we going after?",
                    "It decides the tech play each account has to match, and therefore "
                    "which argument the sequence makes.",
                    ["signal_query"],
                    [
                        {"label": "Sequencing tools",
                         "detail": "Outreach, Salesloft, Apollo — the no-disruption story.",
                         "config": {"signal_query": {"kinds": ["tech", "research"],
                                                     "tech_playbook": ["sequencing"],
                                                     "motion": "outbound"}}},
                        {"label": "Intent / ABM platforms",
                         "detail": "6sense, Demandbase — the signal-activation story.",
                         "config": {"signal_query": {"kinds": ["tech", "research"],
                                                     "tech_playbook": ["intent_abm"],
                                                     "motion": "outbound"}},
                         "note": "Switched to the signal-activation angle: these "
                                 "accounts already generate more signal than they work."},
                        {"label": "Either",
                         "detail": "Any detected GTM stack worth a play.",
                         "config": {"signal_query": {
                             "kinds": ["tech", "research"],
                             "tech_playbook": ["sequencing", "intent_abm"],
                             "motion": "outbound"}}},
                    ]),
                "config": {
                    "name": "GTM stack plays",
                    "description": "Accounts running a detected GTM stack worth a play.",
                    "brief": "These accounts have already bought into outbound tooling, "
                             "so the argument is emphatically not replacement. We ship "
                             "our own email and LinkedIn infrastructure and run "
                             "alongside what they have, adding meetings on top of their "
                             "existing run rate rather than changing anything about it. "
                             "Reference at most ONE tool they run, and only where it "
                             "makes that point land.",
                    "window_start": "2026-06-05",
                    "window_end": "2026-09-03",
                    "membership_mode": "rolling",
                    "variant": "show",
                    "signal_query": {"kinds": ["tech", "research"], "motion": "outbound"},
                },
                "notes": [],
            },
        ],
        "default": {
            "summary": "Configured a general signal-led campaign: accounts showing "
                       "recent news or hiring activity in the last 30 days, worked "
                       "across sales leadership and RevOps.",
            "clarify": q(
                "who",
                "Who should this reach at those accounts?",
                "Persona decides which agent writes the sequence and how the value is "
                "framed — the case to a CRO is a different case to a RevOps lead.",
                ["signal_query"],
                [
                    {"label": "Sales leadership",
                     "detail": "CRO, VP Sales — they own the number.",
                     "config": {"signal_query": {"personas": ["sales-leadership"],
                                                 "kinds": ["research", "hiring"],
                                                 "motion": "outbound"}}},
                    {"label": "Sales leadership + SDR leaders",
                     "detail": "The number and the team that has to hit it.",
                     "config": {"signal_query": {
                         "personas": ["sales-leadership", "sdr-bdr"],
                         "kinds": ["research", "hiring"], "motion": "outbound"}}},
                    {"label": "The whole buying group",
                     "detail": "Widest — includes RevOps and partnerships.",
                     "config": {"signal_query": {"personas": [],
                                                 "kinds": ["research", "hiring"],
                                                 "motion": "outbound"}},
                     "note": "No persona filter — every ICP contact at a qualifying "
                             "account is in scope."},
                ]),
            "config": {
                "name": "Signal-led outbound",
                "description": "Accounts showing recent news or hiring activity.",
                "brief": "Lead every opener on the specific thing that just happened at "
                         "the account, and make the rest of the email earn that opener "
                         "rather than restate it. One question per email, one offer per "
                         "touch, no hype.",
                "window_start": "2026-08-04",
                "window_end": "2026-09-03",
                "membership_mode": "rolling",
                "variant": "value-give",
                "discovery_interval_days": 7,
            },
            "notes": ["Window defaulted to the last 30 days of signal — nothing in the "
                      "description named one."],
        },
    }, indent=2))


def build_campaign_copy_fixture(profile_dir):
    """Sample copy the demo's "Suggest" button returns.

    A demo has no ANTHROPIC_API_KEY and must never say so — the agent writing copy
    IS the product. These drafts are served by the demo fixture route, so the button
    behaves exactly as it does live without generating anything.
    """
    (profile_dir / "campaign_copy.json").write_text(json.dumps({
        "suggestions": {
            "email:1": {
                "subject": "a signal play for {{company}}",
                "body": "{{first_name}} — saw {{company}} is scaling the sales team.\n\n"
                        "I put together a signal play off your hiring and tech-stack "
                        "signals. Accounts working those signals progress about 4.4x "
                        "faster than ones that don't.\n\n"
                        "Worth 15 minutes for me to walk you through it?",
            },
            "email:2": {
                "subject": "your current run rate",
                "body": "{{first_name}} — one more angle.\n\nOur AI SDR ships its own "
                        "email and LinkedIn infrastructure, so nothing about your "
                        "current stack or process changes. It runs alongside and adds "
                        "2-5x more meetings on top of your existing run rate.\n\n"
                        "Worth 15 minutes? I'll calculate {{company}}'s run rate and "
                        "estimate what it would add.",
            },
            "email:3": {
                "subject": "how Memgraph activated their signals",
                "body": "{{first_name}} — Memgraph had more in-market accounts "
                        "surfacing than the team could work. The signals were already "
                        "there; nobody had capacity to act on them.\n\nTheir AI SDR "
                        "now works those the moment they appear.\n\nWant to grab 15 "
                        "minutes to map {{company}}'s signal sets and find the "
                        "highest-yield ones?",
            },
            "email:4": {
                "subject": "should I close your file?",
                "body": "{{first_name}} — I haven't heard back, so I'll assume the "
                        "timing is wrong.\n\nBefore I close your file: worth 15 minutes "
                        "to walk through a one-page playbook of 3 AI-SDR plays for your "
                        "team? Yours either way.",
            },
        },
        "default": {
            "subject": "a quick idea for {{company}}",
            "body": "{{first_name}} — I put together something specific to "
                    "{{company}} off the signals we track.\n\nWorth 15 minutes for me "
                    "to walk you through it?",
        },
        "note": "Demo sample copy — served instead of calling the model.",
    }, indent=2))


def build_copy(profile_dir, contacts, rng):
    """Per-contact copy: its own researched angle, persona voice, and CTA play.

    The point of this generator is that the Outreach view has to survive scrolling.
    One subject template across every row makes the product look like mail-merge,
    which is the opposite of the claim it is demonstrating — so subject, body,
    length, angle and CTA all vary per contact.
    """
    gen = profile_dir / "outreach" / "generated"
    gen.mkdir(parents=True, exist_ok=True)
    tech_by_domain, hiring_by_domain = {}, {}
    for idx, (_c, domain, _s) in enumerate(COMPANIES):
        tech_by_domain[domain] = TECH_STACKS[idx % len(TECH_STACKS)][0]
        hiring_by_domain[domain] = HIRING[idx % len(HIRING)][0]

    written, plays = 0, {}
    for c in contacts:
        if c["status"] not in ("generated", "enrolled"):
            continue
        first, company = c["first_name"], c["company"]
        tech = tech_by_domain[c["domain"]]
        hiring = hiring_by_domain[c["domain"]]
        signal = c["signal"]                        # per-contact, set in build_contacts
        voice = PERSONA_VOICE[c["persona"]]
        play = CTA_PLAYS[(hash(c["contact_id"]) % len(CTA_PLAYS))]
        plays[play["id"]] = plays.get(play["id"], 0) + 1
        sales_roles = " sales:" in hiring
        # Vary the shape too — a uniform word count is its own tell.
        long_form = rng.random() < 0.45

        body1 = (
            f"{first}, saw {company} {signal}.\n\n{voice['line']}\n\n"
            + ("We run an AI SDR that researches each account and writes the first "
               "touches, so your reps start from a warm list instead of a raw one.\n\n"
               if long_form else
               "We run an AI SDR that writes the first touches so your reps start "
               "warm.\n\n")
            + play["ask"].format(company=company)
        )
        body2 = (
            (f"{first}, following up on {company}.\n\n"
             f"{hiring.split('·')[1].strip() if sales_roles else 'On the hiring front'} "
             "— the ramp window is where pipeline usually slips.\n\n"
             if sales_roles else
             f"{first}, quick follow-up for {company}.\n\n")
            + f"On top of {tech.split('·')[0].strip()} we add 2-5x the touch volume "
              "without changing how the team works.\n\nOpen to a look at the run rate?"
        )
        asset = {
            "contact_id": c["contact_id"], "persona": c["persona"],
            "variant": c["variant"], "signal": signal,
            "tech_signals": tech, "hiring_signals": hiring,
            "cta_play": play["id"], "demo": True,
            "email": {
                "subject1": play["subject"].format(company=company),
                "body1": body1,
                "subject2": ("covering the gap while you hire" if sales_roles
                             else f"the {voice['pain']} problem"),
                "body2": body2,
                "subject3": "how Memgraph did it",
                "body3": (f"{first}, last bit of proof for {company}.\n\nMemgraph "
                          "mapped their intent signals to the accounts worth a first "
                          "touch and ran our AI SDR to $2.7M in pipeline.\n\n"
                          f"Want the same mapping for {company}?"),
                "subject4": ("should I close your file?" if long_form
                             else f"last one, {first}"),
                "body4": (f"{first}, I'll stop reaching out on this one. If "
                          f"{voice['pain']} is still open, the offer stands."),
            },
            "linkedin": {
                "li_connect": (f"{first}, saw {company} {signal.split(' and ')[0]}. "
                               f"We work with teams on {voice['pain']}."),
                "li_msg1": (f"Thanks for connecting, {first}. "
                            + play["ask"].format(company=company)),
                "li_msg2": (f"{first}, one more nudge — Memgraph ran the same play to "
                            "$2.7M in pipeline. Worth 20 minutes?"),
            },
        }
        (gen / f"{c['contact_id']}.json").write_text(
            json.dumps(asset, indent=2, ensure_ascii=False))
        written += 1
    return written, plays


def build_contacts_jsonl(profile_dir, contacts):
    path = profile_dir / "outreach" / "contacts.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as f:
        for c in contacts:
            f.write(json.dumps({k: c[k] for k in (
                "contact_id", "first_name", "last_name", "email", "title",
                "company", "persona", "domain")}, ensure_ascii=False) + "\n")
    return path


def build_campaign_stats(profile_dir, rng):
    """Bison denominators + an append-only snapshot history.

    The history is what makes the Trends rate-over-time chart real: six monthly
    snapshots whose differenced windows dip and recover. Numbers are chosen to
    match the trends generator's planted windows.
    """
    d = profile_dir / "campaign-stats"
    d.mkdir(parents=True, exist_ok=True)
    fetched = "2026-06-30T00:00:00Z"
    totals = {"Demo request": (620, 15), "Lead magnet (PDF)": (9100, 56),
              "Event": (2400, 12), "Persona-targeted": (5200, 5)}
    camps, steps, history = [], [], []
    for cid, name, offer in CAMPAIGNS:
        contacted, interested = totals[offer]
        replies = max(interested, int(contacted * 0.07))
        rec = {
            "campaign_id": cid, "campaign_name": name, "status": "active",
            "type": "outbound", "total_leads": contacted + 40,
            "total_leads_contacted": contacted,
            "emails_sent": int(contacted * 2.6), "unique_replies": replies,
            "interested": interested, "bounced": int(contacted * 0.01),
            "unsubscribed": int(contacted * 0.004),
            "interested_rate_pct": round(100 * interested / contacted, 2),
            "reply_rate_pct": round(100 * replies / contacted, 2),
            "fetched_at": fetched,
        }
        camps.append(rec)
        # Step decay + the step-3 trough / step-4 spike the funnel chart reveals.
        for step, (share, rate) in enumerate(
                [(1.0, 0.150), (0.67, 0.190), (0.58, 0.059), (0.48, 0.450)], start=1):
            lc = int(contacted * share)
            si = max(0, round(lc * rate / 100))
            steps.append({
                "campaign_id": cid, "campaign_name": name, "step_number": step,
                "sequence_step_id": cid * 10 + step,
                "email_subject": "{SUBJECT%d}" % step,
                "sent": int(lc * 1.05), "leads_contacted": lc,
                "unique_replies": max(si, int(lc * 0.02)), "interested": si,
                "interested_rate_pct": round(100 * si / lc, 3) if lc else None,
                "reply_rate_pct": round(100 * max(si, int(lc * 0.02)) / lc, 2) if lc else None,
                "fetched_at": fetched,
            })

    # Six snapshots, cumulative — the console differences them.
    windows = [("2026-01-31", 1800, 0.94), ("2026-02-28", 2400, 0.83),
               ("2026-03-31", 3600, 0.36), ("2026-04-30", 4200, 0.31),
               ("2026-05-31", 3800, 0.79), ("2026-06-30", 3740, 0.96)]
    cum_c = cum_i = 0
    for day, new_c, rate in windows:
        cum_c += new_c
        cum_i += round(new_c * rate / 100)
        at = f"{day}T00:00:00Z"
        # Spread each snapshot's totals across the campaigns by their share.
        share_base = sum(totals[o][0] for _, _, o in CAMPAIGNS)
        for cid, name, offer in CAMPAIGNS:
            share = totals[offer][0] / share_base
            history.append({
                "campaign_id": cid, "campaign_name": name, "status": "active",
                "type": "outbound",
                "total_leads": int(cum_c * share) + 40,
                "total_leads_contacted": int(cum_c * share),
                "emails_sent": int(cum_c * share * 2.6),
                "unique_replies": int(cum_c * share * 0.06),
                "interested": int(cum_i * share),
                "bounced": 0, "unsubscribed": 0,
                "interested_rate_pct": None, "reply_rate_pct": None,
                "fetched_at": at,
            })

    for name, rows in (("campaigns.jsonl", camps), ("step_stats.jsonl", steps),
                       ("campaigns_history.jsonl", history)):
        with (d / name).open("w") as f:
            for r in rows:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
    (d / "last_run.json").write_text(json.dumps({
        "fetched_at": fetched, "campaigns": len(camps), "step_rows": len(steps),
        "total_contacted": sum(c["total_leads_contacted"] for c in camps),
        "total_interested": sum(c["interested"] for c in camps),
        "errors": [], "demo": True,
    }, indent=2))
    return len(camps), len(steps), len(history)


def build_replies_queue(profile_dir, contacts, rng):
    """A realistic interested-reply inbox so the Replies view has something to work.

    Written in the shape classify_replies.py produces, since that is what the
    console reads. Threads accompany each item so opening a card shows a real
    conversation rather than an empty pane.
    """
    d = profile_dir / "interested-replies"
    (d / "threads").mkdir(parents=True, exist_ok=True)
    enrolled = [c for c in contacts if c["status"] == "enrolled"]
    rng.shuffle(enrolled)

    # (intent, confidence, reply text) — the mix a healthy inbox actually shows.
    REPLIES = [
        ("meeting_request", 0.93,
         "Yes — this is timely. We're adding two AEs next month and pipeline coverage "
         "is exactly the gap. Tuesday or Wednesday afternoon both work; send an invite."),
        ("meeting_request", 0.89,
         "Interested. I'd want to understand how it works alongside our existing "
         "sequencer before we change anything. Can you do 20 minutes Thursday?"),
        ("info_request", 0.81,
         "Can you send over the detail on how the research step works? Specifically "
         "what it uses to pick the signal it opens on."),
        ("pricing", 0.86,
         "What does this cost at our size (roughly 12 reps)? If it's in range I'll "
         "loop in our RevOps lead."),
        ("referral", 0.84,
         "Not me — our SDR manager owns this. Copying them here; they'll pick it up."),
        ("positive_later", 0.79,
         "Good timing but not this quarter — we're mid-migration. Ping me in early "
         "October and I'll make time."),
        ("info_request", 0.77,
         "How do you handle deliverability at that volume? That's the part that has "
         "burned us before."),
        ("meeting_request", 0.91,
         "The hiring point landed — we have 4 open sales roles and no coverage while "
         "they ramp. Let's talk. What does the pilot look like?"),
    ]
    REASONS = {
        "meeting_request": "Explicit ask for a meeting with availability offered.",
        "info_request": "Asking for specific product detail — warm but not yet a meeting.",
        "pricing": "Direct pricing question with a buying-group referral implied.",
        "referral": "Forwarding to the right owner — interested at the account level.",
        "positive_later": "Positive sentiment with a concrete future date.",
    }

    items = []
    base = datetime.now(timezone.utc)
    for i, (intent, conf, text) in enumerate(REPLIES):
        if i >= len(enrolled):
            break
        c = enrolled[i]
        rid = 500000 + i
        when = base - timedelta(days=i, hours=rng.randint(0, 9))
        subj = f"Re: a signal play for {c['company']}"
        items.append({
            "reply_id": rid,
            "lead_id": 700000 + i,
            "sender_email_id": 12,
            "from_name": f"{c['first_name']} {c['last_name']}",
            "from_email": c["email"],
            "subject": subj,
            "campaign_id": CAMPAIGNS[i % len(CAMPAIGNS)][0],
            "date_received": now_iso(when),
            "text_body": text,
            "already_interested": True,
            "channel": "email",
            "classifier": {"interested": True, "confidence": conf,
                           "reason": REASONS.get(intent, "Positive reply."),
                           "intent": intent},
        })
        # Thread: our first touch, then their reply.
        (d / "threads" / f"{rid}.md").write_text(
            f"# {c['first_name']} {c['last_name']} — {c['company']}\n\n"
            f"## Outbound · {now_iso(when - timedelta(days=3))}\n\n"
            f"**{subj.replace('Re: ', '')}**\n\n"
            f"{c['first_name']}, saw {c['company']} is scaling its commercial team. "
            "Teams at that stage usually add pipeline coverage before the new reps "
            "ramp, not after.\n\nWorth 20 minutes to see the run rate?\n\n"
            f"## Reply · {now_iso(when)}\n\n{text}\n")

    # Pre-written follow-up drafts, in the shape draft_followups.py emits, plus
    # `demo_alternates` so "regenerate" produces a genuinely different message.
    # This is what makes the agent's drafting step demoable without a model call.
    DRAFTS = {
        "meeting_request": (
            "Thanks {first} — Wednesday afternoon works. I'll send an invite for 2pm ET.\n\n"
            "Before we talk I'll pull the signal map for {company} so we're looking at "
            "your actual accounts rather than a generic demo. Anything specific you want "
            "covered?",
            ["Great — sending a Wednesday 2pm ET invite now.\n\nI'll come with the "
             "signal map for {company} and the run-rate model so you can see the "
             "numbers against your own list, not a sample one.",
             "Booked for Wednesday 2pm ET.\n\nI'll bring two things: the accounts we'd "
             "prioritise at {company}, and what the first 30 days would produce. If "
             "someone from RevOps should be on it, feel free to forward."]),
        "info_request": (
            "Good question, {first} — the research step reads three things per account: "
            "recent company news, the GTM stack we can detect from public signals, and "
            "open sales roles.\n\nWhichever is strongest becomes the opener, so no two "
            "emails at {company} would look the same. Happy to walk through a live "
            "example on a call if that's useful.",
            ["Short version, {first}: news, detected GTM stack, and open sales roles. "
             "The strongest of the three becomes the opener.\n\nI can show you the "
             "actual output for five of your accounts if that's more useful than an "
             "explanation."]),
        "pricing": (
            "{first} — at 12 reps you'd be in our mid tier. Rather than quote blind, the "
            "honest answer is it depends on volume and how many personas you're "
            "targeting.\n\nHappy to put real numbers in front of you and your RevOps "
            "lead on a short call — 20 minutes and you'd have a figure you can plan "
            "against.",
            ["{first} — 12 reps puts you in the mid tier. I'd rather give you a real "
             "number than a range, which needs 20 minutes on volume and persona count.\n\n"
             "Want me to include your RevOps lead so pricing and attribution get covered "
             "together?"]),
        "referral": (
            "Appreciated, {first} — thanks for the intro.\n\nHi, copying you in: we run "
            "an AI SDR that researches each account and writes the first touches, so "
            "your reps start from a warm list. Worth 20 minutes to see what it would "
            "produce for {company}?",
            ["Thanks for passing it along, {first}.\n\nCopying in your SDR lead: happy "
             "to show what the first 30 days would look like for {company} — the "
             "accounts we'd prioritise and the copy we'd send."]),
        "positive_later": (
            "Understood, {first} — October it is. I'll follow up in the first week.\n\n"
            "In the meantime I'll keep an eye on {company} and bring anything relevant "
            "when we speak, so we're not starting cold.",
            ["That's fine, {first} — I'll come back the first week of October.\n\n"
             "Nothing needed from you before then. If the migration timeline moves, just "
             "reply here and I'll adjust."]),
    }
    drafts = []
    for it in items:
        intent = it["classifier"]["intent"]
        tmpl = DRAFTS.get(intent)
        if not tmpl:
            continue
        first = (it["from_name"] or "there").split()[0]
        company = next((c["company"] for c in contacts
                        if c["email"] == it["from_email"]), "your team")
        body, alts = tmpl
        drafts.append({
            "reply_id": it["reply_id"], "lead_id": it["lead_id"],
            "channel": "email", "sender_email_id": it["sender_email_id"],
            "linkedin_account_id": None, "conversation_id": None,
            "from_name": it["from_name"], "from_email": it["from_email"],
            "subject": it["subject"], "campaign_id": it["campaign_id"],
            "original_reply": it["text_body"],
            "intent": intent,
            "draft": body.format(first=first, company=company),
            "demo_alternates": [a.format(first=first, company=company) for a in alts],
            "rationale": f"Matches the {intent.replace('_', ' ')} intent; keeps the "
                         "next step concrete and offers proof over persuasion.",
            "error": None, "status": "drafted",
            "drafted_at": now_iso(base - timedelta(minutes=12)),
            "agent": "standard",
        })
    # Inbound rows for the repliers, so the Sequence column's "replied" badge and
    # the detail note are backed by the same ledger the live path uses.
    import sqlite3 as _sq
    _c = _sq.connect(str(profile_dir / "outreach" / "pipeline.db"))
    for it in items:
        cid = next((c["contact_id"] for c in contacts
                    if c["email"] == it["from_email"]), None)
        if not cid:
            continue
        _c.execute(
            "INSERT OR IGNORE INTO hubspot_activity_log (dedup_key, event_type, "
            "channel, contact_id, engagement_id, status, event_ts, created_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (f"inbound:{cid}:{it['reply_id']}", "inbound", "email", cid,
             f"demo-eng-{cid}-in", "logged", it["date_received"], it["date_received"]))
    _c.commit()
    _c.close()

    (d / "followup_drafts.json").write_text(json.dumps({
        "generated_at": now_iso(base - timedelta(minutes=12)),
        "campaign_id": CAMPAIGNS[0][0], "demo": True, "items": drafts,
    }, indent=2, ensure_ascii=False))

    (d / "review_queue.json").write_text(json.dumps({
        "fetched_at": now_iso(base), "demo": True,
        "scanned_at": now_iso(base - timedelta(minutes=18)),
        "counts": {"interested": len(items), "reviewed": 0},
        "items": items,
    }, indent=2))
    (d / "li_review_queue.json").write_text(json.dumps({
        "fetched_at": now_iso(base), "demo": True,
        "scanned_at": now_iso(base - timedelta(minutes=18)), "items": [],
    }, indent=2))
    # Healthy activity-log status, so the Replies header doesn't show the host's
    # HubSpot state (a red "sync issue") inside a demo.
    (d / ".autosync_status.json").write_text(json.dumps({
        "ok": True, "mode": "incremental", "at": now_iso(base - timedelta(minutes=41)),
        "summary": "logged 34 email + 11 LinkedIn activities", "demo": True,
    }, indent=2))
    return len(items), len(drafts)


def build_analytics_fixtures(profile_dir):
    """LinkedIn + AI SDR attribution fixtures.

    Both are normally live API / Mongo reads, which a demo must not perform. These
    stand in so the Analytics view is fully populated instead of showing two
    'not configured' panels.
    """
    synced = now_iso(datetime.now(timezone.utc) - timedelta(hours=7))
    # Field names must match exactly what AnalyticsPage reads (HeyReach's own
    # camelCase keys pass straight through the live path) or the tiles render "—".
    (profile_dir / "linkedin.json").write_text(json.dumps({
        "configured": True, "demo": True,
        "campaign_id": 4821, "campaign_name": "AI SDR — LinkedIn touches",
        "status": "ACTIVE",
        "funnel": {"totalUsers": 640, "totalUsersFinished": 498,
                   "totalUsersInProgress": 118, "totalUsersPending": 24},
        "stats": {"connectionsSent": 612, "connectionsAccepted": 214,
                  "messagesSent": 498, "totalMessageReplies": 71,
                  "uniqueLeadsContacted": 604, "autoTaggedInterested": 38},
    }, indent=2))
    # Deal list first, then derive the stage rollup and the total from it, so the
    # parts always sum to the whole — exactly as mongo_store computes it live.
    # Shape must match aisdr_analytics(): by_stage[] rows and deals[] rows.
    day = datetime(2026, 3, 2, tzinfo=timezone.utc)
    # (name, stage, amount, age_days, owner, contacts, attribution)
    # A believable mix: most outbound-originated, a few influenced where the contact
    # had also come in through an inbound source.
    deals_src = [
        ("Northwind Analytics — AI SDR pilot", "Proposal", 62000, 0, "Dana Whitfield", 3),
        ("Verity Pay — outbound expansion", "Demo scheduled", 54000, 12, "Dana Whitfield", 2),
        ("Sable Security — GTM automation", "Discovery", 48000, 19, "Marcus Bell", 4),
        ("Tandem Labs — pipeline coverage", "Proposal", 45000, 26, "Dana Whitfield", 2),
        ("Orchard Retail Cloud — AI SDR", "Discovery", 41000, 33, "Marcus Bell", 3),
        ("Cobalt Grid — SDR augmentation", "Demo scheduled", 38000, 41, "Dana Whitfield", 1),
        ("Lumen Freight — outbound rebuild", "Discovery", 36000, 48, "Marcus Bell", 2),
        ("Kestrel Health — pipeline pilot", "Closed won", 32000, 55, "Dana Whitfield", 3),
        ("Aperture Robotics — AI SDR", "Discovery", 31000, 62, "Marcus Bell", 2),
        ("Brightsend — GTM coverage", "Demo scheduled", 28000, 70, "Dana Whitfield", 1),
        ("Meridian Talent — SDR pilot", "Discovery", 26000, 77, "Marcus Bell", 2),
        ("Ferrous Supply — outbound", "Proposal", 25000, 84, "Dana Whitfield", 3),
        ("Northwind Analytics — expansion", "Closed lost", 12000, 91, "Marcus Bell", 1),
        ("Cobalt Grid — pilot extension", "Discovery", 8000, 98, "Dana Whitfield", 1),
    ]
    INFLUENCED = {2, 7, 11}          # 1-indexed positions sourced inbound too
    deals = [{
        "id": f"demo-deal-{i}", "name": name, "stage": stage, "amount": amount,
        "created_at": int((day + timedelta(days=age)).timestamp() * 1000),
        "owner": owner, "contacts": contacts,
        "attribution": "influenced" if i in INFLUENCED else "originated",
    } for i, (name, stage, amount, age, owner, contacts) in enumerate(deals_src, start=1)]

    rollup = {}
    for d in deals:
        r = rollup.setdefault(d["stage"], {"stage": d["stage"], "deals": 0, "amount": 0})
        r["deals"] += 1
        r["amount"] += d["amount"]
    by_stage = sorted(rollup.values(), key=lambda r: r["amount"], reverse=True)
    total_pipeline = sum(d["amount"] for d in deals)
    by_attribution = {}
    for d in deals:
        r = by_attribution.setdefault(d["attribution"], {"deals": 0, "amount": 0})
        r["deals"] += 1
        r["amount"] += d["amount"]

    (profile_dir / "aisdr.json").write_text(json.dumps({
        "configured": True, "demo": True,
        "deals_created": len(deals), "total_pipeline": total_pipeline,
        "emails_logged": 18420, "contacts_emailed": 4870,
        "by_stage": by_stage, "deals": deals, "by_attribution": by_attribution,
        "last_sync_at": synced,
    }, indent=2))
    (profile_dir / "aisdr_status.json").write_text(json.dumps({
        "configured": True, "demo": True, "running": False, "started_at": None,
        "last_result": {"ok": True, "emails": 18420, "deals_flagged": 14},
        "watermark_ms": 0, "last_sync_at": synced,
    }, indent=2))


def build_hubspot_lists(profile_dir):
    """HubSpot list search results for the Use view.

    Without this the console shells out to the real portal — which a demo must not
    touch, and which with no token dumps the script's traceback into the page.
    object_type_id 0-1 = contact list, 0-2 = company list (the Use view branches
    on exactly that).
    """
    (profile_dir / "hubspot_lists.json").write_text(json.dumps({
        "demo": True,
        "lists": [
            {"list_id": "2198", "name": "ICP — GTM leadership (US B2B tech)",
             "object_type_id": "0-1", "size": 1240, "processing_type": "DYNAMIC"},
            {"list_id": "2204", "name": "ICP — Series B+ SaaS, 50-500 employees",
             "object_type_id": "0-1", "size": 860, "processing_type": "DYNAMIC"},
            {"list_id": "2211", "name": "Event follow-up — SaaStr attendees",
             "object_type_id": "0-1", "size": 312, "processing_type": "STATIC"},
            {"list_id": "2216", "name": "Re-engage — no reply in 90 days",
             "object_type_id": "0-1", "size": 2480, "processing_type": "DYNAMIC"},
            {"list_id": "3301", "name": "Target accounts — data infrastructure",
             "object_type_id": "0-2", "size": 180, "processing_type": "DYNAMIC"},
            {"list_id": "3308", "name": "Target accounts — fintech expansion",
             "object_type_id": "0-2", "size": 95, "processing_type": "STATIC"},
        ],
    }, indent=2))


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--profile", default="generic", help="profile id (dir name)")
    ap.add_argument("--label", default=None, help="human label shown in the switcher")
    ap.add_argument("--description", default=None)
    ap.add_argument("--customer", default=None,
                    help="customer this profile is tailored for, if any")
    ap.add_argument("--contains-real-accounts", action="store_true",
                    help="the accounts in this profile are REAL companies (e.g. a "
                         "customer's target list) with synthetic engagement. Changes "
                         "the console's banner wording — set it honestly.")
    ap.add_argument("--contacts", type=int, default=120)
    ap.add_argument("--skip-trends", action="store_true",
                    help="don't run the interested-trends generator")
    args = ap.parse_args()

    if not args.profile.replace("-", "").replace("_", "").isalnum():
        print(f"ERROR: invalid profile id {args.profile!r} — use [a-z0-9_-]")
        return 1

    profile_dir = PROJECT_ROOT / "data" / "demo" / args.profile
    profile_dir.mkdir(parents=True, exist_ok=True)
    rng = random.Random(SEED)

    contacts = build_contacts(rng, args.contacts)
    db_path, n_batches = build_db(profile_dir, contacts, rng)
    n_copy, cta_plays = build_copy(profile_dir, contacts, rng)
    build_contacts_jsonl(profile_dir, contacts)
    n_camp, n_step, n_hist = build_campaign_stats(profile_dir, rng)
    n_replies, n_drafts = build_replies_queue(profile_dir, contacts, rng)
    build_analytics_fixtures(profile_dir)
    build_hubspot_lists(profile_dir)
    n_campaigns, camp_counts, overlap_n, hot_n = build_campaigns(profile_dir, contacts, rng)
    build_campaign_copy_fixture(profile_dir)
    build_campaign_brief_fixture(profile_dir)
    n_crm, n_clay = build_source_pools(profile_dir, contacts, rng)
    build_report_recipes(profile_dir)

    trends_ok = True
    if not args.skip_trends:
        r = subprocess.run([sys.executable, str(TRENDS_GEN), "--profile", args.profile],
                           cwd=str(TRENDS_GEN.parent), capture_output=True, text=True)
        trends_ok = r.returncode == 0
        if not trends_ok:
            print(f"WARNING: trends generator failed:\n{r.stderr.strip()[:500]}")

    # Connector states this profile presents in Setup. Every provider the profile
    # shows data for must read as connected, or Setup contradicts the rest of the
    # console: enrolled contacts need the channels, generated copy needs the model,
    # hiring signals need Prospeo, and the attribution tiles need Mongo.
    #
    # An INTEGRATED provider left off this list is HIDDEN, not shown as broken — so
    # anything the demo tells a story about has to be named here. `hubspot-files` is
    # on it because the CTA plays carry content, and a content picker reporting zero
    # connected sources is the not-configured impression a demo must never give.
    (profile_dir / "connectors.json").write_text(json.dumps({
        "connected": ["hubspot", "hubspot-files", "clay", "prospeo", "emailbison",
                      "heyreach", "anthropic", "mongodb"],
        "note": "Demo connector states — declared, never probed from the host.",
    }, indent=2))

    covers = ["pipeline", "signals", "outreach", "analytics", "campaigns"]
    if trends_ok:
        covers += ["trends", "replies"]
    (profile_dir / "profile.json").write_text(json.dumps({
        "id": args.profile,
        # The default profile is just "Demo mode" — it is THE demo until someone
        # builds a named one, so labelling it "Generic demo" only adds noise.
        "label": args.label or ("Demo mode" if args.profile == "generic"
                                else f"{args.profile.replace('-', ' ').title()} demo"),
        "description": args.description or (
            # Must not contradict contains_real_accounts — the two sit next to each
            # other in the switcher.
            "Synthetic end-to-end dataset: generated copy, signals, campaign stats "
            "and reply trends."
            if args.contains_real_accounts else
            "Synthetic end-to-end dataset: fictional accounts, generated copy, "
            "signals, campaign stats and reply trends."),
        "customer": args.customer,
        # This generator only ever emits fictional .example accounts, so the flag is
        # False unless explicitly forced. Spec-driven profiles built from a real
        # target list (see docs/demo-profiles.md) set it to True.
        "contains_real_accounts": bool(args.contains_real_accounts),
        "covers": sorted(set(covers)),
        "generated_at": now_iso(datetime.now(timezone.utc)),
        "generator": "make_demo_profile.py",
        "synthetic": True,
        "contacts": len(contacts),
    }, indent=2))

    print(f"Demo profile '{args.profile}' written to {profile_dir}")
    print(f"  {len(contacts)} contacts across {n_batches} batches, "
          f"{len(COMPANIES)} accounts with signals")
    print(f"  {n_copy} generated copy files across {len(CTA_PLAYS)} CTA plays "
          f"{dict(sorted(cta_plays.items()))}")
    print(f"  {n_camp} bison campaigns / {n_step} step rows / {n_hist} history rows")
    print(f"  {n_campaigns} console campaigns {camp_counts}, "
          f"{overlap_n} contacts in >1 campaign, {hot_n} hot targets")
    print(f"  demo source pools: {n_crm} unpulled CRM contacts, {n_clay} Clay candidates")
    print(f"  {n_replies} interested replies + threads, {n_drafts} pre-written drafts")
    print("  LinkedIn + attribution fixtures, HubSpot lists")
    print(f"  trends slice: {'built' if trends_ok else 'FAILED — see warning above'}")
    print(f"  covers: {', '.join(sorted(set(covers)))}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
