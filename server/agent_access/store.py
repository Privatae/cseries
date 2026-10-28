"""The rows behind an outside agent's connection (migrations/v255_agent_access.sql).

Every code and token is a 256-bit random value handed out once and kept only
as its SHA-256. A connection belongs to this Host's entity and is revoked on
its own; revoking it ends its tokens, its permissions and its open asks.

Nothing here decides what a person approved. The pages (oauth.py, asks.py)
check the person first and only then call connect / decide_ask.
"""
from __future__ import annotations

import base64
import hashlib
import json
import re
import secrets
from typing import Any, Optional

# Lifetimes. A code is used within seconds of the redirect; an hour of access
# is what the MCP clients refresh against; a refresh token outlives a week
# away from the keyboard. An ask waits a day for the person.
CODE_TTL_S = 600
ACCESS_TTL_S = 3600
REFRESH_TTL_S = 30 * 24 * 3600
ASK_TTL_S = 24 * 3600

# A platform protection, not a user limit: registration needs no sign-in, so
# a stranger could fill the table. Clients that never connected are dropped
# after a day, and at most this many wait at once: a new registration past it
# drops the oldest that never connected. It never refuses, because a stranger
# who filled it would otherwise stop the person connecting their own agent
# for a day.
MAX_UNCONNECTED = 50
UNCONNECTED_TTL_S = 24 * 3600

CLIENT_NAME_MAX = 120
REDIRECT_URIS_MAX = 10
REDIRECT_URI_MAX = 2000
NOTE_MAX = 500

ACCESS_PREFIX = "agt_"
REFRESH_PREFIX = "agr_"
CODE_PREFIX = "agc_"
CLIENT_PREFIX = "agent-"

READ, OP = "read", "op"
ANY = "*"


class AgentAccessError(Exception):
    """An OAuth-shaped refusal: `error` is the RFC 6749 / 7591 code, `detail`
    the sentence for the person or the client."""

    def __init__(self, error: str, detail: str, status: int = 400):
        super().__init__(detail)
        self.error = error
        self.detail = detail
        self.status = status


def _sha(raw: str) -> bytes:
    return hashlib.sha256(raw.encode("utf-8")).digest()


def new_secret(prefix: str) -> tuple[str, bytes]:
    raw = prefix + secrets.token_urlsafe(32)
    return raw, _sha(raw)


def args_digest(args) -> str:
    """The box's own digest of an op's arguments (nac_approval.args_digest),
    so a 'just this once' approval binds exactly the bytes the machine would
    be asked about."""
    from server.network.nac_approval import args_digest as _digest
    return _digest([str(a) for a in (args or [])])


# ── PKCE (RFC 7636), S256 only ──────────────────────────────────────────────

_VERIFIER = re.compile(r"[A-Za-z0-9\-._~]{43,128}")
_CHALLENGE = re.compile(r"[A-Za-z0-9\-_]{43}")


def valid_challenge(challenge: str) -> bool:
    return bool(_CHALLENGE.fullmatch(challenge or ""))


def pkce_matches(verifier: str, challenge: str) -> bool:
    if not _VERIFIER.fullmatch(verifier or ""):
        return False
    want = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=")
    return secrets.compare_digest(want.decode("ascii"), challenge or "")


# ── redirect URIs (RFC 8252 §7.3, OAuth 2.1 §2.3.1) ─────────────────────────

_LABEL = r"[a-z0-9](?:[a-z0-9\-]{0,61}[a-z0-9])?"
_HOSTNAME = re.compile(rf"{_LABEL}(?:\.{_LABEL})*")
_IPV6 = re.compile(r"[0-9a-f:.]{2,45}")

def redirect_uri_ok(uri: str) -> bool:
    """https anywhere, or http on the loopback (a CLI's local listener).
    No fragment, no credentials in the URL."""
    from urllib.parse import urlsplit
    if not isinstance(uri, str) or not uri or len(uri) > REDIRECT_URI_MAX:
        return False
    try:
        u = urlsplit(uri)
        host = (u.hostname or "").lower()
        u.port  # noqa: B018 — raises on a bad port
    except ValueError:
        return False
    if u.fragment or u.username is not None or u.password is not None or not host:
        return False
    # A host name or an IP address and nothing else: the connect page names
    # this host in the Host's own sentence (pages.connect_page), so it may not
    # carry words ("https://your host checked this.example/").
    if not (_HOSTNAME.fullmatch(host) or _IPV6.fullmatch(host)):
        return False
    if u.scheme == "https":
        return True
    return u.scheme == "http" and host in ("127.0.0.1", "::1", "localhost")


def redirect_matches(registered: list, uri: str) -> bool:
    """Exact match, except that a loopback redirect may name any port
    (RFC 8252 §7.3): a CLI picks a free port each time."""
    from urllib.parse import urlsplit
    if uri in registered:
        return True
    try:
        u = urlsplit(uri)
    except ValueError:
        return False
    if u.scheme != "http" or (u.hostname or "").lower() not in ("127.0.0.1", "::1", "localhost"):
        return False
    for r in registered:
        try:
            v = urlsplit(r)
        except ValueError:
            continue
        if (v.scheme, (v.hostname or "").lower(), v.path, v.query) == \
                (u.scheme, (u.hostname or "").lower(), u.path, u.query):
            return True
    return False


# ── clients and connections ─────────────────────────────────────────────────

def _clean_name(name) -> str:
    text = "" if name is None else str(name)
    text = "".join(ch for ch in text if ch.isprintable()).strip()
    return text[:CLIENT_NAME_MAX]


async def register_client(pool, eid: int, client_name, redirect_uris) -> dict:
    if not isinstance(redirect_uris, list) or not redirect_uris \
            or len(redirect_uris) > REDIRECT_URIS_MAX:
        raise AgentAccessError("invalid_redirect_uri",
                               f"redirect_uris must list 1 to {REDIRECT_URIS_MAX} addresses")
    for u in redirect_uris:
        if not redirect_uri_ok(u):
            raise AgentAccessError("invalid_redirect_uri",
                                   "each redirect URI must be https, or http on 127.0.0.1, ::1 "
                                   "or localhost, with no fragment")
    name = _clean_name(client_name) or "An unnamed agent"
    client_id = CLIENT_PREFIX + secrets.token_hex(12)
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "DELETE FROM agent_connections WHERE entity_id=$1 AND connected_at IS NULL "
                "AND registered_at < now() - make_interval(secs => $2)", eid, UNCONNECTED_TTL_S)
            waiting = await conn.fetchval(
                "SELECT count(*) FROM agent_connections WHERE entity_id=$1 AND connected_at IS NULL",
                eid)
            if waiting >= MAX_UNCONNECTED:
                await conn.execute(
                    "DELETE FROM agent_connections WHERE id IN (SELECT id FROM agent_connections "
                    "WHERE entity_id=$1 AND connected_at IS NULL ORDER BY registered_at, id LIMIT $2)",
                    eid, waiting - MAX_UNCONNECTED + 1)
            await conn.execute(
                "INSERT INTO agent_connections (entity_id, client_id, client_name, redirect_uris) "
                "VALUES ($1, $2, $3, $4::jsonb)", eid, client_id, name, json.dumps(redirect_uris))
    return {"client_id": client_id, "client_name": name, "redirect_uris": redirect_uris}


def _row(r) -> Optional[dict]:
    if r is None:
        return None
    d = dict(r)
    if isinstance(d.get("redirect_uris"), str):
        d["redirect_uris"] = json.loads(d["redirect_uris"])
    if isinstance(d.get("args"), str):
        d["args"] = json.loads(d["args"])
    if isinstance(d.get("waiting"), str):
        d["waiting"] = json.loads(d["waiting"])
    return d


async def client(pool, eid: int, client_id: str) -> Optional[dict]:
    """A registered, unrevoked client of this Host, or None."""
    if not isinstance(client_id, str) or not client_id.startswith(CLIENT_PREFIX):
        return None
    return _row(await pool.fetchrow(
        "SELECT id, client_id, client_name, redirect_uris, connected_at, last_used_at "
        "FROM agent_connections WHERE entity_id=$1 AND client_id=$2 AND revoked_at IS NULL",
        eid, client_id))


async def connect(pool, eid: int, connection_id: int, *, read: bool) -> None:
    """The person approved the connect page: the connection is live and holds
    `read` exactly when they ticked it, this time (connecting an agent again
    with the box clear takes back the read an earlier connect gave). Called
    only after the person was confirmed."""
    async with pool.acquire() as conn:
        async with conn.transaction():
            n = await conn.execute(
                "UPDATE agent_connections SET connected_at = COALESCE(connected_at, now()) "
                "WHERE id=$1 AND entity_id=$2 AND revoked_at IS NULL", connection_id, eid)
            if n.endswith(" 0"):
                raise AgentAccessError("invalid_client", "this agent's registration is gone")
            if read:
                await conn.execute(
                    "INSERT INTO agent_grants (connection_id, kind, server, op) "
                    "VALUES ($1, 'read', '*', '*') ON CONFLICT DO NOTHING", connection_id)
            else:
                await conn.execute(
                    "UPDATE agent_grants SET revoked_at = now() WHERE connection_id=$1 "
                    "AND kind='read' AND revoked_at IS NULL", connection_id)


async def issue_code(pool, connection_id: int, redirect_uri: str, code_challenge: str,
                     resource: str = "") -> str:
    raw, digest = new_secret(CODE_PREFIX)
    await pool.execute(
        "INSERT INTO agent_codes (code_hash, connection_id, redirect_uri, code_challenge, "
        "resource, expires_at) VALUES ($1, $2, $3, $4, $5, now() + make_interval(secs => $6))",
        digest, connection_id, redirect_uri, code_challenge, resource or "", CODE_TTL_S)
    return raw


async def _tokens(conn, connection_id: int, from_code: Optional[bytes]) -> dict:
    """A new access and refresh pair, both marked with the authorization code
    they descend from (from_code), so a replay of that code revokes them."""
    access, a_hash = new_secret(ACCESS_PREFIX)
    refresh, r_hash = new_secret(REFRESH_PREFIX)
    await conn.execute(
        "INSERT INTO agent_tokens (token_hash, connection_id, kind, expires_at, from_code) VALUES "
        "($1, $3, 'access', now() + make_interval(secs => $4), $6), "
        "($2, $3, 'refresh', now() + make_interval(secs => $5), $6)",
        a_hash, r_hash, connection_id, ACCESS_TTL_S, REFRESH_TTL_S, from_code)
    return {"access_token": access, "token_type": "Bearer", "expires_in": ACCESS_TTL_S,
            "refresh_token": refresh}


async def redeem_code(pool, eid: int, client_id: str, code: str, redirect_uri: str,
                      code_verifier: str) -> dict:
    """authorization_code grant: the code is spent whatever happens next, and a
    code presented again revokes every token that descends from it (RFC 6749
    §4.1.2): someone else held it, and this Host cannot tell which caller is
    the agent."""
    if not isinstance(code, str) or not code.startswith(CODE_PREFIX):
        raise AgentAccessError("invalid_grant", "the code is not one this Host issued")
    digest = _sha(code)
    # Spent in its own statement, committed before anything is checked: a
    # wrong verifier, client or redirect burns the code too (RFC 6749 §4.1.2).
    row = await pool.fetchrow(
        "UPDATE agent_codes c SET used_at = now() FROM agent_connections a "
        "WHERE c.code_hash=$1 AND c.used_at IS NULL AND c.expires_at > now() "
        "AND a.id = c.connection_id AND a.entity_id=$2 AND a.revoked_at IS NULL "
        "AND a.connected_at IS NOT NULL "
        "RETURNING c.connection_id, c.redirect_uri, c.code_challenge, a.client_id",
        digest, eid)
    if row is None:
        replayed = await pool.fetchval(
            "UPDATE agent_tokens t SET revoked_at = now() FROM agent_codes c, agent_connections a "
            "WHERE c.code_hash=$1 AND c.used_at IS NOT NULL AND a.id = c.connection_id "
            "AND a.entity_id=$2 AND t.from_code = c.code_hash AND t.revoked_at IS NULL "
            "RETURNING c.connection_id", digest, eid)
        if replayed is not None:
            import logging
            logging.getLogger("cseries.agent_access").warning(
                "agent connection %s: a spent authorization code was presented again; the "
                "tokens issued from it are revoked", replayed)
        raise AgentAccessError("invalid_grant", "the code is unknown, used or expired")
    if row["client_id"] != client_id:
        raise AgentAccessError("invalid_grant", "the code was issued to another agent")
    if row["redirect_uri"] != redirect_uri:
        raise AgentAccessError("invalid_grant", "redirect_uri differs from the authorize request")
    if not pkce_matches(code_verifier, row["code_challenge"]):
        raise AgentAccessError("invalid_grant", "code_verifier does not match the code_challenge")
    async with pool.acquire() as conn:
        async with conn.transaction():
            return await _tokens(conn, row["connection_id"], digest)


async def refresh_tokens(pool, eid: int, client_id: str, refresh_token: str) -> dict:
    """refresh_token grant, rotated: the old refresh token is revoked.

    A refresh token presented again after it was rotated away (or revoked)
    means two parties hold it, the agent and whoever copied it, and this Host
    cannot tell which one is asking. Every live token of its connection is
    revoked then (RFC 9700 §4.14.2), so a copied token stops working the moment
    the real agent next refreshes; the agent signs in again, and the
    connection and its permissions stay."""
    if not isinstance(refresh_token, str) or not refresh_token.startswith(REFRESH_PREFIX):
        raise AgentAccessError("invalid_grant", "the refresh token is not one this Host issued")
    digest = _sha(refresh_token)
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                "UPDATE agent_tokens t SET revoked_at = now() FROM agent_connections a "
                "WHERE t.token_hash=$1 AND t.kind='refresh' AND t.revoked_at IS NULL "
                "AND t.expires_at > now() AND a.id = t.connection_id AND a.entity_id=$2 "
                "AND a.revoked_at IS NULL RETURNING t.connection_id, t.from_code, a.client_id",
                digest, eid)
            if row is not None:
                if row["client_id"] != client_id:
                    raise AgentAccessError("invalid_grant", "the refresh token belongs to another agent")
                return await _tokens(conn, row["connection_id"], row["from_code"])
    reused = await pool.fetchval(
        "UPDATE agent_tokens t SET revoked_at = now() FROM agent_tokens old, agent_connections a "
        "WHERE old.token_hash=$1 AND old.kind='refresh' AND old.revoked_at IS NOT NULL "
        "AND a.id = old.connection_id AND a.entity_id=$2 "
        "AND t.connection_id = old.connection_id AND t.revoked_at IS NULL "
        "RETURNING old.connection_id", digest, eid)
    if reused is not None:
        import logging
        logging.getLogger("cseries.agent_access").warning(
            "agent connection %s: a rotated refresh token was presented again; its live tokens "
            "are revoked", reused)
    raise AgentAccessError("invalid_grant", "the refresh token is unknown, used, expired or revoked")


async def revoke_token(pool, eid: int, token: str) -> None:
    """RFC 7009: unknown tokens are not an error."""
    if not isinstance(token, str) or not token:
        return
    await pool.execute(
        "UPDATE agent_tokens t SET revoked_at = now() FROM agent_connections a "
        "WHERE t.token_hash=$1 AND t.revoked_at IS NULL AND a.id = t.connection_id "
        "AND a.entity_id=$2", _sha(token), eid)


async def resolve_access(pool, eid: int, token: str) -> Optional[dict]:
    """The live connection behind an access token, or None."""
    if not isinstance(token, str) or not token.startswith(ACCESS_PREFIX):
        return None
    row = await pool.fetchrow(
        "UPDATE agent_connections a SET last_used_at = now() FROM agent_tokens t "
        "WHERE t.token_hash=$1 AND t.kind='access' AND t.revoked_at IS NULL "
        "AND t.expires_at > now() AND a.id = t.connection_id AND a.entity_id=$2 "
        "AND a.revoked_at IS NULL AND a.connected_at IS NOT NULL "
        "RETURNING a.id, a.client_id, a.client_name", _sha(token), eid)
    return dict(row) if row else None


# ── permissions ─────────────────────────────────────────────────────────────

async def grants(pool, connection_id: int) -> list[dict]:
    rows = await pool.fetch(
        "SELECT id, kind, server, op, granted_at FROM agent_grants "
        "WHERE connection_id=$1 AND revoked_at IS NULL ORDER BY id", connection_id)
    return [dict(r) for r in rows]


def holds(held: list[dict], kind: str, server: str, op: str) -> bool:
    for g in held:
        if g["kind"] != kind:
            continue
        if g["server"] in (ANY, server) and g["op"] in (ANY, op):
            return True
    return False


async def revoke_connection(pool, eid: int, connection_id: int) -> bool:
    async with pool.acquire() as conn:
        async with conn.transaction():
            n = await conn.execute(
                "UPDATE agent_connections SET revoked_at = now() "
                "WHERE id=$1 AND entity_id=$2 AND revoked_at IS NULL", connection_id, eid)
            if n.endswith(" 0"):
                return False
            await conn.execute("UPDATE agent_tokens SET revoked_at = now() "
                               "WHERE connection_id=$1 AND revoked_at IS NULL", connection_id)
            await conn.execute("UPDATE agent_grants SET revoked_at = now() "
                               "WHERE connection_id=$1 AND revoked_at IS NULL", connection_id)
            await conn.execute("UPDATE agent_asks SET state='refused', decided_at = now() "
                               "WHERE connection_id=$1 AND state IN ('waiting', 'once')",
                               connection_id)
    return True


async def revoke_grant(pool, eid: int, grant_id: int) -> bool:
    n = await pool.execute(
        "UPDATE agent_grants g SET revoked_at = now() FROM agent_connections a "
        "WHERE g.id=$1 AND g.revoked_at IS NULL AND a.id = g.connection_id AND a.entity_id=$2",
        grant_id, eid)
    return not n.endswith(" 0")


async def list_connections(pool, eid: int) -> list[dict]:
    rows = await pool.fetch(
        "SELECT id, client_name, redirect_uris, connected_at, last_used_at FROM agent_connections "
        "WHERE entity_id=$1 AND revoked_at IS NULL AND connected_at IS NOT NULL "
        "ORDER BY connected_at DESC", eid)
    out = []
    for r in rows:
        d = _row(r)
        d["grants"] = await grants(pool, d["id"])
        d["calls"] = [dict(c) for c in await pool.fetch(
            "SELECT tool, server, op, outcome, at FROM agent_calls WHERE connection_id=$1 "
            "ORDER BY at DESC LIMIT 20", d["id"])]
        out.append(d)
    return out


# ── asks ────────────────────────────────────────────────────────────────────

GRANT_ASK, MACHINE_ASK = "grant", "machine"


async def file_ask(pool, connection_id: int, kind: str, server: str, op: str, args,
                   note: str = "", waiting: Optional[dict] = None) -> str:
    """An ask for the person, or the one already open for the same act (so an
    agent calling again gets the same link)."""
    args = [str(a) for a in (args or [])]
    digest = args_digest(args)
    note = _clean_name(note)[:NOTE_MAX] if note else ""
    async with pool.acquire() as conn:
        async with conn.transaction():
            have = await conn.fetchval(
                "SELECT id FROM agent_asks WHERE connection_id=$1 AND kind=$2 AND server=$3 "
                "AND op=$4 AND args_digest=$5 AND state='waiting' AND expires_at > now() "
                "ORDER BY created_at DESC LIMIT 1", connection_id, kind, server, op, digest)
            if have:
                if waiting is not None:
                    await conn.execute("UPDATE agent_asks SET waiting=$2::jsonb WHERE id=$1",
                                       have, json.dumps(waiting))
                return have
            ask_id = secrets.token_urlsafe(18)
            await conn.execute(
                "INSERT INTO agent_asks (id, connection_id, kind, server, op, args, args_digest, "
                "note, waiting, expires_at) VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7, $8, $9::jsonb, "
                "now() + make_interval(secs => $10))",
                ask_id, connection_id, kind, server, op, json.dumps(args), digest, note,
                json.dumps(waiting or {}), ASK_TTL_S)
            return ask_id


async def take_once(pool, connection_id: int, server: str, op: str, args) -> Optional[str]:
    """Take a 'just this once' approval for exactly these arguments: its id,
    marked spent in one statement so two calls cannot both use it, or None.
    A machine that then holds the op for its own approval gives it back
    (give_back_once) for the agent's next call."""
    digest = args_digest([str(a) for a in (args or [])])
    return await pool.fetchval(
        "UPDATE agent_asks SET state='spent' WHERE id = ("
        " SELECT id FROM agent_asks WHERE connection_id=$1 AND kind='grant' AND server=$2 "
        " AND op=$3 AND args_digest=$4 AND state='once' AND expires_at > now() "
        " ORDER BY decided_at LIMIT 1 FOR UPDATE SKIP LOCKED) RETURNING id",
        connection_id, server, op, digest)


async def give_back_once(pool, ask_id: str) -> None:
    await pool.execute("UPDATE agent_asks SET state='once' WHERE id=$1 AND state='spent'", ask_id)


async def refused_recently(pool, connection_id: int, server: str, op: str, args) -> bool:
    digest = args_digest([str(a) for a in (args or [])])
    return bool(await pool.fetchval(
        "SELECT 1 FROM agent_asks WHERE connection_id=$1 AND server=$2 AND op=$3 "
        "AND args_digest=$4 AND state='refused' AND expires_at > now() LIMIT 1",
        connection_id, server, op, digest))


async def get_ask(pool, eid: int, ask_id: str) -> Optional[dict]:
    if not isinstance(ask_id, str) or not re.fullmatch(r"[A-Za-z0-9_\-]{16,64}", ask_id):
        return None
    return _row(await pool.fetchrow(
        "SELECT k.*, a.client_name, a.revoked_at AS connection_revoked_at FROM agent_asks k "
        "JOIN agent_connections a ON a.id = k.connection_id "
        "WHERE k.id=$1 AND a.entity_id=$2", ask_id, eid))


ONCE, ALWAYS, REFUSE = "once", "always", "refused"


async def decide_ask(pool, eid: int, ask_id: str, choice: str) -> dict:
    """The person's answer to a grant ask. Called only after the person was
    confirmed (asks.py)."""
    if choice not in (ONCE, ALWAYS, REFUSE):
        raise AgentAccessError("invalid_request", "the answer must be once, always or refused")
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                "SELECT k.id, k.connection_id, k.kind, k.server, k.op FROM agent_asks k "
                "JOIN agent_connections a ON a.id = k.connection_id "
                "WHERE k.id=$1 AND a.entity_id=$2 AND a.revoked_at IS NULL "
                "AND k.state='waiting' AND k.expires_at > now() FOR UPDATE OF k", ask_id, eid)
            if row is None:
                raise AgentAccessError("invalid_request",
                                       "this request was already answered, has expired, or its "
                                       "agent was disconnected", status=409)
            if row["kind"] != GRANT_ASK:
                raise AgentAccessError("invalid_request",
                                       "the machine itself takes this approval", status=409)
            if choice == ALWAYS and (ANY in row["server"] or ANY in row["op"]):
                # "*" is every machine or every op to holds(): a permission is
                # only ever for the one machine and op the page named.
                raise AgentAccessError("invalid_request",
                                       "this request does not name one machine and one op", status=409)
            await conn.execute("UPDATE agent_asks SET state=$2, decided_at = now() WHERE id=$1",
                               ask_id, choice)
            if choice == ALWAYS:
                await conn.execute(
                    "INSERT INTO agent_grants (connection_id, kind, server, op) "
                    "VALUES ($1, 'op', $2, $3) ON CONFLICT DO NOTHING",
                    row["connection_id"], row["server"], row["op"])
    return {"ask": ask_id, "state": choice}


async def record_call(pool, connection_id: int, tool: str, server: str, op: str,
                      outcome: str) -> None:
    try:
        await pool.execute(
            "INSERT INTO agent_calls (connection_id, tool, server, op, outcome) "
            "VALUES ($1, $2, $3, $4, $5)", connection_id, tool, (server or "")[:255],
            (op or "")[:255], (outcome or "")[:255])
    except Exception:  # noqa: BLE001 — the record never decides the call
        import logging
        logging.getLogger("cseries.agent_access").exception("agent call not recorded")


def json_arg(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), sort_keys=True)
