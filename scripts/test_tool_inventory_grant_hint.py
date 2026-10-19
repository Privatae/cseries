#!/usr/bin/env python3
"""Gates: the tool inventory tells the host what a dispatch will need.

Writing `grant` requires knowing each tool's capability. Nothing the host read
at that moment carried it:

  - the prompt's "Your Tools" inventory rendered purpose and category only;
  - docs/CAPABILITIES.md uses a different taxonomy ("gate: core.entity") and
    names the seven grant capabilities ZERO times, while the dispatch-agent
    skill says to call `capabilities` for the full catalog;
  - the skill's own table is an explicit sample, not the catalog.

So the host inferred, and dispatches were refused at the door over a fact the
platform already held. Observed repeatedly, most recently `publish_site` and
`manage_site` scoped with `execute_tool` when both need `write_context`.

  gate1  the collector attaches `requires` to every tool that declares one
  gate2  the value is the wall's own — the same function unreachable_tools
         compares against, so what the host reads cannot drift from what it is
         judged by
  gate3  ROUND TRIP: granting exactly what the inventory prints makes the
         dispatch reachable. This is the gate that matters — printing a value
         that is still refused would be worse than printing nothing, because it
         sends the host round the same loop with more confidence
  gate4  the rendered prompt section actually carries it (a value collected and
         never rendered is a value the host never sees)
  gate5  tools with no declared requirement stay quiet, and the literal
         "(none)" that `describe` returns for a zero mask never reaches a prompt
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "brain"))

from mastercode.runtime.agent_grant import (  # noqa: E402
    describe, min_required_bits_for, parse_grant, required_bits_for,
    unreachable_tools)
from mastercode.runtime.prompt_builder import _build_tool_inventory  # noqa: E402
from mastercode.runtime.tools import _grant_requirement  # noqa: E402

fails = []


def ck(name, cond, extra=""):
    if cond:
        print(f"OK    {name}")
    else:
        fails.append(name)
        print(f"FAIL  {name} {extra}")


# The pair from the dispatch that actually failed, plus a spread of shapes.
#
# contacts_manage / workspace_file are load-bearing, not padding. An earlier cut
# of this file sampled only tools where min == union, so swapping
# min_required_bits_for for required_bits_for — the exact decision this hint
# rests on — passed every gate. The suite tested the plumbing of the choice and
# was blind to the choice. 22 of 117 META_TOOL_CAPS entries have min != union and they are
# the verb-named ones a host scopes to DO something; at least one must be here.
SAMPLE = ["publish_site", "manage_site", "inbox_send", "memory_search", "build",
          "contacts_manage", "workspace_file"]

# Tools whose cheapest op is not enough for their write ops.
SPLIT = [t for t in SAMPLE
         if min_required_bits_for(t) and required_bits_for(t) != min_required_bits_for(t)]

# ── gate1/gate2: present, and the wall's own value ──────────────────
ck("gate0 the sample includes a tool whose min differs from its union — "
   "without one, swapping the two sources passes every gate below",
   bool(SPLIT), SPLIT)

for t in SAMPLE:
    bits = min_required_bits_for(t)
    got = _grant_requirement(t)
    if bits:
        ck(f"gate1 {t} carries a requirement", bool(got), repr(got))
        if t in SPLIT:
            ck(f"gate2 {t} opens with the wall's own value",
               got.startswith(describe(bits) + " — "), f"{got!r} vs {describe(bits)!r}")
        else:
            # EXACT for non-split tools. `startswith` alone left the tail free,
            # so appending " — other ops need admin" to every non-split hint
            # passed the whole suite.
            ck(f"gate2 {t} is exactly the wall's value (no free tail)",
               got == describe(bits), f"{got!r} vs {describe(bits)!r}")

ck("gate2 publish_site says write_context (the tool that was refused)",
   _grant_requirement("publish_site") == "write_context",
   repr(_grant_requirement("publish_site")))
ck("gate2 manage_site says write_context",
   _grant_requirement("manage_site") == "write_context",
   repr(_grant_requirement("manage_site")))

# ── gate3: the round trip — following the hint must WORK ────────────
def _grant_from(hint, write=False):
    """The list a host would actually paste, from the rendered hint."""
    part = hint.split("other ops need")[-1] if (write and "other ops need" in hint) \
        else hint.split("—")[0]
    return [n.strip() for n in part.split(",") if n.strip()]


for t in SAMPLE:
    hint = _grant_requirement(t)
    if not hint:
        continue
    bits, unknown = parse_grant(_grant_from(hint))
    ck(f"gate3 {t}: every name in the hint is a real capability", not unknown, unknown)
    ck(f"gate3 {t}: granting exactly the hint makes it reachable",
       not unreachable_tools([t], bits), unreachable_tools([t], bits))

# The exact failed dispatch, both ways round.
_bad, _ = parse_grant(["execute_tool"])
ck("gate3 the original grant is still correctly refused",
   set(unreachable_tools(["publish_site", "manage_site"], _bad)) ==
   {"publish_site", "manage_site"},
   unreachable_tools(["publish_site", "manage_site"], _bad))
_good, _ = parse_grant(["write_context"])
ck("gate3 ...and the hinted grant is accepted for the same scope",
   not unreachable_tools(["publish_site", "manage_site"], _good),
   unreachable_tools(["publish_site", "manage_site"], _good))

# ── gate3b: THE finding — the hint must suffice for the WRITE op ─────
# gate3 above can never fail on the min-vs-union axis: unreachable_tools uses
# min, and union superset min, so any source between them round-trips green. The
# question that actually bit is whether a host following the hint gets DENIED
# MID-RUN — which is checked against the per-op resolver, not the wall.
for t in SPLIT:
    hint = _grant_requirement(t)
    ck(f"gate3b {t}: the hint names the write requirement, not just the cheapest",
       "other ops need" in hint, hint)
    write_bits, _ = parse_grant(_grant_from(hint, write=True))
    need = required_bits_for(t)          # union: enough for every op
    # EXACT equality, not superset. A superset check is a lower bound only and
    # cannot see over-granting: `describe(full | admin)` — a hint telling every
    # host to grant admin — passed this gate before the `==`.
    ck(f"gate3b {t}: the full half is EXACTLY what every op needs — covers the "
       f"write path without over-granting",
       write_bits == need,
       f"hint={hint!r} grants={describe(write_bits)} needs={describe(need)}")
    # ...and the cheap half must still be exactly the wall's value, so the
    # read-only decomposition READ_SAFE_OPS exists to enable stays discoverable.
    cheap_bits, _ = parse_grant(_grant_from(hint))
    ck(f"gate3b {t}: the cheap half still equals the wall's min",
       cheap_bits == min_required_bits_for(t),
       f"{describe(cheap_bits)} vs {describe(min_required_bits_for(t))}")

# ── gate3c: the REAL per-op check the comment above used to claim ───
# gate3b compares union-vs-union: required_bits_for(t) with no args never
# reaches _op_bits (agent_grant returns None on falsy args). That leans on an
# ungated invariant — that every READ_SAFE_OPS value is inside its tool's union
# — held today by a separate hand-maintained table. Resolve real ops instead.
from mastercode.runtime.meta_tool_caps import READ_SAFE_OPS  # noqa: E402

_OP_PARAM = {"contacts_manage": "op", "workspace_file": "op"}
for t in SPLIT:
    ops = READ_SAFE_OPS.get(t) or {}
    if not ops or t not in _OP_PARAM:
        continue
    hint = _grant_requirement(t)
    cheap, _ = parse_grant(_grant_from(hint))
    full, _ = parse_grant(_grant_from(hint, write=True))
    for op in sorted(ops):
        need = required_bits_for(t, {_OP_PARAM[t]: op})
        ck(f"gate3c {t}.{op} (read-safe) is covered by the CHEAP half",
           need is not None and not (need & ~cheap),
           f"{op}: needs {describe(need)}, cheap half grants {describe(cheap)}")
    # ...and an op that is NOT read-safe must be covered by the full half.
    other = next((o for o in ("create", "write", "delete", "set", "update")
                  if o not in ops), None)
    if other:
        need = required_bits_for(t, {_OP_PARAM[t]: other})
        ck(f"gate3c {t}.{other} (not read-safe) is covered by the FULL half",
           need is not None and not (need & ~full),
           f"{other}: needs {describe(need)}, full half grants {describe(full)}")

# ── gate4: it reaches the rendered prompt — BOTH render paths ───────
# _build_tool_inventory renders twice: once for categories in
# _CATEGORY_ORDER, and once in a fallback loop for categories that are not.
# An earlier cut of this gate used only "apps"/"memory" — neither is in
# _CATEGORY_ORDER — so it exercised the fallback alone, and neutering the
# ORDERED path survived the suite. Cover one of each.
ORDERED_CAT = "system"        # in _CATEGORY_ORDER
UNORDERED_CAT = "apps"        # not in it -> fallback loop
rendered = _build_tool_inventory([
    {"name": "ordered_tool", "category": ORDERED_CAT, "priority": 1,
     "purpose": "Ordered-category tool.", "requires": "execute_tool"},
    {"name": "publish_site", "category": UNORDERED_CAT, "priority": 1,
     "purpose": "Publish a website.", "requires": "write_context"},
    {"name": "memory_search", "category": UNORDERED_CAT, "priority": 1,
     "purpose": "Search memory.", "requires": "read_memory"},
    {"name": "plain_tool", "category": UNORDERED_CAT, "priority": 2,
     "purpose": "No declared requirement.", "requires": None},
])
ck("gate4 the ORDERED render path carries the hint too",
   "**ordered_tool** _(dispatch grant: execute_tool)_" in rendered, rendered)
ck("gate4 the rendered inventory carries the requirement",
   "write_context" in rendered and "read_memory" in rendered, rendered)
# publish_site moved into the no-model lane in the 2026-09-19 operate sweep, so
# its line carries BOTH marks now. That is the composition working: `no model`
# is how the HOST reaches it (operate, in its own turn), `dispatch grant` is
# what an agent would need if it ever rides in a scope beside a tool that does
# need a model. Asserted whole, so neither mark can quietly drop the other.
ck("gate4 it is attached to the right tool",
   "**publish_site** _(no model · dispatch grant: write_context)_" in rendered,
   rendered)

# ── gate5: quiet when there is nothing to say ───────────────────────
ck("gate5 a tool with no requirement renders no grant hint",
   "**plain_tool** — " in rendered, rendered)
ck("gate5 the literal '(none)' never reaches a prompt",
   "(none)" not in rendered, rendered)
ck("gate5 _grant_requirement returns None for an unknown tool",
   _grant_requirement("__definitely_not_a_tool__") is None,
   repr(_grant_requirement("__definitely_not_a_tool__")))

# gate5b: a tool declaring ZERO required bits. No live tool does today (checked:
# no META_TOOL_CAPS entry has required_bits 0), which is why the earlier version
# of gate5 only exercised the unknown-tool path and a hand-built render list —
# so `if bits is None:` instead of `if not bits:` passed the whole suite, and a
# line reading "_(dispatch grant: (none))_" would have shipped. describe(0)
# returns the literal "(none)"; it must never become a hint.
from mastercode.runtime import meta_tool_caps as _mtc  # noqa: E402

_saved_caps = dict(getattr(_mtc, "META_TOOL_CAPS", {}))
try:
    _mtc.META_TOOL_CAPS["__zero_bit_probe__"] = {"required_bits": 0}
    ck("gate5b a zero-bit tool yields no hint, never the literal '(none)'",
       _grant_requirement("__zero_bit_probe__") is None,
       repr(_grant_requirement("__zero_bit_probe__")))
finally:
    _mtc.META_TOOL_CAPS.clear()
    _mtc.META_TOOL_CAPS.update(_saved_caps)

# ── gate6: the collector must actually ATTACH it ────────────────────
# Everything above tests _grant_requirement and the renderer. Setting the
# collector's dict entry to a constant None survived all of it — the two ends
# were gated and the wire between them was not. _collect_authorized_tool_meta
# needs a live pool, so this is pinned structurally, at the one line that
# matters.
import inspect  # noqa: E402

from mastercode.runtime import tools as _tools_mod  # noqa: E402

_collector_src = inspect.getsource(_tools_mod._collect_authorized_tool_meta)
ck("gate6 the collector attaches the wall-derived value, not a constant",
   '"requires": _grant_requirement(fn)' in _collector_src,
   _collector_src[-400:])

if fails:
    print(f"\ntool-inventory grant-hint gates: FAIL ({len(fails)}): {fails}")
    sys.exit(1)
print("\ntool-inventory grant-hint gates: all passed")
