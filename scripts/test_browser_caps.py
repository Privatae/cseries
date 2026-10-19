#!/usr/bin/env python3
"""Gate for the browser sidecar limits, lifted where they protected nothing.

A host can run long, large work, so a limit stays only where it protects the
pod, and when it is hit the response says so. One section per audit row:

  HR-7   wait / wait_for_text honour `ms`, bounded by the session idle TTL
  HR-8   page text and snapshot page with `offset`, and say where the next page starts
  HR-10  a desktop document is not refused at 4 MiB
  HR-18  a page whose load event is slow is reported navigated, not failed
  HR-20  a recording past its TTL keeps its steps and drops only field values
  HR-22  the route walk says how many routes it found versus walked

No Chromium: pages are scripted fakes; the two page-side scripts (text and
snapshot) are run for real under node when it is installed.

Run: APP_ROOT=<repo> python3 scripts/test_browser_caps.py
"""
import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
import types
from pathlib import Path

_ROOT = Path(os.environ.get("APP_ROOT") or Path(__file__).resolve().parents[1])
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "sidecars" / "browser"))

_FAILURES: list[str] = []


def check(label: str, cond: bool, extra: str = "") -> None:
    print(f"[g3] {'ok  ' if cond else 'FAIL'} {label}"
          f"{('  ' + extra) if extra and not cond else ''}")
    if not cond:
        _FAILURES.append(label)


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


from control import sessions as S  # noqa: E402
from fastapi import HTTPException  # noqa: E402


class _Clock:
    """Virtual time: a page's wait_for_timeout advances it, so a 30 minute
    wait costs nothing to test."""
    def __init__(self):
        self.now = 1000.0

    def monotonic(self):
        return self.now

    def time(self):
        return self.now


class TimeoutError(Exception):  # noqa: A001 — same class name Playwright raises
    pass


class _Page:
    def __init__(self, clock, text_after_s=None, slow_load=False):
        self.clock = clock
        self.t0 = clock.now
        self.text_after_s = text_after_s
        self.slow_load = slow_load
        self.url = "https://app.example.com/"
        self.waited = 0
        self.goto_wait_until = None
        self.main_frame = object()
        self.frames = [self.main_frame]

    def is_closed(self):
        return False

    async def wait_for_timeout(self, ms):
        self.waited += ms
        self.clock.now += ms / 1000

    async def evaluate(self, script, *a):
        if script is S._FIND_TEXT_JS:
            return (self.text_after_s is not None
                    and self.clock.now - self.t0 >= self.text_after_s)
        return None

    async def title(self):
        return "t"

    async def goto(self, url, wait_until="load", timeout=None):
        self.goto_wait_until = wait_until
        if wait_until == "load" and self.slow_load:
            raise TimeoutError("Page.goto: Timeout exceeded")
        self.url = url
        return types.SimpleNamespace(status=200)

    async def wait_for_load_state(self, state, timeout=None):
        if self.slow_load:
            raise TimeoutError("Timeout exceeded")


def _session(page):
    s = S.Session(name="t", allow=["app.example.com"], persist=False)
    s.page = page
    s.pages = [page]
    return s


def _act(page, action, **kw):
    try:
        resp = run(S._act_locked(_session(page), action, S.ActBody(action=action, **kw)))
        return resp.status_code, json.loads(resp.body)
    except HTTPException as e:
        return e.status_code, {"detail": e.detail}


_real_time = S.time
clock = _Clock()
S.time = clock

# ── HR-7 ────────────────────────────────────────────────────────────────
st, out = _act(_Page(clock, text_after_s=25), "wait_for_text", text="ready", ms=60000)
check("HR-7 wait_for_text honours ms: text arriving after 25 s is found when asked to wait 60 s",
      st == 200 and out.get("found") is True, repr((st, out)))

st, out = _act(_Page(clock), "wait_for_text", text="never", ms=40000)
check("HR-7 a wait_for_text that times out reports the time it really waited",
      st == 408 and "40000ms" in str(out["detail"]), repr((st, out)))

st, out = _act(_Page(clock, text_after_s=5), "wait_for_text", text="ready")
check("HR-7 wait_for_text with no ms keeps the action default and still works",
      st == 200 and out.get("found") is True, repr((st, out)))

st, out = _act(_Page(clock), "wait", ms=60000)
check("HR-7 wait honours 60 s (it was silently cut to the 30 s nav timeout)",
      st == 200 and out.get("waited_ms") == 60000, repr((st, out)))

ttl_ms = S.IDLE_TTL_S * 1000
st, out = _act(_Page(clock), "wait", ms=ttl_ms + 5000)
check("HR-7 a wait past the idle TTL is bounded by it and the response says so",
      st == 200 and out.get("waited_ms") == ttl_ms and "ms_limited" in out
      and str(ttl_ms) in out["ms_limited"], repr((st, out)))

st, out = _act(_Page(clock), "wait_for_text", text="never", ms=ttl_ms + 5000)
check("HR-7 wait_for_text past the idle TTL says it was limited",
      st == 408 and "limited" in str(out["detail"]), repr((st, out)))

# ── HR-18 ───────────────────────────────────────────────────────────────
pg = _Page(clock, slow_load=True)
st, out = _act(pg, "navigate", url="https://app.example.com/heavy")
check("HR-18 a page whose load event never fires is reported navigated, not failed",
      st == 200 and out.get("status") == 200 and out.get("load_event") is False
      and "load event" in out.get("note", ""), repr((st, out)))
st, out = _act(_Page(clock), "navigate", url="https://app.example.com/ok")
check("HR-18 a normal page still reports load_event true",
      st == 200 and out.get("load_event") is True, repr((st, out)))

S.time = _real_time

# ── HR-8: text paging, the page-side script run for real ────────────────
node = shutil.which("node")
TMP = Path(os.environ.get("TMPDIR") or tempfile.gettempdir())


def _node(js: str) -> dict:
    f = TMP / "g3_probe.js"
    f.write_text(js)
    r = subprocess.run([node, str(f)], capture_output=True, text=True, timeout=60)
    if r.returncode:
        raise RuntimeError(r.stderr[:400])
    return json.loads(r.stdout)


if node:
    body = "".join(f"line {i:05d}\n" for i in range(5000))     # 55 000 chars
    pre = ("globalThis.document = {body: {innerText: %s}};\n" % json.dumps(body)
           + "const f = " + S._TEXT_JS.strip() + ";\n")
    p1 = _node(pre + "console.log(JSON.stringify(f({max: 20000, offset: 0})));")
    check("HR-8 text: first page is 20000 chars and says there is more",
          len(p1["text"]) == 20000 and p1["truncated"] is True
          and p1["total_chars"] == len(body.strip()) and p1.get("next_offset") == 20000, repr(p1)[:200])
    p2 = _node(pre + "console.log(JSON.stringify(f({max: 20000, offset: 20000})));")
    p3 = _node(pre + "console.log(JSON.stringify(f({max: 20000, offset: 40000})));")
    check("HR-8 text: later pages are reachable and the last one says it is the last",
          p2.get("next_offset") == 40000 and p3["truncated"] is False
          and "next_offset" not in p3, repr((p2.get("next_offset"), p3))[:200])
    check("HR-8 text: the pages join back into the whole text",
          p1["text"] + p2["text"] + p3["text"] == body.strip())

    # snapshot script: 450 visible buttons, real script, fake DOM
    dom = r"""
const mk = (i) => ({tagName: 'BUTTON', type: 'button', disabled: false,
  isContentEditable: false, innerText: 'b' + i, id: '',
  getAttribute: (a) => null, labels: [], closest: () => null,
  checkVisibility: () => true,
  getBoundingClientRect: () => ({top: i, bottom: i + 5, left: 0, right: 5, width: 5, height: 5}),
  getRootNode: () => doc, shadowRoot: null});
const els = Array.from({length: 450}, (_, i) => mk(i));
const doc = {querySelectorAll: (sel) => sel.includes('button') ? els : els};
globalThis.document = doc; globalThis.innerHeight = 100000; globalThis.innerWidth = 1000;
globalThis.location = {href: 'https://x/'}; globalThis.getComputedStyle = () => ({});
globalThis.CSS = {escape: (s) => s};
const f = %s;
const run = (offset) => { const r = f({maxElements: 200, maxName: 120, offset}); return {n: r.nodes.length, info: Object.assign({}, r.info, {elements: r.info.elements.map(e => e.name)})}; };
console.log(JSON.stringify([run(0), run(200), run(400)]));
""" % S._SNAPSHOT_JS.strip()
    a, b, c = _node(dom)
    check("HR-8 snapshot: page 1 holds 200 of 450 and says there is more",
          a["n"] == 200 and a["info"]["truncated"] is True
          and a["info"].get("visible_total") == 450, repr(a["info"])[:160])
    check("HR-8 snapshot: offset 200 starts where page 1 ended",
          b["info"]["elements"][0] == "b200" and b["n"] == 200, repr(b["info"]["elements"][:2]))
    check("HR-8 snapshot: the last page holds the remaining 50 and is not truncated",
          c["n"] == 50 and c["info"]["truncated"] is False, repr(c["info"])[:160])
else:
    print("[g3] SKIP HR-8 page-side scripts: node is not installed")


# HR-8 snapshot stitching across frames (Python side), against scripted frames.
class _Handle:
    def __init__(self, info, n):
        self._info, self._n = info, n

    async def get_property(self, name):
        info, n = self._info, self._n

        class _P:
            async def json_value(_s):
                return info

            async def get_properties(_s):
                class _E:
                    def as_element(_e):
                        return object()

                    async def dispose(_e):
                        pass
                return {i: _E() for i in range(n)}
        return _P()

    async def dispose(self):
        pass


class _Frame:
    def __init__(self, url, total):
        self.url, self.total = url, total

    async def evaluate_handle(self, _js, limits):
        off, mx = limits.get("offset", 0), limits["maxElements"]
        chosen = list(range(self.total))[off:off + mx]
        info = {"url": self.url, "title": "t", "considered": self.total,
                "visible_total": self.total, "truncated": off + len(chosen) < self.total,
                "elements": [{"role": "button", "name": f"{self.url}{i}", "tag": "button"}
                             for i in chosen]}
        return _Handle(info, len(chosen))


class _SnapPage:
    def __init__(self, frames):
        self.frames = frames
        self.main_frame = frames[0]
        self.url = frames[0].url


def _snap(offset):
    pg = _SnapPage([_Frame("main", 150), _Frame("frame", 120)])
    try:
        return run(S._snapshot(_session(pg), pg, offset))
    except TypeError:       # a build with no offset parameter
        return {"element_count": -1, "elements": []}


a = _snap(0)
check("HR-8 snapshot across frames: 200 of 270, total and next_offset reported",
      a["element_count"] == 200 and a.get("total_elements") == 270
      and a["truncated"] is True and a.get("next_offset") == 200, repr({k: a[k] for k in a if k != "elements"}))
b = _snap(200)
check("HR-8 snapshot across frames: page 2 is the last 70 and has no next_offset",
      b["element_count"] == 70 and "next_offset" not in b and b.get("truncated") is False,
      repr({k: b[k] for k in b if k != "elements"}))

# ── HR-20 ───────────────────────────────────────────────────────────────
from control import recorder as R  # noqa: E402

R._reset_for_tests()
R._rec = R.Recording("job-run")
R._rec.add({"go": "https://bank.example.com/"})
R._rec.add({"click": {"role": "button", "name": "Sign in"}})
R._rec.merge_fields("bank.example.com", [
    {"key": "user", "label": "User", "type": "text", "value": "alex", "secret": False},
    {"key": "pw", "label": "Password", "type": "password", "value": "hunter2", "secret": True}])
R.RECORD_TTL_S = 0
R._rec.started -= 5
check("HR-20 past the TTL the recording stops capturing", R.is_active() is False)
st = run(R.status())
check("HR-20 status still returns the steps and says it was auto-stopped",
      st.get("step_count") == 2 and st.get("auto_stopped") is True
      and st.get("values_dropped") is True and st.get("recording") is False, repr(st)[:300])
vals = [f.get("length") for h in st.get("fields", []) for f in h["fields"]]
check("HR-20 the plaintext field values are gone from memory",
      all(v == 0 for v in vals) and all(
          f["value"] == "" for b in (R._rec.fields.values() if R._rec else []) for f in b.values()), repr(vals))
try:
    run(R.start(R.StartBody(name="second-run")))
    blocked = False
except HTTPException as e:
    blocked = e.status_code == 409
check("HR-20 a new recording does not silently replace the uncollected one", blocked)
try:
    out = run(R.stop(R.StopBody()))
except HTTPException as e:
    out = {"detail": e.detail}
check("HR-20 stop hands the kept steps back, with nothing to seal",
      out.get("step_count") == 2 and out.get("captures") == {} and out.get("auto_stopped") is True,
      repr(out)[:300])
check("HR-20 after stop the recorder is clear", R._rec is None)
R._reset_for_tests()

# ── HR-22 ───────────────────────────────────────────────────────────────
from control import verify as V  # noqa: E402


class _VReq:
    def __init__(self, url):
        self.url = url
        self.resource_type = "document"
        self.headers = {}


class _VPage:
    def __init__(self, links):
        self.links, self.url, self.handlers = links, None, {}

    def on(self, e, fn):
        self.handlers[e] = fn

    async def goto(self, url, wait_until="load", **_kw):
        self.url = url
        return types.SimpleNamespace(status=200)

    async def evaluate(self, _s):
        return {"app_ready": True, "sdk_present": True}

    async def eval_on_selector_all(self, *_a):
        return list(self.links)

    async def wait_for_timeout(self, _ms):
        return None


class _VCtx:
    def __init__(self, links):
        self.links = links

    def set_default_timeout(self, *_a): ...
    def set_default_navigation_timeout(self, *_a): ...

    async def route(self, *_a): ...
    async def add_init_script(self, *_a): ...
    async def add_cookies(self, *_a): ...

    async def new_page(self):
        return _VPage(self.links)

    async def close(self): ...


class _VBrowser:
    def __init__(self, links):
        self.links = links

    async def new_context(self, **_kw):
        return _VCtx(self.links)


K = "entity-9128-0.entity-9128.entity-9128.svc.cluster.local"
MOUNT = "/system-apps/trading-bot/"
ENTRY = f"http://{K}:8800{MOUNT}index.html"


def _verify(n_links):
    links = [f"http://{K}:8800{MOUNT}r{i}" for i in range(n_links)]
    V.set_browser_provider(lambda: _VBrowser(links))
    return json.loads(run(V.verify(V.VerifyBody(url=ENTRY, settle_ms=0))).body)


rep = _verify(30)
check("HR-22 the report states routes found and not walked",
      rep.get("routes_found") == 30 and rep.get("routes_not_walked") == 30 - len(rep["routes_walked"])
      and rep["routes_not_walked"] > 0, repr({k: rep.get(k) for k in ("routes_found", "routes_not_walked")}))
check("HR-22 a passing verdict over a partial walk says PARTIAL in plain words",
      rep["ok"] is True and "PARTIAL" in rep["why"] and "30" in rep["why"], rep["why"])
rep = _verify(3)
check("HR-22 a full walk is not called partial",
      rep.get("routes_found") == 3 and rep.get("routes_not_walked") == 0
      and "PARTIAL" not in rep["why"], rep["why"])

# ── HR-10 ───────────────────────────────────────────────────────────────
from control import document_verify as D  # noqa: E402

big = "<html>" + "x" * (5 * 1024 * 1024) + "</html>"
try:
    D.DocumentBody(page=big)
    accepted = True
except Exception:  # pydantic ValidationError on main
    accepted = False
check("HR-10 a 5 MiB desktop document is accepted for checking", accepted)

D.MAX_PAGE_CHARS = 1000
try:
    run(D.verify_document(types.SimpleNamespace(
        page="y" * 2000, probes=[], settle_ms=0, width=1440, height=900, subject="subject")))
    msg = ""
except HTTPException as e:
    msg = f"{e.status_code} {e.detail}"
check("HR-10 a page past the pod-protecting bound is refused in plain words",
      msg.startswith("413") and "not checked" in msg, msg)

print()
if _FAILURES:
    print(f"[g3] {len(_FAILURES)} FAILED: " + "; ".join(_FAILURES))
    sys.exit(1)
print("[g3] all ok")
