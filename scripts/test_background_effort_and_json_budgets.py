#!/usr/bin/env python3
"""The BACKGROUND summary call must forward its route's reasoning effort, and
its budget must survive a reasoning model.

Measured on one Host. All three resolved the `consolidation` route
and then dropped its `effort`, so the model chose for itself (thinking on) and
spent the output budget on thinking instead of the answer:

  * the classifier truncated mid-string —
    raw='{"action":"UPDATE","target_id":748' — so it "defaulted to ADD" and
    stored a duplicate of the memory it had just decided to supersede;
  * graph extraction logged 'no JSON object in model reply (2241 chars)' and
    dead-lettered 12 rows at GRAPH_EXTRACT_MAX_FAILS (5) each;
  * the thread consolidator returned an empty summary.

The classifier and graph extraction went with their code (no second model
decides what to remember, and the memory graph is gone), and with them the
turn consolidator's
routed_model_fn they rode. The thread summary is the background caller left.

The user's own setting never arrived at any of it — the user's consolidation
role was effort=high in the Orchestrator. The rule is now one function,
inference_config.route_effort: the route's pick, else an EXPLICIT "low",
because no effort on the wire is NOT the same as effort=low (provider_gateway
sends `reasoning: {"effort": ...}` only when it is given one).

Also gates the dispatch refusal: it knew the missing capabilities and only
stated them, so the host burned a whole tool round re-deriving the grant
("Tool round 2: ALL 1 tools failed — dispatch(error): ... manage_site (missing
write_context)"). It now ends with a machine-readable `Retry with grant:` line.

Pure: no network, no DB (fake pools and fake model_fns throughout).
Run from the repo root:
    PYTHONPATH=.:brain python3 scripts/test_background_effort_and_json_budgets.py
"""
import json
import os
import sys
from pathlib import Path

_root = os.environ.get("APP_ROOT") or str(Path(__file__).resolve().parents[1])
sys.path.insert(0, _root)
sys.path.insert(0, os.path.join(_root, "brain"))

from mastercode import inference_config as ic          # noqa: E402
from mastercode.runtime import agent_grant as ag       # noqa: E402
from mastercode.runtime import scoped_agent as sa      # noqa: E402
from mastercode.runtime import tools as rt             # noqa: E402

FAILS = 0


def check(label: str, cond: bool, detail: str = "") -> None:
    global FAILS
    print(("  ok   " if cond else "  FAIL ") + label
          + (f"  [{detail}]" if detail and not cond else ""))
    if not cond:
        FAILS += 1


class FakePool:
    """Enough asyncpg surface that nothing under test touches a database."""

    async def fetch(self, sql, *args):
        return []

    async def fetchrow(self, sql, *args):
        return None

    async def fetchval(self, sql, *args):
        return None

    async def execute(self, sql, *args):
        return "UPDATE 0"


# ── 0. the rule itself, in ONE place ────────────────────────────────────────
print("== 0. inference_config.route_effort — one rule, mirroring the chat turn ==")
check("the route's own effort wins", ic.route_effort({"effort": "high"}) == "high")
check("surrounding whitespace is stripped",
      ic.route_effort({"effort": "  high "}) == "high")
check("an unset effort falls back to an EXPLICIT low",
      ic.route_effort({"provider": "kimi", "model": "k3"}) == "low")
check("an empty-string effort is a fallback, not an effort",
      ic.route_effort({"effort": "   "}) == "low")
check("a None effort falls back", ic.route_effort({"effort": None}) == "low")
check("no route at all still yields low, never None", ic.route_effort(None) == "low")
_sess = open(os.path.join(_root, "brain/mastercode/runtime/session.py"),
             encoding="utf-8").read()
check("the chat turn's literal rule is the one being mirrored",
      '_reasoning_effort = (_chat_route.get("effort") or "").strip() or "low"'
      in _sess)


# The routes the summary call resolves, per entity: one with an effort set,
# one without.
ROUTES = {
    3: {"provider": "kimi", "model": "k3", "base_url": None, "effort": "high",
        "fallback_provider": "openrouter", "fallback_model": "moonshotai/kimi-k3"},
    4: {"provider": "kimi", "model": "k3", "base_url": None},   # no effort set
}


# ── 1. no thread summary call remains ───────────────────────────────────────
# The rolling summary's call (thread_consolidator._default_model_fn) is
# retired with its module (migrations/v241); the wrapper check below stays.
print("\n== 1. no thread summary call remains ==")
_orig_role_route = ic.resolve_role_route
_orig_entity_model = rt._call_entity_model
check("no thread summary module ships",
      not (Path(_root) / "brain/mastercode/thread_consolidator.py").exists())

ic.resolve_role_route = _orig_role_route
rt._call_entity_model = _orig_entity_model

# The wrapper this lane goes through must NAME reasoning_effort, or the kwarg
# would be silently dropped one frame below the call under test.
import inspect  # noqa: E402
check("runtime/tools._call_entity_model names reasoning_effort explicitly",
      "reasoning_effort" in inspect.signature(_orig_entity_model).parameters)


# ── 2. the dispatch refusal returns the grant that WOULD be accepted ────────
print("\n== 2. scoped_agent refusal ends with a machine-readable retry grant ==")
# manage_site and publish_site both declare required_bits 4 (write_context) in
# runtime/meta_tool_caps.py — the exact case from the log.
#
# `read_url` rides along, and it is load-bearing rather than decoration.
# manage_site and publish_site are both `operate` ops, so a scope of only those
# two is refused one wall EARLIER — by clerk_scope_refusal, which says "run
# these yourself" — and never reaches the grant wall this section is about.
# The third tool is what keeps the dispatch an agent's: reading one web page
# is untrusted work (agents are only for untrusted work). read_url needs external_api, which the grant below already
# holds, so the retry grant adds exactly write_context and every assertion here
# tests what it always tested.
#
# The door refuses this scope one question earlier: an
# in-platform tool beside a reader (scoped_agent.in_platform_scope_refusal),
# naming the reader-only scope. The grant text below is the run-time wall's,
# which a caller that does not pass the door still meets; one helper builds
# it for both sites.
_door = sa.dispatch_precheck(
    ["manage_site", "publish_site", "read_url"], ["read_context", "external_api"])
check("the door refuses the mixed scope before its grant",
      bool(_door) and 'tool_scope: ["read_url"]' in _door
      and "Retry with grant" not in _door, str(_door))
_scope = {"manage_site", "publish_site", "read_url"}
_bits = ag.parse_grant(["read_context", "external_api"])[0]
msg = sa.unreachable_grant_refusal(ag.unreachable_tools(_scope, _bits), _scope, _bits,
                                   at_door=False)
check("the dispatch is still refused", bool(msg))
# The diagnosis names the unreachable tools and leads with dropping
# in-platform tools before widening the grant.
check("the diagnosis names the tools and their missing capabilities",
      bool(msg) and "you scoped tools whose capabilities you did not grant" in msg
      and "manage_site (missing write_context)" in msg
      and "do that part yourself" in msg, str(msg))
check("it ends with the machine-readable line", "Retry with grant: " in (msg or ""),
      str(msg))
_line = (msg or "").rsplit("Retry with grant: ", 1)[-1].strip()
try:
    _names = json.loads(_line)
except Exception:
    _names = None
check("the line is parseable JSON", isinstance(_names, list), _line)
# manage_site and publish_site are in-platform work: the retry leaves them out of the scope and adds no capability
# for them, so the line cannot carry the Host straight back to dispatching
# its own work with a wider grant.
check("the retry scope leaves the in-platform tools out",
      'Retry with tool_scope: ["read_url"]' in (msg or ""), str(msg))
check("no capability is added for an in-platform tool",
      _names == ["read_context", "external_api"], _line)
# The additive retry still holds for the tool an agent is for: read_url reads
# the web and needs external_api.
msg = sa.dispatch_precheck(["read_url"], ["read_context"])
_line = (msg or "").rsplit("Retry with grant: ", 1)[-1].strip()
try:
    _names = json.loads(_line)
except Exception:
    _names = None
check("the granted capability is KEPT (additive, never a narrowing)",
      "read_context" in (_names or []), _line)
check("the missing capability is ADDED", "external_api" in (_names or []), _line)
check("nothing else is invented",
      set(_names or []) == {"read_context", "external_api"}, _line)

# Both refusal sites carry it. dispatch_precheck is the synchronous DOOR (no
# task row exists yet, so a bad grant never becomes a user-visible failed
# task); the wall inside run_scoped_agent stays as defense in depth for
# dispatch_sync callers that never pass the door. Both build
# their text with one helper, unreachable_grant_refusal, which ends with the
# line.
_sa_src = open(os.path.join(_root, "brain/mastercode/runtime/scoped_agent.py"),
               encoding="utf-8").read()
check("both refusal sites call the one refusal, which calls the line builder",
      _sa_src.count("_retry_grant_line(") == 2
      and _sa_src.count("unreachable_grant_refusal(") == 3,
      (_sa_src.count("_retry_grant_line("), _sa_src.count("unreachable_grant_refusal(")))

# The union is computed from BITS, not parsed back out of the prose.
check("a grant that already suffices round-trips unchanged",
      ag.retry_grant(["manage_site"], ag.parse_grant(["write_context"])[0])
      == ["write_context"])
check("an empty scope returns exactly what was granted",
      ag.retry_grant([], ag.parse_grant(["read_memory"])[0]) == ["read_memory"])
check("an unknown tool contributes nothing (a registry gap is not a grant error)",
      ag.retry_grant(["no_such_tool_xyz"], ag.parse_grant(["read_context"])[0])
      == ["read_context"])
check("the names come back in bit order",
      ag.retry_grant([], ag.parse_grant(["write_memory", "read_context"])[0])
      == ["read_context", "write_memory"])

print()
print("FAIL" if FAILS else "PASS", f"({FAILS} failing checks)")
sys.exit(1 if FAILS else 0)
