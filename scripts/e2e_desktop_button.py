#!/usr/bin/env python3
""""Use as desktop" reflects the kernel's verdict — asserted in a real browser.

scripts/test_desktop_eligibility.py proves the check and the setter are the same
code. That is the important half, and it says nothing about whether the WINDOW
acts on the answer — the first version of this feature looked correct in source
and left the button reading "checking…" forever, because the call site was never
reached.

So this drives the real artifacts app in Chromium against a stubbed KERNEL, and
flips the verdict between runs: the button is observed responding to the
kernel's answer rather than to a document the test also wrote.

Run:  python3 scripts/e2e_desktop_button.py
"""
import http.server, socketserver, threading, json, os, pathlib
# The tree this file lives in (APP_ROOT to point elsewhere) — it used to be
# a fixed shared checkout, so the gate drove whatever that checkout held and
# never the branch under review.
ROOT = pathlib.Path(os.environ.get("APP_ROOT") or pathlib.Path(__file__).resolve().parents[1])
VERDICT = {"ok": False, "mode": None, "errors": ["no mode declared — add <meta name=\"cseries-desktop\" ...>"],
           "warnings": [], "tokens": 0, "report": "REFUSED — the document does not meet the desktop floor:\n  - no mode declared"}
class H(http.server.SimpleHTTPRequestHandler):
    def _json(self, obj):
        b = json.dumps(obj).encode(); self.send_response(200)
        self.send_header("Content-Type","application/json"); self.send_header("Content-Length",str(len(b)))
        self.end_headers(); self.wfile.write(b)
    def do_GET(self):
        p = self.path.split("?")[0]
        if p == "/api/appearance/desktop/check": return self._json(VERDICT)
        if p == "/api/artifacts": return self._json({"artifacts":[{"artifact_id":1,"name":"poster","entry":"index.html","updated_at":"2026-09-07T00:00:00Z"}]})
        if p.startswith("/api/artifacts/1/render"):
            b=b"<!DOCTYPE html><html><body>poster</body></html>"; self.send_response(200)
            self.send_header("Content-Type","text/html"); self.send_header("Content-Length",str(len(b)))
            self.end_headers(); self.wfile.write(b); return
        if p.startswith("/api/artifacts/1"): return self._json({"artifact_id":1,"name":"poster","entry":"index.html","files":["index.html"]})
        if p.startswith("/api/"): return self._json({})
        if p in ("/","/index.html"): self.path="/system-apps/artifacts/index.html"
        # The shell's /js/, /css/ and /fonts/ live under dashboard/base, as the kernel
        # serves them — without them the window runs with no sandbox policy.
        if p.startswith(("/js/", "/css/", "/fonts/")): self.path = "/dashboard/base" + self.path
        return super().do_GET()
    def log_message(self,*a): pass
os.chdir(ROOT)
# A free port, bound once: the fixed port with allow_reuse_address set after
# the bind (too late — TCPServer binds in its constructor) made a second run
# inside TIME_WAIT die on "Address already in use".
srv = socketserver.TCPServer(("127.0.0.1", 0), H)
BASE = f"http://127.0.0.1:{srv.server_address[1]}"
threading.Thread(target=srv.serve_forever, daemon=True).start()

refused_disabled = refused_reason = compliant_disabled = no_panel = None
from playwright.sync_api import sync_playwright
with sync_playwright() as p:
    b=p.chromium.launch(); pg=b.new_page()
    errs=[]; pg.on("pageerror", lambda e: errs.append(str(e)))
    reqs=[]
    pg.on("request", lambda r: reqs.append(r.url.replace(BASE,"")))
    pg.on("console", lambda m: errs.append("console:"+m.text) if m.type=="error" else None)
    pg.goto(BASE + "/", wait_until="networkidle"); pg.wait_for_timeout(1500)
    print("API calls before opening:", [r for r in reqs if r.startswith("/api")][:8])
    # Open the artifact the way the window does (the list click path calls this).
    pg.evaluate("() => window.App.openArtifact(1)")
    pg.wait_for_timeout(1500)
    print("API calls after opening:", [r for r in reqs if r.startswith("/api")][:12])
    dis = pg.get_attribute("#btn-desktop","disabled")
    title = pg.get_attribute("#btn-desktop","title")
    refused_disabled = dis is not None
    print("REFUSED doc  -> disabled:", refused_disabled, "| title:", (title or "")[:80])
    # The reason rides in the tooltip, and NOTHING is painted into the viewer:
    # every artifact that was never meant to be a desktop is one the floor
    # declines, so a refusal panel was showing on almost all of them
    # (2026-09-10). The report belongs to the click, not to merely looking.
    refused_reason = "no mode declared" in (title or "")
    print("             -> reason in the tooltip:", refused_reason)
    no_panel = pg.query_selector("#desktop-why") is None
    print("             -> no refusal panel in the viewer:", no_panel)
    # now flip the verdict to OK and re-render
    VERDICT.update({"ok":True,"mode":"wallpaper","errors":[],"tokens":11,"report":""})
    pg.reload(wait_until="networkidle"); pg.wait_for_timeout(800)
    pg.evaluate("() => window.App.openArtifact(1)")
    pg.wait_for_timeout(1500)
    dis2 = pg.get_attribute("#btn-desktop","disabled")
    compliant_disabled = dis2 is not None
    print("COMPLIANT doc-> disabled:", compliant_disabled, "| title:", (pg.get_attribute("#btn-desktop","title") or "")[:70])
    print("page errors:", errs[:2])
    b.close()

import sys
fails = []
if not refused_disabled:
    fails.append("a document the kernel refuses must leave the button DISABLED")
if not refused_reason:
    fails.append("the refusal reason must be in the button's title, so the user can ask why")
if not no_panel:
    fails.append("a declined document must NOT paint a refusal panel into the viewer")
if compliant_disabled:
    fails.append("a document the kernel accepts must ENABLE the button")
if any("desktop/check" not in "".join(reqs) for _ in [0]):
    fails.append("the window must ask /api/appearance/desktop/check")
if fails:
    print("\nFAIL — " + str(len(fails)) + " problem(s):")
    for f in fails:
        print("  · " + f)
    sys.exit(1)
print("\nok — the button follows the kernel, both ways")
