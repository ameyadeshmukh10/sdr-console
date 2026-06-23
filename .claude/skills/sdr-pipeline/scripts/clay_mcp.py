"""Minimal direct client for Clay's MCP server (Streamable HTTP + JSON-RPC 2.0).

Drops the LLM from the enrichment loop: instead of asking a model to relay each
Clay tool call, we speak MCP directly over HTTPS using the OAuth bearer from
clay_oauth. Firing a find task and polling it become plain HTTP round-trips
(~0.5s) instead of a model turn (~several seconds) — which removes the bulk of
the per-company overhead. Pure stdlib (urllib), same philosophy as clay_oauth.

  mcp = ClayMCP()                       # initializes the MCP session
  data = mcp.call_tool(name, arguments) # returns the parsed JSON text payload

One ClayMCP/session is safe to share across threads: every call_tool() is an
independent request/response HTTP POST that carries the session header.
"""

import json
import threading
import urllib.error
import urllib.request

import clay_oauth

PROTOCOL_VERSION = "2025-06-18"


class ClayMCPError(RuntimeError):
    pass


class ClayMCP:
    def __init__(self, timeout=60):
        self.url = clay_oauth.mcp_url()
        self.token = clay_oauth.get_access_token()  # raises ClayNotConnected if unusable
        self.timeout = timeout
        self.session_id = None
        self._id_lock = threading.Lock()
        self._next_id = 0
        self._initialize()

    # ---- transport -------------------------------------------------------
    def _post(self, body):
        req = urllib.request.Request(self.url, data=json.dumps(body).encode(), method="POST")
        req.add_header("Authorization", f"Bearer {self.token}")
        req.add_header("Content-Type", "application/json")
        req.add_header("Accept", "application/json, text/event-stream")
        req.add_header("MCP-Protocol-Version", PROTOCOL_VERSION)
        if self.session_id:
            req.add_header("Mcp-Session-Id", self.session_id)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return resp.status, dict(resp.headers), resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            return e.code, dict(e.headers), e.read().decode("utf-8", "replace")

    @staticmethod
    def _parse(body):
        """Clay answers with either a JSON body or an SSE stream; handle both."""
        body = body.strip()
        if not body:
            return {}
        if body.startswith("{"):
            return json.loads(body)
        for line in reversed(body.splitlines()):  # last data: frame with a result/error
            if line.startswith("data:"):
                try:
                    obj = json.loads(line[5:].strip())
                except json.JSONDecodeError:
                    continue
                if "result" in obj or "error" in obj:
                    return obj
        return {}

    def _rpc(self, method, params=None, notify=False):
        body = {"jsonrpc": "2.0", "method": method}
        if not notify:
            with self._id_lock:
                self._next_id += 1
                body["id"] = self._next_id
        if params is not None:
            body["params"] = params
        status, headers, raw = self._post(body)
        if notify:
            return headers, None
        if status >= 400:
            raise ClayMCPError(f"{method} -> HTTP {status}: {raw[:200]}")
        return headers, self._parse(raw)

    # ---- handshake + tool calls -----------------------------------------
    def _initialize(self):
        headers, res = self._rpc("initialize", {
            "protocolVersion": PROTOCOL_VERSION, "capabilities": {},
            "clientInfo": {"name": "sdr-console", "version": "1.0"}})
        self.session_id = headers.get("Mcp-Session-Id") or headers.get("mcp-session-id")
        if (res or {}).get("error"):
            raise ClayMCPError(f"initialize error: {res['error']}")
        self._rpc("notifications/initialized", notify=True)

    def call_tool(self, name, arguments):
        """Call an MCP tool and return the parsed JSON payload from its text content."""
        _, res = self._rpc("tools/call", {"name": name, "arguments": arguments})
        if (res or {}).get("error"):
            raise ClayMCPError(f"{name} error: {res['error']}")
        result = (res or {}).get("result") or {}
        if result.get("isError"):
            raise ClayMCPError(f"{name} tool error: {json.dumps(result.get('content'))[:200]}")
        for blk in result.get("content") or []:
            if blk.get("type") == "text":
                try:
                    return json.loads(blk["text"])
                except json.JSONDecodeError:
                    return {"_text": blk["text"]}
        return {}
