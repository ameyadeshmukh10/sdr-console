"""SQLite-backed batch store for the SDR pipeline.

Durable, resumable queue so sub-agents can work batches of contacts in parallel.
DB lives at data/outreach/pipeline.db. Generated copy stays in
data/outreach/generated/<contact_id>.json (the DB tracks status + batching).

Statuses: contacts = pending|generated|enrolled|failed ; batches = pending|in_progress|done
"""

import hashlib
import json
import re
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[4]  # scripts/ -> sdr-pipeline -> skills -> .claude -> project
DB_PATH = PROJECT_ROOT / "data" / "outreach" / "pipeline.db"
GEN_DIR = PROJECT_ROOT / "data" / "outreach" / "generated"


def now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def email_domain(email):
    """Company key for the signal cache: the lowercased email domain."""
    email = (email or "").strip().lower()
    return email.split("@")[-1] if "@" in email else ""


def connect(path=None):
    """Open the pipeline DB read-write.

    `path` overrides the live location — used by demo mode to point writes at a
    profile's own synthetic DB, and by tests. Passed per call rather than by
    mutating DB_PATH, because the server is threaded and a global swap would leak
    one request's demo profile into another's writes.
    """
    DB_PATH_ = Path(path) if path else DB_PATH
    DB_PATH_.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH_), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")       # safe concurrent reads/writes
    conn.execute("PRAGMA synchronous=NORMAL")     # WAL pairing: fsync at checkpoint, not per commit
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def retry_locked(fn, attempts=5, base_delay=1.0):
    """Run fn() with bounded expo backoff on SQLite write-slot contention
    ("database is locked" under WAL = another writer starved us past
    busy_timeout). fn must be idempotent and open its OWN connection — a failed
    attempt's connection is not reused. Non-lock errors propagate immediately."""
    for attempt in range(attempts):
        try:
            return fn()
        except sqlite3.OperationalError as e:
            msg = str(e).lower()
            if "locked" not in msg or attempt >= attempts - 1:
                raise
            time.sleep(min(base_delay * (2 ** attempt), 10))


def init_schema(conn):
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS contacts (
        contact_id  TEXT PRIMARY KEY,
        first_name  TEXT, last_name TEXT, email TEXT, title TEXT, company TEXT,
        linkedin_url TEXT, persona TEXT, domain TEXT, variant TEXT,
        -- Provenance from HubSpot: how this contact came to exist. `motion` is our
        -- derived outbound|inbound classification (see MOTION_INBOUND_SOURCES);
        -- the raw source/lifecycle are kept so the derivation can be revisited
        -- without a re-pull.
        source TEXT, latest_source TEXT, lifecycle_stage TEXT, motion TEXT,
        -- Direct-dial and mobile, pulled from the CRM. Kept so the call list can
        -- offer the number the rep actually needs when the score says "call" —
        -- a prioritised call list without a phone number is a to-do list.
        phone TEXT, mobile_phone TEXT,
        batch_id    INTEGER,
        status      TEXT DEFAULT 'pending',
        error       TEXT,
        updated_at  TEXT
    );
    CREATE TABLE IF NOT EXISTS batches (
        batch_id     INTEGER PRIMARY KEY,
        status       TEXT DEFAULT 'pending',
        size         INTEGER,
        claimed_at   TEXT,
        completed_at TEXT
    );
    -- Per-company research cache, keyed by email domain. Reused for 90 days so a
    -- company is searched once instead of once per contact / per re-run.
    -- tech_* columns hold the technographic scan (tech_signals.py): the formatted
    -- summary string, the structured detections JSON, when it ran, and any error.
    -- hiring_* columns hold the job-postings scan (hiring_signals.py), same shape.
    CREATE TABLE IF NOT EXISTS account_signals (
        domain            TEXT PRIMARY KEY,
        company_name      TEXT,
        signal            TEXT,
        has_recent        INTEGER,
        researched_at     TEXT,
        model             TEXT,
        updated_at        TEXT,
        tech_signals      TEXT,
        tech_detail       TEXT,
        tech_checked_at   TEXT,
        tech_error        TEXT,
        hiring_signals    TEXT,
        hiring_detail     TEXT,
        hiring_checked_at TEXT,
        hiring_error      TEXT
    );
    -- Idempotency ledger for HubSpot activity logging (hubspot_activity_sync.py).
    -- One row per logged email engagement; the dedup_key makes re-runs no-ops.
    CREATE TABLE IF NOT EXISTS hubspot_activity_log (
        dedup_key     TEXT PRIMARY KEY,
        event_type    TEXT NOT NULL,   -- outbound | inbound | our_reply
        channel       TEXT,            -- email (linkedin reserved; out of scope)
        contact_id    TEXT,            -- resolved HubSpot contact id (NULL if unresolved)
        engagement_id TEXT,            -- HubSpot email engagement id (NULL on failure)
        status        TEXT NOT NULL,   -- logged | failed | skipped_no_contact
        error         TEXT,
        event_ts      TEXT,            -- source timestamp (sent_at / date_received)
        created_at    TEXT
    );
    -- Cache of Bison email -> lead_id (the bulk enroll path discards lead_id, and the
    -- contacts table has none). Built by a paginated /api/leads sweep, then used to
    -- pull each lead's actually-sent emails. last_sent_at lets steady-state runs skip
    -- leads whose sequence has finished instead of re-fetching all ~4.6k every time.
    CREATE TABLE IF NOT EXISTS bison_lead_map (
        email         TEXT PRIMARY KEY,
        lead_id       INTEGER,
        contact_id    TEXT,
        last_sent_at  TEXT,
        last_fetch_at TEXT,
        updated_at    TEXT
    );
    -- Durable inbox for HeyReach (LinkedIn) webhook events. The webhook handler
    -- persists the raw payload here on receipt and ACKs immediately; a separate
    -- drain (heyreach_activity.py) processes pending rows into HubSpot LinkedIn
    -- Communications + the hubspot_activity_log ledger. dedup_key (when present)
    -- makes HeyReach retries no-ops; raw is the verbatim body, our source of truth.
    CREATE TABLE IF NOT EXISTS heyreach_events (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        event_id     TEXT,            -- HeyReach event id when the payload carries one
        event_type   TEXT,            -- raw HeyReach discriminator (best-effort parse)
        dedup_key    TEXT,            -- computed normalized key (NULL if unparseable)
        status       TEXT DEFAULT 'pending',  -- pending | done | skipped | failed
        attempts     INTEGER DEFAULT 0,
        error        TEXT,
        raw          TEXT NOT NULL,   -- verbatim request body
        received_at  TEXT,
        processed_at TEXT
    );
    -- Unenrollment/suppression ledger (unenrollment_check.py). One row per
    -- (rule, channel, contact); dedup_key makes the 30-min sweeps idempotent.
    -- A 'failed' row is retried next sweep and flips to 'done' (same contract
    -- as hubspot_activity_log). rule is the checker id ('everworker_tag' is
    -- the first of several planned suppression rules).
    CREATE TABLE IF NOT EXISTS unenrollment_log (
        dedup_key    TEXT PRIMARY KEY,  -- "<rule>:<channel>:<contact_id>"
        rule         TEXT NOT NULL,     -- 'everworker_tag' | future rule ids
        contact_id   TEXT NOT NULL,
        email        TEXT,
        linkedin_url TEXT,
        channel      TEXT NOT NULL,     -- bison | heyreach
        campaign_ids TEXT,              -- JSON list of campaigns stopped in (NULL if none)
        action       TEXT NOT NULL,     -- stopped | not_active | not_found | no_identifier | skipped_unconfigured
        status       TEXT NOT NULL,     -- done | failed
        error        TEXT,
        created_at   TEXT
    );
    -- Append-only observation log for account signals. account_signals holds one
    -- MUTABLE latest row per domain, so "which accounts showed signal between X and
    -- Y" is not answerable from it — an upsert destroys the prior value and the
    -- *_checked_at columns record when we LOOKED, not what we found. This table is
    -- the time dimension campaigns qualify against: one row the first time a given
    -- signal value is seen for a domain.
    --
    -- fingerprint dedups by VALUE, not by scan: re-running an unchanged scan is not
    -- a new event, so observed_at stays the moment the signal actually appeared to
    -- us. A changed signal (new funding round, new roles, new tool) writes a new row.
    CREATE TABLE IF NOT EXISTS signal_events (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        domain      TEXT NOT NULL,
        kind        TEXT NOT NULL,   -- research | tech | hiring
        summary     TEXT,            -- the formatted line as shown in the UI
        has_recent  INTEGER,         -- research only: a real dated event vs a fallback anchor
        detail      TEXT,            -- JSON payload (tech detections / hiring postings)
        fingerprint TEXT NOT NULL,   -- sha1 of the normalized summary
        observed_at TEXT NOT NULL
    );
    CREATE UNIQUE INDEX IF NOT EXISTS idx_sigev_fp
        ON signal_events(domain, kind, fingerprint);
    CREATE INDEX IF NOT EXISTS idx_sigev_window ON signal_events(observed_at, kind);
    CREATE INDEX IF NOT EXISTS idx_sigev_domain ON signal_events(domain);

    -- ---- Campaigns -------------------------------------------------------
    -- A campaign is a DEFINED SET OF ACCOUNTS SHOWING SIGNAL OVER A TARGET WINDOW,
    -- worked through an ordered sequence of steps. Membership is derived, not typed:
    -- signal_query + [window_start, window_end] is the definition, campaign_members
    -- is the materialized result of applying it (re-applied on every sweep when
    -- membership_mode='rolling', frozen at launch when 'snapshot').
    --
    -- 1:1 with the downstream sender: one console campaign owns one Bison email
    -- campaign and one HeyReach LinkedIn campaign, so stats roll straight up.
    CREATE TABLE IF NOT EXISTS campaigns (
        campaign_id     INTEGER PRIMARY KEY AUTOINCREMENT,
        key             TEXT UNIQUE,     -- url/CLI-friendly slug
        name            TEXT NOT NULL,
        description     TEXT,
        status          TEXT DEFAULT 'draft',    -- draft|active|paused|completed|archived
        window_start    TEXT,            -- inclusive ISO date; NULL = open-ended
        window_end      TEXT,            -- inclusive ISO date; NULL = open-ended
        signal_query    TEXT,            -- JSON, see campaigns.SIGNAL_QUERY_KEYS
        -- WHICH ACCOUNTS are in scope, as opposed to signal_query which decides
        -- which of them qualify. JSON, see audiences.AUDIENCE_TYPES:
        --   {"type":"all_contacts"}                       everything already pulled
        --   {"type":"hubspot_list","list_id":"2198"}      a HubSpot list
        --   {"type":"crm_query","preset":"closed_lost",   a CRM segment, e.g. every
        --    "days":30}                                   contact on a deal lost in 30d
        -- Composable with signal_query: audience narrows the pool, signal_query
        -- decides who in that pool is worth working now.
        audience        TEXT,
        -- The agreed DIRECTION for this campaign, in prose: what was decided about
        -- who we are going after and what we are saying to them. Distinct from
        -- `description` (a label) and from the structured fields (which say what to
        -- DO): this is the part of "we had a meeting and decided X" that no column
        -- captures, and it is fed to the copy generator so the sequence argues the
        -- case that was actually agreed. Written by the campaign brief configurator
        -- (webui/server/campaign_brief.py) or typed by hand.
        brief           TEXT,
        -- Discovery = actively SCANNING in-scope accounts for signal, as opposed to
        -- qualification, which only reads signals already observed. Re-run on this
        -- cadence so a campaign keeps finding accounts rather than going stale.
        discovery_interval_days INTEGER DEFAULT 7,
        last_discovery_at       TEXT,
        -- EVERGREEN: a campaign that is meant to keep running rather than end when
        -- its window closes. It does NOT silently relaunch itself — at the end of
        -- each cycle it stops and asks, because the thing that goes stale in an
        -- always-on campaign is the MESSAGE, and a campaign that quietly re-ran the
        -- same four emails at the next cohort forever is exactly the failure mode.
        --   evergreen_interval_days  how long a cycle runs before the review is due
        --   review_due_at            when the user gets asked (set at launch/relaunch)
        --   review_state             NULL | 'pending' — waiting on a human right now
        --   cycle                    which run this is, so results stay comparable
        -- WHICH CHANNELS this campaign is meant to use. A declaration of intent,
        -- not a capability check: the same account list is worked differently
        -- depending on the play — LinkedIn only for a senior committee, email for
        -- volume, and ads across the whole account to build familiarity before
        -- either lands. Per-contact channel RECOMMENDATIONS (capacity.py) still
        -- apply inside whatever this allows.
        -- {"email": bool, "linkedin": bool, "ads": bool}
        channels            TEXT,
        -- OUTBOUND or INBOUND. Not the same thing as signal_query.motion, which is a
        -- filter over who gets picked: this is what KIND of campaign it is, and it
        -- changes the framing. An inbound contact raised their hand, so the copy
        -- must not cold-open at them, and the pipeline it produces must not be
        -- reported as outbound-created (see the inbound/outbound delineation note).
        campaign_type       TEXT DEFAULT 'outbound',
        evergreen           INTEGER DEFAULT 0,
        evergreen_interval_days INTEGER,
        review_due_at       TEXT,
        review_state        TEXT,
        cycle               INTEGER DEFAULT 1,
        relaunched_at       TEXT,
        membership_mode TEXT DEFAULT 'rolling',  -- rolling|snapshot
        variant         TEXT,            -- instruction variant for generation
        bison_campaign_id    TEXT,
        heyreach_campaign_id TEXT,
        target_accounts   INTEGER,       -- soft cap; NULL = uncapped
        created_at        TEXT,
        updated_at        TEXT,
        launched_at       TEXT,
        completed_at      TEXT,
        last_qualified_at TEXT
    );
    -- The offer library (ai-sdr/knowledge/cta-offers.md) as data, so a sequence step
    -- can REFERENCE a CTA by key instead of the copy being the only record of which
    -- offer it carried. Seeded from CTA_LIBRARY; user rows have builtin=0.
    CREATE TABLE IF NOT EXISTS campaign_ctas (
        cta_key      TEXT PRIMARY KEY,
        label        TEXT NOT NULL,
        tier         TEXT,             -- A | B
        give         TEXT NOT NULL,    -- the deliverable the meeting is anchored on
        ask          TEXT NOT NULL,    -- the meeting ask
        example      TEXT,             -- verbatim example line from the library
        channels     TEXT,             -- JSON list: email | linkedin | reply
        default_step INTEGER,          -- cadence default (cta-offers.md placement)
        active       INTEGER DEFAULT 1,
        builtin      INTEGER DEFAULT 1,
        updated_at   TEXT
    );
    -- One row per step of the sequence. cta_key is THE LINK between a sequence step
    -- and the offer it carries — previously this existed only as prose in the
    -- generation prompt and was reverse-engineered afterwards by app.derive_cta().
    -- copy_mode='manual' means subject/body here are authoritative (merge variables
    -- allowed); 'generated' means the persona agent writes it inside the frame this
    -- row declares (cta_key + angle).
    CREATE TABLE IF NOT EXISTS campaign_steps (
        campaign_id INTEGER NOT NULL,
        step_no     INTEGER NOT NULL,
        channel     TEXT NOT NULL DEFAULT 'email',   -- email | linkedin
        day_offset  INTEGER,          -- days after enrollment
        cta_key     TEXT,             -- -> campaign_ctas.cta_key
        angle       TEXT,             -- this step's job, fed verbatim to the generator
        copy_mode   TEXT DEFAULT 'generated',        -- generated | manual
        subject     TEXT,
        body        TEXT,
        updated_at  TEXT,
        PRIMARY KEY (campaign_id, step_no, channel)
    );
    -- Materialized membership. signal_kind/signal_snapshot record WHY this contact
    -- qualified, at the moment they did — the campaign's audit trail, and what makes
    -- a rolling campaign explainable after the signal row has since been overwritten.
    -- priority_score is the SIGNAL STRENGTH at the moment of qualification, frozen
    -- there on purpose: it is what ordered the SDR's call list on the day the
    -- campaign launched, and a score that silently drifted as signals aged would
    -- make yesterday's call list unreproducible. Re-scoring is an explicit action.
    CREATE TABLE IF NOT EXISTS campaign_members (
        campaign_id     INTEGER NOT NULL,
        contact_id      TEXT NOT NULL,
        domain          TEXT,
        state           TEXT DEFAULT 'qualified',  -- qualified|generated|enrolled|replied|removed
        signal_kind     TEXT,        -- research | tech | hiring (which family qualified them)
        signal_snapshot TEXT,        -- JSON of the signal as it read at qualification
        priority_score  REAL,        -- 0-100 signal strength, frozen at qualification
        score_band      TEXT,        -- hot | warm | cool
        score_detail    TEXT,        -- JSON {components:{...}} — why it scored that
        scored_at       TEXT,
        -- Score MOMENTUM. A contact worked in an earlier campaign already has a
        -- score on record; whether this one is higher or lower is itself a signal.
        -- An account warming up outranks a statically-equal account going cold, so
        -- the call list sorts on rank_score (= priority_score + a bounded momentum
        -- adjustment), while priority_score stays the pure, comparable signal
        -- strength. Both are frozen at qualification.
        previous_score  REAL,        -- their most recent prior score, if any
        momentum        REAL,        -- priority_score - previous_score
        rank_score      REAL,        -- what the call list actually orders by
        -- Which channels this contact is worth spending on, derived from the score
        -- and the buyer-group role. JSON {call, email, linkedin, ads: bool} plus a
        -- reason — sending capacity is finite, so the score has to say where to
        -- spend it, not just who is best.
        channels        TEXT,
        buyer_role      TEXT,        -- decision-maker | champion | influencer | user
        -- Provenance: did we already have this contact, or did enrichment find them?
        origin          TEXT DEFAULT 'existing',   -- existing | enriched
        origin_detail   TEXT,        -- JSON (audience id, clay task, credits spent)
        -- WORKING STATE, scoped to this campaign. A rep changing their mind about
        -- one person in one campaign must not change them everywhere: "not a fit
        -- for the funding push" and "stop contacting this person" are different
        -- decisions, and conflating them is how a good contact gets silently
        -- burned. Person-level state lives on `contacts` (engagement_state).
        --   manual_priority  a hand override; the call list sorts on it when set,
        --                    so a rep can pull someone to the top without the
        --                    scorer's opinion being lost (priority_score stays)
        --   snoozed_until    hidden from the list until this date, still a member
        --   outcome          what happened when they were worked
        manual_priority REAL,
        snoozed_until   TEXT,
        worked_at       TEXT,
        outcome         TEXT,        -- worked | no_answer | not_a_fit | later
        note            TEXT,
        qualified_at    TEXT,
        enrolled_at     TEXT,
        bison_lead_id   INTEGER,
        updated_at      TEXT,
        PRIMARY KEY (campaign_id, contact_id)
    );
    -- NB: the priority_score index is created AFTER the ALTER migrations below —
    -- on a DB that predates scoring the column does not exist yet, and CREATE TABLE
    -- IF NOT EXISTS above is a no-op there.
    CREATE INDEX IF NOT EXISTS idx_cmembers_contact ON campaign_members(contact_id);
    CREATE INDEX IF NOT EXISTS idx_cmembers_state   ON campaign_members(campaign_id, state);
    CREATE INDEX IF NOT EXISTS idx_campaigns_status ON campaigns(status);

    -- ---- Metered spend + sending capacity ---------------------------------
    -- Every call that costs money or consumes a send allowance lands here. The
    -- system can spend real money (Clay and Prospeo credits) and can burn a
    -- finite daily/monthly sending allowance without a human watching, so both
    -- are measured against the same ledger. Report-only by design: nothing here
    -- blocks an action, it makes the spend visible.
    --
    -- units is deliberately generic — "credits" for enrichment, "sends" for
    -- outbound — because the question ("what did this campaign consume?") is the
    -- same shape for both.
    CREATE TABLE IF NOT EXISTS usage_ledger (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        provider    TEXT NOT NULL,   -- clay | prospeo | bison | heyreach | anthropic
        operation   TEXT NOT NULL,   -- find-contacts | enrich-company | email-send | li-send
        units       REAL NOT NULL DEFAULT 1,
        unit_kind   TEXT NOT NULL DEFAULT 'credits',   -- credits | sends
        campaign_id INTEGER,         -- NULL for work not attributable to one campaign
        ref         TEXT,            -- domain / contact id / job id, for tracing
        detail      TEXT,            -- JSON
        occurred_at TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_usage_when ON usage_ledger(occurred_at);
    CREATE INDEX IF NOT EXISTS idx_usage_prov ON usage_ledger(provider, occurred_at);
    CREATE INDEX IF NOT EXISTS idx_usage_camp ON usage_ledger(campaign_id);

    -- ---- Buyer group ------------------------------------------------------
    -- WHO we sell to, as data. This one definition drives four things that used to
    -- be four separate hardcoded lists and could drift apart silently:
    --   1. which titles Clay is asked to search for   (clay_titles)
    --   2. which returned titles we keep              (match_patterns, is_icp)
    --   3. which persona agent writes their copy      (persona)
    --   4. which contacts justify a rep's call        (worth_calling)
    --
    -- Rules are ORDERED and first-match-wins, which is load-bearing: "Sales
    -- Operations" has to hit the RevOps rule before the generic Sales one. Exclusion
    -- is not a special case — it is simply the highest-priority rule (sort_order 0)
    -- mapping to is_icp=0, so "no interns" and "CROs are buyers" are the same kind
    -- of statement and are edited in the same place.
    CREATE TABLE IF NOT EXISTS buyer_group_roles (
        role_key       TEXT PRIMARY KEY,
        label          TEXT NOT NULL,
        seniority      TEXT,        -- decision-maker | champion | influencer | excluded
        match_patterns TEXT,        -- JSON list of case-insensitive regex fragments
        clay_titles    TEXT,        -- JSON list of literal titles to search for
        persona        TEXT,        -- which persona agent writes for them
        is_icp         INTEGER DEFAULT 1,
        worth_calling  INTEGER DEFAULT 0,   -- senior enough for a rep's time
        sort_order     INTEGER NOT NULL,    -- match priority; 0 runs first
        active         INTEGER DEFAULT 1,
        builtin        INTEGER DEFAULT 1,
        updated_at     TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_bgroles_order ON buyer_group_roles(sort_order);

    -- ---- CRM field map ----------------------------------------------------
    -- The CRM is the source of truth. The console computes signals and PUSHES
    -- them into CRM properties, but always READS the CRM value back as
    -- authoritative — so a human editing the property in HubSpot overrides us
    -- rather than being silently reverted on the next scan.
    --
    -- One row per wired field. `local_key` names what the console computes
    -- (see crm_sync.LOCAL_FIELDS); object_type/property_name name where it lives
    -- in the CRM. Rows are seeded from CRM_FIELD_MAP but fully editable, because
    -- a different portal (or Salesforce) will use different API names.
    CREATE TABLE IF NOT EXISTS crm_field_map (
        local_key     TEXT PRIMARY KEY,
        object_type   TEXT NOT NULL,   -- contacts | companies | deals
        property_name TEXT NOT NULL,   -- the CRM API name
        label         TEXT,
        field_type    TEXT DEFAULT 'text',   -- text|textarea|html|number|bool
        direction     TEXT DEFAULT 'push',   -- push | pull | both | off
        enabled       INTEGER DEFAULT 1,
        auto_create   INTEGER DEFAULT 1,     -- create the property if absent
        last_push_at  TEXT,
        last_pull_at  TEXT,
        last_error    TEXT,
        pushed        INTEGER DEFAULT 0,     -- running counts, for the status card
        pulled        INTEGER DEFAULT 0,
        updated_at    TEXT
    );
    -- A list that arrived as a FILE: an event export, a webinar list, badge scans.
    -- Recorded as its own entity rather than dissolved into `contacts` because a
    -- campaign needs to be able to say "the people from the SaaStr list" as an
    -- audience — durably, and long after the file is gone. See contact_import.py.
    CREATE TABLE IF NOT EXISTS contact_imports (
        import_id   INTEGER PRIMARY KEY AUTOINCREMENT,
        label       TEXT NOT NULL,
        filename    TEXT,
        source      TEXT DEFAULT 'file',   -- file | api (room for other origins)
        rows        INTEGER,               -- rows in the file
        matched     INTEGER,               -- contacts we ended up holding
        detail      TEXT,                  -- JSON: the preview stats + create summary
        created_at  TEXT
    );
    CREATE TABLE IF NOT EXISTS contact_import_members (
        import_id   INTEGER NOT NULL,
        contact_id  TEXT NOT NULL,
        PRIMARY KEY (import_id, contact_id)
    );
    CREATE INDEX IF NOT EXISTS idx_import_members ON contact_import_members(contact_id);
    -- SIGNAL DEFINITIONS. The kinds of signal this deployment recognises, as DATA.
    --
    -- These used to be a dict in campaigns.py, which meant "what counts as a signal
    -- here" was a deploy. It is the most customer-specific thing in the product —
    -- one team's buying trigger is a page view, another's is "we lost a deal to them
    -- last year and they've gone quiet since" — so it belongs in a table with a
    -- screen in front of it.
    --
    -- Builtin rows (builtin=1) are seeded from SIGNAL_DEF_SEED and can be RETUNED
    -- (strength, decay, active) but not deleted: code writes events against those
    -- ids, and deleting one would strand history the scorer still reads.
    --
    -- `rule` is what makes a row self-executing: NULL means something else in the
    -- pipeline writes this kind's events (a scanner, the LLM, the CRM sync), while a
    -- rule means crm_signals.py derives it from CRM/contact data on a schedule.
    -- CUSTOMER PROOF, as data. A CTA is an offer; a reference is the evidence the
    -- offer is credible, and the two are separate because one story backs several
    -- offers ("Memgraph activated their signal set" is the proof under both the
    -- signal-mapping CTA and the run-rate one).
    --
    -- `nameable` is the load-bearing field, not a nicety: citing a customer by name
    -- without permission is a relationship and legal problem, and it is a fact about
    -- the CUSTOMER, not about the CTA that happens to reference them. When it is 0
    -- the generation prompt is told to describe them by industry instead, and the
    -- name is never sent.
    CREATE TABLE IF NOT EXISTS customer_references (
        ref_key     TEXT PRIMARY KEY,
        customer    TEXT NOT NULL,      -- who it is (only used in copy if nameable)
        nameable    INTEGER DEFAULT 0,  -- may we say the name out loud?
        anonymous   TEXT,               -- how to describe them when we may not
        industry    TEXT,
        story       TEXT NOT NULL,      -- what happened, in one or two sentences
        metric      TEXT,               -- the number worth quoting
        quote       TEXT,               -- a usable customer quote, if we have one
        source      TEXT,               -- where it came from (deck page, call, case study)
        -- Most proof already LIVES somewhere — a case-study page, a deck, a Drive
        -- doc. Holding the link (rather than only a retyped summary) means the
        -- console points at the source of truth instead of forking it, and anyone
        -- reading a play can go and check the claim.
        url         TEXT,
        kind        TEXT DEFAULT 'proof',   -- proof | asset | doc
        active      INTEGER DEFAULT 1,
        builtin     INTEGER DEFAULT 0,
        updated_at  TEXT
    );
    -- Which content each offer carries. A JOIN, not a column, because "add and
    -- remove content from a play" is inherently many-to-many: one case study backs
    -- several offers, and one offer often carries a story AND the one-pager.
    CREATE TABLE IF NOT EXISTS cta_content (
        cta_key    TEXT NOT NULL,
        ref_key    TEXT NOT NULL,
        sort_order INTEGER DEFAULT 100,
        added_at   TEXT,
        PRIMARY KEY (cta_key, ref_key)
    );
    CREATE TABLE IF NOT EXISTS signal_defs (
        kind         TEXT PRIMARY KEY,   -- slug used in signal_events.kind
        label        TEXT NOT NULL,
        description  TEXT,
        strength     REAL NOT NULL,      -- 0-50 base, before recency decay
        decay_scale  REAL DEFAULT 1.0,   -- >1 ages faster, <1 slower
        detector     TEXT,               -- scan|llm|crm|internal|rule
        rule         TEXT,               -- JSON, see crm_signals.validate_rule
        active       INTEGER DEFAULT 1,
        builtin      INTEGER DEFAULT 0,
        sort_order   INTEGER DEFAULT 100,
        last_run_at  TEXT,
        last_run_detail TEXT,            -- JSON: matches/errors from the last evaluation
        updated_at   TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_unenroll_contact ON unenrollment_log(contact_id);
    CREATE INDEX IF NOT EXISTS idx_unenroll_rule    ON unenrollment_log(rule, status);
    CREATE INDEX IF NOT EXISTS idx_contacts_batch  ON contacts(batch_id);
    CREATE INDEX IF NOT EXISTS idx_contacts_status ON contacts(status);
    CREATE INDEX IF NOT EXISTS idx_hsact_contact   ON hubspot_activity_log(contact_id);
    CREATE INDEX IF NOT EXISTS idx_hsact_status    ON hubspot_activity_log(status);
    CREATE INDEX IF NOT EXISTS idx_bisonmap_lead   ON bison_lead_map(lead_id);
    CREATE INDEX IF NOT EXISTS idx_hrev_status     ON heyreach_events(status);
    -- Partial unique index: dedups real ids, but never collapses NULL-keyed rows
    -- (an unparseable payload is still stored losslessly rather than dropped).
    CREATE UNIQUE INDEX IF NOT EXISTS idx_hrev_dedup ON heyreach_events(dedup_key)
        WHERE dedup_key IS NOT NULL;
    """)
    # migrate older DBs that predate the domain column, then backfill from email
    cols = [r["name"] for r in conn.execute("PRAGMA table_info(contacts)")]
    if "domain" not in cols:
        conn.execute("ALTER TABLE contacts ADD COLUMN domain TEXT")
    if "variant" not in cols:
        conn.execute("ALTER TABLE contacts ADD COLUMN variant TEXT")
    # migrate older DBs: contact provenance (inbound vs outbound delineation)
    for col in ("source", "latest_source", "lifecycle_stage", "motion",
                "phone", "mobile_phone",
                # PERSON-level engagement, independent of any campaign:
                #   active     the default
                #   paused     no outreach until paused_until, then active again
                #   suppressed do not contact, full stop
                # This is the level a rep reaches for when the problem is the
                # PERSON ("they asked us to stop") rather than the fit for one
                # campaign. It is enforced at qualification AND at the enroll gate
                # (unenrollment_check.suppressed_set) — a do-not-contact switch
                # that only hid a row would be worse than not having one.
                "engagement_state", "paused_until", "engagement_note",
                "engagement_updated_at"):
        if col not in cols:
            conn.execute(f"ALTER TABLE contacts ADD COLUMN {col} TEXT")
    # Gate the backfill behind a read: the UPDATE grabs the single WAL write
    # slot even when it changes nothing, and init_schema runs from every
    # subprocess entrypoint — unconditional, it starves against the server's
    # detector/sweep writers (the prod "database is locked" crash). Steady
    # state never matches (upsert_contacts computes domain on insert), so
    # gated init_schema takes no write lock at all.
    if conn.execute("SELECT 1 FROM contacts WHERE (domain IS NULL OR domain='') "
                    "AND email LIKE '%@%' LIMIT 1").fetchone():
        conn.execute("UPDATE contacts SET domain=lower(substr(email, instr(email,'@')+1)) "
                     "WHERE (domain IS NULL OR domain='') AND email LIKE '%@%'")
    # index after the column is guaranteed to exist
    conn.execute("CREATE INDEX IF NOT EXISTS idx_contacts_domain ON contacts(domain)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_contacts_motion ON contacts(motion)")
    # migrate older DBs: additive technographic + hiring columns on the signal cache
    sig_cols = [r["name"] for r in conn.execute("PRAGMA table_info(account_signals)")]
    for col in ("tech_signals", "tech_detail", "tech_checked_at", "tech_error",
                "hiring_signals", "hiring_detail", "hiring_checked_at", "hiring_error"):
        if col not in sig_cols:
            conn.execute(f"ALTER TABLE account_signals ADD COLUMN {col} TEXT")
    # migrate older DBs: additive campaign columns (scoring, discovery cadence)
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                    "AND name='campaign_members'").fetchone():
        mcols = [r["name"] for r in conn.execute("PRAGMA table_info(campaign_members)")]
        for col, typ in (("priority_score", "REAL"), ("score_band", "TEXT"),
                         ("score_detail", "TEXT"), ("scored_at", "TEXT"),
                         ("channels", "TEXT"), ("buyer_role", "TEXT"),
                         ("origin", "TEXT"), ("origin_detail", "TEXT"),
                         ("previous_score", "REAL"), ("momentum", "REAL"),
                         ("rank_score", "REAL"), ("manual_priority", "REAL"),
                         ("snoozed_until", "TEXT"), ("worked_at", "TEXT"),
                         ("outcome", "TEXT"), ("note", "TEXT")):
            if col not in mcols:
                conn.execute(f"ALTER TABLE campaign_members ADD COLUMN {col} {typ}")
        ccols = [r["name"] for r in conn.execute("PRAGMA table_info(campaigns)")]
        for col, typ in (("discovery_interval_days", "INTEGER"),
                         ("last_discovery_at", "TEXT"), ("audience", "TEXT"),
                         ("last_enrich_at", "TEXT"), ("brief", "TEXT"),
                         ("channels", "TEXT"), ("campaign_type", "TEXT"),
                         ("evergreen", "INTEGER"),
                         ("evergreen_interval_days", "INTEGER"),
                         ("review_due_at", "TEXT"), ("review_state", "TEXT"),
                         ("cycle", "INTEGER"), ("relaunched_at", "TEXT")):
            if col not in ccols:
                conn.execute(f"ALTER TABLE campaigns ADD COLUMN {col} {typ}")
        # safe now that priority_score is guaranteed to exist
        conn.execute("CREATE INDEX IF NOT EXISTS idx_cmembers_score "
                     "ON campaign_members(campaign_id, priority_score DESC)")
    # Seed the buyer group. Same read-gate; only ADDS missing rules, so an edited
    # or reordered ruleset survives a redeploy.
    have_bg = {r["role_key"] for r in conn.execute("SELECT role_key FROM buyer_group_roles")}
    new_bg = [b for b in BUYER_GROUP_SEED if b["role_key"] not in have_bg]
    if new_bg:
        conn.executemany("""
            INSERT OR IGNORE INTO buyer_group_roles
              (role_key, label, seniority, match_patterns, clay_titles, persona,
               is_icp, worth_calling, sort_order, active, builtin, updated_at)
            VALUES (:role_key,:label,:seniority,:match_patterns,:clay_titles,:persona,
                    :is_icp,:worth_calling,:sort_order,1,1,:ts)
        """, [{**b, "match_patterns": json.dumps(b["match_patterns"]),
               "clay_titles": json.dumps(b["clay_titles"]), "ts": now()}
              for b in new_bg])
    # Seed the CRM field map. Same read-gate. Only ADDS missing keys — an edited
    # row (a renamed property, a flipped direction) is never reset by a redeploy.
    have_map = {r["local_key"] for r in conn.execute("SELECT local_key FROM crm_field_map")}
    new_map = [f for f in CRM_FIELD_MAP if f["local_key"] not in have_map]
    if new_map:
        conn.executemany("""
            INSERT OR IGNORE INTO crm_field_map
              (local_key, object_type, property_name, label, field_type, direction,
               enabled, auto_create, updated_at)
            VALUES (:local_key,:object_type,:property_name,:label,:field_type,
                    :direction,1,:auto_create,:ts)
        """, [{**f, "auto_create": f.get("auto_create", 1), "ts": now()} for f in new_map])
    # Seed the CTA library. Gated behind a read for the same write-slot reason as
    # the domain backfill above: init_schema runs from every subprocess entrypoint.
    # Only ever ADDS missing builtin keys — an edited builtin row is left alone.
    # Seed the signal definitions. Same read-gate and same rule as every other seed
    # here: only ADDS missing builtin kinds, so a retuned strength or a deactivated
    # kind survives a redeploy.
    have_sig = {r["kind"] for r in conn.execute("SELECT kind FROM signal_defs")}
    new_sig = [s for s in SIGNAL_DEF_SEED if s["kind"] not in have_sig]
    if new_sig:
        conn.executemany("""
            INSERT OR IGNORE INTO signal_defs
              (kind, label, description, strength, decay_scale, detector, rule,
               active, builtin, sort_order, updated_at)
            VALUES (:kind,:label,:description,:strength,:decay_scale,:detector,NULL,
                    1,1,:sort_order,:ts)
        """, [{**s, "decay_scale": s.get("decay_scale", 1.0), "ts": now()}
              for s in new_sig])
    ref_cols = [r["name"] for r in conn.execute("PRAGMA table_info(customer_references)")]
    for col, typ in (("url", "TEXT"), ("kind", "TEXT")):
        if col not in ref_cols:
            conn.execute(f"ALTER TABLE customer_references ADD COLUMN {col} {typ}")
    # Seed the proof library. Same read-gate + add-only rule as every other seed:
    # an edited story or a revoked `nameable` survives a redeploy.
    have_refs = {r["ref_key"] for r in conn.execute("SELECT ref_key FROM customer_references")}
    new_refs = [r for r in CUSTOMER_REFERENCES if r["ref_key"] not in have_refs]
    if new_refs:
        conn.executemany("""
            INSERT OR IGNORE INTO customer_references
              (ref_key, customer, nameable, anonymous, industry, story, metric, quote,
               source, url, kind, active, builtin, updated_at)
            VALUES (:ref_key,:customer,:nameable,:anonymous,:industry,:story,:metric,
                    :quote,:source,:url,:kind,1,1,:ts)
        """, [{**r, "ts": now()} for r in new_refs])
    have_ctas = {r["cta_key"] for r in conn.execute("SELECT cta_key FROM campaign_ctas")}
    missing = [c for c in CTA_LIBRARY if c["cta_key"] not in have_ctas]
    if missing:
        conn.executemany("""
            INSERT OR IGNORE INTO campaign_ctas
              (cta_key, label, tier, give, ask, example, channels, default_step,
               active, builtin, updated_at)
            VALUES (:cta_key,:label,:tier,:give,:ask,:example,:channels,:default_step,
                    1,1,:ts)
        """, [{**c, "channels": json.dumps(c["channels"]), "ts": now()} for c in missing])
    # Attach the builtin offers to their proof, once. INSERT OR IGNORE, so content
    # someone detached by hand stays detached across a redeploy.
    if not conn.execute("SELECT 1 FROM cta_content LIMIT 1").fetchone():
        conn.executemany(
            "INSERT OR IGNORE INTO cta_content (cta_key, ref_key, sort_order, added_at) "
            "VALUES (?,?,?,?)",
            [(c, r, 10, now()) for c, r in (("signal-mapping", "memgraph-pipeline"),
                                            ("run-rate", "memgraph-scale"),
                                            ("signal-play", "memgraph-pipeline"))])
    # One-time seed of the signal event log from the existing latest-value rows, so
    # campaigns created today can still see the signals already in the cache. Their
    # observed_at is the scan timestamp we have, which is the best available proxy —
    # every event written from here on is stamped at real observation time.
    if (conn.execute("SELECT 1 FROM account_signals LIMIT 1").fetchone()
            and not conn.execute("SELECT 1 FROM signal_events LIMIT 1").fetchone()):
        for r in conn.execute("SELECT * FROM account_signals"):
            row = dict(r)
            _insert_signal_event(conn, row["domain"], "research", row.get("signal"),
                                 has_recent=row.get("has_recent"),
                                 observed_at=row.get("researched_at"))
            _insert_signal_event(conn, row["domain"], "tech", row.get("tech_signals"),
                                 detail=row.get("tech_detail"),
                                 observed_at=row.get("tech_checked_at"))
            _insert_signal_event(conn, row["domain"], "hiring", row.get("hiring_signals"),
                                 detail=row.get("hiring_detail"),
                                 observed_at=row.get("hiring_checked_at"))
    conn.commit()


# ---- Buyer group seed ------------------------------------------------------
# Ported verbatim from the behaviour of ai-sdr/scripts/buyer_group.py plus
# clay_enrich's search keywords, so turning this into data changed nothing about who
# qualifies on day one. ORDER IS THE LOGIC: first match wins, so the exclusion rule
# runs first and "Sales Operations" reaches RevOps before the generic Sales rule.
BUYER_GROUP_SEED = [
    {"role_key": "excluded", "label": "Excluded (IC / junior)", "seniority": "excluded",
     "sort_order": 0, "is_icp": 0, "worth_calling": 0, "persona": None,
     "match_patterns": [r"\b(intern|assistant|representative|coordinator|specialist"
                        r"|associate|account executive|junior|entry)\b"],
     "clay_titles": ["Intern", "Assistant", "Representative", "Coordinator",
                     "Specialist", "Associate", "Account Executive", "Junior"]},
    {"role_key": "cro", "label": "CRO / Sales Chief", "seniority": "decision-maker",
     "sort_order": 10, "is_icp": 1, "worth_calling": 1, "persona": "sales-leadership",
     "match_patterns": [r"\bcro\b|\bcso\b|\bcco\b|chief (revenue|sales|commercial|growth) officer"],
     "clay_titles": ["CRO", "Chief Revenue Officer", "Chief Sales Officer"]},
    {"role_key": "sdr_bdr", "label": "SDR/BDR", "seniority": "champion",
     "sort_order": 20, "is_icp": 1, "worth_calling": 0, "persona": "sdr-bdr",
     "match_patterns": [r"\bsdr\b|\bbdr\b|sales development|sales dev\b"],
     "clay_titles": ["SDR Manager", "BDR Manager", "Sales Development Manager",
                     "Head of Sales Development"]},
    # Before the generic sales rule on purpose: "Sales Operations" is RevOps.
    {"role_key": "revops", "label": "RevOps/Sales Ops", "seniority": "champion",
     "sort_order": 30, "is_icp": 1, "worth_calling": 0, "persona": "revops",
     "match_patterns": [r"revenue operations|\brevops\b|rev\s?ops|sales operations"
                        r"|sales ops|\bgtm ops\b"],
     "clay_titles": ["Director of Revenue Operations", "Revenue Operations", "RevOps",
                     "Sales Operations", "Sales Ops"]},
    {"role_key": "partnerships", "label": "Partnerships", "seniority": "influencer",
     "sort_order": 40, "is_icp": 1, "worth_calling": 0, "persona": "partnerships",
     "match_patterns": [r"\bpartnership|\bpartner(s)?\b|\balliances?\b|\bchannel\b|\becosystem\b"],
     "clay_titles": ["Head of Partnerships", "Director of Alliances", "Channel Director"]},
    {"role_key": "sales_leadership", "label": "VP/Head/Dir Sales-GTM",
     "seniority": "decision-maker", "sort_order": 50, "is_icp": 1, "worth_calling": 1,
     "persona": "sales-leadership",
     "match_patterns": [r"\b(chief|vp|evp|svp|vice president|head|director|dir)\b.*"
                        r"\b(sales|revenue|gtm|go[\s-]?to[\s-]?market|business development"
                        r"|biz dev|commercial)\b",
                        r"\b(sales|revenue|gtm|commercial)\b.*"
                        r"\b(vp|vice president|head|director)\b"],
     "clay_titles": ["VP of Sales", "Head of Sales", "Director of Sales",
                     "VP Revenue", "Head of GTM"]},
    {"role_key": "sales_ic", "label": "Sales/BD IC & Ops", "seniority": "influencer",
     "sort_order": 60, "is_icp": 1, "worth_calling": 0, "persona": "sales-leadership",
     "match_patterns": [r"\bsales\b|\brevenue\b|\bgtm\b|go[\s-]?to[\s-]?market"
                        r"|business development|\bbiz dev\b|\bcommercial\b"],
     "clay_titles": ["Sales Manager"]},
    # Marketing is ICP only at leadership level, where it owns pipeline/SDRs.
    {"role_key": "marketing_pipeline", "label": "Marketing-pipeline",
     "seniority": "influencer", "sort_order": 70, "is_icp": 1, "worth_calling": 0,
     "persona": "sales-leadership",
     "match_patterns": [r"\b(chief|cmo|vp|svp|evp|vice president|head|director)\b.*"
                        r"\b(marketing|demand gen|demand generation|growth)\b",
                        r"\bcmo\b"],
     "clay_titles": ["CMO", "VP of Marketing", "Head of Demand Generation"]},
    {"role_key": "founder", "label": "Founder/CEO", "seniority": "decision-maker",
     "sort_order": 80, "is_icp": 0, "worth_calling": 0, "persona": "sales-leadership",
     "match_patterns": [r"\bfounder\b|co-?founder|\bceo\b|chief executive"],
     "clay_titles": []},
]


def buyer_group_roles(conn, active_only=True, icp_only=False):
    """The ordered buyer-group rules. JSON columns decoded."""
    where = ["1=1"]
    if active_only:
        where.append("active=1")
    if icp_only:
        where.append("is_icp=1")
    out = []
    for r in conn.execute(f"SELECT * FROM buyer_group_roles WHERE {' AND '.join(where)} "
                          "ORDER BY sort_order, role_key"):
        row = dict(r)
        for k in ("match_patterns", "clay_titles"):
            try:
                row[k] = json.loads(row[k] or "[]")
            except (json.JSONDecodeError, TypeError):
                row[k] = []
        out.append(row)
    return out


BUYER_ROLE_FIELDS = ("label", "seniority", "persona", "is_icp", "worth_calling",
                     "sort_order", "active")


def update_buyer_role(conn, role_key, **fields):
    sets, params = [], []
    for k in BUYER_ROLE_FIELDS:
        if k in fields:
            sets.append(f"{k}=?")
            params.append(fields[k])
    for k in ("match_patterns", "clay_titles"):
        if k in fields:
            sets.append(f"{k}=?")
            params.append(json.dumps(fields[k] or []))
    if not sets:
        return None
    conn.execute(f"UPDATE buyer_group_roles SET {', '.join(sets)}, updated_at=? "
                 "WHERE role_key=?", params + [now(), role_key])
    conn.commit()
    r = conn.execute("SELECT * FROM buyer_group_roles WHERE role_key=?",
                     (role_key,)).fetchone()
    return dict(r) if r else None


def add_buyer_role(conn, role_key, label, **fields):
    conn.execute("""
        INSERT OR IGNORE INTO buyer_group_roles
          (role_key, label, seniority, match_patterns, clay_titles, persona, is_icp,
           worth_calling, sort_order, active, builtin, updated_at)
        VALUES (?,?,?,?,?,?,?,?,?,1,0,?)
    """, (role_key, label, fields.get("seniority") or "influencer",
          json.dumps(fields.get("match_patterns") or []),
          json.dumps(fields.get("clay_titles") or []),
          fields.get("persona"), 1 if fields.get("is_icp", True) else 0,
          1 if fields.get("worth_calling") else 0,
          int(fields.get("sort_order") or 100), now()))
    conn.commit()
    r = conn.execute("SELECT * FROM buyer_group_roles WHERE role_key=?",
                     (role_key,)).fetchone()
    return dict(r) if r else None


def delete_buyer_role(conn, role_key):
    """Only user-added rules can be deleted; a builtin is deactivated instead so the
    ordering the rest of the ruleset depends on stays intact."""
    row = conn.execute("SELECT builtin FROM buyer_group_roles WHERE role_key=?",
                       (role_key,)).fetchone()
    if not row:
        return False
    if row["builtin"]:
        conn.execute("UPDATE buyer_group_roles SET active=0, updated_at=? "
                     "WHERE role_key=?", (now(), role_key))
    else:
        conn.execute("DELETE FROM buyer_group_roles WHERE role_key=?", (role_key,))
    conn.commit()
    return True


# ---- CRM field map seed ----------------------------------------------------
# The fields the console wires to the CRM. The CRM is the source of truth: these
# say where each computed value LIVES over there, so a signal written today can be
# read back tomorrow — by us, by RevOps, or by a workflow nobody told us about.
#
# `direction`:
#   push  console computes it, CRM stores it (we overwrite)
#   pull  CRM owns it, we read it (a human or another system writes it)
#   both  push what we compute, but a CRM value that differs wins on the next read
#   off   wired but inactive
#
# The three company signal properties already exist in the portal and are already
# written by tech_signals/hiring_signals — mapping them here doesn't change that
# behaviour, it makes it visible and re-pointable instead of hardcoded.
CRM_FIELD_MAP = [
    {"local_key": "tech_signals", "object_type": "companies",
     "property_name": "technographic_signals", "label": "Technographic Signals",
     "field_type": "textarea", "direction": "both"},
    {"local_key": "hiring_signals", "object_type": "companies",
     "property_name": "hiring_signals", "label": "Hiring Signals",
     "field_type": "text", "direction": "both"},
    {"local_key": "hiring_roles_count", "object_type": "companies",
     "property_name": "open_roles_count", "label": "Open Roles Count",
     "field_type": "text", "direction": "both"},
    {"local_key": "hiring_job_titles", "object_type": "companies",
     "property_name": "hiring_signals_job_titles", "label": "Hiring Signals Job Titles",
     "field_type": "html", "direction": "push"},
    # The account signal in prose — the thing an AE actually reads before a call.
    {"local_key": "account_signal", "object_type": "companies",
     "property_name": "ai_sdr_account_signal", "label": "AI SDR Account Signal",
     "field_type": "textarea", "direction": "both"},
    # Per-contact campaign state. These make the CRM answer "is the AI SDR working
    # this person, how urgent, and on which channels" without opening the console.
    {"local_key": "priority_score", "object_type": "contacts",
     "property_name": "ai_sdr_priority_score", "label": "AI SDR Priority Score",
     "field_type": "number", "direction": "push"},
    {"local_key": "priority_band", "object_type": "contacts",
     "property_name": "ai_sdr_priority_band", "label": "AI SDR Priority Band",
     "field_type": "text", "direction": "push"},
    # ALL campaigns this contact is in, "; "-joined — membership lives on the
    # person, so an overlap has to be visible in the CRM too.
    {"local_key": "campaign_name", "object_type": "contacts",
     "property_name": "ai_sdr_campaigns", "label": "AI SDR Campaigns",
     "field_type": "text", "direction": "push"},
    {"local_key": "recommended_channels", "object_type": "contacts",
     "property_name": "ai_sdr_channels", "label": "AI SDR Recommended Channels",
     "field_type": "text", "direction": "push"},
    {"local_key": "buyer_role", "object_type": "contacts",
     "property_name": "ai_sdr_buyer_role", "label": "AI SDR Buyer Group Role",
     "field_type": "text", "direction": "push"},
    # Read-only: RevOps owns this one and we must never write it.
    {"local_key": "suppressed", "object_type": "contacts",
     "property_name": "everworker_tag", "label": "EverWorker Tag",
     "field_type": "text", "direction": "pull", "auto_create": 0},
]


# ---- CTA library seed ------------------------------------------------------
# Mirrors .claude/skills/ai-sdr/knowledge/cta-offers.md — that file stays the prose
# source the generation prompt reads; this is the same library as data so a sequence
# step can point at a specific offer. Keep the two in sync when either changes.
# ---- signal definitions ----------------------------------------------------
# The BUILTIN signal kinds. This is the seed for `signal_defs`, and the fallback
# `campaigns.SIGNAL_REGISTRY` is derived from it — one source, so a kind cannot mean
# one thing to the scorer and another to the builder.
#
# `strength` is the 0-50 base the scorer uses before recency decay: how good a
# REASON TO CALL the kind is on its own. A funding round is a conversation; a page
# view is a hint.
# `decay_scale` multiplies apparent AGE, so a kind can go stale faster (intent) or
# slower (a warm prior score) than the default.
# `detector` is what produces the events: scan (discovery), llm (copy generation),
# crm (field sync), internal (our own history), rule (crm_signals.py evaluates a
# user-defined rule).
SIGNAL_DEF_SEED = [
    {"kind": "research", "label": "Account news", "strength": 50, "detector": "llm",
     "sort_order": 10,
     "description": "Funding, launches, acquisitions, exec hires — the researched "
                    "event the opener is built on."},
    {"kind": "hiring", "label": "Hiring", "strength": 43, "detector": "scan",
     "sort_order": 20,
     "description": "Open roles from the job-postings scan. Sales roles are the hook."},
    {"kind": "tech", "label": "Tech stack", "strength": 27, "detector": "scan",
     "sort_order": 30,
     "description": "GTM tooling detected from the website and DNS."},
    {"kind": "website_visit", "label": "Website visit", "strength": 40,
     "detector": "crm", "decay_scale": 3.0, "sort_order": 40,
     "description": "They came to the site. Behavioural intent, and the freshest "
                    "signal there is — so it also ages the fastest."},
    # Identified website visitors. Distinct from `website_visit` (which is a known
    # contact returning) — this is a de-anonymisation vendor putting a NAME to
    # anonymous traffic, i.e. an account researching you that you had no idea was
    # there. Strongest behavioural signal available, and the fastest to go stale:
    # someone who was on the pricing page this morning is in-market, someone who was
    # there a month ago is a statistic.
    {"kind": "web_deanon", "label": "Identified website visitor", "strength": 46,
     "detector": "crm", "decay_scale": 4.0, "sort_order": 35,
     "description": "A de-anonymisation tool put a name to anonymous traffic — an "
                    "account on your site that never filled anything in."},
    {"kind": "prior_score", "label": "Prior campaign score", "strength": 22,
     "detector": "internal", "decay_scale": 0.3, "sort_order": 50,
     "description": "Aggregate priority they carried in earlier campaigns — a warm "
                    "history is a weaker reason to call than news, but a durable one."},
    {"kind": "crm_field", "label": "CRM field", "strength": 30, "detector": "crm",
     "sort_order": 60,
     "description": "Anything a mapped CRM property says is worth acting on."},
]


def signal_defs(conn, active_only=False, with_rules=None):
    """Every signal kind this deployment recognises. `rule` decoded to a dict.

    `with_rules=True` returns only the self-executing ones (the kinds crm_signals
    derives); `False` only the ones something else in the pipeline writes."""
    where = "WHERE active=1" if active_only else ""
    out = []
    for r in conn.execute(f"SELECT * FROM signal_defs {where} "
                          "ORDER BY sort_order, kind"):
        row = dict(r)
        try:
            row["rule"] = json.loads(row["rule"] or "null")
        except (json.JSONDecodeError, TypeError):
            row["rule"] = None
        if with_rules is True and not row["rule"]:
            continue
        if with_rules is False and row["rule"]:
            continue
        out.append(row)
    return out


def get_signal_def(conn, kind):
    r = conn.execute("SELECT * FROM signal_defs WHERE kind=?", (kind,)).fetchone()
    if not r:
        return None
    row = dict(r)
    try:
        row["rule"] = json.loads(row["rule"] or "null")
    except (json.JSONDecodeError, TypeError):
        row["rule"] = None
    return row


def upsert_signal_def(conn, kind, **fields):
    """Create or patch one definition. Only the keys passed are written.

    A builtin's IDENTITY is immutable — `kind` is what every stored event references,
    so renaming one would orphan its history. Everything else about a builtin
    (strength, decay, whether it is active) is fair game to retune."""
    existing = get_signal_def(conn, kind)
    cols = ("label", "description", "strength", "decay_scale", "detector",
            "active", "sort_order", "last_run_at", "last_run_detail")
    vals = {k: fields[k] for k in cols if k in fields}
    if "rule" in fields:
        vals["rule"] = (json.dumps(fields["rule"])
                        if fields["rule"] is not None else None)
    vals["updated_at"] = now()
    if existing:
        sets = ", ".join(f"{k}=?" for k in vals)
        conn.execute(f"UPDATE signal_defs SET {sets} WHERE kind=?",
                     list(vals.values()) + [kind])
    else:
        vals.setdefault("label", kind)
        vals.setdefault("strength", 30.0)
        vals.setdefault("decay_scale", 1.0)
        vals.setdefault("detector", "rule")
        vals.setdefault("active", 1)
        vals.setdefault("sort_order", 200)
        vals["kind"] = kind
        vals["builtin"] = 0
        keys = list(vals)
        conn.execute(
            f"INSERT INTO signal_defs ({', '.join(keys)}) "
            f"VALUES ({', '.join('?' * len(keys))})", [vals[k] for k in keys])
    conn.commit()
    return get_signal_def(conn, kind)


def delete_signal_def(conn, kind):
    """Remove a user-defined kind. Builtins are never deletable — code writes events
    against those ids. Returns False when the kind is builtin or absent."""
    row = get_signal_def(conn, kind)
    if not row or row.get("builtin"):
        return False
    conn.execute("DELETE FROM signal_defs WHERE kind=?", (kind,))
    conn.commit()
    return True


# The proof library, seeded from ai-sdr/knowledge/offer.md § Proof. Anything added
# here has to be genuinely sourced — a reference is the one thing in the prompt the
# model is allowed to state as fact about another company.
CUSTOMER_REFERENCES = [
    {"ref_key": "memgraph-pipeline", "customer": "Memgraph", "nameable": 1, "kind": "proof",
     "anonymous": "a graph-database company", "industry": "Developer infrastructure",
     "story": "Came in signal-rich — reo.dev, 6sense and product telemetry were "
              "already surfacing more in-market accounts than the team could "
              "prospect into. The AI SDR was pointed at that full signal set and "
              "activated it automatically.",
     "metric": "$2.7M qualified pipeline, 600 replies and 60 BANT-qualified deals "
               "in 90 days; live in 4 weeks",
     "quote": "We had more in-market accounts than the team could touch, and hiring "
              "enough SDRs to cover them wasn't realistic.",
     "source": "Customer deck p12 + customer-confirmed signal-activation story",
     "url": None},
    {"ref_key": "memgraph-scale", "customer": "Memgraph", "nameable": 1, "kind": "proof",
     "anonymous": "a graph-database company", "industry": "Developer infrastructure",
     "story": "Ran 45,000 contacts across 500 target accounts with the existing "
              "team, scaling to 100,000 the following quarter.",
     "metric": "45,000 contacts across 500 accounts, same headcount",
     "quote": None, "source": "Customer deck p12", "url": None},
]

CTA_LIBRARY = [
    {"cta_key": "signal-play", "label": "Signal play", "tier": "A", "default_step": 1,
     "channels": ["email", "linkedin"],
     "give": "a personalized signal play built off their hiring and tech-stack signals",
     "ask": "15 minutes to walk you through it",
     "example": "I built a personalized signal play for {company} off your hiring and "
                "tech-stack signals — accounts showing them progress ~4.4x faster. "
                "Worth 15 minutes for me to walk you through it?"},
    {"cta_key": "pipeline-model", "label": "Pipeline gap analysis", "tier": "A", "default_step": 1,
     "channels": ["email", "linkedin"],
     "give": "our pipeline model, showing how many meetings they need to hit target",
     "ask": "grab 15 minutes",
     "example": "Want to grab 15 minutes? I'll walk you through our pipeline model and "
                "show exactly how many meetings {company} needs to hit target this quarter."},
    {"cta_key": "personalized-drafts", "label": "Personalized drafts", "tier": "A", "default_step": 1,
     "channels": ["email", "linkedin"],
     "give": "3 personalized emails our AI SDR drafted to their top 3 accounts",
     "ask": "hop on a quick call",
     "example": "I had our AI SDR draft 3 personalized emails to your top 3 accounts. "
                "Want to hop on a quick call and I'll walk you through them?"},
    {"cta_key": "run-rate", "label": "Run-rate + signal-set estimate", "tier": "A", "default_step": 2,
     "channels": ["email"],
     "give": "their current meeting run rate, the signal set they already generate, and "
             "how many additional meetings the AI SDR adds on top",
     "ask": "worth 15 minutes",
     "example": "Worth 15 minutes? I'll calculate {company}'s current meeting run rate, "
                "map the signal set you're already generating, and walk you through how "
                "many additional meetings the AI SDR would add on top."},
    {"cta_key": "signal-mapping", "label": "Signal-mapping session", "tier": "A", "default_step": 3,
     "channels": ["email"],
     "give": "a map of their signal sets and the highest-yield sources, paired with the "
             "Memgraph signal-activation proof",
     "ask": "grab 15 minutes",
     "example": "Want to grab 15 minutes? We'll map {company}'s signal sets, find the "
                "highest-yield sources, and I'll show you exactly where the AI SDR "
                "increases output."},
    {"cta_key": "outbound-teardown", "label": "Outbound teardown", "tier": "B", "default_step": None,
     "channels": ["email", "linkedin"],
     "give": "a teardown of their current outbound — 3 things we'd change",
     "ask": "worth 15 minutes",
     "example": "Worth 15 minutes? I'll walk you through a teardown of your current "
                "outbound — 3 things I'd change. Our best practices alone usually lift "
                "response rates 50-70%."},
    {"cta_key": "benchmark", "label": "Peer benchmark", "tier": "B", "default_step": None,
     "channels": ["linkedin", "reply"],
     "give": "how their reply rate compares to peer startups at the same stage",
     "ask": "grab time",
     "example": "Want to grab time so I can walk you through how {company}'s reply rate "
                "compares to other seed/Series-A startups?"},
    {"cta_key": "playbook", "label": "Pilot playbook (breakup)", "tier": "B", "default_step": 4,
     "channels": ["email"],
     "give": "a one-page playbook of 3 AI-SDR plays for their team/motion",
     "ask": "worth 15 minutes before I close your file",
     "example": "Before I close your file — worth 15 minutes to walk through a one-page "
                "playbook of 3 AI-SDR plays for your team?"},
]


# HubSpot original-source values that mean the contact came to US. Anything else
# (or an absent value) is treated as outbound: the conservative default is that we
# only claim inbound when HubSpot explicitly says so, never the reverse.
MOTION_INBOUND_SOURCES = {
    "ORGANIC_SEARCH", "PAID_SEARCH", "PAID_SOCIAL", "SOCIAL_MEDIA",
    "ORGANIC_SOCIAL", "EMAIL_MARKETING", "REFERRALS", "DIRECT_TRAFFIC",
    "OTHER_CAMPAIGNS",
}
# Lifecycle stages that mean the contact was already engaged before we touched them.
MOTION_INBOUND_STAGES = {"marketingqualifiedlead", "salesqualifiedlead",
                         "opportunity", "customer", "evangelist"}


def classify_motion(source, latest_source, lifecycle_stage):
    """'inbound' | 'outbound' — how this contact entered the funnel.

    Deliberately conservative: a contact is only inbound when HubSpot's own
    original-source (or an already-engaged lifecycle stage) says so. Unknown reads
    as outbound, because over-claiming inbound would quietly shrink the outbound
    numbers, and the reverse error is the one this whole distinction exists to stop.
    """
    src = (source or "").strip().upper()
    latest = (latest_source or "").strip().upper()
    stage = (lifecycle_stage or "").strip().lower()
    if src in MOTION_INBOUND_SOURCES or latest in MOTION_INBOUND_SOURCES:
        return "inbound"
    if stage in MOTION_INBOUND_STAGES:
        return "inbound"
    return "outbound"


def upsert_contacts(conn, rows):
    """Insert new contacts (ignore existing). Returns count of newly inserted."""
    before = conn.execute("SELECT COUNT(*) FROM contacts").fetchone()[0]
    conn.executemany("""
        INSERT OR IGNORE INTO contacts
          (contact_id, first_name, last_name, email, title, company, linkedin_url, persona, domain, variant,
           source, latest_source, lifecycle_stage, motion, phone, mobile_phone, status, updated_at)
        VALUES (:contact_id,:first_name,:last_name,:email,:title,:company,:linkedin_url,:persona,:domain,:variant,
                :source,:latest_source,:lifecycle_stage,:motion,:phone,:mobile_phone,'pending',:ts)
    """, [{"variant": None, "source": None, "latest_source": None,
           "lifecycle_stage": None, "motion": None, "phone": None, "mobile_phone": None,
           **r, "domain": email_domain(r.get("email")), "ts": now()} for r in rows])
    conn.commit()
    return conn.execute("SELECT COUNT(*) FROM contacts").fetchone()[0] - before


def assign_batches(conn, batch_size=25):
    """Group any unbatched contacts into batches of `batch_size`. Returns new batch count.

    Ordered by domain so contacts from the same company land in the same batch —
    one web search then covers all of them (and feeds the signal cache).
    """
    unbatched = [r["contact_id"] for r in
                 conn.execute("SELECT contact_id FROM contacts WHERE batch_id IS NULL "
                              "ORDER BY domain, rowid")]
    next_bid = (conn.execute("SELECT COALESCE(MAX(batch_id),0) FROM batches").fetchone()[0]) + 1
    made = 0
    for i in range(0, len(unbatched), batch_size):
        chunk = unbatched[i:i + batch_size]
        bid = next_bid + made
        conn.execute("INSERT INTO batches (batch_id, status, size) VALUES (?, 'pending', ?)", (bid, len(chunk)))
        conn.executemany("UPDATE contacts SET batch_id=? WHERE contact_id=?", [(bid, c) for c in chunk])
        made += 1
    conn.commit()
    return made


def get_batch(conn, batch_id):
    return [dict(r) for r in conn.execute(
        "SELECT contact_id, first_name, last_name, email, title, company, linkedin_url, persona, domain, variant "
        "FROM contacts WHERE batch_id=? ORDER BY domain, rowid", (batch_id,))]


def pending_batches(conn):
    return [r["batch_id"] for r in
            conn.execute("SELECT batch_id FROM batches WHERE status='pending' ORDER BY batch_id")]


def set_batch_status(conn, batch_id, status):
    col = "completed_at" if status == "done" else "claimed_at"
    conn.execute(f"UPDATE batches SET status=?, {col}=? WHERE batch_id=?", (status, now(), batch_id))
    conn.commit()


def set_contact_status(conn, contact_id, status, error=None):
    conn.execute("UPDATE contacts SET status=?, error=?, updated_at=? WHERE contact_id=?",
                 (status, error, now(), contact_id))
    conn.commit()


def contacts_by_status(conn, status):
    return [dict(r) for r in conn.execute(
        "SELECT * FROM contacts WHERE status=? ORDER BY batch_id, rowid", (status,))]


def counts(conn):
    cstat = {r["status"]: r["n"] for r in
             conn.execute("SELECT status, COUNT(*) n FROM contacts GROUP BY status")}
    bstat = {r["status"]: r["n"] for r in
             conn.execute("SELECT status, COUNT(*) n FROM batches GROUP BY status")}
    total = conn.execute("SELECT COUNT(*) FROM contacts").fetchone()[0]
    return {"total_contacts": total, "contacts_by_status": cstat, "batches_by_status": bstat}


# ---- per-company signal cache --------------------------------------------
def get_signal(conn, domain):
    if not domain:
        return None
    r = conn.execute("SELECT * FROM account_signals WHERE domain=?", (domain,)).fetchone()
    return dict(r) if r else None


def signal_age_days(row):
    """Whole days since the signal was researched, or None if unparseable."""
    if not row or not row.get("researched_at"):
        return None
    try:
        ts = datetime.strptime(row["researched_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None
    return (datetime.now(timezone.utc) - ts).days


def signal_fresh(row, days=90):
    age = signal_age_days(row)
    return age is not None and age < days


def upsert_signal(conn, domain, company_name, signal, has_recent, model=None):
    conn.execute("""
        INSERT INTO account_signals (domain, company_name, signal, has_recent, researched_at, model, updated_at)
        VALUES (?,?,?,?,?,?,?)
        ON CONFLICT(domain) DO UPDATE SET
          company_name=excluded.company_name, signal=excluded.signal, has_recent=excluded.has_recent,
          researched_at=excluded.researched_at, model=excluded.model, updated_at=excluded.updated_at
    """, (domain, company_name, signal, 1 if has_recent else 0, now(), model, now()))
    _insert_signal_event(conn, domain, "research", signal, has_recent=has_recent)
    conn.commit()


def all_signals(conn):
    return [dict(r) for r in conn.execute(
        "SELECT * FROM account_signals ORDER BY updated_at DESC")]


# ---- signal event log (the time dimension) ---------------------------------
# Values that mean "we looked and there was nothing" — a real scan result worth
# caching, but NOT a signal, so it never enters the event log and can never
# qualify an account into a campaign.
_NULL_SIGNALS = ("no signals detected", "no open roles detected")


def _signal_fingerprint(summary):
    """Stable key for a signal VALUE. Case- and whitespace-insensitive so a
    re-scan that reformats the same finding is not a new event."""
    norm = re.sub(r"\s+", " ", (summary or "").strip().lower())
    return hashlib.sha1(norm.encode("utf-8")).hexdigest()


def _is_signal(summary):
    """True if this scan result represents an actual signal."""
    s = (summary or "").strip().lower()
    if not s or s in _NULL_SIGNALS:
        return False
    # research falls back to a product/ICP anchor when nothing recent was found;
    # the prompt contract guarantees that prefix (see generate_batch.RESEARCH_BLOCK)
    return not s.startswith("no recent signal")


def _insert_signal_event(conn, domain, kind, summary, *, has_recent=None,
                         detail=None, observed_at=None):
    """Append one observation. No-op for empty/negative results, and idempotent on
    (domain, kind, value) — re-observing an unchanged signal does not move its
    observed_at, because the event is when the signal APPEARED, not when we last
    happened to look. Does not commit; the caller's upsert does."""
    if not domain or not _is_signal(summary):
        return False
    cur = conn.execute("""
        INSERT OR IGNORE INTO signal_events
          (domain, kind, summary, has_recent, detail, fingerprint, observed_at)
        VALUES (?,?,?,?,?,?,?)
    """, (domain, kind, summary,
          None if has_recent is None else (1 if has_recent else 0),
          detail, _signal_fingerprint(summary), observed_at or now()))
    return bool(cur.rowcount)


def record_signal_event(conn, domain, kind, summary, **kw):
    """Public wrapper around _insert_signal_event that commits."""
    added = _insert_signal_event(conn, domain, kind, summary, **kw)
    conn.commit()
    return added


def signal_events_in_window(conn, start=None, end=None, kinds=None):
    """Observations inside [start, end] (ISO strings; either bound may be None for
    open-ended), optionally restricted to a set of signal kinds. Newest first.

    This is the query account_signals cannot answer: which accounts SHOWED signal
    over a period, as opposed to which cached rows are stale."""
    where, params = ["1=1"], []
    if start:
        where.append("observed_at >= ?")
        params.append(start)
    if end:
        where.append("observed_at <= ?")
        params.append(end)
    if kinds:
        where.append("kind IN (%s)" % ",".join("?" * len(kinds)))
        params += list(kinds)
    return [dict(r) for r in conn.execute(
        "SELECT * FROM signal_events WHERE %s ORDER BY observed_at DESC"
        % " AND ".join(where), params)]


def signal_event_counts(conn, days=30):
    """{kind: n} for observations in the last `days` — the Signals/Campaign
    'what fired recently' summary."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=int(days))).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {r["kind"]: r["n"] for r in conn.execute(
        "SELECT kind, COUNT(*) n FROM signal_events WHERE observed_at >= ? GROUP BY kind",
        (cutoff,))}


# ---- per-company technographic scan (tech_signals.py) ----------------------
def tech_age_days(row):
    """Whole days since the tech scan ran, or None if it never has."""
    if not row or not row.get("tech_checked_at"):
        return None
    try:
        ts = datetime.strptime(row["tech_checked_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None
    return (datetime.now(timezone.utc) - ts).days


def tech_fresh(row, days=90):
    age = tech_age_days(row)
    return age is not None and age < days


def upsert_tech_signals(conn, domain, tech_signals, tech_detail=None, tech_error=None,
                        company_name=None):
    """Store one domain's technographic scan. Touches ONLY the tech_* columns (plus
    updated_at, so a fresh scan surfaces atop the Signals list) — the research signal
    fields and their freshness (researched_at) are never affected. company_name only
    fills a blank; an existing name wins."""
    conn.execute("""
        INSERT INTO account_signals
          (domain, company_name, tech_signals, tech_detail, tech_checked_at, tech_error, updated_at)
        VALUES (?,?,?,?,?,?,?)
        ON CONFLICT(domain) DO UPDATE SET
          company_name=COALESCE(NULLIF(account_signals.company_name,''), excluded.company_name),
          tech_signals=excluded.tech_signals, tech_detail=excluded.tech_detail,
          tech_checked_at=excluded.tech_checked_at, tech_error=excluded.tech_error,
          updated_at=excluded.updated_at
    """, (domain, company_name, tech_signals, tech_detail, now(), tech_error, now()))
    _insert_signal_event(conn, domain, "tech", tech_signals, detail=tech_detail)
    conn.commit()


def domains_missing_tech(conn, stale_days=None, limit=None):
    """Domains with no tech scan yet (plus, when stale_days is given, scans older than
    the cutoff). Newest research first, so backfills hit active accounts before
    dormant ones. Returns [{domain, company_name}, ...]."""
    where = "tech_checked_at IS NULL"
    params = []
    if stale_days is not None:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=int(stale_days))).strftime("%Y-%m-%dT%H:%M:%SZ")
        where += " OR tech_checked_at < ?"
        params.append(cutoff)
    sql = f"SELECT domain, company_name FROM account_signals WHERE {where} ORDER BY updated_at DESC"
    if limit:
        sql += " LIMIT ?"
        params.append(int(limit))
    return [dict(r) for r in conn.execute(sql, params)]


# ---- per-company hiring scan (hiring_signals.py) ---------------------------
def hiring_age_days(row):
    """Whole days since the hiring scan ran, or None if it never has."""
    if not row or not row.get("hiring_checked_at"):
        return None
    try:
        ts = datetime.strptime(row["hiring_checked_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None
    return (datetime.now(timezone.utc) - ts).days


def hiring_fresh(row, days=90):
    age = hiring_age_days(row)
    return age is not None and age < days


def upsert_hiring_signals(conn, domain, hiring_signals, hiring_detail=None, hiring_error=None,
                          company_name=None):
    """Store one domain's hiring scan. Touches ONLY the hiring_* columns (plus
    updated_at, so a fresh scan surfaces atop the Signals list) — the research signal
    and tech fields, and their freshness clocks, are never affected. company_name only
    fills a blank; an existing name wins."""
    conn.execute("""
        INSERT INTO account_signals
          (domain, company_name, hiring_signals, hiring_detail, hiring_checked_at, hiring_error, updated_at)
        VALUES (?,?,?,?,?,?,?)
        ON CONFLICT(domain) DO UPDATE SET
          company_name=COALESCE(NULLIF(account_signals.company_name,''), excluded.company_name),
          hiring_signals=excluded.hiring_signals, hiring_detail=excluded.hiring_detail,
          hiring_checked_at=excluded.hiring_checked_at, hiring_error=excluded.hiring_error,
          updated_at=excluded.updated_at
    """, (domain, company_name, hiring_signals, hiring_detail, now(), hiring_error, now()))
    _insert_signal_event(conn, domain, "hiring", hiring_signals, detail=hiring_detail)
    conn.commit()


def domains_missing_hiring(conn, stale_days=None, limit=None):
    """Domains with no hiring scan yet (plus, when stale_days is given, scans older
    than the cutoff). Newest research first, so backfills hit active accounts before
    dormant ones. Returns [{domain, company_name}, ...]."""
    where = "hiring_checked_at IS NULL"
    params = []
    if stale_days is not None:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=int(stale_days))).strftime("%Y-%m-%dT%H:%M:%SZ")
        where += " OR hiring_checked_at < ?"
        params.append(cutoff)
    sql = f"SELECT domain, company_name FROM account_signals WHERE {where} ORDER BY updated_at DESC"
    if limit:
        sql += " LIMIT ?"
        params.append(int(limit))
    return [dict(r) for r in conn.execute(sql, params)]


# ---- HubSpot activity ledger ---------------------------------------------
def activity_logged(conn, dedup_key):
    """True if this event was already logged successfully (skip on re-run)."""
    row = conn.execute(
        "SELECT 1 FROM hubspot_activity_log WHERE dedup_key=? AND status='logged'",
        (dedup_key,)).fetchone()
    return row is not None


def record_activity(conn, dedup_key, event_type, status, contact_id=None,
                    engagement_id=None, channel="email", event_ts=None, error=None):
    """Upsert a ledger row. A prior 'failed'/'skipped_no_contact' row is retried on
    the next run and flips to 'logged' once it succeeds; successes are never re-sent."""
    conn.execute("""
        INSERT INTO hubspot_activity_log
          (dedup_key, event_type, channel, contact_id, engagement_id, status, error, event_ts, created_at)
        VALUES (?,?,?,?,?,?,?,?,?)
        ON CONFLICT(dedup_key) DO UPDATE SET
          event_type=excluded.event_type, channel=excluded.channel, contact_id=excluded.contact_id,
          engagement_id=excluded.engagement_id, status=excluded.status, error=excluded.error,
          event_ts=excluded.event_ts, created_at=excluded.created_at
    """, (dedup_key, event_type, channel, contact_id, engagement_id, status, error, event_ts, now()))
    conn.commit()


def activity_counts(conn):
    """Summary of the ledger for status reporting."""
    by_status = {r["status"]: r["n"] for r in
                 conn.execute("SELECT status, COUNT(*) n FROM hubspot_activity_log GROUP BY status")}
    by_type = {r["event_type"]: r["n"] for r in
               conn.execute("SELECT event_type, COUNT(*) n FROM hubspot_activity_log "
                            "WHERE status='logged' GROUP BY event_type")}
    return {"by_status": by_status, "logged_by_type": by_type,
            "total": conn.execute("SELECT COUNT(*) FROM hubspot_activity_log").fetchone()[0]}


# ---- Bison email -> lead_id cache ----------------------------------------
def upsert_lead_map(conn, email, lead_id, contact_id=None):
    """Cache an email -> Bison lead_id mapping (from a /api/leads sweep)."""
    email = (email or "").strip().lower()
    if not email:
        return
    conn.execute("""
        INSERT INTO bison_lead_map (email, lead_id, contact_id, updated_at)
        VALUES (?,?,?,?)
        ON CONFLICT(email) DO UPDATE SET
          lead_id=excluded.lead_id,
          contact_id=COALESCE(excluded.contact_id, bison_lead_map.contact_id),
          updated_at=excluded.updated_at
    """, (email, int(lead_id) if lead_id is not None else None, contact_id, now()))


def mark_lead_fetched(conn, email, last_sent_at):
    """Record when a lead's sent-emails were last pulled + its newest send time."""
    conn.execute("UPDATE bison_lead_map SET last_fetch_at=?, last_sent_at=? WHERE email=?",
                 (now(), last_sent_at, (email or "").strip().lower()))
    conn.commit()


def lead_map_rows(conn):
    return [dict(r) for r in conn.execute("SELECT * FROM bison_lead_map")]


# ---- Unenrollment/suppression ledger --------------------------------------
def unenrollment_done(conn, dedup_key):
    """True if this (rule, channel, contact) was already handled successfully."""
    row = conn.execute(
        "SELECT 1 FROM unenrollment_log WHERE dedup_key=? AND status='done'",
        (dedup_key,)).fetchone()
    return row is not None


def record_unenrollment(conn, dedup_key, rule, channel, contact_id, action, status,
                        email=None, linkedin_url=None, campaign_ids=None, error=None):
    """Upsert a ledger row. A prior 'failed' row is retried on the next sweep and
    flips to 'done' once it succeeds; successes are never re-processed.
    campaign_ids: list of campaign ids stopped in (stored as JSON), or None."""
    cids = json.dumps(campaign_ids) if campaign_ids else None
    conn.execute("""
        INSERT INTO unenrollment_log
          (dedup_key, rule, contact_id, email, linkedin_url, channel, campaign_ids,
           action, status, error, created_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(dedup_key) DO UPDATE SET
          rule=excluded.rule, contact_id=excluded.contact_id, email=excluded.email,
          linkedin_url=excluded.linkedin_url, channel=excluded.channel,
          campaign_ids=excluded.campaign_ids, action=excluded.action,
          status=excluded.status, error=excluded.error, created_at=excluded.created_at
    """, (dedup_key, rule, contact_id, email, linkedin_url, channel, cids,
          action, status, error, now()))
    conn.commit()


def unenrollment_counts(conn, rule=None):
    """Summary of the unenrollment ledger for status reporting (per rule when given)."""
    where, params = ("WHERE rule=?", [rule]) if rule else ("", [])
    by_status = {r["status"]: r["n"] for r in conn.execute(
        f"SELECT status, COUNT(*) n FROM unenrollment_log {where} GROUP BY status", params)}
    by_channel_action = {}
    for r in conn.execute(
            f"SELECT channel, action, COUNT(*) n FROM unenrollment_log {where} "
            "GROUP BY channel, action", params):
        by_channel_action.setdefault(r["channel"], {})[r["action"]] = r["n"]
    contacts = conn.execute(
        f"SELECT COUNT(DISTINCT contact_id) FROM unenrollment_log {where}", params).fetchone()[0]
    last = conn.execute(
        f"SELECT MAX(created_at) FROM unenrollment_log {where}", params).fetchone()[0]
    return {"by_status": by_status, "by_channel_action": by_channel_action,
            "contacts": contacts, "failed": by_status.get("failed", 0),
            "last_action_at": last}


def suppressed_contact_ids(conn, rule=None):
    """Contact ids the pipeline must not work.

    Two sources, unioned, because they mean the same thing to a sender even though
    they arrive differently:
      * the unenrollment ledger — RevOps flagged them in the CRM and a sweep stopped
        them (the enroll gate's offline fallback when the live tag check is down)
      * `contacts.engagement_state` — somebody hit "do not contact" in the console,
        or paused them and the pause has not expired yet

    A pause that has RUN OUT is not suppression: the whole point of a pause is that
    it lifts on its own, so it is filtered by date here rather than needing a job to
    un-set it."""
    where, params = ("AND rule=?", [rule]) if rule else ("", [])
    out = {r["contact_id"] for r in conn.execute(
        f"SELECT DISTINCT contact_id FROM unenrollment_log WHERE status='done' {where}",
        params)}
    try:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        out |= {r["contact_id"] for r in conn.execute(
            "SELECT contact_id FROM contacts WHERE engagement_state='suppressed' "
            "   OR (engagement_state='paused' "
            "       AND (paused_until IS NULL OR paused_until >= ?))", (today,))}
    except sqlite3.Error:
        pass   # older DB without the columns — the ledger alone still gates
    return out


def set_engagement(conn, contact_id, state, paused_until=None, note=None):
    """Set a contact's PERSON-level engagement. Applies across every campaign.

    Clearing back to 'active' also clears the pause date, so resuming someone can
    never leave a stale expiry that re-pauses them later."""
    state = (state or "active").strip().lower()
    if state not in ("active", "paused", "suppressed"):
        raise ValueError("engagement_state must be active, paused or suppressed")
    if state != "paused":
        paused_until = None
    conn.execute(
        "UPDATE contacts SET engagement_state=?, paused_until=?, "
        "  engagement_note=COALESCE(?, engagement_note), engagement_updated_at=? "
        "WHERE contact_id=?",
        (state, paused_until, note, now(), str(contact_id)))
    conn.commit()
    r = conn.execute(
        "SELECT contact_id, engagement_state, paused_until, engagement_note, "
        "       engagement_updated_at FROM contacts WHERE contact_id=?",
        (str(contact_id),)).fetchone()
    return dict(r) if r else None


def update_member(conn, campaign_id, contact_id, **fields):
    """Patch one membership row — the CAMPAIGN-level half of working the list.

    Only the keys passed are written, and only from a fixed set: this is reached
    from an API, and a patch that could set `priority_score` or `state` to anything
    would let the UI rewrite the scorer's output."""
    allowed = ("state", "manual_priority", "snoozed_until", "worked_at",
               "outcome", "note")
    sets, params = [], []
    for k in allowed:
        if k in fields:
            sets.append(f"{k}=?")
            params.append(fields[k])
    if not sets:
        return get_member(conn, campaign_id, contact_id)
    sets.append("updated_at=?")
    params += [now(), campaign_id, str(contact_id)]
    conn.execute(f"UPDATE campaign_members SET {', '.join(sets)} "
                 "WHERE campaign_id=? AND contact_id=?", params)
    conn.commit()
    return get_member(conn, campaign_id, contact_id)


def get_member(conn, campaign_id, contact_id):
    r = conn.execute("SELECT * FROM campaign_members WHERE campaign_id=? AND contact_id=?",
                     (campaign_id, str(contact_id))).fetchone()
    return dict(r) if r else None


# ---- HeyReach (LinkedIn) webhook inbox -----------------------------------
def enqueue_heyreach_event(conn, raw, *, event_id=None, event_type=None, dedup_key=None):
    """Persist one raw HeyReach webhook payload. Idempotent on dedup_key (a HeyReach
    retry of an already-stored event is ignored). Returns the new row id, or None if
    the event was a duplicate. Commits. Never raises on a dupe — INSERT OR IGNORE."""
    cur = conn.execute("""
        INSERT OR IGNORE INTO heyreach_events
          (event_id, event_type, dedup_key, status, attempts, raw, received_at)
        VALUES (?,?,?,'pending',0,?,?)
    """, (event_id, event_type, dedup_key, raw, now()))
    conn.commit()
    return cur.lastrowid if cur.rowcount else None


def pending_heyreach_events(conn, limit=200, max_attempts=25):
    """Unprocessed inbox rows (pending or previously failed), oldest first. A poison
    row that keeps failing is dropped from the queue once it exceeds max_attempts so
    it can't be retried forever every tick (it stays in the table for inspection)."""
    return [dict(r) for r in conn.execute(
        "SELECT * FROM heyreach_events WHERE status IN ('pending','failed') "
        "AND attempts < ? ORDER BY id LIMIT ?", (int(max_attempts), int(limit)))]


def retry_skipped_heyreach_events(conn):
    """Flip previously 'skipped' rows (e.g. no matching contact at the time) back to
    'pending' so a later drain re-attempts them after contacts have been added.
    Returns the number requeued."""
    cur = conn.execute(
        "UPDATE heyreach_events SET status='pending', attempts=0 WHERE status='skipped'")
    conn.commit()
    return cur.rowcount


def mark_heyreach_event(conn, row_id, status, *, error=None):
    """Set the processing outcome for one inbox row (bumps attempts, stamps time)."""
    conn.execute(
        "UPDATE heyreach_events SET status=?, error=?, attempts=attempts+1, processed_at=? "
        "WHERE id=?", (status, error, now(), row_id))
    conn.commit()


def heyreach_event_counts(conn):
    """Inbox summary by status, for status reporting."""
    return {r["status"]: r["n"] for r in
            conn.execute("SELECT status, COUNT(*) n FROM heyreach_events GROUP BY status")}


# ---- campaigns -------------------------------------------------------------
CAMPAIGN_STATUSES = ("draft", "active", "paused", "completed", "archived")
MEMBER_STATES = ("qualified", "generated", "enrolled", "replied", "removed")


def slugify(name, fallback="campaign"):
    s = re.sub(r"[^a-z0-9]+", "-", (name or "").strip().lower()).strip("-")
    return (s or fallback)[:60]


def list_ctas(conn, active_only=True):
    """The offer library, each row carrying its customer reference if it has one."""
    where = "WHERE active=1" if active_only else ""
    refs = {r["ref_key"]: r for r in customer_references(conn)}
    attached = cta_content_map(conn)
    out = []
    for r in conn.execute(f"SELECT * FROM campaign_ctas {where} "
                          "ORDER BY tier, COALESCE(default_step, 99), cta_key"):
        row = dict(r)
        try:
            row["channels"] = json.loads(row["channels"] or "[]")
        except json.JSONDecodeError:
            row["channels"] = []
        row["content"] = [refs[k] for k in attached.get(row["cta_key"], []) if k in refs]
        out.append(row)
    return out


def customer_references(conn, active_only=True):
    """The proof library. Never raises on an older DB without the table."""
    try:
        where = "WHERE active=1" if active_only else ""
        return [dict(r) for r in conn.execute(
            f"SELECT * FROM customer_references {where} ORDER BY customer, ref_key")]
    except sqlite3.Error:
        return []


def upsert_customer_reference(conn, ref_key, **fields):
    cols = ("customer", "nameable", "anonymous", "industry", "story", "metric",
            "quote", "source", "url", "kind", "active")
    existing = conn.execute("SELECT 1 FROM customer_references WHERE ref_key=?",
                            (ref_key,)).fetchone()
    vals = {k: fields[k] for k in cols if k in fields}
    vals["updated_at"] = now()
    if existing:
        sets = ", ".join(f"{k}=?" for k in vals)
        conn.execute(f"UPDATE customer_references SET {sets} WHERE ref_key=?",
                     list(vals.values()) + [ref_key])
    else:
        vals.setdefault("customer", ref_key)
        vals.setdefault("story", "")
        vals.setdefault("nameable", 0)
        vals.setdefault("active", 1)
        vals["ref_key"] = ref_key
        vals["builtin"] = 0
        keys = list(vals)
        conn.execute(f"INSERT INTO customer_references ({', '.join(keys)}) "
                     f"VALUES ({', '.join('?' * len(keys))})", [vals[k] for k in keys])
    conn.commit()
    r = conn.execute("SELECT * FROM customer_references WHERE ref_key=?",
                     (ref_key,)).fetchone()
    return dict(r) if r else None


def cta_content_map(conn):
    """{cta_key: [ref_key, ...]} in display order. Never raises on an older DB."""
    out = {}
    try:
        for r in conn.execute("SELECT cta_key, ref_key FROM cta_content "
                              "ORDER BY cta_key, sort_order, ref_key"):
            out.setdefault(r["cta_key"], []).append(r["ref_key"])
    except sqlite3.Error:
        pass
    return out


def attach_cta_content(conn, cta_key, ref_key):
    conn.execute("INSERT OR IGNORE INTO cta_content (cta_key, ref_key, sort_order, "
                 "added_at) VALUES (?,?,?,?)", (cta_key, ref_key, 100, now()))
    conn.commit()
    return next((c for c in list_ctas(conn, active_only=False)
                 if c["cta_key"] == cta_key), None)


def detach_cta_content(conn, cta_key, ref_key):
    conn.execute("DELETE FROM cta_content WHERE cta_key=? AND ref_key=?",
                 (cta_key, ref_key))
    conn.commit()
    return next((c for c in list_ctas(conn, active_only=False)
                 if c["cta_key"] == cta_key), None)


def _campaign_row(r):
    row = dict(r)
    for k, default in (("signal_query", {}), ("audience", None), ("channels", None)):
        try:
            row[k] = json.loads(row.get(k) or "null")
        except (json.JSONDecodeError, TypeError):
            row[k] = None
        if row[k] is None:
            row[k] = default
    return row


def get_campaign(conn, campaign_id):
    r = conn.execute("SELECT * FROM campaigns WHERE campaign_id=?", (campaign_id,)).fetchone()
    return _campaign_row(r) if r else None


def get_campaign_by_key(conn, key):
    r = conn.execute("SELECT * FROM campaigns WHERE key=?", (key,)).fetchone()
    return _campaign_row(r) if r else None


def list_campaigns(conn, status=None):
    where, params = ("WHERE status=?", [status]) if status else ("", [])
    return [_campaign_row(r) for r in conn.execute(
        f"SELECT * FROM campaigns {where} ORDER BY "
        # active work first, then drafts, then everything finished
        "CASE status WHEN 'active' THEN 0 WHEN 'paused' THEN 1 WHEN 'draft' THEN 2 "
        "ELSE 3 END, COALESCE(launched_at, created_at) DESC", params)]


CAMPAIGN_FIELDS = ("name", "description", "brief", "campaign_type", "status", "window_start",
                   "window_end", "membership_mode", "variant", "bison_campaign_id",
                   "heyreach_campaign_id", "target_accounts",
                   "discovery_interval_days", "evergreen",
                   "evergreen_interval_days", "review_due_at", "review_state",
                   "cycle", "relaunched_at")
# JSON-encoded columns, patched separately so a dict round-trips correctly.
CAMPAIGN_JSON_FIELDS = ("signal_query", "audience", "channels")


def create_campaign(conn, name, **fields):
    """Insert a campaign and return its full row. `signal_query` may be passed as a
    dict; the key is derived from the name and de-duplicated with a numeric suffix."""
    base = slugify(name)
    key, n = base, 2
    while conn.execute("SELECT 1 FROM campaigns WHERE key=?", (key,)).fetchone():
        key, n = f"{base}-{n}", n + 1
    vals = {k: fields.get(k) for k in CAMPAIGN_FIELDS}
    vals["name"] = name
    vals["status"] = vals["status"] or "draft"
    vals["membership_mode"] = vals["membership_mode"] or "rolling"
    sq = fields.get("signal_query")
    aud = fields.get("audience")
    cur = conn.execute("""
        INSERT INTO campaigns
          (key, name, description, brief, campaign_type, status, window_start, window_end,
           signal_query,
           audience, channels, membership_mode, variant, bison_campaign_id,
           heyreach_campaign_id,
           target_accounts, discovery_interval_days, evergreen,
           evergreen_interval_days, review_due_at, cycle, created_at, updated_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (key, vals["name"], vals["description"], vals["brief"],
          vals["campaign_type"] or "outbound", vals["status"],
          vals["window_start"],
          vals["window_end"], json.dumps(sq) if sq is not None else None,
          json.dumps(aud) if aud is not None else None,
          json.dumps(fields.get("channels")) if fields.get("channels") else None,
          vals["membership_mode"], vals["variant"], vals["bison_campaign_id"],
          vals["heyreach_campaign_id"], vals["target_accounts"],
          vals["discovery_interval_days"] if vals["discovery_interval_days"] is not None else 7,
          1 if vals["evergreen"] else 0, vals["evergreen_interval_days"],
          vals["review_due_at"], 1,
          now(), now()))
    conn.commit()
    return get_campaign(conn, cur.lastrowid)


def update_campaign(conn, campaign_id, **fields):
    """Patch the given fields only — an absent key is left untouched (so a partial
    PATCH from the UI can never blank a column it didn't send)."""
    sets, params = [], []
    for k in CAMPAIGN_FIELDS:
        if k in fields:
            sets.append(f"{k}=?")
            params.append(fields[k])
    for jk in CAMPAIGN_JSON_FIELDS:
        if jk in fields:
            v = fields[jk]
            sets.append(f"{jk}=?")
            params.append(json.dumps(v) if v is not None else None)
    for k in ("launched_at", "completed_at", "last_qualified_at", "last_discovery_at"):
        if k in fields:
            sets.append(f"{k}=?")
            params.append(fields[k])
    if not sets:
        return get_campaign(conn, campaign_id)
    sets.append("updated_at=?")
    params += [now(), campaign_id]
    conn.execute(f"UPDATE campaigns SET {', '.join(sets)} WHERE campaign_id=?", params)
    conn.commit()
    return get_campaign(conn, campaign_id)


def delete_campaign(conn, campaign_id):
    conn.execute("DELETE FROM campaign_members WHERE campaign_id=?", (campaign_id,))
    conn.execute("DELETE FROM campaign_steps WHERE campaign_id=?", (campaign_id,))
    cur = conn.execute("DELETE FROM campaigns WHERE campaign_id=?", (campaign_id,))
    conn.commit()
    return cur.rowcount > 0


# ---- campaign steps --------------------------------------------------------
def get_steps(conn, campaign_id):
    return [dict(r) for r in conn.execute(
        "SELECT * FROM campaign_steps WHERE campaign_id=? "
        "ORDER BY CASE channel WHEN 'email' THEN 0 ELSE 1 END, step_no", (campaign_id,))]


def upsert_step(conn, campaign_id, step_no, channel="email", **fields):
    """Create or patch one step. Absent fields are left untouched on an existing row."""
    existing = conn.execute(
        "SELECT * FROM campaign_steps WHERE campaign_id=? AND step_no=? AND channel=?",
        (campaign_id, step_no, channel)).fetchone()
    cols = ("day_offset", "cta_key", "angle", "copy_mode", "subject", "body")
    if existing is None:
        vals = {c: fields.get(c) for c in cols}
        vals["copy_mode"] = vals["copy_mode"] or "generated"
        conn.execute("""
            INSERT INTO campaign_steps
              (campaign_id, step_no, channel, day_offset, cta_key, angle, copy_mode,
               subject, body, updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?)
        """, (campaign_id, step_no, channel, vals["day_offset"], vals["cta_key"],
              vals["angle"], vals["copy_mode"], vals["subject"], vals["body"], now()))
    else:
        sets = [f"{c}=?" for c in cols if c in fields]
        if not sets:
            return dict(existing)
        params = [fields[c] for c in cols if c in fields]
        conn.execute(
            f"UPDATE campaign_steps SET {', '.join(sets)}, updated_at=? "
            "WHERE campaign_id=? AND step_no=? AND channel=?",
            params + [now(), campaign_id, step_no, channel])
    conn.commit()
    return dict(conn.execute(
        "SELECT * FROM campaign_steps WHERE campaign_id=? AND step_no=? AND channel=?",
        (campaign_id, step_no, channel)).fetchone())


def delete_step(conn, campaign_id, step_no, channel="email"):
    cur = conn.execute(
        "DELETE FROM campaign_steps WHERE campaign_id=? AND step_no=? AND channel=?",
        (campaign_id, step_no, channel))
    conn.commit()
    return cur.rowcount > 0


# The 4-touch email cadence + 3-touch LinkedIn track this pipeline actually runs,
# with each step pre-bound to the CTA that cta-offers.md names as its default. A new
# campaign starts here so the step->CTA link is populated rather than empty; every
# field stays editable.
DEFAULT_SEQUENCE = [
    {"step_no": 1, "channel": "email", "day_offset": 0, "cta_key": "signal-play",
     "angle": "Personalized opener on the researched account signal, plus the strongest "
              "Tier-A give."},
    {"step_no": 2, "channel": "email", "day_offset": 3, "cta_key": "run-rate",
     "angle": "A new angle, not a nag. Open on the hiring signal when one is present "
              "(role count + 1-2 sales roles, tied to covering pipeline while new reps "
              "ramp); add the no-disruption point when a sequencing tool is detected."},
    {"step_no": 3, "channel": "email", "day_offset": 7, "cta_key": "signal-mapping",
     "angle": "The Memgraph signal-activation proof. Name one detected intent/ABM tool "
              "when flagged; reference ad investment generically when only pixels are."},
    {"step_no": 4, "channel": "email", "day_offset": 12, "cta_key": "playbook",
     "angle": "Breakup + soft give. Should I close your file?"},
    {"step_no": 1, "channel": "linkedin", "day_offset": 1, "cta_key": None,
     "angle": "Connection request. No pitch, no CTA — context only."},
    {"step_no": 2, "channel": "linkedin", "day_offset": 4, "cta_key": "signal-play",
     "angle": "First message after connecting: the signal play give."},
    {"step_no": 3, "channel": "linkedin", "day_offset": 9, "cta_key": "benchmark",
     "angle": "Follow-up: the peer benchmark give (LinkedIn-only offer)."},
]


def seed_default_sequence(conn, campaign_id):
    """Populate a new campaign with the standard cadence. No-op if steps exist."""
    if conn.execute("SELECT 1 FROM campaign_steps WHERE campaign_id=? LIMIT 1",
                    (campaign_id,)).fetchone():
        return 0
    conn.executemany("""
        INSERT OR IGNORE INTO campaign_steps
          (campaign_id, step_no, channel, day_offset, cta_key, angle, copy_mode, updated_at)
        VALUES (:cid,:step_no,:channel,:day_offset,:cta_key,:angle,'generated',:ts)
    """, [{**s, "cid": campaign_id, "ts": now()} for s in DEFAULT_SEQUENCE])
    conn.commit()
    return len(DEFAULT_SEQUENCE)


# ---- campaign members ------------------------------------------------------
def add_members(conn, campaign_id, rows):
    """Insert qualified contacts. Existing members are left untouched (their original
    qualified_at and signal_snapshot are the audit trail). Returns count added.

    rows: [{contact_id, domain, signal_kind, signal_snapshot(dict|None),
            priority_score, score_band, score_detail(dict|None)}, ...]"""
    before = conn.execute("SELECT COUNT(*) FROM campaign_members WHERE campaign_id=?",
                          (campaign_id,)).fetchone()[0]
    conn.executemany("""
        INSERT OR IGNORE INTO campaign_members
          (campaign_id, contact_id, domain, state, signal_kind, signal_snapshot,
           priority_score, score_band, score_detail, scored_at, channels, buyer_role,
           origin, origin_detail, previous_score, momentum, rank_score,
           qualified_at, updated_at)
        VALUES (:cid,:contact_id,:domain,'qualified',:signal_kind,:snap,
                :score,:band,:detail,:ts,:channels,:role,:origin,:odetail,
                :prev,:mom,:rank,:ts,:ts)
    """, [{"cid": campaign_id, "contact_id": r["contact_id"], "domain": r.get("domain"),
           "signal_kind": r.get("signal_kind"),
           "snap": json.dumps(r["signal_snapshot"]) if r.get("signal_snapshot") else None,
           "score": r.get("priority_score"), "band": r.get("score_band"),
           "detail": json.dumps(r["score_detail"]) if r.get("score_detail") else None,
           "channels": json.dumps(r["channels"]) if r.get("channels") else None,
           "role": r.get("buyer_role"), "origin": r.get("origin") or "existing",
           "odetail": json.dumps(r["origin_detail"]) if r.get("origin_detail") else None,
           "prev": r.get("previous_score"), "mom": r.get("momentum"),
           "rank": r.get("rank_score", r.get("priority_score")),
           "ts": now()} for r in rows])
    conn.commit()
    after = conn.execute("SELECT COUNT(*) FROM campaign_members WHERE campaign_id=?",
                         (campaign_id,)).fetchone()[0]
    return after - before


def set_member_state(conn, campaign_id, contact_id, state, **fields):
    sets, params = ["state=?"], [state]
    if state == "enrolled" and "enrolled_at" not in fields:
        fields["enrolled_at"] = now()
    for k in ("enrolled_at", "bison_lead_id"):
        if k in fields:
            sets.append(f"{k}=?")
            params.append(fields[k])
    params += [now(), campaign_id, contact_id]
    conn.execute(f"UPDATE campaign_members SET {', '.join(sets)}, updated_at=? "
                 "WHERE campaign_id=? AND contact_id=?", params)
    conn.commit()


def campaign_members(conn, campaign_id=None, state=None, limit=None,
                     order="priority", min_score=None, hide_snoozed=False):
    """Members joined with the contact record.

    Defaults to PRIORITY order — the campaign detail table and the SDR call list are
    the same query, and strongest-signal-first is the useful default for both. Pass
    campaign_id=None for a cross-campaign call list. NULL scores sort last rather
    than first, so members qualified before scoring existed don't head the list.

    Priority order is ACCOUNT-DIVERSE: every account's best contact comes before any
    account's second contact. Signal is an account-level property, so a strict score
    sort puts all 49 buyers at one funded company above every other account — a list
    nobody can work. Round-robin by account keeps the same score ranking while
    spreading the top of the list across companies. Pass order='score' for the raw
    ranking, or 'recent' for newest-qualified first.
    """
    where, params = ["1=1"], []
    if campaign_id is not None:
        where.append("m.campaign_id=?")
        params.append(campaign_id)
    if state:
        where.append("m.state=?")
        params.append(state)
    if min_score is not None:
        where.append("m.priority_score >= ?")
        params.append(float(min_score))
    if hide_snoozed:
        # A snoozed member is still a member — they are hidden from the working
        # list until the date, not removed from the campaign.
        where.append("(m.snoozed_until IS NULL OR m.snoozed_until <= ?)")
        params.append(datetime.now(timezone.utc).strftime("%Y-%m-%d"))
    select = ("SELECT m.*, c.first_name, c.last_name, c.email, c.title, c.company, "
              "c.persona, c.motion, c.linkedin_url, c.phone, c.mobile_phone, c.status AS contact_status, "
              # Person-level engagement travels with the row: a rep must be able to
              # see "this person is paused everywhere" without opening them.
              "c.engagement_state, c.paused_until, c.engagement_note, "
              "cam.name AS campaign_name, cam.key AS campaign_key")
    frm = ("FROM campaign_members m LEFT JOIN contacts c USING (contact_id) "
           "LEFT JOIN campaigns cam USING (campaign_id) "
           f"WHERE {' AND '.join(where)}")
    # rank_score = priority_score + a bounded momentum adjustment. Fall back to
    # priority_score for rows scored before momentum existed.
    # A hand-set priority wins over the computed one. The scorer's number is kept
    # (priority_score is untouched) so the override is visible as an override rather
    # than silently becoming the score.
    rank = "COALESCE(m.manual_priority, m.rank_score, m.priority_score)"
    if order == "priority":
        # rank within account, then interleave: rank 1 of every account, rank 2, …
        sql = (f"{select}, ROW_NUMBER() OVER (PARTITION BY m.domain "
               f"ORDER BY {rank} IS NULL, {rank} DESC, m.contact_id) "
               f"AS account_rank {frm} "
               f"ORDER BY account_rank, {rank} IS NULL, {rank} DESC, m.qualified_at DESC")
    else:
        order_sql = (f"{rank} IS NULL, {rank} DESC, m.qualified_at DESC"
                     if order == "score" else "m.qualified_at DESC")
        sql = f"{select} {frm} ORDER BY {order_sql}"
    if limit:
        sql += " LIMIT ?"
        params.append(int(limit))
    out = []
    for r in conn.execute(sql, params):
        row = dict(r)
        for k in ("signal_snapshot", "score_detail", "channels", "origin_detail"):
            try:
                row[k] = json.loads(row[k] or "null")
            except (json.JSONDecodeError, TypeError):
                row[k] = None
        out.append(row)
    # Attach every campaign each person belongs to, so an overlap is visible on the
    # row rather than only inside whichever campaign you happened to open.
    if out:
        tags = contact_campaign_tags(conn, {r["contact_id"] for r in out})
        for row in out:
            all_c = tags.get(row["contact_id"], [])
            row["all_campaigns"] = all_c
            row["overlapping"] = len(all_c) > 1
    return out


def set_member_score(conn, campaign_id, contact_id, score, band, detail,
                     previous_score=None, momentum=None, rank_score=None,
                     channels=None, buyer_role=None):
    conn.execute("UPDATE campaign_members SET priority_score=?, score_band=?, "
                 "score_detail=?, scored_at=?, previous_score=?, momentum=?, "
                 "rank_score=?, channels=COALESCE(?, channels), "
                 "buyer_role=COALESCE(?, buyer_role), updated_at=? "
                 "WHERE campaign_id=? AND contact_id=?",
                 (score, band, json.dumps(detail) if detail else None, now(),
                  previous_score, momentum,
                  rank_score if rank_score is not None else score,
                  json.dumps(channels) if channels else None, buyer_role, now(),
                  campaign_id, contact_id))


def previous_score(conn, contact_id, exclude_campaign_id=None):
    """This contact's most recent prior score, or None if never scored.

    Looks across ALL campaigns: the point of momentum is that being worked before —
    in whatever campaign — establishes a baseline, and moving off it is news."""
    where, params = ["contact_id=?", "priority_score IS NOT NULL"], [contact_id]
    if exclude_campaign_id is not None:
        where.append("campaign_id != ?")
        params.append(exclude_campaign_id)
    r = conn.execute(
        f"SELECT priority_score, scored_at, campaign_id FROM campaign_members "
        f"WHERE {' AND '.join(where)} ORDER BY scored_at DESC LIMIT 1", params).fetchone()
    return dict(r) if r else None


def campaign_counts(conn, campaign_id):
    """{by_state, members, accounts} for one campaign."""
    by_state = {r["state"]: r["n"] for r in conn.execute(
        "SELECT state, COUNT(*) n FROM campaign_members WHERE campaign_id=? GROUP BY state",
        (campaign_id,))}
    total = sum(by_state.values())
    accounts = conn.execute(
        "SELECT COUNT(DISTINCT domain) FROM campaign_members WHERE campaign_id=?",
        (campaign_id,)).fetchone()[0]
    by_band = {r["score_band"]: r["n"] for r in conn.execute(
        "SELECT score_band, COUNT(*) n FROM campaign_members WHERE campaign_id=? "
        "AND score_band IS NOT NULL GROUP BY score_band", (campaign_id,))}
    avg = conn.execute("SELECT AVG(priority_score) FROM campaign_members "
                       "WHERE campaign_id=?", (campaign_id,)).fetchone()[0]
    return {"by_state": by_state, "members": total, "accounts": accounts,
            "by_band": by_band, "avg_score": round(avg, 1) if avg is not None else None}


# ---- usage ledger (credits + sending capacity) -----------------------------
def record_usage(conn, provider, operation, units=1, unit_kind="credits",
                 campaign_id=None, ref=None, detail=None, occurred_at=None):
    """Append one metered event. Never raises into the caller's path — a failure to
    RECORD spend must not fail the work that spent it, or a ledger bug becomes an
    enrichment outage."""
    try:
        conn.execute("""
            INSERT INTO usage_ledger
              (provider, operation, units, unit_kind, campaign_id, ref, detail, occurred_at)
            VALUES (?,?,?,?,?,?,?,?)
        """, (provider, operation, float(units), unit_kind, campaign_id, ref,
              json.dumps(detail) if detail else None, occurred_at or now()))
        conn.commit()
        return True
    except sqlite3.Error:
        return False


def usage_totals(conn, since=None, campaign_id=None):
    """{(provider, unit_kind): units} plus a per-operation breakdown."""
    where, params = ["1=1"], []
    if since:
        where.append("occurred_at >= ?")
        params.append(since)
    if campaign_id is not None:
        where.append("campaign_id = ?")
        params.append(campaign_id)
    w = " AND ".join(where)
    by_provider = [dict(r) for r in conn.execute(
        f"SELECT provider, unit_kind, SUM(units) units, COUNT(*) events "
        f"FROM usage_ledger WHERE {w} GROUP BY provider, unit_kind "
        "ORDER BY units DESC", params)]
    by_operation = [dict(r) for r in conn.execute(
        f"SELECT provider, operation, unit_kind, SUM(units) units, COUNT(*) events "
        f"FROM usage_ledger WHERE {w} GROUP BY provider, operation, unit_kind "
        "ORDER BY units DESC", params)]
    return {"by_provider": by_provider, "by_operation": by_operation}


def usage_sum(conn, provider=None, unit_kind=None, since=None, operation=None):
    """Total units matching the filters — the number a capacity check compares."""
    where, params = ["1=1"], []
    for col, val in (("provider", provider), ("unit_kind", unit_kind),
                     ("operation", operation)):
        if val:
            where.append(f"{col} = ?")
            params.append(val)
    if since:
        where.append("occurred_at >= ?")
        params.append(since)
    row = conn.execute(f"SELECT COALESCE(SUM(units),0) FROM usage_ledger "
                       f"WHERE {' AND '.join(where)}", params).fetchone()
    return float(row[0] or 0)


def usage_recent(conn, limit=50):
    return [dict(r) for r in conn.execute(
        "SELECT * FROM usage_ledger ORDER BY occurred_at DESC, id DESC LIMIT ?",
        (int(limit),))]


# ---- CRM field map ---------------------------------------------------------
def crm_fields(conn, enabled_only=False, object_type=None, direction=None):
    where, params = ["1=1"], []
    if enabled_only:
        where.append("enabled=1")
    if object_type:
        where.append("object_type=?")
        params.append(object_type)
    if direction:
        # 'both' satisfies a request for either push or pull
        where.append("(direction=? OR direction='both')")
        params.append(direction)
    return [dict(r) for r in conn.execute(
        f"SELECT * FROM crm_field_map WHERE {' AND '.join(where)} "
        "ORDER BY object_type, local_key", params)]


def update_crm_field(conn, local_key, **fields):
    cols = ("object_type", "property_name", "label", "field_type", "direction",
            "enabled", "auto_create", "last_push_at", "last_pull_at", "last_error")
    sets = [f"{c}=?" for c in cols if c in fields]
    if not sets:
        return None
    params = [fields[c] for c in cols if c in fields]
    conn.execute(f"UPDATE crm_field_map SET {', '.join(sets)}, updated_at=? "
                 "WHERE local_key=?", params + [now(), local_key])
    conn.commit()
    r = conn.execute("SELECT * FROM crm_field_map WHERE local_key=?", (local_key,)).fetchone()
    return dict(r) if r else None


def bump_crm_field(conn, local_key, *, pushed=0, pulled=0, error=None):
    """Record the outcome of a sync pass for one field."""
    sets, params = [], []
    if pushed:
        sets.append("pushed=pushed+?")
        params.append(int(pushed))
        sets.append("last_push_at=?")
        params.append(now())
    if pulled:
        sets.append("pulled=pulled+?")
        params.append(int(pulled))
        sets.append("last_pull_at=?")
        params.append(now())
    sets.append("last_error=?")
    params.append(error)
    conn.execute(f"UPDATE crm_field_map SET {', '.join(sets)}, updated_at=? "
                 "WHERE local_key=?", params + [now(), local_key])
    conn.commit()


def contact_campaign_tags(conn, contact_ids=None, live_only=True):
    """{contact_id: [{campaign_id, name, key, state, score, band}]} — every campaign
    a person belongs to.

    Campaign membership lives on the PERSON, not just inside the campaign that
    happens to be open: a contact in three campaigns needs to show all three
    wherever they appear, or the same prospect looks unrelated on each screen and
    nobody notices they are being worked three times.
    """
    where = ["1=1"]
    params = []
    if live_only:
        where.append("c.status IN ('active','paused','draft')")
    where.append("m.state != 'removed'")
    if contact_ids:
        ids = list(contact_ids)
        where.append("m.contact_id IN (%s)" % ",".join("?" * len(ids)))
        params += [str(i) for i in ids]
    out = {}
    for r in conn.execute(
            f"SELECT m.contact_id, m.campaign_id, m.state, m.priority_score, "
            f"m.score_band, c.name, c.key, c.status "
            f"FROM campaign_members m JOIN campaigns c USING (campaign_id) "
            f"WHERE {' AND '.join(where)} "
            "ORDER BY m.contact_id, COALESCE(m.rank_score, m.priority_score) DESC", params):
        out.setdefault(r["contact_id"], []).append({
            "campaign_id": r["campaign_id"], "name": r["name"], "key": r["key"],
            "status": r["status"], "state": r["state"],
            "score": r["priority_score"], "band": r["score_band"],
        })
    return out


def campaign_ids_for_contact(conn, contact_id):
    return [r["campaign_id"] for r in conn.execute(
        "SELECT campaign_id FROM campaign_members WHERE contact_id=? AND state!='removed'",
        (contact_id,))]
