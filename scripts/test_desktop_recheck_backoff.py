#!/usr/bin/env python3
"""Gate: the worn desktop's re-check never wakes paid compute for nothing.

On a Host a desktop worn "not render-checked" was re-asked every 300 s
forever: each try woke the 2-vCPU browser, which answered HTTP 404 (its image predated
/v1/verify/document) and then lingered 240 s. This drives the REAL
desktop_pin / desktop_render / harness_backend code with only the browser
call (app_verify.browser_sidecar_post) and the clock scripted:

  - a browser that answers it has no render check (404) is remembered with
    its image; the background re-check, a Host write and a landed build do
    not wake it again until a wake reports a different image; then it is
    asked once, and the browser is released as soon as it answers (linger 0);
  - the notice says what is true ("too old", "asked again later"), never
    "checked again when a browser can be woken";
  - any other not-checked answer is re-asked with a doubling wait (300 s,
    600 s, ... capped at 6 h), kept in the pin's record so a restart does
    not reset it, and never on the first poll after a set that just checked;
  - a document whose only failure is "shows nothing" is looked at once more
    with a longer settle before it is refused;
  - every desktop document a landed build holds is listed for checking, and
    .html files past the scan backstop are named as not scanned;
  - a resident app whose pod will not start is re-woken with a doubling wait
    capped at 5 min, never every 60 s forever, and back to the first wait
    once one lands;
  - the image a wake reports is kept (harness_backend.known_image).

No Chromium, no pod, no DB.
Run: APP_ROOT=<repo> python3 scripts/test_desktop_recheck_backoff.py
"""
import asyncio
import os
import sys
import tempfile
import time
import types
from pathlib import Path

_ROOT = Path(os.environ.get("APP_ROOT") or Path(__file__).resolve().parents[1])
sys.path.insert(0, str(_ROOT / "brain"))
sys.path.insert(0, str(_ROOT))
sys.modules.setdefault("server.main", types.ModuleType("server.main"))

_FAILURES: list[str] = []


def check(label: str, cond: bool, extra: str = "") -> None:
    print(f"[recheck] {'ok  ' if cond else 'FAIL'} {label}"
          f"{('  ' + extra) if extra and not cond else ''}")
    if not cond:
        _FAILURES.append(label)


try:
    from server.applications import artifacts as ART  # noqa: E402
    from server.applications import desktop_pin as PIN  # noqa: E402
    from server.applications import desktop_render as DR  # noqa: E402
    from server.applications import harness_backend as HB  # noqa: E402
    from mastercode.runtime import app_verify as AV  # noqa: E402
    for name in ("sidecar_outdated", "SETTLE_RETRY_MS"):
        if not hasattr(DR, name):
            raise ImportError(f"desktop_render has no {name}")
    for name in ("known_image", "resident_retry_wait"):
        if not hasattr(HB, name):
            raise ImportError(f"harness_backend has no {name}")
except ImportError as e:
    check("the code under test has the re-check backoff (" + str(e) + ")", False)
    print(f"\n[recheck] {len(_FAILURES)} FAILED")
    sys.exit(1)

LOOP = asyncio.new_event_loop()
asyncio.set_event_loop(LOOP)
run = LOOP.run_until_complete
TMP = Path(tempfile.mkdtemp(dir=os.environ.get("TMPDIR")))
ART._projects_root = lambda: TMP / "projects"
DR.state_dir = lambda: TMP / "kernel-state"


async def _owned(*_a, **_k):
    return None


ART._require_artifact = _owned


async def _stand_in(_pool):
    return [], "smoke"


DR._stand_in = _stand_in


async def _quiet():
    return None


PIN._notify_shell = _quiet

WORN = {"id": 7}


class Pool:
    async def fetchrow(self, sql, *a):
        if "FROM entity_appearance" in sql:
            return {"wallpaper": "html", "wallpaper_mobile": None,
                    "desktop_artifact_id": WORN["id"], "desktop_entry": "index.html"}
        return None

    async def execute(self, *_a):
        return "UPDATE 1"

    async def fetchval(self, *_a):
        return None

    def acquire(self):
        pool = self

        class _A:
            async def __aenter__(self_inner):
                return pool

            async def __aexit__(self_inner, *a):
                return False
        return _A()


POOL = Pool()

GOOD = {"loaded": True, "subject_found": True, "ready": True, "uncaught": [],
        "paint": {"painted_share": 0.3, "dominant": "#101010", "content_painted_px": [900]},
        "content": {"text": 1, "media": 0}, "probes": {}, "settle_ms": DR.SETTLE_MS}
BLANK = {"loaded": True, "subject_found": True, "ready": True, "uncaught": [],
         "paint": {"painted_share": 0.0, "dominant": "#000000", "content_painted_px": [0]},
         "content": {"text": 1, "media": 0}, "probes": {}, "settle_ms": DR.SETTLE_MS}
SIDE: dict = {"mode": "404", "calls": [], "late": 0}


# The browser's hold, as browser_sidecar_post leaves it: lingering when the
# call asked for a linger. RELEASED records each release the check makes.
HOLD = {"lingering": False}
RELEASED: list = []


async def _release_now(app_id, harness_id):
    RELEASED.append(("release_now", app_id))
    HOLD["lingering"] = False


def _linger(app_id, harness_id, idle_s):
    RELEASED.append(("linger", idle_s))
    HOLD["lingering"] = True


HB.lingering = lambda app_id: HOLD["lingering"]
HB.release_now = _release_now
HB.linger = _linger


async def fake_post(path, payload, *, what, label="", linger_s=0.0):
    SIDE["calls"].append({"path": path, "linger_s": linger_s,
                          "settle_ms": payload.get("settle_ms")})
    HOLD["lingering"] = linger_s > 0
    mode = SIDE["mode"]
    if mode == "404":
        return {"checked": False,
                "reason": f'{what} answered HTTP 404: {{"detail":"Not Found"}}'}
    if mode == "down":
        return {"checked": False, "reason": "browser wake refused or failed (see harness log)"}
    if mode == "late":          # blank at 2 s, drawn after the longer settle
        return {"checked": True,
                "report": GOOD if payload.get("settle_ms") == DR.SETTLE_RETRY_MS else BLANK}
    if mode == "blank":
        return {"checked": True, "report": BLANK}
    return {"checked": True, "report": GOOD}


AV.browser_sidecar_post = fake_post

CLOCK = [time.time()]
PIN.time = types.SimpleNamespace(time=lambda: CLOCK[0], monotonic=time.monotonic)


def doc(tag: str) -> str:
    return (f"<!DOCTYPE html><html><head><meta name=\"cseries-desktop\" content=\"wallpaper\">"
            f"</head>\n<body>\n<p>{tag}</p>\n</body></html>")


def ws(aid):
    p = TMP / "projects" / str(aid)
    p.mkdir(parents=True, exist_ok=True)
    return p


def write(aid, tag):
    return run(ART.write_files(POOL, 3, aid, [{"path": "index.html", "content": doc(tag)}],
                               desktop_wait_s=5))


def settle():
    run(asyncio.sleep(0.05))


def poll(aid, n=1):
    for _ in range(n):
        run(PIN.reconcile(POOL, 3, aid, "index.html"))
        settle()


# ── A. an older browser (404): remembered with its image, not asked again ───
ws(7)
PIN.write_pin(7, "index.html", doc("GOOD v0"), {"checked": True, "ok": True}, origin="set")
HB._last_image.pop("browser", None)
SIDE.update(mode="404", calls=[])
r = write(7, "GOOD v1")
d = r.get("desktop") or {}
check("a write the browser answers 404 to is worn NOT RENDER-CHECKED, the browser asked once "
      "with the write's linger", d.get("advanced") is True and len(SIDE["calls"]) == 1
      and SIDE["calls"][0]["linger_s"] == DR.LINGER_S, repr((d, SIDE["calls"])))
rec = DR.sidecar_outdated()
check("...and the answer is remembered as an outdated browser", rec is not None, repr(rec))
st = PIN.status(7) or {}
msg = st.get("message", "")
check("...the notice says the browser is too old, not that it is checked 'when a browser can "
      "be woken'", "too old" in msg and "when a browser can be woken" not in msg, msg)

real_recheck = PIN.RECHECK_S
PIN.RECHECK_S = 0.0                      # even with no wait at all ...
CLOCK[0] += 10 * 3600
SIDE["calls"].clear()
poll(7, 3)
check("the background re-check does NOT wake an outdated browser (3 polls, no wait)",
      SIDE["calls"] == [], repr(SIDE["calls"]))

r = write(7, "GOOD v2 (Host edit)")
d = r.get("desktop") or {}
check("a Host write to the worn desktop does not wake it either",
      SIDE["calls"] == [] and d.get("advanced") is True, repr((d, SIDE["calls"])))
check("...and the write says why it was not checked",
      "not asked" in str((d.get("render") or {}).get("reason") or ""), repr(d))

# A person's own wear still asks (that is how a new image is ever learned
# when nothing else wakes the browser).
rc = run(DR.render_check(POOL, doc("person wears")))
check("a person's own check (set / wear) still asks the browser",
      len(SIDE["calls"]) == 1 and rc.get("outdated") is True, repr(rc))
SIDE["calls"].clear()

# A wake (by anything) reports a NEW image: the answer no longer stands.
HB._last_image["browser"] = "registry.example/cseries-browser:v0.5.3"
check("a different browser image clears the outdated answer", DR.sidecar_outdated() is None)
SIDE["mode"] = "ok"
RELEASED.clear()
poll(7, 1)
check("...the re-check asks once per size (desktop, then phone), and releases the browser as "
      "soon as the last answers (held only for the next look, then released at once)",
      len(SIDE["calls"]) == 2
      and all(c["linger_s"] == DR.SECOND_LOOK_LINGER_S for c in SIDE["calls"])
      and RELEASED == [("release_now", "browser")], repr((SIDE["calls"], RELEASED)))
pin = PIN.read_pin(7) or {}
check("...a pass is worn as checked", pin.get("checked") is True
      and (PIN.status(7) or {}).get("state") == "current", repr(pin))
check("...and the outdated record is gone", not (DR.state_dir() / "sidecar-outdated.json").exists())
PIN.RECHECK_S = real_recheck

# ── B. any other not-checked answer: a doubling wait, kept across restarts ──
WORN["id"] = 8
ws(8)
SIDE.update(mode="down", calls=[])
html8 = doc("GOOD w1")
(ws(8) / "index.html").write_text(html8)
PIN.write_pin(8, "index.html", ART.render_workspace(ws(8), "index.html"),
              {"checked": False, "reason": "the plan has no room for the browser right now"},
              origin="set")
poll(8, 2)
check("the first poll after a set that just ran a check does NOT check again",
      SIDE["calls"] == [], repr(SIDE["calls"]))
t0 = CLOCK[0]
waits = []
RELEASED.clear()
asked = 0
for expect in (300, 600, 1200, 2400):
    CLOCK[0] += expect - 1
    poll(8, 1)
    early = len(SIDE["calls"])
    CLOCK[0] += 1
    poll(8, 1)
    waits.append((expect, early, len(SIDE["calls"])))
    asked += len(SIDE["calls"])
    SIDE["calls"].clear()
check("each re-check that cannot run doubles the wait: 300, 600, 1200, 2400 s",
      all(e == 0 and n == 1 for _x, e, n in waits), repr(waits))
check("...and every background re-check released the browser at once",
      asked == 4 and RELEASED == [("release_now", "browser")] * 4, repr((asked, RELEASED)))
check("the wait is capped at 6 h", PIN._recheck_wait(40) == PIN.RECHECK_MAX_S == 6 * 3600.0)
check("...even at try 5,000 (no OverflowError)", PIN._recheck_wait(5000) == 6 * 3600.0)

# A kernel restart: the in-memory maps are gone, the pin's record is not.
PIN._seen_fp.clear()
PIN._seq.clear()
PIN._applied.clear()
poll(8, 2)
check("a restart does not reset the wait (it is kept in the pin's record)",
      SIDE["calls"] == [], repr(SIDE["calls"]))
CLOCK[0] += 4800
poll(8, 1)
check("...and the next try comes when the doubled wait is up", len(SIDE["calls"]) == 1)
st = (PIN.status(8) or {}).get("message", "")
check("the notice says it is asked again later, waiting longer each time",
      "waits twice as long" in st and "when a browser can be woken" not in st, st)

# ── C. drawn late is not blank ──────────────────────────────────────────────
SIDE.update(mode="late", calls=[])
RELEASED.clear()
v = run(DR.render_check(POOL, doc("fades in"), linger_s=0.0))
check("a document blank at 2 s and drawn after the longer settle PASSES",
      v.get("checked") and v.get("ok"), repr(v)[:300])
check("...it was looked at twice at each size, the second time with the longer settle "
      "(desktop, then phone)",
      [c["settle_ms"] for c in SIDE["calls"]]
      == [DR.SETTLE_MS, DR.SETTLE_RETRY_MS, DR.SETTLE_MS, DR.SETTLE_RETRY_MS],
      repr(SIDE["calls"]))
check("...the browser was kept from each look to the next (no cold wake between), then "
      "released as the caller asked once the last had answered",
      [c["linger_s"] for c in SIDE["calls"]] == [DR.SECOND_LOOK_LINGER_S] * 4
      and RELEASED == [("release_now", "browser")], repr((SIDE["calls"], RELEASED)))
SIDE.update(mode="blank", calls=[])
v = run(DR.render_check(POOL, doc("never draws")))
check("a document still blank after the longer settle is refused, saying it shows nothing",
      v.get("checked") and not v.get("ok") and "shows nothing" in str(v.get("why")),
      repr(v)[:300])

# ── C2. the image changes and nothing has to be woken to see it ──────────────
DCLOCK = [1_000_000.0]
DR.time = types.SimpleNamespace(time=lambda: DCLOCK[0], monotonic=lambda: DCLOCK[0])
HB._last_image.pop("browser", None)
SIDE.update(mode="404", calls=[])
run(DR.render_check(POOL, doc("probe 1")))
rec = DR.sidecar_outdated()
check("no image known on either side: the outdated answer stands at first",
      rec is not None and rec.get("image") is None, repr(rec))
DCLOCK[0] += DR.UNKNOWN_IMAGE_PROBE_S - 1
stands = DR.sidecar_outdated() is not None
DCLOCK[0] += 2
check("...until its probe wait is up (1 h), then one caller may ask again",
      stands and DR.sidecar_outdated() is None)
run(DR.render_check(POOL, doc("probe 2")))                     # still 404
DCLOCK[0] += DR.UNKNOWN_IMAGE_PROBE_S + 1
check("...a second 404 doubles the wait (2 h), so an old operator costs a handful of "
      "wakes a day at most", DR.sidecar_outdated() is not None)
check("...capped at a day", DR._probe_wait(5000) == DR.UNKNOWN_IMAGE_PROBE_MAX_S)


class _CapBC:
    _connected = True
    asked: list = []

    async def request_full(self, op, params):
        self.asked.append((op, params))
        return {"ok": True, "data": {"gated": False, "fits": ["browser"],
                                     "images": {"browser": "reg/cseries-browser:v0.5.9"}}}


_bc = _CapBC()
sys.modules["server.main"].get_bridge_client = lambda: _bc
DR._image_refreshed[0] = 0.0
run(DR.refresh_browser_image())
check("the background path asks the operator for the browser's image without a wake",
      _bc.asked == [("harness.capacity", {"harnesses": ["browser"]})]
      and HB.known_image("browser") == "reg/cseries-browser:v0.5.9", repr(_bc.asked))
check("...and a new image clears the outdated answer with nothing woken",
      DR.sidecar_outdated() is None and SIDE["calls"][-1:] and len(SIDE["calls"]) == 2)
run(DR.refresh_browser_image())
check("...asked at most every IMAGE_REFRESH_S", len(_bc.asked) == 1)
DR.time = time
HB._last_image.pop("browser", None)
(DR.state_dir() / "sidecar-outdated.json").unlink(missing_ok=True)

# ── D. every landed desktop document, the rest named ────────────────────────
proj = TMP / "proj-many"
proj.mkdir()
for i in range(30):
    (proj / f"theme-{i:02d}.html").write_text(doc(f"theme {i}"))
docs, unscanned = DR.landed_desktop_documents(proj)
check("all 30 desktop documents of a landed build are listed for checking (no count cap)",
      len(docs) == 30 and unscanned == [], f"{len(docs)} {unscanned[:3]}")
real_scan = DR._MAX_SCANNED_FILES
DR._MAX_SCANNED_FILES = 25
docs, unscanned = DR.landed_desktop_documents(proj)
DR._MAX_SCANNED_FILES = real_scan
check(".html files past the scan backstop are NAMED as not scanned, not dropped",
      len(docs) == 25 and len(unscanned) == 5, f"{len(docs)} {unscanned}")

# ── E. a resident app that will not start: doubling re-wake ─────────────────
SLEEPS: list[float] = []


class _AsyncioProxy:
    def __getattr__(self, name):
        return getattr(asyncio, name)

    @staticmethod
    async def sleep(d):
        SLEEPS.append(float(d))
        await asyncio.sleep(0)


wakes = {"n": 0}


async def _wake_app(app_id):
    wakes["n"] += 1
    if wakes["n"] <= 4:
        return {"ok": False, "error": "operator /harness/wake 504: not Ready"}
    return {"ok": True, "url": "http://[fd00::5]:8080"}


real_asyncio, real_wake_app = HB.asyncio, HB._wake_app
HB.asyncio = _AsyncioProxy()
HB._wake_app = _wake_app


async def _hold():
    HB.app_hold("bot")


run(_hold())


async def _until(n):
    for _ in range(2000):
        if len(SLEEPS) >= n:
            return
        await asyncio.sleep(0)


run(_until(7))
HB._state["bot"]["resident"] = False
run(asyncio.sleep(0))
HB.asyncio, HB._wake_app = real_asyncio, real_wake_app
base, cap = HB.APP_RT_RESIDENT_RETRY_S, HB.APP_RT_RESIDENT_RETRY_MAX_S
want = [base, base, base * 2, base * 4, min(base * 8, cap), HB.HARNESS_LEASE_HEARTBEAT_S]
check("a resident pod that will not start is re-woken with a doubling wait, capped, and the "
      "heartbeat cadence once it is up", SLEEPS[:6] == [float(x) for x in want],
      f"{SLEEPS[:7]} want {want}")
check("...never more often than the cap once it has failed a few times",
      HB.resident_retry_wait(50) == float(cap) and HB.resident_retry_wait(5000) == float(cap))

# ── F. the image a wake reports is kept ─────────────────────────────────────


async def _wake(harness_id, creds=None):
    return {"ok": True, "url": "http://harness-codex:8840", "image": "reg/cseries-harness-codex@sha256:ab"}


async def _no_token(*_a, **_k):
    raise RuntimeError("no pool in this gate")


real_wake, real_tok = HB._wake, HB.egress_subject_token
HB._wake, HB.egress_subject_token = _wake, _no_token
run(HB.ensure_awake("probe", "codex"))
HB._wake, HB.egress_subject_token = real_wake, real_tok
check("the image the operator's wake reports is kept (harness_backend.known_image)",
      HB.known_image("probe") == "reg/cseries-harness-codex@sha256:ab", repr(HB.known_image("probe")))

if _FAILURES:
    print(f"\n[recheck] {len(_FAILURES)} FAILED")
    sys.exit(1)
print("\n[recheck] ALL OK")
