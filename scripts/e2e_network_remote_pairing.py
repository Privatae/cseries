#!/usr/bin/env python3
"""Pairing a machine with a Host on a server of its own, as the Network app
shows it in a real browser, from the real setup route's answer.

The Network app's "Set up a bastion" card renders the answer of
POST /api/network/bastions/setup as it comes (run.install, run.pair,
run.note) and an error's detail as it comes. The server therefore carries
everything a remote pairing needs in those fields, with no change to the
signed app. This drives the real Bastions lens in Chromium and serves it the
answer of the real route (server/routes/network.py setup_bastion, called
in-process), with stand-ins only for the database pool, the token mint, the
Host CA and the server's pairing helper (a unix socket here):

  1. Without a fresh passkey check the card shows the refusal, naming
     /passkey, and the pairing port is not opened.
  2. With one it shows the exact TCP pair command; a command per kind of
     machine that, copied off the card, writes the Host CA as host-ca.pem
     (the card runs a value's lines together, so a PEM shown as it is would
     be broken by the copy); and the window: the address the port opened to
     and for how long.

Every assertion reads visible text (inner_text), not markup.

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
FAKE_CA = "-----BEGIN CERTIFICATE-----\nMIIBinventedCAforthistestonly\n-----END CERTIFICATE-----\n"
TOKEN = "skb_inventedtoken"
HELPER_LINES = []


class Helper(socketserver.StreamRequestHandler):
    def handle(self):
        HELPER_LINES.append(self.rfile.readline())
        self.wfile.write(b'{"ok": true, "expires_in": 900}\n')


def stand_ins():
    from server import brain_service
    from server.middleware import owner_session
    from server.network import bastion_enroll, host_ca, local_bastion, nac_commit

    async def mint(pool, eid, name, *, window=None):
        # The real mint's wording and the real TCP step, with a fixed token:
        # only the pool is stood in.
        class Pool:
            async def execute(self, sql, *args):
                return "INSERT 0 1"
        out = await bastion_enroll.mint_pairing_token(Pool(), entity_id=9003, handle="home", name="bastion")
        out["run"]["pair"] = out["run"]["pair"].replace(out["token"], TOKEN)
        out["token"] = TOKEN
        await bastion_enroll.add_tcp_pairing(pool, out, window)
        return out

    async def nothing(pool):
        return None

    async def ca(pool):
        return FAKE_CA

    async def owner(scope):
        return "owner@example.com", "tok-owner"

    brain_service.get_pool = lambda: object()
    nac_commit.setup_bastion = mint
    host_ca.ensure, host_ca.ca_cert_pem = nothing, ca
    local_bastion.standalone = lambda: True
    owner_session.live_session = owner


def answer_setup() -> tuple[int, dict]:
    """The real route's answer to a request forwarded for PUBLIC."""
    from fastapi import HTTPException
    from starlette.requests import Request
    from server.routes import network as NR

    async def receive():
        return {"type": "http.request", "body": b"{}", "more_body": False}
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
            return self._json({"bastions": []})
        if p == "/api/network/bastion-latest":
            return self._json({"version": "0.50.0"})
        if p.startswith("/api/"):
            return self._json({})
        return super().do_GET()

    def do_POST(self):
        if self.path.split("?")[0] == "/api/network/bastions/setup":
            status, body = answer_setup()
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

    try:
        with sync_playwright() as pw:
            b = pw.chromium.launch(args=["--no-sandbox"])
            page = b.new_page()
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

            print("\n── 2. with a fresh passkey check ──")
            owner_passkeys._note_step_up("tok-owner", owner_passkeys.PASSKEY)
            page.click("#inspector-close")
            page.click("#gw-add")
            page.wait_for_timeout(800)
            t = page.locator("#inspector-body").inner_text()
            cmd = ("privatae-bastion pair --redeem-url https://home.box.example:8810/v1/local/redeem "
                   f"--host-ca host-ca.pem {TOKEN}")
            check("the card shows the exact TCP pair command", cmd in t, t[:600])
            # The text as a person would copy it off the card: one command each.
            posix = re.search(r"echo (\S+) \| base64 -d > host-ca\.pem", t)
            win = re.search(r"\[Convert\]::FromBase64String\('(\S+)'\)", t)
            check("the install step's CA command, copied off the card, writes the Host CA",
                  posix and win and base64.b64decode(posix.group(1)).decode() == FAKE_CA
                  and base64.b64decode(win.group(1)).decode() == FAKE_CA, t[:900])
            check("the card shows the window: the address and how long",
                  PUBLIC in t and "15 minutes" in t, t[-500:])
            check("the port was opened to that address",
                  [json.loads(x) for x in HELPER_LINES] == [{"op": "pairing.open", "addr": PUBLIC}], HELPER_LINES)
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
