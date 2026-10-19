#!/usr/bin/env python3
"""Gate: a ship proof's ready window is the APP's time, and a pod the
platform never woke is NOT CHECKED — never a failed proof and never a paid
repair.

Production evidence: app_verify 388 and 390 failed
"not ready within 180s — last state: not running yet (start: runtime pod for
mail-sorter did not wake yet …)", and auto-deliver opened a paid repair
build for an app the platform had never started. app_verify 73
failed with its last state "running and ready with a clean log":
the deadline fell between the first clean read and the settle read.

What this pins (no DB, no pod — fakes at the seams, like
test_app_delivery_proven.py):
  1. a pod that never wakes → verdict "not checked", no repair brief, no
     repair started, the repair round unchanged — even with auto-deliver ON;
  2. the ready window starts when the pod is up: an app whose wake lands late
     and then comes up within its own window is delivered;
  3. a clean read in hand at the deadline gets its settle read;
  4. a NEW runtime pod printing output counts as up — an app that then never
     answers ready is still a failed proof with its repair (the app's fault).

Run: APP_ROOT=<repo> python3 scripts/test_app_verify_wake_clock.py
"""
import asyncio
import json
import os
import sys
import tempfile
import textwrap
import time
from pathlib import Path

_ROOT = Path(os.environ.get("APP_ROOT") or Path(__file__).resolve().parents[1])
sys.path[:0] = [str(_ROOT), str(_ROOT / "brain")]

FAILED: list[str] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"[app-verify-wake] ok   {label}")
    else:
        FAILED.append(label)
        print(f"[app-verify-wake] FAIL {label}" + (f" — {detail}" if detail else ""))


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


class FakePool:
    def __init__(self, *, auto=True):
        self.auto = auto
        self.executed: list[tuple[str, tuple]] = []

    async def fetchval(self, sql, *args):
        if "auto_merge" in sql:
            return self.auto
        return None

    async def fetchrow(self, sql, *args):
        return None

    async def execute(self, sql, *args):
        self.executed.append((sql, args))
        return "UPDATE 1"

    def final(self):
        """(status, error, envelope) of the row's finalize UPDATE."""
        for sql, args in self.executed:
            if "SET status='completed'" in sql:
                return "completed", None, json.loads(args[0])
            if "SET status='failed'" in sql:
                return "failed", args[0], json.loads(args[2])
        return None, None, {}


def main() -> int:
    tmpd = os.environ.get("TMPDIR") or None
    from server.applications import registry, storage
    from server.applications import harness_backend as hb
    from server import brain_service
    from mastercode.runtime import tools, app_verify as AV

    tmp = Path(tempfile.mkdtemp(prefix="app-verify-wake-", dir=tmpd))
    storage.APPDATA_ROOT = tmp / "appdata"

    d = tmp / "mailapp"
    d.mkdir(exist_ok=True)
    (d / "app.yaml").write_text(textwrap.dedent("""\
        id: mailapp
        name: M
        entrypoint: index.html
        provenance: community
        version: 0.1.0
        permissions: []
        resources:
          baseline_memory_mi: 64
        network: [example.com]
        runtime:
          kind: helper
        """))
    row = registry.InstalledApp.__new__(registry.InstalledApp)
    row.__dict__.update(dict(id="mailapp", name="M", version="0.1.0", provenance="community",
                             entrypoint="index.html", source_dir=str(d), status="active",
                             permissions=[], resident_enabled=None))

    async def fake_get(pool, app_id):
        return row if app_id == "mailapp" else None
    registry.get = fake_get

    delivered: list = []

    class FakeDispatch:
        async def _deliver(self, task_id):
            delivered.append(task_id)

    class FakeRuntime:
        agent_dispatch = FakeDispatch()
    brain_service.get_runtime = lambda: FakeRuntime()

    started_repairs: list = []

    async def fake_task_start(pool, entity_id, **kw):
        started_repairs.append(kw)
        return 777

    async def fake_run_phase_build(**kw):
        return None
    tools._factory_build_task_start = fake_task_start
    tools._run_phase_build = fake_run_phase_build
    from server.routes import review as RV

    async def fake_resolve(explicit, model=None):
        return ("kimi", None, None)
    RV.resolve_build_target = fake_resolve

    # The pod lane: `awake_at` is when a wake may land (None = never).
    lane = {"awake_at": None, "up": False, "wakes": 0}

    async def fake_wake(app_id):
        lane["wakes"] += 1
        if lane["awake_at"] is not None and time.monotonic() >= lane["awake_at"]:
            lane["up"] = True
            return "[fd00::1]:8850"
        return None
    hb.ensure_awake_app = fake_wake
    hb.app_window_closed = lambda app_id: None

    NOT_RUNNING = {"installed": True, "runtime_kind": "helper", "running": False,
                   "ready": None, "log_tail": "", "log_errors": []}
    BOOTING = {"installed": True, "runtime_kind": "helper", "running": True,
               "ready": {"ok": False, "status": 200, "body": {"ok": False, "phase": "booting"}},
               "log_tail": "phase: booting\n", "log_errors": []}
    GOOD = {"installed": True, "runtime_kind": "helper", "running": True,
            "ready": {"ok": True, "status": 200, "body": {"ok": True}},
            "log_tail": "ok\n", "log_errors": []}

    # status(now) decides what the app looks like at each read.
    status_fn = {"f": lambda now: NOT_RUNNING}

    async def scripted(args):
        return {"status": "ok", "result": dict(status_fn["f"](time.monotonic()))}
    tools._exec_system_app_status = scripted

    def proof(pool, task_id, repair_round=0):
        delivered.clear()
        started_repairs.clear()
        hb._state.pop("mailapp", None)
        run(AV._run_verify(pool=pool, entity_id=9003, project_id=1, app_id="mailapp",
                           task_id=task_id, origin={"session_id": "s1"},
                           repair_round=repair_round, harness=None, model=None))
        return pool.final()

    AV.MIN_WINDOW_S = 1
    AV.MAX_WINDOW_S = 1      # manifest ready_timeout + 120 clamps to a 1 s window
    AV.WAKE_RETRY_S = 0.05   # absent on main; harmless there

    # ── 1. a pod that never wakes is NOT CHECKED ─────────────────────────
    AV.POLL_S = 0.05
    lane.update(awake_at=None, up=False, wakes=0)
    status_fn["f"] = lambda now: NOT_RUNNING
    pool = FakePool(auto=True)
    st, err, env = proof(pool, 801)
    v = env.get("verify") or {}
    rep = env.get("repair") or {}
    check("a pod the platform never woke is verdict 'not checked', not 'failed'",
          v.get("verdict") == "not checked", f"verdict={v.get('verdict')!r} err={err!r}")
    check("...its reason says the platform did not bring the app up, not the app",
          "platform did not bring up" in (v.get("reason") or "")
          and "not a failure of the app" in (v.get("reason") or ""), v.get("reason") or "")
    check("...it writes no repair brief (nothing in the app to fix)",
          "seed" not in v, (v.get("seed") or "")[:120])
    check("...and with auto-deliver ON it starts NO paid repair",
          not started_repairs and rep.get("start") is False, f"{started_repairs} {rep}")
    check("...and spends no repair round (round stays at 0)",
          rep.get("round") == 0 and "no repair round was spent" in (rep.get("why") or ""),
          json.dumps(rep))
    check("...the result is still delivered to the thread", 801 in delivered, repr(delivered))
    check("...and the proof kept asking the lane for the wake", lane["wakes"] >= 3,
          f"wakes={lane['wakes']}")

    # ── 2. the ready window starts when the pod is up ────────────────────
    lane.update(up=False, wakes=0)
    base = {"t": None}

    def late_wake(now):
        if base["t"] is None:
            base["t"] = now
        if not lane["up"]:
            return NOT_RUNNING
        # Up at ~0.6 s; the app's own boot takes another 0.8 s — inside its
        # 1 s window counted from the wake, outside one counted from before it.
        return GOOD if now - base["t"] >= 1.4 else BOOTING
    lane["awake_at"] = time.monotonic() + 10**6   # set per run below
    status_fn["f"] = late_wake
    pool = FakePool(auto=True)
    base["t"] = None
    lane["awake_at"] = time.monotonic() + 0.6
    st, err, env = proof(pool, 802)
    v = env.get("verify") or {}
    check("a wake that lands late does not eat the app's ready window — delivered",
          st == "completed" and v.get("verdict") == "delivered",
          f"status={st} verdict={v.get('verdict')!r} err={err!r}")
    check("...and no repair was started", not started_repairs, repr(started_repairs))

    # ── 3. the settle read is given past the deadline ────────────────────
    lane.update(awake_at=0.0, up=False, wakes=0)
    AV.POLL_S = 0.2
    base["t"] = None

    def clean_at_the_wire(now):
        if base["t"] is None:
            base["t"] = now
        # Reads at ~0, 0.2, … 0.8 are booting; the read at ~1.0 s (just past
        # the 1 s deadline) is the first clean one.
        return GOOD if now - base["t"] >= 0.9 else BOOTING
    status_fn["f"] = clean_at_the_wire
    pool = FakePool(auto=True)
    st, err, env = proof(pool, 803)
    v = env.get("verify") or {}
    check("a clean read at the deadline gets its settle read — delivered, not failed",
          st == "completed" and v.get("verdict") == "delivered",
          f"status={st} verdict={v.get('verdict')!r} err={err!r}")
    check("...and no repair was started", not started_repairs, repr(started_repairs))

    # ── 4. a new runtime pod printing output IS up: the app's own failure ─
    AV.POLL_S = 0.05
    lane.update(awake_at=None, up=False, wakes=0)
    base["t"] = None

    def pod_prints_never_ready(now):
        if base["t"] is None:
            base["t"] = now
        if now - base["t"] >= 0.2:
            # runtime_log._pod_generation records the pod that pushed output.
            hb._st("mailapp", hb._APP_RT_MARK)["runtime_pod"] = "app-rt-mailapp-new"
        return {**NOT_RUNNING, "log_tail": "Traceback (most recent call last):\nboom\n"}
    status_fn["f"] = pod_prints_never_ready
    pool = FakePool(auto=True)
    st, err, env = proof(pool, 804)
    v = env.get("verify") or {}
    check("an app whose new pod prints but never gets ready is a FAILED proof",
          st == "failed" and v.get("verdict") == "failed" and "not ready within" in (err or ""),
          f"status={st} verdict={v.get('verdict')!r} err={err!r}")
    check("...which keeps its repair brief and its paid repair (the app's fault)",
          "seed" in v and len(started_repairs) == 1, f"{started_repairs}")

    if FAILED:
        print(f"FAIL: app-verify wake clock — {len(FAILED)} check(s): " + "; ".join(FAILED))
        return 1
    print("OK: app-verify wake clock — the ready window is the app's; an unwoken pod is not checked")
    return 0


if __name__ == "__main__":
    sys.exit(main())
