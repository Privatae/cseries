#!/usr/bin/env python3
"""Gate: the Workbench's door, the Start dialog, in the book of pages, in a real browser.

"What would you like to work on?" is the one door into the Workbench, and its
General thread is retired; the book of pages is the Workbench's one layout.
Driven against the BUILT app
(system-apps/workbench/index.html, the book) over a mock kernel API that
records what the UI sends:

  A. an empty book (no open pages: archived projects are not pages) shows its
     empty state with Start, and no dialog opens by itself; Start opens it;
     typing, "New project", the suggested title, Create → POST /api/projects
     carries the typed text as the description, and the book turns to the
     new page, its tab selected and noted "pending", the text in its
     composer;
  B. a book with pages opens on the page last open, else on the newest open
     page (by last activity, never an archived one), and no dialog opens;
  C. the retired General thread still holds the person's turns: the dialog
     opens by itself over the page, pre-filled with the thread's opening
     message (its first 400 characters) and saying how many messages move;
     creating a project from it sends adopt_thread {kind:'app',
     ref:'workbench'} and opens the new page with the text in its composer;
     the move leaves only its marker in the General thread, and a reload
     opens no dialog. With other pages to pick from, typing ranks a project
     under the box, and picking it moves the turns there (POST
     /api/projects/{id}/adopt-thread, the same body) and turns to that page
     with the text in its composer, creating nothing. An entity whose General
     thread is empty gets no dialog at boot.

Retired with the rail and the viewer: B's "Start → typing ranks a
project → picking opens it" on a book with pages, which has no Start button
(the Index finds and opens pages, scripts/test_workbench_book.py I; the
dialog's ranking and pick are checked in C, the one way it opens over
pages); D, the rail's buckets, status badges and status filter; E, the
viewer's search by text, status and bucket (the Index finds every page, open
and archived, by its words, I, and its chapters are the buckets, G).

Needs Playwright + Chromium (python3 -m playwright install chromium) and a
free local port — no pod, no DB, no network beyond loopback. Listed in
scripts/gates-infra-only.d/ for that reason. Run:

  python3 scripts/test_workbench_start_flow.py
"""
from __future__ import annotations

import asyncio
import os
import socket
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from playwright.sync_api import TimeoutError as PlaywrightTimeout, expect, sync_playwright

ROOT = Path(os.environ.get("APP_ROOT") or Path(__file__).resolve().parents[1])
APP_DIR = ROOT / "system-apps" / "workbench"
SHOTS = Path(os.environ.get("SHOTS_DIR", "/tmp/wb-start-flow"))
SHOTS.mkdir(parents=True, exist_ok=True)

fails = 0


def check(name, ok, detail=""):
    global fails
    print(("  ok    " if ok else "  FAIL  ") + name + (f"  — {detail}" if detail and not ok else ""))
    if not ok:
        fails += 1


# ── the mock kernel ──────────────────────────────────────────────────────────
from fastapi import FastAPI, Request  # noqa: E402
from fastapi.responses import FileResponse, JSONResponse, Response  # noqa: E402
import uvicorn  # noqa: E402
from gate_loopback import IDLE_KEEP_ALIVE_S  # noqa: E402

STATE: dict = {}
NOW = "2026-09-07T12:00:00Z"
# A project made here is the newest of all, as one just made is.
CREATED_AT = "2026-09-07T14:00:00Z"


def proj(i, name, status, description="", at=NOW):
    return {"id": i, "name": name, "status": status, "bucket_id": None,
            "description": description, "phase_count": 0, "repo": None,
            "last_activity_at": at}


def reset(projects=(), general_turns=(), last_open=None):
    STATE.clear()
    STATE.update({
        "projects": [dict(p) for p in projects],
        "general_turns": [dict(t) for t in general_turns],
        "last_open": last_open,
        "last_open_puts": [],
        "posts": [], "adopts": [], "searches": [],
        "next_id": 1000,
    })


def _group(status):
    return {"active": "active", "review": "active", "planning": "pending",
            "complete": "completed", "archived": "archived"}.get(status, "pending")


def _counts(rows):
    c = {"active": 0, "pending": 0, "completed": 0, "archived": 0, "total": 0}
    for p in rows:
        c[_group(p["status"])] += 1
        c["total"] += 1
    return c


def _move_general(pid: int, name: str) -> int:
    """adopt_thread_core (server/routes/projects.py), in memory: the General
    thread's turns move into the project and one marker turn stays behind in
    it, as the server writes it: an assistant row with source_kind
    'thread_marker'. Returns how many turns moved."""
    turns = STATE["general_turns"]
    markers = [t for t in turns if (t.get("metadata") or {}).get("source_kind") == "thread_marker"]
    moved = len(turns) - len(markers)
    STATE["general_turns"] = markers + [{
        "id": 9000 + pid, "role": "assistant", "created_at": NOW,
        "content": f"→ Moved {moved} messages into the project “{name}” — nothing was deleted; "
                   "the conversation continues in that project's thread.",
        "metadata": {"role": "assistant", "source_kind": "thread_marker",
                     "source_context": {"kind": "app", "ref": "workbench"}, "app_id": "workbench",
                     "marker": {"project_id": pid, "project_name": name, "moved_turns": moved}},
    }]
    return moved


app = FastAPI()


@app.get("/api/projects")
async def list_projects(include_archived: bool = False, limit: int = 50):
    # open_book_list_last holds the list until the book's two other boot reads
    # have reached the page; reset() lets go of it too.
    while STATE.get("hold_list"):
        await asyncio.sleep(0.02)
    rows = [dict(p, group=_group(p["status"])) for p in STATE["projects"]
            if include_archived or p["status"] != "archived"]
    return {"projects": rows[:limit]}


@app.get("/api/projects/last-open")
def get_last_open():
    # projects.py projects_last_open: the project last open, or null.
    return {"ref": STATE["last_open"]}


@app.put("/api/projects/last-open")
async def put_last_open(req: Request):
    body = await req.json()
    STATE["last_open"] = str(body.get("ref") or "") or None
    STATE["last_open_puts"].append(STATE["last_open"])
    return {"ref": STATE["last_open"]}


@app.put("/api/projects/{pid}/seen")
def seen(pid: int):
    return {"project_id": pid, "seen_at": datetime.now(timezone.utc).isoformat()}


@app.get("/api/projects/{pid}/page")
def project_page(pid: int):
    # A page with nothing written on it yet.
    return {"project_id": pid, "where_this_stands": None, "totals": None, "engines": None,
            "seen_at": None, "last_landed_at": None, "unseen_landed_at": None, "waiting_on_you": 0,
            "ask_cards_open": 0, "approvals_waiting": 0, "approvals_checked_at": None}


@app.get("/api/projects/buckets")
def list_buckets():
    return {"buckets": [], "unfiled": {"counts": _counts(STATE["projects"])}}


@app.post("/api/projects/search")
async def search(req: Request):
    body = await req.json()
    q, limit = str(body.get("q") or "").strip().lower(), int(body.get("limit") or 5)
    # What the box asked, and on which URL: the words are the body's, so the
    # URL has to carry nothing (a URL is kept by whatever sits in front of the pod).
    STATE["searches"].append({"url": str(req.url), "body": body})
    if not q:
        rows = [p for p in STATE["projects"] if p["status"] != "archived"]
        return {"projects": [dict(p, group=_group(p["status"]), score=None) for p in rows[:limit]],
                "semantic": False, "q": q}
    hits = []
    for p in STATE["projects"]:
        name_hit = q in p["name"].lower()
        desc_hit = q in (p.get("description") or "").lower()
        if name_hit or desc_hit:
            hits.append(dict(p, group=_group(p["status"]), score=1.0 if name_hit else 0.5))
    hits.sort(key=lambda r: -r["score"])
    return {"projects": hits[:limit], "semantic": False, "q": q}


@app.post("/api/projects")
async def create_project(req: Request):
    body = await req.json()
    STATE["posts"].append(body)
    pid = STATE["next_id"]
    STATE["next_id"] += 1
    row = proj(pid, body["name"], "planning", description=body.get("description", ""), at=CREATED_AT)
    STATE["projects"].insert(0, row)
    out = {"id": pid, "name": row["name"], "status": "planning", "repo": None}
    if body.get("adopt_thread"):
        out["adopted"] = {"project_id": pid, "moved_turns": _move_general(pid, row["name"]),
                          "moved_memories": 0}
    return out


@app.post("/api/projects/{pid}/adopt-thread")
async def adopt(pid: int, req: Request):
    body = await req.json()
    STATE["adopts"].append({"project_id": pid, "body": body})
    name = next((p["name"] for p in STATE["projects"] if p["id"] == pid), "")
    return {"project_id": pid, "project_name": name, "moved_turns": _move_general(pid, name),
            "moved_memories": 0}


@app.get("/api/brain/chat")
def chat(app_id: str = "", project_id: int | None = None, order: str = "asc"):
    # The real route honours order=desc (newest first); api.ts asks for desc
    # and reverses. The mock must do the same or the seed picks the wrong turn.
    if project_id is None and app_id == "workbench":
        turns = list(STATE["general_turns"])
        return {"turns": turns[::-1] if order == "desc" else turns}
    return {"turns": []}


@app.get("/api/brain/turns/active")
def turns_active(project_id: int | None = None):
    return {"ok": True, "turn": None, "turns": []}


@app.get("/css/tokens.css")
def tokens():
    return Response(":root{}", media_type="text/css")


@app.get("/js/{name}")
def shell_js(name: str):
    """The shell scripts index.html loads from the kernel's /js/
    (sandbox-csp.js and desktop-preview.js)."""
    base = (ROOT / "dashboard" / "base" / "js").resolve()
    target = (base / name).resolve()
    if target.parent != base or not target.is_file():
        return Response(status_code=404)
    return FileResponse(str(target), media_type="text/javascript")


@app.api_route("/api/{rest:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
def other_api(rest: str):
    # Bench/ops/context calls the flows do not exercise. A 404 is the shape
    # the API layer already tolerates (ApiResult ok:false) — an empty {} would
    # not be, for callers that read r.data.<key>.
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
    # process launches on this same box. Nothing reaches it over a network.
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error", timeout_keep_alive=IDLE_KEEP_ALIVE_S)
    server = uvicorn.Server(config)
    threading.Thread(target=server.run, daemon=True).start()
    deadline = time.time() + 10
    while not server.started and time.time() < deadline:
        time.sleep(0.05)
    return port


# ── scenarios ────────────────────────────────────────────────────────────────
DIALOG_BOX = "textarea[placeholder='What would you like to work on?']"
DIALOG = "[role='dialog']"
# The live page, never the copy a page turn lays over it (src/book/flip.ts
# copyOfPage: the copy is .book-ghost and has no ids).
SHEET = "article.book-sheet:not(.book-ghost)"
PAGE_TITLE = "#book-page > .book-inner > h1.book-title"
COMPOSER = f"{SHEET} textarea"
EMPTY = "[data-book-empty]"

# The retired General thread, as GET /api/brain/chat?app_id=workbench answers
# it: the person's opening message is longer than the 400 characters the
# Start box is pre-filled with (book-app.tsx GENERAL_SEED_CHARS).
OPENING = ("let's build a garden planner with a planting calendar. It should know the last frost date "
           "where I live, and for every crop I grow it should say when to sow it indoors, when to plant "
           "it out, and when to expect the first harvest. I keep four raised beds and a few pots on the "
           "balcony, so it needs to know which bed each crop is in and what grew there last year, "
           "because I rotate the beds. Tomatoes, beans, garlic and lettuce to start with; the herbs "
           "can come later, once the beds work.")
GENERAL = [
    {"id": 11, "role": "user", "content": OPENING, "created_at": "2026-09-01T10:00:00Z", "metadata": {"role": "user"}},
    {"id": 12, "role": "assistant", "content": "Sure.", "created_at": "2026-09-01T10:00:05Z", "metadata": {"role": "assistant"}},
    {"id": 13, "role": "user", "content": "raised beds first", "created_at": "2026-09-01T10:01:00Z", "metadata": {"role": "user"}},
]


def holds(assertion) -> bool:
    """An expect(...) as a bool: True when it held within its timeout."""
    try:
        assertion()
        return True
    except (PlaywrightTimeout, AssertionError):
        return False


def wait_for(cond, secs=5.0, step=0.05):
    """Poll a condition on the mock's side until it holds or time runs out."""
    deadline = time.time() + secs
    while time.time() < deadline:
        if cond():
            return True
        time.sleep(step)
    return bool(cond())


def text_of(page, sel):
    """The first match's text, or None when nothing matches (for details)."""
    loc = page.locator(sel)
    try:
        return loc.first.text_content(timeout=1_000) if loc.count() else None
    except PlaywrightTimeout:
        return None


def value_of(page, sel):
    loc = page.locator(sel)
    try:
        return loc.first.input_value(timeout=1_000) if loc.count() else None
    except PlaywrightTimeout:
        return None


def no_dialog(page) -> bool:
    """No dialog is open. Meaningful only once the book's boot has decided:
    after a page's title shows (the boot opens the page in the same update
    that opens the Start dialog, book-app.tsx), or after open_book_list_last."""
    return page.locator(DIALOG).count() == 0


def goto_book(page, base):
    page.goto(f"{base}/system-apps/workbench/index.html", wait_until="domcontentloaded", timeout=15_000)


def _general_read(r) -> bool:
    return "/api/brain/chat?" in r.url and "app_id=workbench" in r.url and "project_id=" not in r.url


def _last_open_read(r) -> bool:
    return r.method == "GET" and r.url.endswith("/api/projects/last-open")


def open_book_list_last(page, base):
    """Open the book with the project list held until the boot's two other
    reads (the last open page and the General thread) have reached the page,
    so the list answers last. The boot decides on all three at once
    (book-app.tsx: Promise.all, then the Start dialog and the page in one
    update), and an empty book shows its empty state as soon as the list has
    landed: with the list last, the first render of the empty state carries
    the boot's whole decision, the Start dialog included. Without the hold it
    could show before the General thread was read."""
    STATE["hold_list"] = True
    try:
        with page.expect_event("requestfinished", predicate=_general_read, timeout=10_000), \
                page.expect_event("requestfinished", predicate=_last_open_read, timeout=10_000):
            goto_book(page, base)
    finally:
        STATE["hold_list"] = False


def landed_last_open(n0: int, ref: str) -> bool:
    """The page's own write of the last open page has landed (wait before the
    mock is reset, or a late write lands in the next part)."""
    return wait_for(lambda: len(STATE["last_open_puts"]) > n0 and STATE["last_open"] == ref)


def scenario_a(page, base):
    print("A. an empty book → its empty state offers Start, no dialog by itself; New project carries the text")
    # Archived projects are not pages (lib/book.ts isOpenPage): the book is empty.
    reset(projects=[proj(1, "Old thing", "archived"), proj(2, "Parked", "archived")])
    open_book_list_last(page, base)
    start = page.locator(f"{EMPTY} button:has-text('Start')")
    check("A: the empty book shows its empty state with Start (archived projects are no pages)",
          holds(lambda: expect(start).to_be_visible(timeout=10_000))
          and page.locator(PAGE_TITLE).count() == 0
          and "No open pages yet" in (text_of(page, EMPTY) or ""),
          repr(text_of(page, EMPTY)))
    check("A: no dialog opened by itself", no_dialog(page), repr(text_of(page, DIALOG)))
    page.screenshot(path=str(SHOTS / "A-empty.png"))
    start.click()
    box = page.locator(DIALOG_BOX)
    if not holds(lambda: expect(box).to_be_visible(timeout=5_000)):
        check("A: Start opens the dialog", False)
        page.screenshot(path=str(SHOTS / "A-no-dialog.png"))
        return
    check("A: Start opens the dialog", True)
    typed = "calculator that adds two numbers"
    box.fill(typed)
    page.locator(f"{DIALOG} button:has-text('New project')").first.click()
    title = page.locator("input[placeholder='Project title']")
    expect(title).to_be_visible(timeout=5_000)
    check("A: a title is suggested from the text", (title.input_value() or "").strip() != "", repr(title.input_value()))
    title.fill("Calculator")
    page.locator("button:has-text('Create project')").click()
    expect(title).to_be_hidden(timeout=5_000)
    posts = STATE["posts"]
    check("A: POST /api/projects carries name + the typed text as description, no adopt",
          len(posts) == 1 and posts[0]["name"] == "Calculator" and posts[0]["description"] == typed
          and "adopt_thread" not in posts[0], repr(posts))
    composer = page.locator(COMPOSER)
    check("A: the book turns to the new page (Calculator) with the text in its composer",
          holds(lambda: expect(page.locator(PAGE_TITLE)).to_have_text("Calculator", timeout=5_000))
          and holds(lambda: expect(composer).to_have_value(typed, timeout=5_000))
          and composer.get_attribute("placeholder") == "Write on this page…",
          repr((text_of(page, PAGE_TITLE), value_of(page, COMPOSER))))
    # The list is read again after the create: the new project is 'planning',
    # its tab notes its group (lib/book.ts tabNote), and it is the open page.
    tab = page.locator(".book-tabs [data-tab='1000']")
    check("A: the book shows the new page: its tab is the one selected, noted 'pending', and the empty state is gone",
          holds(lambda: expect(tab).to_have_attribute("aria-selected", "true", timeout=5_000))
          and "pending" in (text_of(page, ".book-tabs [data-tab='1000'] .book-tab-note") or "")
          and page.locator(EMPTY).count() == 0,
          repr((text_of(page, ".book-tabs [data-tab='1000']"), page.locator(EMPTY).count())))
    page.screenshot(path=str(SHOTS / "A-created.png"))


def scenario_b(page, base):
    print("B. a book with pages → opens on the page last open, else on the newest open page; no dialog")
    # In the list route's order (last activity first). The archived project is
    # the newest of all and Trading software has the higher id: neither makes
    # it the newest open page.
    projects = [proj(3, "Old", "archived", at="2026-09-07T13:00:00Z"),
                proj(2, "Weather app", "active", description="forecast dashboard", at="2026-09-07T12:00:00Z"),
                proj(5, "Trading software", "active", description="backtesting engine", at="2026-09-07T10:00:00Z")]
    reset(projects=projects)
    goto_book(page, base)
    check("B: with no page last open, the book opens on the newest open page (Weather app)",
          holds(lambda: expect(page.locator(PAGE_TITLE)).to_have_text("Weather app", timeout=10_000)),
          repr(text_of(page, PAGE_TITLE)))
    check("B: no dialog on open (the General thread is empty)", no_dialog(page), repr(text_of(page, DIALOG)))
    check("B: the book keeps the page it opened as the last open one (PUT /api/projects/last-open)",
          landed_last_open(0, "2"), repr(STATE["last_open_puts"]))
    page.screenshot(path=str(SHOTS / "B-newest.png"))

    page.goto("about:blank")
    reset(projects=projects, last_open="5")
    goto_book(page, base)
    check("B: with a page last open, the book opens on it although it is not the newest (Trading software)",
          holds(lambda: expect(page.locator(PAGE_TITLE)).to_have_text("Trading software", timeout=10_000)),
          repr(text_of(page, PAGE_TITLE)))
    check("B: no dialog on open", no_dialog(page), repr(text_of(page, DIALOG)))


def scenario_c(page, base):
    print("C. a retired General thread with turns → the dialog opens by itself, pre-filled; creating or picking adopts them")
    seed = OPENING[:400]
    # ── creating a project from it ──
    reset(projects=[proj(1, "Trading software", "active")], general_turns=GENERAL)
    goto_book(page, base)
    box = page.locator(DIALOG_BOX)
    if not holds(lambda: expect(box).to_be_visible(timeout=10_000)):
        check("C: the dialog opened by itself although the book has a page", False)
        page.screenshot(path=str(SHOTS / "C-no-dialog.png"))
        return
    check("C: the dialog opened by itself although the book has a page",
          text_of(page, PAGE_TITLE) == "Trading software", repr(text_of(page, PAGE_TITLE)))
    check("C: pre-filled with the thread's opening message, its first 400 characters",
          box.input_value() == seed, repr(box.input_value()))
    dlg = page.locator(DIALOG).inner_text()
    check("C: the notice says how many messages will move", "2 messages" in dlg, dlg)
    page.screenshot(path=str(SHOTS / "C-dialog.png"))
    page.locator(f"{DIALOG} button:has-text('New project')").first.click()
    title = page.locator("input[placeholder='Project title']")
    expect(title).to_be_visible(timeout=5_000)
    name = title.input_value()
    check("C: suggested title drops the intent phrase", name.startswith("Garden planner"), name)
    page.locator("button:has-text('Create project')").click()
    expect(title).to_be_hidden(timeout=5_000)
    posts = STATE["posts"]
    check("C: the create carries adopt_thread {app, workbench} and the message as its description — the "
          "General turns move into it; no second adopt call",
          len(posts) == 1 and posts[0].get("adopt_thread") == {"kind": "app", "ref": "workbench"}
          and posts[0].get("description") == seed.strip() and STATE["adopts"] == [],
          repr((posts, STATE["adopts"])))
    check("C: the General thread keeps only the move's marker afterwards (the mock moved its turns)",
          [(t["metadata"] or {}).get("source_kind") for t in STATE["general_turns"]] == ["thread_marker"],
          repr(STATE["general_turns"]))
    composer = page.locator(COMPOSER)
    check("C: the book turns to the new page with the message in its composer",
          holds(lambda: expect(page.locator(PAGE_TITLE)).to_have_text(name, timeout=5_000))
          and holds(lambda: expect(composer).to_have_value(seed.strip(), timeout=5_000)),
          repr((text_of(page, PAGE_TITLE), value_of(page, COMPOSER))))
    landed_last_open(0, "1000")
    n0 = len(STATE["last_open_puts"])
    page.reload(wait_until="domcontentloaded", timeout=15_000)
    check("C: a reload after the move opens the book on that page, and no dialog: the General thread "
          "holds only the marker",
          holds(lambda: expect(page.locator(PAGE_TITLE)).to_have_text(name, timeout=10_000)) and no_dialog(page),
          repr((text_of(page, PAGE_TITLE), text_of(page, DIALOG))))
    landed_last_open(n0, "1000")

    # ── picking a page: the dialog's ranking (once B's) ──
    page.goto("about:blank")
    reset(projects=[proj(2, "Weather app", "active", description="forecast dashboard", at="2026-09-07T12:00:00Z"),
                    proj(1, "Trading software", "active", description="backtesting engine", at="2026-09-07T10:00:00Z")],
          general_turns=GENERAL)
    goto_book(page, base)
    if not holds(lambda: expect(box).to_be_visible(timeout=10_000)):
        check("C: with two pages, the dialog opens by itself over the newest, pre-filled", False)
        page.screenshot(path=str(SHOTS / "C-pick-no-dialog.png"))
        return
    check("C: with two pages, the dialog opens by itself over the newest (Weather app), pre-filled",
          text_of(page, PAGE_TITLE) == "Weather app" and box.input_value() == seed,
          repr((text_of(page, PAGE_TITLE), box.input_value()[:60])))
    box.fill("trading")
    hit = page.locator(f"{DIALOG} button:has-text('Trading software')")
    if not holds(lambda: expect(hit).to_be_visible(timeout=5_000)):
        check("C: typing ranks the matching project under the box", False)
        page.screenshot(path=str(SHOTS / "C-no-hit.png"))
        return
    check("C: typing ranks the matching project under the box", True)
    check("C: ...the search is a POST whose body holds the words ({q, limit}), and its URL carries nothing",
          bool(STATE["searches"])
          and all(urlsplit(x["url"]).path == "/api/projects/search" and urlsplit(x["url"]).query == ""
                  for x in STATE["searches"])
          and any(x["body"].get("q") == "trading" and set(x["body"]) == {"q", "limit"} for x in STATE["searches"]),
          repr(STATE["searches"][-2:]))
    hit.click()
    expect(box).to_be_hidden(timeout=5_000)
    check("C: picking it sends POST /api/projects/1/adopt-thread {kind:'app', ref:'workbench'}",
          wait_for(lambda: bool(STATE["adopts"]))
          and STATE["adopts"] == [{"project_id": 1, "body": {"kind": "app", "ref": "workbench"}}],
          repr(STATE["adopts"]))
    check("C: picking it opens that page with the text in the composer",
          holds(lambda: expect(page.locator(PAGE_TITLE)).to_have_text("Trading software", timeout=5_000))
          and holds(lambda: expect(composer).to_have_value("trading", timeout=5_000)),
          repr((text_of(page, PAGE_TITLE), value_of(page, COMPOSER))))
    check("C: no project was created", len(STATE["posts"]) == 0, repr(STATE["posts"]))
    page.screenshot(path=str(SHOTS / "C-picked.png"))
    landed_last_open(0, "1")

    # ── an empty General thread ──
    page.goto("about:blank")
    reset(projects=[proj(1, "Trading software", "active")])
    goto_book(page, base)
    check("C: an entity whose General thread is empty gets no dialog at boot",
          holds(lambda: expect(page.locator(PAGE_TITLE)).to_have_text("Trading software", timeout=10_000))
          and no_dialog(page),
          repr((text_of(page, PAGE_TITLE), text_of(page, DIALOG))))


def main():
    if not (APP_DIR / "index.html").is_file():
        print(f"FAIL  no built app at {APP_DIR} — run `npm run ship` in workbench-web/")
        return 1
    port = serve()
    base = f"http://127.0.0.1:{port}"
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        for fn in (scenario_a, scenario_b, scenario_c):
            ctx = browser.new_context(viewport={"width": 1400, "height": 900})
            page = ctx.new_page()
            errors = []
            page.on("pageerror", lambda e: errors.append(str(e)))
            try:
                fn(page, base)
            except Exception as e:  # a scenario that throws is a failed gate, not a crash
                check(f"{fn.__name__}: completed", False, f"{type(e).__name__}: {e}")
                page.screenshot(path=str(SHOTS / f"{fn.__name__}-error.png"))
            check(f"{fn.__name__}: no uncaught page errors", not errors, "; ".join(errors)[:300])
            ctx.close()
        browser.close()
    print("\nPASS workbench_start_flow" if not fails else f"\nFAIL workbench_start_flow: {fails} check(s)")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
