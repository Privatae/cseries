#!/usr/bin/env python3
"""Open every installed app on the VM's Host in headless Chromium and record every
401 "application token required" (and every other /api/ 401/403), per app, URL,
the frame that made it, the resource type, and whether it left before that
frame's load event.

usage: token_sweep.py <jar.json> <outdir> <mode>
  mode normal       open each app as the desktop does
  mode hold-token   the isolated app's frame-token answer held 8 s (app-frame-init.js)
  mode hold-launch  POST /api/applications/<id>/launch held 8 s (the shell's own token
                    for a same-origin app comes from it; the frame then loads at once)
  mode hold-init    the shell's cseries:app-init message to an SDK-managed frame held 8 s
                    (window.postMessage from the desktop delayed, desktop side)
Writes <outdir>/<mode>.json and <mode>.events.jsonl."""
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from ct_lib import test_host_or_exit  # noqa: E402
B = test_host_or_exit()   # before playwright: a refused run touches nothing
from playwright.sync_api import sync_playwright  # noqa: E402

# The Host as the browser reaches it (run.py sets it; the default is a local install).
jar, out, mode = sys.argv[1], sys.argv[2], sys.argv[3]
HOLD = 8.0
ev = open(f"{out}/{mode}.events.jsonl", "w")

# Every frame records when its document loaded, on its own clock (epoch ms).
INIT = """(() => { try {
  window.__sweepLoadedAt = null;
  window.addEventListener('load', () => { window.__sweepLoadedAt = Date.now(); });
  window.__sweepHref = String(location.href);
} catch (e) {} })();"""

# Inside each app frame: the desktop's cseries:app-init is held 8 s, then
# delivered again with the same data, origin and source.
HOLD_INIT = """(() => { try {
  if (window === window.top) return;
  window.addEventListener('message', (e) => {
    if (e.__sweepRedelivered) return;
    const d = e.data;
    if (!d || d.type !== 'cseries:app-init') return;
    e.stopImmediatePropagation();
    setTimeout(() => {
      const again = new MessageEvent('message', {data: d, origin: e.origin, source: e.source});
      again.__sweepRedelivered = true;
      window.dispatchEvent(again);
    }, %d);
  }, true);
} catch (e) {} })();""" % int(HOLD * 1000)

results = []
with sync_playwright() as p:
    b = p.chromium.launch()
    ctx = b.new_context(storage_state=jar, viewport={"width": 1440, "height": 1000})
    ctx.add_init_script(INIT)
    if mode == "hold-init":
        ctx.add_init_script(HOLD_INIT)
    page = ctx.new_page()

    def hold(route):
        time.sleep(HOLD)
        route.continue_()
    if mode == "hold-token":
        ctx.route("**/api/applications/*/frame-token", hold)
    if mode == "hold-launch":
        ctx.route("**/api/applications/*/launch", hold)

    cur = {"app": None, "hits": [], "reqs": {}}

    def on_request(r):
        if "/api/" in r.url:
            cur["reqs"][id(r)] = time.time() * 1000
            try:
                furl = r.frame.url.split("?")[0]
            except Exception:  # noqa: BLE001
                furl = ""
            cur.setdefault("all", []).append({
                "url": r.url.split("?")[0], "frame": furl, "type": r.resource_type,
                "t_req": time.time() * 1000,
                "has_token_header": "x-application-token" in {k.lower() for k in r.headers}})

    def on_response(r):
        if "/api/" not in r.url or r.status not in (401, 403):
            return
        try:
            body = r.text()[:200]
        except Exception as e:  # noqa: BLE001
            body = f"<{type(e).__name__}>"
        f = r.request.frame
        furl = ""
        try:
            furl = f.url.split("?")[0]
        except Exception:  # noqa: BLE001
            pass
        t_req = cur["reqs"].get(id(r.request))
        hit = {"app": cur["app"], "status": r.status, "method": r.request.method,
               "url": r.url.split("?")[0], "frame": furl, "type": r.request.resource_type,
               "body": body, "t_req": t_req, "has_token_header": "x-application-token" in
               {k.lower() for k in r.request.headers}}
        cur["hits"].append(hit)
        ev.write(json.dumps(hit) + "\n")
        ev.flush()
    page.on("request", on_request)
    page.on("response", on_response)
    page.on("pageerror", lambda e: ev.write(json.dumps({"app": cur["app"], "pageerror": str(e)[:300]}) + "\n"))
    page.goto(B + "/")
    page.wait_for_timeout(5000)
    apps = page.evaluate("async () => (await (await fetch('/api/applications')).json()).applications.map(a => ({id: a.id, provenance: a.provenance, isolated: !!a.isolated}))")
    for a in apps:
        aid = a["id"]
        page.evaluate("() => WindowManager.getAll().forEach(w => WindowManager.close(w.id))")
        page.wait_for_timeout(800)
        cur.update(app=aid, hits=[], reqs={}, all=[])
        t0 = time.time() * 1000
        try:
            page.evaluate("(id) => AppRunner.launch(id)", aid)
        except Exception as e:  # noqa: BLE001
            results.append({"app": aid, "error": f"launch: {e}"[:200]})
            continue
        page.wait_for_timeout(int((HOLD + 9) * 1000) if mode != "normal" else 9000)
        frames = [f for f in page.frames if f"/system-apps/{aid}/" in f.url and f != page.main_frame]
        loaded = None
        origin = None
        if frames:
            try:
                loaded = frames[0].evaluate("() => window.__sweepLoadedAt")
                origin = frames[0].evaluate("() => location.origin")
            except Exception:  # noqa: BLE001
                pass
        for h in cur["hits"]:
            h["before_load"] = (None if (loaded is None or h["t_req"] is None)
                                else h["t_req"] < loaded)
        tok = [h for h in cur["hits"] if "application token required" in h["body"]]
        mine = [q for q in cur["all"] if f"/system-apps/{aid}/" in q["frame"] or
                (origin and q["frame"].startswith(origin))]
        for q in mine:
            q["before_load"] = None if loaded is None else q["t_req"] < loaded
        no_tok = [q for q in mine if not q["has_token_header"]]
        results.append({"app": aid, "provenance": a["provenance"],
                        "frame_origin": origin, "frame_found": bool(frames),
                        "loaded_after_ms": (loaded - t0) if loaded else None,
                        "token_required_401": tok,
                        "api_requests": len(mine),
                        "api_before_load": sum(1 for q in mine if q["before_load"]),
                        "api_before_load_with_token": sum(1 for q in mine if q["before_load"] and q["has_token_header"]),
                        "api_without_token": [(q["url"], q["type"], q["before_load"]) for q in no_tok],
                        "other_401_403": [h for h in cur["hits"] if h not in tok]})
        print(f"{mode} {aid:22s} origin={'app' if origin and 'apps-' in origin else 'desktop' if origin else '-'} "
              f"401-token={len(tok)} other={len(cur['hits']) - len(tok)} api={len(mine)} "
              f"before-load={sum(1 for q in mine if q['before_load'])} "
              f"no-token={len(no_tok)} {sorted(set(u for u, _, _ in [(q['url'].split('8800')[-1], 0, 0) for q in no_tok]))[:4]}", flush=True)
    json.dump(results, open(f"{out}/{mode}.json", "w"), indent=1)
    ctx.storage_state(path=jar)
    b.close()
