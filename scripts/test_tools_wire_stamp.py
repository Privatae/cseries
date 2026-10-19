#!/usr/bin/env python3
"""Every model call's result carries the size and hash of the tool list it
sent (provider_gateway._tools_wire_stamp), and the context ledger copies them
onto that call's per_call entry. The hash has to be the SAME one the turn's `tools.first_call_sha` uses
(session._bytes_sha) and the size the same as `tools.chars`, or a per-call
value cannot be compared with the turn's and a mid-turn change in the list is
invisible again.

  PYTHONPATH=brain python3 scripts/test_tools_wire_stamp.py
"""
import json
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, os.environ.get("CSERIES_BRAIN", str(REPO / "brain")))

from mastercode.provider_gateway import _tools_wire_stamp  # noqa: E402
from mastercode.runtime.session import _bytes_sha, _build_host_orchestration_tools  # noqa: E402

FAILS = []


def check(cond, what):
    print(("PASS " if cond else "FAIL ") + what)
    if not cond:
        FAILS.append(what)


tools = _build_host_orchestration_tools()
st = _tools_wire_stamp(tools)
check(st["sha"] == _bytes_sha(tools), "the stamp's sha is the turn ledger's own hash (_bytes_sha)")
check(st["chars"] == len(json.dumps(tools, default=str)),
      "the stamp's size is measured the way the ledger's tools.chars is")
check(_tools_wire_stamp(None) == {"chars": 0, "sha": None} and _tools_wire_stamp([]) == {"chars": 0, "sha": None},
      "no tools: zero characters and no hash")
check(_tools_wire_stamp(tools[:1]) != st, "a different list gives a different stamp")

src = (REPO / "brain/mastercode/runtime/session.py").read_text()
check('_entry["tools_chars"] = _tw.get("chars")' in src and '_entry["tools_sha"] = _tw.get("sha")' in src,
      "the per-call ledger entry copies the stamp (session._note_served)")
gw = (REPO / "brain/mastercode/provider_gateway.py").read_text()
check('result["tools_wire"] = dict(_tools_wire)' in gw,
      "every dialled result is stamped (provider_gateway.call_model._dial)")

# ── the stamp, through call_model itself (no network: every dial is stubbed) ──
# The source check above cannot tell a live line from a dead one. These run
# the three ways a result leaves call_model after a dial: the primary's own
# answer, the failover's answer, and the re-dial after a refused picture.
import asyncio  # noqa: E402
from mastercode import provider_gateway as GW  # noqa: E402

_dials: list = []
_answers: list = []


async def _stub_remote(**kw):
    _dials.append(kw.get("model"))
    return _answers.pop(0) if _answers else {"content": "ok"}


async def _no_failure_log(*a, **k):
    return None

_real = (GW._call_remote, GW._call_anthropic, GW._call_local, GW._log_call_failure)
GW._call_remote = GW._call_anthropic = GW._call_local = _stub_remote
GW._log_call_failure = _no_failure_log
_want = _tools_wire_stamp(tools)
_png = "iVBORw0KGgoAAAAAAAAAAAAAAAAAAAAA"
try:
    _dials.clear()
    _answers[:] = [{"content": "primary answered"}]
    r = asyncio.run(GW.call_model([{"role": "user", "content": "hi"}], tools=tools,
                                  provider="kimi", provider_model="k-unknown", max_tokens=64))
    check(r.get("tools_wire") == _want, f"the primary's own answer is stamped (got {r.get('tools_wire')})")

    _dials.clear()
    _answers[:] = [{"error": "upstream 503", "content": ""}, {"content": "fallback answered"}]
    r = asyncio.run(GW.call_model([{"role": "user", "content": "hi"}], tools=tools,
                                  provider="kimi", provider_model="k-unknown", max_tokens=64,
                                  fallback_provider="openrouter", fallback_model="other/model"))
    check(r.get("content") == "fallback answered" and r.get("failed_over")
          and r.get("tools_wire") == _want,
          f"the failover's answer is stamped too (dials {_dials}, got {r.get('tools_wire')})")

    _dials.clear()
    _answers[:] = [{"error": "kimi 400: image input is not supported", "content": ""},
                   {"content": "answered with a note"}]
    _pic = [{"role": "user", "content": [
        {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": _png}},
        {"type": "text", "text": "what is this"}]}]
    r = asyncio.run(GW.call_model(_pic, tools=tools, provider="kimi",
                                  provider_model="k-unknown", max_tokens=64))
    check(len(_dials) == 2 and r.get("content") == "answered with a note"
          and r.get("tools_wire") == _want,
          f"the re-dial after a refused picture is stamped (dials {_dials}, got {r.get('tools_wire')})")

    _dials.clear()
    r = asyncio.run(GW.call_model([{"role": "user", "content": "hi"}],
                                  provider="kimi", provider_model="k-unknown", max_tokens=64))
    check(r.get("tools_wire") == {"chars": 0, "sha": None}, "a call with no tools is stamped as none")
finally:
    GW._call_remote, GW._call_anthropic, GW._call_local, GW._log_call_failure = _real

# ── the Host's mirror shows what it showed before (the stamp is measurement only) ──
# `context action='status'` (runtime/context_ops._status) hands the Host
# context_budget.window_report, whose last_turn.per_call is the ledger's
# per_call. The two new keys are for the ledger and the rig; a Host reading its
# own window must see exactly the entries it saw before they existed.
import datetime  # noqa: E402
from mastercode import context_budget as _cb  # noqa: E402


class _LedgerPool:
    async def fetchrow(self, q, *a):
        return {"created_at": datetime.datetime(2026, 10, 5, tzinfo=datetime.timezone.utc),
                "ledger": json.dumps({"calls": 2, "per_call": [
                    {"in": 1000, "cached": 800, "cache_write": 100, "out": 50, "usd": 0.001,
                     "tools_chars": 60871, "tools_sha": "0123456789ab"},
                    {"in": 1200, "cached": 1000, "cache_write": 0, "out": 40}]})}

    async def fetch(self, q, *a):
        return []


_w = asyncio.run(_cb.window_report(_LedgerPool(), 3))
_pc = (_w.get("last_turn") or {}).get("per_call") or []
check(_pc == [{"in": 1000, "cached": 800, "cache_write": 100, "out": 50, "usd": 0.001},
              {"in": 1200, "cached": 1000, "cache_write": 0, "out": 40}],
      f"the Host's window shows each call as before, without tools_chars/tools_sha (got {_pc})")
check((_w.get("last_turn") or {}).get("tokens_in_total") == 2200, "...and still sums the calls")

if FAILS:
    print(f"\n{len(FAILS)} failure(s)")
    sys.exit(1)
print("\nall tools-wire stamp checks passed")
