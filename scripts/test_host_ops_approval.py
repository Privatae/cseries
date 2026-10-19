#!/usr/bin/env python3
"""test_host_ops_approval.py — host-authored ops, approval,
the pod's half of bastion 0.33.0: a write on a machine anchored to this Host runs
only on the person's approval, obtained from their approval device or the
machine's console and verified by the box.

Pins, container-free (stub pool, stub relay, the real code):

  1. the classification and the purpose match the box's (writes.go): the read
     set, the own-approval set, the tighten set, and the args digest's FIXED
     VECTOR — sha256 over "<len>:<bytes>\\n" per argument, never JSON;
  2. `nac_approval.obtain`: not_needed on a box that gates nothing, no_way on
     one that can take no approval, the DEVICE path (file, poll, approved),
     pending when the wait runs out, the CONSOLE path when only a factor is
     held;
  3. `run_mgmt_op` obtains the approval AFTER the pod gate and hands it to the
     relay; a pending approval is a pending result (nothing dispatched,
     audited `awaiting-approval`), never a refusal;
  4. the management frame carries `approval` only when there is one;
  5. the beat carries `approvals` beside its directives (self-update,
     contain:<pid>, contain_finding:<id>, mirror) and the completion task fills
     a missing one in from the device;
  6. the enforcing report's refusal ring and quarantine become oversight
     findings, and a box that can take no approval is a `warn`;
  7. the terminal's begin/poll for session:open;
  8. the migration, the tool wiring, the card version.

Run: APP_ROOT=<repo> python3 scripts/test_host_ops_approval.py
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import sys
from pathlib import Path

ROOT = Path(os.environ.get("APP_ROOT") or Path(__file__).resolve().parent.parent)
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "brain"))

failures: list[str] = []
asyncio.set_event_loop(asyncio.new_event_loop())


def check(name: str, cond: bool, detail="") -> None:
    print(("  OK   " if cond else "  FAIL ") + name + (f"  — {detail}" if detail and not cond else ""))
    if not cond:
        failures.append(name)


def src(rel: str) -> str:
    return (ROOT / rel).read_text()


def run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


from server.network import nac_approval  # noqa: E402

# ── 1. classification + purpose, byte for byte with the box ──────────────────
print("classification and purpose")
for op in ["uptime", "audit_fetch", "dcn_list", "ctx_get", "model:list", "pairing_list",
           "stepup_request", "device_status", "observe:host", "computer:tree", "egress", "inference"]:
    check(f"{op} is a read", not nac_approval.op_writes(op))
for op in ["ctx_put", "ctx_extract", "backup_fetch", "model:drop", "contain", "self-update",
           "mirror", "session:open", "harness:run", "brand_new_op"]:
    check(f"{op} is a write", nac_approval.op_writes(op))
for op in ["dcn_grant_sync", "dcn_plan_apply", "model:pull", "pairing_grant_sync",
           "dcn_grant_drop", "pairing_grant_drop", "backup_fetch", "ctx_put", "ctx_del"]:
    check(f"{op} carries its own approval / tightens / is standing work", nac_approval.own_approval(op))
check("ctx_extract has no approval of its own (it executes an extractor)", not nac_approval.own_approval("ctx_extract"))
check("empty args digest is sha256('')",
      nac_approval.args_digest([]) == "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855")
check("fixed vector matches the Go test (hostops_test.go TestArgsDigestIsTheFramedSHA256)",
      nac_approval.args_digest(["a", "b<c"]) == "922c052b1872b6644665fa394e99c19aa26037ee0b81b5c4bc22ed4fd34ddd0b")
check("the length prefix keeps one argument apart from two",
      nac_approval.args_digest(["a\nb"]) != nac_approval.args_digest(["a", "b"]))
check("purpose shape", nac_approval.mgmt_op_purpose("ctx_put", "box-7", ["x"])
      == "mgmt_op|ctx_put|box-7|" + nac_approval.args_digest(["x"]))
d = nac_approval.mgmt_op_details("ctx_put", "box-7", ["p", "q"], "why")
check("details are the plain fields the device derives from",
      d == {"kind": "mgmt_op", "op": "ctx_put", "server": "box-7", "args": ["p", "q"], "note": "why"})
ex = json.dumps({"tool": "pdf-text", "mime": "application/pdf", "doc": "ctx:42", "ticket": "t-1", "b64": "aGk="})
import hashlib as _hl
check("the extract approval binds tool, mime, the document's name and the inline bytes' digest — never the ticket",
      nac_approval.approval_args("ctx_extract", [ex]) == ["pdf-text", "application/pdf", "ctx:42", _hl.sha256(b"aGk=").hexdigest()]
      and nac_approval.mgmt_op_purpose("ctx_extract", "b", [ex.replace("t-1", "t-2")])
      == nac_approval.mgmt_op_purpose("ctx_extract", "b", [ex])
      and nac_approval.approval_args("ctx_extract", [json.dumps({"tool": "t", "mime": "m", "ticket": "t-9"})]) == ["t", "m", "ticket:t-9", ""]
      and nac_approval.approval_args("ctx_put", ["x", "y"]) == ["x", "y"])

# ── stub pool + stub relay ────────────────────────────────────────────────────


class Pool:
    """Answers by SQL substring. `enforcing` is what the box last reported;
    `devices` maps approval_device_key -> (token_id, hostname)."""

    def __init__(self, enforcing=None, devices=None):
        self.enforcing = enforcing
        self.devices = devices or {}
        self.executed: list = []
        self.rows: dict = {"update": None, "contain": [], "finding": []}

    async def fetchval(self, sql, *a):
        if "SELECT enforcing FROM network_bastion_heartbeat" in sql:
            return json.dumps(self.enforcing) if self.enforcing is not None else None
        if "SELECT enforcing->>'stepup'" in sql:
            return "true" if (self.enforcing or {}).get("stepup") else "false"
        if "SELECT token_id FROM network_bastion_heartbeat WHERE hostname" in sql:
            return 77
        if "count(DISTINCT h.token_id)" in sql:
            return 1
        if "SELECT 1 FROM mcp_client_tokens" in sql:
            return 1
        return None

    async def fetchrow(self, sql, *a):
        if "approval_device_key" in sql:
            keys = a[1]
            for k, (tid, host) in self.devices.items():
                if k in keys:
                    return {"token_id": tid, "hostname": host, "last_seen_at": None}
            return None
        if "SELECT update_to_version, update_approval, gateway_version" in sql:
            return self.rows["update"]
        return None

    async def fetch(self, sql, *a):
        if "FROM nac_agent_contain" in sql:
            return self.rows["contain"]
        if "FROM nac_finding_contain" in sql:
            return self.rows["finding"]
        return []

    async def execute(self, sql, *a):
        self.executed.append((sql, a))
        return "UPDATE 1"

    def acquire(self):
        pool = self

        class _Cx:
            async def __aenter__(self):
                return pool

            async def __aexit__(self, *x):
                return False
        return _Cx()


class Relay:
    """nac_dcn._dispatch_box stand-in: scripted answers per (op)."""

    def __init__(self):
        self.calls: list = []
        self.device_state = "pending"
        self.tap_after_ask = False      # the ask comes back pending; the next read finds the tap

    async def __call__(self, pool, eid, server, op, args, token_id=None, timeout=None):
        self.calls.append((server, op, list(args), token_id))
        if op == "device_request" and self.tap_after_ask:
            return {"exit": 0, "stdout": json.dumps({"id": "req-1", "state": "pending"}), "stderr": ""}
        if op == "device_status" and self.tap_after_ask:
            return {"exit": 0, "stdout": json.dumps({"state": "approved", "approval": '{"v":1,"sig":"x"}'}),
                    "stderr": ""}
        if op == "device_request":
            if self.device_state == "denied":
                # the device refuses to file an ask the person denied (device.go)
                return {"exit": 126, "stdout": "", "stderr": "you refused this on your device; it cannot be asked again for 24h"}
            if self.device_state == "approved":
                return {"exit": 0, "stdout": json.dumps({"id": "req-1", "state": "approved",
                                                         "approval": '{"v":1,"sig":"x"}'}), "stderr": ""}
            return {"exit": 0, "stdout": json.dumps({"id": "req-1", "state": "pending"}), "stderr": ""}
        if op == "device_status":
            if self.device_state == "approved":
                return {"exit": 0, "stdout": json.dumps({"state": "approved", "approval": '{"v":1,"sig":"x"}'}),
                        "stderr": ""}
            return {"exit": 0, "stdout": json.dumps({"state": self.device_state}), "stderr": ""}
        if op == "stepup_request":
            if self.device_state == "approved":
                return {"exit": 0, "stdout": json.dumps({"state": "approved", "request": "con-1",
                                                         "approval": "con-appr"}), "stderr": ""}
            return {"exit": 0, "stdout": json.dumps({"state": "pending", "request": "con-1"}), "stderr": ""}
        if op == "stepup_status":
            return {"exit": 0, "stdout": json.dumps({"state": "approved", "approval": "con-appr"}), "stderr": ""}
        return {"exit": 0, "stdout": "", "stderr": ""}


from server.network import nac_dcn  # noqa: E402

relay = Relay()
nac_dcn._dispatch_box = relay
nac_approval.POLL_S = 0.01
# Where a case expects an ANSWER (approved / denied), the wait is a ceiling, not
# a budget: obtain() polls until the answer is no longer pending and returns at
# once, so the ceiling is spent only by a run that is really stuck. A wait of
# one second expired on a loaded box (a poll's sleep alone can take that long
# under a load of 70); the cases that expect "pending" use 0, never the clock.
ANSWER_CEILING_S = 60

# ── 2. obtain ─────────────────────────────────────────────────────────────────
print("obtain")
res = run(nac_approval.obtain(Pool(enforcing={"grants": []}), 3, "box-7", "ctx_extract", ["a"]))
check("a box that gates nothing: not_needed", res["state"] == "not_needed")
res = run(nac_approval.obtain(Pool(enforcing={"write_gate": True}), 3, "box-7", "ctx_extract", ["a"]))
check("a gating box with no way to approve: no_way, naming the fix",
      res["state"] == "no_way" and "pin-device" in res["detail"])
res = run(nac_approval.obtain(Pool(enforcing={"write_gate": True, "second_factor": True}), 3, "box-7", "uptime", []))
check("a read never asks", res["state"] == "not_needed")
res = run(nac_approval.obtain(Pool(enforcing={"write_gate": True, "second_factor": True}), 3, "box-7",
                              "dcn_grant_drop", ["1"]))
check("a tighten never asks", res["state"] == "not_needed")
check("standing work under a configured store never asks",
      run(nac_approval.obtain(Pool(enforcing={"write_gate": True, "second_factor": True}), 3, "box-7",
                              "ctx_put", ["a", "b"]))["state"] == "not_needed"
      and run(nac_approval.obtain(Pool(enforcing={"write_gate": True, "second_factor": True}), 3, "box-7",
                                  "backup_fetch", ["{}"]))["state"] == "not_needed")


class TokenPool(Pool):
    async def fetchval(self, sql, *a):
        if "WHERE token_id=$1" in sql and "SELECT enforcing" in sql:
            return json.dumps({"write_gate": True, "second_factor": True}) if a[0] == 77 else json.dumps({})
        return await super().fetchval(sql, *a)

check("the model's own ctx_put asks anyway (force), the store's own does not",
      run(nac_approval.obtain(Pool(enforcing={"write_gate": True, "second_factor": True}), 3, "box-7",
                              "ctx_put", ["a"], force=True))["state"] == "no_way"
      and "ctx_put" in nac_approval.TOOL_ASKS_ANYWAY)
check("the gate is read by token when the caller resolved one",
      run(nac_approval.obtain(TokenPool(), 3, "box-7", "ctx_extract", ["x"], token_id=77))["state"] == "no_way"
      and run(nac_approval.obtain(TokenPool(), 3, "box-7", "ctx_extract", ["x"], token_id=78))["state"] == "not_needed")

gate = {"write_gate": True, "second_factor": True, "device": True, "device_keys": ["KEY-A"]}
pool = Pool(enforcing=gate, devices={"KEY-A": (55, "laptop.example")})
relay.calls.clear()
res = run(nac_approval.obtain(pool, 3, "box-7", "ctx_extract", ["notes/a.txt", "aGk="], note="save it", wait_s=0))
check("device path, wait 0: pending with the device named",
      res["state"] == "pending" and res["request"]["via"] == "device"
      and res["request"]["device"] == "laptop.example" and "device-approvals" in res["next"])
filed = [c for c in relay.calls if c[1] == "device_request"]
check("the ask went to the DEVICE's own lane, by its token", filed and filed[0][0] == "laptop.example" and filed[0][3] == 55)
details = json.loads(base64.b64decode(filed[0][2][0]))
check("the device receives the plain details and nothing else (the machine is inside them)",
      details == nac_approval.mgmt_op_details("ctx_extract", "box-7", ["notes/a.txt", "aGk="], "save it")
      and len(filed[0][2]) == 1)
relay.device_state = "approved"
res = run(nac_approval.obtain(pool, 3, "box-7", "ctx_extract", ["notes/a.txt", "aGk="], wait_s=ANSWER_CEILING_S))
check("device path: approved once the person tapped, with the signed approval",
      res["state"] == "approved" and res["via"] == "device" and '"sig"' in res["approval"])
relay.device_state = "denied"
res = run(nac_approval.obtain(pool, 3, "box-7", "ctx_extract", ["x"], wait_s=ANSWER_CEILING_S))
check("device path: denied stands", res["state"] == "denied" and "refused" in res["detail"])
relay.device_state = "pending"
# The console path: a factor, no device.
pool_c = Pool(enforcing={"write_gate": True, "second_factor": True, "stepup": True})
res = run(nac_approval.obtain(pool_c, 3, "box-7", "ctx_extract", ["x"], wait_s=ANSWER_CEILING_S))
check("console path: stepup_request kind mgmt_op, then approved",
      res["state"] == "approved" and res["via"] == "console" and res["approval"] == "con-appr")
# Any wait over 0 reads the answer once more, in code, not by the clock: a wait
# of a nanosecond is over before the first check, and the tap is still read.
res = run(nac_approval.obtain(pool_c, 3, "box-7", "ctx_extract", ["y"], wait_s=1e-9))
check("a wait over 0 reads the answer again, however long filing the ask took",
      res["state"] == "approved" and res["via"] == "console", res)
relay.tap_after_ask = True
res = run(nac_approval.obtain(pool, 3, "box-7", "ctx_extract", ["z1"], wait_s=1e-9))
relay.tap_after_ask = False
check("...on the device path too", res["state"] == "approved" and res["via"] == "device", res)
relay.device_state = "approved"
res = run(nac_approval.obtain(pool_c, 3, "box-7", "ctx_extract", ["x"], wait_s=0))
check("console path, wait 0: a missed tap is read on the file itself",
      res["state"] == "approved" and res["approval"] == "con-appr")
relay.device_state = "pending"
asked = [c for c in relay.calls if c[1] == "stepup_request"]
check("the console ask carries the same details, purpose first",
      asked and asked[-1][2][0] == nac_approval.mgmt_op_purpose("ctx_extract", "box-7", ["x"])
      and json.loads(base64.b64decode(asked[-1][2][1]))["kind"] == "mgmt_op")
# A device pinned that this Host cannot see, and no factor: say so.
res = run(nac_approval.obtain(Pool(enforcing={"write_gate": True, "second_factor": True, "device": True,
                                              "device_keys": ["KEY-Z"]}), 3, "box-7", "ctx_extract", ["x"], wait_s=0))
check("a device this Host cannot see and no factor: no_way, saying which",
      res["state"] == "no_way" and "device" in res["detail"])

# ── 3. run_mgmt_op ────────────────────────────────────────────────────────────
print("run_mgmt_op")
from server.network import nac_mgmt  # noqa: E402

# Rung 8 put the person's CARD in front of every write's device ask
# (nac_approval.obtain_via_card → nac_ops.mgmt_card, pinned in
# test_host_ops_rung8.py on stubs that hold tickets). This gate pins the
# DEVICE lane behind it, so the card is stepped over here: the callers reach
# obtain() directly, as they did when this gate was written.


async def _straight(pool, eid, server, op, args, *, note="", wait_s=nac_approval.TOOL_WAIT_S, token_id=None, force=False,
                    final_no=False):
    # final_no is the card's own rule (a beat's "don't" stands for good); the device lane behind it has no such thing.
    return await nac_approval.obtain(pool, eid, server, op, args, note=note, wait_s=wait_s, token_id=token_id, force=force)

nac_approval.obtain_via_card = _straight

audits: list = []


async def _audit(pool, eid, server, op, exit_code=None, decision=None, **kw):
    audits.append((server, op, exit_code, decision))

nac_mgmt.audit_mgmt = _audit


async def _state(pool, eid):
    return [{"server": "*", "ops": ["ctx_extract", "contain"]}], set(), set()

nac_mgmt._gate_state = _state
nac_mgmt.mgmt_decision = lambda grants, eid, server, op, suspended, quarantined: True
nac_mgmt.mgmt_denied_by = lambda grants, server, op: None


class MRelay:
    def __init__(self):
        self.calls: list = []

    def worker_count(self, token_id):
        return 1

    async def dispatch_detailed(self, token_id, op, args, timeout, approval=""):
        self.calls.append((token_id, op, list(args), approval))
        return (b"done", b"", 0), ""

mrelay = MRelay()
nac_mgmt.get_mgmt_relay = lambda: mrelay

relay.device_state = "pending"
audits.clear()
res = run(nac_mgmt.run_mgmt_op(pool, 3, "box-7", "ctx_extract", ["{}"], note="read", approval_wait_s=0))
check("pending approval: a pending result, nothing dispatched, audited awaiting-approval",
      res.get("state") == "pending_approval" and res.get("exit") is None and "next" in res
      and not mrelay.calls and audits and audits[-1][3] == "awaiting-approval")
relay.device_state = "approved"
res = run(nac_mgmt.run_mgmt_op(pool, 3, "box-7", "ctx_extract", ["{}"], note="read", approval_wait_s=ANSWER_CEILING_S))
check("approved: dispatched WITH the approval, exit from the box",
      res.get("exit") == 0 and mrelay.calls and '"sig"' in mrelay.calls[-1][3])
relay.device_state = "denied"
audits.clear()
res = run(nac_mgmt.run_mgmt_op(pool, 3, "box-7", "ctx_extract", ["z"], approval_wait_s=ANSWER_CEILING_S))
check("denied on the device: 126 in the lane's own shape, audited denied-person",
      res.get("exit") == 126 and res["stderr"].startswith("refused:") and audits[-1][3] == "denied-person")
res = run(nac_mgmt.run_mgmt_op(Pool(enforcing={"grants": []}), 3, "box-7", "ctx_extract", ["z"]))
check("a box that gates nothing: dispatched with no approval", res.get("exit") == 0 and mrelay.calls[-1][3] == "")
res = run(nac_mgmt.run_mgmt_op(pool, 3, "box-7", "ctx_put", ["notes/a.txt", "aGk="]))
check("ctx_put is standing work: dispatched with no approval on a gating box too",
      res.get("exit") == 0 and mrelay.calls[-1][1] == "ctx_put" and mrelay.calls[-1][3] == "")
relay.device_state = "pending"

# contain: the order is placed even while the approval is pending, and the
# approval is stored with it once given.
from server.network import topology, nac_agents  # noqa: E402

queued: list = []


async def _request_contain(pool, eid, token_id, pid, approval=""):
    queued.append((pid, approval))
    return "queued"

topology.request_contain = _request_contain


async def _kind(pool, token_id, pid):
    return "agent"

nac_agents.observation_kind = _kind
res = run(nac_mgmt.run_mgmt_op(pool, 3, "box-7", "contain", ["4242"], approval_wait_s=0))
check("contain pending: the order is queued without its approval and the result says pending",
      queued == [(4242, "")] and res.get("state") == "pending_approval" and res.get("queued") is True)
relay.device_state = "approved"
res = run(nac_mgmt.run_mgmt_op(pool, 3, "box-7", "contain", ["4242"], approval_wait_s=ANSWER_CEILING_S))
check("contain approved: the order carries the approval", queued[-1][0] == 4242 and '"sig"' in queued[-1][1])
relay.device_state = "pending"
# An open order that already carries its approval is not asked for again.
relay.calls.clear()


class HeldPool(Pool):
    async def fetchval(self, sql, *a):
        if "SELECT approval FROM nac_agent_contain" in sql:
            return "HELD"
        return await super().fetchval(sql, *a)

res = run(nac_mgmt.run_mgmt_op(HeldPool(enforcing=gate, devices={"KEY-A": (55, "laptop.example")}), 3, "box-7",
                               "contain", ["4242"], approval_wait_s=0))
check("an open order with its approval: no new ask, the order carries it",
      not [c for c in relay.calls if c[1] == "device_request"] and queued[-1] == (4242, "HELD"))
try:
    run(nac_mgmt.run_mgmt_op(pool, 3, "box-7", "contain", ["finding:nothex"], approval_wait_s=0))
    check("a malformed fingerprint is refused before anyone is asked", False)
except nac_mgmt.MgmtError as e:
    check("a malformed fingerprint is refused before anyone is asked",
          e.status == 400 and not [c for c in relay.calls if c[1] == "device_request"])
check("run_mgmt_op knows who is asking (origin); the tool and the route say so",
      'origin: str = "service"' in src("server/network/nac_mgmt.py") and 'origin="tool"' in src("brain/mastercode/runtime/tools.py")
      and 'origin="route"' in src("server/routes/network.py")
      and 'origin in ("tool", "route")' in src("server/network/nac_mgmt.py"))
# A contain whose order already RAN is reported, never placed again.


class DonePool(Pool):
    """A contain for 4242 was cleared `outcome` a moment ago; the pid now
    wears `now_sig` (None = not observed)."""

    def __init__(self, outcome="done", now_sig=None, **kw):
        super().__init__(**kw)
        self.outcome, self.now_sig = outcome, now_sig

    async def fetchrow(self, sql, *a):
        if "SELECT outcome, cleared_at, signature FROM nac_agent_contain" in sql:
            return {"outcome": "done", "cleared_at": "2026-09-24T21:00:00Z", "signature": "sig-a"} \
                if self.outcome == "done" else None  # the SQL itself asks for outcome = 'done'
        return await super().fetchrow(sql, *a)

    async def fetchval(self, sql, *a):
        if "SELECT approval FROM nac_agent_contain" in sql or "SELECT 1 FROM nac_agent_contain" in sql:
            return None
        if "FROM nac_agent_activity" in sql:
            return self.now_sig
        return await super().fetchval(sql, *a)

devs = {"KEY-A": (55, "laptop.example")}
relay.calls.clear()
before = len(queued)
res = run(nac_mgmt.run_mgmt_op(DonePool(enforcing=gate, devices=devs), 3, "box-7", "contain", ["4242"], approval_wait_s=0))
check("a re-called contain after the kill ran is 'already done': no card, no new order",
      res.get("exit") == 0 and "already done" in res.get("stdout", "") and len(queued) == before
      and not [c for c in relay.calls if c[1] == "device_request"])
res = run(nac_mgmt.run_mgmt_op(DonePool(outcome="expired", enforcing=gate, devices=devs), 3, "box-7", "contain", ["4242"], approval_wait_s=0))
check("an order that lapsed is placed again", len(queued) == before + 1 and res.get("state") == "pending_approval")
res = run(nac_mgmt.run_mgmt_op(DonePool(outcome="done", now_sig="sig-b", enforcing=gate, devices=devs), 3, "box-7", "contain", ["4242"], approval_wait_s=0))
check("a pid that now wears another program's signature is a new act", len(queued) == before + 2)
check("the SQL asks only for outcomes that ran",
      "AND outcome = 'done'" in src("server/network/nac_mgmt.py") and "AND outcome = 'killed'" in src("server/network/nac_mgmt.py"))
check("the contain pending words say not to call again",
      "do NOT call again" in nac_approval.pending_on_device_message("dev", "box-7", "contain")
      and "never\ncall it again" in src("default-skills/network.md"))
t2 = src("server/network/topology.py")
check("an open order takes the approval it lacked on a re-call",
      "SET approval=$3 WHERE token_id=$1 AND pid=$2" in t2 and "SET approval=$3 WHERE token_id=$1 AND fp=$2" in t2)

# ── 4. the frame ──────────────────────────────────────────────────────────────
print("the frame")


class WS:
    def __init__(self):
        self.sent: list = []
        self._frames = [b"X0"]

    async def send_bytes(self, b):
        self.sent.append(b)

    async def receive_bytes(self):
        return self._frames.pop(0)

w = nac_mgmt.MgmtWorker(WS(), 7)
fut = asyncio.get_event_loop().create_future()
run(w._run("ctx_put", ["a"], fut, "APPR"))
check("the C frame carries approval when given",
      json.loads(w.ws.sent[0][1:]) == {"op": "ctx_put", "args": ["a"], "approval": "APPR"})
w = nac_mgmt.MgmtWorker(WS(), 7)
fut = asyncio.get_event_loop().create_future()
run(w._run("uptime", [], fut, ""))
check("and not when there is none (an older box reads the frame it always did)",
      json.loads(w.ws.sent[0][1:]) == {"op": "uptime", "args": []})

# ── 5. the beat's approvals + completion ─────────────────────────────────────
print("the beat")
from server.network import nac_harness  # noqa: E402
m = nac_approval.directive_approvals_for_beat(("0.33.0", "U"), [(1, "C1"), (2, None)], [(9, "F9")], {"approval": "M"})
check("approvals keyed the way the box reads them",
      m == {"self-update": "U", "contain:1": "C1", "contain_finding:9": "F9", "mirror": "M"})
p5 = Pool(enforcing=gate, devices={"KEY-A": (55, "laptop.example")})
p5.rows["update"] = {"update_to_version": "0.33.0", "update_approval": None, "gateway_version": "0.32.2"}
p5.rows["contain"] = [{"pid": 4242}]
relay.device_state = "approved"
done = run(nac_approval.complete_directive_approvals(p5, 3, 77, "box-7"))
check("the completion task stores the self-update approval and the contain approval",
      done["update"] == "0.33.0" and done["contain"] == [4242]
      and any("update_approval" in sql for sql, _ in p5.executed)
      and any("nac_agent_contain SET approval" in sql for sql, _ in p5.executed))
relay.device_state = "pending"
# Bastion 0.44.0 takes a newer signed release with no approval and says so in
# its enforcing report, so a box that says so is not asked for a move forward.
# A box that does not say so (anything older, including the update that brings
# it ONTO 0.44.0), and any move backward, still is. The version vectors follow
# the box's versionNewer test.
V = nac_approval.update_needs_approval
FWD = {**gate, "anchored": True, "is_anchor": True, nac_approval.UPDATE_FORWARD_KEY: True}
MEMBER = {**FWD, "is_anchor": False}
check("a box that says so needs no approval to move forward",
      not V(FWD, "0.44.0", "0.45.0") and not V(FWD, "0.44.0", "0.44.1") and not V(FWD, "0.44.0", "1.0.0")
      and not V(FWD, "0.45.2", "0.46.0") and not V(FWD, "0.44", "0.44.1"))
check("a box that does not say so, an older or equal target, and anything unreadable still need one",
      V(gate, "0.44.0", "0.45.0") and V(None, "0.44.0", "0.45.0")
      and V({**FWD, nac_approval.UPDATE_FORWARD_KEY: "true"}, "0.44.0", "0.45.0")
      and V(MEMBER, "0.44.0", "0.45.0") and V({k: v for k, v in FWD.items() if k != "is_anchor"}, "0.44.0", "0.45.0")
      and V(FWD, "0.44.0", "0.43.1") and V(FWD, "0.44.0", "0.9.0") and V(FWD, "0.44.0", "0.44")
      and V(FWD, "0.44.0", "0.44.0")
      and V(FWD, "dev", "0.45.0") and V(FWD, "", "0.45.0") and V(FWD, None, "0.45.0") and V(FWD, "0.44.0", None)
      and V(FWD, "0.44.0", "0.45.0-rc1") and V(FWD, "0.44.0", "v0.45.0") and V(FWD, "0.44.0", " 0.45.0")
      and V(FWD, "0.44.0+lab", "0.45.0") and V(FWD, "0.44.0", "0.45.٣")
      and V(FWD, "0.44.0", "0.45." + "9" * 19))
nac_approval._last_ask.clear()
relay.calls.clear()
p8 = Pool(enforcing=FWD, devices={"KEY-A": (55, "laptop.example")})
p8.rows["update"] = {"update_to_version": "0.45.0", "update_approval": None, "gateway_version": "0.44.0"}
relay.device_state = "approved"
done = run(nac_approval.complete_directive_approvals(p8, 3, 77, "box-7"))
check("the completion task files no ask for a move forward on a box that says it needs none",
      done["update"] is None and not relay.calls
      and not any("update_approval" in sql for sql, _ in p8.executed))
p8.rows["update"] = {"update_to_version": "0.43.1", "update_approval": None, "gateway_version": "0.44.0"}
done = run(nac_approval.complete_directive_approvals(p8, 3, 77, "box-7"))
check("and still asks it for a move backward", done["update"] == "0.43.1" and relay.calls)
relay.calls.clear()
p9 = Pool(enforcing=gate, devices={"KEY-A": (55, "laptop.example")})
p9.rows["update"] = {"update_to_version": "0.45.0", "update_approval": None, "gateway_version": "0.44.0"}
done = run(nac_approval.complete_directive_approvals(p9, 3, 77, "box-7"))
check("a box that does not say so is asked for a move forward, whatever its version",
      done["update"] == "0.45.0" and relay.calls)
nac_approval._last_ask.clear()
relay.calls.clear()
p10 = Pool(enforcing=MEMBER, devices={"KEY-A": (55, "laptop.example")})
p10.rows["update"] = {"update_to_version": "0.45.0", "update_approval": None, "gateway_version": "0.44.0"}
done = run(nac_approval.complete_directive_approvals(p10, 3, 77, "box-7"))
check("a box anchored to ANOTHER Host is never asked for an update: it refuses one from a member whatever is sent",
      done["update"] is None and not relay.calls and nac_approval.update_from_member(MEMBER)
      and not nac_approval.update_from_member(FWD) and not nac_approval.update_from_member(gate))
p11 = Pool(enforcing=MEMBER, devices={"KEY-A": (55, "laptop.example")})
p11.rows["update"] = {"update_to_version": "0.45.0", "update_approval": '{"v":1,"sig":"x"}', "gateway_version": "0.44.0"}
relay.calls.clear()
run(nac_approval.complete_directive_approvals(p11, 3, 77, "box-7"))
check("…and an approval stored for one is dropped: it can never be used there",
      any("SET update_approval = NULL" in sql for sql, _ in p11.executed) and not relay.calls)
relay.device_state = "pending"
t = src("server/network/topology.py")
check("the beat withholds an update from a gating box only when that box needs an approval for it",
      "elif write_gate and update_needs_approval(enforcing, running, target):" in t)
check("upsert_heartbeat attaches result['approvals'] and schedules the completion task",
      'result["approvals"] = approvals' in t and "schedule_directive_approvals" in t
      and 'approvals["self-update"]' in t and 'approvals[f"contain:{pid}"]' in t
      and 'approvals[f"contain_finding:{o[\'id\']}"]' in t and 'approvals["mirror"]' in t)
check("an unapproved directive is WITHHELD from a gating box, never sent to be refused",
      "elif write_gate:" in t and "raise _Withheld()" in t and "deliver_pids" in t
      and "withheld: an unapproved order would be answered refused_anchor and lost" in t)
check("a refused approval is forgotten before the task asks again",
      "clear_refused_directive_approvals(pool, entity_id, token_id, enforcing)" in t)
c = src("server/network/nac_commit.py")
check("a new update target drops the old approval",
      "update_approval = CASE WHEN update_to_version IS DISTINCT FROM $2 THEN NULL" in c
      and "SET update_to_version = NULL, update_approval = NULL" in c)
p6 = Pool(); p6.rows["update"] = {"update_to_version": "0.33.0", "update_approval": '{"v":1,"expires":"2000-01-01T00:00:00Z","sig":"x"}', "gateway_version": "0.32.2"}
check("an expired stored approval is recognised", nac_approval._approval_expired(p6.rows["update"]["update_approval"])
      and not nac_approval._approval_expired('{"v":1,"expires":"2999-01-01T00:00:00Z"}')
      and not nac_approval._approval_expired("console-id"))
cleared = []


class ClearPool(Pool):
    async def execute(self, sql, *a):
        cleared.append(sql)
        return "UPDATE 1"

run(nac_approval.clear_refused_directive_approvals(
    ClearPool(), 3, 77, {"last_directive_refusal": {"what": "self-update to 0.33.0", "why": "refused: self-update changes this machine and approval expired"}}))
check("a box's refusal of a self-update approval clears it", any("update_approval = NULL" in q for q in cleared))
cleared.clear()
run(nac_approval.clear_refused_directive_approvals(
    ClearPool(), 3, 77, {"last_directive_refusal": {"what": "self-update to 0.33.0", "why": "refused: self-update changes this machine and approval expired"}}))
check("the same refusal on the next beat clears nothing (once per `at`)", cleared == [])
run(nac_approval.clear_refused_directive_approvals(
    ClearPool(), 3, 77, {"last_directive_refusal": {"what": "self-update to 0.33.0", "why": "refused: ... approval expired", "at": "later"}}))
check("a new refusal clears again", any("update_approval = NULL" in q for q in cleared))
cleared.clear()
run(nac_approval.clear_refused_directive_approvals(ClearPool(), 3, 77, {"last_directive_refusal": {"what": "contain", "why": "refused: ... anchor host"}}))
check("a refusal that is not about the approval clears nothing", cleared == [])
nac_approval._last_ask.clear()
relay.device_state = "pending"
p7 = Pool(enforcing=gate, devices={"KEY-A": (55, "laptop.example")})
a1 = run(nac_approval._ask_directive(p7, 3, "box-7", "contain", ["1"], "n"))
a2 = run(nac_approval._ask_directive(p7, 3, "box-7", "contain", ["1"], "n"))
relay.device_state = "approved"
a3 = run(nac_approval._ask_directive(p7, 3, "box-7", "contain", ["2"], "n"))
a4 = run(nac_approval._ask_directive(p7, 3, "box-7", "contain", ["2"], "n"))
check("asks back off only while pending; an answered ask goes out at once next time",
      a1["state"] == "pending" and a2["state"] == "backoff" and a3["state"] == "approved" and a4["state"] == "approved")
relay.device_state = "denied"
d1 = run(nac_approval._ask_directive(p7, 3, "box-7", "contain", ["3"], "n"))
relay.device_state = "approved"
d2 = run(nac_approval._ask_directive(p7, 3, "box-7", "contain", ["3"], "n"))
check("a denial holds its backoff (the device refuses it for 24 h; no knock per beat)",
      d1["state"] == "denied" and d2["state"] == "backoff")
relay.device_state = "pending"
cleared.clear()
run(nac_approval.clear_refused_directive_approvals(
    ClearPool(), 3, 78, {"last_directive_refusal": {"what": "self-update to 0.33.0", "why": "refused: ... approval expired", "at": "2000-01-01T00:00:00Z"}}))
check("a refusal recorded before this process started was an earlier process's to act on", cleared == [])
hargs2 = nac_harness.harness_approval_args({"harness_id": "h", "mode": "lab", "envs": {"PORT": "1", "PORT2": "2", "A": "y", "A-B": "x"}})
check("envs are sorted as k=v strings, byte order, like Go", hargs2[11] == "A-B=x\nA=y\nPORT2=2\nPORT=1")
check("orders are stored with their approval (v179 columns)",
      "INSERT INTO nac_agent_contain (token_id, pid, signature, approval)" in t
      and "INSERT INTO nac_finding_contain (token_id, fp, approval)" in t)
note = nac_approval.write_gate_note({"write_gate": True, "second_factor": False,
                                     "quarantined": ["h.example"], "quarantine": {"h.example": {"until": "T"}}})
check("the posture note names both states", "pins no approval device" in note and "h.example until T" in note
      and note.startswith("every write is refused"))
check("…and on a box that takes a forward update with no approval, says that one still goes through",
      nac_approval.write_gate_note({"write_gate": True, "second_factor": False, "is_anchor": True,
                                    nac_approval.UPDATE_FORWARD_KEY: True})
      .startswith("every write except an update to a newer bastion version is refused"))
check("…but not on a member Host's card, where no update from it goes through",
      nac_approval.write_gate_note({"write_gate": True, "second_factor": False, "anchored": True,
                                    nac_approval.UPDATE_FORWARD_KEY: True}).startswith("every write is refused"))
check("no note for a box that gates nothing", nac_approval.write_gate_note({"grants": []}) is None)

# ── 6. oversight ──────────────────────────────────────────────────────────────
print("oversight")
from mastercode.runtime import oversight  # noqa: E402

out: list = []
oversight._fold_write_gate({"enforcing": {
    "refusals": [{"ts": "2026-09-24T20:00:00Z", "pairing": "h.example", "op": "ctx_put",
                  "why": "refused: op 'ctx_put' changes this machine and approval expired", "write": True}],
    "quarantined": ["h.example"], "quarantine": {"h.example": {"until": "2026-09-24T20:10:00Z", "strikes": 1}},
    "write_gate": True, "second_factor": False}}, "box-7", out)
kinds = {f["kind"]: f for f in out}
check("a refusal is a notice finding with the box's words",
      kinds.get("refusal", {}).get("severity") == "notice" and "approval expired" in kinds["refusal"]["evidence"]["why"])
check("a quarantine is a warn finding keyed on the pairing",
      kinds.get("quarantine", {}).get("severity") == "warn" and kinds["quarantine"]["key"].endswith("|h.example"))
check("no way to approve is a warn finding", kinds.get("write-gate", {}).get("severity") == "warn")
out2: list = []
oversight._fold_write_gate({"enforcing": {"grants": []}}, "box-7", out2)
check("a quiet box folds nothing", out2 == [])
check("derive_findings calls the fold", "_fold_write_gate(b, machine, out)" in src("brain/mastercode/runtime/oversight.py"))

# ── 7. the terminal ───────────────────────────────────────────────────────────
print("the terminal")
from server.network import session_lane  # noqa: E402


async def _host_gate(pool, eid, server, op):
    return {"terms_bytes": b"{}", "signature": "s"}, None

session_lane.host_gate = _host_gate
st = run(session_lane.begin(Pool(enforcing={"grants": []}), 3, "box-7", "session:open", "sess-0001"))
check("session:open on a box that gates nothing: approved with no approval", st == {"state": "approved", "approval": ""})
st = run(session_lane.begin(pool, 3, "box-7", "session:open", "sess-0001"))
check("session:open on a gating box: pending on the device", st["state"] == "pending" and st["request"]["via"] == "device")
relay.device_state = "approved"
st = run(session_lane.poll(pool, 3, "box-7", "session:open", "sess-0001", existing=st["request"]))
check("poll reads the tap", st["state"] == "approved" and '"sig"' in st["approval"])
relay.device_state = "pending"
r = src("server/routes/network.py")
check("the terminal route begins/polls for either op", "session_lane.begin(" in r and "session_lane.poll(" in r)
hargs = nac_harness.harness_approval_args({"harness_id": "h1", "mode": "git", "image": "img", "repo": "owner/site",
                                           "branch": "main", "prompt": "fix", "command": ["a", "b"],
                                           "worktree_b64": "", "port": 0, "max_minutes": 10,
                                           "hosts": 0, "envs": {"B": "2", "A": "1"}, "tests": ["t1"],
                                           "engine": "e", "gate": "auto", "app": "app"})
check("the harness approval binds the whole run in the box's order (harness.go harnessArgs)",
      hargs == ["h1", "git", "img", "owner/site", "main", "fix", "a\x00b", "", "0", "10", "0", "A=1\nB=2", "t1", "e", "auto", "app"])
check("the harness run obtains an approval from those args",
      "nac_approval.obtain(" in src("server/network/nac_harness.py")
      and "harness_approval_args(spec)" in src("server/network/nac_harness.py"))
check("ctx_extract spends an approval on a gating box and says pending as pending (through the card, rung 8)",
      "nac_approval.obtain_via_card(pool, entity_id, chosen[\"server\"], EXTRACT_OP" in src("server/routes/context_extract.py")
      and '"pending_approval": True' in src("server/routes/context_extract.py"))
check("the contain buttons do not block on the tap",
      src("server/routes/network.py").count("approval_wait_s=0") == 2)

# ── 8. wiring ─────────────────────────────────────────────────────────────────
print("wiring")
mig = src("migrations/v179_host_ops_approvals.sql")
check("v179 adds the three approval columns",
      "network_bastion_heartbeat ADD COLUMN IF NOT EXISTS update_approval" in mig
      and "nac_agent_contain         ADD COLUMN IF NOT EXISTS approval" in mig
      and "nac_finding_contain       ADD COLUMN IF NOT EXISTS approval" in mig)
tl = src("brain/mastercode/runtime/tools.py")
check("mgmt_run passes the note and reads a pending approval as ok + state",
      'note=str(a.get("note") or "")' in tl and 'res.get("state") in ("pending_approval", "asked")' in tl)
check("the registry names note and the pending state",
      '"note": {"type": "string"' in src("brain/mastercode/public_tool_registry.py")
      and "pending_approval" in src("brain/mastercode/public_tool_registry.py"))
check("the extract job carries the document's stable name on both paths",
      src("server/routes/context_extract.py").count('"doc": f"ctx:{ctx_id}"') == 2)
check("the skill says what to do with a pending approval, and which ops never ask",
      "pending_approval" in src("default-skills/network.md") and "Reads never" in src("default-skills/network.md")
      and "never filed twice" not in src("default-skills/network.md")
      and "standing work under a store they configured" in src("default-skills/network.md"))
import re as _re  # noqa: E402
_nv = _re.search(r'^version: "(\d+)\.(\d+)\.(\d+)"', src("system-apps/network/app.yaml"), _re.M)
_app = src("system-apps/network/app.js")
check("the bastion card says what a standing update waits on, from the server's field",
      "? updatePendingPill(g)" in _app and "g.update_waits_on" in _app
      and all(w in _app for w in ("waiting for your approval", "update refused", "updated by its anchor Host",
                                  "updating on next check-in")))
_lines = _app.splitlines()


def _maps(state, words):
    return any(f"w === '{state}') return" in ln and words in ln for ln in _lines)


check("…each state with its own words",
      _maps("approval", "waiting for your approval") and _maps("no_way", "update needs an approval device")
      and _maps("refused", ">update refused<") and _maps("machine", ">update refused by the machine<")
      and _maps("anchor", ">updated by its anchor Host<"))
check("…the write-gate pills say a forward update goes through only where this Host is the anchor",
      _app.count("e.is_anchor === true && e.update_forward_needs_no_approval === true") == 2)
check("…and Update and Update all reload the list, so the card says at once what the update waits on",
      "await loadBastions();\n            setGwNotice(updateAskedNotice(name" in _app
      and "            await loadBastions();\n        }\n        if (!res) return;" in _app)
check("the Network app card reads the write gate", "writes need your approval" in src("system-apps/network/app.js")
      # AT LEAST the release that added the card (1.13.0) — a later bump for another
      # change must not read as the card going missing.
      and _nv is not None and tuple(int(x) for x in _nv.groups()) >= (1, 13, 0))

print()
if failures:
    print(f"FAILED ({len(failures)}): " + ", ".join(failures))
    sys.exit(1)
print("all green")
