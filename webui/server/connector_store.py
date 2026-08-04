"""Connector credentials that can be set from the console, and survive a redeploy.

Before this, connecting a system meant editing Railway service variables and
waiting for a redeploy — fine for the person who set the project up, impossible for
anyone else, and invisible from the product. Setup could tell you a connector was
not configured and then had nothing to offer.

Where the values live, and why
------------------------------
`<DATA>/connectors/credentials.json`, i.e. the **Railway volume**. This is the one
writable location that outlives a deploy: `.claude/` and the rest of the image are
replaced on every push (the same caveat config_edit.py surfaces for its edits), and
the app cannot write its own service variables. So the volume is the only honest
answer to "connect it from the UI and have it still be connected tomorrow".

Precedence: **stored value wins over the environment.** The opposite rule would be
safer-sounding and worse in practice — someone rotating a key in the console while
a stale Railway variable existed would watch the UI accept it and change nothing.
The payload always reports which source is in force, so an env-set value that is
being overridden is visible rather than mysterious.

Security posture, stated plainly
--------------------------------
* Secrets are stored in PLAINTEXT on the volume, readable by this process. That is
  the same trust level as the environment variables they replace — anything able to
  read this file can already read `os.environ` — but it is a real property and the
  UI says so.
* The file is written 0600 and its directory 0700.
* Values are NEVER returned by any read path. `describe()` yields presence, length
  and a masked hint (`sk-…4f2a`) only, so no endpoint can echo a key back to a
  browser, into a log, or into a demo.
* `apply_to_environ()` pushes stored values into `os.environ` so subprocesses
  (`run_script`) and lazily-imported clients pick them up without a restart.

Demo mode never reaches this module: a demo writes only inside its own profile and
must not learn, store or display real credentials.

Stdlib only.
"""

import json
import os
import re
import threading
from pathlib import Path

_LOCK = threading.RLock()
STORE_DIRNAME = "connectors"
STORE_FILENAME = "credentials.json"

# Keys THIS module put into os.environ. Needed because the store also writes
# through to the process environment: without it, every stored key would then read
# back as "also set in the environment" and every card would claim to be overriding
# a deploy variable that does not exist.
_INJECTED = set()

# Each field a connector needs, in the order the form renders them. `secret` fields
# are masked on read and never echoed; non-secret ones (a base URL, a workspace id)
# are shown in full because hiding them helps nobody and makes debugging harder.
FIELDS = {
    "hubspot": [
        {"key": "HUBSPOT_ACCESS_TOKEN", "label": "Private app access token",
         "secret": True, "required": True,
         "help": "HubSpot → Settings → Integrations → Private Apps. Needs contacts, "
                 "companies and deals read/write, plus sales-email-read for AI SDR "
                 "attribution."},
        {"key": "HUBSPOT_PORTAL_ID", "label": "Portal ID", "secret": False,
         "required": False,
         "help": "Only used to turn contact names into links into your CRM."},
    ],
    "prospeo": [
        {"key": "PROSPEO_API_KEY", "label": "API key", "secret": True, "required": True,
         "help": "Powers the hiring signal. One credit per uncached company scan."},
    ],
    "emailbison": [
        {"key": "EMAILBISON_API_KEY", "label": "API key", "secret": True, "required": True},
        {"key": "EMAILBISON_BASE_URL", "label": "Base URL", "secret": False,
         "required": False, "help": "Only if you're on a dedicated instance."},
        {"key": "BISON_CAMPAIGN_ID", "label": "Default campaign ID", "secret": False,
         "required": False,
         "help": "Fallback for contacts not routed by a console campaign."},
    ],
    "heyreach": [
        {"key": "HEYREACH_API_KEY", "label": "API key", "secret": True, "required": True},
        {"key": "HEYREACH_CAMPAIGN_ID", "label": "Default campaign ID", "secret": False,
         "required": False},
        {"key": "HEYREACH_WEBHOOK_SECRET", "label": "Webhook secret", "secret": True,
         "required": False,
         "help": "Shared secret HeyReach signs inbound activity with."},
    ],
    "anthropic": [
        {"key": "ANTHROPIC_API_KEY", "label": "API key", "secret": True, "required": True,
         "help": "The persona agents that research accounts and write the copy."},
        {"key": "CLAUDE_MODEL", "label": "Model", "secret": False, "required": False,
         "help": "Leave blank for the default."},
    ],
    "mongodb": [
        {"key": "MONGO_URL", "label": "Connection string", "secret": True, "required": True,
         "help": "Only used by the nightly AI SDR deal-attribution sync."},
    ],
}

# Connectors whose connection is an OAuth handshake or an in-process engine — there
# is no key to type, so the form must not pretend otherwise.
NO_FIELDS = {"clay": "oauth", "technographics": "built_in"}


def _dir(data_root):
    return Path(data_root) / STORE_DIRNAME


def _path(data_root):
    return _dir(data_root) / STORE_FILENAME


def load(data_root):
    """{KEY: value} as stored. Never raises — an unreadable store means 'nothing
    stored', which degrades to environment-only config rather than a dead console."""
    p = _path(data_root)
    if not p.is_file():
        return {}
    try:
        data = json.loads(p.read_text())
        return {str(k): str(v) for k, v in (data.get("values") or {}).items()}
    except (ValueError, OSError):
        return {}


def _write(data_root, values):
    d = _dir(data_root)
    d.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(d, 0o700)
    except OSError:
        pass
    p = _path(data_root)
    tmp = p.with_suffix(".tmp")
    # Atomic: a half-written credentials file read by a concurrent request would
    # look like a connector spontaneously disconnecting.
    tmp.write_text(json.dumps({"values": values}, indent=2))
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    tmp.replace(p)


def save(data_root, updates):
    """Merge in new values and push them into os.environ. Returns the keys written.

    An empty string CLEARS a key (and unsets the override) — the natural way to
    express "remove this" from a text field. A value of None leaves the key alone,
    so a form that only submits what changed cannot blank the rest."""
    written = []
    with _LOCK:
        values = load(data_root)
        for key, val in (updates or {}).items():
            key = str(key).strip()
            if not key or not re.match(r"^[A-Z][A-Z0-9_]{1,60}$", key):
                continue
            if val is None:
                continue
            val = str(val).strip()
            if val:
                values[key] = val
                os.environ[key] = val
                _INJECTED.add(key)
            else:
                values.pop(key, None)
                _INJECTED.discard(key)
                # Only drop the process value if WE put it there; a real deploy-time
                # env var must survive clearing a stored override.
                os.environ.pop(key, None)
            written.append(key)
        _write(data_root, values)
    return written


def clear(data_root, keys):
    return save(data_root, {k: "" for k in keys})


def apply_to_environ(data_root):
    """Load stored values into os.environ at boot, BEFORE anything reads config.

    Stored wins over the environment (see the module docstring): a key someone set
    in the console must take effect even when a stale deploy variable exists, or
    the console silently lies about what it just saved."""
    values = load(data_root)
    for k, v in values.items():
        os.environ[k] = v
        _INJECTED.add(k)
    return len(values)


def mask(value):
    """A hint, never the secret: enough to tell two keys apart, not to use one."""
    v = str(value or "")
    if len(v) <= 8:
        return "•" * len(v)
    return f"{v[:3]}…{v[-4:]}"


def describe(data_root, env, connector_id):
    """Field metadata plus PRESENCE for one connector. Never returns a secret."""
    fields = FIELDS.get(connector_id)
    if not fields:
        return None
    stored = load(data_root)
    out = []
    for f in fields:
        key = f["key"]
        in_store = key in stored
        raw = str(stored.get(key) or (env or {}).get(key) or "").strip()
        # A key we injected is not evidence of a deploy variable — see _INJECTED.
        env_raw = "" if key in _INJECTED else str((env or {}).get(key) or "").strip()
        out.append({
            **f,
            "set": bool(raw),
            "source": "console" if in_store else ("environment" if raw else None),
            # Non-secret values are shown so they can be edited without retyping;
            # secrets only ever leave as a mask.
            "value": (mask(raw) if f["secret"] else raw) if raw else "",
            "overrides_env": bool(in_store and env_raw),
        })
    return out


def configurable(connector_id):
    return connector_id in FIELDS
