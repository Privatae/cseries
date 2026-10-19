"""mastercode.runtime.host_memory — how many more of its own turns the Host's
memory holds.

The limit is not set by the platform: the memory is the Host's to manage
(each thread costs this much, the pool has this much free, so it can run
this many threads). So there is no count of turns anywhere:
the Host's own code reads its memory and does the division.

  limit         the Host container's cgroup `memory.max` (its share of the
                person's pool); None when unlimited or unreadable.
  working set   `memory.current` less reclaimable file cache
                (`memory.stat` inactive_file), as Kubernetes counts a
                container's memory. The page cache of saved pages is not
                memory the Host needs.
  a turn        TURN_COST_BYTES, measured by scripts/rig_parallel_threads.py:
                11.5 MB a turn at 2.4 million characters of history.

  turns_that_fit(in_flight) = (limit − working set − in_flight × cost) // cost

Turns already running count at a whole turn's cost, whatever they hold yet,
so turns started together never all count the same free memory. With no
readable limit the answer is None: nothing is held back on a number the Host
cannot read.
"""
from __future__ import annotations

import logging
import os
from typing import Optional

log = logging.getLogger("cseries.mastercode.host_memory")

CGROUP_ROOT = os.environ.get("CSERIES_CGROUP_ROOT", "/sys/fs/cgroup")
# The words a turn of the Host's own that does not fit is held with; callers
# that wait and try again recognise the refusal by them.
NO_ROOM = "the Host's memory has no room for another turn now"
TURN_COST_BYTES = int(11.5 * 1024 * 1024)
_MB = 1024 * 1024


def _read(name: str) -> Optional[str]:
    try:
        with open(os.path.join(CGROUP_ROOT, name)) as f:
            return f.read().strip()
    except OSError:
        return None


def limit_bytes() -> Optional[int]:
    v = _read("memory.max")
    if not v or v == "max":
        return None
    try:
        return int(v)
    except ValueError:
        return None


def working_set_bytes() -> Optional[int]:
    cur = _read("memory.current")
    if cur is None:
        return None
    try:
        used = int(cur)
    except ValueError:
        return None
    inactive = 0
    for line in (_read("memory.stat") or "").splitlines():
        k, _, v = line.partition(" ")
        if k == "inactive_file":
            try:
                inactive = int(v)
            except ValueError:
                pass
            break
    return max(0, used - inactive)


def in_flight_turns() -> int:
    """Turns running in this process now (the turn registry's live entries)."""
    try:
        from server.turn_registry import get_turn_registry
        return len(getattr(get_turn_registry(), "_active", {}) or {})
    except Exception:  # noqa: BLE001
        return 0


def turns_that_fit(in_flight: Optional[int] = None) -> Optional[int]:
    """How many more turns of its own the Host's memory holds now, or None
    when the Host has no readable limit."""
    lim, ws = limit_bytes(), working_set_bytes()
    if lim is None or ws is None:
        return None
    busy = in_flight_turns() if in_flight is None else int(in_flight)
    free = lim - ws - busy * TURN_COST_BYTES
    return max(0, free // TURN_COST_BYTES)


def one_more_fits(in_flight: Optional[int] = None) -> bool:
    n = turns_that_fit(in_flight)
    return n is None or n >= 1


def facts(in_flight: Optional[int] = None) -> dict:
    """What the Host is shown: its limit, its working set, what is free, what
    a turn costs, the turns in flight and how many more fit."""
    lim, ws = limit_bytes(), working_set_bytes()
    busy = in_flight_turns() if in_flight is None else int(in_flight)
    return {
        "limit_mb": round(lim / _MB, 1) if lim is not None else None,
        "working_set_mb": round(ws / _MB, 1) if ws is not None else None,
        "free_mb": round((lim - ws) / _MB, 1) if lim is not None and ws is not None else None,
        "turn_cost_mb": round(TURN_COST_BYTES / _MB, 1),
        "turns_in_flight": busy,
        "more_turns_fit": turns_that_fit(busy),
    }


def why_not(in_flight: Optional[int] = None) -> str:
    """The sentence a turn of the Host's own that does not fit is held with."""
    f = facts(in_flight)
    return (f"{NO_ROOM}: {f['working_set_mb']} MB of "
            f"{f['limit_mb']} MB in use, {f['turns_in_flight']} turn(s) running, and a turn needs "
            f"about {f['turn_cost_mb']} MB")
