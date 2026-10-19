"""runtime/tool_core.py — the fixed tool list and its history notes.

THE CORE. A search-mode call sends one list per surface, built once
before the turn's first call and never edited during the turn, so the
provider's prompt cache keeps holding it:

  the six verbs, find_tools, read,
  the build doors the surface holds (called by name, never through a door;
  sent compact, find_tools(name=...) gives the full schema),
  and the surface's most-used tools (CORE_EXTRAS of them; none now).

"Most-used" is counted from the Host's own turn rows: per surface, the
number of turns that called each tool through any door, over the
USE_WINDOW_DAYS before this process started. Counted once per process, on
the first search-mode turn, from rows written BEFORE the process started, so
a turn in this process never moves the list; it changes when the kernel
restarts. The replay rig (scripts/rig_tool_surface.py) chose its core the
same way: from turns before its cut, scored on turns after it. Only a tool a
native call can run is added: a held read, or the read-only variant of a
mixed tool. An action's schema in the list would invite a native call the
kernel refuses ("use operate"), a wasted round.

HISTORY NOTES. After each turn, the schemas of the tools it called
ride that thread's history as a platform note following the turn, rendered
by DoorCatalog.render — the function find_tools uses — so the model never
sees two versions of a schema. A schema is rendered once per visible window,
at its first appearance; when that turn leaves the window, the next turn
that called the tool renders it. A note never starts the window. The tools
in a note are the tools the turn CALLED (metadata.tool_calls), not every tool
it found.

The prefetch and the groups mode this module once stood beside
are gone: the model searches
with find_tools when it needs a tool.
"""
from __future__ import annotations

import json
import logging
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

log = logging.getLogger("cseries.mastercode.tool_core")

# Top 5 measured as the cheapest list on both turn kinds when the
# list carried ~20 tools. Re-measured
# on a Host's first call: its five extras were 9,150 of the list's 32,992
# characters (about 2,400 tokens, 40% of the 6,000-token target for the whole
# first call). None ride now; a tool is one find_tools away and runs through
# its door.
CORE_EXTRAS = 0

# Surfaces (prefire_tools.surface_key) whose build doors do NOT ride the list:
# find_tools finds them and operate(intent, tool, args) runs them, through the
# same host-own path a call by name takes (session.py). Circles' six doors
# were ~3,900 characters on every Circles call. The Workbench is the build
# surface and keeps them listed.
BUILD_DOORS_FOUND_ON = frozenset({"circles"})


def build_doors_listed(interaction_context) -> bool:
    """Whether this surface's build doors ride the turn's tool list."""
    from mastercode.runtime.prefire_tools import surface_key
    return surface_key(interaction_context) not in BUILD_DOORS_FOUND_ON
# The window the use counts come from; the rig's export is the same 30 days.
USE_WINDOW_DAYS = 30

# The kernel's start (session.py loads this module with the turn path): use is
# counted from rows written before it.
KERNEL_START = datetime.now(timezone.utc)

# Never counted and never noted: the verbs, the search verbs and `orient`
# ride (or, for orient, are absent from) every list already.
_NOT_TOOLS = frozenset({"dispatch", "operate", "recall", "agent_status", "escalate",
                        "close_turn", "find_tools", "read", "orient"})

# A history note's first line. The pre-send trim and the leading-note drop
# find a note by it (context_budget, memory_engine).
NOTE_HEAD = "[platform] Tools called in the turn above, as find_tools shows them:"


# ── Which surface a stored turn ran on ─────────────────────────────────────
def row_interaction_context(kind: str | None, ref: str | None, app_id: str | None) -> str:
    """A stored turn's interaction context from its source_context and app
    tag: a project is the Workbench; a Circles thread or the Circles app is
    chat; another app is that app. prefire_tools.surface_key maps the result
    to the surface ('workbench', 'circles', or the app)."""
    kind = kind or ""
    ref = ref or ""
    if kind == "project" and ref:
        return f"project:{ref}"
    if kind == "thread" or app_id in (None, "", "circles") or ref == "circles":
        return "chat"
    return app_id or ref or "chat"


def called_names(chips) -> list[str]:
    """Every tool a turn called, once each in first-call order, by its
    target: operate's `operate_tool`, a search-mode read's `read_tool`, else
    the name called. The verbs, the search verbs and orient are left out."""
    out: list[str] = []
    for c in chips or []:
        if not isinstance(c, dict) or not c.get("tool"):
            continue
        t = c.get("tool")
        if t == "operate" and c.get("operate_tool"):
            t = c["operate_tool"]
        elif t == "read" and c.get("read_tool"):
            t = c["read_tool"]
        t = str(t)
        if t not in _NOT_TOOLS and t not in out:
            out.append(t)
    return out


def rank_by_use(rows) -> dict[str, list[tuple[str, int]]]:
    """Per surface, every tool by the number of turns that called it, most
    first, ties by name (a set's order is randomised per process: the same
    rows must give the same core). `rows`: [(interaction_context, chips)]."""
    from mastercode.runtime.prefire_tools import surface_key
    per: dict[str, Counter] = defaultdict(Counter)
    for ic, chips in rows or []:
        per[surface_key(ic)].update(set(called_names(chips)))
    return {sk: sorted(c.items(), key=lambda kv: (-kv[1], kv[0]))
            for sk, c in sorted(per.items())}


def pick_extras(ranked: list[tuple[str, int]] | None, eligible, k: int = CORE_EXTRAS) -> list[str]:
    """The first `k` tools of a surface's ranking that a native call can run
    on this turn (`eligible`), in rank order."""
    out: list[str] = []
    for name, _n in ranked or []:
        if len(out) >= k:
            break
        if name in eligible:
            out.append(name)
    return out


_USE_SQL = """
SELECT metadata->'source_context'->>'kind' AS kind,
       metadata->'source_context'->>'ref'  AS ref,
       metadata->>'app_id'                 AS app_id,
       metadata->'tool_calls'              AS chips
  FROM entity_memory
 WHERE entity_id = $1
   AND memory_type = 'turn'
   AND metadata->>'role' = 'assistant'
   AND jsonb_typeof(metadata->'tool_calls') = 'array'
   AND created_at >= $2
   AND created_at < $3
"""

_USE_CACHE: dict[int, dict[str, list[tuple[str, int]]]] = {}


async def use_ranking(pool, entity_id: int) -> dict[str, list[tuple[str, int]]]:
    """The Host's per-surface use ranking, counted once per process from
    rows written before it started (KERNEL_START). A failed read ranks
    nothing and is not remembered: the next turn reads again."""
    hit = _USE_CACHE.get(int(entity_id))
    if hit is not None:
        return hit
    try:
        rows = await pool.fetch(_USE_SQL, int(entity_id),
                                KERNEL_START - timedelta(days=USE_WINDOW_DAYS), KERNEL_START)
    except Exception as e:  # noqa: BLE001
        log.warning("tool core: use counts unavailable, no extras this turn: %s", e)
        return {}
    pairs = []
    for r in rows or []:
        chips = r.get("chips") if hasattr(r, "get") else r["chips"]
        if isinstance(chips, str):
            try:
                chips = json.loads(chips)
            except (TypeError, ValueError):
                chips = None
        if not isinstance(chips, list):
            continue
        pairs.append((row_interaction_context(r["kind"], r["ref"], r["app_id"]), chips))
    ranked = rank_by_use(pairs)
    _USE_CACHE[int(entity_id)] = ranked
    return ranked


# ── History notes ──────────────────────────────────────────────────────────
def render_entries(entries: list[dict]) -> str:
    """Rendered find_tools entries as the wire carries a tool result
    (tool_result_wall._bound_tool_result: json.dumps, default=str), so a
    schema's bytes in a note, in the prefetch and in a find_tools result are
    the same bytes."""
    return json.dumps(entries, default=str)


def note_text(entries: list[dict]) -> str:
    return NOTE_HEAD + "\n" + render_entries(entries)


def is_note(message) -> bool:
    """A history note this module wrote (a system message opening with
    NOTE_HEAD)."""
    if not isinstance(message, dict) or message.get("role") != "system":
        return False
    c = message.get("content")
    return isinstance(c, str) and c.startswith(NOTE_HEAD)


class NoteRenderer:
    """Renders the note for one turn's called tools against this turn's
    catalog: only tools find_tools would return now (`DoorCatalog.searchable`)
    and that are not in the core list (they are in the tool list already)."""

    def __init__(self, catalog, core_names):
        self.catalog = catalog
        self.core = set(core_names or ())
        self._cache: dict[str, str] = {}

    def renderable(self, name: str) -> bool:
        c = self.catalog
        return (c is not None and name in c.searchable and name in c.entries
                and name not in self.core)

    def entry_json(self, name: str) -> str:
        s = self._cache.get(name)
        if s is None:
            s = json.dumps(self.catalog.render(name), default=str)
            self._cache[name] = s
        return s

    def __call__(self, names: list[str]) -> tuple[str, list[str]] | None:
        """(note text, the names it rendered), or None when none renders."""
        keep = [n for n in names if self.renderable(n)]
        if not keep:
            return None
        return note_text([self.catalog.render(n) for n in keep]), keep


def note_plan(turns, note_fn) -> list[tuple[str, list[str]] | None]:
    """Per turn of a visible window, its note: each tool rendered at its
    first appearance in the window, never on a turn that would start the
    window (a turn whose only message would be the note). Mirrors exactly
    what memory_engine.history_messages emits, so sizing and rendering
    agree."""
    seen: set[str] = set()
    started = False
    out: list[tuple[str, list[str]] | None] = []
    for m in turns or []:
        c = m.get("content") or ""
        if not (m.get("role") == "assistant" and not c.strip()):
            started = True
        note = None
        if note_fn is not None and started and m.get("role") == "assistant":
            fresh = [n for n in (m.get("tools_called") or []) if n not in seen]
            if fresh:
                note = note_fn(fresh)
                if note is not None:
                    seen.update(note[1])
        out.append(note)
    return out


def window_chars(turns, note_fn) -> int:
    """What a window of turn rows weighs in history: each row's own text
    plus the notes it renders in this window."""
    total = sum(len(m.get("content") or "") for m in turns or [])
    for note in note_plan(turns, note_fn):
        if note is not None:
            total += len(note[0])
    return total


def fit_history(eligible: list[dict], *, max_hist: int, max_msgs: int, block: int,
                note_fn) -> tuple[list[dict], int]:
    """The history walk with notes counted: the same chunked
    eviction the groups-mode walk uses (session.py), sized on each row's
    text plus its note. Dropping a row from the front can only move a note
    to a later row, never grow the total, so the front is cut until the
    window, notes re-rendered, fits — the newest exchange always stays.
    Returns (kept rows, their weight with notes)."""
    total = window_chars(eligible, note_fn)
    if total <= max_hist:
        kept = list(eligible)
    else:
        target = ((total - max_hist + block - 1) // block) * block
        sizes = [len(m.get("content") or "") for m in eligible]
        for i, note in enumerate(note_plan(eligible, note_fn)):
            if note is not None:
                sizes[i] += len(note[0])
        dropped = cut = 0
        for i, sz in enumerate(sizes):
            if dropped >= target:
                break
            dropped += sz
            cut = i + 1
        cut = min(cut, max(len(eligible) - 2, 0))
        kept = eligible[cut:]
    if len(kept) > max_msgs:
        kept = kept[-max_msgs:]
    weight = window_chars(kept, note_fn)
    # A cut row's tools render at their next appearance: re-check the fit.
    while weight > max_hist and len(kept) > 2:
        kept = kept[1:]
        weight = window_chars(kept, note_fn)
    return kept, weight
