#!/usr/bin/env python3
"""Gate: a broken desktop can never be what the user WEARS.

The render check runs at set / wear / Try (scripts/test_desktop_render_doors.py).
But the desktop is a pointer to an artifact the Host keeps editing, and every
edit used to reach the screen with no check at all. Now wearing PINS the
checked render (server/applications/desktop_pin.py), and the pin moves only
through the same check. This gate drives the REAL artifact service writes
(server/applications/artifacts.py) on a temp workspace, with only the browser
call scripted:

  - a write whose new render FAILS keeps the previous good render on screen,
    and the write's result carries the error and its line;
  - a write that passes advances the pin, and the shell's change stamp moves;
  - a write the browser could not check advances it with NOT RENDER-CHECKED;
  - a write to an artifact that is not worn checks nothing;
  - a slow check answers "pending" with a job, and lands when it finishes;
  - an older check finishing after a newer one never rolls the screen back;
  - a writer that bypasses the artifact ops is caught by reconcile (the
    shell's own poll) and still reaches the screen only by passing;
  - a desktop worn before pins existed keeps its pre-edit render when an
    edit fails;
  - the shell is served the pin, and its remount stamp is the pin's.
Plus the door runner (202 + a job past the router's window) and the
Workbench's landed-build verdict joining the phase's proof.

No Chromium, no pod, no DB.
Run: APP_ROOT=<repo> python3 scripts/test_desktop_worn_pin.py
"""
import asyncio
import os
import sys
import tempfile
import types
from pathlib import Path

_ROOT = Path(os.environ.get("APP_ROOT") or Path(__file__).resolve().parents[1])
sys.path.insert(0, str(_ROOT / "brain"))
sys.path.insert(0, str(_ROOT))
sys.modules.setdefault("server.main", types.ModuleType("server.main"))

_FAILURES: list[str] = []


def check(label: str, cond: bool, extra: str = "") -> None:
    print(f"[worn-pin] {'ok  ' if cond else 'FAIL'} {label}"
          f"{('  ' + extra) if extra and not cond else ''}")
    if not cond:
        _FAILURES.append(label)


from fastapi import HTTPException  # noqa: E402

from server.applications import artifacts as ART  # noqa: E402
from server.applications import desktop_pin as PIN  # noqa: E402
from server.applications import desktop_render as DR  # noqa: E402

LOOP = asyncio.new_event_loop()
run = LOOP.run_until_complete
TMP = Path(tempfile.mkdtemp())
ART._projects_root = lambda: TMP
# Kernel-private state (pins, remembered failures, the Designs try): beside
# the projects tree in the image, a private dir here.
DR.state_dir = lambda: TMP / ".kernel-state"


async def _owned(*_a, **_k):
    return None


ART._require_artifact = _owned


def doc(tag: str) -> str:
    return (f"<!DOCTYPE html><html><head><meta name=\"cseries-desktop\" content=\"wallpaper\">"
            f"</head>\n<body>\n<p>{tag}</p>\n</body></html>")


WORN = {"id": 7}


class Pool:
    async def fetchrow(self, sql, *a):
        if "FROM entity_appearance" in sql:
            return {"wallpaper": "html", "wallpaper_mobile": None,
                    "desktop_artifact_id": WORN["id"], "desktop_entry": "index.html"}
        if "FROM projects" in sql:
            return {"updated_at": None}
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
CALLS: list[str] = []


BROWSER = {"up": True}


async def fake_check(pool, html, **_kw):
    CALLS.append(html)
    if not BROWSER["up"]:
        return {"checked": False, "reason": "the plan has no room for the browser right now"}
    if "SLOW" in html:
        await asyncio.sleep(0.4)
    if "BROKEN" in html:
        return {"checked": True, "ok": False, "why": "an uncaught error at line 3: `x()` — boom",
                "failures": ["an uncaught error at line 3: `x()` — ReferenceError: x is not defined"]}
    if "NOBROWSER" in html:
        return {"checked": False, "reason": "browser wake refused or failed (see harness log)"}
    if "PHONEOFF" in html:
        # Passes at both sizes; on a phone 8 tiles sit past the side edges.
        return {"checked": True, "ok": True, "why": "rendered; on a phone (360×640) too",
                "failures": [], "phone": {"checked": True, "ok": True, "why": "rendered",
                                          "tiles_off": 8},
                "warnings": ["on a phone (360×640) 8 of the 15 app tiles sit past the screen's "
                             "side edges, where nobody can tap them"]}
    return {"checked": True, "ok": True, "why": "rendered", "failures": []}


# The browser itself is the stub; render_check's own memory of failed bytes
# runs for real.
DR._render_check = fake_check


async def _quiet():
    return None


PIN._notify_shell = _quiet


def ws(aid):
    p = TMP / str(aid)
    p.mkdir(parents=True, exist_ok=True)
    return p


def write(aid, tag, **kw):
    return run(ART.write_files(POOL, 3, aid, [{"path": "index.html", "content": doc(tag)}], **kw))


def worn(aid=7):
    return run(PIN.worn_html(POOL, 3, aid, "index.html"))


# ── 1. wearing pins what was checked ────────────────────────────────────────
ws(7)
write(7, "GOOD v1", desktop_wait_s=5)            # pins nothing yet: no pin, no history before
PIN.write_pin(7, "index.html", ART.render_workspace(ws(7), "index.html"),
              {"checked": True, "ok": True}, origin="set")
check("the worn desktop is the pinned render", "GOOD v1" in worn())
stamp1 = PIN.pin_stamp(7, "index.html")

# ── 2. a failing edit keeps the good one on screen ──────────────────────────
r = write(7, "BROKEN v2", desktop_wait_s=5)
d = r.get("desktop") or {}
check("an edit that does not render is NOT worn", d.get("advanced") is False, repr(d))
check("...and the write's result names the error and its line",
      "NOT on the desktop" in (d.get("message") or "") and "line 3" in d.get("message", ""),
      d.get("message", "")[:200])
check("...the previous good render stays on screen", "GOOD v1" in worn())
check("...and the shell's remount stamp did not move", PIN.pin_stamp(7, "index.html") == stamp1)
check("...while the edit itself is kept in the artifact (the Host fixes it there)",
      "BROKEN v2" in (ws(7) / "index.html").read_text())

# ── 3. a passing edit advances ──────────────────────────────────────────────
r = write(7, "GOOD v3", desktop_wait_s=5)
check("an edit that renders is worn", (r.get("desktop") or {}).get("advanced") is True
      and "GOOD v3" in worn())
check("...and the shell's remount stamp moved", PIN.pin_stamp(7, "index.html") != stamp1)

# ── 4. no browser: advance, and say so ──────────────────────────────────────
r = write(7, "NOBROWSER v4", desktop_wait_s=5)
d = r.get("desktop") or {}
check("an edit the browser could not check is worn (the user's own screen)",
      d.get("advanced") is True and "NOBROWSER v4" in worn(), repr(d))
check("...with the same NOT RENDER-CHECKED warning set and wear give",
      "NOT RENDER-CHECKED" in (d.get("warning") or ""))

# ── 5. not the worn desktop: nothing to check ───────────────────────────────
ws(8)
CALLS.clear()
r = write(8, "BROKEN elsewhere", desktop_wait_s=5)
check("a write to an artifact that is not worn checks nothing and says nothing",
      "desktop" not in r and CALLS == [])

# ── 6. slow check: pending, then lands ──────────────────────────────────────
real_budget = DR.DOOR_BUDGET_S
DR.DOOR_BUDGET_S = 0.05
r = write(7, "GOOD SLOW v5")          # a route's default wait
d = r.get("desktop") or {}
check("a check that outlasts the window answers pending, with a job to poll",
      d.get("pending") is True and d.get("job") and d.get("poll", "").endswith(d["job"]), repr(d))
check("...the screen has not changed yet", "NOBROWSER v4" in worn())
run(asyncio.sleep(0.8))
j = DR.job_status(d["job"])
check("...the job ends with the same verdict", j and j["status"] == "done"
      and j["result"].get("advanced") is True, repr(j))
check("...and the screen moved when it passed", "GOOD SLOW v5" in worn())

# ── 7. an older check never rolls the screen back ───────────────────────────
r_old = write(7, "GOOD SLOW v6")       # pending (slow)
r_new = write(7, "GOOD v7", desktop_wait_s=5)
run(asyncio.sleep(0.8))
check("a newer edit's verdict wins over an older, slower one",
      r_new["desktop"].get("advanced") is True and "GOOD v7" in worn(), worn()[-60:])
check("...the older one reports it was superseded",
      (DR.job_status(r_old["desktop"]["job"]) or {}).get("result", {}).get("superseded") is True)
DR.DOOR_BUDGET_S = real_budget

# ── 8. a writer that bypasses the artifact ops ──────────────────────────────
(ws(7) / "index.html").write_text(doc("BROKEN direct"))
run(PIN.reconcile(POOL, 3, 7, "index.html"))
run(asyncio.sleep(0.2))
check("reconcile checks a worn artifact changed outside the ops — a failure stays off screen",
      "GOOD v7" in worn())
(ws(7) / "index.html").write_text(doc("GOOD direct v8") + " ")
run(PIN.reconcile(POOL, 3, 7, "index.html"))
run(asyncio.sleep(0.2))
check("...and a pass reaches the screen", "GOOD direct v8" in worn())
CALLS.clear()
run(PIN.reconcile(POOL, 3, 7, "index.html"))
check("...an unchanged workspace costs no render and no check", CALLS == [])

# ── 9. a desktop worn before pins existed ──────────────────────────────────
WORN["id"] = 9
ws(9)
run(ART.write_files(POOL, 3, 9, [{"path": "index.html", "content": doc("OLD GOOD")}]))
# (the write above was the first with this artifact worn; reset to legacy)
import shutil  # noqa: E402
shutil.rmtree(PIN._dir(9), ignore_errors=True)
r = write(9, "BROKEN after deploy", desktop_wait_s=5)
check("a legacy desktop's failed edit leaves its pre-edit render worn",
      (r.get("desktop") or {}).get("advanced") is False and "OLD GOOD" in worn(9), worn(9)[-80:])
WORN["id"] = 7

# ── 10. what the shell is served ────────────────────────────────────────────
from server.routes import appearance as A  # noqa: E402


class _Conn:
    async def fetchrow(self, sql, *a):
        import datetime as _dt
        return {"updated_at": _dt.datetime(2026, 9, 1, tzinfo=_dt.timezone.utc)}


desk = run(A._desktop_pointer(_Conn(), 3, {"desktop_artifact_id": 7, "desktop_entry": "index.html"}))
check("the shell's remount stamp is the pin's, not the artifact's last write",
      desk["updated_at"] == PIN.pin_stamp(7, "index.html"), repr(desk))


async def _pointer(_pool):
    return {"artifact_id": 7, "entry": "index.html", "updated_at": "x"}


A._current_pointer = _pointer
A.brain_service = types.SimpleNamespace(get_pool=lambda: POOL, _entity_id=9003)
(ws(7) / "index.html").write_text(doc("BROKEN unserved"))
resp = run(A.serve_desktop_html())
check("GET /api/appearance/desktop serves the pin, never the unchecked working copy",
      b"GOOD direct v8" in resp.body and b"BROKEN unserved" not in resp.body)
run(asyncio.sleep(0.2))

# ── 11. the door runner: inside the window, or 202 + a job ──────────────────


async def fast():
    return {"wallpaper": "html"}


async def refused():
    raise HTTPException(422, "REFUSED — the document does not render in a browser")


async def slow_refused():
    await asyncio.sleep(0.3)
    raise HTTPException(422, "REFUSED — slow")


check("a door that finishes in time answers as it always did",
      run(DR.run_door("worn", fast, budget_s=1)) == {"wallpaper": "html"})
try:
    run(DR.run_door("worn", refused, budget_s=1))
    raised = None
except HTTPException as e:
    raised = e.status_code
check("...and a refusal in time is the same HTTP refusal", raised == 422)
resp = run(DR.run_door("published", slow_refused, budget_s=0.05))
import json as _json  # noqa: E402
body = _json.loads(resp.body)
check("past the window: 202 with a job to poll",
      resp.status_code == 202 and body.get("pending") and body["poll"].endswith(body["job"]))
run(asyncio.sleep(0.5))
j = DR.job_status(body["job"])
check("...whose answer is the refusal the request would have raised",
      j["status"] == "failed" and j["http_status"] == 422 and "slow" in j["detail"], repr(j))
check("an unknown job is None (the route answers 404)", DR.job_status("nope") is None)
from server.applications import long_request as LR  # noqa: E402
check("a desktop job is not readable through another area's poll route (scope stays bound)",
      LR.status(body["job"], area="backup") is None and LR.status(body["job"], area="desktop"))
check("the window is the router's 30 s less room for the request (20 s)",
      DR.DOOR_BUDGET_S == 20.0)

# ── 12. a Workbench build that lands a desktop ─────────────────────────────
from mastercode.runtime import app_verify as AV  # noqa: E402
from mastercode.runtime import pipeline as PL  # noqa: E402
import server.routes.projects as PRJ  # noqa: E402

proof = {"on": False}


async def _proof(_pool, _eid):
    return proof["on"]


AV.browser_proof_enabled = _proof
proj = TMP / "50"
proj.mkdir()
(proj / "index.html").write_text(doc("GOOD landed"))
(proj / "variant-2.html").write_text(doc("BROKEN landed"))
(proj / "notes.html").write_text("<html><body>not a desktop</body></html>")
PRJ.PROJECTS_ROOT = TMP
import server.routes.applications as APPS  # noqa: E402  (where PROJECTS_ROOT lives)
APPS.PROJECTS_ROOT = TMP
check("browser proof OFF: a landed desktop is not loaded (no unattended wake)",
      run(DR.landed_tree_verdict(POOL, 3, 50)) is None)
proof["on"] = True
CALLS.clear()
v = run(DR.landed_tree_verdict(POOL, 3, 50))
check("browser proof ON: every landed desktop document is loaded, and only those",
      v and [x["name"] for x in v["results"]] == ["index.html", "variant-2.html"]
      and len(CALLS) == 2, repr(v))
check("...one that does not render fails the verdict, naming the file and error",
      v["ran"] is True and v["ok"] is False
      and "line 3" in [x for x in v["results"] if x["name"] == "variant-2.html"][0]["output_tail"])
pipe = {**PL.default_pipeline(), "check": [*PL.default_pipeline()["check"],
                                          {"provider": DR.PROVIDER, "required": True}]}
acc_ok = PL.harness_check_verdict({"ran": True, "ok": True, "results": []}, True)
# Wherever it lives (project 251 put its designs at themes/<name>/index.html),
# and only what the build delivered: not a dependency, not a dot directory.
(proj / "themes" / "dusk").mkdir(parents=True)
(proj / "themes" / "dusk" / "index.html").write_text(doc("GOOD nested theme"))
(proj / "themes" / "dusk" / "readme.html").write_text("<html><body>notes</body></html>")
(proj / "node_modules" / "pkg").mkdir(parents=True)
(proj / "node_modules" / "pkg" / "demo.html").write_text(doc("BROKEN dependency demo"))
(proj / ".history").mkdir()
(proj / ".history" / "old.html").write_text(doc("BROKEN old revision"))
CALLS.clear()
v2 = run(DR.landed_tree_verdict(POOL, 3, 50))
check("...every landed .html that declares a desktop is checked, at ANY depth — and nothing "
      "from dependencies or dot directories",
      v2 and [x["name"] for x in v2["results"]] == ["index.html", "themes/dusk/index.html",
                                                   "variant-2.html"]
      and len(CALLS) == 3 and not any("dependency" in c or "old revision" in c for c in CALLS),
      repr(v2)[:300])
# No count of desktop documents goes unchecked; only the .html files past the
# SCAN backstop are left, and those are NAMED as not scanned, never silently
# skipped.
real_scan = DR._MAX_SCANNED_FILES
DR._MAX_SCANNED_FILES = 2
v3 = run(DR.landed_tree_verdict(POOL, 3, 50))
check("...and past the scan backstop the unread .html files are NAMED as not scanned",
      len(v3["results"]) <= 2
      and any("not scanned" in r for r in v3.get("not_checked") or []), repr(v3)[:300])
DR._MAX_SCANNED_FILES = real_scan
# A document the scan found and that is gone before its check (removed
# mid-scan): reported by its own name, never swapped for the shallowest
# index.html and checked (and filed) under the removed name.
real_landed = DR.landed_desktop_documents
DR.landed_desktop_documents = lambda root: (["removed-mid-scan.html", "index.html"], [])
CALLS.clear()
v4 = run(DR.landed_tree_verdict(POOL, 3, 50))
DR.landed_desktop_documents = real_landed
check("...a document removed after the scan is named as not checked, and nothing else is "
      "checked in its place",
      [x["name"] for x in v4["results"]] == ["index.html"] and len(CALLS) == 1
      and any(r.startswith("removed-mid-scan.html:") for r in v4.get("not_checked") or []),
      repr(v4)[:300])
_exact_ws = TMP / "exact-ws"
_exact_ws.mkdir()
(_exact_ws / "index.html").write_text(doc("GOOD fallback page"))
try:
    ART.render_workspace(_exact_ws, "gone.html", exact=True)
    _exact = "rendered"
except ART.ArtifactError as e:
    _exact = e.status
check("render_workspace(exact=True): a named document that is not there is 404, no fallback",
      _exact == 404 and "GOOD fallback page" in ART.render_workspace(_exact_ws, "gone.html"),
      repr(_exact))
check("...and every render-check call in the pin asks for the exact document",
      (_ROOT / "server/applications/desktop_pin.py").read_text().count(
          "artifact_service.render(") == 4
      and (_ROOT / "server/applications/desktop_pin.py").read_text().count("exact=True") == 4)
check("...and joins the build's checks: a failing render reads as failed",
      PL.decide(True, pipe, [acc_ok, v])["status"] == PL.FAILED)
unran = {"provider": DR.PROVIDER, "where": "browser_sidecar", "ran": False, "ok": None,
         "results": [], "reason": "browser wake refused"}
check("...a browser that could not be asked holds nothing (not a failure)",
      PL.decide(True, pipe, [acc_ok, unran])["status"] == PL.COMPLETE)
check("...and a render pass alone proves nothing (the acceptance still does)",
      PL.decide(True, pipe, [PL.harness_check_verdict(None, False),
                             {**v, "ok": True}])["status"] == PL.REVIEW)
src = (_ROOT / "brain/mastercode/runtime/tools.py").read_text()
body_ = src[src.index("async def _run_phase_build("):]
body_ = body_[:body_.index("\nasync def ", 10)]
check("the phase build asks for the verdict after the build and before its checks are read",
      body_.index("landed_tree_verdict(") < body_.index("_verdicts.append(_render_verdict)")
      < body_.index("_pipeline.decide(ok, _pipe, _verdicts)"))

# ── 12b. the Host is TOLD: the verdict rides the row, the record, the words ─
import ast as _ast  # noqa: E402
import json as _json2  # noqa: E402

_sess_src = (_ROOT / "brain/mastercode/runtime/session.py").read_text()
_fn = next(n for n in _ast.parse(_sess_src).body
           if isinstance(n, _ast.FunctionDef) and n.name == "_desktop_render_facts")
_ns: dict = {}
exec(compile(_ast.Module(body=[_fn], type_ignores=[]), "session.py", "exec"), _ns)
facts = _ns["_desktop_render_facts"]
t_fail = facts({"desktop_render": v})
check("the delivery STATES a failed render check, naming the file and the error line",
      "Desktop render check: FAILED" in t_fail and "variant-2.html" in t_fail and "line 3" in t_fail,
      t_fail)
t_ok = facts({"desktop_render": {**v, "ok": True,
                                 "results": [{**x, "ok": True} for x in v["results"]]}})
check("...a pass, naming what rendered", t_ok.startswith("Desktop render check: passed")
      and "index.html" in t_ok)
check("...and 'not checked' as not checked, never as working",
      "NOT CHECKED" in facts({"desktop_render": unran}) and facts({}) == "")
build_branch = _sess_src[_sess_src.index("_facts += _acceptance_facts(record)"):][:200]
check("deliver_agent_result's build branch adds the line (one-shot runs and phases alike)",
      "_facts += _desktop_render_facts(record)" in build_branch)

_ad_src = (_ROOT / "brain/mastercode/runtime/agent_dispatch.py").read_text()
_fn2 = next(n for n in _ast.parse(_ad_src).body
            if isinstance(n, _ast.FunctionDef) and n.name == "delivery_record")
_ns2: dict = {"json": _json2}
exec(compile(_ast.Module(body=[_fn2], type_ignores=[]), "agent_dispatch.py", "exec"), _ns2)


class _Row(dict):
    pass


_row = _Row(task_id=1, entity_id=9003, title="t", description="d", task_type="factory_build",
            status="completed", error=None, created_at=None, started_at=None, completed_at=None)
try:
    rec = _ns2["delivery_record"](_row, {"desktop_render": v})
    carried = rec.get("desktop_render") == v
except Exception as e:  # noqa: BLE001
    carried = False
    print("delivery_record raised:", e)
check("the delivery record built from the task row carries the verdict", carried)

from mastercode.runtime import tools as _T  # noqa: E402
_merged: list = []


class _RowPool:
    async def execute(self, sql, *a):
        if "SET status='completed'" in sql:
            _merged.append(_json2.loads(a[0]))
        return "UPDATE 1"


async def _no_usage(_pool, _tid):
    return {}


_T._build_usage_for_task = _no_usage
run(_T._factory_build_task_finish(_RowPool(), 9, {"status": "ok", "result": {
    "summary": "landed", "desktop_render": v}}))
check("the landed build's row keeps the verdict (what the delivery is built from)",
      _merged and _merged[0].get("desktop_render") == v)

# ── 14. re-selecting "Host design" is a wear ───────────────────────────────
(ws(7) / "index.html").write_text(doc("GOOD edited while another wallpaper showed"))
out = run(PIN.wear_current(POOL, 3, 7, "index.html"))
check("choosing Host design checks the CURRENT artifact and wears it on a pass",
      out.get("pinned") is True and "GOOD edited while" in worn(), repr(out))
(ws(7) / "index.html").write_text(doc("BROKEN edited while away"))
out = run(PIN.wear_current(POOL, 3, 7, "index.html"))
check("...a failing current version keeps the last good one on screen, and says why",
      out.get("pinned") is False and "last version that rendered" in out.get("message", "")
      and "line 3" in out["message"] and "GOOD edited while" in worn(), repr(out)[:200])
(ws(7) / "index.html").write_text(doc("PHONEOFF edited while away"))
out = run(PIN.wear_current(POOL, 3, 7, "index.html"))
check("choosing Host design for a design whose apps run off a phone: worn, and the person is "
      "told in their words (Settings shows desk.warning)",
      out.get("pinned") is True and "PHONEOFF" in worn()
      and "On a phone, 8 of this design's apps sit off the side of the screen" in (out.get("warning") or "")
      and "CSeriesDesktop" not in (out.get("warning") or ""), repr(out)[:300])
r = write(7, "PHONEOFF Host edit", desktop_wait_s=5)
d = r.get("desktop") or {}
check("...a Host write to the worn desktop advances, and the Host reads the phone warning in "
      "desktop.warning", d.get("advanced") is True
      and "past the screen's side edges" in (d.get("warning") or ""), repr(d)[:300])
out = run(PIN.wear_current(POOL, 3, 7, "index.html"))
check("...choosing Host design again with those bytes still pinned (not re-checked) still tells "
      "the person, naming the button a phone actually shows",
      out.get("current") is True
      and "On a phone, 8 of this design's apps sit off the side of the screen" in (out.get("warning") or "")
      and "logo button at the left end of the taskbar" in (out.get("warning") or "")
      and "Apps button" not in (out.get("warning") or ""), repr(out)[:300])
(ws(7) / "index.html").write_text(doc("GOOD back again"))
run(PIN.wear_current(POOL, 3, 7, "index.html"))
out = run(PIN.wear_current(POOL, 3, 7, "index.html"))
check("...and once a version with every app on the phone screen is pinned, it says nothing",
      out.get("current") is True and "warning" not in out, repr(out)[:300])
ws(11)
(ws(11) / "index.html").write_text(doc("BROKEN never good"))
try:
    run(PIN.wear_current(POOL, 3, 11, "index.html"))
    refused = None
except ART.ArtifactError as e:
    refused = e.status
check("...and with no version that ever rendered it is refused like any wear (422)", refused == 422)
(ws(7) / "index.html").write_text(doc("GOOD via the Settings route"))


class _Req:
    def __init__(self, body):
        self._b = body

    async def json(self):
        return self._b


res = run(A.set_appearance(_Req({"wallpaper": "html", "variant": "desktop"})))
check("PUT /api/appearance {wallpaper: html} runs the wear and reports it",
      isinstance(res, dict) and res.get("desktop", {}).get("pinned") is True
      and "GOOD via the Settings route" in worn(), repr(res)[:200])
CALLS.clear()
res = run(A.set_appearance(_Req({"wallpaper": "default", "variant": "desktop"})))
check("...any other swatch checks nothing", CALLS == [] and "desktop" not in res)

# ── 13. the browser lingers between checks, and yields to a full plan ──────
from server.applications import harness_backend as HB  # noqa: E402

released: list[str] = []


async def _bill(app_id, harness_id):
    released.append(app_id)


HB._release_and_bill = _bill
HB._state.clear()
HB._state["browser"] = {"harness": "browser", "url": "http://b:8830", "ws": 0, "runs": 0,
                        "release_task": None, "lease_task": None}


async def _linger(idle):
    HB.linger("browser", "browser", idle)     # called from a running loop, as in the kernel


run(_linger(0.2))
check("after a render check the browser LINGERS instead of releasing at once",
      HB.lingering("browser") and released == [])
run(asyncio.sleep(0.35))
check("...and is released when the idle window ends", released == ["browser"]
      and HB._state["browser"]["url"] is None and not HB.lingering("browser"))

released.clear()
HB._state["browser"].update(url="http://b:8830", ws=1)
run(_linger(0.1))
check("a browser with a person's live session is never 'lingering'", not HB.lingering("browser"))
run(asyncio.sleep(0.2))
check("...and the window's end does not release it under them", released == [])
HB._state["browser"].update(ws=0, url="http://b:8830")

# A wake the PLAN refuses frees the linger, and is asked once more.
run(_linger(60))
wakes: list[str] = []


async def _wake(harness_id, creds=None):
    wakes.append(harness_id)
    if len(wakes) == 1:
        return {"ok": False, "error": 'operator /harness/wake 402: {"detail":{"error":'
                '"capability_over_plan","needs_mi":512,"headroom_mi":0}}'}
    return {"ok": True, "url": "http://harness-claude-code:8832"}


async def _no_token(*_a, **_k):
    raise RuntimeError("no pool in this gate")


HB._wake = _wake
HB.egress_subject_token = _no_token
up = run(HB.ensure_awake("claude-code", "claude-code"))
check("a coding engine refused for room releases the lingering browser and retries",
      up and released == ["browser"] and wakes == ["claude-code", "claude-code"], f"{up} {released} {wakes}")

# The render check asks for the linger; the delivery proof keeps releasing at once.
src_av = (_ROOT / "brain/mastercode/runtime/app_verify.py").read_text()
check("the shared sidecar call lingers only when its caller asks (render check), "
      "and takes over a linger it finds",
      "_hb.linger(_BROWSER_APP_ID, _BROWSER_APP_ID, linger_s)" in src_av
      and "and not _hb.lingering(_BROWSER_APP_ID)" in src_av
      # A door's check lingers by default; the background re-check passes 0
      # (scripts/test_desktop_recheck_backoff.py proves that path).
      and __import__("inspect").signature(DR.render_check).parameters["linger_s"].default
      == DR.LINGER_S)
check("the linger is the measured in-session edit gap (240 s)", DR.LINGER_S == 240.0)

# ── 15. the race: refused, then "no room" — BROKEN is never worn ─────────────
WORN["id"] = 20
ws(20)
write(20, "GOOD r1", desktop_wait_s=5)
PIN.write_pin(20, "index.html", ART.render_workspace(ws(20), "index.html"),
              {"checked": True, "ok": True}, origin="set")
run(PIN.reconcile(POOL, 3, 20, "index.html"))          # the shell polls while GOOD is worn
r = write(20, "BROKEN r2", desktop_wait_s=5)
check("RACE: the broken edit is refused while the browser answers",
      (r.get("desktop") or {}).get("advanced") is False and "GOOD r1" in worn(20))
BROWSER["up"] = False                                    # the next wake is refused: plan full
CALLS.clear()
run(PIN.reconcile(POOL, 3, 20, "index.html"))           # the shell's next 3-second poll
run(asyncio.sleep(0.2))
check("...then with no browser, the shell's next poll does NOT wear it (not checked never "
      "replaces a failure)", "GOOD r1" in worn(20) and "BROKEN" not in worn(20), worn(20)[-60:])
check("...and reconcile never asked the browser about bytes already known to fail", CALLS == [])
st = PIN.status(20) or {}
check("...the hold is SURFACED: a status naming the error and what is on screen",
      st.get("state") == "held" and "line 3" in st.get("message", "")
      and "NOT on the desktop" in st.get("message", ""), repr(st))
broken = ART.render_workspace(ws(20), "index.html")
rc = run(DR.render_check(POOL, broken))
check("...asked directly with no browser, those bytes are still FAILED, and say they stay "
      "refused until a browser passes them",
      rc.get("checked") is True and rc.get("ok") is False and rc.get("remembered")
      and "stays refused until a browser passes it" in DR.format_render(rc)
      and "Publish again once a browser can be started" not in DR.format_render(rc), repr(rc)[:200])
try:
    run(PIN.wear_unchecked(POOL, 3, 20, "index.html", broken, origin="restore"))
    refused = None
except ART.ArtifactError as e:
    refused = e.status
check("...Go back to a pinless desktop holding those bytes is refused too (422)", refused == 422)
r = write(20, "BROKEN r2 again", desktop_wait_s=5)       # new bytes, still no browser
check("...NEW bytes with no browser advance — worn NOT RENDER-CHECKED, and say so",
      (r.get("desktop") or {}).get("advanced") is True
      and "NOT RENDER-CHECKED" in (r["desktop"].get("warning") or ""), repr(r.get("desktop"))[:200])
st = PIN.status(20) or {}
check("...a not-checked advance is SURFACED as such", st.get("state") == "unchecked"
      and "WITHOUT a render check" in st.get("message", ""), repr(st))

# ── 15b. the same race when render_check itself is replaced wholesale ──────
# A race that stubs render_check entirely passes no verdict through its
# memory: the pin must record the failure it acts on itself.
_real_rc = DR.render_check


async def raw_check(pool, html, **_kw):
    if not BROWSER["up"]:
        return {"checked": False, "reason": "the plan has no room for the browser right now"}
    if "BROKEN" in html:
        return {"checked": True, "ok": False, "why": "boom", "failures": ["an uncaught error at line 3"]}
    return {"checked": True, "ok": True, "why": "ok", "failures": []}


DR.render_check = raw_check
BROWSER["up"] = True
WORN["id"] = 23
ws(23)
write(23, "GOOD w1", desktop_wait_s=5)
PIN.write_pin(23, "index.html", ART.render_workspace(ws(23), "index.html"),
              {"checked": True, "ok": True}, origin="set")
run(PIN.reconcile(POOL, 3, 23, "index.html"))
write(23, "BROKEN w2 (raw stub)", desktop_wait_s=5)
BROWSER["up"] = False
run(PIN.reconcile(POOL, 3, 23, "index.html"))
run(asyncio.sleep(0.2))
check("RACE with render_check replaced wholesale: BROKEN is still never worn — the pin "
      "recorded the failure it acted on", "GOOD w1" in worn(23) and "BROKEN" not in worn(23),
      worn(23)[-60:])
DR.render_check = _real_rc
BROWSER["up"] = False
WORN["id"] = 20

# ── 16. a version worn without a check is asked about again, and comes off ──
BROWSER["up"] = True
real_recheck = PIN.RECHECK_S
PIN.RECHECK_S = 0.0
CALLS.clear()
run(PIN.reconcile(POOL, 3, 20, "index.html"))
run(asyncio.sleep(0.2))
check("once a browser answers, the shell's poll re-asks about the unchecked version",
      len(CALLS) == 1 and "BROKEN r2 again" in CALLS[0])
check("...it fails, so it is TAKEN OFF: the last version that passed is worn again",
      "GOOD r1" in worn(20) and "BROKEN" not in worn(20), worn(20)[-60:])
st = PIN.status(20) or {}
check("...and that is surfaced too", st.get("state") == "held" and "taken off" in st.get("message", ""),
      repr(st))
PIN.RECHECK_S = real_recheck

# ── 17. what the shell and the Host are told ───────────────────────────────


async def _pointer20(_pool):
    return {"artifact_id": 20, "entry": "index.html", "updated_at": "x"}


A._current_pointer = _pointer20
from starlette.responses import Response  # noqa: E402
meta = run(A.desktop_html_meta(Response()))
check("GET /desktop/meta carries the pin's status for the shell's notice",
      (meta.get("pin") or {}).get("state") == "held" and "taken off" in meta["pin"]["message"],
      repr(meta)[:200])
shell = (_ROOT / "dashboard/base/js/desktop-surface.js").read_text()
check("...and the shell shows it as a notice once per change (pollTick → pinNotice)",
      "pinNotice(m && m.pin);" in shell and "wm.notify('Desktop'" in shell
      and "PIN_NOTICE_KEY" in shell)
from mastercode.runtime import prompt_schema as PS  # noqa: E402


class _RtPool:
    async def fetchrow(self, sql, *a):
        return {"desktop_artifact_id": 20, "desktop_entry": "index.html", "wallpaper": "html",
                "artifact_name": "Halloween", "artifact_updated_at": None}


line = run(PS._b_appearance(types.SimpleNamespace(pool=_RtPool()),
                            types.SimpleNamespace(entity_id=9003), ""))
check("the Host's per-turn appearance line says what is ON SCREEN (held, and why)",
      "On screen (desktop status, held)" in (line or "") and "taken off" in line, repr(line)[:300])

# ── 18. a forged or damaged pin is never served ────────────────────────────
check("the pin lives OUTSIDE the artifact's workspace, where no project writer reaches",
      ws(20).resolve() not in PIN._dir(20).resolve().parents
      and TMP.resolve() in PIN._dir(20).resolve().parents)
write(20, "GOOD r3", desktop_wait_s=5)
check("(a passing edit is worn)", "GOOD r3" in worn(20))
cur = PIN.read_pin(20)
(PIN._dir(20) / f"{cur['digest']}.html").write_text(doc("FORGED unchecked"))
served = worn(20)
check("bytes that do not match their record are NOT served — the last verifiable version is",
      "FORGED" not in served and "GOOD r1" in served, served[-60:])
st = PIN.status(20) or {}
check("...and that is surfaced", st.get("state") == "fallback" and "did not match" in st.get("message", ""),
      repr(st))

# ── 19. a desktop worn before checks existed, whose bytes fail ─────────────
WORN["id"] = 21
ws(21)
(ws(21) / "index.html").write_text(doc("BROKEN legacy"))
served = worn(21)                       # worn as it stood, and checked at once
run(asyncio.sleep(0.2))
try:
    worn(21)
    gone = None
except ART.ArtifactError as e:
    gone = e.status
check("a pre-check desktop whose bytes fail is taken off once the check answers "
      "(nothing shown: 404, the default wallpaper)", "BROKEN legacy" in served and gone == 404)
check("...and says so", (PIN.status(21) or {}).get("state") == "none")
WORN["id"] = 22
ws(22)
(ws(22) / "index.html").write_text(doc("BROKEN r2"))     # bytes already known to fail
CALLS.clear()
try:
    worn(22)
    gone = None
except ART.ArtifactError as e:
    gone = e.status
check("a pre-check desktop holding bytes already known to fail is never worn, not even once",
      gone == 404 and CALLS == [])
WORN["id"] = 7

# ── 18. an unchecked worn version re-checked with no browser changes NOTHING ─
# The shell remounts on a new pin stamp and shows a notice on a new status:
# asking again every RECHECK_S and getting "not checked" again must write
# neither.
WORN["id"] = 30
ws(30)
BROWSER["up"] = False
write(30, "GOOD q1", desktop_wait_s=5)                 # worn WITHOUT a check
pin0, st0 = PIN.read_pin(30), PIN.status(30)
check("an unchecked version is worn and says so",
      pin0 is not None and pin0["checked"] is False and (st0 or {}).get("state") == "unchecked")
real_recheck = PIN.RECHECK_S
PIN.RECHECK_S = 0.0
CALLS.clear()
for _ in range(3):
    run(PIN.reconcile(POOL, 3, 30, "index.html"))
    run(asyncio.sleep(0.2))
pin1, st1 = PIN.read_pin(30), PIN.status(30)
check("three no-browser re-checks of the same worn digest really asked the browser",
      len(CALLS) == 3, repr(len(CALLS)))
check("...and left the remount stamp (pin 'at') unchanged",
      pin1["at"] == pin0["at"] and pin1["digest"] == pin0["digest"], f"{pin0['at']} -> {pin1['at']}")
check("...and the notice (status 'at' and message) unchanged",
      st1 == st0, f"{st0} -> {st1}")
BROWSER["up"] = True
run(PIN.reconcile(POOL, 3, 30, "index.html"))
run(asyncio.sleep(0.2))
pin2, st2 = PIN.read_pin(30), PIN.status(30)
check("...then a pass DOES change them: a new stamp, checked, state current",
      pin2["at"] != pin0["at"] and pin2["checked"] is True and (st2 or {}).get("state") == "current",
      repr((pin2, st2)))
PIN.RECHECK_S = real_recheck

# ── 19. deleting an artifact removes its pins ───────────────────────────────
pins30 = DR.state_dir() / "pins" / "30"
outside = TMP / "outside-30"
outside.mkdir()
(outside / "keep.txt").write_text("not ours")
(pins30 / "link").symlink_to(outside)
check("an artifact's pins exist before it is deleted", pins30.is_dir() and any(pins30.glob("*.html")))
(DR.state_dir() / "pins" / "31").mkdir(parents=True, exist_ok=True)
run(ART.delete_artifact(POOL, 3, 30))
check("delete_artifact removes that artifact's pins dir", not pins30.exists())
check("...follows no link out of it", (outside / "keep.txt").read_text() == "not ours")
check("...and leaves another artifact's pins alone", (DR.state_dir() / "pins" / "31").is_dir())
WORN["id"] = 7

# ── 20. a check still in flight when its artifact is deleted pins nothing ───
WORN["id"] = 32
ws(32)
write(32, "GOOD d1", desktop_wait_s=5)
PIN.write_pin(32, "index.html", ART.render_workspace(ws(32), "index.html"),
              {"checked": True, "ok": True}, origin="set")
BROWSER["up"] = True
r = write(32, "SLOW d2", desktop_wait_s=0)               # the check is still running
check("the check is in flight when the delete comes", (r.get("desktop") or {}).get("pending") is True,
      repr(r.get("desktop"))[:160])
run(ART.delete_artifact(POOL, 3, 32))
run(asyncio.sleep(0.8))                                   # the check lands
check("a check that lands after its artifact was deleted does not recreate its pins dir",
      not (DR.state_dir() / "pins" / "32").exists())
WORN["id"] = 7

# ── 21. the shell saw the worn desktop reload itself: it comes off ──────────
WORN["id"] = 33
ws(33)
BROWSER["up"] = True
write(33, "GOOD loop1", desktop_wait_s=5)
PIN.write_pin(33, "index.html", ART.render_workspace(ws(33), "index.html"),
              {"checked": True, "ok": True}, origin="set")
write(33, "GOOD loop2", desktop_wait_s=5)             # passes its check: advances
loop2 = ART.render_workspace(ws(33), "index.html")
check("the second version is worn", "GOOD loop2" in worn(33))
stamp = PIN.read_pin(33)["at"]
check("a report about a version just worn is too early: nothing changes (a forged report "
      "cannot walk the history back faster than LOOP_MIN_WORN_S)",
      PIN.take_off_reloading(33, "index.html", stamp) is False and "GOOD loop2" in worn(33)
      and PIN.LOOP_MIN_WORN_S >= 10.0)
_real_min = PIN.LOOP_MIN_WORN_S
PIN.LOOP_MIN_WORN_S = 0.0                                  # as if it had been worn for a while
check("a report naming a stale stamp changes nothing",
      PIN.take_off_reloading(33, "index.html", "1999-01-01T00:00:00+00:00") is False
      and "GOOD loop2" in worn(33))
check("...and so does one naming no stamp", PIN.take_off_reloading(33, "index.html", None) is False)


class _Req:
    def __init__(self, body):
        self._b = body

    async def json(self):
        return self._b


async def _ptr33(_pool):
    return {"artifact_id": 33, "entry": "index.html", "updated_at": stamp}


A._current_pointer = _ptr33
out = run(A.desktop_reloads_itself(_Req({"updated_at": stamp})))
check("the shell's report (POST /desktop/loop) takes the looping version off", out == {"taken_off": True})
check("...the version before it is worn again", "GOOD loop1" in worn(33) and "GOOD loop2" not in worn(33))
st = PIN.status(33) or {}
check("...with a notice that it came off on a reload report, unconfirmed", st.get("state") == "held"
      and "taken off" in st.get("message", "") and "reloading itself over and over" in st.get("message", "")
      and "unconfirmed" in st.get("message", ""), repr(st))
# The report is the shell's word alone (any page framed on the desktop's
# origin can send it), so it is not a failed verdict: a failed verdict would
# refuse every later wear no browser can check, as "reloads itself over and
# over", with nothing that saw the loop.
check("...and the looping bytes are NOT remembered as failed",
      DR.known_failure(loop2) is None, repr(DR.known_failure(loop2)))
check("...they are recorded as taken off after a reload report (unconfirmed)",
      bool((DR.reload_report(loop2) or {}).get("at")), repr(DR.reload_report(loop2)))
CALLS.clear()
run(PIN.reconcile(POOL, 3, 33, "index.html"))
run(asyncio.sleep(0.2))
check("...so the shell's reconcile never asks the browser about them again, nor wears them",
      CALLS == [] and "GOOD loop2" not in worn(33))
check("...and its notice still says why they are not on screen",
      "reload report (unconfirmed" in (PIN.status(33) or {}).get("message", ""), repr(PIN.status(33)))
check("a second report for the same (now stale) stamp does nothing",
      run(A.desktop_reloads_itself(_Req({"updated_at": stamp}))) == {"taken_off": False})
# Wearing it again (Settings → "Host design" here; set-desktop runs the same
# render_check, scripts/test_desktop_render_doors.py) runs the normal check.
# No browser: it is worn, with a note naming the unconfirmed report — never
# refused on the report alone.


def _on_screen(aid):
    """What is worn, or "" when nothing is (worn_html's 404), so a mutated
    run reports each check below instead of stopping at the first."""
    try:
        return worn(aid)
    except ART.ArtifactError:
        return ""


BROWSER["up"] = False
CALLS.clear()
out = run(PIN.wear_current(POOL, 3, 33, "index.html"))
check("wearing it again with no browser: worn, not refused",
      out.get("pinned") is True and "GOOD loop2" in _on_screen(33), repr(out)[:300])
check("...with a note that it was taken off after an unconfirmed reload report",
      "NOT RENDER-CHECKED" in (out.get("warning") or "")
      and "reload report (unconfirmed" in out.get("warning", "")
      and (out.get("render") or {}).get("reload_report", {}).get("unconfirmed") is True,
      repr(out)[:400])
check("...which the screen's notice carries too",
      (PIN.status(33) or {}).get("state") == "unchecked"
      and "reload report (unconfirmed" in (PIN.status(33) or {}).get("message", ""),
      repr(PIN.status(33)))
check("...and once worn, the report is history (a new loop is reported afresh)",
      DR.reload_report(loop2) is None)
# Reported again, then worn with a browser: the browser's own verdict decides.
check("the shell reports it again: off again",
      PIN.take_off_reloading(33, "index.html", (PIN.read_pin(33) or {}).get("at")) is True
      and "GOOD loop1" in _on_screen(33) and DR.reload_report(loop2) is not None)
BROWSER["up"] = True
CALLS.clear()
out = run(PIN.wear_current(POOL, 3, 33, "index.html"))
check("wearing it again with a browser runs the normal check, and a pass wears it",
      CALLS == [loop2] and out.get("pinned") is True
      and (out.get("render") or {}).get("checked") is True and "GOOD loop2" in _on_screen(33),
      repr(out)[:300])
# nothing before it: the default wallpaper
WORN["id"] = 35
ws(35)
write(35, "ONLY loop", desktop_wait_s=5)
PIN.write_pin(35, "index.html", ART.render_workspace(ws(35), "index.html"),
              {"checked": True, "ok": True}, origin="set")
st35 = PIN.read_pin(35)["at"]
check("with no earlier version, the desktop comes off entirely",
      PIN.take_off_reloading(35, "index.html", st35) is True and (PIN.status(35) or {}).get("state") == "none"
      and PIN.read_pin(35) is None)
PIN.LOOP_MIN_WORN_S = _real_min
WORN["id"] = 7

# ── 22. a deleted id that is worn again is alive again ─────────────────────
WORN["id"] = 34
ws(34)
write(34, "GOOD again1", desktop_wait_s=5)
PIN.write_pin(34, "index.html", ART.render_workspace(ws(34), "index.html"),
              {"checked": True, "ok": True}, origin="set")
run(ART.delete_artifact(POOL, 3, 34))
check("a deleted artifact is recorded as deleted", 34 in PIN._deleted)
ws(34)
PIN.write_pin(34, "index.html", doc("GOOD again2"), {"checked": True, "ok": True}, origin="set")
check("wearing the same id again clears the record", 34 not in PIN._deleted)
write(34, "GOOD again3", desktop_wait_s=5)
check("...and a check on the re-created id lands and pins (not dropped as 'deleted')",
      "GOOD again3" in worn(34), worn(34)[-60:])
WORN["id"] = 7

# ── 23. the pre-edit baseline reads only a real revision folder ────────────
# _baseline_from_history (a worn desktop with no pin yet keeps the render from
# before the write) takes the revision id from the log, a file in the
# workspace. It checks it as render_history does: revision-shaped (_REV_RE),
# and its folder reached with no link (open_tree_no_escape within
# .history/<rev>). Each case below is caught by exactly one of the two.
import json as _json3  # noqa: E402

ws(36)
(ws(36) / "index.html").write_text(doc("GOOD current 36"))
H36 = ws(36) / ".history"
(H36 / "0001").mkdir(parents=True)
(H36 / "0001" / "index.html").write_text(doc("GOOD rev one"))


def _log36(*revs):
    (H36 / "log.jsonl").write_text("".join(_json3.dumps({"rev": r}) + "\n" for r in revs))


_log36("0001", "0002")
check("the baseline is the revision before the newest (the control: a real one is read)",
      "GOOD rev one" in (PIN._baseline_from_history(36, "index.html") or ""))
(H36 / "latest").mkdir()
(H36 / "latest" / "index.html").write_text(doc("FORGED latest"))
_log36("latest", "0002")
check("a bad rev (a real folder in .history, but not a revision id) is not read",
      PIN._baseline_from_history(36, "index.html") is None)
ws(37)
(ws(37) / "index.html").write_text(doc("ANOTHER artifact"))
_log36("../../37", "0002")
check("...nor one that climbs out of .history (another artifact's folder)",
      PIN._baseline_from_history(36, "index.html") is None)
_out36 = TMP / "outside-36"
_out36.mkdir()
(_out36 / "index.html").write_text(doc("OUTSIDE linked revision"))
os.symlink(_out36, H36 / "0003")
_log36("0003", "0004")
check("a linked revision folder (.history/0003 -> a folder outside) is not read",
      PIN._baseline_from_history(36, "index.html") is None)
ws(38)
(ws(38) / "index.html").write_text(doc("GOOD current 38"))
_out38 = TMP / "outside-38"
(_out38 / "0001").mkdir(parents=True)
(_out38 / "0001" / "index.html").write_text(doc("OUTSIDE linked history"))
(_out38 / "log.jsonl").write_text('{"rev": "0001"}\n{"rev": "0002"}\n')
os.symlink(_out38, ws(38) / ".history")
check("...nor a linked .history (it has no history at all)",
      PIN._baseline_from_history(38, "index.html") is None)

LOOP.close()
print()
if _FAILURES:
    print(f"[worn-pin] {len(_FAILURES)} FAILED")
    sys.exit(1)
print("[worn-pin] ALL OK")
