#!/usr/bin/env python3
"""Gate: a connector words a failure it is about to try again so the Host's
app proof does not read it as an error, and a failure it gives up on still
reads as one.

Two readers decide whether a line an app logged is an error:
brain/mastercode/runtime/tools.py _LOG_ERROR_RE, which app_status uses for
log_errors and its verdict ("running and ready, but the log shows
error-shaped lines"), and the narrower app_verify._ERROR_RE, which the
delivery proof reads before it calls an app delivered or opens a repair. The
connectors' example gates read the connector's log with the first
(scripts/test_connector_*_example.py, checks L and M).

The connectors logged ordinary failures they were about to try again in
Python's words ("<urlopen error [Errno 111] Connection refused>") or in the
Host's ("the call to api.telegram.org failed (ClientConnectorError)"), so a
connector that recovered read as a broken one, and the example gates failed
whenever such a line landed before their check (seen in several CI runs).

Each system-apps/<id>/connector.py with these paths (telegram, slack, discord)
is loaded as a module, its call to the Host and its clock replaced, and each
path that logs a failure it tries again is handed that failure the way the
standard library raises it. No line it logs may match either reader, and the
line still says what went wrong:

  A  the long poll (Telegram), the Socket Mode loop (Slack) and the gateway
     loop (Discord): the call refused, cut (a broken pipe, a reset, closed
     with no answer, an answer cut short or unreadable), timed out, or its
     name not resolved; a socket's TLS failing for real (an untrusted
     certificate, a handshake the other end hung up on); a socket's handshake
     answered "500 Internal Server Error"
  B  a part's send: the same failures; the platform's 502 page that is not
     JSON; Telegram's own 500; Slack's internal_error and fatal_error;
     Discord's 503; and the Host's 502 for a platform call it could not make,
     carrying the detail the kernel builds for it (K)
  C  Slack's picture upload cut part-way: a broken pipe, a reset, closed with
     no answer, TLS
  D  the Host answering 503 to a message, which is posted again
  E  every errno, raised as it is and inside urllib's URLError, and every
     getaddrinfo failure (its text read from the C library), through the
     loop and through a send

  G  a failure the connector gives up on reads as an error to both readers: a
     message dropped (by the connector's own fault, or with the Host
     unreachable while a pairing code is reported), and a reply let go after
     REATTACH_TRIES tries that got nothing

  K  the kernel's 502 detail for a platform call that did not go through
     reads as no error, over both of the vault broker's lanes (the URL-path
     lane Telegram's token takes, the header lane Slack's and Discord's take):
     a real refused connection on loopback, through the real
     server/vault/authz.py _do_http_call and nac_connector.platform_call

No network past loopback, no database, no container.
Run: APP_ROOT=<repo> python3 scripts/test_connector_retry_wording.py
"""
from __future__ import annotations

import asyncio
import base64
import contextlib
import ctypes
import errno
import http.client
import importlib.util
import io
import json
import os
import re
import socket
import ssl
import sys
import tempfile
import threading
import time
import types
import urllib.error
from pathlib import Path

ROOT = Path(os.environ.get("APP_ROOT") or Path(__file__).resolve().parents[1])
sys.path[:0] = [str(ROOT), str(ROOT / "brain"), str(ROOT / "scripts")]
# connector.py is loaded from system-apps/<id>/, which
# scripts/rebuild_factory_bundles.sh packs and signs whole: no bytecode there.
sys.dont_write_bytecode = True
# K makes real connections on loopback, never through a proxy.
for _k in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
    os.environ.pop(_k, None)

from mastercode.runtime import app_verify  # noqa: E402
from mastercode.runtime import tools as host_tools  # noqa: E402

READERS = {"tools._LOG_ERROR_RE": host_tools._LOG_ERROR_RE,
           "app_verify._ERROR_RE": app_verify._ERROR_RE}
APPS = ("telegram", "slack", "discord")
_TMP = tempfile.TemporaryDirectory(prefix="connector-retry-wording-")
fails: list[str] = []
counted = [0]


def check(label, cond, detail=""):
    counted[0] += 1
    print(("  PASS  " if cond else "  FAIL  ") + label
          + (("   [" + str(detail)[:1500] + "]") if detail and not cond else ""))
    if not cond:
        fails.append(label)


def as_error(line: str) -> list[str]:
    """The readers that take `line` for an error."""
    return [name for name, rx in READERS.items() if rx.search(line)]


# ── a connector, loaded as a module ──────────────────────────────────────

def load(app: str):
    """system-apps/<app>/connector.py as a module, its settings from the
    environment as the platform sets them, its state file in a scratch dir."""
    os.environ.update({"PRIVATAE_APP_ID": app, "CSERIES_KERNEL_URL": "http://127.0.0.1:9",
                       "CSERIES_CONNECTOR_CAP": "gate-cap", "PORT": "0",
                       "CONNECTOR_STATE_DIR": os.path.join(_TMP.name, app)})
    spec = importlib.util.spec_from_file_location(f"{app}_connector_wording",
                                                  ROOT / "system-apps" / app / "connector.py")
    c = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(c)
    return c


class Stop(Exception):
    """The clock's way to end a loop once it has logged its first failure."""


def clock(c, *, stop: bool) -> None:
    def sleep(_seconds):
        if stop:
            raise Stop()
    c.time = types.SimpleNamespace(sleep=sleep, monotonic=time.monotonic, time=time.time)


def said(fn, *args) -> list[str]:
    """The lines the connector printed while fn ran."""
    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.suppress(Stop):
        fn(*args)
    return [ln for ln in out.getvalue().splitlines() if ln.strip()]


def raising(make):
    """A stand-in that raises make() each time it is called."""
    def stand_in(*_a, **_k):
        raise make()
    return stand_in


class Answer(io.BytesIO):
    """An answer as urllib's opener returns it: a status and a body."""

    def __init__(self, obj, status: int = 200):
        super().__init__(json.dumps(obj).encode())
        self.status = status


def broker(status, body_json, body_text: str = "") -> dict:
    """The Host's 200 for a platform call: what the platform answered."""
    return {"status": status, "json": body_json, "body": body_text, "truncated": False}


def host_error(code: int, detail: str) -> urllib.error.HTTPError:
    """The Host answering `code` with FastAPI's {"detail": ...}, as urllib
    raises it."""
    return urllib.error.HTTPError("http://127.0.0.1:9/api/connector", code, "gate", {},
                                  io.BytesIO(json.dumps({"detail": detail}).encode()))


def module_like(mod, **over):
    """A module's names with some replaced: stands in for it inside one
    connector without changing it for the rest of this process."""
    ns = types.SimpleNamespace(**{k: getattr(mod, k) for k in dir(mod) if not k.startswith("__")})
    for k, v in over.items():
        setattr(ns, k, v)
    return ns


KEEP: list = []       # the far ends of stand-in sockets, held open until the run ends


def answering(raw: bytes):
    """socket.create_connection's stand-in: a connected socket whose far end
    has already answered `raw`."""
    def create_connection(_address, timeout=None, *_a, **_k):
        here, there = socket.socketpair()
        there.sendall(raw)
        KEEP.append(there)
        return here
    return create_connection


class NoTLS:
    """ssl.create_default_context's stand-in: the socket as it is."""

    def wrap_socket(self, raw, server_hostname=None):
        return raw


# ── the failures, as the standard library raises them ────────────────────

def urllib_failures() -> list:
    """(what happened, words the line must carry, make). Every call a
    connector makes to its Host goes through urllib: what fails before the
    answer starts (connecting, sending) comes inside URLError, what fails
    after it comes as it is."""
    return [
        ("refused", "connection refused",
         lambda: urllib.error.URLError(ConnectionRefusedError(errno.ECONNREFUSED, "Connection refused"))),
        ("timed out connecting", "timed out", lambda: urllib.error.URLError(TimeoutError("timed out"))),
        ("its name not resolved", "did not resolve",
         lambda: urllib.error.URLError(socket.gaierror(socket.EAI_AGAIN,
                                                        "Temporary failure in name resolution"))),
        ("cut by a broken pipe", "broken pipe",
         lambda: urllib.error.URLError(BrokenPipeError(errno.EPIPE, "Broken pipe"))),
        ("reset", "connection reset",
         lambda: ConnectionResetError(errno.ECONNRESET, "Connection reset by peer")),
        ("closed with no answer", "closed with no answer",
         lambda: http.client.RemoteDisconnected("Remote end closed connection without response")),
        ("its answer cut short", "cut short", lambda: http.client.IncompleteRead(b"", 64)),
        ("its answer unreadable", "unreadable",
         lambda: http.client.HTTPException("got more than 100 headers")),
        ("timed out reading", "timed out", lambda: TimeoutError("timed out")),
    ]


TLS: dict = {}


def socket_failures() -> list:
    """(what happened, words the line must carry, make): a socket the
    connector opens itself (Slack's Socket Mode, Discord's gateway)."""
    return [
        ("refused", "connection refused",
         lambda: ConnectionRefusedError(errno.ECONNREFUSED, "Connection refused")),
        ("timed out", "timed out", lambda: TimeoutError("timed out")),
        ("its name not resolved", "did not resolve",
         lambda: socket.gaierror(socket.EAI_NONAME, "Name or service not known")),
        ("cut by a broken pipe", "broken pipe", lambda: BrokenPipeError(errno.EPIPE, "Broken pipe")),
        ("reset", "connection reset",
         lambda: ConnectionResetError(errno.ECONNRESET, "Connection reset by peer")),
        ("its TLS refusing an untrusted certificate", tls_words("untrusted"),
         lambda: TLS["untrusted"]),
        ("its TLS hung up on", tls_words("eof"), lambda: TLS["eof"]),
    ]


def tls_words(kind: str) -> str:
    """What a line about a real TLS failure must carry: OpenSSL's reason."""
    return str(getattr(TLS.get(kind), "reason", None) or "TLS")


def real_tls_failures() -> dict:
    """Two TLS failures made for real over a socket pair: the client refuses
    a certificate no CA it trusts signed, and the other end reads the hello
    and hangs up. Real ones carry what OpenSSL set (reason), which an
    SSLError made in Python does not."""
    import connector_gate_kit as kit
    _ca, server_ctx = kit.make_tls(_TMP.name, ["files.slack.com", "wss-primary.slack.com"])
    out = {}
    for kind in ("untrusted", "eof"):
        here, there = socket.socketpair()

        def serve(there=there, kind=kind):
            with contextlib.suppress(Exception):
                if kind == "untrusted":
                    server_ctx.wrap_socket(there, server_side=True).close()
                else:
                    there.recv(65536)
            with contextlib.suppress(Exception):
                there.close()
        t = threading.Thread(target=serve, daemon=True)
        t.start()
        try:
            ssl.create_default_context().wrap_socket(here, server_hostname="files.slack.com")
            out[kind] = None
        except OSError as e:            # ssl.SSLError is an OSError
            out[kind] = e
        finally:
            here.close()
            t.join(10)
    return out


def gai_failures() -> list:
    """Every getaddrinfo failure, its text as the C library words it."""
    try:
        libc = ctypes.CDLL(None)
        libc.gai_strerror.restype = ctypes.c_char_p
        text = lambda code: libc.gai_strerror(code).decode()  # noqa: E731
    except (OSError, AttributeError):
        text = lambda code: f"getaddrinfo failure {code}"  # noqa: E731
    return [(name, getattr(socket, name), text(getattr(socket, name)))
            for name in sorted(dir(socket)) if name.startswith("EAI_")]


# ── the paths that log a failure ─────────────────────────────────────────

SLACK_WSS = "wss://wss-primary.slack.com/link/?ticket=gate"
HANDSHAKE_500 = b"HTTP/1.1 500 Internal Server Error\r\nContent-Length: 0\r\n\r\n"
TEXT_PART = b'{"type": "text", "seq": 0, "text": "Hello from the Host."}\n'
IMAGE_PART = (b'{"type": "image", "seq": 1, "mime": "image/png", "name": "p", "data_b64": "'
              + base64.b64encode(b"\x89PNG\r\n\x1a\n" + bytes(56)) + b'"}\n')
CHAT = {"telegram": "42", "slack": "D0GATE", "discord": "222222"}
CODE = "BCDF GHJK"    # a pairing code: eight letters of CODE_ALPHABET
PAGE_502 = "<html><body><h1>502 Bad Gateway</h1></body></html>"


def message(app: str, text: str) -> dict:
    """One message from the person, as each connector's poller hands it on."""
    if app == "telegram":
        return {"message_id": 7, "chat": {"id": 42, "type": "private"},
                "from": {"id": 42, "first_name": "Gate"}, "text": text}
    if app == "slack":
        return {"kind": "message", "team": "T0GATE", "user": "U0PERSON", "channel": "D0GATE",
                "ts": "1700000000.000100", "native": None, "text": text, "files": False}
    return {"user": "111111", "channel": "222222", "id": "333333", "name": "Gate",
            "text": text, "files": False, "reply_to": None}


def slack_opened(c) -> None:
    """Slack's side up to the socket: the bot known, a Socket Mode URL given."""
    c.BOT.update(user_id="UBOT", team_id="T0GATE")
    c.slack = lambda method, form=None, *, use="bot", wait=30: {"ok": True, "url": SLACK_WSS}


def loop(app: str, c, fail) -> list[str]:
    """The listening loop, its first try failing with fail(): the lines it
    logged before it would sleep and try again. Telegram's getUpdates and
    Slack's auth.test go through the Host (urllib); Discord's gateway is a
    socket to the Host."""
    clock(c, stop=True)
    if app == "discord":
        c.Socket = raising(fail)
        return said(c.listen_forever)
    c._request = raising(fail)
    if app == "slack":
        c.BOT.clear()
        return said(c.listen_forever)
    return said(c.poll_forever)


def slack_socket(c, fail) -> list[str]:
    """Slack's loop, the Socket Mode socket failing once it is asked for."""
    clock(c, stop=True)
    slack_opened(c)
    c.run_socket = raising(fail)
    return said(c.listen_forever)


def handshake(app: str, c, raw: bytes) -> list[str]:
    """Slack's or Discord's loop, the socket's handshake answered `raw`, read
    by the connector's own Socket."""
    clock(c, stop=True)
    c.socket = module_like(socket, create_connection=answering(raw))
    if app == "slack":
        c.ssl = module_like(ssl, create_default_context=NoTLS)
        slack_opened(c)
    return said(c.listen_forever)


def send(app: str, c, request) -> list[str]:
    """One text part of a reply, its platform call made by `request` (the
    connector's _request: raise, or return an Answer)."""
    clock(c, stop=False)
    c._request = request
    return said(c.read_stream, io.BytesIO(TEXT_PART), c.Turn(CHAT[app]))


def slack_upload(c, fail, where: str) -> list[str]:
    """One picture of a reply: Slack gives the upload URL, then the upload
    fails with fail() at `where` (endheaders connects and handshakes; send
    carries the bytes; getresponse reads the answer)."""
    clock(c, stop=False)
    c._request = lambda *a, **k: Answer(broker(200, {"ok": True, "file_id": "F0GATE",
                                                    "upload_url": "https://files.slack.com/upload/v1/gate"}))

    class Conn:
        def __init__(self, *a, **k):
            pass

        def _at(self, name):
            if name == where:
                raise fail()

        def putrequest(self, *a, **k):
            pass

        def putheader(self, *a, **k):
            pass

        def endheaders(self, *a, **k):
            self._at("endheaders")

        def send(self, _data):
            self._at("send")

        def getresponse(self):
            self._at("getresponse")
            raise AssertionError("the upload was meant to fail")

        def close(self):
            pass
    c.http = types.SimpleNamespace(client=module_like(http.client, HTTPSConnection=Conn))
    return said(c.read_stream, io.BytesIO(IMAGE_PART), c.Turn(CHAT["slack"]))


def host_busy(app: str, c) -> list[str]:
    """A message the Host answers 503, every time it is posted."""
    clock(c, stop=False)

    def request(method, path, body=None, timeout=60.0):
        if path == "/api/connector/messages":
            raise host_error(503, "brain pool not ready")
        return Answer(broker(200, {"ok": True, "user": {"name": "gate"}}))
    c._request = request
    return said(c.on_message, message(app, "Hello"), threading.Event())


def dropped_by_fault(app: str, c) -> list[str]:
    c.handle = raising(lambda: KeyError("chat_id"))
    return said(c.on_message, message(app, "Hello"), threading.Event())


def dropped_pairing(app: str, c) -> list[str]:
    clock(c, stop=False)
    c._request = raising(lambda: urllib.error.URLError(
        ConnectionRefusedError(errno.ECONNREFUSED, "Connection refused")))
    return said(c.on_message, message(app, CODE), threading.Event())


def expect_retry(app: str, label: str, lines: list[str], words: str) -> None:
    """The failure was logged, the line says why, and no line reads as an error."""
    mine = [ln for ln in lines if ln.startswith(f"[{app}-connector] ")]
    errs = {ln: as_error(ln) for ln in lines if as_error(ln)}
    check(f"{app} {label}: the line says why ({words}) and reads as no error",
          any(words.lower() in ln.lower() for ln in mine) and not errs, errs or lines)


def expect_given_up(app: str, label: str, lines: list[str], marker: str) -> None:
    """The failure was logged once and both readers take it for an error."""
    hit = [ln for ln in lines if marker in ln]
    check(f"{app} {label}: reads as an error to both readers",
          len(hit) == 1 and len(as_error(hit[0])) == len(READERS),
          {ln: as_error(ln) for ln in (hit or lines)})


PLATFORM_FAILURES = {
    "telegram": [
        ("Telegram's 502 page, not JSON", "no JSON (502)", broker(502, None, PAGE_502)),
        ("Telegram's own 500", "(500)",
         broker(500, {"ok": False, "error_code": 500, "description": "Internal Server Error"})),
    ],
    "slack": [
        ("Slack's 502 page, not JSON", "no JSON (502)", broker(502, None, PAGE_502)),
        ("Slack's internal_error", "internal_error", broker(200, {"ok": False, "error": "internal_error"})),
        ("Slack's fatal_error", "fatal_error", broker(200, {"ok": False, "error": "fatal_error"})),
    ],
    "discord": [
        ("Discord's 502 page, not JSON", "(502)", broker(502, None, PAGE_502)),
        ("Discord's 503", "(503)", broker(503, {"message": "Service Unavailable"})),
    ],
}


# ── K: the kernel's 502 for a platform call that did not go through ──────

def closed_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


async def kernel_details() -> dict:
    """{platform: (status, detail)} that nac_connector.platform_call answers
    when the platform cannot be reached: the request build_platform_request
    aims at the platform, moved to a closed port on loopback, made by the real
    authz._do_http_call (the path lane for Telegram, the header lane for the
    others). The vault's own open_secret and audit are the only part left out."""
    from server.network import nac_connector as nc
    from server.vault import authz
    port = closed_port()
    real = authz.use_credential

    async def use_credential(pool, *, entity_id, name, action, request=None, actor=None,
                             path_secret=False, **_kw):
        req = dict(request or {})
        req["url"] = re.sub(r"^https://[^/]+", f"https://127.0.0.1:{port}", str(req.get("url")))
        return await authz._do_http_call(b"7123456789:AAGate-token_NotReal42", req,
                                         path_secret=path_secret)
    authz.use_credential = use_credential
    bodies = {"telegram": {"method": "POST", "path": "/getUpdates", "json": {"timeout": 0}},
              "slack": {"method": "POST", "path": "/api/chat.postMessage",
                        "form": {"channel": "D0GATE", "text": "hi"}},
              "discord": {"method": "POST", "path": "/api/v10/channels/222222/messages",
                          "json": {"content": "hi"}}}
    out = {}
    try:
        for platform, body in bodies.items():
            caller = nc.ConnectorCaller(app_id=platform, stages=frozenset({nc.STAGE_ACTIVE}),
                                        platform=platform, token_name=f"{platform}-bot")
            try:
                await nc.platform_call(None, caller, body)
                out[platform] = (None, "the call went through")
            except nc.ConnectorError as e:
                out[platform] = (e.status, e.detail)
    finally:
        authz.use_credential = real
    return out


# ── the run ──────────────────────────────────────────────────────────────

def run_app(app: str, kd: dict) -> None:
    print(f"[{app}] system-apps/{app}/connector.py")
    # A: the listening loop.
    c = load(app)
    for what, words, make in (socket_failures() if app == "discord" else urllib_failures()):
        expect_retry(app, f"A the loop, its call {what}", loop(app, c, make), words)
    if app == "slack":
        for what, words, make in socket_failures():
            expect_retry(app, f"A the loop, its Socket Mode socket {what}",
                         slack_socket(load(app), make), words)
    if app in ("slack", "discord"):
        expect_retry(app, "A the loop, its socket's handshake answered 500 Internal Server Error",
                     handshake(app, load(app), HANDSHAKE_500), "500")

    # B: a part's send.
    c = load(app)
    host_502 = ("the Host's 502 for a platform call it could not make", "502",
                lambda: host_error(502, kd[app][1]))
    for what, words, make in urllib_failures() + [host_502]:
        expect_retry(app, f"B a send, its call {what}", send(app, c, raising(make)), words)
    for what, words, answer in PLATFORM_FAILURES[app]:
        expect_retry(app, f"B a send, {what}",
                     send(app, c, lambda *a, answer=answer, **k: Answer(answer)), words)

    # C: Slack's picture upload.
    if app == "slack":
        for what, words, where, make in (
                ("cut by a broken pipe", "broken pipe", "send",
                 lambda: BrokenPipeError(errno.EPIPE, "Broken pipe")),
                ("reset", "connection reset", "send",
                 lambda: ConnectionResetError(errno.ECONNRESET, "Connection reset by peer")),
                ("closed with no answer", "closed with no answer", "getresponse",
                 lambda: http.client.RemoteDisconnected("Remote end closed connection without response")),
                ("its TLS refusing an untrusted certificate", tls_words("untrusted"), "endheaders",
                 lambda: TLS["untrusted"]),
                ("its TLS hung up on", tls_words("eof"), "endheaders", lambda: TLS["eof"])):
            expect_retry(app, f"C a picture's upload {what}", slack_upload(load(app), make, where), words)

    # D: the Host answering 503 to a message; G: the reply let go.
    lines = host_busy(app, load(app))
    expect_retry(app, "D a message the Host answers 503, posted again",
                 [ln for ln in lines if "let go" not in ln], "503")
    expect_given_up(app, f"G a reply let go after {load(app).REATTACH_TRIES} tries that got nothing",
                    lines, "let go")

    # G: a message dropped.
    expect_given_up(app, "G a message dropped by the connector's own fault",
                    dropped_by_fault(app, load(app)), "dropped")
    expect_given_up(app, "G a message dropped, the Host unreachable while a pairing code is reported",
                    dropped_pairing(app, load(app)), "dropped")

    # E: every errno and getaddrinfo failure, in the loop and in a send.
    c_loop, c_send = load(app), load(app)
    clock(c_send, stop=False)
    plants = []
    for n in sorted(errno.errorcode):
        raw = (lambda n=n: OSError(n, os.strerror(n)))
        plants.append((errno.errorcode[n], raw,
                       lambda raw=raw: urllib.error.URLError(raw())))
    for name, code, text in gai_failures():
        raw = (lambda code=code, text=text: socket.gaierror(code, text))
        plants.append((name, raw, lambda raw=raw: urllib.error.URLError(raw())))
    bad, logged = [], 0
    for name, raw, wrapped in plants:
        for make in ((raw,) if app == "discord" else (raw, wrapped)):
            lines = loop(app, c_loop, make)
            logged += bool(lines)
            bad += [(name, ln) for ln in lines if as_error(ln)]
        for make in (raw, wrapped):
            lines = said(c_send._failed, make())
            logged += bool(lines)
            bad += [(name, ln) for ln in lines if as_error(ln)]
    check(f"{app} E every errno ({len(errno.errorcode)}) and getaddrinfo failure, in the loop and "
          f"in a send, reads as no error ({logged} lines)", not bad and logged, bad[:6])


def main() -> int:
    print("[K] the kernel's 502 for a platform call that did not go through")
    kd = asyncio.run(kernel_details())
    for platform, (status, detail) in kd.items():
        check(f"K {platform}: the Host answers 502 ({detail!r}) and the detail reads as no error",
              status == 502 and not as_error(detail), (status, detail, as_error(detail)))

    TLS.update(real_tls_failures())
    # A refused certificate always carries OpenSSL's reason; a hang-up does
    # from OpenSSL 3 (UNEXPECTED_EOF_WHILE_READING), and cause() words it
    # either way.
    check("the TLS failures are real ones, the certificate's with OpenSSL's reason",
          all(isinstance(e, ssl.SSLError) for e in TLS.values())
          and bool(getattr(TLS.get("untrusted"), "reason", None)),
          {k: repr(e) for k, e in TLS.items()})
    for app in APPS:
        run_app(app, kd)
    for s in KEEP:
        with contextlib.suppress(OSError):
            s.close()

    leftover = [str(p) for app in APPS for p in (ROOT / "system-apps" / app).glob("__pycache__")]
    check("no bytecode left in system-apps/<id>/ (the packer signs the directory whole)",
          not leftover, leftover)
    print(f"\n{counted[0]} checks, {len(fails)} failure(s)")
    if fails:
        print("FAILED: " + "; ".join(fails[:12]) + (" ..." if len(fails) > 12 else ""))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
