"""Registry of the selectable reply agents.

The Replies UI shows one agent dropdown per interested reply; the chosen agent id
is persisted per lead (reply_state.json) and drives how a follow-up draft is
(re)generated:

- "prompt" agents draft synchronously via draft_followups.py.
- "pipeline" agents run a multi-stage async job (see the signal-playbook skill),
  and their drafts land in the same followup_drafts.json for the normal
  edit-before-send approval flow.
"""

AGENTS = [
    {
        "id": "standard",
        "label": "Standard Reply Agent",
        "description": "Playbook-grounded follow-up reply: deliver the give first, "
                       "one idea, one low-friction ask.",
        "kind": "prompt",
        "async": False,
    },
    {
        "id": "signal-playbook",
        "label": "Signal Playbook Reply Agent",
        "description": "Researches the lead's company, builds a personalized signal "
                       "play (HTML + PDF), hosts the PDF via HubSpot File Manager, and "
                       "drafts a reply around the link.",
        "kind": "pipeline",
        "async": True,
    },
]

DEFAULT_AGENT = "standard"


def get(agent_id):
    return next((a for a in AGENTS if a["id"] == agent_id), None)
