#!/usr/bin/env python3
"""Gate: the platform-only controls, in a real browser (headless Chromium).

Each page is a system app as the Host serves it (system-apps/<app>/index.html
and app.js, with the shared /js and /css), in front of a stand-in for the
Host's routes. GET /api/system/status answers "network" as
server/bridge/platform_reach.py does, and the gated controls follow it
(dashboard/base/js/network-reach.js):

  * a standalone Host (not a member, no ops), a member whose membership
    lapsed (the server answers that the same way) and a Host whose status
    read fails see none of them;
  * a member Host on today's member door (member_client.NETWORK_OPS) sees
    none of them either: the door carries none of their ops;
  * a member Host whose network carries each op sees each control.

The controls: Network's automatic bastion updates (bastion.rollout),
Settings' History tab (pc.update_apply), Designs' Publish (design.publish),
the Gitea app's Mirrors tab (gitea.user_token), and the App Store's "For your
public sites" shelf (webapp.publish_bundle).

Needs the playwright Python package and its Chromium; without them it says so
and exits 3 (a gate that cannot run is not a pass).

    python3 scripts/test_platform_gate_chromium.py
"""
import http.server
import json
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT), str(ROOT / "scripts")]
FAILED = []


def check(label, cond, detail=""):
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  [{str(detail)[:400]}]"))
    if not cond:
        FAILED.append(label)


try:
    from playwright.sync_api import sync_playwright
except ImportError:
    print("cannot run: the playwright Python package is not installed")
    sys.exit(3)

from gate_loopback import ClientClosesFirst  # noqa: E402
from server.bridge.member_client import NETWORK_OPS  # noqa: E402

GATED = {
    "bastion.rollout": "Network: automatic updates",
    "pc.update_apply": "Settings: History tab",
    "design.publish": "Designs: Publish",
    "gitea.user_token": "Gitea: Mirrors tab",
    "webapp.publish_bundle": "App Store: For your public sites",
}

# Invented values only.
DESIGNS = {"designs": [], "local": [{"artifact_id": 7, "name": "Desktop - Example"}],
           "wearing": None, "host": "Example Host", "publishes_to": "account",
           "library_error": None}

state = {"network": None, "status_code": 200}


class Host(ClientClosesFirst, http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _file(self, base: Path, rel: str):
        f = (base / rel).resolve()
        if not str(f).startswith(str(base.resolve())) or not f.is_file():
            return self._send(404, b"", "text/plain")
        ctype = {".html": "text/html; charset=utf-8", ".css": "text/css",
                 ".js": "text/javascript", ".svg": "image/svg+xml"}.get(f.suffix, "application/octet-stream")
        return self._send(200, f.read_bytes(), ctype)

    def do_GET(self):
        path = self.path.partition("?")[0]
        if path.startswith("/css/") or path.startswith("/js/"):
            return self._file(ROOT / "dashboard/base", path.lstrip("/"))
        if path.startswith("/system-apps/"):
            return self._file(ROOT / "system-apps", path[len("/system-apps/"):])
        if path == "/api/system/status":
            if state["status_code"] != 200:
                return self._send(state["status_code"], {"detail": "down"})
            return self._send(200, {"entity_id": 1, "entity_display_name": "Example Host",
                                    "network": state["network"]})
        if path == "/api/designs":
            return self._send(200, DESIGNS)
        if path.startswith("/api/designs/local/"):
            return self._send(200, {"artifact_id": 7, "html": "<!DOCTYPE html><p>x</p>"})
        if path == "/api/network/bastions/auto-update":
            return self._send(200, {"enabled": True, "latest": None})
        if path == "/api/update/history":
            return self._send(200, {"history": []})
        if path.startswith("/api/"):
            return self._send(200, {})
        return self._send(404, b"", "text/plain")

    def do_POST(self):
        return self._send(200, {})


srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Host)
threading.Thread(target=srv.serve_forever, daemon=True).start()
BASE = f"http://127.0.0.1:{srv.server_address[1]}"


def shown(page, op):
    """Whether the control that needs `op` is on screen."""
    js = {
        "bastion.rollout": "(() => { const e = document.getElementById('autoupdate-block');"
                           " return !!e && !e.hidden && e.offsetParent !== null; })()",
        "pc.update_apply": "(() => { const e = document.querySelector('.cseries-nav-btn[data-view=\"history\"]');"
                           " return !!e && !e.hidden && e.offsetParent !== null; })()",
        "design.publish": "!!document.querySelector('[data-act=\"publish\"]')",
        "gitea.user_token": "(() => { const e = document.querySelector('.cseries-nav-btn[data-view=\"mirrors\"]');"
                            " return !!e && !e.hidden && e.offsetParent !== null; })()",
        "webapp.publish_bundle": "(() => { const e = document.querySelector('[data-filter=\"sites\"]');"
                                 " return !!e && !e.hidden && e.offsetParent !== null; })()",
    }[op]
    return page.evaluate(js)


PAGES = {
    "bastion.rollout": "/system-apps/network/index.html",
    "pc.update_apply": "/system-apps/settings/index.html",
    "design.publish": "/system-apps/designs/index.html",
    "gitea.user_token": "/system-apps/gitea/index.html",
    "webapp.publish_bundle": "/system-apps/app-store/index.html",
}


def visit(browser, op):
    page = browser.new_page()
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto(BASE + PAGES[op])
    # The helper has read the status once the page's own first read is done.
    page.wait_for_function("window.CSeriesReach && window.CSeriesReach.load().then(() => true)",
                           timeout=15000)
    if op == "design.publish":
        page.wait_for_function("document.querySelectorAll('.card').length > 0", timeout=15000)
    elif op == "bastion.rollout":
        # The block sits on the Bastions lens.
        page.click('.net-rail-btn[data-lens="bastions"]')
        page.wait_for_timeout(300)
    else:
        page.wait_for_timeout(300)
    out = shown(page, op)
    page.close()
    return out, errors


SCENARIOS = [
    ("a standalone Host", {"member": False, "ops": []}, 200, set()),
    ("a lapsed member (the server answers not a member)", {"member": False, "ops": []}, 200, set()),
    ("a Host whose status read fails", None, 500, set()),
    ("an answer with ops but no membership", {"member": False, "ops": sorted(GATED)}, 200, set()),
    ("a member on today's member door", {"member": True, "ops": sorted(NETWORK_OPS)}, 200,
     set(GATED) & set(NETWORK_OPS)),
    ("a member whose network carries every op", {"member": True, "ops": sorted(GATED)}, 200, set(GATED)),
]

pw = sync_playwright().start()
try:
    browser = pw.chromium.launch()
except Exception as e:
    print(f"cannot run: no browser here ({str(e)[:300]})")
    sys.exit(3)
try:
    for label, network, code, want in SCENARIOS:
        state["network"], state["status_code"] = network, code
        for op, name in GATED.items():
            got, errors = visit(browser, op)
            should = op in want
            check(f"{label}: {name} {'shown' if should else 'hidden'}", got is should,
                  f"shown={got} errors={errors[:2]}")
finally:
    browser.close()
    pw.stop()
    srv.shutdown()

if FAILED:
    print(f"\nFAIL: {len(FAILED)} check(s)")
    sys.exit(1)
print("\nall checks passed")
