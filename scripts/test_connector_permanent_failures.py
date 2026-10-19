#!/usr/bin/env python3
"""Gate: a failure a connector cannot fix by trying again is said once as an
error, checked again slowly, and shown on the connector's status; a token put
right recovers without a restart; a failure worth trying again stays a retry.

Before this, the connectors retried such failures forever and logged them as
ordinary retries: Slack's sign-in check refused (a revoked token) came back as
"no connection to Slack this time (auth.test: invalid_auth); trying again in
5 s", Telegram's revoked token as "getUpdates: Unauthorized" every 5 s, and
Discord's close code 4004 every 300 s, so the app proof never read them as
errors and the person was never told the connection was dead.

Two readers decide whether a line an app logged is an error:
brain/mastercode/runtime/tools.py _LOG_ERROR_RE (app_status: log_errors and
its verdict) and app_verify._ERROR_RE (the delivery proof). The status the
Host and the person read is the connector's own ready_path, /health
({ok, phase, error}: system.app_status's `ready`), and its page (the app's
window), both served by connector.py's Health handler.

Each system-apps/<id>/connector.py is loaded as a module, its call to the
Host (_request), its socket class and its clock replaced, and its own loop
(Telegram's poll_forever, Slack's and Discord's listen_forever) runs against
a scripted Host until the script ends. /health and the page are read over
HTTP from the connector's real Health handler, mid-run.

  T  Telegram: getUpdates answered 401 Unauthorized (a revoked or wrong
     token) and 404 Not Found (a malformed one), as api.telegram.org answers
     made-up tokens (measured)
  S  Slack: auth.test refused (token_revoked, invalid_auth; account_inactive
     and missing_scope once each), apps.connections.open refused for the
     app-level token, and Socket Mode turned off (disconnect link_disabled)
  D  Discord: the gateway closed with 4004 (the token is not the bot's) and
     4014 (an intent the bot is not allowed), read by the connector's own
     Socket from the frames the Host relays
  For each:
    1  one line both readers take for an error, however many checks fail the
       same way; a different refusal is said once more
    2  every check after it waits RECHECK_S (10 minutes), never seconds
    3  /health answers ok false with the refusal as its error, and the page
       says what was refused
    4  the token put right: the next check goes through, a plain line says so,
       /health is ok again and the page says listening
    5  a failure worth trying again while the refusal lasts: worded as a retry,
       no error line, the refusal still on /health
  G  a failure a connector gives up on reads as an error to both readers: a
     message the platform did not take, a picture it did not take, the Host
     not recording what was sent, a part of a reply too large to read, and
     Telegram's update too large to read
  R  the report of what was sent (POST /api/connector/messages/sent) timing
     out: the part is already marked sent, so nothing else reports it again.
     Tried again with the wait growing, the same report each time, worded as
     a retry; when it goes through the reply goes on with no error line, and
     when every try times out it is given up once, as an error both readers
     take, and the reply still goes on
  N  guards, which pass before this change too and must keep passing: a
     failure worth trying again at the sign-in is no refusal (Telegram's 500,
     Slack's internal_error, the Host's own 409, Discord's resumable 4000)

No network past loopback, no database, no container.
Run: APP_ROOT=<repo> python3 scripts/test_connector_permanent_failures.py
"""
from __future__ import annotations

import contextlib
import errno
import html
import importlib.util
import io
import json
import os
import sys
import tempfile
import threading
import time
import types
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(os.environ.get("APP_ROOT") or Path(__file__).resolve().parents[1])
sys.path[:0] = [str(ROOT), str(ROOT / "brain"), str(ROOT / "scripts")]
from gate_loopback import ClientClosesFirst  # noqa: E402
# connector.py is loaded from system-apps/<id>/, which
# scripts/rebuild_factory_bundles.sh packs and signs whole: no bytecode there.
sys.dont_write_bytecode = True
for _k in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
    os.environ.pop(_k, None)

from mastercode.runtime import app_verify  # noqa: E402
from mastercode.runtime import tools as host_tools  # noqa: E402

READERS = {"tools._LOG_ERROR_RE": host_tools._LOG_ERROR_RE,
           "app_verify._ERROR_RE": app_verify._ERROR_RE}
APPS = ("telegram", "slack", "discord")
_TMP = tempfile.TemporaryDirectory(prefix="connector-permanent-")
fails: list[str] = []
counted = {"fix": 0, "guard": 0}


def check(label, cond, detail="", *, guard: bool = False):
    counted["guard" if guard else "fix"] += 1
    print(("  PASS  " if cond else "  FAIL  ") + ("(guard) " if guard else "") + label
          + (("   [" + str(detail)[:1500] + "]") if detail and not cond else ""))
    if not cond:
        fails.append(label)


def as_error(line: str) -> list[str]:
    """The readers that take `line` for an error."""
    return [name for name, rx in READERS.items() if rx.search(line)]


def errors(lines) -> list[str]:
    """The lines both readers take for an error."""
    return [ln for ln in lines if len(as_error(ln)) == len(READERS)]


# ── a connector, loaded as a module ──────────────────────────────────────

def load(app: str):
    """system-apps/<app>/connector.py as a module, its settings from the
    environment as the platform sets them, its state file in a scratch dir."""
    os.environ.update({"PRIVATAE_APP_ID": app, "CSERIES_KERNEL_URL": "http://127.0.0.1:9",
                       "CSERIES_CONNECTOR_CAP": "gate-cap", "PORT": "0",
                       "CONNECTOR_STATE_DIR": os.path.join(_TMP.name, app)})
    spec = importlib.util.spec_from_file_location(f"{app}_connector_permanent",
                                                  ROOT / "system-apps" / app / "connector.py")
    c = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(c)
    return c


class Stop(Exception):
    """The script is over: ends the connector's loop (no handler catches it)."""


SLEPT = "<<slept "


def clocked(c) -> None:
    """The connector's clock: each sleep is written into its log as a marker
    line, in order with what it says, and takes no time."""
    def sleep(seconds):
        print(f"{SLEPT}{seconds}>>", flush=True)
    c.time = types.SimpleNamespace(sleep=sleep, monotonic=time.monotonic, time=time.time)


def run(fn) -> list[str]:
    """What the connector printed (and slept) while fn ran to the script's end."""
    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.suppress(Stop):
        fn()
    return [ln for ln in out.getvalue().splitlines() if ln.strip()]


def said(lines) -> list[str]:
    return [ln for ln in lines if not ln.startswith(SLEPT)]


def sleeps(lines) -> list[float]:
    return [float(ln[len(SLEPT):-2]) for ln in lines if ln.startswith(SLEPT)]


def split_at(lines, marker: str) -> tuple[list[str], list[str]]:
    """The lines before the first one holding `marker`, and from it on."""
    for i, ln in enumerate(lines):
        if marker in ln:
            return lines[:i], lines[i:]
    return lines, []


# ── the connector's status, read as the Host and the person read it ──────

_DIRECT = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def status(c) -> dict:
    """/health and the page from the connector's own Health handler, served
    on loopback for this one read."""
    class Health(ClientClosesFirst, c.Health):
        """The connector's own handler. After it has answered, the server waits
        for the reader to hang up before it does, so the TIME_WAIT is left on
        the reader's side (scripts/gate_loopback.py)."""
    srv = c.V4Only(("127.0.0.1", 0), Health)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        port = srv.server_address[1]
        with _DIRECT.open(f"http://127.0.0.1:{port}/health", timeout=5) as r:
            health = json.loads(r.read())
        with _DIRECT.open(f"http://127.0.0.1:{port}/", timeout=5) as r:
            page = r.read().decode("utf-8")
    finally:
        srv.shutdown()
        srv.server_close()
        t.join(5)
    return {"health": health, "page": page}


def refused_on_status(st: dict, words: str) -> bool:
    """/health says not ready, with an error, and both it and the page say
    what was refused."""
    h = st.get("health") or {}
    return (h.get("ok") is False and bool(h.get("error"))
            and words in str(h.get("phase")) and html.escape(words) in st.get("page", ""))


def listening_on_status(st: dict) -> bool:
    h = st.get("health") or {}
    return (h.get("ok") is True and "error" not in h and h.get("phase") == "listening"
            and "connector: listening" in st.get("page", ""))


# ── the Host, scripted ───────────────────────────────────────────────────

class Answer(io.BytesIO):
    """An answer as urllib's opener returns it: a status and a body."""

    def __init__(self, obj, status: int = 200):
        super().__init__(json.dumps(obj).encode())
        self.status = status


def broker(status, body_json, body_text: str = "", truncated: bool = False) -> dict:
    """The Host's 200 for a platform call: what the platform answered."""
    return {"status": status, "json": body_json, "body": body_text, "truncated": truncated}


def host_error(code: int, detail: str) -> urllib.error.HTTPError:
    """The Host answering `code` with FastAPI's {"detail": ...}, as urllib
    raises it."""
    return urllib.error.HTTPError("http://127.0.0.1:9/api/connector", code, "gate", {},
                                  io.BytesIO(json.dumps({"detail": detail}).encode()))


def refused_conn() -> urllib.error.URLError:
    return urllib.error.URLError(ConnectionRefusedError(errno.ECONNREFUSED, "Connection refused"))


SNAPS: dict = {}


def scripted(c, steps):
    """The connector's _request: each call to the Host takes the next step,
    ("answer", the Host's JSON) or ("raise", exception); ("snap", name) steps
    first record the connector's status. Stop once the steps are spent. An
    answer is built for each call: the connector closes what it reads."""
    steps = list(steps)

    def request(method, path, body=None, timeout=60.0):
        while steps and steps[0][0] == "snap":
            SNAPS[steps.pop(0)[1]] = status(c)
        if not steps:
            raise Stop()
        kind, what = steps.pop(0)
        if kind == "raise":
            raise what
        return Answer(what)
    return request


def tg(code: int, description: str):
    return ("answer", broker(code, {"ok": False, "error_code": code,
                                    "description": description}))


TG_OK = ("answer", broker(200, {"ok": True, "result": []}))


def sl(answer: dict):
    return ("answer", broker(200, answer))


SL_AUTH_OK = sl({"ok": True, "user_id": "UBOT", "team_id": "T0GATE"})
SL_OPEN_OK = sl({"ok": True, "url": "wss://wss-primary.slack.com/link/?ticket=gate"})


def sl_refused(code: str):
    return sl({"ok": False, "error": code})


class SlackSocket:
    """Slack's Socket Mode socket: each one opened plays the next connection
    of PLAN, a list of the frames Slack sends, then closes."""
    PLAN: list = []

    def __init__(self, url):
        if not SlackSocket.PLAN:
            raise Stop()
        self.frames = list(SlackSocket.PLAN.pop(0))
        self.sock, self.lock = None, threading.Lock()

    def recv(self):
        return self.frames.pop(0) if self.frames else None

    def send_json(self, obj):
        pass

    def close(self):
        pass


HELLO = json.dumps({"type": "hello", "num_connections": 1})
LINK_DISABLED = json.dumps({"type": "disconnect", "reason": "link_disabled"})


class Gateway:
    """The gateway as the Host relays it: each socket the connector opens
    plays the next item of PLAN, ("snap", name) or (frames, close code); a
    ("snap", name) among the frames records the status at that point."""
    PLAN: list = []
    MODULE = None

    def __init__(self, url):
        while Gateway.PLAN and Gateway.PLAN[0][0] == "snap":
            SNAPS[Gateway.PLAN.pop(0)[1]] = status(Gateway.MODULE)
        if not Gateway.PLAN:
            raise Stop()
        frames, self.code = Gateway.PLAN.pop(0)
        self.frames = list(frames)
        self.sock, self.lock, self.close_code = None, threading.Lock(), None

    def recv(self):
        while self.frames and isinstance(self.frames[0], tuple):
            SNAPS[self.frames.pop(0)[1]] = status(Gateway.MODULE)
        if self.frames:
            return self.frames.pop(0)
        self.close_code = self.code
        return None

    def send_json(self, obj):
        pass

    def close(self, code: int = 1000):
        pass


GW_HELLO = json.dumps({"op": 10, "d": {"heartbeat_interval": 45000}})
GW_READY = json.dumps({"op": 0, "t": "READY", "s": 1,
                       "d": {"v": 10, "user": {"id": "1300", "bot": True},
                             "session_id": "s1", "resume_gateway_url": "wss://gateway.discord.gg"}})


# ── the refusal, the slow checks, the status, the fix ────────────────────

def expect_refusal(app: str, label: str, c, lines: list[str], *, words: str, snap: str,
                   fixed_snap: str | None, tries: int) -> None:
    """Checks 1 to 4 over one run: `tries` refusals in a row, then (when
    fixed_snap names a snapshot) the token put right."""
    before, after = split_at(lines, "connected again")
    errs = errors(said(before))
    check(f"{app} {label}: 1 one line both readers take for an error over {tries} refusals, "
          f"saying {words!r}", len(errs) == 1 and words in errs[0], said(before))
    wait = getattr(c, "RECHECK_S", None)
    waits = sleeps(before)
    check(f"{app} {label}: 2 every check after it waits RECHECK_S (10 minutes), never seconds",
          wait == 600 and waits == [wait] * tries, (wait, waits))
    check(f"{app} {label}: 3 /health says not ready with the refusal as its error, and the "
          "page says what was refused", refused_on_status(SNAPS.get(snap) or {}, words),
          SNAPS.get(snap))
    if fixed_snap is None:
        return
    plain = [ln for ln in said(after) if "connected again" in ln]
    check(f"{app} {label}: 4 the token put right is taken at the next check: one plain line "
          "says so, /health is ok and the page says listening",
          len(plain) == 1 and not as_error(plain[0]) and not errors(said(after))
          and listening_on_status(SNAPS.get(fixed_snap) or {}),
          (said(after), SNAPS.get(fixed_snap)))


def telegram_runs() -> None:
    print("[T] system-apps/telegram/connector.py")
    c = load("telegram")
    clocked(c)
    c._request = scripted(c, [tg(401, "Unauthorized"), tg(401, "Unauthorized"),
                              ("snap", "tg-refused"), tg(401, "Unauthorized"),
                              TG_OK, ("snap", "tg-fixed")])
    expect_refusal("telegram", "T getUpdates answered 401 Unauthorized", c, run(c.poll_forever),
                   words="Unauthorized", snap="tg-refused", fixed_snap="tg-fixed", tries=3)

    c = load("telegram")
    clocked(c)
    c._request = scripted(c, [tg(404, "Not Found"), tg(404, "Not Found"),
                              ("snap", "tg-404"), tg(404, "Not Found")])
    expect_refusal("telegram", "T getUpdates answered 404 Not Found (a malformed token)", c,
                   run(c.poll_forever), words="Not Found", snap="tg-404", fixed_snap=None, tries=3)

    # 5 and 1 (a different refusal): a retry in the middle, then 404 after 401.
    c = load("telegram")
    clocked(c)
    c._request = scripted(c, [tg(401, "Unauthorized"), ("raise", refused_conn()),
                              ("snap", "tg-retry"), tg(401, "Unauthorized"),
                              tg(404, "Not Found")])
    lines = run(c.poll_forever)
    retry = [ln for ln in said(lines) if "no updates this time" in ln]
    check("telegram T 5 a refused connection while the refusal lasts is worded as a retry, no "
          "error, and the refusal stays on /health",
          len(retry) == 1 and "connection refused" in retry[0] and not as_error(retry[0])
          and refused_on_status(SNAPS.get("tg-retry") or {}, "Unauthorized"),
          (said(lines), SNAPS.get("tg-retry")))
    errs = errors(said(lines))
    check("telegram T 1 a different refusal (404 after 401) is said once more, and only once",
          len(errs) == 2 and "Unauthorized" in errs[0] and "Not Found" in errs[1], said(lines))


def slack_runs() -> None:
    print("[S] system-apps/slack/connector.py")
    c = load("slack")
    clocked(c)
    c.Socket = SlackSocket
    SlackSocket.PLAN[:] = [[HELLO]]
    c._request = scripted(c, [sl_refused("token_revoked"), sl_refused("token_revoked"),
                              ("snap", "sl-refused"), sl_refused("token_revoked"),
                              SL_AUTH_OK, SL_OPEN_OK, ("snap", "sl-fixed")])
    expect_refusal("slack", "S auth.test answered token_revoked (the bot token)", c,
                   run(c.listen_forever), words="token_revoked", snap="sl-refused",
                   fixed_snap="sl-fixed", tries=3)

    c = load("slack")
    clocked(c)
    c.Socket = SlackSocket
    c._request = scripted(c, [sl_refused("invalid_auth"), ("snap", "sl-invalid"),
                              sl_refused("invalid_auth")])
    expect_refusal("slack", "S auth.test answered invalid_auth", c, run(c.listen_forever),
                   words="invalid_auth", snap="sl-invalid", fixed_snap=None, tries=2)

    c = load("slack")
    clocked(c)
    c.Socket = SlackSocket
    SlackSocket.PLAN[:] = [[HELLO]]
    c._request = scripted(c, [SL_AUTH_OK, sl_refused("invalid_auth"), ("snap", "sl-app"),
                              sl_refused("invalid_auth"), SL_OPEN_OK, ("snap", "sl-app-fixed")])
    lines = run(c.listen_forever)
    expect_refusal("slack", "S apps.connections.open answered invalid_auth (the app-level token)",
                   c, lines, words="apps.connections.open: invalid_auth", snap="sl-app",
                   fixed_snap="sl-app-fixed", tries=2)
    errs = errors(said(lines))
    check("slack S the line and the page name the app-level token, not the bot token",
          errs[:1] and "app-level token" in errs[0]
          and "app-level token" in str(((SNAPS.get("sl-app") or {}).get("health") or {}).get("phase")),
          errs)

    c = load("slack")
    clocked(c)
    c.Socket = SlackSocket
    SlackSocket.PLAN[:] = [[HELLO, LINK_DISABLED], [LINK_DISABLED], [HELLO]]
    c._request = scripted(c, [SL_AUTH_OK, SL_OPEN_OK, SL_OPEN_OK, ("snap", "sl-link"),
                              SL_OPEN_OK, ("snap", "sl-link-fixed")])
    expect_refusal("slack", "S Socket Mode turned off (disconnect link_disabled)", c,
                   run(c.listen_forever), words="link_disabled", snap="sl-link",
                   fixed_snap="sl-link-fixed", tries=2)

    # 5 and 1 (a different refusal), and the advice for a scope.
    c = load("slack")
    clocked(c)
    c.Socket = SlackSocket
    c._request = scripted(c, [sl_refused("invalid_auth"), ("raise", refused_conn()),
                              ("snap", "sl-retry"), sl_refused("invalid_auth"),
                              sl_refused("account_inactive"), sl_refused("missing_scope"),
                              ("snap", "sl-scope")])
    lines = run(c.listen_forever)
    retry = [ln for ln in said(lines) if "no connection to Slack this time" in ln]
    check("slack S 5 a refused connection while the refusal lasts is worded as a retry, no "
          "error, and the refusal stays on /health",
          len(retry) == 1 and "connection refused" in retry[0] and not as_error(retry[0])
          and refused_on_status(SNAPS.get("sl-retry") or {}, "invalid_auth"),
          (said(lines), SNAPS.get("sl-retry")))
    errs = errors(said(lines))
    check("slack S 1 each different refusal (invalid_auth, account_inactive, missing_scope) is "
          "said once", len(errs) == 3 and all(w in e for w, e in zip(
              ("invalid_auth", "account_inactive", "missing_scope"), errs)), said(lines))
    phase = str(((SNAPS.get("sl-scope") or {}).get("health") or {}).get("phase"))
    check("slack S 3 a missing scope sends the person to the Slack app's settings, a bad token "
          "to Keys", "api.slack.com/apps" in phase and "Keys" in errs[0] if errs else False,
          (phase, errs))


def discord_runs() -> None:
    print("[D] system-apps/discord/connector.py")
    c = load("discord")
    clocked(c)
    c.Socket, Gateway.MODULE = Gateway, c
    Gateway.PLAN[:] = [([GW_HELLO], 4004), ([GW_HELLO], 4004), ("snap", "dc-refused"),
                       ([GW_HELLO], 4004), ([GW_HELLO, GW_READY, ("snap", "dc-fixed")], 1000)]
    expect_refusal("discord", "D the gateway closed with 4004 (the token is not the bot's)", c,
                   run(c.listen_forever), words="Discord refused the bot (4004)",
                   snap="dc-refused", fixed_snap="dc-fixed", tries=3)

    c = load("discord")
    clocked(c)
    c.Socket, Gateway.MODULE = Gateway, c
    Gateway.PLAN[:] = [([GW_HELLO], 4014), ("snap", "dc-4014"), ([GW_HELLO], 4014)]
    expect_refusal("discord", "D the gateway closed with 4014 (an intent not allowed)", c,
                   run(c.listen_forever), words="Discord refused the bot (4014)",
                   snap="dc-4014", fixed_snap=None, tries=2)

    # 5 and 1 (a different refusal): a resumable close in the middle.
    c = load("discord")
    clocked(c)
    c.Socket, Gateway.MODULE = Gateway, c
    Gateway.PLAN[:] = [([GW_HELLO], 4004), ([GW_HELLO], 4000), ("snap", "dc-retry"),
                       ([GW_HELLO], 4004), ([GW_HELLO], 4014)]
    lines = run(c.listen_forever)
    retry = [ln for ln in said(lines) if "the gateway closed (4000" in ln]
    check("discord D 5 a close Discord lets the bot resume (4000) while the refusal lasts is no "
          "error, and the refusal stays on /health",
          len(retry) == 1 and not as_error(retry[0])
          and refused_on_status(SNAPS.get("dc-retry") or {}, "Discord refused the bot (4004)"),
          (said(lines), SNAPS.get("dc-retry")))
    errs = errors(said(lines))
    check("discord D 1 a different refusal (4014 after 4004) is said once more, and only once",
          len(errs) == 2 and "4004" in errs[0] and "4014" in errs[1], said(lines))


# ── G: what a connector gives up on ──────────────────────────────────────

PNG_B64 = b"iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="


def by_path(routes: dict):
    """_request answering by path: {path: the Host's JSON, or a callable that
    makes the exception to raise}, a fresh answer for each call."""
    def request(method, path, body=None, timeout=60.0):
        got = routes[path]
        if callable(got):
            raise got()
        return Answer(got)
    return request


def expect_given_up(app: str, label: str, lines: list[str], marker: str) -> None:
    hit = [ln for ln in said(lines) if marker in ln]
    check(f"{app} G {label}: said once, as an error both readers take",
          len(hit) == 1 and len(as_error(hit[0])) == len(READERS),
          {ln: as_error(ln) for ln in (hit or said(lines))})


def gave_up() -> None:
    print("[G] what a connector gives up on")
    platform, sent = "/api/connector/platform", "/api/connector/messages/sent"
    no_turn = (lambda: host_error(404, "no reply of this connector has that turn_id"))
    picture = {"type": "image", "seq": 1, "mime": "image/png", "data_b64": PNG_B64}
    # Telegram
    c = load("telegram")
    c._request = by_path({platform: broker(400, {"ok": False, "error_code": 400,
                                                 "description": "Bad Request: chat not found"})})
    expect_given_up("telegram", "a message Telegram did not take",
                    run(lambda: c.send_text(c.Turn("42"), "Hello")), "did not take a message")
    lines = run(lambda: c.send_image(c.Turn("42"), dict(picture)))
    expect_given_up("telegram", "a picture Telegram took neither as a photo nor as a file", lines,
                    "did not take the picture as a file")
    check("telegram G ...while the fallback to a file, which is tried, reads as no error",
          [ln for ln in said(lines) if "as a photo" in ln and not as_error(ln)], said(lines),
          guard=True)
    c = load("telegram")
    c._request = by_path({platform: broker(200, {"ok": True, "result": {"message_id": 9}}),
                          sent: no_turn})
    expect_given_up("telegram", "the Host not recording what was sent",
                    run(lambda: c.deliver(c.Turn("42", turn_id="t1"),
                                          {"type": "text", "seq": 0, "text": "Hi"})),
                    "did not record what was sent")
    c = load("telegram")
    clocked(c)
    head = '{"ok":true,"result":[{"update_id":5,"message":{"text":"'
    c._request = scripted(c, [("answer", broker(200, None, head, truncated=True))] * 2)
    expect_given_up("telegram", "an update too large to read", run(c.poll_forever),
                    "update too large to read")
    # Slack
    c = load("slack")
    c._request = by_path({platform: broker(200, {"ok": False, "error": "channel_not_found"})})
    expect_given_up("slack", "a message Slack did not take",
                    run(lambda: c.send_text(c.Turn("D0GATE"), "Hello")), "did not take a message")
    expect_given_up("slack", "a picture Slack gave no upload URL for",
                    run(lambda: c.send_image(c.Turn("D0GATE"), dict(picture))), "gave no upload URL")
    c = load("slack")
    answers = [Answer(broker(200, {"ok": True, "file_id": "F1",
                                   "upload_url": "https://files.slack.com/upload/v1/gate"})),
               Answer(broker(200, {"ok": False, "error": "file_not_found"}))]
    c._request = lambda *a, **k: answers.pop(0)
    c.upload = lambda *a, **k: None
    expect_given_up("slack", "a picture Slack did not share",
                    run(lambda: c.send_image(c.Turn("D0GATE"), dict(picture))),
                    "did not share a picture")
    c = load("slack")
    clocked(c)              # a 503 is tried again before it is given up
    c._request = by_path({platform: broker(200, {"ok": True, "ts": "1.000001"}),
                          sent: lambda: host_error(503, "brain pool not ready")})
    expect_given_up("slack", "the Host not recording what was sent",
                    run(lambda: c.deliver(c.Turn("D0GATE", turn_id="t1"),
                                          {"type": "text", "seq": 0, "text": "Hi"})),
                    "did not record what was sent")
    # Discord
    c = load("discord")
    c._request = by_path({platform: broker(403, {"message": "Cannot send messages to this user",
                                                 "code": 50007})})
    expect_given_up("discord", "a message Discord did not take",
                    run(lambda: c.send_text(c.Turn("222222"), "Hello")), "did not take a message")
    c._request = by_path({platform: broker(413, {"message": "Request entity too large",
                                                 "code": 40005})})
    expect_given_up("discord", "a picture Discord did not take",
                    run(lambda: c.send_image(c.Turn("222222"), dict(picture))),
                    "did not take a picture")
    c._request = by_path({platform: broker(200, {"id": "9"}), sent: no_turn})
    expect_given_up("discord", "the Host not recording what was sent",
                    run(lambda: c.deliver(c.Turn("222222", turn_id="t1"),
                                          {"type": "text", "seq": 0, "text": "Hi"})),
                    "did not record what was sent")
    # A part of a reply too large to read, with no part header to stand for it.
    for app in APPS:
        c = load(app)
        c.LINE_MAX = 1024
        expect_given_up(app, "a part of a reply too large to read",
                        run(lambda: c.big_part(io.BytesIO(b"x" * (2 << 20) + b"\n"),
                                               c.Turn("42" if app == "telegram" else "D0GATE"),
                                               b"y" * 64)),
                        "too large to read")


# ── R: the report of what was sent, timing out ────────────────────────────

SENT_OK = {"telegram": broker(200, {"ok": True, "result": {"message_id": 9}}),
           "slack": broker(200, {"ok": True, "ts": "1.000001"}),
           "discord": broker(200, {"id": "9"})}
CHAT = {"telegram": "42", "slack": "D0GATE", "discord": "222222"}


def report_run(app: str, timeouts):
    """One text part read from a reply's stream and sent, its report to the
    Host timing out `timeouts` times (None: every time). (what the connector
    said and slept, the reports the Host was sent, the stream's outcome)."""
    c = load(app)
    clocked(c)
    reports: list = []

    def request(method, path, body=None, timeout=60.0):
        if path == "/api/connector/platform":
            return Answer(SENT_OK[app])
        if path != "/api/connector/messages/sent":
            raise Stop()
        reports.append(json.loads(json.dumps(body)))
        if timeouts is None or len(reports) <= timeouts:
            raise TimeoutError("timed out")
        return Answer({"turn_id": "t1", "recorded": 1})
    c._request = request
    stream = io.BytesIO(b'{"type": "text", "seq": 0, "text": "Hi"}\n{"type": "done"}\n')
    outcome: list = []
    lines = run(lambda: outcome.append(c.read_stream(stream, c.Turn(CHAT[app], turn_id="t1"))))
    return c, lines, reports, (outcome[0][0] if outcome else None)


def report_retried() -> None:
    print("[R] the report of what was sent, timing out")
    for app in APPS:
        c, lines, reports, outcome = report_run(app, 1)
        check(f"{app} R a report that timed out once is sent again, the same report",
              len(reports) == 2 and reports[0] == reports[1]
              and [s.get("seq") for s in reports[0].get("sent") or []] == [0],
              (reports, said(lines)))
        check(f"{app} R ...worded as a retry, the reply goes on to its end, no error line",
              outcome == "done" and not errors(said(lines))
              and any("trying again" in ln for ln in said(lines)) and sleeps(lines)[:1]
              and 0 < sleeps(lines)[0] <= 30, (outcome, said(lines), sleeps(lines)))
        c, lines, reports, outcome = report_run(app, None)
        tries = getattr(c, "REPORT_TRIES", None)
        check(f"{app} R a report that times out every time is tried REPORT_TRIES ({tries}) "
              "times, the wait growing, never past 30 s",
              isinstance(tries, int) and tries > 1 and len(reports) == tries
              and len(sleeps(lines)) == tries - 1
              and sleeps(lines) == sorted(sleeps(lines)) and max(sleeps(lines)) <= 30,
              (len(reports), sleeps(lines)))
        bad = errors(said(lines))
        check(f"{app} R ...then given up once, as an error both readers take, said last; "
              "the reply goes on to its end",
              outcome == "done" and len(bad) == 1 and "did not record what was sent" in bad[0]
              and said(lines)[-1] == bad[0], (outcome, said(lines)))


# ── N: what stays a retry ─────────────────────────────────────────────────

def stays_retry(app: str, label: str, c, fn, marker: str) -> None:
    lines = run(fn)
    hit = [ln for ln in said(lines) if marker in ln]
    h = (status(c).get("health") or {})
    check(f"{app} N {label}: worded as a retry, tried again within a minute, and no refusal "
          "on /health", hit and not errors(said(lines)) and sleeps(lines)[:1]
          and sleeps(lines)[0] <= 60 and h.get("ok") is True and "error" not in h,
          (said(lines), sleeps(lines), h), guard=True)


def guards() -> None:
    print("[N] what stays a retry (guards: these pass before the change too)")
    c = load("telegram")
    clocked(c)
    c._request = scripted(c, [tg(500, "Internal Server Error")])
    stays_retry("telegram", "getUpdates answered Telegram's 500", c, c.poll_forever,
                "no updates this time")
    c = load("slack")
    clocked(c)
    c._request = scripted(c, [sl_refused("internal_error")])
    stays_retry("slack", "auth.test answered internal_error", c, c.listen_forever,
                "no connection to Slack this time")
    c = load("slack")
    clocked(c)
    c._request = scripted(c, [("raise", host_error(409, "this connector's grant names no token "
                                                        "in Keys for that"))])
    stays_retry("slack", "auth.test refused by the Host itself (409), not by Slack", c,
                c.listen_forever, "no connection to Slack this time")
    c = load("discord")
    clocked(c)
    c.Socket, Gateway.MODULE = Gateway, c
    Gateway.PLAN[:] = [([GW_HELLO], 4000)]
    stays_retry("discord", "the gateway closed with 4000 (Discord: try reconnecting)", c,
                c.listen_forever, "the gateway closed (4000")


def main() -> int:
    telegram_runs()
    slack_runs()
    discord_runs()
    gave_up()
    report_retried()
    guards()
    leftover = [str(p) for app in APPS for p in (ROOT / "system-apps" / app).glob("__pycache__")]
    check("no bytecode left in system-apps/<id>/ (the packer signs the directory whole)",
          not leftover, leftover, guard=True)
    print(f"\n{counted['fix']} checks of the change and {counted['guard']} guards, "
          f"{len(fails)} failure(s)")
    if fails:
        print("FAILED: " + "; ".join(fails[:12]) + (" ..." if len(fails) > 12 else ""))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
