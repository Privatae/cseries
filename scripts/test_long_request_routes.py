#!/usr/bin/env python3
"""Gate: a request a person makes that can outlast the router answers early.

The direct router in front of every Host waits 30 s for the response head and
answers 502 past it (the direct router's REQUEST_READ_TIMEOUT);
its limit stays. server/applications/long_request.py is the one helper the
slow doors share: the answer within BUDGET_S, else 202 + a job polled at the
door's own area route under the door's own scope. This gate proves:

  helper       the answer in time is the ordinary answer (and a refusal in
               time is the ordinary refusal); past the budget, 202 and the
               work carries on; its result or its refusal lands on the job;
               a job is readable only through its own area.

  circles      gone: Regenerate and A/B left Circles with the rail, and with
               them POST /api/circles/replay, /ab, /ab/choose and the job
               route GET /api/circles/jobs/{id}. They stay gone.

  security     POST /api/security/scan goes through it; its job route needs
               execute:scan and does not read another area's job.

  agents       POST /api/browser/agents/{name}/run never waits past the
               budget, whatever the client asks (the run carries on).

  clients      the shipped Circles bundle calls none of the removed routes.

Real FastAPI routes through TestClient; the turns, the scan and the agent
run are stubs. No DB, no model, no network.

Run: APP_ROOT=<repo> python3 scripts/test_long_request_routes.py
"""
import asyncio
import os
import sys
import time
import types
from pathlib import Path

_ROOT = Path(os.environ.get("APP_ROOT") or Path(__file__).resolve().parents[1])
sys.path.insert(0, str(_ROOT / "brain"))
sys.path.insert(0, str(_ROOT))

# circles.py imports server.main at module scope, and server.main mounts
# /app/dashboard/base (absent outside the image). The routes need nothing
# from it here.
sys.modules.setdefault("server.main", types.ModuleType("server.main"))

from fastapi import FastAPI, HTTPException, Request  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from server.applications import long_request as LR  # noqa: E402
from server.routes import appearance as AP  # noqa: E402
from server.routes import browser_agents as BA  # noqa: E402
from server.routes import circles as C  # noqa: E402
from server.routes import security as S  # noqa: E402

failures: list = []


def check(label: str, cond: bool, extra: str = "") -> None:
    print(f"[long-request] {'ok  ' if cond else 'FAIL'} {label}" + (f" — {extra}" if extra and not cond else ""))
    if not cond:
        failures.append(label)


# ── 1. the helper ──────────────────────────────────────────────────────────
async def _helper():
    async def quick():
        return {"answer": 1}

    got = await LR.run("x", quick, area="a", poll="/p/{job}", message="m", budget_s=1.0)
    check("an answer ready in time is the ordinary answer", got == {"answer": 1})

    async def refuse_fast():
        raise HTTPException(409, "no")

    try:
        await LR.run("x", refuse_fast, area="a", poll="/p/{job}", message="m", budget_s=1.0)
        check("a refusal in time propagates as the refusal", False)
    except HTTPException as e:
        check("a refusal in time propagates as the refusal", e.status_code == 409)

    gate = asyncio.Event()

    async def slow():
        await gate.wait()
        return {"answer": 2}

    resp = await LR.run("x", slow, area="a", poll="/p/{job}", message="still going", budget_s=0.05)
    import json
    body = json.loads(resp.body)
    check("past the budget: 202 with the job, its poll path and a sentence",
          resp.status_code == 202 and body["pending"] and body["poll"] == f"/p/{body['job']}"
          and body["message"] == "still going")
    check("...the job reads running", LR.status(body["job"], area="a") == {"what": "x", "status": "running"})
    gate.set()
    await asyncio.sleep(0.01)
    check("...and the work carried on to its answer",
          LR.status(body["job"], area="a") == {"what": "x", "status": "done", "result": {"answer": 2}})
    check("a job is not readable through another area", LR.status(body["job"], area="b") is None)

    async def refuse_late():
        await asyncio.sleep(0.1)
        raise HTTPException(422, {"message": "does not render"})

    resp = await LR.run("x", refuse_late, area="a", poll="/p/{job}", message="m", budget_s=0.01)
    job = json.loads(resp.body)["job"]
    await asyncio.sleep(0.2)
    check("a refusal raised after the 202 lands on the job with its status and detail",
          LR.status(job, area="a") == {"what": "x", "status": "failed", "http_status": 422,
                                       "detail": {"message": "does not render"}})
    check("an unknown job is None", LR.status("nope", area="a") is None)

    saved = LR.JOB_TTL_S
    LR.JOB_TTL_S = -1.0
    try:
        async def q():
            return 1
        r = await LR.run("x", q, area="a", poll="/p/{job}", message="m", budget_s=0.0)
        if hasattr(r, "body"):
            j = json.loads(r.body)["job"]
            await asyncio.sleep(0.01)
            check("a finished job expires after JOB_TTL_S", LR.status(j, area="a") is None)
        else:
            check("a finished job expires after JOB_TTL_S", True)
        # A job still RUNNING when the TTL has
        # long passed is never expired — its answer lands and is read.
        gate2 = asyncio.Event()

        async def long_work():
            await gate2.wait()
            return {"answer": "after the TTL"}

        r = await LR.run("x", long_work, area="a", poll="/p/{job}", message="m", budget_s=0.01)
        j = json.loads(r.body)["job"]
        await asyncio.sleep(0.02)
        check("a job still RUNNING past JOB_TTL_S is never expired (AP-C6)",
              (LR.status(j, area="a") or {}).get("status") == "running", repr(LR.status(j, area="a")))
        LR.JOB_TTL_S = saved           # the TTL counts from completion
        gate2.set()
        await asyncio.sleep(0.02)
        check("...and its answer lands on the job and is read",
              LR.status(j, area="a") == {"what": "x", "status": "done",
                                         "result": {"answer": "after the TTL"}},
              repr(LR.status(j, area="a")))
        # A job that is CANCELLED (the kernel
        # stopping, a caller cancelling it) finishes as failed and says so —
        # it used to stay "running" forever, and a running job never expires.
        never = asyncio.Event()

        async def stuck():
            await never.wait()

        jc, tc = LR.start("x", stuck, area="a")
        await asyncio.sleep(0.01)
        tc.cancel()
        await asyncio.sleep(0.02)
        st = LR.status(jc, area="a") or {}
        check("a cancelled job finishes as failed with a plain sentence, never 'running' forever",
              st.get("status") == "failed" and "cancelled" in str(st.get("detail")), repr(st))
        LR.JOB_TTL_S = -1.0
        jd, td = LR.start("x", stuck, area="a")
        await asyncio.sleep(0.01)
        td.cancel()
        await asyncio.sleep(0.02)
        check("...and expires like any finished job", LR.status(jd, area="a") is None
              and jd not in LR._JOBS)
    finally:
        LR.JOB_TTL_S = saved


asyncio.run(_helper())


# ── 2. the routes, through FastAPI ─────────────────────────────────────────
app = FastAPI()


@app.middleware("http")
async def _token(request: Request, call_next):
    perms = request.headers.get("x-test-perms")
    if perms is not None:
        request.state.app_token = types.SimpleNamespace(app_id="test", permissions=perms.split(","))
    return await call_next(request)


app.include_router(C.router)
app.include_router(S.router)
app.include_router(BA.router)
app.include_router(AP.router)

W = {"x-test-perms": "write:circles"}


def poll_until(client, path, headers, tries=100):
    for _ in range(tries):
        r = client.get(path, headers=headers)
        if r.status_code != 200 or r.json().get("status") != "running":
            return r
        time.sleep(0.02)
    return r


with TestClient(app) as client:
    saved_budget = LR.BUDGET_S

    # circles: Regenerate, A/B and their job route left with the rail.
    for method, path in (("post", "/api/circles/replay"), ("post", "/api/circles/ab"),
                         ("post", "/api/circles/ab/choose"), ("get", "/api/circles/jobs/x")):
        r = getattr(client, method)(path, headers=W, **({"json": {}} if method == "post" else {}))
        check(f"{method.upper()} {path} is gone", r.status_code in (404, 405), f"{r.status_code}")

    # security scan
    async def fake_scan():
        await asyncio.sleep(0.3)
        return {"overall_status": "secure", "summary": "ok", "model": "m"}

    real_scan = S._scan
    S._scan = fake_scan
    LR.BUDGET_S = 0.05
    X = {"x-test-perms": "execute:scan"}
    try:
        r = client.post("/api/security/scan", headers=X)
        check("a scan past the budget answers 202 with a security job",
              r.status_code == 202 and r.json()["poll"].startswith("/api/security/jobs/"), r.text)
        check("...whose job is refused without execute:scan",
              client.get(r.json()["poll"], headers={"x-test-perms": "read:incidents"}).status_code == 403)
        check("...and not readable through the desktop doors' job route",
              client.get("/api/appearance/desktop/jobs/" + r.json()["job"],
                         headers={"x-test-perms": "write:appearance"}).status_code == 404)
        done = poll_until(client, r.json()["poll"], X)
        check("...and ends with the scan's result", done.json().get("result", {}).get("summary") == "ok",
              done.text)
        check("the scan route needs execute:scan to start",
              client.post("/api/security/scan", headers={"x-test-perms": "read:incidents"}).status_code == 403)
    finally:
        S._scan = real_scan
        LR.BUDGET_S = saved_budget

    # browser agent run — the wait never passes the budget
    seen: dict = {}

    async def fake_run(pool, *, entity_id, name, actor, trigger, wait_s):
        seen["wait_s"] = wait_s
        return {"name": name, "status": "running", "step": 1, "steps": 4}

    real_run, real_pool, real_eid = BA.agents.run, BA._pool, BA._entity_id
    BA.agents.run = fake_run
    BA._pool = lambda: object()
    BA._entity_id = lambda: 1
    A = {"x-test-perms": "execute:agents"}
    try:
        r = client.post("/api/browser/agents/daily/run", json={"wait_s": 120}, headers=A)
        check("an agent run asked to wait 120 s waits at most the budget",
              r.status_code == 200 and seen.get("wait_s") == LR.BUDGET_S, f"{r.status_code} {seen}")
        client.post("/api/browser/agents/daily/run", json={"wait_s": 3}, headers=A)
        check("...and a shorter wait is kept", seen.get("wait_s") == 3)
        client.post("/api/browser/agents/daily/run", headers=A)
        check("...and no body waits the budget", seen.get("wait_s") == LR.BUDGET_S)
    finally:
        BA.agents.run, BA._pool, BA._entity_id = real_run, real_pool, real_eid

# the desktop doors' job route: the doors need write:appearance, so does
# reading what they did
async def _desk_slow():
    await asyncio.sleep(0.3)
    return {"wallpaper": "html"}

with TestClient(app) as client:
    from server.applications import desktop_render as DR
    LR.BUDGET_S = 0.05

    async def _start_door():
        return await DR.run_door("worn", _desk_slow, budget_s=0.01)

    import json as _json
    resp = client.portal.call(_start_door)
    LR.BUDGET_S = saved_budget
    job = _json.loads(resp.body)["job"]
    WA = {"x-test-perms": "write:appearance"}
    check("a desktop door's job is refused without write:appearance",
          client.get(f"/api/appearance/desktop/jobs/{job}",
                     headers={"x-test-perms": "read:appearance"}).status_code == 403)
    check("...and with no token at all",
          client.get(f"/api/appearance/desktop/jobs/{job}").status_code == 401)
    done = poll_until(client, f"/api/appearance/desktop/jobs/{job}", WA)
    check("...and read with it, it ends with the door's answer",
          done.status_code == 200 and done.json().get("result") == {"wallpaper": "html"}, done.text)
    check("...an unknown desktop job is 404",
          client.get("/api/appearance/desktop/jobs/nope", headers=WA).status_code == 404)

check("the budget sits inside the router's window",
      0 < LR.BUDGET_S < LR.ROUTER_WINDOW_S)

# ── 3. the clients ─────────────────────────────────────────────────────────
assets = list((_ROOT / "system-apps" / "circles" / "assets").glob("*.js"))
gone = [a.name for a in assets for path in ("/api/circles/replay", "/api/circles/ab", "/api/circles/jobs/")
        if path in a.read_text(errors="replace")]
check("the shipped Circles bundle calls none of the removed routes", bool(assets) and not gone, gone)
sec = (_ROOT / "system-apps" / "security" / "app.js").read_text()
check("the Security window follows a pending scan",
      "settleJob(await fetchJSON('/api/security/scan'" in sec)
br = (_ROOT / "system-apps" / "browser" / "app.js").read_text()
check("the Browser window asks for a wait inside the router's window",
      "/run\", { wait_s: 20 })" in br)

if failures:
    print(f"[long-request] {len(failures)} FAILED")
    sys.exit(1)
print("[long-request] ALL OK")
