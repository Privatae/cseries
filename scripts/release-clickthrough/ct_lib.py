import json, os, sys, time


def test_host_or_exit():
    """The Host URL to drive, or exit. These steps seal stand-in keys over the
    engine's own (harness-<id>), revoke them and chat, so they run only on a
    machine the caller names as a test machine: CSERIES_CLICKTHROUGH_TEST_HOST
    must equal CSERIES_HOST_URL. The default URL is a local install, which can
    be a person's real Host, so nothing runs on it by default."""
    b = os.environ.get("CSERIES_HOST_URL", "http://home.cseries.localhost:8800").rstrip("/")
    if os.environ.get("CSERIES_CLICKTHROUGH_TEST_HOST", "").rstrip("/") != b:
        sys.exit(f"refusing to drive {b}: set CSERIES_CLICKTHROUGH_TEST_HOST={b} to say it is a "
                 "test machine (these steps replace and revoke keys)")
    return b
INIT = """(() => { window.addEventListener('unhandledrejection', (e) => {
  try { console.error('[unhandledrejection] ' + (e.reason && (e.reason.stack || e.reason.message) || String(e.reason))); } catch (x) {} });
  window.addEventListener('error', (e) => { try { if (e.message) console.error('[window.onerror] ' + e.message + ' @ ' + e.filename + ':' + e.lineno); } catch (x) {} }); })();"""
class Rec:
    def __init__(self, out):
        self.f = open(f"{out}/events.jsonl", "w"); self.step = "start"; self.n = 0
        self.steps = open(f"{out}/steps.log", "w")
    def ev(self, kind, **kw):
        kw.update(kind=kind, step=self.step, t=time.strftime('%H:%M:%S'))
        self.f.write(json.dumps(kw) + "\n"); self.f.flush()
    def say(self, s):
        line = time.strftime('%H:%M:%S ') + f"[{self.step}] " + s
        print(line, flush=True); self.steps.write(line + "\n"); self.steps.flush()
    def attach(self, ctx, page):
        ctx.add_init_script(INIT)
        page.on("console", lambda m: self.ev("console", type=m.type, text=m.text[:600], url=(m.location or {}).get("url", "")[:200]))
        page.on("pageerror", lambda e: self.ev("pageerror", text=str(e)[:600]))
        page.on("requestfailed", lambda r: self.ev("requestfailed", method=r.method, url=r.url[:300], failure=r.failure))
        page.on("response", lambda r: r.status >= 400 and self.ev("http", status=r.status, method=r.request.method, url=r.url[:300]))
def frames(page): return [f.url.split('?')[0] for f in page.frames if f != page.main_frame]
def frame(page, app, wait=15000):
    t = time.time()
    while time.time() - t < wait / 1000:
        for f in page.frames:
            if f"/system-apps/{app}/" in f.url and f != page.main_frame: return f
        page.wait_for_timeout(200)
    return None
def wins(page):
    return page.evaluate("() => (window.WindowManager && WindowManager.getAll ? WindowManager.getAll().map(w => w.id) : [])")
