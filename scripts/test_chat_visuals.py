#!/usr/bin/env python3
"""Gate: VISUALS and artifact cards in the Circles and Workbench chats.

A visual is a page the Host draws in its reply — a ```visual fence — run in
the sealed frame from app-web-kit/visuals (sandbox="allow-scripts" + the
shared CSP from /js/sandbox-csp.js). It is never saved unless the user clicks
(nothing is saved automatically; the usual save/export options). An
artifact a turn made or changed shows as a live thumbnail card.

Driven against the BUILT apps (system-apps/circles/, system-apps/workbench/)
over a loopback mock kernel that serves the real /js/sandbox-csp.js and
/js/desktop-preview.js, with Circles rows in the shapes the server really
sends (routes/circles.py: a self-thread reply is author_kind "entity" + the
Host's name — exactly a peer host's shape, a bug once caught here).
A second loopback port is the outside world: anything it receives leaked.

  BOTH
  B. each app BOOTS (Circles, the cards, on your card's back: your own Host's
     original conversation; the Workbench with its projects listed). The
     mock answers every route it does not
     model with {}; a boot read answered that way left Circles on a blank
     pane once, so a failure here names those routes and stops;
  CIRCLES
  V. the own Host's saved visual RUNS (its inline script ran) and wears the
     desktop (its background is the served --cseries-ink);
  O. it runs at an OPAQUE origin (self.origin is "null"), in both apps —
     measured from inside the frame, so a sandbox widened to
     allow-same-origin by any route (attribute, ref, prop spread) goes red;
  S. SEALED: the same visual's fetch, image and external script never reach
     the outside world, and the block names what was not loaded;
  L. a visual OPENING WITH A NO-BREAK SPACE is sealed too (a
     CSP bypass: /^\\s*<!DOCTYPE/ put the policy in <body>);
  H. a plain ```html block stays a code listing — no frame;
  A. an artifact write chip (persisted digest.artifact_id) shows ONE card,
     its thumbnail is the artifact, and Open asks for the Artifacts window;
  K. Save as artifact posts once, named from the visual's title, and turns
     into "Saved — open"; Download offers <title>.html;
  T. while the reply streams, the open fence is a placeholder; when the
     fence closes it runs, and still runs once the saved row replaces the
     streamed echo;
  P. a PEER's visual is shown as code on its card's back, its artifact chip
     makes no card, and no frame ever mounts with the peer's page — not even
     for the one commit a card switch renders with the old rows;
  WORKBENCH
  W. a history turn's visual runs, sealed (nothing reaches the probe), and a
     reloaded artifact chip (mapPersistedToolChip keeps digest) shows a card;
  CONTROLS
  X. MUTATION: with a policy that seals nothing served in place of
     sandbox-csp.js, the same visual DOES reach the probe — S and L would go
     red if the frame stopped sealing;
  Z. with sandbox-csp.js missing, both apps FAIL CLOSED: no visual frame, and
     the block says the policy did not load.

Needs Playwright + Chromium, fastapi + uvicorn, and the built apps (`npm run
ship` in circles-web/ and workbench-web/) — no pod, no DB. Run:
  python3 scripts/test_chat_visuals.py
"""
from __future__ import annotations

import http.server
import json
import os
import socket
import sys
import threading
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(os.environ.get("APP_ROOT") or Path(__file__).resolve().parents[1])
JS = ROOT / "dashboard" / "base" / "js"

fails = 0


def check(name, ok, detail=""):
    global fails
    print(("  ok    " if ok else "  FAIL  ") + name + (f"  — {detail}" if detail and not ok else ""))
    if not ok:
        fails += 1


# ── the outside world ────────────────────────────────────────────────────────
PROBE_HITS: list = []


class _Probe(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        PROBE_HITS.append(self.path)
        body = b"window.__probe_ran = 1;" if ".js" in self.path else b""
        self.send_response(200)
        self.send_header("Content-Type", "text/javascript" if body else "image/png")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    do_POST = do_GET

    def log_message(self, *a):
        pass


def _probe_port() -> int:
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Probe)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv.server_address[1]


PROBE = _probe_port()


def leaky(tag: str, title: str, lead: str = "") -> str:
    """A visual that tries to reach the outside world three ways, tagged."""
    u = f"http://127.0.0.1:{PROBE}/{tag}"
    return (lead + f"<!DOCTYPE html><html><head><title>{title}</title></head><body>"
            f"<p id='msg'>did-not-run</p><p id='origin'></p><script src='{u}/script.js'></script><img src='{u}/img.png' alt=''>"
            "<script>document.getElementById('msg').textContent='script ran';"
            # The frame's own origin: "null" only while the sandbox withholds
            # allow-same-origin (checked as O below).
            "document.getElementById('origin').textContent=String(self.origin);"
            f"try{{fetch('{u}/fetch').catch(function(){{}})}}catch(e){{}}</script></body></html>")


def leaked(tag: str) -> list:
    return [h for h in PROBE_HITS if h.startswith(f"/{tag}/")]


INK = "#123456"           # the served --cseries-ink: what a visual's background must be
TOKENS = f":root{{--cseries-ink:{INK};--cseries-paper:#fedcba}}"
ART = ("<!DOCTYPE html><html><head><title>Pricing mockup</title></head><body style='margin:0'>"
       "<div id='art' style='height:800px;background:#14a;color:#fff'>PRICING MOCKUP</div></body></html>")
STREAMED = ('Streaming one:\n\n```visual title="Live one"\n<div id=a style="padding:12px">streamed page</div>'
            "\n```\n\nDone.")


def fence(html: str, title: str | None = None) -> str:
    return "```visual" + (f' title="{title}"' if title else "") + "\n" + html + "\n```\n"


# ── the mock kernel ──────────────────────────────────────────────────────────
from fastapi import FastAPI, Request  # noqa: E402
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response, StreamingResponse  # noqa: E402
import uvicorn  # noqa: E402
from gate_loopback import IDLE_KEEP_ALIVE_S  # noqa: E402

POLICY = {"mode": "real"}
MUTANT = ("window.CSeriesSandbox={CSP:'',META:'',withSandboxCsp:function(h){return String(h)},"
          "externalRefs:function(){return []}};")
STATE = {"sent": 0}
POSTS: list = []
app = FastAPI()


def circles_self_rows():
    rows = [
        {"id": 1, "author_name": "You", "author_kind": "self", "body": "show me something",
         "created_at": "2026-10-03T20:00:00Z"},
        {"id": 2, "author_name": "Nova", "author_kind": "entity",
         "body": "Here:\n\n" + fence(leaky("c-own", "Orbit demo"), "Orbit demo")
                 + "\nAnd source stays code:\n\n```html\n<p>just source</p>\n```\n",
         "created_at": "2026-10-03T20:00:05Z",
         "tool_calls": [{"tool": "operate", "operate_tool": "artifact", "op": "write", "status": "ok",
                         "args_summary": "", "digest": {"artifact_id": 41}}]},
        {"id": 3, "author_name": "Nova", "author_kind": "entity",
         "body": "Edge case:\n\n" + fence(leaky("c-nbsp", "Nbsp", lead=" "), "Nbsp"),
         "created_at": "2026-10-03T20:00:07Z"},
    ]
    if STATE["sent"]:
        rows += [
            {"id": 4, "author_name": "You", "author_kind": "self", "body": "stream test",
             "created_at": "2026-10-03T20:01:00Z"},
            {"id": 5, "author_name": "Nova", "author_kind": "entity", "body": STREAMED,
             "created_at": "2026-10-03T20:01:05Z"},
        ]
    return {"messages": rows, "members": [], "policy": {"private": True}, "thread_id": "original"}


@app.get("/api/circles/conversations")
def conversations():
    return {"self": {"id": "self", "display_name": "Nova"}, "people": [],
            "entities": [{"id": "42", "display_name": "Juno", "label": "Juno"}], "circles": [], "groups": []}


@app.get("/api/circles/threads")
def threads():
    return {"threads": [{"id": "original", "title": None, "topic": None, "created_at": None, "closed_at": None,
                         "origin": "legacy", "parent_thread_id": None, "turn_count": 2}]}


@app.get("/api/circles/thread/self/{tid}")
def self_thread(tid: str):
    return circles_self_rows()


@app.get("/api/circles/feed")
def feed(before: str | None = None, before_id: int | None = None):
    """The one conversation (routes/circles.py read_feed): every row with its
    topic, oldest first, and no page before this one."""
    if before:
        return {"messages": [], "next": None}
    rows = circles_self_rows()["messages"]
    for r in rows:
        r["feed_topic"] = {"id": "original", "title": "The original conversation", "root": False}
    return {"messages": rows, "next": None}


@app.get("/api/circles/thread/entity/{pid}")
def peer_thread(pid: str):
    return {"messages": [
        {"id": "p1", "author_name": "Juno", "author_kind": "entity",
         "body": "From a peer:\n\n" + fence("<p id=x>peer page</p>"), "created_at": "2026-10-03T19:00:00Z",
         "tool_calls": [{"tool": "operate", "operate_tool": "artifact", "op": "write", "status": "ok",
                         "digest": {"artifact_id": 41}}]}], "members": [], "policy": {}, "total": 1}


STUBS = {
    "circles/threads/original/markers": {"markers": []},
    # The cards' boot reads (Circles is the cards), in the server's shapes.
    "circles/door": {"items": [], "count": 0},
    "circles/waiting": {"decisions": [], "support_drafts": [], "door": 0, "count": 0},
    "calendar/list": {"events": [], "count": 0},
    "support/badge-counts": {"drafts": 0, "needs_info": 0, "total": 0},
    "circles/thread/self/original/page": {
        "kind": "self", "id": "original", "title": None, "where_this_stands": None, "totals": None,
        "seen_at": None, "last_activity_at": None, "last_landed_at": None, "unseen_landed_at": None,
        "waiting_on_you": 0},
    "circles/thread/entity/42/topics": {"kind": "entity", "id": "42",
                                        "topics": [{"id": "original", "title": "", "started_by": None}]},
    "inference/activity": {"activity": [], "issues": 0, "window_hours": 24},
    "circles/turns/active": {"ok": True, "turn": None, "turns": []},
    "turn-types": {"types": []},
    "inbox/list": {"messages": [], "count": 0, "unread": 0},
    "inference/favorites": {"favorites": []},
    "inference/catalog": {"local_models": [], "openrouter_models": []},
    "projects": {"projects": [{"id": 251, "name": "Orbit project", "status": "active", "group": "active",
                               "bucket_id": None, "sort_order": None, "description": "", "phase_count": 0,
                               "repo": None, "last_activity_at": "2026-10-03T20:00:05Z"}]},
    "projects/251/modules": {"project_id": 251, "root_app": None, "modules": []},
    "projects/251/phases": {"phases": []},
    "projects/buckets": {"buckets": [], "unfiled": {"counts": {}}},
    "applications": {"applications": [{"id": "circles", "name": "Circles"}]},
    "agents": {"agents": []},
    "agents/scheduled": {"scheduled": []},
    "review/sessions": {"sessions": []},
    "compute/runs": {"runs": []},
    # Boot reads, in the server's shapes. The catch-all's {} is a shape the
    # server never sends for these, and on it Circles opened no conversation:
    # with no `ref` the pane waited for the last-open read forever, and with
    # no `efforts` it threw on its first render. Check V failed from the first while this gate
    # ran nowhere. Nothing stored: server/ui_last_open.py read(); a model that
    # takes no effort level: server/routes/inference.py effort_options().
    "circles/last-open": {"surface": "circles", "kind": "thread", "ref": None},
    "projects/last-open": {"surface": "workbench", "kind": "project", "ref": None},
    "inference/effort-options": {"provider": "", "model": "", "efforts": [], "default": None, "known": True},
    # Your Host's one conversation, in
    # the server's shapes: the running internal threads (routes/circles.py
    # read_work: none run here) and the paired machines (list_bastion_directory).
    # The feed itself is the route below.
    "circles/work": {"threads": []},
    "circles/bastions": {"machines": []},
}
# Every /api route the catch-all answered with {}: named when an app fails to
# boot, since a new boot read answered that way is the usual cause.
CAUGHT: set = set()


@app.get("/api/brain/chat")
def wb_chat(app_id: str = "", project_id: int | None = None, order: str = "asc"):
    return {"turns": [
        {"id": 11, "role": "user", "content": "draw it", "created_at": "2026-10-03T20:00:00Z", "metadata": {}},
        {"id": 12, "role": "assistant", "created_at": "2026-10-03T20:00:05Z",
         "content": "Here:\n\n" + fence(leaky("w-own", "Bench demo"), "Bench demo"),
         "metadata": {"tool_calls": [{"tool": "artifact", "op": "write", "status": "ok",
                                      "args_summary": "", "digest": {"artifact_id": 41}}]}},
    ]}


@app.get("/api/artifacts/41/render")
def art():
    return HTMLResponse(ART)


@app.post("/api/artifacts")
async def save(request: Request):
    body = await request.json()
    POSTS.append(body)
    return {"artifact_id": 99, "name": body.get("name")}


@app.post("/api/circles/messages")
def send():
    STATE["sent"] = 1

    def gen():
        yield "f:" + json.dumps({"messageId": "m1"}) + "\n"
        yield "0:" + json.dumps(STREAMED[:STREAMED.index("streamed page")]) + "\n"
        time.sleep(2.0)
        yield "0:" + json.dumps(STREAMED[STREAMED.index("streamed page"):]) + "\n"
        yield "e:" + json.dumps({"finishReason": "stop", "usage": {"promptTokens": 1, "completionTokens": 1}}) + "\n"
    return StreamingResponse(gen(), media_type="text/plain")


@app.get("/css/tokens.css")
def tokens():
    return Response(TOKENS, media_type="text/css")


@app.get("/js/sandbox-csp.js")
def policy():
    if POLICY["mode"] == "missing":
        return Response(status_code=404)
    if POLICY["mode"] == "mutant":
        return Response(MUTANT, media_type="text/javascript")
    return FileResponse(str(JS / "sandbox-csp.js"), media_type="text/javascript")


@app.get("/js/desktop-preview.js")
def preview():
    return FileResponse(str(JS / "desktop-preview.js"), media_type="text/javascript")


@app.api_route("/api/{rest:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
def other(rest: str):
    if rest in STUBS:
        return STUBS[rest]
    if rest.startswith("circles/thread-context/"):
        # A route an older server lacks: the depth bar stays out of the
        # header, as it does there.
        return JSONResponse({"detail": "Not Found"}, status_code=404)
    CAUGHT.add(rest)
    return JSONResponse({}, status_code=200)


@app.get("/system-apps/{app_id}/{rest:path}")
def static(app_id: str, rest: str):
    base = (ROOT / "system-apps" / app_id).resolve()
    target = (base / (rest or "index.html")).resolve()
    if app_id not in ("circles", "workbench") or base not in target.parents:
        return Response(status_code=403)
    return FileResponse(str(target)) if target.is_file() else Response(status_code=404)


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


# Every frame ever attached, with the start of its srcdoc — a frame that
# mounts for one commit and is gone the next is still seen.
FRAMES_INIT = """
window.__frames = []; window.__opened = [];
new MutationObserver(ms => { for (const m of ms) for (const n of m.addedNodes) {
  const all = n.nodeType === 1 ? [n, ...n.querySelectorAll('iframe')] : [];
  for (const f of all) if (f.tagName === 'IFRAME') window.__frames.push(f.getAttribute('srcdoc') || '');
}}).observe(document, {subtree: true, childList: true});
addEventListener('message', e => { if (e.data && e.data.type === 'cseries:open-app') window.__opened.push(e.data); });
"""


def visual(page, title):
    return page.locator('[data-lang="visual"]').filter(has_text=title).first


def frame_text(block, sel="#msg"):
    try:
        return block.frame_locator("iframe").locator(sel).inner_text(timeout=4000)
    except Exception:  # noqa: BLE001
        return None


def open_page(b, base, app_id):
    pg = b.new_page(viewport={"width": 1200, "height": 2600}, accept_downloads=True)
    pg.add_init_script(FRAMES_INIT)
    pg.goto(f"{base}/system-apps/{app_id}/index.html", wait_until="domcontentloaded")
    pg.wait_for_timeout(2500)
    return pg


def show_earlier(pg) -> int:
    """Circles opens your Host's conversation on a blank page; the saved rows
    are one tap up. Returns how many offers there were."""
    earlier = pg.locator("[data-earlier]")
    n = earlier.count()
    if n:
        earlier.first.click()
        pg.wait_for_timeout(2500)
    return n


def booted(name, ready) -> bool:
    """Check B: the app reached `ready`. Every check after it needs that, so a
    failure here stops the gate with the routes the mock answered {} instead
    of a 30 s locator timeout and a traceback."""
    try:
        ready.first.wait_for(timeout=8000)
        ok = True
    except Exception:  # noqa: BLE001
        ok = False
    check(name, ok, "routes the mock answered with {} (give the one this app reads at boot the server's "
                    "shape in STUBS): " + ", ".join(sorted(CAUGHT)))
    return ok


def main() -> int:
    for a in ("circles", "workbench"):
        if not (ROOT / "system-apps" / a / "assets").is_dir():
            print(f"FAIL — system-apps/{a} has no build; run `npm run ship` in {a}-web/")
            return 1
    base = f"http://127.0.0.1:{serve()}"
    with sync_playwright() as p:
        b = p.chromium.launch()

        # ── CIRCLES ──────────────────────────────────────────────────────────
        pg = open_page(b, base, "circles")
        # One conversation: the back opens
        # on a blank page with a greeting and the box names no topic; what was
        # said before is one tap up ("Earlier messages"), read from the feed.
        box = pg.get_by_placeholder("Write to Nova", exact=True)
        if not booted("B. Circles boots on your card's back, the own Host's conversation (its box is shown)", box):
            b.close()
            print(f"\nFAIL — chat visuals: {fails} failing (Circles did not boot)")
            return 1
        _n_earlier = show_earlier(pg)
        check("B. ...on a blank page that offers the earlier messages", _n_earlier >= 1, str(_n_earlier))
        own = visual(pg, "Orbit demo")
        check("V. the own Host's saved visual runs (its inline script ran)", frame_text(own) == "script ran")
        # Behavioural, so it holds however the sandbox gets set (attribute,
        # ref, spread): with allow-same-origin the frame would share the app's
        # origin, and its script could reach the parent's realm — and the
        # parent's fetch — where the frame's own CSP does not apply.
        o = frame_text(own, "#origin")
        check("O. ...at an opaque origin (the sandbox withholds allow-same-origin)", o == "null", str(o))
        try:
            bg = own.frame_locator("iframe").locator("html").evaluate(
                "e => getComputedStyle(e).backgroundColor", timeout=4000)
        except Exception as e:  # noqa: BLE001 — no frame: reported, and the rest still runs
            bg = f"no frame ({type(e).__name__})"
        check("V. ...and wears the desktop: its background is the served --cseries-ink", bg == "rgb(18, 52, 86)", bg)
        notice = own.inner_text()
        check("S. ...and the block names what was not loaded", f"127.0.0.1:{PROBE}/c-own" in notice, notice[:200])
        check("L. a visual opening with a no-break space still runs", frame_text(visual(pg, "Nbsp")) == "script ran")
        check("H. a plain ```html block stays a code listing — no frame",
              pg.get_by_text("<p>just source</p>").count() >= 1
              and not any("just source" in f for f in pg.evaluate("window.__frames")))

        cards = pg.get_by_role("button", name="Open", exact=True)
        check("A. the artifact write chip shows one card", cards.count() == 1, str(cards.count()))
        thumb = pg.locator("button[aria-label='Open Pricing mockup in Artifacts']")
        check("A. ...whose thumbnail is the artifact itself",
              thumb.count() == 1 and thumb.frame_locator("iframe").locator("#art").count() == 1)
        cards.first.click()
        pg.wait_for_timeout(200)
        check("A. ...and Open asks for the Artifacts window on it",
              {"type": "cseries:open-app", "app_id": "artifacts", "view": "artifact:41"} in pg.evaluate("window.__opened"))

        own.get_by_role("button", name="Save as artifact").click()
        pg.wait_for_timeout(600)
        check("K. Save as artifact posts once, named from the visual's title",
              len(POSTS) == 1 and POSTS[0].get("name") == "Orbit demo"
              and POSTS[0].get("files", [{}])[0].get("path") == "index.html", json.dumps(POSTS)[:200])
        check("K. ...and turns into Saved — open", own.get_by_role("button", name="Saved — open").count() == 1)
        with pg.expect_download() as dl:
            own.get_by_role("button", name="Download").click()
        check("K. Download offers <title>.html", dl.value.suggested_filename == "orbit-demo.html",
              dl.value.suggested_filename)

        box.fill("stream test")
        pg.keyboard.press("Enter")
        pg.wait_for_timeout(900)
        check("T. while the fence is open the visual is a placeholder",
              pg.get_by_text("Drawing the visual “Live one”").count() == 1
              and not any("streamed page" in f for f in pg.evaluate("window.__frames")))
        pg.wait_for_timeout(5000)
        live = visual(pg, "Live one")
        check("T. when it closes it runs — and still does once the saved row replaced the echo",
              pg.get_by_text("Done.", exact=True).count() >= 1 and frame_text(live, "#a") == "streamed page"
              and pg.get_by_text("shown as code").count() == 0)

        pg.locator("[data-card='host:42']").click()
        pg.wait_for_timeout(2000)
        peer = pg.locator("[data-face-side='back'] [data-peer-topic]")
        check("P. a peer's visual is shown as code, on its card's back",
              "peer page" in (peer.inner_text() if peer.count() else "")
              and peer.locator('[data-lang="visual"]').count() == 0 and peer.get_by_text("shown as code").count() == 1)
        check("P. ...its artifact chip makes no card", peer.get_by_role("button", name="Open", exact=True).count() == 0)
        pg.locator("[data-card='you']").click()
        pg.wait_for_timeout(2500)
        check("P. ...and no frame ever mounted with the peer's page, thread switches included",
              not any("peer page" in f for f in pg.evaluate("window.__frames")))
        pg.close()

        # ── WORKBENCH ────────────────────────────────────────────────────────
        wb = open_page(b, base, "workbench")
        wb.keyboard.press("Escape")  # the "What would you like to work on?" start dialog
        if not booted("B. the Workbench boots with its projects listed",
                      wb.get_by_text("Orbit project", exact=True)):
            b.close()
            print(f"\nFAIL — chat visuals: {fails} failing (the Workbench did not boot)")
            return 1
        wb.get_by_text("Orbit project", exact=True).first.click()
        wb.wait_for_timeout(2500)
        wv = visual(wb, "Bench demo")
        check("W. a Workbench history turn's visual runs", frame_text(wv) == "script ran")
        o = frame_text(wv, "#origin")
        check("O. ...at an opaque origin (the sandbox withholds allow-same-origin)", o == "null", str(o))
        check("W. ...and a reloaded artifact chip shows a card",
              wb.locator("button[aria-label='Open Pricing mockup in Artifacts']").count() == 1)
        wb.close()

        time.sleep(1.0)
        for tag, label in (("c-own", "S. the own visual"), ("c-nbsp", "L. the no-break-space visual"),
                           ("w-own", "W. the Workbench visual")):
            check(f"{label} reached nothing outside (fetch, image, script)", not leaked(tag), str(leaked(tag)))

        # ── CONTROLS ─────────────────────────────────────────────────────────
        POLICY["mode"] = "mutant"
        PROBE_HITS.clear()
        pg = open_page(b, base, "circles")
        show_earlier(pg)
        pg.wait_for_timeout(1500)
        check("X. MUTATION: with a policy that seals nothing, the same visual DOES reach the probe",
              bool(leaked("c-own")) and bool(leaked("c-nbsp")), str(PROBE_HITS))
        pg.close()

        POLICY["mode"] = "missing"
        for a in ("circles", "workbench"):
            pg = open_page(b, base, a)
            if a == "circles":
                show_earlier(pg)
            if a == "workbench":
                pg.keyboard.press("Escape")
                pg.get_by_text("Orbit project", exact=True).first.click()
                pg.wait_for_timeout(2500)
            blocks = pg.locator('[data-lang="visual"]')
            check(f"Z. {a}: with sandbox-csp.js missing, no visual frame mounts and it says so",
                  blocks.count() >= 1 and blocks.locator("iframe").count() == 0
                  and blocks.first.locator("[data-sandbox-missing]").count() == 1
                  and "did not load" in blocks.first.inner_text())
            pg.close()
        POLICY["mode"] = "real"
        b.close()

    print(f"\n{'FAIL' if fails else 'ok'} — chat visuals: {fails} failing")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
