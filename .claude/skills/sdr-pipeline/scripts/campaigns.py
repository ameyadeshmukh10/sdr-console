#!/usr/bin/env python3
"""Campaign definition + qualification engine.

A campaign is a DEFINED SET OF ACCOUNTS SHOWING SIGNAL OVER A TARGET WINDOW, worked
through an ordered sequence of steps, where every step declares the CTA/offer it
carries. Membership is derived from the definition, never typed in by hand:

    signal_query + [window_start, window_end]  ->  campaign_members

`rolling` campaigns re-run that derivation on every sweep, so an account that first
shows signal on day 9 of a 30-day window joins on day 9. `snapshot` campaigns freeze
their membership at launch.

The time dimension comes from batch_db.signal_events, an append-only observation log:
account_signals holds one mutable latest row per domain and its *_checked_at columns
record when we LOOKED, so "which accounts showed signal between X and Y" is not
answerable from it.

Module + CLI:
    python3 campaigns.py list [--json]
    python3 campaigns.py show <id-or-key> [--json]
    python3 campaigns.py qualify <id-or-key> [--dry-run] [--json]
    python3 campaigns.py sweep [--json]            # qualify every active rolling campaign
    python3 campaigns.py plan <id-or-key>          # the step->CTA plan the generator reads
"""

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import batch_db as db

# ---- the signal registry ---------------------------------------------------
# Every kind of thing that counts as "this account is worth a touch right now".
# ONE place: adding a kind here makes it selectable in the campaign builder,
# scorable, chartable and CRM-mappable without touching anything else.
#
# To add a signal kind:
#   1. append an entry below (id, label, strength, how it arrives)
#   2. write events with db.record_signal_event(conn, domain, "<id>", summary, ...)
# Qualification, scoring, the UI vocabulary and the analytics breakdowns all read
# this dict, so nothing else needs to change.
#
# `strength` is the 0-50 base the scorer uses before recency decay. It encodes how
# good a REASON TO CALL each kind is on its own — a funding round is a conversation,
# a page view is a hint.
# `detector` is what produces it: 'scan' runs in discovery, 'llm' at copy-generation
# time, 'crm' arrives by field sync, 'internal' is computed from our own history.
# The BUILTIN kinds, derived from the same seed the `signal_defs` table is filled
# from — one source, so a kind cannot mean one thing to the scorer and another to
# the builder. These are the FALLBACK: the live registry is the table (see
# `signal_registry()`), which also carries whatever kinds this deployment defined
# for itself.
SIGNAL_REGISTRY = {
    s["kind"]: {k: v for k, v in s.items() if k not in ("kind", "sort_order")}
    for s in db.SIGNAL_DEF_SEED
}
SIGNAL_KINDS = tuple(SIGNAL_REGISTRY)

# Kinds discovery can actively go and look for (see DISCOVERY_KINDS below for the
# subset that has a working detector wired today).
SCAN_KINDS = tuple(k for k, v in SIGNAL_REGISTRY.items() if v["detector"] == "scan")


def signal_registry(conn=None):
    """The signal kinds in force, builtin + whatever this deployment defined.

    Reads `signal_defs` when given a connection, because what counts as a signal is
    configuration, not code: one team's buying trigger is a page view, another's is
    "we lost a deal to them last year". Falls back to the builtin constant when
    there is no connection or the table predates this feature — a caller that cannot
    reach the DB still gets a working vocabulary rather than an empty one.

    INACTIVE kinds are included. Deactivating a kind stops it being OFFERED and
    stops its rule running; it must not retroactively invalidate campaigns that
    already qualify against it, or turning one off would break saved definitions.
    """
    if conn is None:
        return dict(SIGNAL_REGISTRY)
    try:
        rows = db.signal_defs(conn)
    except Exception:  # noqa: BLE001 — an older DB has no table yet
        return dict(SIGNAL_REGISTRY)
    if not rows:
        return dict(SIGNAL_REGISTRY)
    return {r["kind"]: {
        "label": r["label"], "strength": r["strength"],
        "decay_scale": r.get("decay_scale") or 1.0,
        "detector": r.get("detector"), "description": r.get("description"),
        "active": bool(r.get("active", 1)), "builtin": bool(r.get("builtin")),
        "rule": r.get("rule"),
    } for r in rows}


def known_kinds(conn=None):
    """Every kind a signal_query may name."""
    return tuple(signal_registry(conn))

# Recognized signal_query keys. Anything else is rejected at validation time rather
# than silently ignored — a typo'd filter that quietly matches everything would
# enroll the wrong accounts.
SIGNAL_QUERY_KEYS = {
    "kinds",             # list[str]  which signal families count (default: all)
    "min_score",         # int 0-100  ALSO require this fit score (see the note below)
    "require_senior",    # bool       only buyers senior enough to be worth a touch
    "require_recent",    # bool       research signal must be a real dated event
    "hiring_sales_min",  # int        minimum open SALES roles
    "tech_playbook",     # list[str]  any of sequencing|intent_abm|ads must be present
    "personas",          # list[str]  contact persona allowlist
    "motion",            # str        outbound|inbound|any (default outbound)
    "domains",           # list[str]  explicit account allowlist (bypasses signal match)
    "exclude_enrolled",  # bool       skip contacts already enrolled anywhere (default true)
}

VALID_MOTIONS = ("outbound", "inbound", "any")
VALID_PLAYBOOK = ("sequencing", "intent_abm", "ads")


def validate_signal_query(q, conn=None):
    """Return a normalized copy, or raise ValueError. Empty/None is valid and means
    'any signal of any kind' — deliberately permissive, because the WINDOW is then
    doing the defining work.

    `conn` widens the accepted kinds to whatever this deployment has DEFINED (see
    signal_registry). Without it only the builtins validate, which is the safe
    direction to be wrong in: a caller with no DB access rejects a custom kind
    rather than accepting a typo."""
    q = dict(q or {})
    unknown = set(q) - SIGNAL_QUERY_KEYS
    if unknown:
        raise ValueError(f"unknown signal_query keys: {', '.join(sorted(unknown))}")
    valid = known_kinds(conn)
    kinds = q.get("kinds") or list(valid)
    if not isinstance(kinds, list) or any(k not in valid for k in kinds):
        raise ValueError(f"kinds must be a subset of {list(valid)}")
    pb = q.get("tech_playbook") or []
    if not isinstance(pb, list) or any(p not in VALID_PLAYBOOK for p in pb):
        raise ValueError(f"tech_playbook must be a subset of {list(VALID_PLAYBOOK)}")
    motion = q.get("motion") or "outbound"
    if motion not in VALID_MOTIONS:
        raise ValueError(f"motion must be one of {list(VALID_MOTIONS)}")
    hsm = q.get("hiring_sales_min")
    if hsm is not None and (not isinstance(hsm, int) or hsm < 0):
        raise ValueError("hiring_sales_min must be a non-negative integer")
    ms = q.get("min_score")
    if ms is not None:
        try:
            ms = float(ms)
        except (TypeError, ValueError):
            raise ValueError("min_score must be a number between 0 and 100")
        if not 0 <= ms <= 100:
            raise ValueError("min_score must be between 0 and 100")
    out = {
        "kinds": kinds,
        "require_recent": bool(q.get("require_recent", False)),
        "tech_playbook": pb,
        "motion": motion,
        "personas": [str(p) for p in (q.get("personas") or [])],
        "domains": [str(d).strip().lower() for d in (q.get("domains") or []) if str(d).strip()],
        "exclude_enrolled": bool(q.get("exclude_enrolled", True)),
    }
    if hsm is not None:
        out["hiring_sales_min"] = hsm
    if ms is not None:
        out["min_score"] = ms
    if q.get("require_senior"):
        out["require_senior"] = True
    return out


def _sales_count(detail_json):
    try:
        return len(json.loads(detail_json or "{}").get("sales_titles") or [])
    except (json.JSONDecodeError, AttributeError, TypeError):
        return 0


_EMPTY_PLAYBOOK = {"ads": [], "intent_abm": [], "sequencing": []}


def _playbook(detail_json):
    """Playbook groups from a stored tech_detail. Imported lazily — tech_signals
    pulls dnspython, and the server must boot without it.

    Always a dict: playbook_from_detail returns None for a legacy row with no
    parseable detail, and a caller indexing that None fails the whole sweep."""
    try:
        import tech_signals
        return tech_signals.playbook_from_detail(detail_json) or _EMPTY_PLAYBOOK
    except Exception:  # noqa: BLE001 — a missing optional dep must not fail a sweep
        return _EMPTY_PLAYBOOK


def _event_qualifies(event, q):
    """Does one signal observation satisfy the campaign's filters?"""
    kind = event["kind"]
    if kind not in q["kinds"]:
        return False
    if kind == "research" and q["require_recent"] and not event.get("has_recent"):
        return False
    if kind == "hiring" and q.get("hiring_sales_min") is not None:
        if _sales_count(event.get("detail")) < q["hiring_sales_min"]:
            return False
    if kind == "tech" and q["tech_playbook"]:
        groups = _playbook(event.get("detail"))
        if not any(groups.get(g) for g in q["tech_playbook"]):
            return False
    return True


def qualifying_accounts(conn, campaign):
    """{domain: {"winner": event, "events": [all qualifying events]}} for every
    account that showed a qualifying signal inside the campaign window.

    The NEWEST qualifying event wins — that is the signal the copy should be written
    against, and it is what the member snapshot records. The full list is kept
    because scoring rewards an account whose signals STACK (news + hiring + a tech
    play is a materially stronger account than any one of them alone).

    An explicit `domains` allowlist bypasses the signal match entirely, so a campaign
    can also be a hand-picked account list on the same rails."""
    q = validate_signal_query(campaign.get("signal_query"), conn)
    if q["domains"]:
        return {d: {"winner": {"kind": "manual", "summary": None, "domain": d},
                    "events": []} for d in q["domains"]}
    events = db.signal_events_in_window(
        conn, start=campaign.get("window_start"), end=_end_bound(campaign.get("window_end")),
        kinds=q["kinds"])
    out = {}
    for ev in events:  # already newest-first
        if not _event_qualifies(ev, q):
            continue
        slot = out.setdefault(ev["domain"], {"winner": ev, "events": []})
        slot["events"].append(ev)
    return out


# ---- signal scoring --------------------------------------------------------
# Priority = how STRONG the signal is at the moment the campaign qualifies the
# account. It orders the SDR's call list, so it has to be explainable: every
# component below is reported alongside the total, and the UI shows them on hover.
#
# The score is deliberately frozen at qualification. A score that drifted as signals
# aged would make yesterday's call list unreproducible, and "why was this person top
# of my list on Monday" is a question an SDR will actually ask. Re-scoring is an
# explicit action, not a side effect of time passing.
SCORE_BANDS = (("hot", 70), ("warm", 45), ("cool", 0))

# Recency DECAYS the signal's strength rather than adding to it. Outbound ages fast —
# a funding round called in week one lands very differently from the same round eight
# weeks later — but freshness alone is not a reason to call: as an independent
# additive term it let a trivial "they run HubSpot" detection reach warm just for
# having been scanned today. Multiplying keeps a weak signal weak however fresh.
_RECENCY_DECAY = ((7, 1.0), (14, 0.85), (30, 0.6), (60, 0.35), (10 ** 6, 0.15))

# Persona fit for THIS product (an SDR AI Worker). Sales leadership owns the number
# the pitch is about; partnerships sits furthest from it.
_PERSONA_FIT = {"sales-leadership": 25, "sdr-bdr": 20, "revops": 17, "partnerships": 12}

_MAX_STRENGTH, _MAX_STACKING, _MAX_PERSONA = 50, 25, 25


def _days_since(ts):
    if not ts:
        return None
    try:
        t = datetime.strptime(ts[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None
    return max(0, (datetime.now(timezone.utc) - t).days)


def _kind_strength(ev, registry=None):
    """0-50 for one observation, by how strong a reason to call it actually is.

    The registry supplies each kind's base; the refinements below only apply where a
    kind has sub-grades worth distinguishing. A kind added to SIGNAL_REGISTRY with no
    clause here scores at its declared base, which is the point — new signals work
    immediately rather than silently falling through to a floor value."""
    kind = ev.get("kind")
    reg = registry if registry is not None else SIGNAL_REGISTRY
    base = (reg.get(kind) or {}).get("strength")
    if kind == "research":
        # a dated, real event is the whole point; the fallback anchor is not a signal
        return base if ev.get("has_recent") else 8
    if kind == "hiring":
        n = _sales_count(ev.get("detail"))
        return base if n >= 3 else 30 if n >= 1 else 10
    if kind == "tech":
        groups = _playbook(ev.get("detail"))
        # a detected sequencing or intent tool is a play we can name; a generic
        # stack detection is only background colour
        return base if (groups.get("sequencing") or groups.get("intent_abm")) else 13
    if kind == "manual":
        return 25  # hand-picked account: real intent, but no observed signal to rate
    return base if base is not None else 8


# Momentum: how much a score MOVED since this contact was last scored, and how much
# that movement is allowed to shift their rank. An account warming up (last quarter
# 40, now 65) is a better call than a statically-equal account cooling off, because
# the direction is itself the news. Bounded on purpose — momentum nudges the order,
# it never overturns raw signal strength.
MOMENTUM_WEIGHT = 0.5
MOMENTUM_CAP = 15.0


def apply_momentum(score, prev):
    """(momentum, rank_score) — the delta and the value the call list sorts on.

    No prior score means no momentum, NOT zero momentum penalised against people who
    improved: a first-time contact ranks on raw strength alone."""
    if prev is None:
        return None, score
    momentum = round(score - float(prev), 1)
    adj = max(-MOMENTUM_CAP, min(MOMENTUM_CAP, momentum * MOMENTUM_WEIGHT))
    return momentum, round(score + adj, 1)


# ---- the money scale -------------------------------------------------------
# One glyph carrying the two things a rep actually weighs before picking up the
# phone, on two independent axes so neither hides the other:
#
#   COUNT ($ to $$$$$)  how much is here — aggregate signal strength and ICP fit
#   HEAT   (the colour) how ready they are — is this warming up, and did they
#                       come to us
#
# They are genuinely different questions and conflating them is the mistake this
# replaces. A perfect-fit account with three stacked signals that nobody has warmed
# is a big opportunity and a cold call: five dollar signs, cool colour. An inbound
# contact who filled in a form is a small opportunity and a hot one: one dollar
# sign, hot colour. A single number cannot say that, and a rep planning a day needs
# both — which to invest in, and which to call first.
MONEY_STEPS = ((82, 5), (66, 4), (48, 3), (30, 2), (0, 1))
# Momentum needed to read as warming/cooling rather than noise. Below it the score
# moved by less than a rounding of one component.
MONEY_MOMENTUM_EPS = 4.0
MONEY_HEAT = ("cold", "open", "warming", "hot")


def money_rating(score, momentum=None, motion=None, band=None, base=None):
    """{level 1-5, heat, label} — the aggregate signal, as money.

    `heat` is deliberately NOT a restatement of the band; the band is already the
    count. It answers "is this getting warmer":

      hot      inbound — they came to us, whatever the size of the opportunity
      warming  the score is climbing since the last campaign scored them
      cooling  … falling. Rendered as `open`: still real opportunity, just going
               the wrong way, which is exactly when a rep should call rather than
               wait for it to warm on its own
      open     no momentum either way and a strong score: lots of opportunity on
               ICP fit, no evidence they are ready
      cold     no momentum, nothing much there

    `score` is what the level is cut from (rank_score, so momentum counts toward the
    glyph); `base` is the pure priority score. Both are returned because the UI
    itemises the components, and those sum to `base` — a breakdown whose total was
    the rank score would visibly fail to add up.
    """
    try:
        s = float(score or 0)
    except (TypeError, ValueError):
        s = 0.0
    level = next(v for floor, v in MONEY_STEPS if s >= floor)

    if str(motion or "").lower() == "inbound":
        heat = "hot"
    elif momentum is not None and float(momentum) >= MONEY_MOMENTUM_EPS:
        heat = "warming"
    elif level >= 3:
        # Cooling and flat-but-strong both read as `open`: unworked opportunity.
        heat = "open"
    else:
        heat = "cold"
    return {"level": level, "heat": heat, "score": round(s, 1),
            "base": round(float(base), 1) if base is not None else round(s, 1),
            "band": band, "momentum": momentum}


def attach_money(rows):
    """Add `money` to each member row, in place, and return them.

    Derived on read rather than stored: every input is already frozen on the row
    (rank_score, momentum) or fixed on the contact (motion), so a stored copy could
    only ever disagree with the score displayed beside it. The rating still moves
    only when a rescore moves the score — the freeze that keeps a call list
    reproducible holds.
    """
    for r in rows or []:
        r["money"] = money_rating(
            r.get("rank_score") if r.get("rank_score") is not None else r.get("priority_score"),
            momentum=r.get("momentum"), motion=r.get("motion"), band=r.get("score_band"),
            base=r.get("priority_score"))
    return rows


def score_member(events, contact, winner=None, registry=None):
    """(score 0-100, band, {components}) for one contact at qualification time.

    signal_strength  0-50  the strongest signal, decayed by how old it is
    stacking         0-25  +12.5 per ADDITIONAL signal family firing in the window
    persona_fit      0-25  how close this buyer sits to the number we pitch

    Stacking is the component worth arguing about, and it earns its weight: an
    account with fresh news AND open sales roles AND a sequencing tool is a
    materially better call than one with any of the three, because each gives the
    rep a different thing to say.
    """
    reg = registry if registry is not None else SIGNAL_REGISTRY
    events = events or ([winner] if winner else [])
    strength = lambda e: _kind_strength(e, reg)  # noqa: E731
    best = max(events, key=strength) if events else (winner or {})
    raw = strength(best) if best else 0

    days = _days_since(best.get("observed_at")) if best else None
    scale = (reg.get(best.get("kind")) or {}).get("decay_scale", 1.0) or 1.0
    eff = None if days is None else days * scale
    decay = 0.15 if eff is None else next(v for lim, v in _RECENCY_DECAY if eff <= lim)
    strength = round(raw * decay, 1)

    kinds = {e.get("kind") for e in events if e.get("kind")}
    stacking = min(_MAX_STACKING, 12.5 * max(0, len(kinds) - 1))

    persona = _PERSONA_FIT.get(contact.get("persona"), 10)

    components = {"signal_strength": strength, "stacking": stacking,
                  "persona_fit": persona}
    total = round(sum(components.values()), 1)
    band = next(name for name, floor in SCORE_BANDS if total >= floor)
    return total, band, {
        "components": components,
        "signal_kinds": sorted(kinds),
        "days_since_signal": days,
        "recency_multiplier": decay,
        "basis": best.get("summary") if best else None,
    }


def _end_bound(window_end):
    """A date-only end bound is inclusive of that whole day. '2026-08-31' compares
    lexically below '2026-08-31T14:00:00Z', which would silently drop the final
    day's signals, so widen a bare date to end-of-day."""
    we = (window_end or "").strip()
    if len(we) == 10 and we.count("-") == 2:
        return we + "T23:59:59Z"
    return we or None


def _is_senior(role):
    """Is this buyer role senior enough to be worth a direct touch?

    Reuses capacity.SENIOR_ROLES — the same taxonomy the channel recommendation
    uses — rather than a parallel list, so "senior" cannot come to mean two
    different things on two screens."""
    if not role:
        return False
    try:
        import capacity
        return role in capacity.SENIOR_ROLES
    except Exception:  # noqa: BLE001 — fail OPEN: a missing taxonomy must not
        return True    # silently empty a campaign


def _buyer_role(title):
    """The buyer-group role label for a job title, e.g. 'CRO / Sales Chief'.

    Reads the CONFIGURED buyer group (`buyer_group_roles`), which is the same ruleset
    that decides what enrichment searches for and which returned contacts survive —
    so editing a role in the console changes all of them together. Falls back to the
    original hardcoded taxonomy when the table is absent. Feeds the channel
    recommendation (see capacity.SENIOR_ROLES) and the ad-audience grouping.

    Lazy import: buyer_group lives in the ai-sdr skill, and a missing module must
    never break qualification."""
    if not title:
        return None
    try:
        import buyer_group_config as _bg
        conn = db.connect()
        try:
            return _bg.role_label(conn, title)
        finally:
            conn.close()
    except Exception:  # noqa: BLE001
        return None


def qualify(conn, campaign, commit=True, audience_crm=None):
    """Apply the campaign's definition and (by default) materialize the result.

    Two filters compose here: the AUDIENCE decides which accounts are in the pool at
    all (a HubSpot list, a closed-lost CRM segment, everything), the SIGNAL QUERY
    decides which of that pool is worth working now. Each member also gets a channel
    recommendation, because a priority order that doesn't say where to spend leaves
    the scarce resource — LinkedIn actions, rep time — allocated by whoever scrolls
    first.

    Returns a summary dict. Never removes existing members: a contact who qualified
    on day 3 stays a member even if their signal later ages out of the window —
    they are mid-sequence, and yanking them would strand a half-sent cadence.

    `audience_crm` overrides where a HubSpot-list or CRM-segment audience resolves
    from (see audiences.LIVE) — passed explicitly rather than read from a global so
    a demo request can never leak its source into a concurrent live one."""
    cid = campaign["campaign_id"]
    q = validate_signal_query(campaign.get("signal_query"), conn)
    # The live registry, read once per run: the scorer needs each kind's strength
    # and decay, and those are configuration now rather than constants.
    _reg = signal_registry(conn)
    winners = qualifying_accounts(conn, campaign)
    reasons = {"no_signal_window": 0, "persona": 0, "motion": 0,
               "already_member": 0, "suppressed": 0, "enrolled_elsewhere": 0,
               "over_cap": 0, "outside_audience": 0, "below_fit": 0, "not_senior": 0}

    # Audience gate. None = no restriction (the all_contacts default and the
    # pre-audience behaviour). A resolution FAILURE must not silently widen the
    # campaign to everyone, so it is reported and the run yields nothing.
    allowed, audience_desc, audience_err = None, None, None
    try:
        import audiences
        aud = audiences.validate_audience(campaign.get("audience"))
        audience_desc = audiences.describe(aud)
        if aud["type"] != "all_contacts":
            res = audiences.resolve(conn, aud, crm=audience_crm)
            if (res.get("stats") or {}).get("error"):
                audience_err = res["stats"]["error"]
            else:
                allowed = set(res["contact_ids"])
    except Exception as e:  # noqa: BLE001
        audience_err = f"{type(e).__name__}: {e}"
    if audience_err:
        return {"campaign_id": cid, "accounts_matched": 0, "candidates": 0,
                "added": 0, "skipped": reasons, "committed": False,
                "audience": audience_desc, "audience_error": audience_err}

    # Capacity feeds the channel recommendation: when today's LinkedIn allowance is
    # gone, only the very top of the list keeps that channel.
    li_left = em_left = None
    try:
        import capacity
        cap = capacity.status(conn)
        li_left, em_left = cap["linkedin"]["remaining"], cap["email"]["remaining"]
    except Exception:  # noqa: BLE001
        pass

    if not winners:
        return {"campaign_id": cid, "accounts_matched": 0, "candidates": 0,
                "added": 0, "skipped": reasons, "committed": False}

    existing = {r["contact_id"] for r in conn.execute(
        "SELECT contact_id FROM campaign_members WHERE campaign_id=?", (cid,))}
    suppressed = db.suppressed_contact_ids(conn)
    enrolled_elsewhere = set()
    if q["exclude_enrolled"]:
        enrolled_elsewhere = {r["contact_id"] for r in conn.execute(
            "SELECT contact_id FROM campaign_members "
            "WHERE state IN ('enrolled','replied') AND campaign_id!=?", (cid,))}

    # Respect the account cap on ACCOUNTS, not contacts — a campaign targeting 50
    # accounts means 50 companies, however many buyers each carries.
    cap = campaign.get("target_accounts")
    room = None
    if cap:
        current = conn.execute(
            "SELECT COUNT(DISTINCT domain) FROM campaign_members WHERE campaign_id=?",
            (cid,)).fetchone()[0]
        room = max(0, int(cap) - current)

    placeholders = ",".join("?" * len(winners))
    rows = [dict(r) for r in conn.execute(
        f"SELECT * FROM contacts WHERE domain IN ({placeholders}) ORDER BY domain, rowid",
        list(winners))]

    picked, accounts_used, off_targets = [], set(), []
    for c in rows:
        if c["contact_id"] in existing:
            reasons["already_member"] += 1
            continue
        if allowed is not None and c["contact_id"] not in allowed:
            reasons["outside_audience"] += 1
            continue
        if q["personas"] and c.get("persona") not in q["personas"]:
            reasons["persona"] += 1
            continue
        if q["motion"] != "any" and (c.get("motion") or "outbound") != q["motion"]:
            reasons["motion"] += 1
            continue
        if c["contact_id"] in suppressed:
            # Outreach is switched off for this person. They are NOT added to the
            # campaign and never will be while it is off — but they still MATCHED,
            # and that is worth knowing: they are part of the addressable set you
            # have chosen not to contact, not an account that failed the filter.
            # Silently dropping them would understate the real size of the target.
            reasons["suppressed"] += 1
            if len(off_targets) < 50:
                off_targets.append({
                    "contact_id": c["contact_id"], "domain": c.get("domain"),
                    "name": f"{c.get('first_name') or ''} {c.get('last_name') or ''}".strip(),
                    "company": c.get("company"), "title": c.get("title"),
                })
            continue
        if c["contact_id"] in enrolled_elsewhere:
            reasons["enrolled_elsewhere"] += 1
            continue
        dom = c.get("domain")
        if room is not None and dom not in accounts_used and len(accounts_used) >= room:
            reasons["over_cap"] += 1
            continue
        slot = winners.get(dom) or {}
        ev = slot.get("winner") or {}
        score, band, detail = score_member(slot.get("events"), c, winner=ev,
                                           registry=_reg)
        role = _buyer_role(c.get("title"))
        # FIT GATE. The signal criteria say the ACCOUNT is worth working; this says
        # whether this PERSON at it is. Without it a qualifying account sweeps in
        # everyone we hold there, including the people the scorer rates as barely
        # worth a touch — which is how a campaign quietly becomes a blast.
        #
        # Applied after scoring on purpose: the score already blends signal
        # strength, stacking and persona fit, so it IS the fit measure rather than a
        # second opinion about one.
        if q.get("min_score") is not None and score < q["min_score"]:
            reasons["below_fit"] += 1
            continue
        if q.get("require_senior") and not _is_senior(role):
            reasons["not_senior"] += 1
            continue
        channels = None
        try:
            import capacity
            channels = capacity.recommend_channels(score, role, li_left, em_left)
        except Exception:  # noqa: BLE001
            pass
        prior = db.previous_score(conn, c["contact_id"])
        prev = prior["priority_score"] if prior else None
        momentum, rank = apply_momentum(score, prev)
        if momentum is not None:
            detail["previous_score"] = prev
            detail["momentum"] = momentum
        accounts_used.add(dom)
        picked.append({
            "contact_id": c["contact_id"], "domain": dom,
            "signal_kind": ev.get("kind"),
            "signal_snapshot": {"kind": ev.get("kind"), "summary": ev.get("summary"),
                                "observed_at": ev.get("observed_at")},
            "priority_score": score, "score_band": band, "score_detail": detail,
            "previous_score": prev, "momentum": momentum, "rank_score": rank,
            "buyer_role": role, "channels": channels,
            "origin": c.get("_origin") or "existing",
            "_contact": c,
        })

    # Strongest first by RANK, so a partial run (cap hit, manual stop) takes the best
    # accounts rather than whichever happened to sort first by domain.
    picked.sort(key=lambda p: -(p["rank_score"] or 0))

    added = 0
    if commit and picked:
        added = db.add_members(conn, cid, picked)
        db.update_campaign(conn, cid, last_qualified_at=db.now())
    bands = {}
    for p in picked:
        bands[p["score_band"]] = bands.get(p["score_band"], 0) + 1
    return {
        "campaign_id": cid, "campaign": campaign.get("name"),
        "accounts_matched": len(winners), "candidates": len(picked),
        "accounts_added": len(accounts_used), "added": added,
        "skipped": reasons, "committed": bool(commit), "bands": bands,
        "audience": audience_desc,
        # People who MATCH but have outreach switched off. Reported separately from
        # both the candidates and the other skip reasons, because they are neither:
        # they are part of the addressable set, deliberately not being contacted.
        # Rolling them into "skipped" would understate how big the target actually is.
        "off_targets": off_targets,
        "off_count": reasons["suppressed"],
        "preview": [{"contact_id": p["contact_id"], "domain": p["domain"],
                     "name": f"{p['_contact'].get('first_name','')} "
                             f"{p['_contact'].get('last_name','')}".strip(),
                     "title": p["_contact"].get("title"),
                     "company": p["_contact"].get("company"),
                     "persona": p["_contact"].get("persona"),
                     "signal_kind": p["signal_kind"],
                     "priority_score": p["priority_score"],
                     "score_band": p["score_band"],
                     "score_detail": p["score_detail"],
                     "previous_score": p["previous_score"],
                     "momentum": p["momentum"],
                     "rank_score": p["rank_score"],
                     "buyer_role": p["buyer_role"],
                     "channels": p["channels"],
                     "signal": p["signal_snapshot"].get("summary")}
                    for p in picked[:50]],
    }


def rescore(conn, campaign, commit=True):
    """Recompute every member's priority against the signals visible now.

    Explicit on purpose (see the frozen-score note on SCORE_BANDS): an SDR working a
    list wants it stable, so scores only move when someone asks. Useful after a
    discovery run turns up new signals for accounts already in the campaign."""
    cid = campaign["campaign_id"]
    winners = qualifying_accounts(conn, campaign)
    _reg = signal_registry(conn)
    li_left = em_left = None
    try:
        import capacity
        cap = capacity.status(conn)
        li_left, em_left = cap["linkedin"]["remaining"], cap["email"]["remaining"]
    except Exception:  # noqa: BLE001
        pass
    changed, up, down = 0, 0, 0
    for m in db.campaign_members(conn, cid):
        slot = winners.get(m.get("domain")) or {}
        score, band, detail = score_member(slot.get("events"), m,
                                           winner=slot.get("winner"), registry=_reg)
        role = m.get("buyer_role") or _buyer_role(m.get("title"))
        channels = None
        try:
            import capacity
            channels = capacity.recommend_channels(score, role, li_left, em_left)
        except Exception:  # noqa: BLE001
            pass
        # On a RESCORE the baseline is this member's own current score — the whole
        # point is "did it move since we last looked at them here". A prior score in
        # a different campaign only seeds the very first scoring.
        prev = m.get("priority_score")
        if prev is None:
            prior = db.previous_score(conn, m["contact_id"], exclude_campaign_id=cid)
            prev = prior["priority_score"] if prior else None
        momentum, rank = apply_momentum(score, prev)
        if momentum is not None:
            detail["previous_score"] = prev
            detail["momentum"] = momentum
            if momentum > 0:
                up += 1
            elif momentum < 0:
                down += 1
        if m.get("priority_score") != score:
            changed += 1
        if commit:
            db.set_member_score(conn, cid, m["contact_id"], score, band, detail,
                                previous_score=prev, momentum=momentum,
                                rank_score=rank, channels=channels, buyer_role=role)
    if commit:
        conn.commit()
    return {"campaign_id": cid, "rescored": changed, "warming": up, "cooling": down,
            "committed": bool(commit)}


# ---- discovery: actively FIND accounts for a campaign ----------------------
# Qualification only reads signals already observed, so a brand-new campaign can
# only ever catch accounts something else happened to scan. Discovery is the other
# half: it runs the detectors over in-scope accounts that have no fresh scan, which
# turns "no accounts match" into "here are the ones that do".
#
# Only the two DETERMINISTIC detectors run here. The research signal comes from an
# LLM web search and is produced during copy generation (generate_batch.py) — firing
# it per-account from a background sweep would be slow and expensive, so discovery
# reports it as generation-time rather than pretending to cover it.
DISCOVERY_KINDS = ("tech", "hiring")
DEFAULT_DISCOVERY_INTERVAL_DAYS = 7

# The literals a detector stores when it looked and there was nothing. "We checked
# and found nothing" is a legitimate, useful result — it is what stops the account
# being re-scanned and re-billed — but it is NOT a detection, and counting it as one
# is how a scan that found nothing reports as a success.
NEGATIVE_RESULTS = {"No signals detected", "No open roles detected"}


def _scan_result(conn, domain, company, kinds):
    """What a just-scanned domain actually yielded: {kind: line} for real findings.

    Read back from account_signals rather than inferred from "the runner didn't
    raise", because those are different facts and only one of them is worth showing
    a rep."""
    row = conn.execute(
        "SELECT company_name, tech_signals, hiring_signals FROM account_signals "
        "WHERE domain=?", (domain,)).fetchone()
    found = {}
    if row:
        for kind, col in (("hiring", "hiring_signals"), ("tech", "tech_signals")):
            if kind not in kinds:
                continue
            val = (row[col] or "").strip()
            if val and val not in NEGATIVE_RESULTS:
                found[kind] = val
    return {"domain": domain,
            "company": company or (row["company_name"] if row else None),
            "found": found, "any": bool(found)}


def discovery_scope(conn, campaign, limit=None, kinds=None):
    """In-scope domains that have no fresh scan for the kinds this campaign uses.

    Scope = domains of contacts matching the campaign's persona/motion filters, minus
    accounts already in the campaign. Ordered by how many in-scope contacts each
    domain carries, so a scan buys the most reach per call."""
    q = validate_signal_query(campaign.get("signal_query"), conn)
    kinds = [k for k in (kinds or q["kinds"]) if k in DISCOVERY_KINDS]
    if not kinds:
        return []
    where = ["c.domain IS NOT NULL", "c.domain != ''"]
    params = []
    if q["personas"]:
        where.append("c.persona IN (%s)" % ",".join("?" * len(q["personas"])))
        params += q["personas"]
    if q["motion"] != "any":
        # NULL motion reads as outbound, matching batch_db.classify_motion's default
        where.append("COALESCE(c.motion,'outbound') = ?")
        params.append(q["motion"])
    where.append("c.domain NOT IN (SELECT domain FROM campaign_members "
                 "WHERE campaign_id=? AND domain IS NOT NULL)")
    params.append(campaign["campaign_id"])

    # "Needs a scan" = never scanned for at least one requested kind. A stale scan is
    # left to the detectors' own refresh windows (TECH/HIRING_REFRESH_DAYS), which
    # already know how long a result stays good.
    needs = " OR ".join(f"s.{k}_checked_at IS NULL" for k in kinds)
    sql = (f"SELECT c.domain, MAX(c.company) company, COUNT(*) contacts "
           f"FROM contacts c LEFT JOIN account_signals s ON s.domain = c.domain "
           f"WHERE {' AND '.join(where)} AND ({needs}) "
           f"GROUP BY c.domain ORDER BY contacts DESC, c.domain")
    if limit:
        sql += " LIMIT ?"
        params.append(int(limit))
    return [dict(r) for r in conn.execute(sql, params)]


def discover(conn, campaign, limit=50, kinds=None, progress=None, qualify_after=True):
    """Scan candidate accounts, then qualify whatever now matches.

    COSTS: the hiring detector spends one Prospeo credit per non-cached domain. The
    tech detector is free (DNS + a static fetch). Callers must surface that — the UI
    states the credit cost on the button and defaults the limit low.

    progress: optional callback(done, total, domain) for the job UI."""
    q = validate_signal_query(campaign.get("signal_query"), conn)
    want = [k for k in (kinds or q["kinds"]) if k in DISCOVERY_KINDS]
    candidates = discovery_scope(conn, campaign, limit=limit, kinds=want)
    res = {"campaign_id": campaign["campaign_id"], "scanned": 0, "candidates": len(candidates),
           "kinds": want, "detected": {}, "errors": [], "unavailable": {},
           # Per-account outcomes. The screen's job is to show WHAT was found, not
           # just how many calls were made, so the list travels with the summary.
           "results": []}

    runners = {}
    if "tech" in want:
        try:
            import tech_signals as _tech
            ok, why = _tech.tech_available()
            if ok:
                runners["tech"] = lambda d, c: _tech.detect_and_store(d, company=c)
            else:
                res["unavailable"]["tech"] = why
        except Exception as e:  # noqa: BLE001
            res["unavailable"]["tech"] = str(e)
    if "hiring" in want:
        try:
            import hiring_signals as _hiring
            ok, why = _hiring.hiring_available()
            if ok:
                runners["hiring"] = lambda d, c: _hiring.detect_and_store(d, company=c)
            else:
                res["unavailable"]["hiring"] = why
        except Exception as e:  # noqa: BLE001
            res["unavailable"]["hiring"] = str(e)
    if "research" in (kinds or q["kinds"]):
        res["unavailable"]["research"] = (
            "researched at copy-generation time, not by discovery")

    if not runners:
        res["note"] = "no detector available for the requested signal kinds"
        return res

    for i, cand in enumerate(candidates):
        dom, company = cand["domain"], cand.get("company")
        failed = []
        for kind, run in runners.items():
            try:
                run(dom, company)
            except Exception as e:  # noqa: BLE001 — one bad domain must not stop a sweep
                failed.append(kind)
                res["errors"].append(f"{dom} [{kind}]: {type(e).__name__}: {e}")
        # `detected` counts REAL findings, not successful calls. Incrementing per
        # non-raising run reported "12 hiring" for twelve accounts that had none.
        item = _scan_result(conn, dom, company, list(runners))
        item["contacts"] = cand.get("contacts")
        if failed:
            item["error"] = ", ".join(failed)
        for kind in item["found"]:
            res["detected"][kind] = res["detected"].get(kind, 0) + 1
        res["results"].append(item)
        res["scanned"] += 1
        if progress:
            progress(i + 1, len(candidates), dom)
    res["found_accounts"] = sum(1 for r in res["results"] if r["any"])

    db.update_campaign(conn, campaign["campaign_id"], last_discovery_at=db.now())
    if qualify_after:
        # Re-read: the detectors wrote through their own connections.
        fresh = db.get_campaign(conn, campaign["campaign_id"])
        res["qualified"] = qualify(conn, fresh, commit=True)
    return res


# ---- enrichment: find NEW contacts at in-scope accounts --------------------
# Discovery scans accounts we already have CONTACTS at. Enrichment is the other
# direction: at those same accounts, find the buyers we DON'T have — the rest of the
# buying committee. That is what turns a campaign from "the 3 people we happen to
# hold at Acme" into a mapped buyer group, which is also what makes an ad audience
# worth buying.
#
# It costs Clay credits, so every call is metered into usage_ledger and the caller
# is told the floor cost before committing.
def enrichment_scope(conn, campaign, limit=None, min_existing=0):
    """Accounts in this campaign worth enriching, with how many contacts we hold.

    Ordered by FEWEST contacts first: the marginal value of a Clay call is highest
    where the buyer group is thinnest. An account where we already hold eight buyers
    does not need a ninth."""
    cid = campaign["campaign_id"]
    rows = [dict(r) for r in conn.execute("""
        SELECT m.domain, MAX(c.company) company, COUNT(*) held,
               MAX(m.priority_score) best_score
        FROM campaign_members m LEFT JOIN contacts c USING (contact_id)
        WHERE m.campaign_id=? AND m.domain IS NOT NULL AND m.domain != ''
        GROUP BY m.domain
        ORDER BY held ASC, best_score DESC
    """, (cid,))]
    rows = [r for r in rows if r["held"] >= min_existing]
    return rows[:int(limit)] if limit else rows


def enrich_estimate(conn, campaign, limit=25):
    """What enriching this campaign would cost, before spending anything."""
    scope = enrichment_scope(conn, campaign, limit=limit)
    try:
        import capacity
        est = capacity.estimate([("clay", "find-contacts", len(scope))])
    except Exception:  # noqa: BLE001
        est = {"credits_floor": float(len(scope)), "lines": []}
    return {"accounts": len(scope), "sample": scope[:10], **est}


def enrich(conn, campaign, limit=25, titles=None, locations=None,
           per_company_cap=3, progress=None, add_to_campaign=True):
    """Run Clay against this campaign's accounts and optionally enrol what it finds.

    Returns a summary including credits recorded. Every Clay call is metered whether
    or not it yields a contact — an empty search still bills.

    add_to_campaign=False stops after creating the contacts in HubSpot and the local
    pipeline, so the buyer group can be reviewed before anyone is sequenced. That is
    the default the UI uses for the first run.
    """
    cid = campaign["campaign_id"]
    scope = enrichment_scope(conn, campaign, limit=limit)
    res = {"campaign_id": cid, "accounts": len(scope), "found": 0, "created": 0,
           "added_to_campaign": 0, "credits": 0.0, "errors": [], "unavailable": None}
    if not scope:
        res["note"] = "no accounts in this campaign to enrich"
        return res

    try:
        import clay_mcp
        import clay_enrich
        mcp = clay_mcp.ClayMCP()
    except Exception as e:  # noqa: BLE001
        res["unavailable"] = f"Clay is not connected: {type(e).__name__}: {e}"
        return res

    candidates = []
    for i, acct in enumerate(scope):
        dom = acct["domain"]
        try:
            task_id = clay_enrich.fire_find_task(mcp, dom, titles, locations)
            # Meter on FIRE, not on success: Clay bills for the search regardless of
            # whether it returns anybody.
            db.record_usage(conn, "clay", "find-contacts", 1, "credits",
                            campaign_id=cid, ref=dom)
            res["credits"] += 1
            found = clay_enrich.poll_task(mcp, task_id, dom, acct.get("company"))
            for c in (found or [])[:per_company_cap]:
                candidates.append(c)
            res["found"] += len(found or [])
        except Exception as e:  # noqa: BLE001 — one bad account never stops the run
            res["errors"].append(f"{dom}: {type(e).__name__}: {e}")
        if progress:
            progress(i + 1, len(scope), dom)

    if not candidates:
        res["note"] = "no new contacts found"
        return res

    # Hand off to the existing create+dedup path rather than re-implementing it:
    # source_contacts.py already handles HubSpot dedup by email, the ICP gate, list
    # creation and pipeline ingest, and it is idempotent.
    import tempfile
    import subprocess
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
        json.dump(candidates, fh)
        path = fh.name
    try:
        script = str(Path(__file__).resolve().parent / "source_contacts.py")
        out = subprocess.run(
            [sys.executable, script, path, "--list-name",
             f"AI SDR — {campaign.get('name') or cid}"],
            capture_output=True, text=True, timeout=1800)
        last = [ln for ln in (out.stdout or "").splitlines() if ln.strip()]
        summary = json.loads(last[-1]) if last else {}
        res["source"] = summary
        res["created"] = summary.get("created", 0)
        # Each revealed work email is a second Clay charge.
        if summary.get("with_email_unique"):
            db.record_usage(conn, "clay", "reveal-email",
                            summary["with_email_unique"], "credits",
                            campaign_id=cid, ref="batch")
            res["credits"] += summary["with_email_unique"]
    except Exception as e:  # noqa: BLE001
        res["errors"].append(f"create: {type(e).__name__}: {e}")
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass

    db.update_campaign(conn, cid, last_enrich_at=db.now())
    if add_to_campaign:
        fresh = db.get_campaign(conn, cid)
        qres = qualify(conn, fresh, commit=True)
        res["added_to_campaign"] = qres.get("added", 0)
        res["qualified"] = qres
    return res


def discovery_due(campaign):
    """True when this campaign's discovery cadence has elapsed (default weekly)."""
    if campaign.get("status") != "active":
        return False
    every = campaign.get("discovery_interval_days")
    every = DEFAULT_DISCOVERY_INTERVAL_DAYS if every is None else int(every)
    if every <= 0:
        return False  # 0 = never auto-discover
    last = campaign.get("last_discovery_at")
    if not last:
        return True
    days = _days_since(last)
    return days is None or days >= every


# ---- evergreen: campaigns that keep running, but never unattended --------------
# An always-on campaign is the obvious thing to want and the easy thing to get
# wrong. What decays is not the targeting — a rolling window keeps finding fresh
# accounts on its own — it is the MESSAGE: the same four emails, sent to the next
# cohort, quarter after quarter, long after the story stopped being true.
#
# So evergreen here means "re-runs on a cadence, ASKS FIRST". At the end of a cycle
# the campaign stops adding accounts and raises a review; a human confirms or
# changes the angle and the sequence; only then does the next cycle open. The
# interval is not how often it relaunches — it is how often somebody is asked.
DEFAULT_EVERGREEN_INTERVAL_DAYS = 30


def review_due(campaign, today=None):
    """True when an evergreen campaign has reached the end of its cycle.

    Two triggers, either of which ends a cycle: the review date has arrived, or the
    target window closed early. The second matters because a campaign whose window
    ran out is finished working whatever the calendar says."""
    if not campaign.get("evergreen"):
        return False
    if campaign.get("status") not in ("active", "paused"):
        return False
    if campaign.get("review_state") == "pending":
        return False   # already asked; waiting on a human, not on the clock
    today = today or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    due = (campaign.get("review_due_at") or "")[:10]
    if due and due <= today:
        return True
    end = (campaign.get("window_end") or "")[:10]
    return bool(end and end < today)


def raise_review(conn, campaign):
    """End the cycle and ask. Pauses growth; nothing already enrolled is stopped.

    Paused rather than completed on purpose: completing would drop it out of the
    active list and make an evergreen campaign look like it ended, which is the one
    thing it did not do."""
    db.update_campaign(conn, campaign["campaign_id"],
                       status="paused", review_state="pending")
    return {"campaign_id": campaign["campaign_id"], "review": "pending"}


def pending_reviews(conn):
    """Evergreen campaigns waiting on a human, with what their cycle actually did.

    The numbers travel WITH the question. "This campaign wants your input" is a
    notification; "this cycle reached 34 accounts and 3 replied — same message
    again?" is a decision someone can make without opening four other screens."""
    out = []
    try:
        rows = [c for c in db.list_campaigns(conn)
                if c.get("evergreen") and c.get("review_state") == "pending"]
    except Exception:  # noqa: BLE001
        return out
    for c in rows:
        counts = db.campaign_counts(conn, c["campaign_id"])
        by_state = counts.get("by_state") or {}
        out.append({
            "campaign_id": c["campaign_id"], "name": c["name"], "key": c.get("key"),
            "cycle": c.get("cycle") or 1,
            "brief": c.get("brief"),
            "window_start": c.get("window_start"), "window_end": c.get("window_end"),
            "interval_days": c.get("evergreen_interval_days")
                             or DEFAULT_EVERGREEN_INTERVAL_DAYS,
            "accounts": counts.get("accounts", 0),
            "contacts": counts.get("members", 0),
            "enrolled": by_state.get("enrolled", 0),
            "replied": by_state.get("replied", 0),
            "steps": len(db.get_steps(conn, c["campaign_id"])),
        })
    return out


def relaunch(conn, campaign, brief=None, window_days=None, note=None):
    """Open the next cycle after a review. The ONLY way an evergreen campaign
    restarts — there is no path that skips the human.

    A fresh window is what makes the next cycle mean something: membership qualifies
    against signals observed inside it, so reusing the old dates would re-run against
    a period that has already been worked."""
    cid = campaign["campaign_id"]
    days = int(window_days or campaign.get("evergreen_interval_days")
               or DEFAULT_EVERGREEN_INTERVAL_DAYS)
    days = max(1, min(days, 365))
    today = datetime.now(timezone.utc)
    fields = {
        "status": "active",
        "review_state": None,
        "cycle": int(campaign.get("cycle") or 1) + 1,
        "relaunched_at": db.now(),
        "window_start": today.strftime("%Y-%m-%d"),
        "window_end": (today + timedelta(days=days)).strftime("%Y-%m-%d"),
        "review_due_at": (today + timedelta(days=days)).strftime("%Y-%m-%d"),
    }
    if brief is not None:
        fields["brief"] = brief.strip() or None
    updated = db.update_campaign(conn, cid, **fields)
    res = {"campaign_id": cid, "cycle": fields["cycle"],
           "window": [fields["window_start"], fields["window_end"]],
           "note": note}
    try:
        res["qualified"] = qualify(conn, updated, commit=True)
    except Exception as e:  # noqa: BLE001 — the relaunch itself already succeeded
        res["qualify_error"] = f"{type(e).__name__}: {e}"
    return res


def sweep(conn, commit=True, discovery_limit=25):
    """Re-qualify every active rolling campaign whose window is still open, and run
    discovery for any whose cadence has come due (default weekly).

    Qualification is cheap and local. Discovery makes network calls and can spend
    Prospeo credits, so it is rate-limited two ways: it only fires when the campaign's
    own interval has elapsed, and it scans at most `discovery_limit` accounts per
    campaign per sweep. Set CAMPAIGN_DISCOVERY_LIMIT=0 to disable it entirely and
    leave discovery a manual action."""
    out = []
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    # Rule-backed signal kinds first: they can create the very observations the
    # qualification below reads, exactly like discovery. Local-field rules are free;
    # the CRM-backed ones are skipped when no token is configured rather than
    # failing the whole sweep.
    signals = None
    if commit:
        try:
            import crm_signals
            signals = crm_signals.run_all(
                conn, include_crm=bool((os.environ.get("HUBSPOT_ACCESS_TOKEN") or "").strip()))
        except Exception as e:  # noqa: BLE001 — a bad rule never stops the sweep
            signals = {"error": f"{type(e).__name__}: {e}"}
    for c in db.list_campaigns(conn, status="active"):
        # Evergreen campaigns END A CYCLE rather than end. They are paused and a
        # review is raised — checked BEFORE the close below, or an evergreen
        # campaign whose window ran out would be completed and never asked.
        if review_due(c, today):
            if commit:
                out.append(raise_review(conn, c))
            continue
        if c.get("window_end") and c["window_end"] < today:
            # window closed — stop growing it, and mark it done so the sweep and the
            # UI agree about which campaigns are still live
            db.update_campaign(conn, c["campaign_id"], status="completed",
                               completed_at=db.now())
            out.append({"campaign_id": c["campaign_id"], "closed": True})
            continue
        entry = {"campaign_id": c["campaign_id"]}
        # Discovery first: it can create the very signals this qualification reads.
        if commit and discovery_limit and discovery_due(c):
            try:
                d = discover(conn, c, limit=discovery_limit, qualify_after=False)
                entry["discovered"] = {"scanned": d["scanned"], "errors": len(d["errors"])}
                c = db.get_campaign(conn, c["campaign_id"])
            except Exception as e:  # noqa: BLE001
                entry["discovery_error"] = f"{type(e).__name__}: {e}"
        if (c.get("membership_mode") or "rolling") != "rolling":
            # snapshot campaigns never re-qualify, but they still get discovery above
            # so their accounts' signals stay current for re-scoring
            if len(entry) > 1:
                out.append(entry)
            continue
        try:
            entry.update(qualify(conn, c, commit=commit))
        except ValueError as e:
            entry["error"] = str(e)
        out.append(entry)
    # The hot target list is a DAILY report: rebuild it once a day, not every sweep,
    # so it stays stable for a working day rather than reshuffling hourly under
    # whoever is working it.
    hot = None
    if commit and hot_list_stale():
        try:
            snap = refresh_hot_list(conn)
            hot = {"accounts": len(snap["accounts"]), "pool": snap["pool"]}
        except Exception as e:  # noqa: BLE001
            hot = {"error": f"{type(e).__name__}: {e}"}
    return {"swept": len(out), "results": out, "hot_list": hot,
            "signals": signals,
            "reviews_pending": len(pending_reviews(conn))}


# ---- hot target list -------------------------------------------------------
# The daily standing report: the N ACCOUNTS that best fit whatever campaigns are
# currently active. Deliberately account-level, not contact-level — a rep plans a
# day around accounts and then works the buying committee inside each, and the call
# list already covers the contact ordering.
#
# Refreshed daily rather than computed live so the list is STABLE for a working day:
# a target list that reshuffled between two page loads is not a list anyone can plan
# against. `hot_target_list(refresh=True)` rebuilds it; the sweep does that once a day.
HOT_LIST_SIZE = 20


def _account_rows(conn):
    """Every account in an active campaign, aggregated to account level.

    Deduped to ONE ROW PER CONTACT first. A person can sit in several active
    campaigns at once, and grouping the raw membership rows counted them once per
    campaign — an account with 3 buyers in 2 campaigns reported "6 buyers mapped"
    and double-weighted its own momentum. The inner query keeps each contact's
    best-ranked membership and the outer one aggregates that.
    """
    return [dict(r) for r in conn.execute("""
        WITH best AS (
            SELECT m.*, cam.name AS campaign_name,
                   ROW_NUMBER() OVER (
                       PARTITION BY m.contact_id
                       ORDER BY COALESCE(m.rank_score, m.priority_score) DESC,
                                m.qualified_at DESC) AS rn
            FROM campaign_members m
            JOIN campaigns cam USING (campaign_id)
            WHERE cam.status='active' AND m.state != 'removed'
              AND m.domain IS NOT NULL AND m.domain != ''
        )
        SELECT b.domain,
               MAX(COALESCE(c.company, b.domain))            AS company,
               COUNT(*)                                      AS contacts,
               MAX(COALESCE(b.rank_score, b.priority_score)) AS best_rank,
               MAX(b.priority_score)                         AS best_score,
               AVG(b.priority_score)                         AS avg_score,
               SUM(CASE WHEN b.score_band='hot' THEN 1 ELSE 0 END) AS hot,
               SUM(COALESCE(b.momentum, 0))                  AS momentum_sum,
               SUM(CASE WHEN b.state='enrolled' THEN 1 ELSE 0 END) AS enrolled,
               MAX(b.campaign_name)                          AS campaign_name,
               MAX(b.campaign_id)                            AS campaign_id,
               COUNT(DISTINCT b.campaign_id)                 AS campaigns
        FROM best b LEFT JOIN contacts c USING (contact_id)
        WHERE b.rn = 1
        GROUP BY b.domain
    """)]


def hot_target_list(conn, size=HOT_LIST_SIZE):
    """The top `size` accounts across all active campaigns, with why each is there.

    Fit is the account's best contact rank, plus credit for a mapped buying committee
    and for warming momentum — the same three things the per-contact score weighs,
    lifted to the account. An account already fully enrolled is not excluded but is
    marked, because "we're already on it" is what a rep needs to know before calling.
    """
    rows = _account_rows(conn)
    for r in rows:
        best = r["best_rank"] or 0
        # committee depth: a mapped buying group is worth real weight, capped so a
        # big-but-cold account cannot outrank a small hot one
        depth = min(10, 2.5 * max(0, (r["contacts"] or 1) - 1))
        mom = max(-10, min(10, (r["momentum_sum"] or 0) * 0.5))
        r["fit"] = round(best + depth + mom, 1)
        r["reasons"] = []
        if r["hot"]:
            r["reasons"].append(f"{r['hot']} hot contact{'s' if r['hot'] > 1 else ''}")
        if (r["contacts"] or 0) > 1:
            r["reasons"].append(f"{r['contacts']} buyers mapped")
        if (r["momentum_sum"] or 0) > 0:
            r["reasons"].append("signal warming")
        elif (r["momentum_sum"] or 0) < 0:
            r["reasons"].append("signal cooling")
        if r["enrolled"]:
            r["reasons"].append(f"{r['enrolled']} already enrolled")
        r["avg_score"] = round(r["avg_score"], 1) if r["avg_score"] is not None else None
        r["momentum_sum"] = round(r["momentum_sum"] or 0, 1)
        # Same glyph as the call list, lifted to the account: sized on the best
        # contact's rank so a big account cannot inflate its own rating by having
        # more people in it (committee depth is already priced into `fit`).
        r["money"] = money_rating(r["best_rank"], momentum=r["momentum_sum"])
    rows.sort(key=lambda r: -r["fit"])
    return {
        "generated_at": db.now(),
        "size": size,
        "accounts": rows[:size],
        "pool": len(rows),
    }


HOT_LIST_PATH = db.DB_PATH.parent / "hot-list.json"


def refresh_hot_list(conn, size=HOT_LIST_SIZE):
    """Recompute and persist the daily snapshot. Atomic write — a half-written file
    read by the console would render an empty report."""
    payload = hot_target_list(conn, size=size)
    HOT_LIST_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = HOT_LIST_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False))
    os.replace(tmp, HOT_LIST_PATH)
    return payload


def hot_list_stale(path=None, hours=24):
    """True when the snapshot is missing or older than `hours` — the daily trigger."""
    p = Path(path or HOT_LIST_PATH)
    if not p.is_file():
        return True
    try:
        gen = json.loads(p.read_text()).get("generated_at")
    except (json.JSONDecodeError, OSError):
        return True
    age = _days_since(gen)
    if age is None:
        return True
    return age * 24 >= hours or _hours_since(gen) >= hours


def _hours_since(ts):
    if not ts:
        return 10 ** 6
    try:
        t = datetime.strptime(ts[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return 10 ** 6
    return (datetime.now(timezone.utc) - t).total_seconds() / 3600.0


# ---- overlapping campaigns: one merged cadence per person ------------------
# Campaigns are defined independently, so the same person can qualify for several —
# a closed-lost re-engagement AND a funding-signal play, say. Left alone, each
# campaign schedules its own 4 emails and 3 LinkedIn touches and the contact gets
# both cadences on top of each other. That is the single fastest way to burn a
# domain and a prospect.
#
# So overlapping campaigns are SEQUENCED TOGETHER: all their steps merge into one
# timeline for that person, spaced by MIN_TOUCH_GAP_DAYS, at most one touch a day.
# Spacing wins over the campaign's target window — a campaign window says when an
# account may ENTER, not that we may talk over ourselves to finish inside it.
MIN_TOUCH_GAP_DAYS = 2
MAX_TOUCHES_PER_DAY = 1


def contact_campaigns(conn, contact_id):
    """Every live campaign this contact belongs to, oldest membership first."""
    return [db._campaign_row(r) for r in conn.execute("""
        SELECT c.*, m.qualified_at AS _qualified_at
        FROM campaign_members m JOIN campaigns c USING (campaign_id)
        WHERE m.contact_id=? AND m.state != 'removed'
          AND c.status IN ('active','paused','draft')
        ORDER BY m.qualified_at
    """, (contact_id,))]


def touch_plan(conn, contact_id):
    """The single merged, de-conflicted cadence for one person.

    Every step of every campaign they belong to, ordered by intended day, then
    pushed apart so no two touches land within MIN_TOUCH_GAP_DAYS (and never two on
    one day). `day` is the intended offset, `send_day` the de-conflicted one;
    `deferred` marks the touches that moved and by how much.

    Offsets are relative to each campaign's own qualification date, so a campaign
    joined a fortnight later starts a fortnight later rather than colliding with
    touch 1 of the first.
    """
    camps = contact_campaigns(conn, contact_id)
    if not camps:
        return {"contact_id": contact_id, "campaigns": [], "touches": [], "conflicts": 0}

    base = min((c.get("_qualified_at") or "") for c in camps) or db.now()
    touches = []
    for c in camps:
        offset = _days_between(base, c.get("_qualified_at") or base)
        for s in step_plan(conn, c["campaign_id"]):
            touches.append({
                "campaign_id": c["campaign_id"], "campaign": c.get("name"),
                "channel": s["channel"], "step_no": s["step_no"],
                "cta_key": s.get("cta_key"),
                "cta_label": (s.get("cta") or {}).get("label"),
                "day": offset + (s.get("day_offset") or 0),
            })
    # Stable order: intended day, then email before LinkedIn, then campaign age.
    touches.sort(key=lambda t: (t["day"], 0 if t["channel"] == "email" else 1,
                                t["campaign_id"], t["step_no"]))

    conflicts, last_day = 0, None
    for t in touches:
        want = t["day"]
        send = want if last_day is None else max(want, last_day + MIN_TOUCH_GAP_DAYS)
        t["send_day"] = send
        t["deferred"] = send - want
        if t["deferred"] > 0:
            conflicts += 1
        last_day = send
    return {
        "contact_id": contact_id,
        "campaigns": [{"campaign_id": c["campaign_id"], "name": c.get("name")} for c in camps],
        "touches": touches,
        "conflicts": conflicts,
        "span_days": touches[-1]["send_day"] if touches else 0,
        "overlapping": len(camps) > 1,
    }


def _days_between(a, b):
    try:
        da = datetime.strptime((a or "")[:10], "%Y-%m-%d")
        dbb = datetime.strptime((b or "")[:10], "%Y-%m-%d")
    except (ValueError, TypeError):
        return 0
    return max(0, (dbb - da).days)


def overlap_summary(conn):
    """Who is in more than one live campaign — the population whose cadence had to
    be merged. Surfaced so the overlap is visible rather than silently reshuffling
    someone's sequence."""
    rows = [dict(r) for r in conn.execute("""
        SELECT m.contact_id, COUNT(DISTINCT m.campaign_id) n,
               GROUP_CONCAT(DISTINCT c.name) campaigns,
               MAX(ct.first_name) first_name, MAX(ct.last_name) last_name,
               MAX(ct.company) company
        FROM campaign_members m
        JOIN campaigns c USING (campaign_id)
        LEFT JOIN contacts ct USING (contact_id)
        WHERE m.state != 'removed' AND c.status IN ('active','paused','draft')
        GROUP BY m.contact_id HAVING n > 1
        ORDER BY n DESC
    """)]
    return {"contacts": len(rows), "sample": rows[:25]}


def render_context_prompt(plan, campaign_id):
    """The 'what else this person is getting' block for the copy prompt.

    Without it, each campaign writes as though it is the only conversation, and a
    prospect in two campaigns gets two unrelated openers both claiming to be a first
    touch. Naming the other campaign's touches lets the writer acknowledge the
    thread instead of talking over it.
    """
    others = [t for t in plan.get("touches", []) if t["campaign_id"] != campaign_id]
    if not others:
        return ""
    lines = [
        "OTHER OUTREACH THIS PERSON IS ALREADY RECEIVING (from a different campaign "
        "they also qualified for). Their touches are interleaved with yours on one "
        "shared schedule, so write as part of ONE conversation, not a fresh cold "
        "open: never re-introduce yourself as if this is first contact, never repeat "
        "an offer listed below, and do not reference the other campaign by name.",
        "",
    ]
    for t in sorted(others, key=lambda x: x["send_day"]):
        offer = f" offering {t['cta_label']}" if t.get("cta_label") else ""
        lines.append(f"- day {t['send_day']}: {t['channel']} touch{offer}")
    mine = [t for t in plan.get("touches", []) if t["campaign_id"] == campaign_id]
    if mine:
        days = ", ".join(f"day {t['send_day']}" for t in sorted(mine, key=lambda x: x["send_day"]))
        lines.append("")
        lines.append(f"Your own touches land on: {days}.")
    lines.append("")
    return "\n".join(lines)


# ---- the step -> CTA plan the generator reads ------------------------------
def step_plan(conn, campaign_id):
    """The campaign's sequence as the generator needs it: each step with the CTA it
    carries, resolved against the offer library.

    This is the inversion the campaign model exists for. Before, which offer a step
    carried lived only as prose in the prompt and was reverse-engineered afterwards
    by app.derive_cta() over the finished copy. Here the step DECLARES it, the
    generator receives it as a constraint, and derive_cta becomes a check."""
    ctas = {c["cta_key"]: c for c in db.list_ctas(conn, active_only=False)}
    plan = []
    for s in db.get_steps(conn, campaign_id):
        cta = ctas.get(s.get("cta_key"))
        plan.append({
            "step_no": s["step_no"], "channel": s["channel"],
            "day_offset": s.get("day_offset"), "angle": s.get("angle"),
            "copy_mode": s.get("copy_mode") or "generated",
            "subject": s.get("subject"), "body": s.get("body"),
            "cta_key": s.get("cta_key"),
            "cta": None if not cta else {
                "key": cta["cta_key"], "label": cta["label"], "tier": cta.get("tier"),
                "give": cta["give"], "ask": cta["ask"], "example": cta.get("example"),
                "content": cta.get("content") or [],
            },
        })
    return plan


def _render_reference(ref):
    """The proof line(s) for one CTA, or [].

    `nameable` decides whether the customer's NAME reaches the prompt at all. When
    it is off the name is not sent — not "sent with an instruction not to use it",
    because the reliable way to stop a model naming a customer it must not name is
    for the model never to see the name.
    """
    if not ref or not ref.get("story"):
        return []
    if ref.get("nameable"):
        who = ref.get("customer") or "a customer"
        rule = (f"You MAY name {who}.")
    else:
        who = ref.get("anonymous") or (
            f"a {ref['industry'].lower()} company" if ref.get("industry")
            else "an existing customer")
        rule = ("This customer has NOT agreed to be named — refer to them only as "
                f"\"{who}\" and never guess at who they are.")
    out = [f"    PROOF you may cite for this touch — {who}: {ref['story']}"]
    if ref.get("metric"):
        out.append(f"      the number: {ref['metric']}")
    if ref.get("quote") and ref.get("nameable"):
        out.append(f"      usable quote: \"{ref['quote']}\"")
    out.append(f"      {rule} Use it only if it earns its place; never stretch it "
               "beyond what it says, and never invent a second customer.")
    return out


def render_plan_prompt(plan):
    """The step->CTA plan as prompt text for generate_batch.build_user().

    Only the EMAIL track is rendered as numbered touches; LinkedIn steps follow as a
    short block, because the generated JSON keeps them in a separate object.
    Manual-copy steps are stated as fixed so the model does not rewrite them."""
    email = [s for s in plan if s["channel"] == "email"]
    li = [s for s in plan if s["channel"] == "linkedin"]
    if not email and not li:
        return ""
    lines = [
        "CAMPAIGN SEQUENCE PLAN (authoritative — overrides any default cadence in the "
        "knowledge base). Each touch below names the offer it must carry; the CTA of "
        "that touch has to be that offer's give plus its meeting ask, in your own words:",
        "",
    ]
    for s in sorted(email, key=lambda x: x["step_no"]):
        n = s["step_no"]
        if s["copy_mode"] == "manual":
            lines.append(f"- EMAIL {n}: FIXED COPY, already written. Do not change it.")
            continue
        cta = s.get("cta") or {}
        lines.append(f"- EMAIL {n}:")
        if s.get("angle"):
            lines.append(f"    job: {s['angle']}")
        if cta:
            lines.append(f"    CTA — {cta['label']}: anchor the meeting on {cta['give']}. "
                         f"Meeting ask: \"{cta['ask']}\".")
            for item in (cta.get("content") or []):
                lines.extend(_render_reference(item))
        else:
            lines.append("    CTA: no offer assigned — close on the strongest give the "
                         "knowledge base supports for this touch.")
    if li:
        lines.append("")
        lines.append("LinkedIn track:")
        for s in sorted(li, key=lambda x: x["step_no"]):
            cta = s.get("cta") or {}
            slot = {1: "li_connect", 2: "li_msg1", 3: "li_msg2"}.get(s["step_no"], f"li_{s['step_no']}")
            bit = f"    job: {s['angle']}" if s.get("angle") else ""
            lines.append(f"- {slot}:")
            if bit:
                lines.append(bit)
            if cta:
                lines.append(f"    CTA — {cta['label']}: anchor on {cta['give']}. "
                             f"Meeting ask: \"{cta['ask']}\".")
                for item in (cta.get("content") or []):
                    lines.extend(_render_reference(item))
    lines.append("")
    return "\n".join(lines)


CAMPAIGN_TYPES = ("outbound", "inbound")


def render_type_prompt(campaign):
    """The framing an INBOUND campaign needs, as prompt text.

    This is why inbound is a campaign TYPE rather than just a motion filter. The
    same account list, worked as inbound, has to be written completely differently:
    these people came to us. Opening cold at someone who filled in a form last
    Tuesday is the single most damaging thing an SDR agent can do — it tells them
    nobody is paying attention.
    """
    if (campaign or {}).get("campaign_type") != "inbound":
        return ""
    return (
        "THIS IS AN INBOUND CAMPAIGN. Every person here came to US — a form, a "
        "download, an event, or an identified visit to the site. Write accordingly:\n"
        "- NEVER open as a cold approach. No \"I came across\", no \"I noticed your "
        "company\", no introduction of who we are as though this is first contact.\n"
        "- Reference what they actually did, and be specific about it without being "
        "creepy: name the thing they engaged with, not the fact that you can see "
        "their behaviour.\n"
        "- The ask is lighter and sooner. They have already shown interest; the job "
        "is to make it easy to continue, not to earn the first reply.\n"
        "- Shorter than a cold sequence. An inbound follow-up that reads like a cold "
        "email wastes the one advantage it has.\n"
    )


def render_brief_prompt(campaign):
    """The campaign's agreed direction as a prompt block, or "".

    This is what makes the setup conversation carry through to the copy. The
    structured fields say who to work; the brief says what was DECIDED about them —
    the argument to make, the framing agreed in the room. Without it that context
    lived only in the head of whoever filled the form, and the sequence argued
    whatever the knowledge base argues by default.

    Deliberately NOT authoritative over the knowledge base the way the step plan is:
    a brief is direction, not licence to invent product claims. The sequence plan
    below it still owns the per-touch offer.
    """
    brief = (campaign or {}).get("brief")
    brief = brief.strip() if isinstance(brief, str) else ""
    if not brief:
        return ""
    return (
        "CAMPAIGN BRIEF — what was agreed for this campaign. Treat it as direction on "
        "angle, framing and emphasis, and follow it wherever it does not conflict "
        "with the knowledge base. It does NOT license product claims, numbers or "
        "proof that the knowledge base does not support, and it never overrides the "
        "offer each touch is assigned below.\n\n"
        f"{brief}\n"
    )


def campaign_for_contact(conn, contact_id):
    """The active campaign a contact belongs to, or None. Used by the generator to
    pick up the sequence plan. A contact in several campaigns takes the most
    recently qualified one."""
    r = conn.execute("""
        SELECT c.* FROM campaign_members m JOIN campaigns c USING (campaign_id)
        WHERE m.contact_id=? AND m.state!='removed'
          AND c.status IN ('active','paused','draft')
        ORDER BY m.qualified_at DESC LIMIT 1
    """, (contact_id,)).fetchone()
    return db._campaign_row(r) if r else None


# ---- CLI -------------------------------------------------------------------
def _resolve(conn, ident):
    c = None
    if str(ident).isdigit():
        c = db.get_campaign(conn, int(ident))
    return c or db.get_campaign_by_key(conn, str(ident))


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list").add_argument("--json", action="store_true")
    for name in ("show", "plan"):
        p = sub.add_parser(name)
        p.add_argument("campaign")
        p.add_argument("--json", action="store_true")
    p = sub.add_parser("qualify")
    p.add_argument("campaign")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("sweep")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--discovery-limit", type=int, default=25)
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("discover", help="scan in-scope accounts for signal (costs Prospeo credits)")
    p.add_argument("campaign")
    p.add_argument("--limit", type=int, default=25)
    p.add_argument("--kinds", help="comma-separated subset of tech,hiring")
    p.add_argument("--dry-run", action="store_true", help="list candidates, scan nothing")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("rescore", help="recompute member priorities against current signals")
    p.add_argument("campaign")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("calllist", help="priority-ordered contacts to work")
    p.add_argument("campaign", nargs="?", help="omit for a cross-campaign list")
    p.add_argument("--limit", type=int, default=50)
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("hotlist", help="the daily top-20 account report")
    p.add_argument("--size", type=int, default=HOT_LIST_SIZE)
    p.add_argument("--refresh", action="store_true", help="rebuild the snapshot now")
    p.add_argument("--json", action="store_true")
    args = ap.parse_args()

    conn = db.connect()
    db.init_schema(conn)

    if args.cmd == "list":
        rows = db.list_campaigns(conn)
        for r in rows:
            r["counts"] = db.campaign_counts(conn, r["campaign_id"])
        if args.json:
            print(json.dumps({"campaigns": rows}, ensure_ascii=False))
        else:
            for r in rows:
                print(f"{r['campaign_id']:>4}  {r['status']:<10} {r['key']:<28} "
                      f"{r['counts']['members']:>5} members / "
                      f"{r['counts']['accounts']:>4} accounts   {r['name']}")
        return 0

    if args.cmd in ("show", "plan", "qualify", "discover", "rescore") or \
            (args.cmd == "calllist" and args.campaign):
        c = _resolve(conn, args.campaign)
        if not c:
            print(f"no campaign {args.campaign!r}", file=sys.stderr)
            return 1

    if args.cmd == "show":
        out = {"campaign": c, "steps": step_plan(conn, c["campaign_id"]),
               "counts": db.campaign_counts(conn, c["campaign_id"])}
        print(json.dumps(out, ensure_ascii=False, indent=None if args.json else 2))
        return 0

    if args.cmd == "plan":
        plan = step_plan(conn, c["campaign_id"])
        print(json.dumps(plan, ensure_ascii=False) if args.json
              else render_plan_prompt(plan))
        return 0

    if args.cmd == "qualify":
        res = qualify(conn, c, commit=not args.dry_run)
        if args.json:
            print(json.dumps(res, ensure_ascii=False))
        else:
            print(f"{res['accounts_matched']} accounts showed signal in window; "
                  f"{res['candidates']} new contacts; added {res['added']}")
            for k, v in sorted(res["skipped"].items()):
                if v:
                    print(f"  skipped {k}: {v}")
        return 0

    if args.cmd == "discover":
        kinds = [k.strip() for k in (args.kinds or "").split(",") if k.strip()] or None
        if args.dry_run:
            cands = discovery_scope(conn, c, limit=args.limit, kinds=kinds)
            if args.json:
                print(json.dumps({"candidates": cands}, ensure_ascii=False))
            else:
                print(f"{len(cands)} accounts would be scanned "
                      f"(hiring scans cost one Prospeo credit each):")
                for x in cands:
                    print(f"  {x['domain']:<32} {x['contacts']:>3} contacts  {x.get('company') or ''}")
            return 0
        res = discover(conn, c, limit=args.limit, kinds=kinds,
                       progress=None if args.json else
                       (lambda d, t, dom: print(f"  [{d}/{t}] {dom}")))
        print(json.dumps(res, ensure_ascii=False))
        return 0

    if args.cmd == "rescore":
        print(json.dumps(rescore(conn, c, commit=not args.dry_run), ensure_ascii=False))
        return 0

    if args.cmd == "calllist":
        cid = c["campaign_id"] if args.campaign else None
        rows = db.campaign_members(conn, cid, state="qualified", limit=args.limit)
        if args.json:
            print(json.dumps({"call_list": rows}, ensure_ascii=False, default=str))
        else:
            for m in rows:
                name = f"{m.get('first_name') or ''} {m.get('last_name') or ''}".strip()
                score = m.get("priority_score")
                print(f"{'—' if score is None else round(score):>4} "
                      f"{(m.get('score_band') or ''):<5} {name:<26} "
                      f"{(m.get('company') or m.get('domain') or ''):<24} "
                      f"{(m.get('signal_snapshot') or {}).get('summary') or ''}"[:150])
        return 0

    if args.cmd == "hotlist":
        snap = (refresh_hot_list(conn, size=args.size) if args.refresh
                else (json.loads(HOT_LIST_PATH.read_text())
                      if HOT_LIST_PATH.is_file() else refresh_hot_list(conn, size=args.size)))
        if args.json:
            print(json.dumps(snap, ensure_ascii=False))
        else:
            print(f"Hot targets — {len(snap['accounts'])} of {snap['pool']} accounts "
                  f"(generated {snap['generated_at']})")
            for i, a in enumerate(snap["accounts"], 1):
                print(f"{i:>3}. {a['fit']:>6} {(a['company'] or a['domain'])[:28]:<30} "
                      f"{', '.join(a['reasons'])}")
        return 0

    if args.cmd == "sweep":
        res = sweep(conn, commit=not args.dry_run, discovery_limit=args.discovery_limit)
        print(json.dumps(res, ensure_ascii=False))
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
