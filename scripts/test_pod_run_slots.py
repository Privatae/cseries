#!/usr/bin/env python3
"""Variants of one harness run AT ONCE inside its pod, as many as it has slots.

`_harness_group` ran a harness's variants in a serial loop. It now starts them
together and each takes one of the pod's run slots (_pod_slot) for the length
of its run. The operator sizes the pod for `slots` runs on a wake from sleep,
charges the plan for every slot, and every wake answers with what the pod
holds. Unknown = 1, which is the old series.

What this pins, driving the REAL runner (_run_fan_out_background) with the
bridge and the pod faked out:
  * with a 2-slot pod, four variants run two at a time — never more;
  * the wake asks for the variants the plan said it holds (capacity `slots`),
    and with no capacity answer it asks for all of them;
  * an operator that answers no `slots` leaves the group in series (1 at once);
  * a wake refused at the bigger claim is retried for ONE run, and the group
    still finishes, in series;
  * a run that has to wait for a slot says so (queued_for "slot" + a
    `waiting` frame), and none of that shows when there was no wait;
  * a single build and a compare on the same harness share its slots;
  * parallel phase builds on one engine ask for their count and run together,
    and the phase scheduler passes that count down the whole chain;
  * exactly one release per pod window still holds with concurrent runs;
  * the capacity ask carries the per-harness run counts and reads the answer.

Container-free: no bridge, no operator, no pod.

Run: APP_ROOT=<repo> python3 scripts/test_pod_run_slots.py
"""
import asyncio
import json
import pathlib
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from server.routes import review as rv  # noqa: E402

FAILURES = []


def check(name, cond, detail=""):
    if cond:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name} {detail}")
        FAILURES.append(name)


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


class _Pod:
    """The operator + the pod: answers wakes with a slot count, and counts
    how many runs are inside the pod at once."""

    def __init__(self, answer_slots=True, refuse_over=None, hold_s=0.05):
        self.answer_slots = answer_slots
        self.refuse_over = refuse_over     # refuse a wake asking more slots than this
        self.hold_s = hold_s
        self.size = None                   # slots the pod was sized for at its cold wake
        self.wakes = []                    # the `slots` each wake asked for
        self.releases = 0
        self.inside = 0
        self.max_inside = 0
        self.ran = []

    async def bridge(self, op, harness_id, **params):
        if op == "harness.release":
            self.releases += 1
            self.size = None
            return {"ok": True, "ready_duration_s": 1.0}
        if op == "harness.wake":
            ask = params.get("slots")
            self.wakes.append(ask)
            if self.refuse_over is not None and (ask or 1) > self.refuse_over:
                return {"ok": False, "error": "operator /harness/wake 402: over plan"}
            if self.size is None:          # cold: sized now
                self.size = ask or 1
            out = {"ok": True, "url": "http://pod:8840"}
            if self.answer_slots:
                out["slots"] = self.size
            return out
        return {"ok": True}

    async def run_on_pod(self, http, base, harness_id, label, *a, **k):
        self.inside += 1
        self.max_inside = max(self.max_inside, self.inside)
        await asyncio.sleep(self.hold_s)
        self.inside -= 1
        self.ran.append(label)
        return {"ok": True, "harness": label, "elapsed_seconds": self.hold_s,
                "diff": "", "collected": False}


async def _noop(*a, **k):
    return None


STOPS = []


async def _record_stop(base, harness_id):
    STOPS.append((base, harness_id))


def _fresh(pod):
    rv._harness_holders.clear()
    rv._pod_slots.clear()
    rv._pod_running.clear()
    rv._pod_slot_cond.clear()
    rv._pod_ready.clear()
    rv._window_shared.clear()
    rv._cap_base.clear()
    rv._woken.clear()
    STOPS.clear()
    rv._harness_bridge = pod.bridge
    rv._run_on_pod = pod.run_on_pod
    rv._collect_session = _noop
    rv._harness_pod_creds = lambda h: None
    rv._stop_leftovers = _record_stop


def _variants(h, n):
    return [{"vid": h if i == 1 else f"{h}-s{i}", "harness": h,
             "model": None, "effort": None} for i in range(1, n + 1)]


async def _fanout(h, n, capacity=None):
    tmp = pathlib.Path(tempfile.mkdtemp())
    sd = tmp / "sess"
    (sd / "diffs").mkdir(parents=True)
    (sd / "manifest.json").write_text("{}")
    vs = _variants(h, n)
    rv._write_session_state(sd, {v["vid"]: {"state": "queued"} for v in vs})
    wts = {}
    for v in vs:
        w = sd / "work" / v["vid"]
        w.mkdir(parents=True)
        wts[v["vid"]] = w
    await rv._run_fan_out_background(
        session_id=sd.name, session_dir=sd, project_dir=tmp, variants=vs,
        worktrees=wts, prompt="p", max_minutes=5, capacity=capacity)
    return sd


def _frames(sd, vid):
    p = sd / "streams" / f"{vid}.jsonl"
    if not p.exists():
        p = next(iter(sd.rglob(f"{vid}*.jsonl")), None)
    if p is None or not p.exists():
        return []
    return [json.loads(x) for x in p.read_text().splitlines() if x.strip()]


print("== a 2-slot pod runs four variants two at a time ==")
pod = _Pod()
_fresh(pod)
sd = run(_fanout("codex", 4, capacity={"fits": ["codex"], "refused": [],
                                        "slots": {"codex": 2}}))
check("all four ran", sorted(pod.ran) == sorted(v["vid"] for v in _variants("codex", 4)), pod.ran)
check("two at once — never more", pod.max_inside == 2, pod.max_inside)
check("every wake asked for the 2 slots the plan holds",
      set(pod.wakes) == {2}, pod.wakes)
check("one release for the window", pod.releases == 1, pod.releases)
state = rv._read_session_state(sd)
check("every variant ended done", all(state[v]["state"] == "done" for v in state), state)
waited = [v for v in state if state[v].get("queued_for") == "slot"
          or "run slot" in (state[v].get("queued_note") or "")]
check("the two that waited for a slot said so", len(waited) == 2, waited)

print("== no capacity answer: ask for every variant at wake ==")
pod = _Pod()
_fresh(pod)
run(_fanout("codex", 3, capacity=None))
check("the wake asked for all three", pod.wakes and pod.wakes[0] == 3, pod.wakes)
check("all three ran at once", pod.max_inside == 3, pod.max_inside)

print("== an operator that predates slots: the old series ==")
pod = _Pod(answer_slots=False)
_fresh(pod)
sd = run(_fanout("codex", 3, capacity=None))
check("one at a time", pod.max_inside == 1, pod.max_inside)
check("all three still ran", len(pod.ran) == 3, pod.ran)

print("== a wake refused at the bigger claim retries for one run ==")
pod = _Pod(refuse_over=1)
_fresh(pod)
sd = run(_fanout("codex", 3, capacity=None))
check("the group asked for 3, then for 1",
      pod.wakes[:2] == [3, None], pod.wakes)
check("...and ran in series", pod.max_inside == 1, pod.max_inside)
state = rv._read_session_state(sd)
check("...and every variant finished", all(state[v]["state"] == "done" for v in state), state)

print("== a pod big enough for everyone: nobody is told to wait ==")
pod = _Pod()
_fresh(pod)
sd = run(_fanout("codex", 3, capacity={"fits": ["codex"], "refused": [],
                                        "slots": {"codex": 3}}))
state = rv._read_session_state(sd)
check("no waiting note on any variant",
      not any(state[v].get("queued_for") == "slot" for v in state), state)

print("== a single build and a compare share the harness's slots ==")
pod = _Pod(hold_s=0.08)
_fresh(pod)


async def _solo_and_compare():
    tmp = pathlib.Path(tempfile.mkdtemp())
    wt = tmp / "solo"
    wt.mkdir()
    import aiohttp
    async with aiohttp.ClientSession() as http:
        solo = rv._dispatch_to_pod_harness(http, "codex", "p", wt, 5, 0.0)
        comp = _fanout("codex", 2, capacity=None)
        await asyncio.gather(solo, comp)


run(_solo_and_compare())
check("three runs happened", len(pod.ran) == 3, pod.ran)
check("never more inside the pod than it was sized for",
      pod.max_inside <= max(w or 1 for w in pod.wakes), (pod.max_inside, pod.wakes))
check("one release for the shared window", pod.releases == 1, pod.releases)

print("== parallel phases on one engine: the builds ask for their count ==")
pod = _Pod(hold_s=0.08)
_fresh(pod)


async def _two_phase_builds():
    import aiohttp
    tmp = pathlib.Path(tempfile.mkdtemp())
    wts = []
    for n in ("a", "b"):
        (tmp / n).mkdir()
        wts.append(tmp / n)
    async with aiohttp.ClientSession() as http:
        await asyncio.gather(*(rv._dispatch_to_pod_harness(
            http, "codex", "p", w, 5, 0.0, slots=2) for w in wts))


run(_two_phase_builds())
check("both builds ran", len(pod.ran) == 2, pod.ran)
check("their wakes asked for 2 slots", set(pod.wakes) == {2}, pod.wakes)
check("...and they ran together", pod.max_inside == 2, pod.max_inside)
check("one release", pod.releases == 1, pod.releases)

pod = _Pod(hold_s=0.05, refuse_over=1)
_fresh(pod)
run(_two_phase_builds())
check("refused at 2 slots, each build asked for one run and they took turns",
      pod.max_inside == 1 and None in pod.wakes and len(pod.ran) == 2,
      (pod.max_inside, pod.wakes, pod.ran))

print("== single runs started in one round: the wake counts every build in flight ==")
# Measured: four build(action='run') calls on project 9239 in one
# round, each passing no slot count. The wake must size the pod from the
# builds running on the engine WHEN it wakes, so the four run together.
pod = _Pod(hold_s=0.08)
_fresh(pod)
_saved_bif = rv._builds_in_flight


async def _four_in_flight(h):
    return 4 if h == "codex" else 0

rv._builds_in_flight = _four_in_flight


async def _four_runs():
    import aiohttp
    tmp = pathlib.Path(tempfile.mkdtemp())
    wts = []
    for n in ("sol", "terra", "luna", "astra"):
        (tmp / n).mkdir()
        wts.append(tmp / n)
    async with aiohttp.ClientSession() as http:
        await asyncio.gather(*(rv._dispatch_to_pod_harness(
            http, "codex", "p", w, 5, 0.0) for w in wts))


try:
    run(_four_runs())
finally:
    rv._builds_in_flight = _saved_bif
check("all four runs ran", len(pod.ran) == 4, pod.ran)
check("the cold wake asked for 4 slots", pod.wakes and pod.wakes[0] == 4, pod.wakes)
check("...and the four ran together", pod.max_inside == 4, pod.max_inside)
check("one release", pod.releases == 1, pod.releases)

pod = _Pod(hold_s=0.02)
_fresh(pod)
_fresh_count = run(rv._builds_in_flight("codex"))
check("no pool: the in-flight count is 0 and the caller's own count stands",
      _fresh_count == 0, _fresh_count)

print("== the phase scheduler hands the count down the whole chain ==")
import ast as _ast
_tools = (ROOT / "brain/mastercode/runtime/tools.py").read_text()
_review = (ROOT / "server/routes/review.py").read_text()
check("the build door claims first, then counts per resolved engine, then launches",
      _tools.count("_per_engine, _keys = await _engine_counts(kw[\"harness\"] for _t, kw in _ready)") == 2
      and _tools.count('_kw["pod_slots"] = _per_engine[_key]') == 2
      and _tools.count("_launch_phase_build(_tid, _kw)") == 3)
check("the auto-advance chain claims without launching, then counts",
      "launch=False)" in _tools and "if not launch:\n            return tid, kwargs" in _tools)
check("a phase with no engine is counted under the engine the build resolves",
      "key = (await resolve_build_target(None))[0]" in _tools)
check("_autoadvance_start_phase -> _run_phase_build carries it",
      "engine_default=engine_default or (harness, model),\n            pod_slots=pod_slots)" in _tools)
check("_run_phase_build -> _pool_build carries it", "pod_slots=pod_slots)" in _tools)
check("_pool_build -> run_single_harness_build carries it",
      "module=module, target=target, slots=pod_slots)" in _tools)
check("run_single_harness_build -> _dispatch_to_pod_harness carries it",
      "session_dir=session_dir, acceptance=acceptance, slots=slots," in _review)

print("== the first wake of a window clears leftovers, once, before any run ==")
pod = _Pod(hold_s=0.03)
_fresh(pod)
order = []
_orig_run = pod.run_on_pod


async def _run_logged(*a, **k):
    order.append("run")
    return await _orig_run(*a, **k)


async def _stop_logged(base, h):
    order.append("stop")
    STOPS.append((base, h))

pod.run_on_pod = _run_logged
rv._run_on_pod = _run_logged
rv._stop_leftovers = _stop_logged
run(_fanout("codex", 3, capacity=None))
check("exactly one leftover stop for the window", len(STOPS) == 1, STOPS)
check("...and it came before every run", order and order[0] == "stop"
      and order.count("stop") == 1, order)
check("the window's first-wake marker is gone after the release",
      "codex" not in rv._pod_ready, rv._pod_ready)

pod = _Pod(hold_s=0.03)
_fresh(pod)
rv._woken.add("codex")      # a login this kernel left open — with NO capability on record
run(_fanout("codex", 2, capacity=None))
check("a pod this kernel already woke (a login left open, no cap on record) is never stopped",
      STOPS == [] and "codex" not in rv._cap_base, STOPS)

print("== a cancel never leaves a slot counted as taken ==")
pod = _Pod()
_fresh(pod)


async def _cancel_probe():
    rv._pod_slots["codex"] = 1
    gate = asyncio.Event()

    async def _slow_note():
        await gate.wait()

    async def _holder():
        async with rv._pod_slot("codex"):
            await asyncio.sleep(0.05)

    async def _waiter():
        async with rv._pod_slot("codex", on_wait=_slow_note):
            pass

    h = asyncio.create_task(_holder())
    await asyncio.sleep(0)
    w = asyncio.create_task(_waiter())
    await asyncio.sleep(0.01)          # the waiter is inside its note
    w.cancel()                         # cancelled mid-note
    await asyncio.gather(w, return_exceptions=True)
    await h
    h2 = asyncio.create_task(_holder())
    await h2
    await asyncio.sleep(0)
    t = asyncio.create_task(_holder())
    t.cancel()                         # cancelled right as it runs
    await asyncio.gather(t, return_exceptions=True)
    return rv._pod_running.get("codex", 0)


left = run(_cancel_probe())
check("no slot is left taken after cancels mid-note and mid-run", left == 0, left)

print("== a single build last out of a SHARED window bills its own run ==")
pod = _Pod(hold_s=0.05)
_fresh(pod)


async def _shared_window():
    import aiohttp
    tmp = pathlib.Path(tempfile.mkdtemp())
    (tmp / "w").mkdir()
    rv._hold_harness("codex")          # a compare already in the pod
    async with aiohttp.ClientSession() as http:
        solo = asyncio.create_task(rv._dispatch_to_pod_harness(
            http, "codex", "p", tmp / "w", 5, 0.0))
        await asyncio.sleep(0.01)
        await rv._release_harness_when_last("codex")   # the compare leaves first
        return await solo


res = run(_shared_window())
check("billed by its own wall clock, not the window's ready_duration_s (1.0)",
      res.get("compute_s") is not None and res["compute_s"] < 1.0, res.get("compute_s"))


async def _alone_window():
    import aiohttp
    tmp = pathlib.Path(tempfile.mkdtemp())
    (tmp / "w").mkdir()
    async with aiohttp.ClientSession() as http:
        return await rv._dispatch_to_pod_harness(http, "codex", "p", tmp / "w", 5, 0.0)

pod = _Pod(hold_s=0.01)
_fresh(pod)
res = run(_alone_window())
check("alone in its window it still bills the window", res.get("compute_s") == 1.0,
      res.get("compute_s"))

print("== the waiting note says why, in the pod's words ==")
pod = _Pod()
_fresh(pod)
pod.size = 1                                   # already warm at one run
sd = run(_fanout("codex", 3, capacity={"fits": ["codex"], "refused": [],
                                        "slots": {"codex": 3}}))
state = rv._read_session_state(sd)
notes = [state[v].get("queued_note") or "" for v in state]
check("the note names the pod's size and that it was already running",
      any("runs 1 at once" in n and "already running at that size" in n for n in notes),
      notes)

print("== a wake the plan refused at N says so in the note ==")
pod = _Pod(refuse_over=1)
_fresh(pod)
sd = run(_fanout("codex", 3, capacity={"fits": ["codex"], "refused": [],
                                        "slots": {"codex": 3}}))
state = rv._read_session_state(sd)
notes = [state[v].get("queued_note") or "" for v in state]
check("the fallback note blames the plan's room, not an already-running pod",
      any("did not have room for 3" in n for n in notes)
      and not any("already running" in n for n in notes), notes)

print("== an interrupted build door still launches every phase it claimed ==")
check("_exec_build launches claimed phases when the loop raises, then re-raises",
      "        except BaseException:\n" in _tools
      and "_raw = collections.Counter(kw[\"harness\"] for _t, kw in _ready)" in _tools)

print("== the capacity ask carries run counts and reads slots back ==")


class _BC:
    def __init__(self):
        self.sent = None

    async def request_full(self, op, body):
        self.sent = (op, body)
        return {"ok": True, "data": {"gated": True, "fits": ["codex", "kimi"],
                                     "refused": [], "slots": {"codex": 2, "kimi": 1}}}


bc = _BC()
rv._bridge = lambda: bc
cap = run(rv._fanout_capacity(["codex", "kimi"], {"codex": 4, "kimi": 1}))
check("slots asked only for harnesses with more than one run",
      bc.sent[1].get("slots") == {"codex": 4}, bc.sent)
check("the answer's slots are returned", cap["slots"] == {"codex": 2, "kimi": 1}, cap)
cap = run(rv._fanout_capacity(["codex"], {"codex": 20}))
check("the ask is clamped to the platform bound",
      bc.sent[1]["slots"] == {"codex": rv._POD_SLOTS_MAX}, bc.sent)
cap = run(rv._fanout_capacity(["codex"]))
check("no counts, no slots key (an old operator sees the old body)",
      "slots" not in bc.sent[1], bc.sent)

print()
if FAILURES:
    print(f"FAIL {len(FAILURES)}: {FAILURES}")
    sys.exit(1)
print("all green")
