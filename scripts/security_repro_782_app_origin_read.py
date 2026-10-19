#!/usr/bin/env python3
"""Security regression: one isolated app must not read another.

THE FINDING. Every isolated app (Settings AND every community app) used to be
framed by the desktop from ONE origin per Host — 'apps-' + location.hostname —
with a sandbox that carries both `allow-scripts` and `allow-same-origin`. Two
frames on one real origin are same-origin to each other: a script in one
reaches the other through the desktop's frame list (`window.top[i]`, allowed
cross-origin) and reads its DOM and globals. A co-open community app could read
the Settings frame's recovery-key input (#bk-restore-reckey, the key the
platform is built never to hold) and `CSeriesApp.token`.

THE FIX. Each isolated
app has its own origin, apps-<key>-<handle>; that origin serves only its own
app's files (server/middleware/app_origin_guard.py) and may be framed only by
its Host's desktop (app_origin.response_csp); the window token is minted on the
app's own origin and handed to the SDK there (dashboard/base/js/app-frame-init.js).

THIS SCRIPT IS THE GUARD, and it runs THE REAL CODE in real Chromium:
  - the desktop page loads the repo's dashboard/base/js/app-runner.js and asks
    ITS frameOrigin() where each app goes, with the key computed by the
    kernel's own app_origin.origin_key();
  - the frames carry the exact sandbox string app-runner.js sets;
  - each app page is the kernel's injection pipeline's output — the repo's
    app-sdk.js plus proxy._inject_frame_init's app-frame-init.js — so the
    Settings frame really receives its token the production way;
  - every response passes through the real AppOriginGuard and the real
    app_origin.response_csp (what server/main.py's security_headers applies).
Hosts are the real zone names over https, resolved to a local TLS listener
(Chromium --host-resolver-rules), so frame-ancestors is checked exactly as in
production.

Each of these must be BLOCKED (no recovery key, no token, no same-origin read):
  1. a community app (national-weather) reading Settings;
  2. two first-party apps (Settings and Backups) reading each other;
  3. two community apps reading each other;
  4. a community page on Settings' origin: the community app frames
     Settings' origin with ITS OWN page and with Settings' real page
     (frame-ancestors must refuse an app as a framer, so neither loads);
  5. the same, framed by a page on the DESKTOP's own origin (a Host-built app
     is one), which frame-ancestors admits: the desktop frames Settings' origin
     with the community app's page, and the guard must not serve it;
  6. a community page loaded on the DESKTOP's origin itself
     (https://<handle><zone>/system-apps/<community>/), where it would run
     same-origin with the live desktop: the kernel must not serve it there
     (app_origin.app_files_here, asked by proxy.serve_app).
Two controls keep the harness honest: two DIFFERENT plain origins must block,
and two frames on ONE plain origin MUST leak — a harness that cannot see a
leak would pass anything. The Settings frame must also hold its real token,
or "the attacker read no token" would prove nothing.

On a tree without per-app origins (app_origin missing, or app-runner.js with no
frameOrigin) it runs the shared-origin case the shell would build, shows the
read, and FAILS.

Measured (Chromium 151): exit 1 on a tree with no per-app
origins (the shared control reads the key and the token), exit 0 with them.
Mutations, each applied to a copy of the tree and this script run (red = exit 1):
  response_csp leaves frame-ancestors zone-wide        red (scenario 4)
  guard serves any path on an app origin               red (scenario 5)
  guard drops the /system-apps/<id> key comparison     red (scenario 5)
  app-runner frameOrigin builds one shared origin      red (every scenario)
  app-frame-init.js never hands the SDK its token      red (Settings holds no token)
  app_files_here serves an isolated app on the desktop red (scenario 6; also exit 1
                                                       before the fix)
The fixture answers /system-apps/ with app_files_here itself, so a serve_app
that ignored that decision is caught by scripts/test_app_origin.py, not here.

Chromium dependency -> listed in scripts/gates-infra-only.txt, run on demand:
    python3 scripts/security_repro_782_app_origin_read.py
"""
from __future__ import annotations

import asyncio
import datetime
import ipaddress
import json
import os
import re
import socket
import sys
import tempfile
import threading
import time
from pathlib import Path
from urllib.parse import parse_qs

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

JS = REPO / "dashboard" / "base" / "js"
RUNNER_SRC = (JS / "app-runner.js").read_text()
m = re.search(r"iframe\.setAttribute\('sandbox', '([^']+)'\)", RUNNER_SRC)
if not m:
    print("HARNESS ERROR: cannot find the sandbox string in app-runner.js")
    sys.exit(2)
SANDBOX = m.group(1)

HANDLE = "0123456789abcdef"
DESKTOP = f"{HANDLE}.host.example.test"
RECKEY = "AGE-SECRET-KEY-1QQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQ0000"

try:
    from server.applications import app_origin as ao
    from server.applications.proxy import _inject_frame_init
    from server.middleware.app_origin_guard import AppOriginGuard
    PER_APP = "frameOrigin" in RUNNER_SRC
except ImportError as e:   # a tree before per-app origins
    ao = None
    PER_APP = False
    IMPORT_ERROR = str(e)


# The fixture's apps and their provenance (Settings and Backups are on the
# kernel's SDK roster; the other two are community apps).
PROVENANCE = {"settings": "native", "backups": "standard",
              "national-weather": "community", "community-two": "community"}


def token_for(app_id: str, host: str) -> str:
    return f"WINDOW-TOKEN-{app_id}-ON-{host}"


# ── the app pages ─────────────────────────────────────────────────────────

SETTINGS_BODY = f"""<input id="bk-restore-reckey" type="password" value="{RECKEY}">
<script>
(async function () {{
  try {{
    await CSeriesApp.ready;
    console.log('SETTINGS_READY ' + JSON.stringify({{token: CSeriesApp.token, origin: location.origin}}));
  }} catch (e) {{
    console.log('SETTINGS_READY ' + JSON.stringify({{error: String(e), origin: location.origin}}));
  }}
}})();
</script>"""

ATTACKER_BODY = """<pre id=out>run</pre>
<script>
var q = new URLSearchParams(location.search);
var nest = q.get('nest');           // Settings' origin, for scenario 4
var nestedLoaded = false;
window.addEventListener('message', function (ev) {
  if (ev.data && ev.data.type === 'cseries:app-ready' && ev.source !== window) nestedLoaded = true;
});
if (nest) {
  var mine = location.pathname.split('/')[2];
  ['/system-apps/' + mine + '/evil.html', '/system-apps/settings/'].forEach(function (p) {
    var f = document.createElement('iframe');
    f.setAttribute('sandbox', 'allow-scripts allow-same-origin');
    f.src = nest + p;
    document.body.appendChild(f);
  });
}
function read() {
  var r = {token: null, recoveryKey: null, sameOriginReadOK: false, nestedLoaded: false};
  var top = window.top;
  for (var i = 0; i < top.length; i++) {
    var w = top[i];
    if (w === window) continue;
    try {
      var doc = w.document;                       // throws unless same-origin
      r.sameOriginReadOK = true;
      var inp = doc.getElementById('bk-restore-reckey');
      if (inp) r.recoveryKey = inp.value;
      if (w.CSeriesApp && w.CSeriesApp.token) r.token = w.CSeriesApp.token;
    } catch (e) { /* cross-origin sibling: blocked, as intended */ }
  }
  r.nestedLoaded = nestedLoaded;
  try { r.shellRead = !!window.top.document.title; } catch (e) { r.shellRead = false; }
  return r;
}
setTimeout(function () {
  var r = read();
  document.getElementById('out').textContent = JSON.stringify(r);
  r.origin = location.origin;
  console.log('REPRO_RESULT ' + location.pathname.split('/')[2] + ' ' + JSON.stringify(r));
}, 2500);
</script>"""

EVIL_BODY = """<script>
console.log('EVIL_RAN ' + location.origin);
var hit = null;
for (var i = 0; i < window.top.length; i++) {
  try { var inp = window.top[i].document.getElementById('bk-restore-reckey');
        if (inp) hit = inp.value; } catch (e) {}
}
console.log('EVIL_READ ' + JSON.stringify(hit));
</script>"""


def app_page(app_id: str) -> str:
    body = SETTINGS_BODY if app_id == "settings" else ATTACKER_BODY
    html = (f'<!doctype html><html><head><meta charset="utf-8"><title>{app_id}</title></head>'
            f"<body>{body}</body></html>")
    # The kernel's pipeline for an isolated app: the SDK first in <head>
    # (proxy._inject_app_sdk, minus the SRI attribute the signed store adds),
    # then the frame bootstrap right after it (proxy._inject_frame_init).
    html = html.replace("<head>", '<head><script src="/js/app-sdk.js"></script>', 1)
    if PER_APP:
        html = _inject_frame_init(html, app_id)
    return html


def desktop_page(manifests: dict, plan: list[dict]) -> str:
    """The desktop: loads the REAL app-runner.js and frames each app where its
    frameOrigin() says (or, for the controls, at a fixed plain origin)."""
    return f"""<!doctype html><meta charset=utf-8><title>desktop</title><body>
<script src="/js/app-runner.js"></script>
<script>
var MANIFESTS = {json.dumps(manifests)};
var PLAN = {json.dumps(plan)};
PLAN.forEach(function (p) {{
  var origin = p.origin || AppRunner.frameOrigin(MANIFESTS[p.id], location);
  console.log('FRAME ' + p.id + ' ' + origin);
  if (!origin) return;
  var f = document.createElement('iframe');
  f.setAttribute('sandbox', {SANDBOX!r});
  f.src = origin + '/system-apps/' + encodeURIComponent(p.id) + '/' + (p.query || '');
  if (p.handoff) {{
    // The controls hand the token over the way the desktop did before per-app origins
    // (app-runner.js sendInit: postMessage to the frame's origin), so they
    // behave the same on a tree with or without per-app origins.
    f.addEventListener('load', function () {{
      f.contentWindow.postMessage({{type: 'cseries:app-init', token: p.handoff, app_id: p.id}}, origin);
    }});
  }}
  document.body.appendChild(f);
}});
</script></body>"""


# ── the server: real guard + real headers around a fixture app ────────────

DESKTOP_PAGES: dict[str, str] = {}


async def fixture_app(scope, receive, send):
    if scope["type"] != "http":
        return
    host = ""
    for k, v in scope["headers"]:
        if k == b"host":
            host = v.decode().split(":")[0].lower()
    path = scope["path"]
    status, ctype, body = 404, "text/plain", b"not found"
    if path in ("/js/app-runner.js", "/js/app-sdk.js", "/js/app-frame-init.js"):
        status, ctype, body = 200, "application/javascript", (JS / path.split("/")[-1]).read_bytes()
    elif path == "/" and host == DESKTOP:
        page = DESKTOP_PAGES.get(parse_qs(scope.get("query_string", b"").decode()).get("s", [""])[0])
        if page:
            status, ctype, body = 200, "text/html", page.encode()
    elif path.startswith("/system-apps/"):
        parts = path.split("/")
        app_id, rest = parts[2], "/".join(parts[3:])
        # Which origins serve this app's files: the kernel's one decision
        # (app_origin.app_files_here, which proxy.serve_app asks), with the
        # real is_isolated. A tree without it served every app's files on
        # every origin, and so does this fixture then.
        here = "serve"
        if ao is not None and hasattr(ao, "app_files_here"):
            from server.applications.proxy import is_isolated
            o = ao.classify(ao.browser_host(scope["headers"]))
            here = ao.app_files_here(app_id, is_isolated(app_id, PROVENANCE.get(app_id, "standard")),
                                     o, rest)
        if here != "serve":
            pass    # 404 below: none of the fixture's apps ships an image
        elif rest == "evil.html":
            status, ctype, body = 200, "text/html", EVIL_BODY.encode()
        elif rest in ("", "index.html"):
            status, ctype, body = 200, "text/html", app_page(app_id).encode()
    elif re.fullmatch(r"/api/applications/[a-z0-9-]+/frame-token", path):
        # The origin half of routes/applications.py frame_token (its own unit
        # test drives the real route): a token only on the app's own origin.
        app_id = path.split("/")[3]
        o = ao.classify(host) if ao else None
        if o and o.kind == ao.APP and o.key == ao.origin_key(app_id):
            status, ctype, body = 200, "application/json", json.dumps(
                {"token": token_for(app_id, host)}).encode()
        else:
            status, body = 403, b"not this app's address"
    await send({"type": "http.response.start", "status": status,
                "headers": [(b"content-type", ctype.encode()), (b"cache-control", b"no-store")]})
    await send({"type": "http.response.body", "body": body})


def security_headers(app):
    """What server/main.py security_headers does: on an app origin, the CSP from
    app_origin.response_csp; the desktop gets X-Frame-Options. The control
    hosts (*.control.test) get nothing, so they can be framed."""
    async def wrapped(scope, receive, send):
        if scope["type"] != "http":
            return await app(scope, receive, send)
        o = ao.classify(ao.browser_host(scope["headers"])) if ao else None

        async def send2(msg):
            if msg["type"] == "http.response.start" and o is not None:
                hdrs = [(k, v) for k, v in msg["headers"] if k != b"content-security-policy"]
                existing = dict(msg["headers"]).get(b"content-security-policy", b"").decode()
                if o.kind == ao.APP:
                    hdrs.append((b"content-security-policy",
                                 ao.response_csp(o, scope["path"], existing).encode()))
                elif o.kind == ao.DESKTOP:
                    hdrs.append((b"x-frame-options", b"SAMEORIGIN"))
                msg = {**msg, "headers": hdrs}
            await send(msg)
        return await app(scope, receive, send2)
    return wrapped


def make_cert(tmp: Path):
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "repro-782")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(minutes=5))
            .not_valid_after(now + datetime.timedelta(hours=1))
            .add_extension(x509.SubjectAlternativeName([
                x509.DNSName("*.host.example.test"), x509.DNSName("*.control.test"),
                x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]), critical=False)
            .sign(key, hashes.SHA256()))
    (tmp / "cert.pem").write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    (tmp / "key.pem").write_bytes(key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption()))
    return str(tmp / "cert.pem"), str(tmp / "key.pem")


def serve(port: int, certfile: str, keyfile: str):
    # Loopback on purpose: the only client is the headless Chromium this
    # script launches on the same host, which reaches it through
    # --host-resolver-rules. Nothing off the box may reach a test fixture.
    import uvicorn
    from gate_loopback import IDLE_KEEP_ALIVE_S
    app = security_headers(fixture_app)
    if ao is not None:
        app = AppOriginGuard(app)
    cfg = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error",
                         ssl_certfile=certfile, ssl_keyfile=keyfile, lifespan="off",
                         timeout_keep_alive=IDLE_KEEP_ALIVE_S)
    server = uvicorn.Server(cfg)
    threading.Thread(target=server.run, daemon=True).start()
    for _ in range(100):
        if server.started:
            return server
        time.sleep(0.05)
    raise RuntimeError("fixture server did not start")


# ── the runs ──────────────────────────────────────────────────────────────

def run_desktop(browser, port, key: str) -> list[str]:
    out: list[str] = []
    ctx = browser.new_context(ignore_https_errors=True)
    page = ctx.new_page()
    page.on("console", lambda msg: out.append(msg.text))
    page.goto(f"https://{DESKTOP}/?s={key}", wait_until="load")
    page.wait_for_timeout(4000)
    ctx.close()
    return out


def results(lines: list[str]) -> dict:
    r = {}
    for line in lines:
        if line.startswith("REPRO_RESULT "):
            _, who, js = line.split(" ", 2)
            r[who] = json.loads(js)
    return r


def leaked(r: dict) -> bool:
    return bool(r.get("sameOriginReadOK") or r.get("token") or r.get("recoveryKey"))


def main() -> int:
    if PER_APP:
        manifests = {a: {"id": a, "isolated": True, "origin_key": ao.origin_key(a)}
                     for a in ("settings", "backups", "national-weather", "community-two")}
    else:
        manifests = {}
    settings_origin = (f"https://apps-{ao.origin_key('settings')}-{HANDLE}.host.example.test"
                       if PER_APP else None)

    scenarios = {}
    if PER_APP:
        scenarios = {
            "1 community app vs Settings": [{"id": "settings"}, {"id": "national-weather"}],
            "2 two first-party apps (Settings, Backups)": [{"id": "settings"}, {"id": "backups"}],
            "3 two community apps": [{"id": "national-weather"}, {"id": "community-two"}],
            "4 a community page on Settings' origin": [
                {"id": "settings"},
                {"id": "national-weather", "query": f"?nest={settings_origin}"}],
            "5 a desktop-origin page puts community code on Settings' origin": [
                {"id": "settings"},
                {"id": "national-weather", "origin": settings_origin}],
            "6 a community page loaded on the desktop's origin": [
                {"id": "settings"},
                {"id": "national-weather", "origin": f"https://{DESKTOP}"}],
        }
    controls = {
        "control: two DIFFERENT plain origins": [
            {"id": "settings", "origin": "https://a.control.test",
             "handoff": token_for("settings", "a.control.test")},
            {"id": "national-weather", "origin": "https://b.control.test"}],
        "control: ONE plain origin (what the desktop built before per-app origins)": [
            {"id": "settings", "origin": "https://shared.control.test",
             "handoff": token_for("settings", "shared.control.test")},
            {"id": "national-weather", "origin": "https://shared.control.test"}],
    }
    for i, (name, plan) in enumerate({**scenarios, **controls}.items()):
        DESKTOP_PAGES[str(i)] = desktop_page(manifests, plan)

    from playwright.sync_api import sync_playwright
    with tempfile.TemporaryDirectory() as tmp:
        certfile, keyfile = make_cert(Path(tmp))
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        server = serve(port, certfile, keyfile)
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True, args=[
                    "--no-sandbox",
                    f"--host-resolver-rules=MAP *.host.example.test 127.0.0.1:{port}, "
                    f"MAP *.control.test 127.0.0.1:{port}"])
                print(f"chromium {browser.version}")
                out = {name: run_desktop(browser, port, str(i))
                       for i, name in enumerate({**scenarios, **controls})}
                browser.close()
        finally:
            server.should_exit = True

    ok = True
    names = list({**scenarios, **controls})
    for name in names:
        lines = out[name]
        frames = [l for l in lines if l.startswith("FRAME ")]
        res = results(lines)
        print(f"\n== {name}")
        for f in frames:
            print("   " + f)
        for who, r in res.items():
            print(f"   {who} read: {json.dumps(r)}")
        for l in lines:
            if l.startswith(("SETTINGS_READY", "EVIL_")):
                print("   " + l)

    # Controls first: if they are wrong, nothing else can be concluded.
    distinct = results(out[names[-2]]).get("national-weather", {})
    shared = results(out[names[-1]]).get("national-weather", {})
    if not distinct or leaked(distinct):
        print("\nHARNESS ERROR: the distinct-origin control did not block (or never ran).")
        return 2
    if shared.get("recoveryKey") != RECKEY or not shared.get("token"):
        print("\nHARNESS ERROR: the shared-origin control did not leak — this harness "
              "cannot see a leak, so it proves nothing.")
        return 2

    if not PER_APP:
        why = ("server.applications.app_origin is missing (" + IMPORT_ERROR + ")"
               if ao is None else "app-runner.js has no frameOrigin()")
        print(f"\nFAIL  no per-app origins in this tree — {why}. Every isolated "
              "app is framed from apps-<handle>, the ONE origin the shared control "
              "above just read the recovery key and token across.")
        return 1

    for name in names[:-2]:
        lines = out[name]
        res = results(lines)
        if name.startswith("6 "):
            # Loaded at https://<handle><zone>/system-apps/<community>/ it would
            # run same-origin with the live desktop: the shell, Circles, the
            # Workbench, every same-origin app.
            ran = [r for r in res.values() if r.get("origin") == f"https://{DESKTOP}"]
            if ran:
                print(f"\nFAIL  {name}: community code RAN on the desktop's origin "
                      f"(read the desktop: {any(r.get('shellRead') for r in ran)}): {ran}")
                ok = False
            continue
        if name.startswith("5 "):
            ran = [r for r in res.values() if r.get("origin") == settings_origin]
            if ran or any(l.startswith("EVIL_RAN") for l in lines):
                print(f"\nFAIL  {name}: community code RAN on Settings' origin: {ran}")
                ok = False
            continue
        if not res:
            print(f"\nFAIL  {name}: no attacker frame reported (did it load?)")
            ok = False
            continue
        for who, r in res.items():
            if leaked(r):
                print(f"\nFAIL  {name}: {who} READ another app's frame: {json.dumps(r)}")
                ok = False
            if r.get("nestedLoaded"):
                print(f"\nFAIL  {name}: {who} framed Settings' origin (frame-ancestors not pinned)")
                ok = False
        if any(l.startswith("EVIL_RAN") for l in lines):
            print(f"\nFAIL  {name}: the community app's own page RAN on Settings' origin")
            ok = False
        frames = dict(l.split(" ", 2)[1:] for l in lines if l.startswith("FRAME "))
        if len(set(frames.values())) != len(frames) or "null" in frames.values():
            print(f"\nFAIL  {name}: the desktop did not give each app its own origin: {frames}")
            ok = False
        ready = [json.loads(l.split(" ", 1)[1]) for l in lines if l.startswith("SETTINGS_READY")]
        if any(p["id"] == "settings" for p in scenarios[name]):
            st_host = f"apps-{ao.origin_key('settings')}-{HANDLE}.host.example.test"
            if not ready or ready[0].get("token") != token_for("settings", st_host):
                print(f"\nFAIL  {name}: the Settings frame never got its own token on its own "
                      f"origin ({ready}) — a 'blocked' read would prove nothing")
                ok = False

    if ok:
        print("\nOK    no isolated app can read another's frame; Settings holds its token "
              "on its own origin; the desktop frames each app at its own origin.")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
