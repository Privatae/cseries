#!/usr/bin/env python3
"""Replay a Host's recorded turns through today's tool selection and the
proposed one.

Two modes, the same turns:

  groups   what ran before the brief prompt: the verbs +
           `orient` + the groups the turn RECORDED in its ledger. The prefire
           pick that chose them is deleted, so this column replays the
           recorded groups and REPRODUCTION below is equal by construction —
           it is no longer a measurement.
  search   as built: the fixed core per surface (the verbs, `find_tools`,
           `read`, the build doors the surface holds, the surface's most-used
           tools); the prefetch (the search on the person's message, up to 5
           tools); the schemas of tools the thread called earlier, rendered
           into its history.

The catalog is the SESSION's: the schema build session.py runs
(runtime/tools._build_tool_schemas, no surface filter), the held read set,
the per-op read map and read-only variants, the build doors a surface holds,
and for search mode `tool_doors.DoorCatalog` built from those same sets — so
what the rig searches, ranks and renders is what find_tools searches, ranks
and renders. The core's extras and the prefetch are runtime/tool_core's own
functions.

What it reports, in the order the design says to trust it:

  1. REPRODUCTION. How often the replayed group pick equals the group set
     the turn recorded (turns with no `orient` open). Until this is high,
     nothing else here describes the real Host.
  2. COVERAGE. For every tool a turn actually called: was it in the core,
     already in the thread's history, or in the prefetch top 5 — or would
     the Host have had to search for it (one more model call)? Recall@5 is
     the prefetch alone against real calls.
  3. CHARACTERS. Tool characters each mode puts on a call.
  4. PRICE, under every pricing in PRICINGS. A price-weighted model of the
     tool text per turn, using the turn's own recorded call count and gap
     from the thread's previous turn, with the price below for each schema
     search mode adds, and charging every needed search a whole extra call
     that re-reads the prompt search mode would send. A model, labelled as
     one; a live comparison measures it for real.

     The pricing is per provider path: what a cache write and a cache read
     count for, against a fresh input token at 1.0. Anthropic writes at 1.25
     and reads at 0.1. A subscription that counts cache reads at full value
     reads at 1.0 — Kimi's subscription quota is REPORTED to (kimi-code issue
     #3899, open, unconfirmed), so the rig reports every result under both
     and the decision does not rest on that report.

A turn is replayed on the person's message that started it. A turn the
platform started (origin 'continuation') ran on a wake-up brief that is not
in the export; it is replayed on no message and left out of the
reproduction check, and the report says how many there were.

Input: a JSONL export of turn rows, one object per row:
  {id, at, role, sc, app_id, origin, content (user rows), tool_calls
   [{tool, operate_tool, op, status}], ledger (assistant rows:
   metadata.context_ledger), go_ahead_fired, provider, model}
Export it read-only from the Host's own database (`entity_memory` rows with
memory_type='turn'); see --help for the query. Keep the export outside the
repo: it holds the person's messages.

  PYTHONPATH=brain:. python3 scripts/rig_tool_surface.py EXPORT.jsonl [--json OUT]
  PYTHONPATH=brain:. python3 scripts/rig_tool_surface.py EXPORT.jsonl --table --train-until T
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, os.environ.get("CSERIES_BRAIN", str(REPO / "brain")))
sys.path.insert(0, str(REPO))

EXPORT_SQL = """BEGIN READ ONLY;
SELECT json_build_object(
 'id', id, 'at', created_at, 'role', metadata->>'role',
 'sc', (metadata->'source_context'->>'kind')||':'||coalesce(metadata->'source_context'->>'ref',''),
 'app_id', metadata->>'app_id', 'origin', metadata->>'origin',
 'content', CASE WHEN metadata->>'role'='user' THEN content END,
 'tool_calls', (SELECT json_agg(json_build_object('tool', c->>'tool',
     'operate_tool', c->>'operate_tool', 'read_tool', c->>'read_tool',
     'op', c->>'op', 'status', c->>'status'))
   FROM jsonb_array_elements(CASE WHEN jsonb_typeof(metadata->'tool_calls')='array'
     THEN metadata->'tool_calls' ELSE '[]'::jsonb END) c),
 'ledger', metadata->'context_ledger', 'go_ahead_fired', metadata->'go_ahead'->'fired',
 'provider', metadata->>'provider', 'model', metadata->>'model')
FROM entity_memory WHERE memory_type='turn' AND created_at > now()-interval '30 days'
ORDER BY created_at, id;
COMMIT;"""

PREFETCH = 5
# What rode every groups-mode list by name (the deleted prefire_tools
# .ALWAYS_TOOLS), and the deleted prefetch block's head, for replaying the
# old surface on old rows.
_ALWAYS_TOOLS = frozenset({"orient"})
PREFETCH_HEAD = (
    "## Tools for this message\n"
    "Found by searching your tools for this message; find_tools finds any "
    "other. Each comes with its door: a 'read' tool runs with read(tool, args), "
    "an 'operate' tool with operate(intent, tool, args).")
# History the model sees: recall_recent_turns(limit=30) rows, about 15
# exchanges (memory_engine.py).
NOTE_WINDOW_ASSISTANT_TURNS = 15
# Anthropic 5-minute cache.
CACHE_TTL_S = 300
VERBS = {"dispatch", "operate", "recall", "agent_status", "escalate", "close_turn"}
# What a cache write and a cache read count for, per provider path, against
# a fresh input token at 1.0 (the module docstring).
PRICINGS: dict[str, dict[str, float]] = {
    "anthropic": {"write": 1.25, "read": 0.1},
    "kimi-sub": {"write": 1.0, "read": 1.0},
}
# The Anthropic figures.
WRITE, READ = PRICINGS["anthropic"]["write"], PRICINGS["anthropic"]["read"]


def _size(obj) -> int:
    return len(json.dumps(obj, default=str))


def _when(s: str) -> datetime:
    """An export time or a --train-until value as a time. Compared as
    strings, "22:47:01+00:00" sorts before "22:47Z" and an offset other than
    +00:00 moves a row by hours; as times neither happens. A value with no
    offset is read as UTC."""
    t = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def interaction_context(row: dict) -> str:
    """The surface a turn ran on, from its scope — runtime/tool_core's own
    mapping, the one the kernel counts its core extras by."""
    from mastercode.runtime.tool_core import row_interaction_context
    kind, _, ref = (row.get("sc") or "").partition(":")
    return row_interaction_context(kind, ref, row.get("app_id"))


class _Pool:
    """The one read _build_tool_schemas makes: is this Host infra.codebase
    admin (the bridge tools)."""

    def __init__(self, bridge: bool):
        self.bridge = bridge

    async def fetchval(self, q, *a):
        return 1 if self.bridge and "infra.codebase" in q else None


class Catalog:
    """What the Host holds, built with the session's own functions
    (session.py, the tool-list build): the CEIGAS schema build with no
    surface filter, retired tools removed, the held reads, the per-op read
    map and the read-only variants, the build doors per surface — and, for
    search mode, tool_doors.DoorCatalog over the same sets."""

    def __init__(self, *, bridge: bool, operator: bool):
        from mastercode.public_tool_registry import (
            OPERATOR_PLATFORM_TOOL_SCHEMAS, PUBLIC_TOOL_SCHEMAS)
        from mastercode.runtime.session import (
            _build_host_orchestration_tools, _build_search_door_tools)
        from mastercode.runtime.tool_access import read_only_variant, read_safe_ops
        from mastercode.runtime.tool_doors import DoorCatalog
        from mastercode.runtime.tool_lanes import lane_for
        from mastercode.runtime.tools import OPERATOR_ENTITY_ID, _build_tool_schemas
        from mastercode.runtime.triage import _BRIDGE_TOOL_SCHEMAS
        eid = OPERATOR_ENTITY_ID if operator else OPERATOR_ENTITY_ID + 1000
        cap = asyncio.run(_build_tool_schemas(_Pool(bridge), eid, interaction_context=None))
        self.cap = [s for s in cap if lane_for(s["function"]["name"], s) != "retired"]
        self.wire = {s["function"]["name"]: s for s in self.cap}
        # Each tool's group, from the registry's _meta (what the session's
        # group map reads).
        self.meta: dict[str, dict] = {}
        for src in (PUBLIC_TOOL_SCHEMAS, _BRIDGE_TOOL_SCHEMAS, OPERATOR_PLATFORM_TOOL_SCHEMAS):
            for k, sch in src.items():
                if isinstance(sch, dict) and not k.startswith("_meta_"):
                    nm = (sch.get("function") or {}).get("name") or k
                    self.meta.setdefault(nm, sch.get("_meta") or {})
        self.lane = {n: lane_for(n, s) for n, s in self.wire.items()}
        self.read_safe_tools = {n for n, ln in self.lane.items() if ln == "read"}
        self.read_safe_ops: dict = {}
        self.variant: dict = {}
        for n, s in self.wire.items():
            if n in self.read_safe_tools:
                continue
            ops = read_safe_ops(n, s)
            if ops:
                self.read_safe_ops[n] = ops
                v = read_only_variant(n, s)
                if v:
                    self.variant[n] = v
        self.doors = DoorCatalog(self.cap, read_safe_tools=self.read_safe_tools,
                                 read_safe_ops=self.read_safe_ops)
        self.verbs = _build_host_orchestration_tools()
        self.verbs_chars = _size(self.verbs)
        self.search_verbs = _build_host_orchestration_tools() + _build_search_door_tools()
        self._held: dict[str, dict] = {}
        self._entry: dict[str, int] = {}

    def held(self, ic: str) -> dict:
        """{name: {group, lane, wire, chars, loadable, native}} for every
        tool this surface holds. `loadable`: today's tools= candidates (the
        session's _domain_tools: reads, read-only variants, this surface's
        build doors). `native`: a core extra may be it (loadable, not a door,
        not orient)."""
        if ic in self._held:
            return self._held[ic]
        ALWAYS_TOOLS = _ALWAYS_TOOLS
        from mastercode.runtime.tools import host_tool_allowed_on
        out = {}
        for nm, s in self.wire.items():
            lane = self.lane[nm]
            group = self.meta.get(nm, {}).get("category") or "other"
            wire, loadable = s, False
            if lane == "read":
                loadable = True
            elif lane == "host":
                if not host_tool_allowed_on(nm, ic):
                    continue
                loadable = True
            elif nm in self.variant:
                wire, loadable = self.variant[nm], True
            out[nm] = {"group": group, "lane": lane, "wire": wire, "chars": _size(wire),
                       "loadable": loadable,
                       "native": loadable and lane != "host" and nm not in ALWAYS_TOOLS}
        self._held[ic] = out
        return out

    def entry_chars(self, name: str) -> int:
        """One tool as find_tools, the prefetch and a history note carry it."""
        if name not in self._entry:
            self._entry[name] = _size(self.doors.render(name))
        return self._entry[name]

    def core(self, ic: str, extras: list[str]) -> list[dict]:
        """The search-mode list on this surface, as session.py builds it."""
        h = self.held(ic)
        doors = [v["wire"] for v in h.values() if v["lane"] == "host"]
        return self.search_verbs + doors + [h[n]["wire"] for n in extras]


def called_tools(row: dict) -> list[str]:
    """The tools a turn called, a door's target in place of the door
    (operate's `operate_tool`, a search-mode read's `read_tool`)."""
    out = []
    for c in row.get("tool_calls") or []:
        if not isinstance(c, dict) or not c.get("tool"):
            continue
        t = c["tool"]
        if t == "operate" and c.get("operate_tool"):
            t = c["operate_tool"]
        elif t == "read" and c.get("read_tool"):
            t = c["read_tool"]
        out.append(t)
    return out


def prompt_chars(ledger: dict) -> int:
    """The turn's first-call prompt size from its own ledger: system, static
    additions, history, tools, per-turn blocks and the message."""
    tools = ledger.get("tools") or {}
    return int((ledger.get("system_chars") or 0) + (ledger.get("static_appended_chars") or 0)
               + ((ledger.get("history") or {}).get("chars") or 0) + (tools.get("chars") or 0)
               + sum((ledger.get("per_turn_blocks") or {}).values()) + (ledger.get("user_chars") or 0))


def _median(sizes) -> int:
    sizes = sorted(sizes)
    return sizes[len(sizes) // 2] if sizes else 0


def tool_cost(chars: float, calls: int, warm: bool, p: dict | None = None) -> float:
    """Price-weighted characters for a block in the cached prefix (tools,
    core): read on every call when warm, written on the first and read after
    when cold."""
    p = p or PRICINGS["anthropic"]
    calls = max(1, calls)
    if warm:
        return chars * p["read"] * calls
    return chars * p["write"] + chars * p["read"] * (calls - 1)


def tail_cost(chars: float, calls: int, p: dict | None = None) -> float:
    """Price-weighted characters for text in the turn's final message
    (prefetch, a newly rendered note): full on call 1, written on call 2,
    read after (provider_gateway.py: breakpoint 2 sits on the second-to-last
    message)."""
    p = p or PRICINGS["anthropic"]
    calls = max(1, calls)
    return chars * (1.0 + (p["write"] if calls >= 2 else 0) + p["read"] * max(0, calls - 2))


def staples_from(rows: list[dict], *, until: str | None, k: int,
                 eligible=None) -> dict[str, list[str]]:
    """Per surface, the k tools called in the most turns — counted ONLY on
    turns before `until`, so a variant that adds them to the core is judged
    on turns it never saw. runtime/tool_core's own ranking and pick, the
    kernel's at kernel start. `eligible(interaction_context, tool)` says
    which a native call can run there (run() passes the catalog's); without
    it every counted tool may be added."""
    if k <= 0:
        return {}
    from mastercode.runtime.prefire_tools import surface_key
    from mastercode.runtime.tool_core import pick_extras, rank_by_use
    cut = _when(until) if until else None
    pairs, ic_of = [], {}
    for r in rows:
        if r.get("role") != "assistant" or (cut and _when(r.get("at")) >= cut):
            continue
        ic = interaction_context(r)
        ic_of.setdefault(surface_key(ic), ic)
        pairs.append((ic, r.get("tool_calls") or []))
    out = {}
    for sk, ranked in rank_by_use(pairs).items():
        ok = ({n for n, _ in ranked if eligible(ic_of[sk], n)} if eligible
              else {n for n, _ in ranked})
        out[sk] = pick_extras(ranked, ok, k)
    return out


def run(rows: list[dict], *, bridge: bool, operator: bool,
        staples_k: int = 0, train_until: str | None = None,
        prev_message: bool = False, prefetch: int = PREFETCH) -> dict:
    """`prefetch`: how many matches the prefetch puts in front of the model
    (tool_core.PREFETCH_LIMIT in the kernel); 0 measures the turn without one."""
    from mastercode.runtime.prefire_tools import (
        surface_key)
    ALWAYS_TOOLS = _ALWAYS_TOOLS
    try:
        from server.applications.desktop_contract import looks_like_desktop_task
    except Exception:  # noqa: BLE001
        def looks_like_desktop_task(_m):
            return False
    cat = Catalog(bridge=bridge, operator=operator)
    idx = cat.doors.index()

    def _may_add(ic: str, tool: str) -> bool:
        v = cat.held(ic).get(tool)
        return bool(v) and v["native"]
    staples = staples_from(rows, until=train_until, k=staples_k, eligible=_may_add)
    cut = _when(train_until) if train_until else None
    others_median = _median(cat.entry_chars(n) for n in cat.doors.searchable)

    rows = sorted(rows, key=lambda r: (_when(r.get("at")), r.get("id") or 0))
    by_scope: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_scope[r.get("sc") or f"none:{r.get('id')}"].append(r)

    turns = []
    for _scope, srows in by_scope.items():
        last_user = None
        before_user = None
        # The person's message that no turn has answered yet. A turn takes
        # it; the next turn before another user row has none of its own.
        open_user = None
        prior_assist: list[dict] = []
        prev_turn = None
        for r in srows:
            role = r.get("role")
            if role == "user":
                before_user, last_user = last_user, r
                open_user = r
                continue
            if role != "assistant":
                continue
            led = r.get("ledger") or {}
            if not led:
                if r.get("tool_calls"):
                    prior_assist.append(r)
                continue
            # A turn the platform started (origin 'continuation') ran on a
            # wake-up brief that writes no user row (session.py send_message
            # `system_turn`), so the export does not have its message. It is
            # replayed on none, never on the person's previous message.
            own = open_user if r.get("origin") != "continuation" else None
            if own is not None:
                open_user = None
            msg = (own.get("content") or "") if own is not None else ""
            ic = interaction_context(r)
            held = cat.held(ic)
            ltools = led.get("tools") or {}
            rec_groups = set(ltools.get("groups") or [])
            picked = set(rec_groups)
            orient_opens = int(ltools.get("orient_opens") or 0)

            groups_tools = [n for n, v in held.items()
                            if v["loadable"] and (v["group"] in picked or n in ALWAYS_TOOLS)]
            groups_chars = cat.verbs_chars + sum(held[n]["chars"] for n in groups_tools)

            extras = [n for n in staples.get(surface_key(ic), []) if n in held]
            core_list = cat.core(ic, extras)
            core_chars = _size(core_list)
            core = {(t.get("function") or {}).get("name") for t in core_list}

            # History notes: a schema is rendered once per
            # visible window, after the first turn in it that called the tool,
            # and only a tool find_tools would return that is not in the core
            # (runtime/tool_core.NoteRenderer). So this turn's NEW notes are
            # the previous turn's tools that no earlier turn in the window had
            # called; the rest sit in the cached history already.
            window = prior_assist[-NOTE_WINDOW_ASSISTANT_TURNS:]

            def _notable(a):
                return {t for t in called_tools(a)
                        if t in cat.doors.searchable and t not in core}
            noted = {t for a in window for t in _notable(a)}
            earlier = {t for a in window[:-1] for t in _notable(a)}
            new_notes = (_notable(window[-1]) - earlier) if window else set()
            standing_notes = noted - new_notes
            allowed = set(cat.doors.searchable) - core - noted
            query = msg
            if prev_message and own is not None and before_user is not None:
                query = (before_user.get("content") or "") + "\n" + msg
            # The kernel's own prefetch (tool_core.prefetch_names), and the
            # full ranking for recall@5.
            hits = ([n for n, _ in idx.search(query, limit=prefetch,
                                              allowed=set(cat.doors.searchable) - core - noted)]
                    if prefetch > 0 and query.strip() else [])
            ranked = [n for n, _ in idx.search(query, limit=len(allowed) or 1, allowed=allowed)]
            prefetch_chars = ((len(PREFETCH_HEAD) + 1 + _size([cat.doors.render(n) for n in hits]))
                              if hits else 0)
            new_note_chars = sum(cat.entry_chars(n) for n in new_notes)
            standing_note_chars = sum(cat.entry_chars(n) for n in standing_notes)

            called = [t for t in called_tools(r) if t not in VERBS | {"orient", "find_tools", "read"}]
            cov = Counter()
            per_tool = []
            for t in dict.fromkeys(called):
                if t in core:
                    how = "core"
                elif t not in cat.doors.entries:
                    how = "not_held"
                elif t in noted:
                    how = "history"
                elif t in hits:
                    how = "prefetch"
                else:
                    how = "search"
                cov[how] += 1
                rank = (ranked.index(t) + 1) if t in ranked else None
                per_tool.append({"tool": t, "how": how, "rank": rank,
                                 "today": ("loaded" if t in groups_tools else
                                           "no_schema" if t in held and not held[t]["loadable"] else
                                           "unloaded")})

            calls = int(led.get("calls") or len(led.get("per_call") or []) or 1)
            gap = ((_when(r["at"]) - _when(prev_turn["at"])).total_seconds()
                   if prev_turn else None)
            within_ttl = gap is not None and gap <= CACHE_TTL_S
            rec_sha = ltools.get("first_call_sha")
            prev_sha = (((prev_turn.get("ledger") or {}).get("tools") or {}).get("first_call_sha")
                        if prev_turn else None)
            same_list = bool(rec_sha) and rec_sha == prev_sha
            searches = cov["search"]
            found = [p["tool"] for p in per_tool if p["how"] == "search"]
            pchars = prompt_chars(led)
            n_calls = calls + searches
            rec_chars = int(ltools.get("chars") or 0)
            orient_calls = sum(1 for c in r.get("tool_calls") or [] if (c or {}).get("tool") == "orient")
            # Whether each of the turn's calls made at most one tool call
            # (close_turn rides the last one): only then is an orient call
            # known to be a model call of its own. 94 of one Host's 244 turns
            # in 30 days made more tool calls than that, so it is not assumed.
            one_per_call = sum(1 for c in r.get("tool_calls") or []
                               if (c or {}).get("tool") not in (None, "close_turn")) <= max(calls - 1, 0)
            # The prompt a search-mode call carries: the recorded prompt with
            # today's tool list swapped for the core and the tool text search
            # mode adds.
            search_prompt = (pchars - rec_chars + core_chars
                             + prefetch_chars + new_note_chars + standing_note_chars)
            prices, parts_by = {}, {}
            for pname, pw in PRICINGS.items():
                # The price for each thing search mode adds:
                #   core           cached prefix, like today's list;
                #   prefetch       the turn's final message: 1.0, write, then read;
                #   new_notes      the same on this turn, plus the one rewrite the
                #                  note gets on the turn after;
                #   standing_notes already in the cached history: read per call;
                #   search_calls   each search a whole extra call, re-reading the
                #                  prompt SEARCH MODE would send;
                #   found          what a search returns, from the call after the
                #                  search: the needed tool plus PREFETCH-1 entries
                #                  of the median size find_tools returns.
                parts = {
                    "core": tool_cost(core_chars, n_calls, within_ttl, pw),
                    "prefetch": tail_cost(prefetch_chars, n_calls, pw),
                    "new_notes": (tail_cost(new_note_chars, n_calls, pw) + pw["write"] * new_note_chars
                                  if new_note_chars else 0),
                    "standing_notes": (tool_cost(standing_note_chars, n_calls, within_ttl, pw)
                                       if standing_note_chars else 0),
                    "search_calls": searches * (max(search_prompt, 0) * pw["read"]),
                    "found": sum(tail_cost(cat.entry_chars(t) + (PREFETCH - 1) * others_median,
                                           n_calls - 1, pw) for t in found),
                }
                parts_by[pname] = parts
                prices[pname] = {
                    "groups": round(tool_cost(groups_chars, calls, within_ttl and same_list, pw)),
                    "groups_recorded": round(tool_cost(rec_chars or groups_chars, calls,
                                                       within_ttl and same_list, pw)),
                    "search": round(sum(parts.values())),
                    # Never the headline: on a turn whose calls made one tool
                    # call each, an orient call was a model call of its own,
                    # which search mode would not make (a needed search is
                    # charged above as its own call) — its whole prompt.
                    "orient_credit": (round(orient_calls * max(pchars, 0) * pw["read"])
                                      if one_per_call else 0),
                }

            if cut and _when(r.get("at")) < cut:
                # Before the cut: history only, never scored.
                prev_turn = r
                prior_assist.append(r)
                continue
            turns.append({
                "id": r.get("id"), "ic": ic, "calls": calls, "gap_s": gap,
                "warm_same_list": within_ttl and same_list, "within_ttl": within_ttl,
                "replay_groups": sorted(picked), "recorded_groups": sorted(rec_groups),
                "orient_opens": orient_opens, "orient_calls": orient_calls,
                "person_message": own is not None,
                # Checkable only where the pick's input is known: a turn
                # replayed on no message is not the turn that ran.
                "reproduced": ((picked == rec_groups)
                               if orient_opens == 0 and rec_groups and own is not None else None),
                "recorded_tools_chars": ltools.get("chars"),
                "recorded_first_count": ltools.get("first_call_count"),
                "replay_groups_count": len(groups_tools) + len(cat.verbs),
                "groups_chars": groups_chars, "core_chars": core_chars,
                "prefetch_chars": prefetch_chars, "new_note_chars": new_note_chars,
                "standing_note_chars": standing_note_chars,
                "noted_tools": len(noted), "coverage": dict(cov), "per_tool": per_tool,
                "searches": searches, "prompt_chars": pchars,
                "prices": prices,
                # The Anthropic figures under their earlier names.
                "price_groups": prices["anthropic"]["groups"],
                "price_search": prices["anthropic"]["search"],
                "price_search_parts": parts_by["anthropic"],
                "price_parts": parts_by,
                "go_ahead_fired": bool(r.get("go_ahead_fired")),
                "spend_stop": bool(((led.get("spend") or {}).get("stopped_on_allowance"))),
                "provider": r.get("provider"),
            })
            prev_turn = r
            prior_assist.append(r)
    out = summarise(turns, cat)
    out["staples"] = staples
    out["train_until"] = train_until
    return out


def _pct(a, b):
    return f"{(100.0 * a / b):.1f}%" if b else "n/a"


def _change(new, old):
    return f"{(100.0 * (new - old) / old):+.1f}%" if old else "n/a"


def price_summary(turns: list[dict]) -> dict:
    """Per pricing, per turn kind: the price-weighted totals and the change
    search mode makes, against the replayed list and the recorded one."""
    kinds = {"all": turns,
             "person": [t for t in turns if t["person_message"]],
             "platform": [t for t in turns if not t["person_message"]]}
    out: dict = {}
    for pname in PRICINGS:
        out[pname] = {}
        for kind, ts in kinds.items():
            g = sum(t["prices"][pname]["groups"] for t in ts)
            gr = sum(t["prices"][pname]["groups_recorded"] for t in ts)
            s = sum(t["prices"][pname]["search"] for t in ts)
            oc = sum(t["prices"][pname]["orient_credit"] for t in ts)
            out[pname][kind] = {
                "turns": len(ts), "groups": g, "groups_recorded": gr, "search": s,
                "change": _change(s, g), "change_vs_recorded": _change(s, gr),
                "change_with_orient_credit": _change(s - oc, g),
                "need_search": _pct(sum(1 for t in ts if t["searches"]), len(ts)),
                "orient_today": _pct(sum(1 for t in ts if t["orient_opens"]), len(ts)),
            }
    return out


def summarise(turns: list[dict], cat: Catalog) -> dict:
    rep = [t for t in turns if t["reproduced"] is not None]
    rep_ok = [t for t in rep if t["reproduced"]]
    miss = Counter()
    extra = Counter()
    for t in rep:
        if not t["reproduced"]:
            miss.update(set(t["recorded_groups"]) - set(t["replay_groups"]))
            extra.update(set(t["replay_groups"]) - set(t["recorded_groups"]))
    cov = Counter()
    for t in turns:
        cov.update(t["coverage"])
    uses = [p for t in turns for p in t["per_tool"] if p["how"] not in ("core", "not_held")]
    r5 = [p for p in uses if p["rank"] is not None and p["rank"] <= PREFETCH]
    need_search = [t for t in turns if t["searches"]]
    search_miss = Counter(p["tool"] for t in turns for p in t["per_tool"] if p["how"] == "search")

    def med(xs):
        xs = [x for x in xs if x is not None]
        return statistics.median(xs) if xs else None

    # The recorded tool COUNTS (and hashes) should be reproduced, not only
    # the group set; say how often the count is.
    count_eq = [t for t in rep if t["recorded_first_count"] is not None
                and t["recorded_first_count"] == t["replay_groups_count"]]
    return {
        "turns": len(turns),
        "turns_without_person_message": sum(1 for t in turns if not t["person_message"]),
        "catalog_tools": len(cat.wire), "searchable_tools": len(cat.doors.searchable),
        "verbs_chars": cat.verbs_chars,
        "reproduction": {"checked": len(rep), "equal": len(rep_ok),
                         "rate": _pct(len(rep_ok), len(rep)),
                         "tool_count_equal": len(count_eq),
                         "groups_recorded_not_replayed": miss.most_common(8),
                         "groups_replayed_not_recorded": extra.most_common(8)},
        "coverage": {"tool_uses": sum(cov.values()), **cov},
        "recall_at_5": {"uses": len(uses), "hit": len(r5), "rate": _pct(len(r5), len(uses))},
        "turns_needing_search": {"count": len(need_search), "rate": _pct(len(need_search), len(turns))},
        "search_needed_for": search_miss.most_common(12),
        "chars_per_call_median": {
            "recorded_tools": med([t["recorded_tools_chars"] for t in turns]),
            "replayed_groups": med([t["groups_chars"] for t in turns]),
            "search_core": med([t["core_chars"] for t in turns]),
            "search_prefetch": med([t["prefetch_chars"] for t in turns]),
            "search_new_notes": med([t["new_note_chars"] for t in turns]),
        },
        "price_weighted_tool_text": {
            "groups_total": sum(t["price_groups"] for t in turns),
            "search_total": sum(t["price_search"] for t in turns),
            "groups_median": med([t["price_groups"] for t in turns]),
            "search_median": med([t["price_search"] for t in turns]),
        },
        "prices": price_summary(turns),
        "warm_same_list_turns": sum(1 for t in turns if t["warm_same_list"]),
        "exits": {"go_ahead_fired": sum(1 for t in turns if t["go_ahead_fired"]),
                  "spend_stop": sum(1 for t in turns if t["spend_stop"])},
        "by_surface": dict(Counter(t["ic"].split(":")[0] for t in turns)),
        "per_turn": turns,
    }


def render(s: dict) -> str:
    rp, cv = s["reproduction"], s["coverage"]
    lines = [
        f"# Tool-surface replay — {s['turns']} turns, catalog {s['catalog_tools']} tools "
        f"({s['searchable_tools']} searchable)",
        "",
        "## 1. Reproduction (must be high before the rest means anything)",
        f"- replayed group pick equals the recorded one: {rp['equal']} of {rp['checked']} "
        f"turns with no orient open and a person's message ({rp['rate']})",
        f"- recorded first-call tool count equal: {rp['tool_count_equal']} of {rp['checked']} "
        f"(the recorded hashes are not compared)",
        f"- turns replayed on no message (platform-started; the export has no brief): "
        f"{s['turns_without_person_message']} of {s['turns']}",
        f"- groups recorded but not replayed: {rp['groups_recorded_not_replayed']}",
        f"- groups replayed but not recorded: {rp['groups_replayed_not_recorded']}",
        "",
        "## 2. Coverage of the tools turns actually called (search mode)",
        f"- tool uses: {cv.get('tool_uses', 0)} — core {cv.get('core', 0)}, already in history "
        f"{cv.get('history', 0)}, prefetch {cv.get('prefetch', 0)}, needs a search "
        f"{cv.get('search', 0)}, not held {cv.get('not_held', 0)}",
        f"- recall@5 of the prefetch alone: {s['recall_at_5']['hit']} of {s['recall_at_5']['uses']} "
        f"({s['recall_at_5']['rate']})",
        f"- turns that would need at least one search call: {s['turns_needing_search']['count']} "
        f"({s['turns_needing_search']['rate']})",
        f"- tools most often needing a search: {s['search_needed_for']}",
        "",
        "## 3. Tool characters per call (median)",
        *[f"- {k}: {v}" for k, v in s["chars_per_call_median"].items()],
        "",
        "## 4. Price-weighted tool text (model; a search is a whole extra call)",
    ]
    for pname, kinds in s["prices"].items():
        pw = PRICINGS[pname]
        lines.append(f"- {pname} (write {pw['write']}, read {pw['read']}):")
        for kind, v in kinds.items():
            lines.append(
                f"  - {kind} ({v['turns']} turns): {v['change']} vs the replayed list, "
                f"{v['change_vs_recorded']} vs the recorded one; {v['need_search']} need a search "
                f"(orient today {v['orient_today']}); crediting the orient calls of turns that "
                f"made one tool call per model call, {v['change_with_orient_credit']}")
    lines += [
        f"- turns warm with an unchanged list today: {s['warm_same_list_turns']}",
        "",
        f"Exits: go-ahead repair fired on {s['exits']['go_ahead_fired']} turns; "
        f"spend stop on {s['exits']['spend_stop']}.",
        f"Surfaces: {s['by_surface']}",
    ]
    return "\n".join(lines)


def table(rows: list[dict], *, bridge: bool, operator: bool, train_until: str | None,
          ks=(0, 5, 10)) -> str:
    """The comparison table — core extras per surface against all turns, turns
    with a person's message and platform-started turns — under every
    pricing, against the replayed list and the recorded one."""
    runs = {k: run(rows, bridge=bridge, operator=operator, staples_k=k, train_until=train_until)
            for k in ks}
    label = {0: "nothing", 5: "top 5 by use", 10: "top 10 by use"}
    out = []
    for pname in PRICINGS:
        pw = PRICINGS[pname]
        for base, key in (("replayed list", "change"), ("recorded final list", "change_vs_recorded")):
            out += ["", f"### {pname} (write {pw['write']}, read {pw['read']}), today = the {base}", "",
                    "| core adds per surface | all turns | turns with a person's message "
                    "| platform-started turns |",
                    "|---|---|---|---|"]
            for k, s in runs.items():
                v = s["prices"][pname]
                out.append(f"| {label.get(k, f'top {k}')} | {v['all'][key]}; {v['all']['need_search']} "
                           f"need a search | {v['person'][key]}; {v['person']['need_search']} "
                           f"| {v['platform'][key]}; {v['platform']['need_search']} |")
    s0 = next(iter(runs.values()))
    a0 = s0["prices"]["anthropic"]
    head = (f"{s0['turns']} scored turns after {train_until} "
            f"({s0['turns'] - s0['turns_without_person_message']} with a person's message, "
            f"{s0['turns_without_person_message']} platform-started); orient today on "
            f"{a0['person']['orient_today']} of person turns, {a0['platform']['orient_today']} "
            f"of platform-started.")
    staples = {k: s.get("staples") for k, s in runs.items() if k}
    return head + "\n" + "\n".join(out) + "\n\nCore extras chosen before the cut: " + json.dumps(staples)


# ── --results: big results stay in the database ─────────────────────────────
# Replays a turn's TOOL RESULTS, not its tool list: what each model call of the
# turn carried of earlier results, under the policy the turn ran (no store; a
# used READ over context_budget.DROP_FLOOR_CHARS cut to a "call it again"
# stub) and under the store (runtime/result_store: a result over
# STORE_OVER_CHARS rides as its preview; a used one over the handle floor is
# cut to its handle). Both policies run through the kernel's own functions —
# result_store.preview and context_budget.drop_used_tool_results — on a
# message list of the recorded sizes.
#
# The ledger records sizes, not which round each result came back in, and each
# call's input in tokens. So a turn is FITTED first: its results are placed in
# rounds in order (dynamic programming), with tokens = in(call 1) + a × result
# characters carried + b × rounds so far (b: the per-round assistant message
# and whatever else rides per round), chosen to reproduce the recorded input of
# every call. The fit's error is reported with it. A model, labelled as one.
_RESULT_NOT_READ = {"operate", "dispatch", "escalate", "build", "close_turn"}


def _fit_rounds(ins: list[int], res: list[int], drops: dict[int, int]):
    best = None
    n, m = len(ins), len(res)
    pre = [0]
    for c in res:
        pre.append(pre[-1] + c)
    for a in [x / 100 for x in range(15, 61)]:
        for b in range(0, 3001, 125):
            INF = float("inf")
            dp = [[INF] * (m + 1) for _ in range(n)]
            bk = [[0] * (m + 1) for _ in range(n)]
            dp[0][0] = 0.0
            for k in range(1, n):
                dr = sum(v for c, v in drops.items() if c <= k + 1)
                for j in range(m + 1):
                    if dp[k - 1][j] == INF:
                        continue
                    for jj in range(j + 1 if m >= n - 1 else j, m + 1):
                        pred = ins[0] + a * (pre[jj] - dr) + b * k
                        e = dp[k - 1][j] + (pred - ins[k]) ** 2
                        if e < dp[k][jj]:
                            dp[k][jj], bk[k][jj] = e, j
            if best is None or dp[n - 1][m] < best[0]:
                j, cuts = m, []
                for k in range(n - 1, 0, -1):
                    cuts.append((bk[k][j], j))
                    j = bk[k][j]
                best = (dp[n - 1][m], a, b, list(reversed(cuts)))
    return best


def _replay_policy(ins, results, cuts, a, b, *, store: bool) -> list[float]:
    from mastercode import context_budget as cb
    from mastercode.runtime import result_store as rs
    msgs: list = [{"role": "system", "content": ""}, {"role": "user", "content": ""}]
    droppable: dict = {}
    out = [float(ins[0])]
    mark = len(msgs)
    for k in range(1, len(ins)):
        j0, j1 = cuts[k - 1]
        ids = [f"c{i}" for i in range(j0, j1)]
        msgs.append({"role": "assistant", "content": None,
                     "tool_calls": [{"id": t, "type": "function",
                                     "function": {"name": "x", "arguments": "{}"}} for t in ids]})
        for i, t in zip(range(j0, j1), ids):
            tool, chars = results[i]
            text = "x" * chars
            reg = None
            if store and chars > rs.STORE_OVER_CHARS:
                h = rs.handle_of(i + 1)
                text = rs.preview(tool, h, text)
                reg = {"tool": tool, "handle": h}
            elif tool not in _RESULT_NOT_READ:
                reg = {"tool": tool}
            msgs.append({"role": "tool", "tool_call_id": t, "content": text})
            if reg is not None:
                reg["msg"] = msgs[-1]
                droppable[t] = reg
        new, gone = cb.drop_used_tool_results(msgs, mark, droppable)
        if gone:
            msgs[:] = new
            for g in gone:
                droppable.pop(g["tool_call_id"], None)
        mark = len(msgs)
        carried = sum(len(m["content"]) for m in msgs if m.get("role") == "tool")
        out.append(ins[0] + a * carried + b * k)
    return out


def results_replay(rows: list[dict], turn_ids: list[int]) -> list[dict]:
    """Per turn: measured input, the fitted model of today's policy, and the
    same model under the store. Rows need id and ledger (EXPORT_SQL)."""
    out = []
    for r in rows:
        if r.get("id") not in turn_ids or not isinstance(r.get("ledger"), dict):
            continue
        led = r["ledger"]
        ins = [int(c.get("in") or 0) for c in led.get("per_call") or []]
        results = [(t.get("tool") or "?", int(t.get("chars") or 0)) for t in led.get("tool_results") or []]
        drops: dict[int, int] = {}
        for d in led.get("dropped") or []:
            drops[int(d["at_call"])] = drops.get(int(d["at_call"]), 0) + int(d["chars"]) - 300
        err, a, b, cuts = _fit_rounds(ins, [c for _t, c in results], drops)
        before = _replay_policy(ins, results, cuts, a, b, store=False)
        after = _replay_policy(ins, results, cuts, a, b, store=True)
        spend = (led.get("spend") or {}).get("tokens") or {}
        out.append({
            "id": r["id"], "calls_recorded": len(ins), "calls_in_turn": led.get("calls"),
            "results_recorded": len(results),
            "measured_in": sum(ins),
            "measured_in_whole_turn": (int(spend.get("uncached") or 0) + int(spend.get("cache_read") or 0)
                                       or None),
            "fit": {"tokens_per_char": a, "per_round": b,
                    "rms_per_call": round((err / max(1, len(ins))) ** 0.5)},
            "model_today": round(sum(before)), "model_store": round(sum(after)),
            "store_vs_today": round(sum(after) / max(1.0, sum(before)), 3),
            "first_call": ins[0] if ins else None,
        })
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__ + "\nExport query:\n" + EXPORT_SQL,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("export", help="JSONL export of turn rows")
    ap.add_argument("--json", help="write the full summary, per turn, here")
    ap.add_argument("--no-bridge", action="store_true",
                    help="the Host does not hold the bridge tools (infra.codebase admin)")
    ap.add_argument("--no-operator", action="store_true",
                    help="the Host does not hold the operator Host's platform tools")
    ap.add_argument("--staples", type=int, default=0,
                    help="add each surface's K most-called tools to its core (default 0)")
    ap.add_argument("--train-until",
                    help="ISO time: count staples only before it, score only turns from it")
    ap.add_argument("--prev-message", action="store_true",
                    help="the prefetch query also carries the thread's previous user message")
    ap.add_argument("--prefetch", type=int, default=PREFETCH,
                    help=f"matches the prefetch carries (default {PREFETCH}; 0 = none)")
    ap.add_argument("--table", action="store_true",
                    help="print the comparison table (core adds nothing / top 5 / top 10) "
                         "under every pricing")
    ap.add_argument("--results", help="comma-separated turn ids: replay their tool results "
                                      "today and with the result store")
    a = ap.parse_args()
    rows = [json.loads(line) for line in open(a.export) if line.strip().startswith("{")]
    if a.results:
        print(json.dumps(results_replay(rows, [int(x) for x in a.results.split(",")]), indent=1))
        return 0
    if a.table:
        print(table(rows, bridge=not a.no_bridge, operator=not a.no_operator, train_until=a.train_until))
        return 0
    s = run(rows, bridge=not a.no_bridge, operator=not a.no_operator,
            staples_k=a.staples, train_until=a.train_until, prev_message=a.prev_message,
            prefetch=a.prefetch)
    print(render(s))
    if s.get("staples"):
        print(f"Staples (counted before {s.get('train_until') or 'the end'}): {s['staples']}")
    if a.json:
        Path(a.json).write_text(json.dumps(s, indent=1, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
