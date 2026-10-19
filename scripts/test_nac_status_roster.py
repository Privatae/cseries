#!/usr/bin/env python3
"""nac_status reads the bastions as a roster, and one machine's record on request.

Measured 2026-10-06 on one Host: nac_status returned ~144,000 characters, twice
a turn in the Telegram rework, and a tool result stays in every later call of
its turn. The NAC rows were ~2,800 of it; the rest was each of 25 bastions'
whole check-in record (~4,000 characters a machine). The roster keeps the ids
the containment tools take and the states worth acting on; server= (a hostname,
token name or token id) returns one machine's full record, so nothing became
unreachable.

Pinned here, through the real _exec_nac_status with status_snapshot stubbed:
  - the default result is a roster: every machine, its token_id, online,
    version, detect/alerts state; no heavy check-in fields;
  - a 25-machine fleet's result shrinks by most of its size;
  - the NAC rows (with their ids) pass through unchanged;
  - server= (hostname, name or token id, any case) returns that machine's full
    record, a hostname before another machine's same-named token; token_id= is
    not a second way in; an unknown one is refused with the hostnames to pick
    from;
  - in a typed turn the wall (resolve_targets, then check_call) tests the
    hostname of the machine the door would return, so a listed string that is
    another machine's token name or id is refused, and a machine with no
    hostname needs '*';
  - a failed bastion source passes through as it came.

Run: APP_ROOT=<repo> python3 scripts/test_nac_status_roster.py
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from pathlib import Path

_ROOT = Path(os.environ.get("APP_ROOT") or Path(__file__).resolve().parents[1])
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "brain"))
logging.disable(logging.CRITICAL)

failures: list = []


def check(label, ok, detail=""):
    print(f"[nac-roster] {'ok  ' if ok else 'FAIL'} {label}"
          + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        failures.append(label)


def machine(i: int) -> dict:
    """A bastion row in topology._bastion_row's shape, with check-in fields
    sized like one Host's (enforcing ~2.1k, detect ~600, anchor ~340, ...)."""
    return {
        "id": 100 + i, "name": f"box-{i}", "handle": f"h{i}", "scopes": ["mgmt", "mcp"],
        "hardware_fingerprint": "f" * 64, "machine_anchor_id": i,
        "created_at": "2026-09-01T00:00:00+00:00", "last_used_at": "2026-10-06T00:00:00+00:00",
        "hostname": f"fleet-box-{i}", "bastion_version": "0.44.2", "platform": "linux",
        "tunnel_status": "up", "reported_ip": f"10.0.0.{i}", "wg_pubkey": "k" * 44,
        "capabilities": ["embed", "mgmt", "observe"], "models": [],
        "last_seen_at": "2026-10-06T01:00:00+00:00", "online": i != 7,
        "freeagent_ok": True, "freeagent_stores": {},
        "enforcing": {"mgmt_ops": ["audit_fetch", "backup_fetch"] * 40,
                      "grants": [{"signature": "s" * 88, "store_id": 1}] * 6},
        "nac_posture": "allow", "nac_decision": "allow",
        "detect": {"state": "ok", "critical": 0, "findings": ["x" * 40] * 12},
        "alerts": None, "schedulers": [{"job": "y" * 60}] * 5, "scheduler_scan": {"paths": 12},
        "anchor": {"by": "z" * 300}, "owner_claim": {"c": "o" * 200},
        "posture": {"p": "q" * 140}, "prep_tools": ["t" * 30] * 20,
        "pairings": [{"p": "r" * 120}],
        "update_to_version": "0.45.0" if i == 3 else None, "update_approved": i == 3,
        "contain_pending": [{"pid": 1}] if i == 5 else [], "contain_findings_pending": [],
    }


FLEET = [machine(i) for i in range(25)]
NAC = {
    "policy": {"profiles": [], "policy": [], "stepup_enrolled": True, "engine": {"tamper": False}},
    "egress": {"rules": [], "default": "open"},
    "mgmt_grants": {"grants": [{"id": 9, "server": "fleet-box-1", "ops": ["audit_fetch"]}],
                    "default": "deny"},
    "dcn_stores": {"stores": []}, "suspended": {"suspended": []},
    "quarantined": {"quarantined": [{"server": "fleet-box-2", "token_id": 102}]},
    "connectors": {"connectors": []},
}


def main() -> int:
    from server import brain_service
    from server.network import nac_commit
    from mastercode.runtime import tools as T
    from mastercode.runtime.tool_call_ledger import classify, fault_class

    snap_holder: dict = {}

    async def _snap(pool, eid):
        return json.loads(json.dumps(snap_holder["snap"]))

    saved = (nac_commit.status_snapshot, brain_service.get_pool)
    nac_commit.status_snapshot = _snap
    brain_service.get_pool = lambda: object()
    try:
        def call(args=None, snap=None):
            snap_holder["snap"] = snap or {**NAC, "bastions": {"bastions": FLEET}}
            return asyncio.run(T._exec_nac_status(args or {}))

        full_chars = len(json.dumps({**NAC, "bastions": {"bastions": FLEET}}))
        r = call()
        res = r.get("result") or {}
        roster = (res.get("bastions") or {}).get("bastions") or []
        chars = len(json.dumps(res))
        check("default: a roster of every machine, each with the token_id the tools take",
              r.get("status") == "ok" and len(roster) == 25
              and [m.get("token_id") for m in roster] == [100 + i for i in range(25)]
              and all(m["hostname"] == f"fleet-box-{i}" for i, m in enumerate(roster)),
              repr(roster[:2]))
        check("default: online, version, detect and alert state per machine; the "
              "online count matches",
              all({"online", "bastion_version", "detect_state", "alerts_state", "last_seen_at"}
                  <= set(m) for m in roster)
              and res["bastions"].get("online") == 24 and roster[7]["online"] is False,
              repr(roster[7]))
        heavy = {"enforcing", "detect", "anchor", "owner_claim", "posture", "prep_tools",
                 "pairings", "schedulers", "wg_pubkey", "hardware_fingerprint", "models"}
        check("default: no machine carries its check-in record",
              not any(heavy & set(m) for m in roster),
              repr([sorted(heavy & set(m)) for m in roster][:2]))
        check("default: a pending update and a pending containment still show",
              roster[3].get("update_to_version") == "0.45.0" and roster[3].get("update_approved") is True
              and roster[5].get("contain_pending") == 1
              and "update_to_version" not in roster[0] and "contain_pending" not in roster[0],
              repr((roster[3], roster[5])))
        check(f"default: a 25-machine fleet shrinks by most of its size "
              f"({full_chars:,} -> {chars:,} chars)",
              chars * 4 < full_chars, f"{full_chars} -> {chars}")
        check("default: the NAC rows and their ids pass through unchanged",
              all(res.get(k) == v for k, v in NAC.items()), repr({k: res.get(k) for k in NAC}))
        check("default: the result says how to read one machine's whole record",
              "server=" in str(res["bastions"].get("full_record") or ""))

        r = call({"server": "FLEET-BOX-4"})
        check("server=<hostname>, any case: that machine's full record",
              r.get("status") == "ok" and r["result"].get("bastion") == FLEET[4], repr(r)[:300])
        r = call({"server": "box-6"})
        check("server=<name>: that machine's full record",
              (r.get("result") or {}).get("bastion", {}).get("id") == 106, repr(r)[:300])
        r = call({"server": "113"})
        check("server=<token id>: the one argument takes the id too",
              (r.get("result") or {}).get("bastion", {}).get("hostname") == "fleet-box-13",
              repr(r)[:300])
        r = call({"token_id": 111})
        check("token_id= is not a second way to name a machine (a typed turn checks "
              "only `server`): the roster comes back",
              "bastion" not in (r.get("result") or {})
              and len(((r.get("result") or {}).get("bastions") or {}).get("bastions") or []) == 25,
              repr(r)[:300])
        # A token name is free text chosen at enroll (manage_bastion `name`), so
        # it can be another machine's hostname. The machine with that HOSTNAME
        # is the one meant, whatever the list order (newest check-in first).
        namesake = {**machine(40), "id": 300, "name": "fleet-box-4", "hostname": "ws-7"}
        r = call({"server": "fleet-box-4"}, snap={**NAC, "bastions": {"bastions": [namesake] + FLEET}})
        check("server=<hostname>: that machine, not an earlier one whose token name is "
              "the same string",
              ((r.get("result") or {}).get("bastion") or {}).get("hostname") == "fleet-box-4",
              repr(((r.get("result") or {}).get("bastion") or {}).get("hostname")))

        # The typed-turn wall, the way _authorize_meta_tool runs it:
        # resolve_targets, then check_call. The type lists hostnames.
        from mastercode.runtime import turn_types as TT
        from server.network import topology
        typed = {"id": "00000000-0000-0000-0000-000000000001", "name": "Ops",
                 "status": "active", "keywords": [], "context": {}, "skill": None,
                 "tool_groups": ["network"], "grant_names": [], "keys": [],
                 "bastions": ["fleet-box-1", "laptop", "113", "spare"], "model": None,
                 "thread_id": None, "version": 1}
        # Token "laptop" is installed on ws-9; no machine reports hostname
        # "laptop", "113" or "spare". Token 113 is fleet-box-13; token "spare"
        # has never checked in.
        stray = {**machine(41), "id": 301, "name": "laptop", "hostname": "ws-9"}
        unreported = {**machine(42), "id": 302, "name": "spare", "hostname": None}
        fleet_rows = [stray, unreported] + FLEET
        saved_lb = topology.list_bastions

        async def _lb(pool, eid):
            return json.loads(json.dumps(fleet_rows))
        topology.list_bastions = _lb

        def gate(args):
            targets = asyncio.run(TT.resolve_targets(object(), 1, "nac_status", args))
            return TT.check_call(typed, "nac_status", args, targets=targets)
        try:
            outside = gate({"server": "fleet-box-2"})
            inside = gate({"server": "fleet-box-1"})
            roster_ok = gate({})
            by_name = gate({"server": "laptop"})
            by_id = gate({"server": "113"})
            no_host = gate({"server": "spare"})
            fleet_rows[:] = []
            unread = gate({"server": "fleet-box-1"})
        finally:
            topology.list_bastions = saved_lb
        check("a typed turn reads the full record only of a machine it lists; the "
              "roster still passes",
              isinstance(outside, dict) and inside is None and roster_ok is None,
              repr((outside, inside, roster_ok)))
        check("a typed turn: a listed string that is another machine's token NAME is "
              "refused, naming the machine it reaches",
              isinstance(by_name, dict) and by_name.get("item") == "ws-9", repr(by_name))
        check("a typed turn: a listed string that is a token ID is checked as the "
              "hostname behind it",
              isinstance(by_id, dict) and by_id.get("item") == "fleet-box-13", repr(by_id))
        check("a typed turn: a machine with no reported hostname, or one the read "
              "cannot find, needs '*'",
              isinstance(no_host, dict) and no_host.get("item") == "*"
              and isinstance(unread, dict) and unread.get("item") == "*",
              repr((no_host, unread)))
        r = call({"server": "nope"})
        check("server=<unknown>: refused, naming the hostnames to pick from",
              r.get("status") != "ok" and "fleet-box-0" in json.dumps(r) and "nope" in json.dumps(r),
              repr(r)[:300])
        check("server=<unknown>: the ledger files it as the model's error, not a platform fault",
              fault_class(classify(r), r) == "model", repr((classify(r), r)))

        bad = {**NAC, "bastions": {"error": "relation does not exist"}}
        r = call(snap=bad)
        check("a failed bastion source passes through as it came",
              r.get("status") == "ok"
              and r["result"].get("bastions") == {"error": "relation does not exist"},
              repr(r)[:300])
        r = call(snap={**NAC, "bastions": {"bastions": []}})
        check("no bastions: an empty roster, online 0",
              (r.get("result") or {}).get("bastions", {}).get("bastions") == [] and (r.get("result") or {}).get("bastions", {}).get("online") == 0,
              repr(r)[:300])
    finally:
        nac_commit.status_snapshot, brain_service.get_pool = saved

    print(f"[nac-roster] {'all passed' if not failures else f'{len(failures)} FAIL(s): {failures}'}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
