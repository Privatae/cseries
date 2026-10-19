#!/usr/bin/env python3
"""Gate: the Workbench's bench sections, in a real browser.

Driven against the BUILT app (system-apps/workbench/index.html, the book of
pages) over a mock kernel API that records what the UI sends. The
bench's sections are on the book's right side (aside.book-right), under "This
page"; both scenarios open the book on Reader's page and work there:

  O. the ops pane opens with RUNS closed and MODULES at the top: module →
     versions with one "open version" each, candidates that "use as version"
     lands through review.adopt(into=module), installed/running a badge on
     the module; PLAN groups phases under their module with how each is
     proven;
  C. the run card exposes exactly one open button in every run state (live,
     complete, failed, timeout) — disabled while live — and it opens the run's
     own version; a landed run's version is ALREADY loaded in its card with no
     click (scroll to see which version it is), a run with no page says so in the card, and the card's changed
     files come from the diff route.

R (the rail's buckets), D (dragging projects between the rail's buckets) and P
(picking a row up out of Unfiled) were retired with the rail, and so were the two gates
that drove the rail's drag, scripts/test_workbench_rail_native_drag.py and
tests/test_workbench_rail_drag_js.py. The book's own drag, a tab onto a
chapter, is scripts/test_workbench_book.py G.

Needs Playwright + Chromium (python3 -m playwright install chromium) and a
free local port — no pod, no DB, no network beyond loopback. Run:

  python3 scripts/test_workbench_panes.py
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import socket
import sys
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

ROOT = Path(os.environ.get("APP_ROOT") or Path(__file__).resolve().parents[1])
APP_DIR = ROOT / "system-apps" / "workbench"
# Screenshots go where SHOTS_DIR says, or to a fresh temp folder that is
# removed after a clean run and kept (its path printed) only when a check
# failed, so repeated runs never pile files up in /tmp.
_OWN_SHOTS = not os.environ.get("SHOTS_DIR")
SHOTS = Path(os.environ["SHOTS_DIR"]) if not _OWN_SHOTS else Path(tempfile.mkdtemp(prefix="wb-panes-"))
SHOTS.mkdir(parents=True, exist_ok=True)

fails = 0


def check(name, ok, detail=""):
    global fails
    print(("  ok    " if ok else "  FAIL  ") + name + (f"  — {detail}" if detail and not ok else ""))
    if not ok:
        fails += 1


# ── the mock kernel ──────────────────────────────────────────────────────────
from fastapi import FastAPI, Request  # noqa: E402
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response, StreamingResponse  # noqa: E402
import uvicorn  # noqa: E402
from gate_loopback import IDLE_KEEP_ALIVE_S  # noqa: E402

NOW = "2026-09-23T12:00:00Z"
STATE: dict = {}

# Four runs, one per terminal state the card must handle.
RUNS = {
    "sess-live": {"id": 701, "harness": "codex", "state": "live"},
    "sess-done": {"id": 702, "harness": "claude-code", "state": "complete"},
    "sess-fail": {"id": 703, "harness": "kimi", "state": "failed"},
    "sess-tout": {"id": 704, "harness": "astra", "state": "timeout"},
}


def proj(i, name):
    """An Active project as the list answers it, with the page marks the
    book's tabs read: nothing landed on it, nothing waiting."""
    return {"id": i, "name": name, "status": "active", "group": "active", "bucket_id": None,
            "sort_order": None, "description": "", "phase_count": 0, "repo": None,
            "last_activity_at": NOW, "seen_at": None, "last_landed_at": None, "unseen_landed_at": None,
            "waiting_on_you": 0, "ask_cards_open": 0, "approvals_waiting": 0}


def reset():
    STATE.clear()
    # Reader: the newest Active project, which the rail layout opened on, and
    # the page the book was last left on.
    STATE.update({"projects": [proj(10, "Reader")], "last_open": "10", "adopts": []})


app = FastAPI()


# The book's boot reads (src/book/book-app.tsx): the list, the page it was
# last left on, the buckets (its Index's chapters: none here), the page's top
# and its seen mark, and the running turns.
@app.get("/api/projects")
def list_projects(include_archived: bool = False, limit: int = 50):
    rows = [dict(p) for p in STATE["projects"] if include_archived or p["status"] != "archived"]
    return {"projects": rows[:limit]}


@app.get("/api/projects/last-open")
def last_open():
    return {"ref": STATE["last_open"]}


@app.put("/api/projects/last-open")
async def put_last_open(req: Request):
    body = await req.json()
    STATE["last_open"] = str(body.get("ref") or "")
    return {"ref": STATE["last_open"]}


@app.get("/api/projects/buckets")
def list_buckets():
    return {"buckets": [], "unfiled": {"counts": {}}}


@app.put("/api/projects/{pid}/seen")
def seen(pid: int):
    return {"project_id": pid, "seen_at": datetime.now(timezone.utc).isoformat()}


@app.get("/api/projects/{pid}/page")
def project_page(pid: int):
    """The page's top (projects.py get_project_page): nothing written on where
    it stands yet, nothing landed, nothing waiting."""
    return {"project_id": pid, "where_this_stands": None, "totals": None, "engines": None,
            "seen_at": None, "last_landed_at": None, "unseen_landed_at": None, "waiting_on_you": 0,
            "ask_cards_open": 0, "approvals_waiting": 0, "approvals_checked_at": None}


@app.get("/api/brain/turns/active")
def turns_active(project_id: int | None = None):
    return {"ok": True, "turn": None, "turns": []}


APP_RUNNING = {"app_id": "reader", "kind": "static", "installed": True, "running": True,
               "opens_via": "desktop", "install_action": None, "manifest_error": None}


@app.get("/api/projects/{pid}/modules")
def modules(pid: int):
    return {"project_id": pid, "root_app": None, "modules": [
        {"name": "app", "target": "private_app", "validation": "proof", "description": "",
         "current_version": {"number": 2, "run_id": 690, "commit": "abc1234", "source": "build", "at": NOW},
         "version_count": 2, "app": APP_RUNNING,
         "candidates": [{"run_id": 702, "session_id": "sess-done", "harness": "claude-code",
                         "model": "opus", "kind": "compare", "at": NOW}]},
        {"name": "site", "target": "public_site", "validation": "review", "description": "",
         "current_version": None, "version_count": 0, "app": None, "candidates": []},
    ]}


@app.get("/api/projects/{pid}/modules/{mod}/versions")
def versions(pid: int, mod: str):
    if mod != "app":
        return {"versions": []}
    return {"versions": [
        {"number": 2, "run_id": 690, "commit": "abc1234", "source": "build", "note": "", "current": True,
         "restorable": True, "undone": False, "at": NOW},
        {"number": 1, "run_id": 650, "commit": "0ff1ce0", "source": "build", "note": "", "current": False,
         "restorable": True, "undone": False, "at": NOW},
    ]}


@app.get("/api/projects/{pid}/phases")
def phases(pid: int):
    return {"phases": [
        {"id": 1, "phase_number": 1, "title": "Reader shell", "status": "complete", "module": "app",
         "validation": "proof", "acceptance_checks": 0, "step_count": 0, "steps_done": 0, "gate": "none"},
        {"id": 2, "phase_number": 2, "title": "Landing page", "status": "ready", "module": "site",
         "validation": "review", "acceptance_checks": 0, "step_count": 0, "steps_done": 0, "gate": "none"},
        {"id": 3, "phase_number": 3, "title": "Legacy landed", "status": "review", "module": "site",
         "validation": "review", "acceptance_checks": 2, "step_count": 0, "steps_done": 0, "gate": "none"},
        {"id": 4, "phase_number": 4, "title": "Needs sign-off", "status": "planning", "module": "app",
         "validation": None, "acceptance_checks": 0, "step_count": 0, "steps_done": 0, "gate": "user", "approved_at": None},
    ]}


@app.get("/api/projects/{pid}/preview")
def project_preview(pid: int, module: str = "", version: int | None = None):
    return HTMLResponse(f"<html><body><h1>module {module} v{version}</h1></body></html>")


@app.post("/api/review/adopt")
async def adopt(req: Request):
    body = await req.json()
    STATE["adopts"].append(body)
    return {"ok": True, "version": {"number": 3}}


@app.get("/api/compute/runs")
def runs(project_id: int | None = None, window: str = "", limit: int = 40):
    return {"runs": [
        {"id": r["id"], "session_id": sid, "project_id": project_id, "project_name": "Reader",
         "harness": r["harness"], "model": None, "ok": r["state"] == "complete", "status": "active",
         "exit_code": 0, "has_error": False, "diff_bytes": 10, "elapsed_s": 30, "created_at": NOW}
        for sid, r in RUNS.items()]}


@app.get("/api/review/sessions/{sid}")
def session(sid: str):
    r = RUNS.get(sid)
    if not r:
        return JSONResponse({"detail": "session not found"}, status_code=404)
    return {"session_id": sid, "status": r["state"],
            "manifest": {"harnesses": [r["harness"]], "project_id": 10, "prompt": "build it",
                         "module": "app", "seeded": True},
            "entrypoints": {r["harness"]: sid == "sess-done"}}


@app.get("/api/review/sessions/{sid}/stream/{harness}")
async def stream(sid: str, harness: str, from_seq: int = 0):
    r = RUNS[sid]

    async def gen():
        if from_seq <= 1:
            yield json.dumps({"seq": 1, "kind": "text", "payload": {"text": f"{harness} working"}}) + "\n"
        if r["state"] == "live":
            await asyncio.sleep(20)   # a live run: no terminal frame, the feed stays open
            return
        st = "timeout" if r["state"] == "timeout" else "complete"
        yield json.dumps({"seq": 2, "kind": "status", "payload": {
            "state": st, "ok": r["state"] == "complete", "exit_code": 0 if r["state"] == "complete" else 1,
            "elapsed_seconds": 30}}) + "\n"

    return StreamingResponse(gen(), media_type="application/x-ndjson")


@app.get("/api/review/sessions/{sid}/preview/{harness}")
def run_preview(sid: str, harness: str):
    if sid == "sess-done":
        return HTMLResponse("<html><body><h1>the run's own version</h1></body></html>")
    return JSONResponse({"detail": "no renderable index.html"}, status_code=404)


@app.get("/api/review/sessions/{sid}/diff/{harness}")
def run_diff(sid: str, harness: str):
    return {"diff": "diff --git a/x b/x\n+added line\n"}


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
    # No turns anywhere, the retired General thread's (app_id=workbench)
    # included: with turns there the book opens the Start dialog at boot.
    return {"turns": []}


@app.get("/css/tokens.css")
def tokens():
    return Response(":root{}", media_type="text/css")


@app.get("/js/{name}")
def shell_js(name: str):
    """The shell scripts the kernel serves at /js/ — the real files. The
    version window mounts every page under /js/sandbox-csp.js and refuses to
    render without it (fail closed), exactly as on a live Host."""
    target = (ROOT / "dashboard" / "base" / "js" / name).resolve()
    if target.parent != (ROOT / "dashboard" / "base" / "js").resolve() or not target.is_file():
        return Response(status_code=404)
    return FileResponse(str(target), media_type="text/javascript")


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


# ── scenarios ────────────────────────────────────────────────────────────────
# The book's right side: "This page", then the bench's sections, which are the
# ops pane (components/workbench/ops-pane.tsx) in its page chrome.
RIGHT = "aside.book-right"
BENCH = f"{RIGHT} > aside[data-chrome='page']"
# The live page's own title (src/book/book-app.tsx), never a copy a turn lays
# over the book.
PAGE_TITLE = "#book-page > .book-inner > h1.book-title"


def open_book(page, base):
    page.goto(f"{base}/system-apps/workbench/index.html", wait_until="domcontentloaded", timeout=15_000)
    expect(page.locator(PAGE_TITLE)).to_have_text("Reader", timeout=10_000)


def wait_for(cond, secs=10.0, step=0.1):
    """Poll a condition on the mock's side until it holds or time runs out."""
    deadline = time.time() + secs
    while time.time() < deadline:
        if cond():
            return True
        time.sleep(step)
    return bool(cond())


def in_front(loc):
    """What the browser hits at a window's head and at its middle: the window
    itself when nothing is drawn over it. The book's page and tabs are layers
    of their own beside the right side, so a window the right side opens must
    come up in front of them for the person to see and use it."""
    return loc.evaluate("""el => {
        const r = el.getBoundingClientRect();
        const at = (x, y) => {
            const h = document.elementFromPoint(x, y);
            return {inside: el.contains(h),
                    hit: h ? h.tagName.toLowerCase() + '.' + String(h.className).split(' ')[0] : null};
        };
        const head = at(r.left + r.width / 2, r.top + 20);
        const middle = at(r.left + r.width / 2, r.top + r.height / 2);
        return {ok: head.inside && middle.inside, head, middle};
    }""")


def scenario_ops(page, base):
    print("O. ops pane: RUNS closed, MODULES on top, candidates use-as-version, PLAN by module")
    reset()
    open_book(page, base)
    bench = page.locator(BENCH)
    expect(bench.locator("[data-module='app']")).to_be_visible(timeout=8_000)
    runs = bench.locator("button[data-state]").filter(has_text=re.compile(r"^\s*Runs"))
    check("O: the ops pane opens with RUNS closed", runs.first.get_attribute("data-state") == "closed",
          runs.first.get_attribute("data-state"))
    check("O: no run row is showing while RUNS is closed", bench.get_by_text("#701").count() == 0)
    labels = bench.locator("div.uppercase").all_text_contents()
    check("O: MODULES is the first section, above PLAN",
          labels and labels[0].strip().lower().startswith("modules") and any("plan" in x.lower() for x in labels),
          repr(labels))
    app_card = bench.locator("[data-module='app']")
    check("O: installed/running is a badge on the module",
          app_card.locator("[data-app-badge='running']").count() == 1)
    check("O: a module says what it ships as, with no proving words",
          "private app" in (app_card.text_content() or "") and "proven" not in (app_card.text_content() or ""),
          app_card.text_content())
    app_card.locator("button").first.click()
    expect(app_card.locator("[data-version='2']")).to_be_visible(timeout=3_000)
    check("O: every version carries exactly one open-version button",
          all(app_card.locator(f"[data-version='{n}'] [data-open-version]").count() == 1 for n in (1, 2)))
    check("O: the current version offers undo, the older one make-current",
          app_card.locator("[data-version='2'] button:has-text('undo')").count() == 1
          and app_card.locator("[data-version='1'] button:has-text('make current')").count() == 1)
    cand = app_card.locator("[data-candidate='702']")
    check("O: a run built for the module shows as a candidate with one open-version button",
          cand.count() == 1 and cand.locator("[data-open-version]").count() == 1)
    cand.locator("button:has-text('use as version')").click()
    wait_for(lambda: bool(STATE["adopts"]), 5)
    check("O: 'use as version' lands the run in the module (review.adopt into=module)",
          STATE["adopts"] == [{"run_id": 702, "into": "app"}], repr(STATE["adopts"]))
    app_card.locator("[data-version='1'] [data-open-version]").click()
    vv = page.locator("[data-testid='version-view']")
    expect(vv).to_be_visible(timeout=3_000)
    frame = vv.frame_locator("iframe")
    expect(frame.locator("h1")).to_have_text("module app v1", timeout=3_000)
    seen = in_front(vv)
    check("O: opening a version renders that version's snapshot, in front of the page", seen["ok"], repr(seen))
    vv.locator("button[aria-label='Close']").click()
    plan = bench.locator("[data-plan-module]")
    check("O: PLAN groups phases under their module",
          plan.evaluate_all("els => els.map(e => e.dataset.planModule)") == ["app", "site"])
    plan_all = " ".join(plan.all_text_contents()).lower()
    check("O: no phase talks about being proven",
          not any(w in plan_all for w in ("proven", "unproven", "proof", "confirm")), plan_all)
    site = bench.locator("[data-plan-module='site']")
    check("O: an old 'review' phase shows as complete and has no button",
          "complete" in (site.text_content() or "") and "checks: 2 listed" in (site.text_content() or "")
          and site.locator("button:has-text('Confirm')").count() == 0, site.text_content())
    check("O: a gate='user' phase keeps its Approve button",
          bench.locator("[data-plan-module='app'] button:has-text('Approve')").count() == 1)
    page.screenshot(path=str(SHOTS / "O-bench.png"))


def scenario_card(page, base):
    print("C. the run card: exactly one open button in every run state")
    reset()
    open_book(page, base)
    bench = page.locator(BENCH)
    expect(bench.locator("[data-module='app']")).to_be_visible(timeout=8_000)
    bench.locator("button[data-state]").filter(has_text=re.compile(r"^\s*Runs")).first.click()
    for sid, r in RUNS.items():
        bench.locator("button").filter(has_text=f"#{r['id']}").first.click()
    def card(harness):
        return bench.locator(f"[data-run-card][data-harness='{harness}']").first

    for sid, r in RUNS.items():
        term = card(r["harness"])
        if r["state"] != "live":
            expect(term.locator("[data-open-version]:not([disabled])")).to_have_count(1, timeout=8_000)
        buttons = term.locator("[data-open-version]")
        text = term.text_content() or ""
        disabled = buttons.first.is_disabled() if buttons.count() else None
        check(f"C: {r['state']} run — exactly one open button"
              + (" (disabled while live)" if r["state"] == "live" else " (enabled)"),
              buttons.count() == 1 and disabled == (r["state"] == "live")
              and not any(w in text for w in ("preview files", "open app", "review diff")),
              repr((buttons.count(), disabled, text[:200])))
    done = card("claude-code")
    # The landed run's version is in the card already: no click.
    inline = done.locator("[data-run-preview] iframe")
    expect(inline).to_have_count(1, timeout=8_000)
    expect(done.frame_locator("[data-run-preview] iframe").locator("h1")).to_have_text("the run's own version", timeout=5_000)
    check("C: a landed run's version loads in its card with no click", True)
    check("C: the inline frame is sealed (sandbox allow-scripts only) and takes no pointer",
          inline.get_attribute("sandbox") == "allow-scripts"
          and "pointer-events-none" in (inline.get_attribute("class") or ""),
          repr((inline.get_attribute("sandbox"), inline.get_attribute("class"))))
    expect(done.locator("[data-changed-files]")).to_contain_text("+1", timeout=5_000)
    check("C: the card's changed files come from the diff route", True)
    live = card("codex")
    check("C: a live run has no inline version yet", live.locator("[data-run-preview]").count() == 0)
    failed = card("kimi")
    expect(failed).to_contain_text("This run left no page to preview", timeout=5_000)
    check("C: a run with no page says so in its card", True)
    done.locator("[data-run-preview] [role='button']").click()
    vv = page.locator("[data-testid='version-view']")
    expect(vv.frame_locator("iframe").locator("h1")).to_have_text("the run's own version", timeout=5_000)
    seen = in_front(vv)
    check("C: clicking the inline version opens the run's own worktree, in front of the page", seen["ok"],
          repr(seen))
    vv.locator("button[aria-label='Close']").click()
    done.locator("[data-open-version]").click()
    expect(vv.frame_locator("iframe").locator("h1")).to_have_text("the run's own version", timeout=5_000)
    seen = in_front(vv)
    check("C: open version renders the run's own worktree, in front of the page", seen["ok"], repr(seen))
    vv.locator("button:has-text('Changes')").click()
    expect(vv).to_contain_text("added line", timeout=3_000)
    check("C: the same window shows the run's changes", True)
    vv.locator("button[aria-label='Close']").click()
    failed.locator("[data-open-version]").click()
    expect(vv).to_contain_text("added line", timeout=5_000)
    check("C: a run with nothing to render opens on its changes, with the server's reason",
          "no renderable" in (vv.text_content() or ""), (vv.text_content() or "")[:200])
    vv.locator("button[aria-label='Close']").click()
    page.screenshot(path=str(SHOTS / "C-card.png"))


def main():
    if not (APP_DIR / "index.html").is_file():
        print(f"FAIL  no built app at {APP_DIR} — run `npm run ship` in workbench-web/")
        return 1
    port = serve()
    base = f"http://127.0.0.1:{port}"
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        for fn in (scenario_ops, scenario_card):
            ctx = browser.new_context(viewport={"width": 1500, "height": 950})
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
    print("\nPASS workbench_panes" if not fails else f"\nFAIL workbench_panes: {fails} check(s)")
    if _OWN_SHOTS and not fails:
        shutil.rmtree(SHOTS, ignore_errors=True)
    elif fails:
        print(f"screenshots: {SHOTS}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
