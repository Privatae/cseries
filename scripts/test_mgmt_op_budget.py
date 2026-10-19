#!/usr/bin/env python3
"""A long management op must not be reported as a missing machine, and must not cost
the user their approval twice.

WHAT WENT WRONG. run_mgmt_op dispatched EVERY management op with a hardcoded 30 s.
model:pull downloads a model onto the user's disk and is bounded on the box at 20
minutes (the gateway's client-go/modelswap.go:217 `modelPullTimeout`) plus a
behavioural probe. So on any model bigger than a demo:

  1. the host gave up at 30 s and raised MgmtError(502, "no live management worker") —
     a false statement about the machine. The worker was connected and working;
  2. the box finished the download anyway, and gigabytes landed on the user's disk
     while this host believed nothing had happened;
  3. worst of the three, the box burned the user's SINGLE-USE machine-side approval
     (modelswap.go:858) on a pull this host had recorded as failed. To get a model the
     host thought was missing, the user had to walk back to their machine and approve
     a second time for a model already sitting on their disk.

WHAT IS CHECKED HERE, all of it against the REAL MgmtRelay and the REAL run_mgmt_op —
a fake worker stands in for the bastion's WebSocket and nothing else is stubbed but
the pool and the grant loaders:

  * the budget is PER OP, derived from the box's own bounds. model:pull gets more than
    the box can possibly take; every other op keeps 30 s, because a blanket raise
    would let one wedged op park a management worker (a bastion runs two) for half an
    hour.
  * a worker that TOOK the job and has not answered is reported as exactly that — not
    as an absent worker. The two cases are distinguished in the relay itself, so no
    caller can collapse them by accident.
  * when the reply is lost but the download landed, the host learns it from the
    machine's own ledger through model:list and reports the pull as DONE. The user is
    never asked to approve the same download twice. The reconcile runs the grant gate
    for model:list like any other op, and asks for nothing when it is not granted.

RED WITHOUT THE FIX: with `timeout=30.0` restored in run_mgmt_op and the single-outcome
`dispatch`, the no-reply case raises MgmtError(502, "no live management worker") and
the reconcile does not exist at all — checks 3, 4, 5 and 6 fail.

Run: APP_ROOT=<repo> python3 scripts/test_mgmt_op_budget.py
"""
from __future__ import annotations

import asyncio
import json
import os
import sys

_ROOT = os.environ.get("APP_ROOT") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path[:0] = [_ROOT, os.path.join(_ROOT, "brain")]

failures: list[str] = []
checks = 0


def check(label, cond, extra=""):
    global checks
    checks += 1
    print(("  ok    " if cond else "  FAIL  ") + label
          + (("\n        " + str(extra)) if extra and not cond else ""))
    if not cond:
        failures.append(label)


from server.network import nac_mgmt as M  # noqa: E402


# ── a stand-in for one bastion worker WebSocket ─────────────────────────────
# The real MgmtWorker owns the socket; the relay only ever calls .assign((op, args,
# future)) and reads .bastion_token_id. That is the whole surface, so this is the
# real dispatch path with a scripted box on the far end.

class FakeWorker:
    def __init__(self, token_id: int, replies: dict, silent_ops=()):
        self.bastion_token_id = token_id
        self.replies = replies          # op -> (stdout bytes, stderr bytes, exit)
        self.silent_ops = set(silent_ops)
        self.seen: list = []

    def assign(self, job) -> bool:
        op, args, result = job[0], job[1], job[2]  # (+ approval since 0.33.0)
        self.seen.append((op, list(args)))
        if op in self.silent_ops:
            return True                 # took the job, will never answer
        out, err, code = self.replies.get(op, (b"", b"unscripted op\n", 1))
        result.set_result((out, err, code))
        return True


class StalePool:
    """Only the one statement run_mgmt_op issues is answered."""
    def __init__(self, token_id: int):
        self.token_id = token_id

    async def fetchval(self, sql, *a):
        assert "network_bastion_heartbeat" in sql, sql
        if "count(DISTINCT h.token_id)" in sql:
            return 1  # one live bastion reports this hostname (nac_mgmt's ambiguity check)
        return self.token_id

    async def execute(self, *a, **k):
        return None


PULL_ARG = json.dumps({"model": "nomic-embed-text", "role": "embed",
                       "max_bytes": 300_000_000, "approval": "ap_1"})

LIST_REPLY_PLACED = (json.dumps({"models": [
    {"model": "qwen3:0.6b"},
    {"model": "nomic-embed-text", "placed_by_this_host": True,
     "role": "embed", "dim": 768, "placed_at": "2026-09-13T00:00:00Z"},
]}).encode(), b"", 0)

LIST_REPLY_ABSENT = (json.dumps({"models": [{"model": "qwen3:0.6b"}]}).encode(), b"", 0)


def with_stubs(grants, pool_token=7):
    """Swap the DB-backed loaders and the audit writer for the fixtures this gate needs.
    mgmt_decision itself is NOT stubbed — the reconcile's grant check has to be real."""
    saved = (M.load_mgmt_grants, M.load_suspended_servers, M.load_quarantined,
             M.audit_mgmt, M._relay)
    audited: list = []

    async def _grants(pool, pk=None):
        return grants

    async def _empty(pool):
        return set()

    async def _audit(pool, eid, server, op, exit_code=None, decision=""):
        audited.append((op, decision))

    M.load_mgmt_grants = _grants
    M.load_suspended_servers = _empty
    M.load_quarantined = _empty
    M.audit_mgmt = _audit
    M._relay = M.MgmtRelay()
    return saved, audited


def restore(saved):
    (M.load_mgmt_grants, M.load_suspended_servers, M.load_quarantined,
     M.audit_mgmt, M._relay) = saved


# ── 1. the budget table ─────────────────────────────────────────────────────

print("the per-op budget")

box_worst = (M.MODEL_PULL_BOX_TIMEOUT_S
             + M.MODEL_PROBE_CALLS * M.MODEL_PROBE_BOX_TIMEOUT_S)
check("model:pull is waited for longer than the box can possibly take",
      M.mgmt_op_budget("model:pull") > box_worst,
      f"budget {M.mgmt_op_budget('model:pull')}s vs box worst case {box_worst}s")
check("the box's 20-minute pull bound is the number this is derived from",
      M.MODEL_PULL_BOX_TIMEOUT_S == 20 * 60.0)
for op in ("uptime", "df", "model:list", "model:drop", "backup_fetch", "ctx_put",
           "ctx_get", "ctx_del", "dcn_list", "dcn_search", "dcn_plan_apply",
           "stepup_request", "stepup_status", "computer:tree", "computer:windows",
           "observe:host", "observe:net", "observe:k8s", "pairing_list"):
    if M.mgmt_op_budget(op) != M.MGMT_DISPATCH_TIMEOUT_S:
        check(f"{op} must keep the 30 s default — a raise parks one of two workers",
              False, f"got {M.mgmt_op_budget(op)}s")
check("every op whose box bound is under 30 s keeps the default",
      not any(f.startswith(("uptime", "df", "model:list", "model:drop", "backup_fetch",
                            "ctx_", "dcn_", "stepup_", "computer:", "observe:",
                            "pairing_")) for f in failures))
check("an op this host has never heard of gets the default, never the pull budget",
      M.mgmt_op_budget("some:future-op") == M.MGMT_DISPATCH_TIMEOUT_S)


# ── 2. the relay tells the two no-result cases apart ────────────────────────

print("\nthe relay's two no-result cases")


async def relay_cases():
    relay = M.MgmtRelay()
    got, why = await relay.dispatch_detailed(7, "uptime", [], timeout=0.05)
    check("no worker at all -> DISPATCH_NO_WORKER",
          got is None and why == M.DISPATCH_NO_WORKER, why)

    relay2 = M.MgmtRelay()
    relay2._register_idle(FakeWorker(7, {}, silent_ops={"model:pull"}))
    got, why = await relay2.dispatch_detailed(7, "model:pull", [PULL_ARG], timeout=0.05)
    check("a worker took the job and went quiet -> DISPATCH_NO_REPLY",
          got is None and why == M.DISPATCH_NO_REPLY, why)

    relay3 = M.MgmtRelay()
    relay3._register_idle(FakeWorker(7, {"uptime": (b"up 3 days\n", b"", 0)}))
    got, why = await relay3.dispatch_detailed(7, "uptime", [], timeout=1.0)
    check("a reply comes back with no reason attached",
          got is not None and got[2] == 0 and why == "", (got, why))


asyncio.run(relay_cases())


# ── 3. the host's words when a worker is still working ─────────────────────

print("\nwhat the host says while the box is still working")

GRANTS_PULL_ONLY = [{"server": "box", "ops": ["model:pull"], "deny": []}]
GRANTS_PULL_AND_LIST = [{"server": "box", "ops": ["model:pull", "model:list"], "deny": []}]


async def still_working():
    saved, audited = with_stubs(GRANTS_PULL_ONLY)
    try:
        M._relay._register_idle(FakeWorker(7, {}, silent_ops={"model:pull"}))
        M.MGMT_OP_BUDGET_S["model:pull"] = 0.05          # the shape, not the clock
        try:
            await M.run_mgmt_op(StalePool(7), 1, "box", "model:pull", [PULL_ARG])
            check("a silent worker does not read as a success", False)
        except M.MgmtError as e:
            check("the host does NOT claim the machine has no management worker",
                  "no live management worker" not in e.detail and
                  "no management worker connected" not in e.detail, e.detail)
            check("it says the job was taken and nothing was cancelled",
                  "took this job" in e.detail and "nothing was cancelled" in e.detail,
                  e.detail)
            check("it is not reported as a 502 about a missing component",
                  e.status == 504, e.status)
        check("the audit records no-reply, never no-worker",
              ("model:pull", "no-reply") in audited and
              ("model:pull", "no-worker") not in audited, audited)
        check("an ungranted model:list is never run behind the user's back",
              all(op != "model:list" for op, _ in audited), audited)
    finally:
        M.MGMT_OP_BUDGET_S["model:pull"] = (
            M.MODEL_PULL_BOX_TIMEOUT_S + M.MODEL_PROBE_CALLS * M.MODEL_PROBE_BOX_TIMEOUT_S
            + M.MGMT_REPLY_ROUND_TRIP_S)
        restore(saved)


asyncio.run(still_working())


# ── 4. the approval is never spent twice ───────────────────────────────────

print("\nthe approval a lost reply would otherwise have wasted")


async def reconcile(list_reply, label, expect_ok):
    saved, audited = with_stubs(GRANTS_PULL_AND_LIST)
    try:
        w = FakeWorker(7, {"model:list": list_reply}, silent_ops={"model:pull"})
        M._relay._register_idle(w)
        M._relay._register_idle(FakeWorker(7, {"model:list": list_reply}))
        M.MGMT_OP_BUDGET_S["model:pull"] = 0.05
        res, err = None, None
        try:
            res = await M.run_mgmt_op(StalePool(7), 1, "box", "model:pull", [PULL_ARG])
        except M.MgmtError as e:
            err = e
        if expect_ok:
            check(f"{label}: the pull is reported DONE, not failed",
                  res is not None and res["exit"] == 0, err.detail if err else res)
            body = json.loads(res["stdout"]) if res else {}
            check(f"{label}: the reply says the model is here and this host placed it",
                  body.get("downloaded") is True and body.get("placed_by_this_host") is True
                  and body.get("reconciled") is True, body)
            check(f"{label}: and says in words not to ask for another approval",
                  "do not ask for another one" in (body.get("note") or ""), body)
            check(f"{label}: what the machine measured comes back with it",
                  body.get("role") == "embed" and body.get("dim") == 768, body)
        else:
            check(f"{label}: a model that is NOT on the machine is never claimed as landed",
                  res is None and err is not None and err.status == 504,
                  res if res else err)
        check(f"{label}: the ledger was actually asked",
              ("model:list", "ran") in audited, audited)
    finally:
        M.MGMT_OP_BUDGET_S["model:pull"] = (
            M.MODEL_PULL_BOX_TIMEOUT_S + M.MODEL_PROBE_CALLS * M.MODEL_PROBE_BOX_TIMEOUT_S
            + M.MGMT_REPLY_ROUND_TRIP_S)
        restore(saved)


asyncio.run(
    reconcile(LIST_REPLY_PLACED, "the download landed", True))
asyncio.run(
    reconcile(LIST_REPLY_ABSENT, "still downloading", False))


print()
if failures:
    print(f"FAILED — {len(failures)} of {checks} checks")
    for f in failures:
        print("  - " + f)
    sys.exit(1)
print(f"PASSED — {checks} checks: a long op is not a missing machine, and one approval "
      f"funds one download")
