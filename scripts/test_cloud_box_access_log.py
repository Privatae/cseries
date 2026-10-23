#!/usr/bin/env python3
"""The cloud machine's copy of its access log (vm/cloud/overlay/usr/local/lib/
cseries-cloud/cseries_accesslog.py), against the shared contract
(the platform's machine_access_log rows and the heartbeat's answer).

  1. The row hash is the contract's: both rows of the shared test vector
     hash to the contract's values, and the second chains onto the first.
  2. A row is taken only when it is the next one: seq count+1, prev_hash the
     head this machine holds, a hash that is right for its fields, exactly
     the contract's fields, no newline anywhere, a known actor.
  3. An answer is applied row by row: good rows are appended, the first bad
     one marks the copy mismatched and nothing after it is taken; a mismatch
     stays, however good a later answer is; match false is a mismatch; an
     answer without an access log (a platform that sends none) changes
     nothing.
  4. The copy is appended in place (the same inode, which the Host's
     read-only view of it needs), and a copy edited on disk no longer
     verifies.
  5. The vault's mirror: a prefix of the copy is brought up to date; one that
     differs in a row both hold marks the log mismatched.

Container-free: python3 scripts/test_cloud_box_access_log.py
"""
import copy
import json
import os
import sys
import tempfile

ROOT = os.environ.get("APP_ROOT") or os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(ROOT, "vm/cloud/overlay/usr/local/lib/cseries-cloud"))
import cseries_accesslog as al  # noqa: E402

failed = 0


def check(cond, what, detail=""):
    global failed
    if cond:
        print(f"OK    {what}")
    else:
        failed = 1
        print(f"FAIL  {what} {detail}")


MID = "7c9e6679-7425-40de-944b-e07fc1f90ae7"
# The test vector the platform's machine_access_log trigger is held to as well,
# so the box and the platform hash a row the same way. All values invented.
V1 = {"seq": 1, "at": "2026-10-23T10:12:01.123456Z", "actor": "provisioner", "action": "stop",
      "summary": "Privatae stopped this machine at the end of its subscription.",
      "detail": "{\"reason\":\"cancelled\"}", "provider_task_id": None,
      "prev_hash": "0" * 64,
      "hash": "731cfffe05cec22e40c85b82e4e14f684bb7ea89e0f50885a98b36926971ca2a"}
V2 = {"seq": 2, "at": "2026-10-23T10:12:02.000001Z", "actor": "ovh-task", "action": "provider-task",
      "summary": "Our provider opened the console of this machine; Privatae did not start it.",
      "detail": "{\"note\":\"invented — café\",\"state\":\"done\",\"type\":\"openConsoleAccess\"}",
      "provider_task_id": "1000001",
      "prev_hash": "731cfffe05cec22e40c85b82e4e14f684bb7ea89e0f50885a98b36926971ca2a",
      "hash": "8f615f6a109b39c847c4a0778fac47db4d991ede52859b9d966ebb9a8b687205"}


def hashed(row):
    r = dict(row)
    r["hash"] = al.row_hash(r["prev_hash"], MID, r["seq"], r["at"], r["actor"], r["action"],
                           r["summary"], r["detail"], r["provider_task_id"])
    return r


# 1. the vector
for v in (V1, V2):
    got = al.row_hash(v["prev_hash"], MID, v["seq"], v["at"], v["actor"], v["action"], v["summary"],
                      v["detail"], v["provider_task_id"])
    check(got == v["hash"], f"row {v['seq']} of the contract's vector hashes to the contract's value", got)
check(al.row_problem(V1, MID, 0, al.ZERO) is None and al.row_problem(V2, MID, 1, V1["hash"]) is None,
      "both vector rows are taken in order")

# 2. one row at a time
bad = {
    "the wrong seq": dict(V2, seq=3),
    "a prev_hash that is not the head": hashed(dict(V2, prev_hash="1" * 64)),
    "a summary changed after hashing": dict(V2, summary=V2["summary"] + " "),
    "an extra field": dict(V2, extra=1),
    "a missing field": {k: v for k, v in V2.items() if k != "detail"},
    "a newline in the summary": hashed(dict(V2, summary="two\nlines")),
    "an unknown actor": hashed(dict(V2, actor="someone")),
    "detail that is not JSON": hashed(dict(V2, detail="{not json")),
    "a time without microseconds": hashed(dict(V2, at="2026-10-23T10:12:02Z")),
    "a task id with a space": hashed(dict(V2, provider_task_id="10 01")),
    "seq given as text": dict(V2, seq="2"),
    "seq given as true": dict(V1, seq=True),
    "a carriage return in the detail": hashed(dict(V2, detail="{\"a\":\"b\"}\r")),
    "detail over 2048 bytes": hashed(dict(V2, detail=json.dumps({"n": "x" * 2050}))),
}
for what, row in bad.items():
    check(al.row_problem(row, MID, 1 if row.get("seq") != 1 and row.get("seq") is not True else 0,
                         V1["hash"] if row.get("seq") not in (1, True) else al.ZERO) is not None,
          f"refused: {what}")
check(al.row_problem(V2, "00000000-0000-0000-0000-000000000000", 1, V1["hash"]) is not None,
      "a row hashed for another machine is refused")

with tempfile.TemporaryDirectory() as tmp:
    path, state = os.path.join(tmp, "access-log.jsonl"), os.path.join(tmp, "access-log.state.json")
    log = al.AccessLog(path, state, MID)

    # 3. answers
    check(log.status() == {"count": 0, "head": al.ZERO, "mismatch": False}, "an empty copy: count 0")
    check(log.apply_answer({"ok": True}) is None and not os.path.exists(path),
          "an answer without an access log changes nothing")
    st = log.apply_answer({"ok": True, "access_log": {"match": True, "rows": [V1]}})
    check(st == {"count": 1, "head": V1["hash"], "mismatch": False}, "the first row is taken", st)
    ino = os.stat(path).st_ino
    st = log.apply_answer({"ok": True, "access_log": {"match": True, "rows": [V2]}})
    check(st == {"count": 2, "head": V2["hash"], "mismatch": False}, "the second row is taken", st)
    check(os.stat(path).st_ino == ino, "the copy is appended in place: the same inode")
    check(log.apply_answer({"ok": True, "access_log": {"match": True, "rows": []}})["count"] == 2,
          "an answer with no new rows changes nothing")

    third = hashed({"seq": 3, "at": "2026-10-23T11:00:00.000000Z", "actor": "breakglass",
                    "action": "vps-console", "summary": "Privatae opened the provider console: invented.",
                    "detail": "{}", "provider_task_id": None, "prev_hash": V2["hash"], "hash": ""})
    fourth = hashed(dict(third, seq=4, prev_hash=third["hash"], action="done", summary="Done."))
    tampered = dict(fourth, summary="Nothing happened.")
    st = log.apply_answer({"ok": True, "access_log": {"match": True, "rows": [third, tampered]}})
    check(st["mismatch"] is True and st["count"] == 3,
          "a bad row after a good one: the good one is kept, the copy marked mismatched", st)
    st = log.apply_answer({"ok": True, "access_log": {"match": True, "rows": [fourth]}})
    check(st["mismatch"] is True and log.status()["count"] == 3,
          "a mismatch stays: a later good answer adds nothing", st)

with tempfile.TemporaryDirectory() as tmp:
    path, state = os.path.join(tmp, "access-log.jsonl"), os.path.join(tmp, "access-log.state.json")
    log = al.AccessLog(path, state, MID)
    st = log.apply_answer({"ok": True, "access_log": {"match": False, "rows": []}})
    check(st["mismatch"] is True, "match false is a mismatch", st)

with tempfile.TemporaryDirectory() as tmp:
    path, state = os.path.join(tmp, "access-log.jsonl"), os.path.join(tmp, "access-log.state.json")
    log = al.AccessLog(path, state, MID)
    st = log.apply_answer({"ok": True, "access_log": {"match": True, "rows": [V1] * 101}})
    check(st["mismatch"] is True and log.status()["count"] == 0,
          "more rows than an answer carries is refused whole", st)

# Every character but \n and \r: a summary or detail holding any of the
# others that str.splitlines() breaks a line at (U+2028, U+2029, U+0085, \v,
# \f, \x1c-\x1e) stays one row, here and in the copy the Host reads: both
# are read on "\n" alone.
SPLITTERS = "\u2028\u2029\u0085\x0b\x0c\x1c\x1d\x1e"
with tempfile.TemporaryDirectory() as tmp:
    path, state = os.path.join(tmp, "access-log.jsonl"), os.path.join(tmp, "access-log.state.json")
    log = al.AccessLog(path, state, MID)
    odd = hashed(dict(V1, summary="Invented: one" + SPLITTERS + "\t\x00 row."))
    odd2 = hashed(dict(V2, prev_hash=odd["hash"],
                       detail=json.dumps({"note": "invented" + SPLITTERS}, sort_keys=True,
                                         separators=(",", ":"), ensure_ascii=False)))
    st = log.apply_answer({"ok": True, "access_log": {"match": True, "rows": [odd, odd2]}})
    check(st == {"count": 2, "head": odd2["hash"], "mismatch": False} and al.read_rows(path) == [odd, odd2],
          "a summary or detail holding U+2028, U+2029, U+0085, \\v, \\f or \\x1c-\\x1e stays one row", st)
    check(log.status() == st, "and the copy still verifies when read back", log.status())
    mirror = os.path.join(tmp, "vault", "access-log.jsonl")
    check(log.sync_mirror(mirror)["mismatch"] is False and al.read_rows(mirror) == [odd, odd2],
          "and so does the vault's mirror of it")
    sys.path.insert(0, ROOT)
    from server import cloud_machine  # noqa: E402
    check(cloud_machine._read_lines(path) == [odd, odd2], "the Host reads the same two rows")
    for ch in ("\n", "\r"):
        check(al.row_problem(hashed(dict(V1, summary="a" + ch + "b")), MID, 0, al.ZERO) is not None,
              f"refused: {ch!r} in a summary")
    surrogate = dict(V1, summary="invented \ud800")
    try:
        why = al.row_problem(surrogate, MID, 0, al.ZERO)
    except Exception as e:     # noqa: BLE001 - the point is that nothing raises
        why = None
        check(False, "a lone surrogate is refused, not raised", repr(e))
    else:
        check(why is not None, "a lone surrogate (no UTF-8 text) is refused, not raised", why)

# 4. an edited copy
with tempfile.TemporaryDirectory() as tmp:
    path, state = os.path.join(tmp, "access-log.jsonl"), os.path.join(tmp, "access-log.state.json")
    log = al.AccessLog(path, state, MID)
    log.apply_answer({"ok": True, "access_log": {"match": True, "rows": [V1, V2]}})
    with open(path, encoding="utf-8") as f:
        lines = f.read().splitlines()
    edited = json.loads(lines[0])
    edited["summary"] = "Nothing to see."
    with open(path, "w", encoding="utf-8") as f:
        f.write(al.line_of(edited) + lines[1] + "\n")
    st = log.status()
    check(st["mismatch"] is True and st["count"] == 0, "a copy edited on disk no longer verifies", st)

# 5. the vault's mirror
with tempfile.TemporaryDirectory() as tmp:
    path, state = os.path.join(tmp, "access-log.jsonl"), os.path.join(tmp, "access-log.state.json")
    mirror = os.path.join(tmp, "vault", "access-log.jsonl")
    log = al.AccessLog(path, state, MID)
    log.apply_answer({"ok": True, "access_log": {"match": True, "rows": [V1]}})
    st = log.sync_mirror(mirror)
    check(st["mismatch"] is False and al.read_rows(mirror) == [V1], "an empty mirror is brought up to date")
    log.apply_answer({"ok": True, "access_log": {"match": True, "rows": [V2]}})
    st = log.sync_mirror(mirror)
    check(st["mismatch"] is False and al.read_rows(mirror) == [V1, V2],
          "rows received while locked are added to the mirror at the next unlock")
    other = copy.deepcopy(V1)
    other["summary"] = "Rewritten."
    with open(mirror, "w", encoding="utf-8") as f:
        f.write(al.line_of(other) + al.line_of(V2))
    st = log.sync_mirror(mirror)
    check(st["mismatch"] is True, "a mirror that differs in a row both hold is a mismatch", st)

sys.exit(failed)
