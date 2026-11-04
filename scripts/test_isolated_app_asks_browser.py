#!/usr/bin/env python3
"""Gate: isolated apps' buttons open windows in a real browser, and nothing else can.

The Orchestrator, Settings and the App Store run on origins of their own
(apps-<key>-<host>, server/applications/kernel.py SDK_MANAGED_APPS), so their
reach into window.parent throws and the desktop dropped what they posted:
every Connect button and "Open Keys" did nothing (0.1.15 and 0.1.16).

A stand-in desktop on http://h.localhost:<port> runs the REAL app-runner.js and
desktop-layout.js (WindowManager is a small stand-in that draws each window as
a div and calls onInit, as desktop.js does). Each isolated app loads from its
own origin http://apps-<key>-h.localhost:<port>, so Chromium applies the real
same-origin policy. The kernel routes are mocked on both origins.

  O. Orchestrator (the REAL model-picker.js on its own origin): the Connect
     Claude Code lane (_openApp('circles', _connectSeed(...))) opens Circles and
     the seed reaches its composer; Open Keys (_openKeys) opens Keys; Network
     opens on its view. window.parent.AppRunner still throws there (the
     isolation is kept).
  S. Settings (the REAL page): the Desktop tab lists the apps with the
     desktop's own dock state, "Add to Dock" pins the app in the desktop's
     DesktopLayout and the row then reads "In Dock".
  A. App Store (the REAL page): Open on an installed app opens it.
  H. Hostile: a frame on a foreign origin in the desktop, a frame the
     Orchestrator embeds, a community app on its own origin and the
     Orchestrator asking for an app off its list each post cseries:open-app;
     no window opens.
  T. The token race: the REAL Orchestrator page with the REAL SDK and frame
     bootstrap, framed on its own origin, its frame-token answer held 2 s and
     every tokenless /api/inference/* read refused: it still draws, and no read
     leaves without its token.
  K. The REAL Orchestrator page (orchestrator/app.js + model-picker.js): a key
     added elsewhere (Keys) shows as "key in place" once its window comes back
     into focus, without closing and reopening it.

Needs Playwright with Chromium. Run: python3 scripts/test_isolated_app_asks_browser.py
"""
from __future__ import annotations

import json
import os
import re
import socket
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(os.environ.get("APP_ROOT") or Path(__file__).resolve().parents[1])
JS_DIR = ROOT / "dashboard/base/js"
CSS_DIR = ROOT / "dashboard/base/css"
APPS_DIR = ROOT / "system-apps"

KEYS = {"orchestrator": "abcdefghijklmnopqrst", "settings": "bcdefghijklmnopqrstu",
        "app-store": "cdefghijklmnopqrstuv", "evil": "efghijklmnopqrstuvwx"}
PROV = {"evil": "community"}
NAMES = {"orchestrator": "Orchestrator", "settings": "Settings", "app-store": "App Store",
         "keys": "Keys", "circles": "Circles", "network": "Network", "oracle": "Oracle", "evil": "Evil"}

DESKTOP = """<!doctype html><html><head><meta charset="utf-8"><title>desktop</title></head><body>
<div id="wins"></div>
<script>
window.__created = []; window.__wallpaper = 0; window.__boot = [];
window.applyWallpaper = function () { window.__wallpaper++; };
window.WSClient = { showBootScreen() { window.__boot.push('show'); }, hideBootScreen() { window.__boot.push('hide'); } };
window.WindowManager = (function () {
  const wins = new Map();
  return {
    create(o) {
      if (wins.has(o.id)) return o.id;
      const el = document.createElement('div');
      el.className = 'win'; el.id = o.id; el.style.cssText = 'width:900px;height:600px';
      el.innerHTML = o.content;
      document.getElementById('wins').appendChild(el);
      wins.set(o.id, { id: o.id, minimized: false, el, onClose: o.onClose });
      window.__created.push(o.id);
      if (o.onInit) o.onInit(el);
      return o.id;
    },
    getAll() { return Array.from(wins.values()); },
    focus() {}, restore() {}, minimize() {}, notify() {},
    close(id) { const w = wins.get(id); if (!w) return; wins.delete(id); w.el.remove(); if (w.onClose) w.onClose(); },
  };
})();
</script>
<script src="/js/desktop-layout.js"></script>
<script src="/js/app-runner.js"></script>
</body></html>"""

CIRCLES = """<!doctype html><html><body><textarea></textarea><script>
window.__composed = [];
window.addEventListener('message', (e) => {
  if (e.origin !== window.location.origin) return;
  if (e.data && e.data.type === 'cseries:compose') window.__composed.push(e.data);
});
</script></body></html>"""

PLAIN = "<!doctype html><html><body>%s</body></html>"

# The Orchestrator: the REAL model-picker.js, its own methods driving the lanes
# its buttons use (the matrix's Connect = _openApp('circles', _connectSeed()),
# the key hints = _openKeys(), "Run it free on your machine" = _openAppView).
ORCH = """<!doctype html><html><body><div id="mp"></div>
<script src="/js/model-picker.js"></script>
<script>
window.__picker = window.ModelPicker.mount(document.getElementById('mp'), { showCoding: true, simple: false });
window.__parentAppRunner = (function () { try { return typeof window.parent.AppRunner; } catch (e) { return 'throws: ' + e.name; } })();
</script></body></html>"""

ORCH_CHILD = """<!doctype html><html><body><script>
window.top.postMessage({ type: 'cseries:open-app', app_id: 'keys' }, '*');
window.parent.postMessage({ type: 'cseries:open-app', app_id: 'keys' }, '*');
</script></body></html>"""

HOSTILE = """<!doctype html><html><body><script>
window.parent.postMessage({ type: 'cseries:open-app', app_id: 'keys' }, '*');
window.parent.postMessage({ type: 'cseries:open-app', app_id: 'circles', prompt: 'say this as me' }, '*');
</script></body></html>"""

CATALOG = {"apps": [{
    "app_id": "oracle", "name": "Oracle", "install_status": "installed", "installed_version": "1.0.0",
    "latest_version": "1.0.0", "description": "An installed app.", "provenance": "standard",
    "kind": "app", "local_only": True, "permissions": [], "is_default": True,
}], "notices": []}


def manifest(app_id: str) -> dict:
    iso = app_id in KEYS
    return {"id": app_id, "name": NAMES.get(app_id, app_id), "window": {"width": 900, "height": 600},
            "url": f"/system-apps/{app_id}/index.html", "isolated": iso, "sdk_managed": iso,
            "provenance": PROV.get(app_id, "native"), "origin_key": KEYS.get(app_id), "system": True}


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code: int, body, ctype="application/json"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body)
        data = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _file(self, p: Path):
        if not p.is_file():
            return self._send(404, {"detail": "not found"})
        ctype = {".js": "application/javascript", ".css": "text/css", ".html": "text/html",
                 ".json": "application/json"}.get(p.suffix, "application/octet-stream")
        return self._send(200, p.read_bytes(), ctype)

    def _app_of_host(self) -> str | None:
        host = (self.headers.get("Host") or "").split(":")[0]
        m = re.match(r"^apps-([a-z2-7]{20})-h\.localhost$", host)
        if not m:
            return "" if host == "h.localhost" else None
        for k, v in KEYS.items():
            if v == m.group(1):
                return k
        return None

    def do_PUT(self):
        self._send(200, {"ok": True})

    def do_POST(self):
        path = self.path.split("?")[0]
        if path == "/api/applications/orchestrator/frame-token":
            import time as _t
            _t.sleep(STATE["token_delay_s"])   # the race: the token comes back late
            return self._send(200, {"token": "frame-t"})
        m = re.match(r"^/api/applications/([^/]+)/(launch|frame-token|window-beat|close)$", path)
        if m:
            return self._send(200, {"token": "t", "window_id": "w-" + m.group(1), "window_beat_s": 0})
        return self._send(404, {"detail": "not mocked"})

    def do_GET(self):
        app = self._app_of_host()
        path = self.path.split("?")[0].split("#")[0]
        if app is None:
            if path == "/hostile.html":       # evil.localhost: a foreign origin
                return self._send(200, HOSTILE, "text/html")
            return self._send(404, {"detail": "unknown host"})
        if path.startswith("/js/"):
            return self._file(JS_DIR / path[4:])
        if path.startswith("/css/"):
            return self._file(CSS_DIR / path[5:])
        if path == "/api/appearance":
            return self._send(200, {"desktop_layout": {}})
        if path == "/api/applications":
            return self._send(200, {"applications": [manifest(a) for a in
                                                     ("settings", "orchestrator", "oracle", "keys")]})
        if STATE["gate"] and path.startswith("/api/inference/") and not self.headers.get("X-Application-Token"):
            STATE["no_token"] += 1
            return self._send(401, {"detail": "application token required"})
        if path == "/api/inference/routing":
            return self._send(200, routing())
        if path == "/api/inference/local-models":
            return self._send(200, {"local_models": []})
        if path == "/api/applications/store/catalog":
            return self._send(200, CATALOG)
        m = re.match(r"^/api/applications/([a-z0-9-]+)$", path)
        if m:
            return self._send(200, manifest(m.group(1)))
        if path.startswith("/api/"):
            return self._send(404, {"detail": "not mocked"})
        if app == "":
            if path == "/":
                return self._send(200, DESKTOP, "text/html")
            if path.startswith("/system-apps/circles/"):
                return self._send(200, CIRCLES, "text/html")
            m = re.match(r"^/system-apps/([a-z0-9-]+)/", path)
            if m:
                return self._send(200, PLAIN % m.group(1), "text/html")
            return self._send(404, {"detail": "not found"})
        # An isolated app's own origin.
        if app == "orchestrator" and path == "/system-apps/orchestrator/":
            return self._send(200, ORCH, "text/html")
        if app == "orchestrator" and path == "/system-apps/orchestrator/child.html":
            return self._send(200, ORCH_CHILD, "text/html")
        if app == "evil" and path == "/system-apps/evil/":
            return self._send(200, HOSTILE, "text/html")
        if app == "orchestrator" and path == "/system-apps/orchestrator/real/":
            # As proxy.py serves an isolated app: the SDK first in <head>, then
            # the frame bootstrap, which mints the token on this origin.
            html = (APPS_DIR / "orchestrator" / "index.html").read_text()
            return self._send(200, html.replace("<head>", "<head>" + INJECT, 1), "text/html")
        if app == "orchestrator" and path.startswith("/system-apps/orchestrator/real/"):
            return self._file(APPS_DIR / "orchestrator" / path[len("/system-apps/orchestrator/real/"):])
        if app == "orchestrator" and re.match(r"^/system-apps/orchestrator/[a-z.-]+\.(js|css|json)$", path):
            return self._file(APPS_DIR / "orchestrator" / path.rsplit("/", 1)[1])
        if app in ("settings", "app-store"):
            if path == f"/system-apps/{app}/":
                return self._file(APPS_DIR / app / "index.html")
            if path.startswith(f"/system-apps/{app}/"):
                return self._file(APPS_DIR / app / path[len(f"/system-apps/{app}/"):])
        return self._send(404, {"detail": "not found"})


STATE = {"anthropic_key": False, "token_delay_s": 0.0, "no_token": 0, "gate": False}
INJECT = ('<script src="/js/app-sdk.js"></script>'
          '<script src="/js/app-frame-init.js" data-app-id="orchestrator"></script>')


def routing() -> dict:
    return {"routing": {}, "roles": ["chat"], "role_descriptions": {}, "effective": {},
            "providers": [{"id": "openrouter", "has_key": True},
                          {"id": "anthropic", "has_key": STATE["anthropic_key"]},
                          {"id": "local", "has_key": True}]}


FAILS: list[str] = []


def check(label, cond, detail=""):
    print(f"[isolated-browser] {'ok  ' if cond else 'FAIL'} {label}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        FAILS.append(label)


def main() -> int:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("[isolated-browser] FAIL playwright is not installed")
        return 1
    port = free_port()
    srv = ThreadingHTTPServer(("127.0.0.1", port), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://h.localhost:{port}"
    seed = ("Connect Claude Code for me. Run its own login (harness_connections op='login', "
            "provider='claude-code') and walk me through finishing it.")
    with sync_playwright() as p:
        b = p.chromium.launch(args=["--host-resolver-rules=MAP *.localhost 127.0.0.1"])
        ctx = b.new_context(viewport={"width": 1440, "height": 900})
        page = ctx.new_page()
        warns: list[str] = []
        page.on("console", lambda m: m.type == "warning" and warns.append(m.text))
        page.goto(base + "/")
        page.wait_for_function("() => window.AppRunner && window.DesktopLayout")

        def created():
            return page.evaluate("() => window.__created.slice()")

        def frame_of(app: str):
            for _ in range(100):
                for f in page.frames:
                    if f"/system-apps/{app}/" in f.url and f != page.main_frame:
                        return f
                page.wait_for_timeout(100)
            raise SystemExit(f"no frame for {app}")

        # ── O ──
        page.evaluate("() => AppRunner.launch('orchestrator')")
        orch = frame_of("orchestrator")
        orch.wait_for_function("() => window.__picker")
        check("O the Orchestrator loads on its own origin",
              orch.url.startswith(f"http://apps-{KEYS['orchestrator']}-h.localhost:{port}/"), orch.url)
        check("O ...and window.parent.AppRunner still throws there (isolation kept)",
              orch.evaluate("() => window.__parentAppRunner") == "throws: SecurityError",
              orch.evaluate("() => window.__parentAppRunner"))
        n0 = len(created())
        # H (part): the Orchestrator asking for an app off its list.
        orch.evaluate("() => window.parent.postMessage({type:'cseries:open-app', app_id:'settings'}, '*')")
        page.wait_for_timeout(500)
        check("H the Orchestrator asking for Settings (not on its list) opens nothing", created()[n0:] == [],
              repr(created()[n0:]))
        check("H ...and the desktop says it refused", any("orchestrator may not ask cseries:open-app" in w for w in warns),
              repr(warns[-3:]))
        # H (part): a frame the Orchestrator embeds posts to the desktop.
        orch.evaluate("""() => { const f = document.createElement('iframe');
            f.src = '/system-apps/orchestrator/child.html'; document.body.appendChild(f); }""")
        page.wait_for_timeout(800)
        check("H a frame the Orchestrator embeds (same origin) cannot open Keys", created()[n0:] == [],
              repr(created()[n0:]))
        # The Connect Claude Code lane.
        orch.evaluate("() => window.__picker._openApp('circles', window.__picker._connectSeed('claude-code', 'Claude Code'))")
        circles = frame_of("circles")
        circles.wait_for_function("() => window.__composed && window.__composed.length > 0", timeout=10000)
        composed = circles.evaluate("() => window.__composed")
        check("O Connect Claude Code opens Circles", "app-circles" in created(), repr(created()))
        check("O ...and the Connect seed reaches its composer, sent as the person's message",
              composed == [{"type": "cseries:compose", "mode": "send", "text": seed}], repr(composed))
        orch.evaluate("() => window.__picker._openKeys()")
        frame_of("keys")
        check("O Open Keys opens Keys", "app-keys" in created(), repr(created()))
        orch.evaluate("() => window.__picker._openAppView('network', 'logical')")
        net = frame_of("network")
        check("O Network opens on its view", "app-network" in created() and net.url.endswith("#view=logical"), net.url)

        # ── H: a foreign origin in the desktop, a community app on its own
        # origin, and the Orchestrator's own frame navigated to a foreign page
        # (same window, another origin). The windows they ask for are closed
        # first, so an ask that got through would CREATE one.
        page.evaluate("() => { ['app-keys', 'app-circles', 'app-network'].forEach((id) => WindowManager.close(id)); }")
        n1 = len(created())
        page.evaluate(f"""() => {{ const f = document.createElement('iframe');
            f.src = 'http://evil.localhost:{port}/hostile.html'; document.body.appendChild(f); }}""")
        page.wait_for_timeout(800)
        page.evaluate("() => AppRunner.launch('evil')")
        frame_of("evil")
        page.wait_for_timeout(800)
        after = [c for c in created()[n1:] if c != "app-evil"]
        check("H a foreign-origin frame and a community app on its own origin open nothing", after == [], repr(after))
        page.evaluate(f"""() => {{ document.querySelector('.app-frame-host[data-app-id="orchestrator"] iframe').src =
            'http://evil.localhost:{port}/hostile.html'; }}""")
        page.wait_for_timeout(1500)
        after = [c for c in created()[n1:] if c != "app-evil"]
        check("H the Orchestrator's window, navigated to a foreign origin, opens nothing", after == [], repr(after))

        # ── S: Settings' Desktop tab ──
        page.evaluate("() => AppRunner.launch('settings')")
        st = frame_of("settings")
        st.wait_for_selector("[data-view=desktop]")
        st.click("[data-view=desktop]")
        st.wait_for_selector("#desktop-app-list .desktop-row", timeout=10000)
        txt = st.inner_text("#desktop-app-list")
        check("S the Desktop tab lists the apps (no 'Shell not reachable')",
              "Shell not reachable" not in txt and "Oracle" in txt, txt[:200])
        dock = page.evaluate("() => DesktopLayout.dockPinned()")
        check("S ...with the desktop's own dock state (Settings in the default dock reads In Dock)",
              "settings" in dock and "IN DOCK" in st.inner_text('.desktop-row[data-app-id="settings"]').upper(),
              st.inner_text('.desktop-row[data-app-id="settings"]'))
        st.click('.desktop-row[data-app-id="oracle"] [data-kind=dock]')
        st.wait_for_function("""() => { const b = document.querySelector('.desktop-row[data-app-id="oracle"] [data-kind=dock]');
            return b && b.textContent.toUpperCase().includes('IN DOCK'); }""", timeout=10000)
        check("S Add to Dock pins Oracle in the desktop's DesktopLayout",
              "oracle" in page.evaluate("() => DesktopLayout.dockPinned()"),
              repr(page.evaluate("() => DesktopLayout.dockPinned()")))

        # ── A: the App Store's Open ──
        page.evaluate("() => AppRunner.launch('app-store')")
        store = frame_of("app-store")
        n2 = len(created())
        btn = None
        for _ in range(50):
            loc = store.locator('[data-app-open="oracle"]')
            if loc.count():
                btn = loc.first
                break
            card = store.locator('[data-app-detail="oracle"]')
            if card.count():
                card.first.click()
            store.wait_for_timeout(200)
        check("A the App Store shows Open for the installed app", btn is not None)
        if btn is not None:
            btn.click()
            frame_of("oracle")
            check("A Open opens the app", "app-oracle" in created()[n2:], repr(created()[n2:]))
        # ── T: the token race. The REAL Orchestrator page framed in the desktop,
        # on its own origin, with the real SDK and frame bootstrap; the
        # frame-token answer is held 2 s and every /api/inference/* read
        # without a token is refused as the kernel refuses it.
        STATE.update(token_delay_s=2.0, gate=True, no_token=0)
        tp = ctx.new_page()
        tp.goto(base + "/")
        tp.wait_for_function("() => window.AppRunner")
        tp.evaluate(f"""() => {{ const f = document.createElement('iframe');
            f.src = 'http://apps-{KEYS['orchestrator']}-h.localhost:{port}/system-apps/orchestrator/real/';
            f.style.cssText = 'width:1200px;height:800px'; document.body.appendChild(f); }}""")
        of = None
        for _ in range(100):
            of = next((f for f in tp.frames if "/system-apps/orchestrator/real/" in f.url), None)
            if of: break
            tp.wait_for_timeout(100)
        drawn = True
        try:
            of.wait_for_selector(".mp-prov-card", timeout=15000)
        except Exception:
            drawn = False
        check("T a frame token that comes back 2 s late: the Orchestrator still draws its providers",
              drawn, of.evaluate("() => document.body.innerText.slice(0, 200)") if of else "no frame")
        check("T ...and no read left without its token", STATE["no_token"] == 0, f"{STATE['no_token']} refused")
        tp.close()
        STATE.update(token_delay_s=0.0, gate=False)

        # ── K: the REAL Orchestrator page re-reads when a key is added elsewhere ──
        kp = ctx.new_page()
        kp.goto(f"http://apps-{KEYS['orchestrator']}-h.localhost:{port}/system-apps/orchestrator/real/")
        kp.wait_for_selector(".mp-prov-card", timeout=15000)
        card = lambda: " ".join(kp.evaluate("""() => Array.from(document.querySelectorAll('.mp-prov-card'))
            .map(c => c.innerText.replace(/\\s+/g, ' ')).filter(t => /anthropic/i.test(t))""")).upper()
        check("K before: Anthropic says add key", "ADD KEY" in card(), card())
        STATE["anthropic_key"] = True          # the person adds it in Keys
        kp.evaluate("() => window.dispatchEvent(new Event('focus'))")
        try:
            kp.wait_for_function("""() => Array.from(document.querySelectorAll('.mp-prov-card'))
                .some(c => /anthropic/i.test(c.innerText) && /key in place/i.test(c.innerText))""", timeout=10000)
        except Exception:
            pass
        check("K after the window comes back into focus: Anthropic says key in place", "KEY IN PLACE" in card(), card())
        b.close()
    srv.shutdown()
    if FAILS:
        print(f"[isolated-browser] {len(FAILS)} FAILED")
        return 1
    print("[isolated-browser] all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
