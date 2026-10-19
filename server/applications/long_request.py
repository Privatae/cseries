"""A request a PERSON makes that can outlast the router answers 202 + a job.

Every request a person's browser makes to their Host passes the direct
router, which waits at most 30 s for the Host's response HEAD and answers
502 past that (the router's REQUEST_READ_TIMEOUT = 30.0, applied to the
upstream response). The router's limit protects the router and stays.
So a route whose work can take longer must answer before then.

run() starts the work as its own task and waits BUDGET_S for it:
  - finished in time: the ordinary answer, exactly as before (and an
    exception raised in time propagates exactly as if the route ran inline);
  - not finished: 202 {pending, job, poll, message}; the work carries on, and
    its answer — the result, or the HTTP refusal it raised — lands on the job
    for JOB_TTL_S. The client polls `poll` until {status: done|failed}.

A job belongs to an AREA, and each area's poll route reads only its own
area's jobs, under the same permission its doors need — so a job is never
readable with a lesser scope than the request that started it. The task is
detached from the request: a person who closes the tab does not cancel the
backup, publish or desktop change they asked for.

Callers that never pass the router (the Host's own tool calls, in-kernel)
call the work directly and wait.
"""
from __future__ import annotations

import asyncio
import secrets
import time
from typing import Any, Awaitable, Callable, Optional

ROUTER_WINDOW_S = 30.0
# Inside the router's window with room for the rest of the request path.
BUDGET_S = 20.0
# How long a FINISHED job's answer stays pollable. A job that is still
# running never expires: a backup, publish or desktop change that takes longer
# than this kept running while its poll answered "unknown" and its result was
# written to a job nobody could read any more. The clock
# starts when the work ends.
JOB_TTL_S = 900.0

_JOBS: dict[str, dict] = {}
_TASKS: set = set()


def _expire() -> None:
    now = time.monotonic()
    for k in [k for k, j in _JOBS.items()
              if j.get("status") != "running" and j.get("expires", now) < now]:
        _JOBS.pop(k, None)


def _record(job: dict, task: "asyncio.Task") -> None:
    job["expires"] = time.monotonic() + JOB_TTL_S
    if task.cancelled():
        # CancelledError is not an Exception: without this a cancelled job
        # (the kernel shutting down, a caller cancelling it) raised out of this
        # callback and stayed "running" forever — and a running job never
        # expires.
        job.update(status="failed", http_status=503,
                   detail=f"{job.get('what') or 'this request'} was stopped before it "
                          "finished (cancelled); nothing was answered — run it again")
        return
    try:
        job.update(status="done", result=task.result())
    except Exception as e:  # noqa: BLE001 — the refusal IS the answer
        code = getattr(e, "status_code", None) or getattr(e, "status", None) or 500
        try:
            code = int(code)
        except (TypeError, ValueError):
            code = 500
        job.update(status="failed", http_status=code,
                   detail=getattr(e, "detail", None) or str(e))


def start(what: str, work: Callable[[], Awaitable[Any]], *, area: str) -> tuple[str, "asyncio.Task"]:
    """Run `work()` (a coroutine factory) detached, registered as a job of
    `area`."""
    _expire()
    job_id = secrets.token_urlsafe(18)
    job = {"what": what, "area": area, "status": "running"}
    _JOBS[job_id] = job
    task = asyncio.ensure_future(work())
    _TASKS.add(task)
    task.add_done_callback(_TASKS.discard)
    task.add_done_callback(lambda t, j=job: _record(j, t))
    return job_id, task


def pending(job_id: str, *, poll: str, message: str) -> dict:
    return {"pending": True, "job": job_id, "poll": poll, "message": message}


async def run(what: str, work: Callable[[], Awaitable[Any]], *, area: str, poll: str,
              message: str, budget_s: Optional[float] = None):
    """The answer within budget, or JSONResponse(202, pending(...)). `poll` is
    a format string with {job} for the area's poll route."""
    from starlette.responses import JSONResponse
    job_id, task = start(what, work, area=area)
    done, _ = await asyncio.wait({task}, timeout=BUDGET_S if budget_s is None else budget_s)
    if done:
        _JOBS.pop(job_id, None)
        return task.result()
    return JSONResponse(status_code=202, content=pending(
        job_id, poll=poll.format(job=job_id), message=message))


def status(job_id: str, *, area: str) -> Optional[dict]:
    """{status: running} | {status: done, result} | {status: failed,
    http_status, detail} — None for an unknown or expired job, or one that
    belongs to another area."""
    _expire()
    job = _JOBS.get(str(job_id or ""))
    if job is None or job.get("area") != area:
        return None
    return {k: v for k, v in job.items() if k not in ("expires", "area")}
