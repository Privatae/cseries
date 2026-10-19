#!/usr/bin/env python3
"""A harness.release survives an operator roll (2026-09-06).

Measured on one Host's 4-harness fan-out: the operator Deployment is
`strategy: Recreate`, so every roll has ~30 s with zero Service endpoints
plus the readiness probe. antigravity's run SUCCEEDED, its release landed
in that window ("operator unreachable: ... [No route to host]"), and the pod
sat on metered compute until the idle sweep found it 15+ minutes later —
112.4 s billed. bridge_client.request_release_retrying is the fix, in the
caller; the policy and the measured window are documented on it.

What this pins:
  * the classifier: a transport failure (bridge could not reach the operator,
    pod<->bridge infra, timeouts) is retried; an answer the operator GAVE
    (400 unknown harness, 404 unprovisioned) fails fast, exactly one call;
  * inline mode retries until the operator answers, and the answer that
    lands carries ready_duration_s for billing;
  * the retry is BOUNDED — by the wall-clock budget and by the attempt cap —
    and never raises;
  * detached mode: one attempt inline, the rest off the request path,
    on_result exactly once, a concurrent retry for the same pod is joined;
  * _harness_bridge routes ONLY release through it (a wake is never retried
    here — harness_login has its own provision retry) and still drops the
    control cap when a detached retry lands;
  * the HTTP-handler lanes ask for detached mode and the harness_backend
    lanes no longer send a bare release;
  * B1 (review of 77fb622c): a retry waiting out a backoff is ABANDONED when
    a wake for the same pod lands from another lane — no further release
    call, no on_result (no bill), the waker's control cap intact — and a
    wake on a different key does not abandon it.

Container-free: the bridge is a stub with the one method under test, and the
backoff constants are shrunk so the real sleeps are milliseconds.

Run: APP_ROOT=<repo> python3 scripts/test_harness_release_retry.py
"""
import asyncio
import pathlib
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from server.bridge import bridge_client as bcm  # noqa: E402

FAILURES = []


def check(name, cond, detail=""):
    if cond:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name} {detail}")
        FAILURES.append(name)


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


UNREACHABLE = {"ok": False, "code": 502,
               "error": "operator unreachable: Cannot connect to host "
                        "2001:db8:ff::f495:8011 ssl:default [No route to host]"}
OPERATOR_400 = {"ok": False, "code": 400,
                "error": "operator /harness/release 400: unknown harness 'nope'"}
RELEASED = {"ok": True, "data": {"ready_duration_s": 112.4, "vcpus": 2, "scaled_to": 0}}


class _BC:
    """Answers the scripted envelopes in order, then the last one forever."""
    _connected = True

    def __init__(self, *script):
        self.script = list(script)
        self.calls = []

    async def request_full(self, op, params=None):
        self.calls.append((op, dict(params or {})))
        env = self.script.pop(0) if len(self.script) > 1 else self.script[0]
        return env


# Milliseconds, not seconds: the shape of the policy is under test, not the
# wall clock. The production values are asserted separately below.
bcm.RELEASE_RETRY_BASE_S = 0.002
bcm.RELEASE_RETRY_CAP_S = 0.004

print("== production policy covers the measured window ==")
check("budget is twice the measured ~45 s window",
      bcm.RELEASE_RETRY_BUDGET_S == 90.0, bcm.RELEASE_RETRY_BUDGET_S)
check("attempts are capped", bcm.RELEASE_RETRY_MAX_ATTEMPTS == 16)

print("== classifier: transport vs an answer the operator gave ==")
check("bridge could not reach the operator -> transport",
      bcm.release_error_is_transport(UNREACHABLE))
check("pod<->bridge infra (request_full's own shape) -> transport",
      bcm.release_error_is_transport({"ok": False, "error": "not connected", "error_kind": "infra"}))
check("REQUEST_TIMEOUT -> transport",
      bcm.release_error_is_transport({"ok": False, "error": "timeout", "error_kind": "infra"}))
check("bridge server read timeout (408 'Timeout') -> transport",
      bcm.release_error_is_transport({"ok": False, "error": "Timeout", "code": 408}))
check("operator 400 unknown harness -> NOT transport",
      not bcm.release_error_is_transport(OPERATOR_400))
check("operator 404 unprovisioned -> NOT transport",
      not bcm.release_error_is_transport(
          {"ok": False, "code": 404, "error": "operator /harness/release 404: no harness-x Deployment"}))
check("bridge-side refusal (allowlist) -> NOT transport",
      not bcm.release_error_is_transport({"ok": False, "code": 400, "error": "harness must be one of codex|grok"}))
check("an ok envelope is never 'transport'", not bcm.release_error_is_transport(RELEASED))

print("== inline: the release that lands is the one that gets billed ==")
bc = _BC(UNREACHABLE, UNREACHABLE, RELEASED)
env = run(bcm.request_release_retrying(bc, {"harness": "antigravity"}))
check("two unreachable attempts, then the operator answers -> ok",
      env.get("ok") is True and len(bc.calls) == 3, f"calls={len(bc.calls)} env={env}")
check("the answer carries ready_duration_s for billing",
      (env.get("data") or {}).get("ready_duration_s") == 112.4)
check("every attempt is the SAME op and payload (idempotent re-send)",
      all(c == ("harness.release", {"harness": "antigravity"}) for c in bc.calls), bc.calls)

bc = _BC(OPERATOR_400)
env = run(bcm.request_release_retrying(bc, {"harness": "nope"}))
check("an operator 400 fails fast: exactly one call, envelope passed through",
      len(bc.calls) == 1 and env is OPERATOR_400, f"calls={len(bc.calls)}")

print("== bounded: budget and attempt cap, never raises ==")
bc = _BC(UNREACHABLE)
t0 = time.monotonic()
env = run(bcm.request_release_retrying(bc, {"harness": "codex"}, budget_s=0.05))
took = time.monotonic() - t0
check("always-unreachable gives up at the budget (not ok, no raise)",
      env.get("ok") is False and "release_attempts" in env, env)
check("...having actually retried", len(bc.calls) >= 2, f"calls={len(bc.calls)}")
check("...inside the budget (+ one attempt's slack)", took < 1.0, f"{took:.2f}s")

bc = _BC(UNREACHABLE)
env = run(bcm.request_release_retrying(bc, {"harness": "codex"}, budget_s=30))
check("the attempt cap holds when the operator fast-fails",
      len(bc.calls) == bcm.RELEASE_RETRY_MAX_ATTEMPTS
      and env.get("release_attempts") == bcm.RELEASE_RETRY_MAX_ATTEMPTS,
      f"calls={len(bc.calls)} env={env}")

print("== detached: one attempt on the request path, the rest off it ==")


async def _detached_scenario():
    bc = _BC(UNREACHABLE, UNREACHABLE, RELEASED)
    seen = []

    async def on_result(final):
        seen.append(final)

    first = await bcm.request_release_retrying(
        bc, {"harness": "grok"}, detach=True, on_result=on_result)
    inline_calls = len(bc.calls)
    key = "harness:grok"
    task = bcm._detached_release_tasks.get(key)
    joined = await bcm.request_release_retrying(
        bc, {"harness": "grok"}, detach=True, on_result=on_result)
    joined_calls = len(bc.calls)
    same_task = bcm._detached_release_tasks.get(key) is task
    await task
    return first, inline_calls, task, joined, joined_calls, bc, seen, same_task


first, inline_calls, task, joined, joined_calls, bc, seen, same_task = run(_detached_scenario())
check("first attempt fails on transport -> returned at once, marked retrying",
      first.get("ok") is False and first.get("retrying") is True, first)
check("exactly ONE call happened on the request path", inline_calls == 1)
check("a retry task exists for that pod", task is not None)
check("a second detached release for the SAME pod joins it (one inline attempt, no new task)",
      joined.get("retrying") is True and joined_calls == inline_calls + 1 and same_task,
      f"joined={joined} calls={inline_calls}->{joined_calls} same_task={same_task}")
check("the detached retry lands and on_result fires exactly once with the ok envelope",
      len(seen) == 1 and seen[0].get("ok") is True, seen)
check("the task unregisters itself when done",
      "harness:grok" not in bcm._detached_release_tasks)


async def _detached_first_ok():
    bc = _BC(RELEASED)
    seen = []

    async def on_result(final):
        seen.append(final)

    env = await bcm.request_release_retrying(bc, {"app_rt": "freq"}, detach=True, on_result=on_result)
    await asyncio.sleep(0.01)
    return env, seen, bc


env, seen, bc = run(_detached_first_ok())
check("detached with a first-attempt success: the envelope is returned, on_result is NOT called",
      env is RELEASED and not seen and len(bc.calls) == 1)


async def _detached_operator_answer():
    bc = _BC(OPERATOR_400)
    seen = []

    async def on_result(final):
        seen.append(final)

    env = await bcm.request_release_retrying(bc, {"harness": "x"}, detach=True, on_result=on_result)
    return env, seen, bc


env, seen, bc = run(_detached_operator_answer())
check("detached with an operator 400: no task, no retry flag, one call",
      env is OPERATOR_400 and "retrying" not in env and not seen and len(bc.calls) == 1)

print("== _harness_bridge: release goes through the retry, wake does not ==")
from server.routes import review  # noqa: E402

_orig_bridge = review._bridge


async def _bridge_scenarios():
    out = {}
    bc = _BC(UNREACHABLE, RELEASED)
    review._bridge = lambda: bc
    review._remember_control_cap("codex", "http://codex.test:8841", "cap-1")
    rel = await review._harness_bridge("harness.release", "codex")
    out["inline"] = (rel, len(bc.calls), "codex" in review._cap_base)

    bc = _BC(UNREACHABLE)
    review._bridge = lambda: bc
    wake = await review._harness_bridge("harness.wake", "codex", creds={"api_key": "k"})
    out["wake"] = (wake, len(bc.calls), bc.calls[0] if bc.calls else None)

    bc = _BC(UNREACHABLE, RELEASED)
    review._bridge = lambda: bc
    review._remember_control_cap("grok", "http://grok.test:8841", "cap-2")
    rel = await review._harness_bridge("harness.release", "grok", release_in_background=True)
    out["bg_first"] = (rel, len(bc.calls), "grok" in review._cap_base, bc.calls[0])
    await bcm._detached_release_tasks["harness:grok"]
    out["bg_after"] = (len(bc.calls), "grok" in review._cap_base)
    return out


try:
    out = run(_bridge_scenarios())
finally:
    review._bridge = _orig_bridge
    review._control_cap.clear()
    review._cap_base.clear()

rel, n, held = out["inline"]
check("inline release: unreachable then ok -> ok, two calls, control cap dropped",
      rel.get("ok") is True and rel.get("ready_duration_s") == 112.4 and n == 2 and not held,
      f"rel={rel} calls={n} held={held}")
wake, n, first_call = out["wake"]
check("a wake is NEVER retried here (one call, the failure passed through)",
      wake.get("ok") is False and n == 1 and wake.get("error", "").startswith("operator unreachable"),
      f"wake={wake} calls={n}")
check("the wake payload is untouched by the new keyword (no leak into the body)",
      first_call == ("harness.wake", {"harness": "codex", "creds": {"api_key": "k"}}), first_call)
rel, n, held, first_call = out["bg_first"]
check("background release: first failure returned at once with retrying=True, cap still held",
      rel.get("ok") is False and rel.get("retrying") is True and n == 1 and held, f"rel={rel} calls={n}")
check("the release body carries only the harness id",
      first_call == ("harness.release", {"harness": "grok"}), first_call)
n, held = out["bg_after"]
check("...and when the detached retry lands the control cap is dropped",
      n == 2 and not held, f"calls={n} held={held}")

print("== B1: a retried release never kills a pod another lane has since woken ==")
# Reviewed on 77fb622c: lane A's release fails on transport in the roll window
# and waits out a backoff. The operator comes back and lane B wakes the SAME
# harness — warm (replicas still 1, A's release never landed), so the operator
# reuses the URL and the cap. A's next attempt would patch replicas:0 under B
# and, on "landing", drop B's control cap. Once a wake for the same key lands,
# the retry must be ABANDONED before its next attempt: no further release
# call (replicas untouched), no on_result (nothing billed), cap intact.
from server.applications import harness_backend as hb  # noqa: E402

_orig_hb_bridge = hb._bridge
WOKE ={"ok": True, "data": {"url": "http://codex.test:8841", "cap": "cap-B"}}


class _OpBC(_BC):
    """Releases scripted as _BC; every wake succeeds with the same URL + cap."""

    async def request_full(self, op, params=None):
        if op == "harness.wake":
            self.calls.append((op, dict(params or {})))
            return WOKE
        return await super().request_full(op, params)


def _releases(bc):
    return [c for c in bc.calls if c[0] == "harness.release"]


async def _b1_review_lane():
    # Lane A = a login route (detached); lane B = a build's wake via _harness_bridge.
    bc = _OpBC(UNREACHABLE, RELEASED)
    review._bridge = lambda: bc
    review._remember_control_cap("codex", "http://codex.test:8841", "cap-A")
    rel = await review._harness_bridge("harness.release", "codex", release_in_background=True)
    task = bcm._detached_release_tasks.get("harness:codex")
    wake = await review._harness_bridge("harness.wake", "codex")
    if task is not None:
        await task
    await asyncio.sleep(0.02)
    return (rel, wake, _releases(bc), review._control_cap.get("http://codex.test:8841"),
            "codex" in review._cap_base)


async def _b1_backend_detached():
    # Lane A = release_now / a user's stop (detached); lane B = ensure_awake's _wake.
    bc = _OpBC(UNREACHABLE, RELEASED)
    hb._bridge = lambda: bc
    seen = []

    async def on_result(final):
        seen.append(final)

    first = await bcm.request_release_retrying(
        bc, {"harness": "vscode"}, detach=True, on_result=on_result)
    task = bcm._detached_release_tasks.get("harness:vscode")
    woke = await hb._wake("vscode")
    if task is not None:
        await task
    await asyncio.sleep(0.02)
    return first, woke, _releases(bc), seen


async def _b1_inline(params, wake):
    # Lane A = a build / fan-out release (inline, shielded); the wake lands in
    # A's first backoff. `wake` is the real sender the other lane would use.
    bc = _OpBC(UNREACHABLE, UNREACHABLE, RELEASED)
    hb._bridge = lambda: bc
    review._bridge = lambda: bc
    rel_task = asyncio.ensure_future(bcm.request_release_retrying(bc, params))
    await asyncio.sleep(0)            # attempt 1 sent + failed; A is in its backoff
    woke = await wake()
    env = await rel_task
    return env, woke, _releases(bc)


try:
    out_review = run(_b1_review_lane())
    out_backend = run(_b1_backend_detached())
    out_inline_h = run(_b1_inline({"harness": "vscode"}, lambda: hb._wake("vscode")))
    out_inline_app = run(_b1_inline({"app_rt": "freq"}, lambda: hb._wake_app("freq")))
    out_other = run(_b1_inline({"harness": "codex"}, lambda: hb._wake("grok")))
    out_other_kind = run(_b1_inline({"harness": "freq"}, lambda: hb._wake_app("freq")))
finally:
    review._bridge = _orig_bridge
    hb._bridge = _orig_hb_bridge
    review._control_cap.clear()
    review._cap_base.clear()

rel, wake, rels, cap_now, held = out_review
check("B1 review lane: A's first attempt failed and went detached",
      rel.get("ok") is False and rel.get("retrying") is True, rel)
check("B1 review lane: B's wake landed and installed its cap",
      wake.get("ok") is True and cap_now == "cap-B", f"wake={wake} cap={cap_now}")
check("B1 review lane: A's retry was ABANDONED — no second release (replicas untouched)",
      len(rels) == 1, f"releases={len(rels)}")
check("B1 review lane: B's control cap is intact (nothing 'landed')",
      cap_now == "cap-B" and held, f"cap={cap_now} held={held}")

first, woke, rels, seen = out_backend
check("B1 backend lane: ensure_awake's wake abandons the detached retry — one release call",
      first.get("retrying") is True and woke.get("ok") is True and len(rels) == 1,
      f"first={first} woke={woke} releases={len(rels)}")
check("B1 backend lane: on_result never fires — nothing is billed",
      seen == [], seen)

env, woke, rels = out_inline_h
check("B1 inline (harness): the wake in A's backoff abandons the retry — one call, not ok, marked abandoned",
      len(rels) == 1 and env.get("ok") is False and env.get("abandoned") is True,
      f"releases={len(rels)} env={env}")
env, woke, rels = out_inline_app
check("B1 inline (app_rt): same for the app-runtime lane (_wake_app)",
      len(rels) == 1 and env.get("ok") is False and env.get("abandoned") is True,
      f"releases={len(rels)} env={env}")
env, woke, rels = out_other
check("a wake on a DIFFERENT harness does not abandon the retry (it lands)",
      len(rels) == 3 and env.get("ok") is True, f"releases={len(rels)} env={env}")
env, woke, rels = out_other_kind
check("same id, different kind (app_rt 'freq' vs harness 'freq') does not abandon",
      len(rels) == 3 and env.get("ok") is True, f"releases={len(rels)} env={env}")

print("== the lanes are wired ==")
review_src = (ROOT / "server/routes/review.py").read_text()
hb_src = (ROOT / "server/applications/harness_backend.py").read_text()
check("the three login routes (HTTP handlers) release in the background",
      review_src.count("release_in_background=True") == 3,
      review_src.count("release_in_background=True"))
check("build / fan-out / catalog releases stay inline and shielded (billing needs the answer)",
      # Every build lane releases inline through the holder count (the LAST
      # holder out sends the one release) and the single build keeps its
      # shield; the retry policy under test here lives in _harness_bridge,
      # which both helpers call. See scripts/test_fanout_shared_pod_release.py.
      'rel = await asyncio.shield(_release_harness_when_last(harness_id))' in review_src
      and 'rel = await asyncio.shield(_release_harness_when_last(h))' in review_src
      and 'return await _harness_bridge("harness.release", harness_id, **bridge_kw)' in review_src)
check("harness_backend sends no bare release any more",
      'request_full("harness.release"' not in hb_src
      and hb_src.count("request_release_retrying(") >= 2)

print()
if FAILURES:
    print(f"FAIL {len(FAILURES)}: {FAILURES}")
    sys.exit(1)
print("all green")
