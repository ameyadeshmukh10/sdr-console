"""Connector inventory for the Setup view — what's wired up, and what could be.

Two jobs:

  1. Report the REAL state of everything this console actually integrates with,
     detected from config rather than hand-maintained (a checklist that drifts from
     reality is worse than none).
  2. Show the adjacent providers it does NOT integrate with yet, so Setup answers
     "what else could feed this?" instead of only "what is on today".

The distinction is explicit in the payload: `integrated: true` means code exists and
the status is measured; `integrated: false` means catalogue-only — it is a roadmap
entry, and the UI must never render it as if it were merely switched off.

Status values
  connected       credentials present (and, for OAuth, a usable token)
  expired         OAuth connected once, refresh now failing — needs reconnecting
  not_configured  we support it; no credentials found
  built_in        no credentials needed (runs in-process)
  available       catalogue-only (integrated: false)

Stdlib only. Detection never raises — a connector that can't be probed reports
`not_configured` with a reason rather than 500-ing the Setup page.
"""

# Categories, in the order Setup renders them.
CATEGORIES = [
    ("crm", "CRM & source of record"),
    ("enrichment", "Data & enrichment"),
    ("channels", "Outbound channels"),
    # Where the proof and assets a CTA play cites actually live. Separate from
    # `platform` because the question "where does our content come from?" is asked
    # by a different person, for a different reason, than "what is this deployed on".
    ("content", "Content & enablement"),
    ("platform", "Platform & infrastructure"),
]

# Brand colours for the monogram tiles. These are approximations used for tinting
# only — they are NOT official logo assets. Drop real SVGs into
# webui/frontend/src/assets/connectors/<id>.svg to replace the monogram (see
# ConnectorMark in the frontend).
_INTEGRATED = [
    # id, name, category, what it does here, detection
    ("hubspot", "HubSpot", "crm", "#FF7A59",
     "Contact source of record. ICP lists in, activity + AI SDR properties back out.",
     {"env": "HUBSPOT_ACCESS_TOKEN"}),
    ("clay", "Clay", "enrichment", "#4B3BFF",
     "Buying-group enrichment for company lists — finds the GTM contacts to target.",
     {"oauth": "clay"}),
    ("prospeo", "Prospeo", "enrichment", "#1F8A70",
     "Job-postings lookup behind the hiring signal (one credit per uncached scan).",
     {"env": "PROSPEO_API_KEY"}),
    ("technographics", "Technographics", "enrichment", "#22826F",
     "In-process DNS + website fingerprinting for the GTM stack an account runs.",
     {"builtin": True}),
    ("emailbison", "Email Bison", "channels", "#0F766E",
     "Email sequencing. The 4-touch copy is enrolled here as custom variables.",
     {"env": "EMAILBISON_API_KEY"}),
    ("heyreach", "HeyReach", "channels", "#6D5DD3",
     "LinkedIn sequencing — 3 touches per contact, replies flow back via webhook.",
     {"env": "HEYREACH_API_KEY"}),
    # Content repositories. A play's proof is usually a link, and linking works from
    # anywhere — but a CONNECTED repository is one the console can also browse and
    # pull from, which is the difference this category has to make visible.
    ("hubspot-files", "HubSpot File Manager", "content", "#FF7A59",
     "Hosts generated assets (signal plays, one-pagers) and serves them on a public "
     "URL a CTA play can cite. Uses the same private-app token as the CRM.",
     {"env": "HUBSPOT_ACCESS_TOKEN"}),
    ("anthropic", "Claude (Anthropic)", "platform", "#D97757",
     "The persona agents that research each account and write the copy.",
     {"env": "ANTHROPIC_API_KEY"}),
    ("mongodb", "MongoDB", "platform", "#00ED64",
     "Store for AI SDR deal attribution (nightly HubSpot deal sync).",
     {"env": "MONGO_URL"}),
]

# Catalogue-only: adjacent providers a deployment might want next. Ordered by how
# plausibly they'd come up in a conversation about this pipeline.
_AVAILABLE = [
    ("salesforce", "Salesforce", "crm", "#00A1E0",
     "Alternative source of record. Would replace the HubSpot pull + write-back."),
    ("dynamics", "Microsoft Dynamics", "crm", "#002050",
     "Alternative source of record for Microsoft-stack organisations."),
    ("pipedrive", "Pipedrive", "crm", "#017737",
     "Alternative source of record for smaller sales orgs."),
    # Website DE-ANONYMISATION. A distinct job from enrichment: enrichment tells you
    # more about someone you already know, this tells you WHO was on your site when
    # they never filled anything in. It is the highest-intent inbound signal there
    # is, and the one most teams have already bought — which is why it is listed
    # even though nothing is wired to it yet. Feeds the `web_deanon` signal kind.
    ("rb2b", "RB2B", "enrichment", "#111111",
     "Person-level website de-anonymisation (US). Would write identified visitors "
     "in as web_deanon signals the moment they land."),
    ("vector", "Vector", "enrichment", "#5B3DF5",
     "Person-level de-anonymisation plus intent scoring across the buying group."),
    ("warmly", "Warmly", "enrichment", "#FF6B4A",
     "Visitor de-anonymisation with an orchestration layer over the alert."),
    ("dealfront", "Dealfront (Leadfeeder)", "enrichment", "#0B5FFF",
     "Company-level de-anonymisation with strong EU/GDPR coverage."),
    ("factors", "Factors.ai", "enrichment", "#2D6AE0",
     "De-anonymisation joined to account-level intent and campaign attribution."),
    ("sixsense", "6sense", "enrichment", "#0A2540",
     "Account intent and de-anonymisation. Detected today as a technographic "
     "signal, not read from."),
    ("zoominfo", "ZoomInfo", "enrichment", "#E1523D",
     "Contact + company data. Could source net-new ICP contacts and firmographics."),
    ("apollo", "Apollo", "enrichment", "#3A26FF",
     "Contact data and intent signals. Could source contacts or verify emails."),
    ("pitchbook", "PitchBook", "enrichment", "#FF4D00",
     "Funding and M&A data — a strong buying trigger for the email 1 signal."),
    ("crunchbase", "Crunchbase", "enrichment", "#146AFF",
     "Funding events and company profiles as an alternative signal source."),
    ("cognism", "Cognism", "enrichment", "#00C2A8",
     "European contact data with stronger GDPR coverage."),
    ("clearbit", "Clearbit", "enrichment", "#2D6AE0",
     "Firmographic enrichment and website-visitor reveal."),
    ("salesnav", "LinkedIn Sales Navigator", "enrichment", "#0A66C2",
     "Account and people research, job-change alerts."),
    ("similarweb", "Similarweb", "enrichment", "#0043FF",
     "Traffic and digital-footprint signals for account prioritisation."),
    ("outreach", "Outreach", "channels", "#5952FF",
     "Alternative sequencer. Detected today as a technographic signal, not written to."),
    ("salesloft", "Salesloft", "channels", "#00B6A0",
     "Alternative sequencer. Detected today as a technographic signal."),
    ("instantly", "Instantly", "channels", "#4F46E5",
     "Alternative email sequencer."),
    ("gdrive", "Google Drive", "content", "#1FA463",
     "Case studies, decks and one-pagers in Drive. Links work today; connecting it "
     "would let the content picker browse folders instead of pasting URLs."),
    ("notion", "Notion", "content", "#111111",
     "Proof library or battlecards kept in Notion. Would sync pages into the "
     "content library rather than duplicating them."),
    ("sharepoint", "SharePoint / OneDrive", "content", "#0364B8",
     "The Microsoft-stack equivalent — where enablement collateral usually lives."),
    ("confluence", "Confluence", "content", "#172B4D",
     "Internal proof and messaging docs, for teams that keep them in the wiki."),
    ("highspot", "Highspot", "content", "#0090C8",
     "Sales enablement library. Would make governed, approved content the only "
     "content a play can cite."),
    ("seismic", "Seismic", "content", "#F04E23",
     "Enablement and content automation — same role as Highspot."),
    ("gong", "Gong", "content", "#8A56F7",
     "Call recordings and transcripts. Would feed the campaign generator directly "
     "instead of exporting a transcript by hand."),
    ("slack", "Slack", "platform", "#4A154B",
     "Notify a channel on interested replies, meetings booked, or failed sweeps."),
    ("snowflake", "Snowflake", "platform", "#29B5E8",
     "Warehouse the pipeline + reply history for BI reporting."),
]

# What a demo profile shows when it doesn't ship its own connectors.json. Default is
# EVERY integrated provider: a demo represents a working deployment, so a card
# reading "not configured" would show a broken system rather than a story. Anything
# a profile leaves out is HIDDEN, not shown as a failure (see connectors_payload).
DEMO_DEFAULT_CONNECTED = tuple(c[0] for c in _INTEGRATED)


def _probe(detect, env, project_root):
    """(status, reason) for one integrated connector. Never raises."""
    if detect.get("builtin"):
        return "built_in", None
    key = detect.get("env")
    if key:
        return ("connected", None) if (env.get(key) or "").strip() else \
               ("not_configured", f"{key} is not set")
    if detect.get("oauth") == "clay":
        try:
            import sys
            from pathlib import Path
            scripts = Path(project_root) / ".claude" / "skills" / "sdr-pipeline" / "scripts"
            if str(scripts) not in sys.path:
                sys.path.insert(0, str(scripts))
            import clay_oauth
            st = clay_oauth.status()
            if st == "connected":
                return "connected", None
            if st == "expired":
                return "expired", "OAuth token expired — reconnect required"
            return "not_configured", "not connected — authorize Clay from the Use view"
        except Exception as e:  # noqa: BLE001 — Setup must render regardless
            return "not_configured", f"status unavailable ({type(e).__name__})"
    return "not_configured", "no detection rule"


def content_repositories(env, project_root, demo_profile=None, demo_connected=None):
    """The content sources, with whether each is actually connected.

    Read by BOTH the Setup page and the CTA content picker, so the two can never
    disagree about where content may come from. The distinction the picker needs is
    narrower than "is it configured": LINKING works from any source at all, while
    BROWSING needs a repository the console can reach — so each entry says which of
    those it supports today.
    """
    payload = connectors_payload(env, project_root, demo_profile=demo_profile,
                                 demo_connected=demo_connected)
    repos = [c for c in payload["connectors"] if c["category"] == "content"]
    for r in repos:
        r["browsable"] = bool(r["integrated"]
                              and r["status"] in ("connected", "built_in"))
    return {
        "repositories": repos,
        "connected": [r["id"] for r in repos if r["browsable"]],
        # Stated once, here, rather than repeated in every surface that shows this:
        # a link is always allowed, and a repository only changes how you FIND it.
        "note": "You can link content from anywhere. Connecting a repository lets "
                "the console browse it instead of pasting a URL.",
    }


def is_integrated(connector_id):
    """True only for providers this console actually integrates with. The write
    endpoints gate on it so a catalogue-only entry can never accept a credential
    for an integration that does not exist."""
    return any(c[0] == connector_id for c in _INTEGRATED)


def test_connection(connector_id, env, project_root):
    """Actually call the provider and report what came back. {ok, detail}.

    A saved key that is wrong looks identical to a saved key that is right — the
    status is 'credentials present', not 'credentials work' — so connecting from
    the console is only useful if it can be verified. Every probe is a cheap READ:
    nothing here creates, sends or spends.

    Never raises: a failed test is a result, not an error page.
    """
    import sys
    from pathlib import Path
    for sub in ("sdr-pipeline", "email-bison", "ai-sdr"):
        d = Path(project_root) / ".claude" / "skills" / sub / "scripts"
        if d.is_dir() and str(d) not in sys.path:
            sys.path.insert(0, str(d))

    def _missing(key):
        return {"ok": False, "detail": f"{key} is not set"}

    try:
        if connector_id == "hubspot":
            if not (env.get("HUBSPOT_ACCESS_TOKEN") or "").strip():
                return _missing("HUBSPOT_ACCESS_TOKEN")
            import hubspot_client
            client = hubspot_client.HubSpotClient()
            # One property definition: the cheapest authenticated read there is, and
            # it exercises the same scopes the pipeline actually depends on.
            client.get_property("contacts", "email")
            return {"ok": True, "detail": "Authenticated against the HubSpot API."}

        if connector_id == "anthropic":
            if not (env.get("ANTHROPIC_API_KEY") or "").strip():
                return _missing("ANTHROPIC_API_KEY")
            import anthropic_client
            client = anthropic_client.AnthropicClient()
            res = client.complete("Reply with the single word: ok", "ping",
                                  max_tokens=8, timeout=30)
            return {"ok": True,
                    "detail": f"Model {client.model} responded "
                              f"({(res.get('text') or '').strip()[:20]})."}

        if connector_id == "prospeo":
            if not (env.get("PROSPEO_API_KEY") or "").strip():
                return _missing("PROSPEO_API_KEY")
            import hiring_signals
            ok, why = hiring_signals.hiring_available()
            # Deliberately does NOT run a company scan: every scan is a paid credit,
            # and a connection test that quietly bills is a bad trade.
            return {"ok": bool(ok),
                    "detail": "Key present. Not scanning a company — each lookup "
                              "costs a credit." if ok else (why or "unavailable")}

        if connector_id == "emailbison":
            if not (env.get("EMAILBISON_API_KEY") or "").strip():
                return _missing("EMAILBISON_API_KEY")
            import bison_client
            camps = bison_client.BisonClient().list_campaigns() or []
            n = len(camps.get("data", camps)) if isinstance(camps, dict) else len(camps)
            return {"ok": True, "detail": f"Reached Email Bison — {n} campaigns visible."}

        if connector_id == "heyreach":
            if not (env.get("HEYREACH_API_KEY") or "").strip():
                return _missing("HEYREACH_API_KEY")
            import heyreach_client
            # check_key is HeyReach's own auth probe — cheaper and more direct than
            # listing anything, and it fails loudly on a bad key.
            heyreach_client.HeyReachClient().check_key()
            return {"ok": True, "detail": "HeyReach API key accepted."}

        if connector_id == "mongodb":
            if not (env.get("MONGO_URL") or "").strip():
                return _missing("MONGO_URL")
            import mongo_store
            db = mongo_store.get_db()
            db.command("ping")
            return {"ok": True, "detail": f"Connected to MongoDB database '{db.name}'."}

        if connector_id == "clay":
            import clay_oauth
            st = clay_oauth.status()
            return {"ok": st == "connected",
                    "detail": {"connected": "Clay OAuth token is valid.",
                               "expired": "Token expired — reconnect Clay.",
                               }.get(st, "Not connected — authorize Clay first.")}

        if connector_id == "technographics":
            import tech_signals
            ok, why = tech_signals.tech_available()
            return {"ok": bool(ok),
                    "detail": "Runs in-process; DNS resolvers reachable." if ok
                              else (why or "unavailable")}
    except Exception as e:  # noqa: BLE001 — a failed test is a result
        return {"ok": False, "detail": f"{type(e).__name__}: {e}"[:300]}
    return {"ok": False, "detail": "no connection test for this provider"}


def connectors_payload(env, project_root, demo_profile=None, demo_connected=None):
    """Full inventory. In demo mode statuses come from the profile, not the host.

    A demo must not leak which credentials the machine running it happens to hold,
    and it should show a clean, deliberate configuration — so demo statuses are
    declared by the profile (`connectors.json` → `{"connected": [...]}`), defaulting
    to DEMO_DEFAULT_CONNECTED.
    """
    demo = demo_profile is not None
    connected_ids = set(demo_connected if demo_connected is not None
                        else DEMO_DEFAULT_CONNECTED) if demo else set()

    items = []
    for cid, name, cat, color, blurb, detect in _INTEGRATED:
        if demo and detect.get("builtin"):
            # Runs in-process with no credential, so a profile can't turn it "off"
            # without saying something untrue about how the system works.
            status, reason = "built_in", None
        elif demo:
            # Omitted from the profile = not part of this story. Drop the card
            # entirely rather than render a failure state in a demo.
            if cid not in connected_ids:
                continue
            status, reason = "connected", None
        else:
            status, reason = _probe(detect, env, project_root)
        items.append({
            "id": cid, "name": name, "category": cat, "color": color,
            "blurb": blurb, "integrated": True, "status": status, "reason": reason,
        })
    for cid, name, cat, color, blurb in _AVAILABLE:
        items.append({
            "id": cid, "name": name, "category": cat, "color": color,
            "blurb": blurb, "integrated": False, "status": "available", "reason": None,
        })

    live = [i for i in items if i["integrated"]]
    return {
        "demo": demo_profile,
        "categories": [{"id": c, "label": lbl} for c, lbl in CATEGORIES],
        "connectors": items,
        "summary": {
            "connected": sum(1 for i in live if i["status"] in ("connected", "built_in")),
            "integrated": len(live),
            "available": sum(1 for i in items if not i["integrated"]),
            "needs_attention": [i["id"] for i in live if i["status"] == "expired"],
        },
        "note": "Statuses are detected from configuration, not hand-maintained. "
                "'Available' entries are not integrations yet — nothing is wired to them.",
    }
