#!/usr/bin/env python3
"""Gate: a connector sends a picture through POST /api/connector/platform
(the connector sends the image parts of a reply; Telegram takes them as
multipart/form-data).

What it proves, through the real request builder
(nac_connector.build_platform_request) and the real broker transport
(vault/authz._do_http_call, path mode for Telegram):

  - `files` makes the call multipart/form-data, built by the kernel: one part
    per form field, one per file, with the field name the platform reads
    (Telegram sendPhoto: "photo"; sendDocument: "document"), the file name,
    its media type and its exact bytes; JSON form values (a media group's
    `media`) are serialised;
  - the request still goes to the platform's API host only, with the token in
    the path and nowhere in the body, the headers, or a refusal's text;
  - the bounds hold: at most 10 files, a file at most 10 MiB (Telegram's
    photo limit, https://core.telegram.org/bots/api#sending-files), 12 MiB in
    all; a field or file name a Content-Disposition line would need to
    escape, a media type that is not one, data that is not base64, and json
    beside files are refused;
  - the route reads a files call under its own bound and keeps the old 16 KiB
    bound for a call without files;
  - the wait a rate-limited platform asks for comes back as `retry_after`
    (nac_connector.retry_after_s): Slack's Retry-After header, Discord's 429
    body field, Telegram's parameters.retry_after.

Pure: aiohttp's ClientSession is a recording fake; no network, no DB.
Run: python3 scripts/test_connector_platform_upload.py
"""
from __future__ import annotations

import asyncio
import base64
import email.parser
import email.policy
import json
import os
import sys
from pathlib import Path

ROOT = Path(os.environ.get("APP_ROOT") or Path(__file__).resolve().parents[1])
sys.path[:0] = [str(ROOT), str(ROOT / "brain")]

FAILS: list[str] = []


def check(label: str, ok: bool, detail=None) -> None:
    print(("  ok   " if ok else "  FAIL ") + label + ("" if ok or detail is None else f"\n         {detail!r}"[:600]))
    if not ok:
        FAILS.append(label)


TOKEN = "123456789:AAH-secret_tokenValue"
PNG = b"\x89PNG\r\n\x1a\n" + bytes(range(256)) * 40


class _Content:
    """A stream, as aiohttp's StreamReader is: each read takes what it returns,
    and the end is an empty read."""
    def __init__(self, data: bytes):
        self._data = data

    async def read(self, n=-1):
        out = self._data if n is None or n < 0 else self._data[:n]
        self._data = self._data[len(out):]
        return out


class _Resp:
    def __init__(self, status=200, text='{"ok":true,"result":{"message_id":77}}', headers=None):
        self.status = status
        self._text = text
        self.content = _Content(text.encode())
        if headers is not None:
            self.headers = headers

    async def text(self):
        return self._text

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class _Session:
    calls: list = []

    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    answer: dict = {}       # the next answer's {status, text, headers}, else the default

    def request(self, method, url, **kw):
        _Session.calls.append({"method": method, "url": str(url), **kw})
        a, _Session.answer = _Session.answer, {}
        return _Resp(**a) if a else _Resp()


import aiohttp  # noqa: E402
aiohttp.ClientSession = _Session

from server.network import nac_connector as nc  # noqa: E402
from server.vault import authz  # noqa: E402

CALLER = nc.ConnectorCaller(app_id="tg-conn", stages=frozenset({nc.STAGE_ACTIVE}), grant_id=1,
                            platform="telegram", account_id="42", token_name="tg-bot")


def parts_of(body: bytes, ctype: str) -> list:
    msg = email.parser.BytesParser(policy=email.policy.HTTP).parsebytes(
        b"Content-Type: " + ctype.encode() + b"\r\n\r\n" + body)
    return list(msg.iter_parts())


def refused(body: dict, status: int, words: str) -> None:
    try:
        nc.build_platform_request(CALLER, body)
        check(f"refused ({words})", False, "built")
    except nc.ConnectorError as e:
        check(f"refused ({words}): {e.status}", e.status == status and TOKEN not in str(e.detail),
              (e.status, e.detail))


print("1. sendPhoto with a picture: multipart, built by the kernel")
try:
    name, req = nc.build_platform_request(CALLER, {
        "method": "POST", "path": "/sendPhoto",
        "form": {"chat_id": "42", "caption": "Weekly spend"},
        "files": [{"field": "photo", "filename": "weekly-spend.png", "mime": "image/png",
                   "data_b64": base64.b64encode(PNG).decode()}]})
except Exception as e:  # noqa: BLE001 — red before the build: report, and stop
    check("a files call is built", False, repr(e))
    print("\nRESULT: 1 FAIL (no multipart support)")
    sys.exit(1)
check("the grant's token name is the one opened", name == "tg-bot", name)
ctype = (req.get("headers") or {}).get("Content-Type", "")
check("the Content-Type is multipart/form-data with a boundary",
      ctype.startswith("multipart/form-data; boundary=cseries-"), ctype)
check("the body is bytes (aiohttp sends it as it is)", isinstance(req.get("body"), bytes), type(req.get("body")))
if not isinstance(req.get("body"), bytes) or not ctype:
    print("\nRESULT: FAIL (no multipart body)")
    sys.exit(1)
ps = parts_of(req["body"], ctype)
by = {p.get_param("name", header="content-disposition"): p for p in ps}
check("a part per form field and per file", set(by) == {"chat_id", "caption", "photo"}, list(by))
check("the form fields carry their values",
      by["chat_id"].get_content().strip() == "42" and by["caption"].get_content().strip() == "Weekly spend",
      [by["chat_id"].get_content(), by["caption"].get_content()])
photo = by["photo"]
check("the file part is named `photo` with its filename and media type",
      photo.get_filename() == "weekly-spend.png" and photo.get_content_type() == "image/png",
      (photo.get_filename(), photo.get_content_type()))
check("the file's bytes arrive exactly", photo.get_payload(decode=True) == PNG,
      len(photo.get_payload(decode=True) or b""))
check("the token is nowhere in the body or the headers",
      TOKEN.encode() not in req["body"] and TOKEN not in json.dumps(req.get("headers")))
check("the url is the platform's host with the placeholder in the path",
      req["url"] == "https://api.telegram.org/bot{secret}/sendPhoto" and req["auth_scheme"] == "path",
      req["url"])

print("2. through the broker's transport")
_Session.calls.clear()
out = asyncio.run(authz._do_http_call(TOKEN.encode(), req, path_secret=True))
sent = _Session.calls[-1] if _Session.calls else {}
check("one request, POST, to api.telegram.org with the token in the path only",
      len(_Session.calls) == 1 and sent.get("method") == "POST"
      and sent.get("url") == f"https://api.telegram.org/bot{TOKEN}/sendPhoto", sent.get("url"))
check("the multipart body and its Content-Type reach aiohttp unchanged",
      sent.get("data") == req["body"] and (sent.get("headers") or {}).get("Content-Type") == ctype)
check("no redirect is followed", sent.get("allow_redirects") is False, sent.get("allow_redirects"))
check("the platform's answer comes back", out.get("json", {}).get("result", {}).get("message_id") == 77, out)

print("3. a media group: JSON form values serialised, two files by attach:// name")
name, req = nc.build_platform_request(CALLER, {
    "path": "/sendMediaGroup",
    "form": {"chat_id": "42", "media": [{"type": "photo", "media": "attach://a"},
                                        {"type": "photo", "media": "attach://b"}]},
    "files": [{"field": "a", "filename": "a.png", "mime": "image/png",
               "data_b64": base64.b64encode(PNG).decode()},
              {"field": "b", "filename": "b.png", "mime": "image/png",
               "data_b64": base64.b64encode(PNG[::-1]).decode()}]})
ps = parts_of(req["body"], req["headers"]["Content-Type"])
by = {p.get_param("name", header="content-disposition"): p for p in ps}
check("the media field is the JSON Telegram reads",
      json.loads(by["media"].get_content())[1]["media"] == "attach://b", by["media"].get_content())
check("each file under its attach name, bytes intact",
      by["a"].get_payload(decode=True) == PNG and by["b"].get_payload(decode=True) == PNG[::-1])

print("4. Discord's files[n] field names pass; the header token stays out of the body")
dc = nc.ConnectorCaller(app_id="dc", stages=frozenset({nc.STAGE_ACTIVE}), grant_id=2,
                        platform="discord", account_id="123456", token_name="dc-bot")
name, req = nc.build_platform_request(dc, {
    "path": "/api/v10/channels/1/messages", "form": {"payload_json": {"content": "hi"}},
    "files": [{"field": "files[0]", "filename": "card.png", "mime": "image/png",
               "data_b64": base64.b64encode(PNG).decode()}]})
check("discord multipart goes to discord.com with a Bot header scheme",
      req["url"] == "https://discord.com/api/v10/channels/1/messages" and req["auth_prefix"] == "Bot "
      and "files[0]" in req["body"].decode("latin-1"), req["url"])

print("5. the bounds")
b64 = base64.b64encode(PNG).decode()
one = {"field": "photo", "filename": "p.png", "mime": "image/png", "data_b64": b64}
refused({"path": "/sendPhoto", "files": [dict(one, field=f"f{i}") for i in range(11)]}, 413,
        "eleven files")
big = base64.b64encode(b"\0" * (nc.PLATFORM_FILE_MAX + 1)).decode()
refused({"path": "/sendPhoto", "files": [dict(one, data_b64=big)]}, 413, "a file over 10 MiB")
seven = base64.b64encode(b"\0" * (7 * 1024 * 1024)).decode()
refused({"path": "/sendPhoto", "files": [dict(one, field="a", data_b64=seven),
                                         dict(one, field="b", data_b64=seven)]}, 413,
        "two 7 MiB files: over 12 MiB in all")
refused({"path": "/sendPhoto", "files": [dict(one, field='photo"; filename="x')]}, 400,
        "a quote in the field name")
refused({"path": "/sendPhoto", "files": [dict(one, filename="a\r\nX-Evil: 1.png")]}, 400,
        "CR/LF in the file name")
refused({"path": "/sendPhoto", "files": [dict(one, filename="../../etc/passwd")]}, 400,
        "a path as the file name")
refused({"path": "/sendPhoto", "files": [dict(one, mime="image/png\r\nX: y")]}, 400,
        "a media type with a header in it")
refused({"path": "/sendPhoto", "files": [dict(one, data_b64="not base64!!")]}, 400,
        "data that is not base64")
refused({"path": "/sendPhoto", "json": {"chat_id": 1}, "files": [one]}, 400, "json beside files")
refused({"path": "/sendPhoto", "files": []}, 400, "an empty file list")
refused({"path": "/sendPhoto", "form": {'a"b': "x"}, "files": [one]}, 400, "a quote in a form name")
refused({"path": "/file/bot/x", "files": [one]}, 400, "a file-download path")

print("6. the route's bounds")
from server.routes import connector as route  # noqa: E402
check("a files call is read under a bound that fits 12 MiB of base64",
      route._PLATFORM_BODY_MAX >= (nc.PLATFORM_FILES_TOTAL_MAX * 4) // 3
      and route._PLATFORM_BODY_MAX < 20 * 1024 * 1024, route._PLATFORM_BODY_MAX)
check("a call without files keeps the 16 KiB bound", route._BODY_MAX == 16 * 1024, route._BODY_MAX)

print("7. the wait a platform asks for comes back as retry_after (seconds)")
_saved_use = authz.use_credential


async def _use(pool, *, entity_id, name, action, request, actor, path_secret=False, **k):
    return await authz._do_http_call(TOKEN.encode(), request, path_secret=path_secret)
authz.use_credential = _use
try:
    SLACK = nc.ConnectorCaller(app_id="sl", stages=frozenset({nc.STAGE_ACTIVE}), grant_id=2,
                               platform="slack", account_id="T1:U1", token_name="slack-token")
    DISCORD = nc.ConnectorCaller(app_id="dc", stages=frozenset({nc.STAGE_ACTIVE}), grant_id=3,
                                 platform="discord", account_id="12345678", token_name="dc-bot")
    _Session.answer = {"status": 429, "text": '{"ok":false,"error":"ratelimited"}',
                       "headers": {"Retry-After": "7"}}
    out = asyncio.run(nc.platform_call(None, SLACK, {"path": "/api/chat.postMessage", "form": {}}))
    check("Slack: its Retry-After header (7) is the answer's retry_after, and the raw header "
          "is not passed on", out.get("status") == 429 and out.get("retry_after") == 7.0
          and "retry_after_header" not in out, out)
    _Session.answer = {"status": 429, "text": '{"message":"You are being rate limited.",'
                                              '"retry_after":1.5,"global":false}',
                       "headers": {}}
    out = asyncio.run(nc.platform_call(None, DISCORD, {"path": "/api/v10/channels/1/messages",
                                                        "json": {"content": "x"}}))
    check("Discord: with no header, the 429 body's retry_after (1.5) is the answer's",
          out.get("retry_after") == 1.5, out)
    _Session.answer = {"status": 429, "text": '{"ok":false,"error_code":429,"description":'
                                              '"Too Many Requests","parameters":{"retry_after":5}}'}
    out = asyncio.run(nc.platform_call(None, CALLER, {"path": "/sendMessage", "json": {}}))
    check("Telegram (path mode, a response with no headers at all): parameters.retry_after (5) "
          "is the answer's", out.get("retry_after") == 5.0, out)
    out = asyncio.run(nc.platform_call(None, CALLER, {"path": "/sendMessage", "json": {}}))
    check("an answer that asks for no wait carries no retry_after", "retry_after" not in out, out)
    check("an HTTP-date Retry-After is read as the seconds until then, and a wait is bounded to an "
          "hour", 0 <= nc.retry_after_s({"retry_after_header": "Wed, 21 Oct 2015 07:28:00 GMT"}) <= 1
          and nc.retry_after_s({"retry_after_header": "99999"}) == nc.RETRY_AFTER_MAX_S
          and nc.retry_after_s({"retry_after_header": "soon"}) is None)
finally:
    authz.use_credential = _saved_use

print()
print("RESULT: " + ("ALL PASS" if not FAILS else f"{len(FAILS)} FAIL: {FAILS}"))
sys.exit(1 if FAILS else 0)
