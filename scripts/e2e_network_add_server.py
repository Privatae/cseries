#!/usr/bin/env python3
""""Add a server" in the Network app, in a real browser, through the real route.

The Network app's Bastions lens offers "Add a server" on a Host with no platform
(GET /api/network/bastions add_server). Its card takes the server's address and
the code `privatae-bastion pair --open` showed there, and POSTs them to
/api/network/servers/pair, answered here by the real route (server/routes/
network.py pair_server) running the real pairing (server/network/host_dials.py)
against a stand-in bastion over real TLS on loopback
(scripts/host_dials_standin.py, the wire contract of the bastion's
docs/host-connects.md). The real bastion binary is the end-to-end run
(docs/verification/serve-lane-and-host-dials-2026-11-03.md).

  1. "Add a server" sits beside "Set up a bastion" on a Host with no platform,
     and is not shown on a Host with a platform.
  2. With an empty field the card says what to give and sends nothing.
  3. A code the server does not take: the card stays, the reason beside the
     fields in the person's words, nothing kept.
  4. The right code: the card shows what the server said ("Your Host is
     connected to far-server ..."), and the Host filed the pairing.

Every assertion reads visible text (inner_text).

Run:  python3 scripts/e2e_network_add_server.py
"""
import asyncio
import http.server
import json
import os
import pathlib
import socketserver
import sys
import threading

ROOT = pathlib.Path(os.environ.get("APP_ROOT") or pathlib.Path(__file__).resolve().parent.parent)
APP = ROOT / "system-apps" / "network"
BASE = ROOT / "dashboard" / "base"
sys.path[:0] = [str(ROOT), str(ROOT / "brain"), str(ROOT / "scripts")]

from host_dials_standin import CODE, Pool, StandinBastion  # noqa: E402
from server.network import host_dials, local_bastion  # noqa: E402

LOOP = asyncio.new_event_loop()
threading.Thread(target=LOOP.run_forever, daemon=True).start()
POOL = Pool()
STANDALONE = {"on": True}
BASTIONS: list = []
POSTS: list = []


def run(coro, timeout=60):
    return asyncio.run_coroutine_threadsafe(coro, LOOP).result(timeout)


async def _listen(b):
    srv = await asyncio.start_server(b.serve, "127.0.0.1", 0)
    return srv, srv.sockets[0].getsockname()[1]


_REAL_CONNECT = host_dials._connect


def with_bastion(b):
    """The stand-in bastion listens; the Host's dial of any address reaches it."""
    srv, port = run(_listen(b))

    async def _c(host, p):
        return await _REAL_CONNECT("127.0.0.1", port)
    host_dials._connect = _c
    return srv


def answer_pair(body: bytes):
    from fastapi import HTTPException
    from starlette.requests import Request
    from server import brain_service
    from server.routes import network as NR
    brain_service.get_pool = lambda: POOL
    brain_service._entity_id = 1

    async def go():
        async def receive():
            return {"type": "http.request", "body": body or b"{}", "more_body": False}
        req = Request({"type": "http", "method": "POST", "path": "/api/network/servers/pair",
                       "headers": [(b"content-type", b"application/json")],
                       "client": ("127.0.0.1", 40000), "server": ("127.0.0.1", 8800), "scheme": "http",
                       "query_string": b"", "root_path": ""}, receive)
        try:
            return 200, await NR.pair_server(req)
        except HTTPException as e:
            return e.status_code, {"detail": e.detail}
    return run(go())


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
            return self._json({"bastions": BASTIONS, "pairing_window": False, "add_server": STANDALONE["on"]})
        if p == "/api/network/bastion-latest":
            return self._json({"version": "0.63.0"})
        if p.startswith("/api/"):
            return self._json({})
        return super().do_GET()

    def do_POST(self):
        if self.path.split("?")[0] == "/api/network/servers/pair":
            sent = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            POSTS.append(json.loads(sent or b"{}"))
            status, body = answer_pair(sent)
            return self._json(body, status)
        return self._json({"ok": True})


def main() -> int:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("SKIP — playwright not installed; a skip proves nothing here")
        return 2
    local_bastion.standalone = lambda: STANDALONE["on"]
    host_dials.wake = lambda: None
    socketserver.TCPServer.allow_reuse_address = True
    srv = socketserver.ThreadingTCPServer(("127.0.0.1", 0), H)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    fails = []

    def check(label, cond, extra=""):
        print(("  PASS  " if cond else "  FAIL  ") + label + (("\n        " + str(extra)) if extra and not cond else ""))
        if not cond:
            fails.append(label)

    shots = os.environ.get("E2E_SHOT_DIR", "").strip()
    if shots:
        os.makedirs(shots, exist_ok=True)

    def shot(page, name):
        if shots:
            page.screenshot(path=os.path.join(shots, name), full_page=True)

    with sync_playwright() as pw:
        b = pw.chromium.launch(args=["--no-sandbox"])
        page = b.new_context().new_page()

        print("\n── 1. offered on a Host with no platform only ──")
        STANDALONE["on"] = False
        page.goto(f"http://127.0.0.1:{port}/index.html", wait_until="networkidle")
        page.click('[data-lens="bastions"]')
        page.wait_for_timeout(600)
        check("a Host with a platform: no \"Add a server\"", not page.locator("#gw-add-server").is_visible())
        STANDALONE["on"] = True
        page.goto(f"http://127.0.0.1:{port}/index.html", wait_until="networkidle")
        page.click('[data-lens="bastions"]')
        page.wait_for_timeout(600)
        add, setup = page.locator("#gw-add-server"), page.locator("#gw-add")
        bx, sx = add.bounding_box(), setup.bounding_box()
        check("\"Add a server\" sits beside \"Set up a bastion\"",
              add.is_visible() and "add a server" in add.inner_text().lower() and bx and sx
              and abs(bx["y"] - sx["y"]) < sx["height"] and bx["x"] > sx["x"], (bx, sx))
        shot(page, "a-bastions-add-server.png")

        print("\n── 2. the card ──")
        add.click()
        page.wait_for_timeout(300)
        t = page.locator("#inspector").inner_text()
        check("the card says to run pair --open on the server and asks for the address and the code",
              "Add a server" in t and "privatae-bastion pair --open" in t
              and page.locator("#srv-addr").is_visible() and page.locator("#srv-code").is_visible(), t[:400])
        page.click("#inspector-actions button")
        page.wait_for_timeout(300)
        check("empty: it says what to give, and sends nothing",
              "address and the code" in page.locator("#srv-status").inner_text() and POSTS == [],
              page.locator("#srv-status").inner_text())

        print("\n── 3. a code the server does not take ──")
        wrong = StandinBastion(code="0" * 20)
        s1 = with_bastion(wrong)
        page.fill("#srv-addr", "203.0.113.9")
        page.fill("#srv-code", CODE)
        page.click("#inspector-actions button")
        page.wait_for_function("document.getElementById('srv-status') && "
                               "!document.getElementById('srv-status').textContent.startsWith('Connecting')",
                               timeout=20000)
        st = page.locator("#srv-status").inner_text()
        check("the card stays, with the reason in plain words beside the fields",
              "didn't take that code" in st and page.locator("#srv-addr").is_visible()
              and page.locator("#srv-addr").input_value() == "203.0.113.9", st)
        check("...and nothing was kept", POOL.rows == [], POOL.rows)
        check("the card sent the address and the code as typed",
              POSTS[-1:] == [{"address": "203.0.113.9", "code": CODE}], POSTS)
        shot(page, "b-add-server-wrong-code.png")
        run(_close(s1))

        print("\n── 4. the right code ──")
        right = StandinBastion()
        s2 = with_bastion(right)
        page.click("#inspector-actions button")
        page.wait_for_function("document.getElementById('inspector-title').textContent === 'Server added'",
                               timeout=20000)
        body = page.locator("#inspector-body").inner_text()
        check("the card shows what the server said",
              "Your Host is connected to far-server" in body and "by itself" in body, body)
        check("...and the Host filed the pairing (host_dials, the server's address)",
              len(POOL.rows) == 1 and POOL.rows[0]["notes"].get("transport") == "host_dials"
              and POOL.rows[0]["notes"].get("door") == "203.0.113.9:8812", POOL.rows)
        shot(page, "c-add-server-connected.png")
        run(_close(s2))
        b.close()
    srv.shutdown()
    print(f"\n{'FAIL: ' + str(len(fails)) if fails else 'all passed'}")
    return 1 if fails else 0


async def _close(s):
    s.close()


if __name__ == "__main__":
    sys.exit(main())
