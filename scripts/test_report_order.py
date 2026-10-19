"""Reports land in a thread in the order that makes them true.

Project 9239: a build's report went through a ~9 s
phrasing call while the proof its auto-ship started was fixed text,
so the thread read "installed and PROVEN running" and THEN, from the build,
"the app isn't installed". Two rules now hold, both in AgentDispatch._deliver:

  * a proof carries origin.after_task_id and its report waits for that
    build's report to be written, even when the proof finishes first;
  * reports into one thread are written one at a time, in claim order.

Runs the real _deliver against an in-memory pool and a fake runtime.
"""
import asyncio
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path[:0] = [os.path.join(ROOT, "brain"), ROOT]
import mastercode.runtime.agent_dispatch as ad  # noqa: E402

failures = []


def check(name, ok, detail=""):
    print(("PASS  " if ok else "FAIL  ") + name + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        failures.append(name)


class Pool:
    """Just the queries _deliver makes, over a dict of task rows."""

    def __init__(self, rows):
        self.rows = rows

    async def fetchrow(self, sql, task_id):
        r = self.rows.get(task_id)
        if r is None:
            return None
        return {**r, "result": json.dumps(r["result"])}

    async def fetchval(self, sql, task_id):
        r = self.rows.get(task_id)
        if "RETURNING task_id" in sql:  # the atomic claim
            if r is None or "delivered_at" in r["result"] or r["status"] not in ("completed", "failed"):
                return None
            r["result"]["delivered_at"] = "now"
            return task_id
        if "result->'origin'" in sql:
            return None if r is None else json.dumps(r["result"].get("origin"))
        if "? 'delivered_at'" in sql:
            return None if r is None else ("delivered_at" in r["result"] or "origin" not in r["result"])
        return None

    async def execute(self, sql, *a):
        if "result - 'delivered_at'" in sql:   # the un-claim
            self.rows[a[0]]["result"].pop("delivered_at", None)
        return None


class Runtime:
    def __init__(self, slow):
        self.slow, self.written = slow, []

    async def deliver_agent_result(self, record):
        await asyncio.sleep(self.slow.get(record["task_id"], 0))
        self.written.append(record["task_id"])


def row(task_id, task_type, origin):
    return {"task_id": task_id, "entity_id": 9003, "title": f"t{task_id}", "description": "",
            "task_type": task_type, "status": "completed", "error": None,
            "result": {"origin": origin}}


WB = {"app_id": "workbench", "project_id": 9239}


async def scenario(proof_first: bool, after: bool):
    rows = {404: row(404, "factory_build", WB),
            405: row(405, "app_verify", {**WB, **({"after_task_id": 404} if after else {})})}
    d = ad.AgentDispatch(Pool(rows))
    d.runtime = Runtime({404: 0.3})          # the build's phrasing call is slow
    d._maybe_continue_round = lambda *a, **k: asyncio.sleep(0)
    if proof_first:
        t405 = asyncio.create_task(d._deliver(405))
        await asyncio.sleep(0.05)            # the proof is ahead by a clear margin
        t404 = asyncio.create_task(d._deliver(404))
    else:
        t404 = asyncio.create_task(d._deliver(404))
        await asyncio.sleep(0.01)
        t405 = asyncio.create_task(d._deliver(405))
    await asyncio.gather(t404, t405)
    return d.runtime.written, d


written, _ = asyncio.run(scenario(proof_first=False, after=False))
check("claim order holds in one thread: the slow build first, the fast proof after",
      written == [404, 405], str(written))

written, d = asyncio.run(scenario(proof_first=True, after=True))
check("a proof that finishes FIRST still follows its build's report", written == [404, 405], str(written))
check("nothing is left waiting or marked in flight",
      not d._delivering and not d._delivered_ev, f"{d._delivering} {d._delivered_ev}")


async def unrelated():
    rows = {1: row(1, "factory_build", {"app_id": "workbench", "project_id": 1}),
            2: row(2, "factory_build", {"app_id": "workbench", "project_id": 2})}
    d = ad.AgentDispatch(Pool(rows))
    d.runtime = Runtime({1: 0.3})
    d._maybe_continue_round = lambda *a, **k: asyncio.sleep(0)
    t1 = asyncio.create_task(d._deliver(1))
    await asyncio.sleep(0.01)
    t2 = asyncio.create_task(d._deliver(2))
    await asyncio.gather(t1, t2)
    return d.runtime.written


check("different threads never wait on each other", asyncio.run(unrelated()) == [2, 1])


async def already_written():
    rows = {404: row(404, "factory_build", WB), 405: row(405, "app_verify", {**WB, "after_task_id": 404})}
    rows[404]["result"]["delivered_at"] = "earlier"
    d = ad.AgentDispatch(Pool(rows))
    d.runtime = Runtime({})
    d._maybe_continue_round = lambda *a, **k: asyncio.sleep(0)
    await asyncio.wait_for(d._deliver(405), timeout=2)
    return d.runtime.written


check("a proof whose build was already reported does not wait", asyncio.run(already_written()) == [405])

async def boot_reap():
    # Restart after build 404 finished (unreported) and proof 405 opened: the
    # reap delivers 405 before the monitor exists. It must not wait out its
    # bound, and 404's report still comes first.
    rows = {404: row(404, "factory_build", WB), 405: row(405, "app_verify", {**WB, "after_task_id": 404})}
    d = ad.AgentDispatch(Pool(rows))
    d.runtime = Runtime({})
    d._maybe_continue_round = lambda *a, **k: asyncio.sleep(0)
    t0 = asyncio.get_running_loop().time()
    await asyncio.wait_for(d._deliver(405, deliver_prior=True), timeout=5)
    return d.runtime.written, asyncio.get_running_loop().time() - t0, d


written, took, d = asyncio.run(boot_reap())
check("boot reap: the proof delivers its build's report first, without waiting",
      written == [404, 405] and took < 1 and not d._delivered_ev, f"{written} {took:.2f}s")


async def cancelled_in_line():
    # A report cancelled while it waits for its thread gives its claim back,
    # so the monitor delivers it later; nothing stays marked in flight.
    rows = {1: row(1, "factory_build", WB), 2: row(2, "factory_build", WB)}
    d = ad.AgentDispatch(Pool(rows))
    d.runtime = Runtime({1: 0.5})
    d._maybe_continue_round = lambda *a, **k: asyncio.sleep(0)
    t1 = asyncio.create_task(d._deliver(1))
    await asyncio.sleep(0.05)
    t2 = asyncio.create_task(d._deliver(2))
    await asyncio.sleep(0.05)
    t2.cancel()
    await asyncio.gather(t1, t2, return_exceptions=True)
    return rows, d


rows, d = asyncio.run(cancelled_in_line())
check("a report cancelled in line gives its claim back and leaves nothing in flight",
      "delivered_at" in rows[1]["result"] and "delivered_at" not in rows[2]["result"]
      and not d._delivering and not d._thread_locks[ad.delivery_thread_key(WB)].locked(),
      f"{rows[2]['result']} {d._delivering}")

async def cancelled_after_write():
    # Interrupted AFTER the report row was written (the inbox/broadcast tail):
    # the claim stays, so the monitor does not post the report a second time.
    rows = {7: row(7, "factory_build", WB)}
    pool = Pool(rows)
    written = set()
    orig = pool.fetchval

    async def fetchval(sql, task_id):
        if "FROM entity_memory" in sql:
            return 1 if int(task_id) in written else None
        return await orig(sql, task_id)
    pool.fetchval = fetchval
    d = ad.AgentDispatch(pool)

    class R:
        async def deliver_agent_result(self, record):
            written.add(record["task_id"])
            raise asyncio.CancelledError()
    d.runtime = R()
    try:
        await d._deliver(7)
    except asyncio.CancelledError:
        pass
    return rows


rows = asyncio.run(cancelled_after_write())
check("a report interrupted after its row was written keeps its claim (no second copy)",
      "delivered_at" in rows[7]["result"], str(rows[7]["result"]))

check("thread key reads a JSON-string origin (older rows)",
      ad.delivery_thread_key(json.dumps(WB)) == ad.delivery_thread_key(WB) == ("workbench", 9239, None))

src = open(os.path.join(ROOT, "brain/mastercode/runtime/tools.py")).read()
check("auto-ship's proof origin names the build it follows", '"after_task_id": task_id}' in src)

print("all gates passed" if not failures else f"{len(failures)} failing")
sys.exit(1 if failures else 0)
