#!/usr/bin/env python3
"""A Kill records WHO pressed it.

Found on one Host. The person stopped a
build with Kill. POST /api/review/sessions/{id}/kill recorded by="user" and
no identity, and nothing reached the task record agent_status reads: its
cancelled_by was empty, so the Host could only say "someone may have pressed
stop".

The caller is read the way this kernel's other write routes read it:
server/operator_identity.request_email (the X-Authentik-Email the direct
router verifies and injects; routes/vault.py, routes/browser_agents.py).

What this pins:
  1. A kill through the route puts the caller's email on the run's
     status/killed frame (`by_email`), next to `by`, which stays 'user'.
  2. ...and on the factory build's task record as result.cancelled_by
     ("user:<email>"), the field agent_dispatch._row_to_agent hands
     agent_status.
  3. A kill before the run started records the caller the same way.
  4. A Host kill (no person) records "host"; a person with no identity on
     the request records "user".
  5. A kill that stopped nothing writes nothing.
  6. A kill of a whole compare records the caller on its fan_out row, found
     by result.session_id (tools._fan_out_task_start).
  7. A kill of one compare variant writes no row (the compare runs on); the
     variant's frame carries the caller.

Container-free: FastAPI's TestClient on the review router, a stub app token,
the pod stop, frame writer and pool stubbed.

Run: APP_ROOT=<repo> python3 scripts/test_kill_records_who_pressed_it.py
"""
import asyncio
import json
import logging
import os
import pathlib
import sys
from types import SimpleNamespace

ROOT = pathlib.Path(os.environ.get("APP_ROOT") or pathlib.Path(__file__).resolve().parent.parent)
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "brain"))
logging.disable(logging.CRITICAL)

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from server.routes import review as rv  # noqa: E402

FAILS: list = []


def check(label, cond, detail=""):
    print(f"  {'ok  ' if cond else 'FAIL'} {label}" + ("" if cond else f"  [{str(detail)[:400]}]"))
    if not cond:
        FAILS.append(label)


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


FRAMES: list = []


async def _fake_pod_stop(harness):
    return {"harness": harness, "ok": True, "held": True, "stopped": 1, "could_not_stop": 0}


def _fake_frame(session_dir, label, kind, payload):
    FRAMES.append((pathlib.Path(session_dir).name, label, kind, dict(payload)))


class _Pool:
    def __init__(self):
        self.executed: list = []

    async def fetchval(self, q, *args):
        return None          # no project to halt

    async def execute(self, q, *args):
        self.executed.append((" ".join(q.split()), args))
        return "UPDATE 1"

    def task_writes(self):
        return [(q, a) for q, a in self.executed
                if "UPDATE entity_tasks" in q and "cancelled_by" in json.dumps(a, default=str)]


def _ids(args):
    first = args[0] if args else None
    return first if isinstance(first, list) else [first]


def _written(args) -> dict:
    """The JSON object a task write merges into result."""
    for a in args:
        if isinstance(a, str):
            try:
                v = json.loads(a)
            except ValueError:
                continue
            if isinstance(v, dict):
                return v
    return {}


def _reset():
    rv._live_pod_runs.clear()
    rv._kill_locks.clear()
    rv._build_prep.clear()
    rv._kill_before_start.clear()
    FRAMES.clear()


app = FastAPI()
app.include_router(rv.router)


@app.middleware("http")
async def _window_token(request, call_next):
    # What ApplicationTokenMiddleware attaches for a Workbench window.
    request.state.app_token = SimpleNamespace(app_id="workbench", permissions=["write:review"],
                                              origin_key="")
    return await call_next(request)


S = pathlib.Path("/var/lib/cseries/review-sessions")
EMAIL = "alex@example.test"
saved = (rv._pod_stop, rv._append_run_frame, rv.brain_service.get_pool, rv.brain_service._entity_id)
pool = _Pool()
rv._pod_stop, rv._append_run_frame = _fake_pod_stop, _fake_frame
rv.brain_service.get_pool = lambda: pool
rv.brain_service._entity_id = 9003
try:
    with TestClient(app) as client:
        print("== 1-2. a kill through the route records the caller ==")
        _reset()
        entry = rv._register_live_run("claude-code", S / "factory-build-433", "claude-code")
        r = client.post("/api/review/sessions/factory-build-433/kill",
                        headers={"X-Authentik-Email": EMAIL})
        body = r.json() if r.status_code == 200 else {"text": r.text}
        check("the route answered and stopped the run",
              r.status_code == 200 and [s["session_id"] for s in body.get("stopped", [])]
              == ["factory-build-433"], body)
        check("the run is marked killed by the user (the category is unchanged)",
              entry["killed_by"] == "user", entry)
        killed = [f for f in FRAMES if f[2] == "status" and f[3].get("state") == "killed"]
        check("the status/killed frame names the caller",
              len(killed) == 1 and killed[0][3].get("by") == "user"
              and killed[0][3].get("by_email") == EMAIL, FRAMES)
        w = pool.task_writes()
        check("the task record gets cancelled_by = the caller",
              len(w) == 1 and _written(w[0][1]).get("cancelled_by") == f"user:{EMAIL}", pool.executed)
        check("...on task 433, this Host's factory build row only",
              bool(w) and 433 in _ids(w[0][1]) and 9003 in w[0][1]
              and "task_type = 'factory_build'" in w[0][0], w)
        rv._unregister_live_run("claude-code", entry)

        print("== 3. a kill before the run started records the caller too ==")
        _reset()
        pool.executed.clear()
        rv.build_prep_open("factory-build-436", "claude-code")
        r = client.post("/api/review/sessions/factory-build-436/kill",
                        headers={"X-Authentik-Email": EMAIL})
        check("stopped before it started", r.status_code == 200
              and (r.json().get("stopped") or [{}])[0].get("not_started") is True, r.text)
        w = pool.task_writes()
        check("the task record gets cancelled_by = the caller",
              len(w) == 1 and _written(w[0][1]).get("cancelled_by") == f"user:{EMAIL}"
              and 436 in _ids(w[0][1]), pool.executed)

        print("== 4. no person, or no identity on the request ==")
        _reset()
        pool.executed.clear()
        e2 = rv._register_live_run("claude-code", S / "factory-build-437", "claude-code")
        run(rv.kill_session_runs("factory-build-437", by="host"))
        w = pool.task_writes()
        check("a Host kill records cancelled_by 'host'",
              len(w) == 1 and _written(w[0][1]).get("cancelled_by") == "host", pool.executed)
        rv._unregister_live_run("claude-code", e2)
        _reset()
        pool.executed.clear()
        e3 = rv._register_live_run("claude-code", S / "factory-build-438", "claude-code")
        r = client.post("/api/review/sessions/factory-build-438/kill")
        w = pool.task_writes()
        check("a kill with no identity on the request records 'user'",
              r.status_code == 200 and len(w) == 1
              and _written(w[0][1]).get("cancelled_by") == "user", pool.executed)
        killed = [f for f in FRAMES if f[2] == "status" and f[3].get("state") == "killed"]
        check("...and its frame carries no by_email",
              bool(killed) and "by_email" not in killed[0][3], FRAMES)
        rv._unregister_live_run("claude-code", e3)

        print("== 5. a kill that stopped nothing writes nothing ==")
        _reset()
        pool.executed.clear()
        r = client.post("/api/review/sessions/factory-build-439/kill",
                        headers={"X-Authentik-Email": EMAIL})
        check("nothing running: an ordinary answer, no task write",
              r.status_code == 200 and r.json().get("stopped") == [] and pool.task_writes() == [],
              (r.text, pool.executed))

        print("== 6. a whole-compare kill records the caller on the compare's row ==")
        _reset()
        pool.executed.clear()
        c1 = rv._register_live_run("claude-code", S / "fanout-7f3a", "claude-code")
        c2 = rv._register_live_run("codex", S / "fanout-7f3a", "codex")
        r = client.post("/api/review/sessions/fanout-7f3a/kill",
                        headers={"X-Authentik-Email": EMAIL})
        w = pool.task_writes()
        check("both variants stopped", r.status_code == 200
              and sorted(s["harness"] for s in r.json().get("stopped") or []) == ["claude-code", "codex"],
              r.text)
        check("one write: the fan_out row matched by its session id, cancelled_by = the caller",
              len(w) == 1 and "task_type = 'fan_out'" in w[0][0]
              and "result->>'session_id'" in w[0][0] and w[0][1][0] == "fanout-7f3a"
              and 9003 in w[0][1] and _written(w[0][1]).get("cancelled_by") == f"user:{EMAIL}",
              pool.executed)
        rv._unregister_live_run("claude-code", c1)
        rv._unregister_live_run("codex", c2)

        print("== 7. a one-variant kill leaves the compare's row alone ==")
        _reset()
        pool.executed.clear()
        c1 = rv._register_live_run("claude-code", S / "fanout-7f3b", "claude-code")
        c2 = rv._register_live_run("codex", S / "fanout-7f3b", "codex")
        r = client.post("/api/review/sessions/fanout-7f3b/kill", json={"harness": "codex"},
                        headers={"X-Authentik-Email": EMAIL})
        check("only the named variant stopped", r.status_code == 200
              and [s["harness"] for s in r.json().get("stopped") or []] == ["codex"], r.text)
        check("no task write: the compare is still running", pool.task_writes() == [],
              pool.executed)
        killed = [f for f in FRAMES if f[2] == "status" and f[3].get("state") == "killed"]
        check("...and the variant's frame names the caller",
              len(killed) == 1 and killed[0][1] == "codex" and killed[0][3].get("by_email") == EMAIL,
              FRAMES)
        rv._unregister_live_run("claude-code", c1)
        rv._unregister_live_run("codex", c2)
finally:
    (rv._pod_stop, rv._append_run_frame, rv.brain_service.get_pool,
     rv.brain_service._entity_id) = saved
    _reset()

print()
if FAILS:
    print(f"FAILED: {len(FAILS)} check(s)")
    for f in FAILS:
        print(f"  - {f}")
    sys.exit(1)
print("all checks passed")
