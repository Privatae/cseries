#!/usr/bin/env python3
"""The desktop shell's Content-Security-Policy (server/shell_csp.py), measured
in Chromium against the real shell.

The real dashboard/base/index.html and every script it loads are served from a
loopback server with the policy shell_policy() builds, the API stubbed so the
shell boots and mounts a Host-made desktop (the desktop contract's own template,
in its srcdoc frame). Five policies, each built by the real shell_policy() in
a process that reads its addresses from the environment as the Host does at
start: the default local address (http://home.cseries.localhost:8800), a cloud
machine's own https name, the remote address that machine answers on through
its remote-access door, a member's own machine published on a port of its own
(whose remote address, on https and the default port, gets that address's
scheme and port, not the local one's), and an address that is no desktop name
(127.0.0.1). The cloud and remote addresses are invented (.example).

  P1  the shell boots and mounts the Host-made desktop with no policy violation
      in the shell or in the desktop's frame;
  P2  the shell may frame its own origin, and on a desktop name an isolated
      app's origin on its zone, with the scheme and port the shell builds the
      frame's URL with (app-runner.js frameOrigin);
  P3  script running in the shell reaches no other host: a fetch, an image, a
      WebSocket and a frame aimed at a second loopback server are refused, and
      that server counts no request;
  P4  the control: the same requests with no policy reach the second server,
      so P3 measures the policy and not a dead probe;
  P5  pinned, the lane the policy does not close: script in the shell frames a
      same-origin response that carries no policy (a stylesheet) and reaches
      the second server through that document's fetch and Image.

No pod, no network beyond loopback. Needs Playwright and Chromium.
Run: APP_ROOT=<repo> python3 scripts/test_shell_csp_chromium.py
"""
from __future__ import annotations

import http.server
import json
import mimetypes
import os
import pathlib
import socketserver
import subprocess
import sys
import threading

ROOT = pathlib.Path(os.environ.get("APP_ROOT") or pathlib.Path(__file__).resolve().parents[1])
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "brain"))

from server.applications.desktop_contract import TEMPLATE_HTML  # noqa: E402

BASE = ROOT / "dashboard" / "base"
FAILS: list = []

LOCAL_URL = "http://home.cseries.localhost:8800"
CLOUD_URL = "https://home.b7c4d2.cloud.example"
REMOTE_URL = "https://home.m3n8p5.remote.example"
# A member's own machine, published on a port of its own (CSERIES_PORT).
MEMBER_URL = "http://home.cseries.localhost:9000"

# Run in a fresh interpreter, so home_origin reads the addresses the way the
# Host does when it starts.
_POLICY = """
import json, sys
sys.path.insert(0, sys.argv[1])
from server import shell_csp
from server.applications import app_origin
host = sys.argv[2]
print(json.dumps({"policy": shell_csp.shell_policy(app_origin.classify(host)),
                  "app": app_origin.app_host("a-test-app", host)}))
"""


def policy_for(host, url=None, remote=None):
    """shell_policy() for `host` on a Host whose CSERIES_URL is `url` (None:
    unset, the default) and whose remote address is `remote`, and the name an
    isolated app of that Host is framed on there."""
    env = {k: v for k, v in os.environ.items() if k not in ("CSERIES_URL", "CSERIES_REMOTE_URL")}
    env["CSERIES_REMOTE_STATE"] = str(ROOT / "no-such-remote-state.json")
    if url:
        env["CSERIES_URL"] = url
    if remote:
        env["CSERIES_REMOTE_URL"] = remote
    out = subprocess.run([sys.executable, "-c", _POLICY, str(ROOT), host], env=env,
                         capture_output=True, text=True, timeout=60)
    if out.returncode:
        raise RuntimeError(out.stderr[-600:])
    return json.loads(out.stdout)


def check(label, cond, detail=""):
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  [{str(detail)[:400]}]"))
    if not cond:
        FAILS.append(label)


STUBS = {
    "/api/appearance": {"wallpaper": "html", "desktop_html_url": "/api/appearance/desktop",
                        "desktop_updated_at": "2026-01-01T00:00:00Z", "css": None, "image_url": None},
    "/api/appearance/desktop/meta": {"updated_at": "2026-01-01T00:00:00Z"},
    "/api/appearance/layout": {},
    "/api/applications": [],
    "/api/applications/shell-token": {},
    "/api/system/status": {},
    "/api/system/resources": {},
    "/api/entity/state": {},
    "/api/calendar/list": [],
    "/api/inbox/unread-count": {"count": 0},
}


def shell_server(policy):
    class H(http.server.BaseHTTPRequestHandler):
        def _send(self, code, body, ctype, extra=()):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            for k, v in extra:
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            path = self.path.split("?")[0]
            if path in STUBS:
                return self._send(200, json.dumps(STUBS[path]).encode(), "application/json")
            if path == "/api/appearance/desktop":
                return self._send(200, TEMPLATE_HTML.encode(), "text/html; charset=utf-8")
            if path.startswith("/api/") or path.startswith("/__host/"):
                return self._send(404, b"{}", "application/json")
            if path in ("/", "/index.html"):
                body = (BASE / "index.html").read_bytes()
                extra = [("Content-Security-Policy", policy)] if policy else []
                return self._send(200, body, "text/html; charset=utf-8", extra)
            if path == "/frame-self":
                return self._send(200, b"<!DOCTYPE html><p>same origin</p>", "text/html")
            f = (BASE / path.lstrip("/")).resolve()
            if BASE.resolve() in f.parents and f.is_file():
                ctype = mimetypes.guess_type(str(f))[0] or "application/octet-stream"
                return self._send(200, f.read_bytes(), ctype)
            return self._send(404, b"", "text/plain")

        def log_message(self, *a):
            pass

    srv = socketserver.ThreadingTCPServer(("127.0.0.1", 0), H)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def leak_server(hits):
    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            hits.append(self.path)
            self.send_response(200)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *a):
            pass

    srv = socketserver.ThreadingTCPServer(("127.0.0.2", 0), H)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


# Every frame's violations, the srcdoc desktop's included: an init script runs
# in each document before its own scripts.
INIT = """
window.__csp = [];
document.addEventListener('securitypolicyviolation', (e) => {
  window.__csp.push({dir: e.effectiveDirective, uri: e.blockedURI, src: e.sourceFile, line: e.lineNumber});
});
"""


def violations(pg):
    out = []
    for fr in pg.frames:
        try:
            out += [dict(v, frame=fr.url[:60]) for v in (fr.evaluate("window.__csp || []") or [])]
        except Exception:  # noqa: BLE001 — a frame gone between listing and reading
            pass
    return out


def run(p, label, policy, app_url=None):
    shell = shell_server(policy)
    hits: list = []
    leak = leak_server(hits)
    base = f"http://127.0.0.1:{shell.server_address[1]}"
    other = f"127.0.0.2:{leak.server_address[1]}"
    b = p.chromium.launch()
    try:
        pg = b.new_page()
        # An isolated app's origin is answered here, in the browser: its name
        # is invented, and no server here holds it.
        if app_url:
            pg.route(app_url + "**", lambda r: r.fulfill(
                status=200, content_type="text/html", body="<!DOCTYPE html><p>app</p>"))
        pg.add_init_script(INIT)
        console = []
        pg.on("console", lambda m: console.append(m.text) if "Content Security Policy" in m.text else None)
        pg.goto(base + "/", wait_until="domcontentloaded")
        mounted = True
        try:
            pg.wait_for_selector("iframe.desktop-surface", state="attached", timeout=15000)
            pg.wait_for_timeout(2500)
        except Exception:  # noqa: BLE001
            mounted = False
        before = 0
        if policy is not None:
            check(f"P1 ({label}): the shell mounts the Host-made desktop", mounted)
            v = violations(pg)
            check(f"P1 ({label}): no policy violation while the shell boots and the desktop renders",
                  not v and not console, (v, console[:3]))
            allowed = ["/frame-self"] + ([app_url] if app_url else [])
            before = len(violations(pg))
            pg.evaluate("""(urls) => { for (const u of urls) { const f = document.createElement('iframe');
                f.src = u; document.body.appendChild(f); } }""", allowed)
            pg.wait_for_timeout(800)
            v = violations(pg)[before:]
            check(f"P2 ({label}): the shell frames its own origin"
                  + (f" and an isolated app's origin on its zone ({app_url})" if app_url else ""),
                  not [x for x in v if x["dir"] == "frame-src"], v)
            before = len(violations(pg))
        pg.evaluate("""(o) => {
            fetch('http://' + o + '/fetch').catch(() => {});
            new Image().src = 'http://' + o + '/img';
            try { new WebSocket('ws://' + o + '/ws'); } catch (e) {}
            const f = document.createElement('iframe'); f.src = 'http://' + o + '/frame';
            document.body.appendChild(f);
        }""", other)
        pg.wait_for_timeout(1500)
        if policy is not None:
            dirs = {x["dir"] for x in violations(pg)[before:]}
            check(f"P3 ({label}): a fetch, an image, a WebSocket and a frame to another host are refused",
                  {"connect-src", "img-src", "frame-src"} <= dirs, dirs)
            check(f"P3 ({label}): ... and the other host gets no request", hits == [], hits)
            # The lane the policy does not close (shell_csp.py): a policy is
            # one document's, so a same-origin response served without one is
            # a window whose fetch and Image are not under it. Pinned so the
            # day every document on the origin carries a policy this check
            # fails and the module's note gets rewritten with it.
            pg.evaluate("""(o) => new Promise((done) => {
                const f = document.createElement('iframe'); f.src = '/css/tokens.css';
                f.onload = () => { try {
                    f.contentWindow.fetch('http://' + o + '/same-origin-fetch').catch(() => {});
                    new f.contentWindow.Image().src = 'http://' + o + '/same-origin-img';
                } catch (e) {} done(); };
                document.body.appendChild(f); })""", other)
            pg.wait_for_timeout(1500)
            check(f"P5 ({label}): pinned open lane: a same-origin document with no policy of its own "
                  "still reaches another host",
                  {"/same-origin-fetch", "/same-origin-img"} <= set(hits), hits)
        else:
            check("P4 (control, no policy): the same requests reach the other host",
                  {"/fetch", "/img", "/frame"} <= set(hits), hits)
    finally:
        b.close()
        shell.shutdown()
        leak.shutdown()


def main() -> int:
    from playwright.sync_api import sync_playwright
    local = policy_for("home.cseries.localhost")
    cloud = policy_for("home.b7c4d2.cloud.example", CLOUD_URL, REMOTE_URL)
    remote = policy_for("home.m3n8p5.remote.example", CLOUD_URL, REMOTE_URL)
    elsewhere = policy_for("127.0.0.1")["policy"]
    # A member's own machine on a port of its own with remote access on: its
    # two names differ in scheme and port, and each policy carries its own.
    member = policy_for("home.cseries.localhost", MEMBER_URL, REMOTE_URL)
    member_remote = policy_for("home.m3n8p5.remote.example", MEMBER_URL, REMOTE_URL)
    # The app frame's URL as the shell builds it (app-runner.js frameOrigin):
    # the page's scheme, then "apps-<key>-" and the page's host and port.
    local_app = f"http://{local['app']}:8800/"
    cloud_app = f"https://{cloud['app']}/"
    remote_app = f"https://{remote['app']}/"
    member_app = f"http://{member['app']}:9000/"
    check("a member machine's local address on its own port: its policy frames that zone's apps on "
          "that port and names its WebSocket there",
          "frame-src 'self' http://*.cseries.localhost:9000;" in member["policy"]
          and "connect-src 'self' ws://home.cseries.localhost:9000;" in member["policy"], member)
    check("the same machine's remote address: https on the default port, not the local address's",
          "frame-src 'self' https://*.m3n8p5.remote.example;" in member_remote["policy"]
          and "connect-src 'self' wss://home.m3n8p5.remote.example;" in member_remote["policy"],
          member_remote)
    check("the local address's policy frames that zone's apps on its scheme and port, and "
          "names its WebSocket",
          "frame-src 'self' http://*.cseries.localhost:8800;" in local["policy"]
          and "connect-src 'self' ws://home.cseries.localhost:8800;" in local["policy"], local)
    check("the cloud name's policy frames that zone's apps and names its WebSocket",
          "frame-src 'self' https://*.b7c4d2.cloud.example;" in cloud["policy"]
          and "connect-src 'self' wss://home.b7c4d2.cloud.example;" in cloud["policy"], cloud)
    check("the remote address's policy frames that zone's apps and names its WebSocket",
          "frame-src 'self' https://*.m3n8p5.remote.example;" in remote["policy"]
          and "connect-src 'self' wss://home.m3n8p5.remote.example;" in remote["policy"], remote)
    check("any other address's policy frames and connects only to its own origin",
          "frame-src 'self';" in elsewhere and "connect-src 'self';" in elsewhere, elsewhere)
    with sync_playwright() as p:
        run(p, "local address", local["policy"], local_app)
        run(p, "cloud name", cloud["policy"], cloud_app)
        run(p, "remote address", remote["policy"], remote_app)
        run(p, "local address on its own port", member["policy"], member_app)
        run(p, "no desktop name", elsewhere)
        run(p, "control", None)
    if FAILS:
        print(f"test_shell_csp_chromium: {len(FAILS)} FAILED")
        return 1
    print("test_shell_csp_chromium: all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
