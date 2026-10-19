#!/usr/bin/env python3
"""A store app's CSP wall, measured in Chromium: wasm runs, workers are walled.

A community (store) app is served by proxy._serve_static with proxy._app_csp.
Two defects found 2026-10-10, both measured here in headless Chromium:

  WASM. script-src had no 'wasm-unsafe-eval', and Chromium refuses to compile
  WebAssembly without it: PhotoCraft's official web build (a Rust app
  compiled to a 25 MB wasm) hung on "Loading PhotoCraft..." as a store copy,
  while the same files ran as the Host's own custom copy, which is served
  with no CSP at all.

  WORKERS. Only the HTML page carried the policy. A worker runs under the CSP
  of its own script's response, not the page's, so a store app's
  `new Worker('w.js')` fetched any host past manifest.network while the page
  itself was refused (found by review; it predates the wasm change).

What this checks, against loopback servers on free ports:

  A. the policy, from the real _app_csp, both forms (default with inline
     script, strict-inline pinned to hashes): a wasm module fetched as
     application/wasm compiles (WebAssembly.compileStreaming, the way
     wasm-bindgen loads one); JS eval stays refused; the same policy with
     'wasm-unsafe-eval' removed is refused, so the probe sees the defect.
  B. the serving path, through the real route (proxy.serve_app, with the DB
     row and the pod wake stubbed) over a community app's install dir: the
     page and its worker are both refused a cross-origin fetch to an
     undeclared server (which counts the hits it gets), the worker still
     reaches its own origin (so the refusal is the wall, not a dead worker),
     and the worker may compile wasm.
  C. a store app with its own pod (runtime process + image): every path with
     no file on disk is the pod's to answer, headers included. A worker script
     and a page the pod serves, one with no policy and one with its own loose
     policy, are refused the undeclared server too.
  D. a store app may not register a service worker: one answers the app's
     requests with responses built in the browser, which carry no policy, so
     the worker or page it answers ran past the wall. A first-party app's
     registration still succeeds, so the refusal is the store rule.

No real pod, no DB, no network beyond loopback. Needs Playwright and Chromium.

Run: APP_ROOT=<repo> python3 scripts/test_app_csp_chromium.py
"""
from __future__ import annotations

import asyncio
import http.server
import os
import socketserver
import sys
import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

_ROOT = Path(os.environ.get("APP_ROOT") or Path(__file__).resolve().parents[1])
sys.path.insert(0, str(_ROOT))

from fastapi import HTTPException  # noqa: E402
from starlette.requests import Request  # noqa: E402
from starlette.responses import FileResponse, StreamingResponse  # noqa: E402

from server.applications import harness_backend, proxy  # noqa: E402
from server.applications.proxy import _app_csp  # noqa: E402

_FAILURES: list[str] = []


def check(label: str, cond: bool, extra: str = "") -> None:
    print(("PASS " if cond else "FAIL ") + label + ("" if cond else f"  [{extra}]"))
    if not cond:
        _FAILURES.append(label)


# The smallest valid module: magic + version 1, no sections.
_WASM = b"\x00asm\x01\x00\x00\x00"

_PROBE_JS = """
(async () => {
  let wasm, evalr;
  try {
    const m = await WebAssembly.compileStreaming(fetch('/m.wasm'));
    wasm = (m instanceof WebAssembly.Module) ? 'compiled' : 'not a module';
  } catch (e) { wasm = 'refused: ' + e.name; }
  try { eval('1'); evalr = 'ran'; } catch (e) { evalr = 'refused'; }
  document.title = 'wasm=' + wasm + ';eval=' + evalr;
})();
"""

_POLICIES: dict[str, str] = {}
_SINK_HITS: list[str] = []
_PROVENANCE: dict[str, str] = {}
_POD: dict[str, int] = {}

# The app ids, one install dir each under the stubbed INSTALL_ROOT.
_DEFAULT, _STRICT, _PODDED, _OWN = "csp-probe", "csp-probe-strict", "csp-probe-pod", "csp-probe-own"


class _Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True


class _Sink(http.server.BaseHTTPRequestHandler):
    """An undeclared host: anything that reaches it went past the wall."""

    def do_GET(self):
        _SINK_HITS.append(self.path)
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *a):
        pass


def _worker_js(sink: str) -> str:
    return (
        "(async () => {\n"
        "  const r = {};\n"
        f"  try {{ await fetch('{sink}/from-worker'); r.out = 'reached'; }}"
        " catch (e) { r.out = 'refused'; }\n"
        "  try { const x = await fetch('m.wasm'); r.own = 'ok ' + x.status; }"
        " catch (e) { r.own = 'refused'; }\n"
        "  try { await WebAssembly.compileStreaming(fetch('m.wasm')); r.wasm = 'compiled'; }"
        " catch (e) { r.wasm = 'refused: ' + e.name; }\n"
        "  postMessage('out=' + r.out + ';own=' + r.own + ';wasm=' + r.wasm);\n"
        "})();\n")


def _install(root: Path, app_id: str, manifest: str, sink: str, worker: str = "w.js") -> Path:
    """An app's install dir, as the installer leaves it."""
    d = root / app_id
    d.mkdir()
    (d / "app.yaml").write_text(f"id: {app_id}\n" + manifest, encoding="utf-8")
    (d / "index.html").write_text(
        "<!doctype html><title>pending</title><script src=\"page.js\"></script>",
        encoding="utf-8")
    (d / "page.js").write_text(
        f"fetch('{sink}/from-page').then(() => {{ window.pageFetch = 'reached'; }},"
        " () => { window.pageFetch = 'refused'; });\n"
        f"const w = new Worker('{worker}');\n"
        "w.onmessage = (e) => { document.title = e.data; };\n"
        "w.onerror = (e) => { document.title = 'worker error ' + e.message; };\n",
        encoding="utf-8")
    (d / "w.js").write_text(_worker_js(sink), encoding="utf-8")
    (d / "m.wasm").write_bytes(_WASM)
    # D: a page that registers sw.js. The worker the service worker builds
    # (no policy on it) reports whether it reached the undeclared server.
    (d / "reg.html").write_text(
        "<!doctype html><title>pending</title><script src=\"reg.js\"></script>",
        encoding="utf-8")
    (d / "reg.js").write_text(
        "navigator.serviceWorker.register('sw.js').then(async () => {\n"
        "  await navigator.serviceWorker.ready;\n"
        "  if (!navigator.serviceWorker.controller) await new Promise((ok) =>\n"
        "    navigator.serviceWorker.addEventListener('controllerchange', ok));\n"
        "  const w = new Worker('sw-made.js');\n"
        "  w.onmessage = (e) => { document.title = 'registered;sw-made=' + e.data; };\n"
        "  w.onerror = () => { document.title = 'registered;sw-made=error'; };\n"
        "}, (e) => { document.title = 'register refused ' + e.name; });\n",
        encoding="utf-8")
    (d / "sw.js").write_text(
        "self.addEventListener('install', () => self.skipWaiting());\n"
        "self.addEventListener('activate', (e) => e.waitUntil(clients.claim()));\n"
        "self.addEventListener('fetch', (e) => {\n"
        "  if (new URL(e.request.url).pathname.endsWith('/sw-made.js')) e.respondWith(\n"
        f"    new Response(\"fetch('{sink}/from-sw-made').then(() => postMessage('reached'),"
        " () => postMessage('refused'))\",\n"
        "      { headers: { 'Content-Type': 'text/javascript' } }));\n"
        "});\n", encoding="utf-8")
    return d


class _Pod(http.server.BaseHTTPRequestHandler):
    """The store app's own pod: author code, which sets its own headers."""

    sink = ""

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/pod-w.js":
            self._send({"Content-Type": "text/javascript"}, _worker_js(self.sink).encode())
        elif path == "/pod-w-loose.js":
            self._send({"Content-Type": "text/javascript",
                        "Content-Security-Policy": "connect-src *"},
                       _worker_js(self.sink).encode())
        elif path == "/pod-page.html":
            self._send({"Content-Type": "text/html; charset=utf-8",
                        "Content-Security-Policy-Report-Only": "default-src 'none'",
                        "NEL": '{"report_to":"pod","max_age":86400}',
                        "Report-To": ('{"group":"pod","max_age":86400,"endpoints":'
                                      f'[{{"url":"{self.sink}/report"}}]}}'),
                        "Reporting-Endpoints": f'pod="{self.sink}/report"'},
                       ("<!doctype html><title>pending</title><script>"
                        f"fetch('{self.sink}/from-pod-page').then("
                        "() => { document.title = 'out=reached'; },"
                        " () => { document.title = 'out=refused'; });</script>").encode())
        else:
            self._send({"Content-Type": "text/plain"}, b"no", 404)

    def _send(self, headers, body, code=200):
        self.send_response(code)
        for k, v in headers.items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


async def _get_row(_pool, app_id):
    if app_id not in _PROVENANCE:
        return None
    return SimpleNamespace(status="active", provenance=_PROVENANCE[app_id])


async def _awake(app_id):
    return f"127.0.0.1:{_POD['port']}" if app_id == _PODDED else None


def _stub_route_deps(root: Path) -> None:
    """The route's DB row and pod wake, stubbed; everything else is real."""
    proxy.INSTALL_ROOT = root
    proxy.brain_service = SimpleNamespace(get_pool=lambda: object())
    proxy.registry = SimpleNamespace(get=_get_row)
    harness_backend.ensure_awake_app = _awake
    harness_backend.invalidate_app_url = lambda app_id: None


async def _through_route(handler) -> tuple[int, dict, bytes]:
    """One browser request through the real proxy.serve_app."""
    u = urlparse(handler.path)
    _, _, app_id, rest = (u.path + ("" if u.path.count("/") > 2 else "/")).split("/", 3)
    scope = {
        "type": "http", "http_version": "1.1", "method": "GET", "scheme": "http",
        "path": u.path, "raw_path": u.path.encode(), "query_string": u.query.encode(),
        "headers": [(k.lower().encode("latin-1"), v.encode("latin-1"))
                    for k, v in handler.headers.items()],
        "server": handler.server.server_address, "client": handler.client_address,
        "root_path": "",
    }

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    try:
        resp = await proxy.serve_app(app_id, rest, Request(scope, receive))
    except HTTPException as e:
        return e.status_code, {"Content-Type": "text/plain"}, str(e.detail).encode()
    if isinstance(resp, StreamingResponse):
        body = b"".join([c async for c in resp.body_iterator])
    elif isinstance(resp, FileResponse):
        body = Path(resp.path).read_bytes()
    else:
        body = bytes(resp.body)
    return resp.status_code, dict(resp.headers.items()), body


_LAST_HEADERS: dict[str, dict] = {}


class _Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        u = urlparse(self.path)
        if u.path == "/m.wasm":
            self._send(200, {"Content-Type": "application/wasm"}, _WASM)
        elif u.path == "/probe.js":
            self._send(200, {"Content-Type": "text/javascript"}, _PROBE_JS.encode())
        elif u.path == "/page":
            q = parse_qs(u.query)
            inline = q.get("inline", ["1"])[0] == "1"
            script = (f"<script>{_PROBE_JS}</script>" if inline
                      else '<script src="/probe.js"></script>')
            body = f"<!doctype html><title>pending</title>{script}".encode()
            self._send(200, {"Content-Type": "text/html; charset=utf-8",
                             "Content-Security-Policy": _POLICIES[q["policy"][0]]}, body)
        elif u.path.startswith("/system-apps/"):
            code, headers, body = asyncio.run(_through_route(self))
            _LAST_HEADERS[u.path] = headers
            self._send(code, {k: v for k, v in headers.items()
                              if k.lower() != "content-length"}, body)
        else:
            self._send(404, {"Content-Type": "text/plain"}, b"no")

    def _send(self, code, headers, body):
        self.send_response(code)
        for k, v in headers.items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


def main() -> int:
    default = _app_csp(["api.example.com"])
    strict = _app_csp(["api.example.com"], script_hashes=[])
    check("the default form carries 'wasm-unsafe-eval'", "'wasm-unsafe-eval'" in default, default)
    check("the strict-inline form carries 'wasm-unsafe-eval'", "'wasm-unsafe-eval'" in strict, strict)
    for name, csp in (("default", default), ("strict", strict)):
        check(f"the {name} form never carries 'unsafe-eval'",
              "'unsafe-eval'" not in csp.replace("'wasm-unsafe-eval'", ""), csp)
    _POLICIES["default"] = default
    _POLICIES["strict"] = strict
    _POLICIES["without"] = default.replace(" 'wasm-unsafe-eval'", "")

    sink = _Server(("127.0.0.1", 0), _Sink)
    srv = _Server(("127.0.0.1", 0), _Handler)
    pod = _Server(("127.0.0.1", 0), _Pod)
    _POD["port"] = pod.server_address[1]
    _Pod.sink = f"http://127.0.0.1:{sink.server_address[1]}"
    for s in (sink, srv, pod):
        threading.Thread(target=s.serve_forever, daemon=True).start()
    try:
        with tempfile.TemporaryDirectory(prefix="csp-probe-") as tmp:
            root = Path(tmp)
            net = "network:\n  - api.example.com\n"
            store = "provenance: community\n" + net
            _install(root, _DEFAULT, store, _Pod.sink)
            _install(root, _STRICT, store + "csp_strict_inline: true\n", _Pod.sink)
            _install(root, _PODDED, store + "runtime:\n  kind: process\n  image: python\n",
                     _Pod.sink, worker="pod-w.js")
            _install(root, _OWN, "provenance: standard\n" + net, _Pod.sink)
            for app_id in (_DEFAULT, _STRICT, _PODDED):
                _PROVENANCE[app_id] = "community"
            _PROVENANCE[_OWN] = "standard"
            _stub_route_deps(root)
            _browse(srv.server_address[1])
    finally:
        for s in (srv, sink, pod):
            s.shutdown()

    if _FAILURES:
        print(f"\n{len(_FAILURES)} failed")
        return 1
    print("\nall passed")
    return 0


def _settle(read, timeout_s: float = 10.0):
    """Poll from here, never with wait_for_function: its string predicate is
    re-run by eval inside the page, which the strict policy refuses (a race
    measured 2026-10-10)."""
    deadline = time.monotonic() + timeout_s
    while True:
        v = read()
        if v is not None or time.monotonic() > deadline:
            return v
        time.sleep(0.05)


def _browse(port: int) -> None:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            page = browser.new_page()

            def run(url: str) -> str:
                page.goto(f"http://127.0.0.1:{port}{url}")
                got = _settle(lambda: None if page.title() == "pending" else page.title())
                return got if got is not None else "timed out"

            # A. the policy
            got = run("/page?policy=default&inline=1")
            check("default form: a wasm module compiles", "wasm=compiled" in got, got)
            check("default form: JS eval is still refused", "eval=refused" in got, got)
            got = run("/page?policy=strict&inline=0")
            check("strict-inline form: a wasm module compiles", "wasm=compiled" in got, got)
            check("strict-inline form: JS eval is still refused", "eval=refused" in got, got)
            got = run("/page?policy=without&inline=1")
            check("without 'wasm-unsafe-eval' the browser refuses to compile",
                  "wasm=refused" in got, got)

            # B. the serving path; C. the app's own pod answers its worker
            for name, app_id in (("default", _DEFAULT), ("strict", _STRICT),
                                 ("pod-served", _PODDED)):
                _SINK_HITS.clear()
                got = run(f"/system-apps/{app_id}/index.html")
                pf = _settle(lambda: page.evaluate("() => window.pageFetch ?? null"))
                check(f"{name} store app: the page is refused an undeclared host",
                      pf == "refused", str(pf))
                check(f"{name} store app: its worker is refused an undeclared host",
                      "out=refused" in got, got)
                check(f"{name} store app: its worker still reaches its own origin",
                      "own=ok 200" in got, got)
                check(f"{name} store app: its worker may compile wasm",
                      "wasm=compiled" in got, got)
                check(f"{name} store app: nothing reached the undeclared server",
                      not _SINK_HITS, str(_SINK_HITS))

            # C. the pod's own loose policy, and a page the pod serves
            _SINK_HITS.clear()
            got = page.evaluate(
                "() => new Promise((ok) => { const w = new Worker('pod-w-loose.js');"
                " w.onmessage = (e) => ok(e.data); w.onerror = () => ok('worker error'); })")
            check("a pod worker sent with connect-src * is still refused an undeclared host",
                  "out=refused" in got and "own=ok 200" in got, got)
            loose = _LAST_HEADERS.get(f"/system-apps/{_PODDED}/pod-w-loose.js", {})
            check("the pod's own policy is replaced by the store wall",
                  loose.get("content-security-policy") == _POLICIES["default"], str(loose))
            got = run(f"/system-apps/{_PODDED}/pod-page.html")
            check("a page the pod serves is refused an undeclared host",
                  got == "out=refused", got)
            served = _LAST_HEADERS.get(f"/system-apps/{_PODDED}/pod-page.html", {})
            check("the pod's report-only policy is dropped",
                  "content-security-policy-report-only" not in served, str(served))
            # The browser sends NEL and Reporting API reports to the address
            # the response names, past connect-src (HTTPS only, so checked on
            # the headers rather than by a report reaching the sink here).
            check("the pod's reporting endpoints (NEL, Report-To, Reporting-Endpoints) are dropped",
                  not {"nel", "report-to", "reporting-endpoints"} & set(served), str(served))
            check("pod lane: nothing reached the undeclared server",
                  not _SINK_HITS, str(_SINK_HITS))

            # D. service workers
            for name, app_id in (("store", _DEFAULT), ("pod-backed store", _PODDED)):
                _SINK_HITS.clear()
                got = run(f"/system-apps/{app_id}/reg.html")
                check(f"a {name} app may not register a service worker",
                      got.startswith("register refused"), got)
                check(f"{name} app: nothing reached the undeclared server",
                      not _SINK_HITS, str(_SINK_HITS))
            got = run(f"/system-apps/{_OWN}/reg.html")
            check("a first-party app still registers its service worker",
                  got.startswith("registered;"), got)
        finally:
            browser.close()


if __name__ == "__main__":
    sys.exit(main())
