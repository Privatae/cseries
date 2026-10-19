"""Discord connector: lets ONE Discord account talk to its Host.

A resident process app. It never holds the bot token. Discord sends a bot its
direct messages over the Gateway, a WebSocket whose token travels inside the
Identify and Resume payloads (https://docs.discord.com/developers/events/gateway),
so the Host holds that connection: the connector opens a WebSocket to its Host
(GET /api/connector/gateway), the Host opens Discord's and relays the frames
both ways, and puts the bot token from Keys into the Identify (op 2) and
Resume (op 6) frames, where this connector writes a placeholder. Every REST
call (sending a message, a picture) goes through the Host too
(POST /api/connector/platform), which adds `Authorization: Bot <token>`.

What the Host does not do, this does: the heartbeat, the sequence number,
the session and resuming it. Discord's rules, read 2026-10-05 at
https://docs.discord.com/developers/events/gateway:
  - Hello (op 10) gives heartbeat_interval; the first heartbeat waits
    "heartbeat_interval * jitter" (0..1), then one every interval, each
    carrying the last sequence number; no ACK (op 11) between two heartbeats
    is a "zombied" connection: close with a code other than 1000/1001 and
    Resume;
  - READY carries session_id and resume_gateway_url, "a WebSocket URL that
    your app should use when it Resumes after a disconnect";
  - Reconnect (op 7): resume; Invalid Session (op 9): resume when d is true,
    else identify anew;
  - close codes 4004 and 4010-4014 cannot be resumed (4004: the token is
    wrong; 4014: an intent the app has not been allowed).
Intents: DIRECT_MESSAGES (1 << 12) only. The message text of a DM needs no
MESSAGE_CONTENT intent: "Content in DMs with the app" is one of its
exceptions (same page).

Direct messages only, one account, as the Telegram and Slack apps. A DM has
no threads (Discord's threads are channels in a server), so a reply carries
its reply reference: a message that answers one of the bot's (Discord's
message_reference) goes to the Host as reply_to_message_id, and the Host's
answer is sent as a reply to the person's message.

A forwarded message (message_reference type 1, FORWARD, with the original in
message_snapshots; https://docs.discord.com/developers/resources/message)
goes to the Host as that original's text, with an origin the Host labels it
by. Discord leaves the original's author out of a snapshot ("of note: author
will be excluded"), so its source is reported as hidden.

Standard library only, so there is nothing to install and no lockfile.

The platform sets PORT, PRIVATAE_APP_ID and CSERIES_KERNEL_URL, and
CSERIES_CONNECTOR_CAP once the Host has started the pairing. What has to
survive a restart (the paired account, the gateway session, the messages not
yet taken, the replies still on their way) is one JSON file in the app's data
dir.
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
import random
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
from urllib.parse import quote, urlsplit

APP_ID = os.environ.get("PRIVATAE_APP_ID", "")
HOST_URL = os.environ.get("CSERIES_KERNEL_URL", "").rstrip("/")
CAP = os.environ.get("CSERIES_CONNECTOR_CAP", "")
PORT = int(os.environ.get("PORT") or "8080")
# The app's data dir survives restarts and re-installs. CONNECTOR_STATE_DIR
# lets a test put it somewhere else.
STATE_DIR = os.environ.get("CONNECTOR_STATE_DIR") or f"/var/lib/cseries/appdata/{APP_ID}"

API = "/api/v10"
# The Host fills d.token of an Identify or Resume frame that carries this.
TOKEN_PLACEHOLDER = "cseries-connector-token"
INTENTS = 1 << 12           # DIRECT_MESSAGES
# Create Message (https://docs.discord.com/developers/resources/message):
# "Message contents (up to 2000 characters)"; SUPPRESS_EMBEDS is 1 << 2, so a
# link sends no preview.
TEXT_CHARS = 2000
SUPPRESS_EMBEDS = 1 << 2
TEXT_JSON = 12_000          # a /platform call without files is at most 16 KiB of JSON, in UTF-8 bytes
FILE_BYTES = 10 * 1024 * 1024   # the largest picture the Host streams (reply_parts.IMAGE_PART_MAX)
LINE_MAX = FILE_BYTES * 4 // 3 + 64 * 1024   # the longest line of a reply worth reading whole
HEAD_MAX = 256 * 1024       # a line longer than this is a picture's: the rest is read under PICTURE
# A rate-limited call is waited out as long as Discord says (the Host passes
# its Retry-After back as `retry_after`), up to this, then tried later.
RATE_WAIT_MAX_S = 60
RATE_TRIES = 5
RATE_FALLBACK_S = 2         # when a 429 names no wait
STREAM_READ_S = 60          # the Host sends a line at least every 10 s while a reply runs
ACCEPT_WAIT_S = 120         # how long a message waits for the Host to take it
REATTACH_TRIES = 8          # reconnects in a row that get nothing before a reply is let go
REPORT_TRIES = 4            # tries of a report of what was sent before it is let go
WS_MESSAGE_MAX = 2 * 1024 * 1024   # the Host relays at most this per frame from Discord
SEEN_MAX = 500              # messages already taken, remembered against a replay
CODE_ALPHABET = "BCDFGHJKLMNPQRSTVWXZ"   # a pairing code is 8 of these
# Discord's close codes whose Reconnect is false (Gateway Close Event Codes,
# docs.discord.com/developers/topics/opcodes-and-status-codes, read
# 2026-10-09): 4004 the token is wrong, 4010-4014 shards, the API version and
# intents. A failure that cannot fix itself is not tried again every few
# minutes: it is logged once as the error it is, shown on /health and the
# app's page, and checked again every RECHECK_S, so a token put right in
# Keys is picked up without a restart.
NO_RESUME = frozenset({4004, 4010, 4011, 4012, 4013, 4014})
RECHECK_S = 600
HOST_CLOSES = {4401: "revoked", 4409: "replaced", 4413: "a frame too large",
               4429: "over Discord's send limit", 4502: "Discord could not be reached"}
_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"   # RFC 6455 §1.3

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


def say(line: str) -> None:
    print(f"[discord-connector] {line}", flush=True)


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
        # matched and waits for the person's approval.
        self.account = data.get("account")
        self.pending = data.get("pending")
        # The gateway session, so a restart Resumes and Discord replays what
        # it sent meanwhile: {id, seq, url}.
        self.session = data.get("session") or {}
        self.inbox = data.get("inbox") or []
        self.seen = data.get("seen") or []
        self.turns = data.get("turns") or {}

    def save(self) -> None:
        with self.lock:
            snap = json.dumps({"account": self.account, "pending": self.pending,
                               "session": self.session, "inbox": self.inbox,
                               "seen": self.seen[-SEEN_MAX:], "turns": self.turns})
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                fh.write(snap)
            os.replace(tmp, self.path)


STATE = State(os.path.join(STATE_DIR, "discord-connector.json"))
# What /health and the app's page show. `error`: the failure that cannot fix
# itself, while it lasts (broken()); any other phase ends it.
PHASE = {"text": "starting", "error": ""}
BOT: dict = {}              # READY: the bot's own user id


def set_phase(text: str) -> None:
    if PHASE["text"] != text:
        PHASE.update(text=text, error="")
        say(text)


def broken(why: str, status: str) -> int:
    """A failure that cannot fix itself: logged as the error it is when it
    starts or changes, never on each check, and shown on /health (ok false)
    and the app's page until it ends. How long to wait before checking again."""
    if PHASE["error"] != why:
        say(f"connecting to Discord failed: {status}. This does not fix itself; "
            f"checking again every {RECHECK_S // 60} minutes")
    PHASE.update(text=f"{status}. Checked again every {RECHECK_S // 60} minutes.", error=why)
    return RECHECK_S


def mended() -> None:
    """The failure broken() reported is over: Discord took the session (READY
    or RESUMED)."""
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
    is then cut out. The file is the call's last value."""
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


def discord(method: str, path: str, *, json_body=None, form=None, files=None,
            wait: int = 30) -> tuple:
    """One REST call through the Host: (Discord's HTTP status, its JSON).
    Raises Refused on 403 from the Host, and OSError when the call could not
    be made now (worth trying again later): the Host busy (429) or failing
    (5xx), Discord failing (5xx), or still rate-limiting after RATE_TRIES
    waits. A rate limit is waited out as long as Discord asks: the Host
    passes its Retry-After (or the 429 body's retry_after) back as
    `retry_after`."""
    body = {"method": method, "path": API + path, "timeout": wait}
    if files:
        body["form"], body["files"] = form or {}, files
    elif json_body is not None:
        body["json"] = json_body
    for _ in range(RATE_TRIES):
        status, out = host_json("POST", "/api/connector/platform", body, timeout=wait + 20)
        if status == 403:
            raise Refused(detail(out))
        if status == 429 or status >= 500:
            raise OSError(f"the Host could not make the call now ({status}: {detail(out)})")
        if status != 200 or not isinstance(out, dict):
            return 0, {"message": f"the Host refused the call ({status}: {detail(out)})"}
        upstream, answer = out.get("status"), out.get("json")
        if upstream == 429:
            pause = out.get("retry_after")
            pause = float(pause) if isinstance(pause, (int, float)) else RATE_FALLBACK_S
            if pause > RATE_WAIT_MAX_S:
                raise OSError(f"Discord asks to wait {pause:.0f} s")
            time.sleep(pause)
            continue
        if isinstance(upstream, int) and upstream >= 500:
            raise OSError(f"Discord could not take the call now ({upstream})")
        return upstream, answer if isinstance(answer, dict) else {}
    raise OSError("Discord is still rate-limiting")


# ── sending to Discord ───────────────────────────────────────────────────

class Turn:
    """One answer of the Host on its way to a direct message."""

    def __init__(self, chat_id: str, reply_to=None, turn_id=None, sent=(), sender=None):
        self.chat_id, self.reply_to = chat_id, reply_to
        self.id = turn_id
        self.sent = set(sent)
        self.answered = False      # the first message sent answers the person's message
        self.sender = sender       # the account whose message this answers
        self.partial = {}          # seq -> the pieces of a part sent so far, and their ids


def payload(turn: Turn, content: str) -> dict:
    """A message's JSON: no mentions parsed, no link preview, and the first
    one a reply to the person's message."""
    out = {"content": content, "allowed_mentions": {"parse": []}, "flags": SUPPRESS_EMBEDS}
    if turn.reply_to and not turn.answered:
        out["message_reference"] = {"message_id": str(turn.reply_to), "fail_if_not_exists": False}
        turn.answered = True
    return out


def _fits(text: str) -> bool:
    return (len(text) <= TEXT_CHARS
            and len(json.dumps(text, ensure_ascii=False).encode("utf-8", "surrogatepass"))
            <= TEXT_JSON)


def split_text(text: str) -> list:
    """Pieces Discord takes as one message each, cut at a paragraph, a line
    or a space where one is near the limit."""
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


def post(turn: Turn, content: str):
    """Create Message. The id Discord gave it, or None when it did not take it."""
    status, answer = discord("POST", f"/channels/{turn.chat_id}/messages",
                             json_body=payload(turn, content))
    mid = answer.get("id") if status in (200, 201) else None
    if not mid:
        # Not tried again, so worded as the error it is (the app proof reads it).
        say(f"Discord did not take a message; sending it failed ({status}: {answer.get('message')})")
    return str(mid) if mid else None


def send_text(turn: Turn, text: str, seq=None) -> list:
    """Send `text` in as many messages as it takes. A part tried again after
    a send that did not go through carries on after the pieces already sent
    (turn.partial), so no piece goes out twice."""
    done = turn.partial.setdefault(seq, {"n": 0, "ids": []}) if seq is not None \
        else {"n": 0, "ids": []}
    for piece in split_text(text)[done["n"]:]:
        mid = post(turn, piece)
        if mid:
            done["ids"].append(mid)
        done["n"] += 1
    return list(done["ids"])


def send_link(turn: Turn, part: dict, seq) -> list:
    """A link as Discord's masked link [label](url), so the label is what
    shows; a URL that would break the markup goes as the label and the URL."""
    url, label = str(part.get("url") or ""), str(part.get("label") or "Open in your Host")
    if seq in turn.partial or not re.match(r"^https?://[^\s()<>]+$", url):
        return send_text(turn, f"{label}\n{url}", seq)
    turn.partial[seq] = {"n": 0, "ids": []}
    mid = post(turn, f"[{re.sub(r'[][()]', '', label) or 'Open'}]({url})")
    turn.partial[seq] = {"n": 1, "ids": [mid] if mid else []}
    return [mid] if mid else []


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


def send_image(turn: Turn, part: dict) -> list:
    """Called with PICTURE held. A Create Message with the picture as
    files[0] and the rest in payload_json, multipart/form-data built by the
    Host (Discord: "Files must be attached using a multipart/form-data body"
    with files[n] and payload_json). The base64 is bytes and goes to the Host
    as it is, never copied (_encode)."""
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
    body = payload(turn, "")
    body.pop("content", None)
    body["attachments"] = [{"id": 0, "filename": name}]
    status, answer = discord("POST", f"/channels/{turn.chat_id}/messages",
                             form={"payload_json": body},
                             files=[{"field": "files[0]", "filename": name, "mime": mime,
                                     "data_b64": raw}])
    mid = answer.get("id") if status in (200, 201) else None
    if not mid:
        say(f"Discord did not take a picture; sending it failed ({status}: {answer.get('message')})")
        return []
    return [str(mid)]


def reply(chat_id: str, message_id, text: str) -> None:
    """A line from the connector itself, never part of a Host reply."""
    send_text(Turn(chat_id, reply_to=message_id), text)


# ── one reply of the Host ────────────────────────────────────────────────

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
                                "answered": turn.answered, "sent": sorted(turn.sent)}
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
    report_sent(turn, report)


_HEAD = re.compile(rb'^\{"type":\s*"([a-z]+)",\s*"seq":\s*(\d+)')
_DATA = re.compile(rb'"data_b64":\s*"')

# One picture at a time in the whole process (the Telegram
# and Slack apps' rule): a picture is up to 14 MB of base64, read from the
# Host, sent and let go with this held.
PICTURE = threading.Lock()


def _refused(e) -> str:
    say(f"the Host refused this connector ({e}); sending nothing more")
    return "refused"


def _failed(e) -> str:
    say(f"a send did not go through ({cause(e)}); picking the reply up again")
    return "failed"


def _deliver_held(turn: Turn, part: dict):
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
    else how it ended: 'dropped', 'failed' or 'refused'. The base64 is read
    into one buffer, which is what is sent."""
    start = _HEAD.match(head)
    if start and int(start.group(2)) in turn.sent:
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


def typing(turn: Turn) -> None:
    """Trigger Typing Indicator, while the Host works on a reply."""
    with contextlib.suppress(OSError, Refused):
        discord("POST", f"/channels/{turn.chat_id}/typing")


def read_stream(resp, turn: Turn, accepted=None) -> tuple:
    """Read one NDJSON answer to its end: ('done' | 'dropped' | 'failed' |
    'refused', how many lines came)."""
    lines = 0
    with resp:
        while True:
            try:
                raw = resp.readline(HEAD_MAX)
            except (OSError, http.client.HTTPException):
                return "dropped", lines
            if not raw:
                return "dropped", lines
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
                        if not part.get("mine") and part.get("note"):
                            send_text(turn, str(part["note"]))
                    elif part.get("turn_id"):
                        turn.id = str(part["turn_id"])
                        if not claim(turn):
                            return "done", lines    # another stream is sending it
                        remember(turn)
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
    same reply."""
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
        if (lines and outcome != "failed") or len(turn.sent) > before:
            misses = 0
    forget(turn)


# ── one message from Discord ─────────────────────────────────────────────

def pairing_code(text: str):
    code = re.sub(r"[\s-]+", "", text or "").upper()
    return code if len(code) == 8 and all(c in CODE_ALPHABET for c in code) else None


def pair(code: str, account: str, name: str, chat_id: str, message_id: str) -> bool:
    """Report a pairing code. True when it was one (matched or not); False
    when no pairing is open, so the text is an ordinary message."""
    status, answer = host_json("POST", "/api/connector/pair",
                               {"code": code, "account_id": account, "display_name": name})
    why = detail(answer)
    if status == 200:
        with STATE.lock:
            STATE.pending = account
        STATE.save()
        say(f"paired Discord account {account}; the person approves it in the Host")
        try:
            reply(chat_id, message_id, PAIRED_TEXT)
        except Refused:
            say("the bot can answer once the person has approved the account")
        return True
    if status == 403 and why.startswith("wrong code"):
        reply(chat_id, message_id, VOID_TEXT if "fifth" in why else WRONG_CODE_TEXT)
        return True
    if status == 410:
        reply(chat_id, message_id, EXPIRED_TEXT)
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
    account, chat_id, message_id = item["user"], item["channel"], item["id"]
    origin = item.get("origin")
    with STATE.lock:
        mine = {a for a in (STATE.account, STATE.pending) if a}
    text = item.get("text") or ""
    # Someone else's words are never the person's pairing code.
    code = pairing_code(text) if origin is None else None
    if code and pair(code, account, item.get("name") or "", chat_id, message_id):
        return
    if mine and account not in mine:
        say("ignored a message from a Discord account that is not the paired one")
        return
    body = {"chat_id": chat_id, "message_id": message_id,
            "text": text or ("(sent a file; this connector passes on text only)"
                             if item.get("files") else "(sent a message with no text)"),
            "account_id": account, "display_name": item.get("name") or ""}
    if item.get("reply_to"):
        body["reply_to_message_id"] = item["reply_to"]
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
            reply(chat_id, message_id, NOT_YET_TEXT)
        elif e.code == 403 and not mine and why == "this connector is not paired":
            reply(chat_id, message_id, UNPAIRED_TEXT)
        if e.code < 500:
            return
        resp = None  # the Host is busy: posted again below
    except (OSError, http.client.HTTPException):
        resp = None  # posted again below; the Host takes a message once
    follow(resp, Turn(chat_id, reply_to=message_id, sender=account), accepted, again=body)


def forward_of(msg: dict):
    """(the forwarded message's text, its origin) for a forward, else None.
    message_reference type 1 is FORWARD and message_snapshots holds the
    original, without its author."""
    ref = msg.get("message_reference")
    snaps = msg.get("message_snapshots")
    if not isinstance(ref, dict) or ref.get("type") != 1 or not isinstance(snaps, list) or not snaps:
        return None
    inner = (snaps[0] or {}).get("message") if isinstance(snaps[0], dict) else None
    if not isinstance(inner, dict):
        return None
    text = str(inner.get("content") or "").strip()
    if not text:
        text = ("(forwarded a file; this connector passes on text only)"
                if inner.get("attachments") else "(forwarded a message with no text)")
    return text, {"kind": "forward", "source": "hidden"}


def take_message(msg: dict) -> None:
    """A MESSAGE_CREATE, when it is a person's direct message to this bot."""
    if not isinstance(msg, dict) or msg.get("guild_id"):
        return          # a server channel: direct messages only
    author = msg.get("author") if isinstance(msg.get("author"), dict) else {}
    uid = str(author.get("id") or "")
    if not uid or author.get("bot") or uid == BOT.get("user_id"):
        return
    mid, channel = str(msg.get("id") or ""), str(msg.get("channel_id") or "")
    if not mid or not channel:
        return
    item = {"user": uid, "channel": channel, "id": mid,
            "name": str(author.get("global_name") or author.get("username") or ""),
            "text": str(msg.get("content") or "").strip(),
            "files": bool(msg.get("attachments")), "reply_to": None}
    fwd = forward_of(msg)
    if fwd is not None:
        item["text"], item["origin"] = fwd
    else:
        ref = msg.get("message_reference")
        if isinstance(ref, dict) and ref.get("type", 0) == 0 and ref.get("message_id"):
            item["reply_to"] = str(ref["message_id"])
    queue(item)


INBOX_WAKE = threading.Event()


def queue(item: dict) -> None:
    """Keep a message until the Host has taken it; once is enough."""
    k = item["id"]
    with STATE.lock:
        if k in STATE.seen or any(i.get("id") == k for i in STATE.inbox):
            return
        STATE.inbox.append(item)
    STATE.save()
    INBOX_WAKE.set()


def work_forever() -> None:
    """Hand the kept messages to the Host one at a time, in order, each
    waited on until the Host has taken it (it answered with the turn)."""
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
            STATE.inbox = [i for i in STATE.inbox if i.get("id") != item["id"]]
            STATE.seen.append(item["id"])
            del STATE.seen[:-SEEN_MAX]
        STATE.save()


# ── the gateway, through the Host ────────────────────────────────────────

class Socket:
    """A WebSocket client (RFC 6455) to the Host's /api/connector/gateway:
    text frames both ways, pings answered. Over TLS when the Host's URL is
    https."""

    def __init__(self, url: str):
        u = urlsplit(url)
        host, tls = u.hostname or "", u.scheme == "wss"
        port = u.port or (443 if tls else 80)
        raw = socket.create_connection((host, port), timeout=30)
        try:
            self.sock = (ssl.create_default_context().wrap_socket(raw, server_hostname=host)
                         if tls else raw)
        except (OSError, ssl.SSLError):
            raw.close()
            raise
        self.lock = threading.Lock()
        self.buf = bytearray()
        self.close_code = None
        key = base64.b64encode(os.urandom(16)).decode()
        path = (u.path or "/") + (f"?{u.query}" if u.query else "")
        self.sock.sendall((f"GET {path} HTTP/1.1\r\nHost: {u.netloc}\r\n"
                           "Upgrade: websocket\r\nConnection: Upgrade\r\n"
                           f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n"
                           f"X-CSeries-App-Id: {APP_ID}\r\nX-CSeries-Connector-Cap: {CAP}\r\n\r\n"
                           ).encode("ascii"))
        while b"\r\n\r\n" not in self.buf:
            if len(self.buf) > 16 * 1024:
                raise OSError("the socket's handshake answer is too long")
            self._fill()
        head, _, rest = bytes(self.buf).partition(b"\r\n\r\n")
        self.buf = bytearray(rest)
        lines = head.decode("latin-1").split("\r\n")
        status = lines[0].split()[1] if len(lines[0].split()) >= 2 else ""
        if status == "403":
            raise Refused("the Host refused the gateway")
        if status != "101":
            # The code alone: its reason phrase ("500 Internal Server Error")
            # would make the line that says it tries again read as an error.
            raise OSError("the gateway was not opened (the Host answered "
                          + (status if status.isdigit() else "with no HTTP status") + ")")
        headers = {k.strip().lower(): v.strip() for k, _, v in
                   (ln.partition(":") for ln in lines[1:])}
        want = base64.b64encode(hashlib.sha1((key + _GUID).encode()).digest()).decode()
        if headers.get("sec-websocket-accept") != want:
            raise OSError("the socket's handshake did not answer this connector's key")
        self.sock.settimeout(None)

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
        self.send(0x1, json.dumps(obj, separators=(",", ":")).encode("utf-8"))

    def recv(self):
        """The next text message, or None when the socket closes (its code in
        close_code). Raises OSError when it broke."""
        message = bytearray()
        while True:
            b0, b1 = self._take(2)
            fin, opcode = b0 & 0x80, b0 & 0x0F
            n = b1 & 0x7F
            if n == 126:
                n = struct.unpack(">H", self._take(2))[0]
            elif n == 127:
                n = struct.unpack(">Q", self._take(8))[0]
            if n > WS_MESSAGE_MAX or len(message) + n > WS_MESSAGE_MAX:
                raise OSError("a socket message too large to be Discord's")
            mask = self._take(4) if b1 & 0x80 else None
            payload = self._take(n)
            if mask:
                payload = (int.from_bytes(payload, "big") ^ int.from_bytes(
                    (mask * (n // 4 + 1))[:n], "big")).to_bytes(n, "big")
            if opcode == 0x8:                       # close
                self.close_code = struct.unpack(">H", payload[:2])[0] if len(payload) >= 2 else 1005
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

    def close(self, code: int = 1000) -> None:
        with contextlib.suppress(OSError):
            self.send(0x8, struct.pack(">H", code))
        with contextlib.suppress(OSError):
            self.sock.shutdown(socket.SHUT_RDWR)
        with contextlib.suppress(OSError):
            self.sock.close()


CURRENT: dict = {}          # the open gateway connection


def gateway_url(resume: bool) -> str:
    base = HOST_URL.replace("https://", "wss://", 1).replace("http://", "ws://", 1)
    url = base + "/api/connector/gateway"
    with STATE.lock:
        r = STATE.session.get("url") if resume else None
    return url + (f"?url={quote(str(r), safe='')}" if r else "")


class Beat:
    """The heartbeat: op 1 with the last sequence number, every interval;
    a heartbeat whose ACK has not come by the next one closes the
    connection (Discord's zombied connection) with 4000, which keeps the
    session resumable."""

    def __init__(self, ws: Socket, interval_ms: float):
        self.ws, self.interval = ws, max(1.0, interval_ms / 1000.0)
        self.acked = True
        self.stop = threading.Event()
        threading.Thread(target=self.run, daemon=True).start()

    def beat(self) -> None:
        with STATE.lock:
            seq = STATE.session.get("seq")
        self.ws.send_json({"op": 1, "d": seq})

    def run(self) -> None:
        if self.stop.wait(self.interval * random.random()):
            return
        while not self.stop.is_set():
            if not self.acked:
                say("no heartbeat ACK from Discord; reconnecting")
                self.ws.close(4000)
                return
            self.acked = False
            with contextlib.suppress(OSError):
                self.beat()
            if self.stop.wait(self.interval):
                return


def identify(ws: Socket) -> None:
    ws.send_json({"op": 2, "d": {"token": TOKEN_PLACEHOLDER, "intents": INTENTS,
                                 "properties": {"os": "linux", "browser": "cseries-connector",
                                                "device": "cseries-connector"}}})


def run_gateway() -> str:
    """Read one gateway connection to its end. Returns why it ended:
    'resume', 'identify', or 'fatal:<code>'."""
    with STATE.lock:
        resumable = bool(STATE.session.get("id") and STATE.session.get("seq") is not None)
    ws = Socket(gateway_url(resumable))
    CURRENT["ws"] = ws
    beat = None
    try:
        hello = _loads(ws.recv())
        if not isinstance(hello, dict) or hello.get("op") != 10:
            raise OSError("the gateway did not start with Hello")
        beat = Beat(ws, float((hello.get("d") or {}).get("heartbeat_interval") or 41250))
        if resumable:
            with STATE.lock:
                s = dict(STATE.session)
            ws.send_json({"op": 6, "d": {"token": TOKEN_PLACEHOLDER, "session_id": s["id"],
                                         "seq": s["seq"]}})
        else:
            identify(ws)
        while True:
            raw = ws.recv()
            if raw is None:
                code = ws.close_code or 1005
                if code == 4401:
                    raise Refused("the Host revoked this connector")
                if code in NO_RESUME:
                    return f"fatal:{code}"
                say(f"the gateway closed ({code}: {HOST_CLOSES.get(code, 'Discord')})")
                return "resume"
            ev = _loads(raw)
            if not isinstance(ev, dict):
                continue
            op = ev.get("op")
            if op == 0:
                if isinstance(ev.get("s"), int):
                    with STATE.lock:
                        STATE.session["seq"] = ev["s"]
                t, d = ev.get("t"), ev.get("d") if isinstance(ev.get("d"), dict) else {}
                if t == "READY":
                    BOT["user_id"] = str((d.get("user") or {}).get("id") or "")
                    with STATE.lock:
                        STATE.session.update(id=d.get("session_id"),
                                             url=d.get("resume_gateway_url"))
                    STATE.save()
                    mended()
                    set_phase("listening")
                elif t == "RESUMED":
                    mended()
                    set_phase("listening")
                elif t == "MESSAGE_CREATE":
                    take_message(d)
                    STATE.save()
            elif op == 1:
                beat.beat()
            elif op == 11:
                beat.acked = True
            elif op == 7:
                return "resume"
            elif op == 9:
                if not ev.get("d"):
                    with STATE.lock:
                        STATE.session = {}
                    STATE.save()
                time.sleep(1 + 4 * random.random())
                return "identify"
    finally:
        if beat is not None:
            beat.stop.set()
        CURRENT.pop("ws", None)
        ws.close(4000)


def hang_up() -> None:
    ws = CURRENT.get("ws")
    if ws is not None:
        with contextlib.suppress(OSError):
            ws.sock.shutdown(socket.SHUT_RDWR)


def listen_forever() -> None:
    pause, refused = 0, 0
    while True:
        if pause:
            time.sleep(pause)
        try:
            if not PHASE["error"]:
                set_phase("connecting")     # while broken, the page keeps saying why
            why = run_gateway()
        except Refused as e:
            # Not paired yet, or revoked. Soon after a pairing the person is
            # approving it right now, so ask again often, then once a minute.
            refused += 1
            set_phase(f"the Host refuses this connector for now ({e})")
            pause = 10 if refused <= 30 else 60
            continue
        except (OSError, ssl.SSLError, ValueError) as e:
            pause = min(60, max(5, pause * 2))
            refused = 0
            say(f"no connection to Discord this time ({cause(e)}); trying again in {pause} s")
            continue
        refused = 0
        if why.startswith("fatal:"):
            code = int(why.split(":")[1])
            with STATE.lock:
                STATE.session = {}
            STATE.save()
            pause = broken(f"close code {code}", f"Discord refused the bot ({code}): "
                           + ("the token in Keys is not this bot's" if code == 4004
                              else "check the bot's settings in the Discord Developer Portal"))
            continue
        pause = 1


def resume() -> None:
    """Pick up the replies that were on their way when the connector stopped."""
    with STATE.lock:
        saved = dict(STATE.turns)
    for turn_id, t in saved.items():
        turn = Turn(str(t.get("chat_id")), reply_to=t.get("reply_to"), turn_id=turn_id,
                    sent=t.get("sent") or ())
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
            body = ("<!doctype html><meta charset=utf-8><title>Discord connector</title>"
                    f"<p>Discord connector: {html.escape(PHASE['text'])}</p>")
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
        resume()
        threading.Thread(target=work_forever, daemon=True).start()
        listen_forever()
    threading.Event().wait()


if __name__ == "__main__":
    main()
