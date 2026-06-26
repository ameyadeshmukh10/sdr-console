"""Clay MCP OAuth 2.1 (+ PKCE + dynamic client registration + refresh).

Clay's official MCP server (https://api.clay.com/v3/mcp) is OAuth-only — there is
no static API key. This module runs the full browser authorization-code flow once
and then keeps a long-running backend authenticated by silently refreshing the
access token from the stored refresh token. Pure stdlib (urllib), same
zero-dependency philosophy as hubspot_client.py / anthropic_client.py.

Flow used by the web UI (webui/server/app.py):
  start_authorization()  -> authorize URL the user opens in a browser
  handle_callback(code, state) -> exchanges code, persists tokens
  get_access_token()     -> a valid bearer (auto-refreshes), for the MCP connector
  status()               -> "disconnected" | "connected" | "expired"

Token store: data/outreach/clay_oauth.json (gitignored — holds secrets).
Env overrides: CLAY_MCP_URL, CLAY_OAUTH_REDIRECT.
"""

import base64
import hashlib
import json
import os
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

DEFAULT_MCP_URL = "https://api.clay.com/v3/mcp"
DEFAULT_REDIRECT = "http://localhost:8787/api/clay/oauth/callback"
# Refresh a little before the token actually expires so in-flight calls don't 401.
EXPIRY_BUFFER_SECONDS = 120
PROJECT_ROOT = Path(__file__).resolve().parents[4]
TOKEN_STORE = PROJECT_ROOT / "data" / "outreach" / "clay_oauth.json"


def _load_dotenv():
    here = Path(__file__).resolve()
    for parent in [here.parent, *here.parents]:
        env_path = parent / ".env"
        if env_path.is_file():
            for raw in env_path.read_text().splitlines():
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                os.environ.setdefault(k.strip(), v.split(" #")[0].strip().strip('"').strip("'"))
            return


def mcp_url():
    _load_dotenv()
    return os.environ.get("CLAY_MCP_URL") or DEFAULT_MCP_URL


def redirect_uri(override=None):
    """The OAuth callback URL Clay redirects the browser back to.

    Precedence: an explicit CLAY_OAUTH_REDIRECT env var (operator override, used
    for local dev) wins; else the per-request `override` derived from the host the
    console is actually served from (so a deployed app redirects back to itself,
    not to localhost); else the localhost default as a last resort.
    """
    _load_dotenv()
    return os.environ.get("CLAY_OAUTH_REDIRECT") or override or DEFAULT_REDIRECT


class ClayAuthError(RuntimeError):
    pass


class ClayNotConnected(ClayAuthError):
    """No stored tokens / refresh failed — the user must (re)authorize."""


# ---------------------------------------------------------------------------
# token store
# ---------------------------------------------------------------------------
def _load_store():
    if TOKEN_STORE.is_file():
        try:
            return json.loads(TOKEN_STORE.read_text())
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def _save_store(store):
    TOKEN_STORE.parent.mkdir(parents=True, exist_ok=True)
    TOKEN_STORE.write_text(json.dumps(store, indent=2))
    try:
        os.chmod(TOKEN_STORE, 0o600)  # tokens are secrets
    except OSError:
        pass


# ---------------------------------------------------------------------------
# small HTTP helpers
# ---------------------------------------------------------------------------
def _http(method, url, *, headers=None, data=None, timeout=30):
    req = urllib.request.Request(url, data=data, method=method)
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, dict(resp.headers), resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read().decode("utf-8", "replace")


def _get_json(url, timeout=30):
    status, _, body = _http("GET", url, headers={"Accept": "application/json"}, timeout=timeout)
    if status >= 400:
        raise ClayAuthError(f"GET {url} -> HTTP {status}: {body[:200]}")
    return json.loads(body) if body else {}


def _post_form(url, form, timeout=30):
    data = urllib.parse.urlencode(form).encode()
    status, _, body = _http(
        "POST", url, data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
        timeout=timeout)
    if status >= 400:
        raise ClayAuthError(f"POST {url} -> HTTP {status}: {body[:300]}")
    return json.loads(body) if body else {}


def _post_json(url, obj, timeout=30):
    data = json.dumps(obj).encode()
    status, _, body = _http(
        "POST", url, data=data,
        headers={"Content-Type": "application/json", "Accept": "application/json"}, timeout=timeout)
    if status >= 400:
        raise ClayAuthError(f"POST {url} -> HTTP {status}: {body[:300]}")
    return json.loads(body) if body else {}


# ---------------------------------------------------------------------------
# OAuth discovery (RFC 9728 protected-resource metadata -> RFC 8414 AS metadata)
# ---------------------------------------------------------------------------
def _origin(url):
    p = urllib.parse.urlparse(url)
    return f"{p.scheme}://{p.netloc}"


def _discover():
    """Resolve the authorization-server metadata for the Clay MCP endpoint."""
    base = mcp_url()
    origin = _origin(base)

    # 1. Protected-resource metadata. Try the WWW-Authenticate hint first, then the
    #    well-known path on the resource origin.
    as_url = None
    status, headers, _ = _http("POST", base, headers={"Accept": "application/json"},
                               data=b"{}", timeout=20)
    www = headers.get("WWW-Authenticate") or headers.get("www-authenticate") or ""
    for part in www.split(","):
        part = part.strip()
        if part.startswith("resource_metadata="):
            rm = part.split("=", 1)[1].strip().strip('"')
            try:
                pr = _get_json(rm)
                servers = pr.get("authorization_servers") or []
                if servers:
                    as_url = servers[0]
            except ClayAuthError:
                pass
    if not as_url:
        try:
            pr = _get_json(f"{origin}/.well-known/oauth-protected-resource")
            servers = pr.get("authorization_servers") or []
            if servers:
                as_url = servers[0]
        except ClayAuthError:
            pass
    as_url = as_url or origin  # last resort: AS lives at the resource origin

    # 2. Authorization-server metadata (oauth first, then openid fallback).
    as_origin = _origin(as_url)
    for path in ("/.well-known/oauth-authorization-server",
                 "/.well-known/openid-configuration"):
        try:
            meta = _get_json(as_origin + path)
            if meta.get("authorization_endpoint") and meta.get("token_endpoint"):
                return meta
        except ClayAuthError:
            continue
    raise ClayAuthError(f"could not discover OAuth metadata for {base}")


def _register_client(meta, store, want_redirect):
    """Dynamic client registration (RFC 7591).

    A registered client is bound to a fixed set of redirect_uris, so we must
    (re)register whenever the redirect we're about to use isn't one the stored
    client was registered for — otherwise the authorization server rejects the
    authorize request with a redirect_uri mismatch. We register both the desired
    redirect and the localhost default so one client covers prod + local dev.
    """
    registered = store.get("redirect_uris") or []
    if store.get("client_id") and want_redirect in registered:
        return store["client_id"], store.get("client_secret")
    reg = meta.get("registration_endpoint")
    if not reg:
        if store.get("client_id"):  # can't re-register; hope the existing client fits
            return store["client_id"], store.get("client_secret")
        raise ClayAuthError("no registration_endpoint and no pre-set CLAY client_id")
    redirect_uris = sorted({want_redirect, DEFAULT_REDIRECT})
    body = {
        "client_name": "SDR Console",
        "redirect_uris": redirect_uris,
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "none",  # public client + PKCE
    }
    if meta.get("scopes_supported"):
        body["scope"] = " ".join(meta["scopes_supported"])
    res = _post_json(reg, body)
    store["client_id"] = res.get("client_id")
    if res.get("client_secret"):
        store["client_secret"] = res["client_secret"]
    if not store["client_id"]:
        raise ClayAuthError(f"client registration returned no client_id: {res}")
    store["redirect_uris"] = redirect_uris
    return store["client_id"], store.get("client_secret")


# ---------------------------------------------------------------------------
# PKCE helpers
# ---------------------------------------------------------------------------
def _b64url(raw):
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _new_pkce():
    verifier = _b64url(secrets.token_bytes(48))
    challenge = _b64url(hashlib.sha256(verifier.encode()).digest())
    return verifier, challenge


# ---------------------------------------------------------------------------
# public API
# ---------------------------------------------------------------------------
def start_authorization(redirect_override=None):
    """Begin the auth-code+PKCE flow. Returns the authorize URL to open in a browser.
    Persists the pending PKCE verifier + state + the exact redirect_uri used, so the
    token exchange in handle_callback() can reuse it byte-for-byte (OAuth requires the
    authorize and token redirect_uri to match)."""
    store = _load_store()
    redirect = redirect_uri(redirect_override)
    meta = _discover()
    client_id, _ = _register_client(meta, store, redirect)
    verifier, challenge = _new_pkce()
    state = secrets.token_urlsafe(24)

    store["meta"] = {"authorization_endpoint": meta["authorization_endpoint"],
                     "token_endpoint": meta["token_endpoint"]}
    store["pending"] = {"state": state, "verifier": verifier,
                        "redirect_uri": redirect, "created_at": time.time()}
    _save_store(store)

    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": state,
    }
    if meta.get("scopes_supported"):
        params["scope"] = " ".join(meta["scopes_supported"])
    return f"{meta['authorization_endpoint']}?{urllib.parse.urlencode(params)}"


def handle_callback(code, state):
    """Exchange the authorization code for tokens and persist them."""
    store = _load_store()
    pending = store.get("pending") or {}
    if not pending or not code:
        raise ClayAuthError("no pending authorization (start the flow again)")
    if state and pending.get("state") and state != pending["state"]:
        raise ClayAuthError("state mismatch — possible CSRF, aborting")

    form = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": pending.get("redirect_uri") or redirect_uri(),
        "client_id": store["client_id"],
        "code_verifier": pending["verifier"],
    }
    if store.get("client_secret"):
        form["client_secret"] = store["client_secret"]
    tok = _post_form(store["meta"]["token_endpoint"], form)
    _store_tokens(store, tok)
    store.pop("pending", None)
    _save_store(store)
    return True


def _store_tokens(store, tok):
    store["access_token"] = tok.get("access_token")
    if tok.get("refresh_token"):  # some servers omit it on refresh
        store["refresh_token"] = tok["refresh_token"]
    expires_in = tok.get("expires_in")
    store["expires_at"] = (time.time() + float(expires_in)) if expires_in else None
    return store


def _refresh(store):
    if not store.get("refresh_token"):
        raise ClayNotConnected("no refresh token — reauthorize")
    form = {
        "grant_type": "refresh_token",
        "refresh_token": store["refresh_token"],
        "client_id": store.get("client_id", ""),
    }
    if store.get("client_secret"):
        form["client_secret"] = store["client_secret"]
    try:
        tok = _post_form(store["meta"]["token_endpoint"], form)
    except ClayAuthError as e:
        raise ClayNotConnected(f"refresh failed: {e}") from e
    _store_tokens(store, tok)
    _save_store(store)
    return store


def get_access_token():
    """Return a valid Clay access token, refreshing if needed. Raises
    ClayNotConnected if there are no usable credentials."""
    store = _load_store()
    if not store.get("access_token") and not store.get("refresh_token"):
        raise ClayNotConnected("Clay is not connected — authorize first")
    exp = store.get("expires_at")
    if store.get("access_token") and (exp is None or exp - EXPIRY_BUFFER_SECONDS > time.time()):
        return store["access_token"]
    store = _refresh(store)
    return store["access_token"]


def status():
    """disconnected | connected | expired (refresh would be required & failed)."""
    store = _load_store()
    if not store.get("refresh_token") and not store.get("access_token"):
        return "disconnected"
    exp = store.get("expires_at")
    if store.get("access_token") and (exp is None or exp - EXPIRY_BUFFER_SECONDS > time.time()):
        return "connected"
    try:
        _refresh(store)
        return "connected"
    except ClayNotConnected:
        return "expired"


if __name__ == "__main__":
    import sys
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    if cmd == "status":
        print(status())
    elif cmd == "start":
        print(start_authorization())
    elif cmd == "token":
        print(get_access_token())
    else:
        print(f"unknown command: {cmd}", file=sys.stderr)
        raise SystemExit(2)
