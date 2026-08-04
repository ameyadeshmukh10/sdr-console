"""Drop a CSV or Excel file into the campaign builder — an event list, a webinar
export, a conference scan — and turn it into a working audience.

This is the third way contacts get into the console, and it fills a real gap:

    pull        a HubSpot list we already have          (hubspot_pull.py)
    enrich      find buyers we DON'T have, via Clay     (campaigns.enrich)
    import      a list that exists only as a file       (here)

The file is the input the other two cannot cover. Nobody's CRM has the badge scans
from last week's conference until somebody puts them there, and the window where
that list is worth working is measured in days.

What it does, in order: parse (stdlib only — see `_parse_xlsx`), map columns to the
contact shape, dedup against what the pipeline already holds, then hand the net-new
rows to `source_contacts.py`. That last step is the important reuse: it is already
the deterministic create path — dedup against HubSpot by email, the ICP/persona
gate, contact creation, a static list, pipeline ingest — and it is idempotent. So
"drop a file" gets CRM creation for free and cannot drift from how Clay-sourced
contacts are created.

The import is RECORDED (`contact_imports` / `contact_import_members`), which is what
lets a campaign point an audience at it: `{"type": "upload", "import_id": 3}` means
"the people from the SaaStr list", forever, rather than a set of ids pasted into a
JSON blob.

Both halves of the enrichment story then work on it: the file is net-new supply, and
the accounts it lands on are ones enrichment can go and complete the buying group
at — the imported name is usually one person at a company where five matter.

Preview writes nothing. Import is the explicit second call.
"""

import base64
import csv
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path
from xml.etree import ElementTree

import batch_db as db

MAX_BYTES = 8 * 1024 * 1024      # 8 MB — a contact list is text, not a data lake
MAX_ROWS = 20000
PREVIEW_ROWS = 8

# Target field -> header patterns, most specific first. Work email beats personal;
# "company name" beats a bare "company" only because both map to the same field.
FIELD_PATTERNS = [
    ("email", (r"^work[ _-]?e-?mail", r"^e-?mail", r"e-?mail[ _-]?address",
               r"^business[ _-]?e-?mail")),
    ("first_name", (r"^first[ _-]?name", r"^given[ _-]?name", r"^fname$", r"^first$")),
    ("last_name", (r"^last[ _-]?name", r"^surname", r"^family[ _-]?name",
                   r"^lname$", r"^last$")),
    ("name", (r"^full[ _-]?name", r"^name$", r"^contact[ _-]?name", r"^attendee$")),
    ("title", (r"^job[ _-]?title", r"^title$", r"^position", r"^role$", r"^headline")),
    ("company", (r"^company", r"^organi[sz]ation", r"^account[ _-]?name", r"^employer")),
    ("domain", (r"^domain", r"^website", r"^company[ _-]?url", r"^web[ _-]?site")),
    ("linkedin_url", (r"linked-?in", r"^li[ _-]?url$", r"^profile[ _-]?url")),
    ("phone", (r"^mobile", r"^phone", r"^direct[ _-]?dial", r"^cell", r"^tel")),
]
FIELDS = [f for f, _ in FIELD_PATTERNS]


class ImportError_(ValueError):
    """Bad file or bad mapping — surfaced as a 400, never a 500."""


# ---- parsing ---------------------------------------------------------------
def parse(filename, content_b64):
    """(headers, rows) from a base64 CSV/TSV/XLSX payload. Rows are dicts by header.

    One transport for both formats: a spreadsheet is binary and has to arrive
    base64'd anyway, and a second text path would just be a second thing to keep
    working.
    """
    name = (filename or "upload").strip()
    try:
        raw = base64.b64decode(content_b64 or "", validate=False)
    except Exception:  # noqa: BLE001
        raise ImportError_("could not decode the uploaded file")
    if not raw:
        raise ImportError_("the file is empty")
    if len(raw) > MAX_BYTES:
        raise ImportError_(f"file is too large (max {MAX_BYTES // (1024 * 1024)} MB)")

    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    if ext in ("xlsx", "xlsm"):
        return _parse_xlsx(raw)
    if ext == "xls":
        # The old binary format is not a zip and has no stdlib reader. Say so
        # precisely — "unsupported file" would send someone hunting for the bug in
        # their data instead of re-saving the sheet.
        raise ImportError_(
            "legacy .xls isn't supported — re-save it as .xlsx or export a CSV")
    return _parse_csv(raw)


def _parse_csv(raw):
    text = None
    for enc in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        raise ImportError_("could not read the file as text — is it a CSV?")
    sample = text[:8000]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    reader = csv.reader(io.StringIO(text), dialect)
    rows = []
    headers = None
    for row in reader:
        if headers is None:
            # Skip leading blank/decorative lines — exports from event platforms
            # routinely open with a title row.
            if not any(str(c).strip() for c in row):
                continue
            headers = _dedupe_headers(row)
            continue
        if len(rows) >= MAX_ROWS:
            break
        if not any(str(c).strip() for c in row):
            continue
        rows.append({h: (row[i].strip() if i < len(row) else "")
                     for i, h in enumerate(headers)})
    if not headers:
        raise ImportError_("no header row found")
    return headers, rows


_NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}


def _parse_xlsx(raw):
    """Read the first worksheet of an .xlsx with zipfile + ElementTree.

    An xlsx is a zip of XML, so this needs no third-party reader — which matters:
    the backend is stdlib-only by design (see CLAUDE.md), and adding openpyxl to
    accept a spreadsheet would be a new dependency in the deploy for a parser this
    size. Handles shared strings, inline strings and dates-as-numbers, which is
    everything a contact list actually contains.
    """
    try:
        zf = zipfile.ZipFile(io.BytesIO(raw))
    except zipfile.BadZipFile:
        raise ImportError_("that doesn't look like a valid .xlsx file")
    with zf:
        shared = []
        if "xl/sharedStrings.xml" in zf.namelist():
            root = ElementTree.fromstring(zf.read("xl/sharedStrings.xml"))
            for si in root.findall("m:si", _NS):
                shared.append("".join(t.text or "" for t in si.iter(
                    "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}t")))
        sheets = sorted(n for n in zf.namelist()
                        if n.startswith("xl/worksheets/sheet") and n.endswith(".xml"))
        if not sheets:
            raise ImportError_("the workbook has no worksheets")
        root = ElementTree.fromstring(zf.read(sheets[0]))

    grid = []
    for row in root.iter("{%s}row" % _NS["m"]):
        cells = {}
        for c in row.findall("m:c", _NS):
            col = re.match(r"[A-Z]+", c.get("r") or "A").group(0)
            t = c.get("t")
            if t == "inlineStr":
                node = c.find("m:is", _NS)
                val = "".join(x.text or "" for x in node.iter(
                    "{%s}t" % _NS["m"])) if node is not None else ""
            else:
                v = c.find("m:v", _NS)
                val = v.text if v is not None else ""
                if t == "s" and val not in (None, ""):
                    try:
                        val = shared[int(val)]
                    except (ValueError, IndexError):
                        val = ""
            cells[col] = (val or "").strip()
        grid.append(cells)
        if len(grid) > MAX_ROWS + 1:
            break

    # Drop leading blank rows, then take the first non-empty row as headers.
    while grid and not any(grid[0].values()):
        grid.pop(0)
    if not grid:
        raise ImportError_("the sheet is empty")
    head_cells = grid[0]
    cols = sorted(head_cells, key=_col_index)
    headers = _dedupe_headers([head_cells.get(c, "") for c in cols])
    rows = []
    for cells in grid[1:]:
        if not any(cells.values()):
            continue
        rows.append({h: cells.get(cols[i], "") for i, h in enumerate(headers)})
    return headers, rows


def _col_index(col):
    n = 0
    for ch in col:
        n = n * 26 + (ord(ch) - 64)
    return n


def _dedupe_headers(row):
    """Non-empty, unique header names — a duplicate column would otherwise silently
    overwrite the first one when rows are built as dicts."""
    out, seen = [], {}
    for i, h in enumerate(row):
        name = str(h or "").strip() or f"column {i + 1}"
        if name in seen:
            seen[name] += 1
            name = f"{name} ({seen[name]})"
        else:
            seen[name] = 1
        out.append(name)
    return out


# ---- mapping ---------------------------------------------------------------
def auto_map(headers):
    """{field: header} guessed from the header names. Only ever a SUGGESTION — it is
    returned to the UI for confirmation, because a wrong email column silently
    imports a list of nobody."""
    mapping = {}
    used = set()
    for field, patterns in FIELD_PATTERNS:
        for pat in patterns:
            hit = next((h for h in headers
                        if h not in used and re.search(pat, h.strip(), re.I)), None)
            if hit:
                mapping[field] = hit
                used.add(hit)
                break
    return mapping


def normalize_rows(rows, mapping):
    """Apply the mapping and return contact-shaped dicts (source_contacts' shape)."""
    mapping = {k: v for k, v in (mapping or {}).items() if k in FIELDS and v}
    if not mapping.get("email"):
        raise ImportError_("map a column to Email — a contact without one can't be "
                           "created or sequenced")
    out = []
    for r in rows:
        get = lambda f: str(r.get(mapping.get(f) or "", "") or "").strip()  # noqa: E731
        first, last = get("first_name"), get("last_name")
        if not first and mapping.get("name"):
            parts = get("name").split()
            first, last = (parts[0] if parts else ""), " ".join(parts[1:])
        email = get("email").lower()
        domain = get("domain").lower()
        domain = re.sub(r"^https?://(www\.)?", "", domain).split("/")[0]
        if not domain and "@" in email:
            domain = email.rsplit("@", 1)[-1]
        out.append({
            "first_name": first, "last_name": last, "email": email,
            "title": get("title"), "company": get("company"), "domain": domain,
            "linkedin_url": get("linkedin_url"), "phone": get("phone"),
        })
    return out


# ---- preview ---------------------------------------------------------------
def preview(conn, filename, content_b64, mapping=None, project_root=None):
    """Everything the import WOULD do, having written nothing.

    The counts are the point. "482 rows" is not a decision; "310 have an email, 244
    pass the ICP gate, 190 are people we don't already hold" is.
    """
    headers, rows = parse(filename, content_b64)
    if not rows:
        raise ImportError_("the file has a header row but no data")
    mapping = mapping or auto_map(headers)
    stats = {"rows": len(rows)}
    contacts = normalize_rows(rows, mapping) if mapping.get("email") else []

    seen, uniq = set(), []
    for c in contacts:
        if not c["email"] or "@" not in c["email"] or c["email"] in seen:
            continue
        seen.add(c["email"])
        uniq.append(c)
    stats["with_email"] = len(uniq)
    stats["no_email"] = len(contacts) - len(uniq)

    # ICP gate — the same buyer-group ruleset enrichment and the pull use, so the
    # file is held to the identical standard rather than a laxer one.
    icp, non_icp = [], 0
    persona_for = _persona_fn(project_root)
    for c in uniq:
        persona = persona_for(c["title"])
        if not persona:
            non_icp += 1
            continue
        c["persona"] = persona
        icp.append(c)
    stats["icp"] = len(icp)
    stats["non_icp"] = non_icp

    held = {str(r["contact_id"]): r for r in conn.execute(
        "SELECT contact_id, email FROM contacts WHERE email IS NOT NULL")}
    by_email = {(r["email"] or "").lower() for r in held.values()}
    stats["already_in_pipeline"] = sum(1 for c in icp if c["email"] in by_email)
    stats["net_new"] = len(icp) - stats["already_in_pipeline"]
    stats["accounts"] = len({c["domain"] for c in icp if c["domain"]})

    return {
        "headers": headers, "mapping": mapping, "fields": FIELDS,
        "stats": stats,
        "sample": [{**c, "in_pipeline": c["email"] in by_email}
                   for c in icp[:PREVIEW_ROWS]],
        "unmapped": [h for h in headers if h not in set(mapping.values())],
    }


def _persona_fn(project_root):
    """buyer_group.persona_for_title, or a permissive fallback.

    Falling back to 'everyone is ICP' rather than 'nobody is' is deliberate: if the
    classifier can't be loaded, dropping the user's whole file silently is the worse
    failure. The enroll-time gates still apply downstream."""
    try:
        scripts = Path(project_root or ".") / ".claude" / "skills" / "ai-sdr" / "scripts"
        if str(scripts) not in sys.path:
            sys.path.insert(0, str(scripts))
        import buyer_group_config as bgc
        import buyer_group

        def persona(title):
            try:
                conn = db.connect()
                try:
                    return bgc.persona_for_title(conn, title)
                finally:
                    conn.close()
            except Exception:  # noqa: BLE001
                return buyer_group.persona_for_title(title)
        # Probe once; a broken ruleset should degrade now, not per row.
        persona("VP Sales")
        return persona
    except Exception:  # noqa: BLE001
        return lambda title: "sales-leadership"


# ---- commit ----------------------------------------------------------------
def commit(conn, filename, content_b64, mapping, label=None, project_root=None,
           scripts_dir=None, create_in_crm=True):
    """Import for real: create in the CRM + pipeline, and record the import.

    Delegates the create to source_contacts.py rather than re-implementing dedup and
    contact creation — that path is already idempotent, already ICP-gated and
    already the one Clay-sourced contacts go through. Re-importing the same file
    therefore adds nobody twice.
    """
    pv = preview(conn, filename, content_b64, mapping, project_root)
    headers, rows = parse(filename, content_b64)
    contacts = normalize_rows(rows, pv["mapping"])
    seen, candidates = set(), []
    persona_for = _persona_fn(project_root)
    for c in contacts:
        if not c["email"] or "@" not in c["email"] or c["email"] in seen:
            continue
        seen.add(c["email"])
        if not persona_for(c["title"]):
            continue
        candidates.append(c)
    if not candidates:
        raise ImportError_(
            "nothing in this file passed the ICP filter — check the Title column is "
            "mapped, since the buyer-group rules read job titles")

    label = (label or Path(filename).stem or "Imported list").strip()[:120]
    summary = {}
    if create_in_crm:
        script = str(Path(scripts_dir) / "source_contacts.py")
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
            json.dump(candidates, fh)
            path = fh.name
        try:
            out = subprocess.run(
                [sys.executable, script, path, "--list-name", f"AI SDR — {label}"],
                capture_output=True, text=True, timeout=1800)
            lines = [l for l in (out.stdout or "").splitlines() if l.strip()]
            try:
                summary = json.loads(lines[-1]) if lines else {}
            except json.JSONDecodeError:
                raise ImportError_(
                    f"import failed: {(out.stderr or out.stdout or '').strip()[:300]}")
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass

    # Resolve the imported people back to contact ids so the audience can name them.
    emails = [c["email"] for c in candidates]
    ids = _ids_for_emails(conn, emails)
    import_id = record_import(conn, label, filename, source="file",
                              rows=len(rows), matched=len(ids),
                              contact_ids=ids,
                              detail={"stats": pv["stats"], "source_contacts": summary})
    return {
        "import_id": import_id, "label": label,
        "stats": pv["stats"], "source": summary,
        "contacts": len(ids),
        # Ids the CRM path created but that have not landed in the pipeline yet are
        # reported rather than hidden: it is the difference between "imported 190"
        # and "imported 190, 12 of which you cannot sequence".
        "not_in_pipeline": max(0, len(candidates) - len(ids)),
        "audience": {"type": "upload", "import_id": import_id, "label": label},
    }


def _ids_for_emails(conn, emails):
    out = []
    for i in range(0, len(emails), 400):
        chunk = [e.lower() for e in emails[i:i + 400]]
        q = ",".join("?" * len(chunk))
        out += [str(r["contact_id"]) for r in conn.execute(
            f"SELECT contact_id FROM contacts WHERE lower(email) IN ({q})", chunk)]
    return sorted(set(out))


# ---- the import ledger -----------------------------------------------------
def record_import(conn, label, filename, source="file", rows=0, matched=0,
                  contact_ids=(), detail=None):
    db.init_schema(conn)
    cur = conn.execute(
        "INSERT INTO contact_imports (label, filename, source, rows, matched, "
        "detail, created_at) VALUES (?,?,?,?,?,?,?)",
        (label, filename, source, int(rows), int(matched),
         json.dumps(detail or {}), db.now()))
    import_id = cur.lastrowid
    conn.executemany(
        "INSERT OR IGNORE INTO contact_import_members (import_id, contact_id) VALUES (?,?)",
        [(import_id, cid) for cid in contact_ids])
    conn.commit()
    return import_id


def list_imports(conn, limit=50):
    try:
        db.init_schema(conn)
        return [dict(r) for r in conn.execute(
            "SELECT i.*, (SELECT COUNT(*) FROM contact_import_members m "
            "WHERE m.import_id = i.import_id) AS contacts "
            "FROM contact_imports i ORDER BY i.import_id DESC LIMIT ?", (int(limit),))]
    except Exception:  # noqa: BLE001 — an old DB with no table reports none
        return []


def import_contact_ids(conn, import_id):
    return [str(r["contact_id"]) for r in conn.execute(
        "SELECT contact_id FROM contact_import_members WHERE import_id=?",
        (int(import_id),))]


def import_label(conn, import_id):
    r = conn.execute("SELECT label FROM contact_imports WHERE import_id=?",
                     (int(import_id),)).fetchone()
    return r["label"] if r else None
