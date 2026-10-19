"""server/mcp_client.py — the Host's MCP client, hand-written (no SDK).

The Host is an MCP
server already (server/routes/mcp.py); this is the other direction: the kernel
calling a remote MCP server the person connected and approved
(server/mcp_connections.py owns the connection, its approval and its tools).

TRANSPORT. Streamable HTTP, POST only. Every request goes through the signed
egress gate as an explicit proxy (nac_harness._egress_proxy(), the loopback
CONNECT gate the kernel starts) on a session opened with trust_env=False, so
the HTTP(S)_PROXY / NO_PROXY variables, which point at the node squid and
skip the person's egress rules, are never read. Redirects are not followed:
a connection names one URL, and a redirect could carry an API-key header to a
host nobody approved.

TWO ERAS (spec revision 2026-07-28, "Backward Compatibility"):
  modern  2026-07-28. No session and no handshake: every request carries
          `_meta` io.modelcontextprotocol/protocolVersion, clientInfo and
          clientCapabilities (empty: this client offers no elicitation,
          sampling or roots), and the transport mirrors them into
          MCP-Protocol-Version, Mcp-Method and Mcp-Name, plus one
          Mcp-Param-<name> header per argument a tool's schema marks with
          x-mcp-header.
  legacy  2025-11-25 and earlier. `initialize`, then
          notifications/initialized, then requests carrying the
          Mcp-Session-Id the server minted (if it minted one) and the
          negotiated MCP-Protocol-Version. A 404 on a request that carried a
          session id means the session ended: one new handshake, one retry.
The client tries modern first. A 400 whose body is a recognised modern
JSON-RPC error (HeaderMismatch -32020, MissingRequiredClientCapability
-32021, UnsupportedProtocolVersion -32022) is a modern server answering; any
other 400 is a legacy server, and the client runs the handshake. The era found
is returned so the caller keeps it per connection.

RESPONSES. JSON, or an event stream scoped to the request read to its final
response. Both are read under a byte cap and a timeout. Notifications on the
stream (progress, log messages) are dropped, as is anything else that is not
the response to this request. A result of type `input_required` is an error:
the client declares no capability a server could ask it to use.

What this module does not do: decide whether a call may be made (the
connection's approval, its signed rule, the turn type, the Host's skill) or
inject a credential — the caller passes the auth header the vault broker built
(server/vault/authz.py, action mcp_call) and has already decided.
"""
from __future__ import annotations

import asyncio
import base64
import codecs
import itertools
import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Optional

import aiohttp

log = logging.getLogger("privatae.mcp_client")

MODERN_VERSION = "2026-07-28"
# Newest first: what this client answers an older server's initialize with.
LEGACY_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26")

HEADER_MISMATCH = -32020
MISSING_CLIENT_CAPABILITY = -32021
UNSUPPORTED_PROTOCOL_VERSION = -32022
METHOD_NOT_FOUND = -32601
# The errors only a modern server sends (spec 2026-07-28, basic/index "Error
# Codes"): a 400 carrying one of these is not a legacy server.
MODERN_ERROR_CODES = frozenset({HEADER_MISMATCH, MISSING_CLIENT_CAPABILITY,
                                UNSUPPORTED_PROTOCOL_VERSION})

CLIENT_INFO = {"name": "privatae-host", "version": "1.0"}

# One response, JSON or a whole event stream, at most this many bytes. A tool
# result the model can use is far smaller; the result wall cuts it again.
MAX_RESPONSE_BYTES = 4 * 1024 * 1024
# One request, from the POST to the final response.
TIMEOUT_S = 60.0
# tools/list pages followed before the list is cut.
MAX_TOOL_PAGES = 20

_RESULT_COMPLETE = "complete"
_RESULT_INPUT_REQUIRED = "input_required"
_IDS = itertools.count(1)


class McpError(Exception):
    """A request that did not produce a result.

    kind: transport (the gate, the network, an HTTP status), timeout,
    too_large, protocol (the server's JSON-RPC error), input_required,
    invalid (a response or a tool definition this client cannot use), args (a
    call argument that cannot go in a header), auth (a 401, or a 403 carrying
    a WWW-Authenticate challenge: the server wants a sign-in or more scope;
    `www_authenticate` holds every challenge header it sent, for
    server/mcp_oauth.py). `caller_refusal` marks the kinds that are the call's
    own to fix (tool_call_ledger.refused_if_caller reads it), never a platform
    fault."""

    def __init__(self, message: str, *, kind: str = "transport", code: Optional[int] = None,
                 http_status: Optional[int] = None, data: Any = None,
                 www_authenticate: tuple = ()):
        super().__init__(message)
        self.kind = kind
        self.code = code
        self.http_status = http_status
        self.data = data
        self.www_authenticate = tuple(www_authenticate or ())

    @property
    def caller_refusal(self) -> bool:
        return self.kind in ("protocol", "input_required", "args")


@dataclass
class Exchange:
    """What one request produced: the JSON-RPC result, the era the server
    answered in and the version spoken, and what it cost."""
    result: dict
    era: str
    protocol_version: str
    http_status: int
    bytes: int = 0
    ms: int = 0
    session_id: Optional[str] = None


@dataclass
class _Reply:
    status: int
    message: Any = None
    session_id: Optional[str] = None
    nbytes: int = 0
    content_type: str = ""
    www_authenticate: tuple = ()


@dataclass
class _LegacySession:
    session_id: Optional[str]
    version: str
    created: float = field(default_factory=time.monotonic)


# Legacy sessions, per connection (the caller's cache_key). In memory only: a
# restarted kernel runs one new handshake, which is what the legacy transport
# expects of a client that lost its session.
_LEGACY: dict[str, _LegacySession] = {}


def gate_proxy() -> str:
    """The signed egress gate every request goes through (server/main.py
    starts it on loopback; NAC_EGRESS_PORT moves it)."""
    from server.network.nac_harness import _egress_proxy
    return _egress_proxy()


def forget_session(cache_key: Optional[str]) -> None:
    """Drop a connection's legacy session (a disconnect, a revoked rule)."""
    if cache_key:
        _LEGACY.pop(cache_key, None)


# ── x-mcp-header (spec 2026-07-28, transports/streamable-http) ───────────────

_TCHAR = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")
_SAFE_INT = 2 ** 53 - 1
_SENTINEL_HEAD, _SENTINEL_TAIL = "=?base64?", "?="


def _count_annotations(node) -> int:
    if isinstance(node, dict):
        return (1 if "x-mcp-header" in node else 0) + sum(_count_annotations(v) for v in node.values())
    if isinstance(node, list):
        return sum(_count_annotations(v) for v in node)
    return 0


def header_params(schema: Any) -> list[tuple[tuple[str, ...], str]]:
    """Every argument a tool's inputSchema mirrors into a header, as
    [(path, name)], `path` the chain of `properties` keys. ValueError when an
    annotation breaks the spec's rules: empty or not an HTTP token, a
    duplicate (case-insensitive), on a type other than integer, string or
    boolean, or not reachable from the root through `properties` alone. A tool
    with such a schema is left out of the tool list (the spec's MUST)."""
    out: list[tuple[tuple[str, ...], str]] = []
    if not isinstance(schema, dict):
        return out

    def walk(node: dict, path: tuple[str, ...]):
        props = node.get("properties")
        if not isinstance(props, dict):
            return
        for key, sub in props.items():
            if not isinstance(sub, dict):
                continue
            here = path + (str(key),)
            if "x-mcp-header" in sub:
                out.append((here, sub))
            walk(sub, here)

    walk(schema, ())
    if _count_annotations(schema) != len(out):
        raise ValueError("an x-mcp-header annotation is not reachable from the schema root "
                         "through `properties` alone")
    seen: set[str] = set()
    result = []
    for path, sub in out:
        name = sub.get("x-mcp-header")
        if not isinstance(name, str) or not name or not _TCHAR.match(name):
            raise ValueError(f"x-mcp-header on {'.'.join(path)} is not an HTTP header token")
        if name.lower() in seen:
            raise ValueError(f"x-mcp-header {name!r} is used twice")
        seen.add(name.lower())
        typ = sub.get("type")
        if typ not in ("integer", "string", "boolean"):
            raise ValueError(f"x-mcp-header on {'.'.join(path)} is on a {typ!r}, not an "
                             "integer, string or boolean")
        result.append((path, name))
    return result


def encode_header_value(value: Any) -> str:
    """A value as an MCP header carries it: a string as it is when it is plain
    visible ASCII with no surrounding whitespace, else `=?base64?…?=`; an
    integer in decimal; a boolean as true/false."""
    if isinstance(value, bool):
        text = "true" if value else "false"
    elif isinstance(value, int):
        if abs(value) > _SAFE_INT:
            raise McpError(f"{value} is outside the integer range a header may carry", kind="args")
        text = str(value)
    else:
        text = str(value)
    plain = (all(c == "\t" or 0x20 <= ord(c) <= 0x7E for c in text)
             and text == text.strip()
             and not (text.startswith(_SENTINEL_HEAD) and text.endswith(_SENTINEL_TAIL)))
    if plain:
        return text
    return _SENTINEL_HEAD + base64.b64encode(text.encode("utf-8")).decode("ascii") + _SENTINEL_TAIL


def _value_at(args: Any, path: tuple[str, ...]):
    node = args
    for key in path:
        if not isinstance(node, dict) or key not in node:
            return None
        node = node[key]
    return node


def call_headers(method: str, params: dict, input_schema: Any = None) -> dict[str, str]:
    """Mcp-Method, Mcp-Name and the Mcp-Param-* headers of a modern request."""
    h = {"MCP-Protocol-Version": MODERN_VERSION, "Mcp-Method": method}
    name = None
    if method in ("tools/call", "prompts/get"):
        name = params.get("name")
    elif method == "resources/read":
        name = params.get("uri")
    if name is not None:
        h["Mcp-Name"] = encode_header_value(str(name))
    if method == "tools/call" and input_schema is not None:
        try:
            marked = header_params(input_schema)
        except ValueError as e:
            raise McpError(f"this tool's schema cannot be called: {e}", kind="invalid") from None
        args = params.get("arguments") if isinstance(params.get("arguments"), dict) else {}
        for path, hname in marked:
            v = _value_at(args, path)
            if v is not None:
                if isinstance(v, (dict, list)) or isinstance(v, float):
                    raise McpError(f"`{'.'.join(path)}` goes in a header and must be an integer, "
                                   "string or boolean", kind="args")
                h[f"Mcp-Param-{hname}"] = encode_header_value(v)
    return h


# ── reading a response ──────────────────────────────────────────────────────

async def _read_capped(resp: aiohttp.ClientResponse, max_bytes: int) -> bytes:
    buf = bytearray()
    async for chunk in resp.content.iter_any():
        buf += chunk
        if len(buf) > max_bytes:
            raise McpError(f"the server's response is over {max_bytes} bytes", kind="too_large")
    return bytes(buf)


def _is_response_to(msg: Any, want_id) -> bool:
    return (isinstance(msg, dict) and msg.get("id") == want_id
            and ("result" in msg or "error" in msg) and "method" not in msg)


_EOL = re.compile(r"\r\n|\n|\r(?!$)")


async def _read_stream(resp: aiohttp.ClientResponse, want_id, max_bytes: int) -> tuple[Any, int]:
    """Read an event stream until the response to `want_id`. Every other event
    (progress, log messages, a legacy server's own requests) is dropped."""
    total = 0
    pending = ""
    data_lines: list[str] = []

    def dispatch() -> Any:
        if not data_lines:
            return None
        text = "\n".join(data_lines)
        data_lines.clear()
        try:
            return json.loads(text)
        except ValueError:
            return None

    # One decoder for the whole stream, so a character split across two chunks
    # decodes whole; and the line search starts where the last one stopped, so
    # a long run with no line break costs one pass, not one pass per chunk.
    decoder = codecs.getincrementaldecoder("utf-8")("replace")
    scan_from = 0
    async for chunk in resp.content.iter_any():
        total += len(chunk)
        if total > max_bytes:
            raise McpError(f"the server's event stream is over {max_bytes} bytes", kind="too_large")
        pending += decoder.decode(chunk)
        # Lines end with \r\n, \n or \r. A \r at the very end may be the first
        # half of a \r\n still in flight, so it waits for the next chunk.
        while True:
            m = _EOL.search(pending, scan_from)
            if not m:
                # Everything but a trailing \r was searched; start there next time.
                scan_from = max(len(pending) - 1, 0)
                break
            line, pending = pending[:m.start()], pending[m.end():]
            scan_from = 0
            if line == "":
                msg = dispatch()
                if _is_response_to(msg, want_id):
                    return msg, total
                continue
            if line.startswith(":"):
                continue            # a comment (keep-alive)
            fname, _, value = line.partition(":")
            if value.startswith(" "):
                value = value[1:]
            if fname == "data":
                data_lines.append(value)
    # The stream ended: an event with no blank line after it still counts.
    pending += decoder.decode(b"", final=True)
    if pending.strip():
        fname, _, value = pending.rstrip("\r\n").partition(":")
        if fname == "data":
            data_lines.append(value[1:] if value.startswith(" ") else value)
    msg = dispatch()
    if _is_response_to(msg, want_id):
        return msg, total
    raise McpError("the server's event stream ended without a response", kind="invalid")


async def _post(session: aiohttp.ClientSession, url: str, body: dict, headers: dict, *,
                want_id, max_bytes: int, ssl, proxy: Optional[str]) -> _Reply:
    h = {"Content-Type": "application/json",
         "Accept": "application/json, text/event-stream", **headers}
    try:
        async with session.post(url, data=json.dumps(body).encode("utf-8"), headers=h,
                                proxy=proxy, allow_redirects=False,
                                ssl=True if ssl is None else ssl) as resp:
            ctype = (resp.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            sid = resp.headers.get("Mcp-Session-Id")
            # Kept only on the answers that can carry a sign-in challenge.
            challenge = (tuple(resp.headers.getall("WWW-Authenticate", ()))[:8]
                         if resp.status in (401, 403) else ())
            if want_id is None:
                await _read_capped(resp, 65536)
                return _Reply(resp.status, None, sid, 0, ctype, challenge)
            if ctype == "text/event-stream" and 200 <= resp.status < 300:
                msg, n = await _read_stream(resp, want_id, max_bytes)
                return _Reply(resp.status, msg, sid, n, ctype)
            raw = await _read_capped(resp, max_bytes)
            try:
                msg = json.loads(raw) if raw.strip() else None
            except ValueError:
                msg = None
            return _Reply(resp.status, msg, sid, len(raw), ctype, challenge)
    except McpError:
        raise
    except asyncio.TimeoutError:
        raise McpError("the server did not answer in time", kind="timeout") from None
    except aiohttp.ClientProxyConnectionError as e:
        raise McpError(f"the egress gate is not reachable ({type(e).__name__})") from None
    except aiohttp.ClientHttpProxyError as e:
        # The gate answered the CONNECT with an error: the person's egress
        # rules refuse this host, or the path out failed.
        raise McpError(f"the egress gate refused the connection (HTTP {e.status})",
                       http_status=e.status) from None
    except aiohttp.ClientError as e:
        raise McpError(f"the request failed ({type(e).__name__})") from None


def _raise_if_auth(reply: _Reply, method: str) -> None:
    """McpError(kind='auth') for a 401, or a 403 that carries a challenge: the
    server wants a sign-in, or a token with more scope (spec 2026-07-28,
    basic/authorization "Error Handling"). A 403 with no challenge is the
    server refusing the call, which stays the protocol error it was."""
    if reply.status == 401 or (reply.status == 403 and reply.www_authenticate):
        what = "a sign-in" if reply.status == 401 else "more access than this sign-in gave"
        raise McpError(f"the server answered {method} with HTTP {reply.status}: it wants {what}",
                       kind="auth", http_status=reply.status,
                       www_authenticate=reply.www_authenticate)


def _modern_error_code(msg: Any) -> Optional[int]:
    if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0":
        return None
    err = msg.get("error")
    code = err.get("code") if isinstance(err, dict) else None
    return code if isinstance(code, int) and not isinstance(code, bool) else None


def _result_of(reply: _Reply, *, method: str) -> dict:
    """The result of a JSON-RPC response, or McpError."""
    msg = reply.message
    if not isinstance(msg, dict):
        raise McpError(f"the server answered {method} with no JSON-RPC response "
                       f"(HTTP {reply.status})", kind="invalid", http_status=reply.status)
    if "error" in msg:
        err = msg.get("error") if isinstance(msg.get("error"), dict) else {}
        raise McpError(f"the server refused {method}: {str(err.get('message') or 'error')[:500]}",
                       kind="protocol", code=err.get("code"), http_status=reply.status,
                       data=err.get("data"))
    result = msg.get("result")
    if not isinstance(result, dict):
        raise McpError(f"the server's {method} result is not an object", kind="invalid")
    rtype = result.get("resultType", _RESULT_COMPLETE)
    if rtype == _RESULT_INPUT_REQUIRED:
        asked = result.get("inputRequests")
        what = sorted({str((v or {}).get("method")) for v in asked.values()
                       if isinstance(v, dict)}) if isinstance(asked, dict) else []
        raise McpError("the server asked for input this Host does not provide"
                       + (f" ({', '.join(what)})" if what else ""), kind="input_required")
    if rtype != _RESULT_COMPLETE:
        raise McpError(f"the server's result type {str(rtype)[:60]!r} is not one this client knows",
                       kind="invalid")
    return result


# ── the request ─────────────────────────────────────────────────────────────

def _session(timeout: float) -> aiohttp.ClientSession:
    # trust_env=False: the proxy variables are never read. The gate is named
    # on every request instead (_post).
    return aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout), trust_env=False)


async def _modern(session, url, method, params, headers, input_schema, max_bytes, ssl,
                  proxy) -> _Reply:
    rid = next(_IDS)
    p = dict(params or {})
    meta = dict(p.get("_meta") or {})
    meta.update({"io.modelcontextprotocol/protocolVersion": MODERN_VERSION,
                 "io.modelcontextprotocol/clientInfo": CLIENT_INFO,
                 "io.modelcontextprotocol/clientCapabilities": {}})
    p["_meta"] = meta
    h = {**headers, **call_headers(method, p, input_schema)}
    return await _post(session, url, {"jsonrpc": "2.0", "id": rid, "method": method, "params": p},
                       h, want_id=rid, max_bytes=max_bytes, ssl=ssl, proxy=proxy)


async def _initialize(session, url, headers, max_bytes, ssl, versions, proxy) -> _LegacySession:
    """The legacy handshake: initialize, then notifications/initialized."""
    rid = next(_IDS)
    want = versions[0]
    reply = await _post(session, url, {
        "jsonrpc": "2.0", "id": rid, "method": "initialize",
        "params": {"protocolVersion": want, "capabilities": {}, "clientInfo": CLIENT_INFO}},
        dict(headers), want_id=rid, max_bytes=max_bytes, ssl=ssl, proxy=proxy)
    _raise_if_auth(reply, "initialize")
    if not 200 <= reply.status < 300:
        raise McpError(f"the server refused initialize (HTTP {reply.status})",
                       http_status=reply.status, kind="protocol" if reply.status < 500 else "transport")
    result = _result_of(reply, method="initialize")
    version = str(result.get("protocolVersion") or want)
    if version not in LEGACY_VERSIONS:
        raise McpError(f"the server speaks protocol {version[:40]!r}, which this client does not",
                       kind="invalid")
    sess = _LegacySession(reply.session_id, version)
    h = dict(headers)
    h["MCP-Protocol-Version"] = version
    if sess.session_id:
        h["Mcp-Session-Id"] = sess.session_id
    note = await _post(session, url, {"jsonrpc": "2.0", "method": "notifications/initialized"},
                       h, want_id=None, max_bytes=max_bytes, ssl=ssl, proxy=proxy)
    _raise_if_auth(note, "notifications/initialized")
    if not 200 <= note.status < 300:
        raise McpError(f"the server refused notifications/initialized (HTTP {note.status})",
                       http_status=note.status)
    return sess


async def _legacy(session, url, method, params, headers, cache_key, max_bytes, ssl,
                  versions, proxy) -> tuple[_Reply, _LegacySession]:
    key = cache_key or url
    for attempt in range(2):
        sess = _LEGACY.get(key)
        if sess is None:
            sess = await _initialize(session, url, headers, max_bytes, ssl, versions, proxy)
            _LEGACY[key] = sess
        rid = next(_IDS)
        h = {**headers, "MCP-Protocol-Version": sess.version}
        if sess.session_id:
            h["Mcp-Session-Id"] = sess.session_id
        reply = await _post(session, url, {"jsonrpc": "2.0", "id": rid, "method": method,
                                           "params": dict(params or {})},
                            h, want_id=rid, max_bytes=max_bytes, ssl=ssl, proxy=proxy)
        if reply.status == 404 and sess.session_id and attempt == 0:
            # The session ended on the server: one new handshake, one retry.
            _LEGACY.pop(key, None)
            continue
        return reply, sess
    return reply, sess   # pragma: no cover - the loop always returns


async def request(url: str, method: str, params: Optional[dict] = None, *,
                  headers: Optional[dict] = None, era: Optional[str] = None,
                  cache_key: Optional[str] = None, input_schema: Any = None,
                  timeout: float = TIMEOUT_S, max_bytes: int = MAX_RESPONSE_BYTES,
                  ssl=None, via_gate: bool = True) -> Exchange:
    """One MCP request through the egress gate, in the era the caller last
    found (`era`), or modern first when it has none. `headers` are the auth
    header the vault broker built; `input_schema` the tool's schema, for its
    x-mcp-header arguments. `ssl` is aiohttp's: None verifies the server's
    certificate as usual (the kernel never passes anything else).

    via_gate=False is for one caller only: an installed app's own MCP server
    (mcp_connections, an app connection), dialled at its pod's address inside
    the person's namespace. That traffic never leaves the cluster, so it is
    not the egress gate's to judge; the session still reads no proxy
    variables (trust_env=False), so it goes direct."""
    t0 = time.monotonic()
    headers = dict(headers or {})
    proxy = gate_proxy() if via_gate else None
    try:
        async with _session(timeout) as session:
            return await asyncio.wait_for(
                _request(session, url, method, params or {}, headers, era, cache_key,
                         input_schema, max_bytes, ssl, t0, proxy),
                timeout=timeout + 5)
    except asyncio.TimeoutError:
        raise McpError("the server did not answer in time", kind="timeout") from None


async def _request(session, url, method, params, headers, era, cache_key, input_schema,
                   max_bytes, ssl, t0, proxy) -> Exchange:
    versions = LEGACY_VERSIONS
    if era != "legacy":
        reply = await _modern(session, url, method, params, headers, input_schema, max_bytes, ssl,
                              proxy)
        code = _modern_error_code(reply.message)
        if 200 <= reply.status < 300:
            result = _result_of(reply, method=method)
            return Exchange(result, "modern", MODERN_VERSION, reply.status, reply.nbytes,
                            int((time.monotonic() - t0) * 1000))
        _raise_if_auth(reply, method)
        if reply.status == 400 and code in MODERN_ERROR_CODES:
            if code == UNSUPPORTED_PROTOCOL_VERSION:
                data = ((reply.message or {}).get("error") or {}).get("data") or {}
                supported = [str(v) for v in (data.get("supported") or [])] if isinstance(data, dict) else []
                older = tuple(v for v in LEGACY_VERSIONS if v in supported)
                if MODERN_VERSION not in supported and older:
                    versions = older       # a dual-era server that will not speak modern
                else:
                    _result_of(reply, method=method)
            else:
                _result_of(reply, method=method)
        elif reply.status == 404 and code == METHOD_NOT_FOUND:
            _result_of(reply, method=method)
        elif reply.status != 400:
            raise McpError(f"the server answered {method} with HTTP {reply.status}",
                           http_status=reply.status,
                           kind="protocol" if 400 <= reply.status < 500 else "transport")
        # A 400 with no modern error, or a version list without the modern
        # one: a legacy server. Fall back (spec: "fall back to initialize").
    try:
        reply, sess = await _legacy(session, url, method, params, headers, cache_key,
                                    max_bytes, ssl, versions, proxy)
    except McpError as e:
        if (era == "legacy" and e.kind != "auth" and e.http_status is not None
                and 400 <= e.http_status < 500):
            # The era kept for this connection is out of date (the server
            # moved to the modern revision): probe modern once.
            return await _request(session, url, method, params, headers, None, cache_key,
                                  input_schema, max_bytes, ssl, t0, proxy)
        raise
    if not 200 <= reply.status < 300:
        _raise_if_auth(reply, method)
        if reply.status >= 400 and isinstance(reply.message, dict) and "error" in reply.message:
            _result_of(reply, method=method)
        raise McpError(f"the server answered {method} with HTTP {reply.status}",
                       http_status=reply.status,
                       kind="protocol" if 400 <= reply.status < 500 else "transport")
    result = _result_of(reply, method=method)
    return Exchange(result, "legacy", sess.version, reply.status, reply.nbytes,
                    int((time.monotonic() - t0) * 1000), sess.session_id)


async def list_tools(url: str, *, headers: Optional[dict] = None, era: Optional[str] = None,
                     cache_key: Optional[str] = None, timeout: float = TIMEOUT_S,
                     max_bytes: int = MAX_RESPONSE_BYTES, ssl=None,
                     via_gate: bool = True) -> tuple[list, Optional[int], Exchange]:
    """Every page of tools/list: (tools, ttl_ms, the last exchange). The era
    the first page found is used for the rest. ttl_ms is the smallest the
    server sent, or None."""
    tools: list = []
    ttl: Optional[int] = None
    cursor = None
    last = None
    total_bytes = 0
    for _ in range(MAX_TOOL_PAGES):
        params = {"cursor": cursor} if cursor else {}
        last = await request(url, "tools/list", params, headers=headers, era=era,
                             cache_key=cache_key, timeout=timeout, max_bytes=max_bytes, ssl=ssl,
                             via_gate=via_gate)
        era = last.era
        total_bytes += last.bytes
        page = last.result.get("tools")
        if not isinstance(page, list):
            raise McpError("the server's tools/list result has no tool list", kind="invalid")
        tools.extend(page)
        t = last.result.get("ttlMs")
        if isinstance(t, int) and not isinstance(t, bool) and t >= 0:
            ttl = t if ttl is None else min(ttl, t)
        cursor = last.result.get("nextCursor")
        if not cursor:
            break
    last.bytes = total_bytes
    return tools, ttl, last


async def call_tool(url: str, tool: str, arguments: dict, *, input_schema: Any = None,
                    headers: Optional[dict] = None, era: Optional[str] = None,
                    cache_key: Optional[str] = None, timeout: float = TIMEOUT_S,
                    max_bytes: int = MAX_RESPONSE_BYTES, ssl=None,
                    via_gate: bool = True) -> Exchange:
    """tools/call: the result (content, structuredContent, isError) as the
    server sent it."""
    return await request(url, "tools/call", {"name": tool, "arguments": arguments or {}},
                         headers=headers, era=era, cache_key=cache_key, input_schema=input_schema,
                         timeout=timeout, max_bytes=max_bytes, ssl=ssl, via_gate=via_gate)


__all__ = [
    "MODERN_VERSION", "LEGACY_VERSIONS", "MODERN_ERROR_CODES", "MAX_RESPONSE_BYTES", "TIMEOUT_S",
    "McpError", "Exchange", "gate_proxy", "forget_session", "header_params",
    "encode_header_value", "call_headers", "request", "list_tools", "call_tool",
]
