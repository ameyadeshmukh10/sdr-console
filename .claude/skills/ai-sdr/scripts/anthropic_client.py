"""Shared Anthropic Messages API client.

Pure stdlib (urllib) so it runs with no `pip install` — same zero-dependency
philosophy as bison_client.py. Used for SDR copy generation (with the server-side
web_search tool) and interested-reply classification (no tools).

  from anthropic_client import AnthropicClient, extract_json, AnthropicError
  c = AnthropicClient()                      # key/model from env
  r = c.complete(system="...", user="...", use_web_search=True)
  data = extract_json(r["text"])             # robust JSON extraction

Env: ANTHROPIC_API_KEY (required), CLAUDE_MODEL (default claude-opus-4-8).
CLI self-test:  python3 anthropic_client.py "say hi as json {\"ok\":true}"
"""

import json
import os
import re
import time
import urllib.error
import urllib.request
from pathlib import Path

API_URL = "https://api.anthropic.com/v1/messages"
BATCH_URL = "https://api.anthropic.com/v1/messages/batches"
ANTHROPIC_VERSION = "2023-06-01"
DEFAULT_MODEL = "claude-opus-4-8"
# Server-side web search tool (model runs the search internally; no client loop).
WEB_SEARCH_TOOL_TYPE = "web_search_20250305"
# Remote MCP connector beta — lets the Messages API call tools on a remote MCP
# server (e.g. Clay) server-side; results come back as mcp_tool_result blocks.
MCP_CONNECTOR_BETA = "mcp-client-2025-04-04"


def _load_dotenv():
    """Load KEY=VALUE lines from a .env walking up from this script.

    Real environment variables always win over .env values.
    """
    here = Path(__file__).resolve()
    for parent in [here.parent, *here.parents]:
        env_path = parent / ".env"
        if env_path.is_file():
            for raw in env_path.read_text().splitlines():
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key, value = key.strip(), value.split(" #")[0].strip().strip('"').strip("'")
                os.environ.setdefault(key, value)
            return


class AnthropicError(RuntimeError):
    pass


class AnthropicJSONError(AnthropicError):
    """The model's output could not be parsed as JSON."""


class AnthropicClient:
    def __init__(self, api_key=None, model=None):
        _load_dotenv()
        self.api_key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        if not self.api_key:
            raise AnthropicError(
                "ANTHROPIC_API_KEY is not set. Add it to the .env at the project "
                "root or export it in your shell."
            )
        self.model = model or os.environ.get("CLAUDE_MODEL") or DEFAULT_MODEL

    def complete(self, system, user, *, use_web_search=False, max_web_searches=3,
                 max_tokens=4096, temperature=None, timeout=180, cache_system=True):
        """One Messages API turn. Returns dict with the final text + usage.

        With use_web_search the model researches server-side and returns the final
        assistant turn — no client-side tool loop is required.

        temperature is omitted from the request unless explicitly set — newer
        models (e.g. Opus 4.8) reject an explicit temperature.

        cache_system (default True) sends the system prompt as an ephemeral cached
        block. The system prefix (incl. the tools block) is then written to cache
        once and read back at 0.1x on every subsequent call with the same prefix
        within ~5 min — e.g. every contact in a batch and every retry. Prompts
        under the model's minimum (1024 tokens for Opus 4.8) are silently not
        cached, no error.
        """
        body = self.build_body(system, user, use_web_search=use_web_search,
                               max_web_searches=max_web_searches, max_tokens=max_tokens,
                               temperature=temperature, cache_system=cache_system)
        payload = self._post(body, timeout=timeout)
        return parse_message(payload)

    def build_body(self, system, user, *, use_web_search=False, max_web_searches=3,
                   max_tokens=4096, temperature=None, cache_system=True, cache_ttl="5m"):
        """Build a Messages-API request body. Reused by complete() (sync) and the
        batch path (as each request's `params`). cache_ttl="1h" uses the 1-hour
        cache (better hit rate for async batches that run >5 min)."""
        if cache_system and system:
            cc = {"type": "ephemeral"}
            if cache_ttl == "1h":
                cc["ttl"] = "1h"
            sys_field = [{"type": "text", "text": system, "cache_control": cc}]
        else:
            sys_field = system
        body = {
            "model": self.model,
            "max_tokens": max_tokens,
            "system": sys_field,
            "messages": [{"role": "user", "content": user}],
        }
        if temperature is not None:
            body["temperature"] = temperature
        if use_web_search:
            body["tools"] = [{
                "type": WEB_SEARCH_TOOL_TYPE,
                "name": "web_search",
                "max_uses": max_web_searches,
            }]
        return body

    def complete_with_mcp(self, system, user, mcp_servers, *, model=None,
                          max_tokens=4096, timeout=180, max_turns=12):
        """One logical turn that may call tools on remote MCP servers.

        mcp_servers = [{"type":"url","url":...,"name":...,"authorization_token":...}].
        Tool calls execute server-side; on a `pause_turn` stop_reason we resend the
        accumulated assistant content to continue (bounded by max_turns). Returns
        the parsed final text + usage (same shape as complete()).
        """
        body = {
            "model": model or self.model,
            "max_tokens": max_tokens,
            "system": system,
            "messages": [{"role": "user", "content": user}],
            "mcp_servers": mcp_servers,
        }
        headers = {"anthropic-beta": MCP_CONNECTOR_BETA}
        payload = self._post(body, timeout=timeout, extra_headers=headers)
        turns = 1
        while payload.get("stop_reason") == "pause_turn" and turns < max_turns:
            body["messages"].append({"role": "assistant", "content": payload.get("content", [])})
            payload = self._post(body, timeout=timeout, extra_headers=headers)
            turns += 1
        return parse_message(payload)

    # ---- Message Batches API ----------------------------------------------
    def create_batch(self, requests):
        """Submit a batch. requests = [{"custom_id","params"}]. Returns the batch obj."""
        return self._post({"requests": requests}, timeout=120, url=BATCH_URL)

    def get_batch(self, batch_id):
        return self._get(f"{BATCH_URL}/{batch_id}", timeout=60)

    def cancel_batch(self, batch_id):
        return self._post({}, timeout=60, url=f"{BATCH_URL}/{batch_id}/cancel")

    def get_batch_results(self, results_url):
        """Yield {custom_id, result} for each line of the .jsonl results file."""
        raw = self._get(results_url, timeout=300, raw=True)
        for line in raw.splitlines():
            line = line.strip()
            if line:
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    continue

    def _headers(self, req):
        req.add_header("x-api-key", self.api_key)
        req.add_header("anthropic-version", ANTHROPIC_VERSION)

    def _get(self, url, timeout=60, raw=False):
        last_err = None
        for attempt in range(5):
            try:
                req = urllib.request.Request(url, method="GET")
                self._headers(req)
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    text = resp.read().decode("utf-8")
                return text if raw else (json.loads(text) if text else {})
            except urllib.error.HTTPError as e:
                detail = e.read().decode("utf-8", "replace")[:400]
                if e.code in (429, 500, 502, 503, 504, 529) and attempt < 4:
                    time.sleep(min(2 ** attempt, 20))
                    last_err = AnthropicError(f"HTTP {e.code}: {detail}")
                    continue
                raise AnthropicError(f"HTTP {e.code}: {detail}") from e
            except urllib.error.URLError as e:
                if attempt < 4:
                    time.sleep(min(2 ** attempt, 20))
                    last_err = AnthropicError(f"Network error: {e.reason}")
                    continue
                raise AnthropicError(f"Network error: {e.reason}") from e
        if last_err:
            raise last_err

    def _post(self, body, timeout, url=API_URL, extra_headers=None):
        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(url, data=data, method="POST")
        req.add_header("x-api-key", self.api_key)
        req.add_header("anthropic-version", ANTHROPIC_VERSION)
        req.add_header("content-type", "application/json")
        for k, v in (extra_headers or {}).items():
            req.add_header(k, v)

        last_err = None
        for attempt in range(5):
            try:
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    raw = resp.read().decode("utf-8")
                return json.loads(raw) if raw else {}
            except urllib.error.HTTPError as e:
                detail = e.read().decode("utf-8", "replace")[:400]
                # 429 rate limit, 529 overloaded, 5xx transient -> backoff + retry
                if e.code in (429, 500, 502, 503, 504, 529) and attempt < 4:
                    time.sleep(min(2 ** attempt, 20))
                    last_err = AnthropicError(f"HTTP {e.code}: {detail}")
                    continue
                raise AnthropicError(f"HTTP {e.code}: {detail}") from e
            except urllib.error.URLError as e:
                if attempt < 4:
                    time.sleep(min(2 ** attempt, 20))
                    last_err = AnthropicError(f"Network error: {e.reason}")
                    continue
                raise AnthropicError(f"Network error: {e.reason}") from e
        if last_err:
            raise last_err


def parse_message(payload):
    """Extract the final text + usage from a Messages response (or a batch
    result's `message` object)."""
    text_parts, web_search_count = [], 0
    for block in payload.get("content", []):
        btype = block.get("type")
        if btype == "text":
            text_parts.append(block.get("text", ""))
        elif btype == "server_tool_use" and block.get("name") == "web_search":
            web_search_count += 1
    return {
        "text": "".join(text_parts).strip(),
        "web_search_count": web_search_count,
        "stop_reason": payload.get("stop_reason"),
        "usage": payload.get("usage", {}),
    }


_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.S)


def extract_json(text):
    """Best-effort extraction of a single JSON object from model output.

    Ladder: raw parse -> strip ```json fences -> first balanced {...} -> raise.
    """
    if not text:
        raise AnthropicJSONError("empty model output")

    # 1. raw
    try:
        return json.loads(text.strip())
    except json.JSONDecodeError:
        pass

    # 2. fenced code block
    m = _FENCE_RE.search(text)
    if m:
        try:
            return json.loads(m.group(1).strip())
        except json.JSONDecodeError:
            pass

    # 3. first balanced brace-delimited object
    start = text.find("{")
    while start != -1:
        depth, in_str, esc = 0, False, False
        for i in range(start, len(text)):
            ch = text[i]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
            else:
                if ch == '"':
                    in_str = True
                elif ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0:
                        candidate = text[start:i + 1]
                        try:
                            return json.loads(candidate)
                        except json.JSONDecodeError:
                            break  # try next '{'
        start = text.find("{", start + 1)

    raise AnthropicJSONError(f"no parseable JSON in output: {text[:300]}")


if __name__ == "__main__":
    import sys
    prompt = sys.argv[1] if len(sys.argv) > 1 else "Reply with JSON only: {\"ok\": true}"
    use_search = "--search" in sys.argv
    client = AnthropicClient()
    print(f"model: {client.model}  web_search: {use_search}")
    res = client.complete(
        system="You are a terse assistant. When asked for JSON, output only JSON.",
        user=prompt, use_web_search=use_search,
    )
    print("--- text ---")
    print(res["text"])
    print("--- meta ---")
    print({k: res[k] for k in ("web_search_count", "stop_reason", "usage")})
