#!/usr/bin/env python3
"""The Artifacts window offers an artifact's pages when the render names several.

An artifact with several pages and none at its root (a/index.html,
b/index.html) is answered by the kernel's render with 300 and the pages
(server/routes/artifacts._render_refusal, from entry_pages — the rule every
preview lane uses). The window used to treat any non-OK render as a failure
and read "render failed" (review of preview-animated at 34ac2abe).

Driven against the REAL artifacts app (system-apps/artifacts/) in Chromium,
over a stubbed kernel whose render answers exactly as the kernel does:

  L. the list: both pages offered, the kernel's sentence shown, nothing
     opened for the person, and no "render failed";
  O. picking a page opens it (?entry= carries the choice) in the sealed frame;
  S. the switcher in the viewer bar offers the pages and switching opens the
     other one;
  N. an artifact whose root page exists opens directly — no list, no switcher;
  X. XSS (review of fa0dccbe, B1): page names are FILE NAMES a Host or harness
     wrote. Names that break out of an attribute (x" onclick="…) or carry
     markup (<img onerror=…>) are shown as text, run nothing, and open exactly
     the page they name — in the list and in the switcher.

Run:  python3 scripts/e2e_artifacts_page_list.py
"""
from __future__ import annotations

import os
import socket
import sys
import threading
import time
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

ROOT = Path(os.environ.get("APP_ROOT") or Path(__file__).resolve().parents[1])
SHOTS = Path(os.environ.get("SHOTS_DIR", "/tmp/e2e-artifacts-pages"))
SHOTS.mkdir(parents=True, exist_ok=True)

fails = 0


def check(name, ok, detail=""):
    global fails
    print(("  ok    " if ok else "  FAIL  ") + name + (f"  — {detail}" if detail and not ok else ""))
    if not ok:
        fails += 1


from fastapi import FastAPI  # noqa: E402
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response  # noqa: E402
import uvicorn  # noqa: E402
from gate_loopback import IDLE_KEEP_ALIVE_S  # noqa: E402

PAGES = ["a/index.html", "b/index.html"]
ASKS: list = []
# The shapes tests/test_artifacts_name_inert_js.py (the variant tabs and the
# history strip) pins, plus markup: both quotes, a click handler, a focus
# handler that fires with no click at all, and an element.
RUN = "window.__pwned=(window.__pwned||0)+1"
HOSTILE = [f'x" onclick="{RUN}" data-x="/index.html',
           f'f" autofocus onfocus="{RUN}" data-z="/index.html',
           f"s' onclick='{RUN}' data-q='/index.html",
           f"<img src=x onerror={RUN}>/index.html"]
ARTIFACTS = [
    {"artifact_id": 1, "name": "two pages", "entry": "index.html", "updated_at": "2026-09-30T00:00:00Z"},
    {"artifact_id": 2, "name": "one page", "entry": "index.html", "updated_at": "2026-09-30T00:00:00Z"},
    {"artifact_id": 3, "name": "hostile names", "entry": "index.html", "updated_at": "2026-09-30T00:00:00Z"},
]
app = FastAPI()


@app.get("/api/artifacts")
def list_artifacts():
    return {"artifacts": ARTIFACTS}


@app.get("/api/artifacts/{aid}/render")
def render(aid: int, entry: str = "index.html"):
    ASKS.append((aid, entry))
    if aid == 3:
        if entry in HOSTILE:
            return HTMLResponse("<!DOCTYPE html><html><body><h1 id='t'>HOSTILE PAGE OPENED</h1></body></html>")
        return JSONResponse(status_code=300, content={
            "detail": "4 pages sit at the top of this artifact — choose one to open",
            "entries": HOSTILE, "basis": "tree"})
    if aid == 1:
        if entry in PAGES:
            return HTMLResponse(f"<!DOCTYPE html><html><body><h1 id='t'>PAGE:{entry}</h1></body></html>")
        # The kernel's own answer for several pages and none at the root.
        return JSONResponse(status_code=300, content={
            "detail": "2 pages sit at the top of this artifact — choose one to open: "
                      "a/index.html, b/index.html — name one with entry=",
            "entries": PAGES, "basis": "tree"})
    return HTMLResponse("<!DOCTYPE html><html><body><h1 id='t'>ROOT PAGE</h1></body></html>")


@app.get("/api/artifacts/{aid}/files")
def files(aid: int):
    return {"files": ["index.html"] if aid == 2 else []}


@app.get("/api/appearance/desktop/check")
def desktop_check():
    return {"ok": False, "mode": None, "errors": ["no mode declared"], "warnings": [], "tokens": 0,
            "report": "REFUSED"}


@app.api_route("/api/{rest:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
def other_api(rest: str):
    return JSONResponse({})


def _static(base: Path, rest: str, ctype: str | None = None):
    target = (base / rest).resolve()
    if base.resolve() not in target.parents or not target.is_file():
        return Response(status_code=404)
    return FileResponse(str(target), media_type=ctype)


@app.get("/js/{rest:path}")
def js(rest: str):
    return _static(ROOT / "dashboard" / "base" / "js", rest, "text/javascript")


@app.get("/css/{rest:path}")
def css(rest: str):
    return _static(ROOT / "dashboard" / "base" / "css", rest, "text/css")


@app.get("/system-apps/artifacts/{rest:path}")
def artifacts_static(rest: str):
    return _static(ROOT / "system-apps" / "artifacts", rest)


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


def main() -> int:
    if not (ROOT / "system-apps" / "artifacts" / "app.js").is_file():
        print(f"FAIL  no artifacts app under {ROOT}")
        return 1
    base = f"http://127.0.0.1:{serve()}"
    with sync_playwright() as pw:
        b = pw.chromium.launch()
        pg = b.new_page(viewport={"width": 1300, "height": 850})
        errors: list = []
        pg.on("pageerror", lambda e: errors.append(str(e)))
        pg.goto(f"{base}/system-apps/artifacts/index.html", wait_until="networkidle")
        pg.wait_for_function("() => window.App && window.App.state.artifacts.length === 3", timeout=10_000)

        print("L. several pages, none at the root: the list")
        pg.evaluate("() => window.App.openArtifact(1)")
        box = pg.locator("#viewer-pages")
        expect(box).to_be_visible(timeout=5_000)
        got = box.locator("[data-page]").evaluate_all("els => els.map(e => e.dataset.page)")
        check("L: both pages are offered", got == PAGES, repr(got))
        check("L: the kernel's sentence says why", "choose one to open" in (box.text_content() or ""))
        check("L: nothing is opened for the person (the frame stays hidden)",
              pg.locator("#artifact-frame").is_hidden())
        status = pg.locator("#viewer-status").text_content() or ""
        check("L: it is not reported as a failed render", "failed" not in status, repr(status))
        pg.screenshot(path=str(SHOTS / "L-list.png"))

        print("O. picking a page opens it")
        box.locator("[data-page='b/index.html']").click()
        frame = pg.frame_locator("#artifact-frame")
        expect(frame.locator("#t")).to_have_text("PAGE:b/index.html", timeout=5_000)
        check("O: the chosen page opens in the frame (?entry= carries the choice)",
              (1, "b/index.html") in ASKS, repr(ASKS))
        check("O: the list is gone", box.is_hidden())

        print("S. the switcher moves between the pages")
        sw = pg.locator("#page-switch")
        opts = sw.locator("option").evaluate_all("els => els.map(e => e.value)") if sw.is_visible() else []
        check("S: the switcher offers both pages, on the open one",
              opts == PAGES and sw.input_value() == "b/index.html", repr((opts, sw.input_value())))
        sw.select_option("a/index.html")
        expect(frame.locator("#t")).to_have_text("PAGE:a/index.html", timeout=5_000)
        check("S: switching opens the other page", (1, "a/index.html") in ASKS, repr(ASKS))
        pg.screenshot(path=str(SHOTS / "S-switched.png"))

        print("N. an artifact with a root page opens directly")
        pg.evaluate("() => window.App.openArtifact(2)")
        expect(frame.locator("#t")).to_have_text("ROOT PAGE", timeout=5_000)
        check("N: no list", box.is_hidden())
        check("N: no switcher", sw.is_hidden())

        print("X. hostile page names are text, run nothing, and open what they name")
        pg.evaluate("() => window.App.openArtifact(3)")
        expect(box).to_be_visible(timeout=5_000)
        buttons = box.locator("button")
        texts = buttons.evaluate_all("els => els.map(e => e.textContent)")
        check("X: each hostile name is shown as its literal text", texts == HOSTILE, repr(texts))
        NO_HANDLER = """els => els.every(e => [...e.attributes].every(a =>
                            !a.name.startsWith('on') && a.name !== 'autofocus'))"""
        check("X: no markup was injected (no <img>, no handler or autofocus attribute)",
              box.locator("img").count() == 0 and buttons.evaluate_all(NO_HANDLER))
        check("X: each button carries its name verbatim as data-page",
              buttons.evaluate_all("els => els.map(e => e.dataset.page)") == HOSTILE)
        for i, name in enumerate(HOSTILE):
            if i:
                pg.evaluate("() => window.App.openArtifact(3)")
                expect(box).to_be_visible(timeout=5_000)
            box.locator("button").nth(i).click()
            expect(frame.locator("#t")).to_have_text("HOSTILE PAGE OPENED", timeout=5_000)
            check(f"X: clicking name {i + 1} opens exactly the page it names", ASKS[-1] == (3, name),
                  repr(ASKS[-1]))
        opts = sw.locator("option")
        check("X: the switcher carries the names verbatim, as values and labels",
              opts.evaluate_all("els => els.map(e => e.value)") == HOSTILE
              and opts.evaluate_all("els => els.map(e => e.textContent)") == HOSTILE)
        check("X: no switcher option carries a handler", opts.evaluate_all(NO_HANDLER))
        pg.wait_for_timeout(300)
        pwned = pg.evaluate("() => window.__pwned || 0")
        check("X: nothing the names carried ran", pwned == 0, f"ran {pwned} time(s)")

        check("no uncaught page errors", not errors, "; ".join(errors)[:300])
        b.close()
    print("\nPASS artifacts_page_list" if not fails else f"\nFAIL artifacts_page_list: {fails} check(s)")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
