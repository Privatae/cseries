#!/usr/bin/env python3
"""C-Series 0.1.17 person-level proof on the Ubuntu test VM's real Host, in
headless Chromium with console / page errors / failed requests / HTTP >= 400
captured per step (events.jsonl, steps.log, screenshots).

usage: drive.py <jar.json> <outdir> <step...>
steps: state clear-chat race local simple connect-kimi claude-signin connect-claude chat
"""
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from ct_lib import Rec, frame, test_host_or_exit  # noqa: E402
B = test_host_or_exit()   # before playwright: a refused run touches nothing
from playwright.sync_api import sync_playwright  # noqa: E402

# The Host as the browser reaches it (run.py sets it; the default is a local install).
jar, out, steps = sys.argv[1], sys.argv[2], sys.argv[3:]
R = Rec(out)
n401: list = []


def jfetch(f, path, method="GET", body=None):
    return f.evaluate("""async ([p, m, b]) => { const r = await fetch(p, {method: m, cache: 'no-store',
        headers: b ? {'Content-Type': 'application/json'} : {}, body: b ? JSON.stringify(b) : undefined});
        let j = null; try { j = await r.json(); } catch (e) {} return {status: r.status, body: j}; }""",
                      [path, method, body])


def open_orch(page, hold=0.0):
    page.evaluate("() => WindowManager.getAll().forEach(w => WindowManager.close(w.id))")
    for _ in range(50):   # the closed window's frame is gone before a new one is looked for
        if not any("/system-apps/orchestrator/" in f.url for f in page.frames if f != page.main_frame):
            break
        page.wait_for_timeout(200)
    page.evaluate("() => AppRunner.launch('orchestrator')")
    o = frame(page, "orchestrator")
    page.wait_for_timeout(int(hold * 1000) + 8000)
    return o


def cards(o):
    return o.evaluate("() => Array.from(document.querySelectorAll('.mp-prov-card')).map(c => c.innerText.replace(/\\s+/g, ' ')).join(' || ')")


def chat_now(o):
    return o.evaluate("""() => { const e = document.querySelector('[data-mp-chat-now], [data-mp-chat-local]');
        return e ? e.innerText.replace(/\\s+/g, ' ') : null; }""")


with sync_playwright() as p:
    b = p.chromium.launch()
    ctx = b.new_context(storage_state=jar, viewport={"width": 1440, "height": 1000})
    page = ctx.new_page()
    R.attach(ctx, page)
    hold_s = {"v": 0.0}

    def hold(route):
        if hold_s["v"]:
            time.sleep(hold_s["v"])
        route.continue_()
    ctx.route("**/api/applications/orchestrator/frame-token", hold)
    page.on("response", lambda r: r.status == 401 and "apps-" in r.url and n401.append(r.url.split('?')[0]))
    page.goto(B + "/")
    page.wait_for_timeout(4000)
    lt = page.get_by_text("Leave the tour", exact=True)
    if lt.count():
        lt.first.click()
        page.wait_for_timeout(800)
    o = None

    for st in steps:
        R.step = st
        if st == "state":
            o = o or open_orch(page)
            r = jfetch(o, "/api/inference/routing")["body"] or {}
            R.say("stored chat route: " + json.dumps((r.get("routing") or {}).get("chat")))
            R.say("effective chat: " + json.dumps({k: v for k, v in ((r.get("effective") or {}).get("chat") or {}).items()
                                                  if k in ("provider", "model", "source", "default_why", "unconfigured", "local_default")}))
            R.say("providers with a key: " + json.dumps([x["id"] for x in r.get("providers") or [] if x.get("has_key")]))
            R.say("engine_keys: " + json.dumps(r.get("engine_keys")))
        elif st == "clear-chat":
            o = o or open_orch(page)
            R.say("DELETE /api/inference/routing/chat from the Orchestrator (setup: no chat route chosen): "
                  + json.dumps(jfetch(o, "/api/inference/routing/chat", "DELETE")))
        elif st == "race":
            hold_s["v"] = 8.0
            n401.clear()
            o = open_orch(page, hold=8.0)
            hold_s["v"] = 0.0
            R.say(f"401s on the Orchestrator's origin while opening with the frame-token held 8 s: {len(n401)} {sorted(set(n401))}")
            R.say("window token held by the SDK: " + str(o.evaluate("() => window.CSeriesApp ? !!window.CSeriesApp.token : null")))
            R.say("provider cards drawn: " + (cards(o) or "NONE"))
            page.screenshot(path=f"{out}/01-orchestrator-token-held-8s.png")
        elif st == "local":
            o = o or open_orch(page)
            lm = jfetch(o, "/api/inference/local-models")["body"] or {}
            R.say("GET /api/inference/local-models: " + json.dumps(
                [(m["id"], m.get("bastion"), "embedding" if m.get("embedding") else "chat") for m in lm.get("local_models") or []]))
            o.get_by_text("Advanced", exact=True).first.click()
            page.wait_for_timeout(3000)
            for tab in ("Routing",):
                t = o.get_by_text(tab, exact=True)
                if t.count():
                    t.first.click()
                    page.wait_for_timeout(1500)
            sel = "select.mp-role-provider[data-role=chat]"
            o.select_option(sel, "local")
            page.wait_for_timeout(2500)
            opts = o.evaluate("() => Array.from(document.querySelector('select.mp-role-model[data-role=chat]').options).map(x => x.value).filter(Boolean)")
            R.say("chat role, provider local, model options offered: " + json.dumps(opts))
            imgs = o.evaluate("""() => { const s = document.querySelector('select.mp-role-provider[data-role=image_gen]');
                return s ? 'image_gen row present' : 'no image_gen row'; }""")
            R.say(imgs)
            R.say("chat row says: " + o.evaluate("""() => { const r = document.querySelector('.mp-role[data-role=chat]');
                return r ? r.innerText.replace(/\\s+/g, ' ').slice(0, 600) : null; }"""))
            page.screenshot(path=f"{out}/02-orchestrator-local-models.png")
            o = open_orch(page)   # reopen, leaving the unsaved browse pick behind
            page.wait_for_timeout(2000)
        elif st == "simple":
            o = o or open_orch(page)
            s = o.get_by_text("Simple", exact=True)
            if s.count():
                s.first.click()
                page.wait_for_timeout(2000)
            R.say("Thinking step says: " + str(chat_now(o)))
            R.say("provider cards: " + (cards(o) or "NONE"))
            page.screenshot(path=f"{out}/03-orchestrator-simple.png")
        elif st.startswith("revoke-"):
            hid = st.split("-", 1)[1]
            page.evaluate("() => WindowManager.getAll().forEach(w => WindowManager.close(w.id))")
            page.evaluate("() => AppRunner.launch('keys')")
            k = frame(page, "keys")
            page.wait_for_timeout(4000)
            k.click("[data-section=vault]")
            page.wait_for_timeout(1200)
            k.click(f"[data-act=revoke][data-name=harness-{hid}]")
            page.wait_for_timeout(1500)
            btns = [x for x in k.locator("button").all() if x.is_visible() and (x.inner_text() or '').strip().lower() in ("revoke", "revoke it", "yes, revoke", "confirm")]
            R.say(f"revoke harness-{hid}: confirm buttons {[x.inner_text() for x in btns]}")
            if btns:
                btns[-1].click()
                page.wait_for_timeout(4000)
            R.say("vault after: " + k.evaluate("() => document.body.innerText").replace("\n", " | ")[:500])
            o = None
        elif st.startswith("connect-") and st != "connect-claude":
            hid = st.split("-", 1)[1]
            o = o or open_orch(page)
            o.get_by_text("Advanced", exact=True).first.click()
            page.wait_for_timeout(2500)
            for tab in ("Coding Engines", "Coding engines", "Coding"):
                t = o.get_by_text(tab, exact=True)
                if t.count():
                    t.first.click()
                    page.wait_for_timeout(2500)
                    break
            btn = o.locator(f"button.mp-mx-action[data-harness={hid}]")
            for _ in range(40):
                if btn.count() and btn.first.is_visible():
                    break
                page.wait_for_timeout(500)
            R.say(f"{hid} row before: " + o.evaluate("""(h) => { const b = document.querySelector('button.mp-mx-action[data-harness=' + h + ']');
                let r = b; for (let i = 0; i < 4 && r && r.parentElement; i++) r = r.parentElement;
                return r ? r.innerText.replace(/\\s+/g, ' ').slice(0, 500) : 'no action button'; }""", hid))
            if btn.count():
                R.say("clicking: " + btn.first.inner_text())
                btn.first.scroll_into_view_if_needed()
                btn.first.click()
            k = frame(page, "keys", 20000)
            R.say(f"Keys window opened by the Orchestrator's button: {k is not None}")
            page.wait_for_timeout(4000)
            page.screenshot(path=f"{out}/04-keys-opened-from-orchestrator.png")
            if k:
                vis = k.evaluate("""() => Array.from(document.querySelectorAll('button, input, textarea, select')).filter(e => e.offsetParent !== null)
                    .map(e => [e.tagName, e.type || '', (e.innerText || e.value || e.placeholder || '').trim().replace(/\\s+/g, ' ').slice(0, 60),
                               Object.keys(e.dataset).map(x => x + '=' + e.dataset[x]).join(',').slice(0, 80)])""")
                R.say("Keys controls: " + json.dumps(vis[-14:]))
                spec = "stand-in-" + hid + "-key-not-a-real-one"
                R.say("Keys opened on: " + str(k.evaluate("() => { const a = document.querySelector('[data-section].active, [data-section][aria-selected=true]'); return a ? a.innerText.replace(/\\s+/g, ' ') : null; }")))
                pre = k.evaluate("() => { const n = document.getElementById('add-name'); return n && n.offsetParent !== null ? n.value : null; }")
                R.say(f"Add a key form open on arrival, name filled in: {pre!r}")
                if pre != "harness-" + hid:
                    # As a person had to before: Vault, Add a key, the engine's name.
                    k.click("[data-section=vault]")
                    page.wait_for_timeout(1200)
                    k.click("[data-act=add-custom]")
                    page.wait_for_timeout(1200)
                name_in = [x for x in k.locator("input").all() if x.is_visible()
                           and (x.get_attribute("placeholder") or "").startswith("e.g.")]
                if name_in and (name_in[0].input_value() or "") != "harness-" + hid:
                    name_in[0].fill("harness-" + hid)
                val = [x for x in k.locator("textarea, input[type=password]").all() if x.is_visible()]
                R.say(f"value fields visible: {len(val)}; name field: {name_in[0].input_value() if name_in else None}")
                if val:
                    val[-1].fill(spec)
                    btns = [x for x in k.locator("button").all() if x.is_visible() and (x.inner_text() or '').strip().lower()
                            in ("save", "add", "store", "seal", "save key", "add key", "seal it", "connect")]
                    R.say(f"save buttons: {[x.inner_text() for x in btns]}")
                    if btns:
                        btns[-1].click()
                        page.wait_for_timeout(4000)
                page.screenshot(path=f"{out}/05-keys-after-save.png")
            # Back to the Orchestrator, as a person would (its window comes to the front).
            page.evaluate("() => WindowManager.focus('app-orchestrator')")
            o.evaluate("() => window.dispatchEvent(new Event('focus'))")
            page.wait_for_timeout(12000)
            R.say("engine_keys now: " + json.dumps((jfetch(o, "/api/inference/routing")["body"] or {}).get("engine_keys")))
            R.say(f"{hid} row after (window not reopened): " + o.evaluate("""(h) => { const b = document.querySelector('[data-harness=' + h + ']');
                let r = b; for (let i = 0; i < 4 && r && r.parentElement; i++) r = r.parentElement;
                return r ? r.innerText.replace(/\\s+/g, ' ').slice(0, 700) : 'no row'; }""", hid))
            mx = jfetch(o, "/api/inference/coding-matrix")["body"] or {}
            row = next((x for x in (mx.get("rows") or mx.get("harnesses") or []) if hid in (x.get("harness"), x.get("id"), x.get("app_id"))), None)
            R.say(f"coding-matrix {hid} row: " + json.dumps({k2: row.get(k2) for k2 in ("state", "key_state", "provider", "models_error")} if row else None)
                  + " models: " + json.dumps([m.get("id") for m in (row or {}).get("models") or []][:10]))
            page.screenshot(path=f"{out}/06-orchestrator-{hid}-after-key.png")
        elif st == "kimi-models":
            o = o or open_orch(page)
            mx = jfetch(o, "/api/inference/coding-matrix")["body"] or {}
            rows = mx.get("rows") or mx.get("harnesses") or []
            R.say("coding-matrix row keys: " + json.dumps(sorted((rows[0] if rows else {}).keys())))
            row = next((x for x in rows if "kimi" in (x.get("harness"), x.get("id"), x.get("app_id"))), None)
            R.say("kimi row: " + json.dumps({k2: v for k2, v in (row or {}).items() if k2 not in ("models",)})[:900])
            R.say("kimi models: " + json.dumps([m.get("id") for m in (row or {}).get("models") or []][:12]))
        elif st == "claude-signin":
            o = o or open_orch(page)
            s = o.get_by_text("Simple", exact=True)
            if s.count():
                s.first.click()
                page.wait_for_timeout(1500)
            bt = []
            for _ in range(30):
                bt = [x for x in o.locator("[data-mp-claude-signin]").all() if x.is_visible()]
                if bt:
                    break
                page.wait_for_timeout(500)
            R.say(f"'Sign in with your Claude account' controls visible: {len(bt)}")
            if bt:
                bt[0].click()
                for _ in range(120):
                    if o.evaluate("() => !!document.querySelector('.mp-claude-scrim [data-mp-cs-link]') && !!document.querySelector('.mp-claude-scrim [data-mp-cs-link]').href"):
                        break
                    page.wait_for_timeout(500)
                link = o.evaluate("() => { const a = document.querySelector('.mp-claude-scrim [data-mp-cs-link]'); return a ? a.href : null; }")
                from urllib.parse import urlsplit
                u = urlsplit(link) if link else None
                R.say("pop-up open: " + str(o.evaluate("() => !!document.querySelector('.mp-claude-scrim')"))
                      + "; link: " + (f"{u.scheme}://{u.netloc}{u.path} (query not recorded)" if u else "NONE"))
                R.say("pop-up text: " + str(o.evaluate("() => { const s = document.querySelector('.mp-claude-scrim'); return s ? s.innerText.replace(/\\s+/g, ' ').slice(0, 400) : null; }")))
                page.screenshot(path=f"{out}/07-claude-signin-popup.png")
                o.evaluate("() => { const c = document.querySelector('.mp-claude-scrim [data-mp-cs-cancel], .mp-claude-scrim [data-mp-cs-close]'); if (c) c.click(); }")
                page.wait_for_timeout(1500)
                R.say("closed without signing in: " + str(not o.evaluate("() => !!document.querySelector('.mp-claude-scrim')")))
        elif st == "connect-claude":
            o = o or open_orch(page)
            o.get_by_text("Advanced", exact=True).first.click()
            page.wait_for_timeout(2500)
            for tab in ("Coding Engines", "Coding engines", "Coding"):
                t = o.get_by_text(tab, exact=True)
                if t.count():
                    t.first.click()
                    page.wait_for_timeout(2500)
                    break
            btn = o.locator("button.mp-mx-action[data-harness=claude-code]")
            for _ in range(40):
                if btn.count() and btn.first.is_visible():
                    break
                page.wait_for_timeout(500)
            R.say(f"Connect Claude Code button: {btn.count()} {btn.first.inner_text() if btn.count() else ''!r}")
            if btn.count():
                btn.first.scroll_into_view_if_needed()
                btn.first.click()
                for _ in range(120):
                    if o.evaluate("() => !!document.querySelector('.mp-claude-scrim [data-mp-cs-link]') && !!document.querySelector('.mp-claude-scrim [data-mp-cs-link]').href"):
                        break
                    page.wait_for_timeout(500)
                R.say("pop-up text: " + str(o.evaluate("() => { const s = document.querySelector('.mp-claude-scrim'); return s ? s.innerText.replace(/\\s+/g, ' ').slice(0, 400) : null; }")))
                link = o.evaluate("() => { const a = document.querySelector('.mp-claude-scrim [data-mp-cs-link]'); return a ? a.href : null; }")
                from urllib.parse import urlsplit
                u = urlsplit(link) if link else None
                R.say("after Connect Claude Code: sign-in pop-up open: " + str(o.evaluate("() => !!document.querySelector('.mp-claude-scrim')"))
                      + "; link: " + (f"{u.scheme}://{u.netloc}{u.path}" if u else "NONE"))
                page.screenshot(path=f"{out}/08-connect-claude-code.png")
                o.evaluate("() => { const c = document.querySelector('.mp-claude-scrim [data-mp-cs-cancel], .mp-claude-scrim [data-mp-cs-close]'); if (c) c.click(); }")
                page.wait_for_timeout(1000)
        elif st == "chat":
            o = None
            page.evaluate("() => WindowManager.getAll().forEach(w => WindowManager.close(w.id))")
            page.evaluate("() => AppRunner.launch('circles')")
            c = frame(page, "circles")
            page.wait_for_timeout(6000)
            ta = [x for x in c.locator("textarea, [contenteditable=true]").all() if x.is_visible()]
            R.say(f"composers: {len(ta)}")
            if ta:
                ta[0].click()
                ta[0].fill("Say hello to me in one short sentence.")
                page.keyboard.press("Enter")
                page.wait_for_timeout(90000)
                R.say("circles after: " + c.evaluate("() => document.body.innerText.slice(-900)").replace("\n", " | "))
            page.screenshot(path=f"{out}/09-chat.png")
    ctx.storage_state(path=jar)
    b.close()
