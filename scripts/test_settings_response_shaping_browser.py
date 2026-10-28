#!/usr/bin/env python3
"""Gate: Settings → Response Shaping in a real browser, over the real brain
routes and a real kernel schema.

No mock of the API: server/routes/brain.py is mounted as the kernel mounts it,
its handlers read and write entity_registry on a fresh clone of the schema a
Host builds at boot, and the window token carries exactly the permissions the
Settings manifest declares (system-apps/settings/app.yaml), as the kernel mints
it. The page is the app's own index.html, served with the desktop's
stylesheets, in headless Chromium. Every scenario at 1440 x 900, the phone one
at 390 x 844 with touch. Each control is set in the page, then read back from
the database AND from a fresh window:

  O. the tab: its own button and section, beside Synaptive and not inside it;
     the three positions and the ten sliders in Invariant 9's order, each drawn
     from the stored row.
  X. how much the Host explains: one click saves it (the request names the
     level), the row's other behavioural settings are kept, and a new window
     opens on it.
  W. personality: a slider moved with the keyboard shows its value and the
     tendency line for that position; Save lists the change, from and to;
     Cancel sends nothing; Save writes seed and current together; a new window
     opens on the saved value.
  D. Discard puts the sliders back and sends nothing.
  K. a fresh read (the tab again) keeps a slider the person moved and has not
     saved, and takes everything else, such as a level the Host's tool set.
  S. a read answered after a newer choice never draws over it.
  R. a refusal is said in words, and the moved slider keeps its place.
  M. the manifest's scopes are the ones the routes take: without
     write:brain_personality the slider save is refused, in words, and nothing
     is written.
  T. a dimension name from the row is text, never markup.
  L. a cseries:app-view message moves an open window to the tab.
  P. the phone: nothing wider than the screen, sliders a finger can use, the
     confirm inside the screen.

The envelope (how far anything but the person could move a slider before the
drift check put it back) left with the simulated psyche, and its scenario
and checks with it: the sliders
are the person's and nothing else moves them.

Fresh clone of the kernel schema on TEST_PG_DSN / PEER_TEST_DSN / TEST_DSN,
else a throwaway cseries-postgres through docker; Playwright with Chromium,
fastapi and uvicorn on a loopback port; screenshots in a mkdtemp, removed on a
pass (SHOTS_DIR keeps them). ONLY=scenario_phone,... runs only those.

  python3 scripts/test_settings_response_shaping_browser.py
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(os.environ.get("APP_ROOT") or Path(__file__).resolve().parents[1])
for p in (str(ROOT / "brain"), str(ROOT), str(ROOT / "scripts")):
    if p not in sys.path:
        sys.path.insert(0, p)

import asyncpg  # noqa: E402
import uvicorn  # noqa: E402
from gate_loopback import IDLE_KEEP_ALIVE_S  # noqa: E402
import yaml  # noqa: E402
from fastapi import FastAPI, Request  # noqa: E402
from fastapi.responses import FileResponse, JSONResponse, Response  # noqa: E402
from playwright.sync_api import expect, sync_playwright  # noqa: E402

from kernel_schema import kernel_schema_db, throwaway_server  # noqa: E402

SERVER = os.environ.get("TEST_PG_DSN") or os.environ.get("PEER_TEST_DSN") or os.environ.get("TEST_DSN")
if not SERVER and subprocess.run(["docker", "info"], capture_output=True).returncode != 0:
    print("SKIP-LOUD: no test server (TEST_PG_DSN/PEER_TEST_DSN/TEST_DSN) and docker unavailable — "
          "this gate drives the real brain routes on a real database.")
    sys.exit(2)

APP_DIR = ROOT / "system-apps" / "settings"
CSS_DIR = ROOT / "dashboard" / "base" / "css"
E = 1
_OWN_SHOTS = not os.environ.get("SHOTS_DIR")
SHOTS = Path(os.environ["SHOTS_DIR"]) if not _OWN_SHOTS else Path(tempfile.mkdtemp(prefix="settings-shaping-"))
SHOTS.mkdir(parents=True, exist_ok=True)
ONLY = {x.strip() for x in os.environ.get("ONLY", "").split(",") if x.strip()}

# What the kernel mints a Settings window's token from.
MANIFEST_PERMS = list(yaml.safe_load((APP_DIR / "app.yaml").read_text()).get("permissions") or [])
DIMS = ["warmth", "directness", "humor", "formality", "curiosity",
        "assertiveness", "patience", "vulnerability", "energy", "depth"]

fails = 0


def check(name, ok, detail=""):
    global fails
    print(("  ok    " if ok else "  FAIL  ") + name + (f"  — {str(detail)[:400]}" if detail and not ok else ""))
    if not ok:
        fails += 1


# ── the kernel side: the real router, a real pool, the app's own token ───────
STATE: dict = {"calls": [], "slow_after": None, "pool_off": False, "perms": None, "dsn": None, "pool": None}


def build_app(dsn: str) -> FastAPI:
    from server import brain_service as bs
    from server.applications.tokens import AppToken
    from server.routes import brain as brain_routes

    @contextlib.asynccontextmanager
    async def lifespan(_app):
        STATE["pool"] = await asyncpg.create_pool(dsn, min_size=1, max_size=4)
        bs._pool = STATE["pool"]
        bs._entity_id = E
        yield
        await STATE["pool"].close()

    app = FastAPI(lifespan=lifespan)

    @app.middleware("http")
    async def window_token(request: Request, call_next):
        # What the kernel's token middleware leaves on the request for a window
        # the Settings app was launched in: its declared scopes, nothing more.
        if request.url.path.startswith("/api/"):
            body = await request.body()
            hold = STATE.get("hold_put")
            # A level PUT reaches the route late, so a read made meanwhile is
            # answered from the row as it was before the person's choice.
            if hold and request.method == "PUT" and request.url.path == hold[0]:
                STATE["hold_put"] = None
                await asyncio.sleep(hold[1])
            STATE["calls"].append({"method": request.method, "path": request.url.path,
                                   "body": json.loads(body) if body else None})
            perms = STATE["perms"] if STATE["perms"] is not None else MANIFEST_PERMS
            request.state.app_token = AppToken(app_id="settings", permissions=list(perms),
                                               window_id="gate", iat=0)
            bs._pool = None if STATE["pool_off"] else STATE["pool"]
            response = await call_next(request)
            slow = STATE.get("slow_after")
            # The read is made now; its answer is held back, so it reaches the
            # page after anything the person does in the meantime.
            if slow and request.method == "GET" and request.url.path == slow[0]:
                STATE["slow_after"] = None
                await asyncio.sleep(slow[1])
                STATE["slow_answered"] = True
            return response
        return await call_next(request)

    app.include_router(brain_routes.router)

    # Settings' other tabs read routes this gate does not mount; they answer as
    # a route that does not exist would, which each of those tabs handles.
    @app.api_route("/api/{rest:path}", methods=["GET", "PUT", "POST", "DELETE"])
    def elsewhere(rest: str):
        return JSONResponse({"detail": "Not Found"}, status_code=404)

    @app.get("/css/{name}")
    def css(name: str):
        target = (CSS_DIR / name).resolve()
        if target.parent != CSS_DIR.resolve() or not target.is_file():
            return Response(status_code=404)
        return FileResponse(str(target), media_type="text/css")

    @app.get("/system-apps/settings/{rest:path}")
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


# ── the row, written and read straight in the schema ─────────────────────────
_DB_LOOP = asyncio.new_event_loop()
threading.Thread(target=_DB_LOOP.run_forever, daemon=True).start()


def db(stmt: str, *args):
    async def _go():
        if STATE.get("conn") is None:
            STATE["conn"] = await asyncpg.connect(STATE["dsn"])
        return await STATE["conn"].fetch(stmt, *args)
    return asyncio.run_coroutine_threadsafe(_go(), _DB_LOOP).result(timeout=60)


def db_close():
    async def _go():
        if STATE.get("conn") is not None:
            await STATE["conn"].close()
            STATE["conn"] = None
    asyncio.run_coroutine_threadsafe(_go(), _DB_LOOP).result(timeout=30)


WEIGHTS = {"warmth": 1.0, "directness": 1.25, "humor": 0.5, "formality": 1.0, "curiosity": 1.5,
           "assertiveness": 1.0, "patience": 1.75, "vulnerability": 0.75, "energy": 1.0, "depth": 1.1}


def reset(weights=None, level="terms"):
    w = weights or WEIGHTS
    bp = {"timezone": "America/New_York", "response_style": "conversational"}
    if level:
        bp["explanation_level"] = level
    db("UPDATE entity_registry SET current_weights = $2::jsonb, seed_weights = $2::jsonb, "
       "behavioral_params = $3::jsonb WHERE id = $1",
       E, json.dumps(w), json.dumps(bp))
    STATE["calls"].clear()
    STATE["slow_after"] = None
    STATE["hold_put"] = None
    STATE["pool_off"] = False
    STATE["perms"] = None


def stored():
    r = db("SELECT current_weights, seed_weights, behavioral_params "
           "FROM entity_registry WHERE id = $1", E)[0]
    return {k: (json.loads(v) if isinstance(v, str) else v) for k, v in dict(r).items()}


def calls(path_part: str, method: str):
    return [c for c in STATE["calls"] if c["path"].endswith(path_part) and c["method"] == method]


# ── the page ─────────────────────────────────────────────────────────────────
_OPENS = iter(range(1, 1_000_000))


def open_app(page, base, view: str = "shaping"):
    """A window as the shell opens one: a fresh load, the view in the hash."""
    page.goto(f"{base}/system-apps/settings/index.html?w={next(_OPENS)}" + (f"#view={view}" if view else ""),
              wait_until="domcontentloaded", timeout=15_000)
    if view == "shaping":
        expect(page.locator("#rs-weights .rs-dim")).to_have_count(10, timeout=10_000)


def current_level(page):
    return page.locator('#rs-explain [role="radio"][aria-checked="true"]').get_attribute("data-level")


def slider(page, box, dim):
    return page.locator(f'#{box} .rs-dim[data-dim="{dim}"] input[type="range"]')


def shown(page, box, dim):
    return page.locator(f'#{box} .rs-dim[data-dim="{dim}"] .rs-dim-value').text_content()


def nudge(page, box, dim, steps):
    """Move a slider the way a person does with the keyboard: one step each press."""
    s = slider(page, box, dim)
    s.focus()
    for _ in range(abs(steps)):
        page.keyboard.press("ArrowRight" if steps > 0 else "ArrowLeft")


def notice(page):
    loc = page.locator("#rs-notice")
    expect(loc).to_be_visible(timeout=10_000)
    return loc.text_content()


def scenario_open(page, base):
    open_app(page, base)
    tab = page.locator('.cseries-nav-btn[data-view="shaping"]')
    check("O the tab is its own button, selected", tab.get_attribute("aria-selected") == "true"
          and tab.text_content().strip() == "Response Shaping")
    order = page.locator(".cseries-nav-btn").evaluate_all("bs => bs.map(b => b.dataset.view)")
    check("O ...beside Connected agents", order.index("shaping") == order.index("agents") + 1, order)
    check("O ...and its section is not inside Connected agents'",
          page.locator("#view-agents #view-shaping").count() == 0
          and page.locator("#view-shaping").is_visible() and not page.locator("#view-agents").is_visible())
    check("O the three positions, the stored one current",
          page.locator('#rs-explain [role="radio"]').evaluate_all("bs => bs.map(b => b.dataset.level)")
          == ["full", "new", "terms"] and current_level(page) == "terms")
    names = page.locator("#rs-weights .rs-dim").evaluate_all("rs => rs.map(r => r.dataset.dim)")
    check("O ten sliders in Invariant 9's order", names == DIMS, names)
    vals = [float(slider(page, "rs-weights", d).input_value()) for d in DIMS]
    check("O each slider at the stored weight", vals == [WEIGHTS[d] for d in DIMS], vals)
    check("O a slider's line is the tendency for its position",
          page.locator('#rs-weights .rs-dim[data-dim="patience"] .rs-tendency').text_content()
          == "“You are endlessly patient — nothing is too slow to do right”")
    check("O nothing to save yet", page.locator("#rs-weights-save").is_disabled())
    brain = [c for c in STATE["calls"] if c["path"].startswith("/api/brain/")]
    # The window reads it when the tab opens and again when its first load of
    # the whole app finishes (loadAll re-shows the open tab, as for every tab).
    check("O the tab only read the row, and wrote nothing",
          brain and all((c["method"], c["path"]) == ("GET", "/api/brain/personality") for c in brain), brain)


def scenario_explain(page, base):
    open_app(page, base)
    page.locator('#rs-explain [data-level="full"]').click()
    expect(page.locator('#rs-explain [data-level="full"]')).to_have_attribute("aria-checked", "true",
                                                                            timeout=10_000)
    puts = calls("/api/brain/personality/explanation", "PUT")
    check("X one click sends one request naming the level", len(puts) == 1 and puts[0]["body"] == {"level": "full"},
          puts)
    check("X the page says what it set", "Explain everything" in notice(page))
    bp = stored()["behavioral_params"]
    check("X the row holds it, and its other settings are kept",
          bp.get("explanation_level") == "full" and bp.get("timezone") == "America/New_York"
          and bp.get("response_style") == "conversational", bp)
    open_app(page, base)
    check("X a new window opens on it", current_level(page) == "full")
    page.locator('#rs-explain [data-level="full"]').click()
    page.wait_for_timeout(300)
    check("X choosing the current one again sends nothing",
          len(calls("/api/brain/personality/explanation", "PUT")) == 1)


def scenario_weights(page, base):
    open_app(page, base)
    nudge(page, "rs-weights", "warmth", 12)              # 1.00 → 1.60
    check("W the slider shows its value as it moves", shown(page, "rs-weights", "warmth") == "1.60")
    check("W ...and the tendency line for that position",
          page.locator('#rs-weights .rs-dim[data-dim="warmth"] .rs-tendency').text_content()
          == "“You radiate warmth — connection comes first, always”")
    check("W Save and Discard are offered", page.locator("#rs-weights-save").is_enabled()
          and page.locator("#rs-weights-discard").is_visible())
    page.locator("#rs-weights-save").click()
    modal = page.locator("#rs-modal")
    expect(modal).to_be_visible(timeout=5_000)
    items = page.locator("#rs-modal-changes li").all_text_contents()
    check("W the confirm lists the one change, from and to",
          [" ".join(t.split()) for t in items] == ["warmth: 1.00 → 1.60"], items)
    page.locator("#rs-modal-cancel").click()
    expect(modal).to_be_hidden(timeout=5_000)
    check("W Cancel sends nothing and keeps the slider where it was moved",
          not calls("/api/brain/personality/weights", "PUT") and shown(page, "rs-weights", "warmth") == "1.60")
    page.locator("#rs-weights-save").click()
    page.locator("#rs-modal-save").click()
    expect(modal).to_be_hidden(timeout=10_000)
    check("W the page says it saved", "Personality updated" in notice(page))
    puts = calls("/api/brain/personality/weights", "PUT")
    sent = puts[0]["body"]["weights"] if puts else {}
    check("W one request carries all ten, warmth at 1.6",
          len(puts) == 1 and sorted(sent) == sorted(DIMS) and abs(sent["warmth"] - 1.6) < 1e-9, puts)
    row = stored()
    check("W the row's seed and current both hold it",
          abs(row["current_weights"]["warmth"] - 1.6) < 1e-9 and row["seed_weights"] == row["current_weights"],
          row)
    check("W after the save there is nothing left to save", page.locator("#rs-weights-save").is_disabled())
    open_app(page, base)
    check("W a new window opens on the saved value", slider(page, "rs-weights", "warmth").input_value() == "1.6"
          and shown(page, "rs-weights", "warmth") == "1.60")


def scenario_discard(page, base):
    open_app(page, base)
    nudge(page, "rs-weights", "depth", -4)
    page.locator("#rs-weights-discard").click()
    check("D Discard puts the slider back", slider(page, "rs-weights", "depth").input_value() == "1.1")
    check("D ...with nothing to save and nothing sent",
          page.locator("#rs-weights-save").is_disabled()
          and not [c for c in STATE["calls"] if c["method"] != "GET"])


def scenario_fresh_read(page, base):
    open_app(page, base)
    nudge(page, "rs-weights", "humor", 6)                # 0.50 → 0.80, not saved
    # Meanwhile the Host's explanation_level tool sets the level the user asked for.
    db("UPDATE entity_registry SET behavioral_params = behavioral_params || '{\"explanation_level\": \"new\"}'::jsonb "
       "WHERE id = $1", E)
    reads = len(calls("/api/brain/personality", "GET"))
    page.locator('.cseries-nav-btn[data-view="shaping"]').click()
    expect(page.locator('#rs-explain [data-level="new"]')).to_have_attribute("aria-checked", "true", timeout=10_000)
    check("K the tab read the row again", len(calls("/api/brain/personality", "GET")) == reads + 1)
    check("K ...took the level the Host set", current_level(page) == "new")
    check("K ...and kept the slider the person moved", shown(page, "rs-weights", "humor") == "0.80"
          and page.locator("#rs-weights-save").is_enabled())


def scenario_stale(page, base):
    open_app(page, base)
    STATE["slow_after"] = ("/api/brain/personality", 1.5)
    STATE["slow_answered"] = False
    page.locator('.cseries-nav-btn[data-view="shaping"]').click()   # a read, answered late, of "terms"
    page.wait_for_timeout(200)
    page.locator('#rs-explain [data-level="new"]').click()
    expect(page.locator('#rs-explain [data-level="new"]')).to_have_attribute("aria-checked", "true", timeout=10_000)
    deadline = time.time() + 15
    while not STATE.get("slow_answered") and time.time() < deadline:
        page.wait_for_timeout(50)
    page.wait_for_timeout(300)
    check("S the older read did answer", STATE.get("slow_answered"))
    check("S ...and never drew over the newer choice", current_level(page) == "new"
          and stored()["behavioral_params"].get("explanation_level") == "new")


def scenario_read_during_put(page, base):
    open_app(page, base)
    STATE["hold_put"] = ("/api/brain/personality/explanation", 1.0)
    STATE["slow_after"] = ("/api/brain/personality", 2.0)
    STATE["slow_answered"] = False
    page.locator('#rs-explain [data-level="new"]').click()        # the PUT, held 1 s
    page.wait_for_timeout(200)
    page.locator('.cseries-nav-btn[data-view="shaping"]').click()   # a read of "terms", answered after the PUT
    page.wait_for_timeout(3000)
    check("Q a read made while a level is sent never draws the old level over it",
          current_level(page) == "new" and stored()["behavioral_params"].get("explanation_level") == "new",
          current_level(page))


def scenario_fresh_others(page, base):
    open_app(page, base)
    nudge(page, "rs-weights", "humor", 6)                # 0.50 → 0.80, not saved
    # Meanwhile another window (or Circles' pane) saves warmth.
    w = dict(WEIGHTS, warmth=1.4)
    db("UPDATE entity_registry SET current_weights = $2::jsonb, seed_weights = $2::jsonb WHERE id = $1",
       E, json.dumps(w))
    page.locator('.cseries-nav-btn[data-view="shaping"]').click()
    expect(page.locator('#rs-weights .rs-dim[data-dim="warmth"] .rs-dim-value')).to_have_text("1.40", timeout=10_000)
    check("F a fresh read moves the sliders the person did not touch",
          shown(page, "rs-weights", "warmth") == "1.40")
    check("F ...and keeps the ones they moved", shown(page, "rs-weights", "humor") == "0.80")
    page.locator("#rs-weights-save").click()
    items = [" ".join(t.split()) for t in page.locator("#rs-modal-changes li").all_text_contents()]
    check("F the confirm lists only the person's change", items == ["humor: 0.50 → 0.80"], items)
    page.locator("#rs-modal-save").click()
    expect(page.locator("#rs-modal")).to_be_hidden(timeout=10_000)
    row = stored()
    check("F the save keeps the other window's values",
          abs(row["current_weights"]["warmth"] - 1.4) < 1e-9 and abs(row["current_weights"]["humor"] - 0.8) < 1e-9,
          row)


def scenario_phone_tabs(page, base):
    open_app(page, base, view="")
    views = page.locator(".cseries-nav-btn").evaluate_all("bs => bs.map(b => b.dataset.view)")
    nav = page.locator(".cseries-nav")
    check("PT the tab bar is wider than a phone", nav.evaluate("n => n.scrollWidth > n.clientWidth"))
    seen = []
    for v in list(reversed(views)) + views:
        page.evaluate(f"window.postMessage({{type: 'cseries:app-view', view: 'view={v}'}}, window.location.origin)")
        page.wait_for_timeout(150)
        inside = page.evaluate(
            "v => { const n = document.querySelector('.cseries-nav').getBoundingClientRect();"
            " const b = document.querySelector('.cseries-nav-btn[data-view=\"' + v + '\"]').getBoundingClientRect();"
            " return b.left >= n.left - 0.5 && b.right <= n.right + 0.5; }", v)
        if not inside:
            seen.append(v)
    check("PT every selected tab is scrolled into the tab bar", not seen, seen)


def scenario_refusal(page, base):
    open_app(page, base)
    nudge(page, "rs-weights", "energy", 2)
    STATE["pool_off"] = True
    page.locator("#rs-weights-save").click()
    page.locator("#rs-modal-save").click()
    expect(page.locator("#rs-modal")).to_be_hidden(timeout=10_000)
    said = notice(page)
    check("R a refusal is said in words", said == "Couldn't update personality: Brain not initialized", said)
    STATE["pool_off"] = False
    check("R ...the slider keeps its place, Save is still offered, the row is unchanged",
          shown(page, "rs-weights", "energy") == "1.10" and page.locator("#rs-weights-save").is_enabled()
          and stored()["current_weights"] == WEIGHTS)


def scenario_scopes(page, base):
    check("M the manifest declares the two scopes the routes take",
          {"read:brain_personality", "write:brain_personality"} <= set(MANIFEST_PERMS),
          MANIFEST_PERMS)
    STATE["perms"] = [p for p in MANIFEST_PERMS if p != "write:brain_personality"]
    open_app(page, base)
    nudge(page, "rs-weights", "warmth", 2)
    page.locator("#rs-weights-save").click()
    page.locator("#rs-modal-save").click()
    expect(page.locator("#rs-modal")).to_be_hidden(timeout=10_000)
    said = notice(page)
    check("M without write:brain_personality the slider save is refused, in words",
          said.startswith("Couldn't update personality: the Host answered 403")
          and "write:brain_personality" in said, said)
    check("M ...and nothing is written", stored()["current_weights"] == WEIGHTS)


def scenario_text(page, base):
    odd = dict(WEIGHTS)
    odd['<img src=x onerror="window.__owned=1">'] = 1.0
    reset(weights=odd)
    page.goto(f"{base}/system-apps/settings/index.html?w={next(_OPENS)}#view=shaping",
              wait_until="domcontentloaded", timeout=15_000)
    expect(page.locator("#rs-weights .rs-dim")).to_have_count(11, timeout=10_000)
    check("T a dimension name from the row is text, never markup",
          page.locator("#view-shaping img").count() == 0
          and page.evaluate("window.__owned === undefined")
          and page.locator("#rs-weights .rs-dim").nth(10).locator(".rs-dim-name").text_content()
          == '<img src=x onerror="window.__owned=1">')


def scenario_links(page, base):
    open_app(page, base, view="")
    check("L the window opens on About", page.locator("#view-overview").is_visible())
    page.evaluate("window.postMessage({type: 'cseries:app-view', view: 'view=shaping'}, window.location.origin)")
    expect(page.locator("#rs-weights .rs-dim")).to_have_count(10, timeout=10_000)
    check("L a cseries:app-view message moves it to Response Shaping",
          page.locator("#view-shaping").is_visible()
          and page.locator('.cseries-nav-btn[data-view="shaping"]').get_attribute("aria-selected") == "true")


def scenario_phone(page, base):
    open_app(page, base, view="")
    tab = page.locator('.cseries-nav-btn[data-view="shaping"]')
    # On a phone the tab bar scrolls sideways (cseries-components.css); the tab
    # is reached that way and opened with a tap.
    tab.scroll_into_view_if_needed()
    tab.tap()
    expect(page.locator("#rs-weights .rs-dim")).to_have_count(10, timeout=10_000)
    check("P the tab is reached in the tab bar and opened with a tap", page.locator("#view-shaping").is_visible())
    width = page.evaluate("document.documentElement.scrollWidth")
    check("P nothing wider than the screen", width <= 390, width)
    s = slider(page, "rs-weights", "warmth")
    s.scroll_into_view_if_needed()
    b = s.bounding_box()
    check("P a slider is wide and tall enough for a finger", b and b["width"] >= 250 and b["height"] >= 24, b)
    # A tap at three quarters of the track: 0..2, so about 1.5.
    page.touchscreen.tap(b["x"] + b["width"] * 0.75, b["y"] + b["height"] / 2)
    v = float(s.input_value())
    check("P a tap moves the slider", 1.3 <= v <= 1.7, v)
    page.locator("#rs-weights-save").click()
    card = page.locator(".rs-modal-card").bounding_box()
    check("P the confirm sits inside the screen", card and card["x"] >= 0 and card["x"] + card["width"] <= 390,
          card)
    page.locator("#rs-modal-save").click()
    expect(page.locator("#rs-modal")).to_be_hidden(timeout=10_000)
    check("P ...and saves", abs(stored()["current_weights"]["warmth"] - v) < 1e-9)
    page.screenshot(path=str(SHOTS / "phone.png"), full_page=True)


PHONE = {"viewport": {"width": 390, "height": 844}, "has_touch": True, "is_mobile": True, "device_scale_factor": 2}
SCENARIOS = (
    (scenario_open, {}), (scenario_explain, {}), (scenario_weights, {}),
    (scenario_discard, {}), (scenario_fresh_read, {}), (scenario_stale, {}), (scenario_refusal, {}),
    (scenario_scopes, {}), (scenario_text, {}), (scenario_links, {}), (scenario_phone, PHONE),
    (scenario_read_during_put, {}), (scenario_fresh_others, {}), (scenario_phone_tabs, PHONE),
)


def main() -> int:
    if not (APP_DIR / "index.html").is_file():
        print(f"FAIL  no Settings app at {APP_DIR}")
        return 1
    with (contextlib.nullcontext(SERVER) if SERVER else throwaway_server()) as srv, \
            kernel_schema_db(srv, entity_id=E) as dsn:
        STATE["dsn"] = dsn
        port = serve(build_app(dsn))
        base = f"http://127.0.0.1:{port}"
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            for fn, opts in SCENARIOS:
                if ONLY and fn.__name__ not in ONLY:
                    continue
                reset()
                ctx = browser.new_context(**{"viewport": {"width": 1440, "height": 900}, "locale": "en-US", **opts})
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
        # Closed before the database is dropped, which waits on no session.
        db_close()
    print("\nPASS settings_response_shaping_browser" if not fails
          else f"\nFAIL settings_response_shaping_browser: {fails} check(s)")
    if _OWN_SHOTS and not fails:
        shutil.rmtree(SHOTS, ignore_errors=True)
    else:
        print(f"screenshots: {SHOTS}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
