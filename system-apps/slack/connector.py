"""Slack connector: lets ONE Slack account talk to its Host.

A resident process app. It never holds a Slack token. Every Web API call
(auth.test, users.info, chat.postMessage, the two calls around a picture's
upload) goes through the Host (POST /api/connector/platform), which adds the
bot token from Keys; apps.connections.open, the one method that takes the
app-level token, goes the same way with use=app. What that call returns is a
Socket Mode WebSocket URL that carries its own one-time ticket and no token,
and the connector opens that socket itself, from its pod. So does each
picture's upload URL (files.slack.com, no token either). Both hosts are
subdomains of slack.com, the one host the app declares, and the pod's egress
wall lets a declared host's subdomains through.

Socket Mode, read 2026-10-05 at
https://docs.slack.dev/apis/events-api/using-socket-mode: the app "receives a
WebSocket URL" from apps.connections.open, the first message is "hello", every
envelope is acknowledged with its envelope_id "so that Slack knows whether to
retry", a "disconnect" message comes before Slack refreshes the connection.
https://docs.slack.dev/apis/events-api/: "respond ... within three seconds",
else Slack retries "up to 3 times". So an event is written to the state file
and acknowledged at once, and handed to the Host from there: a message is
let go only once the Host has taken it, and a restart picks up the rest.

What the person's Slack app needs (default-skills/connectors.md gives them a
manifest with exactly this), each read 2026-10-05 at docs.slack.dev:
  - Socket Mode on, and an app-level token with connections:write
    (/apis/events-api/using-socket-mode; apps.connections.open takes "an
    app-level token", /reference/methods/apps.connections.open);
  - the message.im event, which needs im:history (/reference/events/message.im);
  - chat:write for chat.postMessage (/reference/methods/chat.postMessage);
  - files:write for files.getUploadURLExternal and files.completeUploadExternal
    (/reference/methods/files.getUploadURLExternal);
  - users:read for users.info, the sender's name on the pairing card
    (/reference/methods/users.info);
  - commands, for the /threads, /thread and /new slash commands (the slash
    command docs, /interactivity/implementing-slash-commands, do not name a
    scope; the manifest asks for this one, not yet tried on a live workspace);
  - the App Home's Messages tab with sending allowed
    (messages_tab_read_only_enabled false, /reference/app-manifest).

Direct messages only, one account, as the Telegram app. A message in a Slack
thread carries the thread's ts as its native thread, and the answer goes back
into that thread; a message outside one is answered outside one.

Standard library only, so there is nothing to install and no lockfile.

The platform sets PORT, PRIVATAE_APP_ID and CSERIES_KERNEL_URL, and
CSERIES_CONNECTOR_CAP once the Host has started the pairing. What has to
survive a restart (the paired account, the events not yet taken, the replies
still on their way) is one JSON file in the app's data dir.
"""
from __future__ import annotations

import base64
import contextlib
import errno
import hashlib
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
from urllib.parse import urlsplit

APP_ID = os.environ.get("PRIVATAE_APP_ID", "")
HOST_URL = os.environ.get("CSERIES_KERNEL_URL", "").rstrip("/")
CAP = os.environ.get("CSERIES_CONNECTOR_CAP", "")
PORT = int(os.environ.get("PORT") or "8080")
# The app's data dir survives restarts and re-installs. CONNECTOR_STATE_DIR
# lets a test put it somewhere else.
STATE_DIR = os.environ.get("CONNECTOR_STATE_DIR") or f"/var/lib/cseries/appdata/{APP_ID}"

SLACK_DOMAIN = "slack.com"
# chat.postMessage (https://docs.slack.dev/reference/methods/chat.postMessage):
# text is "limited to 4,000 characters for best results"; Slack truncates past
# 40,000. Counted after & < > are escaped, as Slack reads it.
TEXT_CHARS = 4000
TEXT_JSON = 12_000          # a /platform call without files is at most 16 KiB of JSON, in UTF-8 bytes
FILE_BYTES = 10 * 1024 * 1024   # the largest picture the Host streams (reply_parts.IMAGE_PART_MAX)
LINE_MAX = FILE_BYTES * 4 // 3 + 64 * 1024   # the longest line of a reply worth reading whole
HEAD_MAX = 256 * 1024       # a line longer than this is a picture's: the rest is read under PICTURE
UPLOAD_CHUNK = 48 * 1024    # base64 decoded and sent this much at a time (a multiple of 4)
# Slack answers a call over its rate limit with HTTP 429 and "ratelimited"
# (chat.postMessage: about one message a second to a channel), "and a
# Retry-After HTTP header containing the number of seconds until you can
# retry" (https://docs.slack.dev/apis/web-api/rate-limits). The Host passes
# that back as `retry_after`, and the connector waits that long, up to
# RATE_WAIT_MAX_S; without one, the waits are these, growing.
RATE_WAITS = (1, 2, 4, 8, 16)
RATE_WAIT_MAX_S = 60
STREAM_READ_S = 60          # the Host sends a line at least every 10 s while a reply runs
ACCEPT_WAIT_S = 120         # how long a message waits for the Host to take it
REATTACH_TRIES = 8          # reconnects in a row that get nothing before a reply is let go
REPORT_TRIES = 4            # tries of a report of what was sent before it is let go
# A failure that cannot fix itself (Slack refusing the sign-in or the
# connection, or Socket Mode turned off) is not tried again every few
# seconds: it is logged once as the error it is, shown on /health and the
# app's page, and checked again this often, so a token put right in Keys is
# picked up without a restart.
RECHECK_S = 600
WS_IDLE_S = 30              # no frame for this long: ping; three such in a row: reconnect
WS_MESSAGE_MAX = 1 << 20    # a Socket Mode message larger than this is not Slack's
SEEN_MAX = 500              # events already taken, remembered against Slack's retries
CODE_ALPHABET = "BCDFGHJKLMNPQRSTVWXZ"   # a pairing code is 8 of these
# The thread commands the Host's route reads (server/connectors/turns.py).
# Slack's client takes any message that starts with "/" as one of its own
# commands, so the app declares these three as slash commands, which Socket
# Mode delivers as their own envelopes.
COMMANDS = ("/new", "/threads", "/thread")
_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"   # RFC 6455 §1.3

PAIRED_TEXT = ("Code accepted. Now let this account in from your Host: open the Network "
               "app, or the card in your chat, and approve it with your authenticator code.")
WRONG_CODE_TEXT = "That code does not match. Check the code in your Host's Network app."
VOID_TEXT = "That was the fifth wrong code, so this pairing is closed. Ask your Host to start it again."
EXPIRED_TEXT = "That code has expired. Ask your Host to start the pairing again."
UNPAIRED_TEXT = "To pair this app with your Host, send it the code your Host's Network app shows."
NOT_YET_TEXT = ("Your Host has not let this account in yet, so this message was not passed on. "
                "Send the code from the Network app if you have not, approve the account there, "
                "then send your message again.")
TOO_BIG_TEXT = "A picture in this reply is too large to send here; it is in your Host."


def say(line: str) -> None:
    print(f"[slack-connector] {line}", flush=True)


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
        # The account the Host takes messages from, and the one whose code
        # matched and waits for the person's approval. Messages from either
        # go to the Host, which decides; anyone else's are dropped here.
        self.account = data.get("account")
        self.pending = data.get("pending")
        # account -> the direct-message channel it writes from: a slash
        # command is taken only from there.
        self.dms = data.get("dms") or {}
        # Events acknowledged to Slack and not yet taken by the Host, in order.
        self.inbox = data.get("inbox") or []
        # Events already taken: Slack delivers one again when an ack is lost.
        self.seen = data.get("seen") or []
        self.turns = data.get("turns") or {}

    def save(self) -> None:
        with self.lock:
            snap = json.dumps({"account": self.account, "pending": self.pending,
                               "dms": self.dms, "inbox": self.inbox,
                               "seen": self.seen[-SEEN_MAX:], "turns": self.turns})
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                fh.write(snap)
            os.replace(tmp, self.path)


STATE = State(os.path.join(STATE_DIR, "slack-connector.json"))
# What /health and the app's page show. `error`: the failure that cannot fix
# itself, while it lasts (broken()); any other phase ends it.
PHASE = {"text": "starting", "error": ""}
BOT: dict = {}              # auth.test: the bot's own user id and workspace
NAMES: dict = {}            # user id -> display name (users.info)


def set_phase(text: str) -> None:
    if PHASE["text"] != text:
        PHASE.update(text=text, error="")
        say(text)


def broken(why: str, status: str) -> int:
    """A failure that cannot fix itself: logged as the error it is when it
    starts or changes, never on each check, and shown on /health (ok false)
    and the app's page until it ends. How long to wait before checking again."""
    if PHASE["error"] != why:
        say(f"connecting to Slack failed: {status}. This does not fix itself; "
            f"checking again every {RECHECK_S // 60} minutes")
    PHASE.update(text=f"{status}. Checked again every {RECHECK_S // 60} minutes.", error=why)
    return RECHECK_S


def mended() -> None:
    """The failure broken() reported is over: Slack said hello on a new socket."""
    if PHASE["error"]:
        say(f"connected again: the refusal ({PHASE['error']}) is over")
        PHASE["error"] = ""


# ── the Host ─────────────────────────────────────────────────────────────

# The Host is reached directly, never through the pod's web proxy.
_DIRECT = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class Refused(Exception):
    """The Host answered 403: this connector is not paired, or was revoked."""


def _request(method: str, path: str, body=None, timeout: float = 60.0):
    data = None if body is None else json.dumps(body).encode("utf-8")
    req = urllib.request.Request(HOST_URL + path, data=data, method=method)
    req.add_header("X-CSeries-App-Id", APP_ID)
    req.add_header("X-CSeries-Connector-Cap", CAP)
    if data is not None:
        req.add_header("Content-Type", "application/json")
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


# Slack's own words for a failure on its side, worth trying again later.
_SLACK_LATER = frozenset({"internal_error", "fatal_error", "service_unavailable",
                          "request_timeout"})
# Every other error Slack answers auth.test or apps.connections.open with
# (their Errors tables, read 2026-10-09 at docs.slack.dev/reference/methods/)
# refuses the token, its scopes or the workspace, and stays so until the
# person changes something; slack() has already tried again on the ones above
# and on ratelimited. Of those, these say the token itself does not work
# (measured 2026-10-09: a made-up token gets invalid_auth, none gets
# not_authed).
_SLACK_BAD_TOKEN = frozenset({"invalid_auth", "not_authed", "account_inactive", "token_revoked",
                              "token_expired", "not_allowed_token_type", "missing_args"})
_SLACK_CODE = re.compile(r"[a-z][a-z0-9_]*")


class Unusable(Exception):
    """Slack's own refusal of the sign-in (auth.test) or of the connection
    (apps.connections.open). Asking again soon does not change it."""

    def __init__(self, method: str, code: str, token: str):
        super().__init__(f"{method}: {code}")
        self.why = f"{method}: {code}"
        self.status = (f"Slack refused the {token} ({self.why}): "
                       + ("put a working one in Keys" if code in _SLACK_BAD_TOKEN
                          else "check the Slack app's settings at api.slack.com/apps"))


def refusal(answer: dict, method: str, token: str):
    """Unusable when `answer` is Slack's own refusal: an error code, not the
    Host's words for a call it would not make."""
    code = answer.get("error")
    if not answer.get("ok") and isinstance(code, str) and _SLACK_CODE.fullmatch(code):
        return Unusable(method, code, token)
    return None


def slack(method: str, form=None, *, use: str = "bot", wait: int = 30) -> dict:
    """One Web API call through the Host, as a form (every method takes
    application/x-www-form-urlencoded). Returns Slack's own answer,
    {"ok": true, ...} or {"ok": false, "error": ...}. Raises Refused on 403,
    and OSError when the call could not be made now (worth trying again
    later): the Host busy (429) or failing (5xx), Slack failing, or Slack
    still rate-limiting after the waits in RATE_WAITS."""
    body = {"method": "POST", "path": "/api/" + method, "form": form or {}, "timeout": wait}
    if use != "bot":
        body["use"] = use
    for pause in RATE_WAITS + (None,):
        status, out = host_json("POST", "/api/connector/platform", body, timeout=wait + 20)
        if status == 403:
            raise Refused(detail(out))
        if status == 429 or status >= 500:
            raise OSError(f"the Host could not make the {method} call now ({status}: {detail(out)})")
        if status != 200 or not isinstance(out, dict):
            return {"ok": False, "error": f"the Host refused {method} ({status}: {detail(out)})"}
        answer, upstream = out.get("json"), out.get("status")
        if upstream == 429 or (isinstance(answer, dict) and answer.get("error") == "ratelimited"):
            if pause is None:
                break
            told = out.get("retry_after")
            if isinstance(told, (int, float)) and not isinstance(told, bool):
                if told > RATE_WAIT_MAX_S:
                    raise OSError(f"Slack asks to wait {told:.0f} s before {method}")
                pause = told
            time.sleep(pause)
            continue
        if not isinstance(answer, dict):
            raise OSError(f"Slack answered {method} with no JSON ({upstream})")
        if (isinstance(upstream, int) and upstream >= 500) or answer.get("error") in _SLACK_LATER:
            raise OSError(f"Slack could not take {method} now ({upstream}: {answer.get('error')})")
        return answer
    raise OSError(f"Slack is still rate-limiting {method}")


def slack_url(url, scheme: str):
    """The URL's parts when it is `scheme` on a subdomain of slack.com, else
    None: an answer that names any other host is never followed."""
    try:
        u = urlsplit(str(url or ""))
        host = (u.hostname or "").lower()
        u.port      # a malformed port raises here
    except ValueError:
        return None
    if u.scheme != scheme or not host.endswith("." + SLACK_DOMAIN):
        return None
    return u


# ── sending to Slack ─────────────────────────────────────────────────────

class Turn:
    """One answer of the Host on its way to a direct message."""

    def __init__(self, chat_id: str, native=None, turn_id=None, sent=(), sender=None):
        self.chat_id, self.native = chat_id, native
        self.id = turn_id
        self.sent = set(sent)
        self.sender = sender       # the account whose message this answers
        self.partial = {}          # seq -> the pieces of a part sent so far, and their ids


def where(turn: Turn, form: dict) -> dict:
    """The direct message, and the Slack thread when the person wrote in one."""
    form["channel"] = turn.chat_id
    if turn.native:
        form["thread_ts"] = turn.native
    return form


def escape(text: str) -> str:
    """Slack reads &, < and > as its own markup; these three are escaped
    (https://docs.slack.dev/messaging/formatting-message-text)."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _fits(text: str) -> bool:
    out = escape(text)
    # Measured as the Host measures the call: UTF-8 bytes.
    return (len(out) <= TEXT_CHARS
            and len(json.dumps(out, ensure_ascii=False).encode("utf-8", "surrogatepass"))
            <= TEXT_JSON)


def split_text(text: str) -> list:
    """Pieces Slack takes as one message each, cut at a paragraph, a line or
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


def post(turn: Turn, text: str, *, escaped: bool = False):
    """chat.postMessage, with link previews off. The ts Slack gave the
    message, or None when Slack did not take it."""
    answer = slack("chat.postMessage", where(turn, {
        "text": text if escaped else escape(text), "unfurl_links": "false",
        "unfurl_media": "false"}))
    ts = answer.get("ts") if answer.get("ok") else None
    if not ts:
        # Not tried again, so worded as the error it is (the app proof reads it).
        say(f"Slack did not take a message; sending it failed ({answer.get('error')})")
    return str(ts) if ts else None


def send_text(turn: Turn, text: str, seq=None) -> list:
    """Send `text` in as many messages as it takes. A part tried again after
    a send that did not go through carries on after the pieces already sent
    (turn.partial), so no piece goes out twice."""
    done = turn.partial.setdefault(seq, {"n": 0, "ids": []}) if seq is not None \
        else {"n": 0, "ids": []}
    for piece in split_text(text)[done["n"]:]:
        ts = post(turn, piece)
        if ts:
            done["ids"].append(ts)
        done["n"] += 1
    return list(done["ids"])


def send_link(turn: Turn, part: dict, seq) -> list:
    """A link as Slack's own <url|label>, so the label is what shows."""
    url, label = str(part.get("url") or ""), str(part.get("label") or "Open in your Host")
    if seq in turn.partial or not re.match(r"^https?://\S+$", url) or "|" in url:
        return send_text(turn, f"{label}\n{url}", seq)
    turn.partial[seq] = {"n": 0, "ids": []}
    ts = post(turn, f"<{escape(url)}|{escape(label.replace('|', '/'))}>", escaped=True)
    turn.partial[seq] = {"n": 1, "ids": [ts] if ts else []}
    return [ts] if ts else []


_B64 = re.compile(rb"[A-Za-z0-9+/]+=*")


def b64_size(raw_b64: bytes) -> int:
    return len(raw_b64) * 3 // 4 - raw_b64[-2:].count(b"=")


def file_name(name, mime: str) -> str:
    ext = {"image/png": "png", "image/jpeg": "jpg", "image/gif": "gif",
           "image/webp": "webp"}.get(mime, "bin")
    clean = re.sub(r"[^A-Za-z0-9._-]+", "-", str(name or "")).strip(".-_")[:100]
    if not clean:
        clean = "picture"
    return clean if "." in clean else f"{clean}.{ext}"


def upload(url, raw_b64, mime: str) -> None:
    """POST the picture's bytes to the upload URL Slack gave, decoding its
    base64 a piece at a time (never a second copy of the whole picture).
    The URL carries no token. Raises OSError when it did not go through."""
    u = slack_url(url, "https")
    if u is None:
        raise OSError("Slack gave an upload URL that is not on slack.com")
    path = (u.path or "/") + (f"?{u.query}" if u.query else "")
    conn = http.client.HTTPSConnection(u.hostname, u.port or 443, timeout=120,
                                       context=ssl.create_default_context())
    try:
        conn.putrequest("POST", path)
        conn.putheader("Content-Type", mime)
        conn.putheader("Content-Length", str(b64_size(raw_b64)))
        conn.endheaders()
        with memoryview(raw_b64) as view:
            for at in range(0, len(view), UPLOAD_CHUNK):
                conn.send(base64.b64decode(view[at:at + UPLOAD_CHUNK]))
        resp = conn.getresponse()
        resp.read(64 * 1024)
        if resp.status != 200:
            raise OSError(f"the upload to {u.hostname} answered {resp.status}")
    except (http.client.HTTPException, ssl.SSLError) as e:
        raise OSError(f"the upload to {u.hostname} did not finish: {cause(e)}") from None
    finally:
        conn.close()


def send_image(turn: Turn, part: dict) -> list:
    """Called with PICTURE held. Slack's upload in three steps
    (https://docs.slack.dev/reference/methods/files.getUploadURLExternal): an
    upload URL from the Host's call, the bytes straight to that URL, then
    files.completeUploadExternal shares the file into the chat. Slack's answer
    names the file, not the message that carries it, so a picture reports no
    message id."""
    raw = part.get("data_b64") or b""
    if isinstance(raw, str):
        raw = raw.encode("ascii", "replace")    # a short line, parsed whole
    if part.get("too_big") or b64_size(raw) > FILE_BYTES:
        say("a picture in this reply is over the Host's limit for a file; sending a line instead")
        return send_text(turn, TOO_BIG_TEXT)
    if len(raw) < 32 or not _B64.fullmatch(raw):
        return []
    mime = str(part.get("mime") or "application/octet-stream")
    name = file_name(part.get("name"), mime)
    got = slack("files.getUploadURLExternal", {"filename": name, "length": str(b64_size(raw))})
    if not got.get("ok") or not got.get("file_id"):
        say(f"Slack gave no upload URL for a picture; sending it failed ({got.get('error')})")
        return []
    upload(got.get("upload_url"), raw, mime)
    form = {"files": json.dumps([{"id": str(got["file_id"]), "title": name}]),
            "channel_id": turn.chat_id}
    if turn.native:
        form["thread_ts"] = turn.native
    done = slack("files.completeUploadExternal", form)
    if not done.get("ok"):
        say(f"Slack did not share a picture; sending it failed ({done.get('error')})")
    return []


def reply(chat_id: str, native, text: str) -> None:
    """A line from the connector itself, never part of a Host reply."""
    send_text(Turn(chat_id, native=native), text)


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
        STATE.turns[turn.id] = {"chat_id": turn.chat_id, "native": turn.native,
                                "sent": sorted(turn.sent)}
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
        ids = send_link(turn, part, seq)
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
# base64. It is read from the Host, uploaded and let go with this held, so
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

    The base64 is read into one buffer, which is what is uploaded: the line
    is never also held as a string, or the picture as parsed JSON. A line
    longer than LINE_MAX (over the Host's limit for a file; it never streams
    one) is never held whole: it is read past and stands as {type, seq,
    too_big}, read from its start."""
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
                if kind == "turn":
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
                elif kind in ("text", "link"):
                    deliver(turn, part)
                elif kind == "done":
                    return "done", lines
                # "thread" and "alive": Slack has no typing indicator for an
                # app's bot outside its assistant threads, so nothing to show.
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


# ── one message from Slack ───────────────────────────────────────────────

def pairing_code(text: str):
    code = re.sub(r"[\s-]+", "", text or "").upper()
    return code if len(code) == 8 and all(c in CODE_ALPHABET for c in code) else None


def display_name(user: str) -> str:
    """The person's name as Slack shows it (users.info), or ''."""
    if user in NAMES:
        return NAMES[user]
    name = ""
    with contextlib.suppress(OSError):
        got = slack("users.info", {"user": user})
        u = got.get("user") if got.get("ok") else None
        if isinstance(u, dict):
            prof = u.get("profile") if isinstance(u.get("profile"), dict) else {}
            name = str(prof.get("display_name") or prof.get("real_name") or u.get("real_name")
                       or u.get("name") or "")
    NAMES[user] = name
    return name


_LINK = re.compile(r"<((?:https?|mailto):[^|>\s]+)(?:\|[^>]*)?>")


def plain(text: str) -> str:
    """A Slack message's text as the person typed it: a link Slack wrapped
    as <url|text> becomes the url, and & < > come back from their escapes."""
    text = _LINK.sub(lambda m: m.group(1), text or "")
    return text.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")


def pair(code: str, account: str, name: str, chat_id: str, native) -> bool:
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
        say(f"paired Slack account {account}; the person approves it in the Host")
        try:
            reply(chat_id, native, PAIRED_TEXT)
        except Refused:
            say("the app can answer once the person has approved the account")
        return True
    if status == 403 and why.startswith("wrong code"):
        reply(chat_id, native, VOID_TEXT if "fifth" in why else WRONG_CODE_TEXT)
        return True
    if status == 410:
        reply(chat_id, native, EXPIRED_TEXT)
        return True
    return False


def on_message(item: dict, accepted: threading.Event) -> None:
    try:
        handle(item, accepted)
    except Refused as e:
        say(f"the Host refused this connector ({e})")
    except Exception as e:  # noqa: BLE001 — one message never stops the connector
        # Not tried again, so worded as the error it is (the app proof reads it).
        say(f"a message was dropped: passing it on failed ({type(e).__name__}: {e})")
    finally:
        accepted.set()


def handle(item: dict, accepted: threading.Event) -> None:
    account = f"{item['team']}:{item['user']}"
    chat_id, message_id, native = item["channel"], item["ts"], item.get("native")
    command = item.get("kind") == "command"
    with STATE.lock:
        mine = {a for a in (STATE.account, STATE.pending) if a}
        home = STATE.dms.get(account)
    if command and home != chat_id:
        # A slash command can be typed in any conversation; only one typed in
        # the paired account's own direct message with this app is taken.
        say("ignored a slash command from outside the paired account's direct message")
        return
    text = item.get("text") or ""
    origin = item.get("origin")
    # Someone else's words are never the person's pairing code.
    code = None if command or origin else pairing_code(text)
    if code and pair(code, account, display_name(item["user"]), chat_id, native):
        with STATE.lock:
            STATE.dms[account] = chat_id
        STATE.save()
        return
    if mine and account not in mine:
        say("ignored a message from a Slack account that is not the paired one")
        return
    if mine and home != chat_id:
        with STATE.lock:
            STATE.dms[account] = chat_id
        STATE.save()
    name = display_name(item["user"])
    body = {"chat_id": chat_id, "message_id": message_id,
            "text": text or ("(sent a file; this connector passes on text only)"
                             if item.get("files") else "(sent a message with no text)"),
            "account_id": account, "display_name": name}
    if native:
        # A message in a Slack thread answers the thread's first message.
        body["reply_to_message_id"] = native
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
            reply(chat_id, native, NOT_YET_TEXT)
        elif e.code == 403 and not mine and why == "this connector is not paired":
            reply(chat_id, native, UNPAIRED_TEXT)
        elif e.code == 403 and why == "connector capability required":
            hang_up()     # revoked: nothing from Slack can be passed on now
        if e.code < 500:
            return  # a 403 means not paired, not this account, or revoked: nothing more
        resp = None  # the Host is busy: posted again below
    except (OSError, http.client.HTTPException):
        resp = None  # posted again below; the Host takes a message once
    follow(resp, Turn(chat_id, native=native, sender=account), accepted, again=body)


# ── Socket Mode ──────────────────────────────────────────────────────────

class Socket:
    """A WebSocket client (RFC 6455) over TLS, enough for Socket Mode: text
    messages in, acknowledgements out, pings answered."""

    def __init__(self, url: str):
        u = slack_url(url, "wss")
        if u is None:
            raise OSError("apps.connections.open gave a URL that is not wss on slack.com")
        host, port = u.hostname, u.port or 443
        raw = socket.create_connection((host, port), timeout=30)
        try:
            self.sock = ssl.create_default_context().wrap_socket(raw, server_hostname=host)
        except (OSError, ssl.SSLError):
            raw.close()
            raise
        self.lock = threading.Lock()
        self.buf = bytearray()
        key = base64.b64encode(os.urandom(16)).decode()
        path = (u.path or "/") + (f"?{u.query}" if u.query else "")
        self.sock.sendall((f"GET {path} HTTP/1.1\r\nHost: {u.netloc}\r\n"
                           "Upgrade: websocket\r\nConnection: Upgrade\r\n"
                           f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
                           ).encode("ascii"))
        while b"\r\n\r\n" not in self.buf:
            if len(self.buf) > 16 * 1024:
                raise OSError("the socket's handshake answer is too long")
            self._fill()
        head, _, rest = bytes(self.buf).partition(b"\r\n\r\n")
        self.buf = bytearray(rest)
        lines = head.decode("latin-1").split("\r\n")
        status = (lines[0].split() + ["", ""])[1]
        if status != "101":
            # The code alone: its reason phrase ("500 Internal Server Error")
            # would make the line that says it tries again read as an error.
            raise OSError("the socket was not opened (Slack answered "
                          + (status if status.isdigit() else "with no HTTP status") + ")")
        headers = {k.strip().lower(): v.strip() for k, _, v in
                   (ln.partition(":") for ln in lines[1:])}
        want = base64.b64encode(hashlib.sha1((key + _GUID).encode()).digest()).decode()
        if headers.get("sec-websocket-accept") != want:
            raise OSError("the socket's handshake did not answer this connector's key")
        self.sock.settimeout(WS_IDLE_S)

    def _fill(self) -> None:
        chunk = self.sock.recv(64 * 1024)
        if not chunk:
            raise OSError("the socket was closed")
        self.buf += chunk

    def _take(self, n: int) -> bytes:
        while len(self.buf) < n:
            self._fill()
        out = bytes(self.buf[:n])
        del self.buf[:n]
        return out

    def send(self, opcode: int, payload: bytes = b"") -> None:
        n = len(payload)
        head = bytes([0x80 | opcode])
        if n < 126:
            head += bytes([0x80 | n])
        elif n < 1 << 16:
            head += bytes([0x80 | 126]) + struct.pack(">H", n)
        else:
            head += bytes([0x80 | 127]) + struct.pack(">Q", n)
        mask = os.urandom(4)
        body = (int.from_bytes(payload, "big")
                ^ int.from_bytes((mask * (n // 4 + 1))[:n], "big")).to_bytes(n, "big") if n else b""
        with self.lock:
            self.sock.sendall(head + mask + body)

    def send_json(self, obj) -> None:
        self.send(0x1, json.dumps(obj).encode("utf-8"))

    def recv(self):
        """The next text message, or None when the socket closes. Raises
        OSError when it went silent or broke."""
        message, quiet = bytearray(), 0
        while True:
            try:
                b0, b1 = self._take(2)
            except (TimeoutError, socket.timeout):
                quiet += 1
                if quiet >= 3:
                    raise OSError("the socket went silent") from None
                self.send(0x9, b"slack-connector")
                continue
            quiet = 0
            fin, opcode = b0 & 0x80, b0 & 0x0F
            n = b1 & 0x7F
            if n == 126:
                n = struct.unpack(">H", self._take(2))[0]
            elif n == 127:
                n = struct.unpack(">Q", self._take(8))[0]
            if n > WS_MESSAGE_MAX or len(message) + n > WS_MESSAGE_MAX:
                raise OSError("a socket message too large to be Slack's")
            mask = self._take(4) if b1 & 0x80 else None
            payload = self._take(n)
            if mask:
                payload = (int.from_bytes(payload, "big") ^ int.from_bytes(
                    (mask * (n // 4 + 1))[:n], "big")).to_bytes(n, "big")
            if opcode == 0x8:                       # close
                with contextlib.suppress(OSError):
                    self.send(0x8, payload[:2])
                return None
            if opcode == 0x9:                       # ping
                self.send(0xA, payload)
                continue
            if opcode == 0xA:                       # pong
                continue
            message += payload
            if fin:
                return message.decode("utf-8", "replace")

    def close(self) -> None:
        with contextlib.suppress(OSError):
            self.send(0x8, struct.pack(">H", 1000))
        with contextlib.suppress(OSError):
            self.sock.close()


INBOX_WAKE = threading.Event()


def _key(item: dict) -> str:
    return f"{item.get('channel')}:{item.get('ts')}"


def queue(item: dict) -> None:
    """Keep an event until the Host has taken it; once is enough."""
    k = _key(item)
    with STATE.lock:
        if k in STATE.seen or any(_key(i) == k for i in STATE.inbox):
            return
        STATE.inbox.append(item)
    STATE.save()
    INBOX_WAKE.set()


def shared_of(ev: dict):
    """(the text, the origin) of what a message carries that the person did
    not write: a message they shared into the DM, which arrives as an
    attachment of their own message. Slack's docs do not describe how a
    share arrives; what is read here is only what they do document on an
    attachment (MessageAttachment,
    https://docs.slack.dev/tools/node-slack-sdk/reference/types/interfaces/MessageAttachment):
    `text`, "the main body text", `author_name`, "the author's name", and
    `bot_id`. With a bot_id the source is a bot; without, it is not named.
    None when there is no attachment text."""
    atts = ev.get("attachments")
    if not isinstance(atts, list):
        return None
    for a in atts:
        if not isinstance(a, dict) or not str(a.get("text") or "").strip():
            continue
        origin = {"kind": "forward", "source": "bot" if a.get("bot_id") else "hidden"}
        if a.get("author_name"):
            origin["name"] = str(a["author_name"])
        return plain(str(a["text"])).strip(), origin
    return None


def take_event(payload) -> None:
    """A message event, when it is a person's direct message to this app."""
    if not isinstance(payload, dict):
        return
    ev = payload.get("event")
    if not isinstance(ev, dict) or ev.get("type") != "message" or ev.get("channel_type") != "im":
        return
    if ev.get("subtype") not in (None, "file_share", "thread_broadcast"):
        return          # edits, deletions, joins: not something the person said
    user, team = str(ev.get("user") or ""), str(payload.get("team_id") or ev.get("team") or "")
    if ev.get("bot_id") or not user or user == BOT.get("user_id") or not team:
        return
    ts, thread = str(ev.get("ts") or ""), str(ev.get("thread_ts") or "")
    if not ts or not ev.get("channel"):
        return
    native = thread if thread and thread != ts else None
    text = plain(str(ev.get("text") or "")).strip()
    shared = shared_of(ev)
    if shared is not None:
        # The person's own words, when they wrote any, go first as theirs;
        # what they shared goes as its own message, with its origin.
        if text:
            queue({"kind": "message", "team": team, "user": user,
                   "channel": str(ev["channel"]), "ts": ts, "native": native, "text": text,
                   "files": False})
        queue({"kind": "message", "team": team, "user": user, "channel": str(ev["channel"]),
               "ts": f"{ts}:shared" if text else ts, "native": native, "text": shared[0],
               "files": False, "origin": shared[1]})
        return
    queue({"kind": "message", "team": team, "user": user, "channel": str(ev["channel"]),
           "ts": ts, "native": native, "text": text, "files": bool(ev.get("files"))})


def take_command(payload) -> None:
    """/new, /threads or /thread, typed as Slack's slash command."""
    if not isinstance(payload, dict) or payload.get("command") not in COMMANDS:
        return
    user, team = str(payload.get("user_id") or ""), str(payload.get("team_id") or "")
    trigger, channel = str(payload.get("trigger_id") or ""), str(payload.get("channel_id") or "")
    if not (user and team and trigger and channel):
        return
    text = f"{payload['command']} {plain(str(payload.get('text') or ''))}".strip()
    # A command is no message, so it has no ts: its trigger id stands for one.
    queue({"kind": "command", "team": team, "user": user, "channel": channel, "ts": trigger,
           "native": None, "text": text, "files": False})


CURRENT: dict = {}          # the open Socket Mode connection


def hang_up() -> None:
    """Close the open connection: the Host no longer takes this connector's
    messages, and the next apps.connections.open is refused until it does."""
    ws = CURRENT.get("ws")
    if ws is not None:
        with contextlib.suppress(OSError):
            ws.sock.shutdown(socket.SHUT_RDWR)


def run_socket(url: str) -> str:
    """Read one Socket Mode connection to its end. Returns why it ended."""
    ws = Socket(url)
    CURRENT["ws"] = ws
    try:
        while True:
            raw = ws.recv()
            if raw is None:
                return "closed"
            env = _loads(raw)
            if not isinstance(env, dict):
                continue
            kind = env.get("type")
            if kind == "hello":
                mended()
                set_phase("listening")
                continue
            if kind == "disconnect":
                return str(env.get("reason") or "disconnect")
            if kind == "events_api":
                take_event(env.get("payload"))
            elif kind == "slash_commands":
                take_command(env.get("payload"))
            # Acknowledged once it is in the state file, never before.
            if env.get("envelope_id"):
                ws.send_json({"envelope_id": env["envelope_id"]})
    finally:
        CURRENT.pop("ws", None)
        ws.close()


def identify() -> None:
    """auth.test: the bot's own user id (its own messages are not the
    person's) and its workspace."""
    got = slack("auth.test")
    unusable = refusal(got, "auth.test", "bot token")
    if unusable:
        raise unusable
    if not got.get("ok") or not got.get("user_id"):
        raise OSError(f"auth.test: {got.get('error')}")
    BOT.update(user_id=str(got["user_id"]), team_id=str(got.get("team_id") or ""))


def listen_forever() -> None:
    pause, refused = 0, 0
    while True:
        if pause:
            time.sleep(pause)
        try:
            if not BOT:
                identify()
            got = slack("apps.connections.open", use="app")
            if not got.get("ok"):
                raise (refusal(got, "apps.connections.open", "app-level token")
                       or OSError(f"apps.connections.open: {got.get('error')}"))
            if not PHASE["error"]:
                set_phase("connecting")     # while broken, the page keeps saying why
            why = run_socket(str(got.get("url") or ""))
        except Refused as e:
            # Not approved yet, or revoked. Soon after a pairing the person is
            # approving it right now, so ask again often, then once a minute.
            refused += 1
            set_phase(f"the Host refuses this connector for now ({e})")
            pause = 10 if refused <= 30 else 60
            continue
        except Unusable as e:
            refused = 0
            pause = broken(e.why, e.status)
            continue
        except (OSError, ssl.SSLError, ValueError) as e:
            pause = min(60, max(5, pause * 2))
            refused = 0
            say(f"no connection to Slack this time ({cause(e)}); trying again in {pause} s")
            continue
        refused = 0
        if why == "link_disabled":
            # Socket Mode was turned off in the app's settings
            # (docs.slack.dev/apis/events-api/using-socket-mode): no new
            # connection works until the person turns it back on.
            pause = broken("link_disabled", "Slack turned Socket Mode off for this app "
                                            "(link_disabled): turn it back on at api.slack.com/apps")
            continue
        # Slack refreshes a connection every few hours, with a disconnect
        # first; a new one is opened at once.
        say(f"the Slack connection ended ({why}); opening a new one")
        pause = 0


def work_forever() -> None:
    """Hand the kept events to the Host one at a time, in order, each waited
    on until the Host has taken it (it answered with the turn)."""
    while True:
        INBOX_WAKE.clear()
        with STATE.lock:
            item = dict(STATE.inbox[0]) if STATE.inbox else None
        if item is None:
            INBOX_WAKE.wait(5)
            continue
        accepted = threading.Event()
        threading.Thread(target=on_message, args=(item, accepted), daemon=True).start()
        if not accepted.wait(ACCEPT_WAIT_S):
            say("the Host has not taken a message yet; going on with the next")
        with STATE.lock:
            k = _key(item)
            STATE.inbox = [i for i in STATE.inbox if _key(i) != k]
            STATE.seen.append(k)
            del STATE.seen[:-SEEN_MAX]
        STATE.save()


def resume() -> None:
    """Pick up the replies that were on their way when the connector stopped.
    The events not yet taken are in the inbox, which work_forever reads."""
    with STATE.lock:
        saved = dict(STATE.turns)
    for turn_id, t in saved.items():
        turn = Turn(str(t.get("chat_id")), native=t.get("native"), turn_id=turn_id,
                    sent=t.get("sent") or ())
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
            body = ("<!doctype html><meta charset=utf-8><title>Slack connector</title>"
                    f"<p>Slack connector: {html.escape(PHASE['text'])}</p>")
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
        threading.Thread(target=work_forever, daemon=True).start()
        listen_forever()
    threading.Event().wait()


if __name__ == "__main__":
    main()
