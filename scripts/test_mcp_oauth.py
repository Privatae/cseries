#!/usr/bin/env python3
"""Gate: signing in to an MCP server with OAuth (server/mcp_oauth.py,
migrations/v239, the MCP authorization spec revision 2026-07-28).

Fake servers over TLS on 127.0.0.1, reachable ONLY through a stand-in for the
kernel's egress gate (a CONNECT tunnel that maps the names below to local
ports, as scripts/test_mcp_client.py does):

  mcp-files.example.com    an MCP server that wants a bearer token from its
                           authorization server: 401 with a challenge naming
                           its resource metadata and scope files:read; a
                           write_file tool that answers 403 insufficient_scope
                           without files:write.
  auth.example.com         its authorization server, issuer
                           https://auth.example.com/tenant1 (a path, so the
                           path-inserted metadata URL is the first tried):
                           S256, iss in responses, Dynamic Client Registration,
                           rotating refresh tokens.
  mcp-nopkce / auth-nopkce   an authorization server with no S256.
  mcp-liar / auth-liar       metadata whose issuer is another one.
  mcp-wrongres               resource metadata for another resource.
  mcp-noreg / auth-noreg     no registration endpoint: a client the person
                             enters, authenticated with client_secret_basic.
  mcp-cimd / auth-cimd       Client ID Metadata Documents only;
  client.example.com         serves a document the person configured;
  the published document's   serves .github/pages/oauth/client-metadata.json,
  own host                   the document C-Series publishes, as it is.

  U  the pieces: WWW-Authenticate parsing, the canonical URI, the well-known
     URL orders, the redirect URI for each kind of home address; which
     document a Host signs in with, and the published one against the code.
  M  migrations/v239 on the full current schema, run twice.
  D  a keyless connection to a server that wants a sign-in: the approval's
     tool fetch finds the authorization server (the challenge's metadata URL,
     then the path-inserted metadata URL), keeps it, and says the person signs
     in; the Network app's card and op='servers' show it; every request went
     through the gate and none followed a redirect.
  S  the sign-in: Dynamic Client Registration as a native client with the
     loopback redirect; the authorization URL carries S256, state, resource
     and the scope (offline_access added); the callback, through the real
     middleware with no session and a non-loopback peer, trades the code with
     the PKCE verifier, keeps the tokens only in the vault and fetches the
     tools; a second use of the state, a wrong or missing iss, an expired
     sign-in and an error from the wrong issuer are refused before the token
     endpoint is called.
  C  calls carry the token through the vault broker; the reserved names are
     refused to keys_manage, to connect, and to any broker action but the MCP
     client's; refresh before expiry, rotation, one refresh after a 401, one
     refresh for two calls at once, a refused refresh ends the sign-in.
  P  step-up: a 403 insufficient_scope keeps the token and the other tools,
     and the next sign-in asks for the union; a server no sign-in satisfies is
     named as such after three sign-ins that asked for its scope.
  A  the server moves to another authorization server (auth-moved): the next
     sign-in reads its resource metadata again and registers there.
  R  refusals: no S256, a lying issuer, metadata for another resource, a key
     the server refuses.
  E  registration without DCR: a client the person enters (client_secret_basic),
     a Client ID Metadata Document the person names, the published one when
     none is named, and none when the setting is empty; a document that
     cannot be read is not waited on again at the next sign-in.
  X  sign-out, disconnect (rows and vault entries gone) and the sweep after a
     rule is removed in the Network app.

Fresh clone of the kernel's schema (scripts/kernel_schema.py) on
TEST_PG_DSN / PEER_TEST_DSN / TEST_DSN, else a throwaway cseries-postgres
through docker.

  python3 scripts/test_mcp_oauth.py
"""
from __future__ import annotations

import base64
import contextlib
import datetime
import hashlib
import json
import os
import pathlib
import secrets
import socket
import ssl
import subprocess
import sys
import tempfile
import time
from urllib.parse import parse_qs, urlencode, urlsplit

ROOT = pathlib.Path(os.environ.get("APP_ROOT") or pathlib.Path(__file__).resolve().parents[1])
sys.path[:0] = [str(ROOT), str(ROOT / "brain"), str(ROOT / "scripts")]

# 2026-10-18 (server/mcp_oauth.py, migrations/v239): signing in to an MCP
# server with OAuth, per the MCP authorization spec revision 2026-07-28.
EID = 1
MIGRATION = ROOT / "migrations" / "v239_mcp_oauth.sql"
MCP = "mcp-files.example.com"
AS = "auth.example.com"
ISSUER = f"https://{AS}/tenant1"
RESOURCE = f"https://{MCP}/mcp"
CALLBACK = "http://127.0.0.1:8800/api/mcp/oauth/callback"
# The Client ID Metadata Document C-Series publishes (.github/workflows/pages.yml),
# served here at its own address, from the file, so section E signs in with it.
PAGES = ROOT / ".github" / "pages"
PUBLISHED_FILE = PAGES / "oauth" / "client-metadata.json"
PUBLISHED = json.loads(PUBLISHED_FILE.read_text())
PUBLISHED_URL = urlsplit(PUBLISHED["client_id"])
# Section E6: requests that reached it, and whether it answers 404 or stalls.
PUBLISHED_SITE = {"hits": 0, "down": False, "stall": 0.0}
HOSTS = [MCP, AS, "mcp-nopkce.example.com", "auth-nopkce.example.com", "mcp-liar.example.com",
         "auth-liar.example.com", "mcp-wrongres.example.com", "mcp-noreg.example.com",
         "auth-noreg.example.com", "mcp-cimd.example.com", "auth-cimd.example.com",
         "client.example.com", "auth-moved.example.com", PUBLISHED_URL.hostname]
CIMD_URL = "https://client.example.com/oauth/cseries.json"
NOREG_CLIENT, NOREG_SECRET = "person-made-client", "s3cr3t:with/odd chars"


def _make_tls(tmp: pathlib.Path) -> tuple[str, str, str]:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
    now = datetime.datetime.now(datetime.timezone.utc)
    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "mcp oauth gate test CA")])
    ca = (x509.CertificateBuilder().subject_name(ca_name).issuer_name(ca_name)
          .public_key(ca_key.public_key()).serial_number(x509.random_serial_number())
          .not_valid_before(now - datetime.timedelta(minutes=5))
          .not_valid_after(now + datetime.timedelta(days=1))
          .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
          .add_extension(x509.KeyUsage(digital_signature=True, content_commitment=False,
                                       key_encipherment=False, data_encipherment=False,
                                       key_agreement=False, key_cert_sign=True, crl_sign=True,
                                       encipher_only=False, decipher_only=False), critical=True)
          .add_extension(x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()), critical=False)
          .sign(ca_key, hashes.SHA256()))
    key = ec.generate_private_key(ec.SECP256R1())
    cert = (x509.CertificateBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, MCP)]))
            .issuer_name(ca_name).public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(minutes=5))
            .not_valid_after(now + datetime.timedelta(days=1))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName(h) for h in HOSTS]), critical=False)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
            .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()),
                           critical=False)
            .sign(ca_key, hashes.SHA256()))
    ca_pem, cert_pem, key_pem = tmp / "ca.pem", tmp / "server.pem", tmp / "server.key"
    ca_pem.write_bytes(ca.public_bytes(serialization.Encoding.PEM))
    cert_pem.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_pem.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                          serialization.NoEncryption()))
    return str(ca_pem), str(cert_pem), str(key_pem)


_TMP = pathlib.Path(tempfile.mkdtemp(prefix="mcp-oauth-gate-"))
CA_PEM, CERT_PEM, KEY_PEM = _make_tls(_TMP)
# Set BEFORE aiohttp is imported: it builds its default verifying context then.
os.environ["SSL_CERT_FILE"] = CA_PEM
os.environ.pop("SSL_CERT_DIR", None)
os.environ.setdefault("GITEA_URL", "https://forge.example")
os.environ.pop("CSERIES_URL", None)        # the default home address: the loopback redirect

import asyncio  # noqa: E402

import aiohttp  # noqa: E402
from aiohttp import web  # noqa: E402
from gate_loopback import await_peer_close, client_closes_first_aiohttp  # noqa: E402

FAILED = 0


def check(label, cond, detail=""):
    global FAILED
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  <- {str(detail)[:1500]}"))
    if not cond:
        FAILED += 1


# ── the stand-in egress gate ────────────────────────────────────────────────
GATE_LOG: list = []
ROUTES: dict[str, int] = {}
SEEN: list = []


async def _pipe(r, w):
    try:
        while True:
            b = await r.read(65536)
            if not b:
                break
            w.write(b)
            await w.drain()
    except Exception:  # noqa: BLE001
        pass
    finally:
        with contextlib.suppress(Exception):
            w.close()


async def _gate(reader, writer):
    try:
        head = await reader.readuntil(b"\r\n\r\n")
    except Exception:  # noqa: BLE001
        writer.close()
        return
    parts = head.split(b"\r\n", 1)[0].decode("latin-1").split()
    if len(parts) < 2 or parts[0] != "CONNECT":
        GATE_LOG.append(("not-connect", " ".join(parts)))
        writer.write(b"HTTP/1.1 405 Method Not Allowed\r\nContent-Length: 0\r\n\r\n")
        await writer.drain()
        await await_peer_close(reader)
        writer.close()
        return
    host, _, port = parts[1].rpartition(":")
    GATE_LOG.append((host, int(port)))
    if host not in ROUTES:
        writer.write(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n")
        await writer.drain()
        await await_peer_close(reader)
        writer.close()
        return
    up_r, up_w = await asyncio.open_connection("127.0.0.1", ROUTES[host])
    writer.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
    await writer.drain()
    await asyncio.gather(_pipe(reader, up_w), _pipe(up_r, writer))


def _spy_aiohttp():
    orig = aiohttp.ClientSession._request

    async def spy(self, method, str_or_url, **kw):
        SEEN.append({"trust_env": self._trust_env, "proxy": kw.get("proxy"), "url": str(str_or_url),
                     "redirects": kw.get("allow_redirects", True)})
        return await orig(self, method, str_or_url, **kw)
    aiohttp.ClientSession._request = spy


# ── the fake authorization servers ──────────────────────────────────────────
def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


class FakeAS:
    """One authorization server: metadata, registration, authorize, token."""

    def __init__(self, host: str, path: str = "", *, pkce=True, iss_param=True, register=True,
                 cimd=False, auth_methods=("none",), liar=False):
        self.host, self.path = host, path
        self.issuer = f"https://{host}{path}"
        self.pkce, self.iss_param, self.register, self.cimd = pkce, iss_param, register, cimd
        self.auth_methods, self.liar = list(auth_methods), liar
        self.clients: dict[str, dict] = {}
        self.codes: dict[str, dict] = {}
        self.access: dict[str, dict] = {}       # token -> {scope, resource, revoked}
        self.refresh: dict[str, dict] = {}      # token -> {client, scope, resource, used}
        self.reqs: list = []
        self.refuse_refresh = False
        self.refresh_error: str | None = None   # another 400 error for a refresh
        self.expires_in = 3600
        self.n = 0

    def meta(self) -> dict:
        m = {"issuer": "https://honest.example.com" if self.liar else self.issuer,
             "authorization_endpoint": f"{self.issuer}/authorize",
             "token_endpoint": f"{self.issuer}/token",
             "response_types_supported": ["code"],
             "grant_types_supported": ["authorization_code", "refresh_token"],
             "scopes_supported": ["files:read", "files:write", "offline_access"],
             "token_endpoint_auth_methods_supported": self.auth_methods,
             "authorization_response_iss_parameter_supported": self.iss_param}
        if self.pkce:
            m["code_challenge_methods_supported"] = ["S256"]
        if self.register:
            m["registration_endpoint"] = f"{self.issuer}/register"
        if self.cimd:
            m["client_id_metadata_document_supported"] = True
        return m

    def authorize(self, url: str, *, iss: object = "same") -> dict:
        """What the person's browser does: open the URL, sign in, approve.
        Returns the callback's query parameters."""
        q = {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}
        self.reqs.append(("authorize", q))
        client = self.clients.get(q.get("client_id")) or {}
        assert q.get("response_type") == "code", q
        assert q.get("code_challenge_method") == "S256", q
        assert q.get("redirect_uri") in client.get("redirect_uris", []), (q, client)
        code = _b64url(secrets.token_bytes(16))
        self.codes[code] = {"challenge": q["code_challenge"], "client": q["client_id"],
                            "redirect_uri": q["redirect_uri"], "resource": q.get("resource"),
                            "scope": q.get("scope")}
        out = {"code": code, "state": q["state"]}
        if iss == "same":
            out["iss"] = self.issuer
        elif iss is not None:
            out["iss"] = iss
        return out

    def _issue(self, client: str, scope, resource) -> dict:
        self.n += 1
        at, rt = f"at-{self.host}-{self.n}", f"rt-{self.host}-{self.n}"
        self.access[at] = {"scope": set((scope or "").split()), "resource": resource, "revoked": False,
                           "client": client}
        self.refresh[rt] = {"client": client, "scope": scope, "resource": resource, "used": False}
        return {"access_token": at, "token_type": "Bearer", "expires_in": self.expires_in,
                "refresh_token": rt, "scope": scope}

    async def handle(self, request: web.Request):
        p = request.path
        if p == f"/.well-known/oauth-authorization-server{self.path}":
            self.reqs.append(("meta", p))
            return web.json_response(self.meta())
        if p.startswith("/.well-known/"):
            self.reqs.append(("meta-miss", p))
            return web.Response(status=404)
        if p == f"{self.path}/register" and self.register:
            body = await request.json()
            self.reqs.append(("register", body))
            cid = f"dcr-{len(self.clients) + 1}"
            self.clients[cid] = {"redirect_uris": body.get("redirect_uris") or [], "secret": None}
            return web.json_response({"client_id": cid, "token_endpoint_auth_method": "none",
                                      "redirect_uris": body.get("redirect_uris")}, status=201)
        if p == f"{self.path}/token":
            form = dict(await request.post())
            auth = request.headers.get("Authorization")
            self.reqs.append(("token", form, auth))
            client = form.get("client_id")
            if auth and auth.startswith("Basic "):
                from urllib.parse import unquote
                cid, _, sec = base64.b64decode(auth[6:]).decode().partition(":")
                cid, sec = unquote(cid), unquote(sec)
                if self.clients.get(cid, {}).get("secret") != sec:
                    return web.json_response({"error": "invalid_client"}, status=401)
                client = cid
            elif self.clients.get(client, {}).get("secret"):
                return web.json_response({"error": "invalid_client"}, status=401)
            if client not in self.clients:
                return web.json_response({"error": "invalid_client"}, status=401)
            if form.get("grant_type") == "authorization_code":
                c = self.codes.pop(form.get("code", ""), None)
                if not c or c["client"] != client or c["redirect_uri"] != form.get("redirect_uri"):
                    return web.json_response({"error": "invalid_grant"}, status=400)
                want = _b64url(hashlib.sha256(form.get("code_verifier", "").encode()).digest())
                if want != c["challenge"] or form.get("resource") != c["resource"]:
                    return web.json_response({"error": "invalid_grant"}, status=400)
                return web.json_response(self._issue(client, c["scope"], c["resource"]))
            if form.get("grant_type") == "refresh_token":
                if self.refresh_error:
                    return web.json_response({"error": self.refresh_error}, status=400)
                r = self.refresh.get(form.get("refresh_token", ""))
                if self.refuse_refresh or not r or r["used"] or r["client"] != client:
                    return web.json_response({"error": "invalid_grant"}, status=400)
                r["used"] = True                     # rotation: an old refresh token is spent
                return web.json_response(self._issue(client, r["scope"], form.get("resource")))
            return web.json_response({"error": "unsupported_grant_type"}, status=400)
        return web.Response(status=404)


AUTH = FakeAS(AS, "/tenant1")
AUTH_NOPKCE = FakeAS("auth-nopkce.example.com", pkce=False)
AUTH_LIAR = FakeAS("auth-liar.example.com", liar=True)
AUTH_NOREG = FakeAS("auth-noreg.example.com", register=False, auth_methods=("client_secret_basic",))
AUTH_NOREG.clients[NOREG_CLIENT] = {"redirect_uris": [CALLBACK], "secret": NOREG_SECRET}
AUTH_CIMD = FakeAS("auth-cimd.example.com", register=False, cimd=True)
AUTH_CIMD.clients[CIMD_URL] = {"redirect_uris": [CALLBACK], "secret": None}
AUTH_CIMD.clients[PUBLISHED["client_id"]] = {"redirect_uris": PUBLISHED["redirect_uris"], "secret": None}
# Where mcp-files moves its sign-in to in section A.
AUTH_MOVED = FakeAS("auth-moved.example.com")
AS_BY_HOST = {a.host: a for a in (AUTH, AUTH_NOPKCE, AUTH_LIAR, AUTH_NOREG, AUTH_CIMD, AUTH_MOVED)}


# ── the fake MCP servers ────────────────────────────────────────────────────
MCP_REQS: list = []
TOOLS = [
    {"name": "read_file", "description": "Read a file.",
     "inputSchema": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}},
    {"name": "write_file", "description": "Write a file.",
     "inputSchema": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}},
]
MCP_AS = {MCP: AUTH, "mcp-nopkce.example.com": AUTH_NOPKCE, "mcp-liar.example.com": AUTH_LIAR,
          "mcp-wrongres.example.com": AUTH, "mcp-noreg.example.com": AUTH_NOREG,
          "mcp-cimd.example.com": AUTH_CIMD}
# Section P: write_file refuses with insufficient_scope even to a token that
# carries files:write (an account that is not allowed to write).
STUBBORN = [False]


def _challenge(host: str, scope: str, error: str = "") -> str:
    v = f'Bearer resource_metadata="https://{host}/.well-known/oauth-protected-resource/mcp"'
    if error:
        v += f', error="{error}"'
    return v + f', scope="{scope}"'


async def _mcp(request: web.Request):
    host = request.host.split(":")[0]
    a = MCP_AS[host]
    if request.path == "/.well-known/oauth-protected-resource/mcp":
        MCP_REQS.append((host, "prm", None))
        res = "https://other.example.com/mcp" if host == "mcp-wrongres.example.com" else f"https://{host}/mcp"
        return web.json_response({"resource": res, "authorization_servers": [a.issuer],
                                  "scopes_supported": ["files:read"]})
    if request.path.startswith("/.well-known/"):
        return web.Response(status=302, headers={"Location": "https://elsewhere.example.com/x"})
    body = await request.json()
    auth = request.headers.get("Authorization") or ""
    MCP_REQS.append((host, body.get("method"), auth))
    tok = a.access.get(auth[7:]) if auth.startswith("Bearer ") else None
    if not tok or tok["revoked"] or tok["resource"] != f"https://{host}/mcp":
        return web.Response(status=401, headers={"WWW-Authenticate": _challenge(host, "files:read")})
    rid, method, params = body.get("id"), body.get("method"), body.get("params") or {}
    if method == "tools/list":
        return web.json_response({"jsonrpc": "2.0", "id": rid, "result": {"resultType": "complete",
                                                                         "tools": TOOLS}})
    if method == "tools/call":
        name = params.get("name")
        if name == "write_file" and ("files:write" not in tok["scope"] or STUBBORN[0]):
            return web.Response(status=403, headers={
                "WWW-Authenticate": _challenge(host, "files:write", "insufficient_scope")})
        return web.json_response({"jsonrpc": "2.0", "id": rid, "result": {
            "resultType": "complete",
            "content": [{"type": "text", "text": f"{name}:{(params.get('arguments') or {}).get('path')}"}]}})
    return web.json_response({"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": "no"}},
                             status=404)


async def _client_doc(request: web.Request):
    if request.path == "/oauth/cseries.json":
        return web.json_response({"client_id": CIMD_URL, "client_name": "C-Series",
                                  "redirect_uris": [CALLBACK], "grant_types": ["authorization_code"],
                                  "response_types": ["code"], "token_endpoint_auth_method": "none"})
    return web.Response(status=404)


async def _dispatch(request: web.Request):
    host = request.host.split(":")[0]
    if host in AS_BY_HOST:
        return await AS_BY_HOST[host].handle(request)
    if host == "client.example.com":
        return await _client_doc(request)
    if host == PUBLISHED_URL.hostname:
        PUBLISHED_SITE["hits"] += 1
        if PUBLISHED_SITE["stall"]:
            await asyncio.sleep(PUBLISHED_SITE["stall"])
        if request.path == PUBLISHED_URL.path and not PUBLISHED_SITE["down"]:
            return web.Response(body=PUBLISHED_FILE.read_bytes(), content_type="application/json")
        return web.Response(status=404)
    return await _mcp(request)


async def _serve() -> tuple[web.AppRunner, int]:
    app = web.Application()
    app.router.add_route("*", "/{tail:.*}", _dispatch)
    client_closes_first_aiohttp(app)
    runner = web.AppRunner(app)
    await runner.setup()
    ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    ctx.load_cert_chain(CERT_PEM, KEY_PEM)
    site = web.TCPSite(runner, "127.0.0.1", 0, ssl_context=ctx)
    await site.start()
    return runner, site._server.sockets[0].getsockname()[1]


def _reserve() -> socket.socket:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    s.listen(16)
    return s


def _skill(server, tools):
    allowed = ", ".join(["operate"] + [f"mcp:{server}/{t}" for t in tools])
    return f"""---
name: mcp-{server}
description: Use the {server} test server's tools and check what they return.
allowed-tools: {allowed}
sources: mcp:{server}
mcp-server: {server}
---

## When to use

The person asks for something the {server} server does.

## Steps

1. Call `operate` with the server's tool and its arguments.

## Checks

The result names what was asked.

## When it fails

Say what the server answered.
"""


_LAST_COUNTER = [-1]
# Set once section D has approved through a real step-up code (see there).
CODES_FREE = [False]


async def _next_code(secret: str) -> str:
    """A step-up code the verifier takes: one 30-second counter per approval."""
    from server.network import nac_stepup
    if CODES_FREE[0]:
        return "000000"
    while True:
        now = int(time.time() // 30)
        c = max(_LAST_COUNTER[0] + 1, now)
        if c <= now + 1:
            _LAST_COUNTER[0] = c
            return nac_stepup.current_code(secret, at=c * 30 + 1)
        await asyncio.sleep(1)


async def _connect(pool, secret, name, host):
    """Connect `host` with no key and approve it as the Network app does."""
    from server import mcp_connections as MC
    from server.network import nac_commit
    out = await MC.connect(pool, EID, name=name, url=f"https://{host}/mcp")
    await nac_commit.approve_egress(pool, EID, out["rule_id"], stepup_code=await _next_code(secret),
                                    via="ui", approved_by="person@example.com")
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        row = await MC.live_row(pool, EID, name)
        if row.get("tools_fetched_at") is not None or row.get("tools_error"):
            return row
        await asyncio.sleep(0.05)
    return await MC.live_row(pool, EID, name)


async def _vault_names(pool):
    from server.vault import api as vault_api
    return {r["logical_name"]: r["cred_type"] for r in await vault_api.list_secrets(pool, entity_id=EID)}


def _tokens(as_: FakeAS, kind: str) -> list:
    return [r for r in as_.reqs if r[0] == "token" and r[1].get("grant_type") == kind]


def _query(url: str) -> dict:
    return {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}


# ── U ───────────────────────────────────────────────────────────────────────
def unit_checks():
    from server import home_origin
    from server import mcp_oauth as O
    print("U. the pieces")
    ch = O.parse_challenges(['Basic realm="x", Bearer error="invalid_token", '
                             'resource_metadata="https://a.example/.well-known/oauth-protected-resource", '
                             'scope="files:read  files:write"',
                             'Newauth realm="apps", type=1, title="Login to \\"apps\\""'])
    check("U1 several challenges in one header and across headers, quoted and token values",
          [c[0] for c in ch] == ["basic", "bearer", "newauth"]
          and ch[1][1]["resource_metadata"].endswith("/oauth-protected-resource")
          and ch[1][1]["scope"] == "files:read  files:write" and ch[2][1]["title"] == 'Login to "apps"'
          and ch[2][1]["type"] == "1", ch)
    check("U1 a token68 credential reads as a scheme, and Bearer is still found after it",
          O.bearer_challenge(["Basic dXNlcjpwYXNz==, Bearer scope=x"]) == {"scope": "x"},
          O.parse_challenges(["Basic dXNlcjpwYXNz==, Bearer scope=x"]))
    check("U1 no Bearer challenge is None", O.bearer_challenge(['Basic realm="x"']) is None)
    check("U2 the canonical URI: lowercase host, no default port, no query, no slash on an origin",
          O.canonical_resource("https://MCP.Example.com:443/") == "https://mcp.example.com"
          and O.canonical_resource("https://mcp.example.com:8443/mcp?x=1") == "https://mcp.example.com:8443/mcp")
    check("U3 resource metadata: under the endpoint's path, then the root",
          O.prm_urls("https://e.example/public/mcp") == [
              "https://e.example/.well-known/oauth-protected-resource/public/mcp",
              "https://e.example/.well-known/oauth-protected-resource"]
          and O.prm_urls("https://e.example/") == ["https://e.example/.well-known/oauth-protected-resource"])
    check("U3 issuer with a path: RFC 8414 inserted, OIDC inserted, OIDC appended (the spec's order)",
          O.as_metadata_urls("https://auth.example.com/tenant1") == [
              "https://auth.example.com/.well-known/oauth-authorization-server/tenant1",
              "https://auth.example.com/.well-known/openid-configuration/tenant1",
              "https://auth.example.com/tenant1/.well-known/openid-configuration"])
    check("U3 issuer without a path: RFC 8414, then OIDC",
          O.as_metadata_urls("https://auth.example.com/") == [
              "https://auth.example.com/.well-known/oauth-authorization-server",
              "https://auth.example.com/.well-known/openid-configuration"])
    check("U4 the default home address signs in through the loopback address",
          O.redirect_uri() == CALLBACK, O.redirect_uri())
    saved = home_origin.HOME
    try:
        home_origin.HOME = home_origin.parse("https://home.cseries.lan")
        check("U4 an https home address is its own redirect",
              O.redirect_uri() == "https://home.cseries.lan/api/mcp/oauth/callback", O.redirect_uri())
        home_origin.HOME = home_origin.parse("http://home.cseries.lan:8800")
        try:
            O.redirect_uri()
            check("U4 http on a name that is not this machine is refused", False, "allowed")
        except O.OAuthError as e:
            check("U4 http on a name that is not this machine is refused, saying why",
                  "https" in str(e) and "this machine" in str(e), str(e))
    finally:
        home_origin.HOME = saved
    check("U5 the reserved names", O.is_reserved_key_name("mcp-oauth:x:1")
          and O.is_reserved_key_name(" MCP-OAUTH-refresh:x:1") and not O.is_reserved_key_name("github-token"))
    saved_env = os.environ.pop(O.CIMD_URL_ENV, None)
    # A Client Identifier URL must not hold a "." or ".." path segment (the
    # draft's section 3), written plainly or percent-encoded; a dot inside a
    # segment is an ordinary character.
    dotted = ("https://client.example.com/oauth/../cseries.json", "https://client.example.com/./cseries.json",
              "https://client.example.com/oauth/%2E%2e/cseries.json", "https://client.example.com/oauth/.")
    plain_dots = "https://client.example.com/v1.2/cseries..json"
    try:
        unset = O.cimd_url()
        seen = {}
        for v in ("", CIMD_URL, "http://client.example.com/oauth/cseries.json", "https://client.example.com/"):
            os.environ[O.CIMD_URL_ENV] = v
            seen[v] = O.cimd_url()
        seen_dots = {}
        for v in (*dotted, plain_dots):
            os.environ[O.CIMD_URL_ENV] = v
            seen_dots[v] = O.cimd_url()
    finally:
        os.environ.pop(O.CIMD_URL_ENV, None)
        if saved_env is not None:
            os.environ[O.CIMD_URL_ENV] = saved_env
    check("U6 the document a Host signs in with: C-Series's own when none is named, the named one, "
          "none when the setting is empty or names no https URL with a path",
          unset == O.CIMD_URL_DEFAULT and seen == {"": None, CIMD_URL: CIMD_URL,
                                                  "http://client.example.com/oauth/cseries.json": None,
                                                  "https://client.example.com/": None}, (unset, seen))
    check("U6 ...none when the named URL has a dot segment, which no authorization server accepts as a "
          "client ID; a dot inside a segment is fine",
          seen_dots == {**{v: None for v in dotted}, plain_dots: plain_dots}, seen_dots)
    rel = PUBLISHED_FILE.relative_to(PAGES).as_posix()
    check("U7 the published document is the code's default, at the address its place in .github/pages gives",
          PUBLISHED["client_id"] == O.CIMD_URL_DEFAULT and PUBLISHED_URL.hostname.endswith(".github.io")
          and PUBLISHED_URL.path.count("/") >= 2 and PUBLISHED_URL.path.split("/", 2)[2] == rel,
          (PUBLISHED["client_id"], rel))
    check("U7 ...it lists the default home address's redirect, and only loopback or https ones",
          home_origin.HOME == home_origin.parse(home_origin.DEFAULT_URL)
          and O.redirect_uri() in PUBLISHED["redirect_uris"]
          and all(u.startswith(("http://127.0.0.1:", "https://")) for u in PUBLISHED["redirect_uris"]),
          PUBLISHED["redirect_uris"])
    check("U7 ...a public native client: no secret, the grants this Host uses, its own name",
          PUBLISHED.get("token_endpoint_auth_method") == "none"
          and not {"client_secret", "client_secret_expires_at", "jwks"} & set(PUBLISHED)
          and set(PUBLISHED.get("grant_types") or []) == {"authorization_code", "refresh_token"}
          and PUBLISHED.get("response_types") == ["code"] and PUBLISHED.get("application_type") == "native"
          and PUBLISHED.get("client_name") == O.CLIENT_NAME, PUBLISHED)
    workflow = (ROOT / ".github" / "workflows" / "pages.yml").read_text()
    check("U7 ...and the pages workflow publishes that directory, from the default branch",
          f"path: {PAGES.relative_to(ROOT).as_posix()}\n" in workflow
          and "if: github.ref_name == github.event.repository.default_branch\n" in workflow
          and f'open("{PUBLISHED_FILE.relative_to(ROOT).as_posix()}")' in workflow)
    # Every branch's push starts a run. GitHub cancels a run waiting in a
    # concurrency group when another joins it, so a shared group would let a
    # push to another branch (whose job is skipped) cancel the default branch's
    # waiting deploy, and the newest document would never be published.
    check("U7 ...each branch waits in its own concurrency group",
          "group: pages-${{ github.ref }}\n" in workflow and "group: pages\n" not in workflow)


# ── the rest, on a real kernel schema ───────────────────────────────────────
async def service_checks(dsn: str):
    import asyncpg
    import httpx

    from mastercode.runtime import host_skills
    from mastercode.runtime import tools as T
    from server import brain_service
    from server import mcp_connections as MC
    from server import mcp_oauth as O
    from server.network import nac_commit, nac_stepup
    from server.vault import api as vault_api
    from server.vault import authz

    pool = await asyncpg.create_pool(dsn, min_size=2, max_size=8)
    brain_service._pool = pool
    brain_service._entity_id = EID
    try:
        for dom in ("core.entity", "external.operator"):
            await pool.execute("INSERT INTO domain_registry (id, name, domain_group) VALUES ($1, $1, 'core') "
                               "ON CONFLICT DO NOTHING", dom)
        secret = await nac_stepup.ensure_stepup_secret(pool)
        await nac_stepup.mark_enrolled(pool)
        MC.FETCH_AFTER_APPROVAL_S = 0.05

        # ── D ──
        print("D. a server that wants a sign-in")
        GATE_LOG.clear()
        SEEN.clear()
        row = await _connect(pool, secret, "files", MCP)
        oauth = await O.load(pool, row)
        check("D1 the approval's tool fetch met a 401 and found the authorization server",
              oauth is not None and oauth["status"] == "sign_in_needed" and oauth["issuer"] == ISSUER
              and oauth["resource"] == RESOURCE and oauth["challenged_scope"] == "files:read"
              and oauth["token_endpoint"] == f"{ISSUER}/token" and oauth["iss_parameter_supported"],
              oauth)
        check("D1 ...it used the metadata URL the challenge named, and the path-inserted RFC 8414 URL first",
              (MCP, "prm", None) in MCP_REQS
              and [r for r in AUTH.reqs if r[0].startswith("meta")][:1]
              == [("meta", "/.well-known/oauth-authorization-server/tenant1")], (MCP_REQS, AUTH.reqs))
        err = row.get("tools_error") or ""
        check("D1 ...and the fetch error says the person signs in, in the Network app",
              "sign in" in err and "Network app" in err, err)
        out = await T._execute_meta_tool("mcp_connections", {"op": "tools", "name": "files"},
                                         entity_id=EID, pool=pool)
        check("D2 the Host's op='tools' is refused with what the person does, never a request for a token",
              out.get("status") != "ok" and "Sign in" in str(out.get("result"))
              and "password" in str(out.get("result")), out)
        sv = await MC.servers(pool, EID)
        entry = next((s for s in sv["servers"] if s["name"] == "files"), {})
        check("D3 op='servers' shows the sign-in state and where",
              (entry.get("sign_in") or {}).get("state") == "sign_in_needed"
              and entry["sign_in"]["authorization_server"] == AS, entry)
        listed = (await nac_commit.egress_list(pool, EID))["rules"]
        card = next((d for d in listed if (d.get("mcp") or {}).get("name") == "files"), {})
        check("D3 the Network app's card carries the sign-in state",
              ((card.get("mcp") or {}).get("oauth") or {}).get("state") == "sign_in_needed", card)
        hosts = {h for h, _ in GATE_LOG if h != "not-connect"}
        check("D4 every request went through the gate (the server and its authorization server)",
              {MCP, AS} <= hosts, GATE_LOG)
        oauth_reqs = [s for s in SEEN if AS in s["url"] or ".well-known" in s["url"]]
        check("D4 ...with the proxy variables never read and no redirect followed",
              bool(oauth_reqs) and all(s["trust_env"] is False and s["proxy"] and s["redirects"] is False
                                       for s in oauth_reqs), oauth_reqs)

        # ── S ──
        print("S. the sign-in")
        b = await O.begin(pool, EID, row)
        reg = [r for r in AUTH.reqs if r[0] == "register"]
        check("S1 Dynamic Client Registration: a native client, the loopback redirect, no client secret",
              len(reg) == 1 and reg[0][1]["redirect_uris"] == [CALLBACK]
              and reg[0][1]["application_type"] == "native"
              and reg[0][1]["token_endpoint_auth_method"] == "none"
              and "refresh_token" in reg[0][1]["grant_types"], reg)
        q = _query(b["authorize_url"])
        check("S2 the authorization URL: S256, a state, the resource, the redirect, the scope with "
              "offline_access added",
              q.get("code_challenge_method") == "S256" and len(q.get("code_challenge", "")) == 43
              and len(q.get("state", "")) >= 43 and q.get("resource") == RESOURCE
              and q.get("redirect_uri") == CALLBACK and q.get("scope") == "files:read offline_access"
              and q.get("client_id") == "dcr-1" and b["authorization_server"] == AS, q)
        pend = await pool.fetchrow("SELECT * FROM mcp_oauth_pending WHERE connection_id = $1", row["id"])
        check("S2 the pending sign-in keeps the state's hash, never the state",
              pend is not None and pend["state_hash"] == hashlib.sha256(q["state"].encode()).hexdigest()
              and q["state"] not in json.dumps(dict(pend), default=str), dict(pend or {}))

        # The callback, through the real middleware: a peer that is not loopback,
        # Host 127.0.0.1:8800, no session cookie.
        from fastapi import FastAPI

        from server.middleware.host_header import HostHeaderGuard
        from server.middleware.localhost_only import LocalhostOnlyMiddleware
        from server.middleware.owner_session import OwnerSessionGate
        from server.routes import mcp_oauth as RO
        app = FastAPI()
        app.include_router(RO.router)

        @app.get("/api/elsewhere")
        async def _elsewhere():
            return {"reached": True}
        app.add_middleware(LocalhostOnlyMiddleware)
        app.add_middleware(OwnerSessionGate)
        app.add_middleware(HostHeaderGuard)
        params = AUTH.authorize(b["authorize_url"])
        transport = httpx.ASGITransport(app=app, client=("10.0.2.2", 51000))
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8800") as hc:
            ctl = await hc.get("/api/elsewhere", headers={"Accept": "application/json"})
            check("S3 control: another route from that peer with no session is refused",
                  ctl.status_code == 403, (ctl.status_code, ctl.text))
            cb = await hc.get("/api/mcp/oauth/callback?" + urlencode(params),
                              headers={"Accept": "text/html"})
            post = await hc.post("/api/mcp/oauth/callback?" + urlencode(params))
        check("S3 the callback reaches its handler without a session and signs in",
              cb.status_code == 200 and "Signed in to files" in cb.text, (cb.status_code, cb.text[:400]))
        csp = cb.headers.get("content-security-policy", "")
        check("S3 ...its page runs no script, sends no referrer and cannot be framed",
              "<script" not in cb.text and cb.headers.get("referrer-policy") == "no-referrer"
              and "frame-ancestors 'none'" in csp and "script-src" not in csp, dict(cb.headers))
        check("S3 ...and only a GET is let past the session gate", post.status_code in (403, 405),
              post.status_code)
        tok = _tokens(AUTH, "authorization_code")
        check("S4 the code was traded with the PKCE verifier, the resource and the redirect",
              len(tok) == 1 and tok[0][1].get("resource") == RESOURCE and tok[0][1].get("redirect_uri") == CALLBACK
              and len(tok[0][1].get("code_verifier", "")) >= 43 and tok[0][2] is None, tok)
        oauth = await O.load(pool, row)
        names = await _vault_names(pool)
        acc, ref = O.access_secret_name(row), O.refresh_secret_name(row)
        check("S5 signed in: the tokens are in the vault only, under the reserved names",
              oauth["status"] == "active" and oauth["has_refresh"] and oauth["scope"] == "files:read offline_access"
              and names.get(acc) == "mcp_oauth_access" and names.get(ref) == "mcp_oauth_refresh", (oauth, names))
        dump = json.dumps({k: str(v) for k, v in oauth.items()})
        check("S5 ...no table holds a token", "at-" not in dump and "rt-" not in dump, dump)
        raw = json.loads((await vault_api.open_secret(pool, entity_id=EID, name=ref, sign_in=True)).decode())
        check("S5 ...the refresh token is sealed in the vault",
              raw.get("refresh_token", "").startswith("rt-"), list(raw))
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            row = await MC.live_row(pool, EID, "files")
            if row.get("tools") and not row.get("tools_error"):
                break
            await asyncio.sleep(0.05)
        check("S6 the tool list was fetched with the token straight after the sign-in",
              [t["name"] for t in (row.get("tools") or {}).get("kept", [])] == ["read_file", "write_file"],
              (row.get("tools"), row.get("tools_error")))

        r2 = await O.finish(pool, EID, params)
        check("S7 the same answer a second time is refused: a state is used once",
              r2.get("ok") is False and r2.get("error") == "unknown_state", r2)
        n_tok = len(_tokens(AUTH, "authorization_code"))
        b2 = await O.begin(pool, EID, row)
        r3 = await O.finish(pool, EID, AUTH.authorize(b2["authorize_url"], iss="https://evil.example.com/tenant1"))
        check("S8 an answer naming another issuer is refused and its code never reaches a token endpoint",
              r3.get("error") == "issuer_mismatch" and len(_tokens(AUTH, "authorization_code")) == n_tok, r3)
        b3 = await O.begin(pool, EID, row)
        r4 = await O.finish(pool, EID, AUTH.authorize(b3["authorize_url"], iss=None))
        check("S8 ...so is one with no iss from a server that says it sends one",
              r4.get("error") == "issuer_missing" and len(_tokens(AUTH, "authorization_code")) == n_tok, r4)
        b4 = await O.begin(pool, EID, row)
        before = (await O.load(pool, row))["last_error"]
        r5 = await O.finish(pool, EID, {"state": _query(b4["authorize_url"])["state"],
                                        "iss": "https://evil.example.com", "error": "access_denied",
                                        "error_description": "<b>gotcha</b>"})
        check("S8 ...and an error from the wrong issuer is not acted on or shown",
              r5.get("error") == "issuer_mismatch" and "gotcha" not in json.dumps(r5)
              and (await O.load(pool, row))["last_error"] == before, r5)
        b5 = await O.begin(pool, EID, row)
        p5 = AUTH.authorize(b5["authorize_url"])
        await pool.execute("UPDATE mcp_oauth_pending SET expires_at = now() - interval '1 second'")
        r6 = await O.finish(pool, EID, p5)
        check("S9 a sign-in older than ten minutes is refused",
              r6.get("error") == "unknown_state" and len(_tokens(AUTH, "authorization_code")) == n_tok, r6)
        check("S9 ...and begin keeps one registration per issuer and redirect (no second DCR)",
              len([r for r in AUTH.reqs if r[0] == "register"]) == 1, AUTH.reqs)

        # ── C ──
        print("C. calls, the reserved names, refresh")
        w = await host_skills.write(pool, EID, "mcp-files", _skill("files", ["read_file", "write_file"]),
                                    author="host")
        check("C0 the server's skill is written", w.get("status") != "refused", w)
        MCP_REQS.clear()
        out = await MC.call(pool, EID, "mcp:files/read_file", {"path": "a.txt"})
        sent = [r for r in MCP_REQS if r[1] == "tools/call"]
        check("C1 a call carries the access token as a bearer and comes back fenced",
              out.get("status") == "ok" and "read_file:a.txt" in out["result"]
              and sent and sent[-1][2].startswith(f"Bearer at-{AS}-"), (out, sent))
        uses = await pool.fetch("SELECT detail FROM vault_audit WHERE logical_name = $1 AND op = 'use:mcp_call'",
                                acc)
        check("C1 ...one vault_audit use:mcp_call row for the sign-in, naming the server",
              len(uses) >= 1 and "files" in str(uses[-1]["detail"]), [dict(u) for u in uses])
        for op, extra in (("use", {"action": "http_call", "request": {"url": "https://evil.example.com/x"}}),
                          ("add", {"value": "x"}), ("revoke", {})):
            r = await T._execute_meta_tool("keys_manage", {"op": op, "name": acc, **extra},
                                           entity_id=EID, pool=pool)
            check(f"C2 keys_manage op='{op}' refuses an MCP server's sign-in",
                  r.get("status") != "ok" and "sign-in" in str(r.get("result")), r)
        lst = await T._execute_meta_tool("keys_manage", {"op": "list"}, entity_id=EID, pool=pool)
        check("C2 ...it may list it (metadata only)", acc in json.dumps(lst, default=str), lst)
        try:
            await authz.use_credential(pool, entity_id=EID, name=acc, action="http_call",
                                       request={"url": "https://evil.example.com/x"})
            check("C3 the broker refuses the sign-in for any action but the MCP client's", False, "used")
        except authz.VaultAuthzError as e:
            check("C3 the broker refuses the sign-in for any action but the MCP client's", "sign-in" in str(e),
                  str(e))
        try:
            await MC.connect(pool, EID, name="steal", url="https://mcp-cimd.example.com/mcp", key=acc)
            check("C4 connect refuses another server's sign-in as a key", False, "connected")
        except MC.McpConnectionError as e:
            check("C4 connect refuses another server's sign-in as a key", "sign-in" in str(e), str(e))
        # The vault itself: every other reader takes a name from configuration,
        # a plan or a tool argument, so the sign-in reads as absent to all of
        # them and cannot be overwritten by any of them.
        check("C4b the vault reads a sign-in as absent to any caller but the sign-in",
              await vault_api.open_secret(pool, entity_id=EID, name=acc) is None
              and await vault_api.open_secret(pool, entity_id=EID, name=acc, sign_in=True) is not None)
        try:
            await vault_api.store_secret(pool, entity_id=EID, name=acc, cred_type="token", plaintext=b"theirs")
            check("C4b ...and refuses to let anything else write one", False, "written")
        except vault_api.SignInName as e:
            check("C4b ...and refuses to let anything else write one",
                  "sign-in" in str(e) and (await vault_api.open_secret(pool, entity_id=EID, name=acc,
                                                                         sign_in=True)).startswith(b"at-"), str(e))
        from server.ships import seal as ship_seal
        # Any machine key: the refusal comes before anything is sealed.
        machine_pub = base64.b64encode(secrets.token_bytes(32)).decode()
        try:
            await ship_seal.sealed_for_line(pool, EID, [acc], machine_pub, "n0nce", "2099-01-01T00:00:00Z")
            check("C4c a ship plan cannot hand a sign-in to a machine", False, "sealed")
        except ship_seal.SealError as e:
            check("C4c a ship plan cannot hand a sign-in to a machine (it reads as absent)",
                  "holds no credential" in str(e), str(e))
        # The Keys app's routes: a break-glass recipient re-wraps the envelope
        # straight from the store, so it is refused by name too; a write is a
        # 409 that says where to sign in, not a server error.
        from fastapi import HTTPException as _HTTPException
        from starlette.requests import Request as _Request

        from server.routes import vault as RV
        saved_eid = os.environ.get("ENTITY_ID")
        os.environ["ENTITY_ID"] = str(EID)
        got = []
        try:
            for label, call in (
                    ("break-glass", lambda: RV.add_break_glass(acc, RV.BreakGlassReq(
                        recipient_pubkey_b64=base64.b64encode(secrets.token_bytes(32)).decode()))),
                    ("store", lambda: RV.store_secret(RV.StoreReq(
                        name=acc, value_b64=base64.b64encode(b"theirs").decode()),
                        _Request({"type": "http", "headers": [], "client": ("127.0.0.1", 1)})))):
                try:
                    await call()
                    got.append((label, "done"))
                except _HTTPException as e:
                    got.append((label, e.status_code))
                except Exception as e:  # noqa: BLE001 - what reaches the person as a 500
                    got.append((label, type(e).__name__))
        finally:
            if saved_eid is None:
                os.environ.pop("ENTITY_ID", None)
            else:
                os.environ["ENTITY_ID"] = saved_eid
        envelope = await pool.fetchval("SELECT envelope::text FROM vault_envelope WHERE logical_name = $1 "
                                       "AND superseded_at IS NULL", acc)
        check("C4d the Keys app cannot add a break-glass recipient to a sign-in, and a write to one is a 409",
              got == [("break-glass", 409), ("store", 409)]
              and len(json.loads(envelope).get("recipients") or []) == 1, (got, envelope and envelope[:300]))

        # mcp_call is the MCP client's alone: refused before anything is opened.
        audited = await pool.fetchval("SELECT count(*) FROM vault_audit WHERE logical_name = $1", acc)
        try:
            await authz.use_credential(pool, entity_id=EID, name=acc, action="mcp_call",
                                       request={"url": RESOURCE})
            check("C4e mcp_call with no exchange is refused before the vault is opened", False, "used")
        except authz.VaultAuthzError as e:
            check("C4e mcp_call with no exchange is refused before the vault is opened (nothing audited)",
                  "MCP client" in str(e)
                  and await pool.fetchval("SELECT count(*) FROM vault_audit WHERE logical_name = $1", acc)
                  == audited, str(e))
        # The Ships app's key list says a sign-in is not held: a run would refuse it.
        from server.routes import ships as ships_route
        from server.ships import pipeline as ship_pipeline
        from server.ships import runner as ship_runner
        saved = (ship_pipeline.find_all, ship_runner.load_action_op)

        async def _pipelines(_pool):
            return [{"name": "deploy", "spec": {"steps": [{"step": "push", "op": "push-op"}]}}]

        async def _op(_pool, _name):
            return {"manifest": {"credentials": [acc, "wrong-key-not-here"]}}
        ship_pipeline.find_all, ship_runner.load_action_op = _pipelines, _op
        try:
            keys = {k["name"]: k["held"] for k in (await ships_route.list_keys())["keys"]}
        finally:
            ship_pipeline.find_all, ship_runner.load_action_op = saved
        check("C4f the Ships app counts a sign-in as not held, as the run treats it", keys.get(acc) is False, keys)

        await pool.execute("UPDATE mcp_oauth SET expires_at = now() + interval '20 seconds' "
                           "WHERE connection_id = $1", row["id"])
        n_ref = len(_tokens(AUTH, "refresh_token"))
        old_rt = json.loads((await vault_api.open_secret(pool, entity_id=EID, name=ref, sign_in=True)).decode())["refresh_token"]
        out = await MC.call(pool, EID, "mcp:files/read_file", {"path": "b.txt"})
        new_rt = json.loads((await vault_api.open_secret(pool, entity_id=EID, name=ref, sign_in=True)).decode())["refresh_token"]
        check("C5 a token about to expire is refreshed first, with the resource; the refresh token rotates",
              out.get("status") == "ok" and len(_tokens(AUTH, "refresh_token")) == n_ref + 1
              and _tokens(AUTH, "refresh_token")[-1][1].get("resource") == RESOURCE
              and new_rt != old_rt and AUTH.refresh[old_rt]["used"], (out, new_rt, old_rt))

        cur = (await vault_api.open_secret(pool, entity_id=EID, name=acc, sign_in=True)).decode()
        AUTH.access[cur]["revoked"] = True
        n_ref = len(_tokens(AUTH, "refresh_token"))
        out = await MC.call(pool, EID, "mcp:files/read_file", {"path": "c.txt"})
        check("C6 a 401 to a signed-in server: one refresh, one retry, and the call succeeds",
              out.get("status") == "ok" and len(_tokens(AUTH, "refresh_token")) == n_ref + 1, out)

        await pool.execute("UPDATE mcp_oauth SET expires_at = now() + interval '5 seconds' "
                           "WHERE connection_id = $1", row["id"])
        n_ref = len(_tokens(AUTH, "refresh_token"))
        a, b_ = await asyncio.gather(MC.call(pool, EID, "mcp:files/read_file", {"path": "d"}),
                                     MC.call(pool, EID, "mcp:files/read_file", {"path": "e"}))
        check("C7 two calls with an expiring token: one refresh between them, both succeed",
              a.get("status") == "ok" and b_.get("status") == "ok"
              and len(_tokens(AUTH, "refresh_token")) == n_ref + 1, (a, b_, len(_tokens(AUTH, "refresh_token"))))

        AUTH.refuse_refresh = True
        await pool.execute("UPDATE mcp_oauth SET expires_at = now() - interval '1 second' "
                           "WHERE connection_id = $1", row["id"])
        out = await MC.call(pool, EID, "mcp:files/read_file", {"path": "f"})
        oauth = await O.load(pool, row)
        names = await _vault_names(pool)
        check("C8 a refused refresh ends the sign-in: refused call, state sign_in_needed, token gone",
              out.get("status") != "ok" and "Sign in" in str(out.get("result"))
              and oauth["status"] == "sign_in_needed" and acc not in names, (out, oauth["status"], names))
        AUTH.refuse_refresh = False

        bc = await O.begin(pool, EID, row)
        await O.finish(pool, EID, AUTH.authorize(bc["authorize_url"]))
        AUTH.refresh_error = "invalid_target"
        await pool.execute("UPDATE mcp_oauth SET expires_at = now() - interval '1 second' "
                           "WHERE connection_id = $1", row["id"])
        out = await MC.call(pool, EID, "mcp:files/read_file", {"path": "g"})
        oauth = await O.load(pool, row)
        check("C9 a refresh refused with another 4xx error (invalid_target) ends the sign-in too, so the "
              "card does not stay 'signed in' while every call fails",
              out.get("status") != "ok" and "Sign in" in str(out.get("result"))
              and oauth["status"] == "sign_in_needed", (out, oauth["status"]))
        AUTH.refresh_error = None

        # ── P ──
        print("P. step-up")
        b6 = await O.begin(pool, EID, row)
        await O.finish(pool, EID, AUTH.authorize(b6["authorize_url"]))
        out = await MC.call(pool, EID, "mcp:files/write_file", {"path": "w"})
        oauth = await O.load(pool, row)
        check("P1 a 403 insufficient_scope: refused with what to do, the token kept, the scope asked for kept",
              out.get("status") != "ok" and "more access" in str(out.get("result"))
              and oauth["status"] == "active" and oauth["challenged_scope"] == "files:write"
              and O.access_secret_name(row) in await _vault_names(pool), (out, oauth))
        out = await MC.call(pool, EID, "mcp:files/read_file", {"path": "still"})
        check("P1 ...the server's other tools keep working", out.get("status") == "ok", out)
        sv = await MC.servers(pool, EID)
        entry = next((s for s in sv["servers"] if s["name"] == "files"), {})
        check("P2 op='servers' says more access is needed", entry["sign_in"]["state"] == "more_access_needed",
              entry)
        b7 = await O.begin(pool, EID, row)
        q7 = _query(b7["authorize_url"])
        check("P3 the step-up sign-in asks for the union of what it had and what the server named",
              set(q7["scope"].split()) == {"files:read", "offline_access", "files:write"}, q7.get("scope"))
        await O.finish(pool, EID, AUTH.authorize(b7["authorize_url"]))
        out = await MC.call(pool, EID, "mcp:files/write_file", {"path": "w"})
        check("P3 ...and after it the tool runs", out.get("status") == "ok"
              and (await O.load(pool, row))["challenged_scope"] is None, out)

        # A server that refuses even after sign-ins that asked for the scope it
        # names: the count survives those sign-ins, and the third refusal says
        # another sign-in will not help.
        STUBBORN[0] = True
        said = []
        for i in range(3):
            out = await MC.call(pool, EID, "mcp:files/write_file", {"path": "w"})
            said.append(str(out.get("result")))
            if i < 2:
                bx = await O.begin(pool, EID, row)
                await O.finish(pool, EID, AUTH.authorize(bx["authorize_url"]))
        STUBBORN[0] = False
        check("P4 a server no sign-in satisfies: asked to sign in again twice, then told the account may "
              "not be allowed, after 3 sign-ins that asked for it",
              all("sign in again" in s for s in said[:2]) and "may not be allowed" in said[2]
              and "after 3 sign-ins" in said[2], said)

        # ── A ──
        print("A. the server moves to another authorization server")
        MCP_AS[MCP] = AUTH_MOVED
        b10 = await O.begin(pool, EID, row)
        q10 = _query(b10["authorize_url"])
        moved = await O.load(pool, row)
        check("A1 the next sign-in reads the resource metadata again and goes to the new authorization "
              "server, registered there; the old registration and tokens are dropped",
              urlsplit(b10["authorize_url"]).hostname == AUTH_MOVED.host and q10.get("client_id") in AUTH_MOVED.clients
              and moved["issuer"] == AUTH_MOVED.issuer and moved["client_registration"] == "dcr"
              and O.access_secret_name(row) not in await _vault_names(pool), (b10, q10, moved))
        res = await O.finish(pool, EID, AUTH_MOVED.authorize(b10["authorize_url"]))
        out = await MC.call(pool, EID, "mcp:files/read_file", {"path": "moved"})
        check("A1 ...and its token works at the server", res.get("ok") and out.get("status") == "ok", (res, out))
        MCP_AS[MCP] = AUTH

        # The approvals below take the real path (the rule and the connection
        # signed and activated together, in the Network app's lane), but not a
        # fresh 30-second step-up code each: the code is the gate's subject in
        # section D and in scripts/test_mcp_client.py, and waiting out six more
        # windows is most of this gate's time.
        async def _code_ok(_pool, _code):
            return True
        nac_stepup.verify_stepup = _code_ok
        CODES_FREE[0] = True

        # ── R ──
        print("R. refusals")
        for name, host, want in (("nopkce", "mcp-nopkce.example.com", "S256"),
                                 ("liar", "mcp-liar.example.com", "another issuer"),
                                 ("wrongres", "mcp-wrongres.example.com", "another resource")):
            r = await _connect(pool, secret, name, host)
            try:
                await O.begin(pool, EID, r)
                check(f"R1 {name}: the sign-in is refused", False, "began")
            except O.OAuthError as e:
                check(f"R1 {name}: the sign-in is refused ({want})", want in str(e), str(e))
                if name == "wrongres":
                    # The text reaches the Host: only the host name of what the
                    # server's document said, never the rest of it.
                    check("R1 ...naming only the other resource's host", "other.example.com" in str(e)
                          and "other.example.com/mcp" not in str(e), str(e))
        await vault_api.store_secret(pool, entity_id=EID, name="wrong-key", cred_type="token", plaintext=b"nope")
        out = await MC.connect(pool, EID, name="keyed", url="https://mcp-cimd.example.com/mcp", key="wrong-key")
        await nac_commit.approve_egress(pool, EID, out["rule_id"], stepup_code=await _next_code(secret), via="ui")
        await asyncio.sleep(0.3)
        try:
            await MC.fetch_tools(pool, EID, "keyed")
            check("R2 a key the server refuses", False, "fetched")
        except MC.McpConnectionError as e:
            check("R2 a key the server refuses says to replace the key, not to sign in",
                  "refused your key wrong-key" in str(e) and "Keys app" in str(e), str(e))
        krow = await MC.live_row(pool, EID, "keyed")
        try:
            await O.begin(pool, EID, krow)
            check("R2 ...and a keyed connection cannot start a sign-in", False, "began")
        except O.OAuthError as e:
            check("R2 ...and a keyed connection cannot start a sign-in", "with your key" in str(e), str(e))

        # ── E ──
        print("E. registration without DCR")
        r = await _connect(pool, secret, "noreg", "mcp-noreg.example.com")
        try:
            await O.begin(pool, EID, r)
            check("E1 no registration endpoint: the person is asked for a client", False, "began")
        except O.NeedsClient as e:
            check("E1 no registration endpoint: the person is asked for a client, with the redirect to register",
                  e.redirect_uri == CALLBACK and "client ID" in str(e), str(e))
        b8 = await O.begin(pool, EID, r, client_id=NOREG_CLIENT, client_secret=NOREG_SECRET)
        res = await O.finish(pool, EID, AUTH_NOREG.authorize(b8["authorize_url"]))
        last = [x for x in AUTH_NOREG.reqs if x[0] == "token"][-1]
        check("E2 the person's client signs in with client_secret_basic (form-urlencoded parts), "
              "its secret never in the body",
              res.get("ok") and last[2] and last[2].startswith("Basic ") and "client_secret" not in last[1]
              and "client_id" not in last[1], (res, last))
        ox = await O.load(pool, r)
        sealed = json.loads((await vault_api.open_secret(pool, entity_id=EID, name=O.refresh_secret_name(r),
                                                         sign_in=True)).decode())
        check("E2 ...the secret is in the vault, not the table",
              ox["client_registration"] == "pre" and NOREG_SECRET not in json.dumps({k: str(v) for k, v in ox.items()})
              and sealed["client_secret"] == NOREG_SECRET, ox)
        os.environ[O.CIMD_URL_ENV] = CIMD_URL
        r = await _connect(pool, secret, "cimd", "mcp-cimd.example.com")
        b9 = await O.begin(pool, EID, r)
        q9 = _query(b9["authorize_url"])
        res = await O.finish(pool, EID, AUTH_CIMD.authorize(b9["authorize_url"]))
        check("E3 a Client ID Metadata Document: its URL is the client_id when it lists this redirect",
              q9.get("client_id") == CIMD_URL and res.get("ok")
              and (await O.load(pool, r))["client_registration"] == "cimd", (q9, res))
        os.environ.pop(O.CIMD_URL_ENV, None)
        try:
            b10 = await O.begin(pool, EID, r)
            q10 = _query(b10["authorize_url"])
            res = await O.finish(pool, EID, AUTH_CIMD.authorize(b10["authorize_url"]))
        except O.OAuthError as e:
            q10, res = {}, {"refused": str(e)}
        check("E4 with none named, the next sign-in uses the document C-Series publishes, read from its "
              "own address: its URL is the client_id",
              q10.get("client_id") == O.CIMD_URL_DEFAULT and q10.get("redirect_uri") == CALLBACK
              and res.get("ok") and (await O.load(pool, r))["client_id"] == O.CIMD_URL_DEFAULT, (q10, res))
        os.environ[O.CIMD_URL_ENV] = ""
        try:
            await O.begin(pool, EID, r)
            check("E5 set empty, no document is used", False, "a sign-in began")
        except O.NeedsClient as e:
            check("E5 set empty, no document is used: a server that offers nothing else asks the person "
                  "for a client", e.redirect_uri == CALLBACK, str(e))
        os.environ.pop(O.CIMD_URL_ENV, None)
        O._CIMD_CACHE.clear()
        PUBLISHED_SITE["down"], hits0 = True, PUBLISHED_SITE["hits"]
        first, second = await O._cimd_client_id(CALLBACK), await O._cimd_client_id(CALLBACK)
        missed = PUBLISHED_SITE["hits"] - hits0
        PUBLISHED_SITE["down"] = False
        again = await O._cimd_client_id(CALLBACK)
        O._CIMD_CACHE[O.CIMD_URL_DEFAULT] = (time.monotonic() - O._CIMD_MISS_TTL_S - 1, None)
        after = await O._cimd_client_id(CALLBACK)
        check("E6 a document the Host could not read is not asked for at the next sign-in, and is read "
              "again once the wait is over",
              first is None and second is None and missed == 1 and again is None
              and after == O.CIMD_URL_DEFAULT and PUBLISHED_SITE["hits"] - hits0 == 2,
              (first, second, missed, again, after, PUBLISHED_SITE["hits"] - hits0))
        O._CIMD_CACHE.clear()
        saved_timeout, O._CIMD_TIMEOUT_S = O._CIMD_TIMEOUT_S, 0.5
        PUBLISHED_SITE["stall"] = 3.0
        t0 = time.monotonic()
        try:
            stalled = await O._cimd_client_id(CALLBACK)
        finally:
            PUBLISHED_SITE["stall"], O._CIMD_TIMEOUT_S = 0.0, saved_timeout
            O._CIMD_CACHE.clear()
        waited = time.monotonic() - t0
        check("E6 ...and a document that does not answer is given up on after its own short wait",
              stalled is None and waited < 2.0, (stalled, round(waited, 2)))
        # auth-cimd offers no registration: there, a miss remembered from an
        # earlier sign-in would send the person off to register a client by
        # hand although the document is back, so it is read again; a miss from
        # the same sign-in is not (one wait per sign-in, not two).
        PUBLISHED_SITE["down"], hits0 = True, PUBLISHED_SITE["hits"]
        try:
            await O.begin(pool, EID, r)
            down = "a sign-in began"
        except O.NeedsClient:
            down = "needs client"
        down_hits = PUBLISHED_SITE["hits"] - hits0
        PUBLISHED_SITE["down"], hits0 = False, PUBLISHED_SITE["hits"]
        try:
            b11 = await O.begin(pool, EID, r)
            q11 = _query(b11["authorize_url"])
        except O.OAuthError as e:
            q11 = {"refused": str(e)}
        back_hits = PUBLISHED_SITE["hits"] - hits0
        O._CIMD_CACHE.clear()
        check("E6 ...but where the authorization server offers no other way, a miss from an earlier sign-in "
              "is read again: the document is used once it is back, and one sign-in waits on it once",
              down == "needs client" and down_hits == 1 and q11.get("client_id") == O.CIMD_URL_DEFAULT
              and back_hits == 1, (down, down_hits, q11, back_hits))

        # ── X ──
        print("X. sign-out, disconnect, the sweep")
        await O.sign_out(pool, EID, row)
        check("X1 sign-out forgets the tokens; the server needs a sign-in",
              (await O.load(pool, row))["status"] == "sign_in_needed"
              and O.access_secret_name(row) not in await _vault_names(pool))
        nr = await MC.live_row(pool, EID, "noreg")
        await MC.disconnect(pool, EID, name="noreg")
        names = await _vault_names(pool)
        check("X2 disconnect deletes the sign-in and its vault entries",
              await pool.fetchval("SELECT count(*) FROM mcp_oauth WHERE connection_id = $1", nr["id"]) == 0
              and O.access_secret_name(nr) not in names and O.refresh_secret_name(nr) not in names, names)
        cr = await MC.live_row(pool, EID, "cimd")
        await nac_commit.revoke_egress(pool, EID, int(cr["rule_id"]))
        check("X3 removing the rule leaves the token unusable at once (the connection is revoked)",
              (await MC.live_row(pool, EID, "cimd")) is None)
        await MC.servers(pool, EID)
        check("X3 ...and the next listing sweeps its vault entries",
              O.access_secret_name(cr) not in await _vault_names(pool))
    finally:
        await pool.close()


async def main(dsn: str) -> int:
    gate_sock = _reserve()
    gate = await asyncio.start_server(_gate, sock=gate_sock)
    gate_port = gate_sock.getsockname()[1]
    os.environ["NAC_EGRESS_PORT"] = str(gate_port)
    _spy_aiohttp()
    runner, port = await _serve()
    ROUTES.update({h: port for h in HOSTS})
    from server import mcp_connections as _MC

    async def _fake_resolve(host: str) -> list[str]:
        if host in HOSTS or host == "evil.example.com":
            return ["93.184.215.14"]
        raise OSError(f"{host}: no such name in this test")
    _MC._resolve_addrs = _fake_resolve
    try:
        unit_checks()
        print("M. migrations/v239 on the full current schema, run twice")
        from kernel_schema import _psql
        r1 = _psql(dsn, "-v", "ON_ERROR_STOP=1", "-f", str(MIGRATION))
        r2 = _psql(dsn, "-v", "ON_ERROR_STOP=1", "-f", str(MIGRATION))
        check("M1 v239 runs again on the full schema, and again", r1.returncode == 0 and r2.returncode == 0,
              (r1.stderr[-400:], r2.stderr[-400:]))
        bad = _psql(dsn, "-c", "INSERT INTO mcp_oauth (connection_id, entity_id, status, resource) "
                               "VALUES (999999, 1, 'active', 'https://a.example')")
        check("M2 the table refuses an active sign-in with no issuer (or no connection)",
              "violates" in bad.stderr, bad.stderr[-200:])
        # As the kernel creates it: the table owner is `entity`, whose default
        # privileges hand host_recall SELECT on every new table.
        fresh = _psql(dsn, "-v", "ON_ERROR_STOP=1", "-tA",
                      "-c", "DROP TABLE mcp_oauth_pending", "-c", "SET ROLE entity", "-f", str(MIGRATION),
                      "-c", "RESET ROLE",
                      "-c", "SELECT has_table_privilege('host_recall', 'mcp_oauth_pending', 'SELECT')")
        check("M3 the AI's read lane cannot read a sign-in in flight (its PKCE verifier) once v239 has run",
              fresh.returncode == 0 and fresh.stdout.strip().splitlines()[-1:] == ["f"],
              (fresh.returncode, fresh.stdout[-300:], fresh.stderr[-300:]))
        await service_checks(dsn)
    finally:
        await runner.cleanup()
        gate.close()
    return 0 if FAILED == 0 else 1


if __name__ == "__main__":
    import signal

    from kernel_schema import kernel_schema_db, throwaway_server
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))
    SERVER = os.environ.get("TEST_PG_DSN") or os.environ.get("PEER_TEST_DSN") or os.environ.get("TEST_DSN")
    if not SERVER and subprocess.run(["docker", "info"], capture_output=True).returncode != 0:
        print("SKIP-LOUD: no test server (TEST_PG_DSN/PEER_TEST_DSN/TEST_DSN) and docker unavailable — "
              "this gate needs a real database for the sign-in service.")
        sys.exit(2)
    with (contextlib.nullcontext(SERVER) if SERVER else throwaway_server()) as _srv, \
            kernel_schema_db(_srv, entity_id=EID) as _db:
        rc = asyncio.run(main(_db))
    print("RESULT: ALL PASS" if FAILED == 0 else f"RESULT: {FAILED} FAILED")
    sys.exit(rc)
