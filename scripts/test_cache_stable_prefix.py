#!/usr/bin/env python3
"""Gate: the cached part of the Host's prompt carries nothing that moves turn
to turn, and the ledger can prove it. No DB, no network.

Measured on one Host: four scheduled turns 15 seconds apart each
read 0 from the prompt cache and rewrote ~101k tokens, because the "static"
prompt was not static — the map printed a live online count and bastion names
in heartbeat order (34 sizes in 62 turns), and the source-code block's felt
state flipped on a wording threshold.

  1. The source_code block and its felt-state half (felt_senses) are gone
     with the simulated psyche, and
     with them skill_events, relationships and integrity: none is in the
     schema, so none can move the cached part. The static hash is taken
     after the last static append.
  2. prompt_log carries a content hash per block, so a same-length change is
     visible.
  3. The turn ledger hashes the first call's tool list and the static text.

The map's machines line is pinned in test_host_map.py.

  PYTHONPATH=brain:. python3 scripts/test_cache_stable_prefix.py
"""
import asyncio
import inspect
import os
import sys
from pathlib import Path
from turn_source import turn_source  # session.py and the modules split out of it

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, os.environ.get("CSERIES_BRAIN", str(REPO / "brain")))
sys.path.insert(0, str(REPO))
fails = 0


def check(name, ok, detail=""):
    global fails
    print(("  ok    " if ok else "  FAIL  ") + name + (f"  — {detail}" if detail and not ok else ""))
    if not ok:
        fails += 1


from mastercode.runtime import prompt_builder as pb  # noqa: E402
from mastercode.runtime import prompt_schema as ps  # noqa: E402

# ── 1. the simulated psyche's blocks are gone, the static hash is last ──────
blocks = {b.name: b for b in ps.PROMPT_SCHEMA}
_GONE = ("source_code", "felt_senses", "skill_events", "relationships", "integrity")
check("no source_code, felt_senses, skill_events, relationships or integrity block in the schema",
      not [n for n in _GONE if n in blocks], str([n for n in _GONE if n in blocks]))
check("no builder for them is left (prompt_builder._build_source_code_block, "
      "prompt_schema._b_source_code / _b_felt_senses)",
      not hasattr(pb, "_build_source_code_block")
      and not hasattr(ps, "_b_source_code") and not hasattr(ps, "_b_felt_senses"))
sess_txt = turn_source(REPO)
_i = sess_txt.index("_static_sys_sha = _bytes_sha(")
_ceigas = sess_txt.index("# -- The operator kill switch on the inference crossing --")
_host = sess_txt.index('grok_messages[0]["content"] = _host_rules + ')
check("static hash is taken after the last static append and outside the orchestration branch",
      _host < _i < _ceigas
      and sess_txt[sess_txt.rfind("\n", 0, _i) + 1:_i].startswith("            ")
      and not sess_txt[sess_txt.rfind("\n", 0, _i) + 1:_i].startswith("                "))

# ── 2. prompt_log carries a hash per block ──────────────────────────────────


class _Conn:
    def __init__(self, sink):
        self.sink = sink

    async def execute(self, sql, *args):
        self.sink.append(args)


class _Acq:
    def __init__(self, sink):
        self.sink = sink

    async def __aenter__(self):
        return _Conn(self.sink)

    async def __aexit__(self, *a):
        return False


class _LogPool:
    def __init__(self):
        self.rows = []

    def acquire(self):
        return _Acq(self.rows)


class _RT:
    def __init__(self):
        self.pool = _LogPool()


class _Sess:
    entity_id = 9003
    session_id = "s"


rt = _RT()
asyncio.run(ps._write_prompt_log(rt, _Sess(), "per_rebuild", [("map", 10, "per_rebuild")], False,
                                 block_hashes={"map": "abc123def456"}))
import json  # noqa: E402
payload = json.loads(rt.pool.rows[0][6]) if rt.pool.rows else []
check("prompt_log block entries carry sha", payload and payload[0].get("sha") == "abc123def456",
      str(payload))
src = inspect.getsource(ps._build_grouped)
check("_build_grouped hashes every rendered block and passes the hashes on",
      "block_hashes[block.name] = hashlib.sha256" in src and "block_hashes=block_hashes" in src)

# ── 3. the ledger hashes the tool list and the static text ─────────────────
sess_src = turn_source(REPO)
# One tool mode (the groups mode and its own list build are
# gone), so the normal list is built, and hashed, in ONE place; a root turn's
# allowlist (runtime/root_turn) replaces it and is hashed where it
# is built, the second site. A third hash site would mean another list build,
# the shape that let the list change inside a turn. That every call of a turn
# sends that same list byte for byte, and that the ledger's hash is call 1's,
# is driven in test_host_context_trims.py (part_loop, "one list per turn").
check("ledger records the first call's tool-list hash: the one list build and a root turn's allowlist",
      sess_src.count("_tools_first_call_sha = _bytes_sha(tools)") == 2
      and '"first_call_sha": _loc.get("_tools_first_call_sha")' in sess_src,
      f"{sess_src.count('_tools_first_call_sha = _bytes_sha(tools)')} hash sites")
check("ledger records the static-text hash", '"static_sha": _static_sha' in sess_src)
ns: dict = {}
exec(sess_src[sess_src.index("def _bytes_sha"):sess_src.index("\n\n\n", sess_src.index("def _bytes_sha"))],
     {"json": json, "hashlib": __import__("hashlib")}, ns)
_sha = ns["_bytes_sha"]
check("hash: same bytes → same hash; order matters (the wire's order)",
      _sha([{"a": 1}, {"b": 2}]) == _sha([{"a": 1}, {"b": 2}])
      and _sha([{"a": 1}, {"b": 2}]) != _sha([{"b": 2}, {"a": 1}]) and len(_sha("x")) == 12)

print(f"\n{fails} failure(s)" if fails else "\nall checks passed")
sys.exit(1 if fails else 0)
