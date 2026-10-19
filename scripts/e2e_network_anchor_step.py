#!/usr/bin/env python3
"""The anchor step, asserted in a REAL browser against the REAL server payload.

2026-09-12: the page never said where to get a pair token, which is not
coherent for a user who already paired the bastion and then changes the mode
to networked: where does the admin token come from?

The fix is instructions, so the only thing that proves it is a page a person
could read. scripts/test_anchor_step_is_visible.py already drives the builders
headlessly — but a field list is not a rendered card, and markup in a string is
not markup a browser parsed. Both readings of the Update button on 2026-09-12
came from the source and neither survived Chromium; this is the same lesson
applied before the claim, not after.

Two things that make this a real exercise rather than a restatement:

  * The setup card is served the OUTPUT OF THE REAL mint_pairing_token, imported
    in-process and called here. If that function stops emitting the share step,
    this page stops showing it. Nothing about the wording is planted by the test.
  * Every assertion reads visible TEXT off the page (inner_text), not innerHTML
    — so a row that renders as escaped &lt;code&gt; markup, or is written into a
    hidden element, fails instead of passing on a substring match.

Run:  python3 scripts/e2e_network_anchor_step.py
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
sys.path.insert(0, str(ROOT))

NOW = "2026-09-12T20:00:00Z"


def _machine(mid, host, **kw):
    m = {"id": mid, "hostname": host, "name": host, "bastion_version": "0.23.0",
         "online": True, "platform": "linux", "last_seen_at": NOW,
         "update_to_version": None, "capabilities": [], "models": [],
         "mode": None, "peers": [], "pairings": []}
    m.update(kw)
    return m


# One machine per state the instructions branch on.
BASTIONS = [
    _machine(1, "local-box", mode="local"),
    _machine(2, "shared-box", mode="networked"),
    _machine(3, "anchored-box", mode="networked",
             anchor={"owner": "Alex", "anchor_host": "host-a.zone.example.test",
                     "anchor_handle": "host-a", "verified": True}),
    _machine(4, "old-box"),                                   # reports no mode
    _machine(5, "win-box", mode="local", platform="windows-amd64"),
    # 'darwin'.indexOf('win') === 3 — a substring test for 'win' called every Mac
    # a Windows box and told it to open PowerShell. Caught in review 2026-09-12.
    _machine(6, "mac-box", mode="local", platform="darwin"),
    _machine(7, "mac-arm", mode="local", platform="darwin-arm64"),
    # The rows are gated on 0.23.0, where claim-anchor's token became optional.
    # A 0.22.0 box exits 2 on a bare claim-anchor, so it must not be offered one.
    _machine(8, "old-ver", mode="local", bastion_version="0.22.0"),
]


class H(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def translate_path(self, path):
        # Two roots: index.html pulls /css and /js from the dashboard base and
        # only /system-apps/ from the app. Serving one root 404s the shared
        # assets and the lens never initialises.
        p = path.split("?")[0].lstrip("/")
        if p in ("", "index.html"):
            return str(APP / "index.html")
        if p.startswith("system-apps/"):
            return str(ROOT / p)
        return str(BASE / p)

    def _json(self, obj):
        b = json.dumps(obj).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        p = self.path.split("?")[0]
        if p == "/api/network/bastions":
            return self._json({"bastions": BASTIONS})
        if p == "/api/network/bastion-latest":
            return self._json({"version": "0.23.0"})
        if p.startswith("/api/"):
            return self._json({})
        return super().do_GET()

    def do_POST(self):
        p = self.path.split("?")[0]
        if p == "/api/network/bastions/setup":
            # THE REAL FUNCTION. Not a fixture of what it ought to return.
            return self._json(REAL_SETUP_PAYLOAD)
        return self._json({"ok": True})


class StubPool:
    """mint_pairing_token runs the DDL and one INSERT; nothing else is touched."""

    async def execute(self, sql, *args):
        return "INSERT 0 1"


def real_setup_payload() -> dict:
    from server.network import bastion_enroll
    return asyncio.run(bastion_enroll.mint_pairing_token(
        StubPool(), entity_id=9003, handle="acme", name="bastion"))


REAL_SETUP_PAYLOAD = real_setup_payload()


def main() -> int:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("SKIP — playwright not installed; a skip proves nothing here")
        return 2

    socketserver.TCPServer.allow_reuse_address = True
    srv = socketserver.TCPServer(("127.0.0.1", 0), H)
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
            page.on("dialog", lambda d: d.accept())
            page.goto(f"http://127.0.0.1:{port}/index.html", wait_until="networkidle")
            page.click('[data-lens="bastions"]')
            page.wait_for_timeout(900)

            def open_machine(mid):
                page.click(f'.net-card[data-gw="{mid}"]')
                page.wait_for_timeout(250)
                el = page.locator("#inspector-body")
                # Visible text only — markup that failed to parse is not text.
                return el.inner_text()

            print("\n── 1. a LOCAL machine is told how to become shared ──")
            t = open_machine(1)
            check("names set-mode networked", "privatae-bastion set-mode networked" in t, t[:200])
            check("names claim-anchor", "privatae-bastion claim-anchor" in t)
            check("reads as optional", "Optional" in t)
            check("sends the user after NO token",
                  "token" not in t.replace("claim-anchor", "").lower(),
                  "this is the whole complaint — the step must not need a second token")

            print("\n── 2. a NETWORKED machine with no anchor says so ──")
            t = open_machine(2)
            check("names claim-anchor", "privatae-bastion claim-anchor" in t)
            check("says it is not anchored", "not anchored" in t.lower(), t[:200])
            check("does not re-tell it to set the mode", "set-mode networked" not in t)

            print("\n── 3. an ANCHORED machine is told nothing — the step is done ──")
            t = open_machine(3)
            check("no claim-anchor offered", "claim-anchor" not in t, t[:200])
            check("still shows who anchored it", "host-a" in t)

            print("\n── 4. a machine that reports NO mode is not guessed at ──")
            t = open_machine(4)
            check("no instructions on an unknown mode",
                  "set-mode" not in t and "claim-anchor" not in t, t[:200])

            print("\n── 5. Windows is never told to sudo ──")
            t = open_machine(5)
            check("told to use an Administrator terminal", "Administrator" in t, t[:200])
            check("NEVER told to sudo", "sudo" not in t.lower(),
                  "a Windows user handed `sudo -H` reads a correct step as broken")

            print("\n── 5b. a Mac is never told to open PowerShell ──")
            for mid, what in ((6, "darwin"), (7, "darwin-arm64")):
                t = open_machine(mid)
                check(f"{what}: told sudo -H", "sudo -H" in t, t[:200])
                check(f"{what}: NOT told PowerShell",
                      "PowerShell" not in t and "Administrator" not in t)

            print("\n── 5c. a bastion too old to run it is not offered it ──")
            t = open_machine(8)
            check("0.22.0 is not offered claim-anchor", "claim-anchor" not in t, t[:200])

            print("\n── 6. the setup card, from the REAL mint_pairing_token ──")
            page.click("#gw-add")
            page.wait_for_timeout(600)
            t = page.locator("#inspector-body").inner_text()
            check("still shows the pairing token", "skb_" in t, t[:160])
            check("still shows the pair command", "privatae-bastion pair" in t)
            check("carries the share step from the server", "set-mode networked" in t)
            check("names claim-anchor", "claim-anchor" in t)
            check("claim-anchor carries no token argument",
                  "claim-anchor skb_" not in t and "claim-anchor <" not in t)
            check("covers BOTH platforms — the card cannot know which machine this is",
                  "Administrator" in t and "sudo -H" in t)

            b.close()
    finally:
        srv.shutdown()

    print()
    if fails:
        print(f"FAILED — {len(fails)} check(s):")
        for f in fails:
            print("  - " + f)
        return 1
    print("PASSED — the anchor step is readable in a real browser, from the real payload")
    return 0


if __name__ == "__main__":
    sys.exit(main())
