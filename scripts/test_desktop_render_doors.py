#!/usr/bin/env python3
"""Gate for the desktop RENDER check's doors — every place a desktop document
becomes something someone sees runs it, and each reads "not checked" the way
it was decided.

Origin (2026-09-30): five harness-built desktop designs reached the
shared Designs shelf without ever being rendered. Publish and set-desktop ran
only the text floor (desktop_contract.check_document). The render check
(server/applications/desktop_render.py) loads the document in the browser
sidecar the way the shell mounts it; tests/test_desktop_render_check.py proves
the check itself in real Chromium. This gate proves the WIRING, with stubs —
no Chromium, no pod, no network:

  kernel   render_check -> app_verify.browser_sidecar_post -> POST
           /v1/verify/document: the served mount scripts ride in the page,
           the wake/call/release discipline is the delivery proof's own, and
           every way the browser cannot be asked reads checked:False — an
           older sidecar with no route says so by name.

  doors    publish_document     not checked REFUSES (503) — others see it
           set_desktop_artifact not checked PROCEEDS with a NOT RENDER-CHECKED
                                warning — the user's own, undoable screen
           install_design       checks BEFORE copying (no orphan on refusal)
                                and hands its result on, so one load per try
           wear_local           through set_desktop_artifact, checked
           _restore             never checked — going back must always work
           every door           a failed check refuses (422) with the
                                error and its line; the floor runs first

Run: APP_ROOT=<repo> python3 scripts/test_desktop_render_doors.py
"""
import asyncio
import json as _json
import os
import sys
import types
from pathlib import Path

_ROOT = Path(os.environ.get("APP_ROOT") or Path(__file__).resolve().parents[1])
sys.path.insert(0, str(_ROOT / "brain"))
sys.path.insert(0, str(_ROOT))

_FAILURES: list[str] = []


def check(label: str, cond: bool, extra: str = "") -> None:
    print(f"[render-doors] {'ok  ' if cond else 'FAIL'} {label}"
          f"{('  ' + extra) if extra and not cond else ''}")
    if not cond:
        _FAILURES.append(label)


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


# server.main is the whole app; the doors only reach for it lazily (broadcast,
# the bridge client, the dashboard dirs) and every one of those is fail-soft.
sys.modules.setdefault("server.main", types.ModuleType("server.main"))

from fastapi import HTTPException  # noqa: E402

from server import brain_service  # noqa: E402
from server.applications import desktop_render as DR  # noqa: E402
from server.applications.artifacts import ArtifactError  # noqa: E402
from server.applications.desktop_contract import TEMPLATE_HTML  # noqa: E402

brain_service._entity_id = 9003
# Kernel-private state (pins, remembered failures): a private dir here.
import tempfile as _tempfile  # noqa: E402
_STATE = Path(_tempfile.mkdtemp())
DR.state_dir = lambda: _STATE
OK = {"checked": True, "ok": True, "why": "rendered", "failures": []}
FAILED = {"checked": True, "ok": False,
          "why": "an uncaught error at line 196: `null.boom()` — TypeError: boom",
          "failures": ["an uncaught error at line 196: `null.boom()` — TypeError: boom"]}
UNCHECKED = {"checked": False, "reason": "browser wake refused or failed (see harness log)"}


# ═══════════════════════════════════════════════════════════════════════════
# 1. KERNEL — render_check through the shared sidecar call
# ═══════════════════════════════════════════════════════════════════════════
from server.applications import harness_backend as HB  # noqa: E402
from server.vault import authz as AZ  # noqa: E402

_calls: dict = {}


class _Content:
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
        raw = (_json.dumps(payload) if not isinstance(payload, str) else payload).encode()
        self.content, self.content_length = _Content(raw), len(raw)

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
        _calls.setdefault("posted", []).append(json)
        return _AioResp(*_calls["answer"])


_real_aiohttp = sys.modules.get("aiohttp")
sys.modules["aiohttp"] = types.SimpleNamespace(
    ClientSession=lambda *a, **k: _AioSession(), ClientTimeout=lambda **k: None,
    ClientConnectionError=ConnectionError)
AZ._BROWSER_CONTROL_TOKEN = "tok"
AZ._browser_base_url = lambda: "http://harness-browser.host-ns.svc.cluster.local:8830"
_awake: dict = {"url": None}


async def _wake_ok(app_id, harness_id):
    _calls["woke"] = (app_id, harness_id)
    return "harness-browser.host-ns.svc.cluster.local:8830"


async def _wake_refused(app_id, harness_id):
    return None


async def _release(app_id, harness_id):
    _calls["released"] = (app_id, harness_id)


HB.upstream_host = lambda app_id: _awake["url"]
HB.release_now = _release
HB.forget_upstream = lambda app_id: None
HB.linger = lambda app_id, harness_id, idle_s: _calls.update(lingered=(app_id, idle_s))
HB.lingering = lambda app_id: False


class _AppRow:
    def __init__(self, i, n):
        self.id, self.name = i, n


async def _list_active(_pool):
    return [_AppRow("circles", "Circles"), _AppRow("designs", "Designs")]


from server.applications import registry as _REG  # noqa: E402
_REG.list_active = _list_active

# K1. the request: the right route, the bearer, the served mount scripts
HB.ensure_awake = _wake_ok
_report = {"loaded": True, "subject_found": True, "ready": True, "uncaught": [],
           "paint": {"painted_share": 0.3, "dominant": "#101010"},
           "probes": {"tiles": 2, "bar": 5}, "settle_ms": DR.SETTLE_MS}
_calls.clear()
_calls["answer"] = (200, _report)
res = run(DR.render_check(object(), TEMPLATE_HTML))
posted = _calls.get("posted") or []
sent = posted[0] if posted else {}
check("render_check posts to the sidecar's /v1/verify/document with the bearer",
      _calls.get("url", "").endswith("/v1/verify/document")
      and (_calls.get("headers") or {}).get("X-Browser-Token") == "tok", repr(_calls.get("url")))
sandbox_js = (_ROOT / "dashboard/base/js/sandbox-csp.js").read_text()
preview_js = (_ROOT / "dashboard/base/js/desktop-preview.js").read_text()
check("...the page carries the kernel's OWN sandbox-csp.js and desktop-preview.js, verbatim",
      sandbox_js in sent.get("page", "") and preview_js in sent.get("page", ""))
check("...mounted in a sandbox=\"allow-scripts\" frame the sidecar finds by its ELEMENT id",
      f'<iframe id="{DR.SUBJECT}" sandbox="allow-scripts">' in sent.get("page", "")
      and sent.get("subject") == DR.SUBJECT)
check("...with no flag in the document's world that marks it as a check",
      "reportErrors" not in sent.get("page", "") and "var R=" not in sent.get("page", ""))
check("...and the Host's real apps handed to it",
      '"id": "circles"' in sent.get("page", "") and '"id": "designs"' in sent.get("page", ""))
check("...the template (desktop + bar) is probed for tiles (attached, and reachable by a "
      "click) AND its bar (attached anywhere, or shown in its strip)",
      set((sent.get("probes") or {})) == {"tiles", "tiles_reach", "bar", "bar_strip"}
      and sent["probes"]["tiles"]["kind"] == "attached"
      and sent["probes"]["tiles_reach"]["kind"] == "reachable"
      and sent["probes"]["bar"]["kind"] == "attached"
      and sent["probes"]["bar_strip"]["rect"] == [0, DR.VIEWPORT[1] - 52, DR.VIEWPORT[0], 52.0],
      repr(sent.get("probes")))
check("...the settle is the shell's own later deadline (2 s)", sent.get("settle_ms") == 2000)
# The phone pass: a document that passes at desktop size is
# loaded again the way a phone mounts it.
ph = posted[1] if len(posted) > 1 else {}
check("a pass at desktop size is loaded AGAIN as a phone: two posts, desktop then phone size",
      len(posted) == 2 and (sent.get("width"), sent.get("height")) == DR.VIEWPORT
      and (ph.get("width"), ph.get("height")) == DR.PHONE_VIEWPORT, repr([(p.get("width"), p.get("height")) for p in posted]))
check("...the phone look's stand-in says CSeriesDesktop.phone; the desktop look's does not",
      '"phone": true' in ph.get("page", "") and '"phone": true' not in sent.get("page", ""))
check("...on a phone no bar is expected (the shell keeps its taskbar) and the tiles' place is "
      "measured: inside the screen's width, and anywhere",
      set((ph.get("probes") or {})) == {"tiles", "tiles_reach", "tiles_across", "tiles_anywhere"}
      and ph["probes"]["tiles_across"]["rect"][2] == DR.PHONE_VIEWPORT[0]
      and ph["probes"]["tiles_across"]["kind"] == ph["probes"]["tiles_anywhere"]["kind"] == "shown",
      repr(ph.get("probes")))
check("a clean report comes back checked:True ok:True", res.get("checked") and res.get("ok"), repr(res))
check("...and says the phone passed too", (res.get("phone") or {}).get("ok") is True
      and "on a phone" in (res.get("why") or ""), repr(res.get("phone")))

# The phone look's answer joins the verdict.
_calls.clear()
_calls["answer"] = (200, _report)
_real_look = DR._look
_phone_answers: list = []


async def _scripted_look(payload, **kw):
    if (payload.get("width"), payload.get("height")) == DR.PHONE_VIEWPORT:
        return _phone_answers.pop(0)
    return await _real_look(payload, **kw)


DR._look = _scripted_look
_phone_answers.append((None, {"checked": False, "reason": "the browser went away"}))
res = run(DR.render_check(object(), TEMPLATE_HTML))
check("a phone size the browser could not answer leaves the desktop pass standing, and says so",
      res.get("checked") and res.get("ok") and (res.get("phone") or {}).get("checked") is False
      and any("phone size" in w and "not checked" in w and "went away" in w
              for w in res.get("warnings") or []), repr(res)[:400])
_phone_answers.append(({"checked": True, "ok": False, "why": "it shows nothing — …",
                        "failures": ["it shows nothing — …"], "errors": []}, {}))
res = run(DR.render_check(object(), TEMPLATE_HTML))
check("a phone failure fails the document, every failure named 'on a phone'",
      res.get("checked") and res.get("ok") is False
      and res.get("failures") == [f"on a phone ({DR.PHONE_VIEWPORT[0]}×{DR.PHONE_VIEWPORT[1]}): "
                                  "it shows nothing — …"]
      and res.get("why") == res["failures"][0], repr(res)[:400])
check("...and that failure is REMEMBERED for these bytes, like any other",
      (DR.known_failure(TEMPLATE_HTML) or {}).get("why") == res.get("why"))
DR._forget(TEMPLATE_HTML)
_phone_answers.append(({"checked": True, "ok": True, "why": "rendered"},
                       {"probes": {"tiles_anywhere": 15, "tiles_across": 7}}))
res = run(DR.render_check(object(), TEMPLATE_HTML))
check("tiles past the side edges of the phone screen PASS with a warning that counts them",
      res.get("ok") is True and len(res.get("warnings") or []) == 1
      and "8 of the 15 app tiles sit past the screen's side edges" in res["warnings"][0],
      repr(res.get("warnings")))
check("...the warning is in the text the Host reads and in the summary a door returns",
      res["warnings"][0] in DR.format_render(res) and DR.summary(res).get("warnings") == res["warnings"])
DR._look = _real_look
_calls.clear()
_calls["answer"] = (200, _report)
run(DR.render_check(object(), TEMPLATE_HTML))
check("the browser this check woke LINGERS for the edit-burst window instead of a release "
      "per check (harness_backend.linger)",
      _calls.get("lingered") == ("browser", DR.LINGER_S) and "released" not in _calls,
      repr(_calls.get("lingered")))

# K2. a document containing </script> cannot close the script it rides in
evil = TEMPLATE_HTML.replace("</body>", "<script>1</script></body>")
page = DR.compose_page(evil, apps=[], host="")
check("the document rides as JSON that cannot close the mounting <script>",
      page.lower().count("</script>") == 3, str(page.lower().count("</script>")))

# K3. not checked, three ways — never a pass
HB.ensure_awake = _wake_refused
res = run(DR.render_check(object(), TEMPLATE_HTML))
check("a refused wake is checked:False with the reason", res.get("checked") is False
      and "refused" in (res.get("reason") or ""), repr(res))
check("...and carries no ok at all", "ok" not in res)
# A FULL PLAN is named, with the operator's numbers (harness_backend.wake_refusal
# reads the 402 the bridge relays), so the person can act on it.
HB._state.setdefault("browser", {})["wake_error"] = (
    'operator /harness/wake 402: {"detail":{"error":"capability_over_plan",'
    '"harness":"browser","needs_mi":1024,"headroom_mi":32,"upgrade_to":"pro",'
    '"pool_add_mi":null}}')
res = run(DR.render_check(object(), TEMPLATE_HTML))
check("a wake the PLAN refused says so, with the numbers",
      res.get("checked") is False and res.get("over_plan", {}).get("needs_mi") == 1024
      and "no room for the browser right now" in (res.get("reason") or "")
      and "1024 MiB" in res["reason"] and "32 MiB" in res["reason"], repr(res))
HB._state["browser"].pop("wake_error", None)
res = run(DR.render_check(object(), TEMPLATE_HTML))
check("...and a wake refused for any other reason is not dressed up as a full plan",
      "over_plan" not in res and "no room" not in (res.get("reason") or ""), repr(res))
HB.ensure_awake = _wake_ok
_calls["answer"] = (404, {"detail": "Not Found"})
res = run(DR.render_check(object(), TEMPLATE_HTML))
check("an OLDER sidecar (no route: 404) is checked:False and says the sidecar predates the check",
      res.get("checked") is False and "predates" in (res.get("reason") or ""), repr(res))
_calls["answer"] = (200, {"something": "else"})
res = run(DR.render_check(object(), TEMPLATE_HTML))
check("a 200 with no render report is checked:False, not a pass",
      res.get("checked") is False, repr(res))

# K4. judge: a report that shows the CHECK broke is not blamed on the document
res = DR.judge({"loaded": True, "subject_found": True, "ready": True, "uncaught": [],
                "paint": None, "paint_error": "screenshot timed out", "probes": {}},
               html=TEMPLATE_HTML, expect=DR.expectations(TEMPLATE_HTML), apps_handed=2)
check("a frame the browser could not capture is checked:False", res.get("checked") is False)
res = DR.judge({"loaded": True, "subject_found": True, "ready": True,
                "uncaught": [{"message": "boom", "url": "about:srcdoc", "line": 7, "column": 3}],
                "paint": None, "paint_error": "screenshot timed out", "probes": {}},
               html=TEMPLATE_HTML, expect=DR.expectations(TEMPLATE_HTML), apps_handed=2)
check("...but an error it DID see still fails the document — never downgraded to 'not checked'",
      res.get("checked") is True and res.get("ok") is False and "boom" in res.get("why", ""), repr(res))
line1 = DR.judge({"loaded": True, "subject_found": True, "ready": True,
                  "uncaught": [{"message": "x", "url": "about:srcdoc", "line": 1, "column": 900}],
                  "paint": {"painted_share": 0.5}, "probes": {}},
                 html=TEMPLATE_HTML, expect={"mode": None, "bar": None}, apps_handed=0)
check("an error on the doctype line keeps its line but drops the column the injection shifted",
      line1["errors"][0].get("line") == 1 and "column" not in line1["errors"][0], repr(line1["errors"]))

if _real_aiohttp is not None:
    sys.modules["aiohttp"] = _real_aiohttp
else:
    sys.modules.pop("aiohttp", None)


# ═══════════════════════════════════════════════════════════════════════════
# 2. DOORS — each one, with the check's answer scripted
# ═══════════════════════════════════════════════════════════════════════════
import server.routes.appearance as A  # noqa: E402
from server.routes import designs as D  # noqa: E402

_render: dict = {"answer": OK, "calls": 0}


async def _fake_render_check(pool, html, **_kw):
    _render["calls"] += 1
    _render["html"] = html
    return dict(_render["answer"])


_REAL_RENDER_CHECK = DR.render_check
DR.render_check = _fake_render_check


async def _no_ids(_pool):
    return frozenset()


class _Conn:
    def __init__(self, pool):
        self.pool = pool

    async def execute(self, sql, *a):
        self.pool.writes.append(sql)

    async def fetchrow(self, sql, *a):
        return {"wallpaper": "default"}


class _Acquire:
    def __init__(self, pool):
        self.pool = pool

    async def __aenter__(self):
        return _Conn(self.pool)

    async def __aexit__(self, *a):
        return False


class _Pool:
    def __init__(self):
        self.writes: list[str] = []

    def acquire(self):
        return _Acquire(self)

    async def fetchval(self, sql, *a):
        return "Nova"


# ── publish_document ────────────────────────────────────────────────────────
_bridge_sent: list = []


async def _bridge(op, params):
    _bridge_sent.append(op)
    if op == "design.get":
        return {"name": "Samhain", "html": TEMPLATE_HTML, "author_name": "Nova"}
    return {"published": True, "design_id": 9, "shelf": "account"}


D._bridge = _bridge
D._installed_ids = _no_ids

_render.update(answer=UNCHECKED, calls=0)
_bridge_sent.clear()
try:
    run(D.publish_document(_Pool(), "Samhain", TEMPLATE_HTML))
    code, detail = None, ""
except HTTPException as e:
    code, detail = e.status_code, str(e.detail)
check("PUBLISH: the browser could not be asked -> REFUSED 503, not published",
      code == 503 and "design.publish" not in _bridge_sent, f"{code} {_bridge_sent}")
check("...and the refusal says so plainly (NOT RENDER-CHECKED, nothing was published, why)",
      "NOT RENDER-CHECKED" in detail and "Nothing was published" in detail
      and "refused" in detail, detail[:200])

_render.update(answer={"checked": False,
                       "reason": "the plan has no room for the browser right now (it needs "
                                 "1024 MiB and 32 MiB are free)",
                       "over_plan": {"needs_mi": 1024, "headroom_mi": 32,
                                     "upgrade_to": "pro", "pool_add_mi": None}}, calls=0)
_bridge_sent.clear()
try:
    run(D.publish_document(_Pool(), "Samhain", TEMPLATE_HTML))
    code, detail = None, ""
except HTTPException as e:
    code, detail = e.status_code, str(e.detail)
check("PUBLISH on a FULL PLAN: still refused (503), not published",
      code == 503 and "design.publish" not in _bridge_sent, f"{code} {_bridge_sent}")
check("...and the refusal says exactly why: no room for the browser right now, with the numbers",
      "no room for the browser right now" in detail and "1024 MiB" in detail, detail[:300])
check("...and what frees room — and that the larger plan has it — without changing anything",
      "Room comes back when" in detail and "coding engine" in detail
      and "Browser app being closed" in detail and "The pro plan has room for it" in detail
      and "Then publish again" in detail, detail[-400:])

_render.update(answer={"checked": False, "outdated": True,
                       "reason": "browser render check answered HTTP 404 — this browser "
                                 "sidecar predates the desktop render check"}, calls=0)
_bridge_sent.clear()
try:
    run(D.publish_document(_Pool(), "Samhain", TEMPLATE_HTML))
    code, detail = None, ""
except HTTPException as e:
    code, detail = e.status_code, str(e.detail)
check("PUBLISH against an OLDER browser sidecar: refused (503), not published, and says why",
      code == 503 and "design.publish" not in _bridge_sent and "predates" in detail
      and "older than the check" in detail, detail[-300:])

_render.update(answer=FAILED, calls=0)
_bridge_sent.clear()
try:
    run(D.publish_document(_Pool(), "Samhain", TEMPLATE_HTML))
    code, detail = None, ""
except HTTPException as e:
    code, detail = e.status_code, str(e.detail)
check("PUBLISH: a failed render -> 422 with the error and its line, not published",
      code == 422 and "line 196" in detail and "design.publish" not in _bridge_sent, detail[:200])

_render.update(answer=OK, calls=0)
_bridge_sent.clear()
out = run(D.publish_document(_Pool(), "Samhain", TEMPLATE_HTML))
check("PUBLISH: a clean render publishes, and the result says it was rendered",
      _bridge_sent == ["design.publish"] and out.get("render", {}).get("ok") is True, repr(out))

_render.update(answer=OK, calls=0)
try:
    run(D.publish_document(_Pool(), "Stub", "<html><body>no mode, no tokens</body></html>"))
except HTTPException:
    pass
check("PUBLISH: the text floor runs FIRST — a floor refusal never wakes a browser",
      _render["calls"] == 0)

# A design that FAILED is never offered "publish again once a browser can be
# started": with no browser now, its bytes are still the failure they were.
# The real render_check (its memory of failed bytes), the browser scripted.
DR.render_check = _REAL_RENDER_CHECK
_browser_says = {"answer": FAILED}


async def _scripted_browser(pool, html, **_kw):
    return dict(_browser_says["answer"])


DR._render_check = _scripted_browser


def _publish(html):
    _bridge_sent.clear()
    try:
        run(D.publish_document(_Pool(), "Samhain", html))
        return None, ""
    except HTTPException as e:
        return e.status_code, str(e.detail)


code, _ = _publish(TEMPLATE_HTML)
_browser_says["answer"] = UNCHECKED
code2, detail = _publish(TEMPLATE_HTML)
check("PUBLISH a design that FAILED, now with no browser: still REFUSED as failed (422), "
      "never 'publish again once a browser can be started'",
      code == 422 and code2 == 422 and "FAILED the render check" in detail
      and "stays refused until a browser passes it" in detail
      and "once a browser can be started" not in detail
      and "design.publish" not in _bridge_sent, f"{code} {code2} {detail[:300]}")
other = TEMPLATE_HTML.replace("</body>", "<!-- another version -->\n</body>", 1)
code3, detail3 = _publish(other)
check("...while bytes that never failed, with no browser, are the plain 503 (publish again later)",
      code3 == 503 and "once a browser can be started" in detail3, f"{code3} {detail3[-200:]}")
_browser_says["answer"] = OK
code4, _ = _publish(TEMPLATE_HTML)
check("...and once a browser PASSES the same bytes, they publish (the memory is cleared)",
      code4 is None and _bridge_sent == ["design.publish"] and DR.known_failure(TEMPLATE_HTML) is None,
      f"{code4} {_bridge_sent}")
DR.render_check = _fake_render_check

# ── set_desktop_artifact ────────────────────────────────────────────────────
_doc = {"html": TEMPLATE_HTML}


async def _render_art(pool, eid, aid, entry="index.html"):
    return _doc["html"]


A.artifact_service = types.SimpleNamespace(render=_render_art)
A._installed_app_ids = _no_ids


async def _quiet(*a, **k):
    return False


A._notify_shell = _quiet
A.publish_desktop_snapshot = _quiet
_real_path = A.Path


class _AlwaysFile(_real_path):
    def is_file(self):
        return True


A.Path = _AlwaysFile

_render.update(answer=FAILED, calls=0)
pool = _Pool()
try:
    run(A.set_desktop_artifact(pool, 9003, 236))
    err = None
except ArtifactError as e:
    err = e
check("SET-DESKTOP: a failed render -> 422 with the error and its line",
      err is not None and err.status == 422 and "line 196" in str(err.detail), repr(err))
check("...and nothing was written — the desktop did not change", pool.writes == [])
check("...the check ran on the RENDERED artifact (the exact bytes the shell mounts)",
      _render.get("html") == TEMPLATE_HTML)

_render.update(answer=UNCHECKED, calls=0)
pool = _Pool()
out = run(A.set_desktop_artifact(pool, 9003, 236))
check("SET-DESKTOP: the browser could not be asked -> it PROCEEDS (the user's own screen)",
      pool.writes and out.get("desktop_artifact_id") == 236, repr(out)[:200])
check("...and says so — a NOT RENDER-CHECKED warning and render.checked False, never silent",
      any("NOT RENDER-CHECKED" in w for w in out.get("warnings") or [])
      and out.get("render") == {"checked": False, "reason": UNCHECKED["reason"]}, repr(out)[:300])

_render.update(answer=OK, calls=0)
_set_pool = _Pool()
out = run(A.set_desktop_artifact(_set_pool, 9003, 236))
check("SET-DESKTOP: a clean render sets it and reports render.ok",
      out.get("render", {}).get("ok") is True and _render["calls"] == 1)
_upsert = [w for w in _set_pool.writes if "desktop_artifact_id = EXCLUDED" in w]
check("...and the PHONE follows it: the phone variant goes back to 'Same as desktop' (NULL)",
      len(_upsert) == 1 and "wallpaper_mobile    = NULL" in _upsert[0], repr(_set_pool.writes)[:300])
_render.update(answer={**OK, "warnings": ["on a phone (360×640) 8 of the 15 app tiles sit past "
                                          "the screen's side edges"]}, calls=0)
out = run(A.set_desktop_artifact(_Pool(), 9003, 236))
check("SET-DESKTOP: a pass with phone warnings is set, and the warnings are in the result the "
      "Host reads", out.get("desktop_artifact_id") == 236
      and any("past the screen's side edges" in w for w in out.get("warnings") or [])
      and (out.get("render") or {}).get("warnings"), repr(out)[:300])
_render.update(answer=OK, calls=0)
run(A.set_desktop_artifact(_Pool(), 9003, 236))
from server.applications import desktop_pin as PIN  # noqa: E402
_pin = PIN.read_pin(236) or {}
check("...and WEARS what it checked: the pin is those exact bytes, render-checked",
      _pin.get("digest") == PIN.digest(TEMPLATE_HTML) and _pin.get("checked") is True
      and _pin.get("origin") == "set" and PIN.pinned_html(236, "index.html") == TEMPLATE_HTML,
      repr(_pin))

_render.update(answer=FAILED, calls=0)
out = run(A.set_desktop_artifact(_Pool(), 9003, 236, render_check=False))
check("SET-DESKTOP render_check=False (going back) does not load a browser at all",
      _render["calls"] == 0 and "render" not in out)
out = run(A.set_desktop_artifact(_Pool(), 9003, 236, render_check=dict(OK)))
check("SET-DESKTOP with a result in hand uses it instead of loading the same bytes twice",
      _render["calls"] == 0 and out.get("render", {}).get("ok") is True)
try:
    run(A.set_desktop_artifact(_Pool(), 9003, 236, render_check=dict(FAILED)))
    refused = False
except ArtifactError as e:
    refused = e.status == 422
check("...and a FAILED result in hand still refuses", refused)

_doc["html"] = "<html><body>no mode, no tokens</body></html>"
_render.update(answer=OK, calls=0)
try:
    run(A.set_desktop_artifact(_Pool(), 9003, 236))
except ArtifactError:
    pass
check("SET-DESKTOP: the text floor runs FIRST — a floor refusal never wakes a browser",
      _render["calls"] == 0)
_doc["html"] = TEMPLATE_HTML

# A version the shell's reload report took off (desktop_pin.take_off_reloading)
# is not a FAILED one: only the shell saw the loop, and any page framed on the
# desktop's origin can send that report. Setting it again runs the normal
# check; with no browser it is worn with a note, never refused as "reloads
# itself over and over". The real render_check, the browser scripted.
DR.render_check = _REAL_RENDER_CHECK
DR._render_check = _scripted_browser
_reported = TEMPLATE_HTML.replace("</body>", "<!-- reported looping -->\n</body>", 1)
_doc["html"] = _reported
DR.remember_reload_report(_reported, "the shell counted more than 3 loads in 45 s")
_browser_says["answer"] = UNCHECKED
try:
    out = run(A.set_desktop_artifact(_Pool(), 9003, 236))
    err = None
except ArtifactError as e:
    out, err = {}, e
check("SET-DESKTOP a version taken off on a reload report, no browser: it PROCEEDS, not refused",
      err is None and out.get("desktop_artifact_id") == 236, repr(err)[:300])
check("...with a note naming the unconfirmed reload report",
      any("NOT RENDER-CHECKED" in w and "reload report (unconfirmed" in w
          for w in out.get("warnings") or [])
      and (out.get("render") or {}).get("reload_report", {}).get("unconfirmed") is True,
      repr(out)[:400])
check("...and it is not recorded as failed", DR.known_failure(_reported) is None)
DR.remember_reload_report(_reported, "the shell counted more than 3 loads in 45 s")
_browser_says["answer"] = OK
out = run(A.set_desktop_artifact(_Pool(), 9003, 236))
check("SET-DESKTOP the same version with a browser: the browser's verdict decides (a pass sets it)",
      (out.get("render") or {}).get("ok") is True and "reload_report" not in (out.get("render") or {}),
      repr(out)[:300])
DR.render_check = _fake_render_check
_doc["html"] = TEMPLATE_HTML

# The Artifacts window's cheap "can this be a desktop?" read stays cheap.
_render.update(answer=FAILED, calls=0)
A.brain_service = types.SimpleNamespace(get_pool=lambda: _Pool(), _entity_id=9003)
got = run(A.check_desktop_candidate(artifact_id=236))
check("GET /desktop/check stays the text floor only — it never wakes a browser",
      _render["calls"] == 0 and got.get("ok") is True)
A.brain_service = brain_service

# ── the Designs doors ───────────────────────────────────────────────────────
_set_calls: list = []


async def _set_desktop(pool, eid, aid, entry="index.html", *, render_check=True):
    _set_calls.append({"aid": aid, "render_check": render_check})
    return {"wallpaper": "html", "desktop_artifact_id": aid}


A.set_desktop_artifact = _set_desktop
_created: list = []


async def _create_artifact(pool, eid, name, files, description=""):
    _created.append(name)
    return {"artifact_id": 777}


D.artifact_service.create_artifact = _create_artifact
D._mark_trial = lambda *a, **k: None


async def _wearing(_pool):
    return {"wallpaper": "default", "artifact_id": None, "entry": None}


async def _discard(*a, **k):
    return False


D._wearing = _wearing
D._discard_trial = _discard
D._pool = lambda: _Pool()


class _Req:
    async def json(self):
        return {}


_render.update(answer=FAILED, calls=0)
_created.clear()
_set_calls.clear()
try:
    run(D.install_design(9, _Req()))
    code, detail = None, ""
except HTTPException as e:
    code, detail = e.status_code, str(e.detail)
check("INSTALL (Try): a failed render -> 422 with the error and its line",
      code == 422 and "line 196" in detail, f"{code} {detail[:120]}")
check("...refused BEFORE the copy — no orphan artifact, desktop untouched",
      _created == [] and _set_calls == [])

_render.update(answer=UNCHECKED, calls=0)
_created.clear()
_set_calls.clear()
out = run(D.install_design(9, _Req()))
check("INSTALL: the browser could not be asked -> it proceeds (a wear), the result handed on",
      _created and _set_calls and _set_calls[0]["render_check"] == UNCHECKED, repr(_set_calls))
check("...so the same bytes are loaded ONCE per try, not twice", _render["calls"] == 1)

_render.update(answer=OK, calls=0)
_set_calls.clear()
run(D.wear_local(236, _Req()))
check("WEAR (one of this Host's own): goes through set_desktop_artifact, checked",
      _set_calls and _set_calls[0]["render_check"] is True, repr(_set_calls))

_set_calls.clear()
run(D._restore(_Pool(), {"wallpaper": "html", "artifact_id": 236, "entry": "index.html"}))
check("GO BACK: putting the previous desktop back never waits on (or is refused by) a browser",
      _set_calls and _set_calls[0]["render_check"] is False, repr(_set_calls))

A.Path = _real_path

print()
if _FAILURES:
    print(f"[render-doors] {len(_FAILURES)} FAILED")
    sys.exit(1)
print("[render-doors] ALL OK")
