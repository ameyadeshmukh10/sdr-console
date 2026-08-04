"""Chat-driven config editing — propose, diff, approve.

The Setup view can tell you exactly which file controls a behaviour. This turns
that into an edit path that doesn't require touching the repo: describe the change
(or upload a doc), Claude proposes a concrete rewrite of the whitelisted file(s),
and you approve or reject a real unified diff. Nothing is written on the strength
of the conversation alone.

Design rules, in order of importance:

  1. **Whitelist only.** A scope names exact paths. A proposal for anything else is
     rejected outright — the model never chooses which file to write.
  2. **Diff before write.** propose() only computes; apply() is a separate, explicit
     call against a stored proposal.
  3. **Reversible.** apply() snapshots the previous content under the data volume and
     appends an audit entry, so any change can be undone even after a restart.
  4. **Scoped by risk.** Prose files (the offer, the CTA library) degrade copy quality
     if a change is bad. The ICP filter and linter change WHO gets contacted — those
     are marked high-risk and stay non-editable here until the flow has earned trust.

Persistence caveat, surfaced to the UI rather than hidden: `.claude/` ships inside
the Docker image, so an applied edit lives until the next redeploy. Every apply also
emits a patch the change can be committed from. See `persistence_note()`.

Stdlib only apart from a lazy import of the shared Anthropic client (boot rule: the
server must start with no API key).
"""

import difflib
import json
import os
import sys
import threading
import time
from pathlib import Path

MAX_INSTRUCTION = 4000
MAX_ATTACH_CHARS = 40000        # per attachment, after decode
MAX_ATTACHMENTS = 5
# A proposal that deletes most of a file is more likely a truncated generation than
# an intended rewrite. Not blocked — flagged, so the diff gets a closer read.
SHRINK_WARN_RATIO = 0.7

# scope id -> what it is, which files it owns, and whether chat may edit it.
# `editable: False` entries still render their paths so the UI can explain why not.
SCOPES = {
    "knowledge": {
        "label": "Knowledge base",
        "paths": [".claude/skills/ai-sdr/knowledge/offer.md"],
        "editable": True,
        "risk": "low",
        "affects": "the product story and the proof points copy may cite. A bad edit "
                   "degrades copy quality; it cannot change who gets contacted.",
    },
    "sequencing": {
        "label": "Sequencing & CTA offers",
        "paths": [".claude/skills/ai-sdr/knowledge/icp-email.md",
                  ".claude/skills/ai-sdr/knowledge/cta-offers.md"],
        "editable": False,
        "risk": "medium",
        "affects": "the touch structure and CTA library. Edits here ripple into the "
                   "persona agents and the linter's expectations.",
    },
    "icp": {
        "label": "ICP filter",
        "paths": [".claude/skills/ai-sdr/scripts/buyer_group.py"],
        "editable": False,
        "risk": "high",
        "affects": "WHO gets contacted. Executable code, and a wrong pattern silently "
                   "widens or narrows targeting.",
    },
    "guardrails": {
        "label": "Guardrails",
        "paths": [".claude/skills/ai-sdr/scripts/lint_sequence.py"],
        "editable": False,
        "risk": "high",
        "affects": "the checks that gate enrollment. Weakening one lets bad copy out.",
    },
}

_PROPOSALS = {}                 # id -> proposal dict (in-process; short-lived)
_LOCK = threading.Lock()
_SEQ = [0]

SYSTEM = """You edit configuration files for an autonomous outbound (AI SDR) system.

You are given the CURRENT contents of one or more files and a change request. Return
the COMPLETE new contents for every file you change — never a fragment, never a diff.

Rules:
- Preserve the file's existing structure, heading style, and voice. These files are
  read by other agents at generation time; layout is load-bearing.
- Change only what the request calls for. Do not reformat, reorder, or "improve"
  untouched sections.
- Never invent facts, metrics, customer names or claims. If the request implies a
  claim you have not been given, say so in `notes` and leave it out.
- If the request is ambiguous or you would have to guess at something material, make
  no change to that file and explain what you need in `notes`.

Return ONLY a JSON object:
{"files":[{"path":"<exact path given>","new_content":"<full file>","summary":"<one line>"}],
 "notes":"<what you did, what you deliberately left out, anything you need>"}

Omit a file from `files` entirely if you are not changing it."""


def _root(project_root):
    return Path(project_root).resolve()


def scope_paths(project_root, scope):
    spec = SCOPES.get(scope)
    if not spec:
        return []
    return [_root(project_root) / rel for rel in spec["paths"]]


def _safe_path(project_root, scope, rel):
    """Resolve `rel` only if it is exactly one of the scope's whitelisted paths."""
    spec = SCOPES.get(scope)
    if not spec:
        return None
    rel = str(rel).strip().lstrip("/")
    if rel not in spec["paths"]:
        return None
    p = (_root(project_root) / rel).resolve()
    # Defence in depth: even a whitelisted string must land inside the repo.
    if not str(p).startswith(str(_root(project_root)) + os.sep):
        return None
    return p


def read_scope(project_root, scope):
    """Current contents of a scope's files. Missing files report as None."""
    out = []
    spec = SCOPES.get(scope)
    if not spec:
        return out
    for rel in spec["paths"]:
        p = _root(project_root) / rel
        try:
            content = p.read_text() if p.is_file() else None
        except OSError:
            content = None
        out.append({"path": rel, "exists": content is not None,
                    "content": content, "lines": len((content or "").splitlines())})
    return out


def scopes_payload(project_root, history_dir):
    return {
        "scopes": [
            {"id": sid, "label": s["label"], "paths": s["paths"],
             "editable": s["editable"], "risk": s["risk"], "affects": s["affects"],
             "files": [{"path": f["path"], "exists": f["exists"], "lines": f["lines"]}
                       for f in read_scope(project_root, sid)]}
            for sid, s in SCOPES.items()
        ],
        "available": bool((os.environ.get("ANTHROPIC_API_KEY") or "").strip()),
        "persistence": persistence_note(),
        "history": recent_history(history_dir, limit=10),
    }


def persistence_note():
    """Whether an applied edit survives a redeploy, stated plainly."""
    return {
        "durable": False,
        "note": "Applied edits are written to the running container's filesystem. "
                "`.claude/` ships inside the Docker image, so a redeploy resets it — "
                "commit the emitted patch to make a change permanent.",
    }


def unified_diff(path, before, after):
    return "".join(difflib.unified_diff(
        (before or "").splitlines(keepends=True),
        (after or "").splitlines(keepends=True),
        fromfile=f"a/{path}", tofile=f"b/{path}", n=3))


def _new_id():
    with _LOCK:
        _SEQ[0] += 1
        return f"prop-{int(time.time())}-{_SEQ[0]}"


def propose(project_root, scope, instruction, attachments=None):
    """Ask the model for a rewrite and return a diff. Writes nothing.

    Raises ValueError for anything the caller got wrong (unknown/non-editable scope,
    oversized input) and RuntimeError if the model is unavailable or unusable.
    """
    spec = SCOPES.get(scope)
    if not spec:
        raise ValueError(f"unknown config scope {scope!r}")
    if not spec["editable"]:
        raise ValueError(
            f"{spec['label']} is not editable from chat — {spec['affects']}")
    instruction = (instruction or "").strip()
    if not instruction and not attachments:
        raise ValueError("describe the change, or attach a file to work from")
    if len(instruction) > MAX_INSTRUCTION:
        raise ValueError(f"instruction is too long (max {MAX_INSTRUCTION} chars)")

    attachments = attachments or []
    if len(attachments) > MAX_ATTACHMENTS:
        raise ValueError(f"at most {MAX_ATTACHMENTS} attachments per request")
    att_blocks = []
    for a in attachments:
        name = str(a.get("name") or "attachment")[:120]
        text = str(a.get("text") or "")
        if len(text) > MAX_ATTACH_CHARS:
            raise ValueError(f"{name} is too large (max {MAX_ATTACH_CHARS} chars of text)")
        if not text.strip():
            raise ValueError(f"{name} contained no readable text")
        att_blocks.append(f"<attachment name=\"{name}\">\n{text}\n</attachment>")

    files = read_scope(project_root, scope)
    present = [f for f in files if f["exists"]]
    if not present:
        raise RuntimeError(f"none of the {spec['label']} files exist on disk")

    file_blocks = "\n\n".join(
        f"<file path=\"{f['path']}\">\n{f['content']}\n</file>" for f in present)
    user = (f"{file_blocks}\n\n"
            + ("\n\n".join(att_blocks) + "\n\n" if att_blocks else "")
            + f"<request>\n{instruction or 'Incorporate the attached material.'}\n</request>")

    client_mod = _anthropic()
    try:
        client = client_mod.AnthropicClient()
    except Exception as e:  # noqa: BLE001 — surfaced as a clean 501 upstream
        raise RuntimeError(str(e))
    res = client.complete(SYSTEM, user, max_tokens=16000, timeout=300)
    try:
        parsed = client_mod.extract_json(res["text"])
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(f"the model did not return usable JSON: {e}")

    before_by_path = {f["path"]: f["content"] for f in files}
    changes, warnings = [], []
    for item in (parsed.get("files") or []):
        rel = item.get("path")
        target = _safe_path(project_root, scope, rel)
        if target is None:
            # The model tried to write somewhere it wasn't given. Drop it loudly.
            warnings.append(f"ignored a proposed change to {rel!r} — outside this section")
            continue
        after = item.get("new_content")
        if not isinstance(after, str) or not after.strip():
            warnings.append(f"ignored an empty rewrite of {rel}")
            continue
        before = before_by_path.get(rel) or ""
        if after == before:
            continue
        if before and len(after) < len(before) * SHRINK_WARN_RATIO:
            warnings.append(
                f"{rel} shrinks by {round(100 * (1 - len(after) / len(before)))}% — "
                "check the diff for a truncated rewrite before approving")
        diff = unified_diff(rel, before, after)
        changes.append({
            "path": rel, "summary": (item.get("summary") or "").strip(),
            "diff": diff, "new_content": after,
            "added": sum(1 for l in diff.splitlines() if l.startswith("+") and not l.startswith("+++")),
            "removed": sum(1 for l in diff.splitlines() if l.startswith("-") and not l.startswith("---")),
        })

    proposal = {
        "id": _new_id(), "scope": scope, "instruction": instruction,
        "attachments": [a.get("name") for a in attachments],
        "notes": (parsed.get("notes") or "").strip(),
        "changes": changes, "warnings": warnings,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "usage": res.get("usage", {}),
    }
    if changes:
        with _LOCK:
            _PROPOSALS[proposal["id"]] = proposal
    return proposal


def get_proposal(pid):
    with _LOCK:
        return _PROPOSALS.get(pid)


def apply_proposal(project_root, history_dir, pid, actor=None):
    """Write an approved proposal, snapshotting what it replaced."""
    prop = get_proposal(pid)
    if not prop:
        raise ValueError("that proposal has expired — generate a new one")
    if not prop["changes"]:
        raise ValueError("this proposal contains no changes")

    hist = Path(history_dir) / prop["id"]
    hist.mkdir(parents=True, exist_ok=True)
    written, patch_parts = [], []
    for ch in prop["changes"]:
        target = _safe_path(project_root, prop["scope"], ch["path"])
        if target is None:                       # re-checked at write time
            raise ValueError(f"refusing to write outside the section: {ch['path']}")
        before = target.read_text() if target.is_file() else ""
        # Snapshot BEFORE writing, flat-named so a revert never has to walk dirs.
        (hist / (ch["path"].replace("/", "__") + ".before")).write_text(before)
        target.write_text(ch["new_content"])
        written.append(ch["path"])
        patch_parts.append(ch["diff"])

    entry = {
        "id": prop["id"], "scope": prop["scope"], "actor": actor,
        "instruction": prop["instruction"], "attachments": prop["attachments"],
        "notes": prop["notes"], "files": written,
        "applied_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "reverted": False,
    }
    (hist / "patch.diff").write_text("".join(patch_parts))
    _append_audit(history_dir, entry)
    with _LOCK:
        _PROPOSALS.pop(pid, None)
    return {"ok": True, "applied": entry, "patch": "".join(patch_parts),
            "persistence": persistence_note()}


def revert(project_root, history_dir, entry_id, actor=None):
    """Restore the snapshot an apply replaced."""
    entries = {e["id"]: e for e in _read_audit(history_dir)}
    entry = entries.get(entry_id)
    if not entry:
        raise ValueError("no such change in the audit log")
    if entry.get("reverted"):
        raise ValueError("that change was already reverted")
    hist = Path(history_dir) / entry_id
    restored = []
    for rel in entry["files"]:
        snap = hist / (rel.replace("/", "__") + ".before")
        target = _safe_path(project_root, entry["scope"], rel)
        if target is None or not snap.is_file():
            continue
        target.write_text(snap.read_text())
        restored.append(rel)
    if not restored:
        raise ValueError("the snapshot for that change is missing — cannot revert")
    _append_audit(history_dir, {
        **entry, "reverted": True, "actor": actor,
        "reverted_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    })
    return {"ok": True, "restored": restored}


# --- audit log (append-only jsonl on the data volume) ------------------------

def _audit_path(history_dir):
    return Path(history_dir) / "audit.jsonl"


def _append_audit(history_dir, entry):
    p = _audit_path(history_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def _read_audit(history_dir):
    p = _audit_path(history_dir)
    if not p.is_file():
        return []
    out = []
    for line in p.read_text().splitlines():
        if not line.strip():
            continue
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def recent_history(history_dir, limit=10):
    """Latest state per change id, newest first (a revert appends a second row)."""
    latest = {}
    for e in _read_audit(history_dir):
        latest[e["id"]] = e
    rows = sorted(latest.values(), key=lambda e: e.get("applied_at") or "", reverse=True)
    return rows[:limit]


def _anthropic():
    """Lazy import — the server must boot with no API key and no client present."""
    scripts = _root(Path(__file__).resolve().parents[2]) / ".claude" / "skills" / "ai-sdr" / "scripts"
    if str(scripts) not in sys.path:
        sys.path.insert(0, str(scripts))
    import anthropic_client
    return anthropic_client
