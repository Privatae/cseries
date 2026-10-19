#!/usr/bin/env python3
"""The replay rig (scripts/rig_tool_surface.py) reports what the tool-search
choice is decided on, so each of its inputs has to be the turn's own:

  1. A turn is replayed on the message that started it. A turn the platform
     started (origin 'continuation': a wake-up brief, which writes no user row,
     session.py send_message `system_turn`) has no message in the export and
     is replayed on none, never on the person's previous message.
  2. `--staples K` adds K tools to the core: a build door is in every core
     already and a tool the surface does not hold cannot be added, so neither
     takes one of the K places.
  3. The held-out cut is a time, compared as a time: "22:47Z" is after
     "22:46:59+00:00" and before "22:47:01+00:00", whatever the offset says.
  4. The price model charges what a schema costs in search
     mode: one found by a search is 2.25F in a 3-call turn; a history note is
     2.25F on the turn after the call plus one rewrite (1.25F) later, and is
     read from the cache after that. F is the tool as find_tools renders it.
  5. What the rig searches is what the Host searches: its searchable set is
     tool_doors.DoorCatalog's, built from the session's own schema build, and
     every tool purpose ranks the same top 5 as the session's index
     (measured 111/123 on Circles, 103/123 on the Workbench with
     the rig's own catalog).
  6. Every price is reported under every pricing: Anthropic (cache write
     1.25, read 0.1) and a subscription that counts a cache read in full
     (Kimi's, reportedly: write 1.0, read 1.0).

Synthetic rows only, no database, no network.

  PYTHONPATH=brain:. python3 scripts/test_rig_tool_surface.py
"""
import os
os.environ.setdefault("CSERIES_OPERATOR_ENTITY_ID", "9003")  # the operator Host both catalogs are built for
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, os.environ.get("CSERIES_BRAIN", str(REPO / "brain")))
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts"))

import rig_tool_surface as R  # noqa: E402

FAILS: list[str] = []


def check(cond, what):
    print(("PASS " if cond else "FAIL ") + what)
    if not cond:
        FAILS.append(what)


def led(calls=1):
    return {"calls": calls, "tools": {"groups": ["projects"], "chars": 1000}}


def turn(out, tid):
    return next(t for t in out["per_turn"] if t["id"] == tid)


def how(t, tool):
    return next((p["how"] for p in t["per_tool"] if p["tool"] == tool), None)


SC = "project:1"
CAT = R.Catalog(bridge=True, operator=True)
held = CAT.held(SC)
for name, lane in (("nac_status", "read"), ("get_project", "read"), ("local_grep", "read"),
                   ("workspace_file", "operate"), ("build", "host")):
    assert held.get(name, {}).get("lane") == lane, (name, held.get(name))
assert "review.fan_out" not in held

# ── 1. a platform-started turn is not replayed on the person's earlier words ──
rows = [
    {"id": 1, "at": "2026-10-01T10:00:00+00:00", "role": "user", "sc": SC,
     "content": "check nac_status for me"},
    {"id": 2, "at": "2026-10-01T10:00:10+00:00", "role": "assistant", "sc": SC,
     "ledger": led(), "tool_calls": [{"tool": "get_project"}]},
    {"id": 3, "at": "2026-10-01T10:01:00+00:00", "role": "assistant", "sc": SC,
     "origin": "continuation", "ledger": led(), "tool_calls": [{"tool": "nac_status"}]},
]
out = R.run(rows, bridge=True, operator=True)
t2, t3 = turn(out, 2), turn(out, 3)
check(how(t3, "nac_status") == "search",
      f"a continuation turn gets no prefetch from the person's earlier message (got {how(t3, 'nac_status')})")
check(t2.get("person_message") is True and t3.get("person_message") is False,
      "each turn says whether it had a person's message of its own")
check(t2.get("reproduced") is not None and t3.get("reproduced") is None,
      "a turn whose message is not in the export is not scored for reproduction")
check(out.get("turns_without_person_message") == 1, "the report counts those turns")

# ── 2. staples: K tools the core does not already hold, from tools it can hold ──
before = "2026-09-01T00:00:00+00:00"
srows = []
for i, calls in enumerate([["build", "workspace_file", "review.fan_out"],
                           ["build", "workspace_file", "review.fan_out"],
                           ["build", "get_project", "review.fan_out"],
                           ["build", "local_grep"]]):
    srows.append({"id": 100 + i, "at": f"2026-08-0{i + 1}T00:00:00+00:00", "role": "assistant",
                  "sc": SC, "tool_calls": [{"tool": c} for c in calls]})
out = R.run(srows, bridge=True, operator=True, staples_k=2, train_until=before)
check(out["staples"].get("workbench") == ["workspace_file", "get_project"],
      f"--staples 2 adds two tools: not the door `build`, not unheld `review.fan_out` "
      f"(got {out['staples'].get('workbench')})")

# ── 3. the cut is compared as a time ──
trows = [
    {"id": 200, "at": "2026-09-21T22:46:59+00:00", "role": "assistant", "sc": SC,
     "tool_calls": [{"tool": "get_project"}]},
    {"id": 201, "at": "2026-09-21T22:47:01+00:00", "role": "assistant", "sc": SC,
     "tool_calls": [{"tool": "local_grep"}]},
    # 20:00 at UTC-5 is 01:00Z on the 22nd: after the cut.
    {"id": 202, "at": "2026-09-21T20:00:00-05:00", "role": "assistant", "sc": SC,
     "tool_calls": [{"tool": "local_grep"}]},
]
st = R.staples_from(trows, until="2026-09-21T22:47Z", k=1)
check(st.get("workbench") == ["get_project"],
      f"only turns before the cut train the staples, offsets read as times (got {st})")

# ── 4. the prices for what search mode adds ──
F = CAT.entry_chars("nac_status")
prow = [
    {"id": 300, "at": "2026-10-02T10:00:00+00:00", "role": "user", "sc": SC, "content": "the and of"},
    {"id": 301, "at": "2026-10-02T10:00:10+00:00", "role": "assistant", "sc": SC,
     "ledger": led(calls=2), "tool_calls": [{"tool": "nac_status"}]},
    {"id": 302, "at": "2026-10-02T10:01:00+00:00", "role": "user", "sc": SC, "content": "the and of"},
    {"id": 303, "at": "2026-10-02T10:01:10+00:00", "role": "assistant", "sc": SC,
     "ledger": led(calls=1), "tool_calls": []},
    {"id": 304, "at": "2026-10-02T10:02:00+00:00", "role": "user", "sc": SC, "content": "the and of"},
    {"id": 305, "at": "2026-10-02T10:02:10+00:00", "role": "assistant", "sc": SC,
     "ledger": led(calls=3), "tool_calls": []},
]
out = R.run(prow, bridge=True, operator=True)
p301 = turn(out, 301).get("price_search_parts") or {}
p303 = turn(out, 303).get("price_search_parts") or {}
p305 = turn(out, 305).get("price_search_parts") or {}
# A search result carries up to PREFETCH matches: the needed schema plus
# PREFETCH-1 of the median size among what the search could return.
RESULT = F + (R.PREFETCH - 1) * R._median(CAT.entry_chars(n) for n in CAT.doors.searchable)
check(turn(out, 301)["searches"] == 1 and abs(p301.get("found", 0) - 2.25 * RESULT) < 1e-6,
      f"a search's result (the needed schema + {R.PREFETCH - 1} median ones) in a 3-call turn "
      f"costs 2.25 x its size (size={RESULT}, got {p301.get('found')})")
check(abs(p303.get("new_notes", 0) - (1.0 + 1.25) * F) < 1e-6,
      f"its note on the next (1-call) turn: 1.0F, plus the one later rewrite 1.25F (got {p303.get('new_notes')})")
check(p305.get("new_notes") == 0 and abs(p305.get("standing_notes", 0) - 3 * 0.1 * F) < 1e-6,
      f"after that it is not rendered again and is read at 0.1 per call (got {p305})")
check(all(p and abs(sum(p.values()) - turn(out, i)["price_search"]) <= 1
          for i, p in ((301, p301), (303, p303), (305, p305))),
      "the parts are the turn's price")

# ── 5. the rig searches what the Host searches ──
import asyncio  # noqa: E402
import copy  # noqa: E402
from mastercode.runtime.tool_access import read_safe_ops  # noqa: E402
from mastercode.runtime.tool_doors import DoorCatalog  # noqa: E402
from mastercode.runtime.tool_lanes import lane_for  # noqa: E402
from mastercode.runtime.tools import _build_tool_schemas  # noqa: E402

# The session's own build (session.py, the tool-list build), independently.
_caps = [s for s in asyncio.run(_build_tool_schemas(R._Pool(True), 9003, interaction_context=None))
         if lane_for(s["function"]["name"], s) != "retired"]
_rst = {s["function"]["name"] for s in _caps if lane_for(s["function"]["name"], s) == "read"}
_rso = {s["function"]["name"]: read_safe_ops(s["function"]["name"], s) for s in _caps
        if s["function"]["name"] not in _rst and read_safe_ops(s["function"]["name"], s)}
_sess = DoorCatalog(copy.deepcopy(_caps), read_safe_tools=_rst, read_safe_ops=_rso)
check(CAT.doors.searchable == _sess.searchable and set(CAT.doors.entries) == set(_sess.entries),
      f"the rig's searchable set is the session's DoorCatalog set "
      f"({len(CAT.doors.searchable)} vs {len(_sess.searchable)})")
check("build" not in CAT.doors.searchable and "close_turn" not in CAT.doors.searchable,
      "the build doors and close_turn are not searchable: the core holds them")
from mastercode.runtime.tool_search import tokens as _tokens  # noqa: E402
_n = _same = 0
for _name in sorted(_sess.searchable):
    _q = _sess.entries[_name].purpose
    if not _tokens(_q):
        continue
    _n += 1
    _rig = [n for n, _ in CAT.doors.index().search(_q, limit=5, allowed=set(CAT.doors.searchable))]
    _host = [n for n, _ in _sess.index().search(_q, limit=5, allowed=set(_sess.searchable))]
    _same += _rig == _host
check(_n > 100 and _same == _n,
      f"every tool purpose ranks the same top 5 in the rig and in the session's index ({_same}/{_n}; "
      f"measured 111/123 and 103/123 with the rig's own catalog)")

# ── 6. every pricing ──
_K, _A = R.PRICINGS["kimi-sub"], R.PRICINGS["anthropic"]
check(R.tool_cost(1000, 3, True, _K) == 3000 and abs(R.tool_cost(1000, 3, True, _A) - 300) < 1e-6,
      "a cached block: read in full on a full-count subscription, at 0.1 on Anthropic")
check(R.tool_cost(1000, 3, False, _K) == 3000 and abs(R.tool_cost(1000, 3, False, _A) - 1450) < 1e-6,
      "a cold block: no write premium on the subscription, 1.25 then 0.1 on Anthropic")
check(R.tail_cost(1000, 3, _K) == 3000, "the turn's tail on the subscription: in full on every call")
_t = turn(out, 301)
check(set(_t["prices"]) == set(R.PRICINGS) and all(
          set(v) >= {"groups", "groups_recorded", "search"} for v in _t["prices"].values()),
      "each turn carries its price under every pricing")
check(set(out["prices"]) == set(R.PRICINGS) and all(
          set(v) == {"all", "person", "platform"} for v in out["prices"].values()),
      "the summary reports every pricing for all, person and platform-started turns")
_kp = _t["price_parts"]["kimi-sub"]
check(abs(_kp["found"] - 2 * RESULT) < 1e-6,
      f"a search's result in a 3-call turn on the subscription: read in full on the 2 calls after "
      f"the search (got {_kp['found']}, want {2 * RESULT})")

if FAILS:
    print(f"\n{len(FAILS)} failure(s)")
    sys.exit(1)
print("\nall rig checks passed")
