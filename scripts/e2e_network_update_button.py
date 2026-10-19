#!/usr/bin/env python3
"""The Bastions lens reports the CURRENT published version — asserted in a real browser.

Source-level reasoning about this was wrong twice on 2026-09-12, in both
directions, which is why this exists:

  - I read the API, saw {"version": "0.22.0"}, and reported the Update button
    was live. It was not: the app reads that number ONCE per page load
    (`state.latestVersion === null`), so a tab open across a publish keeps the
    old one for ever and Refresh cannot fix it — the guard blocks the re-read.
  - Then I said the button HIDES. It does not. With the stale number the only
    machines that look out of date are two long-dead boxes on an ancient
    version, so it renders "Update 2 machines to 0.21.0" — the wrong count and
    the wrong version, while twenty live machines that genuinely need the new
    build are shown nothing at all. Worse than hidden: confidently wrong.

Both readings came from the source. Neither survived contact with the rendered
page. So this drives the REAL Bastions lens in Chromium against a stub server,
and CHANGES THE PUBLISHED VERSION BETWEEN REFRESHES — the button is observed
following the server's answer rather than a value the test also planted.

Same shape and the same reason as e2e_desktop_button.py.

Run:  python3 scripts/e2e_network_update_button.py
"""
import http.server
import json
import os
import pathlib
import socketserver
import sys
import threading

# Default to THIS file's tree, as e2e_hosted_map_egress.py does. Hardcoding
# a shared checkout meant a run from a worktree silently measured whatever branch
# that clone happened to be parked on — it reported 2/4 FAIL against code that was
# not under test.
ROOT = pathlib.Path(os.environ.get("APP_ROOT") or pathlib.Path(__file__).resolve().parent.parent)
APP = ROOT / "system-apps" / "network"
BASE = ROOT / "dashboard" / "base"   # /css, /js, /fonts — the page will not boot without them

# What the server currently says is published. Mutated mid-test.
PUBLISHED = {"version": "0.22.0"}

# 22 machines, shaped like a live fleet read on 2026-09-12:
# twenty on 0.21.0 and two long-dead ones on 0.13.13.
BASTIONS = (
    [{"id": i, "hostname": f"box-{i}", "bastion_version": "0.21.0", "online": True,
      "platform": "linux", "last_seen_at": "2026-09-12T18:00:00Z", "update_to_version": None,
      "capabilities": [], "mode": "local"} for i in range(1, 21)]
    + [{"id": 90 + i, "hostname": f"dead-{i}", "bastion_version": "0.13.13", "online": False,
        "platform": "linux", "last_seen_at": "2026-09-08T14:00:00Z", "update_to_version": None,
        "capabilities": [], "mode": None} for i in range(2)]
)


class H(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def translate_path(self, path):
        # The page boots from TWO roots and will not initialise from one: index.html
        # pulls /css/* and /js/* out of the dashboard base, and only /system-apps/
        # out of the app. Serving just the app dir 404s the shared assets, app.js
        # never finishes init, the rail click handler at :3136 is never bound, and
        # #lens-bastions stays hidden for ever — which is exactly how the first
        # version of this test failed while looking like an app bug.
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
        if p == "/api/network/bastion-latest":
            return self._json(PUBLISHED)
        if p == "/api/network/bastions":
            return self._json({"bastions": BASTIONS})
        if p.startswith("/api/"):
            # Everything else the lens touches is best-effort in the app itself.
            return self._json({})
        return super().do_GET()

    def do_POST(self):
        return self._json({"ok": True})


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
            # Update all asks for confirmation; accept it so the click completes.
            page.on("dialog", lambda d: d.accept())
            page.goto(f"http://127.0.0.1:{port}/index.html", wait_until="networkidle")
            page.click('[data-lens="bastions"]')
            page.wait_for_timeout(1200)

            print("\n── 1. a freshly loaded tab reports what is published ──")
            btn = page.locator("#gw-update-all")
            label = (btn.text_content() or "").strip()
            # EXACT, not substring: "22" is inside "0.22.0", so `"22" in label` was
            # true even for the buggy "Update 2 machines to 0.22.0" — the count
            # assertion could not fail, which is the bug this test exists to catch.
            check("the button names the CURRENT version and every machine that needs it",
                  label == "Update 22 machines to 0.22.0", f"button read: {label!r}")

            print("\n── 2. a new version is published WHILE THE TAB IS OPEN ──")
            PUBLISHED["version"] = "0.23.0"
            page.click("#gw-refresh")
            page.wait_for_timeout(1500)
            label2 = (page.locator("#gw-update-all").text_content() or "").strip()
            check("Refresh follows the server to the new version",
                  label2 == "Update 22 machines to 0.23.0", f"button read: {label2!r}")

            print("\n── 3. the lookup failing does not erase a known-good answer ──")
            PUBLISHED.clear()  # bastion-latest now answers {} -> .version undefined
            page.click("#gw-refresh")
            page.wait_for_timeout(1500)
            label3 = (page.locator("#gw-update-all").text_content() or "").strip()
            hidden = page.locator("#gw-update-all").is_hidden()
            check("a blipped lookup keeps the last known version rather than hiding the path",
                  (not hidden) and label3 == "Update 22 machines to 0.23.0",
                  f"hidden={hidden} button={label3!r}")
            check("...and the cannot-tell banner is retracted once it CAN tell",
                  page.locator("#gw-notice").is_hidden(),
                  "notice: " + repr((page.locator("#gw-notice").text_content() or "")[:90]))

            print("\n── 4. FABLE B1: ask, then a new version is published, then Refresh ──")
            # The session set marks a machine "asked". Its ONLY removal needs
            # bastion_version === latestVersion, so once the published version MOVES
            # every machine asked at the old one is stranded: excluded from the count,
            # rendered "updating on next check-in", no per-card Update. After an
            # Update all that is the entire fleet — the original complaint, arriving
            # again by a different route.
            PUBLISHED["version"] = "0.23.0"
            page.click("#gw-refresh"); page.wait_for_timeout(1200)
            page.click("#gw-update-all")
            page.wait_for_timeout(1200)
            for b_ in BASTIONS:                      # the server accepted the directive
                b_["update_to_version"] = "0.23.0"
            PUBLISHED["version"] = "0.24.0"          # ...and now a newer one is published
            for b_ in BASTIONS:
                b_["update_to_version"] = "0.23.0"   # the stale directive stays on the row
            page.click("#gw-refresh"); page.wait_for_timeout(1500)
            label4 = (page.locator("#gw-update-all").text_content() or "").strip()
            hidden4 = page.locator("#gw-update-all").is_hidden()
            check("a machine asked at the OLD version is offered the new one",
                  (not hidden4) and label4 == "Update 22 machines to 0.24.0",
                  f"hidden={hidden4} button={label4!r}")

            b.close()
    finally:
        srv.shutdown()

    print(f"\nFAILURES: {len(fails)}")
    return 1 if fails else 0


sys.exit(main())
