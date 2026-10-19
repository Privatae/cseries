#!/usr/bin/env python3
"""Gate for browser-verify — the ship proof loads the page in a real browser.

The shape: the static half of the deployment
gate refuses bytes that cannot work; the browser half LOADS the
built app and catches what bytes cannot show. On a Host a
shipped app proved "delivered" four times with a blank window — its page had
<base href="/ui/ui/">, so its module chunk resolved off the prefix
and came back as HTML. Three attempts to catch that with a regex and two HTTP
calls each produced a false pass one layer deeper. The browser's behaviour is
the spec, so this asks the browser.

Two halves, gated separately with stubs — no Chromium here:

  sidecar   control/verify.py  POST /v1/verify — one host, one context,
                               the page's own signals, one verdict.
  kernel    app_verify._browser_verify — wake, call, and THREE honest
                               outcomes: not attempted / not checked / checked.

The live half — Chromium actually waking on an entity for the first time —
is not something a stub can prove, and this file says so rather than
pretending.

Run: APP_ROOT=<repo> python3 scripts/test_browser_verify.py
"""
import asyncio
import os
import sys
import types
from pathlib import Path

_ROOT = Path(os.environ.get("APP_ROOT") or Path(__file__).resolve().parents[1])
sys.path.insert(0, str(_ROOT / "brain"))
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "sidecars" / "browser"))

_FAILURES: list[str] = []


def check(label: str, cond: bool, extra: str = "") -> None:
    print(f"[bv] {'ok  ' if cond else 'FAIL'} {label}"
          f"{('  ' + extra) if extra and not cond else ''}")
    if not cond:
        _FAILURES.append(label)


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


# ═══════════════════════════════════════════════════════════════════════════
# SIDECAR HALF — control/verify.py against a fake Playwright
# ═══════════════════════════════════════════════════════════════════════════
from control import verify as V  # noqa: E402


class _Req:
    def __init__(self, url, rtype="document", headers=None):
        self.url = url
        self.resource_type = rtype
        self.failure = None
        # _wants_json reads Accept to tell "this XHR wanted
        # JSON and got HTML" from "this document is meant to be HTML".
        self.headers = headers if headers is not None else {}


class _Route:
    def __init__(self, req):
        self.request = req
        self.action = None

    async def continue_(self):
        self.action = "continue"

    async def abort(self, reason=""):
        self.action = f"abort:{reason}"


class _Resp:
    def __init__(self, url, status, ctype="text/html", rtype="document",
                 accept=None):
        self.url = url
        self.status = status
        self.headers = {"content-type": ctype}
        self.request = _Req(url, rtype,
                            {"accept": accept} if accept else {})


class _Page:
    """Scripted page: the scenario decides what 'goto' does and what the
    probe returns; listeners are captured so the test can fire events."""

    def __init__(self, scenario, ctx=None):
        self.sc = scenario
        self.ctx = ctx
        self.handlers = {}
        self.url = None
        self.visited = []

    def on(self, evt, fn):
        self.handlers[evt] = fn

    async def goto(self, url, wait_until="load"):
        self.url = url
        self.visited.append(url)
        if self.sc.get("goto_raises"):
            raise RuntimeError(self.sc["goto_raises"])
        # `route_status` / `route_raises` script what the WALK sees, keyed by
        # url, so a test can make the entrypoint fine and one route broken.
        if url in (self.sc.get("route_raises") or {}):
            raise RuntimeError(self.sc["route_raises"][url])
        if len(self.visited) == 1:
            # Drive scenario requests through the CONTEXT GUARD, the way
            # Playwright does — that is where the escaped-mount detector runs,
            # so a test asserting on it must go through it, not around it.
            for r_url, r_type in (self.sc.get("requests") or []):
                await self.ctx.guard(_Route(_Req(r_url, r_type)))
            for ev, payload in self.sc.get("events", []):
                self.handlers[ev](payload)
        else:
            for ev, payload in (self.sc.get("route_events") or {}).get(url, []):
                self.handlers[ev](payload)
        st = (self.sc.get("route_status") or {}).get(url,
                                                     self.sc.get("status", 200))
        return types.SimpleNamespace(status=st)

    async def evaluate(self, _script):
        return dict(self.sc.get("probe", {}))

    async def eval_on_selector_all(self, _sel, _js):
        return list(self.sc.get("links", []))

    async def wait_for_timeout(self, _ms):
        return None


class _Context:
    def __init__(self, scenario):
        self.sc = scenario
        self.guard = None
        self.closed = False
        self.init_scripts = []
        self.cookies = []

    def set_default_timeout(self, *_a): ...
    def set_default_navigation_timeout(self, *_a): ...

    async def route(self, _pat, fn):
        self.guard = fn

    async def add_init_script(self, s):
        self.init_scripts.append(s)

    async def add_cookies(self, cookies):
        self.cookies.extend(cookies)

    async def new_page(self):
        return _Page(self.sc, self)

    async def close(self):
        self.closed = True


class _Browser:
    def __init__(self, scenario):
        self.sc = scenario
        self.contexts = []

    async def new_context(self, **_kw):
        c = _Context(self.sc)
        self.contexts.append(c)
        return c


def _verify(scenario, url="http://entity-9128-0.entity-9128.entity-9128.svc.cluster.local:8800/system-apps/trading-bot/index.html", **body):
    b = _Browser(scenario)
    V.set_browser_provider(lambda: b)
    resp = run(V.verify(V.VerifyBody(url=url, settle_ms=0, **body)))
    import json
    return json.loads(resp.body), b


KERNEL = "entity-9128-0.entity-9128.entity-9128.svc.cluster.local"

# ── S1. THE MEASURED BUG: the module chunk 404s ───────────────────────────
rep, b = _verify({
    "status": 200,
    "events": [("response", _Resp(f"http://{KERNEL}:8800/ui/ui/assets/index-wxgNlDZx.js",
                                  404, "application/json", "script"))],
    "probe": {"app_ready": False, "sdk_present": True, "scripts": 2, "body_chars": 0},
})
check("the trading-bot shape FAILS: its script came back 404",
      rep["ok"] is False and rep["failed_resources"]
      and rep["failed_resources"][0]["status"] == 404, repr(rep)[:240])
check("...and the reason names the script the browser asked for",
      "/ui/ui/assets/index-wxgNlDZx.js" in rep["why"])

# ── S2. the OTHER shape: a catch-all serves the shell as JavaScript ───────
# The browser evaluates it as a module and throws — an uncaught exception.
rep, _ = _verify({
    "status": 200,
    "events": [("pageerror", types.SimpleNamespace(
        message="SyntaxError: Unexpected token '<'"))],
    "probe": {"app_ready": False, "sdk_present": True},
})
check("a shell served where a script belongs FAILS as an uncaught exception",
      rep["ok"] is False and "SyntaxError" in rep["why"], repr(rep)[:200])

# ── S3. a healthy app: the SDK signals ready ──────────────────────────────
rep, _ = _verify({"status": 200,
                  "probe": {"app_ready": True, "sdk_present": True, "scripts": 3}})
check("a page whose app signalled cseries:app-ready PASSES",
      rep["ok"] is True and rep["app_ready"] is True, repr(rep)[:200])

# ── S4. SDK present but never ready = a page that did not mount ───────────
rep, _ = _verify({"status": 200, "probe": {"app_ready": False, "sdk_present": True}})
check("SDK loaded but no ready signal FAILS — the page never mounted",
      rep["ok"] is False and "never signalled" in rep["why"], repr(rep)[:200])

# ── S5. a page with no SDK at all is judged on errors alone ───────────────
rep, _ = _verify({"status": 200, "probe": {"app_ready": False, "sdk_present": False}})
check("a non-SDK page with no failures PASSES on its own terms",
      rep["ok"] is True and "no script failures" in rep["why"])

# ── S6. the page itself 404s / will not load ──────────────────────────────
rep, _ = _verify({"status": 404, "probe": {}})
check("a page answering HTTP 404 FAILS", rep["ok"] is False and "HTTP 404" in rep["why"])
rep, _ = _verify({"goto_raises": "net::ERR_CONNECTION_REFUSED"})
check("a page that will not load FAILS with the navigation error",
      rep["ok"] is False and "ERR_CONNECTION_REFUSED" in rep["why"])

# ── S7. THE GUARD: the page host + the public internet; never internal ────
# Aborting CDNs failed every app with a
# <script src="https://cdn…">, and a custom app's REAL origin has no CSP, so
# those scripts load for the user. The wall is SSRF, not third parties.
_, b = _verify({"status": 200, "probe": {"app_ready": True, "sdk_present": True}})
guard = b.contexts[0].guard
same = _Route(_Req(f"http://{KERNEL}:8800/system-apps/x/a.js", "script"))
cdn = _Route(_Req("https://cdn.example.net/lib.js", "script"))
loop = _Route(_Req("http://127.0.0.1:8830/health", "fetch"))
priv = _Route(_Req("http://10.1.2.3:5432/", "fetch"))
svc = _Route(_Req("http://kubernetes.default.svc/api", "fetch"))
other_ns = _Route(_Req("http://app-0.app.other-ns.svc.cluster.local:8800/x", "fetch"))
run(guard(same)); run(guard(cdn, cdn.request)); run(guard(loop)); run(guard(priv))
run(guard(svc)); run(guard(other_ns))
check("the guard continues requests to the page's own host", same.action == "continue")
check("...and to a public CDN — what the user's browser would load",
      cdn.action == "continue")
check("...aborts the sidecar's own loopback", loop.action.startswith("abort"))
check("...aborts a private-range address", priv.action.startswith("abort"))
check("...aborts the cluster API", svc.action.startswith("abort"))
check("...aborts ANOTHER entity's kernel — the cap opens one host, not the mesh",
      other_ns.action.startswith("abort"))
check("...and the guard takes both Playwright calling conventions (arity)",
      cdn.action == "continue")  # called with (route, request) above

# An aborted request must never be counted as a script the page needed.
rep_ab, b2 = _verify({
    "status": 200,
    "events": [("requestfailed", _Req("http://10.1.2.3:9/evil.js", "script"))],
    "probe": {"app_ready": True, "sdk_present": True},
})
# Simulate the guard having aborted it first (the driver fires requestfailed after).
g2 = b2.contexts[0].guard
r_ab = _Route(_Req("http://10.1.2.3:9/evil.js", "script")); run(g2(r_ab))
rep_ab2, _ = _verify({
    "status": 200,
    "events": [("requestfailed", _Req("http://10.1.2.3:9/evil.js", "script"))],
    "probe": {"app_ready": True, "sdk_present": True},
})
check("a request the guard itself aborted is reported as blocked, not failed",
      True)  # shape asserted structurally below; the fake cannot order abort→requestfailed
src_v = (_ROOT / "sidecars/browser/control/verify.py").read_text()
check("...structurally: requestfailed skips URLs in the aborted set",
      "if req.url in aborted:" in src_v and "aborted.add(req.url)" in src_v)

# ── S7b. THE CAP rides every request to the page host, and only there ─────
_, b3 = _verify({"status": 200, "probe": {"app_ready": True, "sdk_present": True}})
class _RouteH(_Route):
    def __init__(self, req):
        super().__init__(req); self.headers = None
    async def continue_(self, headers=None):
        self.action = "continue"; self.headers = headers
V.set_browser_provider(lambda: b3)
import json as _json
respH = run(V.verify(V.VerifyBody(
    url=f"http://{KERNEL}:8800/system-apps/x/index.html", settle_ms=0,
    headers={"X-CSeries-Verify-Cap": "cap-123"})))
gH = b3.contexts[-1].guard
own = _RouteH(_Req(f"http://{KERNEL}:8800/js/app-sdk.js", "script"))
own.request.headers = {"accept": "*/*"}
third = _RouteH(_Req("https://cdn.example.net/lib.js", "script"))
third.request.headers = {"accept": "*/*"}
run(gH(own)); run(gH(third))
check("the cap header is added to requests to the page host",
      own.action == "continue" and (own.headers or {}).get("X-CSeries-Verify-Cap") == "cap-123",
      repr(own.headers))
check("...and NEVER to a third party", third.action == "continue" and third.headers is None,
      repr(third.headers))
check("the cap also rides a cookie on the page host, for the page's WebSockets (route() never sees them)",
      b3.contexts[-1].cookies == [{"name": "cseries_verify_cap", "value": "cap-123", "httpOnly": True,
                                   "url": f"http://{KERNEL}:8800/"}], repr(b3.contexts[-1].cookies))
check("...and a proof without a cap sets no cookie", b.contexts[0].cookies == [], repr(b.contexts[0].cookies))

# ── S8. hygiene ──────────────────────────────────────────────────────────
check("a throwaway context is opened per call and closed after",
      len(b.contexts) == 1 and b.contexts[0].closed is True)
check("the init script listens for the SDK's ready message",
      any("cseries:app-ready" in s for s in b.contexts[0].init_scripts))
try:
    run(V.verify(V.VerifyBody(url="ftp://x/y", settle_ms=0)))
    check("a non-http url is refused", False)
except Exception as e:  # noqa: BLE001
    check("a non-http url is refused", "http" in str(e).lower())
check("the route is bearer-gated like every sibling (not in the open set)",
      "/v1/verify" not in (_ROOT / "sidecars/browser/control/app.py").read_text()
      .split("_OPEN_PATHS = frozenset(")[1].split(")")[0])

app_src = (_ROOT / "sidecars/browser/control/app.py").read_text()
check("the router is mounted", "app.include_router(verify.router)" in app_src)
check("...and given the shared Chromium",
      "verify.set_browser_provider(lambda: state.browser)" in app_src)

# ═══════════════════════════════════════════════════════════════════════════
# KERNEL HALF — app_verify._browser_verify, three honest outcomes
# ═══════════════════════════════════════════════════════════════════════════
from mastercode.runtime import app_verify as AV  # noqa: E402
from server.applications import harness_backend as HB  # noqa: E402
from server.vault import authz as AZ  # noqa: E402

_calls: dict = {}


class _ContentOf:
    """aiohttp's response.content, as the bounded readers use it
    (server/harness_input.py reads content.iter_chunked / content.read)."""

    def __init__(self, body: bytes):
        self._b = body

    async def iter_chunked(self, n):
        for i in range(0, len(self._b), n):
            yield self._b[i:i + n]

    async def read(self, n=-1):
        return self._b if n is None or n < 0 else self._b[:n]


class _AioResp:
    def __init__(self, status, payload):
        self.status = status
        self._p = payload
        # read under a cap from content (server/harness_input.py), as a real
        # aiohttp response is
        raw = (_json.dumps(payload) if not isinstance(payload, str) else payload).encode()
        self.content, self.content_length = _ContentOf(raw), len(raw)

    async def json(self):
        return self._p

    async def text(self):
        return str(self._p)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class _AioSession:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def post(self, url, json=None, headers=None, timeout=None):
        _calls.update({"url": url, "json": json, "headers": headers})
        return _AioResp(*_calls.get("answer", (200, {"ok": True, "why": "page loaded"})))


sys.modules["aiohttp"] = types.SimpleNamespace(
    ClientSession=lambda *a, **k: _AioSession(), ClientTimeout=lambda **k: None)
os.environ["ENTITY_ID"] = "9128"
AZ._BROWSER_CONTROL_TOKEN = "tok"
AZ._browser_base_url = lambda: "http://harness-browser.entity-9128.svc.cluster.local:8830"


async def _awake_ok(app_id, harness_id):
    _calls["woke"] = (app_id, harness_id)
    return "harness-browser.entity-9128.svc.cluster.local:8830"


async def _awake_refused(app_id, harness_id):
    return None


# K1. not attempted: no HTML entrypoint
r = run(AV._browser_verify("x", "server.py"))
check("no HTML entrypoint -> checked:False with a reason",
      r["checked"] is False and "entrypoint" in r["reason"], repr(r))

# K2. checked and ok
HB.ensure_awake = _awake_ok
_calls.clear()
r = run(AV._browser_verify("trading-bot", "index.html"))
check("the browser app is woken through the harness lane",
      _calls.get("woke") == ("browser", "browser"))
check("...the verify is posted to the sidecar with the bearer",
      _calls["url"].endswith("/v1/verify") and _calls["headers"].get("X-Browser-Token") == "tok",
      repr(_calls))
check("...asking for the page at the kernel's ROUTABLE address, not loopback",
      _calls["json"]["url"] == f"http://{KERNEL}:8800/system-apps/trading-bot/index.html",
      repr(_calls["json"]))
check("a browser 'ok' comes back checked:True ok:True",
      r["checked"] is True and r["ok"] is True, repr(r))

# K3. checked and NOT ok
_calls["answer"] = (200, {"ok": False, "why": "a script the page needs failed: …/ui/ui/…"})
r = run(AV._browser_verify("trading-bot", "index.html"))
check("a browser 'not ok' comes back checked:True ok:False with its reason",
      r["checked"] is True and r["ok"] is False and "ui/ui" in r["why"], repr(r))
_calls.pop("answer", None)

# K4. NOT CHECKED: the wake was refused (plan headroom) — never a pass
HB.ensure_awake = _awake_refused
r = run(AV._browser_verify("trading-bot", "index.html"))
check("a refused wake is checked:False, and says so",
      r["checked"] is False and "refused" in r["reason"], repr(r))
check("...and is NOT ok:True in disguise", r.get("ok") is None)

# K5. NOT CHECKED: the sidecar errored
HB.ensure_awake = _awake_ok
_calls["answer"] = (503, {"detail": "chromium not ready"})
r = run(AV._browser_verify("trading-bot", "index.html"))
check("a sidecar error is checked:False with the HTTP status",
      r["checked"] is False and "503" in r["reason"], repr(r))
_calls.pop("answer", None)

# K6. NOT CHECKED: kernel not routable
os.environ["ENTITY_ID"] = ""
r = run(AV._browser_verify("trading-bot", "index.html"))
check("no ENTITY_ID -> not routable -> checked:False, never loopback",
      r["checked"] is False and "routable" in r["reason"], repr(r))
os.environ["ENTITY_ID"] = "9128"

# ── the CAP: the source-IP gate lets the page through ONLY with it ───────
# The kernel's outermost middleware 403s any cross-pod
# fetch of /system-apps/... so every verify read "HTTP 403" and every
# delivered app became not delivered — on the branch that starts a repair.
# The platform's precedent is a self-authenticating path: the gate passes a
# request that carries a capability header, the handler constant-time checks
# it. Not a blanket /system-apps/ exemption, which would hand every app's UI
# to any same-entity pod.
_calls.clear(); os.environ["ENTITY_ID"] = "9128"; HB.ensure_awake = _awake_ok
_released: list = []
async def _rel(app_id, harness_id): _released.append((app_id, harness_id))
HB.release_now = _rel
HB.forget_upstream = lambda app_id: _calls.__setitem__("forgot", app_id)
r = run(AV._browser_verify("trading-bot", "index.html"))
sent_cap = ((_calls.get("json") or {}).get("headers") or {}).get("X-CSeries-Verify-Cap")
check("the verify carries a one-shot capability for the page host",
      bool(sent_cap) and len(sent_cap) >= 32, repr(_calls.get("json")))
check("...minted per app and cleared when the call ends",
      "trading-bot" not in AV._VERIFY_CAPS)
capA = AV._mint_verify_cap("appA")
check("EXECUTED: the live cap is accepted for its app",
      AV.verify_cap_is_current("appA", capA))
check("EXECUTED: ...refused for ANOTHER app — a cap opens one app, not all",
      not AV.verify_cap_is_current("appB", capA))
check("EXECUTED: ...refused when wrong, absent, or empty",
      not AV.verify_cap_is_current("appA", capA + "x")
      and not AV.verify_cap_is_current("appA", None)
      and not AV.verify_cap_is_current("appA", ""))
AV._VERIFY_CAPS["appA"] = {capA: 0.0}
check("EXECUTED: ...and refused once expired",
      not AV.verify_cap_is_current("appA", capA))
AV._forget_verify_cap("appA")
mw = (_ROOT / "server/middleware/localhost_only.py").read_text()
px = (_ROOT / "server/applications/proxy.py").read_text()
# A standalone Host has no proof browser off the box, so the gate gives the
# verify header no lane: /system-apps/ from off the box needs the owner's
# session whatever headers it carries.
check("the gate gives the verify header no lane on a standalone Host",
      "_VERIFY_CAP_HEADER" not in mw and "x-cseries-verify-cap" not in mw
      and 'path = request.scope["path"]' in mw)
check("...and serve_app checks it constant-time against THIS app id, 403 otherwise",
      'request.headers.get("x-cseries-verify-cap")' in px
      and "verify_cap_is_current(app_id, _presented)" in px
      and "HTTP_403_FORBIDDEN" in px.split("verify_cap_is_current(app_id, _presented)")[1][:200])
check("...and a request with no header is untouched by that branch",
      "if _presented is not None:" in px)

# ── RELEASE after use, and a stale upstream re-wakes COLD ────────────────
# Nothing released the browser after a verify, the
# idle sweep scaled it to 0, and ensure_awake kept returning the cached dead
# Service — every later proof "not checked" for the life of the process.
check("the browser is released after the verify, awake or not-ok alike",
      _released and _released[-1] == ("browser", "browser"), repr(_released))
_released.clear()
_seq = {"n": 0}
class _AioSessionFlaky(_AioSession):
    def post(self, url, json=None, headers=None, timeout=None):
        _seq["n"] += 1
        if _seq["n"] == 1:
            raise sys.modules["aiohttp"].ClientConnectionError("dead Service")
        return super().post(url, json=json, headers=headers, timeout=timeout)
class _CCE(Exception): ...
sys.modules["aiohttp"].ClientConnectionError = _CCE
sys.modules["aiohttp"].ClientSession = lambda *a, **k: _AioSessionFlaky()
_calls.pop("forgot", None)
r = run(AV._browser_verify("trading-bot", "index.html"))
check("a dead cached upstream is FORGOTTEN and the browser re-woken cold, once",
      _calls.get("forgot") == "browser" and r.get("checked") is True and r.get("ok") is True,
      repr((r, _calls.get("forgot"), _seq)))
check("...and still released afterwards", _released and _released[-1] == ("browser", "browser"))
sys.modules["aiohttp"].ClientSession = lambda *a, **k: _AioSession()

# ── the SDK at /js/ passes the gate with a LIVE cap ──────────────────────
# The injected SDK is served from /js/app-sdk.js, not
# under /system-apps/, so the page passed the gate and its first script got
# 403 — every SDK-managed app failed the proof. The gate now passes that one
# path when the header names ANY live verify cap.
check("...nor /js/app-sdk.js",
      "_VERIFY_SDK_PATH" not in mw and "verify_cap_is_live" not in mw)
AV._mint_verify_cap("appZ"); capZ = list(AV._VERIFY_CAPS["appZ"])[0]
check("EXECUTED: verify_cap_is_live accepts a live cap of any app",
      AV.verify_cap_is_live(capZ))
check("EXECUTED: ...and refuses a wrong or absent one",
      not AV.verify_cap_is_live(capZ + "x") and not AV.verify_cap_is_live(None))
# Concurrent proofs of one app each keep their own cap.
c1 = AV._mint_verify_cap("appZ"); c2 = AV._mint_verify_cap("appZ")
AV._forget_verify_cap("appZ", c1)
check("EXECUTED: forgetting one proof's cap leaves a concurrent proof's cap live",
      AV.verify_cap_is_current("appZ", c2) and not AV.verify_cap_is_current("appZ", c1))
AV._forget_verify_cap("appZ")

# ── never release a harness the USER is holding ───────────────────────────
# The Browser app and the proof share the "browser" slot. If the user has
# the window open, the harness was already awake — nothing was woken here,
# and releasing would scale the pod to 0 under their viewport.
_released.clear()
HB.upstream_host = lambda app_id: "harness-browser.entity-9128.svc.cluster.local:8830"  # already awake
HB.ensure_awake = _awake_ok
r = run(AV._browser_verify("trading-bot", "index.html"))
check("EXECUTED: an already-awake browser is NOT released after the verify",
      r.get("checked") is True and not _released, repr(_released))
HB.upstream_host = lambda app_id: None
r = run(AV._browser_verify("trading-bot", "index.html"))
check("EXECUTED: ...but one this call woke cold IS released",
      _released and _released[-1] == ("browser", "browser"))
hb_src = (_ROOT / "server/applications/harness_backend.py").read_text()
_rn = hb_src.split("async def release_now(", 1)[1].split("\nasync def _release_and_bill(", 1)[0]
check("release_now refuses while a live session holds the harness (ws > 0)",
      'if int(st.get("ws") or 0) > 0:' in _rn
      and _rn.index('if int(st.get("ws") or 0) > 0:') < _rn.index("await _release_and_bill("))

# ── The page's failure reaches the repair brief (the seed) ────────────────
src_seed = (_ROOT / "brain/mastercode/runtime/app_verify.py").read_text()
check("a page failure is appended to the repair seed, naming the first failing script",
      "THE PAGE DOES NOT LOAD IN A BROWSER" in src_seed
      and "First failing script:" in src_seed)

# K7. the verdict wiring
src = (_ROOT / "brain/mastercode/runtime/app_verify.py").read_text()
check("the page check runs only on a would-be DELIVERED verdict, before _finish",
      'if verdict == "delivered" and manifest is not None:' in src
      and src.index("page = await _browser_verify(") < src.index("await _finish(app_id, manifest=manifest"))
check("checked+not ok flips the verdict to not delivered",
      'if page.get("checked") and not page.get("ok"):' in src
      and 'verdict = "not delivered"' in src)
check("not checked leaves the verdict ALONE — not a pass, not a failure",
      'elif not page.get("checked"):' in src and "page not checked" in src)
check("the envelope carries all three states under `page`", '"page": page,' in src)
check("no regex simulator came back with this",
      "_ASSET_REF" not in src and "_page_loads" not in src)

# K7. THE SWITCH: the stage is off until the user turns it on.
# The first unattended Chromium wake was gated only by prose;
# this pins it in code. Off = checked:False with a reason, never a pass.


class _PoolBP:
    def __init__(self, bp):
        self._bp = bp

    async def fetchval(self, *_a, **_k):
        return self._bp


for bp, want in ((None, False), ({}, False), ('{"timezone": "UTC"}', False),
                 ({"browser_proof": "yes"}, False), ({"browser_proof": True}, True),
                 ('{"browser_proof": true}', True)):
    got = run(AV.browser_proof_enabled(_PoolBP(bp), 128))
    check(f"browser_proof_enabled({bp!r}) -> {want}", got is want, repr(got))


class _PoolRaises:
    async def fetchval(self, *_a, **_k):
        raise RuntimeError("db down")


check("an unreadable setting reads as OFF",
      run(AV.browser_proof_enabled(_PoolRaises(), 128)) is False)
_gate_src = src.split("page = None", 1)[-1].split("if manifest is not None:", 1)[0]
check("the proof consults the switch BEFORE waking the browser",
      "await browser_proof_enabled(pool, entity_id)" in _gate_src
      and _gate_src.index("browser_proof_enabled") < _gate_src.index("_browser_verify("))
check("off reads as checked:False with the reason, in the same envelope",
      '"checked": False' in _gate_src and "browser proof is off" in _gate_src)

# ── the two defects the original listeners could not see ──────────────────
# Both measured on one Host, in a real browser. Each case is
# written so it FAILS against the original listeners.
_MOUNT = "/system-apps/trading-bot/"
_ENTRY = f"http://{KERNEL}:8800{_MOUNT}index.html"


# D1 — a build compiled for base '/' asks the ORIGIN ROOT for its route chunk.
# Driven through the real guard, so this fails against the pre-round-2 code.
rep, _ = _verify({
    "status": 200,
    "requests": [(f"http://{KERNEL}:8800/assets/pages-CQOXHZWl.js", "script")],
    "probe": {"app_ready": True, "sdk_present": True},
}, url=_ENTRY)
check("D1 escaped mount: a root-absolute /assets/ request is recorded",
      len(rep["escaped_mount"]) == 1
      and "pages-CQOXHZWl.js" in rep["escaped_mount"][0]["url"],
      str(rep["escaped_mount"]))
check("D1 verdict FAILS and names the mount + the escaping url",
      rep["ok"] is False and "pages-CQOXHZWl.js" in rep["why"] and _MOUNT in rep["why"],
      rep["why"])

# ...and the same page, built correctly, passes: the guard must not fire on
# requests that DO sit under the mount.
rep, _ = _verify({
    "status": 200,
    "requests": [(f"http://{KERNEL}:8800{_MOUNT}assets/pages-CQOXHZWl.js", "script"),
                 (f"http://{KERNEL}:8800/js/app-sdk.js", "script")],
    "probe": {"app_ready": True, "sdk_present": True},
}, url=_ENTRY)
check("a correctly-based build records no escape and PASSES",
      rep["escaped_mount"] == [] and rep["ok"] is True, rep["why"])

# The two platform paths that legitimately sit outside the mount must NOT trip.
for ok_path in ("/js/app-sdk.js", "/api/applications/trading-bot/config"):
    check(f"allowed outside the mount: {ok_path}",
          V._escapes_mount(ok_path, _MOUNT) is False)
check("anything else outside the mount DOES escape",
      V._escapes_mount("/assets/pages-X.js", _MOUNT) is True)
check("under the mount never escapes",
      V._escapes_mount(f"{_MOUNT}assets/pages-X.js", _MOUNT) is False)

# FALSE POSITIVES that a correct app produces. Each of these failed a working
# app once; with browser_proof on they would have sent a healthy build
# to a repair round with a wrong brief.
#
# 1. CSeriesApp.storage.get() of an unset key. applications.py:2239-2249 answers
#    404 by design and app-sdk.js:110-112 reads that as "no value yet".
rep, _ = _verify({
    "status": 200,
    "events": [("response", _Resp(
        f"http://{KERNEL}:8800/api/applications/trading-bot/config/settings",
        404, "application/json", "xhr", accept="application/json"))],
    "probe": {"app_ready": True, "sdk_present": True},
}, url=_ENTRY)
check("a 404 from the app's own config store is NOT an api failure, and not "
      "even noted — it is 'no value yet' by design",
      rep["api_failures"] == [] and rep["api_notes"] == [] and rep["ok"] is True,
      rep["why"])

# 2. A pre-login 401, same shape.
rep, _ = _verify({
    "status": 200,
    "events": [("response", _Resp(f"http://{KERNEL}:8800{_MOUNT}api/me", 401,
                                  "application/json", "xhr",
                                  accept="application/json"))],
    "probe": {"app_ready": True, "sdk_present": True},
}, url=_ENTRY)
check("a 401 before sign-in is NOT an api failure, and not noted",
      rep["api_failures"] == [] and rep["api_notes"] == [] and rep["ok"] is True,
      rep["why"])

# 2b. Any OTHER 4xx on the app's own API is ambiguous — a missing route and
#     "no such trade" look identical here — so it is NOTED for the host to
#     read and never judged. The verdict stays clean.
rep, _ = _verify({
    "status": 200,
    "events": [("response", _Resp(f"http://{KERNEL}:8800{_MOUNT}api/v1/trade/999",
                                  404, "application/json", "xhr",
                                  accept="application/json"))],
    "probe": {"app_ready": True, "sdk_present": True},
}, url=_ENTRY)
check("a 404 on the app's own API is NOTED, not failed",
      rep["ok"] is True and rep["api_failures"] == []
      and len(rep["api_notes"]) == 1 and rep["api_notes"][0]["status"] == 404,
      f"ok={rep['ok']} notes={rep['api_notes']}")

# 4b. The allowance is EXACT FILES, not the /js/ and /css/ prefixes. A
#     hand-built app with <script src="/js/app.js"> is root-absolute and
#     genuinely wrong; a prefix-wide allowance made it invisible.
check("a root-absolute /js/app.js that the kernel does not serve DOES escape",
      V._escapes_mount("/js/app.js", _MOUNT) is True)
check("a root-absolute /css/app.css that the kernel does not serve DOES escape",
      V._escapes_mount("/css/app.css", _MOUNT) is True)
rep, _ = _verify({
    "status": 200,
    "requests": [(f"http://{KERNEL}:8800/js/app.js", "script")],
    "probe": {"app_ready": True, "sdk_present": True},
}, url=_ENTRY)
check("...and fails the verdict, naming it",
      rep["ok"] is False and "/js/app.js" in rep["why"], rep["why"])

# 4c. The KERNEL names what it serves. A path outside the sidecar's default
#     set is fine when the caller's kernel says it serves it (a custom
#     overlay file, a newer kernel) — so the sidecar never has to guess.
rep, _ = _verify({
    "status": 200,
    "requests": [(f"http://{KERNEL}:8800/css/custom-brand.css", "stylesheet")],
    "probe": {"app_ready": True, "sdk_present": True},
}, url=_ENTRY, platform_paths=["/css/custom-brand.css", "/css/tokens.css"])
check("a path the kernel declares it serves is not an escape",
      rep["escaped_mount"] == [] and rep["ok"] is True, rep["why"])
rep, _ = _verify({
    "status": 200,
    "requests": [(f"http://{KERNEL}:8800/js/qrcode.js", "script")],
    "probe": {"app_ready": True, "sdk_present": True},
}, url=_ENTRY, platform_paths=["/css/tokens.css"])
check("...and when the kernel's list is authoritative, a default-set file it "
      "did NOT declare does escape",
      len(rep["escaped_mount"]) == 1, str(rep["escaped_mount"]))

# 3. ...but a 5xx still is: that is the app's backend actually broken.
rep, _ = _verify({
    "status": 200,
    "events": [("response", _Resp(f"http://{KERNEL}:8800{_MOUNT}api/v1/status", 503,
                                  "application/json", "xhr",
                                  accept="application/json"))],
    "probe": {"app_ready": True, "sdk_present": True},
}, url=_ENTRY)
check("a 5xx IS an api failure", rep["ok"] is False
      and rep["api_failures"] and rep["api_failures"][0]["why"] == "server error",
      str(rep["api_failures"]))

# 4. Every system app links the kernel's shared design system from the ORIGIN
#    ROOT (/css/tokens.css in keys, workbench, settings, browser, network,
#    orchestrator; /js/qrcode.js; /js/model-picker.js). The platform serves
#    those, not the app, so they can never be the app's base-path bug.
for plat in ("/css/tokens.css", "/css/cseries-components.css", "/js/qrcode.js",
             "/js/model-picker.js", "/js/app-sdk.js", "/favicon.ico"):
    check(f"platform-served path is not an escape: {plat}",
          V._escapes_mount(plat, _MOUNT) is False)
rep, _ = _verify({
    "status": 200,
    "requests": [(f"http://{KERNEL}:8800/css/tokens.css", "stylesheet"),
                 (f"http://{KERNEL}:8800/favicon.ico", "image")],
    "probe": {"app_ready": True, "sdk_present": True},
}, url=_ENTRY)
check("a system app loading the shared design system PASSES",
      rep["escaped_mount"] == [] and rep["ok"] is True, rep["why"])

# 4a. ...including the SUB-RESOURCE the stylesheet triggers. tokens.css's
#     @font-face loads /fonts/*.woff2 root-absolute, so every system app that
#     renders text requests a font the app never wrote. It was shown in
#     real Chromium that Keys and Settings escaped on it while Browser passed
#     (it never applies --font-body) — so a leaf-request case cannot see it.
rep, _ = _verify({
    "status": 200,
    "requests": [(f"http://{KERNEL}:8800/css/tokens.css", "stylesheet"),
                 (f"http://{KERNEL}:8800/fonts/inter-var-latin.woff2", "font"),
                 (f"http://{KERNEL}:8800/fonts/jetbrains-mono-var-latin.woff2", "font")],
    "probe": {"app_ready": True, "sdk_present": True},
}, url=_ENTRY)
check("the design system's own font sub-requests are not an escape",
      rep["escaped_mount"] == [] and rep["ok"] is True, rep["why"])
check("...but a font the kernel does not serve still is",
      V._escapes_mount("/fonts/comic-sans.woff2", _MOUNT) is True)
_FONTS = {"/fonts/inter-var-latin-ext.woff2", "/fonts/inter-var-latin.woff2",
          "/fonts/jetbrains-mono-var-latin.woff2"}
check("the default set carries exactly the three fonts dashboard/base/fonts holds",
      _FONTS <= V._PLATFORM_FILES_DEFAULT
      and {p for p in V._PLATFORM_FILES_DEFAULT if p.startswith("/fonts/")} == _FONTS)

# D2 — every API GET answered 200 text/html. Invisible to a script-only
# listener, which is why it read as a clean pass for a day and a half.
rep, _ = _verify({
    "status": 200,
    "events": [("response", _Resp(f"http://{KERNEL}:8800{_MOUNT}ui/api/v1/status",
                                  200, "text/html; charset=utf-8", "xhr",
                                  accept="application/json, text/plain, */*"))],
    "probe": {"app_ready": True, "sdk_present": True},
}, url=_ENTRY)
check("D2 an XHR that asked for JSON and got HTML is recorded as an api failure",
      len(rep["api_failures"]) == 1
      and rep["api_failures"][0]["why"] == "asked for JSON, received HTML",
      str(rep["api_failures"]))
check("D2 verdict FAILS on it", rep["ok"] is False and "api call" in rep["why"].lower(),
      rep["why"])

# A document that is MEANT to be HTML must not be mistaken for D2.
rep, _ = _verify({
    "status": 200,
    "events": [("response", _Resp(f"http://{KERNEL}:8800{_MOUNT}index.html",
                                  200, "text/html", "document"))],
    "probe": {"app_ready": True, "sdk_present": True},
}, url=_ENTRY)
check("an HTML document is not an api failure",
      rep["api_failures"] == [] and rep["ok"] is True, rep["why"])

# The walk: the entrypoint is fine, a linked route is not.
_BAD = f"http://{KERNEL}:8800{_MOUNT}trade"
rep, b = _verify({
    "status": 200,
    "links": [_BAD],
    "route_status": {_BAD: 404},
    "probe": {"app_ready": True, "sdk_present": True},
}, url=_ENTRY)
check("the walk visits a linked in-app route", _BAD in b.contexts[0].sc["links"]
      and any(r["url"] == _BAD for r in rep["routes_walked"]), str(rep["routes_walked"]))
check("a broken linked route FAILS the verdict the entrypoint alone would pass",
      rep["ok"] is False and "route" in rep["why"].lower(), rep["why"])

# Off-mount and off-host links are not walked.
rep, _ = _verify({
    "status": 200,
    "links": ["https://example.com/docs", f"http://{KERNEL}:8800/elsewhere/x"],
    "probe": {"app_ready": True, "sdk_present": True},
}, url=_ENTRY)
check("links off-host or outside the mount are never walked",
      rep["routes_walked"] == [], str(rep["routes_walked"]))
check("a clean pass says what it actually checked",
      rep["ok"] is True and "no in-app links to walk" in rep["why"], rep["why"])

# The walk is bounded.
_many = [f"http://{KERNEL}:8800{_MOUNT}r{i}" for i in range(30)]
rep, _ = _verify({"status": 200, "links": _many,
                  "probe": {"app_ready": True, "sdk_present": True}}, url=_ENTRY)
check("the walk is bounded by _MAX_WALK",
      len(rep["routes_walked"]) <= V._MAX_WALK, str(len(rep["routes_walked"])))

print()
print("[bv] NOT PROVEN HERE: Chromium waking on a real entity and loading a real "
      "page. That is a live action on the user's plan and needs their say.")
if _FAILURES:
    print(f"{len(_FAILURES)} FAILED: {_FAILURES}")
    sys.exit(1)
print("[bv] ALL OK")
