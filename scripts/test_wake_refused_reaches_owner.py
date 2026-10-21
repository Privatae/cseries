#!/usr/bin/env python3
"""A wake this machine refuses reaches the person and the Host with its reason.

The local runtime refuses a wake on a short disk with 507, naming the floor
and each app's data, largest first (cseries_runtime.guard.Guard.disk_text),
and one it has no room for with 402 (over_plan_text, scope machine) or 429.
The Host's lanes used to keep only a 402's numbers, for the harness lane
alone: an app or the browser that did not open said nothing, and the Host
could not say why either.

Against a fake runtime on a real unix socket, answering with the runtime's own
refusal text:
  * the app lane (ensure_awake_app) and the harness lane (ensure_awake) keep
    the refusal, and harness_backend.wake_refused gives its status and words,
    one line; a wake that lands clears it; "still starting" and an operator's
    relayed body are not refusals for a person;
  * the desktop: every request to the app answers with the refusal's status
    and words (proxy._not_awake) instead of 503 "starting"; a pod app's launch
    raises WakeRefused (supervisor._pod_open), which the launch route answers
    with {error: wake_refused, message}; the desktop shell shows that message
    as one line of text and opens no window (app-runner.js, in node);
  * the Host's tool results: an app start (app_verify._start) and a browser
    call (browser_sidecar_post) say what the machine said, and a refused start
    is the platform's, not a failure of the app; an app's MCP tool or file
    that did not wake names it (wake_refused_words);
  * a browser agent's run (browser_agents._execute) the machine refused ends
    at once with the refusal's words in its record: no call to the browser,
    no second cold wake, never "unreachable";
  * the Workbench's coding runs: the runtime's capacity answer and its 402
    (scope machine) say "this machine", a plan's still says "this plan"
    (routes/review.py);
  * a helper app's launch, after a refusal, asks the wake once more and
    answers the refusal instead of opening its window; with none on record it
    wakes nothing (routes/applications.launch_app).

Container-free: python3 scripts/test_wake_refused_reaches_owner.py
"""
import ast
import asyncio
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import types

ROOT = pathlib.Path(os.environ.get("APP_ROOT") or pathlib.Path(__file__).resolve().parent.parent)
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "brain"))
sys.path.insert(0, str(ROOT / "vm/runtime/lib"))

FAILURES = []


def check(cond, name, detail=""):
    if cond:
        print(f"OK    {name}")
    else:
        print(f"FAIL  {name} {detail}")
        FAILURES.append(name)


fake_main = types.ModuleType("server.main")
fake_main.get_bridge_client = lambda: None
fake_main._ws_connections = set()
sys.modules["server.main"] = fake_main

from server import brain_service  # noqa: E402
from server.applications import harness_backend as hb  # noqa: E402
from server.applications import local_runtime as lr  # noqa: E402
from server.applications import proxy  # noqa: E402
from server.applications import supervisor  # noqa: E402
from cseries_runtime.guard import DISK_WAKE_FLOOR, Guard, over_plan_text  # noqa: E402


class FakeRuntime:
    def __init__(self, path):
        self.path = path
        self.answer = lambda op, params: {"ok": True, "data": {}}
        self.server = None

    async def _conn(self, reader, writer):
        msg = json.loads(await reader.readline())
        ans = self.answer(msg["op"], msg.get("params") or {})
        writer.write(json.dumps(ans).encode() + b"\n")
        await writer.drain()
        writer.close()

    async def start(self):
        self.server = await asyncio.start_unix_server(self._conn, path=self.path)

    async def stop(self):
        self.server.close()
        await self.server.wait_closed()


async def fake_egress(pool, subject):
    return "e" * 64


hb.egress_subject_token = fake_egress
brain_service._pool = object()

# The runtime's own words for a short disk: what a wake gets from it.
DATA = [{"app_id": "weather", "bytes": 3 << 30, "complete": True, "installed": True},
        {"app_id": "notes", "bytes": 40 << 20, "complete": True, "installed": False}]
DISK_TEXT = Guard.disk_text(DISK_WAKE_FLOOR // 4, DATA)


def refuse(code, text):
    return lambda op, p: ({"ok": False, "code": code, "error": text} if op == "harness.wake"
                          else {"ok": True, "data": {}})


def fresh():
    hb._state.clear()
    hb._wake_locks.clear()


def refused(app_id):
    """harness_backend.wake_refused, or None on a tree without it (so every
    check below runs, and fails, there)."""
    fn = getattr(hb, "wake_refused", None)
    return fn(app_id) if fn else None


def not_awake(app_id, starting):
    fn = getattr(proxy, "_not_awake", None)
    if fn is None:
        return types.SimpleNamespace(status_code=None, detail=None)
    return fn(app_id, starting)


APP_RUNNER = ROOT / "dashboard/base/js/app-runner.js"
NODE_HARNESS = r"""
const fs = require('fs'), vm = require('vm');
const src = fs.readFileSync(process.argv[2], 'utf8');
const detail = JSON.parse(process.argv[3]);
const status = Number(process.argv[4]);
const log = { created: [], notes: [] };
const WindowManager = {
  create(opts) { log.created.push(opts.id); return opts.id; },
  getAll() { return []; }, focus() {}, restore() {}, minimize() {}, close() {},
  notify(title, body, level) { log.notes.push({ title, body, level }); },
};
async function fetchStub(url) {
  if (url.endsWith('/launch')) {
    return { ok: false, status, json: async () => ({ detail }) };
  }
  return { ok: true, json: async () => ({ id: 'weather', name: 'Weather', window: {}, isolated: false }) };
}
const ctx = {
  console, URL, Promise, Map, Set, JSON, Date, Math, String, Object, Array, Error,
  fetch: fetchStub, setTimeout, clearTimeout, setInterval: () => 1, clearInterval: () => {},
  document: { querySelector: () => null, createElement: () => ({ style: {}, dataset: {}, addEventListener() {}, setAttribute() {} }) },
};
ctx.window = ctx;
ctx.window.location = { origin: 'https://h.example', href: 'https://h.example/', protocol: 'https:', host: 'h.example' };
ctx.window.addEventListener = () => {};
ctx.window.WindowManager = WindowManager;
console.warn = () => {};
vm.createContext(ctx);
vm.runInContext(src, ctx);
(async () => {
  await ctx.window.AppRunner.launch('weather');
  process.stdout.write(JSON.stringify(log) + '\n');
})();
"""


def run_app_runner(detail, status):
    d = pathlib.Path(tempfile.mkdtemp(prefix="wake-refused-", dir=os.environ.get("TMPDIR")))
    try:
        h = d / "h.js"
        h.write_text(NODE_HARNESS)
        r = subprocess.run(["node", str(h), str(APP_RUNNER), json.dumps(detail), str(status)],
                           capture_output=True, text=True, timeout=60)
        if r.returncode != 0:
            raise SystemExit(f"node failed: {r.stderr[-800:]}")
        return json.loads(r.stdout.strip().splitlines()[-1])
    finally:
        shutil.rmtree(d, ignore_errors=True)


def launch_route_arm():
    """The launch route's arms around open_window: (handler names in order,
    the first arm's HTTPException status expression, its detail keys)."""
    src = (ROOT / "server/routes/applications.py").read_text()
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "launch_app":
            for t in ast.walk(node):
                if isinstance(t, ast.Try) and "open_window" in ast.unparse(t.body[0]):
                    names = [ast.unparse(h.type) for h in t.handlers]
                    raise_ = next(n for n in ast.walk(t.handlers[0]) if isinstance(n, ast.Raise))
                    call = raise_.exc
                    keys = ([k.value for k in call.args[1].keys]
                            if len(call.args) > 1 and isinstance(call.args[1], ast.Dict) else [])
                    return names, ast.unparse(call.args[0]), keys
    return None, None, None


def counting(code, text, wakes, lands=False):
    """The runtime's answer, counting each wake it is asked for."""
    def answer(op, p):
        if op == "harness.wake":
            wakes.append(p.get("harness") or p.get("app_rt"))
            if lands:
                return {"ok": True, "data": {"url": "http://10.89.78.20:8000", "ready": True}}
            return {"ok": False, "code": code, "error": text}
        return {"ok": True, "data": {}}
    return answer


async def browser_agent_checks(rt, tmp):
    """A browser agent's run on a machine that refuses the browser's wake ends
    at once with the refusal's words, in the run's own record: no call to a
    browser that is not there, no second (cold) wake, never "unreachable"."""
    from server import browser_agents as ba
    ba.STATE_ROOT = pathlib.Path(tmp) / "state"
    spec = {"name": "ledger", "site": "bank.example", "steps": [{"go": "https://bank.example/"}]}
    calls = []

    async def no_browser(method, path, payload=None, timeout=45.0):
        # What _call says when nothing answers at the browser's address.
        calls.append((method, path))
        raise ba.AgentError("could not reach the browser: ClientConnectorError")

    real_call = ba._call
    ba._call = no_browser
    try:
        fresh()
        wakes = []
        rt.answer = counting(507, DISK_TEXT, wakes)
        r = ba._Run("ledger", "schedule")
        await ba._execute(r, spec, pool=None, entity_id=1, actor="host")
        check(r.status == "failed" and r.error == "the browser was not started: " + DISK_TEXT,
              "a browser agent's run the machine refused ends with the refusal's words",
              (r.status, r.error))
        check(calls == [] and wakes == ["browser"],
              "...at once: no call to the browser and no cold wake after it", (calls, wakes))
        runs = sorted((ba.STATE_ROOT / "agents" / "ledger" / "runs").glob("*.json"))
        rec = json.loads(runs[-1].read_text()) if runs else {}
        check(rec.get("status") == "failed"
              and rec.get("error") == "the browser was not started: " + DISK_TEXT,
              "...and the run record says so", rec.get("error"))

        # A cached address the sweep already scaled to zero: the run wakes it
        # cold, once, and that wake is refused.
        fresh()
        wakes, calls[:] = [], []
        hb._st("browser", "browser")["url"] = "http://10.89.78.9:8830"
        rt.answer = counting(402, over_plan_text(1024, 300), wakes)
        r = ba._Run("ledger", "host")
        await ba._execute(r, spec, pool=None, entity_id=1, actor="host")
        check(r.status == "failed" and r.error == "the browser was not started: this machine has "
              "no room for browser right now: it needs 1024 MiB and 300 MiB are free",
              "a refused cold wake after a dead cached address ends with the refusal, not "
              "unreachable", r.error)
        check(len(calls) == 1 and wakes == ["browser"],
              "...after one call to the dead address and one cold wake", (calls, wakes))

        # A wake that failed for any other reason is not a refusal: the run
        # still finds out from its first call, as before.
        fresh()
        wakes, calls[:] = [], []
        rt.answer = counting(504, "still starting", wakes)
        r = ba._Run("ledger", "host")
        await ba._execute(r, spec, pool=None, entity_id=1, actor="host")
        check(calls and r.error.startswith("could not reach the browser"),
              "a wake that is still starting is not ended as a refusal", (calls, r.error))
    finally:
        ba._call = real_call


async def review_checks(rt):
    """The Workbench's coding runs: a refusal from this machine's own runtime
    (scope machine) says "this machine", a plan's still says "this plan"."""
    import types as _t
    from cseries_runtime import argv as A
    from cseries_runtime.server import Runtime
    from server.routes import review as rv

    # The runtime's own capacity answer, from Runtime.capacity with 300 MiB
    # free and nothing running.
    fake = _t.SimpleNamespace(live=lambda: [], in_flight={}, units={},
                              guard=_t.SimpleNamespace(headroom=lambda units, flight: 300))
    answer = await Runtime.capacity(fake, ["kimi"], {})
    need = A.harness_memory_mi("kimi", 1)
    rt.answer = lambda op, p: ({"ok": True, "data": answer} if op == "harness.capacity"
                               else {"ok": True, "data": {}})
    cap = await rv._fanout_capacity(["kimi"])
    check(cap is not None and cap.get("refused") == [{"harness": "kimi", "needs_mi": need}],
          "the runtime's capacity answer reaches the fan-out", cap)
    cap = cap or {}
    err = rv._refused_variant_error(cap, "kimi")
    check(err == f"Not started: kimi needs {rv._mi_text(need)} and this machine has 300Mi free.",
          "a coding run this machine has no room for says this machine, not a plan", err)
    note = rv._waiting_variant_note(cap, "kimi", ["codex"])
    check(note.startswith(f"Waiting for memory: kimi needs {rv._mi_text(need)} and this machine "
                          "has 300Mi free while codex run."), "...and so does its waiting line", note)
    msg = rv._capacity_message(cap, ["kimi"])
    check(msg.startswith("None of the 1 harnesses fit this machine right now") and "plan" not in msg,
          "...and the fan-out's refusal", msg)
    plan_cap = dict(cap, scope=None)
    check(rv._refused_variant_error(plan_cap, "kimi").endswith("and this plan has 300Mi free."),
          "a plan's answer still says this plan")

    run_err = "harness wake failed: " + over_plan_text(512, 100)
    check(rv._plain_run_failure("codex", run_err)
          == "codex could not start: this machine has 100Mi free and codex needs 512Mi",
          "a coding run's wake this machine refused says this machine",
          rv._plain_run_failure("codex", run_err))
    plan_err = ('harness wake failed: operator /harness/wake 402: {"detail": {"error": '
                '"capability_over_plan", "needs_mi": 512, "headroom_mi": 32, "upgrade_to": "pro"}}')
    check(rv._plain_run_failure("codex", plan_err)
          == "codex could not start: this plan has 32Mi free and codex needs 512Mi "
             "(the pro plan has room for it)", "...and a plan's still says this plan",
          rv._plain_run_failure("codex", plan_err))


async def helper_launch_checks(rt, tmp):
    """A helper app's launch does not wake it. After a refusal its window
    would open on requests that all answer 507, so the launch asks the wake
    once more and answers the refusal instead; with none on record it wakes
    nothing, and space freed since opens the app."""
    from fastapi import HTTPException
    from server.routes import applications as apps_routes

    appdir = pathlib.Path(tmp) / "helpy"
    appdir.mkdir()
    (appdir / "app.yaml").write_text(
        "id: helpy\nname: Helpy\nentrypoint: index.html\nprovenance: community\n"
        "version: 0.1.0\npermissions: []\nresources:\n  baseline_memory_mi: 64\n"
        "network: [example.com]\nruntime:\n  kind: helper\n")
    inst = types.SimpleNamespace(id="helpy", name="Helpy", version="0.1.0", status="active",
                                 source_dir=str(appdir), entrypoint="index.html",
                                 permissions=[], provenance="community", resident_enabled=None)

    async def get(pool, app_id):
        return inst if app_id == "helpy" else None

    def request():
        return types.SimpleNamespace(
            headers={}, state=types.SimpleNamespace(),
            app=types.SimpleNamespace(state=types.SimpleNamespace(kernel_secret=b"s" * 32)))

    async def launch():
        try:
            return await apps_routes.launch_app("helpy", request()), None
        except HTTPException as e:
            return None, e
        finally:
            t = (hb._state.get("helpy") or {}).get("window_task")
            if t is not None:
                t.cancel()

    real_get = apps_routes.registry.get
    apps_routes.registry.get = get
    try:
        fresh()
        wakes = []
        rt.answer = counting(507, DISK_TEXT, wakes)
        out, exc = await launch()
        check(exc is None and out and out.get("window_beat_s") and wakes == [],
              "a helper's launch with no refusal on record opens it and wakes nothing",
              (out, exc, wakes))

        fresh()
        await hb.ensure_awake_app("helpy")      # the window's request, refused
        wakes = []
        rt.answer = counting(507, DISK_TEXT, wakes)
        out, exc = await launch()
        check(exc is not None and exc.status_code == 507
              and exc.detail == {"error": "wake_refused", "app_id": "helpy", "message": DISK_TEXT}
              and wakes == [],
              "a helper refused moments ago answers the refusal and opens no window",
              (out, getattr(exc, "detail", None), wakes))

        hb._state["helpy"]["cold_failed_at"] = None     # past the lane's backoff
        out, exc = await launch()
        check(exc is not None and exc.status_code == 507 and wakes == ["helpy"],
              "...past the backoff the launch asks the wake once more, refused again",
              (out, getattr(exc, "detail", None), wakes))

        hb._state["helpy"]["cold_failed_at"] = None
        wakes = []
        rt.answer = counting(0, "", wakes, lands=True)
        out, exc = await launch()
        check(exc is None and out and wakes == ["helpy"] and refused("helpy") is None,
              "...and once the machine has room the launch opens it", (out, exc, wakes))
    finally:
        apps_routes.registry.get = real_get


async def main():
    tmp = tempfile.mkdtemp(prefix="wake-refused-", dir=os.environ.get("TMPDIR"))
    sock = os.path.join(tmp, "runtime.sock")
    rt = FakeRuntime(sock)
    await rt.start()
    os.environ[lr.ENV_SOCKET] = sock
    try:
        # ── the Host keeps the refusal ───────────────────────────────────────
        fresh()
        rt.answer = refuse(507, DISK_TEXT)
        host = await hb.ensure_awake_app("weather")
        why = refused("weather")
        check(host is None and why == {"code": 507, "message": DISK_TEXT},
              "an app wake refused 507 is kept with the runtime's words", (host, why))
        check("weather 3.0 GiB" in DISK_TEXT and "notes 40 MiB (removed; its data is kept)" in DISK_TEXT,
              "...which name each app's data, largest first", DISK_TEXT)
        check(await hb.ensure_awake_app("weather") is None
              and (refused("weather") or {}).get("code") == 507,
              "...and it is still the answer through the cold-wake backoff")

        fresh()
        rt.answer = refuse(507, "line one\nline two ‮evil end")
        await hb.ensure_awake_app("weather")
        msg = (refused("weather") or {}).get("message", "")
        check(msg == "line one line two evil end",
              "the words are one line: control and reordering characters become spaces", repr(msg))

        fresh()
        rt.answer = refuse(402, over_plan_text(512, 100))
        await hb.ensure_awake_app("weather")
        why = refused("weather")
        check(why == {"code": 402, "message": "this machine has no room for weather right now: "
                                              "it needs 512 MiB and 100 MiB are free"},
              "a 402 with no room says this machine, with its numbers", why)

        fresh()
        rt.answer = refuse(429, "this machine already runs 40 containers (at most 40)")
        await hb.ensure_awake_app("weather")
        check(refused("weather") == {"code": 429, "message":
                                             "this machine already runs 40 containers (at most 40)"},
              "a 429 carries the runtime's words")

        fresh()
        rt.answer = refuse(504, "still starting")
        await hb.ensure_awake_app("weather")
        check(refused("weather") is None, "still starting is not a refusal for a person")

        fresh()
        hb._st("weather", hb._APP_RT_MARK).update(
            {"wake_error": "operator /harness/wake 507: {\"detail\": \"x\"}", "wake_code": 507})
        check(refused("weather") is None, "an operator's relayed body is not passed on")

        fresh()
        rt.answer = refuse(507, DISK_TEXT)
        check(await hb.ensure_awake("browser", "browser") is None
              and refused("browser") == {"code": 507, "message": DISK_TEXT},
              "the harness lane (the browser) keeps a 507 too")
        rt.answer = lambda op, p: {"ok": True, "data": {"harness": "browser", "ready": True,
                                                         "url": "http://10.89.78.9:8830"}}
        check(await hb.ensure_awake("browser", "browser") == "10.89.78.9:8830"
              and refused("browser") is None, "a wake that lands clears it")

        # ── the desktop ──────────────────────────────────────────────────────
        fresh()
        rt.answer = refuse(507, DISK_TEXT)
        await hb.ensure_awake_app("weather")
        exc = not_awake("weather", "weather is starting — retry")
        check(exc.status_code == 507 and exc.detail == DISK_TEXT,
              "the app's own requests answer 507 with the runtime's words, not 503",
              (exc.status_code, exc.detail))
        fresh()
        rt.answer = refuse(504, "still starting")
        await hb.ensure_awake_app("weather")
        exc = not_awake("weather", "weather is starting — retry")
        check(exc.status_code == 503 and exc.detail == "weather is starting — retry",
              "still starting answers 503 starting, as before")

        fresh()
        rt.answer = refuse(507, DISK_TEXT)
        sup = supervisor.Supervisor.__new__(supervisor.Supervisor)
        try:
            await sup._pod_open(None, types.SimpleNamespace(id="weather"), resident=False)
            raised = None
        except supervisor.SupervisorError as e:
            raised = e
        check(isinstance(raised, getattr(supervisor, "WakeRefused", ()))
              and raised.code == 507 and raised.message == DISK_TEXT and raised.status is None,
              "a pod app's open raises WakeRefused with the status and words", repr(raised))

        names, code_expr, keys = launch_route_arm()
        check(bool(names) and names[0] == "supervisor.WakeRefused" and code_expr == "e.code"
              and {"error", "message"} <= set(keys or []),
              "the launch route answers WakeRefused with its status and {error, message}",
              (names, code_expr, keys))

        if shutil.which("node"):
            out = run_app_runner({"error": "wake_refused", "app_id": "weather",
                                  "message": "disk short weather 3.0 GiB"}, 507)
            check(out["created"] == [] and len(out["notes"]) == 1
                  and out["notes"][0]["title"] == "Could not open Weather"
                  and out["notes"][0]["body"] == "disk short weather 3.0 GiB"
                  and out["notes"][0]["level"] == "error",
                  "the desktop shows the refusal as one line and opens no window", out)
            out = run_app_runner("sidecar launch failed: x", 502)
            check(out["created"] == ["app-weather"] and out["notes"] == [],
                  "any other launch failure opens the window, as before", out)
        else:
            check(False, "node is available for the desktop shell check")

        # ── the Host's tool results ──────────────────────────────────────────
        from mastercode.runtime import app_verify as av
        fresh()
        rt.answer = refuse(507, DISK_TEXT)
        err = await av._start("weather", pool=None, installed=object(), manifest=object(),
                              kind="helper", resident=False)
        check(err == "this machine refused to start it: " + DISK_TEXT,
              "an app start says what the machine said", err)
        check(av._platform_start_error(err) and not av._wake_pending(err),
              "...and it is the platform's refusal, not a failure of the app or a pending wake")
        check(av._wake_pending("runtime pod did not wake yet")
              and not av._platform_start_error("runtime pod did not wake yet"),
              "a wake not landed yet is still pending, as before")

        fresh()
        authz = types.ModuleType("server.vault.authz")
        authz.browser_base_url = lambda: "http://127.0.0.1:1"
        authz.browser_headers = lambda: {}
        sys.modules.setdefault("server.vault.authz", authz)
        out = await av.browser_sidecar_post("/v1/verify", {}, what="browser verify")
        check(out.get("checked") is False and (out.get("refused") or {}).get("code") == 507
              and out.get("reason") == "this machine refused to start the browser: " + DISK_TEXT,
              "a browser call the machine refused says why", out)

        # A browser call this machine had no memory for (its runtime's 402,
        # scope machine) says this machine, not a plan, and what frees room
        # on it (desktop_render.room_sentence, which a refused publish shows).
        fresh()
        rt.answer = refuse(402, over_plan_text(1024, 300))
        out = await av.browser_sidecar_post("/v1/verify", {}, what="browser verify")
        check(out.get("reason") == "this machine has no room for the browser right now "
              "(it needs 1024 MiB and 300 MiB are free)"
              and (out.get("over_plan") or {}).get("scope") == "machine",
              "a browser call this machine had no room for says this machine", out)
        from server.applications import desktop_render as _dr
        room = _dr.room_sentence(out.get("over_plan"))
        check("this machine's memory" in room and "plan" not in room,
              "...and what frees room says this machine, with no plan in it", room)
        check("the plan's memory" in _dr.room_sentence({"scope": None, "upgrade_to": "pro"}),
              "...while a plan's refusal still names the plan")

        fresh()
        rt.answer = refuse(507, DISK_TEXT)
        await hb.ensure_awake_app("weather")
        words = getattr(hb, "wake_refused_words", lambda a: "")
        check(words("weather") == ": " + DISK_TEXT and words("x") == "",
              "an app's tool that did not wake names the refusal (wake_refused_words)")
        src = (ROOT / "server/mcp_connections.py").read_text()
        check(src.count("wake_refused_words(app_id)") == 2,
              "both of the app MCP lanes' did-not-wake errors carry it")

        await browser_agent_checks(rt, tmp)
        await review_checks(rt)
        await helper_launch_checks(rt, tmp)
    finally:
        os.environ.pop(lr.ENV_SOCKET, None)
        await rt.stop()
        shutil.rmtree(tmp, ignore_errors=True)


asyncio.run(main())
print()
if FAILURES:
    print(f"FAIL ({len(FAILURES)})")
    sys.exit(1)
print("all wake refusal checks passed")
