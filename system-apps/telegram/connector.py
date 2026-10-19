"""Telegram connector: lets ONE Telegram account talk to its Host.

A resident process app. It never holds the bot token and never talks to
Telegram on its own: every Bot API call goes through the Host
(POST /api/connector/platform), which puts the token from Keys into
Telegram's URL. It long-polls getUpdates, hands the paired person's messages
to POST /api/connector/messages, and sends back the parts the Host streams:
text, pictures and links.

Standard library only, so there is nothing to install and no lockfile.

The platform sets PORT, PRIVATAE_APP_ID and CSERIES_KERNEL_URL, and
CSERIES_CONNECTOR_CAP once the Host has started the pairing. What has to
survive a restart (Telegram's offset, the paired account, the replies still
on their way) is one JSON file in the app's data dir.
"""
from __future__ import annotations

import base64
import contextlib
import errno
import html
import http.client
import json
import os
import re
import socket
import socketserver
import ssl
import struct
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

APP_ID = os.environ.get("PRIVATAE_APP_ID", "")
HOST_URL = os.environ.get("CSERIES_KERNEL_URL", "").rstrip("/")
CAP = os.environ.get("CSERIES_CONNECTOR_CAP", "")
PORT = int(os.environ.get("PORT") or "8080")
# The app's data dir survives restarts and re-installs. CONNECTOR_STATE_DIR
# lets a test put it somewhere else.
STATE_DIR = os.environ.get("CONNECTOR_STATE_DIR") or f"/var/lib/cseries/appdata/{APP_ID}"

POLL_S = 30                 # getUpdates long poll, in seconds
POLL_LIMIT = 10             # updates per poll: the Host returns at most 64 KiB of an answer
TEXT_UNITS = 4096           # sendMessage takes 1-4096 characters; counted in UTF-16 units here
TEXT_JSON = 12_000          # a /platform call without files is at most 16 KiB of JSON, in UTF-8 bytes
PHOTO_BYTES = 10_000_000    # sendPhoto: at most 10 MB; a bigger picture goes as a file
PHOTO_SIDES = 10_000        # sendPhoto: width + height at most 10000
PHOTO_RATIO = 20            # sendPhoto: one side at most 20 times the other
FILE_BYTES = 10 * 1024 * 1024   # the Host uploads at most 10 MiB per file (Telegram takes 50 MB)
LINE_MAX = FILE_BYTES * 4 // 3 + 64 * 1024   # the longest line of a reply worth reading whole
HEAD_MAX = 256 * 1024       # a line longer than this is a picture's: the rest is read under PICTURE
FLOOD_WAIT_MAX_S = 300      # flood control: wait as long as Telegram says, up to this, in place
STREAM_READ_S = 60          # the Host sends a line at least every 10 s while a reply runs
ACCEPT_WAIT_S = 120         # how long the poller waits for the Host to take a message
REATTACH_TRIES = 8          # reconnects in a row that get nothing before a reply is let go
REPORT_TRIES = 4            # tries of a report of what was sent before it is let go
# A failure that cannot fix itself is not tried again every few seconds: it
# is logged once as the error it is, shown on /health and the app's page,
# and checked again this often, so a token put right in Keys is picked up
# without a restart. Telegram's answer to getUpdates when the token is not a
# bot's, measured 2026-10-09 against api.telegram.org with made-up tokens:
# 401 "Unauthorized" for a well-formed token, 404 "Not Found" for a malformed
# one. Asking again soon changes neither.
RECHECK_S = 600
TOKEN_REFUSED = (401, 404)
CODE_ALPHABET = "BCDFGHJKLMNPQRSTVWXZ"   # a pairing code is 8 of these

PAIRED_TEXT = ("Code accepted. Now let this account in from your Host: open the Network "
               "app, or the card in your chat, and approve it with your authenticator code.")
WRONG_CODE_TEXT = "That code does not match. Check the code in your Host's Network app."
VOID_TEXT = "That was the fifth wrong code, so this pairing is closed. Ask your Host to start it again."
EXPIRED_TEXT = "That code has expired. Ask your Host to start the pairing again."
UNPAIRED_TEXT = "To pair this bot with your Host, send it the code your Host's Network app shows."
NOT_YET_TEXT = ("Your Host has not let this account in yet, so this message was not passed on. "
                "Send the code from the Network app if you have not, approve the account there, "
                "then send your message again.")
TOO_BIG_TEXT = "A picture in this reply is too large to send here; it is in your Host."
HELLO_TEXT = ("This bot talks to your Host. Send a message to start. /threads lists your "
              "threads, /thread <number> switches to one, /new starts a new one.")


def say(line: str) -> None:
    print(f"[telegram-connector] {line}", flush=True)


# Plain words for the ways a connection fails; any other errno goes by its
# name (EPROTO, ...).
_ERRNO_WORDS = {
    errno.ECONNREFUSED: "connection refused", errno.ECONNRESET: "connection reset",
    errno.ECONNABORTED: "connection aborted", errno.EPIPE: "broken pipe",
    errno.ETIMEDOUT: "timed out", errno.EHOSTUNREACH: "host unreachable",
    errno.ENETUNREACH: "network unreachable", errno.ENETDOWN: "network down",
    errno.ENOTCONN: "not connected",
}
_EAI_NAMES = {getattr(socket, n): n for n in dir(socket) if n.startswith("EAI_")}


def cause(e: BaseException) -> str:
    """Why a call did not go through, in this file's own words. A line that
    says the connector is trying again must not read as an error to the
    Host's app proof (brain/mastercode/runtime/tools.py _LOG_ERROR_RE:
    traceback, exception, error, failed, fatal), and Python's text for these
    failures often does: "<urlopen error [Errno 111] Connection refused>",
    "certificate verify failed", "HTTPException", "[Errno 71] Protocol error"."""
    if isinstance(e, urllib.error.HTTPError):
        return f"HTTP {e.code}"
    if isinstance(e, urllib.error.URLError):
        return cause(e.reason) if isinstance(e.reason, BaseException) else str(e.reason)
    if isinstance(e, ssl.SSLError):                 # an OSError too
        reason = getattr(e, "reason", None)         # OpenSSL's, e.g. CERTIFICATE_VERIFY_FAILED
        return f"TLS did not complete ({reason})" if reason else "TLS did not complete"
    if isinstance(e, socket.gaierror):
        return f"the name did not resolve ({_EAI_NAMES.get(e.errno, e.errno)})"
    if isinstance(e, TimeoutError):
        return "timed out"
    if isinstance(e, http.client.RemoteDisconnected):   # a ConnectionResetError too
        return "closed with no answer"
    if isinstance(e, http.client.IncompleteRead):
        return "cut short"
    if isinstance(e, http.client.HTTPException):
        return "unreadable"
    if isinstance(e, OSError) and e.errno is not None:
        return _ERRNO_WORDS.get(e.errno) or errno.errorcode.get(e.errno, f"errno {e.errno}")
    return str(e) or type(e).__name__


# ── what survives a restart ──────────────────────────────────────────────

class State:
    def __init__(self, path: str):
        self.path = path
        self.lock = threading.RLock()
        data = {}
        with contextlib.suppress(OSError, ValueError):
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
        self.offset = data.get("offset")
        # The account the Host takes messages from, and the one whose code
        # matched and waits for the person's approval. Messages from either
        # go to the Host, which decides; anyone else's are dropped here.
        self.account = data.get("account")
        self.pending = data.get("pending")
        self.turns = data.get("turns") or {}

    def save(self) -> None:
        with self.lock:
            snap = json.dumps({"offset": self.offset, "account": self.account,
                               "pending": self.pending, "turns": self.turns})
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                fh.write(snap)
            os.replace(tmp, self.path)


STATE = State(os.path.join(STATE_DIR, "telegram-connector.json"))
# What /health and the app's page show. `error`: the failure that cannot fix
# itself, while it lasts (broken()); any other phase ends it.
PHASE = {"text": "starting", "error": ""}


def set_phase(text: str) -> None:
    if PHASE["text"] != text:
        PHASE.update(text=text, error="")
        say(text)


def broken(why: str, status: str) -> int:
    """A failure that cannot fix itself: logged as the error it is when it
    starts or changes, never on each check, and shown on /health (ok false)
    and the app's page until it ends. How long to wait before checking again."""
    if PHASE["error"] != why:
        say(f"connecting to Telegram failed: {status}. This does not fix itself; "
            f"checking again every {RECHECK_S // 60} minutes")
    PHASE.update(text=f"{status}. Checked again every {RECHECK_S // 60} minutes.", error=why)
    return RECHECK_S


def mended() -> None:
    """The failure broken() reported is over: the call it refused went through."""
    if PHASE["error"]:
        say(f"connected again: the refusal ({PHASE['error']}) is over")
        PHASE["error"] = ""


# ── the Host ─────────────────────────────────────────────────────────────

# The Host is reached directly, never through the pod's web proxy.
_DIRECT = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class Refused(Exception):
    """The Host answered 403: this connector is not paired, or was revoked."""


_SLOT = "\x00"       # where a file's base64 goes in a call's JSON (written as \u0000)


def _encode(body) -> list:
    """A call's JSON as the pieces sent one after another. A file's base64
    (bytes, up to 14 MB of it) goes as it is, never copied into one string
    with the rest: the JSON is written with a placeholder in its place, which
    is then cut out. The file is the call's last value (telegram() puts it
    there), so the placeholder is the last one in the text."""
    files = body.get("files") if isinstance(body, dict) else None
    blob = files[-1].get("data_b64") if files else None
    if not isinstance(blob, (bytes, bytearray)):
        return [json.dumps(body).encode("utf-8")]
    shell = dict(body, files=files[:-1] + [dict(files[-1], data_b64=_SLOT)])
    before, _, after = json.dumps(shell).encode("utf-8").rpartition(b'"\\u0000"')
    return [before, b'"', blob, b'"' + after]


def _request(method: str, path: str, body=None, timeout: float = 60.0):
    data = None if body is None else _encode(body)
    req = urllib.request.Request(HOST_URL + path, data=data, method=method)
    req.add_header("X-CSeries-App-Id", APP_ID)
    req.add_header("X-CSeries-Connector-Cap", CAP)
    if data is not None:
        req.add_header("Content-Type", "application/json")
        req.add_header("Content-Length", str(sum(len(piece) for piece in data)))
    return _DIRECT.open(req, timeout=timeout)


def _loads(raw):
    try:
        return json.loads(raw or b"null")
    except ValueError:
        return None


def host_json(method: str, path: str, body=None, timeout: float = 60.0):
    """(HTTP status, the JSON answer). Raises OSError when the Host cannot be
    reached at all."""
    try:
        with _request(method, path, body, timeout) as resp:
            return resp.status, _loads(resp.read())
    except urllib.error.HTTPError as e:
        with e:
            return e.code, _loads(e.read())
    except http.client.HTTPException as e:
        raise OSError(f"broken answer from the Host: {cause(e)}") from None


def detail(answer) -> str:
    return str(answer.get("detail") or "") if isinstance(answer, dict) else ""


def telegram(method: str, payload=None, files=None, wait: int = 30) -> dict:
    """One Bot API call through the Host. Returns Telegram's own answer,
    {"ok": true, "result": ...} or {"ok": false, "description": ...}. Raises
    Refused on 403, and OSError when the call could not be made now (worth
    trying again later): the Host busy (429) or failing (5xx), or Telegram
    failing (5xx, or an answer that is not JSON)."""
    body = {"method": "POST", "path": "/" + method, "timeout": wait}
    if files:
        body["form"], body["files"] = payload or {}, files
    else:
        body["json"] = payload or {}
    answer: dict = {"ok": False, "description": "no answer"}
    for _ in range(3):
        status, out = host_json("POST", "/api/connector/platform", body, timeout=wait + 20)
        if status == 403:
            raise Refused(detail(out))
        if status == 429 or status >= 500:
            raise OSError(f"the Host could not make the {method} call now ({status}: {detail(out)})")
        if status != 200 or not isinstance(out, dict):
            return {"ok": False, "description": f"the Host refused {method} ({status}: {detail(out)})"}
        answer = out.get("json")
        if not isinstance(answer, dict):
            if out.get("truncated"):
                return {"ok": False, "description": "an answer too large to read",
                        "truncated": True, "body": str(out.get("body") or "")}
            raise OSError(f"Telegram answered {method} with no JSON ({out.get('status')})")
        code = answer.get("error_code")
        if answer.get("ok") is False and isinstance(code, int) and code >= 500:
            raise OSError(f"Telegram could not take {method} now ({code})")
        # How long to wait: the Host's `retry_after` (the platform's
        # Retry-After header, else Telegram's own parameters.retry_after),
        # else that field read here.
        retry = out.get("retry_after")
        if not isinstance(retry, (int, float)) or isinstance(retry, bool):
            retry = (answer.get("parameters") or {}).get("retry_after")
        if answer.get("ok") is False and isinstance(retry, (int, float)) and retry > 0:
            if retry > FLOOD_WAIT_MAX_S:
                raise OSError(f"Telegram asks to wait {retry} s before {method}")
            time.sleep(retry)       # Telegram's flood control says how long to wait
            continue
        return answer
    return answer


def sent_id(answer: dict):
    result = answer.get("result") if answer.get("ok") else None
    mid = result.get("message_id") if isinstance(result, dict) else None
    return str(mid) if mid else None


# ── sending to Telegram ──────────────────────────────────────────────────

class Turn:
    """One answer of the Host on its way to a chat."""

    def __init__(self, chat_id: str, reply_to=None, native=None, turn_id=None, sent=(),
                 sender=None):
        self.chat_id, self.reply_to, self.native = chat_id, reply_to, native
        self.id = turn_id
        self.sent = set(sent)
        self.title = ""
        self.answered = False      # the first message sent answers the person's message
        self.sender = sender       # the account whose message this answers
        self.partial = {}          # seq -> the pieces of a part sent so far, and their ids


def where(turn: Turn, payload: dict) -> dict:
    """The chat, the topic, and (first message only) the message answered."""
    payload["chat_id"] = int(turn.chat_id)
    if turn.native:
        payload["message_thread_id"] = int(turn.native)
    if turn.reply_to and not turn.answered:
        payload["reply_parameters"] = {"message_id": int(turn.reply_to),
                                       "allow_sending_without_reply": True}
        turn.answered = True
    return payload


def _fits(text: str) -> bool:
    # Measured as the Host measures the call: UTF-8 bytes, so a message in
    # Cyrillic or Greek is not cut at half the length Telegram takes.
    return (len(text.encode("utf-16-le", "surrogatepass")) // 2 <= TEXT_UNITS
            and len(json.dumps(text, ensure_ascii=False).encode("utf-8", "surrogatepass"))
            <= TEXT_JSON)


def split_text(text: str) -> list:
    """Pieces Telegram takes as one message each, cut at a paragraph, a line or
    a space where one is near the limit."""
    out, rest = [], text.strip()
    while rest:
        if _fits(rest):
            out.append(rest)
            break
        lo, hi = 1, len(rest)
        while lo < hi:                         # the longest piece that fits
            mid = (lo + hi + 1) // 2
            if _fits(rest[:mid]):
                lo = mid
            else:
                hi = mid - 1
        cut = lo
        for sep in ("\n\n", "\n", " "):
            at = rest.rfind(sep, 0, lo)
            if at > lo // 2:
                cut = at
                break
        out.append(rest[:cut].rstrip())
        rest = rest[cut:].lstrip()
    return [piece for piece in out if piece]


def send_text(turn: Turn, text: str, seq=None) -> list:
    """Send `text` in as many messages as it takes. A part tried again after
    a send that did not go through carries on after the pieces already sent
    (turn.partial), so no piece goes out twice."""
    done = turn.partial.setdefault(seq, {"n": 0, "ids": []}) if seq is not None \
        else {"n": 0, "ids": []}
    for piece in split_text(text)[done["n"]:]:
        answer = telegram("sendMessage", where(turn, {
            "text": piece, "link_preview_options": {"is_disabled": True}}))
        if sent_id(answer):
            done["ids"].append(sent_id(answer))
        else:
            # Not tried again, so worded as the error it is (the app proof reads it).
            say(f"Telegram did not take a message; sending it failed ({answer.get('description')})")
        done["n"] += 1
    return list(done["ids"])


_B64 = re.compile(rb"[A-Za-z0-9+/]+=*")


def b64_size(raw_b64: bytes) -> int:
    return len(raw_b64) * 3 // 4 - raw_b64[-2:].count(b"=")


def photo_ok(raw_b64: bytes, mime: str) -> bool:
    """Whether Telegram takes the picture as a photo, read from its size and,
    for a PNG, the width and height in its header (the first 24 bytes)."""
    if mime not in ("image/png", "image/jpeg") or b64_size(raw_b64) > PHOTO_BYTES:
        return False
    head = base64.b64decode(bytes(raw_b64[:32]) + b"=" * (-len(raw_b64[:32]) % 4))
    if mime == "image/png" and head[:8] == b"\x89PNG\r\n\x1a\n" and head[12:16] == b"IHDR":
        w, h = struct.unpack(">II", head[16:24])
        if not w or not h or w + h > PHOTO_SIDES or max(w, h) > PHOTO_RATIO * min(w, h):
            return False
    return True


def file_name(name, mime: str) -> str:
    ext = {"image/png": "png", "image/jpeg": "jpg", "image/gif": "gif",
           "image/webp": "webp"}.get(mime, "bin")
    clean = re.sub(r"[^A-Za-z0-9._-]+", "-", str(name or "")).strip(".-_")[:100]
    if not clean:
        clean = "picture"
    return clean if "." in clean else f"{clean}.{ext}"


def send_image(turn: Turn, part: dict) -> list:
    """Called with PICTURE held. The picture's base64 is bytes (big_part
    reads it so) and goes to the Host as it is, never copied."""
    raw = part.get("data_b64") or b""
    if isinstance(raw, str):
        raw = raw.encode("ascii", "replace")    # a short line, parsed whole
    if part.get("too_big") or b64_size(raw) > FILE_BYTES:
        # The Host would refuse the upload (big_part has not even kept it):
        # say so instead.
        say("a picture in this reply is over the Host's limit for a file; sending a line instead")
        return send_text(turn, TOO_BIG_TEXT)
    if len(raw) < 32 or not _B64.fullmatch(raw):
        return []
    mime = str(part.get("mime") or "application/octet-stream")
    form = where(turn, {})
    # The base64 last: it is the call's last value (_encode).
    upload = {"filename": file_name(part.get("name"), mime), "mime": mime, "data_b64": raw}
    if photo_ok(raw, mime):
        answer = telegram("sendPhoto", form, files=[{"field": "photo", **upload}])
        if sent_id(answer):
            return [sent_id(answer)]
        say(f"Telegram did not take the picture as a photo ({answer.get('description')}); "
            "sending it as a file")
    answer = telegram("sendDocument", form, files=[{"field": "document", **upload}])
    if sent_id(answer):
        return [sent_id(answer)]
    say(f"Telegram did not take the picture as a file; sending it failed "
        f"({answer.get('description')})")
    return []


def typing(turn: Turn) -> None:
    payload = {"chat_id": int(turn.chat_id), "action": "typing"}
    if turn.native:
        payload["message_thread_id"] = int(turn.native)
    with contextlib.suppress(OSError):
        telegram("sendChatAction", payload)


def reply(chat_id: str, message_id: str, native, text: str) -> None:
    """A line from the connector itself, never part of a Host reply."""
    send_text(Turn(chat_id, reply_to=message_id, native=native), text)


# ── one reply of the Host ────────────────────────────────────────────────

# The replies this process is sending, by turn id: one sender per reply,
# even when the same reply reaches it on two streams.
_FOLLOWING: dict = {}
_FOLLOWING_LOCK = threading.Lock()


def claim(turn: Turn) -> bool:
    """True when `turn` is the one sending its reply."""
    with _FOLLOWING_LOCK:
        return _FOLLOWING.setdefault(turn.id, turn) is turn


def adopt(account: str) -> None:
    """The Host took a message from `account`, so it is the paired account."""
    with STATE.lock:
        if STATE.account == account and STATE.pending != account:
            return
        STATE.account = account
        if STATE.pending == account:
            STATE.pending = None
    STATE.save()


def remember(turn: Turn) -> None:
    if not turn.id:
        return
    with STATE.lock:
        STATE.turns[turn.id] = {"chat_id": turn.chat_id, "reply_to": turn.reply_to,
                                "native": turn.native, "sent": sorted(turn.sent),
                                "answered": turn.answered}
    STATE.save()


def forget(turn: Turn) -> None:
    if not turn.id:
        return
    with _FOLLOWING_LOCK:
        if _FOLLOWING.get(turn.id) is not turn:
            return
        _FOLLOWING.pop(turn.id, None)
    with STATE.lock:
        STATE.turns.pop(turn.id, None)
    STATE.save()


def report_sent(turn: Turn, report: dict) -> None:
    """Tell the Host what a part became, so a reply to it lands in the
    reply's thread and a reattach leaves the part out. The part is already
    marked sent, so nothing else sends this report again: it is tried again
    here, the wait growing as follow()'s does, while the Host cannot be
    reached or cannot take it now (429, 5xx). The Host writes a message once
    (message_map.record_sent), so a report that got through but whose answer
    was lost changes nothing when it is sent again."""
    tries = 0
    while True:
        tries += 1
        try:
            status, answer = host_json("POST", "/api/connector/messages/sent", report)
        except OSError as e:
            status, why = 0, cause(e)
            short = why
        else:
            if status == 200:
                return
            if status == 403:
                raise Refused(detail(answer))
            why, short = f"{status}: {detail(answer)}", f"HTTP {status}"
        if (status and status != 429 and status < 500) or tries >= REPORT_TRIES:
            break
        wait = min(30, 2 ** tries)
        # Tried again, so worded as no error: the Host's own words (`detail`)
        # stay out of it, they may hold one.
        say(f"reporting what was sent for reply {turn.id} did not go through ({short}); "
            f"trying again in {wait} s")
        time.sleep(wait)
    # Given up, so worded as the error it is (the app proof reads it).
    say(f"the Host did not record what was sent for reply {turn.id}; reporting it failed "
        + (f"{tries} times in a row ({why})" if tries > 1 else f"({why})"))


def deliver(turn: Turn, part: dict) -> None:
    """Send one part, once, and tell the Host what it became."""
    seq = part.get("seq")
    if not isinstance(seq, int) or seq in turn.sent:
        return
    if part.get("type") == "image":
        ids = send_image(turn, part)
    elif part.get("type") == "link":
        ids = send_text(turn, f"{part.get('label') or 'Open in your Host'}\n{part.get('url') or ''}",
                        seq)
    else:
        ids = send_text(turn, str(part.get("text") or ""), seq)
    turn.partial.pop(seq, None)
    turn.sent.add(seq)
    remember(turn)
    if not ids or not turn.id:
        return
    report = {"turn_id": turn.id, "chat_id": turn.chat_id,
              "sent": [{"seq": seq, "message_id": m} for m in ids]}
    if turn.native:
        report["native_thread_id"] = turn.native
    report_sent(turn, report)


_HEAD = re.compile(rb'^\{"type":\s*"([a-z]+)",\s*"seq":\s*(\d+)')
_DATA = re.compile(rb'"data_b64":\s*"')

# One picture at a time in the whole process (lean on memory:
# better to explain a failure than to over-reserve). A picture is up to 14 MB of
# base64. It is read from the Host, sent on and let go with this held, so
# replies arriving together, or a reply with several pictures, never hold
# more than one; the others wait their turn, and text keeps flowing.
PICTURE = threading.Lock()


def _refused(e) -> str:
    say(f"the Host refused this connector ({e}); sending nothing more")
    return "refused"


def _failed(e) -> str:
    say(f"a send did not go through ({cause(e)}); picking the reply up again")
    return "failed"


def _deliver_held(turn: Turn, part: dict):
    """deliver() a picture with PICTURE held. What it raises comes back as
    the stream's outcome instead: a raised error's traceback holds the
    picture, and it must be let go before PICTURE is."""
    try:
        deliver(turn, part)
        return None
    except Refused as e:
        return _refused(e)
    except OSError as e:
        return _failed(e)


def _read_past(resp) -> bool:
    """Read to the end of the line in pieces, holding none. False when the
    stream ended first."""
    while True:
        rest = resp.readline(1 << 20)
        if not rest:
            return False
        if rest.endswith(b"\n"):
            return True


def big_part(resp, turn: Turn, head: bytes):
    """The rest of a line longer than HEAD_MAX (only a picture is), read, sent
    and let go, called with PICTURE held. None to go on reading the stream,
    else how it ended: 'dropped', 'failed' or 'refused'.

    The base64 is read into one buffer, which is what goes to the Host: the
    line is never also held as a string, or the picture as parsed JSON. A
    line longer than LINE_MAX (over the Host's limit for a file; it never
    streams one) is never held whole: it is read past and stands as
    {type, seq, too_big}, read from its start."""
    start = _HEAD.match(head)
    if start and int(start.group(2)) in turn.sent:
        # Sent before the stream dropped: read past it, holding none of it.
        try:
            return None if _read_past(resp) else "dropped"
        except (OSError, http.client.HTTPException):
            return "dropped"
    data = _DATA.search(head)
    buf = bytearray(head[data.end():] if data else head)
    part = None
    try:
        try:
            while not buf.endswith(b"\n"):
                if len(buf) > LINE_MAX:
                    buf = None
                    if not _read_past(resp):
                        return "dropped"
                    if not start:
                        say("skipped a part of a reply too large to read; reading it failed")
                        return None
                    part = {"type": start.group(1).decode(), "seq": int(start.group(2)),
                            "too_big": True}
                    break
                more = resp.readline(1 << 20)
                if not more:
                    return "dropped"
                buf += more
        except (OSError, http.client.HTTPException):
            return "dropped"
        if part is None and data:
            # {"type": "image", "seq": N, "mime": ..., "data_b64": "<buf>", ...}:
            # the rest of the part is read without the base64, which stays in buf.
            end = buf.find(b'"')
            if end >= 0:
                part = _loads(head[:data.end()] + bytes(buf[end:]))
                del buf[end:]
                if isinstance(part, dict):
                    part["data_b64"] = buf
        elif part is None:
            part = _loads(buf)
        buf = None
        if not isinstance(part, dict) or part.get("type") not in ("text", "image", "link"):
            return None
        return _deliver_held(turn, part)
    finally:
        buf = part = None


def read_stream(resp, turn: Turn, accepted=None) -> tuple:
    """Read one NDJSON answer to its end: ('done' | 'dropped' | 'failed' |
    'refused', how many lines came). 'failed': a send did not go through."""
    lines = 0
    with resp:
        while True:
            try:
                raw = resp.readline(HEAD_MAX)
            except (OSError, http.client.HTTPException):
                return "dropped", lines
            if not raw:
                return "dropped", lines
            # ">=": on a chunked answer readline() can return more than it
            # was asked for (IOBase.readline over HTTPResponse.peek).
            if len(raw) >= HEAD_MAX and not raw.endswith(b"\n"):
                lines += 1
                with PICTURE:
                    outcome = big_part(resp, turn, raw)
                raw = None
                if outcome:
                    return outcome, lines
                continue
            part = _loads(raw)
            raw = None
            if not isinstance(part, dict):
                continue
            lines += 1
            kind = part.get("type")
            if kind == "image":
                with PICTURE:
                    outcome = _deliver_held(turn, part)
                    part = None
                if outcome:
                    return outcome, lines
                continue
            try:
                if kind == "thread":
                    turn.title = str(part.get("title") or "")
                elif kind == "turn":
                    if accepted is not None:
                        accepted.set()
                    if turn.sender:
                        adopt(turn.sender)   # past the Host's check of the sender
                    state = part.get("state")
                    if state == "nudged":
                        # Folded into a turn already running. Its reply comes on
                        # that turn's own stream, or is read in the Host.
                        if not part.get("mine") and part.get("note"):
                            send_text(turn, str(part["note"]))
                    elif part.get("turn_id"):
                        turn.id = str(part["turn_id"])
                        if not claim(turn):
                            return "done", lines    # another stream is sending it
                        remember(turn)
                        if state == "started":
                            typing(turn)
                elif kind == "alive":
                    typing(turn)
                elif kind in ("text", "link"):
                    deliver(turn, part)
                elif kind == "done":
                    return "done", lines
            except Refused as e:
                return _refused(e), lines
            except OSError as e:
                return _failed(e), lines


def follow(resp, turn: Turn, accepted=None, again=None) -> None:
    """Read a reply to its end, picking it up again when the stream drops or
    a send does not go through: by its turn id, or (before the Host named
    one) by posting the same message again, which the Host answers with the
    same reply. The wait grows each time nothing new got through."""
    outcome = "dropped"
    if resp is not None:
        outcome, _ = read_stream(resp, turn, accepted)
    misses = 0
    while outcome in ("dropped", "failed") and (turn.id or again):
        if misses >= REATTACH_TRIES:
            # Given up, so worded as the error it is (the app proof reads it).
            say(f"let go of reply {turn.id or '(unnamed)'}: picking it up again failed "
                f"{REATTACH_TRIES} times in a row")
            break
        time.sleep(min(30, 2 ** misses))
        misses += 1
        try:
            if turn.id:
                resp = _request("GET", f"/api/connector/turns/{turn.id}", timeout=STREAM_READ_S)
            else:
                resp = _request("POST", "/api/connector/messages", again, timeout=STREAM_READ_S)
        except urllib.error.HTTPError as e:
            e.close()
            if e.code < 500:
                say(f"reply {turn.id or '(unnamed)'} is closed ({e.code}); sending nothing more")
                break
            continue
        except (OSError, http.client.HTTPException):
            continue
        say(f"picked up reply {turn.id or '(unnamed)'} again")
        before = len(turn.sent)
        outcome, lines = read_stream(resp, turn, accepted)
        # A stream that came back and dropped again starts the wait over; a
        # send that still does not go through does not, unless a part went
        # out this time (else each try re-reads every part, pictures too).
        if (lines and outcome != "failed") or len(turn.sent) > before:
            misses = 0
    forget(turn)


# ── one message from Telegram ────────────────────────────────────────────

def pairing_code(text: str):
    code = re.sub(r"[\s-]+", "", text or "").upper()
    return code if len(code) == 8 and all(c in CODE_ALPHABET for c in code) else None


def display_name(sender: dict) -> str:
    name = " ".join(str(sender.get(k) or "") for k in ("first_name", "last_name")).strip()
    return name or str(sender.get("username") or "")


def message_origin(msg: dict):
    """Who wrote a message the person forwarded, or sent through an inline
    bot, as the Host's `origin`; None for the person's own words. Read
    2026-10-05 at https://core.telegram.org/bots/api: Message.forward_origin
    is a MessageOrigin, one of MessageOriginUser (type "user", sender_user, a
    User with is_bot), MessageOriginHiddenUser ("hidden_user",
    sender_user_name), MessageOriginChat ("chat", sender_chat) and
    MessageOriginChannel ("channel", chat); Message.via_bot is the "Bot
    through which the message was sent". An origin of a type not listed
    there is still someone else's: hidden."""
    fo = msg.get("forward_origin")
    if isinstance(fo, dict):
        kind = fo.get("type")
        if kind == "user" and isinstance(fo.get("sender_user"), dict):
            u = fo["sender_user"]
            out = {"kind": "forward", "source": "bot" if u.get("is_bot") else "user",
                   "name": display_name(u), "id": u.get("id")}
        elif kind == "hidden_user":
            out = {"kind": "forward", "source": "hidden", "name": fo.get("sender_user_name")}
        elif kind in ("chat", "channel"):
            c = fo.get("sender_chat") if kind == "chat" else fo.get("chat")
            c = c if isinstance(c, dict) else {}
            out = {"kind": "forward", "source": kind, "name": c.get("title") or c.get("username"),
                   "id": c.get("id")}
        else:
            out = {"kind": "forward", "source": "hidden"}
    elif isinstance(msg.get("via_bot"), dict):
        b = msg["via_bot"]
        out = {"kind": "via_bot", "source": "bot", "name": display_name(b), "id": b.get("id")}
    else:
        return None
    if out.get("id") is not None:
        out["id"] = str(out["id"])
    return {k: v for k, v in out.items() if v not in (None, "")}


def what_it_was(msg: dict) -> str:
    for kind in ("photo", "document", "voice", "audio", "video", "video_note", "sticker",
                 "animation", "location", "contact", "poll"):
        if msg.get(kind):
            return f"(sent a {kind.replace('_', ' ')}; this connector passes on text only)"
    return "(sent a message with no text)"


def pair(code: str, account: str, name: str, chat_id: str, message_id: str, native) -> bool:
    """Report a pairing code. True when it was one (matched or not); False
    when no pairing is open, so the text is an ordinary message."""
    status, answer = host_json("POST", "/api/connector/pair",
                               {"code": code, "account_id": account, "display_name": name})
    why = detail(answer)
    if status == 200:
        # Waiting for the person's approval. An account the Host already
        # takes messages from keeps working until then (or after a "Don't pair").
        with STATE.lock:
            STATE.pending = account
        STATE.save()
        say(f"paired Telegram account {account}; the person approves it in the Host")
        try:
            reply(chat_id, message_id, native, PAIRED_TEXT)
        except Refused:
            say("the bot can answer once the person has approved the account")
        return True
    if status == 403 and why.startswith("wrong code"):
        reply(chat_id, message_id, native, VOID_TEXT if "fifth" in why else WRONG_CODE_TEXT)
        return True
    if status == 410:
        reply(chat_id, message_id, native, EXPIRED_TEXT)
        return True
    return False


def on_message(msg: dict, accepted: threading.Event) -> None:
    try:
        handle(msg, accepted)
    except Refused as e:
        say(f"the Host refused this connector ({e})")
    except Exception as e:  # noqa: BLE001 — one message never stops the connector
        # Not tried again, so worded as the error it is (the app proof reads it).
        say(f"a message was dropped: passing it on failed ({type(e).__name__}: {e})")
    finally:
        accepted.set()


def handle(msg: dict, accepted: threading.Event) -> None:
    chat, sender = msg.get("chat") or {}, msg.get("from") or {}
    if chat.get("type") != "private" or sender.get("is_bot") or not sender.get("id"):
        return      # direct messages only: in a group, others would read the Host's replies
    account, chat_id = str(sender["id"]), str(chat.get("id"))
    message_id = str(msg.get("message_id") or "")
    if message_id in ("", "0"):
        return
    native = (str(msg["message_thread_id"])
              if msg.get("is_topic_message") and msg.get("message_thread_id") else None)
    name = display_name(sender)
    text = str(msg.get("text") or msg.get("caption") or "").strip()
    origin = message_origin(msg)
    # Someone else's words are never the person's pairing code or command.
    code = pairing_code(text) if origin is None else None
    if code and pair(code, account, name, chat_id, message_id, native):
        return
    with STATE.lock:
        mine = {a for a in (STATE.account, STATE.pending) if a}
    if mine and account not in mine:
        say("ignored a message from a Telegram account that is not the paired one")
        return
    if origin is None and re.match(r"^/start(@\w+)?(\s|$)", text):
        reply(chat_id, message_id, native, HELLO_TEXT if mine else UNPAIRED_TEXT)
        return
    body = {"chat_id": chat_id, "message_id": message_id, "text": text or what_it_was(msg),
            "account_id": account, "display_name": name}
    replied = (msg.get("reply_to_message") or {}).get("message_id")
    if replied:
        body["reply_to_message_id"] = str(replied)
    if native:
        body["native_thread_id"] = native
    if origin:
        body["origin"] = origin
    try:
        resp = _request("POST", "/api/connector/messages", body, timeout=STREAM_READ_S)
    except urllib.error.HTTPError as e:
        with e:
            answer = _loads(e.read())
        why = detail(answer)
        say(f"the Host did not take the message ({e.code}: {why})")
        if e.code == 403 and account in mine and (why == "this connector is not paired"
                                                  or account == STATE.pending):
            # Their own account, not let in yet: say so, never drop it in silence.
            reply(chat_id, message_id, native, NOT_YET_TEXT)
        if e.code < 500:
            return  # a 403 means not paired, not this account, or revoked: nothing more
        resp = None  # the Host is busy: posted again below
    except (OSError, http.client.HTTPException):
        resp = None  # posted again below; the Host takes a message once
    follow(resp, Turn(chat_id, reply_to=message_id, native=native, sender=account), accepted,
           again=body)


# ── the loop ─────────────────────────────────────────────────────────────

def take(update: dict) -> None:
    """Hand one update to its own thread and wait until the Host has taken
    it (it answered with the turn), so messages reach it in order and a
    message is not lost when the connector restarts."""
    msg = update.get("message")
    if not isinstance(msg, dict):
        return
    accepted = threading.Event()
    threading.Thread(target=on_message, args=(msg, accepted), daemon=True).start()
    if not accepted.wait(ACCEPT_WAIT_S):
        say("the Host has not taken a message yet; going on with the next")


def poll_forever() -> None:
    limit, pause, refused, checked = POLL_LIMIT, 0, 0, False
    while True:
        if pause:
            time.sleep(pause)
        # The first call returns at once, so a wrong token or a refusal shows
        # straight away; every later one is a long poll.
        ask = {"timeout": POLL_S if checked else 0, "limit": limit,
               "allowed_updates": ["message"]}
        if STATE.offset is not None:
            ask["offset"] = STATE.offset
        try:
            answer = telegram("getUpdates", ask, wait=POLL_S + 10)
        except Refused as e:
            # Not approved yet, or revoked. Soon after a pairing the person is
            # approving it right now, so ask again often, then once a minute.
            refused += 1
            set_phase(f"the Host refuses this connector for now ({e})")
            pause = 10 if refused <= 30 else 60
            continue
        except OSError as e:
            pause = min(60, max(5, pause * 2))
            say(f"no updates this time ({cause(e)}); trying again in {pause} s")
            continue
        pause, refused = 0, 0
        if answer.get("truncated"):
            if limit > 1:
                limit = 1          # read them one at a time
                continue
            skip = re.search(r'"update_id"\s*:\s*(\d+)', answer.get("body") or "")
            if skip:
                with STATE.lock:
                    STATE.offset = int(skip.group(1)) + 1
                STATE.save()
                # Its message is never passed on: worded as the error it is.
                say("skipped one update too large to read; reading it failed")
            continue
        limit = POLL_LIMIT
        if not answer.get("ok"):
            code = answer.get("error_code")
            if code in TOKEN_REFUSED:
                pause = broken(f"getUpdates: {code} {answer.get('description')}",
                               f"Telegram refused the bot token ({code} {answer.get('description')}): "
                               "put the token BotFather gave in Keys")
                continue
            if code == 409:
                # getUpdates does not work while the bot has a webhook.
                with contextlib.suppress(OSError, Refused):
                    telegram("deleteWebhook", {"drop_pending_updates": False})
            say(f"getUpdates: {answer.get('description')}")
            pause = 5
            continue
        checked = True
        mended()
        set_phase("listening")
        for update in answer.get("result") or []:
            take(update)
            with STATE.lock:
                STATE.offset = int(update["update_id"]) + 1
            STATE.save()


def resume() -> None:
    """Pick up the replies that were on their way when the connector stopped."""
    with STATE.lock:
        saved = dict(STATE.turns)
    for turn_id, t in saved.items():
        turn = Turn(str(t.get("chat_id")), reply_to=t.get("reply_to"), native=t.get("native"),
                    turn_id=turn_id, sent=t.get("sent") or ())
        turn.answered = bool(t.get("answered"))
        claim(turn)
        say(f"picking up reply {turn_id} after a restart")
        threading.Thread(target=follow, args=(None, turn), daemon=True).start()


# ── health ───────────────────────────────────────────────────────────────

class Health(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.split("?")[0] == "/health":
            # A failure that cannot fix itself is not ready: app_status and the
            # Host read ok and error here (the platform's ready_path contract).
            answer = {"ok": bool(HOST_URL and APP_ID) and not PHASE["error"],
                      "phase": PHASE["text"]}
            if PHASE["error"]:
                answer["error"] = PHASE["error"]
            body = json.dumps(answer)
            ctype = "application/json"
        else:
            body = ("<!doctype html><meta charset=utf-8><title>Telegram connector</title>"
                    f"<p>Telegram connector: {html.escape(PHASE['text'])}</p>")
            ctype = "text/html; charset=utf-8"
        data = body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


class DualStack(ThreadingHTTPServer):
    """Listens on IPv6 and IPv4 at once: the probe comes to the pod's address."""
    address_family = socket.AF_INET6
    daemon_threads = True

    def server_bind(self):
        with contextlib.suppress(OSError):
            self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = "connector", self.server_address[1]


class V4Only(DualStack):
    address_family = socket.AF_INET


def serve_health() -> None:
    try:
        server = DualStack(("::", PORT), Health)
    except OSError:          # no IPv6 on this machine
        server = V4Only(("0.0.0.0", PORT), Health)
    threading.Thread(target=server.serve_forever, daemon=True).start()


def main() -> None:
    serve_health()
    if not (HOST_URL and APP_ID):
        set_phase("CSERIES_KERNEL_URL and PRIVATAE_APP_ID are not set; nothing to do")
    elif not CAP:
        set_phase("waiting for the pairing: the Host starts it, and the connector restarts with it")
    else:
        set_phase("connecting")
        resume()
        poll_forever()
    threading.Event().wait()


if __name__ == "__main__":
    main()
