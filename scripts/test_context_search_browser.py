#!/usr/bin/env python3
"""Gate: the Context app's everything-search in a real browser.

The Context app's box searches the five stores the Host can see through
POST /api/search, the one index. The route here is the REAL one
(server/routes/search.py): its body model, its read:search check against a
window token minted from the Context manifest's own scopes, and its secret cut
(server/secret_cut.py) all run as the kernel runs them. Only the store engine
behind it, memory_retrieval.unified_search, is a recorder that answers in the
shape each store answers in. The rest of what the page reads on load (its
rail, files, placement, coverage) is a small fake. The page is the app's own
index.html with the desktop's stylesheets, in headless Chromium, at 1440 x 900.

  A. one search over five stores: pasted words go out once, after the box's
     wait, as a POST whose URL is the bare route and whose body is {q, limit};
     the documents-only route is never called; the answer is drawn under five
     headings in order (your documents grouped by file, the Host's memory,
     projects, code, the Host's skills — a skill's name, who wrote
     it and its one line), each saying how it searched; the weak matches
     the relevance floor left out are counted; a store that could not run
     says why, and the line at the top counts it; a hit's words are text,
     never markup; a document opens in the inspector.
  T. a secret TYPED, a key at a time with a pause after each longer than the
     box's wait (the gate 4092bed46 added for the Workbench, run here): a
     search goes out after every key, and no URL the page asks for, search or
     not, holds any of what was typed; every search is a POST with the words
     in its body; once the key is whole, the real route cuts it before any
     store is asked.
  S. a slow answer to an older search never draws over the newer one.
  E. a refusal is said in words: a Host whose brain is not up, and a window
     whose token lacks read:search.

Container-free: no pod, no database. Needs Playwright with Chromium, fastapi
and uvicorn on a loopback port, and the repo's brain/ on the path (the secret
cut reads brain/mastercode/secret_shapes.json). Screenshots go to a mkdtemp,
removed on a pass (SHOTS_DIR keeps them). ONLY=scenario_typed,... runs only
those.

  python3 scripts/test_context_search_browser.py
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import socket
import sys
import tempfile
import threading
import time
from pathlib import Path
from urllib.parse import unquote, urlsplit

ROOT = Path(os.environ.get("APP_ROOT") or Path(__file__).resolve().parents[1])
for p in (str(ROOT / "brain"), str(ROOT), str(ROOT / "server")):
    if p not in sys.path:
        sys.path.insert(0, p)

import uvicorn  # noqa: E402
from gate_loopback import IDLE_KEEP_ALIVE_S  # noqa: E402
from fastapi import FastAPI, Request  # noqa: E402
from fastapi.responses import FileResponse, JSONResponse, Response  # noqa: E402
from playwright.sync_api import expect, sync_playwright  # noqa: E402

from mastercode import memory_retrieval as mr  # noqa: E402

# The route logs each cut it makes (rule ids only); the typed scenario makes
# dozens, and the checks below say what was cut.
logging.getLogger("privatae.pc.secret_cut").setLevel(logging.ERROR)

APP_DIR = ROOT / "system-apps" / "context-manager"
CSS_DIR = ROOT / "dashboard" / "base" / "css"
_OWN_SHOTS = not os.environ.get("SHOTS_DIR")
SHOTS = Path(os.environ["SHOTS_DIR"]) if not _OWN_SHOTS else Path(tempfile.mkdtemp(prefix="context-search-"))
SHOTS.mkdir(parents=True, exist_ok=True)
ONLY = {x.strip() for x in os.environ.get("ONLY", "").split(",") if x.strip()}

# The box's wait after the last key (SEARCH_DEBOUNCE_MS in app.js), and the
# pause after each typed key: longer, on the page's own clock, so the search
# for what is in the box so far goes out before the next key.
DEBOUNCE_MS = 350
TYPING_PAUSE_MS = 450
SEARCH_PATH = "/api/search"
SECRET_KEY = "sk-ant-api03-AbCdEfGhIjKlMnOpQrStUvWxYz0123456789abcdefghij"
SECRET_SOFT = "hunter2hunter2"

fails = 0


def check(name, ok, detail=""):
    global fails
    print(("  ok    " if ok else "  FAIL  ") + name + (f"  — {str(detail)[:400]}" if detail and not ok else ""))
    if not ok:
        fails += 1


def manifest_scopes() -> list[str]:
    """The Context app's declared scopes, as the kernel mints its token."""
    text = (APP_DIR / "app.yaml").read_text().split("\npermissions:")[1]
    return [ln.strip()[2:].strip() for ln in text.splitlines() if ln.strip().startswith("- ")]


# ── the kernel side ──────────────────────────────────────────────────────────
STATE: dict = {"asked": [], "slow": {}, "down": None, "scopes": None}


def _iso(days_ago: int) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - days_ago * 86400))


def answer(query: str, stores, limit: int) -> dict:
    """Each store's answer in the shape memory_retrieval.unified_search gives
    it, with the words asked in each hit so a check can tell searches apart."""
    out = {
        "query": query, "stores": list(stores or mr.SEARCH_STORES), "limit": limit, "unavailable": [],
        "documents": {"arms": ["semantic", "lexical"], "suppressed": 1, "results": [
            {"id": 11, "group_key": "batch:budget", "metadata": json.dumps({"from_file": "budget.csv"}),
             "title": "budget.csv rows 1-40", "source": "csv_upload", "content": f"Rows about {query}, month by month.",
             "matched": ["semantic", "lexical"], "similarity": 0.82},
            {"id": 12, "group_key": "batch:budget", "metadata": {"from_file": "budget.csv"},
             "title": "budget.csv rows 41-80", "source": "csv_upload", "content": "More rows.", "matched": ["lexical"]},
            {"id": 13, "group_key": None, "title": "notes.md", "source": "markdown_upload",
             "content": f"A note on {query}.", "matched": ["lexical"]},
        ]},
        "memory": {"arms": ["lexical"], "suppressed": 2, "degraded": True,
                   "semantic_unavailable": "the embedding endpoint is not answering", "lexical_unavailable": None,
                   "hits": [{"id": 1, "memory_type": "fact", "created_at": _iso(3), "matched": ["lexical"],
                             "content": f"You track {query} every week. <img src=x onerror=\"window.__pwned=1\">"}]},
        "projects": {"semantic": False, "results": [
            {"id": 42, "name": f"Project on {query}", "description": "Where the numbers go.", "group": "open",
             "last_activity_at": _iso(1)}]},
        "code": {"semantic": True, "projects": ["ledger"], "results": [
            {"project": "ledger", "file_path": "src/report.py", "chunk_index": 0, "start_line": 12,
             "symbol": "monthly_report", "content": f"def monthly_report():\n    # {query}\n    return 1"}]},
        "skills": {"searched": 58, "results": [
            {"name": "port", "author": "host", "score": 1.5,
             "description": f"Port a codebase on {query} <img src=x onerror=\"window.__pwned=2\">",
             "open": 'read(tool=\'context\', args={"action":"read","path":"skills/port"})'}]},
    }
    down = STATE.get("down")
    if down:
        out[down] = {"unavailable": "timed out after 8s"}
        out["unavailable"] = [down]
    return out


async def recording_search(pool, entity_id, query, *, stores=None, limit=10, timeout_s=None):
    """memory_retrieval.unified_search's seam: what the stores were asked."""
    STATE["asked"].append({"query": query, "stores": stores, "limit": limit})
    delay = STATE["slow"].get(query)
    if delay:
        await asyncio.sleep(delay)
        STATE["slow_answered"] = True
    return answer(query, stores, limit)


def build_app() -> FastAPI:
    from server import brain_service as bs
    from server.applications.tokens import AppToken
    from server.routes import search as search_route

    mr.unified_search = recording_search
    bs._pool, bs._entity_id = "POOL", 1
    app = FastAPI()

    @app.middleware("http")
    async def window_token(request: Request, call_next):
        # What the kernel's token middleware leaves on the request for a window
        # the Context app was launched in: its declared scopes, nothing more.
        if request.url.path.startswith("/api/"):
            scopes = STATE["scopes"] if STATE["scopes"] is not None else manifest_scopes()
            request.state.app_token = AppToken(app_id="context-manager", permissions=scopes, window_id="gate", iat=0)
        return await call_next(request)

    app.include_router(search_route.router)

    # What the page reads on load and around a search, in the shapes it reads.
    @app.get("/api/brain/context/tags")
    def tags():
        return {"tags": [{"tag": "Receipts", "count": 2}]}

    @app.get("/api/context-manager/sources")
    def sources():
        return {"sources": [{"machine": "platform", "backend": "pg", "files": 3, "bytes": 2048, "online": True}],
                "stores": []}

    @app.get("/api/context-manager/files")
    def files():
        if STATE.get("files_detail") is not None:  # scenario R: a refusal as FastAPI shapes it
            return JSONResponse({"detail": STATE["files_detail"]}, status_code=422)
        return {"files": [], "total": 0, "page": 1, "per_page": 50}

    @app.get("/api/context-manager/placement")
    def placement():
        return {"current": {"backend": "pg"}, "routes": []}

    @app.get("/api/context-manager/embedding")
    def embedding():
        return {"unembedded": 2, "embedded": 40}

    @app.get("/api/brain/context/{ctx_id}/content")
    def content(ctx_id: int):
        return {"content": f"The whole of piece {ctx_id}.", "storage_backend": "pg"}

    @app.post("/api/brain/context/search")
    def old_documents_search():
        STATE["old_route"] = STATE.get("old_route", 0) + 1
        return JSONResponse({"results": [], "arms": []})

    @app.get("/css/{name}")
    def css(name: str):
        target = (CSS_DIR / name).resolve()
        if target.parent != CSS_DIR.resolve() or not target.is_file():
            return Response(status_code=404)
        return FileResponse(str(target), media_type="text/css")

    @app.get("/system-apps/context-manager/{rest:path}")
    def static(rest: str):
        target = (APP_DIR / (rest or "index.html")).resolve()
        if APP_DIR.resolve() not in target.parents:
            return Response(status_code=403)
        return FileResponse(str(target)) if target.is_file() else Response(status_code=404)

    return app


def serve(app) -> int:
    # Loopback on purpose: the only client is the headless Chromium this same
    # process launches on this same box.
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error",
                                           timeout_keep_alive=IDLE_KEEP_ALIVE_S))
    threading.Thread(target=server.run, daemon=True).start()
    deadline = time.time() + 15
    while not server.started and time.time() < deadline:
        time.sleep(0.05)
    return port


def reset():
    from server import brain_service as bs
    STATE.update({"asked": [], "slow": {}, "down": None, "scopes": None, "old_route": 0, "slow_answered": False,
                  "files_detail": None})
    bs._pool = "POOL"


# ── the page ─────────────────────────────────────────────────────────────────
BOX = "#exp-search"
RESULTS = "#search-results"


def watch(page) -> list:
    """Every request the page makes from now on, as the browser sends it:
    method, whole URL, body. Read in the browser and not at the server, so a
    request the server would refuse is seen all the same."""
    seen: list = []
    page.on("request", lambda req: seen.append({"method": req.method, "url": req.url, "body": req.post_data}))
    return seen


def searches(seen) -> list:
    return [x for x in seen if urlsplit(x["url"]).path == SEARCH_PATH]


def pump_until(page, cond, secs=6.0) -> bool:
    """Wait for `cond()`, letting the browser's events in meanwhile: the sync
    API delivers them inside a page call, never during time.sleep."""
    deadline = time.monotonic() + secs
    while time.monotonic() < deadline:
        if cond():
            return True
        page.wait_for_timeout(20)
    return cond()


def clock_pause(page):
    """Stop the page's clock a second on (pause_at only jumps forward)."""
    for _ in range(5):
        try:
            page.clock.pause_at((page.evaluate("Date.now()") + 1_000) / 1_000)
            return
        except Exception as e:  # "Cannot fast-forward to the past": the read is stale
            if "past" not in str(e):
                raise
    raise AssertionError("the page's clock would not pause")


def open_app(page, base):
    page.goto(f"{base}/system-apps/context-manager/index.html", wait_until="domcontentloaded", timeout=15_000)
    expect(page.locator("#rail-collections [data-lib='all']")).to_have_count(1, timeout=10_000)


def store_titles(page) -> list:
    return page.locator(f"{RESULTS} .exp-store").evaluate_all("els => els.map(e => e.dataset.store)")


def section(page, store):
    return page.locator(f'{RESULTS} .exp-store[data-store="{store}"]')


# ── scenarios ────────────────────────────────────────────────────────────────
def scenario_six_stores(page, base):
    print("A. one search over five stores, its words in the body, drawn under five headings")
    seen = watch(page)
    open_app(page, base)
    start = len(seen)
    page.locator(BOX).fill("trading journal")
    expect(section(page, "skills")).to_have_count(1, timeout=8_000)
    s = searches(seen[start:])
    check("A: one search went out for the pasted words", len(s) == 1, [(x["method"], x["url"]) for x in s])
    if s:
        u = urlsplit(s[0]["url"])
        body = json.loads(s[0]["body"] or "null")
        check("A: ...a POST on the bare route: no query string", s[0]["method"] == "POST" and u.query == "", s[0]["url"])
        check("A: ...with {q, limit} in the body and no store named, so every store is searched",
              body == {"q": "trading journal", "limit": 50}, s[0]["body"])
    check("A: the stores were asked for the words, every store, 50 each",
          STATE["asked"][-1:] == [{"query": "trading journal", "stores": None, "limit": 50}], STATE["asked"][-1:])
    check("A: the documents-only route is never called", not STATE.get("old_route"), STATE.get("old_route"))
    check("A: five headings, your documents first, then the Host's memory, projects, code and skills",
          store_titles(page) == ["documents", "memory", "projects", "code", "skills"], store_titles(page))
    heads = page.locator(f"{RESULTS} .exp-store-title").all_text_contents()
    check("A: ...named for a person", [h.lower() for h in heads] ==
          ["your documents", "your host's memory", "projects", "your code", "your host's skills"], heads)
    docs = section(page, "documents")
    check("A: documents are grouped by file: budget.csv's two pieces under one name, notes.md under its own",
          docs.locator(".exp-hitgroup-title").all_text_contents() == ["budget.csv", "notes.md"]
          and "2 matches" in (docs.locator(".exp-hitgroup").first.text_content() or ""),
          docs.locator(".exp-hitgroup-title").all_text_contents())
    dnote = docs.locator(".exp-store-note").text_content() or ""
    check("A: documents say how they searched, and count the weak match the floor left out",
          "searched by meaning and exact words" in dnote and "1 weak match left out" in dnote, dnote)
    mem = section(page, "memory")
    mnote = mem.locator(".exp-store-note").text_content() or ""
    check("A: memory says it searched by exact words only, and why", "exact words only" in mnote
          and "the embedding endpoint is not answering" in mnote and "2 weak matches left out" in mnote, mnote)
    check("A: a hit's words are text, never markup (no element made from them, no script run)",
          mem.locator("img").count() == 0 and "<img src=x" in (mem.text_content() or "")
          and not page.evaluate("Boolean(window.__pwned)"), mem.inner_html()[:300])
    check("A: the project and the code hit are listed",
          "Project on trading journal" in (section(page, "projects").text_content() or "")
          and "ledger/src/report.py:12" in (section(page, "code").text_content() or "")
          and "monthly_report" in (section(page, "code").text_content() or ""))
    check("A: code says how many projects are indexed",
          "1 indexed project" in (section(page, "code").locator(".exp-store-note").text_content() or ""))
    sk = section(page, "skills")
    sk_text = sk.text_content() or ""
    check("A: a skill shows its name, who wrote it and its one line, never the call or the skill's text",
          "port" in (sk.locator(".exp-hit-title").first.text_content() or "")
          and "written by your Host" in sk_text and "Port a codebase on trading journal" in sk_text
          and "read(tool=" not in sk_text, sk_text[:300])
    check("A: ...its line is text, never markup", sk.locator("img").count() == 0 and "<img src=x" in sk_text
          and not page.evaluate("Boolean(window.__pwned)"), sk.inner_html()[:300])
    check("A: skills say how they searched and how many",
          "58 skills searched" in (sk.locator(".exp-store-note").text_content() or ""))
    cov = page.locator("#search-coverage").text_content() or ""
    check("A: the line at the top counts the results and the document pieces not embedded yet",
          "7 results" in cov and "2 of your document pieces aren't embedded yet" in cov, cov)
    page.screenshot(path=str(SHOTS / "six-stores.png"), full_page=True)

    # A store that could not run says why, and the top line counts it.
    STATE["down"] = "projects"
    page.locator(BOX).fill("trading journals")
    expect(section(page, "projects").locator(".exp-store-empty.err")).to_have_count(1, timeout=8_000)
    jt = section(page, "projects").text_content() or ""
    check("A: a store that could not run says so and why", "Could not search this right now: timed out after 8s" in jt, jt)
    cov = page.locator("#search-coverage").text_content() or ""
    check("A: ...and the top line says one store could not be searched", "1 store could not be searched" in cov, cov)

    # A document opens in the inspector, as it did before.
    section(page, "documents").locator(".exp-hitgroup-title", has_text="notes.md").click()
    expect(page.locator("#insp-preview")).to_have_text("The whole of piece 13.", timeout=5_000)
    check("A: a document hit opens in the inspector", page.locator("#insp-title").text_content() == "notes.md")


def scenario_typed(page, base):
    print("T. a secret TYPED, a key at a time with a pause after each, never goes into a URL")
    seen = watch(page)
    page.clock.install()
    open_app(page, base)
    clock_pause(page)
    password = f"password = {SECRET_SOFT}"
    typed = {}
    box = page.locator(BOX)
    box.focus()
    for what, text in (("password", password), ("key", SECRET_KEY)):
        start, asked0 = len(seen), len(STATE["asked"])
        ok = True
        for i, ch in enumerate(text):
            n = len(searches(seen))
            page.keyboard.type(ch)
            page.clock.run_for(TYPING_PAUSE_MS)
            if len(text[: i + 1].strip()) >= 2:  # the box searches from two characters
                ok = pump_until(page, lambda: len(searches(seen)) > n) and ok
        # the last search's answer is in before the box is cleared
        ok = pump_until(page, lambda: len(STATE["asked"]) - asked0 >= len(searches(seen[start:]))) and ok
        typed[what] = {"text": text, "ok": ok, "requests": seen[start:], "asked": STATE["asked"][asked0:]}
        box.fill("")
        page.clock.run_for(TYPING_PAUSE_MS)
    page.clock.resume()

    want = sum(len(t["text"]) - 1 for t in typed.values())
    got = [x for t in typed.values() for x in searches(t["requests"])]
    check(f"T: a search went out after every key from the second on ({want} keys): the checks below are not vacuous",
          all(t["ok"] for t in typed.values()) and len(got) >= want,
          (len(got), [(k, t["ok"], len(searches(t["requests"]))) for k, t in typed.items()]))
    every = [x for t in typed.values() for x in t["requests"]]
    urls = [unquote(x["url"]) for x in every]

    def most_held(secret, texts):
        return max((k for k in range(len(secret), 0, -1) if any(secret[:k] in t for t in texts)), default=0)

    leaked = [u for u in urls if any(f in u for f in ("pass", "hunter", "sk-ant", "api03", "AbCdEf"))]
    check("T: no URL the page asked for while typing holds any typed text, search or not",
          bool(urls) and not leaked,
          f"{len(leaked)} of {len(urls)} URLs carry typed text, e.g. {leaked[:3]}; the most of the "
          f"password's value in a URL: {SECRET_SOFT[:most_held(SECRET_SOFT, urls)]!r}; "
          f"of the key: {SECRET_KEY[:most_held(SECRET_KEY, urls)]!r}")
    check("T: ...every search is a POST on the bare route, with no query string",
          bool(got) and all(x["method"] == "POST" and urlsplit(x["url"]).query == "" for x in got),
          sorted({(x["method"], urlsplit(x["url"]).query) for x in got})[:4])
    bodies = []
    for x in got:
        try:
            bodies.append(json.loads(x["body"] or ""))
        except ValueError:
            bodies.append(None)
    check("T: ...with the words in the body, as {q, limit}",
          bool(bodies) and all(isinstance(b, dict) and set(b) == {"q", "limit"} and isinstance(b["q"], str)
                               for b in bodies), [x["body"] for x in got[:2]])
    asked_key = [a["query"] for a in typed["key"]["asked"]]
    check("T: once the key is whole, the route cuts it before any store is asked",
          bool(asked_key) and not any(SECRET_KEY in q for q in asked_key), [q[:70] for q in asked_key[-2:]])
    last_body = (bodies[-1] or {}).get("q", "") if bodies else ""
    check("T: ...it was whole in the last body the page sent (the cut above is the route's, not the page's)",
          last_body == SECRET_KEY, last_body[:70])
    pw_asked = [a["query"] for a in typed["password"]["asked"]]
    # Not a check: what the cut cannot do. A soft shape (a password assignment)
    # is not cut by the route, and a hard one is recognised once it is whole,
    # so the shorter prefixes reach the stores. They travel in a body, never a URL.
    print(f"        (in the body, before the cut can see a whole shape: {most_held(SECRET_KEY, asked_key)} of the "
          f"key's {len(SECRET_KEY)} characters reached the stores; the password's value, a soft shape the route "
          f"does not cut, reached them whole: {any(SECRET_SOFT in q for q in pw_asked)})")


def scenario_stale(page, base):
    print("S. a slow answer to an older search never draws over the newer one")
    seen = watch(page)
    open_app(page, base)
    STATE["slow"]["slow words"] = 1.5
    page.locator(BOX).fill("slow words")
    check("S: the older search went out", pump_until(page, lambda: any(a["query"] == "slow words"
                                                                         for a in STATE["asked"])))
    page.locator(BOX).fill("fast words")
    expect(section(page, "projects")).to_contain_text("Project on fast words", timeout=8_000)
    check("S: the older one answered after the newer one drew",
          pump_until(page, lambda: STATE.get("slow_answered"), secs=6))
    page.wait_for_timeout(300)
    text = page.locator(RESULTS).text_content() or ""
    check("S: the newer search's answer stays on screen", "fast words" in text and "slow words" not in text, text[:200])
    check("S: the crumb names the newer words", "fast words" in (page.locator("#search-crumb").text_content() or ""))
    check("S: both went out as POSTs on the bare route",
          len(searches(seen)) == 2 and all(x["method"] == "POST" and not urlsplit(x["url"]).query
                                           for x in searches(seen)), [(x["method"], x["url"]) for x in searches(seen)])


def scenario_refusals(page, base):
    print("E. a refusal is said in words")
    from server import brain_service as bs
    open_app(page, base)
    bs._pool = None
    page.locator(BOX).fill("budget")
    expect(page.locator(f"{RESULTS} .status-banner.error")).to_contain_text("Brain not initialized", timeout=8_000)
    check("E: a Host whose brain is not up says so, and asks no store", not STATE["asked"])
    bs._pool = "POOL"
    STATE["scopes"] = [s for s in manifest_scopes() if s != "read:search"]
    page.locator(BOX).fill("budgets")
    expect(page.locator(f"{RESULTS} .status-banner.error")).to_contain_text("read:search", timeout=8_000)
    check("E: a window whose token lacks read:search is refused in words, and no store is asked", not STATE["asked"])


def scenario_long_paste(page, base):
    """The route takes at most 2,000 characters of `q` (server/routes/search.py)
    and answers more with a 422. The documents-only route this box used before
    had no ceiling, so a long passage pasted to find what is like it searched;
    it must still, on its first 2,000 characters, saying so, and never draw an
    error ("[object Object]" was what the 422 drew)."""
    print("L. a passage longer than the route takes still searches, on its first 2,000 characters")
    seen = watch(page)
    open_app(page, base)
    start = len(seen)
    passage = ("budget notes for the trading journal " * 70)[:2500].strip()
    page.locator(BOX).fill(passage)
    drew = pump_until(page, lambda: section(page, "skills").count() == 1
                      or page.locator(f"{RESULTS} .status-banner.error").count() > 0, secs=8.0)
    err = page.locator(f"{RESULTS} .status-banner.error")
    check("L: the answer is drawn under the five headings, not an error",
          drew and err.count() == 0 and section(page, "skills").count() == 1,
          (err.first.text_content() if err.count() else "nothing drawn"))
    s = searches(seen[start:])
    sent = [len((json.loads(x["body"] or "{}") or {}).get("q") or "") for x in s]
    check("L: ...one POST, its words cut to the route's 2,000 characters",
          len(s) == 1 and s[0]["method"] == "POST" and sent == [2000], (len(passage), sent))
    check("L: ...and the stores were asked for those 2,000 characters",
          [len(a["query"]) for a in STATE["asked"]] == [2000], [len(a["query"]) for a in STATE["asked"]])
    cov = page.locator("#search-coverage").text_content() or ""
    check("L: the line at the top says only the first 2,000 characters were searched",
          "searched the first 2,000 characters" in cov, cov)
    page.locator(BOX).fill("trading journal")
    covs = page.locator("#search-coverage")
    after = pump_until(page, lambda: len(STATE["asked"]) == 2 and "result" in (covs.text_content() or ""), secs=8.0)
    check("L: a short search after it does not say so",
          after and "first 2,000" not in (covs.text_content() or ""), covs.text_content())


def scenario_validation_words(page, base):
    """Another Context route answering a 422 the way FastAPI shapes one:
    `detail` a list of {loc, msg, type, input}. The box says each `msg` in
    words, never "[object Object]", and never the `input` (what was sent). A
    detail that is neither a string nor that list falls back to the status."""
    print("R. a refusal with FastAPI's list of reasons is said in words")
    STATE["files_detail"] = [
        {"loc": ["query", "per_page"], "msg": "Input should be less than or equal to 200",
         "type": "less_than_equal", "input": "INPUT-ECHO-7781"},
        {"loc": ["query", "page"], "msg": "Input should be a valid integer",
         "type": "int_parsing", "input": "INPUT-ECHO-7782"},
    ]
    banner = page.locator("#files-table .status-banner.error")
    open_app(page, base)
    expect(banner).to_have_count(1, timeout=8_000)
    text = banner.text_content() or ""
    check("R: the file list's refusal says each reason in words",
          "Input should be less than or equal to 200; Input should be a valid integer" in text, text)
    check("R: ...never \"[object Object]\", and never what was sent", "[object" not in text and "INPUT-ECHO" not in text, text)
    STATE["files_detail"] = {"code": 7}
    open_app(page, base)
    expect(banner).to_have_count(1, timeout=8_000)
    text = banner.text_content() or ""
    check("R: a detail that is neither words nor a list of reasons falls back to the status",
          "[object" not in text and "Unprocessable" in text, text)


SCENARIOS = [scenario_six_stores, scenario_typed, scenario_stale, scenario_refusals, scenario_long_paste,
             scenario_validation_words]


def main() -> int:
    if not (APP_DIR / "index.html").is_file():
        print(f"FAIL  no Context app at {APP_DIR}")
        return 1
    check("the Context manifest declares read:search (its window token is minted from it)",
          "read:search" in manifest_scopes(), manifest_scopes())
    port = serve(build_app())
    base = f"http://127.0.0.1:{port}"
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        for fn in SCENARIOS:
            if ONLY and fn.__name__ not in ONLY:
                continue
            reset()
            ctx = browser.new_context(viewport={"width": 1440, "height": 900}, locale="en-US")
            page = ctx.new_page()
            errors = []
            page.on("pageerror", lambda e, errors=errors: errors.append(str(e)))
            try:
                fn(page, base)
            except Exception as e:  # a scenario that throws is a failed gate, not a crash
                check(f"{fn.__name__}: completed", False, f"{type(e).__name__}: {e}")
                page.screenshot(path=str(SHOTS / f"{fn.__name__}-error.png"))
            check(f"{fn.__name__}: no uncaught page errors", not errors, "; ".join(errors)[:300])
            ctx.close()
        browser.close()
    print("\nPASS context_search_browser" if not fails else f"\nFAIL context_search_browser: {fails} check(s)")
    if _OWN_SHOTS and not fails:
        shutil.rmtree(SHOTS, ignore_errors=True)
    else:
        print(f"screenshots: {SHOTS}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
