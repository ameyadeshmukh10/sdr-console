"""Shared ICP buyer-group classifier (go-forward strategy).

We sell the AI SDR packages exclusively, to a TIGHT buyer group at B2B tech startups:
GTM leadership — CRO; VP/Head/Director of Sales/BD/GTM/Revenue; Sales Managers/
Analysts/Ops; SDR/BDR Managers/Leads — plus Head of Marketing ONLY when they own
pipeline/SDRs. Everyone else (founders/CEOs, finance, eng, HR, product, consultants,
account/partnerships, junior marketing) is NOT-ICP.

`buyer_role(title)` -> (role, is_icp). `is_icp_buyer(title)` -> bool.

Note: pure Founder/CEO titles are NOT-ICP by the user's explicit buyer-group definition
(GTM leadership only). Flip FOUNDERS_ARE_ICP if that changes.
"""

import re

FOUNDERS_ARE_ICP = False

NOT_ICP = "NOT-ICP"

# Function signals
_CHIEF_REV = re.compile(r"\bcro\b|\bcso\b|\bcco\b|chief (revenue|sales|commercial|growth) officer", re.I)
_SDR_BDR = re.compile(r"\bsdr\b|\bbdr\b|sales development|sales dev\b", re.I)
_REVOPS = re.compile(r"revenue operations|\brevops\b|rev\s?ops|sales operations|sales ops|\bgtm ops\b", re.I)
_PARTNERSHIPS = re.compile(r"\bpartnership|\bpartner(s)?\b|\balliances?\b|\bchannel\b|\becosystem\b", re.I)
_SALES_FUNC = re.compile(r"\bsales\b|\brevenue\b|\brevops\b|revenue operations|\bgtm\b|"
                         r"go[\s-]?to[\s-]?market|business development|\bbiz dev\b|\bcommercial\b", re.I)
_MKTG_FUNC = re.compile(r"\bmarketing\b|\bcmo\b|demand gen|demand generation|\bgrowth\b", re.I)

# Seniority signals
_LEADERSHIP = re.compile(r"\bchief\b|\bvp\b|\bevp\b|\bsvp\b|vice president|\bhead\b|"
                         r"\bdirector\b|\bdir\b|\bcmo\b|\bcro\b|\bcso\b", re.I)
_ICOPS = re.compile(r"\bmanager\b|\bmgr\b|\blead\b|\banalyst\b|operations|\bops\b|"
                    r"\benablement\b|\bspecialist\b|\bexecutive\b|\brep\b|representative|\bexec\b", re.I)
_FOUNDER = re.compile(r"\bfounder\b|co-?founder", re.I)


def buyer_role(title):
    t = " ".join((title or "").lower().split())
    if not t:
        return (NOT_ICP, False)

    # 1. Revenue/Sales chief
    if _CHIEF_REV.search(t):
        return ("CRO / Sales Chief", True)

    # 2. SDR / BDR (pipeline-gen front line, any level)
    if _SDR_BDR.search(t):
        return ("SDR/BDR", True)

    # 3. RevOps / Sales Ops (checked before generic sales so "Sales Operations" routes here)
    if _REVOPS.search(t):
        return ("RevOps/Sales Ops", True)

    # 4. Partnerships / alliances / channel
    if _PARTNERSHIPS.search(t):
        return ("Partnerships", True)

    # 5. Sales / Revenue / GTM / BD / Commercial
    if _SALES_FUNC.search(t):
        if _LEADERSHIP.search(t):
            return ("VP/Head/Dir Sales-GTM", True)
        return ("Sales/BD IC & Ops", True)

    # 6. Marketing — ICP ONLY at leadership level (owns pipeline/SDRs)
    if _MKTG_FUNC.search(t):
        if _LEADERSHIP.search(t):
            return ("Marketing-pipeline", True)
        return (NOT_ICP, False)

    # 7. Founders/CEOs (no revenue function) — NOT-ICP by definition
    if _FOUNDER.search(t) or "ceo" in t or "chief executive" in t:
        return ("Founder/CEO", FOUNDERS_ARE_ICP)

    return (NOT_ICP, False)


def is_icp_buyer(title):
    return buyer_role(title)[1]


# Role -> outreach persona (which subagent writes the copy). None = skip (no persona).
_PERSONA_BY_ROLE = {
    "CRO / Sales Chief": "sales-leadership",
    "VP/Head/Dir Sales-GTM": "sales-leadership",
    "Sales/BD IC & Ops": "sales-leadership",
    "RevOps/Sales Ops": "revops",
    "Partnerships": "partnerships",
    "SDR/BDR": "sdr-bdr",
    "Marketing-pipeline": None,   # ICP but no persona selected yet
    "Founder/CEO": None,
    NOT_ICP: None,
}


def persona_for_title(title):
    """Return the outreach persona for a title, or None to skip this contact."""
    return _PERSONA_BY_ROLE.get(buyer_role(title)[0])


if __name__ == "__main__":
    import sys
    for line in sys.stdin:
        title = line.rstrip("\n")
        role, icp = buyer_role(title)
        persona = persona_for_title(title) or "-"
        print(f"{'ICP ' if icp else '    '} {role:22s} persona={persona:16s} | {title}")
