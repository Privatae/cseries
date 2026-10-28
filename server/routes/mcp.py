"""
This Host's MCP endpoint, POST /mcp (JSON-RPC 2.0, single or batch).

Four kinds of caller, told apart in _auth_or_401:
  * an outside agent (Claude Code, Codex, an agent inside T3 Code) signed in
    through this Host's own OAuth (server/agent_access). It sees the agent
    tools (agent_access.tools): the person's machines and ops on them, each
    checked against what the person approved for that agent, with an approval
    link for anything new;
  * a coding-harness container with its control capability: read_url only;
  * a bearer from mcp_client_tokens (a bastion's): no tools since the
    identity layer's were retired;
  * the in-container internal secret: likewise.

Protocol shape:
    Request   : {"jsonrpc":"2.0","id":N,"method":"...","params":{...}}
    Response  : {"jsonrpc":"2.0","id":N,"result":{...}}
    Error     : {"jsonrpc":"2.0","id":N,"error":{"code":N,"message":"..."}}
    Notif     : same as request but no `id` — no response is sent for it

Methods: initialize, notifications/initialized, ping, tools/list,
tools/call, prompts/list, prompts/get. A missing or bad token is answered 401
with WWW-Authenticate naming this Host's protected-resource metadata, where a
standard client starts its sign-in.

Hand-written JSON-RPC, no `mcp` SDK dependency.
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Request

from server import brain_service
from server.synaptive_handler import _validate_bearer

router = APIRouter()
logger = logging.getLogger("privatae.pc.mcp")

# The handshake-era protocol versions this endpoint speaks. The answer to
# initialize is the client's own version when it is one of these, else the
# newest (MCP 2025-11-25 lifecycle, version negotiation). 2025-11-25 is the
# one an outside agent's approval link needs: URL-mode elicitation and its
# -32042 error (agent_access.tools.NeedsApproval). The stateless 2026-07-28
# revision is not served; a client probing it with server/discover gets
# "unknown method" and falls back to this handshake.
PROTOCOL_VERSIONS = ("2025-06-18", "2025-11-25")
PROTOCOL_VERSION = PROTOCOL_VERSIONS[-1]
URL_ELICITATION_REQUIRED = -32042

SERVER_INFO = {
    "name": "host",
    "version": "0.2.0",
}

# JSON-RPC 2.0 standard error codes
_PARSE_ERROR = -32700
_INVALID_REQUEST = -32600
_METHOD_NOT_FOUND = -32601
_INVALID_PARAMS = -32602
_INTERNAL_ERROR = -32603

# The old identity layer's prompt and its tools (shape, recall, signal,
# open_app, close_app, list_apps) are retired from /mcp: an outside agent asks
# the Host to act on the person's machines instead (server/agent_access).
# Their only mint path for a person was the Settings app's old tab, retired
# with them.
PROMPTS: list[dict] = []

TOOLS = [
    {
        # The harness→host fetch channel. A coding harness pod
        # has NO public DNS (harness-pod resolvers serve cluster names only)
        # and its only route out is the node squid, so a vendor CLI's own
        # fetch tool that pre-resolves the hostname fails before it ever
        # reaches the proxy: in one build, kimi's FetchURL →
        # "getaddrinfo EAI_AGAIN raw.githubusercontent.com", and the harness
        # then built the bot UI framing blind. This tool is the same reader
        # the host itself uses (runtime/url_reader.py — squid-routed, honest
        # `blocked`), reached over the kernel's /mcp from the harness pod with
        # the per-pod control capability as its credential. Scope `fetch`.
        "name": "read_url",
        "description": (
            "Fetch ONE public web page THROUGH THE HOST and return its "
            "readable text as markdown. Use this instead of any built-in "
            "fetch/curl: this pod has no public DNS and the host's reader is "
            "the only governed way out. Returns {url, final_url, title, "
            "content, chars, truncated, http_status}; a long page comes in "
            "pages — when there is more, the result says so with next_offset, "
            "and passing it as offset reads on. Status 'blocked' means "
            "the site refused — do not retry, and never guess what it said."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["url"],
            "properties": {
                "url": {"type": "string",
                        "description": "The http(s) page to read."},
                "max_chars": {"type": "integer",
                              "description": "Optional size of this page of text."},
                "offset": {"type": "integer",
                           "description": ("Character to start at; pass the previous "
                                           "result's next_offset to read on.")},
            },
        },
    },
]


# ── JSON-RPC envelope helpers ────────────────────────────────────────

def _ok(req_id: Any, result: dict) -> dict:
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def _err(req_id: Any, code: int, msg: str) -> dict:
    return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": msg}}


# ── Auth ─────────────────────────────────────────────────────────────

async def _auth_or_401(
    authorization: str | None,
    *,
    hardware_fingerprint: str | None = None,
    internal_auth: str | None = None,
    harness_cap: str | None = None,
    internal_ok: bool = False,
) -> dict:
    """Resolve bearer to an mcp_client_tokens row. Raises HTTP 401/503 on failure.

    `internal_ok` says the request came from inside this container
    (in_container): the internal secret is compared only then. From anywhere
    else (the runtime's network, the mTLS front door, a door's proxy) a
    request bringing it is refused before any compare, so no caller outside
    the container can test a guess against it, whatever the gates in front
    let through.

    If the token row has `hardware_fingerprint` set (device-class tokens —
    bastion/gateway), the supplied `hardware_fingerprint` argument MUST match
    exactly. NULL `hardware_fingerprint` means a BEARER-ONLY token (the basic
    synaptive token a user pastes into Claude Desktop / Cursor) — there is no
    device to bind, so the device check is skipped.

    INTENTIONAL, NOT A HOLE: bearer-only tokens KEEP full
    access — a user pasting a token into their own app is a sovereign choice; the
    device-blind tradeoff is theirs (the bastion is the device-bound alternative).
    Do NOT "fix" this by restricting NULL-fp. The remaining device-class hardening
    is Fork 4 (cryptographic fp bound from the device-signed CSR at redeem,
    replacing bastion heartbeat-TOFU) — tracked in the NAC build, cross-component
    (gateway client `run.go`), not this branch.
    """
    import hmac as _hmac
    internal_secret = os.environ.get("INTERNAL_API_SECRET", "")
    if internal_auth and not internal_ok:
        raise HTTPException(401, "the internal secret is accepted only from inside this container")
    if internal_auth and internal_secret and _hmac.compare_digest(internal_auth, internal_secret):
        entity_id = int(os.environ.get("ENTITY_ID", "0"))
        return {
            "id": 0,
            "entity_id": entity_id,
            # NARROWED: an in-pod loopback /mcp caller gets the
            # SYNAPTIVE SUBSTRATE ONLY (shape/recall/signal). The secret no longer
            # grants `factory` (the 13 powerful bridge-op tools — systemctl/k8s/
            # docker/code/git) or `apps` device-blind. Verified safe: nothing posts
            # to /mcp with this secret (the in-pod CEIGAS subprocess uses the unix
            # socket; the entity ships/codes via the meta-tool runtime; factory/apps
            # are not mintable on any bearer). The `factory` tools themselves
            # are retired (see TOOL_HANDLERS).
            "scopes": ["shape", "recall", "signal"],
            "hardware_fingerprint": None,
            "_internal": True,
        }
    # Third caller class: a coding-harness POD presenting the
    # control capability the operator minted for it and handed to THIS kernel
    # on harness.wake (review._control_cap — memory only, cleared on release).
    # Possession proves the caller is a pod this kernel woke, and the grant is
    # the `fetch` scope alone: read_url, nothing else. No new secret exists —
    # this is the same control capability, verified against the same store the
    # dispatcher already sends it from. Not `_internal`: the harness is a
    # confined peer, so it keeps the rate limit and the external CEIGAS
    # source domain.
    if harness_cap:
        from server.routes.review import control_cap_is_current
        if control_cap_is_current(harness_cap):
            return {
                "id": 0,
                "entity_id": int(os.environ.get("ENTITY_ID", "0")),
                "scopes": ["fetch"],
                "hardware_fingerprint": None,
                "_harness": True,
            }
        raise HTTPException(401, "harness capability is not current")
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(401, "missing bearer token")
    pool = brain_service.get_pool()
    if pool is None:
        raise HTTPException(503, "brain pool not ready")
    bearer = authorization[7:].strip()
    # An outside agent connected through this Host's own sign-in
    # (server/agent_access). Its token names a connection the person approved;
    # what it may do is that connection's, checked per call
    # (agent_access.tools), and the Host's own gates still run under it.
    from server.agent_access import store as agent_store
    if bearer.startswith(agent_store.ACCESS_PREFIX):
        eid = int(getattr(brain_service, "_entity_id", 0) or 0)
        conn = await agent_store.resolve_access(pool, eid, bearer)
        if conn is None:
            raise HTTPException(401, "invalid, expired or revoked token")
        return {"id": 0, "entity_id": eid, "scopes": [], "hardware_fingerprint": None,
                "_agent": conn}
    auth = await _validate_bearer(pool, bearer)
    if auth is None:
        raise HTTPException(401, "invalid or revoked token")

    bound_fp = auth.get("hardware_fingerprint")
    if bound_fp:
        if not hardware_fingerprint or hardware_fingerprint != bound_fp:
            # Don't leak which side failed; just refuse.
            raise HTTPException(401, "hardware fingerprint mismatch")
    return auth


def in_container(request) -> bool:
    """middleware.localhost_only.in_container: the internal secret's one lane."""
    from server.middleware.localhost_only import in_container as _in
    return _in(request)


async def _nac_gate(auth: dict, nac=None) -> dict | None:
    """P3 — the device->host NETWORK wall on the MCP lane. A device-class token (one with
    a CSR-bound hardware_fingerprint — a bastion) must be PERMITTED by the host's NAC PDP
    (by its posture / classification / fingerprint policy) BEFORE any tool dispatch — the
    network-policy wall, complementary to and ahead of ceigas-local (capability).

    Default-preserve: no NAC policy authored => permit (today's behavior). Bearer-only
    tokens (NULL fingerprint) and in-pod loopback (_internal) are EXEMPT — full access for
    a pasted token is the user's sovereign choice; the NAC governs mesh devices.
    A tampered signed policy denies (the PDP returns quarantine), and so does an engine
    that raises while deciding: an error says nothing about whether the rules admit this
    machine, so it is refused, the way the tamper lock refuses, and logged. Admitting on an
    error let one approved rule the matcher could not evaluate admit every governed
    machine. A request that met a passing error (a DB read) is refused once; the next one
    asks again. With no engine loaded at all (its import or construction failed at start)
    a governed machine is refused the same way.

    Returns the decision dict when the device is QUARANTINED (caller refuses the request),
    or None to allow. `nac` is injectable for tests; production uses get_nac_controller()."""
    if auth.get("_internal"):
        return None
    if not auth.get("hardware_fingerprint"):
        return None  # bearer-only (or pre-CSR bastion) -> sovereign full access
    token_id = auth.get("id")
    if not token_id:
        return None
    if nac is None:
        try:
            from server.main import get_nac_controller
            nac = get_nac_controller()
        except Exception:
            nac = None
    if nac is None:
        # No engine: its import or construction failed at start (main.py logs it and
        # leaves the reference empty; requests are not served before start finishes).
        # Nothing then knows whether the rules admit this machine, so it is refused,
        # as an engine error is.
        logger.error("NAC gate: no network engine is loaded, so token %s is refused", token_id)
        return {"allowed": False, "profile": None, "error": True,
                "why": "the network rules are not loaded on this Host, so this machine is refused"}
    try:
        decision = await nac.evaluate(int(token_id))
    except Exception as e:
        logger.error("NAC gate: the engine could not decide for token %s, so it is refused: %r",
                     token_id, e)
        return {"allowed": False, "profile": None, "error": True,
                "why": "the network rules could not be evaluated for this machine "
                       f"({type(e).__name__}), so it is refused"}
    if not decision.get("allowed", True):
        return decision
    return None


# The inbox notice for a start where the network engine was not built. Its words live here,
# beside the gate that refuses, and not in nac_controller: that module failing to import
# is one of the ways the engine is not built.
NO_ENGINE_NOTICE_SUBJECT = "Your Host is refusing every paired machine"
NO_ENGINE_NOTICE_BODY = (
    "Your Host is refusing every machine you paired with it.\n\n"
    "Its network rules did not load when it started, so it cannot tell which of your "
    "paired machines the rules let in, and it refuses all of them rather than guess. "
    "An app or client you connected with a plain token, not a paired machine, is not "
    "affected.\n\n"
    "Your Host tries to load the rules again each time it starts. If this message comes "
    "back after a restart, the rules still did not load; the Network app's Ingress lane "
    "says so too."
)


async def tell_no_engine(*, notify=None) -> bool:
    """Tell the person that the gate refuses every paired machine because the network
    engine was not built at this start (main.py's NAC init). Called once per start that
    fails, so each such start writes one notice with its own id: a later start that fails
    again after one that loaded is news, and a reused id would be dropped by the inbox
    (memory_handlers.handle_inbox_append: ON CONFLICT (message_id) DO NOTHING).

    Written the way the lock notice is (NACController._tell_lock). `notify` is injectable
    for a test; None is the Host's inbox. Never raises: the Host keeps starting. Returns
    whether the notice was stored."""
    import uuid
    notice = {
        "subject": NO_ENGINE_NOTICE_SUBJECT,
        "body": NO_ENGINE_NOTICE_BODY,
        "body_format": "text/markdown",
        "message_kind": "system_notification",
        "message_id": str(uuid.uuid4()),
        "payload": {"app": "network", "kind": "nac_not_loaded"},
    }
    try:
        if notify is not None:
            res = await notify(notice)
        else:
            from server.bridge.memory_handlers import handle_inbox_append
            res = await handle_inbox_append(notice)
    except Exception as e:
        logger.warning("NAC: could not tell the Host its network rules did not load: %r", e)
        return False
    if isinstance(res, dict) and res.get("ok") is False:
        logger.warning("NAC: the rules-not-loaded notice was not stored: %s", res.get("error"))
        return False
    return True


# ── Tool dispatch ────────────────────────────────────────────────────

# ── Tool registry (single source of truth for dispatch + scope) ──────
#
# Each entry maps tool name → (handler, required_scope). Both `tools/list`
# (scope-filtered) and `tools/call` (scope-checked) read from this dict so
# the advertised surface and the dispatched surface can never drift.
#
# Handlers are wrapper coroutines that take a single `ToolCtx` so different
# call shapes (positional auth vs auth-dict; extra bridge-op name; etc.)
# collapse to one signature at the dispatch site.

class ToolCtx:
    __slots__ = ("authorization", "args", "is_internal", "auth")

    def __init__(self, authorization: str | None, args: dict,
                 is_internal: bool, auth: dict | None) -> None:
        self.authorization = authorization
        self.args = args
        self.is_internal = is_internal
        self.auth = auth


async def _tool_read_url(ctx: ToolCtx) -> dict:
    """The host's own page reader, offered to a harness pod over /mcp.
    Same code path as the chat tool (tools._exec_read_url → url_reader.read):
    curl_cffi through the entity's squid, trafilatura extraction, an honest
    `blocked` on refusal. The harness never gets a raw socket to the web."""
    args = ctx.args or {}
    url = str(args.get("url") or "").strip()
    if not url:
        return {"content": [{"type": "text", "text": "url required"}],
                "isError": True}
    try:
        from mastercode.runtime import url_reader
        out = await url_reader.read(url, fmt="markdown",
                                    max_chars=args.get("max_chars"),
                                    offset=args.get("offset"))
    except Exception as e:  # noqa: BLE001 — the harness gets a reason, never a 500
        out = {"status": "error", "result": f"read_url failed: {e}"}
    ok = isinstance(out, dict) and out.get("status") == "ok"
    return {"content": [{"type": "text", "text": json.dumps(out)}],
            "isError": not ok}


# name → (handler, required_scope). `tools/list` filters by ctx.auth["scopes"];
# `tools/call` rejects unknown names AND tools the caller's token lacks scope for.
TOOL_HANDLERS: dict[str, tuple[Any, str]] = {
    # shape / recall / signal and open_app / close_app / list_apps are
    # retired (see PROMPTS above). An outside agent's tools are not in this
    # table: they are server/agent_access/tools.py, behind its own sign-in
    # (_agent_call below).
    # The `factory` scope's tools (order_software, list_projects,
    # project_status, reject_step, ship_project, code_read, code_grep,
    # code_tree, k8s_rollout_status, and before them the ship pipeline and
    # the platform proposal tool) are retired here: no mint
    # path issues the scope (synaptive.create_token's allowlists,
    # bastion_enroll.BASTION_SCOPES, the internal and harness callers above),
    # so nothing could call them. The Host does each of these through its own
    # tools: projects through operate, the platform source through read, a
    # change to it with gitea op='propose', a ship from its root thread.
    # The harness→host fetch channel: the ONLY tool the `fetch` scope grants.
    "read_url":             (_tool_read_url,             "fetch"),
}


# Identity-layer tool → (target_domain, operation) for the route-level CEIGAS
# gate. Its tools left /mcp; the map and _ceigas_gate_synaptive stay while the
# gates that pin the kill-switch crossing through them still do. read_url (the harness `fetch` scope) is not
# in this map: it runs the Host's own page reader, behind the entity's
# egress proxy. The bridge-op tools this comment used to name went with the
# `factory` scope.
_TOOL_CEIGAS_MAP: dict[str, tuple[str, str, int]] = {
    # tool_name → (target_domain, operation, required_bits)
    # Synaptive substrate (the entity's own personality/recall/signal)
    "shape":     ("synaptive.shape", "invoke", 16),  # EXECUTE_TOOL
    "recall":    ("synaptive.shape", "read",    2),  # READ_MEMORY
    "signal":    ("synaptive.shape", "invoke",  8),  # WRITE_MEMORY
    # Entity app management (touches the entity's own app state)
    "open_app":  ("core.entity",     "write",   4),  # WRITE_CONTEXT
    # A stop: closing a window only reduces running activity, so from the Host
    # (source core.entity) it passes the kill switch
    # (ceigas_kill_switch.KILL_SWITCH_STOP_CROSSINGS). Same grant as a write; an
    # external client (external.mcp) is still refused under the switch.
    "close_app": ("core.entity",     "stop",    4),
    "list_apps": ("core.entity",     "read",    1),  # READ_CONTEXT
}


# MCP token scope → capability bit. Same
# 8-bit vocabulary the meta-tool gate uses, narrowed to the scope words
# MCP tokens actually carry. An MCP token's `scopes` list constrains
# WHICH tools the bearer can invoke (enforced at line ~2019 in the
# dispatcher); the bit map below lets the synaptive gate ALSO intersect
# those scopes with the entity's effective_bits — defense in depth,
# parallel to the `app_permissions` narrowing on the chat path.
_MCP_SCOPE_TO_BITS: dict[str, int] = {
    "shape":    16,  # invoke synaptive substrate (EXECUTE_TOOL)
    "recall":    2,  # read entity memory (READ_MEMORY)
    "signal":    8,  # write a behavioral signal (WRITE_MEMORY)
    "identity":  1,  # read identity prompt (READ_CONTEXT)
}


async def _ceigas_gate_synaptive(
    *,
    entity_id: int,
    tool_name: str,
    source_domain: str,
    target_domain: str,
    operation: str,
    required_bits: int,
    token_scopes: list[str] | None,
    is_internal: bool,
    metadata: dict,
) -> dict:
    """The gate for synaptive MCP tools: the capability bits (the Host's
    entity_domain_access grants, narrowed by the token's scopes for an
    external caller), then the operator kill switch (ceigas_kill_switch).

    External bearer-token MCP callers (is_internal=False)
    have their effective bits narrowed to entity_bits ∩ token_scope_bits
    before the crossing decision. Internal SDK shim calls
    (is_internal=True) skip narrowing — they own everything in their
    own pod, same as internal flows on the chat path.

    Returns {allowed: bool, reason: str}. Fails closed on any error.
    """
    # ── Resolve entity_bits + narrow by token_scopes when external ─────────
    entity_bits = 0
    # Whether entity_bits is a READ value or the 0 that stands in when the
    # store could not be read. A denial has to know which: "this host holds
    # (none)" is false when the truth is "could not be read".
    entity_bits_known = False
    try:
        from server import brain_service
        pool = brain_service.get_pool()
    except Exception:
        pool = None
    if pool is not None and entity_id:
        try:
            from mastercode.runtime.meta_tool_caps import (
                _resolve_entity_scopes, scopes_to_bits,
            )
            # strict: a failed read RAISES here instead of returning the
            # empty set, so `entity_bits_known` stays False and the denial
            # below says "could not be read" rather than "holds none".
            entity_scopes = await _resolve_entity_scopes(pool, entity_id, strict=True)
            entity_bits = scopes_to_bits(entity_scopes)
            entity_bits_known = True
        except Exception as e:
            logger.debug("synaptive gate entity-bits resolve failed (non-fatal): %s", e)

    token_bits: int | None = None
    narrowing_active = False
    if not is_internal and token_scopes is not None:
        token_bits = 0
        for s in token_scopes:
            token_bits |= _MCP_SCOPE_TO_BITS.get(s, 0)
        effective_bits = entity_bits & token_bits
        narrowing_active = True
    else:
        effective_bits = entity_bits

    # ── The required-bits subset check is an Invariant 1/2 check.
    # Two checkpoints in sequence — token-scope dispatch at line ~2019
    # already filtered "can this token call this tool"; here we narrow
    # the entity's effective surface so an over-scoped entity can't
    # widen what the token authorized.
    if (effective_bits & required_bits) != required_bits:
        # In the words a host can repeat to a user: the capability
        # by name, and WHICH wall this is — decided by which wall FIRED, not
        # by whether a token was in flight (keying on
        # narrowing_active alone told a caller whose HOST lacked the bit that
        # a token carrying the scope would pass, when it never can). Three
        # cases, and `wall` names the one that applies so a gate or a caller
        # can read it without parsing prose:
        #   token_scope        — the host holds every required bit; the token
        #                        does not. A token carrying the scope passes.
        #   host_domain_access — the host itself lacks a required bit. No
        #                        token changes that.
        #   unresolved         — the host's bits could not be read this call;
        #                        denied fail-closed, and that is what is said.
        #   no_entity          — the call carries no host identity at all, so
        #                        there is nothing to read; a retry of the
        #                        same call cannot pass (fourth review: this
        #                        used to be told "the store will answer").
        # The bit strings stay in the audit row above.
        try:
            from mastercode.runtime.agent_grant import describe as _describe_bits
            _need = _describe_bits(required_bits)
            _host_has = _describe_bits(entity_bits)
            _token_has = _describe_bits(token_bits or 0)
        except Exception:  # noqa: BLE001 — the gate's message never depends on an import
            _need = f"{required_bits:08b}"
            _host_has, _token_has = f"{entity_bits:08b}", f"{(token_bits or 0):08b}"
        if not entity_id:
            wall = "no_entity"
            reason = (
                f"'{tool_name}' needs capability {_need}. This call carries no host "
                f"identity, so there is no capability set to check and it is denied "
                f"fail-closed. That is the call's credential, not the host's grant and "
                f"not a store failure — the same call cannot pass by retrying."
            )
        elif not entity_bits_known:
            wall = "unresolved"
            reason = (
                f"'{tool_name}' needs capability {_need}. This host's own capabilities "
                f"could not be read for this call, so it is denied fail-closed. That is a "
                f"read failure on the host's side — not the caller's token and not a "
                f"missing grant — and the same call can pass once the host's capability "
                f"store answers."
            )
        elif narrowing_active and (entity_bits & required_bits) == required_bits:
            wall = "token_scope"
            reason = (
                f"'{tool_name}' needs capability {_need}. This call's token authorizes "
                f"{_token_has}; the host itself holds {_host_has}. This is the caller's "
                f"token scope, not the host's wall — a token carrying the scope for "
                f"'{tool_name}' is allowed through."
            )
        else:
            wall = "host_domain_access"
            reason = (
                f"'{tool_name}' needs capability {_need}; this host holds {_host_has}. "
                f"This is the host's own domain-access wall — a capability this host "
                f"was not granted — not a machine, network or tool failure, and no token "
                f"or retry changes it."
            )
        return {"allowed": False, "reason": reason, "wall": wall}

    try:
        from ceigas_kill_switch import KILL_SWITCH_REASON, kill_switch_refuses
        if kill_switch_refuses(source_domain, target_domain, operation):
            return {"allowed": False, "reason": KILL_SWITCH_REASON}
    except Exception as e:
        return {"allowed": False, "reason": f"kill-switch check failed: {e}"}
    return {"allowed": True, "reason": "authorized"}


# name → (handler, required_scope). Empty since the identity prompt was
# retired; prompts/list and prompts/get read it.
PROMPT_HANDLERS: dict[str, tuple[Any, str]] = {}


def _tools_for_scopes(scopes: list[str]) -> list[dict]:
    """Filter the advertised TOOLS list to those the caller's scopes can invoke.
    A token with no scopes sees an empty catalog. Internal-auth (full scope set)
    sees everything."""
    allowed = {name for name, (_, scope) in TOOL_HANDLERS.items() if scope in scopes}
    return [t for t in TOOLS if t.get("name") in allowed]


def _prompts_for_scopes(scopes: list[str]) -> list[dict]:
    allowed = {name for name, (_, scope) in PROMPT_HANDLERS.items() if scope in scopes}
    return [p for p in PROMPTS if p.get("name") in allowed]


# ── Per-token runaway backstop (M4) ──────────────────────────────────
#
# Sliding-window counter keyed on token id; a harness pod is keyed on its own
# capability, so one pod's burst never spends another's. Internal (loopback)
# callers are exempt. In-memory only — resets on container restart.
#
# A RUNAWAY BACKSTOP, NOT A THROTTLE. The old
# 60 requests a minute (1 rps) throttled the person's own agent driving their
# Host — one session runs 100+ tool calls with parallel reads. This number
# only has to stop a client stuck in a loop: 600 a minute is 10 requests a
# second sustained for a whole minute, ten times the old ceiling and far past
# any tool-driving session. CSERIES_MCP_RATE_MAX_REQUESTS overrides it; 0 turns
# the backstop off. When it fires, the answer says how long until the next
# request is accepted (and Retry-After carries it).
#
# A coding-harness POD is not that client. It is a confined peer whose only
# grant is `fetch` (read_url), and the auth comment above says it "keeps the
# rate limit": 60 a minute, as before, but per pod (its own capability's
# bucket) so a compare's parallel pods never spend each other's. Not
# overridable and never off — it bounds how hard an untrusted pod can drive
# this kernel's outbound reader.

_RATE_WINDOW_SECONDS = 60.0
_RATE_HARNESS_MAX_REQUESTS = 60


def _rate_max_requests() -> int:
    raw = os.environ.get("CSERIES_MCP_RATE_MAX_REQUESTS", "").strip()
    try:
        return max(0, int(raw)) if raw else 600
    except ValueError:
        return 600


_RATE_MAX_REQUESTS = _rate_max_requests()
_rate_buckets: dict[str, list[float]] = {}


def _rate_limit_for(token_key: str) -> int:
    """The bound for this bucket: 60 a minute for a harness pod, the
    runaway backstop (0 = off) for everyone else."""
    if token_key.startswith("harness:"):
        return _RATE_HARNESS_MAX_REQUESTS
    return _RATE_MAX_REQUESTS


def _rate_retry_after(token_key: str) -> int:
    """Seconds until the oldest request in the window ages out (>= 1)."""
    import time
    bucket = _rate_buckets.get(token_key) or []
    if not bucket:
        return 1
    return max(1, int(bucket[0] + _RATE_WINDOW_SECONDS - time.time()) + 1)


def _rate_check(token_key: str) -> bool:
    """Returns True if the token is under its bound (_rate_limit_for).
    Side-effect: records this timestamp into the bucket. Caller MUST treat
    False as 429. A bound of 0 (outside clients only) means it is off."""
    import time
    limit = _rate_limit_for(token_key)
    if limit <= 0:
        return True
    now = time.time()
    bucket = _rate_buckets.get(token_key)
    if bucket is None:
        bucket = []
        _rate_buckets[token_key] = bucket
    # Drop timestamps that have aged out.
    cutoff = now - _RATE_WINDOW_SECONDS
    bucket[:] = [t for t in bucket if t >= cutoff]
    if len(bucket) >= limit:
        return False
    bucket.append(now)
    return True


def _rate_key(auth: dict, harness_cap: str | None) -> str:
    """The bucket a caller spends from. A harness pod authenticates as id 0
    with its own capability; keying it on that capability keeps parallel
    pods (a compare runs many) from sharing one bucket."""
    if auth.get("_harness") and harness_cap:
        import hashlib
        return "harness:" + hashlib.sha256(harness_cap.encode()).hexdigest()[:16]
    if auth.get("_agent"):
        return f"agent:{auth['_agent']['id']}"
    return f"token:{auth.get('id')}"


# ── Per-method routing ───────────────────────────────────────────────

# Which connected agents declared URL-mode elicitation in their last
# initialize (connection id -> True/False). Keyed on the connection, not a
# session: every client of one connection is the same agent software. Bounded;
# an agent missing from it is answered with the link in plain text, which
# every client shows.
_AGENT_URL_ELICITATION: dict[int, bool] = {}
_AGENT_URL_ELICITATION_MAX = 1000


def _negotiate(requested) -> str:
    return requested if requested in PROTOCOL_VERSIONS else PROTOCOL_VERSION


def _remember_elicitation(auth: dict | None, params: dict, version: str) -> None:
    conn = (auth or {}).get("_agent")
    if not conn:
        return
    caps = params.get("capabilities") if isinstance(params.get("capabilities"), dict) else {}
    elicit = caps.get("elicitation")
    url_ok = isinstance(elicit, dict) and isinstance(elicit.get("url"), dict) \
        and version == "2025-11-25"
    if len(_AGENT_URL_ELICITATION) >= _AGENT_URL_ELICITATION_MAX:
        _AGENT_URL_ELICITATION.pop(next(iter(_AGENT_URL_ELICITATION)))
    _AGENT_URL_ELICITATION[int(conn["id"])] = url_ok


async def _agent_call(rid, auth: dict, name, args, origin: str) -> dict:
    """tools/call from an outside agent (server/agent_access/tools.py). A call
    the person has to approve first ends with -32042 and its link when the
    client declared URL-mode elicitation, else with the link in the result's
    text: nothing ran either way."""
    from server.agent_access import tools as agent_tools
    conn = auth["_agent"]
    if not isinstance(args, dict):
        return _err(rid, _INVALID_PARAMS, "arguments must be an object")
    pool = brain_service.get_pool()
    eid = int(auth.get("entity_id") or 0)
    try:
        return _ok(rid, await agent_tools.call(pool, eid, conn, name, args, origin))
    except KeyError:
        return _err(rid, _METHOD_NOT_FOUND, f"unknown tool: {name}")
    except agent_tools.NeedsApproval as need:
        if _AGENT_URL_ELICITATION.get(int(conn["id"])):
            return {"jsonrpc": "2.0", "id": rid, "error": {
                "code": URL_ELICITATION_REQUIRED, "message": need.message,
                "data": {"elicitations": [{"mode": "url", "message": need.message,
                                           "url": need.url, "elicitationId": need.ask_id}]}}}
        return _ok(rid, {"content": [{"type": "text", "text": need.message}], "isError": True,
                         "structuredContent": {"state": "needs_approval", "url": need.url,
                                               "ask": need.ask_id}})


async def _handle_one(
    req: dict,
    authorization: str,
    is_internal: bool = False,
    auth: dict | None = None,
    origin: str = "",
) -> dict | None:
    """Handle a single JSON-RPC request. Returns the response dict, or None
    if the request was a notification (no id) and therefore takes no response."""
    if not isinstance(req, dict):
        return _err(None, _INVALID_REQUEST, "request must be an object")
    if req.get("jsonrpc") != "2.0":
        return _err(req.get("id"), _INVALID_REQUEST, "jsonrpc must be '2.0'")

    method = req.get("method")
    if not isinstance(method, str):
        return _err(req.get("id"), _INVALID_REQUEST, "method must be a string")

    is_notification = "id" not in req
    rid = req.get("id")
    params = req.get("params") or {}
    if not isinstance(params, dict):
        if is_notification:
            return None
        return _err(rid, _INVALID_PARAMS, "params must be an object")

    try:
        if method == "initialize":
            version = _negotiate(params.get("protocolVersion"))
            _remember_elicitation(auth, params, version)
            return _ok(rid, {
                "protocolVersion": version,
                "capabilities": {
                    "tools": {"listChanged": False},
                    "prompts": {"listChanged": False},
                },
                "serverInfo": SERVER_INFO,
            })

        if method == "notifications/initialized":
            # Client signalling it's ready. Notification — no response.
            return None

        if method == "ping":
            return _ok(rid, {})

        scopes: list[str] = list((auth or {}).get("scopes") or [])

        if method == "tools/list":
            if (auth or {}).get("_agent"):
                from server.agent_access import tools as agent_tools
                return _ok(rid, {"tools": agent_tools.TOOLS})
            # Scope-filtered catalog: don't advertise tools the caller can't invoke.
            return _ok(rid, {"tools": _tools_for_scopes(scopes)})

        if method == "tools/call":
            name = params.get("name")
            args = params.get("arguments") or {}
            if (auth or {}).get("_agent"):
                return await _agent_call(rid, auth, name, args, origin)
            entry = TOOL_HANDLERS.get(name)
            if entry is None:
                return _err(rid, _METHOD_NOT_FOUND, f"unknown tool: {name}")
            handler, required_scope = entry
            if required_scope not in scopes:
                # Don't leak which tools exist beyond the caller's scope —
                # same response as "unknown" so the catalog and the dispatcher
                # tell the same story to the client.
                return _err(rid, _METHOD_NOT_FOUND, f"unknown tool: {name}")

            # The CEIGAS gate. For tools in
            # _TOOL_CEIGAS_MAP, check the capability bits and the kill
            # switch before dispatching. Source domain depends on caller surface:
            # internal SDK shim (INTERNAL_API_SECRET) → core.entity;
            # external bearer-token MCP client → external.mcp. A tool
            # NOT in the map (read_url, the `fetch` scope) passes through.
            ceigas_spec = _TOOL_CEIGAS_MAP.get(name)
            if ceigas_spec is not None:
                target_domain, operation, required_bits = ceigas_spec
                source_domain = "core.entity" if is_internal else "external.mcp"
                gate = await _ceigas_gate_synaptive(
                    entity_id     = int((auth or {}).get("entity_id") or 0),
                    tool_name     = name,
                    source_domain = source_domain,
                    target_domain = target_domain,
                    operation     = operation,
                    required_bits = required_bits,
                    # External bearer-token MCP callers get
                    # their effective bits narrowed by the token's
                    # `scopes` list. Internal SDK shim → scopes=None
                    # → no narrowing (entity owns its own pod).
                    token_scopes  = (list(scopes) if not is_internal else None),
                    is_internal   = is_internal,
                    metadata      = {"tool": name, "args_keys": sorted(args.keys())},
                )
                if not gate["allowed"]:
                    return _err(rid, _METHOD_NOT_FOUND,
                                f"denied by CEIGAS: {gate['reason']}")

            ctx = ToolCtx(
                authorization=authorization, args=args,
                is_internal=is_internal, auth=auth,
            )
            return _ok(rid, await handler(ctx))

        if method == "prompts/list":
            return _ok(rid, {"prompts": _prompts_for_scopes(scopes)})

        if method == "prompts/get":
            name = params.get("name")
            entry = PROMPT_HANDLERS.get(name)
            if entry is None:
                return _err(rid, _METHOD_NOT_FOUND, f"unknown prompt: {name}")
            phandler, required_scope = entry
            if required_scope not in scopes:
                return _err(rid, _METHOD_NOT_FOUND, f"unknown prompt: {name}")
            return _ok(rid, await phandler(authorization))

    except Exception as e:
        logger.exception("MCP method %s raised", method)
        if is_notification:
            return None
        return _err(rid, _INTERNAL_ERROR, f"{type(e).__name__}: {e}")

    if is_notification:
        return None
    return _err(rid, _METHOD_NOT_FOUND, f"unknown method: {method}")


# ── HTTP entry point ─────────────────────────────────────────────────

@router.get("/mcp")
async def mcp_info(request: Request):
    """POST /mcp is the protocol. A client asking for a server-sent event
    stream here is told there is none (405, MCP streamable HTTP, listening);
    a browser gets a short explainer."""
    from fastapi.responses import JSONResponse
    if "text/event-stream" in (request.headers.get("accept") or ""):
        return JSONResponse({"error": "no event stream; POST JSON-RPC to /mcp"}, status_code=405,
                            headers={"Allow": "POST"})
    return {
        "ok": True,
        "endpoint": "this Host's MCP server (JSON-RPC 2.0 over POST)",
        "protocol_versions": list(PROTOCOL_VERSIONS),
        "server": SERVER_INFO,
        "note": (
            "Add this address to Claude Code, Codex or any MCP client. It signs in "
            "through this Host (OAuth): you approve the connection on a page of your "
            "Host, and every change it asks for on your machines waits for your approval."
        ),
    }


# L3 — max request body size at the route: defense-in-depth for the in-pod
# listener (internal / loopback callers and harness pods never pass the edge).
#
# 16 MiB, the REAL downstream bound: a call
# that reaches the bridge goes as ONE WebSocket frame, whose ws_max_size is
# 16 MiB (server/routes/context_extract.py, MAX_SOURCE_BYTES: "uvicorn's
# 16 MiB ws_max_size"). It was sized for the platform proposal tool's whole
# file contents, which the old 1 MiB refused; that tool is retired
# (a platform change is the Host's gitea op='propose'), and the
# bound stays the frame's. Memory stays bounded: the body is read as a stream
# under this cap (harness_input.read_request) and parsed under the shared
# JSON node budget (harness_input.parse_json).
_MCP_MAX_BODY_BYTES = 16 * 1024 * 1024  # 16 MiB
# A coding-harness POD is an untrusted, confined peer whose only grant is
# `fetch` (read_url: a URL and two integers). Its bound stays 1 MiB — a
# platform protection: a pod must not make the kernel hold 16 MiB per
# request (scripts/test_sidecar_workspace_routes_harness_only.py, inbound_mcp).
_MCP_HARNESS_BODY_BYTES = 1 * 1024 * 1024  # 1 MiB


@router.post("/mcp")
async def mcp_endpoint(
    request: Request,
    authorization: str | None = Header(None),
    x_cseries_hardware_fingerprint: str | None = Header(None),
    x_cseries_internal_auth: str | None = Header(None),
    x_cseries_harness_cap: str | None = Header(None),
):
    # Authentication is wrapped here so failures become JSON-RPC error
    # envelopes instead of bare FastAPI HTTPException
    # bodies. Clients then handle one error shape across the whole protocol
    # surface, not two.
    #
    # NOTE: we still raise HTTPException for non-protocol failures (oversized
    # body, malformed JSON) — those are HTTP-layer concerns and the JSON-RPC
    # envelope cannot carry a meaningful id (the body wasn't parsed yet).
    from fastapi import Response
    try:
        auth = await _auth_or_401(
            authorization,
            hardware_fingerprint=x_cseries_hardware_fingerprint,
            internal_auth=x_cseries_internal_auth,
            harness_cap=x_cseries_harness_cap,
            internal_ok=in_container(request),
        )
    except HTTPException as exc:
        # A 401 names where to sign in (RFC 9728 §5.1, MCP authorization): a
        # standard client starts its OAuth flow from this header, never from a
        # redirect.
        headers = {}
        if exc.status_code == 401:
            from server.agent_access import oauth as agent_oauth
            from server.agent_access.origin import public_origin
            headers["WWW-Authenticate"] = agent_oauth.challenge_header(await public_origin(request))
        return Response(
            content=json.dumps(_err(None, _INVALID_REQUEST, str(exc.detail))),
            status_code=exc.status_code,
            headers=headers,
            media_type="application/json",
        )
    is_internal = bool(auth.get("_internal"))

    # P3 — device->host NAC wall: a quarantined bastion is refused here, before any tool
    # dispatch or the per-tool capability gate. Default-preserve (no policy => permit);
    # bearer-only + internal callers are exempt (sovereign full access).
    nac_deny = await _nac_gate(auth)
    if nac_deny is not None:
        return Response(
            content=json.dumps(_err(None, _INVALID_REQUEST,
                "device refused by network access control (quarantined): "
                + str(nac_deny.get("why", "")))),
            status_code=403,
            media_type="application/json",
        )

    # M4 — per-token rate limit. Sliding-window keyed on token id; internal
    # callers (id=0) effectively unrate-limited (they're loopback). Returning
    # 429 is the explicit signal a future API consumer can back-off on.
    token_key = _rate_key(auth, x_cseries_harness_cap) if not is_internal else "internal"
    if not is_internal and not _rate_check(token_key):
        retry = _rate_retry_after(token_key)
        if token_key.startswith("harness:"):
            why = (f"harness pod rate limit: a coding-harness pod may send "
                   f"{_RATE_HARNESS_MAX_REQUESTS} requests in {int(_RATE_WINDOW_SECONDS)}s "
                   f"(it is a confined peer) — the next is accepted in {retry}s")
        else:
            why = (f"runaway backstop: this token sent {_RATE_MAX_REQUESTS} requests "
                   f"in {int(_RATE_WINDOW_SECONDS)}s — the next is accepted in {retry}s")
        return Response(
            content=json.dumps(_err(None, _INVALID_REQUEST, why)),
            status_code=429,
            headers={"Retry-After": str(retry)},
            media_type="application/json",
        )

    # L3 — refuse oversized bodies before parsing.
    body_cap = _MCP_HARNESS_BODY_BYTES if auth.get("_harness") else _MCP_MAX_BODY_BYTES
    content_length = request.headers.get("content-length")
    if content_length:
        try:
            if int(content_length) > body_cap:
                raise HTTPException(413, f"request body exceeds {body_cap} bytes")
        except ValueError:
            raise HTTPException(400, "invalid content-length")

    # Read as a STREAM under the cap: a chunked body carries no
    # Content-Length, and request.body() read it whole before the length was
    # checked, so any caller that passed auth (a harness pod holds its own
    # capability) could make the kernel buffer as much as it sent.
    from server import harness_input as _hin
    try:
        raw = await _hin.read_request(request, body_cap, "mcp request")
    except _hin.HarnessInputRefused:
        raise HTTPException(413, f"request body exceeds {body_cap} bytes")
    try:
        body = await _hin.parse_json(raw, "mcp request")
    except _hin.HarnessInputRefused as exc:
        raise HTTPException(400, f"invalid JSON body: {exc}")
    except Exception:
        raise HTTPException(400, "invalid JSON body")

    origin = ""
    if auth.get("_agent"):
        from server.agent_access.origin import public_origin
        origin = await public_origin(request)

    # JSON-RPC supports batch (array) and single (object). Honor both.
    if isinstance(body, list):
        if not body:
            raise HTTPException(400, "empty batch")
        responses: list[dict] = []
        for entry in body:
            resp = await _handle_one(entry, authorization, is_internal=is_internal, auth=auth,
                                     origin=origin)
            if resp is not None:
                responses.append(resp)
        # Per spec: if all entries were notifications, return nothing.
        if not responses:
            return Response(status_code=204)
        return responses

    if isinstance(body, dict):
        resp = await _handle_one(body, authorization, is_internal=is_internal, auth=auth,
                                 origin=origin)
        if resp is None:
            return Response(status_code=204)
        return resp

    raise HTTPException(400, "expected JSON-RPC object or batch array")
