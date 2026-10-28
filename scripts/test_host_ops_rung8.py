#!/usr/bin/env python3
"""test_host_ops_rung8.py — host-authored ops: every BUILT-IN write on a machine goes through the person's card, and the
Terminal needs no standing Host grant.

Pins, container-free (the shared stubs of scripts/host_ops_stubs.py — host_op_runs
and entity_tickets in memory, the two relays; the real nac_ops / nac_approval /
nac_mgmt / tickets code):

  1. the words: one line per built-in op from the bound arguments, never the
     Host's note; once / don't, never from now on;
  2. the mgmt door: a write on a gating box files ONE card per act (the mgmt_op
     purpose: op, machine, arguments) — kind op_ask, shape mgmt, locked to the
     user — and answers 'asked' with the ask; asked again → the same card; the
     Host cannot decide it; the click files the mgmt_op device ask with the
     details the box derives the purpose from; the next call waits on the tap
     and dispatches with the approval; ONE CARD ANSWERS ONE ACT — the answer is
     spent once the device answered it (run, refused, lapsed) or sat unread a
     day, and the same act asked again gets a fresh card, never a bare device
     ask; an open card past its day is closed as expired and a click past it
     files nothing; the ticket carries what the approval binds, never the raw
     argument; a card is keyed by the box's token as well as the act; a no is
     a refusal with the note that stands for a day; a card set aside reads
     the same; a box with no way to take an approval gets no card; a box that
     gates nothing gets no card and runs; the model's ctx_put is asked (the
     store's own writes are not); a card that could not be filed is the
     lane's error with the words;
  3. contain: the order is queued and the card is up; the click files the ask;
  4. the beat: a bastion update asks the device directly, with no card (an
     update card left open is closed, and a final answer an earlier card
     recorded still stands); the other directives file their card once (and
     back off while it is up), a newer ask's card replaces the open one, and a
     no on the beat is final for that act;
  5. the extract lane and the tool wrappers carry the ask; the kit raises it;
  6. the Terminal: the pod no longer refuses a shell for want of a grant (the
     session-lane gate pins the behaviour; here the source and the app words).

Run: APP_ROOT=<repo> python3 scripts/test_host_ops_rung8.py
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from tools_source import tools_source  # tools.py and the modules split out of it

ROOT = Path(os.environ.get("APP_ROOT") or Path(__file__).resolve().parent.parent)
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "brain"))
sys.path.insert(0, str(ROOT / "scripts"))

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


from server.network import nac_approval, nac_dcn, nac_mgmt, nac_ops  # noqa: E402
from server.routes import tickets as tk  # noqa: E402
from host_ops_stubs import MRelay, Pool, Relay  # noqa: E402

relay = Relay()
nac_dcn._dispatch_box = relay
nac_approval.POLL_S = 0.01
mrelay = MRelay()
nac_mgmt.get_mgmt_relay = lambda: mrelay
audits: list = []


async def _audit(pool, eid, server, op, exit_code=None, decision=None, **kw):
    audits.append((server, op, exit_code, decision))

nac_mgmt.audit_mgmt = _audit


async def _state(pool, eid):
    return [{"server": "*", "ops": ["ctx_extract", "contain", "model:drop", "ctx_put", "uptime"]}], set(), set()

nac_mgmt._gate_state = _state
nac_mgmt.mgmt_decision = lambda grants, eid, server, op, suspended, quarantined: True
nac_mgmt.mgmt_denied_by = lambda grants, server, op: None

GATING = {"write_gate": True, "device": True, "second_factor": True, "device_keys": ["K1"], "op_view_closed": True}
EID = 9003
pool = Pool(enforcing=GATING, devices={"K1": (55, "phone")})


def door(op, args, wait_s=0, p=None, note=""):
    return run(nac_mgmt.run_mgmt_op(p or pool, EID, "box-7", op, args, note=note, approval_wait_s=wait_s, origin="tool"))


def decide(tid, option, note="", actor="user"):
    return run(tk.decide_ticket_service(pool, EID, tid, option_id=option, note=note, actor=actor))


# ── 1. the words ─────────────────────────────────────────────────────────────
print("the words")
s, o = nac_ops.mgmt_card_words("ctx_extract", "box-7", ["pdf-text", "application/pdf", "ctx:12", "d" * 64], "one tap on your device (phone)")
check("ctx_extract: the document, its type, the reader, the machine — and once only",
      s.startswith("Your Host wants to read ctx:12 (application/pdf) with pdf-text on box-7.") and "runs once" in s
      and [x["id"] for x in o] == ["once", "no"] and o[0]["recommend"] is True and "one tap on your device (phone)" in o[0]["consequence"])
check("contain by pid and by finding",
      "stop the process 4242 on box-7" in nac_ops.mgmt_card_words("contain", "box-7", ["4242"], "x")[0]
      and "the program the finding abcdef012345 names" in nac_ops.mgmt_card_words("contain", "box-7", ["finding:abcdef0123456789"], "x")[0])
check("model:drop, self-update, mirror, ctx_put, ctx_del",
      "remove the model llama3 from box-7" in nac_ops.mgmt_card_words("model:drop", "box-7", ["llama3"], "x")[0]
      and "update the bastion on box-7 to 0.38.0" in nac_ops.mgmt_card_words("self-update", "box-7", ["0.38.0"], "x")[0]
      and "place the memory mirror on box-7 (slot s1, image img:tag)" in nac_ops.mgmt_card_words("mirror", "box-7", ["s1", "img", "tag", "sha"], "x")[0]
      and "write notes/a.txt into box-7's context store" in nac_ops.mgmt_card_words("ctx_put", "box-7", ["notes/a.txt", "aGk="], "x")[0]
      and "delete notes/a.txt from box-7's context store" in nac_ops.mgmt_card_words("ctx_del", "box-7", ["notes/a.txt"], "x")[0])
s, _ = nac_ops.mgmt_card_words("some:op", "box-7", ["a", "b" * 300], "x")
check("any other op: the op and every argument, a long one cut and counted", "run some:op on box-7:" in s and "arg 1: a" in s
      and "arg 2: " + "b" * 200 + "…[300 chars]" in s)

# ── 2. the mgmt door ─────────────────────────────────────────────────────────
print("the mgmt door")
res = door("model:drop", ["llama3"])
check("a write on a gating box: 'asked' with the ask — nothing on the device, nothing on the box",
      res.get("state") == "asked" and res["exit"] is None and res["ask"]["ticket_id"] == 1 and not relay.calls and not mrelay.calls
      and audits[-1][3] == "card-filed")
t = pool.tickets[1]
purpose = nac_approval.mgmt_op_purpose("model:drop", "box-7", ["llama3"])
check("the ticket is the pod's: kind op_ask, shape mgmt, locked to the user, bound to the act",
      t["metadata"]["kind"] == "op_ask" and t["metadata"]["op_ask"]["shape"] == "mgmt" and t["metadata"]["user_only"] is True
      and t["metadata"]["no_pattern"] is True and t["metadata"]["op_ask"]["purpose"] == purpose
      and t["metadata"]["op_ask"]["approval_args"] == ["llama3"] and t["expires_at"] is not None
      and "remove the model llama3 from box-7" in t["sitrep"] and [x["id"] for x in t["options"]] == ["once", "no"])
res = door("model:drop", ["llama3"])
check("asked again: the same card, no second ticket", res["ask"]["ticket_id"] == 1 and len(pool.tickets) == 1
      and "already in front" in res["next"] and audits[-1][3] == "card-filed")
res = door("model:drop", ["llama4"])
check("other arguments are another act: another card", res["ask"]["ticket_id"] == 2 and len(pool.tickets) == 2)
try:
    decide(1, "once", actor="host")
    check("the Host cannot decide its own card", False)
except tk.TicketError as e:
    check("the Host cannot decide its own card", e.status == 403)
relay.calls.clear()
d = decide(1, "once", note="go")
filed = relay.filed()
check("the click files the mgmt_op device ask with the details the box derives the purpose from",
      len(filed) == 1 and filed[0]["kind"] == "mgmt_op" and filed[0]["op"] == "model:drop" and filed[0]["server"] == "box-7"
      and filed[0]["args"] == ["llama3"] and d["op_ask"]["filed"] is True and d["op_ask"]["device"] == "phone"
      and pool.tickets[1]["metadata"]["op_ask_result"]["filed"] is True)
res = door("model:drop", ["llama3"])
check("the Host's next call waits on the SAME ask: pending, nothing dispatched",
      res.get("state") == "pending_approval" and "device" in res["next"] and not mrelay.calls
      and res["request"]["purpose"] == purpose and audits[-1][3] == "awaiting-approval")
relay.device_state = "approved"
res = door("model:drop", ["llama3"], wait_s=1)
check("approved: dispatched WITH the approval, the box's exit",
      res.get("exit") == 0 and mrelay.calls and mrelay.calls[-1][1] == "model:drop" and audits[-1][3] == "ran")
relay.device_state = "pending"
mrelay.calls.clear()
# ONE CARD ANSWERS ONE ACT: the answer is spent with the run.
r1 = pool.tickets[1]["metadata"]["op_ask_result"]
check("the once card's answer is spent on the ticket once the device answered and the act ran",
      r1.get("spent") is True and r1.get("outcome") == "approved" and r1.get("spent_at"))
relay.calls.clear()
res = door("model:drop", ["llama3"])
check("the SAME act again: a fresh card, not the old answer — nothing filed on the device, nothing run",
      res.get("state") == "asked" and res["ask"]["ticket_id"] == 3 and not relay.calls and not mrelay.calls)
check("the ticket carries what the approval binds, never the op's raw arguments (an inline extract's is the document)",
      "args" not in pool.tickets[3]["metadata"]["op_ask"] and pool.tickets[3]["metadata"]["op_ask"]["approval_args"] == ["llama3"])
# the device refuses after the click: the card is spent as refused and the refusal stands
decide(3, "once")
relay.device_state = "denied"
res = door("model:drop", ["llama3"])
check("the device refuses after the click: refused, the card spent as the person's refusal",
      res["exit"] == 126 and pool.tickets[3]["metadata"]["op_ask_result"].get("outcome") == "refused" and not mrelay.calls)
# (the device itself holds that refusal a day: a fresh card now could not be answered)
relay.calls.clear()
res = door("model:drop", ["llama3"])
check("a refusal on the device stands like a card's no: refused with the words, no fresh card, the device not rung",
      res["exit"] == 126 and "the person refused it when asked" in res["stderr"] and len(pool.tickets) == 3 and not relay.calls)
relay.device_state = "pending"
pool.tickets[3]["metadata"]["op_ask_result"]["spent_at"] = nac_ops._rfc3339(dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=25))
res = door("model:drop", ["llama3"])
check("…a day later the next ask of the same act is a fresh card, never a bare device ask",
      res.get("state") == "asked" and res["ask"]["ticket_id"] == 4 and not relay.calls)
# a device-side FAILURE after the click is not the person's refusal: spent as denied, and the next ask is a fresh card
decide(4, "once")
relay.device_state = "error"
res = door("model:drop", ["llama3"])
relay.device_state = "pending"
r4 = pool.tickets[4]["metadata"]["op_ask_result"]
res2 = door("model:drop", ["llama3"])
check("a device-side failure after the click is not the person's refusal: spent as denied, the next ask a fresh card",
      res["exit"] == 126 and "could not save" in res["stderr"] and "the person refused it when asked" not in res["stderr"]
      and r4.get("outcome") == "denied" and res2.get("state") == "asked" and res2["ask"]["ticket_id"] == 5)
# a once click whose answer was never read for a day is stale: a fresh card, no bare device ask
decide(5, "once")
pool.tickets[5]["decided_at"] = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=25)
relay.calls.clear()
res = door("model:drop", ["llama3"])
check("a once click left unread for a day is stale: a fresh card, and the device is not rung",
      res.get("state") == "asked" and res["ask"]["ticket_id"] == 6 and not relay.calls
      and pool.tickets[5]["metadata"]["op_ask_result"].get("outcome") == "stale")
# an open card unanswered past its day closes as expired and a fresh one follows
pool.tickets[6]["expires_at"] = dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=1)
res = door("model:drop", ["llama3"])
check("an open card past its day is closed as expired, with the reason, and a fresh card follows",
      pool.tickets[6]["status"] == "expired" and "unanswered for a day" in pool.tickets[6]["decision"]["note"]
      and res.get("state") == "asked" and res["ask"]["ticket_id"] == 7)
# a click past the card's day files nothing
pool.tickets[7]["expires_at"] = dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=1)
n_filed = len(relay.filed())
d = decide(7, "once")
res = door("model:drop", ["llama3"])
check("a click past the card's day files nothing — the ticket says so, and the next ask is a fresh card",
      d["op_ask"]["filed"] is False and "unanswered past its day" in d["op_ask"]["why"] and len(relay.filed()) == n_filed
      and res.get("state") == "asked" and res["ask"]["ticket_id"] == 8)
# by token: two boxes sharing a hostname do not share a card
ov = run(nac_approval.obtain_via_card(pool, EID, "box-7", "model:drop", ["llama3"], token_id=77))
ov2 = run(nac_approval.obtain_via_card(pool, EID, "box-7", "model:drop", ["llama3"], token_id=78))
check("the card is keyed by the box's token, not its hostname: another box with the same name gets its own card",
      ov["ticket_id"] == 8 and ov2["ticket_id"] == 9 and pool.tickets[9]["metadata"]["op_ask"]["token_id"] == 78)
# don't
n_filed = len(relay.filed())
d = decide(2, "no", note="keep it")
check("'don't' files nothing", d["op_ask"] == {"choice": "no", "filed": False} and len(relay.filed()) == n_filed)
res = door("model:drop", ["llama4"])
check("the Host's next call reads the no as a refusal with the person's note",
      res["exit"] == 126 and "chose not to run it" in res["stderr"] and "keep it" in res["stderr"] and not mrelay.calls
      and audits[-1][3] == "denied-person")
res = door("model:drop", ["llama4"])
check("asked again the same day: the same refusal, no new card", res["exit"] == 126 and len(pool.tickets) == 9)
pool.tickets[2]["decided_at"] = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=25)
res = door("model:drop", ["llama4"])
check("a day later the same act may be asked again: a fresh card", res.get("state") == "asked" and res["ask"]["ticket_id"] == 10)
run(tk.dismiss_ticket_service(pool, EID, 10, note="later", actor="user"))
res = door("model:drop", ["llama4"])
check("a card set aside in Tickets reads as a refusal with the note", res["exit"] == 126 and "set the card aside" in res["stderr"]
      and "later" in res["stderr"])
# THE CONSOLE (a factor, no device): the person's refusal at the machine holds the same way
pool_c = Pool(enforcing={"write_gate": True, "device": False, "second_factor": True, "stepup": True})
res = door("model:drop", ["llama3"], p=pool_c)
run(tk.decide_ticket_service(pool_c, EID, res["ask"]["ticket_id"], option_id="once", actor="user"))
check("console: the click files the ask at the machine", pool_c.tickets[1]["metadata"]["op_ask_result"].get("via") == "console")
relay.console_state = "denied"
res = door("model:drop", ["llama3"], p=pool_c)
res2 = door("model:drop", ["llama3"], p=pool_c)
check("a refusal at the machine's console after the click is the person's refusal: spent as refused, held, no fresh card",
      res["exit"] == 126 and pool_c.tickets[1]["metadata"]["op_ask_result"].get("outcome") == "refused"
      and res2["exit"] == 126 and "the person refused it when asked" in res2["stderr"] and len(pool_c.tickets) == 1)
nac_approval._last_ask.clear()
pool_c.tickets[1]["metadata"]["op_ask_result"]["spent_at"] = nac_ops._rfc3339(dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=3))
a_c = run(nac_approval._ask_directive(pool_c, EID, "box-7", "model:drop", ["llama3"], "n", 77))
check("…and final on the beat", a_c["state"] == "denied" and "not be asked again" in a_c["detail"] and len(pool_c.tickets) == 1)
relay.console_state = "pending"
res = door("model:drop", ["llama3"], p=pool_c)
check("…while in chat, after the day, a fresh card", res.get("state") == "asked" and res["ask"]["ticket_id"] == 2)
run(tk.decide_ticket_service(pool_c, EID, 2, option_id="once", actor="user"))
relay.console_state = "error"
res = door("model:drop", ["llama3"], p=pool_c)
relay.console_state = "pending"
res2 = door("model:drop", ["llama3"], p=pool_c)
check("a failure at the machine's console is not the person's refusal: spent as denied, the next ask a fresh card",
      res["exit"] == 126 and "could not save" in res["stderr"] and pool_c.tickets[2]["metadata"]["op_ask_result"].get("outcome") == "denied"
      and res2.get("state") == "asked" and res2["ask"]["ticket_id"] == 3)
# the refusal typed at the console WHILE the Host polls the filed request (stepup_status, the `existing` branch)
run(tk.decide_ticket_service(pool_c, EID, 3, option_id="once", actor="user"))
relay.console_status_state = "denied"
relay.calls.clear()
res = door("model:drop", ["llama3"], p=pool_c, wait_s=1)
relay.console_status_state = None
res2 = door("model:drop", ["llama3"], p=pool_c)
check("a refusal at the console while the Host polls the request is the person's refusal too: spent as refused and held",
      [c[1] for c in relay.calls][:2] == ["stepup_request", "stepup_status"] and res["exit"] == 126
      and pool_c.tickets[3]["metadata"]["op_ask_result"].get("outcome") == "refused"
      and res2["exit"] == 126 and "the person refused it when asked" in res2["stderr"] and len(pool_c.tickets) == 3)
# the card could not be filed: the lane's error with the words, never a bare 500


class _NoTickets(Pool):
    async def fetchrow(self, sql, *a):
        if "INSERT INTO entity_tickets" in sql:
            raise RuntimeError("tickets table away")
        return await super().fetchrow(sql, *a)


try:
    door("model:drop", ["llama9"], p=_NoTickets(enforcing=GATING, devices={"K1": (55, "phone")}))
    check("a card that could not be filed is the lane's own error, with the words", False)
except nac_mgmt.MgmtError as e:
    check("a card that could not be filed is the lane's own error, with the words",
          e.status == 500 and "could not put the card in front of the person" in e.detail and "tickets table away" in e.detail)
# no way to take an approval: no card
before = len(pool.tickets)
res = door("model:drop", ["llama5"], p=Pool(enforcing={"write_gate": True, "device": False, "second_factor": False}))
check("a machine with no way to take an approval: refused with the fix, no card",
      res["exit"] == 126 and "pin your approval device" in res["stderr"] and len(pool.tickets) == before)
res = door("model:drop", ["llama5"], p=Pool(enforcing={"grants": []}))
check("a box that gates nothing: no card, dispatched with no approval",
      res.get("exit") == 0 and mrelay.calls[-1][1] == "model:drop" and len(pool.tickets) == before)
res = door("ctx_put", ["notes/a.txt", "aGk="])
check("the model's ctx_put is asked, with the path in the words",
      res.get("state") == "asked" and "write notes/a.txt into box-7's context store" in res["ask"]["sitrep"])
res = run(nac_mgmt.run_mgmt_op(pool, EID, "box-7", "ctx_put", ["notes/b.txt", "aGk="], approval_wait_s=0, origin="service"))
check("the context store's own ctx_put is standing work: no card, dispatched", res.get("exit") == 0 and mrelay.calls[-1][1] == "ctx_put")
res = door("uptime", [])
check("a read never has a card", res.get("exit") == 0 and mrelay.calls[-1][1] == "uptime")

# ── 3. contain ───────────────────────────────────────────────────────────────
print("contain")
from server.network import topology, nac_agents  # noqa: E402
queued: list = []


async def _request_contain(pool_, eid, token_id, pid, approval=""):
    queued.append((pid, approval))
    return "queued"

topology.request_contain = _request_contain


async def _kind(pool_, token_id, pid):
    return "agent"

nac_agents.observation_kind = _kind
res = door("contain", ["4242"])
check("contain: the order is queued without its approval and the card is up",
      queued == [(4242, "")] and res.get("state") == "asked" and res.get("queued") is True
      and "stop the process 4242 on box-7" in res["ask"]["sitrep"])
relay.calls.clear()
decide(res["ask"]["ticket_id"], "once")
check("the click files the contain ask", relay.filed()[-1]["op"] == "contain" and relay.filed()[-1]["args"] == ["4242"])
relay.device_state = "approved"
res = door("contain", ["4242"], wait_s=1)
check("approved: the order carries the approval", queued[-1][0] == 4242 and '"sig"' in queued[-1][1])
relay.device_state = "pending"

# ── 4. the beat ──────────────────────────────────────────────────────────────
print("the beat")
# A bastion update asks the person's DEVICE directly, with no card in front of
# it (nac_approval._ask_directive): the device approves every machine waiting
# with one "Approve all shown".
nac_approval._last_ask.clear()
relay.calls.clear()
n_t = len(pool.tickets)
a1 = run(nac_approval._ask_directive(pool, EID, "box-7", "self-update", ["0.38.0"], "n", 77))
a2 = run(nac_approval._ask_directive(pool, EID, "box-7", "self-update", ["0.38.0"], "n", 77))
check("an update asks the device directly, with no card, and backs off while it waits",
      a1["state"] == "pending" and a2["state"] == "backoff" and len(pool.tickets) == n_t
      and relay.filed()[-1]["op"] == "self-update" and relay.filed()[-1]["args"] == ["0.38.0"])
nac_approval._last_ask.clear()
relay.device_state = "approved"
a3 = run(nac_approval._ask_directive(pool, EID, "box-7", "self-update", ["0.38.0"], "n", 77))
check("the next beat reads the tap", a3["state"] == "approved")
relay.device_state = "pending"
# An update card left open from before (the card path, as it was) is closed,
# and the device is asked instead.
legacy = run(nac_approval.obtain_via_card(pool, EID, "box-7", "self-update", ["0.39.0"], wait_s=0, token_id=77,
                                          final_no=True))
t_old = pool.tickets[legacy["ticket_id"]]
nac_approval._last_ask.clear()
relay.calls.clear()
a4 = run(nac_approval._ask_directive(pool, EID, "box-7", "self-update", ["0.39.0"], "n", 77))
check("an update card left open from before is closed, with the reason, and the device is asked instead",
      legacy["state"] == "asked" and t_old["status"] == "expired" and "approval device" in t_old["decision"]["note"]
      and a4["state"] == "pending" and relay.filed()[-1]["args"] == ["0.39.0"])
# A final answer an earlier card recorded for THIS update still stands.
legacy = run(nac_approval.obtain_via_card(pool, EID, "box-7", "self-update", ["0.40.0"], wait_s=0, token_id=77,
                                          final_no=True))
decide(legacy["ticket_id"], "no", note="not this one")
nac_approval._last_ask.clear()
relay.calls.clear()
a5 = run(nac_approval._ask_directive(pool, EID, "box-7", "self-update", ["0.40.0"], "n", 77))
check("a no given on an earlier card for this update still stands: refused with the note, nothing asked",
      a5["state"] == "denied" and "not this one" in a5["detail"] and "not be asked again" in a5["detail"]
      and not relay.filed())
legacy = run(nac_approval.obtain_via_card(pool, EID, "box-7", "self-update", ["0.41.0"], wait_s=0, token_id=77,
                                          final_no=True))
decide(legacy["ticket_id"], "once")
relay.device_state = "denied"
nac_approval._last_ask.clear()
r_old = run(nac_approval.obtain_via_card(pool, EID, "box-7", "self-update", ["0.41.0"], wait_s=0, token_id=77,
                                         final_no=True))
relay.device_state = "pending"
nac_approval._last_ask.clear()
relay.calls.clear()
a6 = run(nac_approval._ask_directive(pool, EID, "box-7", "self-update", ["0.41.0"], "n", 77))
check("a refusal on the device that an earlier card recorded still stands: nothing asked",
      r_old["state"] == "denied" and a6["state"] == "denied" and "not be asked again" in a6["detail"]
      and not relay.filed())
# A refusal on the device is RECORDED by the Host (v190) and final for that
# version: the device forgets it 24 hours after the ask was filed, and automatic
# updates queue the version again every 10 minutes.
relay.device_state = "denied"
nac_approval._last_ask.clear()
a7 = run(nac_approval._ask_directive(pool, EID, "box-7", "self-update", ["0.42.0"], "n", 77))
relay.device_state = "pending"
nac_approval._last_ask.clear()
relay.calls.clear()
a8 = run(nac_approval._ask_directive(pool, EID, "box-7", "self-update", ["0.42.0"], "n", 77))
check("a refusal on the device is recorded, and the same version is never asked again",
      a7["state"] == "denied" and (77, "0.42.0") in pool.refusals
      and a8["state"] == "denied" and "0.42.0" in a8["detail"] and not relay.filed())
nac_approval._last_ask.clear()
relay.calls.clear()
a9 = run(nac_approval._ask_directive(pool, EID, "box-7", "self-update", ["0.43.0"], "n", 77))
check("…a newer version is a new question: asked",
      a9["state"] == "pending" and relay.filed()[-1]["args"] == ["0.43.0"])
relay.device_state = "denied"
nac_approval._last_ask.clear()
run(nac_approval._ask_directive(pool, EID, "box-7", "self-update", ["0.43.0"], "n", 77))
relay.device_state = "pending"
check("…and a no to it is kept beside the older one: a no stands whichever way the target moves",
      pool.refusals == {(77, "0.42.0"), (77, "0.43.0")})
relay.device_state = "error"
nac_approval._last_ask.clear()
run(nac_approval._ask_directive(pool, EID, "box-7", "self-update", ["0.44.0"], "n", 77))
relay.device_state = "pending"
check("a device-side failure is not the person's no: nothing recorded",
      (77, "0.44.0") not in pool.refusals)
# A refusal typed at the machine's console (no device pinned) is the person's no too.
CONSOLE = {"write_gate": True, "second_factor": True, "stepup": True, "device": False}
cpool = Pool(enforcing=CONSOLE)
relay.console_state = "denied"
nac_approval._last_ask.clear()
cr = run(nac_approval._ask_directive(cpool, EID, "box-8", "self-update", ["0.43.0"], "n", 88))
relay.console_state = "pending"
check("a refusal at the machine's console is recorded the same way",
      cr["state"] == "denied" and (88, "0.43.0") in cpool.refusals)
# An OLDER version's open card is closed when a newer version is asked.
legacy = run(nac_approval.obtain_via_card(pool, EID, "box-7", "self-update", ["0.36.0"], wait_s=0, token_id=77,
                                          final_no=True))
t_old = pool.tickets[legacy["ticket_id"]]
nac_approval._last_ask.clear()
run(nac_approval._ask_directive(pool, EID, "box-7", "self-update", ["0.45.0"], "n", 77))
check("an older version's open update card is closed when a newer version is asked",
      t_old["status"] == "expired" and "approval device" in t_old["decision"]["note"])
# An earlier once card that is still waiting, or was approved, does not stop the ask.
legacy = run(nac_approval.obtain_via_card(pool, EID, "box-7", "self-update", ["0.46.0"], wait_s=0, token_id=77,
                                          final_no=True))
decide(legacy["ticket_id"], "once")
nac_approval._last_ask.clear()
relay.calls.clear()
a10 = run(nac_approval._ask_directive(pool, EID, "box-7", "self-update", ["0.46.0"], "n", 77))
check("an earlier card clicked once and still waiting on the device does not stop the ask",
      a10["state"] == "pending" and relay.filed()[-1]["args"] == ["0.46.0"])
legacy = run(nac_approval.obtain_via_card(pool, EID, "box-7", "self-update", ["0.47.0"], wait_s=0, token_id=77,
                                          final_no=True))
decide(legacy["ticket_id"], "once")
relay.device_state = "approved"
nac_approval._last_ask.clear()
run(nac_approval.obtain_via_card(pool, EID, "box-7", "self-update", ["0.47.0"], wait_s=0, token_id=77, final_no=True))
nac_approval._last_ask.clear()
a11 = run(nac_approval._ask_directive(pool, EID, "box-7", "self-update", ["0.47.0"], "n", 77))
relay.device_state = "pending"
check("…nor does one whose approval was already used: the device is asked, and answers",
      pool.tickets[legacy["ticket_id"]]["metadata"]["op_ask_result"].get("outcome") == "approved"
      and a11["state"] == "approved")
# A card set aside in Tickets holds a day, as it did.
legacy = run(nac_approval.obtain_via_card(pool, EID, "box-7", "self-update", ["0.48.0"], wait_s=0, token_id=77,
                                          final_no=True))
run(tk.dismiss_ticket_service(pool, EID, legacy["ticket_id"], note="later", actor="user"))
nac_approval._last_ask.clear()
relay.calls.clear()
a12 = run(nac_approval._ask_directive(pool, EID, "box-7", "self-update", ["0.48.0"], "n", 77))
pool.tickets[legacy["ticket_id"]]["decided_at"] = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=2)
nac_approval._last_ask.clear()
a13 = run(nac_approval._ask_directive(pool, EID, "box-7", "self-update", ["0.48.0"], "n", 77))
check("an update card set aside in Tickets holds a day, and is asked on the device after it",
      a12["state"] == "denied" and "set the card aside" in a12["detail"] and a13["state"] == "pending"
      and relay.filed() and relay.filed()[-1]["args"] == ["0.48.0"])
# Any update card left open is closed on the box's first beat, even when no
# update is asked for (a forward move, a member box).
legacy = run(nac_approval.obtain_via_card(pool, EID, "box-9", "self-update", ["0.49.0"], wait_s=0, token_id=99,
                                          final_no=True))
t_left = pool.tickets[legacy["ticket_id"]]
run(nac_ops.close_update_cards_once(pool, EID, 99))
legacy2 = run(nac_approval.obtain_via_card(pool, EID, "box-9", "self-update", ["0.50.0"], wait_s=0, token_id=99,
                                           final_no=True))
run(nac_ops.close_update_cards_once(pool, EID, 99))
check("a box's open update cards are closed once per process, whatever it is asked",
      t_left["status"] == "expired" and "approval device" in t_left["decision"]["note"]
      and pool.tickets[legacy2["ticket_id"]]["status"] == "open")
# A close that fails does not mark the box done: the next beat tries again.
legacy = run(nac_approval.obtain_via_card(pool, EID, "box-6", "self-update", ["0.51.0"], wait_s=0, token_id=66,
                                          final_no=True))
_real_execute = pool.execute


async def _failing_execute(sql, *a):
    if "SET status='expired'" in sql and "->>'shape' = 'mgmt'" in sql:
        raise RuntimeError("database away")
    return await _real_execute(sql, *a)

pool.execute = _failing_execute
run(nac_ops.close_update_cards_once(pool, EID, 66))
pool.execute = _real_execute
still_open = pool.tickets[legacy["ticket_id"]]["status"] == "open"
run(nac_ops.close_update_cards_once(pool, EID, 66))
check("a close that failed is tried again on the next beat",
      still_open and pool.tickets[legacy["ticket_id"]]["status"] == "expired")
# contain on the beat still goes through its card.
nac_approval._last_ask.clear()
c1 = run(nac_approval._ask_directive(pool, EID, "box-7", "contain", ["5151"], "n", 77))
check("contain on the beat still files its card", c1["state"] == "asked" and c1["ticket_id"] in pool.tickets)
# The other directives keep their card. A mirror placement: one at a time, a
# newer ask's card replaces the open one, and a no on the beat is final.
MA = ["slot-1", "img", "tag-1", "sha256:a"]
MB = ["slot-1", "img", "tag-2", "sha256:b"]
nac_approval._last_ask.clear()
m1 = run(nac_approval._ask_directive(pool, EID, "box-7", "mirror", MA, "n", 77))
t_m1 = pool.tickets[m1["ticket_id"]]
nac_approval._last_ask.clear()
m2 = run(nac_approval._ask_directive(pool, EID, "box-7", "mirror", MB, "n", 77))
check("a mirror placement still files its card, and a newer one's card replaces the open one, with the reason",
      m1["state"] == "asked" and m2["state"] == "asked" and t_m1["status"] == "expired"
      and "newer ask" in t_m1["decision"]["note"] and pool.tickets[m2["ticket_id"]]["status"] == "open")
decide(m2["ticket_id"], "no", note="not this one")
nac_approval._last_ask.clear()
m3 = run(nac_approval._ask_directive(pool, EID, "box-7", "mirror", MB, "n", 77))
pool.tickets[m2["ticket_id"]]["decided_at"] = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=3)
nac_approval._last_ask.clear()
n_t = len(pool.tickets)
m4 = run(nac_approval._ask_directive(pool, EID, "box-7", "mirror", MB, "n", 77))
check("a no on the beat is final for that act: refused with the note, and days later still no fresh card",
      m3["state"] == "denied" and m4["state"] == "denied" and "not this one" in m4["detail"]
      and "not be asked again" in m4["detail"] and len(pool.tickets) == n_t)

# ── 5. the extract lane, the wrappers, the kit ───────────────────────────────
print("the extract lane and the wrappers")
xs = src("server/routes/context_extract.py")
check("ctx_extract goes through the card and says asked as asked",
      'nac_approval.obtain_via_card(pool, entity_id, chosen["server"], EXTRACT_OP' in xs
      and '"asked": True' in xs and '"ask": appr.get("ask") or {}' in xs and "card-filed" in xs)
ts = tools_source()
check("the tool wrappers pass an asked result through as ok, with the ask on it",
      'res.get("state") in ("pending_approval", "asked")' in ts and 'if res.get("pending_approval"):' in ts
      and ts.index('if res.get("pending_approval"):') < ts.index('if res.get("error"):\n                return {"status": "error", "error": res["error"], "result": res}'))
kit = src("app-web-kit/ask.ts")
check("the kit raises the card from mgmt_run and context_ingest results — and no other tool's",
      'const RESULT_ASK_TOOLS = new Set(["pipeline", "mgmt_run", "context_ingest", "turn_type"]);' in kit)
pr = nac_approval.pending_result("box-7", "model:drop", {"state": "asked", "ticket_id": 9, "ask": {"ticket_id": 9}, "next": "n"})
check("pending_result carries the ask for an asked state", pr["state"] == "asked" and pr["ask"] == {"ticket_id": 9} and pr["exit"] is None)
ov = run(nac_approval.obtain_via_card(Pool(enforcing={"grants": []}), EID, "box-7", "model:drop", ["x"]))
check("obtain_via_card answers what obtain answers without asking: a box that gates nothing", ov["state"] == "not_needed")
check("…a read, a tighten, standing work",
      run(nac_approval.obtain_via_card(pool, EID, "box-7", "uptime", []))["state"] == "not_needed"
      and run(nac_approval.obtain_via_card(pool, EID, "box-7", "dcn_grant_drop", ["1"]))["state"] == "not_needed"
      and run(nac_approval.obtain_via_card(pool, EID, "box-7", "backup_fetch", ["{}"]))["state"] == "not_needed")

# ── 6. the Terminal ──────────────────────────────────────────────────────────
print("the terminal")
sl = src("server/network/session_lane.py")
check("the pod no longer refuses a shell for want of a grant; a deny, suspended and quarantined still refuse",
      "no signed grant (default-deny)" not in sl and "if reason is not None:\n        return BastionSession.refused" in sl
      and "denied = None if held else nac_mgmt.mgmt_denied_by(grants, server, op)" in sl)
check("the S frame carries the grant only when one lists the op",
      'if grant.get("terms_bytes") else ""' in sl and '"grant_sig_b64": grant.get("signature") or ""' in sl)
app = src("system-apps/terminal/app.js")
check("the Terminal's words no longer blame a missing grant", "does not list session:open" not in app and "needs no standing grant" in src("system-apps/terminal/index.html"))
_term_yaml = src("system-apps/terminal/app.yaml")
_term_ver = re.search(r'^version:\s*"(\d+)\.(\d+)\.(\d+)"', _term_yaml, re.M)
check("the Terminal app is bumped (to 0.1.3 or later) and says so",
      _term_ver is not None and tuple(int(x) for x in _term_ver.groups()) >= (0, 1, 3)
      and "# 0.1.3 (2026-09-25): a shell needs no standing Host grant" in _term_yaml)

print("wiring")
check("this gate is registered (its own file in gates-container-free.d/)",
      (ROOT / "scripts/gates-container-free.d/test_host_ops_rung8.txt").is_file()
      and "test_host_ops_rung8.py" in src("scripts/gates-container-free.d/test_host_ops_rung8.txt"))
skill = src("default-skills/network.md")
check("the skill says the card comes first for a built-in write", "The ask is a CARD" in skill and "END YOUR TURN" in skill)
check("the registry says asked then pending", "puts their card up ('asked': carry on)" in src("brain/mastercode/public_tool_registry.py"))
node = shutil.which("node")
if node:
    r = subprocess.run([node, "--test", "test/ask.test.ts"], cwd=ROOT / "circles-web", capture_output=True, text=True, timeout=120)
    check("circles-web/test/ask.test.ts passes (the card from context_ingest too)", r.returncode == 0 and "# fail 0" in r.stdout,
          (r.stdout + r.stderr)[-600:])
else:
    check("node present to run the kit test (SKIPPED — no node on PATH)", True)

print()
if failures:
    print(f"{len(failures)} FAILED:")
    for f in failures:
        print("  - " + f)
    sys.exit(1)
print("OK: all checks passed")
