"""
Network HTTP API — the host's view of its sovereign network.

Bastions (machines the host reaches), peers/secured channels, and a one-call
health overview. The PLATFORM never controls the host's outbound — the old
platform-imposed egress allowlist/posture machinery was removed 2026-06-19 as rot
(E0). What this API exposes (P6) is the USER's OWN egress policy: opt-in rules the
user authors through the NAC user-gate, DEFAULT OPEN (full internet), signed by the
host and enforced at a single in-pod routing point that never logs destinations.
The user owns their outbound; this only lets them choose to limit it.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Optional

import asyncpg
from fastapi import APIRouter, HTTPException, Query, Request, Depends, Header, WebSocket, WebSocketDisconnect
from server.applications.middleware import requires_permission

from server import brain_service
from server.network import bastion_enroll, topology

logger = logging.getLogger("privatae.network.routes")

router = APIRouter(prefix="/api/network", tags=["network"])

# The NAC posture query, hoisted to module scope so its GATE can import the real
# thing. scripts/test_nac_endpoints_dedupe.py used to copy-paste it,
# which meant the gate tested a snapshot: change the route's ORDER BY or its JOIN
# and the test went on passing about a query nobody runs. Read the reasoning for
# the JOIN, the DISTINCT ON and the tie-break at the call site in nac_endpoints.
NAC_ENDPOINTS_SQL = (
    "SELECT DISTINCT ON (e.hostname) "
    "       e.token_id, e.hostname, e.classification, e.posture, e.posture_at, "
    "       e.decided_at, e.last_decision, e.fingerprint, "
    "       t.hardware_fingerprint AS bound_fingerprint, "
    "       (t.expires_at IS NOT NULL AND t.expires_at <= now()) AS token_expired "
    "  FROM nac_endpoints e "
    "  JOIN mcp_client_tokens t ON t.id = e.token_id "
    " WHERE t.operator_kind = 'bastion' AND t.revoked_at IS NULL "
    " ORDER BY e.hostname, e.token_id DESC, e.posture_at DESC NULLS LAST"
)


# ══════════════════════════════════════════════════════════════════════
# Network app — topology surface (bastions, peers, health overview).
#
# The read layer the Network system app renders: the host's bastions (machines
# it reaches), its peers/secured channels, and a one-call health snapshot. All
# reads are best-effort and degrade to empty rather than 500 on a pod missing a
# table.
# ══════════════════════════════════════════════════════════════════════


def _bridge_status() -> dict:
    """Bridge (control-plane) reachability — the only connectivity signal that is
    continuously known (the peer :8444 lane is connectionless)."""
    try:
        from server.main import get_bridge_client
        c = get_bridge_client()
        if c is None:
            return {"connected": False}
        return c.status
    except Exception:
        return {"connected": False}


def _peer_health() -> dict:
    """Peer-transport + trust health, read defensively from the lifespan refs.
    These are logged at boot but otherwise unreadable by an app."""
    out = {"transport_up": False, "trust_ready": False, "crl_epoch": None, "keys": 0}
    try:
        from server import main as _m
        ps = getattr(_m, "_peer_server_ref", None)
        out["transport_up"] = bool(ps is not None and getattr(ps, "_server", None) is not None)
        pt = getattr(_m, "_peer_trust_ref", None)
        if pt is not None:
            out["trust_ready"] = bool(getattr(pt, "_ready", False))
            out["crl_epoch"] = getattr(pt, "_crl_epoch", None)
            keys = getattr(pt, "_keys", None)
            out["keys"] = len(keys) if keys else 0
    except Exception:
        pass
    return out


def _self_identity(eid: int) -> dict:
    handle = (os.environ.get("ENTITY_HANDLE") or os.environ.get("HANDLE") or "").strip()
    return {"entity_id": eid, "handle": handle or None}


@router.get("/overview", dependencies=[Depends(requires_permission("read:topology"))])
async def get_overview():
    """One-call health snapshot for the Network app's Topology lens + header."""
    pool = brain_service.get_pool()
    eid = brain_service._entity_id

    bastions: list = []
    peers = {"contacts": [], "secured_channels": []}
    unrequested_agents = 0
    if pool:
        bastions = await topology.list_bastions(pool, eid)
        peers = await topology.list_peers(pool, eid)
        from server.network import nac_agents
        unrequested_agents = await nac_agents.count_unrequested(pool, eid)

    return {
        "identity": _self_identity(eid),
        "bridge": _bridge_status(),
        "peer": _peer_health(),
        "counts": {
            "bastions": len(bastions),
            "bastions_online": sum(1 for g in bastions if g.get("online")),
            "contacts": len(peers.get("contacts", [])),
            "secured_channels": len(peers.get("secured_channels", [])),
            "unrequested_agents": unrequested_agents,
        },
    }


@router.get("/bastions", dependencies=[Depends(requires_permission("read:topology"))])
async def get_bastions():
    """The host's bastion machines (operator_kind='bastion' tokens + heartbeat)."""
    pool = brain_service.get_pool()
    eid = brain_service._entity_id
    from server.network import nac_commit
    return await nac_commit.bastions_list(pool, eid)


@router.get("/bastions/directory", dependencies=[Depends(requires_permission("read:topology"))])
async def get_bastion_directory():
    """The other Hosts on each machine this Host is paired to, with the Host id
    each peering resolved to (server/network/bastion_directory.py). The same
    narrowed list GET /api/circles/bastions returns, under read:topology alone:
    it names this Host's machines and who shares them, which this scope already
    covers, so the Network app can offer Request connection and Message without
    holding read:circles (every Circles conversation)."""
    pool = brain_service.get_pool()
    if pool is None:
        return {"machines": []}
    from server.network import bastion_directory, peer_discovery
    eid = brain_service._entity_id
    bastions = await topology.list_bastions(pool, eid)
    peerings = await peer_discovery.list_peerings(pool, eid)
    return {"machines": bastion_directory.build_directory(bastions, peerings)}


@router.get("/agents", dependencies=[Depends(requires_permission("read:topology"))])
async def get_agents():
    """Agent/AI activity observed by the host's bastions (the OBSERVE plane). Visibility only — soft
    + evadable; the hard wall is the NAC. `requested` per agent + `unrequested_count` reconcile
    against the host's delegation tags (P9.2): unrequested = "agent activity you didn't request"."""
    pool = brain_service.get_pool()
    if not pool:
        return {"agents": [], "unrequested_count": 0}
    from server.network import nac_agents
    agents = await nac_agents.list_observations(pool, brain_service._entity_id)
    # Apps (kind='app') are listed but never counted as unrequested: the user is
    # running them. Automations (kind='automation', bastion 0.13.12) likewise:
    # no model, so inventory, not an agent. Unrequested = agents and unknown
    # model clients only, counted per FAMILY — the unit the lens draws a card for.
    unrequested = len({a.get("family") or f"{a.get('hostname')}|{a.get('pid')}"
                       for a in agents if a.get("active") and not a.get("requested")
                       and not a.get("acknowledged")
                       and (a.get("kind") or "agent") not in nac_agents._NEVER_ALERTED})
    return {"agents": agents, "unrequested_count": unrequested}


@router.post("/agent-delegations", dependencies=[Depends(requires_permission("write:network"))])
async def mint_agent_delegation(request: Request):
    """Mint a delegation tag (P9.2). Launch an agent with PRIVATAE_AGENT_TOKEN=<token> to mark it
    REQUESTED on a server. Body: {server?, note?, ttl_hours?}; server defaults to '*'. The token is
    returned ONCE. Soft + expiring + revocable — it separates sanctioned agents from casual ones."""
    pool = brain_service.get_pool()
    if not pool:
        raise HTTPException(503, "brain pool not ready")
    from server.network import nac_agents
    await nac_agents.ensure_delegation_acl(pool)
    body = await request.json() if request.headers.get("content-type", "").startswith("application/json") else {}
    server = (body.get("server") or "*").strip() or "*"
    note = (body.get("note") or "").strip() or None
    ttl_hours = int(body.get("ttl_hours") or 24)
    import secrets as _sec, datetime as _dt
    token = "del_" + _sec.token_urlsafe(18)
    expires = (_dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(hours=ttl_hours)).replace(microsecond=0)
    gid = await pool.fetchval(
        "INSERT INTO nac_agent_delegations (token, server, note, expires_at) VALUES ($1,$2,$3,$4) RETURNING id",
        token, server, note, expires)
    await nac_agents.audit_disposition(pool, entity_id=brain_service._entity_id,
                                       disposition="sanction", target=server,
                                       detail={"delegation_id": gid, "ttl_hours": ttl_hours, "note": note})
    return {"id": gid, "token": token, "server": server, "expires": expires.isoformat(),
            "env": f"PRIVATAE_AGENT_TOKEN={token}"}


@router.get("/agent-delegations", dependencies=[Depends(requires_permission("read:topology"))])
async def list_agent_delegations():
    """The host's delegation tags (without the secret token). `active` = not revoked + not expired."""
    pool = brain_service.get_pool()
    if not pool:
        return {"delegations": []}
    from server.network import nac_agents
    await nac_agents.ensure_delegation_acl(pool)
    rows = await pool.fetch(
        "SELECT id, server, note, issued_at, expires_at, revoked_at, "
        "(revoked_at IS NULL AND (expires_at IS NULL OR expires_at > now())) AS active "
        "FROM nac_agent_delegations ORDER BY id DESC")
    out = []
    for r in rows:
        d = dict(r)
        for k in ("issued_at", "expires_at", "revoked_at"):
            if d.get(k) is not None and hasattr(d[k], "isoformat"):
                d[k] = d[k].isoformat()
        out.append(d)
    return {"delegations": out}


@router.post("/agent-delegations/{del_id}/revoke", dependencies=[Depends(requires_permission("write:network"))])
async def revoke_agent_delegation(del_id: int):
    """Revoke a delegation tag — agents carrying it become UNREQUESTED on the next heartbeat."""
    pool = brain_service.get_pool()
    if not pool:
        raise HTTPException(503, "brain pool not ready")
    from server.network import nac_agents
    await nac_agents.ensure_delegation_acl(pool)
    await pool.execute("UPDATE nac_agent_delegations SET revoked_at=now() WHERE id=$1 AND revoked_at IS NULL", del_id)
    await nac_agents.audit_disposition(pool, entity_id=brain_service._entity_id,
                                       disposition="revoke_delegation", target=str(del_id))
    return {"id": del_id, "revoked": True}


@router.post("/agents/contain", dependencies=[Depends(requires_permission("write:network"))])
async def contain_agent(request: Request):
    """Contain (terminate) a detected agent on a bastion (P9.3) — the host directs the bastion to
    kill the pid's process tree (SIGKILL, tree frozen first — client-go/killtree.go) on its next
    heartbeat; the bastion REFUSES a pid that isn't a detected agent.
    Body: {token_id, pid}. The surgical containment; revoke_bastion is the broad 'cut the whole
    server's mesh reach' kill. observe -> reconcile -> contain.

    NAC agent: this is a MANAGEMENT op. It goes through run_mgmt_op like
    every other action on a box, so it needs a user-approved signed grant listing 'contain'
    for that server (default-deny, 403 without one). The app permission alone no longer
    fires a kill; the grant does."""
    pool = brain_service.get_pool()
    if not pool:
        raise HTTPException(503, "brain pool not ready")
    body = await request.json() if request.headers.get("content-type", "").startswith("application/json") else {}
    token_id, pid = body.get("token_id"), body.get("pid")
    if not isinstance(token_id, int) or not isinstance(pid, int):
        raise HTTPException(400, "token_id and pid (ints) required")
    eid = brain_service._entity_id
    from server.network import nac_commit, nac_mgmt
    target = await nac_commit.resolve_bastion(pool, eid, token_id=token_id)
    if not target or not target.get("server"):
        raise HTTPException(404, "bastion not found")
    try:
        # run_mgmt_op refuses an APP (409): the desktop app, an IDE, a browser
        # are shown here, never terminated from here. The bastion holds the same
        # line, and contain takes the agent's whole process tree when it does run.
        res = await nac_mgmt.run_mgmt_op(pool, eid, target["server"], nac_mgmt.CONTAIN_OP, [pid],
                                         token_id=token_id, approval_wait_s=0)
    except nac_mgmt.MgmtError as e:
        raise HTTPException(e.status, e.detail)
    if res.get("exit") == 126:
        raise HTTPException(403, res.get("stderr") or
                            "containing an agent needs a management grant listing 'contain' for this server")
    from server.network import nac_agents as _na
    await _na.audit_disposition(pool, entity_id=eid,
                                disposition="contain", token_id=token_id, pid=pid)
    return {"token_id": token_id, "pid": pid, "server": target["server"],
            "contain": _queued_word(res)}


@router.post("/findings/contain", dependencies=[Depends(requires_permission("write:network"))])
async def contain_finding(request: Request):
    """Stop a program the bastion's delta reported. Body: {token_id, fp} — the
    finding's fingerprint, never a pid. The same management op and signed grant as
    /agents/contain (`contain`, default-deny); the bastion resolves the fingerprint to what is
    running under it when the order arrives, refuses anything that is not a finding (a baseline
    entry, an app, something it started), and answers on its next beat."""
    pool = brain_service.get_pool()
    if not pool:
        raise HTTPException(503, "brain pool not ready")
    body = await request.json() if request.headers.get("content-type", "").startswith("application/json") else {}
    token_id, fp = body.get("token_id"), body.get("fp")
    if not isinstance(token_id, int) or not isinstance(fp, str):
        raise HTTPException(400, "token_id (int) and fp (the finding's fingerprint) required")
    eid = brain_service._entity_id
    from server.network import nac_commit, nac_mgmt
    target = await nac_commit.resolve_bastion(pool, eid, token_id=token_id)
    if not target or not target.get("server"):
        raise HTTPException(404, "bastion not found")
    try:
        res = await nac_mgmt.run_mgmt_op(pool, eid, target["server"], nac_mgmt.CONTAIN_OP,
                                         [nac_mgmt.FINDING_ARG_PREFIX + fp], token_id=token_id,
                                         approval_wait_s=0)
    except nac_mgmt.MgmtError as e:
        raise HTTPException(e.status, e.detail)
    if res.get("exit") == 126:
        raise HTTPException(403, res.get("stderr") or
                            "stopping a finding needs a management grant listing 'contain' for this server")
    return {"token_id": token_id, "fp": fp.strip().lower(), "server": target["server"],
            "contain": _queued_word(res)}


def _queued_word(res: dict) -> str:
    """What run_mgmt_op actually did — "queued" or "already queued" — read from
    its result rather than assumed. The routes used to answer "queued" for a
    click that added nothing."""
    try:
        word = json.loads(res.get("stdout") or "{}").get("contain")
    except (TypeError, ValueError):
        word = None
    return word if word in ("queued", "already queued") else "queued"


@router.post("/agents/acknowledge", dependencies=[Depends(requires_permission("write:network"))])
async def acknowledge_agent(request: Request):
    """Acknowledge an unrequested agent (P9.4) — mark it seen/accepted WITHOUT sanctioning it (no
    delegation tag). It leaves the unrequested alert count and stops re-alerting, but stays
    unrequested; a NEW pid still re-alerts. The middle disposition between sanction and contain.
    Body: {token_id, pid}."""
    pool = brain_service.get_pool()
    if not pool:
        raise HTTPException(503, "brain pool not ready")
    body = await request.json() if request.headers.get("content-type", "").startswith("application/json") else {}
    token_id, pid = body.get("token_id"), body.get("pid")
    if not isinstance(token_id, int) or not isinstance(pid, int):
        raise HTTPException(400, "token_id and pid (ints) required")
    from server.network import nac_agents
    if not await nac_agents.acknowledge(pool, brain_service._entity_id, token_id, pid):
        raise HTTPException(404, "agent observation not found")
    await nac_agents.audit_disposition(pool, entity_id=brain_service._entity_id,
                                       disposition="acknowledge", token_id=token_id, pid=pid)
    return {"token_id": token_id, "pid": pid, "acknowledged": True}


@router.post("/bastions/setup", dependencies=[Depends(requires_permission("write:network"))])
async def setup_bastion(request: Request):
    """Mint a short-lived single-use PAIRING TOKEN for a new bastion. Shown once on
    the dashboard; the user installs the cseries-bastion binary and runs
    `cseries-bastion pair <token>`, which generates its own keys and redeems the token
    for its cert + routing. No bundle, no file to ship, no Docker.

    A Host with no platform also answers its CA (host_ca_pem), the redeem URL on
    its enroll port and the lane's address: a bastion on another machine pairs
    over TCP and trusts that CA alone. On a server of its own (CSERIES_LANE_HOST)
    the pair command shown is that TCP pairing; and where the server's firewall
    keeps the enroll port closed (CSERIES_DOOR_HELPER), a fresh passkey check is
    needed first, and the port is opened to the requester's address for the
    pairing window, which the answer gives (pairing_window)."""
    pool = brain_service.get_pool()
    if not pool:
        raise HTTPException(503, "brain pool not ready")
    eid = brain_service._entity_id
    body = await request.json() if request.headers.get("content-type", "").startswith("application/json") else {}
    from server.network import door_helper, local_bastion, nac_commit
    standalone = local_bastion.standalone()
    window = await _open_pairing_window(request) if standalone and door_helper.helper_path() else None
    try:
        minted = await nac_commit.setup_bastion(pool, eid, (body.get("name") or ""), window=window)
    except nac_commit.CommitError as e:
        raise HTTPException(e.status, e.detail)
    return minted


async def _open_pairing_window(request: Request) -> dict:
    """Open the enroll port to the requester for the pairing window, after
    the owner's passkey check in the last five minutes. The requester is the
    socket peer, or behind a trusted proxy the address it forwarded for."""
    from server import owner_passkeys, trusted_proxies
    from server.home_origin import HOME
    from server.middleware.owner_session import live_session
    from server.network import door_helper
    _email, token = await live_session(request.scope)
    if not owner_passkeys.fresh_step_up(token):
        raise HTTPException(403, "Pairing a machine needs your passkey in the last five minutes: "
                                 f"open {HOME.origin}/passkey, confirm there with your passkey, then "
                                 "set up the bastion again.")
    seen = trusted_proxies.visitor(request.client.host if request.client else None,
                                   request.headers.get("x-forwarded-for"))
    addr = door_helper.public_address(seen)
    if not addr:
        raise HTTPException(400, "The pairing port opens only to a public address, and this "
                                 f"request came from {seen}.")
    try:
        expires = await door_helper.open_pairing(addr)
    except door_helper.HelperError as e:
        raise HTTPException(502, f"The pairing port did not open: {e}")
    logger.info("pairing port opened to %s for %ds", addr, expires)
    return {"address": addr, "expires_in_s": expires}


@router.post("/bastions/redeem")
async def redeem_bastion(request: Request):
    """PUBLIC, token-authed: a cseries-bastion daemon redeems its pairing token for a
    cert + routing. Reachable cert-less via the host's public edge ingress; the
    one-time pairing secret is the auth (no app session, no client cert). The bastion
    generated its own keys and sends only its WG pubkey + a CSR."""
    # Reserved control-plane lane: enrolment must not queue behind a busy app
    # for one of this pod's 16 postgres connections. Measured 2026-09-07 — when the
    # server ran out, redeem answered 502 with no log line anywhere.
    pool = brain_service.get_control_pool()
    if not pool:
        raise HTTPException(503, "brain pool not ready")
    eid = brain_service._entity_id
    body = await request.json() if request.headers.get("content-type", "").startswith("application/json") else {}
    # The bastion knows its target handle (decoded from the pairing token) and sends
    # it in the body; the URL-path handle was consumed by the edge enrollment route.
    # Prefer the body handle, fall back to resolving the host's own handle. The bearer
    # + cert are scoped to THIS entity_id regardless — the handle only forms host_name
    # (the *.cseries SAN the bastion verifies), so a bastion can't aim enrollment elsewhere.
    req_handle = (body.get("handle") or "").strip()
    handle = req_handle or await bastion_enroll.resolve_self_handle(eid)
    return await bastion_enroll.redeem(
        pool, entity_id=eid, handle=handle,
        secret=(body.get("secret") or ""),
        bastion_pubkey=(body.get("wg_public_key") or ""),
        operator_csr=(body.get("operator_csr") or ""),
        hostname=(body.get("hostname") or ""),
        platform=(body.get("platform") or ""),
    )


@router.post("/bastions/heartbeat")
async def bastion_heartbeat(
    request: Request,
    authorization: Optional[str] = Header(None),
):
    """A bastion daemon reports its hostname/version/tunnel/wg. Authed by the
    bastion's OWN bearer (the operator_kind='bastion' token minted at redeem) — the
    bastion holds that, not INTERNAL_API_SECRET. It reaches this over its tunnel +
    mTLS; localhost_only lets this path through to the bearer check (like /mcp). The
    token_id is derived from the bearer, never the body, so a bastion can only ever
    heartbeat ITSELF (and the fp is TOFU-bound on the first beat)."""
    # Reserved control-plane lane: the beat also carries the endpoint-rotation
    # directive, so starving it costs relay failover, not just a stale last_seen_at.
    pool = brain_service.get_control_pool()
    if not pool:
        raise HTTPException(503, "brain pool not ready")
    bearer = ""
    if authorization and authorization.lower().startswith("bearer "):
        bearer = authorization[7:].strip()
    if not bearer:
        raise HTTPException(401, "bastion bearer required")
    import hashlib
    token_hash = hashlib.sha256(bearer.encode()).digest()
    row = await pool.fetchrow(
        "SELECT id, entity_id FROM mcp_client_tokens "
        "WHERE token_hash = $1 AND operator_kind = 'bastion' AND revoked_at IS NULL",
        token_hash,
    )
    if row is None:
        raise HTTPException(401, "invalid or revoked bastion bearer")
    body = dict(await request.json() if request.headers.get("content-type", "").startswith("application/json") else {})
    body["token_id"] = row["id"]   # bearer-derived — never trust a body-supplied id
    rec = await topology.upsert_heartbeat(pool, row["entity_id"], body)
    if rec is None:
        raise HTTPException(404, "unknown or non-bastion token")
    # P9.0: record the bastion's observed-agent snapshot (the OBSERVE plane — visibility only;
    # soft + evadable, never relied on for security, which is the NAC). P9.4: then reconcile and
    # push any UNREQUESTED activity to the host's own inbox so it finds the user. Best-effort.
    try:
        from server.network import nac_agents
        await nac_agents.ingest(pool, row["id"], body.get("hostname"), body.get("agents"))
        await nac_agents.notify_unrequested(pool, row["entity_id"])
    except Exception:
        pass
    # Embedding auto-wire: a host with NO embeddings configured
    # and a machine that just checked in serving an embedding model gets wired to
    # it here — the user connected the machine, that is the request. One
    # transition only (unconfigured -> configured), probe-gated, caps-gated,
    # best-effort: liveness recording never depends on it.
    try:
        from mastercode import embed_autowire
        await embed_autowire.maybe_autowire(pool, row["entity_id"], body.get("hostname"))
    except Exception:
        pass
    return rec


@router.get("/bastions/git-credential")
async def bastion_git_credential(authorization: Optional[str] = Header(None)):
    """The bastion's `git-credential` helper fetches the USER's platform-minted gitea
    token here, so their local git never stores a password (build 3, 2026-08-28).
    Authed by the bastion's OWN bearer, like /bastions/heartbeat. Reply is the
    git-credential `get` wire form. 503 when this Host holds no user token (the
    Gitea app's connect mints it)."""
    pool = brain_service.get_pool()
    if not pool:
        raise HTTPException(503, "brain pool not ready")
    bearer = ""
    if authorization and authorization.lower().startswith("bearer "):
        bearer = authorization[7:].strip()
    if not bearer:
        raise HTTPException(401, "bastion bearer required")
    import hashlib
    row = await pool.fetchrow(
        "SELECT id FROM mcp_client_tokens WHERE token_hash = $1 AND operator_kind = 'bastion' "
        "AND revoked_at IS NULL AND (expires_at IS NULL OR expires_at > now())",
        hashlib.sha256(bearer.encode()).digest())
    if row is None:
        raise HTTPException(401, "invalid or revoked bastion bearer")
    # Same refusal as the /bastion-git lane: a quarantined bastion gets no credential
    # (the network cut lands on the next forwarder reconcile; this closes the window).
    try:
        q = await pool.fetchval("SELECT 1 FROM nac_bastion_quarantine WHERE token_id=$1", row["id"])
    except Exception:
        q = 1
    if q:
        raise HTTPException(403, "this bastion is quarantined")
    from server.routes.gitea import _user_bearer_fresh, user_login
    tok = await _user_bearer_fresh()
    if not tok:
        raise HTTPException(503, "no gitea session on this host — open the Gitea app and connect")
    from fastapi.responses import PlainTextResponse
    return PlainTextResponse(f"username={user_login() or 'oauth2'}\npassword={tok}\n")


@router.get("/bastions/context-source")
async def bastion_context_source(
    ticket: str = Query(..., min_length=64, max_length=64),
    authorization: Optional[str] = Header(None),
):
    """Serve the ORIGINAL bytes of the one document this machine was asked to
    extract (the pull path). Authed byte-for-byte like
    /bastions/git-credential — the bastion's OWN bearer — PLUS a one-shot ticket
    the host minted for this machine and this row.

    WHY A TICKET RATHER THAN AN ID. The route takes no ctx_id. A bastion cannot
    name a document, so "fetch a different one" is not a request it can make;
    the only thing it can present is an opaque 256-bit token the host handed it
    seconds earlier, bound to its own token id, good once. That is what stops a
    paired machine from walking the user's corpus with a loop, and it is why
    this is not simply the existing GET /api/brain/context/{id}/file with a
    softer gate: that route is requires_permission("read:memory")
    (server/routes/context.py:714), an application-token scope over EVERY row,
    and a bastion holding it would hold a general read.

    Refusals are deliberately flat: unknown / expired / already-used / minted
    for another machine all answer 404, because distinguishing them tells a
    caller how to search.
    """
    pool = brain_service.get_pool()
    if not pool:
        raise HTTPException(503, "brain pool not ready")
    bearer = ""
    if authorization and authorization.lower().startswith("bearer "):
        bearer = authorization[7:].strip()
    if not bearer:
        raise HTTPException(401, "bastion bearer required")
    import hashlib
    row = await pool.fetchrow(
        "SELECT id, entity_id FROM mcp_client_tokens WHERE token_hash = $1 "
        "AND operator_kind = 'bastion' AND revoked_at IS NULL "
        "AND (expires_at IS NULL OR expires_at > now())",
        hashlib.sha256(bearer.encode()).digest())
    if row is None:
        raise HTTPException(401, "invalid or revoked bastion bearer")
    # A quarantined machine gets no bytes, same as it gets no git credential.
    try:
        q = await pool.fetchval("SELECT 1 FROM nac_bastion_quarantine WHERE token_id=$1", row["id"])
    except Exception:
        q = 1
    if q:
        raise HTTPException(403, "this bastion is quarantined")

    from server.network import context_tickets
    tk = context_tickets.redeem(ticket, row["id"])
    if tk is None:
        raise HTTPException(404, "no such fetch")
    # The ticket carries the entity too. A bearer whose entity moved since the
    # mint is refused rather than served another tenant's row — the row read
    # below is scoped by BOTH, so this is belt and braces on the cheap side.
    if int(row["entity_id"]) != tk.entity_id:
        raise HTTPException(404, "no such fetch")

    from server.routes.context import _rehydrate_uc_content
    src = await pool.fetchrow(
        "SELECT id, title, content, storage_backend, metadata FROM user_context "
        "WHERE id = $1 AND entity_id = $2", tk.ctx_id, tk.entity_id)
    if not src:
        raise HTTPException(404, "no such fetch")
    d = dict(src)
    meta = d.get("metadata")
    meta = json.loads(meta) if isinstance(meta, str) else (meta or {})
    if meta.get("kind") != "file":
        # A ticket is only ever minted for a file row; reaching here means the
        # row changed under the job, not that the caller asked for the wrong
        # thing.
        raise HTTPException(409, "that entry is no longer a stored document")
    await _rehydrate_uc_content(pool, tk.entity_id, d)
    if d.get("content_offline") or not d.get("content"):
        raise HTTPException(503, "this document's holding machine isn't checked in right now")
    import asyncio
    import base64 as _b64
    try:
        # Off the event loop: this is up to ~85 MiB of base64 and the host is
        # concurrently awaiting the extraction dispatch on the same loop.
        data = await asyncio.to_thread(_b64.b64decode, d["content"])
    except Exception:
        raise HTTPException(500, "stored file is unreadable")
    if tk.max_bytes and len(data) > tk.max_bytes:
        # The machine declared its ceiling at dispatch and would abort the read
        # anyway; refusing here saves pushing bytes down a tunnel to be dropped.
        raise HTTPException(413, f"document is {len(data)} bytes, over the "
                                 f"{tk.max_bytes} this extraction was authorized for")
    from fastapi.responses import Response as _Response
    return _Response(content=data,
                     media_type=meta.get("mime") or "application/octet-stream")


@router.post("/bastions/dcn-propose")
async def bastion_dcn_propose(
    request: Request,
    authorization: Optional[str] = Header(None),
):
    """A bastion drafts a DCN machine-store grant for a folder ON ITSELF — the
    `cseries-bastion add-folder` lane. Authed byte-for-byte like
    /bastions/heartbeat: the bastion's OWN bearer; localhost_only lets this path
    through to that check. The `server` in the draft is derived from the bearer's
    own heartbeat hostname — never the body — so a bastion can only ever propose
    folders on itself, and the draft stays INERT until the user approves it with
    their step-up code (an agent or a compromised box cannot produce that)."""
    pool = brain_service.get_pool()
    if not pool:
        raise HTTPException(503, "brain pool not ready")
    bearer = ""
    if authorization and authorization.lower().startswith("bearer "):
        bearer = authorization[7:].strip()
    if not bearer:
        raise HTTPException(401, "bastion bearer required")
    import hashlib
    token_hash = hashlib.sha256(bearer.encode()).digest()
    row = await pool.fetchrow(
        "SELECT id, entity_id FROM mcp_client_tokens "
        "WHERE token_hash = $1 AND operator_kind = 'bastion' AND revoked_at IS NULL",
        token_hash,
    )
    if row is None:
        raise HTTPException(401, "invalid or revoked bastion bearer")
    # Same refusal as git-credential and context-source: a quarantined bastion
    # drafts nothing (fails closed if the quarantine table cannot be read).
    try:
        q = await pool.fetchval("SELECT 1 FROM nac_bastion_quarantine WHERE token_id=$1", row["id"])
    except Exception:
        q = 1
    if q:
        raise HTTPException(403, "this bastion is quarantined")
    hostname = await pool.fetchval(
        "SELECT hostname FROM network_bastion_heartbeat WHERE token_id = $1", row["id"])
    if not hostname:
        raise HTTPException(409, "bastion has no heartbeat yet — start it and retry")
    body = await request.json() if request.headers.get("content-type", "").startswith("application/json") else {}
    from server.network import nac_propose
    try:
        return await nac_propose.propose_dcn_store(
            pool, row["entity_id"], store_class="machine",
            server=hostname,                      # bearer-derived, never the body
            root=body.get("root"),
            display_name=body.get("display_name"),
            proposed_by=f"bastion:{hostname}")
    except ValueError as e:
        raise HTTPException(400, str(e))


# `{token_id:int}` and not `{token_id}`: these two are declared ABOVE the
# /bastions/pairings/* routes, and a bare placeholder matches any single
# segment — so `/bastions/pairings/revoke` was being routed HERE and answered
# 422 ("pairings" is not an int), which is exactly what the Network app's
# pairing Revoke button calls (system-apps/network/app.js). The converter makes
# the match refuse a non-numeric segment instead of capturing it, so ordering
# stops mattering. Found 2026-09-08 by a route-table review, reproduced with a
# TestClient before and after.
@router.post("/bastions/{token_id:int}/revoke", dependencies=[Depends(requires_permission("write:network"))])
async def revoke_bastion(token_id: int):
    """Revoke a bastion (host-local authority). Scoped to bastions of this entity."""
    pool = brain_service.get_pool()
    if not pool:
        raise HTTPException(503, "brain pool not ready")
    from server.network import nac_commit
    try:
        return await nac_commit.revoke_bastion(pool, brain_service._entity_id, token_id)
    except nac_commit.CommitError as e:
        raise HTTPException(e.status, e.detail)


@router.post("/bastions/{token_id:int}/update", dependencies=[Depends(requires_permission("write:network"))])
async def update_bastion(token_id: int, request: Request):
    """Direct a bastion to self-update to a target version on its next heartbeat (update path P2).
    The bastion fetches the published manifest, verifies the sha256, swaps its binary, and restarts.
    Host-directed — the platform can't push code; only the user's host can, and the binary is
    hash-verified. Body: {version}. Scoped to THIS entity's bastions."""
    pool = brain_service.get_pool()
    if not pool:
        raise HTTPException(503, "brain pool not ready")
    body = await request.json() if request.headers.get("content-type", "").startswith("application/json") else {}
    from server.network import nac_commit
    try:
        return await nac_commit.update_bastion(pool, brain_service._entity_id, token_id,
                                                (body.get("version") or ""))
    except nac_commit.CommitError as e:
        raise HTTPException(e.status, e.detail)


@router.post("/bastions/update", dependencies=[Depends(requires_permission("write:network"))])
async def update_bastions(request: Request):
    """Direct every out-of-date bastion to self-update, in one call.

    The single-machine route above is the same directive; this one exists because
    running a fleet is the normal case and asking the user to confirm per machine
    turned "move my network to the version I published" into forty clicks. One
    decision, one call.

    Body: {version, token_ids?}. `token_ids` narrows to a chosen set; omit it for
    every bastion this entity owns. Machines already on that version are skipped
    and named in the reply. Nothing is pushed: each box picks the directive up on
    its next heartbeat and applies it only if the published sha256 matches.
    """
    pool = brain_service.get_pool()
    if not pool:
        raise HTTPException(503, "brain pool not ready")
    body = await request.json() if request.headers.get("content-type", "").startswith("application/json") else {}
    from server.network import nac_commit
    try:
        return await nac_commit.update_bastions(pool, brain_service._entity_id,
                                                (body.get("version") or ""),
                                                body.get("token_ids"))
    except nac_commit.CommitError as e:
        raise HTTPException(e.status, e.detail)


@router.get("/bastions/auto-update", dependencies=[Depends(requires_permission("read:topology"))])
async def get_bastion_auto_update():
    """Whether this Host moves its bastions to each published version on its own,
    and where the newest version stands: this Host's ring, the rollout stage the
    platform reports, and ring 0's verdict (server/network/bastion_autoupdate.py)."""
    pool = brain_service.get_pool()
    if not pool:
        raise HTTPException(503, "brain pool not ready")
    from server.network import bastion_autoupdate
    return await bastion_autoupdate.status(pool)


@router.put("/bastions/auto-update", dependencies=[Depends(requires_permission("write:network"))])
async def set_bastion_auto_update(request: Request):
    """Turn automatic bastion updates on or off for this Host. Body: {enabled}.
    No step-up either way: on is the same directive "Update all" parks without
    one, and off only stops the loop parking it."""
    pool = brain_service.get_pool()
    if not pool:
        raise HTTPException(503, "brain pool not ready")
    body = await request.json() if request.headers.get("content-type", "").startswith("application/json") else {}
    if not isinstance(body.get("enabled"), bool):
        raise HTTPException(400, "enabled (true or false) required")
    from server.network import bastion_autoupdate
    await bastion_autoupdate.set_enabled(pool, body["enabled"])
    return await bastion_autoupdate.status(pool)


_bastion_latest = {"version": None, "at": 0.0}


@router.get("/bastion-latest", dependencies=[Depends(requires_permission("read:topology"))])
async def bastion_latest():
    """The latest published bastion version (from the manifest), so the Network app can flag
    out-of-date bastions + offer an update. Fetched via the host's OWN egress proxy (the same lane
    the host uses for any outbound) and cached 5 min; degrades to null. The platform never tells
    the host this — the host reads the public manifest itself."""
    from server.network import nac_commit
    return await nac_commit.bastion_latest()


@router.get("/peers", dependencies=[Depends(requires_permission("read:topology"))])
async def get_peers():
    """Host↔host peers: contacts (address book) + secured channels (held slips).
    Reachability posture/rules come from the existing /api/domain-policy."""
    pool = brain_service.get_pool()
    if not pool:
        return {"contacts": [], "secured_channels": []}
    return await topology.list_peers(pool, brain_service._entity_id)


# ══════════════════════════════════════════════════════════════════════
# NAC user-gate (P4): the host AUTHORS policy, the USER approves it. Creating an
# ALLOW (granting reach) requires a host-local TOTP step-up; deny/tighten need only
# a normal authenticated approval. On approval the terms are signed with the host's
# entity key (tamper-evidence) and the PDP picks them up. Deterministic — the LLM
# proposes drafts but is never in the commit path.
# ══════════════════════════════════════════════════════════════════════


@router.get("/nac/policy", dependencies=[Depends(requires_permission("read:topology"))])
async def nac_policy_list():
    """The host's NAC: profiles, policy (drafts + approved), and step-up enrollment."""
    pool = brain_service.get_pool()
    eid = brain_service._entity_id
    from server.network import nac_commit
    return await nac_commit.policy_list(pool, eid)


@router.get("/nac/endpoints", dependencies=[Depends(requires_permission("read:topology"))])
async def nac_endpoints_list():
    """The host's NAC endpoint posture — per bastion: its live posture (written from the
    heartbeat, P2, and the liveness reaper), the posture why-string (last_decision), and
    `decision`: what the running engine does with that machine now
    (nac_commit.endpoint_decisions). nac_endpoints.assigned_profile / decided_at are never
    written, whatever v058 says, so they are not read. READ-ONLY; the user never sets posture
    by hand. Degrades to empty on a pod missing the table (no pool)."""
    pool = brain_service.get_pool()
    if not pool:
        return {"endpoints": [], "suspended": [], "quarantined": []}
    # ONE CARD PER MACHINE, AND ONLY FOR A LIVE ENROLMENT.
    #
    # nac_endpoints keys on token_id, and re-pairing a machine mints a NEW token
    # — so every enrolment a box has ever had left its own posture row behind,
    # and the page drew all of them. Measured 2026-09-13 on one Host: 33 rows for
    # 27 machines, one desktop alone holding FIVE, ten rows whose posture had not
    # moved in over a week, and six hostnames with no enrolment at all —
    # including a decommissioned server, still drawn as a card someone could
    # press Quarantine on.
    #
    # The JOIN drops a row whose enrolment is revoked or gone; DISTINCT ON keeps
    # ONE row per hostname. Verified against live data: 33 cards become 21, and
    # the kept token is the heartbeating one in every case (0 mismatches).
    # Read-only — the stale rows stay on disk for forensics, they just stop being
    # drawn as if they were machines.
    #
    # ONE CARD PER HOSTNAME, WHICH IS NOT THE SAME AS ONE PER MACHINE. Two
    # live machines that genuinely share a hostname collapse
    # into a single card. That is still the right trade against the bug this
    # fixes — a card per re-pairing, which is what was actually hit — but it is
    # a real limit, and the tie-break decides which of the two the user sees.
    #
    # THE TIE-BREAK: newest ENROLMENT first, then most recently decided. It was
    # `posture_at DESC NULLS LAST` alone, which preferred a DECIDED STALE row
    # over an UNDECIDED LIVE one — backwards for a page whose whole job is to say
    # what is on the network now. token_id ascends with enrolment, so the highest
    # is the most recent time a machine proved it is here.
    rows = await pool.fetch(NAC_ENDPOINTS_SQL)
    # Each card's access comes from the running engine (nac_commit.endpoint_decisions), not
    # from nac_endpoints.assigned_profile: nothing writes that column, so it read NULL and
    # the card said "default-preserve" whatever the engine was doing.
    from server.network import nac_commit
    live = await nac_commit.endpoint_decisions([dict(r) for r in rows])
    out = []
    for r in rows:
        d = dict(r)
        for k in ("posture_at", "decided_at"):
            if d.get(k) is not None and hasattr(d[k], "isoformat"):
                d[k] = d[k].isoformat()
        d["decision"] = live["by_token"].get(d["token_id"])
        # the fingerprints decided `governed`; the card has no use for the values
        d.pop("fingerprint", None)
        d.pop("bound_fingerprint", None)
        d.pop("token_expired", None)
        out.append(d)
    # The suspended + quarantined sets ride alongside posture so the NAC tab can mark a paused
    # bastion (suspend = a TIGHTEN on the mgmt/harness lanes only) and a quarantined one (a
    # whole-mesh cut — its concentrator peer is also dropped). Both override standing grants
    # without deleting them; resume/release restores them.
    from server.network import nac_mgmt
    suspended = sorted(await nac_mgmt.load_suspended_servers(pool))
    quarantined = sorted(await nac_mgmt.load_quarantined(pool))
    return {"endpoints": out, "suspended": suspended, "quarantined": quarantined,
            "engine": live["engine"]}


@router.post("/nac/stepup/enroll", dependencies=[Depends(requires_permission("write:network"))])
async def nac_stepup_enroll():
    """Host-authored: provision the step-up TOTP secret + return the otpauth URI for the
    user to scan. Idempotent; the secret never leaves the pod."""
    pool = brain_service.get_pool()
    if not pool:
        raise HTTPException(503, "brain pool not ready")
    from server.network import nac_stepup
    secret = await nac_stepup.ensure_stepup_secret(pool)
    handle = await bastion_enroll.resolve_self_handle(brain_service._entity_id)
    return {
        "otpauth_uri": nac_stepup.provisioning_uri(secret, account=handle or "host"),
        "secret": secret,
        "enrolled": await nac_stepup.is_enrolled(pool),
    }


@router.post("/nac/stepup/confirm", dependencies=[Depends(requires_permission("resolve:decision"))])
async def nac_stepup_confirm(request: Request):
    """User confirms enrollment by entering a code from their authenticator app."""
    pool = brain_service.get_pool()
    if not pool:
        raise HTTPException(503, "brain pool not ready")
    from server.network import nac_stepup
    body = await request.json()
    if not nac_stepup.verify_totp(await nac_stepup.ensure_stepup_secret(pool), str(body.get("code", ""))):
        raise HTTPException(401, "invalid code")
    await nac_stepup.mark_enrolled(pool)
    return {"enrolled": True}


@router.post("/nac/policy/propose", dependencies=[Depends(requires_permission("write:network"))])
async def nac_policy_propose(request: Request):
    """Host-authored: create a DRAFT policy rule (+ its profile). Inert until the user
    approves. Body: {name, priority?, match?, enforcement(allow|quarantine), permits?}."""
    pool = brain_service.get_pool()
    if not pool:
        raise HTTPException(503, "brain pool not ready")
    from server.network import nac_propose
    body = await request.json()
    try:
        return await nac_propose.propose_policy(
            pool, name=body.get("name"), enforcement=body.get("enforcement") or "allow",
            permits=body.get("permits"), match=body.get("match"),
            priority=body.get("priority"))
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.post("/nac/policy/{policy_id}/approve",
             dependencies=[Depends(requires_permission("resolve:decision"))])
async def nac_policy_approve(policy_id: int, request: Request,
                             x_authentik_email: Optional[str] = Header(None)):
    """User approves a draft. An ALLOW (granting reach) REQUIRES a step-up TOTP code; a
    quarantine/tighten does not. On approval the terms are signed with the host's entity
    key (tamper-evidence) and the PDP picks them up. Deterministic — no LLM in this path."""
    pool = brain_service.get_pool()
    eid = brain_service._entity_id
    body = await request.json() if request.headers.get("content-type", "").startswith("application/json") else {}
    from server.network import nac_commit
    try:
        # via="ui": this route is the Network app's approve button, so the approval is
        # the user's. The host tool passes via="host_tool" and is recorded as the host.
        return await nac_commit.approve_policy(
            pool, eid, policy_id, stepup_code=body.get("stepup_code"),
            approved_by=x_authentik_email, via="ui")
    except nac_commit.CommitError as e:
        raise HTTPException(e.status, e.detail)


@router.post("/nac/policy/{policy_id}/revoke",
             dependencies=[Depends(requires_permission("write:network"))])
async def nac_policy_revoke(policy_id: int, request: Request):
    """Revoke a policy rule. Removing an approved rule that keeps machines out widens reach, so it
    needs a step-up (body {stepup_code}), like removing an approved egress deny: a quarantine, an
    allow that admits no lane, or a rule failing its integrity check (nac_commit.revoke_policy;
    /nac/policy reports it per row as revoke_needs_code). Removing an allow that admits, or
    declining a draft, only tightens — free."""
    pool = brain_service.get_pool()
    eid = brain_service._entity_id
    body = await request.json() if request.headers.get("content-type", "").startswith("application/json") else {}
    from server.network import nac_commit
    try:
        return await nac_commit.revoke_policy(pool, eid, policy_id, stepup_code=body.get("stepup_code"))
    except nac_commit.CommitError as e:
        raise HTTPException(e.status, e.detail)


# ══════════════════════════════════════════════════════════════════════
# NAC egress (P6) — the USER's outbound layer.
#
# The inbound user-gate, pointed outward. The host proposes a draft egress rule;
# the user approves it; the host signs the approved terms with its entity key. The
# in-pod egress proxy (server/network/nac_egress.py) reads the signed rules and
# decides per destination — DEFAULT OPEN, never logging where the host went.
#
# WIDENING the host's reach needs the user's step-up; TIGHTENING is free:
#   approve an ALLOW  -> grants reach   -> step-up
#   revoke  a  DENY   -> removes a limit -> step-up   (else an agent re-opens egress)
#   approve a  DENY / revoke an ALLOW    -> tightens   -> free
# ══════════════════════════════════════════════════════════════════════


@router.get("/nac/egress", dependencies=[Depends(requires_permission("read:topology"))])
async def nac_egress_list():
    """The host's egress policy: the user's rules (drafts + approved). Default is open."""
    pool = brain_service.get_pool()
    eid = brain_service._entity_id
    from server.network import nac_commit
    return await nac_commit.egress_list(pool, eid)


@router.post("/nac/egress/propose", dependencies=[Depends(requires_permission("write:network"))])
async def nac_egress_propose(request: Request):
    """Host-authored: a DRAFT egress rule, inert until the user approves. Body:
    {pattern, action(allow|deny), priority?, note?}. `*` is the catch-all (an allowlist =
    explicit allows + a final `*` deny). An ALLOW grants reach, so approval needs step-up."""
    pool = brain_service.get_pool()
    if not pool:
        raise HTTPException(503, "brain pool not ready")
    from server.network import nac_propose
    body = await request.json()
    try:
        return await nac_propose.propose_egress(
            pool, pattern=body.get("pattern"), action=body.get("action") or "deny",
            priority=body.get("priority"), note=body.get("note"),
            subject=body.get("subject") or "*", exit=body.get("exit") or "default")
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.post("/nac/egress/{rule_id}/approve",
             dependencies=[Depends(requires_permission("resolve:decision"))])
async def nac_egress_approve(rule_id: int, request: Request,
                             x_authentik_email: Optional[str] = Header(None)):
    """User approves a draft egress rule. An ALLOW (granting reach) REQUIRES a step-up TOTP
    code; a DENY (tightening) does not. The approved terms are signed with the host's entity
    key; the egress proxy verifies that signature and enforces the SIGNED terms."""
    pool = brain_service.get_pool()
    eid = brain_service._entity_id
    body = await request.json() if request.headers.get("content-type", "").startswith("application/json") else {}
    from server.network import nac_commit
    try:
        # via="ui": this route IS the Network app's approve button, and the card next to
        # it renders the rule's subject and exit. That is what earns the right to approve
        # a non-default exit — the host tool cannot pass this and is refused.
        return await nac_commit.approve_egress(
            pool, eid, rule_id, stepup_code=body.get("stepup_code"),
            approved_by=x_authentik_email, via="ui")
    except nac_commit.CommitError as e:
        raise HTTPException(e.status, e.detail)


@router.post("/nac/egress/{rule_id}/revoke",
             dependencies=[Depends(requires_permission("write:network"))])
async def nac_egress_revoke(rule_id: int, request: Request):
    """Revoke an egress rule. Removing an approved DENY WIDENS the host's reach, so it needs
    a step-up (else a manipulated agent could re-open egress by deleting the user's denies).
    Removing an allow (or a draft) only tightens — free. The proxy re-reads within seconds."""
    pool = brain_service.get_pool()
    eid = brain_service._entity_id
    body = await request.json() if request.headers.get("content-type", "").startswith("application/json") else {}
    from server.network import nac_commit
    try:
        return await nac_commit.revoke_egress(pool, eid, rule_id, stepup_code=body.get("stepup_code"))
    except nac_commit.CommitError as e:
        raise HTTPException(e.status, e.detail)


# ══════════════════════════════════════════════════════════════════════
# Messaging connectors —
# the Ingress lane's connector grants. The Host proposes (propose_nac_policy
# kind=connector); the pairing code is shown ONLY here, to the person's own
# window; the person lets the reported account in with their authenticator
# code. Revoke and decline only tighten and are free. The logic lives in
# server/network/nac_connector.py; these routes add who may call them.
# ══════════════════════════════════════════════════════════════════════

# The person's own windows: the same set tickets use for "the person"
# (routes/tickets.py PERSON_SURFACES) plus Network, where the code is shown —
# and only as the platform signed them (nac_stepup._owner_ui checks the
# installed provenance), so a Host-built app that took one of these ids is not
# the person.
CONNECTOR_PERSON_WINDOWS = frozenset({"network", "circles", "workbench"})


async def _person_window(request: Request) -> str:
    from server.network import nac_stepup
    tok = getattr(request.state, "app_token", None)
    app_id = str(getattr(tok, "app_id", "") or "")
    if app_id not in CONNECTOR_PERSON_WINDOWS or not await nac_stepup._owner_ui(
            brain_service.get_pool(), app_id):
        raise HTTPException(403, "only your own Network, Circles or Workbench window can do this")
    return app_id


async def _json_body(request: Request) -> dict:
    if not request.headers.get("content-type", "").startswith("application/json"):
        return {}
    try:
        body = await request.json()
    except Exception:
        return {}
    return body if isinstance(body, dict) else {}


@router.get("/nac/connector", dependencies=[Depends(requires_permission("read:topology"))])
async def nac_connector_list():
    """Connector grants: open pairings, ones awaiting the person, live grants, and
    the last week's ended ones, with each app's capability state. No code."""
    from server.network import nac_connector
    try:
        return await nac_connector.list_connectors(brain_service.get_pool())
    except nac_connector.ConnectorError as e:
        raise HTTPException(e.status, e.detail)


@router.get("/nac/connector/{grant_id}/code",
            dependencies=[Depends(requires_permission("read:topology"))])
async def nac_connector_code(grant_id: int, request: Request):
    """The one-time code of an open pairing, for the person's own window only.
    No Host tool reaches this route; the Host's kernel-op token is refused here
    like every other app's."""
    await _person_window(request)
    from server.network import nac_connector
    try:
        out = await nac_connector.pairing_code(brain_service.get_pool(), grant_id)
    except nac_connector.ConnectorError as e:
        raise HTTPException(e.status, e.detail)
    from fastapi.responses import JSONResponse
    return JSONResponse(out, headers={"Cache-Control": "no-store"})


@router.post("/nac/connector/{grant_id}/approve",
             dependencies=[Depends(requires_permission("resolve:decision"))])
async def nac_connector_approve(grant_id: int, request: Request,
                                x_authentik_email: Optional[str] = Header(None)):
    """'Let this account in': the person's authenticator code, checked and burned
    before anything changes; then one transaction signs the grant and revokes
    the app's older one."""
    await _person_window(request)
    body = await _json_body(request)
    from server.network import nac_connector
    try:
        return await nac_connector.approve_connector(
            brain_service.get_pool(), brain_service._entity_id, grant_id,
            stepup_code=body.get("stepup_code"), approved_by=x_authentik_email, via="ui")
    except nac_connector.ConnectorError as e:
        raise HTTPException(e.status, e.detail)


@router.post("/nac/connector/{grant_id}/decline",
             dependencies=[Depends(requires_permission("write:network"))])
async def nac_connector_decline(grant_id: int, request: Request):
    """'Don't pair': close a pairing or a matched draft. Free — it only tightens."""
    from server.network import nac_connector
    try:
        return await nac_connector.decline_connector(brain_service.get_pool(), grant_id, actor="user")
    except nac_connector.ConnectorError as e:
        raise HTTPException(e.status, e.detail)


@router.post("/nac/connector/{grant_id}/revoke",
             dependencies=[Depends(requires_permission("write:network"))])
async def nac_connector_revoke(grant_id: int, request: Request):
    """Revoke a connector's grant: free (it only tightens). The next post the
    connector makes is refused; its turn in flight is cancelled; the app keeps
    running until it is deleted."""
    from server.network import nac_connector
    try:
        out = await nac_connector.revoke_connector(
            brain_service.get_pool(), grant_id=grant_id, reason="revoked in Network", actor="user")
    except nac_connector.ConnectorError as e:
        raise HTTPException(e.status, e.detail)
    return {"app_id": out.app_id, "revoked": out.grant_ids, "declined": out.drafts,
            "capability_cleared": out.cap_dropped}


# ══════════════════════════════════════════════════════════════════════
# DCN store grants (CN-1) — the user's own stores join the route map.
# Same lifecycle as every NAC widen:
# host proposes a DRAFT, the user approves with the sovereign TOTP step-up,
# the terms are signed, the pod gate (nac_dcn) enforces the SIGNED terms.
# ══════════════════════════════════════════════════════════════════════

@router.get("/nac/dcn/stores", dependencies=[Depends(requires_permission("read:topology"))])
async def nac_dcn_stores_list():
    """The grant-lifecycle view of DCN stores: drafts awaiting approval, granted
    stores with expiry, revoked history."""
    pool = brain_service.get_pool()
    eid = brain_service._entity_id
    from server.network import nac_commit
    return await nac_commit.dcn_stores_list(pool, eid)


@router.post("/nac/dcn/propose", dependencies=[Depends(requires_permission("write:network"))])
async def nac_dcn_propose(request: Request):
    """Host- or gateway-authored: a DRAFT DCN store grant, inert until the user
    approves (ALWAYS step-up — creating a store is a widen, both classes). Body:
    {class: machine|cabinet, server?, root?, bucket?, prefix?, endpoint?, cred_name?,
    mask?, excludes?, sweep?, display_name?, note?, sensitive?, legacy_portal_id?}.
    A cabinet draft bundles its egress allow(s) in the same transaction (§5.1)."""
    pool = brain_service.get_pool()
    if not pool:
        raise HTTPException(503, "brain pool not ready")
    eid = brain_service._entity_id
    from server.network import nac_propose
    body = await request.json()
    try:
        return await nac_propose.propose_dcn_store(
            pool, eid, store_class=body.get("class") or body.get("store_class"),
            server=body.get("server"), root=body.get("root"),
            bucket=body.get("bucket"), prefix=body.get("prefix"),
            endpoint=body.get("endpoint"), cred_name=body.get("cred_name"),
            mask=body.get("mask"), excludes=body.get("excludes"),
            sweep=body.get("sweep") or "hourly",
            display_name=body.get("display_name"), note=body.get("note"),
            sensitive=bool(body.get("sensitive")),
            legacy_portal_id=body.get("legacy_portal_id"))
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.post("/nac/dcn/{store_id}/approve",
             dependencies=[Depends(requires_permission("resolve:decision"))])
async def nac_dcn_approve(store_id: int, request: Request,
                          x_authentik_email: Optional[str] = Header(None)):
    """User approves a draft store grant — ALWAYS requires the step-up TOTP code.
    The cabinet composite approves the store AND its bundled egress allow(s) under
    this one code, in one transaction (§5.1). After approval a machine store's
    signed grant is synced to its box so the Go handlers enforce it locally."""
    pool = brain_service.get_pool()
    eid = brain_service._entity_id
    body = await request.json() if request.headers.get("content-type", "").startswith("application/json") else {}
    from server.network import nac_commit, nac_dcn
    try:
        result = await nac_commit.approve_dcn_store(
            pool, eid, store_id, stepup_code=body.get("stepup_code"),
            ttl_days=body.get("ttl_days"), approved_by=x_authentik_email)
    except nac_commit.CommitError as e:
        raise HTTPException(e.status, e.detail)
    if result.get("class") == "machine":
        try:
            result["grant_sync"] = await nac_dcn.sync_dcn_grant(pool, eid, store_id)
        except Exception as e:
            # The grant stands; the box enforces on next sync. Say so plainly.
            result["grant_sync"] = {"state": "offline", "detail": str(e)}
    return result


# ── Service reach grants (service slot, bastion 0.36.0): which Host may reach which
#    server on a machine. Same lifecycle as a store grant; server/network/service_reach.py.

@router.get("/nac/services/reach", dependencies=[Depends(requires_permission("read:topology"))])
async def nac_reach_list():
    pool = brain_service.get_pool()
    from server.network import service_reach
    return {"grants": await service_reach.list_reach(pool, brain_service._entity_id)}


@router.post("/nac/services/reach/propose", dependencies=[Depends(requires_permission("write:network"))])
async def nac_reach_propose(request: Request):
    """Draft a reach grant for THIS Host to one server on one machine. Inert until
    the person approves it. Body: {server, slot, note?}."""
    pool = brain_service.get_pool()
    body = await request.json()
    from server.network import service_reach
    try:
        return await service_reach.propose_reach(
            pool, brain_service._entity_id, body.get("server") or "", body.get("slot") or "",
            note=body.get("note"), proposed_by="user", mirrors=body.get("mirrors"))
    except service_reach.ReachError as e:
        raise HTTPException(e.status, e.detail)


@router.post("/nac/services/reach/{grant_id}/approve",
             dependencies=[Depends(requires_permission("resolve:decision"))])
async def nac_reach_approve(grant_id: int, request: Request,
                            x_authentik_email: Optional[str] = Header(None)):
    """The person approves a reach grant — a widen: on the machine when it holds a
    second factor, else with the step-up code. Then it is pushed to the box."""
    pool = brain_service.get_pool()
    eid = brain_service._entity_id
    body = await request.json() if request.headers.get("content-type", "").startswith("application/json") else {}
    from server.network import service_reach
    try:
        result = await service_reach.approve_reach(
            pool, eid, grant_id, stepup_code=body.get("stepup_code"),
            ttl_days=body.get("ttl_days"), approved_by=x_authentik_email,
            seen_mirrors_digest=body.get("mirrors_digest"))
    except service_reach.ReachError as e:
        raise HTTPException(e.status, e.detail)
    if result.get("approved"):
        try:
            result["grant_sync"] = await service_reach.sync_reach_grant(pool, eid, grant_id)
        except Exception as e:
            result["grant_sync"] = {"state": "offline", "detail": str(e)}
    return result


@router.post("/nac/services/reach/{grant_id}/revoke",
             dependencies=[Depends(requires_permission("write:network"))])
async def nac_reach_revoke(grant_id: int):
    """Revoke a reach grant — a tighten, free. Pushed to the box now; an offline box
    drops it from its next heartbeat reply."""
    pool = brain_service.get_pool()
    eid = brain_service._entity_id
    from server.network import service_reach
    try:
        result = await service_reach.revoke_reach(pool, eid, grant_id)
    except service_reach.ReachError as e:
        raise HTTPException(e.status, e.detail)
    try:
        result["grant_drop"] = await service_reach.drop_reach_grant(pool, eid, grant_id, result["server"])
    except Exception as e:
        result["grant_drop"] = {"state": "offline", "detail": str(e),
                                "note": "reconciled on the box's next heartbeat"}
    return result


@router.post("/nac/dcn/{store_id}/revoke",
             dependencies=[Depends(requires_permission("write:network"))])
async def nac_dcn_revoke(store_id: int):
    """Revoke a store grant (TIGHTEN — free, no code). A cabinet revoke tears down
    its bundled egress allow(s) too: one capability, revoked whole. The route map's
    index rows are kept — revoking access never deletes the user's index.
    A machine store's revoke is PUSHED to its box (dcn_grant_drop) so the signed
    grant dies there now; an offline box drops it from its next heartbeat reply."""
    pool = brain_service.get_pool()
    eid = brain_service._entity_id
    from server.network import nac_commit, nac_dcn
    try:
        result = await nac_commit.revoke_dcn_store(pool, eid, store_id)
    except nac_commit.CommitError as e:
        raise HTTPException(e.status, e.detail)
    if result.get("class") == "machine" and result.get("server"):
        try:
            result["grant_drop"] = await nac_dcn.drop_dcn_grant(
                pool, eid, store_id, result["server"])
        except Exception as e:
            result["grant_drop"] = {"state": "offline", "detail": str(e),
                                    "note": "reconciled on the box's next heartbeat"}
    return result


# ── DCN move plans: the one write lane, archive-only ───────────────────────────
# host proposes moves inside a granted root → user approves with the step-up code
# → signed → the box dry-runs / applies with rollback + verify → report. No delete.

@router.get("/nac/dcn/plans", dependencies=[Depends(requires_permission("read:topology"))])
async def nac_dcn_plans_list(store_id: Optional[int] = None):
    """Move plans: drafts awaiting the code, approved awaiting a run, applied/failed with reports."""
    pool = brain_service.get_pool()
    eid = brain_service._entity_id
    from server.network import nac_commit
    return await nac_commit.dcn_move_plans_list(pool, eid, store_id=store_id)


@router.post("/nac/dcn/plan/propose", dependencies=[Depends(requires_permission("write:network"))])
async def nac_dcn_plan_propose(request: Request):
    """Draft a move plan for a granted machine store. Body: {store_id, moves:[{from,to,size_bytes?}],
    archive?, title?, note?}. INERT until the user approves with their code."""
    pool = brain_service.get_pool()
    if not pool:
        raise HTTPException(503, "brain pool not ready")
    eid = brain_service._entity_id
    from server.network import nac_propose
    body = await request.json()
    try:
        return await nac_propose.propose_dcn_move_plan(
            pool, eid, store_id=body.get("store_id"), moves=body.get("moves"),
            archive=body.get("archive") or "Archive", title=body.get("title"), note=body.get("note"))
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.post("/nac/dcn/plan/{plan_id}/approve",
             dependencies=[Depends(requires_permission("resolve:decision"))])
async def nac_dcn_plan_approve(plan_id: int, request: Request,
                               x_authentik_email: Optional[str] = Header(None)):
    """User approves a move plan — ALWAYS the step-up code (it changes files on their machine)."""
    pool = brain_service.get_pool()
    eid = brain_service._entity_id
    body = await request.json() if request.headers.get("content-type", "").startswith("application/json") else {}
    from server.network import nac_commit
    try:
        return await nac_commit.approve_dcn_move_plan(
            pool, eid, plan_id, stepup_code=body.get("stepup_code"), approved_by=x_authentik_email)
    except nac_commit.CommitError as e:
        raise HTTPException(e.status, e.detail)


@router.post("/nac/dcn/plan/{plan_id}/revoke",
             dependencies=[Depends(requires_permission("write:network"))])
async def nac_dcn_plan_revoke(plan_id: int):
    """Discard / revoke a plan (TIGHTEN — free)."""
    pool = brain_service.get_pool()
    eid = brain_service._entity_id
    from server.network import nac_commit
    try:
        return await nac_commit.revoke_dcn_move_plan(pool, eid, plan_id)
    except nac_commit.CommitError as e:
        raise HTTPException(e.status, e.detail)


@router.post("/nac/dcn/plan/{plan_id}/run",
             dependencies=[Depends(requires_permission("write:network"))])
async def nac_dcn_plan_run(plan_id: int, request: Request):
    """Run an approved plan on its box. Body: {mode: 'dry_run' | 'apply'}. Typed envelope."""
    pool = brain_service.get_pool()
    if not pool:
        raise HTTPException(503, "brain pool not ready")
    eid = brain_service._entity_id
    body = await request.json() if request.headers.get("content-type", "").startswith("application/json") else {}
    from server.network import nac_dcn
    try:
        return await nac_dcn.run_move_plan(pool, eid, plan_id, body.get("mode") or "dry_run")
    except nac_dcn.MgmtError as e:
        raise HTTPException(e.status, e.detail)


# ══════════════════════════════════════════════════════════════════════
# NAC management (P8) — the entity OPERATES the servers in its mesh.
#
# TACACS+ for the entity: per-command authorization (DEFAULT-DENY, the inverse of egress) +
# command accounting (ALWAYS audited — the entity acting ON the user's machine, unlike the
# user's private egress, which is never logged). The pod dispatches a "run this op on the
# server" job onto that server's bastion worker (server/network/nac_mgmt.py); the BASTION is
# the authoritative wall — it checks its own allowlist before running.
#
# P8.0: one hardcoded read-only op, default-deny proven, audited. P8.1 swaps the hardcoded
# allowlist for user-signed grants verified at the bastion. The route below is the P8.0 TEST
# TRIGGER (internal-auth gated); the permissioned MCP tool surface is P8.2.
# ══════════════════════════════════════════════════════════════════════

# mgmt audit + DDL moved to the service layer (server/network/nac_mgmt.py::audit_mgmt) so the
# /mgmt/run route, the /harness/run route, AND the mgmt_run host tool share ONE audited path with
# ONE signed-grant gate. Alias kept so the other caller in THIS file (/harness/run) is unchanged.
from server.network.nac_mgmt import audit_mgmt as _audit_mgmt


@router.post("/mgmt/run", dependencies=[Depends(requires_permission("write:network"))])
async def mgmt_run(request: Request):
    """P8.1 management dispatch — the entity OPERATES a server in its mesh. Body: {server, op, args?}.

    TWO gates, and they answer different questions.

    1. App scope — `write:network`, the same scope every other network mutation in this file
       requires (propose/revoke, /sessions/{id}/close, /sessions/{server}/ticket). It answers
       "may THIS CALLER ask at all": a caller with no app token gets 401, a valid token that did
       not declare write:network gets 403 (server/applications/middleware.py:247-257). The
       signed grant below cannot answer that question, because a grant is bound to the ENTITY,
       not to the app holding the window token — without this dependency any installed app with
       any valid token could run an op on any machine the host already holds a grant for.
    2. The host's own SIGNED grant, enforced in the service (nac_mgmt.run_mgmt_op). A grant is
       the user's step-up-authorized, Ed25519-signed authorization for (entity, server, ops,
       expiry) — proposed by the host, approved by the user with a TOTP step-up, signed with the
       host entity key (see /nac/mgmt/grant/{propose,approve} + nac_mgmt.load_mgmt_grants).
       DEFAULT-DENY: no grant that covers (server, op) ⇒ refused, never run. It answers "is this
       OPERATION authorized on this machine".

    A grant refusal is returned in the SAME shape as the bastion's own allowlist refusing an
    un-allowlisted op — a command result with exit=126 (the bastion is the authoritative wall;
    the pod-gate just refuses early), so the caller handles one shape whichever wall said no.
    ALWAYS audited (the bright line vs egress, which logs nothing).

    Not internal-auth gated: this is the caller-side dispatch — the bastion daemons connect
    inbound over their own bearer-authed reverse workers."""
    pool = brain_service.get_pool()
    if not pool:
        raise HTTPException(503, "brain pool not ready")
    eid = brain_service._entity_id
    body = await request.json()
    # Thin adapter: the OPERATE logic (signed-grant default-deny gate + dispatch + audit) lives in
    # the service so the mgmt_run host tool shares it without faking an HTTP Request. (2026-06-24)
    from server.network import nac_mgmt
    try:
        # origin="route": whoever holds write:network is not the context store's
        # own writer, so ctx_put / ctx_del ask the person here too (nac_mgmt).
        return await nac_mgmt.run_mgmt_op(pool, eid, body.get("server"), body.get("op"), body.get("args"),
                                          origin="route")
    except nac_mgmt.MgmtError as e:
        raise HTTPException(e.status, e.detail)


def _read_agent_creds() -> Optional[str]:
    """H3b agent-credential read. MOVED to the service layer (server/network/nac_harness.py::
    read_agent_creds) so the /harness/run route AND the mesh_harness_run host tool share ONE
    harness-dispatch path (2026-06-24). Kept here as a thin delegator so any other in-file caller
    is unchanged; run_harness_op reads the creds itself, so the route no longer calls this."""
    from server.network import nac_harness
    return nac_harness.read_agent_creds()


@router.post("/harness/run", dependencies=[Depends(requires_permission("write:network"))])
async def harness_run(request: Request):
    """H4 harness dispatch — the host RUNS its own agent on a server in its mesh. Body:
    {server, harness_id, prompt, image?, mode?}. The bastion runs the EXISTING harness container
    locally (same image, different location); mode='echo' proves the lane before the container lands.

    TWO gates, exactly as on /mgmt/run.

    1. App scope — `write:network` (401 with no app token, 403 with a token that did not declare
       it; server/applications/middleware.py:247-257). The grant below is bound to the ENTITY and
       so says nothing about WHICH app is calling; this dependency is what stops any installed app
       holding any valid window token from starting an agent on the user's machine.
    2. The SAME signed-grant primitive as /mgmt/run, PLUS an image allowlist, enforced in the
       service (nac_harness.run_harness_op). A harness dispatch reads as a REQUESTED agent, so it
       needs a host-signed, user-approved grant that authorizes the 'harness:run' op on this server
       AND enumerates the permitted image(s) — the grant lists 'image:<ref>' tokens (or 'image:*'
       for any), carried inside the same signed `ops`, so the allowlist is tamper-evident for free.
       DEFAULT-DENY: no grant covering (server, harness:run, image) ⇒ refused before any dispatch,
       returned in the bastion's allowlist shape (exit=126); a request that names no image is asking
       for the bastion's default container and must be granted 'image:*' or the default sentinel —
       it can't slip past by omission. ALWAYS audited (op='harness:run').

    Not internal-auth gated: this is the caller-side dispatch (bastions connect inbound over their
    own bearer-authed reverse workers).

    Thin adapter: the H4 logic (signed-grant + image-allowlist default-deny gate + dispatch + audit)
    lives in the service (server/network/nac_harness.py::run_harness_op) so the mesh_harness_run host
    tool shares it without faking an HTTP Request. (2026-06-24)"""
    pool = brain_service.get_pool()
    if not pool:
        raise HTTPException(503, "brain pool not ready")
    eid = brain_service._entity_id
    body = await request.json()
    from server.network import nac_harness, nac_mgmt
    try:
        return await nac_harness.run_harness_op(
            pool, eid, body.get("server"), body.get("harness_id"),
            image=body.get("image"), prompt=body.get("prompt") or "",
            mode=(body.get("mode") or "echo"),
            worktree=body.get("worktree"), worktree_b64=body.get("worktree_b64"),
            command=body.get("command"), port=int(body.get("port") or 0),
            # Unset = the lane's own default (nac_harness.HARNESS_RUN_DEFAULT_MINUTES).
            max_minutes=int(body.get("max_minutes") or 0),
            repo=body.get("repo") or "", branch=body.get("branch"),
            # mode=lab fields (the Lab Harness) — ignored by the other modes
            hosts=int(body.get("hosts") or 0), envs=body.get("envs"),
            tests=body.get("tests"), engine=body.get("engine") or "",
            gate=body.get("gate") or "", app=body.get("app") or "",
            # mode=service fields (the service slot) — ignored by the other modes
            service_op=body.get("service_op") or "", service_name=body.get("service_name") or "",
            data_path=body.get("data_path") or "", data_uid=int(body.get("data_uid") or 0),
            socket=body.get("socket") or "",
            mirrors=body.get("mirrors"),
            purge=body.get("purge") is True)
    except nac_mgmt.MgmtError as e:
        raise HTTPException(e.status, e.detail)


# ══════════════════════════════════════════════════════════════════════
# Terminal sessions — a shell on one of
# the user's machines, streamed to the Terminal app. The gate + relay live in
# server/network/session_lane.py (the same signed-grant default-deny as /mgmt/run, the
# same audit table); this is the browser's door to it.
#
# WHY A TICKET. A browser WebSocket cannot carry the X-Application-Token header the
# permission dependency reads, and the shell hands an app its token only by stamping
# fetch() — the app never holds it. So the app first POSTs for a ticket over an ordinary
# permissioned fetch (write:network — the scope the Network app acts with), then opens
# the socket with `TICKET <t>` as its first frame. A ticket is one-shot, bound to the
# server it was minted for, and dead after 60 s.
# ══════════════════════════════════════════════════════════════════════

_SESSION_TICKETS: dict = {}          # ticket -> (server, app_id, expires_monotonic)
_SESSION_TICKET_TTL_S = 60.0


def _mint_session_ticket(server: str, app_id: str) -> str:
    import secrets as _sec
    import time as _time
    now = _time.monotonic()
    for t, (_s, _a, exp) in list(_SESSION_TICKETS.items()):
        if exp < now:
            _SESSION_TICKETS.pop(t, None)
    t = "st_" + _sec.token_urlsafe(24)
    _SESSION_TICKETS[t] = (server, app_id, now + _SESSION_TICKET_TTL_S)
    return t


def _redeem_session_ticket(ticket: str, server: str) -> Optional[str]:
    """The app_id the ticket was minted for, or None. Redeeming burns it."""
    import time as _time
    row = _SESSION_TICKETS.pop((ticket or "").strip(), None)
    if not row:
        return None
    s, app_id, exp = row
    if exp < _time.monotonic() or s != server:
        return None
    return app_id


@router.get("/sessions", dependencies=[Depends(requires_permission("read:network"))])
async def sessions_list():
    """Every shell open right now on any of the user's machines, whichever door opened it
    (origin 'app' = the user in the Terminal app; 'host-tool' was the host's `session` tool,
    gone 2026-09-24 — a Host holds no shell on a machine).
    Process-local: a session is a socket in this kernel."""
    from server.network import session_lane
    return {"sessions": session_lane.list_live()}


@router.post("/sessions/{session_id}/close", dependencies=[Depends(requires_permission("write:network"))])
async def session_close(session_id: str):
    """Close one open session from the host side — the user's lever over a shell the
    host's tool opened (or one of their own tabs). The worker socket closes and the
    machine kills the shell; the close is audited like any other."""
    from server.network import session_lane
    if not session_lane.close_live(session_id):
        raise HTTPException(404, "no open session with that id")
    return {"session_id": session_id, "closed": True}


@router.post("/sessions/{server}/ticket", dependencies=[Depends(requires_permission("write:network"))])
async def session_ticket(server: str, request: Request):
    """A one-shot ticket to open WS /api/network/sessions/{server}. Needs write:network —
    opening a shell on the user's machine is a management act, like /mgmt/run."""
    server = (server or "").strip()
    if not server:
        raise HTTPException(400, "server required")
    tok = getattr(request.state, "app_token", None)
    app_id = getattr(tok, "app_id", "") or ""
    return {"ticket": _mint_session_ticket(server, app_id), "expires_in": int(_SESSION_TICKET_TTL_S)}


@router.websocket("/sessions/{server}")
async def session_ws(ws: WebSocket, server: str):
    """The Terminal app's socket for ONE shell on `server`.

    Browser -> host: text frames are JSON control messages —
      {"t":"open","op":"session:open"|"session:root","cols":N,"rows":N,"shell":"","note":""}
      {"t":"resize","cols":N,"rows":N}
      {"t":"close"}
    and binary frames are keyboard bytes. Host -> browser: binary frames are pty output;
    text frames are JSON —
      {"t":"stepup","request_id","message"}   a root shell is waiting for approval ON the box
      {"t":"opened","session_id","user","started","op","server"}
      {"t":"notice","text"}                    the box's E frame, in plain words
      {"t":"exit","code"}                      the session is over (126 refused, 124 timer)
      {"t":"error","detail"}                   this side could not open it (no worker, no bastion)
    One socket = one session; reconnect is a new socket. Closing the socket kills the shell."""
    import asyncio as _asyncio
    from server.network import nac_mgmt, session_lane

    async def _close(code: int = 1000):
        try:
            await ws.close(code=code)
        except Exception:
            pass

    async def _say(obj: dict):
        await ws.send_text(json.dumps(obj))

    server = (server or "").strip()
    await ws.accept()
    try:
        hello = await _asyncio.wait_for(ws.receive_text(), timeout=10)
    except Exception:
        await _close(4400)
        return
    parts = (hello or "").split()
    if len(parts) != 2 or parts[0] != "TICKET" or _redeem_session_ticket(parts[1], server) is None:
        await _close(4401)
        return
    pool = brain_service.get_pool()
    if not pool:
        await _say({"t": "error", "detail": "brain pool not ready"})
        await _close(1011)
        return
    eid = brain_service._entity_id

    session = None
    pump = None
    try:
        # ── wait for the open ──
        while session is None:
            msg = await ws.receive()
            if msg.get("type") == "websocket.disconnect":
                return
            if msg.get("text") is None:
                continue
            try:
                ctl = json.loads(msg["text"])
            except json.JSONDecodeError:
                await _say({"t": "error", "detail": "control messages are JSON"})
                continue
            if ctl.get("t") == "close":
                return
            if ctl.get("t") != "open":
                continue
            op = (ctl.get("op") or session_lane.SESSION_OPEN_OP).strip()
            if op not in session_lane.SESSION_OPS:
                await _say({"t": "error", "detail": "op must be session:open or session:root"})
                continue
            session_id = session_lane.new_session_id()
            approval = ""
            # The approval the box will require, for EITHER op (session_lane.begin).
            # Root = the user approves ON the machine with the code. A shell on a
            # box that gates writes (bastion 0.33.0) = the person's approval from
            # their device or that machine's console, bound to this session id.
            # The HOST GATE runs first inside begin, so a server with no grant
            # never rings the person; only then is the request filed, the app
            # told what to say, and the answer polled until approved / denied /
            # gone.
            try:
                state = await session_lane.begin(
                    pool, eid, server, op, session_id, note=str(ctl.get("note") or ""))
            except Exception as e:
                await _say({"t": "error", "detail": f"could not ask for approval on {server}: "
                                                    f"{getattr(e, 'detail', None) or e}"})
                return
            if state.get("state") == "refused":
                await _say({"t": "exit", "code": session_lane.EXIT_REFUSED, "detail": state.get("detail")})
                return
            if state.get("state") == "not_enrolled":
                await _say({"t": "exit", "code": session_lane.EXIT_REFUSED,
                            "detail": f"'{server}' has no second factor enrolled — a root shell needs "
                                      "one on the machine (privatae-bastion stepup-enroll)"})
                return
            if state.get("state") not in ("pending", "approved"):
                await _say({"t": "exit", "code": session_lane.EXIT_REFUSED,
                            "detail": state.get("detail") or "the machine refused the request"})
                return
            if state.get("state") == "pending":
                req = state["request"]
                if req.get("via") == "device":
                    words = (f"approve on your device ({req.get('device')}): "
                             f"`privatae-bastion device-approvals` there, then "
                             f"`privatae-bastion device-approve {str(req.get('id'))[:8]}`")
                else:
                    words = (f"approve on {server}: run `privatae-bastion approvals` there, "
                             f"then `privatae-bastion approve {str(req.get('request') or req.get('id'))[:8]}`")
                await _say({"t": "stepup", "request_id": req.get("request") or req.get("id"),
                            "message": words})
                deadline = _asyncio.get_event_loop().time() + session_lane.STEPUP_WAIT_S
                while True:
                    if _asyncio.get_event_loop().time() > deadline:
                        await _say({"t": "exit", "code": session_lane.EXIT_REFUSED,
                                    "detail": "no approval arrived in time"})
                        return
                    # keep listening so a Close while waiting is honoured
                    try:
                        m = await _asyncio.wait_for(ws.receive(), session_lane.STEPUP_POLL_S)
                        if m.get("type") == "websocket.disconnect":
                            return
                        if m.get("text"):
                            try:
                                if json.loads(m["text"]).get("t") == "close":
                                    return
                            except json.JSONDecodeError:
                                pass
                    except _asyncio.TimeoutError:
                        pass
                    try:
                        state = await session_lane.poll(pool, eid, server, op, session_id, existing=req)
                    except Exception as e:
                        await _say({"t": "error", "detail": f"lost touch while waiting for approval: "
                                                            f"{getattr(e, 'detail', None) or e}"})
                        return
                    if state.get("state") == "approved":
                        break
                    if state.get("state") == "pending":
                        continue
                    await _say({"t": "exit", "code": session_lane.EXIT_REFUSED,
                                "detail": state.get("detail") or "the request was not approved"})
                    return
            approval = str(state.get("approval") or "")
            try:
                session = await session_lane.open_session(
                    pool, eid, server, op, cols=ctl.get("cols") or 80, rows=ctl.get("rows") or 24,
                    shell=str(ctl.get("shell") or ""), approval=approval, session_id=session_id)
            except nac_mgmt.MgmtError as e:
                await _say({"t": "error", "detail": e.detail})
                return
            await _say({"t": "opened", "session_id": session.session_id, "user": session.user,
                        "started": session.started, "op": op, "server": server})

        # ── pump box -> browser ──
        async def _pump():
            while True:
                ev = await session.recv()
                if ev is None:
                    return
                tag, payload = ev
                if tag == "O":
                    await ws.send_bytes(payload)
                elif tag == "E":
                    await _say({"t": "notice", "text": payload})
                elif tag == "X":
                    await _say({"t": "exit", "code": payload})
                    return
        pump = _asyncio.create_task(_pump())

        # ── browser -> box ──
        while not pump.done():
            recv = _asyncio.ensure_future(ws.receive())
            done, _ = await _asyncio.wait({recv, pump}, return_when=_asyncio.FIRST_COMPLETED)
            if recv not in done:
                recv.cancel()
                break
            msg = recv.result()
            if msg.get("type") == "websocket.disconnect":
                break
            if msg.get("bytes"):
                session.write(msg["bytes"])
            elif msg.get("text"):
                try:
                    ctl = json.loads(msg["text"])
                except json.JSONDecodeError:
                    continue
                if ctl.get("t") == "resize":
                    session.resize(ctl.get("cols"), ctl.get("rows"))
                elif ctl.get("t") == "close":
                    break
    except WebSocketDisconnect:
        pass
    except Exception as e:
        logger.warning("session socket for %s: %r", server, e)
    finally:
        if session is not None:
            session.close()
        if pump is not None and not pump.done():
            pump.cancel()
        await _close()


# ── P8.1 management grants: host proposes -> user approves (ALWAYS step-up — granting an entity
# rights ON a server is a WIDEN) -> host signs {entity, server, ops, expires} with its entity key.
# DEFAULT-DENY: no grant ⇒ no rights. Mirrors the egress propose/approve, inverted. ──

@router.get("/nac/mgmt/grants", dependencies=[Depends(requires_permission("read:topology"))])
async def nac_mgmt_grants_list():
    """The host's management grants (drafts + approved). Default is DENY (no grant ⇒ no rights)."""
    pool = brain_service.get_pool()
    eid = brain_service._entity_id
    from server.network import nac_commit
    return await nac_commit.mgmt_grants_list(pool, eid)


@router.post("/nac/mgmt/grant/propose", dependencies=[Depends(requires_permission("write:network"))])
async def nac_mgmt_grant_propose(request: Request):
    """Host-authored: a DRAFT management grant, inert until the user approves. Body:
    {server, ops?:[...], purposes?:[...], deny?:[...], note?}. `server` is a hostname or '*'. Granting rights is a
    WIDEN, so approval needs step-up."""
    pool = brain_service.get_pool()
    if not pool:
        raise HTTPException(503, "brain pool not ready")
    from server.network import nac_propose
    body = await request.json()
    try:
        return await nac_propose.propose_mgmt_grant(
            pool, server=body.get("server"), ops=body.get("ops"), deny=body.get("deny"),
            note=body.get("note"), purposes=body.get("purposes"))
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.post("/nac/mgmt/grant/{grant_id}/approve",
             dependencies=[Depends(requires_permission("resolve:decision"))])
async def nac_mgmt_grant_approve(grant_id: int, request: Request,
                                 x_authentik_email: Optional[str] = Header(None)):
    """User approves a draft grant. ALWAYS requires a step-up TOTP code (granting an entity
    rights ON a server is a WIDEN). The approved terms — {entity, server, ops, expires} — are
    signed with the host's entity key; the gate verifies the signature and enforces the SIGNED
    terms. The agent can't reach here: it can't produce the user's step-up code."""
    pool = brain_service.get_pool()
    eid = brain_service._entity_id
    body = await request.json() if request.headers.get("content-type", "").startswith("application/json") else {}
    from server.network import nac_commit
    try:
        return await nac_commit.approve_mgmt_grant(
            pool, eid, grant_id, stepup_code=body.get("stepup_code"),
            ttl_days=body.get("ttl_days"), approved_by=x_authentik_email, via="ui")
    except nac_commit.CommitError as e:
        raise HTTPException(e.status, e.detail)


@router.post("/nac/mgmt/grant/{grant_id}/revoke",
             dependencies=[Depends(requires_permission("write:network"))])
async def nac_mgmt_grant_revoke(grant_id: int, request: Request):
    """Revoke a management grant (host-local authority). Removing an allow tightens — free.
    Removing an approved grant that holds a DENY widens, so it needs the step-up code (body
    {stepup_code}), like removing an approved egress deny. The gate re-reads per dispatch."""
    pool = brain_service.get_pool()
    eid = brain_service._entity_id
    body = await request.json() if request.headers.get("content-type", "").startswith("application/json") else {}
    from server.network import nac_commit
    try:
        return await nac_commit.revoke_mgmt_grant(pool, eid, grant_id, stepup_code=body.get("stepup_code"))
    except nac_commit.CommitError as e:
        raise HTTPException(e.status, e.detail)


# ── Pairing grants (2026-09-08): the anchor host admits other hosts on its machine.
# Approve WIDENS (a host gets lanes on the user's box) → resolve:decision + step-up, like
# a mgmt grant approve. Deny and revoke tighten → write:network, no code. The service
# (server/network/pairing_grants.py) is the single gate; the manage_pairing host tool
# (op = approve | deny | revoke | consent) calls the same functions. ──

@router.get("/bastions/pairings/grants", dependencies=[Depends(requires_permission("read:topology"))])
async def pairing_grants_list(server: Optional[str] = None):
    """The pairing grants this host signed as anchor (live + revoked), newest first."""
    pool = brain_service.get_pool()
    if not pool:
        return {"grants": []}
    from server.network import pairing_grants
    return {"grants": await pairing_grants.list_grants(pool, brain_service._entity_id, server)}


@router.post("/bastions/pairings/approve",
             dependencies=[Depends(requires_permission("resolve:decision"))])
async def pairing_approve(request: Request, x_authentik_email: Optional[str] = Header(None)):
    """User admits a pending host on a machine they anchored. Body {server, subject,
    caps: [...], stepup_code, ttl_days?}. ALWAYS step-up: a host gaining lanes on the
    user's machine is a widen. Signs the grant with the host's entity key and pushes it
    to the box over the anchor's own mgmt lane."""
    pool = brain_service.get_pool()
    eid = brain_service._entity_id
    body = await request.json() if request.headers.get("content-type", "").startswith("application/json") else {}
    from server.network import pairing_grants
    try:
        return await pairing_grants.approve_pairing(
            pool, eid, body.get("server"), body.get("subject"), body.get("caps"),
            stepup_code=body.get("stepup_code"), approved_by=x_authentik_email,
            ttl_days=body.get("ttl_days"), models=body.get("models"), via="ui")
    except pairing_grants.PairingError as e:
        raise HTTPException(e.status, e.detail)


@router.post("/bastions/pairings/deny",
             dependencies=[Depends(requires_permission("write:network"))])
async def pairing_deny(request: Request, x_authentik_email: Optional[str] = Header(None)):
    """User refuses a pending pairing. Recorded here only; the box keeps reporting it."""
    pool = brain_service.get_pool()
    eid = brain_service._entity_id
    body = await request.json() if request.headers.get("content-type", "").startswith("application/json") else {}
    from server.network import pairing_grants
    try:
        return await pairing_grants.deny_pairing(
            pool, eid, body.get("server"), body.get("subject"), denied_by=x_authentik_email,
            via="ui")
    except pairing_grants.PairingError as e:
        raise HTTPException(e.status, e.detail)


@router.post("/bastions/pairings/revoke",
             dependencies=[Depends(requires_permission("write:network"))])
async def pairing_revoke(request: Request):
    """User removes an admitted host: signed pairing_revoke terms + pairing_grant_drop
    to the box, which tears its lanes down at once. Tightens — no code."""
    pool = brain_service.get_pool()
    eid = brain_service._entity_id
    body = await request.json() if request.headers.get("content-type", "").startswith("application/json") else {}
    from server.network import pairing_grants
    try:
        return await pairing_grants.revoke_pairing(pool, eid, body.get("server"), body.get("subject"))
    except pairing_grants.PairingError as e:
        raise HTTPException(e.status, e.detail)


# ── Sharing a machine's models with another host. The card in Circles
# calls these; the manage_pairing tool (ask | accept | decline) calls the same
# service functions. accept WIDENS — a host gets a lane on the user's machine —
# so it is resolve:decision + the step-up code, exactly like approve above.
# decline and withdraw write nothing near the machine, so they tighten and are
# free. server/network/machine_offers.py carries the reasoning for all of it. ──

@router.get("/bastions/machine-offers", dependencies=[Depends(requires_permission("read:topology"))])
async def machine_offers_list(direction: Optional[str] = None, state: Optional[str] = "open"):
    """Asks to share a machine's models — 'in' (someone asked us; the user
    decides) and 'out' (we asked, waiting). Open ones by default."""
    pool = brain_service.get_pool()
    if not pool:
        return {"offers": []}
    from server.network import machine_offers
    return {"offers": await machine_offers.list_offers(pool, direction=direction, state=state)}


@router.post("/bastions/machine-offers/accept",
             dependencies=[Depends(requires_permission("resolve:decision"))])
async def machine_offer_accept(request: Request, x_authentik_email: Optional[str] = Header(None)):
    """Approve an incoming ask. Body {offer_id, server, models: [...], caps?,
    stepup_code, ttl_days?}. Fetches a fresh enrolment token from the asking
    host over the DIRECT peer lane, hands it to the named machine, and signs a
    grant scoped to those models. Nobody types anything on the machine."""
    pool = brain_service.get_pool()
    eid = brain_service._entity_id
    body = await request.json() if request.headers.get("content-type", "").startswith("application/json") else {}
    from server.network import machine_offers, pairing_grants
    try:
        return await machine_offers.accept_inbound(
            pool, eid, body.get("offer_id"), server=body.get("server"),
            caps=body.get("caps") or ["inference"], models=body.get("models"),
            stepup_code=str(body.get("stepup_code") or ""), ttl_days=body.get("ttl_days"),
            decided_by=x_authentik_email or "user", via="ui")
    except machine_offers.OfferError as e:
        raise HTTPException(e.status, e.message)
    except pairing_grants.PairingError as e:
        # This path runs three pairing_grants calls (_anchored_here, the redeem,
        # the grant), and each raises PairingError, not OfferError. There is no
        # app-level handler for it, so without this the card gets a bare 500
        # instead of the sentence the service wrote — "that machine is anchored
        # elsewhere", "it runs a bastion too old to honour a model list".
        raise HTTPException(e.status, e.detail)


@router.post("/bastions/machine-offers/decline",
             dependencies=[Depends(requires_permission("write:network"))])
async def machine_offer_decline(request: Request, x_authentik_email: Optional[str] = Header(None)):
    """Refuse an incoming ask. Nothing is written on the machine — the box is
    never asked — which is the sentence to show the user."""
    pool = brain_service.get_pool()
    body = await request.json() if request.headers.get("content-type", "").startswith("application/json") else {}
    from server.network import machine_offers
    try:
        return await machine_offers.decline_inbound(
            pool, body.get("offer_id"), decided_by=x_authentik_email or "user", via="ui")
    except machine_offers.OfferError as e:
        raise HTTPException(e.status, e.message)


@router.post("/bastions/machine-offers/ask",
             dependencies=[Depends(requires_permission("write:network"))])
async def machine_offer_ask(request: Request):
    """Ask another host for a model on one of THEIR machines. Body
    {peer_entity_id, purpose, models?}. Carries no credential and no reach."""
    pool = brain_service.get_pool()
    eid = brain_service._entity_id
    body = await request.json() if request.headers.get("content-type", "").startswith("application/json") else {}
    from server.network import machine_offers
    try:
        return await machine_offers.send_offer(
            pool, eid, body.get("peer_entity_id"), purpose=body.get("purpose"),
            models_asked=body.get("models"))
    except machine_offers.OfferError as e:
        raise HTTPException(e.status, e.message)


@router.post("/bastions/machine-offers/withdraw",
             dependencies=[Depends(requires_permission("write:network"))])
async def machine_offer_withdraw(request: Request):
    """Take back an ask we sent. This also closes the standing permission it
    gave that host to make this one mint an enrolment token."""
    pool = brain_service.get_pool()
    body = await request.json() if request.headers.get("content-type", "").startswith("application/json") else {}
    from server.network import machine_offers
    try:
        return await machine_offers.withdraw_offer(pool, body.get("offer_id"))
    except machine_offers.OfferError as e:
        raise HTTPException(e.status, e.message)


@router.get("/nac/mgmt/audit", dependencies=[Depends(requires_permission("read:topology"))])
async def nac_mgmt_audit_list():
    """Command accounting — the management audit trail (v060 nac_mgmt_audit). Every management
    action ON the user's machine is recorded: where (server), what (op), how it was decided, the
    exit code, and when. The bright line vs egress, which logs NOTHING (sovereignty). Most-recent
    first, capped at 100. Degrades to empty on a pod missing the table (no pool)."""
    pool = brain_service.get_pool()
    if not pool:
        return {"audit": []}
    # This used to run nac_mgmt._MGMT_AUDIT_DDL before reading, so a pod that had
    # never written an audit line still answered instead of 500ing. That
    # constant is gone — v060_nac_mgmt.sql:38 declares the table and
    # entrypoint applies it at boot — so the read is guarded instead. Same
    # degradation the docstring always promised: an empty list, never a 500.
    try:
        rows = await pool.fetch(
            "SELECT server, op, decision, exit_code, at FROM nac_mgmt_audit "
            "ORDER BY at DESC LIMIT 100")
    except asyncpg.exceptions.UndefinedTableError as e:
        # Narrow ON PURPOSE. The degradation is for one condition — a pod whose
        # table does not exist yet — and only that condition is a legitimate
        # empty answer. A dropped connection, a permission error or a statement
        # timeout caught here would tell the user "no management actions were
        # recorded on your machine", which is a false statement about their
        # security record, logged at info and visible nowhere. Those keep the
        # 500 they always had.
        logger.info("mgmt audit list unavailable — table absent: %r", e)
        return {"audit": []}
    out = []
    for r in rows:
        d = dict(r)
        if d.get("at") is not None and hasattr(d["at"], "isoformat"):
            d["at"] = d["at"].isoformat()
        out.append(d)
    return {"audit": out}


# ── Per-bastion SUSPEND/RESUME (temporary, distinct from revoke) ──────────────
# Suspend is the "pause this box right now" lever: a TIGHTEN that short-circuits the
# management + harness gate so the server denies even with valid signed grants — WITHOUT
# touching those grants. Resume (a WIDEN) deletes the row and the standing grants take
# effect again. This is the bright distinction from revoke, which tears the grant down.
# Suspend tightens → no step-up; resume widens → step-up; both are audited (op=suspend/resume).

@router.get("/nac/mgmt/suspended", dependencies=[Depends(requires_permission("read:topology"))])
async def nac_mgmt_suspended_list():
    """The currently-suspended servers (server + when + by whom). A suspended server denies
    at the gate regardless of its grants; resuming restores them. Degrades to empty (no pool)."""
    pool = brain_service.get_pool()
    eid = brain_service._entity_id
    from server.network import nac_commit
    return await nac_commit.mgmt_suspended_list(pool, eid)


@router.post("/nac/mgmt/suspend", dependencies=[Depends(requires_permission("write:network"))])
async def nac_mgmt_suspend(request: Request, x_authentik_email: Optional[str] = Header(None)):
    """Suspend a bastion (TIGHTEN — no step-up). Body: {server}. Inserts the suspend row so the
    management + harness gate denies that server even with valid signed grants. The grants are
    untouched (that's the point vs revoke) — resuming restores them. Audited (op='suspend')."""
    pool = brain_service.get_pool()
    if not pool:
        raise HTTPException(503, "brain pool not ready")
    eid = brain_service._entity_id
    body = await request.json() if request.headers.get("content-type", "").startswith("application/json") else {}
    from server.network import nac_commit
    try:
        return await nac_commit.suspend_bastion(pool, eid, body.get("server"), x_authentik_email)
    except nac_commit.CommitError as e:
        raise HTTPException(e.status, e.detail)


@router.post("/nac/mgmt/resume", dependencies=[Depends(requires_permission("write:network"))])
async def nac_mgmt_resume(request: Request, x_authentik_email: Optional[str] = Header(None)):
    """Resume a suspended bastion (WIDEN — requires a step-up TOTP code). Body: {server,
    stepup_code}. Deletes the suspend row so the server's standing grants take effect again.
    Restoring reach is a widen (the agent can't produce the user's code), so it steps up.
    Audited (op='resume')."""
    pool = brain_service.get_pool()
    if not pool:
        raise HTTPException(503, "brain pool not ready")
    eid = brain_service._entity_id
    body = await request.json() if request.headers.get("content-type", "").startswith("application/json") else {}
    from server.network import nac_commit
    try:
        return await nac_commit.resume_bastion(pool, eid, body.get("server"),
                                                str(body.get("stepup_code", "")), x_authentik_email)
    except nac_commit.CommitError as e:
        raise HTTPException(e.status, e.detail)


# ── C1: REVERSIBLE box-level QUARANTINE (whole-mesh) ──────────────────────────
# Quarantine is the missing rung between suspend (reversible, but only the mgmt +
# harness LANES deny — the WG tunnel stays up) and revoke_bastion (DESTRUCTIVE —
# revokes the bearer, so the box must re-enrol). A quarantine cuts the bastion's
# ENTIRE mesh reach by dropping its concentrator peer (bastion.unenroll → the
# forwarder drops the WG peer) AND short-circuiting the mgmt/harness gates, while
# leaving the bearer in mcp_client_tokens intact so a RELEASE brings the box back
# with no re-enrol (restore_concentrator_peer un-revokes the same peer — same cert,
# same tunnel IPs). Quarantine TIGHTENS (no step-up); release WIDENS (step-up TOTP),
# the same posture pair as suspend/resume. Both audited via the nac_agent_audit
# disposition trail (disposition='quarantine'/'release'), the host's own record.


async def _resolve_bastion(pool, entity_id: int, *, server: str = "", token_id=None):
    """Resolve a bastion of THIS entity by hostname OR token_id → (token_id, server,
    wg_bastion_pubkey). Scoped to operator_kind='bastion' + not-revoked so a bearer
    can never reach another host's box or a non-bastion token. The pubkey is the
    enrol-time copy in mcp_client_tokens.notes (authoritative); server falls back to
    the latest heartbeat hostname when only a token_id was given. Returns None if no
    matching live bastion."""
    row = None
    if isinstance(token_id, int):
        row = await pool.fetchrow(
            "SELECT id, notes FROM mcp_client_tokens WHERE id=$1 AND entity_id=$2 "
            "AND operator_kind='bastion' AND revoked_at IS NULL", token_id, entity_id)
    elif server:
        # hostname → the live bastion behind it (latest heartbeat), then its token row.
        tid = await pool.fetchval(
            "SELECT token_id FROM network_bastion_heartbeat WHERE hostname=$1 "
            "ORDER BY last_seen_at DESC LIMIT 1", server)
        if tid is not None:
            row = await pool.fetchrow(
                "SELECT id, notes FROM mcp_client_tokens WHERE id=$1 AND entity_id=$2 "
                "AND operator_kind='bastion' AND revoked_at IS NULL", tid, entity_id)
    if row is None:
        return None
    nts = row["notes"]
    if isinstance(nts, str):
        try:
            nts = json.loads(nts)
        except Exception:
            nts = {}
    pubkey = (nts or {}).get("wg_bastion_pubkey")
    name = server
    if not name:
        name = await pool.fetchval(
            "SELECT hostname FROM network_bastion_heartbeat WHERE token_id=$1 "
            "ORDER BY last_seen_at DESC LIMIT 1", row["id"]) or ""
    return {"token_id": int(row["id"]), "server": name, "wg_bastion_pubkey": pubkey}


@router.get("/nac/quarantined", dependencies=[Depends(requires_permission("read:topology"))])
async def nac_quarantined_list():
    """The currently-quarantined bastions (server + token + when + by whom). A quarantined
    box denies at the mgmt/harness gate AND has its concentrator peer dropped (no tunnel);
    releasing restores both. Degrades to empty (no pool / cold table)."""
    pool = brain_service.get_pool()
    eid = brain_service._entity_id
    from server.network import nac_commit
    return await nac_commit.quarantined_list(pool, eid)


@router.post("/nac/quarantine", dependencies=[Depends(requires_permission("write:network"))])
async def nac_quarantine(request: Request, x_authentik_email: Optional[str] = Header(None)):
    """Quarantine a bastion (TIGHTEN — no step-up). Body: {server} or {token_id}. Cuts the
    box's WHOLE mesh reach: inserts the quarantine row (mgmt + harness gates then deny) AND
    drops its concentrator peer so the WG tunnel — egress + inbound — goes dead on the next
    forwarder reconcile. The bearer in mcp_client_tokens is left intact (that's the point vs
    revoke) so /nac/unquarantine brings the box back with no re-enrol. Audited via the
    agent-governance disposition trail (disposition='quarantine')."""
    pool = brain_service.get_pool()
    if not pool:
        raise HTTPException(503, "brain pool not ready")
    eid = brain_service._entity_id
    body = await request.json() if request.headers.get("content-type", "").startswith("application/json") else {}
    from server.network import nac_commit
    try:
        return await nac_commit.quarantine_bastion(pool, eid, body.get("server"),
                                                    body.get("token_id"), x_authentik_email)
    except nac_commit.CommitError as e:
        raise HTTPException(e.status, e.detail)


@router.post("/nac/unquarantine", dependencies=[Depends(requires_permission("write:network"))])
async def nac_unquarantine(request: Request, x_authentik_email: Optional[str] = Header(None)):
    """Release a quarantined bastion (WIDEN — requires a step-up TOTP code). Body: {server} or
    {token_id} + stepup_code. Restoring a box's whole-mesh reach is a widen (a manipulated agent
    can't produce the user's code, so it can't release itself), so it steps up. Deletes the
    quarantine row (mgmt + harness gates re-permit) and restores the concentrator peer so the WG
    tunnel comes back — same bearer, same cert, no re-enrol. Audited (disposition='release')."""
    pool = brain_service.get_pool()
    if not pool:
        raise HTTPException(503, "brain pool not ready")
    eid = brain_service._entity_id
    body = await request.json() if request.headers.get("content-type", "").startswith("application/json") else {}
    from server.network import nac_commit
    try:
        return await nac_commit.unquarantine_bastion(pool, eid, body.get("server"), body.get("token_id"),
                                                      str(body.get("stepup_code", "")), x_authentik_email)
    except nac_commit.CommitError as e:
        raise HTTPException(e.status, e.detail)
