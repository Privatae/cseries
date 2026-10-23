#!/usr/bin/env python3
"""The saved request's SQL, run against a REAL Postgres.

Every other gate for the saved request (test_tools_before_first_turn.py) drives
the code over a fake pool that matches SQL by substring. A fake cannot see
whether a statement is VALID, and that is exactly how one got through: the
takeover UPDATEs were `UPDATE calendar_events SET ... FROM calendar_events WHERE
...` (the SELECT fragment `FROM calendar_events WHERE ...` appended to an
UPDATE), which Postgres rejects ("table name "calendar_events" specified more
than once"). take_all() logged it as a warning, so no takeover ever worked on a
real database and the held wakes ran on their own.

This gate builds the real schema (init-db.sql and every migration) in a
throwaway cseries-postgres container and runs the real functions on real rows:

  1. the transitions, end to end: save a refused request; a person's turn takes
     it over (scheduled -> taken over -> settled, every outcome); a held request
     taken back; a stale running request taken flagged; the crash sweeps
     (taken-over by another process, stuck running); a reschedule refused for a
     flagged row; the grant/model release; every runner statement (claim,
     touch, requeue, defer, wait-for-plan, plan-confirmed, fold, finalize,
     deleted-thread cancel, orphan sweep); the thread delete; every external
     cancel / complete;
  2. a SWEEP: every static SQL string passed to execute / fetch / fetchrow /
     fetchval in server/ and brain/ is EXPLAINed (a generic plan, nothing run)
     against the real schema. The modules that hold the saved request's SQL
     (identity_hold, offline_task_runner, circles_threads, memory_handlers) must
     have no invalid statement; the rest of the tree is swept and reported, and
     fails the gate (there is no known-bad list).

The DB lane: needs docker (psql runs inside the container; the image is the one
test_restore_carry_real_schema.py uses). Without docker: SKIPPED (exit 0) unless
CSERIES_DB_LANE=1, where a gate that cannot run is a failure (exit 3).

    python3 scripts/test_saved_request_sql_real_db.py
"""
import ast
import asyncio
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

ROOT = Path(os.environ.get("APP_ROOT") or Path(__file__).resolve().parent.parent)
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "brain"))
FAILED = []
LANE = os.environ.get("CSERIES_DB_LANE") == "1"


def check(label, cond, detail=""):
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  [{str(detail)[:500]}]"))
    if not cond:
        FAILED.append(label)


if not shutil.which("docker") or subprocess.run(["docker", "version"], capture_output=True).returncode != 0:
    if LANE:
        print("cannot run: docker is not reachable (CSERIES_DB_LANE=1: a gate that cannot run is a failure)")
        sys.exit(3)
    print("SKIPPED: docker is not reachable (this gate needs a real Postgres; set CSERIES_DB_LANE=1 to require it)")
    sys.exit(0)

_PG_FILES = ("Dockerfile.postgres", "entrypoint-postgres.sh", "LICENSE", "NOTICE",
             "THIRD-PARTY-NOTICES.md")
_PG_HASH = hashlib.sha256(b"".join((ROOT / f).read_bytes() for f in _PG_FILES)).hexdigest()[:12]
IMAGE = os.environ.get("PG_IMG") or f"{os.environ.get('CSERIES_GATE_IMAGE_PREFIX') or 'cseries-postgres-carry'}:{_PG_HASH}"
if subprocess.run(["docker", "image", "inspect", IMAGE], capture_output=True).returncode != 0:
    ctx = Path(tempfile.mkdtemp(prefix="srsql-pgctx-"))
    for f in _PG_FILES:
        shutil.copy(ROOT / f, ctx / f)
    print(f"building {IMAGE} from Dockerfile.postgres")
    b = subprocess.run(["docker", "build", "-q", "-f", str(ctx / "Dockerfile.postgres"), "-t", IMAGE, str(ctx)],
                       capture_output=True, text=True)
    shutil.rmtree(ctx, ignore_errors=True)
    if b.returncode != 0:
        print(f"FAIL: the database image did not build: {b.stderr.strip()[-400:]}")
        sys.exit(1)

import asyncpg  # noqa: E402

SOCK_PATH_MAX = 107


def pick_work_dir():
    cands = []
    if os.environ.get("CSERIES_GATE_SHARED_DIR"):
        cands.append(Path(os.environ["CSERIES_GATE_SHARED_DIR"]))
    cands += [ROOT.parent, Path(tempfile.gettempdir())]
    for base in cands:
        d = base / f".sr-{os.getpid()}"
        if len(str(d) + "/s/.s.PGSQL.5432") > SOCK_PATH_MAX:
            continue
        try:
            d.mkdir(mode=0o755)
        except OSError:
            continue
        return d
    raise SystemExit("FAIL: no folder for the database socket fits a unix socket address")


work = pick_work_dir()
sock = work / "s"
sock.mkdir()
sock.chmod(0o777)
name = f"sr-sql-{os.getpid()}"
started = subprocess.run(["docker", "run", "-d", "--rm", "--name", name, "--network", "none",
                          "-v", f"{sock}:/var/run/postgresql", IMAGE], capture_output=True, text=True)
if started.returncode != 0:
    print(f"FAIL: the database did not start ({started.stderr.strip()[-200:]})")
    shutil.rmtree(work, ignore_errors=True)
    sys.exit(1)


def psql(db, *args, sql_file=None):
    cmd = ["docker", "exec", "-i", name, "psql", "-h", "/var/run/postgresql", "-U", "entity", "-d", db,
           "-v", "ON_ERROR_STOP=1", "-Atq", *args]
    if sql_file is not None:
        cmd += ["-f", "-"]
        return subprocess.run(cmd, input=Path(sql_file).read_text(), capture_output=True, text=True)
    return subprocess.run(cmd, capture_output=True, text=True)


# ── the static SQL sweep ──────────────────────────────────────────────────────
CALLS = {"execute", "fetch", "fetchrow", "fetchval", "executemany"}
DML = re.compile(r"^\s*\(?\s*(SELECT|INSERT|UPDATE|DELETE|WITH)\b", re.I)
STRICT = ("brain/mastercode/runtime/identity_hold.py", "brain/mastercode/brain/offline_task_runner.py",
          "brain/mastercode/circles_threads.py", "server/bridge/memory_handlers.py")


def _eval(node, env):
    """The string an expression evaluates to when it is built only from literals,
    module/function constants, +, str.replace and f-strings of those; else None."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Name):
        return env.get(node.id)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        a, b = _eval(node.left, env), _eval(node.right, env)
        return a + b if a is not None and b is not None else None
    if isinstance(node, ast.JoinedStr):
        out = ""
        for v in node.values:
            if isinstance(v, ast.Constant):
                out += str(v.value)
            elif isinstance(v, ast.FormattedValue):
                e = _eval(v.value, env)
                if e is None:
                    return None
                out += e
            else:
                return None
        return out
    if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "replace"
            and len(node.args) == 2):
        base, a, b = _eval(node.func.value, env), _eval(node.args[0], env), _eval(node.args[1], env)
        if None not in (base, a, b):
            return base.replace(a, b)
    return None


def _holes(node, env):
    """The text of an expression with every part that cannot be resolved shown as `{}`:
    what an interpolated statement says around its holes."""
    v = _eval(node, env)
    if v is not None:
        return v
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return _holes(node.left, env) + _holes(node.right, env)
    if isinstance(node, ast.JoinedStr):
        return "".join(str(p.value) if isinstance(p, ast.Constant)
                       else (_eval(p.value, env) if isinstance(p, ast.FormattedValue)
                             and _eval(p.value, env) is not None else "{}") for p in node.values)
    return "{}"


def sweep_statements(files):
    """[(relpath, lineno, sql_or_None, interpolated, function, text_with_holes)] for every pool call."""
    out = []
    for p in files:
        try:
            tree = ast.parse(p.read_text())
        except Exception:
            continue
        env = {}
        for n in tree.body:                                   # module constants, in order
            if isinstance(n, ast.Assign) and len(n.targets) == 1 and isinstance(n.targets[0], ast.Name):
                v = _eval(n.value, env)
                if v is not None:
                    env[n.targets[0].id] = v
        for fn in [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]:
            local = dict(env)
            for st in ast.walk(fn):                           # constants built inside the function
                if isinstance(st, ast.Assign) and len(st.targets) == 1 and isinstance(st.targets[0], ast.Name):
                    v = _eval(st.value, local)
                    if v is not None:
                        local[st.targets[0].id] = v
            for call in ast.walk(fn):
                if (isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute)
                        and call.func.attr in CALLS and call.args):
                    sql = _eval(call.args[0], local)
                    interp = sql is None and isinstance(call.args[0], (ast.JoinedStr, ast.BinOp))
                    out.append((str(p.relative_to(ROOT)), call.lineno, sql, interp, fn.name,
                                _holes(call.args[0], local) if sql is None else sql))
    return out


# ── the scenarios ─────────────────────────────────────────────────────────────
EID = 9003
THREAD = "0b8f2c1e-4d3a-4a55-9a77-0c1d2e3f4a5b"
MSG = "Put a file called x.txt in your workspace."


async def main(pool):
    import mastercode.memory_engine as me
    import mastercode.runtime.identity_hold as H
    import mastercode.runtime.session  # noqa: F401  (transient_interaction_context)
    from mastercode import circles_threads as CT
    from mastercode.brain import offline_task_runner as otr
    from mastercode.runtime import tools as T
    import server.bridge.memory_handlers as MH
    from server import brain_service
    from server.bridge import identity_seed as S

    brain_service.get_pool = lambda: pool
    brain_service._entity_id = EID
    S.SEED_STATE.update({"status": "ok", "entity_id": EID, "retrying": False})
    await pool.execute("INSERT INTO entity_registry (id, display_name, permission_overrides) "
                       "VALUES ($1, 'sr-gate', '{\"bot_capabilities\": 47}'::jsonb) ON CONFLICT (id) DO NOTHING", EID)

    async def mk(state, *, flagged=False, boot=None, thread=THREAD, age_s=0, start_in_s=0, recurrence=None,
                 extra=None):
        payload = {"saved_request": True, "prompt": MSG,
                   "origin": {"interaction_context": "chat", "thread_id": thread}, **(extra or {})}
        reason = None
        if flagged:
            payload["crash_unknown"] = True
        status = state
        if state == "taken":
            status, reason = "cancelled", H._TAKEN_REASON
            payload["taken_boot"] = boot or H.PROCESS_BOOT_ID
        elif state == "held":
            status, reason = "cancelled", "held_for_person"
        return await pool.fetchval(
            "INSERT INTO calendar_events (entity_id, title, start_time, end_time, event_type, event_at, summary,"
            " payload, source, status, cancelled_reason, recurrence, updated_at)"
            " VALUES ($1, 'a saved request', now() + make_interval(secs => $7), now() + make_interval(secs => $7),"
            " 'scheduled_task', now(), 'a saved request', $2::jsonb, 'platform', $3, $4, $5,"
            " now() - make_interval(secs => $6)) RETURNING id",
            EID, json.dumps(payload), status, reason, recurrence, float(age_s), float(start_in_s))

    async def get(i):
        r = await pool.fetchrow("SELECT status, cancelled_reason, payload, start_time, updated_at, "
                                "(start_time > now()) AS later FROM calendar_events WHERE id=$1", i)
        d = dict(r)
        d["payload"] = json.loads(d["payload"]) if isinstance(d["payload"], str) else d["payload"]
        return d

    def held_flagged(d):
        return (d["status"] == "cancelled" and d["cancelled_reason"] == "held_for_person"
                and d["payload"].get("crash_unknown") is True)

    async def clear():
        await pool.execute("DELETE FROM calendar_events WHERE entity_id=$1", EID)
        H.reset_saved_state()

    # ── 1. a refused request is saved: user row + wake, on the real tables ───
    await clear()

    async def no_embed(_t):
        raise RuntimeError("no embedder in this gate")
    _orig = me._get_memory_embedding
    me._get_memory_embedding = no_embed
    refusal = {"response": "x", "identity_pending": True,
               "identity": {"status": "placeholder", "retrying": True}, "identity_waited_s": 1.0}
    out = await H.save_refused_request(pool, EID, refusal, user_message=MSG, app_id="circles", thread_id=THREAD,
                                       app_permissions=["tutor.read"])
    me._get_memory_embedding = _orig
    row = await pool.fetchrow("SELECT id, status, payload FROM calendar_events WHERE entity_id=$1", EID)
    pl = json.loads(row["payload"]) if isinstance(row["payload"], str) else row["payload"]
    check("save: the person's row and the wake were written on the real tables",
          out == {"rows": True, "wake": True} and row["status"] == "scheduled"
          and pl.get("user_row", {}).get("session_id") and "prompt" not in pl, repr((out, pl)))
    check("save: the saved words read back through the real SELECT",
          await H.saved_request_text(pool, EID, pl) == MSG)
    wake1 = row["id"]

    # ── 2. the takeover, end to end: scheduled -> taken over -> settled ──────
    h = await H.hand_over_saved_requests(pool, EID, app_id="circles", thread_id=THREAD, wait_running=False)
    d = await get(wake1)
    check("takeover: a person's turn takes the scheduled request over on the REAL database",
          bool(h) and d["status"] == "cancelled" and d["cancelled_reason"] == H._TAKEN_REASON
          and d["payload"].get("taken_boot") == H.PROCESS_BOOT_ID, repr(d))
    check("takeover: its words come back for the model", await H.resolve_locators(pool, EID, h) == [MSG])
    h.model_saw = True
    H.post_line = lambda *_a, **_k: None
    await h.settle("ok")
    d = await get(wake1)
    check("takeover: settled 'ok' -> carried forward", d["cancelled_reason"] == H.CARRIED_REASON, repr(d))

    for outcome, why, acted, want_status, want_reason in (
            ("stopped", "", False, "cancelled", H.STOPPED_REASON),
            ("failed", "", True, "cancelled", H.PARTIAL_REASON),
            ("not_run", "blocked", False, "cancelled", "held_for_person"),
            ("failed", "", False, "scheduled", None)):
        await clear()
        i = await mk("scheduled")
        h = await H.hand_over_saved_requests(pool, EID, app_id="circles", thread_id=THREAD, wait_running=False)
        h.model_saw, h.acted = False, acted
        await h.settle(outcome, why)
        d = await get(i)
        check(f"takeover: settled '{outcome}'{' after acting' if acted else ''} -> {want_status}/{want_reason}",
              d["status"] == want_status and d["cancelled_reason"] == want_reason
              and (want_status != "scheduled" or (d["later"] and d["payload"].get("identity_defers") == 1)),
              repr(d))

    # a held request is taken back by the person's next message; a stale running one is taken flagged
    await clear()
    i_held = await mk("held")
    i_run = await mk("running", age_s=10)
    h = await H.hand_over_saved_requests(pool, EID, app_id="circles", thread_id=THREAD, wait_running=True)
    dh, dr = await get(i_held), await get(i_run)
    check("takeover: a held request is taken over by the next message, recording the boot id",
          dh["cancelled_reason"] == H._TAKEN_REASON and dh["payload"].get("taken_boot") == H.PROCESS_BOOT_ID, repr(dh))
    check("takeover: a stale running request is taken over FLAGGED as possibly partly done",
          dr["cancelled_reason"] == H._TAKEN_REASON and dr["payload"].get("crash_unknown") is True
          and i_run in h.ids, repr(dr))
    await h.settle("failed")
    dr = await get(i_run)
    check("takeover: and when that turn fails before acting, it is held, never scheduled",
          held_flagged(dr), repr(dr))

    # ── 3. the crash sweeps ───────────────────────────────────────────────────
    await clear()
    a = await mk("taken", boot="a-process-that-is-gone")
    b = await mk("taken")
    c = await mk("taken", age_s=1800 + 600)
    n = await H.sweep_taken_over(pool, EID, 1800)
    check("sweep: a takeover by a process that is gone is held and flagged at once",
          held_flagged(await get(a)) and n == 2, n)
    check("sweep: a young takeover by THIS process is left alone",
          (await get(b))["cancelled_reason"] == H._TAKEN_REASON)
    check("sweep: this process's own that outlived any turn is still caught (backstop)",
          held_flagged(await get(c)))
    await clear()
    s_saved = await mk("running", age_s=5000)
    s_plain = await pool.fetchval(
        "INSERT INTO calendar_events (entity_id, title, start_time, end_time, event_type, event_at, summary,"
        " payload, source, status, updated_at) VALUES ($1, 'x', now(), now(), 'scheduled_task', now(), 'x',"
        " '{\"prompt\": \"y\"}'::jsonb, 'entity', 'running', now() - interval '5000 seconds') RETURNING id", EID)
    s_young = await mk("running", age_s=1)
    await otr._sweep_orphans(pool, EID)
    check("sweep: a SAVED request stuck running (its process died) is held and flagged, never scheduled",
          held_flagged(await get(s_saved)), repr(await get(s_saved)))
    check("sweep: an ordinary task stuck running is reset to scheduled as before",
          (await get(s_plain))["status"] == "scheduled")
    check("sweep: a young running request is left alone", (await get(s_young))["status"] == "running")

    # ── 4. reschedule: refused for a flagged row; release ────────────────────
    await clear()
    f_t = await mk("taken", flagged=True)
    u_t = await mk("taken")
    await H.reschedule(pool, EID, ids=[f_t, u_t], how="backoff")
    check("reschedule: a flagged request is refused: held, never scheduled", held_flagged(await get(f_t)))
    du = await get(u_t)
    check("reschedule: an unflagged one goes back scheduled, later, counted",
          du["status"] == "scheduled" and du["later"] and du["payload"].get("identity_defers") == 1, repr(du))
    await clear()
    r_ok = await mk("scheduled", start_in_s=600)
    r_flag = await mk("scheduled", start_in_s=600, flagged=True)
    r_held = await mk("held", flagged=True)
    await otr.release_saved_requests(pool, EID)
    d_ok, d_flag, d_held = await get(r_ok), await get(r_flag), await get(r_held)
    check("release: an unflagged request is made due now", d_ok["status"] == "scheduled" and not d_ok["later"], repr(d_ok))
    check("release: a flagged request found scheduled is put back to held, never released",
          held_flagged(d_flag), repr(d_flag))
    check("release: a flagged request already held is untouched", held_flagged(d_held))

    # ── 5. every runner statement ────────────────────────────────────────────
    await clear()
    t = await mk("running")
    await otr._touch(pool, EID, t)
    check("runner: touch leaves it running", (await get(t))["status"] == "running")
    await otr._requeue(pool, EID, t)
    check("runner: requeue -> scheduled", (await get(t))["status"] == "scheduled")
    await pool.execute("UPDATE calendar_events SET status='running' WHERE id=$1", t)
    await otr._defer_for_identity(pool, EID, t, 2)
    d = await get(t)
    check("runner: defer -> scheduled, later, counted", d["status"] == "scheduled" and d["later"]
          and d["payload"].get("identity_defers") == 2, repr(d))
    await pool.execute("UPDATE calendar_events SET status='running' WHERE id=$1", t)
    await otr._wait_for_plan(pool, EID, t, 3)
    d = await get(t)
    check("runner: wait-for-plan -> scheduled, later", d["status"] == "scheduled" and d["later"], repr(d))
    await pool.execute("UPDATE calendar_events SET payload = payload || '{\"plan_event_id\": \"p\"}'::jsonb WHERE id=$1", t)
    await otr._plan_confirmed(pool, EID, t)
    check("runner: plan-confirmed drops the marker", "plan_event_id" not in (await get(t))["payload"])
    other = await mk("scheduled")
    got = await otr._fold_into_newest(pool, EID, {"prompt": "older", "origin": {"interaction_context": "chat",
                                                  "thread_id": THREAD}}, t)
    check("runner: fold writes the older request onto the newest pending one",
          got and (await get(other))["payload"].get("earlier") == [{"prompt": "older"}], repr(await get(other)))
    await pool.execute("UPDATE calendar_events SET status='running' WHERE id=$1", t)
    await otr._finalize(pool, t, EID, None, None, True)
    check("runner: finalize one-shot ok -> completed", (await get(t))["status"] == "completed")
    rec = await mk("running", recurrence="every:3600", extra={"identity_defers": 4})
    await otr._finalize(pool, rec, EID, "every:3600", __import__("datetime").datetime.now(
        __import__("datetime").timezone.utc), True)
    d = await get(rec)
    check("runner: finalize recurring re-arms scheduled and clears the deferral counter",
          d["status"] == "scheduled" and d["later"] and "identity_defers" not in d["payload"], repr(d))

    await clear()
    due = await mk("scheduled")
    ran = []

    async def noop(_p, _e, claimed):
        ran.append(claimed["id"])
    _gr, _pi = otr._guarded_run, otr.POLL_INTERVAL_S
    otr._guarded_run, otr.POLL_INTERVAL_S = noop, 0.02
    task = asyncio.create_task(otr.run_offline_tasks_loop(pool, EID))
    for _ in range(100):
        if ran:
            break
        await asyncio.sleep(0.05)
    task.cancel()
    try:
        await task
    except BaseException:
        pass
    otr._guarded_run, otr.POLL_INTERVAL_S = _gr, _pi
    check("runner: the claim (loop) takes a due request: running", ran == [due] and (await get(due))["status"] == "running")

    # the deleted-thread cancel inside a run
    await clear()
    t = await mk("running")
    from mastercode.circles_threads import DELETED_THREAD_REFUSAL
    _bs = (brain_service.send_message, brain_service.get_runtime, otr.post_to_origin_thread)

    async def deleted(**_kw):
        yield "3:" + json.dumps(DELETED_THREAD_REFUSAL) + "\n"

    async def quiet(*_a, **_k):
        return True
    brain_service.send_message, brain_service.get_runtime, otr.post_to_origin_thread = deleted, (lambda: object()), quiet
    try:
        claimed = await pool.fetchrow("SELECT id, title, recurrence, start_time, payload FROM calendar_events WHERE id=$1", t)
        await otr._run_one(pool, EID, claimed)
    finally:
        brain_service.send_message, brain_service.get_runtime, otr.post_to_origin_thread = _bs
    check("runner: a run in a deleted thread is cancelled", (await get(t))["status"] == "cancelled")

    # ── 6. thread delete, on the real tables ─────────────────────────────────
    await clear()
    th = await CT.create_thread(pool, EID, title="gate thread")
    tid = th["id"]
    w_sched = await mk("scheduled", thread=tid)
    w_held = await mk("held", thread=tid, flagged=True)
    w_other = await mk("scheduled", thread=THREAD)
    await CT.delete_thread(pool, EID, tid)
    ds, dh_, do = await get(w_sched), await get(w_held), await get(w_other)
    check("thread delete: a scheduled wake in it is cancelled",
          ds["status"] == "cancelled" and "deleted" in (ds["cancelled_reason"] or ""), repr(ds))
    check("thread delete: a held request in it stops being looked for (reason cleared)",
          dh_["status"] == "cancelled" and "deleted" in (dh_["cancelled_reason"] or ""), repr(dh_))
    check("thread delete: another thread's wake is untouched", do["status"] == "scheduled")

    # ── 7. every external cancel / complete ──────────────────────────────────
    await clear()
    ev = lambda i: pool.fetchval("SELECT event_id::text FROM calendar_events WHERE id=$1", i)  # noqa: E731
    i1, i2, i3, i4, i5 = [await mk("scheduled") for _ in range(5)]
    r = await MH.handle_calendar_cancel({"event_id": await ev(i1), "reason": "no"})
    check("cancel by id (the person / calendar pane)", r.get("ok") and (await get(i1))["status"] == "cancelled", repr(r))
    r = await T._exec_schedule_task({"op": "cancel", "event_id": await ev(i2)})
    check("the Host's schedule_task cancel", r.get("status") == "ok" and (await get(i2))["status"] == "cancelled", repr(r))
    try:
        import server.routes.agents as agents_mod
        _ae = getattr(agents_mod, "_entity_id", None)
        agents_mod._entity_id = lambda: EID
        try:
            r = await agents_mod.cancel_scheduled(await ev(i3))
        finally:
            if _ae is not None:
                agents_mod._entity_id = _ae
        check("the Agents app's cancel", (await get(i3))["status"] == "cancelled", repr(r))
    except ImportError as exc:
        print(f"  (agents route not importable here: {exc}; covered by the sweep)")
    try:
        import server.browser_agents as ba
        await ba._cancel_event(pool, EID, await ev(i4))
        check("a browser agent's cancel", (await get(i4))["status"] == "cancelled")
    except ImportError as exc:
        print(f"  (browser_agents not importable here: {exc}; covered by the sweep)")
    r = await MH.handle_calendar_complete({"event_id": await ev(i5), "result": {"ok": 1}})
    check("marked complete", r.get("ok") and (await get(i5))["status"] == "completed", repr(r))
    r = await MH.handle_calendar_append({"event_id": str(uuid.uuid4()), "event_type": "scheduled_task",
                                         "summary": "x", "event_at": "2026-10-01T00:00:30Z",
                                         "status": "scheduled", "source": "platform",
                                         "payload": {"saved_request": True}})
    check("append (the wake's INSERT)", r.get("ok"), repr(r))
    r = await MH.handle_calendar_read({"event_id": await ev(i1)})
    check("read returns the cancelled reason and held fields", "cancelled_reason" in r, repr(r))
    r = await MH.handle_calendar_list({})
    check("list runs", r.get("ok") and r["count"] >= 1, repr(r))

    # ── 7b. a restore's renumber carries rows in the state the snapshot held ─
    import server.host_restore as hr
    await clear()
    r_run = await mk("running")
    r_taken = await mk("taken", boot="a-process-that-is-gone")
    r_held = await mk("held", flagged=True)
    before = {i: await get(i) for i in (r_run, r_taken, r_held)}
    async with pool.acquire() as conn:
        tr = conn.transaction()
        await tr.start()
        await conn.execute("SET LOCAL session_replication_role = replica")
        moved = await hr.renumber(conn, EID, 55)
        after_ids = {i: await conn.fetchrow("SELECT entity_id, status, cancelled_reason, payload FROM calendar_events WHERE id=$1", i)
                     for i in before}
        await tr.rollback()
    check("restore: renumber moves calendar_events.entity_id", moved.get("calendar_events.entity_id") is True, repr(moved))
    check("restore: ...and nothing else about a saved request: status, reason and payload come back as they were",
          all(after_ids[i]["entity_id"] == 55 and after_ids[i]["status"] == before[i]["status"]
              and after_ids[i]["cancelled_reason"] == before[i]["cancelled_reason"]
              and (json.loads(after_ids[i]["payload"]) if isinstance(after_ids[i]["payload"], str) else after_ids[i]["payload"])
              == before[i]["payload"] for i in before), repr(after_ids))
    # after a restore the boot sweeps take what the snapshot's process left behind
    await pool.execute("UPDATE calendar_events SET updated_at = now() - interval '5000 seconds' WHERE id=$1", r_run)
    await otr._sweep_orphans(pool, EID)
    check("restore: a restored stuck-running saved request and a restored taken-over one are held and flagged at boot",
          held_flagged(await get(r_run)) and held_flagged(await get(r_taken)), repr((await get(r_run), await get(r_taken))))

    # ── 7c. the Host's handler identity, on the real entity_registry ─────────
    # (prompt_builder.resolve_comm_facts once also asked a table of the old platform's
    # auth database, which a pod never had: a statement that could not succeed.)
    import logging
    from mastercode.runtime import prompt_builder as PB
    await pool.execute("UPDATE entity_registry SET operator_id = NULL, behavioral_params = $2::jsonb WHERE id=$1",
                       EID, json.dumps({"handler_name": "Alex", "timezone": "America/Los_Angeles"}))
    f1 = await PB.resolve_comm_facts(pool, EID)
    check("handler: behavioral_params.handler_name wins, with the timezone, on the real registry row",
          f1 == {"handler_name": "Alex", "handler_verified": True, "user_tz": "America/Los_Angeles"}, repr(f1))
    await pool.execute("UPDATE entity_registry SET operator_id = 'alex.rivera@example.com', behavioral_params = '{}'::jsonb WHERE id=$1", EID)
    f2 = await PB.resolve_comm_facts(pool, EID)
    check("handler: with no handler_name it is derived from the operator_id email",
          f2["handler_name"] == "Alex" and f2["handler_verified"] is True, repr(f2))
    recs = []

    class _Cap(logging.Handler):
        def emit(self, r):
            recs.append(r)
    cap = _Cap(level=logging.DEBUG)
    PB.log.addHandler(cap)
    _lvl = PB.log.level
    PB.log.setLevel(logging.DEBUG)

    class _BrokenPool:
        async def fetchrow(self, *_a, **_k):
            raise RuntimeError("registry unreadable")
    try:
        f3 = await PB.resolve_comm_facts(_BrokenPool(), EID)
    finally:
        PB.log.removeHandler(cap)
        PB.log.setLevel(_lvl)
    check("handler: a failed read of the registry row is a WARNING (not debug) and the turn goes on without a name",
          f3["handler_name"] is None and any(r.levelno == logging.WARNING for r in recs), repr([r.getMessage() for r in recs]))

    # ── 8. the sweep: EXPLAIN every static statement against the real schema ─
    files = sorted(p for base in ("server", "brain") for p in (ROOT / base).rglob("*.py"))
    sqlite_files = {str(p.relative_to(ROOT)) for p in files if re.search(r"^\s*import sqlite3|^\s*from sqlite3", p.read_text(), re.M)}
    stmts = sweep_statements(files)
    seen, bad, skipped_nondml, skipped_sqlite, interp = {}, [], 0, 0, []
    async with pool.acquire() as conn:
        for rel, line, sql, is_interp, _fn, _tpl in stmts:
            if sql is None:
                if is_interp:
                    interp.append((rel, line, _fn, _tpl))
                continue
            if not DML.match(sql):
                skipped_nondml += 1
                continue
            # SQLite, not the pod's Postgres: `?` / :name placeholders, or a file that
            # talks to a sqlite3 connection (the integrations store, the restore's
            # sqlite carry). The strict modules are always checked.
            if rel not in STRICT and (rel in sqlite_files or re.search(r"\?|(?<![:\w]):[a-z_]+\b", sql)):
                skipped_sqlite += 1
                continue
            key = (rel, sql)
            if key in seen:
                continue
            seen[key] = line
            tr = conn.transaction()
            await tr.start()
            try:
                await conn.execute(("EXPLAIN (GENERIC_PLAN) " if "$1" in sql else "EXPLAIN ") + sql)
            except Exception as exc:  # noqa: BLE001
                bad.append((rel, line, type(exc).__name__ + ": " + str(exc).splitlines()[0][:140], sql))
            finally:
                await tr.rollback()
    strict_bad = [b for b in bad if b[0] in STRICT]
    n_strict = sum(1 for (rel, _s) in seen if rel in STRICT)
    # No known-bad list: the two statements this sweep found outside the saved request's modules
    # (a SHARED_REF lookup on a column that never existed; a query on a platform table a pod never
    # had) were removed as dead, so ANY invalid statement fails.
    other_bad = [b for b in bad if b[0] not in STRICT]
    unknown = other_bad
    # "0 invalid" covers only what the sweep could resolve: name the rest so the number is not read
    # as covering every statement (SQL passed in as an argument, held in an attribute, chosen with a
    # conditional, or built with join / % / .format()).
    unresolved = [s for s in stmts if s[2] is None and not s[3]]
    print(f"  sweep: {len(stmts)} pool calls found; {len(seen)} distinct static DML statements EXPLAINed "
          f"({n_strict} in the saved request's modules); {skipped_nondml} non-DML and {skipped_sqlite} "
          f"SQLite skipped; {len(interp)} built with interpolation and {len(unresolved)} unresolved "
          f"(neither is checked); {len(bad)} invalid "
          f"({len(strict_bad)} in the saved request's modules, {len(other_bad)} elsewhere)")
    for u in unresolved:
        if u[0] in STRICT:
            print(f"    unresolved in the saved request's modules: {u[0]}:{u[1]} in {u[4]}()")
    for b in bad:
        print(f"    INVALID {b[0]}:{b[1]}  {b[2]}\n            {b[3][:110]!r}")
    check("sweep: every static statement in the saved request's modules plans on the real schema",
          not strict_bad, repr([(b[0], b[1], b[2]) for b in strict_bad]))
    check("sweep: nothing in server/ or brain/ is an invalid statement (no known-bad list)",
          not unknown, repr([(b[0], b[1], b[2]) for b in unknown[:5]]))

    # The statements the sweep cannot EXPLAIN because part of them is built at run time. In the
    # saved request's modules each must be accounted for: one that writes calendar_events must be
    # a row of the state map (by function), and none may write a COMPUTED table name.
    doc = (ROOT / "docs" / "DESIGN-saved-request-state-machine-2026-10-01.md").read_text()
    doc_sites = set(re.findall(r"`([\w/\.]+\.py)::(\w+)`", doc))
    interp_strict = [i for i in interp if i[0] in STRICT]
    writes_ce = [i for i in interp_strict if re.search(r"\b(UPDATE|INSERT\s+INTO|DELETE\s+FROM)\s+calendar_events\b", i[3], re.I)]
    computed_tbl = [i for i in interp_strict if re.search(r"\b(UPDATE|INSERT\s+INTO|DELETE\s+FROM)\s+\{\}", i[3], re.I)]
    print(f"  interpolated statements in the saved request's modules: {len(interp_strict)} "
          f"({', '.join(f'{m.split(chr(47))[-1]}: ' + str(sum(1 for i in interp_strict if i[0] == m)) for m in STRICT)}); "
          f"{len(writes_ce)} write calendar_events")
    for i in writes_ce:
        print(f"    writes calendar_events  {i[0]}:{i[1]} in {i[2]}()  {i[3][:90]!r}")
    check("sweep: every interpolated write to calendar_events in the saved request's modules is a row of the map",
          all((i[0], i[2]) in doc_sites for i in writes_ce), repr([(i[0], i[2]) for i in writes_ce if (i[0], i[2]) not in doc_sites]))
    check("sweep: none of the saved request's modules writes a computed table name",
          not computed_tbl, repr([(i[0], i[1], i[3][:60]) for i in computed_tbl]))


try:
    for _ in range(90):
        if psql("postgres", "-c", "SELECT 1").returncode == 0:
            break
        time.sleep(1)
    else:
        print("FAIL: the database never became ready")
        sys.exit(1)
    psql("postgres", "-c", "CREATE DATABASE tmpl")
    r = psql("tmpl", "--single-transaction", sql_file=ROOT / "init-db.sql")
    if r.returncode != 0:
        print(f"FAIL: init-db.sql did not apply: {r.stderr[-300:]}")
        sys.exit(1)
    # entrypoint.sh creates the migration ledger itself, before the migrations
    # (it is in no .sql file); server/routes/system.py reads it.
    psql("tmpl", "-c", "CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY, "
                       "name TEXT, applied_at TIMESTAMPTZ DEFAULT now())")
    for f in sorted((ROOT / "migrations").glob("v*.sql"), key=lambda p: int(p.name[1:].split("_")[0])):
        r = psql("tmpl", sql_file=f)
        if r.returncode != 0:
            print(f"FAIL: {f.name} did not apply: {r.stderr[-300:]}")
            sys.exit(1)
    tables = int(psql("tmpl", "-c", "SELECT count(*) FROM pg_tables WHERE schemaname = 'public'").stdout)
    check(f"the real schema is built ({tables} tables)", tables > 100, tables)
    psql("postgres", "-c", "CREATE DATABASE sr TEMPLATE tmpl")

    async def go():
        pool = await asyncpg.create_pool(host=str(sock), user="entity", database="sr", min_size=1, max_size=4)
        try:
            await main(pool)
        finally:
            await pool.close()
    asyncio.run(go())
finally:
    subprocess.run(["docker", "stop", name], capture_output=True)   # --rm removes it
    shutil.rmtree(work, ignore_errors=True)

print()
if FAILED:
    print(f"FAILED: {len(FAILED)}")
    sys.exit(1)
print("OK: the saved request's SQL holds on the real schema")
