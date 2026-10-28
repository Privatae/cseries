#!/usr/bin/env python3
# gate-scope: whole-tree — every schema'd tool against docs/CAPABILITIES.md
"""Invariant: no schema'd, reachable tool is missing from the closed world.

docs/CAPABILITIES.md (and its machine twin docs/capabilities.json) is the
platform's CLOSED WORLD: the host is told that a capability absent from it does
not exist, and to answer "no — propose it" instead. That contract only holds if
the doc actually enumerates everything the host can be offered.

THE DEFECT THIS EXISTS FOR.
scripts/generate_capabilities.py iterates PUBLIC_TOOL_SCHEMAS and nothing else.
There are FOUR schema registries — scripts/check_tool_lanes.py:58 already
iterates all four — plus the host's five PERMANENT orchestration verbs, whose
schemas are built in brain/mastercode/runtime/session.py:239
(_build_host_orchestration_tools) and advertised on every conversational turn
(session.py:2867), with `escalate` appended outside every catalog gate in
runtime/tools.py:281-284. Result: dispatch, operate, recall, agent_status and
escalate were offered to every host on every turn and appeared NOWHERE in the
doc — while the doc's own §7 cited `escalate` as the way to reach /api/tickets
and §3 said it did not exist. Nothing asserted that the doc covered the tool
surface, so the next tool added to a non-public registry repeats it silently.

THE INVARIANT. Every tool that carries a schema in any of the four registries,
plus every permanent orchestration verb, and that is not retired, is EITHER
  (a) enumerated in capabilities.json `host_ops`, OR
  (b) listed in OUT_OF_SCOPE below with a reason that STILL HOLDS in the code.
A tool in neither fails. An OUT_OF_SCOPE id whose schema is gone fails (the list
cannot rot). An OUT_OF_SCOPE id whose stated reason no longer matches the
runtime's own gates fails (the reason cannot rot either) — a bridge tool
promoted into PUBLIC_TOOL_SCHEMAS stops being excusable the moment it is
promoted, which is exactly when it has to appear in the doc.

REACHABILITY IS DERIVED, NEVER RE-IMPLEMENTED. The gates below are read from
the same code that enforces them at runtime: the registry a schema lives in and
its dict KEY (runtime/tools.py::_build_tool_schemas — public is catalog-public,
_BRIDGE_* needs infra.codebase=admin, OPERATOR_PLATFORM_* and the `_meta_*` bot
keys need entity_id == OPERATOR_ENTITY_ID, `_meta_escalate` is appended for
everyone, and the public/bridge loops skip `_meta_`-prefixed keys outright),
runtime/tools.py:89 _surface_allows for `_meta.surfaces`, and
runtime/tool_lanes.py:67 RETIRED_TOOLS for retirement. Re-implementing those
predicates is how the doc's §7 lane vocabulary drifted from tool_lanes.lane_for
in the first place.

TWO LANES.
  in-image (the ship gate; the generated doc exists):
      docker exec <c> python3 /app/scripts/check_capability_coverage.py
      Full strength — the documented set is the REAL generated host_ops.
  against a tree (the pre-merge lane; docs/capabilities.json is generated at
  image build and never committed, so it is absent):
      APP_ROOT=<repo> python3 scripts/check_capability_coverage.py
      Structural half — classification, plus both halves of the OUT_OF_SCOPE
      rot check. It CANNOT compare against the doc, and says so on every run.
The image lane never degrades into the tree lane: with APP_ROOT unset (i.e.
/app, the in-image default) a missing capabilities.json is a FAILURE, not a
skip. A ratchet that skips when its floor is absent is not a ratchet.
"""
import json
import os
import sys

APP_ROOT = os.environ.get("APP_ROOT", "/app")
DOCS = os.environ.get("CSERIES_DOCS", os.path.join(APP_ROOT, "docs"))
sys.path.insert(0, APP_ROOT)
sys.path.insert(0, os.path.join(APP_ROOT, "brain"))

from mastercode.public_tool_registry import (  # noqa: E402
    PUBLIC_TOOL_SCHEMAS, OPERATOR_PLATFORM_TOOL_SCHEMAS,
)
from mastercode.runtime.triage import _BOT_TOOL_SCHEMAS, _BRIDGE_TOOL_SCHEMAS  # noqa: E402
from mastercode.runtime.tool_lanes import RETIRED_TOOLS  # noqa: E402
from mastercode.runtime.tools import _surface_allows  # noqa: E402
from mastercode.runtime.session import _build_host_orchestration_tools  # noqa: E402

# ── the out-of-scope table ────────────────────────────────────────────────
# id → (reason code, why it is NOT in the closed world). The code is checked
# against the runtime's own gates on every run, so the prose can never quietly
# outlive the fact. Three codes, each a real gate in runtime/tools.py:
#   operator_only  entity_id == OPERATOR_ENTITY_ID (tools.py:255-267) — an
#                  operator surface. No user's host is ever offered it, so
#                  putting it in the users' closed world would advertise a
#                  capability they cannot have.
#   bridge_admin   infra.codebase=admin (tools.py:191-204) — the bridge
#                  primitives. Today only the operator Host and the
#                  security Host qualify, and the bridge re-gates every op platform-side.
#   no_surface     `_meta.surfaces: []` (tools.py:89-107 _surface_allows) —
#                  exposed on no chat surface at all.
# Adding an entry is a deliberate, reviewed act: it says "the host may be
# offered this and the doc will not mention it". Prefer documenting the tool.
OUT_OF_SCOPE: dict[str, tuple[str, str]] = {
    # ── operator-only platform ops (OPERATOR_PLATFORM_TOOL_SCHEMAS) ────────
    # (mgmt_run and mesh_harness_run are not listed: every Host has them.)
    "lab_run": ("operator_only", "operator lab-harness execution (mode=lab on the mesh_harness lane); operator Host only until the lab pairing token ships"),
    "bug_review": ("operator_only", "operator door onto the platform's bug cache (validator verdicts, validated reads, fix marks; skill bug-review); operator Host only"),
    "support_collect": ("operator_only", "operator collect of the platform's support queue into its own Tickets pane; operator Host only"),
    # ── private bot meta-tools (_BOT_TOOL_SCHEMAS, `_meta_*` keys) ───────
    # These also carry surfaces: [] — internal turn state, on no chat surface.
    "state_load": ("operator_only", "internal bot turn-state read; operator Host only, surfaces: []"),
    "state_store": ("operator_only", "internal bot turn-state write; operator Host only, surfaces: []"),
    # ── bridge primitives (_BRIDGE_TOOL_SCHEMAS) ─────────────────────────
    # Platform-side operations brokered over the
    # DMZ bridge, behind infra.codebase=admin AND per-op allowlists.
    # (inbox_send and calendar_log are not listed: they are every
    # Host's own channels and now public — see public_tool_registry.py.)
    "code_grep": ("bridge_admin", "bridge: grep the platform codebase"),
    "code_read": ("bridge_admin", "bridge: read a platform source file"),
    "code_tree": ("bridge_admin", "bridge: list the platform source tree"),
    "k8s_rollout_status": ("bridge_admin", "bridge: read a fleet rollout"),
}
REASON_CODES = frozenset({"operator_only", "bridge_admin", "no_surface"})

# ── collect every schema'd tool, with the catalog gate it sits behind ─────
ESCALATE_KEY = "_meta_escalate"


def _catalog_gate(label: str, key: str) -> str | None:
    """The gate _build_tool_schemas applies to this (registry, key) pair.

    "" = admitted to every entity's catalog; a reason code = admitted only
    behind that gate; None = no catalog loop admits this key at all.
    """
    if label in ("public", "bridge"):
        if key.startswith("_meta_"):
            return None        # both loops `continue` on a _meta_-prefixed key
        return "" if label == "public" else "bridge_admin"
    if label == "operator":
        return "operator_only"
    if label == "bot":
        if key == ESCALATE_KEY:
            return ""          # appended for every entity, outside every gate
        if key.startswith("_meta_"):
            return "operator_only"
        return None            # the filename-keyed bot loop is gone
    return None


tools: dict[str, dict] = {}
for label, src in (("public", PUBLIC_TOOL_SCHEMAS),
                   ("operator", OPERATOR_PLATFORM_TOOL_SCHEMAS),
                   ("bot", _BOT_TOOL_SCHEMAS),
                   ("bridge", _BRIDGE_TOOL_SCHEMAS)):
    for key, schema in src.items():
        name = ((schema or {}).get("function") or {}).get("name")
        if not name:
            continue
        t = tools.setdefault(name, {"schemas": [], "gates": [], "sources": set()})
        t["schemas"].append(schema)
        t["gates"].append(_catalog_gate(label, key))
        t["sources"].add(label)

# The five permanent orchestration verbs, taken from the runtime builder
# itself so this can never lag session.py: whatever the host is advertised on
# a conversational turn is what this gate demands the doc account for.
for schema in _build_host_orchestration_tools():
    name = ((schema or {}).get("function") or {}).get("name")
    if not name:
        continue
    t = tools.setdefault(name, {"schemas": [], "gates": [], "sources": set()})
    t["schemas"].append(schema)
    t["gates"].append("")      # advertised on every turn, behind no gate
    t["sources"].add("orchestration")

# The surface universe: every surface any schema names, plus the two chat
# surfaces every host has. Derived, so a new surface is covered on arrival.
SURFACES = sorted({s for t in tools.values() for sc in t["schemas"]
                   for s in ((sc.get("_meta") or {}).get("surfaces") or [])}
                  | {"circles", "workbench"})

fails: list[str] = []
universal: set[str] = set()          # offered to a user's host → must be documented
excusable: dict[str, set[str]] = {}  # name → the reason codes that actually apply
unreachable: list[str] = []

for name, t in sorted(tools.items()):
    if name in RETIRED_TOOLS:
        continue                     # retired: check_tool_lanes.py owns these
    gates = t["gates"]
    if all(g is None for g in gates):
        unreachable.append(name)
        continue
    reasons = {g for g in gates if g}          # the gates on the paths that exist
    if "" in gates:
        reasons = set()                        # at least one ungated path
    if not any(_surface_allows(sc, s) for sc in t["schemas"] for s in SURFACES):
        reasons.add("no_surface")              # exposed on no chat surface
    if reasons:
        excusable[name] = reasons
    else:
        universal.add(name)

if unreachable:
    fails.append(
        "schema'd but no catalog loop admits them (visible to nobody — delete "
        f"the schema or register it): {sorted(unreachable)}")

# ── 1. the out-of-scope list cannot rot ───────────────────────────────────
stale = sorted(n for n in OUT_OF_SCOPE if n not in tools)
if stale:
    fails.append(
        "OUT_OF_SCOPE lists ids that carry no schema in any registry — the tool "
        f"is gone, the excuse goes with it: {stale}")
bad_code = sorted(f"{n} ({OUT_OF_SCOPE[n][0]})" for n in OUT_OF_SCOPE
                  if OUT_OF_SCOPE[n][0] not in REASON_CODES)
if bad_code:
    fails.append(f"OUT_OF_SCOPE reason codes not in {sorted(REASON_CODES)}: {bad_code}")
no_prose = sorted(n for n, (_c, why) in OUT_OF_SCOPE.items() if not (why or "").strip())
if no_prose:
    fails.append(f"OUT_OF_SCOPE entries with no stated reason: {no_prose}")

# ── 2. a stated reason must still hold in the code ────────────────────────
wrong_reason = []
for name, (code, _why) in sorted(OUT_OF_SCOPE.items()):
    if name in stale or code not in REASON_CODES:
        continue
    applies = excusable.get(name)
    if applies is None:
        wrong_reason.append(
            f"{name}: listed '{code}', but it is offered to EVERY host now "
            f"(sources={sorted(tools[name]['sources'])}) — document it")
    elif code not in applies:
        wrong_reason.append(
            f"{name}: listed '{code}', but the gates in force are {sorted(applies)}")
if wrong_reason:
    fails.append("OUT_OF_SCOPE reasons that no longer match the code: "
                 + " | ".join(wrong_reason))

# ── 3. every gated tool is on the list ────────────────────────────────────
undeclared = sorted(n for n in excusable if n not in OUT_OF_SCOPE)
if undeclared:
    fails.append(
        "reachable tools that are neither documented nor declared out of scope — "
        "add them to capabilities.json host_ops (generate_capabilities.py), or to "
        f"OUT_OF_SCOPE here with a reason: {undeclared}")

# ── 4. every universally-offered tool is in the doc ───────────────────────
caps_path = os.path.join(DOCS, "capabilities.json")
documented: set[str] | None = None
if os.path.exists(caps_path):
    try:
        caps = json.load(open(caps_path, encoding="utf-8"))
    except Exception as e:
        print(f"FAIL: capabilities.json unreadable: {e}")
        sys.exit(1)
    documented = {op.get("name") for op in (caps.get("host_ops") or []) if op.get("name")}
    if not documented:
        fails.append("capabilities.json carries no host_ops — the closed world is empty")
    missing = sorted(universal - documented)
    if missing:
        fails.append(
            "offered to every host on every turn, absent from capabilities.json "
            f"host_ops (the doc tells the host these do not exist): {missing}")
    contradicted = sorted(set(OUT_OF_SCOPE) & documented)
    if contradicted:
        fails.append(
            "documented AND declared out of scope — the doc already covers them; "
            f"drop the OUT_OF_SCOPE entry: {contradicted}")
elif APP_ROOT == "/app":
    # The image lane. The doc is generated at image build (Dockerfile.linux)
    # and comparing against it is this gate's whole point.
    fails.append(
        f"{caps_path} is missing in-image — the generated closed world did not "
        "ship, so nothing can be checked against it. Not a skip: a ratchet that "
        "skips when its floor is absent is not a ratchet.")

if fails:
    for f in fails:
        print(f"FAIL: {f}")
    print("      the doc is generated by scripts/generate_capabilities.py; a tool "
          "the host is offered belongs in host_ops, or in OUT_OF_SCOPE here")
    sys.exit(1)

print(f"OK: capability coverage — {len(tools)} schema'd/advertised tools; "
      f"{len(universal)} offered to every host, "
      + (f"all present in capabilities.json host_ops ({len(documented)} ops)"
         if documented is not None else
         "doc comparison NOT RUN (docs/capabilities.json is generated at image "
         "build and is absent from a source tree — the image lane runs it)")
      + f"; {len(OUT_OF_SCOPE)} declared out of scope, every reason still in force")
