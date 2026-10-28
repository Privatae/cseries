#!/usr/bin/env python3
"""Pairing a machine with a Host on a server of its own, as the Network app
shows it in a real browser, from the real setup route's answer.

The Network app's "Set up a bastion" card renders the answer of
POST /api/network/bastions/setup as it comes (run.one_line first, then
run.install, run.pair and run.note folded under "Do it step by step") and an
error's detail as it comes, so the server carries everything a remote
pairing needs in those fields. This drives the real Bastions lens in Chromium
and serves it the answers of the real routes (server/routes/network.py
get_bastions' pairing_window and setup_bastion, called in-process, through
the real nac_commit.setup_bastion and the real token mint). The stand-ins are
the database (bastion_pairing_tokens in memory: the three statements that
path runs, and any other is a failure), the Host CA and the server's pairing
helper (a unix socket here):

  1. Without a fresh passkey check the card shows the refusal, naming
     /passkey, and the pairing port is not opened.
  2. Before setup, "Pairing a machine on another network?" sits beside
     "Set up a bastion", and opened it stays there, its form beneath the
     toolbar: an address typed there sends one setup request, carrying
     pair_from and nothing else, and opens one window, to that address only;
     the choice then closes, its address cleared.
  3. "Set up a bastion" shows at its top the window's address with a
     countdown that runs, then the one line per kind of machine, exactly as
     the server built it (the macOS / Linux one gives the code to sh in its
     environment, never as an argument), wrapped at the box edge; and the
     earlier steps folded. Opened, they show each command on a line of its
     own, wrapped at the box edge too: the exact TCP pair command and a
     command per kind of machine that, copied off the card, writes the Host
     CA as host-ca.pem.
  4. The card's own "Pairing a machine on another network?" says the line
     above stops working and one more opening is used. An address typed
     there sends the card's token as `replaces`: the port opens to that
     address, the first window is shut, the card's token ends and the new
     card says the window is for the address given.
  5. Refused there (a private address; the helper out of openings), the
     card stays: the same line, its copy buttons, its countdown, the reason
     beside the field in the app's error colour, and the line still works.

Every assertion on the card reads visible text (inner_text), not markup.

With E2E_SHOT_DIR set, full-page screenshots go there: the Bastions lens
with the choice beside the button, that choice opened, the card after setup,
the card with the steps opened, and the card after a refused address.

Run:  python3 scripts/e2e_network_remote_pairing.py
"""
import asyncio
import base64
import http.server
import json
import os
import pathlib
import re
import socketserver
import sys
import tempfile
import threading

os.environ["CSERIES_URL"] = "https://home.box.example"
os.environ["CSERIES_LANE_HOST"] = "home.box.example"
os.environ["CSERIES_TRUSTED_PROXIES"] = "127.0.0.1"

ROOT = pathlib.Path(os.environ.get("APP_ROOT") or pathlib.Path(__file__).resolve().parent.parent)
APP = ROOT / "system-apps" / "network"
BASE = ROOT / "dashboard" / "base"
sys.path.insert(0, str(ROOT))

# A public address the test may use: the network's own documented edge.
PUBLIC = "40.160.88.174"
# Other public addresses, for a machine on another network than the browser's.
ELSEWHERE = "1.1.1.1"
BEFORE_SETUP = "9.9.9.9"
# A public address the stand-in helper refuses, as it does once the hour's
# openings are used up.
OUT_OF_OPENINGS = "8.8.4.4"
# A mistyped address: private, refused before the helper is asked.
PRIVATE = "10.20.30.40"


def _invented_ca() -> str:
    """A CA certificate made here, so the pairing code's fingerprint is taken
    over real DER."""
    import datetime
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Host CA invented")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now).not_valid_after(now + datetime.timedelta(days=1))
            .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
            .sign(key, hashes.SHA256()))
    return cert.public_bytes(serialization.Encoding.PEM).decode()


FAKE_CA = _invented_ca()

# Where the card's pairing window row and its first command sit, and whether
# the card is scrolled: the window belongs at the top, above the lines.
TOP_OF_CARD = """() => {
  const body = document.getElementById('inspector-body');
  const cd = document.getElementById('pair-countdown');
  const win = cd && cd.closest('.net-field');
  const code = body.querySelector('.net-field-val.mono');
  const r = (e) => { const b = e.getBoundingClientRect(); return {top: b.top, bottom: b.bottom}; };
  return {scroll: body.scrollTop, body: r(body), win: win && r(win), code: code && r(code)};
}"""

# Every place a shown command on the card wraps without a line break of its
# own: whether it wrapped at a space, whether both sides are one long word (a
# URL, a code, base64) and how far short of the box's right edge that line
# stops, in character widths. A wrap is right at a space, or inside a long word
# within two characters of the edge; anywhere else (after the hyphen of
# "host-" or "-Pair", or early inside a code) it is wrong.
SOFT_WRAPS = """() => {
  const out = [];
  for (const el of document.querySelectorAll('#inspector-body .net-field-val.mono')) {
    if (!el.checkVisibility()) continue;
    const cs = getComputedStyle(el), box = el.getBoundingClientRect();
    const right = box.right - parseFloat(cs.paddingRight) - parseFloat(cs.borderRightWidth);
    const chars = [];
    const walk = document.createTreeWalker(el, NodeFilter.SHOW_TEXT);
    for (let n; (n = walk.nextNode());) {
      for (let i = 0; i < n.data.length; i++) {
        const c = n.data[i];
        if (c === '\\n') { chars.push({nl: true}); continue; }
        if (/\\s/.test(c)) { chars.push({space: true}); continue; }
        const range = document.createRange(); range.setStart(n, i); range.setEnd(n, i + 1);
        const b = range.getBoundingClientRect();
        const long = !!(n.parentElement && n.parentElement.closest('.net-code-long'));
        if (b.width) chars.push({c, long, top: b.top, right: b.right, w: b.width, h: b.height});
      }
    }
    const widths = chars.filter(x => x.w).map(x => x.w).sort((a, b) => a - b);
    const cw = widths[Math.floor(widths.length / 2)] || 1;
    let prev = null, broke = false, spaced = false;
    for (const x of chars) {
      if (x.nl) { broke = true; continue; }
      if (x.space) { spaced = true; continue; }
      if (prev && !broke && x.top > prev.top + prev.h / 2) {
        const gap = Math.round((right - prev.right) / cw * 10) / 10;
        out.push({at: el.textContent.slice(0, 30), last: prev.c, next: x.c, spaced, gap,
                  ok: spaced || (prev.long && x.long && gap < 2)});
      }
      prev = x; broke = false; spaced = false;
    }
  }
  return out;
}"""

# The refusal's colour, the app's error colour (--cseries-clay, as
# .net-inline-status.error uses it) and the card's ordinary text colour.
ERROR_COLOURS = """() => {
  const said = document.getElementById('pair-from-error');
  if (!said) return null;
  const probe = document.createElement('span');
  probe.style.color = 'var(--cseries-clay)';
  said.parentNode.appendChild(probe);
  const out = {said: getComputedStyle(said).color, error: getComputedStyle(probe).color,
               text: getComputedStyle(said.parentNode).color};
  probe.remove();
  return out;
}"""

HELPER_LINES = []
SETUP_BODIES = []
ANSWERS = []


class Helper(socketserver.StreamRequestHandler):
    """cseries-door-helper's protocol (server/network/door_helper.py)."""
    def handle(self):
        line = self.rfile.readline()
        HELPER_LINES.append(line)
        ask = json.loads(line)
        if ask.get("op") == "pairing.open" and ask.get("addr") == OUT_OF_OPENINGS:
            self.wfile.write(b'{"ok": false, "error": "the pairing port was opened 6 times in the last hour"}\n')
        elif ask.get("op") == "pairing.open":
            self.wfile.write(b'{"ok": true, "expires_in": 900}\n')
        else:
            self.wfile.write(b'{"ok": true}\n')


def helper_asks(since=0):
    return [(x["op"], x["addr"]) for x in (json.loads(l) for l in HELPER_LINES[since:])]


class Tokens:
    """bastion_pairing_tokens, in memory: live or not, by secret hash. It
    takes only the statements the setup path makes (bastion_enroll's mint,
    pairing_token_live and revoke_pairing_token); any other fails the run, so
    a change there is seen here instead of answered wrongly."""

    def __init__(self):
        self.live = {}

    def execute(self, sql, args):
        s = " ".join(sql.split())
        if s.startswith("INSERT INTO bastion_pairing_tokens (secret_hash, name, expires_at)"):
            self.live[args[0]] = True
            return "INSERT 0 1"
        if s == ("UPDATE bastion_pairing_tokens SET expires_at = now() WHERE secret_hash = $1 "
                 "AND used_at IS NULL AND expires_at > now()"):
            was = self.live.get(args[0]) is True
            if was:
                self.live[args[0]] = False
            return f"UPDATE {int(was)}"
        raise AssertionError("the setup path ran a statement this stand-in does not know: " + s[:120])

    def fetchrow(self, sql, args):
        s = " ".join(sql.split())
        if s == ("SELECT 1 AS live FROM bastion_pairing_tokens WHERE secret_hash = $1 "
                 "AND used_at IS NULL AND expires_at > now()"):
            return {"live": 1} if self.live.get(args[0]) is True else None
        raise AssertionError("the setup path ran a query this stand-in does not know: " + s[:120])

    def is_live(self, token) -> bool:
        from server.network import bastion_enroll
        return self.live.get(bastion_enroll.pairing_token_secret_hash(token)) is True


TOKENS = Tokens()


class Conn:
    async def execute(self, sql, *args):
        return TOKENS.execute(sql, args)

    async def fetchrow(self, sql, *args):
        return TOKENS.fetchrow(sql, args)

    def transaction(self):
        return Transaction()


class Transaction:
    """Rolls the tokens back when its block raises, as asyncpg's does."""
    async def __aenter__(self):
        self.saved = dict(TOKENS.live)

    async def __aexit__(self, kind, exc, tb):
        if kind is not None:
            TOKENS.live = self.saved
        return False


class Pool(Conn):
    def acquire(self):
        class Acquired:
            async def __aenter__(self):
                return Conn()

            async def __aexit__(self, *exc):
                return False
        return Acquired()


def stand_ins():
    from server import brain_service
    from server.middleware import owner_session
    from server.network import host_ca, local_bastion

    async def nothing(pool):
        return None

    async def ca(pool):
        return FAKE_CA

    async def owner(scope):
        return "owner@example.com", "tok-owner"

    pool = Pool()
    brain_service.get_pool = lambda: pool
    host_ca.ensure, host_ca.ca_cert_pem = nothing, ca
    local_bastion.standalone = lambda: True
    owner_session.live_session = owner


def answer_setup(body: bytes = b"{}") -> tuple[int, dict]:
    """The real route's answer to a request forwarded for PUBLIC, with the
    body the card sent."""
    from fastapi import HTTPException
    from starlette.requests import Request
    from server.routes import network as NR

    async def receive():
        return {"type": "http.request", "body": body or b"{}", "more_body": False}
    req = Request({"type": "http", "method": "POST", "path": "/api/network/bastions/setup",
                   "headers": [(b"content-type", b"application/json"), (b"x-forwarded-for", PUBLIC.encode())],
                   "client": ("127.0.0.1", 40000), "server": ("127.0.0.1", 8800), "scheme": "http",
                   "query_string": b"", "root_path": ""}, receive)
    try:
        return 200, asyncio.run(NR.setup_bastion(req))
    except HTTPException as e:
        return e.status_code, {"detail": e.detail}


class H(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def translate_path(self, path):
        p = path.split("?")[0].lstrip("/")
        if p in ("", "index.html"):
            return str(APP / "index.html")
        if p.startswith("system-apps/"):
            return str(ROOT / p)
        return str(BASE / p)

    def _json(self, obj, status=200):
        b = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        p = self.path.split("?")[0]
        if p == "/api/network/bastions":
            # pairing_window as the real list route answers it (get_bastions).
            from server.routes import network as NR
            return self._json({"bastions": [], "pairing_window": NR._opens_pairing_window()})
        if p == "/api/network/bastion-latest":
            return self._json({"version": "0.50.0"})
        if p.startswith("/api/"):
            return self._json({})
        return super().do_GET()

    def do_POST(self):
        if self.path.split("?")[0] == "/api/network/bastions/setup":
            sent = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            SETUP_BODIES.append(json.loads(sent or b"{}"))
            status, body = answer_setup(sent)
            ANSWERS.append(body)
            return self._json(body, status)
        return self._json({"ok": True})


def main() -> int:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("SKIP — playwright not installed; a skip proves nothing here")
        return 2
    from server import owner_passkeys

    d = tempfile.mkdtemp(prefix="pairui")
    sock = os.path.join(d, "helper.sock")
    helper = socketserver.ThreadingUnixStreamServer(sock, Helper)
    threading.Thread(target=helper.serve_forever, daemon=True).start()
    os.environ["CSERIES_DOOR_HELPER"] = sock
    stand_ins()
    socketserver.TCPServer.allow_reuse_address = True
    srv = socketserver.ThreadingTCPServer(("127.0.0.1", 0), H)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    fails = []

    def check(label, cond, extra=""):
        print(("  PASS  " if cond else "  FAIL  ") + label + (("\n        " + str(extra)) if extra else ""))
        if not cond:
            fails.append(label)

    shots = os.environ.get("E2E_SHOT_DIR", "").strip()
    if shots:
        os.makedirs(shots, exist_ok=True)

    def shot(page, name):
        if shots:
            path = os.path.join(shots, name)
            page.screenshot(path=path, full_page=True)
            print("        screenshot: " + path)

    try:
        with sync_playwright() as pw:
            b = pw.chromium.launch(args=["--no-sandbox"])
            page = b.new_context(permissions=["clipboard-read", "clipboard-write"]).new_page()
            page.goto(f"http://127.0.0.1:{port}/index.html", wait_until="networkidle")
            page.click('[data-lens="bastions"]')
            page.wait_for_timeout(600)

            print("\n── 1. no fresh passkey check ──")
            page.click("#gw-add")
            page.wait_for_timeout(800)
            t = page.locator("#inspector-body").inner_text()
            check("the card shows the refusal, naming where to confirm",
                  "passkey" in t and "https://home.box.example/passkey" in t, t[:300])
            check("the pairing port was not opened", HELPER_LINES == [], HELPER_LINES)
            page.click("#inspector-close")

            print("\n── 2. before setup, a machine on another network ──")
            owner_passkeys._note_step_up("tok-owner", owner_passkeys.PASSKEY)
            bar = page.locator("#lens-bastions .view-toolbar")
            choice = page.locator("#gw-add-elsewhere summary")
            bt = bar.inner_text().lower()  # the button is styled upper-case
            box_go, box_choice = page.locator("#gw-add").bounding_box(), choice.bounding_box()
            check("\"Pairing a machine on another network?\" is shown beside \"Set up a bastion\"",
                  choice.is_visible() and page.locator("#gw-add").is_visible()
                  and "set up a bastion" in bt and "pairing a machine on another network?" in bt
                  # on the button's row, to its right
                  and box_go["y"] <= box_choice["y"] + box_choice["height"] / 2 <= box_go["y"] + box_go["height"]
                  and box_choice["x"] >= box_go["x"] + box_go["width"], (bt, box_go, box_choice))
            shot(page, "a-bastions-lens-choice-beside-setup.png")
            n_setup, n_helper = len(SETUP_BODIES), len(HELPER_LINES)
            choice.click()
            page.wait_for_timeout(200)
            box_open = choice.bounding_box()
            form = page.locator("#gw-add-elsewhere-form")
            box_form = form.bounding_box() if form.count() == 1 and form.is_visible() else None
            check("opened, the choice stays where it was and its form opens beneath the toolbar",
                  abs(box_open["x"] - box_choice["x"]) < 1 and abs(box_open["y"] - box_choice["y"]) < 1
                  and box_form is not None and box_form["y"] >= box_go["y"] + box_go["height"]
                  and page.locator("#gw-add-elsewhere-addr").is_visible(),
                  (box_choice, box_open, box_form))
            shot(page, "a2-choice-opened-form-beneath.png")
            page.fill("#gw-add-elsewhere-addr", BEFORE_SETUP)
            page.click("#gw-add-elsewhere-go")
            page.wait_for_timeout(1000)
            t = page.locator("#inspector-body").inner_text()
            check("after its setup, the choice is closed and its address cleared",
                  page.locator("#gw-add-elsewhere").evaluate("d => !d.open")
                  and page.locator("#gw-add-elsewhere-addr").input_value() == ""
                  and not page.locator("#gw-add-elsewhere-addr").is_visible(),
                  page.locator("#gw-add-elsewhere-addr").input_value())
            check("one setup request, carrying pair_from and nothing else",
                  SETUP_BODIES[n_setup:] == [{"pair_from": BEFORE_SETUP}], SETUP_BODIES[n_setup:])
            check("one window opened, to that address only",
                  helper_asks(n_helper) == [("pairing.open", BEFORE_SETUP)], helper_asks(n_helper))
            check("the card shows its line and says the window is for the address given",
                  ANSWERS[-1]["run"]["one_line"]["unix"] in t and BEFORE_SETUP in t
                  and "the address you gave" in t and PUBLIC not in t, t[:700])
            page.click("#inspector-close")

            print("\n── 3. Set up a bastion ──")
            n_helper = len(HELPER_LINES)
            page.click("#gw-add")
            page.wait_for_timeout(800)
            t = page.locator("#inspector-body").inner_text()
            ans = ANSWERS[-1]
            one = ans["run"]["one_line"]
            check("the card shows the one line per kind of machine, exactly as built, before the steps",
                  one["windows"] in t and one["unix"] in t
                  and t.index(one["windows"]) < t.index("Do it step by step"), t[:700])
            check("the one line carries the pairing code the server made",
                  ans["pairing_code"].startswith("skp_") and ans["pairing_code"] in one["windows"]
                  and ans["pairing_code"] in one["unix"], ans.get("pairing_code"))
            check("the card shows the window: the address, this browser's network",
                  PUBLIC in t and "this browser is on" in t, t[:900])
            check("the macOS / Linux line gives the code to sh in its environment, never as an argument",
                  "curl -fsSL https://privatae.ai/bastion/install.sh | "
                  f"PRIVATAE_PAIR_CODE='{ans['pairing_code']}' sh" in t
                  and "--pair" not in t.split("Do it step by step")[0], one["unix"])
            geo = page.evaluate(TOP_OF_CARD)
            check("the window's address and countdown are at the top of the card, above the first line",
                  geo["win"] is not None and geo["code"] is not None and geo["scroll"] == 0
                  and geo["win"]["bottom"] <= geo["code"]["top"] and geo["win"]["bottom"] <= geo["body"]["bottom"]
                  and t.index(PUBLIC) < t.index(one["windows"]), geo)
            wraps = page.evaluate(SOFT_WRAPS)
            check("the lines wrap at a space or, inside a long word, at the box edge; never after a hyphen",
                  len(wraps) > 3 and all(w["ok"] for w in wraps)
                  and sum(1 for w in wraps if not w["spaced"]) > 3,
                  [w for w in wraps if not w["ok"]][:6] or wraps[:3])
            first = page.locator("#pair-countdown").inner_text()
            page.wait_for_timeout(2200)
            later = page.locator("#pair-countdown").inner_text()
            check("the window's countdown runs", re.fullmatch(r"for 1[45]:\d\d more", first) is not None
                  and later != first, (first, later))
            cmd = ("privatae-bastion pair --redeem-url https://home.box.example:8810/v1/local/redeem "
                   f"--host-ca host-ca.pem {ans['token']}")
            check("the earlier steps are folded away", cmd not in t, t[:900])
            shot(page, "b-card-after-setup.png")
            page.click("text=Do it step by step")
            page.wait_for_timeout(200)
            t = page.locator("#inspector-body").inner_text()
            # The card scrolls inside the inspector, so the opened steps are
            # brought to its top for the screenshot.
            page.locator("details.net-pair-steps summary").evaluate("e => e.scrollIntoView({block: 'start'})")
            shot(page, "c-card-steps-opened.png")
            check("opened, the steps show the exact TCP pair command", cmd in t, t[:900])
            # Read as a person reads the card: each command a line of its own,
            # never run into the one before.
            lines = t.split("\n")
            own = [l for l in lines if l in (
                       "Windows (PowerShell):  irm https://privatae.ai/bastion/install.ps1 | iex",
                       "macOS / Linux:  curl -fsSL https://privatae.ai/bastion/install.sh | sh", cmd)
                   or re.fullmatch(r"macOS / Linux:  echo \S+ \| base64 -d > host-ca\.pem", l)
                   or re.fullmatch(r"Windows \(PowerShell\):  \[IO\.File\]::WriteAllBytes\(\(Join-Path \$PWD "
                                   r"'host-ca\.pem'\), \[Convert\]::FromBase64String\('\S+'\)\)", l)]
            check("opened, each command in the steps is on a line of its own", len(own) == 5,
                  [l[:90] for l in lines if "install" in l or "host-ca" in l or "pair" in l][:8])
            wraps = page.evaluate(SOFT_WRAPS)
            check("opened, the steps wrap the same way, so host-ca.pem is never split",
                  len(wraps) > 10 and all(w["ok"] for w in wraps)
                  and not re.search(r"host-\s*\n", page.locator("details.net-pair-steps").inner_text()),
                  [w for w in wraps if not w["ok"]][:6] or wraps[:3])
            # The text as a person would copy it off the card: one command each.
            posix = re.search(r"echo (\S+) \| base64 -d > host-ca\.pem", t)
            win = re.search(r"\[Convert\]::FromBase64String\('(\S+)'\)", t)
            check("the install step's CA command, copied off the card, writes the Host CA",
                  posix and win and base64.b64decode(posix.group(1)).decode() == FAKE_CA
                  and base64.b64decode(win.group(1)).decode() == FAKE_CA, t[:900])
            check("the steps still name the window and how long",
                  PUBLIC in t and "15 minutes" in t, t[-500:])
            check("the port was opened to that address",
                  helper_asks(n_helper) == [("pairing.open", PUBLIC)], helper_asks(n_helper))
            acts = page.locator("#inspector-actions").inner_text().lower()  # the buttons are styled upper-case
            check("the card's buttons copy the two lines", "copy windows line" in acts
                  and "copy macos / linux line" in acts, acts)
            copied = []
            for label in ("Copy Windows line", "Copy macOS / Linux line"):
                page.locator("#inspector-actions button", has_text=label).click()
                page.wait_for_timeout(150)
                copied.append(page.evaluate("navigator.clipboard.readText()"))
            check("each button copies its line whole, however the card wraps it",
                  copied == [one["windows"], one["unix"]], copied)

            print("\n── 4. from the card, a machine on another network ──")
            card_token = ans["token"]
            page.locator("#inspector-body summary", has_text="Pairing a machine on another network?").click()
            page.wait_for_timeout(200)
            other = page.locator("#inspector-body details.net-pair-other").inner_text()
            check("the card says the line above stops working and one more opening is used",
                  "The line above stops working" in other
                  and "uses one more of the hour's pairing openings" in other, other)
            n_setup, n_helper = len(SETUP_BODIES), len(HELPER_LINES)
            page.fill("#pair-from-addr", ELSEWHERE)
            page.click("#pair-from-go")
            page.wait_for_timeout(1000)
            t = page.locator("#inspector-body").inner_text()
            ans = ANSWERS[-1]
            check("one setup request, carrying the address and the card's token as replaces",
                  SETUP_BODIES[n_setup:] == [{"pair_from": ELSEWHERE, "replaces": card_token}],
                  SETUP_BODIES[n_setup:])
            check("the port opened to that address and the first window was shut",
                  helper_asks(n_helper) == [("pairing.open", ELSEWHERE), ("pairing.close", PUBLIC)],
                  helper_asks(n_helper))
            check("the card's token ended and the new one is live",
                  not TOKENS.is_live(card_token) and ans.get("token") != card_token
                  and TOKENS.is_live(ans.get("token")), ans.get("token"))
            check("the new card shows the new line and says the window is for the address given",
                  ans["run"]["one_line"]["unix"] in t and one["unix"] not in t
                  and ELSEWHERE in t and "the address you gave" in t
                  and PUBLIC not in t.split("Do it step by step")[0], t[:700])

            print("\n── 5. from the card, refused ──")
            card_token, one = ans["token"], ans["run"]["one_line"]
            for addr, reason in ((PRIVATE, f"{PRIVATE} is not a public address"),
                                 (OUT_OF_OPENINGS, "The pairing port did not open: the pairing port "
                                                   "was opened 6 times in the last hour")):
                # A refusal keeps the card, so after the first one it is open already.
                if not page.locator("details.net-pair-other").evaluate("d => d.open"):
                    page.locator("#inspector-body summary", has_text="Pairing a machine on another network?").click()
                    page.wait_for_timeout(200)
                n_setup, n_helper, n_answers = len(SETUP_BODIES), len(HELPER_LINES), len(ANSWERS)
                page.fill("#pair-from-addr", addr)
                page.click("#pair-from-go")
                page.wait_for_timeout(1000)
                t = page.locator("#inspector-body").inner_text()
                beside = page.locator("details.net-pair-other #pair-from-error")
                said = beside.inner_text() if beside.count() else ""
                check(f"{addr}: one setup request with the card's token, refused",
                      SETUP_BODIES[n_setup:] == [{"pair_from": addr, "replaces": card_token}]
                      and len(ANSWERS) == n_answers + 1 and "token" not in ANSWERS[-1],
                      (SETUP_BODIES[n_setup:], ANSWERS[n_answers:]))
                check(f"{addr}: no window opened ("
                      + ("a private address never reaches the helper)" if addr == PRIVATE
                         else "the helper was asked once and refused)"),
                      helper_asks(n_helper) == ([] if addr == PRIVATE else [("pairing.open", addr)]),
                      helper_asks(n_helper))
                check(f"{addr}: the reason is shown beside the field",
                      reason in said and beside.is_visible() and page.locator("#pair-from-addr").is_visible(),
                      said)
                colours = page.evaluate(ERROR_COLOURS)
                check(f"{addr}: the reason looks like an error, in the app's error colour",
                      colours is not None and colours["said"] == colours["error"] != colours["text"], colours)
                acts = page.locator("#inspector-actions").inner_text().lower()
                check(f"{addr}: the card stays, with its line and copy buttons",
                      one["windows"] in t and one["unix"] in t
                      and "copy windows line" in acts and "copy macos / linux line" in acts, t[:700])
                first = page.locator("#pair-countdown").inner_text()
                page.wait_for_timeout(1200)
                later = page.locator("#pair-countdown").inner_text()
                check(f"{addr}: its countdown still runs",
                      re.fullmatch(r"for 1[45]:\d\d more", first) is not None and later != first,
                      (first, later))
                check(f"{addr}: the line still works, and the button can be used again",
                      TOKENS.is_live(card_token) and page.locator("#pair-from-go").is_enabled())
                if addr == PRIVATE:
                    shot(page, "d-card-after-refused-pair-from.png")
            b.close()
    finally:
        srv.shutdown()
        helper.shutdown()
        helper.server_close()
        try:
            os.unlink(sock)
            os.rmdir(d)
        except OSError:
            pass

    print()
    if fails:
        print(f"FAILED — {len(fails)} check(s):")
        for f in fails:
            print("  - " + f)
        return 1
    print("PASSED — the Network app shows a remote pairing from the real setup answer")
    return 0


if __name__ == "__main__":
    sys.exit(main())
