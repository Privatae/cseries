#!/usr/bin/env python3
"""Gate: the vault broker puts a secret into a URL PATH only for a binding that
declares it, only into the path, and never into a query, a body, a header, an
error or a log.

Telegram's Bot API takes its token in the path (/bot<token>/METHOD —
https://core.telegram.org/bots/api). server/vault/authz.py's http_call gains
auth_scheme='path' for it, honoured only when the kernel caller passes
path_secret=True (no `request` field can), which only the connector binding
does (server/network/nac_connector.platform_call). Discord and Slack keep the
header (Authorization: Bot <token> / Bearer <token>).

Also pins the connector's platform request: the host is always the platform's
API host, the token is the grant's, and a path cannot smuggle a query, a
traversal or a file download.

Pure: aiohttp's ClientSession is a recording fake; no network, no DB.
Run: python3 scripts/test_vault_path_secret.py
"""
from __future__ import annotations

import asyncio
import io
import logging
import os
import sys
import traceback
from pathlib import Path

ROOT = Path(os.environ.get("APP_ROOT") or Path(__file__).resolve().parents[1])
sys.path[:0] = [str(ROOT), str(ROOT / "brain")]

failures: list[str] = []


def check(label: str, ok: bool, detail=None) -> None:
    print(("  OK   " if ok else "  FAIL ") + label + (f"\n         {detail!r}"[:500] if detail is not None and not ok else ""))
    if not ok:
        failures.append(label)


TOKEN = "123456789:AAH-secret_tokenValue"

from server.vault import api as vapi, authz  # noqa: E402

# Every log record the broker writes, at every level, for the no-secret-in-logs check.
_logbuf = io.StringIO()
_h = logging.StreamHandler(_logbuf)
_h.setLevel(logging.DEBUG)
logging.getLogger().addHandler(_h)
logging.getLogger().setLevel(logging.DEBUG)


class _Content:
    def __init__(self, data: bytes):
        self._data = data
        self.asked: list = []

    async def read(self, n=-1):
        # A stream, as aiohttp's StreamReader is: each read takes what it
        # returns, and the end is an empty read.
        self.asked.append(n)
        out = self._data if n is None or n < 0 else self._data[:n]
        self._data = self._data[len(out):]
        return out


class _Resp:
    def __init__(self, status=200, text=""):
        self.status = status
        self._text = text
        self.content = _Content(text.encode())

    async def text(self):
        return self._text

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class _Session:
    calls: list = []
    respond = staticmethod(lambda: _Resp(200, '{"ok":true}'))

    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def request(self, method, url, **kw):
        _Session.calls.append({"method": method, "url": str(url), **kw})
        return _Session.respond()


import aiohttp  # noqa: E402
_REAL_SESSION = aiohttp.ClientSession   # section 5 reads a real socket
aiohttp.ClientSession = _Session  # the broker imports aiohttp inside the call


def run(coro):
    return asyncio.run(coro)


print("1. the placeholder goes into the path, and only the path")
url = authz._path_secret_url("https://api.telegram.org/bot{secret}/getMe", TOKEN)
check("the secret lands in the path segment", url == f"https://api.telegram.org/bot{TOKEN}/getMe", url)
for bad, why in (("https://api.telegram.org/getMe?token={secret}", "in the query"),
                 ("https://api.telegram.org/getMe#{secret}", "in the fragment"),
                 ("https://{secret}.telegram.org/getMe", "in the host"),
                 ("https://api.telegram.org/bot{secret}/x/{secret}", "twice"),
                 ("https://api.telegram.org/getMe", "absent"),
                 ("http://api.telegram.org/bot{secret}/getMe", "over plain http")):
    try:
        authz._path_secret_url(bad, TOKEN)
        check(f"a placeholder {why} is refused", False)
    except authz.VaultAuthzError:
        check(f"a placeholder {why} is refused", True)
for s in ("abc/def123456", "abc?x=1234567", "abc def12345", "short"):
    try:
        authz._path_secret_url("https://api.telegram.org/bot{secret}/getMe", s)
        check(f"a secret that cannot sit in a path unencoded is refused ({s!r})", False)
    except authz.VaultAuthzError:
        check(f"a secret that cannot sit in a path unencoded is refused ({s!r})", True)

print("2. only a binding that declares it")
_Session.calls.clear()
try:
    run(authz._do_http_call(TOKEN.encode(), {"url": "https://api.telegram.org/bot{secret}/getMe",
                                             "auth_scheme": "path"}))
    check("auth_scheme='path' without path_secret=True is refused", False)
except authz.VaultAuthzError:
    check("auth_scheme='path' without path_secret=True is refused", True)
try:
    run(authz._do_http_call(TOKEN.encode(), {"url": "https://api.telegram.org/bot{secret}/getMe",
                                             "auth_scheme": "bearer"}))
    check("a placeholder in a header-scheme call is refused (not sent as literal text)", False)
except authz.VaultAuthzError:
    check("a placeholder in a header-scheme call is refused (not sent as literal text)", True)
check("…and nothing was sent for either", _Session.calls == [], _Session.calls)


async def _open_secret(pool, *, entity_id, name, actor=None, include_deactivated=False):
    return TOKEN.encode()

audits = []


async def _audit(pool, *, actor, owner, name, detail):
    audits.append(detail)

vapi.open_secret = _open_secret          # the module the broker opens envelopes through
authz._audit_use = _audit
try:
    run(authz.use_credential(None, entity_id=1, name="tg-bot", action="http_call",
                             request={"url": "https://api.telegram.org/bot{secret}/getMe",
                                      "auth_scheme": "path"}))
    check("a tool's request (use_credential with no path_secret) cannot ask for path mode", False)
except authz.VaultAuthzError:
    check("a tool's request (use_credential with no path_secret) cannot ask for path mode", True)

print("3. the call itself: secret in the path only; the body comes back scrubbed")
_Session.calls.clear()
_Session.respond = staticmethod(lambda: _Resp(200, '{"ok":false,"description":"bot ' + TOKEN + ' blocked"}'))
out = run(authz.use_credential(
    None, entity_id=1, name="tg-bot", action="http_call", path_secret=True,
    request={"url": "https://api.telegram.org/bot{secret}/sendMessage?chat_id=42",
             "auth_scheme": "path", "json": {"chat_id": 42, "text": "hi"}, "allow_redirects": True}))
call = _Session.calls[-1]
from urllib.parse import urlsplit  # noqa: E402
parts = urlsplit(call["url"])
check("the request went to the platform's host with the token in the path",
      parts.netloc == "api.telegram.org" and parts.path == f"/bot{TOKEN}/sendMessage", call["url"])
check("the query, the headers and the body carry no token",
      TOKEN not in parts.query and TOKEN not in repr(call.get("headers"))
      and TOKEN not in repr(call.get("json")) and TOKEN not in repr(call.get("data")), call)
check("redirects are never followed in path mode, whatever the request says",
      call.get("allow_redirects") is False, call.get("allow_redirects"))
check("a body that echoes the token comes back with it cut out",
      TOKEN not in repr(out) and "[redacted]" in out["body"], out)
_big = _Resp(200, "x" * (authz._MAX_BODY * 3))
_Session.respond = staticmethod(lambda: _big)
out = run(authz.use_credential(None, entity_id=1, name="tg-bot", action="http_call", path_secret=True,
                               request={"url": "https://api.telegram.org/bot{secret}/getUpdates",
                                        "auth_scheme": "path"}))
check("the body is read bounded, never whole, and says when it was cut",
      _big.content.asked == [authz._MAX_BODY + 1] and out["truncated"] is True
      and len(out["body"]) == authz._MAX_BODY, (_big.content.asked, out["truncated"], len(out["body"])))
check("the audit row names the host and the template, never the token",
      audits and TOKEN not in repr(audits[-1]) and audits[-1].get("host") == "api.telegram.org"
      and audits[-1].get("path_secret") is True, audits[-1:])


class _Boom(Exception):
    pass


def _raising():
    raise _Boom("Cannot connect to " + f"https://api.telegram.org/bot{TOKEN}/getMe")

_Session.respond = staticmethod(_raising)
try:
    run(authz.use_credential(None, entity_id=1, name="tg-bot", action="http_call", path_secret=True,
                             request={"url": "https://api.telegram.org/bot{secret}/getMe",
                                      "auth_scheme": "path"}))
    check("a failed call raises", False)
except authz.VaultAuthzError as e:
    tb = "".join(traceback.format_exception(type(e), e, e.__traceback__))
    check("a failed call says which host and what kind of failure, never the URL",
          TOKEN not in str(e) and TOKEN not in tb and "api.telegram.org" in str(e) and "_Boom" in str(e),
          (str(e), tb[-300:]))
check("nothing the broker logged carries the token", TOKEN not in _logbuf.getvalue())

print("4. the connector's platform request: the host and the token are not the connector's choice")
from server.network import nac_connector as nc  # noqa: E402


def caller(platform, token="tg-bot", app_token=None):
    return nc.ConnectorCaller(app_id="c1", stages=frozenset({"active"}), grant_id=1, platform=platform,
                              account_id="1", token_name=token, app_token_name=app_token)


name, req = nc.build_platform_request(caller("telegram"), {"path": "/sendMessage", "query": {"chat_id": 42},
                                                           "json": {"text": "hi"}})
check("Telegram: the grant's token, the API host, the token placeholder in the path only",
      name == "tg-bot" and req["auth_scheme"] == "path"
      and req["url"] == "https://api.telegram.org/bot{secret}/sendMessage?chat_id=42"
      and req["allow_redirects"] is False, req)
name, req = nc.build_platform_request(caller("discord"), {"path": "/api/v10/channels/1/messages",
                                                          "json": {"content": "hi"}})
check("Discord: Authorization: Bot <token> on discord.com",
      req["url"] == "https://discord.com/api/v10/channels/1/messages" and req["auth_scheme"] == "header"
      and req["auth_prefix"] == "Bot " and req["auth_header"] == "Authorization", req)
name, req = nc.build_platform_request(caller("slack", app_token="slack-app"),
                                      {"path": "/api/apps.connections.open", "use": "app"})
check("Slack: the app-level token for apps.connections.open, as a bearer on slack.com",
      name == "slack-app" and req["url"] == "https://slack.com/api/apps.connections.open"
      and req["auth_scheme"] == "bearer", (name, req))
for c, body, why in (
        (caller("telegram"), {"path": "/../x"}, "a traversal"),
        (caller("telegram"), {"path": "/getMe?offset=1"}, "a query smuggled into the path"),
        (caller("telegram"), {"path": "//evil.example/x"}, "a second host"),
        (caller("telegram"), {"path": "/file/abc"}, "a file download (not brokered yet)"),
        (caller("telegram"), {"path": "/getMe", "use": "app"}, "Slack's app token on Telegram"),
        (caller("slack", app_token="a"), {"path": "/api/chat.postMessage", "use": "app"},
         "the app-level token for anything but apps.connections.open"),
        (caller("discord"), {"path": "/users/@me"}, "a Discord path outside /api/"),
        (caller("telegram", token=None), {"path": "/getMe"}, "a grant with no token named"),
        (caller("telegram"), {"path": "/getMe", "method": "DELETE"}, "a method other than GET/POST")):
    try:
        nc.build_platform_request(c, body)
        check(f"refused: {why}", False)
    except nc.ConnectorError:
        check(f"refused: {why}", True)

seen = {}


async def _use(pool, **kw):
    seen.update(kw)
    return {"status": 200, "body": "", "json": None, "truncated": False}

authz.use_credential = _use
run(nc.platform_call(None, caller("telegram"), {"path": "/getMe"}))
check("the binding is what turns path mode on", seen.get("path_secret") is True and seen.get("name") == "tg-bot", seen)
seen.clear()
run(nc.platform_call(None, caller("slack"), {"path": "/api/auth.test"}))
check("…and only for a platform that declares it", seen.get("path_secret") is False, seen)

print("5. a body that arrives in pieces is read whole (a real socket, on loopback)")
# aiohttp's StreamReader.read(n) answers with what has ARRIVED, up to n. The
# recording fake above hands the whole body to one read, so it cannot see a
# body cut at its first piece; a real server writing in pieces can.
from aiohttp import web  # noqa: E402

os.environ["NO_PROXY"] = os.environ["no_proxy"] = "127.0.0.1,localhost"
_PIECES = ['{"ok":true,"result":[', ",".join('{"update_id":%d,"text":"%s"}' % (i, "x" * 200)
                                            for i in range(60)),
           ',{"echo":"' + TOKEN + '"}]}']
_BIG = "y" * (authz._MAX_BODY + 5000)


async def _pieces(request):
    resp = web.StreamResponse(headers={"Content-Type": "application/json"})
    await resp.prepare(request)
    for p in (_PIECES if request.path.endswith("/pieces") else [_BIG[:30000], _BIG[30000:]]):
        await resp.write(p.encode())
        await asyncio.sleep(0.05)
    await resp.write_eof()
    return resp


async def _read_real():
    app = web.Application()
    app.router.add_get("/{tail:.*}", _pieces)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    aiohttp.ClientSession = _REAL_SESSION
    # The platform here is a loopback server, which the broker now refuses as
    # this Host itself (server/vault/destination.py). That refusal has
    # its own gate (test_operator_header_trust.py); this section measures how a
    # body is read, so the destination guard is lifted for it and put back.
    _guard = authz.destination.session_kwargs
    authz.destination.session_kwargs = lambda: {}
    try:
        small = await authz._path_secret_request(
            f"http://127.0.0.1:{port}/bot{TOKEN}/pieces", "GET", {}, {"timeout": 10}, TOKEN)
        big = await authz._path_secret_request(
            f"http://127.0.0.1:{port}/bot{TOKEN}/big", "GET", {}, {"timeout": 10}, TOKEN)
    finally:
        authz.destination.session_kwargs = _guard
        aiohttp.ClientSession = _Session
        await runner.cleanup()
    return small, big

small, big = run(_read_real())
check("a body written in three pieces comes back whole and parsed",
      isinstance(small.get("json"), dict) and len(small["json"]["result"]) == 61
      and small["truncated"] is False, {k: small.get(k) for k in ("truncated", "status")})
check("…and the secret is scrubbed from the piece it arrived in",
      TOKEN not in small["body"] and "[redacted]" in small["body"], small["body"][-80:])
check("a body past the bound is cut at the bound and marked truncated",
      big["truncated"] is True and len(big["body"]) == authz._MAX_BODY, (big["truncated"], len(big["body"])))

print("6. a transport failure on the platform call is a 502 that names its type only")


async def _boom(pool, **kw):
    raise RuntimeError(f"Cannot connect to https://discord.com/api/v10/x token={TOKEN}")

authz.use_credential = _boom
for plat, path in (("discord", "/api/v10/x"), ("telegram", "/getMe")):
    try:
        run(nc.platform_call(None, caller(plat), {"path": path}))
        check(f"{plat}: an unexpected transport error is refused", False)
    except nc.ConnectorError as e:
        check(f"{plat}: an unexpected transport error is a 502 naming only its type",
              e.status == 502 and TOKEN not in str(e.detail) and "RuntimeError" in str(e.detail), e.detail)
    except Exception as e:  # noqa: BLE001
        check(f"{plat}: an unexpected transport error is a 502 naming only its type", False, repr(e)[:200])

print(f"\n{'FAILED' if failures else 'PASSED'}: vault path secret ({len(failures)} failure(s))")
sys.exit(1 if failures else 0)
