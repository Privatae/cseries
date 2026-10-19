#!/usr/bin/env python3
"""Gate: a fan-out learns how many harnesses fit the plan BEFORE it wakes any.

Measured on one Host fan-out d33393f9 (project 220, 2026-09-06): five
harnesses asked for. antigravity, codex and claude-code woke first and took the
plan's headroom; kimi and grok were then refused AT WAKE by the operator's plan
gate — verbatim from per_harness_state.json:

    "error":"harness wake failed: operator /harness/wake 402:
      {"detail":{"error":"capability_over_plan","harness":"grok",
                 "needs_mi":512,"headroom_mi":32,"upgrade_to":"pro"}}"

3 s after queueing, one variant at a time, after the user had committed to
five. The gate is right; the fan-out found out too late and said nothing.

Now fan_out asks the operator up front (bridge op harness.capacity, answered
from the wake gate's own ledger) and, in the words the Workbench shows:
  all fit   -> proceeds unchanged;
  some fit  -> runs those NOW (wave 1); the rest wait for memory and start in
               later waves as running harnesses release (run in series
               in case the memory limit is an issue — series is the
               fallback, parallel stays parallel where the plan has room); only
               a harness that does not fit even alone is refused;
  none fit  -> refuses the fan-out, no session, no wake.
The answer is fake here and the gate asserts the sentence, the wake set, the
waiting state of every queued variant, and (section 7, driving the real
runner against an operator whose answer depends on what is awake) the waves
themselves: order, the never-fits-alone refusal, and the fail-open re-ask.
Container-free: no bridge, no pod, no DB.

Run from the repo root:  python3 scripts/test_fanout_capacity_precheck.py
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

_ROOT = Path(os.environ.get("APP_ROOT") or Path(__file__).resolve().parents[1])
sys.path.insert(0, str(_ROOT))
sys.path.insert(1, str(_ROOT / "brain"))

from server.routes import review  # noqa: E402

FAILS: list[str] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    print(("  ok   " if cond else "  FAIL ") + label
          + (f"  [{detail}]" if detail and not cond else ""))
    if not cond:
        FAILS.append(label)


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


FIVE = ["antigravity", "codex", "claude-code", "kimi", "grok"]

# The operator's three answers, in its own shape (harness.capacity).
ALL_FIT = {"gated": True, "fits": FIVE, "refused": [], "headroom_mi": 3584,
           "remaining_mi": 1024, "upgrade_to": None, "pool_add_mi": None}
SOME_FIT = {"gated": True, "fits": FIVE[:3],
            "refused": [{"harness": "kimi", "needs_mi": 512},
                        {"harness": "grok", "needs_mi": 512}],
            "headroom_mi": 1568, "remaining_mi": 32, "upgrade_to": "pro",
            "pool_add_mi": None}
NONE_FIT = {"gated": True, "fits": [],
            "refused": [{"harness": h, "needs_mi": 512} for h in FIVE],
            "headroom_mi": 0, "remaining_mi": 0, "upgrade_to": "pro_heavy",
            "pool_add_mi": None}


class _FakeBridge:
    def __init__(self, envelope):
        self.envelope = envelope
        self.calls: list = []

    async def request_full(self, op, params=None):
        self.calls.append((op, params))
        return self.envelope


def _with_bridge(envelope):
    fb = _FakeBridge(envelope)
    review._bridge = lambda: fb
    return fb


def _events(session_dir: Path, vid: str) -> list:
    p = review._events_path(session_dir, vid)
    if not p.exists():
        return []
    return [json.loads(l) for l in p.read_text().splitlines() if l.strip()]


def _terminal(session_dir: Path, vid: str):
    for ev in _events(session_dir, vid):
        if ev.get("kind") == "status" and (ev.get("payload") or {}).get("state") in ("complete", "timeout"):
            return ev["payload"]
    return None


print("== 1. the pre-check relays the operator's answer, and only a usable one ==")
fb = _with_bridge({"ok": True, "data": SOME_FIT})
got = run(review._fanout_capacity(FIVE))
check("asks the bridge for harness.capacity with the request order",
      fb.calls == [("harness.capacity", {"harnesses": FIVE})], str(fb.calls))
check("relays fits/refused/remaining", got is not None and got["fits"] == FIVE[:3]
      and [r["harness"] for r in got["refused"]] == ["kimi", "grok"]
      and got["remaining_mi"] == 32, str(got))

_with_bridge({"ok": False, "error": "Unknown operation: harness.capacity"})
check("a bridge that does not carry the op yet -> None (proceed ungated)",
      run(review._fanout_capacity(FIVE)) is None)
_with_bridge({"ok": True, "data": {"gated": False, "fits": FIVE, "refused": []}})
check("an ungated answer (the gate would fail open) -> None",
      run(review._fanout_capacity(FIVE)) is None)
_with_bridge({"ok": True, "data": {"gated": True, "fits": ["codex"], "refused": []}})
check("an answer that does not account for every harness -> None",
      run(review._fanout_capacity(FIVE)) is None)
review._bridge = lambda: None
check("no bridge client -> None", run(review._fanout_capacity(FIVE)) is None)
check("nothing asked -> None, no call", run(review._fanout_capacity([])) is None)


print()
print("== 2. the sentence is plain English ==")
msg = review._capacity_message({
    "fits": SOME_FIT["fits"], "refused": SOME_FIT["refused"],
    "remaining_mi": 32, "upgrade_to": "pro", "pool_add_mi": None}, FIVE)
print("     " + msg)
check("some fit: counts, who, what they need, what is free, who runs",
      msg.startswith("3 of 5 harnesses fit this plan (kimi, grok need 512Mi each; 32Mi free). "
                     "Running antigravity, codex, claude-code."), msg)
check("...and names the tier that holds all five",
      "The Pro plan has room for all 5." in msg, msg)
for internal in ("capability_over_plan", "needs_mi", "headroom_mi", "upgrade_to", "402"):
    check(f"no internal name leaks: {internal}", internal not in msg)

none_msg = review._capacity_message({
    "fits": [], "refused": NONE_FIT["refused"], "remaining_mi": 0,
    "upgrade_to": "pro_heavy", "pool_add_mi": None}, FIVE)
print("     " + none_msg)
check("none fit: says so and that nothing started",
      none_msg.startswith("None of the 5 harnesses fit this plan right now (")
      and "need 512Mi each; 0Mi free). Nothing was started." in none_msg, none_msg)
check("...plan ids read as words", "The Pro heavy plan has room for all 5." in none_msg, none_msg)

bad = review._capacity_message({
    "fits": ["codex"], "refused": [{"harness": "kimi"}, {"harness": "grok", "needs_mi": "x"}],
    "remaining_mi": 32, "upgrade_to": None, "pool_add_mi": None}, ["codex", "kimi", "grok"])
check("a refused entry with no/malformed claim still reads as a sentence",
      bad.startswith("1 of 3 harnesses fit this plan (kimi, grok need more than is free; 32Mi free)."),
      bad)
check("...and never 'needs no'", "needs no" not in bad and "need no" not in bad, bad)
bad1 = review._capacity_message({"fits": [], "refused": [{"harness": "kimi"}],
                                 "remaining_mi": None}, ["kimi"])
check("single malformed entry", "kimi needs more than is free" in bad1 and "needs no" not in bad1, bad1)
check("per-variant reason guards the same way",
      review._refused_variant_error({"refused": [{"harness": "kimi"}], "remaining_mi": 32}, "kimi")
      == "Not started: kimi needs more than is free and this plan has 32Mi free.")

pool_msg = review._capacity_message({
    "fits": ["codex"], "refused": [{"harness": "kimi", "needs_mi": 512}],
    "remaining_mi": 256, "upgrade_to": None, "pool_add_mi": 1024}, ["codex", "kimi"])
check("a pooled host is told the amount to add, in Gi when whole",
      pool_msg == "1 of 2 harnesses fit this plan (kimi needs 512Mi; 256Mi free). "
                  "Running codex. Adding 1Gi to your memory pool would make room for all 2.",
      pool_msg)

wave_msg = review._capacity_message({
    "fits": SOME_FIT["fits"], "refused": SOME_FIT["refused"],
    "remaining_mi": 32, "upgrade_to": "pro", "pool_add_mi": None}, FIVE, waves=True)
print("     " + wave_msg)
check("the fan-out's wording: who runs now, who starts as memory frees",
      wave_msg == "3 of 5 harnesses fit this plan at once (kimi, grok need 512Mi each; 32Mi free). "
                  "Running antigravity, codex, claude-code now; kimi and grok start as memory frees. "
                  "The Pro plan has room for all 5 at once.", wave_msg)
for internal in ("capability_over_plan", "needs_mi", "headroom_mi", "upgrade_to", "402", "queued_for"):
    check(f"no internal name leaks (waves): {internal}", internal not in wave_msg)
check("the waiting line names the need, what is free, and who is running",
      review._waiting_variant_note(SOME_FIT, "kimi", FIVE[:3])
      == "Waiting for memory: kimi needs 512Mi and this plan has 32Mi free while "
         "antigravity, codex and claude-code run. It starts as memory frees.",
      review._waiting_variant_note(SOME_FIT, "kimi", FIVE[:3]))


print()
print("== 3. some fit: those run now, the rest WAIT for memory (not terminal) ==")
with tempfile.TemporaryDirectory() as td:
    sd = Path(td)
    variants = [{"vid": h, "harness": h, "model": None, "effort": None} for h in FIVE]
    variants.append({"vid": "grok-s2", "harness": "grok", "model": None, "effort": None})
    review._write_session_state(sd, {v["vid"]: {"state": "queued", "queued_at": 1.0}
                                     for v in variants})
    cap = {**SOME_FIT}
    run_v, wait_v = review._apply_capacity(sd, variants, cap)
    check("the wake set is exactly the harnesses that fit, in order",
          [v["vid"] for v in run_v] == FIVE[:3], str(run_v))
    check("every sample of a waiting harness waits with it",
          sorted(v["vid"] for v in wait_v) == ["grok", "grok-s2", "kimi"], str(wait_v))
    state = review._read_session_state(sd)
    for vid in ("kimi", "grok", "grok-s2"):
        row = state.get(vid) or {}
        check(f"{vid}: state row stays queued (the live vocabulary), queued_for memory, with a plain note",
              row.get("state") == "queued" and row.get("queued_for") == "memory"
              and row.get("queued_at") == 1.0
              and row.get("queued_note") == f"Waiting for memory: {vid.split('-')[0]} needs 512Mi and "
                                            "this plan has 32Mi free while antigravity, codex and "
                                            "claude-code run. It starts as memory frees.", str(row))
        check(f"{vid}: NOT terminal — no closing frame", _terminal(sd, vid) is None)
        evs = _events(sd, vid)
        check(f"{vid}: the run terminal has a waiting frame at seq 0 with the note",
              len(evs) == 1 and evs[0]["seq"] == 0 and evs[0]["kind"] == "status"
              and evs[0]["payload"] == {"state": "waiting", "detail": row.get("queued_note")},
              str(evs))
    for vid in FIVE[:3]:
        check(f"{vid}: still queued, no frame written",
              state[vid]["state"] == "queued" and "queued_for" not in state[vid]
              and not _events(sd, vid))
    check("the boot reaper still counts a waiting row as live",
          "queued" in review._LIVE_STATES)
    check("the tray rollup still reads a waiting session as live",
          review._rollup_session_status(state, session_over=False) == "queued")

    print("   -- and the terminal refusal, kept for a harness that fits nowhere --")
    review._refuse_variants(sd, [v for v in variants if v["harness"] == "grok"], cap)
    state = review._read_session_state(sd)
    for vid in ("grok", "grok-s2"):
        row = state.get(vid) or {}
        check(f"{vid}: state row is failed / over_plan with the plain reason",
              row.get("state") == "failed" and row.get("error_type") == "over_plan"
              and row.get("queued_for") is None
              and row.get("error") == "Not started: grok needs 512Mi and this plan has 32Mi free.",
              str(row))
        term = _terminal(sd, vid)
        check(f"{vid}: the run terminal has its closing frame after the waiting one",
              term is not None and term.get("ok") is False
              and term.get("error") == row.get("error"), str(term))
    check("kimi is untouched by grok's refusal", state["kimi"]["state"] == "queued")
    review._refuse_variants(sd, [v for v in variants if v["harness"] == "grok"], cap)
    n = sum(1 for ev in _events(sd, "grok")
            if ev.get("kind") == "status" and (ev.get("payload") or {}).get("state") == "complete")
    check("re-refusing does not stack terminal frames", n == 1, str(n))

print()
print("== 4. all fit: nothing changes ==")
with tempfile.TemporaryDirectory() as td:
    sd = Path(td)
    variants = [{"vid": h, "harness": h} for h in FIVE]
    review._write_session_state(sd, {h: {"state": "queued"} for h in FIVE})
    run_v, refused_v = review._apply_capacity(sd, variants, ALL_FIT)
    check("all five run", [v["vid"] for v in run_v] == FIVE and refused_v == [])
    check("no state row touched", all(r == {"state": "queued"}
                                      for r in review._read_session_state(sd).values()))
    check("no frames written", all(not _events(sd, h) for h in FIVE))


print()
print("== 5. the route wires it in, in the right order ==")
SRC = (_ROOT / "server" / "routes" / "review.py").read_text()
fo = SRC.index("async def fan_out(req: FanOutReq)")
body = SRC[fo:]
i_cap = body.index("capacity = await _fanout_capacity(target, _runs_per)")
i_sid = body.index("session_id = uuid.uuid4().hex")
i_refuse = body.index("raise HTTPException(409, _capacity_message(capacity, target))")
i_apply = body.index("_apply_capacity(session_dir, variants, capacity)")
i_task = body.index("asyncio.create_task(_run_fan_out_background(")
check("the pre-check runs before a session exists", i_cap < i_sid)
check("none fit -> refused before a session exists, with the sentence", i_cap < i_refuse < i_sid)
check("some fit -> applied after the queued state is seeded, before dispatch",
      body.index("_write_session_state(session_dir, initial_state)") < i_apply < i_task)
check("EVERY variant is handed to the runner, with the waiting harnesses named",
      "variants=variants, worktrees=worktrees" in body
      and "waiting=waiting_harnesses, capacity=capacity" in body)
check("only wave 1's variants get worktrees up front",
      "        for v in run_variants:\n            out[v[\"vid\"]] = _prepare_worktree(" in body)
check("the response names who waits and why",
      '"queued_for_memory"' in body and '"running_now"' in body
      and '"capacity_message": capacity_message' in body)
check("the manifest records the answer", '"capacity": (' in body)
check("the restart safety net measures from the latest wave",
      '"wave_started_at"' in SRC[SRC.index("def _session_is_over"):SRC.index("def _rollup_session_status")])


print()
print("== 6. the REAL route honours the order the user named (S2) ==")
# fan_out is driven for real up to dispatch. The catalog answers in ITS order
# (claude-code first, antigravity last — HARNESS_APP_IDS); the user names
# antigravity first. A fake operator admits the first three it is HANDED, so
# what ends up in `fits` is decided by the order fan_out sends — the thing
# under test — not by a canned answer.
from server.routes import harnesses as _hn  # noqa: E402
from server.routes import _preference_profile as _pp  # noqa: E402

CATALOG = list(review.HARNESS_APP_IDS)
USER_ORDER = ["antigravity", "kimi", "grok", "codex", "claude-code"]
check("premise: the catalog order puts antigravity last and claude-code first",
      CATALOG[0] == "claude-code" and CATALOG[-1] == "antigravity", str(CATALOG))


class _Operator:
    """Admits the first `room` harnesses in the order it is asked."""
    def __init__(self, room):
        self.room = room
        self.calls = []

    async def request_full(self, op, params=None):
        self.calls.append((op, params))
        ids = list((params or {}).get("harnesses") or [])
        fits, refused = ids[:self.room], ids[self.room:]
        return {"ok": True, "data": {
            "gated": True, "fits": fits,
            "refused": [{"harness": h, "needs_mi": 512} for h in refused],
            "headroom_mi": 512 * self.room, "remaining_mi": 32,
            "upgrade_to": "pro" if refused else None, "pool_add_mi": None}}


async def _installed_catalog_order():
    return CATALOG


async def _no_settings():
    return {}


async def _refresh_off():
    return False


dispatched: list = []
bg_kwargs: dict = {}


async def _bg(**kw):
    dispatched.extend(v["vid"] for v in kw["variants"])
    bg_kwargs.update(kw)


_saved = (review._installed_harnesses, review._coding_harness_settings,
          _hn.harness_validity, _hn.platform_refresh_enabled, _pp.ban_for,
          review._prepare_worktree, review._run_fan_out_background,
          review.SESSIONS_ROOT, review.PROJECTS_ROOT, review._bridge)
with tempfile.TemporaryDirectory() as td:
    try:
        review._installed_harnesses = _installed_catalog_order
        review._coding_harness_settings = _no_settings
        _hn.harness_validity = lambda pid: {"valid": True, "present": True}
        _hn.platform_refresh_enabled = _refresh_off
        _pp.ban_for = lambda c: None
        review._prepare_worktree = lambda parent, vid, seed=False, seed_from=None: Path(td) / "wt" / vid
        review._run_fan_out_background = _bg
        review.SESSIONS_ROOT = Path(td) / "sessions"
        review.PROJECTS_ROOT = Path(td) / "projects"
        review.SESSIONS_ROOT.mkdir()
        review.PROJECTS_ROOT.mkdir()

        op = _Operator(room=3)
        review._bridge = lambda: op
        out = run(review.fan_out(review.FanOutReq(prompt="p", harnesses=USER_ORDER)))
        check("the operator is asked in the order the user named, not catalog order",
              op.calls and op.calls[0][1]["harnesses"] == USER_ORDER, str(op.calls))
        check("antigravity — named first — runs now",
              "antigravity" in out["running_now"], str(out))
        check("wave 1 is the first three the user named",
              out["running_now"] == USER_ORDER[:3], str(out))
        check("every named harness is dispatched — the runner gets all five and is told who waits",
              out["harnesses_dispatched"] == USER_ORDER and dispatched == USER_ORDER
              and bg_kwargs.get("waiting") == USER_ORDER[3:]
              and bg_kwargs.get("capacity", {}).get("fits") == USER_ORDER[:3],
              f"{out['harnesses_dispatched']} / {dispatched} / {bg_kwargs.get('waiting')}")
        check("the last two named are the ones that wait; nothing is refused at dispatch",
              [r["harness"] for r in out["queued_for_memory"]] == USER_ORDER[3:]
              and out["refused_over_plan"] == [], str(out))
        check("the sentence says who runs now and who starts as memory frees, in that order",
              "Running antigravity, kimi, grok now; codex and claude-code start as memory frees."
              in (out["capacity_message"] or ""), str(out["capacity_message"]))
        sd = review.SESSIONS_ROOT / out["session_id"]
        st = review._read_session_state(sd)
        check("waiting variants are queued for memory in the session state, not terminal",
              all(st[h]["state"] == "queued" and st[h]["queued_for"] == "memory"
                  for h in USER_ORDER[3:])
              and all(st[h]["state"] == "queued" and "queued_for" not in st[h]
                      for h in USER_ORDER[:3]),
              str(st))
        check("...and each has its waiting frame",
              all(_events(sd, h) and _events(sd, h)[0]["payload"]["state"] == "waiting"
                  for h in USER_ORDER[3:]))
        check("the manifest carries the answer",
              json.loads((sd / "manifest.json").read_text())["capacity"]["fits"] == USER_ORDER[:3])

        # duplicates in the request are asked once, order of first mention
        op2 = _Operator(room=9)
        review._bridge = lambda: op2
        out2 = run(review.fan_out(review.FanOutReq(prompt="p", harnesses=["grok", "codex", "grok"])))
        check("a harness named twice runs once, where it was first named",
              out2["harnesses_dispatched"] == ["grok", "codex"], str(out2))

        # none fit -> 409 with the sentence, no session dir
        from fastapi import HTTPException as _HX
        before = {p.name for p in review.SESSIONS_ROOT.iterdir()}
        op3 = _Operator(room=0)
        review._bridge = lambda: op3
        try:
            run(review.fan_out(review.FanOutReq(prompt="p", harnesses=USER_ORDER)))
            check("none fit -> refused", False, "no HTTPException")
        except _HX as e:
            check("none fit -> 409 with the plain sentence",
                  e.status_code == 409 and str(e.detail).startswith("None of the 5 harnesses fit this plan"),
                  f"{e.status_code} {e.detail}")
        check("...and no session was opened",
              {p.name for p in review.SESSIONS_ROOT.iterdir()} == before)
    finally:
        (review._installed_harnesses, review._coding_harness_settings,
         _hn.harness_validity, _hn.platform_refresh_enabled, _pp.ban_for,
         review._prepare_worktree, review._run_fan_out_background,
         review.SESSIONS_ROOT, review.PROJECTS_ROOT, review._bridge) = _saved


print()
print("== 7. WAVES: the real runner, against an operator whose answer depends on what is awake ==")
# _run_fan_out_background is driven for real. The fake operator keeps the wake
# gate's ledger — what is awake, each harness's need, the plan's budget — and
# answers harness.capacity by walking the asked list in order against it, the
# way the operator's capacity check does (one fit test per step). Pods
# are fake: _run_on_pod parks each variant on an Event the test releases, so
# the test decides who finishes first and watches who starts next.
import time as _t  # noqa: E402


class _Ledger:
    def __init__(self, budget_mi: int, needs: dict, *, answer_after: int | None = None):
        self.budget = budget_mi
        self.needs = needs
        self.awake: dict = {}
        self.calls: list = []
        self.answer_after = answer_after   # capacity asks after this many -> no answer

    async def request_full(self, op, params=None):
        params = dict(params or {})
        self.calls.append((op, params))
        if op == "harness.capacity":
            n_asks = sum(1 for o, _ in self.calls if o == "harness.capacity")
            if self.answer_after is not None and n_asks > self.answer_after:
                return {"ok": False, "error": "operator rolling"}
            remaining = self.budget - sum(self.awake.values())
            fits, refused = [], []
            for h in params.get("harnesses") or []:
                need = self.needs.get(h, 512)
                if need <= remaining:
                    fits.append(h)
                    remaining -= need
                else:
                    refused.append({"harness": h, "needs_mi": need})
            return {"ok": True, "data": {
                "gated": True, "fits": fits, "refused": refused,
                "headroom_mi": self.budget, "remaining_mi": remaining,
                "upgrade_to": "pro" if refused else None, "pool_add_mi": None}}
        if op == "harness.wake":
            h = params["harness"]
            self.awake[h] = self.needs.get(h, 512)
            return {"ok": True, "data": {"url": f"http://{h}.fake", "cap": None}}
        if op == "harness.release":
            self.awake.pop(params["harness"], None)
            return {"ok": True, "data": {}}
        return {"ok": False, "error": f"Unknown operation: {op}"}


gates: dict = {}
run_order: list = []


async def _fake_run_on_pod(http, base, harness_id, label, prompt, worktree, max_minutes,
                           started, *, model=None, effort=None, session_dir=None, **_):
    run_order.append(label)
    await gates[label].wait()
    return {"harness": label, "ok": True, "exit_code": 0, "elapsed_seconds": 0.5,
            "model": model or "m", "diff": "", "transcript_full": ""}


async def _fake_collect(http, base, session_dir, harness_id):
    return True


async def _wait_for(pred, what: str, timeout: float = 8.0) -> bool:
    end = _t.monotonic() + timeout
    while _t.monotonic() < end:
        if pred():
            return True
        await asyncio.sleep(0.02)
    print(f"     (timed out waiting for: {what})")
    return False


def _st(sd: Path) -> dict:
    return {k: v.get("state") for k, v in review._read_session_state(sd).items()}


def _running(sd: Path) -> set:
    return {k for k, v in _st(sd).items() if v == "running"}


async def _open_session(td: str, harnesses: list[str]) -> tuple[Path, list, dict]:
    sd = Path(td) / "sessions" / uuid_hex()
    (sd / "diffs").mkdir(parents=True)
    (sd / "work").mkdir()
    variants = [{"vid": h, "harness": h, "model": None, "effort": None} for h in harnesses]
    (sd / "manifest.json").write_text(json.dumps({
        "session_id": sd.name, "harnesses": harnesses, "started_at": _t.time(),
        "max_minutes": 10, "variants": variants}))
    review._write_session_state(sd, {v["vid"]: {"state": "queued", "queued_at": _t.time()}
                                     for v in variants})
    for h in harnesses:
        gates[h] = asyncio.Event()
    return sd, variants, {v["vid"]: Path(td) / "wt" / v["vid"] for v in variants}


def uuid_hex() -> str:
    import uuid
    return uuid.uuid4().hex


async def _dispatch(sd: Path, variants: list, worktrees_all: dict, ledger: _Ledger):
    """What fan_out does after the pre-check, then the runner as a task."""
    review._bridge = lambda: ledger
    cap = await review._fanout_capacity([v["harness"] for v in variants])
    waiting: list = []
    run_v = variants
    if cap is not None and cap["refused"]:
        run_v, wait_v = review._apply_capacity(sd, variants, cap)
        waiting = [v["harness"] for v in wait_v]
    worktrees = {v["vid"]: worktrees_all[v["vid"]] for v in run_v}
    task = asyncio.create_task(review._run_fan_out_background(
        session_id=sd.name, session_dir=sd, project_dir=sd / "project",
        variants=variants, worktrees=worktrees, prompt="p", max_minutes=10,
        waiting=waiting, capacity=cap))
    return task, cap


async def scenario_all_fit(td):
    ledger = _Ledger(4096, {h: 512 for h in FIVE})
    sd, variants, wts = await _open_session(td, FIVE)
    task, cap = await _dispatch(sd, variants, wts, ledger)
    ok = await _wait_for(lambda: _running(sd) == set(FIVE), "all five running")
    check("all fit -> ONE wave: all five run at once", ok, str(_st(sd)))
    for h in FIVE:
        gates[h].set()
    await asyncio.wait_for(task, 8)
    check("...no capacity re-ask was ever made",
          sum(1 for o, _ in ledger.calls if o == "harness.capacity") == 1, str(ledger.calls))
    m = json.loads((sd / "manifest.json").read_text())
    check("...manifest completed with every result and no wave record",
          m.get("completed_at") and set(m["results"]) == set(FIVE) and "waves" not in m, str(m))
    check("...every pod released", ledger.awake == {}, str(ledger.awake))


async def scenario_three_of_five(td):
    ledger = _Ledger(1536, {h: 512 for h in FIVE})
    sd, variants, wts = await _open_session(td, FIVE)
    task, cap = await _dispatch(sd, variants, wts, ledger)
    ok = await _wait_for(lambda: _running(sd) == set(FIVE[:3]), "wave 1 running")
    check("3 of 5 fit -> wave 1 is the first three named, running at once", ok, str(_st(sd)))
    st = review._read_session_state(sd)
    check("...kimi and grok wait: queued, queued_for memory, waiting frame, no worktree yet",
          all(st[h]["state"] == "queued" and st[h]["queued_for"] == "memory" for h in FIVE[3:])
          and all(_events(sd, h)[0]["payload"]["state"] == "waiting" for h in FIVE[3:])
          and not (Path(td) / "wt" / "kimi").exists(), str(st))
    check("...the tray rollup is 'running' (waiting rows are live)",
          review._rollup_session_status(st, session_over=False) == "running")

    gates["codex"].set()   # codex finishes FIRST (not the first named)
    ok = await _wait_for(lambda: "kimi" in _running(sd), "kimi starts after codex releases")
    check("codex finishes -> its release lands -> kimi (next in USER order) starts", ok, str(_st(sd)))
    check("...the re-ask named the waiting ones in user order, after the release",
          [p["harnesses"] for o, p in ledger.calls if o == "harness.capacity"][1] == ["kimi", "grok"]
          and [o for o, _ in ledger.calls].index("harness.release")
          < len(ledger.calls) - 1 - [o for o, _ in reversed(ledger.calls)].index("harness.capacity"),
          str(ledger.calls))
    st = review._read_session_state(sd)
    check("...grok still waits, and its note now names who is running",
          st["grok"]["state"] == "queued" and st["grok"]["queued_for"] == "memory"
          and "while antigravity, claude-code and kimi run" in st["grok"]["queued_note"],
          str(st["grok"]))
    check("...grok's terminal got the refreshed waiting line, still no closing frame",
          len(_events(sd, "grok")) == 2 and _terminal(sd, "grok") is None, str(_events(sd, "grok")))
    check("...kimi's worktree was prepared when its wave started",
          (Path(td) / "wt" / "kimi").exists())
    check("...kimi's state row dropped queued_for on start", st["kimi"].get("queued_for") is None, str(st["kimi"]))
    check("...pod frames continue the file's numbering: kimi's waiting frame is seq 0",
          _events(sd, "kimi")[0]["seq"] == 0)

    gates["antigravity"].set()
    ok = await _wait_for(lambda: "grok" in _running(sd), "grok starts after antigravity releases")
    check("antigravity finishes -> grok starts (wave 3)", ok, str(_st(sd)))
    m = json.loads((sd / "manifest.json").read_text())
    check("...the manifest records two later waves, in order, and the latest start",
          [w["harnesses"] for w in m.get("waves", [])] == [["kimi"], ["grok"]]
          and m.get("wave_started_at") == m["waves"][-1]["started_at"], str(m.get("waves")))
    check("...the restart net is measured from the latest wave (not over)",
          not review._session_is_over({**m, "started_at": _t.time() - 10 * 60 - 1000}))
    for h in ("claude-code", "kimi", "grok"):
        gates[h].set()
    await asyncio.wait_for(task, 8)
    st = review._read_session_state(sd)
    check("everything finishes done; nothing was refused", all(v == "done" for v in _st(sd).values()), str(_st(sd)))
    check("...every pod released; results for all five",
          ledger.awake == {} and set(json.loads((sd / "manifest.json").read_text())["results"]) == set(FIVE))
    check("...ledger sequence: capacity, 3 wakes, ... never more awake than the plan holds",
          max(len([1 for o, _ in ledger.calls[:i] if o == "harness.wake"])
              - len([1 for o, _ in ledger.calls[:i] if o == "harness.release"])
              for i in range(len(ledger.calls) + 1)) <= 3, str(ledger.calls))


async def scenario_never_fits_alone(td):
    needs = {h: 512 for h in FIVE}
    needs["grok"] = 4096            # more than the whole plan
    ledger = _Ledger(1536, needs)
    sd, variants, wts = await _open_session(td, FIVE)
    task, cap = await _dispatch(sd, variants, wts, ledger)
    ok = await _wait_for(lambda: _running(sd) == set(FIVE[:3]), "wave 1 running")
    check("never-fits-alone: wave 1 still runs the first three", ok, str(_st(sd)))
    check("...grok is NOT refused up front — it waits like kimi (no arithmetic of our own)",
          _st(sd)["grok"] == "queued" and _terminal(sd, "grok") is None)
    gates["codex"].set()
    ok = await _wait_for(lambda: "kimi" in _running(sd), "kimi starts")
    check("kimi starts when codex releases; grok still waits (others awake)", ok and _st(sd)["grok"] == "queued")
    for h in ("antigravity", "claude-code", "kimi"):
        gates[h].set()
    await asyncio.wait_for(task, 8)
    st = review._read_session_state(sd)
    check("with NOTHING awake the operator still refused grok -> terminal, with the plan's reason",
          st["grok"]["state"] == "failed" and st["grok"]["error_type"] == "over_plan"
          and st["grok"]["error"] == "Not started: grok needs 4Gi and this plan has 1536Mi free.",
          str(st["grok"]))
    term = _terminal(sd, "grok")
    check("...and its terminal closes with that reason, after the waiting frames",
          term is not None and term.get("ok") is False and term.get("error") == st["grok"]["error"]
          and _events(sd, "grok")[0]["payload"]["state"] == "waiting", str(term))
    check("...the last ask was grok alone, with nothing awake",
          ledger.calls[-1] == ("harness.capacity", {"harnesses": ["grok"]}), str(ledger.calls[-1]))
    check("...the other four are done, grok never woke",
          all(st[h]["state"] == "done" for h in FIVE if h != "grok")
          and not any(p.get("harness") == "grok" for o, p in ledger.calls if o == "harness.wake"))
    m = json.loads((sd / "manifest.json").read_text())
    check("...the fan-out completed with a result row for grok",
          m.get("completed_at") and m["results"]["grok"]["ok"] is False, str(m.get("results", {}).get("grok")))


async def scenario_reask_fails_open(td):
    ledger = _Ledger(1536, {h: 512 for h in FIVE}, answer_after=1)   # the re-ask has no answer
    sd, variants, wts = await _open_session(td, FIVE)
    task, cap = await _dispatch(sd, variants, wts, ledger)
    ok = await _wait_for(lambda: _running(sd) == set(FIVE[:3]), "wave 1 running")
    check("fail-open: wave 1 as gated", ok, str(_st(sd)))
    gates["codex"].set()
    ok = await _wait_for(lambda: {"kimi", "grok"} <= _running(sd), "both waiting harnesses start")
    check("a re-ask with no answer -> everything still waiting starts, concurrently", ok, str(_st(sd)))
    for h in FIVE:
        gates[h].set()
    await asyncio.wait_for(task, 8)
    check("...all five done", all(v == "done" for v in _st(sd).values()), str(_st(sd)))
    m = json.loads((sd / "manifest.json").read_text())
    check("...recorded as one wave of both", [w["harnesses"] for w in m.get("waves", [])] == [["kimi", "grok"]],
          str(m.get("waves")))


_saved7 = (review._run_on_pod, review._collect_session, review._prepare_worktree,
           review._harness_pod_creds, review._bridge)
with tempfile.TemporaryDirectory() as td:
    try:
        review._run_on_pod = _fake_run_on_pod
        review._collect_session = _fake_collect
        review._harness_pod_creds = lambda h: {}

        def _prep(parent, vid, seed=False, seed_from=None, _td=td):
            p = Path(_td) / "wt" / vid
            p.mkdir(parents=True, exist_ok=True)
            return p
        review._prepare_worktree = _prep
        for scen in (scenario_all_fit, scenario_three_of_five,
                     scenario_never_fits_alone, scenario_reask_fails_open):
            gates.clear()
            run_order.clear()
            run(scen(td))
    finally:
        (review._run_on_pod, review._collect_session, review._prepare_worktree,
         review._harness_pod_creds, review._bridge) = _saved7


print()
if FAILS:
    print(f"FAILED: {len(FAILS)}")
    for f in FAILS:
        print("  - " + f)
    sys.exit(1)
print("all checks passed")
