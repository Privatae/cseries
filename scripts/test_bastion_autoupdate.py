#!/usr/bin/env python3
"""Bastion auto-update behind the canary ring (server/network/bastion_autoupdate.py).

Container-free. judge_canary is pure; run_once is driven with a fake bridge,
a fake manifest and a recording pool, so what is pinned is the DECISION:
  - ring 1 moves only when the platform says general; never on canary/held;
  - ring 0 moves on canary, records which machines it told and whether each
    was beating, and sends a verdict only when the rule says so;
  - the fail rule catches the two update-caused silences (took it then went
    quiet; never came back from the swap) and ignores a box that never took
    it and went quiet later;
  - the pass rule needs the soak, a machine on the new version, and all of
    them beating;
  - off means nothing is directed and nothing is asked.
The SQL itself is exercised against a real Postgres by
test_bastion_autoupdate_sql.py.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import os
import sys

REPO = os.environ.get("APP_ROOT") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from server.network import bastion_autoupdate as bau  # noqa: E402

FAILED = 0


def check(label, cond, extra=""):
    global FAILED
    if cond:
        print(f"ok    {label}")
    else:
        FAILED += 1
        print(f"FAIL  {label} {extra}")


NOW = dt.datetime(2026, 9, 25, 12, 0, tzinfo=dt.timezone.utc)
V = "0.33.0"
OPENED = NOW - dt.timedelta(hours=30)


def h(n):
    return None if n is None else NOW - dt.timedelta(hours=n)


def row(tid, *, told_h=None, online=True, took_h=None, seen_min=1, version=None,
        pending=None, host=None):
    return {"token_id": tid, "hostname": host or f"box-{tid}",
            "told_at": h(told_h), "online_at_told": online if told_h is not None else None,
            "took_at": h(took_h), "gateway_version": version or (V if took_h is not None else "0.32.2"),
            "update_to_version": pending,
            "last_seen_at": None if seen_min is None else NOW - dt.timedelta(minutes=seen_min)}


# ── judge_canary ──
def j(rows, opened=OPENED):
    return bau.judge_canary(rows, V, NOW, opened)


def not_fail(v):
    return v is None or v.get("verdict") != "fail"


check("nothing watched, canary just opened -> still soaking", j([], NOW - dt.timedelta(hours=1)) is None)
check("took it 5h ago, beating -> still soaking", j([row(1, told_h=5, took_h=5)]) is None)
v = j([row(1, told_h=25, took_h=25), row(2, told_h=25, took_h=24.9)])
check("one machine ran it 25h, all beating -> pass", v and v.get("verdict") == "pass", str(v))
v = j([row(1, took_h=25)])
check("REACHED BY A MANUAL UPDATE (never told by the loop) still passes — the stall the review found",
      v and v.get("verdict") == "pass", str(v))
v = j([row(1, told_h=25, took_h=25), row(2, told_h=25, seen_min=1)])
check("a told machine still beating on its old version does not block the pass, and is counted",
      v and v.get("verdict") == "pass" and "1 told to take it have not" in v["reason"], str(v))
v = j([row(1, told_h=3, took_h=3, seen_min=20, host="node-14"), row(2, told_h=3, took_h=3)])
check("took it, then silent 20 min -> fail, named (the node-14 shape)",
      v and v.get("verdict") == "fail" and "node-14" in v["reason"], str(v))
check("took it, silent 10 min -> not yet a failure", j([row(1, told_h=3, took_h=3, seen_min=10)]) is None)
# died on the swap: last beat 2 min after being told, silent since
v = j([row(1, told_h=1, seen_min=58, pending=V)])
check("told while beating, last beat within 5 min of being told, never took it, silent -> fail "
      "(swapped and never came back)", v and v.get("verdict") == "fail", str(v))
check("told 6h ago, went quiet 5h after being told, never took it -> NOT a hold "
      "(the directive is always pending on an untaken box; round-2 review)",
      not_fail(j([row(1, told_h=6, seen_min=60, pending=V)])))
v = j([row(1, told_h=30, took_h=30), row(2, told_h=30, seen_min=60, pending=V, host="rebooting")])
check("after the soak, an untaken machine silent -> waiting (no verdict), named",
      v and v.get("waiting") and "rebooting" in v["waiting"] and "verdict" not in v, str(v))
v = j([row(1, told_h=30, took_h=30), row(2, told_h=30, online=False, seen_min=600)])
check("after the soak, a machine that was OFFLINE when told does not make it wait",
      v and v.get("verdict") == "pass", str(v))
check("the soak runs from took_at, not told_at: told 30h ago, took 5h ago -> still soaking",
      j([row(1, told_h=30, took_h=5)]) is None)
check("told but its last beat was BEFORE being told -> not the update's failure (review #3)",
      not_fail(j([row(1, told_h=1, seen_min=62, pending=V)])))
check("told while OFFLINE, silent since -> not the update's failure (the online_at_told rule)",
      not_fail(j([row(1, told_h=6, online=False, seen_min=60, pending=V)])))
v = j([row(1, told_h=30, took_h=30, seen_min=20), row(2, told_h=30, took_h=30)])
check("fail wins over a finished soak", v and v.get("verdict") == "fail", str(v))
v = j([row(1, told_h=29, seen_min=1)])
check("canary open 30h, nobody took it -> stalled, no verdict",
      v and v.get("stalled") and "none of your machines" in v["stalled"] and "verdict" not in v, str(v))
check("canary open 5h, nobody took it -> still waiting",
      j([row(1, told_h=4, seen_min=1)], NOW - dt.timedelta(hours=5)) is None)
check("no opened_at from the bridge -> never stalled on a guess", j([row(1, told_h=29)], None) is None)


# ── run_once, with fakes ──
class Bridge:
    _connected = True

    def __init__(self, ring, stage):
        self.ring, self.stage, self.calls = ring, stage, []

    async def request_full(self, op, params):
        self.calls.append((op, params))
        if op == "bastion.rollout":
            return {"ok": True, "data": {"version": params["version"], "ring": self.ring,
                                         "stage": self.stage, "first_seen_at": OPENED.isoformat()}}
        if op == "bastion.rollout_verdict":
            return {"ok": True, "data": {"stage": "general" if params["verdict"] == "pass" else "held"}}
        raise AssertionError(op)


class Pool:
    def __init__(self, enabled=None, watched=(), stalled_at=None):
        self.enabled, self.watched, self.sql = enabled, list(watched), []
        self.stalled_at = stalled_at
        self.waiting_at = None

    async def fetchval(self, sql, *a):
        if "stalled_alerted_at" in sql:
            return self.stalled_at
        if "waiting_alerted_at" in sql:
            return self.waiting_at
        return self.enabled

    async def execute(self, sql, *a):
        self.sql.append((sql.split()[0], sql, a))

    async def fetch(self, sql, *a):
        return self.watched


def run(pool, bridge, *, directed=({"token_id": 7}, {"token_id": 8}), online=None, latest=V):
    from server.network import nac_commit
    asked = []

    async def fake_latest():
        return {"version": latest}

    async def fake_update(pool_, eid, version, token_ids=None):
        asked.append(version)
        return {"directed": list(directed), "directed_count": len(directed)}

    async def fake_online(pool_, eid):
        return online or {7: True, 8: False}

    saved = (nac_commit.bastion_latest, nac_commit.update_bastions, bau._online_now)
    nac_commit.bastion_latest, nac_commit.update_bastions, bau._online_now = fake_latest, fake_update, fake_online
    try:
        out = asyncio.run(bau.run_once(pool, bridge, 3, now=NOW))
    finally:
        nac_commit.bastion_latest, nac_commit.update_bastions, bau._online_now = saved
    return out, asked


out, asked = run(Pool(), Bridge(1, "canary"))
check("ring 1 on canary: nothing directed", asked == [], str(asked))
out, asked = run(Pool(), Bridge(1, "held"))
check("ring 1 on held: nothing directed", asked == [])
out, asked = run(Pool(), Bridge(1, "awaiting_canary"))
check("ring 1 before the canary opens: nothing directed", asked == [])
out, asked = run(Pool(), Bridge(1, "general"))
check("ring 1 on general: moves to the published version", asked == [V])
b = Bridge(1, "general")
run(Pool(), b)
check("ring 1 never sends a verdict", all(op != "bastion.rollout_verdict" for op, _ in b.calls))

p = Pool()
out, asked = run(p, Bridge(0, "canary"))
check("ring 0 on canary: moves now", asked == [V])
ins = [a for (verb, sql, a) in p.sql if "bastion_autoupdate_watch" in sql and "told_at" in sql
       and "SELECT" not in sql]
check("ring 0 records each machine it told, and whether it was beating",
      sorted(ins) == [(V, 7, True), (V, 8, False)], str(ins))
check("ring 0 records, from the heartbeat, every machine already on the version",
      any("bastion_autoupdate_watch" in sql and "gateway_version = $1" in sql for (_, sql, _) in p.sql))
out, asked = run(Pool(), Bridge(0, "held"))
check("ring 0 on held: nothing directed", asked == [])

b = Bridge(0, "canary")
p = Pool(watched=[row(7, told_h=25, took_h=25)])
out, _ = run(p, b)
sent = [x for x in b.calls if x[0] == "bastion.rollout_verdict"]
check("ring 0 sends pass after the soak", len(sent) == 1 and sent[0][1]["verdict"] == "pass", str(sent))
check("the verdict is recorded on the state row",
      any("verdict = $2" in sql for (_, sql, _) in p.sql))

b = Bridge(0, "canary")
run(Pool(watched=[row(7, told_h=5, took_h=5)]), b)
check("ring 0 sends nothing while soaking", all(op != "bastion.rollout_verdict" for op, _ in b.calls))

b = Bridge(0, "general")
run(Pool(watched=[row(7, told_h=25, took_h=25)]), b)
check("ring 0 does not re-judge a decided version", all(op != "bastion.rollout_verdict" for op, _ in b.calls))

alerts = []
saved_alert = bau._alert_held


async def fake_alert(version, reason):
    alerts.append((version, reason))
bau._alert_held = fake_alert
b = Bridge(0, "canary")
run(Pool(watched=[row(7, told_h=2, took_h=2, seen_min=30, host="wedged")]), b)
bau._alert_held = saved_alert
sent = [x for x in b.calls if x[0] == "bastion.rollout_verdict"]
check("ring 0 sends fail when a machine wedged", sent and sent[0][1]["verdict"] == "fail", str(sent))
check("a fail raises the held alert naming the machine", alerts and "wedged" in alerts[0][1], str(alerts))

b = Bridge(1, "canary")
run(Pool(watched=[row(7, told_h=25, took_h=25)]), b)
check("ring 1 never judges a canary, even with rows it could judge",
      all(op != "bastion.rollout_verdict" for op, _ in b.calls))

stalls = []
saved_inbox = None
from server.network import pairing_grants  # noqa: E402


async def fake_inbox(subject, body, payload):
    stalls.append(subject)
    return True
saved_inbox, pairing_grants._inbox = pairing_grants._inbox, fake_inbox
b = Bridge(0, "canary")
p = Pool(watched=[row(7, told_h=29, seen_min=1)])
out, _ = run(p, b)
check("a stalled canary sends no verdict", all(op != "bastion.rollout_verdict" for op, _ in b.calls))
check("a stalled canary tells the user, and marks it", stalls and out.get("stalled")
      and any("stalled_alerted_at = now()" in sql for (_, sql, _) in p.sql), str(stalls))
stalls.clear()
run(Pool(watched=[row(7, told_h=29, seen_min=1)], stalled_at=NOW), Bridge(0, "canary"))
check("a stall already told is not told again", stalls == [], str(stalls))
stalls.clear()
p = Pool(watched=[row(7, told_h=30, took_h=30), row(8, told_h=30, seen_min=60, host="gone")],
         stalled_at=NOW)
out, _ = run(p, Bridge(0, "canary"))
check("a waiting canary is told even after a stall was told (separate facts)",
      out.get("waiting") and stalls and any("waiting_alerted_at = now()" in sql for (_, sql, _) in p.sql),
      str((out, stalls)))
pairing_grants._inbox = saved_inbox

b = Bridge(0, "canary")
out, asked = run(Pool(enabled=False), b)
check("off: nothing directed, nothing asked", out == {"status": "off"} and asked == [] and b.calls == [])
out, asked = run(Pool(), Bridge(1, "general"), latest=None)
check("no manifest: nothing directed", out["status"] == "no_manifest" and asked == [])
out, asked = run(Pool(), Bridge(1, "general"), latest="v0.33.0")
check("a v-prefixed manifest version is normalised", asked == ["0.33.0"], str(asked))

# The Network app's sentence for each ring and stage (plain JS, its own harness).
import shutil  # noqa: E402
import subprocess  # noqa: E402

node = shutil.which("node")
check("node is available for the Network half", node is not None)
if node:
    proc = subprocess.run([node, os.path.join(REPO, "scripts", "test_network_autoupdate_line.js")],
                          capture_output=True, text=True, timeout=60)
    sys.stdout.write(proc.stdout)
    check("the Network app words every ring and stage", proc.returncode == 0, proc.stderr[-400:])

print("PASS" if not FAILED else f"{FAILED} FAILED")
sys.exit(1 if FAILED else 0)
