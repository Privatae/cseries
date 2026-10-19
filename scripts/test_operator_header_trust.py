#!/usr/bin/env python3
"""A person or a cert is believed only when the front door says so,
and the credential broker never calls its own Host.

The finding. The kernel took the person on a request from
X-Authentik-Email and the cert from X-Client-Cert-CN. The genuine values come
in over :8443 through the in-pod mTLS front door, which forwards to the plain
:8800 listener on loopback. Every other caller on the pod's loopback reaches
that listener too and could write both headers. The Host's own model can do
so through the credential broker (keys_manage op=use action=http_call): it
reached http://[::1]:8800 with headers it chose, took a Circles token from
/api/applications/circles/launch, and sent Circles and contacts actions as its
person, past the Host-only block.

What this pins, on real sockets: the real front door with real mTLS, a real
uvicorn, the real Circles and contacts routes (their collaborators stubbed),
and the real broker against stand-in servers.

  A. Who a request may say sent it (server/middleware/router_identity.py)
     1. The router path still works: through the front door under the router's
        cert, the Circles send and the contacts routes see the person.
     2. A loopback request naming a person is not that person: the Circles
        person send is refused no_identity and nothing reaches the bridge; the
        contacts routes see no person. With a forged X-Client-Cert-CN and a
        forged proof beside it, the same.
     3. The proof never reaches a route, and a client cannot plant one through
        the front door (it is replaced, never duplicated).
     4. A front-door request under another cert (a bastion's) keeps its CN and
        cannot name a person.
     5. The WebSocket half: an upgrade from loopback loses a forged CN (the
        bastion binding reads it) and a forged person (/ws reads it).
     6. server.main's real app (its middleware list, not its source) has the
        guard outside every layer but the app-origin and Host header guards,
        and those two neither read nor change the identity headers.
  B. The broker never calls its own Host (server/vault/destination.py)
     1. A call to loopback, in any spelling, a localhost name, the unspecified
        address, this machine's own address, or the kernel's own names is
        refused, and the stand-in kernel sees nothing.
     2. A name that resolves to the kernel's own Service address is refused.
     3. A redirect to any of those is refused, through the egress proxy as in
        a pod, and so is a name that answers differently at connect time.
     4. The path-secret lane refuses the same, and its refusal carries no secret.
     5. An ordinary third-party call through the proxy still works.

Run: APP_ROOT=<repo> python3 scripts/test_operator_header_trust.py
Container-free: loopback sockets only, no pod, no DNS beyond this machine.
"""
import asyncio
import datetime
import http.client
import http.server
import ipaddress
import json
import logging
import os
import pathlib
import socket
import ssl
import sys
import tempfile
import threading
import time
from types import SimpleNamespace

ROOT = pathlib.Path(os.environ.get("APP_ROOT") or pathlib.Path(__file__).resolve().parent.parent)
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "brain"))
logging.disable(logging.CRITICAL)

FAILS: list = []


def check(label, cond, detail=""):
    print(f"  {'ok  ' if cond else 'FAIL'} {label}" + ("" if cond else f"  [{str(detail)[:400]}]"))
    if not cond:
        FAILS.append(label)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# ── throwaway CA, the pod's server cert, and the client certs ───────────────
from cryptography import x509  # noqa: E402
from cryptography.hazmat.primitives import hashes, serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import ec  # noqa: E402
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID  # noqa: E402

ROUTER_CN = os.environ.setdefault("DASHBOARD_ROUTER_CN", "router.example.test")
TMP = pathlib.Path(tempfile.mkdtemp(prefix="r160-"))
_now = datetime.datetime.now(datetime.timezone.utc)
_ca_key = ec.generate_private_key(ec.SECP256R1())
_ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "r160 test CA")])
_ca = (x509.CertificateBuilder().subject_name(_ca_name).issuer_name(_ca_name)
       .public_key(_ca_key.public_key()).serial_number(x509.random_serial_number())
       .not_valid_before(_now - datetime.timedelta(minutes=5))
       .not_valid_after(_now + datetime.timedelta(days=1))
       .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
       .add_extension(x509.KeyUsage(digital_signature=True, content_commitment=False,
                                    key_encipherment=False, data_encipherment=False,
                                    key_agreement=False, key_cert_sign=True, crl_sign=True,
                                    encipher_only=False, decipher_only=False), critical=True)
       .add_extension(x509.SubjectKeyIdentifier.from_public_key(_ca_key.public_key()), critical=False)
       .sign(_ca_key, hashes.SHA256()))


def _leaf(cn: str, usage, san=None) -> tuple[str, str]:
    key = ec.generate_private_key(ec.SECP256R1())
    b = (x509.CertificateBuilder()
         .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)]))
         .issuer_name(_ca_name).public_key(key.public_key()).serial_number(x509.random_serial_number())
         .not_valid_before(_now - datetime.timedelta(minutes=5))
         .not_valid_after(_now + datetime.timedelta(days=1))
         .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
         .add_extension(x509.ExtendedKeyUsage([usage]), critical=False)
         .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(_ca_key.public_key()),
                        critical=False))
    if san:
        b = b.add_extension(san, critical=False)
    cert = b.sign(_ca_key, hashes.SHA256())
    stem = cn.replace(".", "_")
    c, k = TMP / f"{stem}.crt", TMP / f"{stem}.key"
    c.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    k.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                    serialization.NoEncryption()))
    return str(c), str(k)


CA_PEM = TMP / "ca.crt"
CA_PEM.write_bytes(_ca.public_bytes(serialization.Encoding.PEM))
SERVER_CRT, SERVER_KEY = _leaf("pod.test", ExtendedKeyUsageOID.SERVER_AUTH,
                               x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]))
ROUTER_CRT, ROUTER_KEY = _leaf(ROUTER_CN, ExtendedKeyUsageOID.CLIENT_AUTH)
BASTION_CRT, BASTION_KEY = _leaf("operator", ExtendedKeyUsageOID.CLIENT_AUTH)

KERNEL_PORT, DOOR_PORT = _free_port(), _free_port()
os.environ.update({
    "MTLS_SERVER_CERT": SERVER_CRT, "MTLS_SERVER_KEY": SERVER_KEY, "MTLS_CA_BUNDLE": str(CA_PEM),
    "FRONTDOOR_UPSTREAM_HOST": "127.0.0.1", "FRONTDOOR_UPSTREAM_PORT": str(KERNEL_PORT),
    "ENTITY_ID": "4242",
})
os.environ.pop("ENTITY_OPERATOR_EMAILS", None)
for _v in ("HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy", "NO_PROXY", "no_proxy"):
    os.environ.pop(_v, None)    # part A's clients talk straight to loopback

from fastapi import FastAPI, Request, WebSocket  # noqa: E402

# circles.py does `from server import brain_service, main as server_main` at
# module scope, and server.main mounts a container path at import time. Stubbed
# as scripts/test_circles_direct_lane_reason.py does; nothing here touches it.
import types  # noqa: E402
_stub_main = types.ModuleType("server.main")
_stub_main.app = None
sys.modules.setdefault("server.main", _stub_main)

from server import mtls_frontdoor as fd  # noqa: E402
from server.middleware.localhost_only import (LocalhostOnlyMiddleware,  # noqa: E402
                                              LocalhostOnlyWebSocketGate)
from server.operator_identity import request_email  # noqa: E402
from server.routes import circles, contacts  # noqa: E402

try:
    from server.middleware import router_identity as RI
except ImportError:          # a tree before this guard: nothing to install
    RI = None
    print("  (no server/middleware/router_identity.py in this tree: the kernel runs without the guard)")

PERSON = "me@example.com"
PEER_HOST = 77

# ── the kernel under test: the real routes, the real gates, the guard ────────
app = FastAPI()
app.include_router(circles.router)
app.include_router(contacts.router)


@app.get("/probe/identity")
async def probe_identity(request: Request):
    return {"email": request_email(request.headers),
            "cn": request.headers.get("x-client-cert-cn"),
            "proof": request.headers.get("x-cseries-frontdoor-proof")}


@app.post("/mcp")
async def probe_mcp(request: Request):
    # /mcp is one of the paths a non-router cert may reach (_MCP_CLIENT_PATHS).
    return {"email": request_email(request.headers),
            "cn": request.headers.get("x-client-cert-cn")}


@app.websocket("/ws-probe")
async def ws_probe(ws: WebSocket):
    await ws.accept()
    await ws.send_json({"email": request_email(ws.headers)})
    await ws.close()


@app.middleware("http")
async def _window_token(request, call_next):
    # What ApplicationTokenMiddleware attaches for the Circles window.
    request.state.app_token = SimpleNamespace(
        app_id="circles", permissions=["write:circles", "write:address_book", "read:circles"],
        origin_key="")
    return await call_next(request)


# Same order as server/main.py: the gates, then the guard outside them.
app.add_middleware(LocalhostOnlyMiddleware)
app.add_middleware(LocalhostOnlyWebSocketGate)
if RI is not None:
    app.add_middleware(RI.FrontDoorProof)

# Circles + contacts collaborators, recorded instead of performed. A person
# send carries to_user_email (circles.py, the dm branch); a Host-to-Host send
# does not, and names a person only through an optional from_user_email.
BRIDGE: list = []
CONTACT_CALLS: list = []


async def _bridge(op, params=None):
    BRIDGE.append((op, dict(params or {})))
    return None


def _person_sends():
    return [p for _op, p in BRIDGE if "to_user_email" in p]


def _host_sends():
    return [p for _op, p in BRIDGE if "to_user_email" not in p and p.get("target_id") == PEER_HOST]


async def _noop(**_kw):
    return None


async def _held(_peer):
    return None


async def _human(_pool, _email):
    return {"peer_entity_id": PEER_HOST, "status": "accepted", "display_name": "Peer"}


async def _accept(_pool, **kw):
    CONTACT_CALLS.append(("accept", kw.get("my_email")))
    return {"ok": True}


async def _by_email(_pool, **kw):
    CONTACT_CALLS.append(("by_email", kw.get("my_email")))
    return {"ok": True}


circles._bridge_call = _bridge
circles._audit = _noop
circles._held_slip_for = _held
circles._entity_id = lambda: 3
circles.brain_service.get_pool = lambda: object()
contacts.find_human_contact = _human
contacts._pool = lambda: object()
contacts.accept_request_service = _accept
contacts.request_by_email_service = _by_email

# ── serve it: uvicorn on loopback, the real front door in front of it ───────
import uvicorn  # noqa: E402
from gate_loopback import IDLE_KEEP_ALIVE_S  # noqa: E402

_kernel = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=KERNEL_PORT,
                                        log_level="critical", lifespan="off", timeout_keep_alive=IDLE_KEEP_ALIVE_S))
threading.Thread(target=_kernel.run, daemon=True).start()


def _door():
    async def main():
        srv = await asyncio.start_server(fd.handle_client, "127.0.0.1", DOOR_PORT,
                                         ssl=fd._build_ssl_context(), limit=fd.STREAM_LIMIT)
        async with srv:
            await srv.serve_forever()
    asyncio.run(main())


threading.Thread(target=_door, daemon=True).start()
for _ in range(200):
    if _kernel.started:
        break
    time.sleep(0.05)


def _door_call(method, path, cert, key, headers=None, body=None):
    ctx = ssl.create_default_context(cafile=str(CA_PEM))
    ctx.check_hostname = False
    ctx.load_cert_chain(cert, key)
    for _ in range(50):
        try:
            c = http.client.HTTPSConnection("127.0.0.1", DOOR_PORT, context=ctx, timeout=10)
            c.request(method, path, body=json.dumps(body) if body is not None else None,
                      headers={"Content-Type": "application/json", **(headers or {})})
            r = c.getresponse()
            return r.status, r.read().decode()
        except (ConnectionRefusedError, ssl.SSLError, OSError):
            time.sleep(0.1)
    raise RuntimeError("the front door never answered")


def _loop_call(method, path, headers=None, body=None):
    c = http.client.HTTPConnection("127.0.0.1", KERNEL_PORT, timeout=10)
    c.request(method, path, body=json.dumps(body) if body is not None else None,
              headers={"Content-Type": "application/json", **(headers or {})})
    r = c.getresponse()
    return r.status, r.read().decode()


def _code(text):
    try:
        d = json.loads(text).get("detail") or {}
        return (d.get("error") or {}).get("code") if isinstance(d, dict) else None
    except ValueError:
        return None


DM = {"target_kind": "dm", "target_id": "peer@example.com", "body": "hi"}
PERSON_HDR = {"X-Authentik-Email": PERSON}

print("\n── A1. the router path still names the person ──")
BRIDGE.clear()
st, txt = _door_call("POST", "/api/circles/messages", ROUTER_CRT, ROUTER_KEY, PERSON_HDR, DM)
sent = _person_sends()
check("through the front door under the router's cert, the Circles send carries the person",
      len(sent) == 1 and sent[0].get("from_user_email") == PERSON, (st, txt[:200], BRIDGE))
CONTACT_CALLS.clear()
_door_call("POST", "/api/contacts/accept-request", ROUTER_CRT, ROUTER_KEY, PERSON_HDR,
           {"handle": "peer", "kind": "host"})
_door_call("POST", "/api/contacts/request-by-email", ROUTER_CRT, ROUTER_KEY, PERSON_HDR,
           {"email": "peer@example.com"})
check("...and both contacts routes see the person",
      CONTACT_CALLS == [("accept", PERSON), ("by_email", PERSON)], CONTACT_CALLS)
st, txt = _door_call("GET", "/probe/identity", ROUTER_CRT, ROUTER_KEY, PERSON_HDR)
got = json.loads(txt) if st == 200 else {}
check("...a route reads the person and the router's CN", got.get("email") == PERSON
      and got.get("cn") == ROUTER_CN, (st, txt))
check("...and never the proof", got.get("proof") is None, got)

print("\n── A2. a loopback request naming a person is not that person ──")
for label, extra in (
        ("the person header alone", {}),
        ("with the router's CN forged beside it", {"X-Client-Cert-CN": ROUTER_CN}),
        ("with a made-up proof too", {"X-Client-Cert-CN": ROUTER_CN,
                                      "X-CSeries-Frontdoor-Proof": "guessed"})):
    BRIDGE.clear()
    st, txt = _loop_call("POST", "/api/circles/messages", {**PERSON_HDR, **extra}, DM)
    check(f"Circles person send from loopback, {label}: refused no_identity, nothing sent",
          st == 403 and _code(txt) == "no_identity" and not BRIDGE, (st, txt[:200], BRIDGE))
    CONTACT_CALLS.clear()
    _loop_call("POST", "/api/contacts/accept-request", {**PERSON_HDR, **extra},
               {"handle": "peer", "kind": "host"})
    _loop_call("POST", "/api/contacts/request-by-email", {**PERSON_HDR, **extra},
               {"email": "peer@example.com"})
    check(f"contacts routes from loopback, {label}: no person",
          CONTACT_CALLS == [("accept", ""), ("by_email", "")], CONTACT_CALLS)
BRIDGE.clear()
_loop_call("POST", "/api/circles/messages", PERSON_HDR,
           {"target_kind": "entity", "target_id": PEER_HOST, "body": "hi"})
host_sends = _host_sends()
check("a Host-to-Host send from loopback goes as the Host, with no person on it",
      len(host_sends) == 1 and "from_user_email" not in host_sends[0], BRIDGE)
st, txt = _loop_call("GET", "/probe/identity", {**PERSON_HDR, "X-Client-Cert-CN": ROUTER_CN,
                                                "X-CSeries-Frontdoor-Proof": "guessed"})
got = json.loads(txt) if st == 200 else {}
check("a route on the loopback path sees no person, no CN and no proof",
      st == 200 and got == {"email": "", "cn": None, "proof": None}, (st, txt))

print("\n── A3. the proof cannot be planted ──")
if RI is not None:
    head = fd._rebuild_request_head(b"GET / HTTP/1.1", [(b"Host", b"x"),
                                    (b"X-CSeries-Frontdoor-Proof", b"planted")], ROUTER_CN)
    proofs = [ln.split(b":", 1)[1].strip() for ln in head.split(b"\r\n")
              if ln.lower().startswith(b"x-cseries-frontdoor-proof:")]
    check("the front door replaces a client's proof with exactly one of its own",
          proofs == [RI.proof()], proofs)
    hdrs = [(b"x-cseries-frontdoor-proof", RI.proof()), (b"x-cseries-frontdoor-proof", RI.proof()),
            (b"x-client-cert-cn", ROUTER_CN.encode()), (b"x-authentik-email", PERSON.encode())]
    out, via_fd, via_router = RI.scrub(hdrs)
    check("two proofs are refused, not one picked", not via_fd and not via_router
          and all(not k.startswith(b"x-authentik-") and k != b"x-client-cert-cn" for k, _ in out), out)
else:
    check("the front door adds a proof the kernel checks", False, "no router_identity module")
st, txt = _door_call("GET", "/probe/identity", ROUTER_CRT, ROUTER_KEY,
                     {**PERSON_HDR, "X-CSeries-Frontdoor-Proof": "planted"})
got = json.loads(txt) if st == 200 else {}
check("a planted proof through the front door changes nothing: the router path still works",
      got.get("email") == PERSON and got.get("proof") is None, (st, txt))

print("\n── A3b. a head the kernel could split differently never goes upstream ──")
# The proof is the last line of the head the front door rebuilds. A bare LF/CR
# or NUL inside a value, or two framings for one body, is where the kernel's
# parser could end the head early and read the proof line as a body, which an
# echoing route would hand back. The front door must answer 400 itself.
_UPSTREAMS: list = []
_real_open_upstream = fd._open_upstream


async def _counting_open_upstream():
    _UPSTREAMS.append(1)
    return await _real_open_upstream()

fd._open_upstream = _counting_open_upstream


def _raw_door(raw: bytes) -> bytes:
    ctx = ssl.create_default_context(cafile=str(CA_PEM))
    ctx.check_hostname = False
    ctx.load_cert_chain(ROUTER_CRT, ROUTER_KEY)
    with socket.create_connection(("127.0.0.1", DOOR_PORT), timeout=10) as s:
        with ctx.wrap_socket(s) as t:
            t.sendall(raw)
            out = b""
            try:
                while True:
                    b = t.recv(65536)
                    if not b:
                        break
                    out += b
            except (socket.timeout, ssl.SSLError, OSError):
                pass
            return out


_SPLIT = (b"POST /probe/identity HTTP/1.1\r\nContent-Length: 400\r\n"
          b"Content-Type: text/plain\r\nConnection: close")
for label, raw in (
        ("a bare LF inside a value",
         b"GET /probe/identity HTTP/1.1\r\nHost: x\r\nX-A: a\n\n" + _SPLIT + b"\r\n\r\n"),
        ("a bare CR inside a value",
         b"GET /probe/identity HTTP/1.1\r\nHost: x\r\nX-A: a\rb\r\nConnection: close\r\n\r\n"),
        ("a NUL inside a value",
         b"GET /probe/identity HTTP/1.1\r\nHost: x\r\nX-A: a\x00b\r\nConnection: close\r\n\r\n"),
        ("two Content-Lengths",
         b"POST /probe/identity HTTP/1.1\r\nHost: x\r\nContent-Length: 2\r\n"
         b"Content-Length: 2\r\nConnection: close\r\n\r\nhi"),
        ("Transfer-Encoding with Content-Length",
         b"POST /probe/identity HTTP/1.1\r\nHost: x\r\nContent-Length: 7\r\n"
         b"Transfer-Encoding: chunked\r\nConnection: close\r\n\r\n2\r\nhi\r\n0\r\n\r\n"),
        ("two Transfer-Encodings",
         b"POST /probe/identity HTTP/1.1\r\nHost: x\r\nTransfer-Encoding: chunked\r\n"
         b"Transfer-Encoding: chunked\r\nConnection: close\r\n\r\n0\r\n\r\n"),
        ("a Content-Length that is not a number",
         b"POST /probe/identity HTTP/1.1\r\nHost: x\r\nContent-Length: +2\r\n"
         b"Connection: close\r\n\r\nhi")):
    _UPSTREAMS.clear()
    resp = _raw_door(raw)
    check(f"{label}: the front door answers 400 itself",
          resp.startswith(b"HTTP/1.1 400 "), resp[:120])
    check(f"...and nothing went upstream ({label})", not _UPSTREAMS, len(_UPSTREAMS))
    if RI is not None:
        check(f"...and the proof is nowhere in the reply ({label})", RI.proof() not in resp)
_UPSTREAMS.clear()
resp = _raw_door(b"GET /probe/identity HTTP/1.1\r\nHost: x\r\nX-Authentik-Email: "
                 + PERSON.encode() + b"\r\nConnection: close\r\n\r\n")
check("a clean head still goes through and names the person",
      resp.startswith(b"HTTP/1.1 200 ") and PERSON.encode() in resp and len(_UPSTREAMS) == 1,
      resp[:200])

print("\n── A4. another cert through the front door keeps its CN, names no one ──")
st, txt = _door_call("POST", "/mcp", BASTION_CRT, BASTION_KEY, PERSON_HDR, {})
got = json.loads(txt) if st == 200 else {}
check("a bastion's request reaches /mcp with its CN and without a person",
      st == 200 and got.get("cn") == "operator" and got.get("email") == "", (st, txt))

print("\n── A5. the WebSocket half ──")


async def _ws_scope_headers(headers, path="/bastion-egress"):
    seen = {}

    async def inner(scope, receive, send):
        seen.update(scope)
    mw = RI.FrontDoorProof(inner) if RI is not None else inner
    await mw({"type": "websocket", "path": path, "client": ("::1", 5000), "headers": headers},
             None, None)
    return dict(seen.get("headers") or [])

h = asyncio.run(_ws_scope_headers([(b"host", b"pod"), (b"x-client-cert-cn", b"operator"),
                                   (b"x-authentik-email", PERSON.encode())]))
check("an upgrade from loopback loses a forged CN (the bastion binding reads it)",
      b"x-client-cert-cn" not in h, h)
check("...and a forged person (/ws reads it)", b"x-authentik-email" not in h, h)
if RI is not None:
    h = asyncio.run(_ws_scope_headers([(b"host", b"pod"), (b"x-client-cert-cn", b"operator"),
                                       (b"x-cseries-frontdoor-proof", RI.proof())]))
    check("an upgrade the front door forwarded keeps its CN, and the proof is taken off",
          h.get(b"x-client-cert-cn") == b"operator" and b"x-cseries-frontdoor-proof" not in h, h)
# The whole stack in-process (this box's Python may carry no WebSocket library
# for uvicorn), from a loopback client address and a localhost Host so the
# localhost gate admits it.
try:
    from starlette.testclient import TestClient
    with TestClient(app, base_url="http://localhost", client=("::1", 5000)) as tc:
        with tc.websocket_connect("ws://localhost/ws-probe", headers={"X-Authentik-Email": PERSON}) as w:
            got = w.receive_json()
except Exception as e:  # noqa: BLE001
    got = {"error": f"{type(e).__name__}: {e}"}
check("a WebSocket from loopback naming a person sees no person", got == {"email": ""}, got)

print("\n── A6. server.main's real app: the guard sits outside every layer but two ──")
# Structural, not source text: the real app's own middleware list, outermost
# first (Starlette inserts each add at the front). A layer added later under
# any name lands outside FrontDoorProof only by being added after it, and then
# this fails. The two layers allowed outside are run for real below and must
# neither read nor alter the identity headers before the scrub.
sys.path.insert(0, str(ROOT / "scripts"))
# The A sections ran against a stub server.main (above); the real one is
# imported here, then the stub is put back for anything that follows.
import server as _server_pkg  # noqa: E402
_stub_saved = sys.modules.pop("server.main", None)
try:
    import test_api_route_guards as _rt  # noqa: E402  (repo + brain/ on sys.path)
    MAIN_APP = _rt.load_app()
except Exception as e:  # noqa: BLE001
    MAIN_APP = None
    check("server.main's app imports container-free", False, f"{type(e).__name__}: {e}")
finally:
    if _stub_saved is not None:
        sys.modules["server.main"] = _stub_saved
        _server_pkg.main = _stub_saved
check("the real server.main app was imported, not the stub",
      MAIN_APP is not None and MAIN_APP is not getattr(_stub_saved, "app", None), MAIN_APP)
ALLOWED_OUTSIDE = {"AppOriginGuard", "HostHeaderGuard"}
if MAIN_APP is not None and RI is not None:
    stack = list(MAIN_APP.user_middleware)
    names = [getattr(m.cls, "__name__", repr(m.cls)) for m in stack]
    at = [i for i, m in enumerate(stack) if m.cls is RI.FrontDoorProof]
    check("FrontDoorProof is in server.main's stack exactly once", len(at) == 1, names)
    if len(at) == 1:
        outside = names[:at[0]]
        check("...and only AppOriginGuard and HostHeaderGuard sit outside it",
              set(outside) == ALLOWED_OUTSIDE and len(outside) == len(ALLOWED_OUTSIDE),
              names)
        check("...with every header reader inside it",
              {"LocalhostOnlyMiddleware", "LocalhostOnlyWebSocketGate",
               "ApplicationTokenMiddleware", "CEIGASGateMiddleware"} <= set(names[at[0] + 1:]),
              names)

        class _Watched(bytes):
            """A header value that records being read: compared, decoded,
            searched or sliced. Only a layer that reads the value calls these."""
            READS: list = []

            def _hit(self, how):
                _Watched.READS.append((bytes(self)[:24], how))

        for _m in ("__eq__", "__ne__", "__hash__", "__contains__", "__getitem__", "__len__",
                   "__iter__", "decode", "lower", "upper", "strip", "split", "startswith",
                   "endswith", "find", "partition"):
            def _wrap(name):
                base = getattr(bytes, name)

                def f(self, *a, **k):
                    self._hit(name)
                    return base(self, *a, **k)
                return f
            setattr(_Watched, _m, _wrap(_m))

        SEEN: list = []

        async def _probe_route(scope, receive, send):
            SEEN.append([(bytes(k), bytes(v)) for k, v in scope.get("headers") or ()])
            if scope["type"] == "http":
                await send({"type": "http.response.start", "status": 200, "headers": []})
                await send({"type": "http.response.body", "body": b""})

        def _chain(layers, inner):
            for m in reversed(layers):
                inner = m.cls(inner, *m.args, **m.kwargs)
            return inner

        IDENTITY = [(b"x-authentik-email", PERSON.encode()), (b"x-authentik-uid", b"1"),
                    (b"x-client-cert-cn", ROUTER_CN.encode()),
                    (RI.PROOF_HEADER, b"forged-proof")]

        def _scope(kind, watched):
            hdrs = [(b"host", b"127.0.0.1:8800")]
            hdrs += [(k, _Watched(v) if watched else v) for k, v in IDENTITY]
            return {"type": kind, "asgi": {"version": "3.0"}, "http_version": "1.1",
                    "method": "GET", "scheme": "http" if kind == "http" else "ws",
                    "path": "/probe/identity", "raw_path": b"/probe/identity",
                    "query_string": b"", "root_path": "", "headers": hdrs,
                    "client": ("127.0.0.1", 5000), "server": ("127.0.0.1", 8800)}

        async def _recv():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def _send(_msg):
            return None

        for kind in ("http", "websocket"):
            # The outer layers alone: the identity values must be passed on
            # untouched and never read.
            SEEN.clear(); _Watched.READS.clear()
            asyncio.run(_chain(stack[:at[0]], _probe_route)(_scope(kind, True), _recv, _send))
            got = SEEN[0] if SEEN else None
            check(f"[{kind}] the layers outside the guard pass every identity header on unchanged",
                  got is not None and all(h in got for h in IDENTITY), got)
            check(f"[{kind}] ...and read none of their values", not _Watched.READS, _Watched.READS)
            # Through the real outer layers and the real guard: nothing a
            # loopback caller forged reaches the route.
            SEEN.clear()
            asyncio.run(_chain(stack[:at[0] + 1], _probe_route)(_scope(kind, False), _recv, _send))
            got = SEEN[0] if SEEN else None
            check(f"[{kind}] through the real stack down to the guard, the route sees none of them",
                  got is not None and not any(k == RI.PROOF_HEADER or k == b"x-client-cert-cn"
                                              or k.startswith(b"x-authentik-") for k, _ in got), got)
else:
    check("server.main's stack can be checked", False, "no app or no router_identity module")

# ── B. the broker ───────────────────────────────────────────────────────────
print("\n── B. the broker never calls its own Host ──")
HITS: list = []


class _Kernel(http.server.BaseHTTPRequestHandler):
    def _any(self):
        n = int(self.headers.get("content-length") or 0)
        if n:
            self.rfile.read(n)
        HITS.append((self.command, self.path, self.headers.get("x-authentik-email")))
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"{}")
    do_GET = do_POST = _any

    def log_message(self, *a):
        pass


class _Proxy(http.server.BaseHTTPRequestHandler):
    """The node's egress proxy, as aiohttp meets it: absolute-form requests.
    Answers for the third parties in this test: one is ordinary, the others
    redirect to the Host."""
    def _any(self):
        n = int(self.headers.get("content-length") or 0)
        if n:
            self.rfile.read(n)
        target = {"/to-v4": f"http://127.0.0.1:{SPORT}/api/circles/messages",
                  "/to-v6": f"http://[::1]:{SPORT}/api/circles/messages",
                  "/to-name": f"http://localhost:{SPORT}/api/circles/messages",
                  "/to-rebind": f"http://rebind.test:{SPORT}/api/circles/messages"}
        for k, v in target.items():
            if self.path.endswith(k):
                self.send_response(307)
                self.send_header("Location", v)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", "11")
        self.end_headers()
        self.wfile.write(b'{"ok":true}')
    do_GET = do_POST = _any

    def log_message(self, *a):
        pass


_ks = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Kernel)
SPORT = _ks.server_address[1]
threading.Thread(target=_ks.serve_forever, daemon=True).start()
try:
    class _V6(http.server.ThreadingHTTPServer):
        address_family = socket.AF_INET6
    _ks6 = _V6(("::1", SPORT), _Kernel)
    threading.Thread(target=_ks6.serve_forever, daemon=True).start()
except OSError:
    pass   # no IPv6 loopback here; the [::1] cases still must be refused
_px = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Proxy)
threading.Thread(target=_px.serve_forever, daemon=True).start()
# The pod's egress settings, as the entity container carries them.
os.environ["HTTP_PROXY"] = os.environ["http_proxy"] = f"http://127.0.0.1:{_px.server_address[1]}"
os.environ["HTTPS_PROXY"] = os.environ["https_proxy"] = os.environ["HTTP_PROXY"]
os.environ["NO_PROXY"] = os.environ["no_proxy"] = \
    "localhost,127.0.0.1,::1,.svc,.svc.cluster.local,.cluster.local,rebind.test"

from server.vault import authz  # noqa: E402

try:
    from server.vault import destination as D
except ImportError:
    D = None
    print("  (no server/vault/destination.py in this tree: the broker sends anywhere)")

CLUSTER_IP = "fd12:3456:ff::1092"
FAKE_DNS = {"api.example.test": ["203.0.113.7"], "redirector.example.test": ["203.0.113.7"],
            "rebind.test": ["203.0.113.8"], "alias.example.test": [CLUSTER_IP],
            "entity-4242.entity-4242.svc.cluster.local": [CLUSTER_IP],
            "entity-4242-0.entity-4242.entity-4242.svc.cluster.local": ["fd12:3456:fe::4242"]}
if D is not None:
    _real_resolve = D._resolve_addrs

    async def _fake_resolve(host, port):
        if host in FAKE_DNS:
            return FAKE_DNS[host]
        return await _real_resolve(host, port)
    D._resolve_addrs = _fake_resolve

# The name that answers one thing to the check and another to the connection,
# installed where a TCPConnector finds its default resolver.
import aiohttp.connector as _ar  # noqa: E402
import aiohttp.resolver as _res  # noqa: E402
_RealDefault = _ar.DefaultResolver


class _Rebinding(_res.ThreadedResolver):
    async def resolve(self, host, port=0, family=socket.AF_INET):
        if host == "rebind.test":
            return [{"hostname": host, "host": "127.0.0.1", "port": port, "family": socket.AF_INET,
                     "proto": 0, "flags": socket.AI_NUMERICHOST}]
        return await super().resolve(host, port, family)


_ar.DefaultResolver = _Rebinding
SECRET = b"sk-test-secret-value"


async def _call(url, **extra):
    req = {"url": url, "method": "POST", "headers": {"X-Authentik-Email": PERSON},
           "json": {"target_kind": "dm", "target_id": "x", "body": "hi"}, **extra}
    try:
        out = await authz._do_http_call(SECRET, req, path_secret=extra.get("auth_scheme") == "path")
        return ("ok", out.get("status"))
    except authz.VaultAuthzError as e:
        return ("refused", str(e))
    except Exception as e:  # noqa: BLE001
        return ("error", f"{type(e).__name__}: {e}")


def refused_self(label, url, **extra):
    HITS.clear()
    kind, detail = asyncio.run(_call(url, **extra))
    check(f"{label}: refused as this Host", kind == "refused" and "this Host" in str(detail),
          (kind, detail))
    check(f"...and the stand-in kernel saw nothing ({label})", not HITS, HITS)
    return detail


for label, url in (("127.0.0.1", f"http://127.0.0.1:{SPORT}/api/circles/messages"),
                   ("[::1]", f"http://[::1]:{SPORT}/api/circles/messages"),
                   ("localhost", f"http://localhost:{SPORT}/api/circles/messages"),
                   ("127.0.0.2 (all of 127/8)", f"http://127.0.0.2:{SPORT}/"),
                   ("127.1 (a legacy spelling)", f"http://127.1:{SPORT}/"),
                   ("[::ffff:127.0.0.1]", f"http://[::ffff:127.0.0.1]:{SPORT}/"),
                   ("0.0.0.0", f"http://0.0.0.0:{SPORT}/"),
                   ("[::]", f"http://[::]:{SPORT}/"),
                   ("the kernel's Service name", "http://entity-4242.entity-4242.svc.cluster.local:8800/"),
                   ("the kernel's short name", "http://entity-4242:8800/"),
                   ("the kernel's pod name",
                    "http://entity-4242-0.entity-4242.entity-4242.svc.cluster.local:8800/"),
                   ("a name for the kernel's ClusterIP", "http://alias.example.test/"),
                   ("the ClusterIP itself", f"http://[{CLUSTER_IP}]:8800/"),
                   # IPv6 forms that carry an IPv4 inside them: a host stack or a
                   # translator on the path can turn each back into that IPv4.
                   ("[::127.0.0.1] (IPv4-compatible)", f"http://[::127.0.0.1]:{SPORT}/"),
                   ("[::127.9.9.9] (IPv4-compatible, all of 127/8)", f"http://[::127.9.9.9]:{SPORT}/"),
                   ("[::0.1.2.3] (IPv4-compatible, 0/8)", f"http://[::0.1.2.3]:{SPORT}/"),
                   ("[64:ff9b::127.0.0.1] (NAT64)", f"http://[64:ff9b::127.0.0.1]:{SPORT}/"),
                   ("[64:ff9b::0.0.0.0] (NAT64, 0/8)", f"http://[64:ff9b::0.0.0.0]:{SPORT}/"),
                   ("[2002:7f00:1::] (6to4)", f"http://[2002:7f00:1::]:{SPORT}/"),
                   ("[2002:0:0::1] (6to4, 0/8)", f"http://[2002:0:0::1]:{SPORT}/")):
    refused_self(label, url)

own = sorted(str(a) for a in (D.own_addresses() if D is not None else [])
             if not a.is_loopback and not a.is_link_local)
if own:
    a = own[0]
    refused_self(f"this machine's own address ({a})",
                 f"http://{'[' + a + ']' if ':' in a else a}:{SPORT}/")
else:
    check("this machine has an address of its own to test with", False,
          "own_addresses() found none" if D is not None else "no destination module")
own4 = [ipaddress.IPv4Address(x) for x in own if ":" not in x]
if own4:
    v4 = own4[0]
    six = int(ipaddress.IPv6Address("2002::")) | (int(v4) << 80)
    for form, addr in (("IPv4-compatible", ipaddress.IPv6Address(int(v4))),
                       ("NAT64", ipaddress.IPv6Address(int(ipaddress.IPv6Address("64:ff9b::")) | int(v4))),
                       ("6to4", ipaddress.IPv6Address(six))):
        refused_self(f"this machine's own IPv4 as {form} ([{addr}])", f"http://[{addr}]:{SPORT}/")
else:
    check("this machine has an IPv4 address of its own to embed", False, own)

print("\n── B3. redirects, through the egress proxy as in a pod ──")
for path in ("/to-v4", "/to-v6", "/to-name", "/to-rebind"):
    refused_self(f"a third party redirecting {path}", f"http://redirector.example.test{path}")

print("\n── B4. the path-secret lane ──")
d = refused_self("a path-secret call to loopback",
                 f"https://127.0.0.1:{SPORT}/bot{{secret}}/getMe", auth_scheme="path")
check("...and its refusal never carries the secret", SECRET.decode() not in str(d), d)

print("\n── B5. an ordinary third party still works ──")
HITS.clear()
kind, detail = asyncio.run(_call("http://api.example.test/v1/thing"))
check("a call through the egress proxy answers", (kind, detail) == ("ok", 200), (kind, detail))
if D is not None:
    for host in ("api.example.test", "203.0.113.9", "64:ff9b::203.0.113.9", "2002:cb00:7109::1"):
        why = asyncio.run(D.refusal(host))
        check(f"refusal() lets {host} through", why is None, why)

_ar.DefaultResolver = _RealDefault
_kernel.should_exit = True
print()
if FAILS:
    print(f"FAILED {len(FAILS)}:")
    for f in FAILS:
        print(f"  - {f}")
    sys.exit(1)
print("operator header trust + broker destination: all checks passed")
