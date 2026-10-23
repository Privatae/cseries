#!/usr/bin/env python3
"""test_host_ops_rung7.py — host-authored ops: the
approval card. An op's apply is never asked for on the device behind the
person's back: `op_apply` puts a card in front of them — a decision ticket the
pod files in the platform's words, the machine's own dry-run diff folded under
it, the choice once / from now on / don't — and their click files the ask on
their own approval device; the Host's next call waits on the tap and applies.

Pins, container-free (stub pool with entity_tickets + host_op_runs in memory,
stub relays, a fixed signing key, the real nac_ops / tickets code):

  1. the words: reusable or not (root never; a glob effect never), the bounds
     "from now on" pins by default, the bounds as a person reads them;
  2. op_apply files ONE card per dry run — kind op_ask, locked to the user —
     with the sitrep from the manifest, the options the op allows, the diff
     as the attachment; asked again before the answer → the same card; a
     second dry run of the same op while a card is open → that open card;
  3. the Host cannot decide its own card; "don't" comes back to the Host as a
     refusal with the person's note, final; a dismissed card the same;
  4. "do it once": the click files the device ask with the op_run details
     under the purpose the box recomputes; the Host's next call waits on the
     same ask and applies exactly the dry run's terms;
  5. "from now on": the click makes the grant row (the card's own day, the
     pinned bounds) and files the op_grant ask; the next call installs the
     grant and runs THIS run through it; later runs inside the bounds ask
     nobody and are listed on the card; outside them → a new card; a Host's
     wider proposal must contain the run shown;
  6. op_grant asks for the standing approval on the newest fitting dry run —
     never without one; a device offline at the click leaves the outcome on
     the ticket and the next call files again; a machine with no way to take
     an approval gets no card; POST /api/tickets cannot forge one; the tool
     result carries the ask; the kit, the panes, the skill, the registry.

Run: APP_ROOT=<repo> python3 scripts/test_host_ops_rung7.py
"""
from __future__ import annotations

import asyncio
import base64
import datetime as dt
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

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

# ── the ops on disk ───────────────────────────────────────────────────────────
MANIFEST = """name: {name}
version: 1.0.0
summary: Write hello.txt under the test folder.
target: machine
privilege: {priv}
interpreter: /bin/sh
time_limit: 30
params:
  name: {{type: string, max_len: 32}}
  count: {{type: integer, min: 1, max: 5}}
effects:
  files: [{effects}]
modes: [dry_run, apply, verify]
undo: {undo}
"""

projects = Path(tempfile.mkdtemp(prefix="rung7-projects-"))


def make_op(name: str, priv: str, effects: str = '"/srv/hostop/"', undo: str = "none") -> Path:
    d = projects / "1" / "ops-mod" / "ops" / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "op.yaml").write_text(MANIFEST.format(name=name, priv=priv, effects=effects, undo=undo)
                               .replace("modes: [dry_run, apply, verify]", "modes: [dry_run, apply, verify, undo]")
                               if undo == "run" else MANIFEST.format(name=name, priv=priv, effects=effects, undo=undo))
    (d / "run").write_text("#!/bin/sh\necho hi\n")
    return d


make_op("write-hello", "root")
make_op("write-user", "user", undo="run")
make_op("write-glob", "user", effects='"/srv/hostop/*.txt"')

from server.routes import project_modules as _pm  # noqa: E402
_pm.module_dir = lambda project_id, name: projects / str(project_id) / name

from cryptography.hazmat.primitives.asymmetric import ed25519  # noqa: E402
KEY = ed25519.Ed25519PrivateKey.generate()


async def _sign(pool, eid, terms):
    from server.network.pairing_grants import canonical
    raw = canonical(terms)
    return base64.b64encode(raw).decode(), base64.b64encode(KEY.sign(raw)).decode()

nac_ops._sign = _sign


from host_ops_stubs import terms_of  # noqa: E402


# ── 1. the words ─────────────────────────────────────────────────────────────
print("the words")
m_root = nac_ops.parse_manifest([("op.yaml", 0o644, MANIFEST.format(name="x", priv="root", effects='"/srv/hostop/"', undo="none").encode()), ("run", 0o755, b"")])
m_user = nac_ops.parse_manifest([("op.yaml", 0o644, MANIFEST.format(name="x", priv="user", effects='"/srv/hostop/"', undo="none").encode()), ("run", 0o755, b"")])
m_glob = nac_ops.parse_manifest([("op.yaml", 0o644, MANIFEST.format(name="x", priv="user", effects='"/srv/hostop/[ab]*"', undo="none").encode()), ("run", 0o755, b"")])
check("the manifest carries undo", m_root["undo"] == "none")
check("root is never reusable", "root" in nac_ops.reusable_why_not(m_root))
check("a glob effect is never reusable", "pattern" in nac_ops.reusable_why_not(m_glob))
check("a user op with concrete effects is", nac_ops.reusable_why_not(m_user) == "")
check("default bounds pin every param to the run's value",
      nac_ops.default_bounds(m_user, {"name": "x", "count": 2}) == {"name": {"values": ["x"]}, "count": {"min": 2, "max": 2}})
check("bounds as a person reads them",
      nac_ops.bounds_words({"count": {"min": 1, "max": 5}, "name": {"values": ["x"]}, "dir": {"prefix": "/srv/x"}, "f": {"fixed": True}})
      == "count 1..5, name x, dir under /srv/x, f true"
      and nac_ops.bounds_words({"count": {"min": 2, "max": 2}}) == "count 2")
rep = {"changes": [{"path": "/srv/hostop/hello.txt", "kind": "created", "size": 6, "mode": "0644", "diff": "+ hello\n"}]}
sitrep, options, att = nac_ops.card_words(m_root, "box-7", rep, None, "one tap on your device (phone)")
check("the sitrep is the platform's words: op, machine, root, summary, count, undo",
      sitrep.startswith("Your Host wants to run x 1.0.0 on box-7 as root.") and "Write hello.txt" in sitrep
      and "It would change 1 file there" in sitrep and "It cannot be undone." in sitrep and "Once only — it runs as root" in sitrep)
check("a root op offers once and don't — never from now on", [o["id"] for o in options] == ["once", "no"] and options[0]["recommend"] is True
      and "one tap on your device (phone)" in options[0]["consequence"])
check("the attachment is the dry run's diff", att["label"] == "What it changes on box-7" and "+ hello" in att["text"])
prop = nac_ops._proposal('{"count":{"max":2,"min":2},"name":{"values":["x"]}}', 20, 30)
sitrep, options, att = nac_ops.card_words(m_user, "box-7", rep, prop, "one tap on your device (phone)", "grant")
day = nac_ops._day(prop["expires"])
check("a user op offers from now on, until the card's own day, with the bounds and the uses",
      [o["id"] for o in options] == ["once", "grant", "no"] and options[1]["label"] == f"Let me do this from now on (until {day})"
      and "up to 20 runs with count 2, name x" in options[1]["consequence"] and options[1]["recommend"] is True
      and "privatae-bastion ops drop" in options[1]["consequence"])
m_undo = dict(m_user, undo="run")
check("an op with an undo says so", "It can be undone" in nac_ops.card_words(m_undo, "b", rep, None, "x")[0])
check("no changes is said, not counted as zero files", "changed nothing" in nac_ops.card_words(m_user, "b", {"changes": []}, None, "x")[0])


# ── the stubs (scripts/host_ops_stubs.py) ────────────────────────────────────
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

GATING = {"write_gate": True, "device": True, "second_factor": True, "device_keys": ["K1"], "op_view_closed": True}
pool = Pool(enforcing=GATING, devices={"K1": (55, "phone")})
EID = 9003


def door(op, args, wait_s=0):
    return run(nac_ops.run_door(pool, EID, "box-7", 77, op, args, wait_s=wait_s))


def decide(tid, option, note="", actor="user"):
    return run(tk.decide_ticket_service(pool, EID, tid, option_id=option, note=note, actor=actor))


# ── 2. the card ──────────────────────────────────────────────────────────────
print("the card")
door("op_dry_run", ["write-hello", '{"name": "x", "count": 2}'])
mrelay.calls.clear()
relay.calls.clear()
res = door("op_apply", ["write-hello", '{"count": 2, "name": "x"}'])
out = json.loads(res["stdout"])
t1 = pool.tickets.get(1)
check("op_apply files the card and answers 'asked' with the ask — nothing on the device, nothing on the box",
      res["exit"] == 0 and out["state"] == "asked" and out["ticket_id"] == 1 and res.get("ask", {}).get("ticket_id") == 1
      and not relay.calls and not mrelay.calls and t1 is not None)
check("the ticket is the pod's: kind op_ask, locked to the user, never a pattern, source runtime",
      t1["metadata"]["kind"] == "op_ask" and t1["metadata"]["user_only"] is True and t1["metadata"]["no_pattern"] is True
      and t1["source"] == "runtime")
check("its words are the platform's, from the manifest and the run",
      t1["sitrep"].startswith("Your Host wants to run write-hello 1.0.0 on box-7 as root.") and "Write hello.txt" in t1["sitrep"]
      and res["ask"]["sitrep"] == t1["sitrep"])
check("a root op's card offers once and don't", [o["id"] for o in t1["options"]] == ["once", "no"]
      and res["ask"]["options"] == t1["options"])
check("the attachment is the machine's own diff, on the ask and on the ticket",
      "+ hello" in res["ask"]["attachment"]["text"] and res["ask"]["attachment"]["label"] == "What it changes on box-7"
      and "+ hello" in t1["metadata"]["op_ask"]["changes"])
ask1 = t1["metadata"]["op_ask"]
dry1 = pool.runs[0]
check("the card names its run and carries the terms' fields for the click",
      ask1["run_id"] == dry1["id"] and ask1["sha256"] == dry1["sha256"] and ask1["params"] == '{"count":2,"name":"x"}'
      and ask1["changes_sha256"] == "c" * 64 and ask1["server"] == "box-7" and ask1["token_id"] == 77
      and ask1["expires"] == dry1["expires"].strftime("%Y-%m-%dT%H:%M:%SZ") and ask1["grant"] is None
      and ask1["binding"] == "one tap on your device (phone)")
check("the run awaits the person; the audit says card-filed", dry1["state"] == "awaiting" and audits[-1][3] == "card-filed")
check("the Host is told to say what it asked and end the turn", "end your turn" in out["next"] and "Never decide" in out["next"])
res2 = door("op_apply", ["write-hello", '{"count": 2, "name": "x"}'])
check("asked again before the answer: the same card, no second ticket",
      json.loads(res2["stdout"])["ticket_id"] == 1 and len(pool.tickets) == 1 and "already in front" in json.loads(res2["stdout"])["next"]
      and audits[-1][3] == "card-open")
door("op_dry_run", ["write-hello", '{"name": "y", "count": 1}'])
res3 = door("op_apply", ["write-hello", '{"count": 1, "name": "y"}'])
check("a second dry run of the same op on the same machine while a card is open: THAT card, not another",
      json.loads(res3["stdout"])["ticket_id"] == 1 and len(pool.tickets) == 1)
check("the tool wrapper passes the ask through as an ok result",
      __import__("mastercode.runtime.tools", fromlist=["_refused_or_ok"])._refused_or_ok(res)["status"] == "ok"
      and __import__("mastercode.runtime.tools", fromlist=["_refused_or_ok"])._refused_or_ok(res)["result"]["ask"]["ticket_id"] == 1)

# ── 3. the person's no ───────────────────────────────────────────────────────
print("the person's no")
try:
    decide(1, "once", actor="host")
    check("the Host cannot decide its own card", False)
except tk.TicketError as e:
    check("the Host cannot decide its own card", e.status == 403)
try:
    run(tk.dismiss_ticket_service(pool, EID, 1, actor="host"))
    check("…nor dismiss it", False)
except tk.TicketError as e:
    check("…nor dismiss it", e.status == 403)
d = decide(1, "no", note="not on a Friday")
check("'don't' records the choice and files nothing", d["op_ask"] == {"choice": "no", "filed": False} and not relay.calls
      and pool.tickets[1]["metadata"]["op_ask_result"]["choice"] == "no" and dry1["state"] == "awaiting")
res = door("op_apply", ["write-hello", '{"count": 2, "name": "x"}'])
check("the Host's next call reads the no as a refusal with the person's note, final",
      res["exit"] == 126 and "chose not to run it" in res["stderr"] and "not on a Friday" in res["stderr"]
      and "only if they say so" in res["stderr"] and dry1["state"] == "refused" and audits[-1][3] == "denied-person")
# the second dry run (y) now gets its own card, since the first is closed
res = door("op_apply", ["write-hello", '{"count": 1, "name": "y"}'])
check("with the first card closed, the other run gets its own", json.loads(res["stdout"])["ticket_id"] == 2)
run(tk.dismiss_ticket_service(pool, EID, 2, note="later", actor="user"))
res = door("op_apply", ["write-hello", '{"count": 1, "name": "y"}'])
check("a card set aside in Tickets reads as a refusal too, with the note",
      res["exit"] == 126 and "set the card aside" in res["stderr"] and "dismissed" in res["stderr"] and "later" in res["stderr"])

# ── 4. do it once ────────────────────────────────────────────────────────────
print("do it once")
door("op_dry_run", ["write-hello", '{"name": "z", "count": 3}'])
res = door("op_apply", ["write-hello", '{"count": 3, "name": "z"}'])
tid = json.loads(res["stdout"])["ticket_id"]
dry = pool.runs[-1]
relay.calls.clear()
mrelay.calls.clear()
d = decide(tid, "once", note="go")
filed = relay.filed()
check("the click files the device ask at once, with the op_run details — the terms' fields and the diff",
      len(filed) == 1 and filed[0]["kind"] == "op_run" and filed[0]["sha256"] == dry["sha256"] and filed[0]["params"] == '{"count":3,"name":"z"}'
      and filed[0]["changes_sha256"] == "c" * 64 and filed[0]["uses"] == 1 and filed[0]["server"] == "box-7"
      and filed[0]["expires"] == dry["expires"].strftime("%Y-%m-%dT%H:%M:%SZ") and "+ hello" in filed[0]["dry_run"]
      and filed[0]["privilege"] == "root")
check("the outcome is on the ticket: filed, on the device, nothing ran",
      d["op_ask"]["filed"] is True and d["op_ask"]["via"] == "device" and d["op_ask"]["device"] == "phone"
      and pool.tickets[tid]["metadata"]["op_ask_result"]["filed"] is True and not mrelay.calls)
res = door("op_apply", ["write-hello", '{"count": 3, "name": "z"}'])
check("the Host's next call waits on the SAME ask (the same details, the box's purpose) — pending, nothing dispatched",
      res.get("state") == "pending_approval" and not mrelay.calls
      and {k: v for k, v in relay.filed()[-1].items() if k != "note"} == {k: v for k, v in filed[0].items() if k != "note"}
      and res["request"]["purpose"] == nac_ops.purpose_run("box-7", "write-hello", dry["sha256"], '{"count":3,"name":"z"}',
                                                            dry["expires"].strftime("%Y-%m-%dT%H:%M:%SZ"), "c" * 64)
      and dry["state"] == "awaiting")
relay.device_state = "approved"
res = door("op_apply", ["write-hello", '{"count": 3, "name": "z"}'], wait_s=1)
ta = terms_of(mrelay.calls[-1][2][0])
check("approved: synced then applied under the dry run's nonce, the approval and the change list",
      mrelay.ops() == ["op_sync", "op_apply"] and ta["nonce"] == dry["nonce"] and ta["approval"] == '{"v":1,"sig":"x"}'
      and ta["changes_sha256"] == "c" * 64 and res["exit"] == 0 and json.loads(res["stdout"])["state"] == "applied"
      and dry["state"] == "applied")
relay.device_state = "pending"

# ── 5. from now on ───────────────────────────────────────────────────────────
print("from now on")
door("op_dry_run", ["write-user", '{"name": "u", "count": 2}'])
res = door("op_apply", ["write-user", '{"count": 2, "name": "u"}'])
out = json.loads(res["stdout"])
tid = out["ticket_id"]
t = pool.tickets[tid]
dry = pool.runs[-1]
prop = t["metadata"]["op_ask"]["grant"]
check("a user op's card offers from now on, pinned to this run by default, 20 uses, 30 days",
      [o["id"] for o in t["options"]] == ["once", "grant", "no"] and out["reusable"] is True
      and prop["bounds"] == '{"count":{"max":2,"min":2},"name":{"values":["u"]}}' and prop["uses"] == 20 and prop["days"] == 30
      and "It can be undone" in t["sitrep"])
day = nac_ops._day(prop["expires"])
check("the day on the card is the day the grant will end", f"(until {day})" in t["options"][1]["label"])
relay.calls.clear()
mrelay.calls.clear()
d = decide(tid, "grant")
grant = pool.runs[-1]
filed = relay.filed()
check("the click makes the grant row — the card's own day, the pinned bounds, awaiting — and files the op_grant ask",
      grant["kind"] == "op_grant" and grant["state"] == "awaiting" and grant["bounds"] == prop["bounds"] and grant["uses"] == 20
      and grant["expires"].strftime("%Y-%m-%dT%H:%M:%SZ") == prop["expires"] and Pool._rep(grant)["ticket_id"] == tid
      and len(filed) == 1 and filed[0]["kind"] == "op_grant" and filed[0]["bounds"] == prop["bounds"] and filed[0]["uses"] == 20
      and filed[0]["expires"] == prop["expires"] and d["op_ask"]["grant_run_id"] == grant["id"] and d["op_ask"]["filed"] is True)
res = door("op_apply", ["write-user", '{"count": 2, "name": "u"}'])
check("pending on the device: the next call waits on the same grant ask", res.get("state") == "pending_approval"
      and res["request"]["purpose"] == nac_ops.purpose_grant("box-7", "write-user", dry["sha256"], prop["bounds"], prop["expires"], 20)
      and len(pool.runs) == len(pool.runs) and not mrelay.calls)
relay.device_state = "approved"
res = door("op_apply", ["write-user", '{"count": 2, "name": "u"}'], wait_s=1)
out = json.loads(res["stdout"])
tg = terms_of(mrelay.calls[0][2][0])
check("approved: the grant is installed (op_sync, the grant's nonce and the approval) and THIS run goes through it",
      mrelay.ops() == ["op_sync", "op_grant_run"] and tg["kind"] == "op_grant" and tg["nonce"] == grant["nonce"]
      and tg["approval"] == '{"v":1,"sig":"x"}' and mrelay.calls[1][2][:2] == [grant["nonce"], "apply"]
      and res["exit"] == 0 and out["state"] == "applied" and out["under_grant"] == grant["nonce"][:8]
      and out["grant"]["uses"] == 20 and grant["state"] == "installed" and dry["state"] == "dropped")
runs = pool.tickets[tid]["metadata"]["op_ask"]["runs"]
check("the run is listed on the card", len(runs) == 1 and runs[0]["state"] == "applied" and runs[0]["params"] == {"count": 2, "name": "u"})
relay.calls.clear()
mrelay.calls.clear()
relay.device_state = "pending"
n_tickets = len(pool.tickets)
res = door("op_apply", ["write-user", '{"count": 2, "name": "u"}'])
check("a later run inside the bounds asks nobody and is listed on the same card",
      not relay.calls and mrelay.ops() == ["op_grant_run"] and json.loads(res["stdout"])["state"] == "applied"
      and len(pool.tickets[tid]["metadata"]["op_ask"]["runs"]) == 2 and len(pool.tickets) == n_tickets)
door("op_dry_run", ["write-user", '{"name": "v", "count": 1}'])
res = door("op_apply", ["write-user", '{"count": 1, "name": "v"}'])
t_v = json.loads(res["stdout"])["ticket_id"]
check("params outside the bounds take the once path: a new card", json.loads(res["stdout"])["state"] == "asked"
      and t_v != tid and pool.tickets[t_v]["status"] == "open")
decide(t_v, "no")
door("op_apply", ["write-user", '{"count": 1, "name": "v"}'])
# a refused grant run is listed too
mrelay.refuse["op_grant_run"] = "op_grant_run refused: the run wrote outside the op's effects"
res = door("op_apply", ["write-user", '{"count": 2, "name": "u"}'])
check("a refused run under the grant is listed on the card with the box's words",
      res["exit"] == 126 and pool.tickets[tid]["metadata"]["op_ask"]["runs"][-1]["state"] == "refused"
      and "outside the op's effects" in pool.tickets[tid]["metadata"]["op_ask"]["runs"][-1]["error"])
del mrelay.refuse["op_grant_run"]
grant["state"] = "dropped"   # the person dropped it at the console
# The Host proposes wider bounds
door("op_dry_run", ["write-user", '{"name": "w", "count": 2}'])
try:
    door("op_apply", ["write-user", '{"count": 2, "name": "w"}', '{"count": {"min": 3, "max": 5}}'])
    check("a proposal that does not contain the run shown is refused on the pod", False)
except nac_ops.OpError as e:
    check("a proposal that does not contain the run shown is refused on the pod", e.status == 400 and "must cover the run" in e.detail)
res = door("op_apply", ["write-user", '{"count": 2, "name": "w"}', '{"count": {"min": 1, "max": 5}, "name": {"values": ["w", "x"]}}', "5", "7"])
t = pool.tickets[json.loads(res["stdout"])["ticket_id"]]
check("a wider proposal rides the card: its bounds, uses and days",
      t["metadata"]["op_ask"]["grant"]["uses"] == 5 and t["metadata"]["op_ask"]["grant"]["days"] == 7
      and "up to 5 runs with count 1..5, name w or x" in t["options"][1]["consequence"])
decide(t["id"], "no")
door("op_apply", ["write-user", '{"count": 2, "name": "w"}'])

# ── 6. op_grant, the edges, the wiring ───────────────────────────────────────
print("op_grant and the edges")
try:
    door("op_grant", ["write-hello", '{"count": {"min": 1, "max": 5}}'])
    check("op_grant on a root op is refused", False)
except nac_ops.OpError as e:
    check("op_grant on a root op is refused", e.status == 400 and "root" in e.detail)
try:
    door("op_grant", ["write-glob", '{"count": {"min": 1, "max": 5}}'])
    check("op_grant on an op with a glob effect is refused with the reason", False)
except nac_ops.OpError as e:
    check("op_grant on an op with a glob effect is refused with the reason", e.status == 400 and "pattern" in e.detail)
try:
    door("op_grant", ["write-user", '{"count": {"min": 4, "max": 5}}'])
    check("op_grant with no live dry run inside the bounds is refused: a card never appears without one", False)
except nac_ops.OpError as e:
    check("op_grant with no live dry run inside the bounds is refused: a card never appears without one",
          e.status == 409 and "op_dry_run" in e.detail)
door("op_dry_run", ["write-user", '{"name": "g", "count": 4}'])
res = door("op_grant", ["write-user", '{"count": {"min": 4, "max": 5}}', "3", "2"])
out = json.loads(res["stdout"])
t = pool.tickets[out["ticket_id"]]
check("op_grant raises the same card on the newest fitting dry run, from now on recommended",
      out["state"] == "asked" and t["metadata"]["op_ask"]["params"] == '{"count":4,"name":"g"}'
      and [o["id"] for o in t["options"]] == ["once", "grant", "no"] and t["options"][1]["recommend"] is True
      and t["options"][0]["recommend"] is False and t["metadata"]["op_ask"]["grant"]["uses"] == 3)
res = door("op_grant", ["write-user", '{"count": {"min": 4, "max": 5}}', "3", "2"])
check("asked again: the same card", json.loads(res["stdout"])["ticket_id"] == t["id"])
# a device offline at the click: the outcome is on the ticket; the next call files again
relay.offline = True
relay.calls.clear()
d = decide(t["id"], "grant")
check("the device offline at the click: recorded on the ticket, not an error",
      d["op_ask"]["filed"] is False and "not connected" in d["op_ask"]["why"] and d["op_ask"].get("grant_run_id")
      and pool.tickets[t["id"]]["metadata"]["op_ask_result"]["filed"] is False)
relay.offline = False
relay.calls.clear()
res = door("op_grant", ["write-user", '{"count": {"min": 4, "max": 5}}', "3", "2"])
check("the next call files the ask on the grant row the click made, and waits",
      res.get("state") == "pending_approval" and len(relay.filed()) == 1 and relay.filed()[0]["kind"] == "op_grant"
      and relay.filed()[0]["uses"] == 3)
# no way to take an approval: no card
door("op_dry_run", ["write-hello", '{"name": "n", "count": 1}'])
pool.enforcing = {"write_gate": True, "device": False, "second_factor": False, "op_view_closed": True}
before = len(pool.tickets)
try:
    door("op_apply", ["write-hello", '{"count": 1, "name": "n"}'])
    check("a machine with no way to take an approval gets no card", False)
except nac_ops.OpError as e:
    check("a machine with no way to take an approval gets no card", e.status == 409 and "pin your approval device" in e.detail
          and len(pool.tickets) == before)
pool.enforcing = {"write_gate": False, "op_view_closed": True}
try:
    door("op_apply", ["write-hello", '{"count": 1, "name": "n"}'])
    check("a machine that gates nothing gets no card either", False)
except nac_ops.OpError as e:
    check("a machine that gates nothing gets no card either", e.status == 409 and "no write gate" in e.detail)
pool.enforcing = GATING
# the machine's gate went away between the card and the click: said in words, not a state name
door("op_dry_run", ["write-hello", '{"name": "n2", "count": 1}'])
res = door("op_apply", ["write-hello", '{"count": 1, "name": "n2"}'])
pool.enforcing = {"write_gate": False, "op_view_closed": True}
d = decide(json.loads(res["stdout"])["ticket_id"], "once")
check("a gate that went away between the card and the click is said in words on the ticket",
      d["op_ask"]["filed"] is False and "no longer reports a write gate" in d["op_ask"]["why"])
pool.enforcing = GATING
check("POST /api/tickets cannot forge a card: kind and payload are stripped",
      tk._strip_platform_locks({"kind": "op_ask", "op_ask": {"run_id": 1}, "op_ask_result": {}, "x": 1}) == {"x": 1})

# ── cards that outlive their dry run, and the edges around them ─────────────
print("a card that outlives its dry run")
relay.device_state = "pending"
relay.calls.clear()
door("op_dry_run", ["write-hello", '{"name": "old", "count": 1}'])
res = door("op_apply", ["write-hello", '{"count": 1, "name": "old"}'])
t_old = pool.tickets[json.loads(res["stdout"])["ticket_id"]]
run_old = pool.runs[-1]
past = dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=1)
run_old["expires"] = run_old["expires_dt"] = past
t_old["metadata"]["op_ask"]["expires"] = past.strftime("%Y-%m-%dT%H:%M:%SZ")
d = decide(t_old["id"], "once")
check("a click on a card whose dry run lapsed files nothing and says why",
      d["op_ask"]["filed"] is False and "lapsed" in d["op_ask"]["why"] and not relay.calls)
# the same, for 'from now on' on a user op: no grant row is made either
door("op_dry_run", ["write-user", '{"name": "old", "count": 1}'])
res = door("op_apply", ["write-user", '{"count": 1, "name": "old"}'])
t_oldg = pool.tickets[json.loads(res["stdout"])["ticket_id"]]
run_oldg = pool.runs[-1]
run_oldg["expires"] = run_oldg["expires_dt"] = past
t_oldg["metadata"]["op_ask"]["expires"] = past.strftime("%Y-%m-%dT%H:%M:%SZ")
n_runs = len(pool.runs)
d = decide(t_oldg["id"], "grant")
check("…and 'from now on' on a lapsed card makes no grant row", d["op_ask"]["filed"] is False and "lapsed" in d["op_ask"]["why"]
      and len(pool.runs) == n_runs and "grant_run_id" not in d["op_ask"])
# an unanswered card whose run lapsed: the Host dry-runs again; the stale card is closed, a fresh one filed
door("op_dry_run", ["write-hello", '{"name": "old2", "count": 1}'])
res = door("op_apply", ["write-hello", '{"count": 1, "name": "old2"}'])
t_stale = pool.tickets[json.loads(res["stdout"])["ticket_id"]]
run_stale = pool.runs[-1]
run_stale["expires"] = run_stale["expires_dt"] = past
t_stale["metadata"]["op_ask"]["expires"] = past.strftime("%Y-%m-%dT%H:%M:%SZ")
try:
    door("op_apply", ["write-hello", '{"count": 1, "name": "old2"}'])
    check("a lapsed dry run: the Host is told to dry-run again", False)
except nac_ops.OpError as e:
    check("a lapsed dry run: the Host is told to dry-run again", e.status == 409 and "op_dry_run" in e.detail)
door("op_dry_run", ["write-hello", '{"name": "old2", "count": 1}'])
res = door("op_apply", ["write-hello", '{"count": 1, "name": "old2"}'])
t_fresh = pool.tickets[json.loads(res["stdout"])["ticket_id"]]
check("the stale card is closed as expired with the reason, and a FRESH card is filed for the new run — never the stale one handed back",
      t_stale["status"] == "expired" and t_stale["decision"].get("note") == nac_ops.STALE_LAPSED
      and t_fresh["id"] != t_stale["id"] and t_fresh["status"] == "open"
      and t_fresh["metadata"]["op_ask"]["run_id"] == pool.runs[-1]["id"])
# a new version of the op (another sha) while a card is open: the old card is closed as replaced
(projects / "1" / "ops-mod" / "ops" / "write-hello" / "run").write_text("#!/bin/sh\necho hi v2\n")
door("op_dry_run", ["write-hello", '{"name": "old2", "count": 1}'])
res = door("op_apply", ["write-hello", '{"count": 1, "name": "old2"}'])
t_v2 = pool.tickets[json.loads(res["stdout"])["ticket_id"]]
check("a newer version of the op replaces the open card: the old one is closed as replaced, the new one stands",
      t_fresh["status"] == "expired" and t_fresh["decision"].get("note") == nac_ops.STALE_REPLACED
      and t_v2["id"] != t_fresh["id"] and t_v2["status"] == "open" and t_v2["metadata"]["op_ask"]["sha256"] != t_fresh["metadata"]["op_ask"]["sha256"])
check("the card carries the run's own expiry", t_v2["expires_at"] is not None)
decide(t_v2["id"], "no")
door("op_apply", ["write-hello", '{"count": 1, "name": "old2"}'])

print("the grant row survives a click that could not file")
grant_row_before = len(pool.runs)
door("op_dry_run", ["write-user", '{"name": "gx", "count": 2}'])
res = door("op_apply", ["write-user", '{"count": 2, "name": "gx"}'])
t_gx = pool.tickets[json.loads(res["stdout"])["ticket_id"]]
_obtain = nac_approval.obtain


async def _boom(*a, **kw):
    raise RuntimeError("relay exploded")

nac_approval.obtain = _boom
d = decide(t_gx["id"], "grant")
nac_approval.obtain = _obtain
g_gx = pool.runs[-1]
check("obtain raising at the click: the grant row is kept AND named on the ticket, the reason recorded",
      d["op_ask"]["filed"] is False and "relay exploded" in d["op_ask"]["why"] and d["op_ask"].get("grant_run_id") == g_gx["id"]
      and g_gx["kind"] == "op_grant" and pool.tickets[t_gx["id"]]["metadata"]["op_ask_result"]["grant_run_id"] == g_gx["id"])
n_runs = len(pool.runs)
res = door("op_apply", ["write-user", '{"count": 2, "name": "gx"}'])
check("the Host's next call reuses that row (pending on the device), never a second grant row",
      res.get("state") == "pending_approval" and len(pool.runs) == n_runs)
res = door("op_apply", ["write-user", '{"count": 2, "name": "gx"}'])
check("…and again", len(pool.runs) == n_runs)
# the same reuse when the click itself failed before naming the row (an older ticket): named on the Host's call
t_gx["metadata"]["op_ask_result"] = {"choice": "grant", "filed": False, "why": "x"}
g_gx["state"] = "dropped"
n_runs = len(pool.runs)
res = door("op_apply", ["write-user", '{"count": 2, "name": "gx"}'])
g_new = pool.runs[-1]
check("a card whose click never named a row: the Host's call makes ONE and names it on the ticket",
      len(pool.runs) == n_runs + 1 and pool.tickets[t_gx["id"]]["metadata"]["op_ask_result"]["grant_run_id"] == g_new["id"])
res = door("op_apply", ["write-user", '{"count": 2, "name": "gx"}'])
check("…reused on the next call", len(pool.runs) == n_runs + 1)
relay.device_state = "approved"
res = door("op_apply", ["write-user", '{"count": 2, "name": "gx"}'], wait_s=1)
check("approved: installed and run", json.loads(res["stdout"])["state"] == "applied" and g_new["state"] == "installed")
relay.device_state = "pending"

print("a grant run whose answer was lost")
_orig_dd = mrelay.dispatch_detailed


async def _lost_grant(token_id, op, args, timeout, approval=""):
    if op == "op_grant_run":
        mrelay.calls.append((token_id, op, list(args), timeout))
        return None, nac_mgmt.DISPATCH_NO_REPLY
    return await _orig_dd(token_id, op, args, timeout, approval)

mrelay.dispatch_detailed = _lost_grant
runs_before = list(pool.tickets[t_gx["id"]]["metadata"]["op_ask"].get("runs") or [])
try:
    door("op_apply", ["write-user", '{"count": 2, "name": "gx"}'])
    check("a lost grant-run answer is a 504, the row 'sent'", False)
except nac_ops.OpError as e:
    check("a lost grant-run answer is a 504, the row 'sent'", e.status == 504 and pool.runs[-1]["state"] == "sent")
mrelay.dispatch_detailed = _orig_dd
sent_row = pool.runs[-1]
runs_on_card = pool.tickets[t_gx["id"]]["metadata"]["op_ask"]["runs"]
check("the sent run is on the card as it is: sent, answer lost", len(runs_on_card) == len(runs_before) + 1
      and runs_on_card[-1]["state"] == "sent" and "lost" in runs_on_card[-1]["error"])
mrelay.calls.clear()
try:
    door("op_apply", ["write-user", '{"count": 2, "name": "gx"}'])
    check("the same params again inside the run's budget are NOT sent again: the words say what happened", False)
except nac_ops.OpError as e:
    check("the same params again inside the run's budget are NOT sent again: the words say what happened",
          e.status == 409 and "answer was lost" in e.detail and "may still be running" in e.detail and not mrelay.calls)
door("op_dry_run", ["write-user", '{"name": "gx", "count": 3}'])
res = door("op_apply", ["write-user", '{"count": 3, "name": "gx"}'])
check("other params under the grant are not held back (outside the bounds here: the once path)", json.loads(res["stdout"])["state"] == "asked")
decide(json.loads(res["stdout"])["ticket_id"], "no")
door("op_apply", ["write-user", '{"count": 3, "name": "gx"}'])
sent_row["created_at"] = dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=nac_mgmt.mgmt_op_budget("op_grant_run") + 5)
# no worker for the run that would carry the note: nothing dispatched, the note is kept for the one that goes
_wc2 = mrelay.worker_count
mrelay.worker_count = lambda token_id: 0
try:
    door("op_apply", ["write-user", '{"count": 2, "name": "gx"}'])
    check("a 502 after a lost run: nothing dispatched, the lost row NOT marked superseded", False)
except nac_ops.OpError as e:
    check("a 502 after a lost run: nothing dispatched, the lost row NOT marked superseded",
          e.status == 502 and not (sent_row.get("result") or {}).get("superseded_by"))
mrelay.worker_count = _wc2
mrelay.calls.clear()
res = door("op_apply", ["write-user", '{"count": 2, "name": "gx"}'])
out = json.loads(res["stdout"])
check("past the budget the run goes, spends another use, and the result says an earlier one never answered",
      res["exit"] == 0 and out["state"] == "applied" and "never answered" in out.get("note", "") and mrelay.ops() == ["op_grant_run"])
res = door("op_apply", ["write-user", '{"count": 2, "name": "gx"}'])
check("said once: the next run of the same params carries no such note, and the lost row still counts as a spent use",
      json.loads(res["stdout"])["state"] == "applied" and "note" not in json.loads(res["stdout"]) and sent_row["state"] == "sent"
      and sent_row["result"]["superseded_by"])

print("a 'sent' apply inside the margin is still read for its outcome")
door("op_dry_run", ["write-hello", '{"name": "lost", "count": 1}'])
res = door("op_apply", ["write-hello", '{"count": 1, "name": "lost"}'])
t_lost = pool.tickets[json.loads(res["stdout"])["ticket_id"]]
check("the card's own expiry is the run's expiry less the apply's budget",
      t_lost["expires_at"] == pool.runs[-1]["expires_dt"] - dt.timedelta(seconds=nac_ops.lapse_margin_s()))
decide(t_lost["id"], "once")
relay.device_state = "approved"
_orig_dd2 = mrelay.dispatch_detailed


async def _lost_apply(token_id, op, args, timeout, approval=""):
    if op == "op_apply":
        mrelay.calls.append((token_id, op, list(args), timeout))
        return None, nac_mgmt.DISPATCH_NO_REPLY
    return await _orig_dd2(token_id, op, args, timeout, approval)

mrelay.dispatch_detailed = _lost_apply
try:
    door("op_apply", ["write-hello", '{"count": 1, "name": "lost"}'], wait_s=1)
except nac_ops.OpError:
    pass
mrelay.dispatch_detailed = _orig_dd2
sent_apply = pool.runs[-1]
check("the apply left and its answer was lost: the row is 'sent'", sent_apply["state"] == "sent")
soon = dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=nac_ops.lapse_margin_s() - 60)
sent_apply["expires"] = sent_apply["expires_dt"] = soon
mrelay.status = {"found": True, "nonce_burned": True, "applied": True, "committed": ["created /srv/hostop/hello.txt"], "changes_sha256": "c" * 64}
mrelay.calls.clear()
res = door("op_apply", ["write-hello", '{"count": 1, "name": "lost"}'], wait_s=1)
check("inside the margin a SENT row is still found and resolved from the box's record (op_run_status), never 'dry-run again'",
      mrelay.ops() == ["op_run_status"] and res["exit"] == 0 and json.loads(res["stdout"])["state"] == "applied"
      and sent_apply["state"] == "applied")
mrelay.status = {"found": False, "nonce_burned": False}
relay.device_state = "pending"

print("a click in the last minutes before the run lapses")
door("op_dry_run", ["write-hello", '{"name": "late", "count": 1}'])
res = door("op_apply", ["write-hello", '{"count": 1, "name": "late"}'])
t_late = pool.tickets[json.loads(res["stdout"])["ticket_id"]]
run_late = pool.runs[-1]
soon = dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=nac_ops.lapse_margin_s() - 60)
run_late["expires"] = run_late["expires_dt"] = soon
t_late["metadata"]["op_ask"]["expires"] = soon.strftime("%Y-%m-%dT%H:%M:%SZ")
relay.calls.clear()
d = decide(t_late["id"], "once")
check("with less than the apply's budget left, the click files nothing and says the terms are about to lapse",
      d["op_ask"]["filed"] is False and "minutes of its terms left" in d["op_ask"]["why"] and not relay.calls)
try:
    door("op_apply", ["write-hello", '{"count": 1, "name": "late"}'])
    check("the Host's next call sees no live dry run either (the same margin), never a device ask for it", False)
except nac_ops.OpError as e:
    check("the Host's next call sees no live dry run either (the same margin), never a device ask for it",
          e.status == 409 and "op_dry_run" in e.detail and not relay.calls)
door("op_dry_run", ["write-hello", '{"name": "late", "count": 1}'])
res = door("op_apply", ["write-hello", '{"count": 1, "name": "late"}'])
check("dry-run again: the answered card stays decided with its reason, and a fresh card is filed for the new run",
      t_late["status"] == "decided" and "minutes of its terms left" in t_late["metadata"]["op_ask_result"]["why"]
      and json.loads(res["stdout"])["ticket_id"] != t_late["id"] and pool.tickets[json.loads(res["stdout"])["ticket_id"]]["status"] == "open")
decide(json.loads(res["stdout"])["ticket_id"], "no")
door("op_apply", ["write-hello", '{"count": 1, "name": "late"}'])

print("wiring")
kit = src("app-web-kit/ask.ts")
check("the kit raises the card from mgmt_run's result and carries the attachment from a platform ask only",
      'const RESULT_ASK_TOOLS = new Set(["pipeline", "mgmt_run", "context_ingest", "turn_type"]);' in kit and "attachment?: AskAttachment;" in kit
      and "const att = asRecord(src?.attachment);" in kit
      and kit.index("const att = asRecord(src?.attachment);") > kit.index("RESULT_ASK_TOOLS.has(opName(t))"))
ov = src("app-web-kit/ui/ask-overlay.tsx")
check("the overlay folds the attachment under the words", "ask.attachment &&" in ov and 'data-ask-attachment=""' in ov and "<details" in ov)
# The Tickets pane's decision in full lives on the cards' Decisions sheet now
# (circles-web/src/cards/tickets.tsx, moved whole).
pane = src("circles-web/src/cards/tickets.tsx")
check("the decision in full (Circles' Decisions sheet) shows the diff, the outcome and the runs under the approval",
      'md.kind !== "op_ask"' in pane and "What it changes on {op.server}" in pane and "Runs under this approval" in pane
      and "opAskOutcome" in pane)
check("the Workbench knows the op target", '| "op";' in src("workbench-web/src/lib/types.ts")
      and 'op: "op — a program for one of your machines"' in src("workbench-web/src/components/workbench/ops-pane.tsx"))
tsrc = src("server/routes/tickets.py")
check("the decide hands an op card to nac_ops after the flip, records the outcome, and strips the kind at the POST route",
      'OP_ASK_KIND = "op_ask"' in tsrc and "nac_ops.card_decided(" in tsrc and "'op_ask_result'" in tsrc
      and tsrc.index("nac_ops.card_decided(") > tsrc.index("SET status = 'decided'")
      and "OP_ASK_KIND, PIPELINE_PROPOSAL_KIND," in tsrc)
skill = src("default-skills/network.md")
check("the skill says the flow: the card, end the turn, never decide it, apply again after the tap",
      "puts THE CARD in" in skill and "END YOUR TURN" in skill and "Never decide the ticket yourself" in skill
      and "Don't run it" in skill and "a card never appears without a dry run" in skill)
check("the registry names the asked state", "puts their card up ('asked': end the turn)" in src("brain/mastercode/public_tool_registry.py"))
node = shutil.which("node")
if node:
    r = subprocess.run([node, "--test", "test/ask.test.ts"], cwd=ROOT / "circles-web", capture_output=True, text=True, timeout=120)
    check("circles-web/test/ask.test.ts passes (the card from mgmt_run, the attachment, nothing from a plain result)",
          r.returncode == 0 and "# fail 0" in r.stdout, (r.stdout + r.stderr)[-600:])
else:
    check("node present to run the kit test (SKIPPED — no node on PATH)", True)
for app in ("circles", "workbench"):
    # Any chunk: Circles loads the cards, the ask card with them, from a chunk of their own.
    assets = list((ROOT / "system-apps" / app / "assets").glob("*.js"))
    check(f"{app}: the shipped bundle carries the fold", any('data-ask-attachment' in a.read_text(errors="replace") for a in assets),
          [a.name for a in assets])

shutil.rmtree(projects, ignore_errors=True)
print()
if failures:
    print(f"{len(failures)} FAILED:")
    for f in failures:
        print("  - " + f)
    sys.exit(1)
print("OK: all checks passed")
