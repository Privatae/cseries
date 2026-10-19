#!/usr/bin/env python3
"""Gate: an app backend inside the Host container answers only its OWN window.

THE DEFECT (confirmed on production). serve_app forwarded any method
and any path under /system-apps/<id>/ to a live in-container process sidecar
with no token, app or origin check (_serve_process only asked whether the
sidecar was alive). Nothing else stood in the way:
  - the app-token middleware covers /api/ only (applications/middleware.py);
  - router traffic skips the Origin check (middleware/localhost_only.py);
  - isolated and community apps were framed on the one apps-<handle> origin
    (dashboard/base/js/app-runner.js) with CSP connect-src 'self'
    (proxy._app_csp), and the router sends apps-<handle> to the same pod.
    (Each isolated app now has its own origin and that origin serves
    only its own files — middleware/app_origin_guard.py — but same-origin apps
    still share the desktop's origin, so the token check here stays the wall.)
So another app's page could call /system-apps/claude-code/<any sidecar route>
while a Claude Code window was open: POST /sessions started an agent with a
shell in the Host container, POST /disconnect deleted the Claude credential,
POST /workspace/load?dir= emptied the projects root.

THE FIX (proxy._require_own_window). In front of every backend IN the Host
container (kind=process with no runtime.image; kind=external with no
runtime.harness; the loopback WebSocket), the caller must present THIS app's
window token in X-Application-Token, or a current kernel verify capability
minted for THIS app. Static UI files stay token-less.

What this pins (loopback fake sidecar, stub registry/pool/secret; no pod, no DB):
  1. every call path claude-code's real app.js makes to its sidecar is
     forwarded with the app's own token, and refused (403, never forwarded)
     with another app's token, no token, or a token signed under another
     kernel secret (a rotated secret is the only way a token goes stale:
     tokens carry no expiry, tokens.py "Lifetime");
  2. the dangerous routes (POST sessions, POST disconnect, workspace load and
     export) are refused to another app and to no token;
  3. the UI shell (index.html, app.js) still loads with no token and never
     touches the sidecar;
  4. the kernel's own page check (a verify cap for THIS app) passes; a cap
     for another app does not;
  5. the loopback kind=external branch and its WebSocket twin are gated the
     same way;
  6. unchanged lanes: a helper app still gets the helperFetch refusal, and an
     own-pod process app (runtime.image) is still forwarded without a token
     (it may serve its own pages to a frame navigation; see proxy.py).

Run: APP_ROOT=<repo> python3 scripts/test_proxy_inpod_apps_own_token.py
"""
import json
import os
import re
import shutil
import sys
import tempfile
import threading
import warnings
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from gate_loopback import ClientClosesFirst
from pathlib import Path

_ROOT = Path(os.environ.get("APP_ROOT") or Path(__file__).resolve().parents[1])
sys.path[:0] = [str(_ROOT), str(_ROOT / "brain")]

FAILED: list[str] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"[inpod-own-token] ok   {label}")
    else:
        FAILED.append(label)
        print(f"[inpod-own-token] FAIL {label}" + (f" — {detail}" if detail else ""))


# ── the fake sidecar: records what actually reached it ────────────────────
SEEN: list[tuple[str, str, str]] = []


class _Sidecar(ClientClosesFirst, BaseHTTPRequestHandler):
    # ClientClosesFirst: a server that closes first leaves a TIME_WAIT on its
    # port per request, which CI's firewall refuses a later request onto
    # (scripts/gate_loopback.py).
    def _any(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n:
            self.rfile.read(n)
        SEEN.append((self.command, self.path, self.headers.get("X-Application-Token") or ""))
        body = json.dumps({"ok": True}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    do_GET = do_POST = do_DELETE = do_PUT = do_PATCH = _any

    def log_message(self, *a):
        pass


def ui_call_paths(app_js: str) -> list[tuple[str, str]]:
    """(method, path) for every sidecarUrl('…') call in the app's own UI."""
    out = []
    for m in re.finditer(r"sidecarUrl\('([^']+)'\)\s*(?:,\s*\{\s*method:\s*'([A-Z]+)')?", app_js):
        out.append(((m.group(2) or "GET"), m.group(1)))
    return sorted(set(out))


def main() -> int:
    warnings.filterwarnings("ignore")
    from fastapi import FastAPI
    from starlette.testclient import TestClient
    from starlette.websockets import WebSocketDisconnect

    from server.applications import proxy as P
    from server.applications import tokens
    from mastercode.runtime import app_verify

    sidecar = ThreadingHTTPServer(("127.0.0.1", 0), _Sidecar)
    threading.Thread(target=sidecar.serve_forever, daemon=True).start()
    sc_port = sidecar.server_address[1]

    tmp = Path(tempfile.mkdtemp(prefix="inpod-own-token-", dir=os.environ.get("TMPDIR") or "/tmp"))
    real = _ROOT / "system-apps/claude-code"
    cc = tmp / "claude-code"
    cc.mkdir()
    for f in ("app.yaml", "index.html", "app.js"):
        shutil.copy(real / f, cc / f)

    def app_dir(app_id: str, manifest: str) -> None:
        d = tmp / app_id
        d.mkdir()
        (d / "app.yaml").write_text(manifest)
        (d / "index.html").write_text(f"<html><body>{app_id}</body></html>")

    app_dir("sites", "id: sites\nprovenance: standard\n")
    app_dir("loopy", f"id: loopy\nprovenance: standard\nruntime:\n  kind: external\n  port: {sc_port}\n")
    app_dir("podapp", "id: podapp\nprovenance: community\nruntime:\n  kind: process\n"
                      "  image: python\n  command: [python3, main.py]\n")
    app_dir("helpy", "id: helpy\nprovenance: community\nruntime:\n  kind: helper\n")

    secret = b"s" * 32
    own, _ = tokens.mint(secret, app_id="claude-code", permissions=[])
    other, _ = tokens.mint(secret, app_id="sites", permissions=[])
    rotated, _ = tokens.mint(b"r" * 32, app_id="claude-code", permissions=[])
    loopy_own, _ = tokens.mint(secret, app_id="loopy", permissions=[])

    class _Installed:
        status = "active"
        provenance = "standard"     # serve_app asks app_origin.app_files_here

        def __init__(self, app_id):
            self.id = app_id

    async def _get(pool, app_id):
        return _Installed(app_id) if (tmp / app_id).is_dir() else None

    async def _secret(pool):
        return secret

    class _Sc:
        base_url = f"http://127.0.0.1:{sc_port}"

        def is_alive(self):
            return True

    class _Sup:
        def get(self, app_id):
            return _Sc()

    from server.applications import harness_backend

    async def _awake(app_id):
        return f"127.0.0.1:{sc_port}"

    saved = (P.INSTALL_ROOT, P.registry.get, P.brain_service.get_pool, tokens.get_secret,
             P.get_supervisor, harness_backend.ensure_awake_app)
    P.INSTALL_ROOT = tmp
    P.registry.get = _get
    P.brain_service.get_pool = lambda: object()
    tokens.get_secret = _secret
    P.get_supervisor = lambda: _Sup()
    harness_backend.ensure_awake_app = _awake

    api = FastAPI()
    api.include_router(P.router)
    client = TestClient(api)

    def call(method: str, app_id: str, path: str, token: str | None = None,
             cap: str | None = None, body: bytes | None = None):
        SEEN.clear()
        h = {}
        if token is not None:
            h["X-Application-Token"] = token
        if cap is not None:
            h["X-CSeries-Verify-Cap"] = cap
        r = client.request(method, f"/system-apps/{app_id}/{path}", headers=h, content=body)
        return r.status_code, list(SEEN)

    try:
        # ── 1. claude-code's own UI call paths ────────────────────────────
        paths = ui_call_paths((real / "app.js").read_text())
        check("claude-code's app.js calls its sidecar (the paths under test are real)",
              ("GET", "status") in paths, f"parsed {paths}")
        for method, path in paths:
            body = b'{"session_id":"s1","reason":"user_request"}' if method == "DELETE" else None
            code, seen = call(method, "claude-code", path, token=own, body=body)
            check(f"own window: {method} {path} -> forwarded with its token",
                  code == 200 and len(seen) == 1 and seen[0][2] == own, f"{code} {seen}")
            for who, tok in (("another app's token", other), ("no token", None),
                             ("a token from a rotated kernel secret", rotated)):
                code, seen = call(method, "claude-code", path, token=tok, body=body)
                check(f"{who}: {method} {path} -> 403, sidecar never reached",
                      code == 403 and not seen, f"{code} {seen}")

        # ── 2. the routes that made this a shell + a credential wipe ──────
        for method, path, body in (("POST", "sessions", b'{"prompt":"id"}'),
                                   ("POST", "sessions/continue", b'{}'),
                                   ("POST", "disconnect", None),
                                   ("POST", "workspace/load?dir=", b"x"),
                                   ("GET", "workspace/export", None),
                                   ("GET", "session/export", None)):
            for who, tok in (("another app", other), ("no token", None)):
                code, seen = call(method, "claude-code", path, token=tok, body=body)
                check(f"{who}: {method} {path} -> 403, sidecar never reached",
                      code == 403 and not seen, f"{code} {seen}")

        # ── 3. the UI shell still loads with no token ─────────────────────
        for f in ("index.html", "app.js", ""):
            code, seen = call("GET", "claude-code", f)
            check(f"static {f or '(entrypoint)'} loads token-less, sidecar untouched",
                  code == 200 and not seen, f"{code} {seen}")

        # ── 4. the kernel's own page check ────────────────────────────────
        cap = app_verify._mint_verify_cap("claude-code")
        code, seen = call("GET", "claude-code", "status", cap=cap)
        check("a current verify cap for THIS app is forwarded (check_page on Claude Code)",
              code == 200 and len(seen) == 1, f"{code} {seen}")
        other_cap = app_verify._mint_verify_cap("sites")
        code, seen = call("POST", "claude-code", "sessions", cap=other_cap)
        check("a verify cap minted for another app is refused",
              code == 403 and not seen, f"{code} {seen}")
        app_verify._forget_verify_cap("claude-code")
        app_verify._forget_verify_cap("sites")
        code, seen = call("GET", "claude-code", "status", cap=cap)
        check("a cap that is no longer current is refused",
              code == 403 and not seen, f"{code} {seen}")

        # ── 5. the loopback external branch + its WebSocket ───────────────
        code, seen = call("POST", "loopy", "anything", token=other)
        check("loopback external app: another app's token -> 403, port never dialled",
              code == 403 and not seen, f"{code} {seen}")
        code, seen = call("GET", "loopy", "anything")
        check("loopback external app: no token -> 403", code == 403 and not seen, f"{code} {seen}")
        code, seen = call("GET", "loopy", "anything", token=loopy_own)
        check("loopback external app: its own token -> forwarded",
              code == 200 and len(seen) == 1, f"{code} {seen}")
        def ws_close(headers: dict | None = None) -> tuple:
            """(close code, reason) of a WS to the loopback app. 1008 = refused
            by the gate before accept; 1011 = passed the gate, accepted, then
            the fake upstream (not a WS server) failed the upstream dial."""
            try:
                with client.websocket_connect("/system-apps/loopy/ws", headers=headers or {}) as ws:
                    ws.receive_text()
                    return ("open", "")
            except WebSocketDisconnect as e:
                return (e.code, e.reason or "")

        ws_code, _ = ws_close()
        check("loopback WebSocket with no token is closed 1008 before any upstream dial",
              ws_code == 1008, f"got {ws_code}")
        ws_code, _ = ws_close({"X-Application-Token": loopy_own})
        check("loopback WebSocket with its own token passes the gate "
              "(the fake upstream is not a WS server, so it then closes 1011)",
              ws_code == 1011, f"got {ws_code}")

        # The verify cap on the WebSocket lane. serve_app checks a presented
        # cap for HTTP before any branch, but serve_app_ws has no cap check of
        # its own, so for a WebSocket _require_own_window's cap branch is the
        # ONLY check. These are the checks that fail if it is inverted or
        # waved through (the HTTP cap checks in section 4 pass either way).
        loopy_cap = app_verify._mint_verify_cap("loopy")
        ws_code, why = ws_close({"X-CSeries-Verify-Cap": loopy_cap})
        check("loopback WebSocket with a current verify cap for THIS app passes the gate (1011)",
              ws_code == 1011, f"got {ws_code} {why!r}")
        sites_cap = app_verify._mint_verify_cap("sites")
        ws_code, why = ws_close({"X-CSeries-Verify-Cap": sites_cap})
        check("loopback WebSocket with a verify cap minted for ANOTHER app is refused 1008",
              ws_code == 1008 and "verify capability not current" in why, f"got {ws_code} {why!r}")
        ws_code, why = ws_close({"X-CSeries-Verify-Cap": "bogus-" + "x" * 40})
        check("loopback WebSocket with a bogus verify cap is refused 1008",
              ws_code == 1008 and "verify capability not current" in why, f"got {ws_code} {why!r}")
        app_verify._forget_verify_cap("loopy")
        app_verify._forget_verify_cap("sites")
        ws_code, why = ws_close({"X-CSeries-Verify-Cap": loopy_cap})
        check("loopback WebSocket with this app's cap after it is no longer current is refused 1008",
              ws_code == 1008 and "verify capability not current" in why, f"got {ws_code} {why!r}")

        # ── 6. lanes this change must leave alone ─────────────────────────
        code, seen = call("GET", "helpy", "helper/fetch/example.com/x")
        r = client.get("/system-apps/helpy/helper/fetch/example.com/x")
        check("helper lane: no token still refused with the helperFetch hint",
              code == 403 and not seen and "helperFetch" in r.text, f"{code} {r.text[:120]}")
        code, seen = call("GET", "podapp", "ui/")
        check("own-pod process app (runtime.image): still forwarded with no token",
              code == 200 and len(seen) == 1, f"{code} {seen}")
    finally:
        (P.INSTALL_ROOT, P.registry.get, P.brain_service.get_pool, tokens.get_secret,
         P.get_supervisor, harness_backend.ensure_awake_app) = saved
        sidecar.shutdown()
        shutil.rmtree(tmp, ignore_errors=True)

    print()
    if FAILED:
        print(f"[inpod-own-token] {len(FAILED)} FAILED: {FAILED}")
        return 1
    print("[inpod-own-token] all green")
    return 0


if __name__ == "__main__":
    sys.exit(main())
