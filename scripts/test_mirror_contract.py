#!/usr/bin/env python3
"""Invariant: what the memory mirror TELLS the user is true.

server/network/mirror.py turns three catalog reads into one word the Backup tab
and the Host both repeat verbatim. If that word is wrong the user is told their
memory is safe when it is not, or told to act when nothing is wrong — and both
are worse than showing nothing. So the mapping is pinned here rather than left
to whoever edits the renderer next.

Pure logic only: no pool, no container. The live-database behaviour (streaming,
shedding, reconcile-on-revoke) is proven separately against a real primary.

Run: python3 scripts/test_mirror_contract.py
"""
import importlib.util
import os
import sys

ROOT = os.environ.get("APP_ROOT", os.path.join(os.path.dirname(__file__), ".."))
_spec = importlib.util.spec_from_file_location(
    "mirror", os.path.join(ROOT, "server", "network", "mirror.py"))
m = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(m)

FAILS = []


def check(cond, label):
    if not cond:
        FAILS.append(label)


RESERVED = {"present": True, "known": True, "wal_status": "reserved"}

# A catalog read we could not COMPLETE is unknown, never a diagnosis. Conflating
# "the query failed" with "the slot is gone" showed the user the single most
# alarming message this surface has — "your copy must start over" — on no
# evidence at all, every time one read hiccuped.
check(m._health({"slot": {"known": False}, "stream": {}}) == "unknown",
      "a FAILED slot read must read unknown, never needs_reseed")
check(m._health({"slot": {"known": False, "present": False},
                 "stream": {"state": "streaming", "lag_bytes": 0}}) == "unknown",
      "unknown outranks a stale stream row too — we do not know, so we say so")

# needs_reseed must WIN. It is the only state that does not recover on its own,
# so any ordering that lets a stale "streaming" mask it is a lie by omission.
check(m._health({"slot": {"present": True, "known": True, "wal_status": "lost"},
                 "stream": {"state": "streaming", "lag_bytes": 0}}) == "needs_reseed",
      "a LOST slot must read needs_reseed even while a stream is still attached")
check(m._health({"slot": {"present": False, "known": True}, "stream": {}}) == "needs_reseed",
      "a missing slot must read needs_reseed")

check(m._health({"slot": RESERVED, "stream": {"state": "streaming", "lag_bytes": 0}}) == "streaming",
      "an attached stream at zero lag is streaming")
check(m._health({"slot": RESERVED,
                 "stream": {"state": "streaming", "lag_bytes": 64 * 1024 * 1024}}) == "catching_up",
      "a stream more than one WAL segment behind is catching_up, not streaming")
check(m._health({"slot": RESERVED, "stream": {"state": "catchup", "lag_bytes": 1}}) == "catching_up",
      "any non-streaming attached state is catching_up")
check(m._health({"slot": RESERVED, "stream": {"state": None}}) == "disconnected",
      "no stream at all is disconnected")
# Unknown lag must not be reported as current. A null here means the catalog
# read failed, and "we could not tell" is closer to catching_up than to
# "up to date" — the direction that cannot mislead.
check(m._health({"slot": RESERVED,
                 "stream": {"state": "streaming", "lag_bytes": None}}) == "catching_up",
      "unknown lag must NOT read as streaming — never claim current on missing data")

# application_name is the anti-forgery property: the kernel sets it, so
# pg_stat_replication attributes a stream to a named bastion. It is rendered in
# a UI and written to logs, so it must be inert.
check(m.application_name("DESKTOP-X") == "mirror:DESKTOP-X", "normal name passes through")
for hostile in ("a b", "a\nb", "a'b", 'a"b', "a;b", "a\x00b"):
    got = m.application_name(hostile)
    check(got.startswith("mirror:"), f"application_name must keep its prefix for {hostile!r}")
    check(all(c.isalnum() or c in "._-:" for c in got),
          f"application_name must sanitise {hostile!r} (got {got!r})")
check(len(m.application_name("x" * 500)) <= 56, "application_name must be bounded")
check(m.application_name("") == "mirror:bastion", "an empty name still yields a usable label")

# Slot names go into SQL identifiers and into a LIKE scan that decides what the
# reconciler may DROP. Anything that widens that scan is a way to delete someone
# else's slot.
check(m.slot_name(7) == "mirror_b7", "slot name shape")
check(m._SLOT_RE.match("mirror_b7"), "the reap pattern must match its own slot names")
for foreign in ("mirror_b", "mirror_bx", "mirror_b7x", "cnpg_slot", "mirror_b7_old"):
    check(not m._SLOT_RE.match(foreign),
          f"the reap pattern must NOT match {foreign!r} — it decides what gets dropped")

# Absent build-info is a legitimate state: an older DB image publishes none, and
# the two images ship independently. It must degrade, never raise.
m._DB_BUILD_INFO = m.Path("/nonexistent/.db-build-info")
info = m.db_build_info()
check(info.get("known") is False and "reason" in info,
      "missing build-info must report known=False with a reason, not raise")

if FAILS:
    for f in FAILS:
        print(f"FAIL: {f}")
    print(f"MIRROR_CONTRACT_FAIL {len(FAILS)} check(s) failed")
    sys.exit(1)
print("MIRROR_CONTRACT_OK health mapping, name sanitisation, slot-reap scope "
      "and absent-build-info all hold")
