#!/usr/bin/env python3
"""The NAC page leads with the lanes, and posture draws one card per machine.

2026-09-13: posture by bastion was messy — duplicates and stale entries made the
whole NAC page noisy; the lanes section is what the page is for, and a
per-bastion view did not read.

Both halves were real and they had different causes.

THE DUPLICATES WERE DATA. nac_endpoints keys on token_id, and re-pairing a
machine mints a NEW token, so every enrolment a box ever had left a posture row
behind and the page drew all of them. Measured on one Host: 33 rows for 27
machines, one desktop alone holding FIVE, ten rows whose posture had not
moved in over a week, and six hostnames with no live enrolment at all —
including a server decommissioned weeks before, still rendered as a
card with a working Quarantine button. Fixed in the route.

THE NOISE WAS ORDER. Posture sat ABOVE the lanes and was always open, so the
first thing the page showed was the longest and least actionable thing on it.
It is a real control, so it stays — below the lanes, collapsed, in the same
accordion idiom as everything else.

Asserted in a real browser on visible TEXT and element order, because "it is
above the lanes" and "it is collapsed" are facts about a rendered page and
nothing else can check them.

Run:  python3 scripts/e2e_nac_posture_noise.py
"""
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

NOW = "2026-09-13T02:00:00Z"

# Shaped like the real table AFTER the route's fix: one row per machine, live
# enrolments only. The duplicates the fix removes are asserted server-side in
# test_nac_endpoints_dedupe.py; here we prove the page renders what it is given
# and puts it in the right place.
ENDPOINTS = [
    {"token_id": 70, "hostname": "DESKTOP-EXAMPLE1", "posture": "healthy",
     "posture_at": NOW, "decision": {"governed": True, "allowed": True, "profile": None, "locked": False, "default": False, "why": "default-preserve: no NAC policy authored"}, "last_decision": "All checks current."},
    {"token_id": 61, "hostname": "box-a", "posture": "healthy",
     "posture_at": NOW, "decision": {"governed": True, "allowed": True, "profile": None, "locked": False, "default": False, "why": "default-preserve: no NAC policy authored"}, "last_decision": "All checks current."},
    {"token_id": 55, "hostname": "router-1", "posture": "degraded",
     "posture_at": NOW, "decision": {"governed": True, "allowed": True, "profile": None, "locked": False, "default": False, "why": "default-preserve: no NAC policy authored"}, "last_decision": "Late check-in."},
]


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

    def _json(self, obj):
        b = json.dumps(obj).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        p = self.path.split("?")[0]
        if p == "/api/network/nac/endpoints":
            return self._json({"endpoints": ENDPOINTS, "suspended": [], "quarantined": []})
        if p.startswith("/api/"):
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
            page = b.new_page(viewport={"width": 1280, "height": 1000})
            page.goto(f"http://127.0.0.1:{port}/index.html", wait_until="networkidle")
            page.click('[data-lens="nac"]')
            page.wait_for_timeout(900)

            print("\n── 1. the lanes come FIRST ──")
            order = page.evaluate("""() => {
                const ids = ['nac-lane-ingress','nac-lane-egress','nac-lane-mgmt','nac-lane-posture'];
                const seen = ids.filter(i => document.getElementById(i));
                return seen.map(i => ({ id: i, top: document.getElementById(i).getBoundingClientRect().top }));
            }""")
            byid = {o["id"]: o["top"] for o in order}
            check("all four sections are present", len(byid) == 4, byid)
            if len(byid) == 4:
                check("posture is BELOW ingress", byid["nac-lane-posture"] > byid["nac-lane-ingress"])
                check("posture is BELOW egress", byid["nac-lane-posture"] > byid["nac-lane-egress"])
                check("posture is BELOW management", byid["nac-lane-posture"] > byid["nac-lane-mgmt"],
                      "this is the whole ask — the lanes are what he wants to see")

            print("\n── 2. posture is COLLAPSED on arrival ──")
            expanded = page.get_attribute('[data-lane-toggle="posture"]', "aria-expanded")
            check("aria-expanded is false", expanded == "false", f"got {expanded!r}")
            check("the card grid is not visible", not page.is_visible("#nac-posture-grid"))
            # The count must be readable WITHOUT opening it, like the other lanes.
            check("the collapsed head carries the count",
                  (page.inner_text("#nac-posture-count") or "").strip() == "3",
                  page.inner_text("#nac-posture-count"))

            print("\n── 3. opening it still works, and draws one card per machine ──")
            page.click('[data-lane-toggle="posture"]')
            page.wait_for_timeout(300)
            check("aria-expanded flips to true",
                  page.get_attribute('[data-lane-toggle="posture"]', "aria-expanded") == "true")
            check("the grid is visible now", page.is_visible("#nac-posture-grid"))
            cards = page.locator("#nac-posture-grid .nac-posture-card").count()
            check("one card per machine", cards == 3, f"got {cards}")
            t = page.inner_text("#nac-posture-grid")
            for h in ("DESKTOP-EXAMPLE1", "box-a", "router-1"):
                check(f"{h} is drawn once", t.count(h) == 1, f"count={t.count(h)}")
            check("the controls survived the move", page.locator('[data-nac-suspend]').count() > 0,
                  "Suspend/Quarantine are the reason this section still exists")

            b.close()
    finally:
        srv.shutdown()

    print()
    if fails:
        print(f"FAILED — {len(fails)} check(s):")
        for f in fails:
            print("  - " + f)
        return 1
    print("PASSED — lanes first, posture collapsed below them, one card per machine")
    return 0


if __name__ == "__main__":
    sys.exit(main())
