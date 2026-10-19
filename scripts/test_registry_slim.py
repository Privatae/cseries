#!/usr/bin/env python3
"""Ratchet: the tool registry stays SLIM ("descriptions → skills").

A tool description says what the tool does and what its ops/arguments are, in
one short paragraph. Doctrine — how to do it well, when to ask, what never to
do, worked examples, measured incidents — lives in a SKILL the host reads on
demand. Measured before this ratchet (one Host): 87 tool schemas
on the wire = 186,560 chars per model call, ~45 % of every turn.

  PYTHONPATH=brain python3 scripts/test_registry_slim.py
"""
import json
import os
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, os.environ.get("CSERIES_BRAIN", str(REPO / "brain")))

from mastercode.public_tool_registry import (  # noqa: E402
    OPERATOR_PLATFORM_TOOL_SCHEMAS, PUBLIC_TOOL_SCHEMAS,
)

DESC_MAX = 600
PARAM_MAX = 160
PURPOSE_MAX = 160
# What the model is actually sent: every schema with `_meta` stripped (that is
# what _build_tool_schemas hands the tool loop). 217,782 on the wire before
# descriptions moved to skills; ~145,000 after. The structure alone (types, enums, names,
# required lists) is ~57 KB, so text trimming bottoms out near here — the
# rest of the reduction is schemas on demand, not more trimming.
# This ratchet only stops regrowth.
#
# Raised 150,000 -> 153,000, and the reason matters because a
# ceiling you lift when you hit it is not a ceiling. What landed was not
# regrowth — no existing description grew — it was two NEW capabilities on the
# same night: manage_pairing (1,245 chars, the anchor host admitting or
# refusing a host onto a machine it anchored) and session (1,242, a terminal on
# a granted box). Each branch measured green alone against 150,000 and their
# sum did not, which is the shape to watch for: the gate runs per branch and
# the wire is global.
#
# Measured: 149,532 before either, 150,777 with pairing, 151,801 with both.
# 153,000 leaves room for roughly one more tool, deliberately — the next one
# should have to argue for itself.
#
# What this does NOT license: text creeping back into descriptions. Every other
# check in this file still holds each one to 600 chars, and the honest fix for
# the next overrun is the on-demand schema lane (schemas fetched when a turn needs them),
# not a bigger number here.
#
# Raised 153,000 -> 156,500, for reach-out (how the host gets its
# person's attention when work finishes while they are away) — and the argument
# the note above asked the next tool to make is below, with the measurements,
# because the trim the note recommended INSTEAD no longer exists.
#
#   The room 153,000 was said to leave is effectively gone: main measures
#   152,566 across 118 tools, 434 chars under. The ceiling was already binding
#   on any new tool, whoever added it.
#
#   The recommended alternative was checked before raising, and it is stale.
#   The three schemas named above were trimmed in the meantime and now measure
#   tickets 4,060 / dcn 3,961 / build 3,862 — about 1,270 chars already
#   reclaimed and spent. Their descriptions are 597, 600 and 595 against the
#   600 cap, so there is no long text left in them: what remains is parameter
#   descriptions (1,740 / 1,463 / 981, each already under 160) and the
#   structure itself (1,723 / 1,898 / 2,286 — types, enums, names). The "worth
#   ~3,000 chars" is not available, and cutting parameter descriptions is
#   cutting what the model needs to call the tool correctly.
#
#   Reach-out costs 3,594 across TWO schemas — reachout 1,869 and
#   reachout_channel 1,725 — measured. 152,566 + 3,594 = 156,160, so 156,500
#   leaves 340 chars: deliberately not room for another tool.
#
#   WHY TWO SCHEMAS AND NOT ONE, since the second is what pushed this over.
#   Building a channel binds a sealed credential to an outbound destination,
#   and everything downstream of creation is designed to run with nobody
#   present. That has to be out of the deterministic and unattended lanes, and
#   a handler cannot tell which lane invoked it (run_direct_op and the chat
#   path both reach _execute_meta_tool with interaction_context=None), so the
#   only structural split available is a second NAME. ~1,700 chars on the wire
#   is what that wall costs, and it is the right trade: the alternative was a
#   scheduled task that mails one of the user's keys to an arbitrary host.
#
#   Both descriptions are under 600 and every parameter under 160, and the
#   doctrine both would otherwise carry is in default-skills/comms.md.
#
# Raised 156,500 -> 157,500, for two branches that each measured
# green against 156,500 in isolation and only collided on merge — the exact
# shape the first note above warned about ("the gate runs per branch and
# the wire is global"). Measured: 156,494 (6 chars of the 340
# already spent by other tools landed since the last raise), +317 for
# oversight_ingest (new, already AT THE SHORT END — no parameters, a single
# sentence pointing at skill 'network') and +306 for propose_capability's new
# `done_looks_like` field (server/proposals/{authoring,checks,intake}.py now
# read it — a real field, not trimmable text). 156,494 + 317 + 306 = 157,117,
# 617 over. Neither addition has slack: oversight_ingest was already cut
# down to nothing removable, and cutting done_looks_like's parameter
# description would cut what the model needs to fill in a field the backend
# requires. 157,500 leaves 383 chars: deliberately not room for another tool.
#
# The honest fix for the NEXT overrun is still the on-demand schema lane (schemas
# fetched when a turn needs them), not a bigger number here.
#
# Raised 157,500 -> 158,500 for ONE new capability arguing for
# itself: model_bench, the text-role admission bench (
# "validated" comes back for text-role models, earned only by this bench; a
# platform safety gate). Measured: 157,477 at 121 tools before it; the schema
# was cut to three ops and two parameters, 810 chars on the wire, and lands
# at 158,287 — the description names the rule and the skill, nothing more.
# 158,500 leaves 213 chars: deliberately not room for another tool, and the
# on-demand schema lane is still the answer to the next one.
#
# Raised 158,500 -> 159,500, and this one is the collision the
# first note predicted rather than a new capability arguing for itself.
# Measured: main (after the operate sweep) sits at 158,371, so 158
# of the 213 were already spent and 129 remained. The desktop branch adds ONE
# optional parameter, `artifact.kind` (enum artifact|desktop) — 201 chars with
# a 116-char description — and lands at 158,572, 72 over. Both branches were
# green against 158,500 in isolation; neither is trimmable into 129, because
# the property's JSON scaffolding alone is 85 and a description short enough
# to fit would have to avoid naming either enum value to pass the enum-gap
# check, i.e. say nothing.
#
# 159,500 leaves 928 chars. That number is NOT the point: this is the third
# raise in two days (156,500 -> 157,500 -> 158,500 -> 159,500), every one for
# a real capability, and the cap is now doing nothing except recording how
# fast the wire is growing. The on-demand schema lane — schemas fetched when a turn
# needs them — was the answer at the last two raises and is overdue at this
# one. Raise it a fourth time only alongside a decision to build that.
#
# SPLIT — which is a RAISE for the operator Host and for nobody else, and
# should be read as one. The number above was measured over BOTH registries —
# the public one every Host pays for on every turn, and
# OPERATOR_PLATFORM_TOOL_SCHEMAS, which _build_tool_schemas hands to the operator
# Host alone. A user's Host never carries an operator tool, so an operator tool
# never costs a user's turn. Measured: public 152,579 across 116
# tools, operator-only 6,912 across 5 (mgmt_run, session, mesh_harness_run,
# lab_run, compute_job) = 159,491. (session and compute_job have since
# gone — a Host holds no shell on a machine; three operator-only remain.)
#
#   WIRE_MAX is now the PUBLIC wire alone and keeps exactly the headroom the
#   combined cap had (159,500 - 6,912 = 152,588, 9 chars): nothing a user's
#   Host pays for got looser, and the on-demand schema argument above stands unchanged
#   for it.
#   OPERATOR_WIRE_MAX caps the operator-only set on its own. bug_review (the
#   bug cache's operator door) measures 1,713 and joins that set
#   because it IS an operator capability — the platform refuses it for any
#   other entity — so 6,912 + 1,713 = 8,625. 8,750 leaves 125 chars:
#   deliberately not room for another operator tool without arguing for it
#   here, the same discipline as above.
#   Stated plainly: the operator Host's own per-turn tool list is now 161,204, over
#   the old combined cap of 159,500 — a fourth raise, for one Host, taken
#   without the on-demand schema decision the note above asks for. The case: it is
#   ONE Host, the platform's operator, and the alternative was an operator
#   capability every user's Host would carry. If that trade is wrong, the
#   fix is the on-demand schema lane, not a smaller bug_review.
#   support_collect
#   measures 544 and joins the set on the same argument: it is the operator
#   Host collecting the platform's support queue into its own Tickets pane —
#   the platform refuses it for every other entity — and it replaces a timer
#   on a management box, which is the direction PRIORITY 1 points (the
#   platform managed from inside itself). One op, one enum value; the
#   standing task is the caller that matters and the schema exists so the
#   host can say "look now". 8,625 + 544 = 9,169; 9,250 leaves 81 chars —
#   again deliberately not room for another operator tool without arguing
#   for it here.
#   turn_type (the Host drafts a turn type
#   and only the person's click makes it live) measures 693 on the public
#   wire; the cap was 678 short. The raise was chosen over
#   trimming other tools' descriptions or holding turn_type for on-demand tool
#   loading, which stays queued as its own project. 152,588 + 678 = 153,266:
#   the new cap is the measured wire, with no headroom left, so the next
#   public tool still has to argue for itself here.
#   close_turn (the Host's
#   structured end of turn, the signal the Host's later machinery reads:
#   turn types written by the Host, the handoff, preferences and the thread
#   goal taken over from the consolidator) measures 1,817 on the public wire
#   (main 153,231 after memory_scope -> 155,048) with its field descriptions
#   cut to what the static prompt does not already say and no
#   additionalProperties (normalize drops unknown fields). No other schema was
#   trimmed to pay for it. Raised as one decision for this
#   tool, as turn_type's was; the next one that does not fit is argued again. The cap is the measured wire with no headroom.
#   mgmt_run + mesh_harness_run MOVE from the operator set to the
#   public one: they are no longer an operator capability, because people need
#   to be able to manage their own servers. Every Host could already propose a management grant and the user
#   approve it, with no tool to use it. Measured: main 154,640 public (408
#   under 155,048) and 8,792 operator; this branch 159,155 public and 4,235
#   operator. mgmt_run 1,168, mesh_harness_run 3,347 (its description now
#   leads with mode='service', the one mode a Host other than the operator
#   may use). So the public cap rises by exactly the 4,515 that moved and the
#   operator cap falls by the 4,557 that left it; both keep the headroom they
#   had, and the operator Host's own per-turn list is unchanged apart from that
#   42-char description cut. Not trimmed further: the schema's agent-only
#   parameters (prompt, repo, branch, worktree, worktree_b64, command, port,
#   max_minutes) are 1,101 chars every other Host now carries and cannot use,
#   and dropping them from its copy needs a per-Host schema variant, which is
#   the on-demand schema lane's job. Merging this is the decision to raise.
#   inbox_send + calendar_log MOVE from triage._BRIDGE_TOOL_SCHEMAS
#   to the public set, measured 473 + 484 = 957 (main 159,557 -> 160,514). They
#   were already on every live Host's per-turn wire, uncounted here: every pod
#   granted itself infra.codebase=admin, and that grant is what listed the
#   bridge registry. The pod no longer grants it (it put the platform's source,
#   image and service tools in every Host's list), and these two are every
#   Host's own channels — a monitor's way to tell its user anything, and its own
#   timeline — so they move to the registry every Host is handed. The cap rises
#   by exactly what moved and keeps the 6 chars of headroom it had; a new Host's
#   real per-turn wire FALLS, since it no longer carries the ten platform tools.
#   Not trimmed: both schemas are already one-line descriptions and bare
#   parameters. Merging this is the decision to raise.
#   Messaging connectors: propose_nac_policy gains kind='connector' with app_id, platform,
#   token_name and app_token_name, and revoke_nac_rule's kind enum gains
#   'connector'. Measured: main 160,514 -> this branch 160,877, +363. Trimmed
#   to that before asking: the new fields carry no descriptions beyond the two
#   Keys names, the guidance is in skill 'network', and propose_nac_policy's
#   required list (now per kind, checked by the executor) is gone. No other
#   schema was trimmed to pay for it. The cap rises by exactly the 363 and keeps
#   the 6 chars of headroom it had. Merging this is the decision to raise.
#   The asset catalog's tool is REMOVED with the asset catalog it browsed
#   (migrations/v206). Its schema measured
#   1,589. Measured: main 160,860 -> this branch 159,271 public, operator
#   unchanged at 4,235. The cap FALLS to the measured wire, 159,271, with no
#   headroom: what this removal freed is not banked for the next tool, which
#   still has to argue for itself here.
#   The task record: one new tool, `task_record`, the record every thread of a task
#   shares. It replaces re-reading helpers' summaries with findings whose
#   quotes code checks against the source, which is the point of the
#   multi-threaded Host. Measured: main 159,268 -> this branch 161,010,
#   +1,742. Trimmed before asking: one-line parameter descriptions, the
#   guidance in skill 'research', no examples. The cap rises to the measured
#   wire with no headroom. Merging this is the decision to raise.
#   The serial run: ops run/stop/report and `request`
#   on the same tool, no new tool. Measured 161,010 -> 161,160, +150.
#   Cited facts: op promote, `findings`, `everywhere`
#   on the same tool. Measured 161,160 -> 161,392, +232.
#   research/web_search's text was shortened:
#   161,392 -> 161,376, -16.
#   research saves sources whole: ops search/find/read/list and `urls`, `source`,
#   `offset` on the same tool, no new tool; its description now says what it
#   does differently from web_search. Measured 161,376 -> 161,901, +525.
#   Parallel task runs:
#   task_record's `threads` on run, and its description says a run can go
#   `threads` at once, as many as the Host's memory holds. Measured
#   161,900 -> 162,056, +156.
#   `data`, the Host's own database: one new tool, its text cut to 789 chars before
#   measuring. Measured 162,056 -> 162,735, +679. Raised rather than trimmed
#   from other tools: search mode is
#   the only mode and groups mode is gone (runtime/tool_core.py:35), so a
#   public schema reaches a call only when find_tools returns it. What every
#   call carries is CORE_MAX below, and `data` does not change it.
#   memory_curate op='alias': one enum word and one
#   sentence on the same tool. Measured 162,735 -> 162,793, +58.
WIRE_MAX = 162_793
OPERATOR_WIRE_MAX = 4_693
# CORE_MAX: what a
# search-mode Host sends on every call, per surface — the six verbs in search
# wording, find_tools and read, the build doors the surface holds, and the
# runtime/tool_core.CORE_EXTRAS most-used tools. The extras differ per Host,
# so the ceiling is the worst any Host can send: the largest schemas a native
# call can run (a held read or a read-only variant), the operator Host's
# platform tools included. Measured with no headroom; the fixed part alone is
# 29,395 / 23,832 / 18,391. Moving prose to skills is what lowers it.
# WIRE_MAX stays while groups mode does: a failed prefire pick still sends
# every held schema there. It goes when group loading is deleted.
# "other" is any surface that holds no build door (an app's own chat); it was
# measured as the VS Code editor's surface until the editor and its `editor`
# tool were removed, and lowered to the measured 33,602 then.
CORE_MAX = {"workbench": 48_107, "circles": 42_544, "other": 33_602}

fails = 0


def check(name, ok, detail=""):
    global fails
    print(("  ok    " if ok else "  FAIL  ") + name + (f"  — {detail}" if detail and not ok else ""))
    if not ok:
        fails += 1


allt = {**PUBLIC_TOOL_SCHEMAS, **OPERATOR_PLATFORM_TOOL_SCHEMAS}
skills_dir = REPO / "default-skills"
wire = 0
operator_wire = 0
long_desc, long_param, long_purpose, shouting, bad_skill = [], [], [], [], []
truncated, enum_gap = [], []

# A description that was cut mid-sentence to fit a cap ends on a joiner. The
# wire carries the dangling clause and the model never sees the rest, so the
# text is worse than the long version it replaced. 34 of these shipped in the
# first pass of the slimming.
TRUNC_TAIL = (";", ",", " —")


def ends_truncated(text):
    return bool(text) and text.rstrip().endswith(TRUNC_TAIL)


def enum_values(pv):
    """The enum on a parameter, or on the items of an array parameter."""
    return pv.get("enum") or ((pv.get("items") or {}).get("enum")) or []


def walk_params(name, props, prefix=""):
    """Yield (dotted_name, schema) for every parameter, including the ones
    nested under an array's items.properties — those are on the wire too and
    were never capped before this ratchet."""
    for pn, pv in (props or {}).items():
        pv = pv or {}
        yield f"{name}.{prefix}{pn}", pv
        nested = (pv.get("items") or {}).get("properties")
        if nested:
            yield from walk_params(name, nested, f"{prefix}{pn}[].")


for name, s in sorted(allt.items()):
    fn = s.get("function") or {}
    meta = s.get("_meta") or {}
    _size = len(json.dumps({k: v for k, v in s.items() if k != "_meta"}))
    if name in OPERATOR_PLATFORM_TOOL_SCHEMAS and name not in PUBLIC_TOOL_SCHEMAS:
        operator_wire += _size
    else:
        wire += _size
    d = fn.get("description") or ""
    if len(d) > DESC_MAX:
        long_desc.append(f"{name}:{len(d)}")
    if re.search(r"\b(NEVER|ALWAYS)\b", d):
        shouting.append(name)
    if ends_truncated(d):
        truncated.append(name)
    pu = meta.get("purpose") or ""
    if len(pu) > PURPOSE_MAX:
        long_purpose.append(f"{name}:{len(pu)}")
    if re.search(r"\b(NEVER|ALWAYS)\b", pu):
        shouting.append(f"{name}._meta.purpose")
    if ends_truncated(pu):
        truncated.append(f"{name}._meta.purpose")
    sk = meta.get("skill")
    if sk and not (skills_dir / f"{sk}.md").is_file():
        bad_skill.append(f"{name}→{sk}")
    for pname, pv in walk_params(name, (fn.get("parameters") or {}).get("properties")):
        pd = pv.get("description") or ""
        if len(pd) > PARAM_MAX:
            long_param.append(f"{pname}:{len(pd)}")
        if re.search(r"\b(NEVER|ALWAYS)\b", pd):
            shouting.append(pname)
        if ends_truncated(pd):
            truncated.append(pname)
        # A description that spells out what SOME of its enum values mean must
        # spell out all of them: a half-listed op enum reads as the whole set
        # and the model never calls the ops that were dropped. (Descriptions
        # that explain the argument without naming any value are fine — the
        # enum itself is already on the wire.)
        vals = [v for v in enum_values(pv) if isinstance(v, str)]
        named = [v for v in vals if re.search(rf"(?<![\w-]){re.escape(v)}(?![\w-])", pd)]
        if named and len(named) != len(vals):
            enum_gap.append(f"{pname}:missing {sorted(set(vals) - set(named))}")

check(f"every description ≤ {DESC_MAX} chars", not long_desc, ", ".join(long_desc[:12]))
check(f"every parameter description ≤ {PARAM_MAX} chars", not long_param, ", ".join(long_param[:12]))
check(f"every _meta.purpose ≤ {PURPOSE_MAX} chars", not long_purpose, ", ".join(long_purpose[:12]))
check("no description shouts (NEVER / ALWAYS in capitals)", not shouting, ", ".join(shouting))
check("nothing on the wire ends mid-sentence (';' ',' '—')", not truncated, ", ".join(truncated[:12]))
check("an enumerating description names EVERY enum value", not enum_gap, "; ".join(enum_gap[:12]))
check("every _meta.skill names a skill file that exists", not bad_skill, ", ".join(bad_skill))
check(f"public schemas on the wire ≤ {WIRE_MAX:,} chars (was 217,782 combined)", wire <= WIRE_MAX, f"{wire:,}")
check(f"operator-only schemas ≤ {OPERATOR_WIRE_MAX:,} chars", operator_wire <= OPERATOR_WIRE_MAX,
      f"{operator_wire:,}")
print(f"        wire size now: public {wire:,} chars across "
      f"{len(allt) - len(set(OPERATOR_PLATFORM_TOOL_SCHEMAS) - set(PUBLIC_TOOL_SCHEMAS))} tools; "
      f"operator-only {operator_wire:,} across "
      f"{len(set(OPERATOR_PLATFORM_TOOL_SCHEMAS) - set(PUBLIC_TOOL_SCHEMAS))}")

# How a readable repo reaches the code index lives in the skill, not the
# schema (event-shaped instructions travel with the event / the skill). The rule
# ("if read access, index") is: no ask, every
# readable repo is indexed (codesearch/repo_sync.py).
emb = (skills_dir / "embedding.md").read_text()
check("embed_jobs: the index-every-readable-repo rule lives in default-skills/embedding.md",
      "if read access, index" in emb and "Ask the user ONCE" not in emb)
ej = (allt["embed_jobs"]["function"].get("description") or "")
check("embed_jobs description points at the skill instead of carrying the rule",
      "skill 'embedding'" in ej and "ask them once" not in ej.lower())

# Doctrine that moved must have landed: one spot check per new skill.
for skill, needle in (
    ("artifacts", "5 minutes and ~11k tokens"),
    ("network", "Detection is soft and evadable"),
    ("computer", "exact text, never a screenshot"),
    ("dcn", "the token budget is the user's money"),
    ("storage", "Switching backends changes only where NEW writes land"),
    ("scheduling", "THIS IS YOUR OWN CLOCK"),
    ("keys", "There is intentionally NO reveal op"),
    ("apps", "INSTALL IS NOT DELIVERY"),
    ("repos", "sent to grep.app, a third party"),
    ("routing", "never route silently"),
    ("memory", "drifting warmer"),
    ("comms", "default-deny already refuses them"),
    ("build-workflow", "no node/npm/npx, make, jq or docker"),
    ("coding-protocols", "The pod STAYS UP while a login is waiting"),
    ("review", "repeated identical shots flatten past ~6"),
    ("tickets", "NOTHING LEAVES THE POD ON OPEN"),
):
    p = skills_dir / f"{skill}.md"
    flat = " ".join(p.read_text().split()) if p.is_file() else ""
    check(f"skill {skill}: moved doctrine present", needle in flat, needle)

# ── CORE_MAX: the search-mode list per surface (see CORE_MAX above) ──
import copy  # noqa: E402
from mastercode.runtime.session import (  # noqa: E402
    _build_host_orchestration_tools, _build_search_door_tools)
from mastercode.runtime.tool_access import read_only_variant  # noqa: E402
from mastercode.runtime.tool_core import CORE_EXTRAS  # noqa: E402
from mastercode.runtime.tool_lanes import lane_for  # noqa: E402
from mastercode.runtime.tools import host_tool_allowed_on  # noqa: E402


def _wire_of(s):
    s = copy.deepcopy(s)
    s.pop("_meta", None)
    return s


_core_verbs = _build_host_orchestration_tools() + _build_search_door_tools()
_natives = []
for _k, _s in allt.items():
    _n = (_s.get("function") or {}).get("name") or _k
    _ln = lane_for(_n, _s)
    if _k.startswith("_meta_") or _ln in ("host", "retired", "verb"):
        continue
    _v = _s if _ln == "read" else read_only_variant(_n, _s)
    if _v:
        _natives.append(_wire_of(_v))
_natives.sort(key=lambda s: -len(json.dumps(s)))
for _surface, _ic in (("workbench", "project:1"), ("circles", "chat"), ("other", "app")):
    from mastercode.runtime.tool_doors import compact_schema as _compact  # noqa: E402
    # The build doors ride compact (find_tools(name=...) gives the full schema).
    _doors = [_compact(_wire_of(_s)) for _k, _s in allt.items()
              if not _k.startswith("_meta_")
              and lane_for(_s["function"]["name"], _s) == "host"
              and host_tool_allowed_on(_s["function"]["name"], _ic)]
    _core = len(json.dumps(_core_verbs + _doors + _natives[:CORE_EXTRAS]))
    check(f"CORE_MAX holds on {_surface}: the search-mode list with the {CORE_EXTRAS} largest extras "
          f"is {_core:,} chars", _core <= CORE_MAX[_surface], f"{_core:,} > {CORE_MAX[_surface]:,}")

print("\nPASS" if not fails else f"\nFAIL: {fails} check(s)")
sys.exit(1 if fails else 0)
