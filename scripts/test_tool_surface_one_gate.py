#!/usr/bin/env python3
"""Gates: what the prompt ADVERTISES and what the model is HANDED agree.

The rule is one gate, decided in one place: a tool is callable iff it
appears in the tool list sent to the model for that surface. There were two,
and they disagreed.

  * session.py built `tools=` iff behavioral_params.response_style ==
    "conversational" AND the entity was above the enrollment tier.
  * prompt_schema rendered the whole "Your Tools" inventory with gate=always,
    off the CEIGAS authorization walk, which never reads response_style.

response_style is a prose-style field. Its live values are conversational /
concise / structured / NULL, and none of them means "may not hold tools" — so
an unseeded host (no response_style at all) got a 20,834-character inventory
beside an EMPTY tools array and narrated tool calls it could not make.

Measured 2026-09-02, one Host: 6 prompt blocks,
tool_inventory 20,834 chars, one model call, 237 completion tokens, zero tool
calls, turn over. A second Host the same. Both had been failing their boot
identity seed since 2026-08-11, so response_style was
never written — which is the condition this suite exists to make harmless.

The docstring on _b_tool_inventory claimed it used "the same gating logic that
builds tools= for the LLM". It did not, and that false claim is why no such
assertion existed: it reads as though the divergence is impossible.

  gate1  the capability predicate keys off the enrollment tier, and DEFAULT-DENIES
         an absent permission_overrides (that absence IS a capability statement)
  gate2  response_style NEVER changes the answer — the whole point
  gate3  the prompt's tool_inventory Block is gated by the SAME predicate, not
         `always` and not a response_style test
  gate4  session.py's tools= site calls the shared predicate and does not
         re-derive the decision from response_style
  gate5  the declared behavioral default is actually applied, not dead code
"""
import inspect
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "brain"))

from mastercode.inference_config import DEFAULT_BEHAVIORAL_PARAMS  # noqa: E402
from mastercode.runtime import prompt_schema as _ps  # noqa: E402
from mastercode.runtime.tool_access import (  # noqa: E402
    ENROLLMENT_CAPS_CEILING, host_orchestration_enabled)

fails = []


def ck(name, cond, extra=""):
    if cond:
        print(f"OK    {name}")
    else:
        fails.append(name)
        print(f"FAIL  {name} {extra}")


def ent(caps=None, style="__unset__", **extra):
    e = dict(extra)
    if caps is not None:
        e["permission_overrides"] = {"bot_capabilities": caps}
    if style != "__unset__":
        e["behavioral_params"] = {"response_style": style}
    return e


# ── gate1: the tier is the capability question ──────────────────────
above = ENROLLMENT_CAPS_CEILING + 1
ck("gate1 above the enrollment ceiling holds the tool surface",
   host_orchestration_enabled(ent(caps=above)) is True)
ck("gate1 at the enrollment ceiling does not",
   host_orchestration_enabled(ent(caps=ENROLLMENT_CAPS_CEILING)) is False)
ck("gate1 absent permission_overrides DEFAULT-DENIES (absence is a capability "
   "statement, unlike an absent prose style)",
   host_orchestration_enabled({}) is False)
ck("gate1 None entity denies rather than raising",
   host_orchestration_enabled(None) is False)
ck("gate1 jsonb-as-str permission_overrides is parsed, not silently denied",
   host_orchestration_enabled(
       {"permission_overrides": '{"bot_capabilities": %d}' % above}) is True)
ck("gate1 a non-numeric capability denies rather than raising",
   host_orchestration_enabled(
       {"permission_overrides": {"bot_capabilities": "banana"}}) is False)

# ── gate2: prose style is not a capability ──────────────────────────
# This is the regression. Every one of these was False before the fix for any
# entity whose row had not been seeded, or whose author simply preferred a
# different prose style.
for style in (None, "conversational", "concise", "structured", "", "voice"):
    ck(f"gate2 style={style!r} does not change the answer (tier decides)",
       host_orchestration_enabled(ent(caps=above, style=style)) is True,
       f"style={style!r}")
ck("gate2 an entity with NO behavioral_params at all still gets tools",
   host_orchestration_enabled(ent(caps=above)) is True)
ck("gate2 style cannot RESCUE an enrollment-tier entity either",
   host_orchestration_enabled(
       ent(caps=ENROLLMENT_CAPS_CEILING, style="conversational")) is False)

# ── gate3: the prompt advertises exactly when the model is handed ───
# The tool_inventory Block is gone; the
# MAP block carries the tools section, and the gate moved INSIDE its fact
# gatherer (_map_facts: tools_meta stays None unless orchestration_tools says
# yes, and the renderer omits the tools section + every "call X" sentence for
# None). The predicate is the same one; this gate pins that it is still the
# one consulted, and that the renderer honours None.
ck("gate3 the tool_inventory Block is retired (the map replaced it)",
   not [b for b in _ps.PROMPT_SCHEMA if b.name == "tool_inventory"])
_map = [b for b in _ps.PROMPT_SCHEMA if b.name == "map"]
ck("gate3 the map Block exists", len(_map) == 1, repr(_map))
_gate = _ps.orchestration_tools
_facts_src = inspect.getsource(_ps._map_facts)
_pos_gate = _facts_src.find("if orchestration_tools(sess):")
_pos_walk = _facts_src.find("_collect_authorized_tool_meta(")
ck("gate3 the map's tool walk is guarded by the shared predicate",
   0 <= _pos_gate < _pos_walk, f"gate@{_pos_gate} walk@{_pos_walk}")
ck("gate3 the guard is not the response_style test",
   "response_style" not in _facts_src)


# The predicate must AGREE with the tools= predicate on every shape.
class _S:
    def __init__(self, e):
        self.entity = e


_shapes = [
    ent(caps=above), ent(caps=ENROLLMENT_CAPS_CEILING), {},
    ent(caps=above, style=None), ent(caps=above, style="concise"),
    ent(caps=ENROLLMENT_CAPS_CEILING, style="conversational"),
    {"permission_overrides": '{"bot_capabilities": %d}' % above},
]
_mismatch = [e for e in _shapes
             if _gate(_S(e)) is not host_orchestration_enabled(e)]
ck("gate3 ROUND TRIP: advertised == callable on every shape",
   not _mismatch, repr(_mismatch))
ck("gate3 the gate is not vacuously False (it says yes to a real host)",
   _gate(_S(ent(caps=above))) is True)

# The renderer: tools_meta=None (no surface) ⇒ no tools section and no
# instruction to call anything; [] ⇒ the section with a zero count.
from mastercode.runtime.prompt_builder import _build_map  # noqa: E402

_base = dict(apps=[{"id": "circles", "name": "Circles"}], current_app_id="circles",
             project_count=2, repos_granted=[], repos_indexed=[],
             skills=["apps"], permissions=[])
_no_tools = _build_map(tools_meta=None, **_base)
_lp = {"name": "list_projects", "category": "projects", "priority": 1, "purpose": "x", "requires": None}
_or = {"name": "orient", "category": "environment", "priority": 1, "purpose": "x", "requires": None}
_with_tools = _build_map(tools_meta=[_lp, _or], **_base)
ck("gate3 renderer: no tool surface ⇒ no Tools section",
   "**Tools" not in _no_tools, _no_tools)
ck("gate3 renderer: no tool surface ⇒ no call instruction anywhere",
   "`orient" not in _no_tools and "describe_app" not in _no_tools
   and "list_projects" not in _no_tools and "context(action" not in _no_tools, _no_tools)
ck("gate3 renderer: with a surface the Tools count and find_tools appear",
   "**Tools.** 2." in _with_tools and "`find_tools`" in _with_tools, _with_tools)
# A surface that holds tools but NOT orient must not be told to call it
# (a surface holding none of the circles/workbench tools).
_no_orient = _build_map(tools_meta=[_lp], **_base)
ck("gate3 renderer: a sentence names a tool only when THIS surface holds it",
   "**Tools.** 1." in _no_orient and "`orient" not in _no_orient
   and "`describe_app" not in _no_orient, _no_orient)

# ── gate4: the tools= site defers to the shared predicate ───────────
# Pinned structurally: importing session.py's chat path needs a live pool, and
# the failure mode is a caller quietly re-deriving the decision locally.
_sess_src = inspect.getsource(
    __import__("mastercode.runtime.session", fromlist=["x"]))
_sess_lines = _sess_src.splitlines()
_tools_line = [ln for ln in _sess_lines
               if "tools = _build_host_orchestration_tools()" in ln]
ck("gate4 the tools= build site still exists", len(_tools_line) == 1,
   repr(_tools_line))
_idx = _sess_lines.index(_tools_line[0]) if _tools_line else -1
_guard = "\n".join(_sess_lines[max(0, _idx - 3):_idx])
ck("gate4 it is guarded by the shared predicate",
   "host_orchestration_enabled(" in _guard, _guard)
ck("gate4 it does NOT re-test response_style",
   "response_style" not in _guard, _guard)

# ── gate5: the declared default is live, not dead code ──────────────
from mastercode.codebook import MastercodeBook  # noqa: E402

ck("gate5 a default for response_style is actually declared",
   "response_style" in DEFAULT_BEHAVIORAL_PARAMS, repr(DEFAULT_BEHAVIORAL_PARAMS))
_seeded = MastercodeBook._with_behavioral_defaults({"behavioral_params": None})
ck("gate5 an unseeded row receives the declared default",
   _seeded["behavioral_params"].get("response_style")
   == DEFAULT_BEHAVIORAL_PARAMS["response_style"], repr(_seeded))
_stored = MastercodeBook._with_behavioral_defaults(
    {"behavioral_params": {"response_style": "concise"}})
ck("gate5 a STORED value always wins over the default",
   _stored["behavioral_params"]["response_style"] == "concise", repr(_stored))
_asstr = MastercodeBook._with_behavioral_defaults(
    {"behavioral_params": '{"response_style": "structured", "x": 1}'})
ck("gate5 jsonb-as-str is normalized to a dict, keys preserved",
   _asstr["behavioral_params"]["response_style"] == "structured"
   and _asstr["behavioral_params"]["x"] == 1, repr(_asstr))
ck("gate5 None entity passes through untouched",
   MastercodeBook._with_behavioral_defaults(None) is None)

# gate5b: the WIRE, not the two ends. Everything above tests the helper in
# isolation, and reverting only the two `return self._with_behavioral_defaults(...)`
# lines in get_entity/get_entity_by_uuid — helper present, never applied, the
# exact dead-code shape of the defect this PR fixes — passed all of it. Same
# blind spot test_tool_inventory_grant_hint gate6 was written for: "the two ends
# were gated and the wire between them was not." get_entity is the loader behind
# every sess.entity, so it is the wire that has to be pinned.
import asyncio  # noqa: E402


class _FakePool:
    """Minimal asyncpg-pool stand-in: fetchrow returns one unseeded row."""

    def __init__(self, row):
        self._row = row

    async def fetchrow(self, *_a, **_k):
        return self._row


def _loaded(row):
    return asyncio.run(MastercodeBook(_FakePool(row)).get_entity(1))


def _style_of(entity):
    """Never raise — an unapplied default leaves behavioral_params None, and
    this gate must report a legible FAIL rather than crash the suite."""
    bp = (entity or {}).get("behavioral_params")
    return bp.get("response_style") if isinstance(bp, dict) else None


_want = DEFAULT_BEHAVIORAL_PARAMS["response_style"]
_unseeded = _loaded({"id": 128, "display_name": "Entity", "behavioral_params": None})
ck("gate5b get_entity APPLIES the default (not merely defines the helper)",
   _style_of(_unseeded) == _want, repr(_unseeded))
_kept = _loaded({"id": 1, "behavioral_params": {"response_style": "structured"}})
ck("gate5b get_entity does not clobber a stored value",
   _style_of(_kept) == "structured", repr(_kept))
ck("gate5b get_entity returns None for a missing row",
   _loaded(None) is None)
_by_uuid = asyncio.run(MastercodeBook(
    _FakePool({"id": 2, "behavioral_params": None})).get_entity_by_uuid("u"))
ck("gate5b get_entity_by_uuid applies it too",
   _style_of(_by_uuid) == _want, repr(_by_uuid))

# ── gate6: no prompt block instructs a tool call the host cannot make ─
# The old environment block told the host to "call `explore_environment` …
# `describe_app` … `open_app`" under the conversational_only gate — a
# prose-style test, not the tool gate — so an entity at or below the
# enrollment tier was handed tools=None alongside instructions to call three
# of them. That block is folded into the map; the map's renderer
# keys every call sentence off `holds_tools` (tools_meta is not None), which
# gate3 above exercises end to end. Pin that no retired block came back.
for _gone in ("environment", "domain_map", "permissions", "tool_inventory"):
    ck(f"gate6 retired block {_gone!r} is not in the schema",
       not [b for b in _ps.PROMPT_SCHEMA if b.name == _gone])
ck("gate6 the map renderer keys call sentences off holds_tools",
   "holds_tools = tools_meta is not None" in inspect.getsource(_build_map))

if fails:
    print(f"\ntool-surface one-gate: FAIL ({len(fails)}): {fails}")
    sys.exit(1)
print("\ntool-surface one-gate: all passed")
