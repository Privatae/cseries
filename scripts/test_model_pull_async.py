#!/usr/bin/env python3
"""A model pull is an ASYNC JOB, and the host drives it the way it already drives
backup_fetch.

WHAT THE HOST HALF HAS TO GET RIGHT, and why each one is here:

  * A STATUS POLL IS NOT A LONG OP. Both sub-ops ride the single `model:pull` op id,
    because mgmt_decision matches op ids exactly and a separate `model:status` would
    be ungranted in every signed grant already written — a user whose grant says
    model:pull could start a download and then not be allowed to watch it finish. So
    the budget cannot be keyed on the op name alone: a START keeps the 26-minute
    ceiling (a published-0.24.0 bastion still answers it synchronously and this host
    must not break against one), a STATUS keeps the 30 s default.

  * A POLL FOR A FORGOTTEN JOB IS A QUESTION FOR THE LEDGER. The box answers
    'unknown' when its in-memory job table has gone — a restart — and 'unknown' is
    not 'failed'. If the download had landed, the approval is spent and the machine's
    own ledger has the model, so model:list finds it and the user must never be asked
    to approve the same download twice. If it had not, nothing was spent and the same
    approval still funds a retry. Both branches are checked here.

  * THE GRANT GATE STILL APPLIES TO THE RECOVERY. model:list is a different op and
    gets the gate every op gets; an ungranted one is never run behind the user's back.

RED WITHOUT THE FIX, measured by reverting `mgmt_op_budget(op, args)` to the op-only
form and dropping _model_pull_restarted: the status budget check fails (a poll is
given 1590 s, so a poll sent to a box that does not understand it parks one of two
management workers for 26 minutes), and every 'unknown' check fails because the
reply is passed through untouched.

Run: APP_ROOT=<repo> python3 scripts/test_model_pull_async.py
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


# The two seams this gate is about, reached through shims so that the failure
# against a host that has NOT got them is behavioural — "a poll was given 1590 s"
# — rather than an import-time TypeError that says nothing about what broke.
def budget(op, args=None):
    try:
        return M.mgmt_op_budget(op, args)
    except TypeError:
        return M.mgmt_op_budget(op)


def sub_op(args):
    """The FIXTURE's own reader, so the scripted box answers the right sub-op even on
    a host that has none — otherwise a missing seam shows up as a KeyError in the
    scaffolding instead of as the check that names it."""
    try:
        req = json.loads(str(args[0])) if args else {}
    except Exception:
        return "start"
    return str((req or {}).get("op") or "start").strip() or "start"


def host_sub_op(args):
    """The HOST's reader, or None when this host has not got one."""
    fn = getattr(M, "model_pull_sub_op", None)
    return fn(args) if fn else None


class FakeWorker:
    """The relay's whole surface is .assign((op, args, future)) and .bastion_token_id,
    so this is the REAL dispatch path with a scripted box on the far end."""

    def __init__(self, token_id: int, replies: dict):
        self.bastion_token_id = token_id
        self.replies = replies      # (op, sub_op) -> (stdout, stderr, exit)
        self.seen: list = []

    def assign(self, job) -> bool:
        op, args, result = job[0], job[1], job[2]  # (+ approval since 0.33.0)
        sub = sub_op(args) if op == M.MODEL_PULL_OP else ""
        self.seen.append((op, sub, list(args)))
        out = self.replies.get((op, sub)) or self.replies.get((op, ""))
        result.set_result(out or (b"", b"unscripted op\n", 1))
        return True


class StalePool:
    def __init__(self, token_id: int):
        self.token_id = token_id

    async def fetchval(self, sql, *a):
        assert "network_bastion_heartbeat" in sql, sql
        if "count(DISTINCT h.token_id)" in sql:
            return 1  # one live bastion reports this hostname (nac_mgmt's ambiguity check)
        return self.token_id

    async def execute(self, *a, **k):
        return None


def with_stubs(grants, workers=2):
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


START_ARG = json.dumps({"op": "start", "model": "nomic-embed-text", "role": "embed",
                        "max_bytes": 300_000_000, "approval": "ap_1"})
STATUS_ARG = json.dumps({"op": "status", "job_id": "mp-1", "model": "nomic-embed-text"})
STATUS_ARG_NO_MODEL = json.dumps({"op": "status", "job_id": "mp-1"})
LEGACY_ARG = json.dumps({"model": "nomic-embed-text", "role": "embed",
                         "max_bytes": 300_000_000, "approval": "ap_1"})

RUNNING = (json.dumps({"id": "mp-1", "model": "nomic-embed-text", "role": "embed",
                       "state": "running", "got_bytes": 1 << 28, "want_bytes": 3 << 28,
                       "poll_after_s": 2, "note": "downloading nomic-embed-text — "
                                                  "256 MB of 768 MB so far"}).encode(), b"", 0)
UNKNOWN = (json.dumps({"state": "unknown", "id": "mp-1",
                       "note": "this machine has no record of that download."}).encode(), b"", 0)
LIST_PLACED = (json.dumps({"models": [
    {"model": "qwen3:0.6b"},
    {"model": "nomic-embed-text", "placed_by_this_host": True,
     "role": "embed", "dim": 768, "placed_at": "2026-09-13T00:00:00Z"},
]}).encode(), b"", 0)
LIST_ABSENT = (json.dumps({"models": [{"model": "qwen3:0.6b"}]}).encode(), b"", 0)

GRANTS_PULL_ONLY = [{"server": "box", "ops": ["model:pull"], "deny": []}]
GRANTS_PULL_AND_LIST = [{"server": "box", "ops": ["model:pull", "model:list"], "deny": []}]


# ── 1. the budget is per SUB-op, not per op ────────────────────────────────

print("the budget a poll gets")

box_worst = (M.MODEL_PULL_BOX_TIMEOUT_S
             + M.MODEL_PROBE_CALLS * M.MODEL_PROBE_BOX_TIMEOUT_S)
check("a START still gets the whole ceiling — a published 0.24.0 box answers it "
      "synchronously and this host must not break against one",
      budget("model:pull", [START_ARG]) > box_worst,
      budget("model:pull", [START_ARG]))
check("a caller that names no sub-op is a START, and keeps the ceiling",
      budget("model:pull", [LEGACY_ARG]) > box_worst,
      budget("model:pull", [LEGACY_ARG]))
check("a STATUS gets the 30 s default — a poll must never park one of two "
      "management workers for 26 minutes",
      budget("model:pull", [STATUS_ARG]) == M.MGMT_DISPATCH_TIMEOUT_S,
      budget("model:pull", [STATUS_ARG]))
check("an unparsable argument is treated as a start, not as a licence to wait",
      budget("model:pull", ["not json"]) > box_worst)
check("no args at all is a start",
      budget("model:pull") > box_worst)
check("every other op is unaffected by the args",
      budget("model:list", [STATUS_ARG]) == M.MGMT_DISPATCH_TIMEOUT_S
      and budget("backup_fetch", [STATUS_ARG]) == M.MGMT_DISPATCH_TIMEOUT_S)
check("the sub-op reader agrees with the box's own vocabulary",
      host_sub_op([START_ARG]) == "start"
      and host_sub_op([STATUS_ARG]) == "status"
      and host_sub_op([LEGACY_ARG]) == "start")


# ── 2. a start returns at once and a poll reports progress ─────────────────

print("\nwhat the user is told while the bytes move")


async def start_then_poll():
    saved, audited = with_stubs(GRANTS_PULL_ONLY)
    try:
        M._relay._register_idle(FakeWorker(7, {
            (M.MODEL_PULL_OP, "start"): (json.dumps(
                {"id": "mp-1", "model": "nomic-embed-text", "role": "embed",
                 "state": "running", "poll_after_s": 2}).encode(), b"", 0)}))
        res = await M.run_mgmt_op(StalePool(7), 1, "box", "model:pull", [START_ARG])
        body = json.loads(res["stdout"])
        check("a start comes back with a job id and a running state, not a result",
              res["exit"] == 0 and body.get("id") and body.get("state") == "running", body)

        M._relay._register_idle(FakeWorker(7, {(M.MODEL_PULL_OP, "status"): RUNNING}))
        res = await M.run_mgmt_op(StalePool(7), 1, "box", "model:pull", [STATUS_ARG])
        body = json.loads(res["stdout"])
        check("a poll reports the bytes that have moved and the size they are moving to",
              body.get("got_bytes") and body.get("want_bytes"), body)
        check("and carries WORDS the host can say to the user during a 26-minute download",
              "downloading" in (body.get("note") or ""), body)
        check("and says when to ask again",
              (body.get("poll_after_s") or 0) > 0, body)
        check("a running poll is not touched by the restart path",
              body.get("state") == "running", body)
    finally:
        restore(saved)


asyncio.run(start_then_poll())


# ── 3. the bastion restarted mid-pull ──────────────────────────────────────

print("\nthe machine restarted and forgot the job")


async def restarted(list_reply, label, expect_landed, grants=GRANTS_PULL_AND_LIST,
                    status_arg=STATUS_ARG):
    saved, audited = with_stubs(grants)
    try:
        M._relay._register_idle(FakeWorker(7, {(M.MODEL_PULL_OP, "status"): UNKNOWN}))
        M._relay._register_idle(FakeWorker(7, {(M.MODEL_LIST_OP, ""): list_reply}))
        res = await M.run_mgmt_op(StalePool(7), 1, "box", "model:pull", [status_arg])
        body = json.loads(res["stdout"])
        check(f"{label}: a forgotten job is never reported as a failure",
              res["exit"] == 0 and body.get("state") != "failed", body)
        if expect_landed:
            check(f"{label}: the ledger says the download landed, so the pull is DONE",
                  body.get("downloaded") is True and body.get("reconciled") is True, body)
            check(f"{label}: and the host is told in words not to ask for another approval",
                  "do not ask for another one" in (body.get("note") or ""), body)
            check(f"{label}: what the machine measured comes back with it",
                  body.get("role") == "embed" and body.get("dim") == 768, body)
        else:
            check(f"{label}: a model that is NOT on the machine is never claimed as landed",
                  body.get("downloaded") is not True and body.get("state") == "unknown", body)
            check(f"{label}: and the answer says the approval was not spent",
                  "still funds it" in (body.get("note") or ""), body)
        return audited
    finally:
        restore(saved)


audited = asyncio.run(restarted(LIST_PLACED, "it had finished", True))
check("it had finished: the ledger was actually asked",
      ("model:list", "ran") in audited, audited)

audited = asyncio.run(restarted(LIST_ABSENT, "it had not finished", False))
check("it had not finished: the ledger was actually asked",
      ("model:list", "ran") in audited, audited)

# The recovery is still an action on someone's machine, so it is still gated.
audited = asyncio.run(restarted(LIST_PLACED, "model:list ungranted", False,
                                grants=GRANTS_PULL_ONLY))
check("model:list ungranted: the recovery never runs an op the user did not grant",
      all(op != "model:list" for op, _ in audited), audited)

# With no model named there is nothing to look for, and the host does not guess.
audited = asyncio.run(restarted(LIST_PLACED, "no model named", False,
                                status_arg=STATUS_ARG_NO_MODEL))
check("no model named: the host asks the ledger nothing rather than guessing a name",
      all(op != "model:list" for op, _ in audited), audited)


print()
if failures:
    print(f"FAILED — {len(failures)} of {checks} checks")
    for f in failures:
        print("  - " + f)
    sys.exit(1)
print(f"PASSED — {checks} checks: a poll is short, a forgotten job is a question for "
      f"the ledger, and one approval still funds one download")
