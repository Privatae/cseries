"""runtime/user_limits.py — the person's limits.

A limit that stops work the person asked for, at the moment they least want
it, is theirs to set. The code supplies defaults, never hard ones. When one
is reached the Host is told which and why, can read the setting, and can
recommend a change; only the person changes it.

WHAT THIS IS. One list (LIMITS) of every limit that can stop or refuse work
the person asked for: spend, counts of turns / repairs / wakes, and time
limits. Each has a coded DEFAULT and a name. The value in force is the
person's, stored at behavioral_params["limits"][name] (or, for the few that
were settings before this module, the top-level key they already had). The
default holds only while the person has set nothing.

WHO WRITES. The person only: the Settings app's Limits page
(server/routes/limits.py, refused for any token that is not a person's
surface) or their click on a limit-change card. The Host reads every limit
(the `limits` tool, op='status') and proposes a change (op='propose'), which
files a card locked to the person (`user_only`, `no_pattern`): the Host can
neither decide it nor have it decided for it next time. Nothing in this
module writes from a Host tool.

ON A HIT. The code that enforces a limit says which one stopped the work, in
the words hit_line() returns, and records it (record_hit) so the Limits page
shows when it last bound. The work's state is kept wherever the code can keep
it. When the person changes a limit, the platform emits `limit_changed`
(server/host_events.py), so a task the Host scheduled with
wake_on={"event": "limit_changed", "match": {"name": ...}} picks the work up
again.

NOT HERE, on purpose: limits that protect the person rather than limit their
work (wrong pairing codes, rate limits on untrusted callers), page sizes,
queues that wait rather than stop, and limits another party sets (a
provider's request size, Discord's rate, GitHub's 1000-result search). The
backup's run limit (host_backup.RUN_LIMIT_S) is one of those: it sits under
the platform's six hours for an unfinished upload, so a longer one would only
fail later.

0 means "no limit" for every entry: it is the person's machine and money.
"""
from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from typing import Any, Optional

log = logging.getLogger("mastercode.user_limits")

LIMITS_KEY = "limits"          # behavioral_params["limits"] = {name: value}
PROPOSAL_KIND = "limit_change_proposal"   # = server/routes/tickets.LIMIT_CHANGE_PROPOSAL_KIND
PAYLOAD_KEY = "limit_change"
OPT_APPLY = "apply"
OPT_KEEP = "keep"
REASON_MAX = 600


@dataclass(frozen=True)
class Limit:
    name: str
    default: Optional[float]   # None: no coded number (worked out elsewhere, see `derived`)
    unit: str                  # what the value counts, in the person's words
    label: str                 # what it limits
    on_hit: str                # what happens when it binds, and what is kept
    group: str                 # spend | turns | background | builds | data
    env: Optional[str] = None  # an operator override that predates this module
    env_scale: float = 1.0     # env value / env_scale = value in `unit`
    bp_key: Optional[str] = None   # a top-level behavioral_params key it already had
    integer: bool = True
    derived: Optional[str] = None  # how the default is worked out when it is not a number
    # For a derived default: the behavioral_params key whose 0 turns the
    # derived figure off (context_budget.read_turn_defaults: turn_rounds /
    # scheduled_rounds = 0 is "no built-in figure"). A stored 0 dollars there
    # would only mean "unset", so "no limit" is written as this key = 0.
    off_key: Optional[str] = None
    # Why 0 ("no limit") is refused for this one, when the code that enforces
    # it has no "off": the value would be shown as "no limit" while the old
    # window kept stopping work.
    no_off: Optional[str] = None


LIMITS: dict[str, Limit] = {lim.name: lim for lim in (
    # ── spend ───────────────────────────────────────────────────────────────
    Limit("turn.allowance_usd", None, "dollars", "What one reply may spend",
          "The Host stops starting rounds it cannot pay for and answers with what it has; "
          "a Continue card lets you give it more.",
          "spend", bp_key="turn_allowance_usd", integer=False, off_key="turn_rounds",
          derived="priced from the model: what about 200 ordinary rounds cost"),
    Limit("scheduled.allowance_usd", None, "dollars", "What one scheduled turn may spend",
          "The scheduled turn stops starting rounds it cannot pay for and reports what it has.",
          "spend", bp_key="scheduled_task_allowance_usd", integer=False,
          off_key="scheduled_rounds",
          derived="priced from the model: what about 200 ordinary rounds cost"),
    Limit("scheduled.turns_per_day", 200, "model turns a day",
          "Model turns one scheduled task may run in 24 hours",
          "That run is skipped (nothing is spent), the thread says so, and the task runs again "
          "at its next time.",
          "spend"),
    # ── one turn ────────────────────────────────────────────────────────────
    Limit("turn.tool_rounds", 200, "tool rounds", "Tool rounds in one turn",
          "The Host stops calling tools and answers with what it has.",
          "turns", env="CSERIES_MAX_TOOL_ROUNDS", bp_key="max_tool_rounds"),
    Limit("turn.all_fail_rounds", 25, "rounds", "Rounds in a row where every tool call failed",
          "The Host stops calling tools and answers with the errors.",
          "turns", env="CSERIES_ALL_FAIL_ROUNDS_MAX"),
    # ── work that carries on by itself ──────────────────────────────────────
    Limit("thread.woken_turns", 40, "turns",
          "Turns a thread runs on its own since you last wrote in it",
          "The Host can still report and ask you, but starts no new work until you write.",
          "background", env="CSERIES_WOKEN_TURNS_MAX"),
    Limit("thread.research_rounds", 15, "rounds",
          "Research rounds a thread runs on its own since you last wrote in it",
          "The Host reports what it found and asks before reading further.",
          "background", env="CSERIES_RESEARCH_ROUNDS_MAX"),
    Limit("thread.watch_wakes", 500, "turns",
          "Turns a watched tool call may wake since you last wrote",
          "The Host can still report and ask you, but starts no new work until you write.",
          "background", env="CSERIES_WATCH_WAKES_MAX"),
    Limit("thread.failed_wakes", 3, "failed runs",
          "Failed runs in a row in one thread that still wake the Host",
          "Further failures are written in the thread but start no turn until you write.",
          "background"),
    Limit("task_run.stall_turns", 3, "turns",
          "Turns in a row a task run may make no progress",
          "The run stops, reports what is still open, and keeps everything it recorded.",
          "background"),
    Limit("task_run.skipped_turns", 3, "turns",
          "Turns in a row the platform may fail to start for a task run",
          "The run stops, the thread says why, and everything it recorded is kept.",
          "background"),
    # ── builds ──────────────────────────────────────────────────────────────
    Limit("build.auto_repairs", 2, "repairs",
          "Repairs a build phase starts by itself when nobody is in the conversation",
          "No further repair starts; the Host asks you instead. The build and its worktree are kept.",
          "builds"),
    Limit("app.auto_repairs", 2, "repairs", "Repairs started by themselves after an app's proof fails",
          "No further repair starts; the Host asks you, with the repair it would run.",
          "builds"),
    Limit("build.chain_phases", 32, "phases", "Phases a project advances through by itself in one go",
          "The chain stops; the remaining phases wait, untouched.",
          "builds"),
    Limit("build.model_wait_max_minutes", 240, "minutes",
          "The longest a build may wait on a model that is still thinking",
          "A build asking for longer gets this; a build that waits this long is stopped with "
          "its transcript, diff and worktree kept.",
          "builds"),
    Limit("build.idle_minutes", 10, "minutes",
          "How long a build may show no output, CPU or disk activity",
          "The build is stopped as stuck, with its transcript, diff and worktree kept.",
          "builds", env="HARNESS_IDLE_MINUTES",
          no_off=("a build pod always has an idle window (it reads 0 as its own default), "
                  "and it stops only a build showing no output, CPU or disk activity at all; "
                  "set a longer one instead")),
    Limit("build.progress_minutes", 120, "minutes",
          "How long a build may go without a model or tool step",
          "The build is stopped as stuck, with its transcript, diff and worktree kept.",
          "builds", env="HARNESS_PROGRESS_MINUTES"),
    Limit("build.tool_step_minutes", 120, "minutes",
          "How long one step of a build may run when it asks for no time of its own",
          "The step is stopped as stuck, with the build's transcript, diff and worktree kept.",
          "builds", env="HARNESS_TOOL_STEP_MINUTES"),
    Limit("build.tool_step_max_minutes", 600, "minutes", "The longest one step of a build may run",
          "The step is stopped as stuck, with the build's transcript, diff and worktree kept.",
          "builds", env="HARNESS_TOOL_STEP_MAX_MINUTES"),
    # ── other work ──────────────────────────────────────────────────────────
    Limit("browser_agent.run_minutes", 25, "minutes", "The longest one browser agent run may take",
          "The run stops, its record is kept, and the Host is told.",
          "data", env="BROWSER_AGENT_RUN_TIMEOUT_S", env_scale=60.0),
    Limit("browser_agent.pause_minutes", 15, "minutes",
          "How long a browser agent waits for you (a code, a confirmation)",
          "The run stops, and you are told it stopped waiting.",
          "data", env="BROWSER_AGENT_PAUSE_TIMEOUT_S", env_scale=60.0),
    Limit("host_data.query_max_minutes", 15, "minutes",
          "The longest one query on the Host's own data may run",
          "The query is cancelled and rolled back; the Host is told and can run it again.",
          "data"),
)}

GROUPS = (("spend", "Spending"), ("turns", "One turn"),
          ("background", "Work that carries on by itself"), ("builds", "Builds"),
          ("data", "Other work"))


# ── the values in force ─────────────────────────────────────────────────────
# One process (server/launch.py runs one uvicorn), so the person's values are
# held here between reads: load() fills it at boot and after every write, and
# a caller with no behavioral_params in hand reads it. A caller that has the
# Host's behavioral_params passes them, and those win.
_SNAPSHOT: dict[str, Any] = {}
# monotonic() of the last load; None until the first. monotonic() counts from
# boot, so a 0.0 start read as fresh for the first 30 s after a machine booted.
_LOADED_AT: Optional[float] = None
_STALE_S = 30.0


def _stored(name: str, bp: Optional[dict]) -> Any:
    lim = LIMITS[name]
    if not isinstance(bp, dict):
        return _SNAPSHOT.get(name)
    if lim.bp_key and bp.get(lim.bp_key) is not None:
        n = _num(bp.get(lim.bp_key))
        if not (lim.off_key and (n is None or n <= 0)):
            return bp.get(lim.bp_key)
    if lim.off_key:
        off = _num(bp.get(lim.off_key))
        return 0 if off is not None and off <= 0 else None
    lims = bp.get(LIMITS_KEY)
    return lims.get(name) if isinstance(lims, dict) else None


def _num(raw: Any) -> Optional[float]:
    if raw is None or raw == "" or isinstance(raw, bool):
        return None
    try:
        n = float(raw)
    except (TypeError, ValueError):
        return None
    if n != n or n in (float("inf"), float("-inf")):
        return None
    return n


def value(name: str, bp: Optional[dict] = None) -> Optional[float]:
    """The limit in force, in its unit, or None for "no limit".

    The person's value first, then the operator's env override (kept so a pod
    that sets one is unchanged), then the coded default. A value of 0 or less
    is "no limit". Anything unreadable falls through to the next source: a bad
    value never fails the work it limits."""
    lim = LIMITS[name]
    n = _num(_stored(name, bp))
    if n is None and lim.env:
        e = _num(os.environ.get(lim.env))
        n = None if e is None else (e / lim.env_scale if e > 0 else e)
    if n is None:
        n = lim.default
    if n is None or n <= 0:
        return None
    return float(int(n)) if lim.integer else float(n)


def person_or(name: str, fallback: Any, bp: Optional[dict] = None, *, off: Any = 10 ** 9) -> Any:
    """The person's value for `name` in its unit if they set one (`off` for
    their "no limit"), else `fallback`: the enforcing module's own constant,
    which already carries its env override and is what its tests patch. The
    registry's default and that constant are pinned equal by
    scripts/test_user_limits.py, so the two never tell different stories."""
    n = _num(_stored(name, bp))
    if n is None:
        return fallback
    if n <= 0:
        return off
    return int(n) if LIMITS[name].integer else n


def person_seconds_or(name: str, fallback_s: Any, bp: Optional[dict] = None, *,
                      off: Any = None) -> Any:
    """person_or() for a time limit, in seconds."""
    v = person_or(name, None, bp, off=None)
    if v is None:
        return off if _num(_stored(name, bp)) is not None else fallback_s
    unit = LIMITS[name].unit
    return v * 3600.0 if unit == "hours" else v * 60.0 if unit == "minutes" else float(v)


def int_value(name: str, bp: Optional[dict] = None, *, off: int = 10 ** 9) -> int:
    """value() as an int for a counter; `off` (never reached) for no limit."""
    v = value(name, bp)
    return off if v is None else int(v)


def seconds(name: str, bp: Optional[dict] = None, *,
            off: Optional[float] = None) -> Optional[float]:
    """A time limit in seconds (its unit is minutes or hours); `off` for no limit."""
    v = value(name, bp)
    if v is None:
        return off
    unit = LIMITS[name].unit
    return v * 3600.0 if unit == "hours" else v * 60.0 if unit == "minutes" else v


def source(name: str, bp: Optional[dict] = None) -> str:
    """Who set the value in force: 'you', 'operator' (env) or 'default'."""
    lim = LIMITS[name]
    if _num(_stored(name, bp)) is not None:
        return "you"
    if lim.env and _num(os.environ.get(lim.env)) is not None:
        return "operator"
    return "default"


def shown(name: str, v: Optional[float]) -> str:
    lim = LIMITS[name]
    if v is None:
        return "no limit"
    if lim.unit == "dollars":
        return f"${v:.2f}"
    n = int(v) if float(v).is_integer() else v
    return f"{n} {lim.unit}"


DERIVED = "derived"   # state(): the default is worked out, not a number


def state(name: str, bp: Optional[dict] = None) -> Any:
    """value(), except a derived default in force reads DERIVED rather than
    None, so "no limit" and "the built-in figure" are never the same thing."""
    v = value(name, bp)
    if v is None and LIMITS[name].default is None and _num(_stored(name, bp)) is None:
        return DERIVED
    return v


def shown_state(name: str, s: Any) -> str:
    if s == DERIVED:
        return LIMITS[name].derived or "the built-in figure"
    return shown(name, s)


def hit_line(name: str, bp: Optional[dict] = None) -> str:
    """The sentence every enforcing site uses when this limit stops work, so
    the Host and the person always see the same name, value and way on."""
    lim = LIMITS[name]
    return (f"Stopped by the limit `{name}` ({lim.label[0].lower() + lim.label[1:]}: "
            f"{shown_state(name, state(name, bp))}). It is the person's setting, in "
            f"Settings > Limits; the Host can propose a new value with limits(op='propose').")


def status(bp: Optional[dict] = None, hits: Optional[dict] = None) -> list[dict]:
    """Every limit as the Host and the Limits page read it."""
    out = []
    for lim in LIMITS.values():
        v = value(lim.name, bp)
        row = {"name": lim.name, "group": lim.group, "label": lim.label, "unit": lim.unit,
               "value": v, "shown": shown_state(lim.name, state(lim.name, bp)),
               "set_by": source(lim.name, bp),
               "default": lim.default,
               "default_shown": (lim.derived if lim.default is None
                                 else shown(lim.name, lim.default)),
               "on_hit": lim.on_hit}
        if hits and lim.name in hits:
            row["last_hit"] = hits[lim.name]
        out.append(row)
    return out


def check_new_value(name: str, raw: Any) -> tuple[Any, Optional[str]]:
    """(value to store, None) or (None, why). A stored None means "back to the
    default"; 0 means "no limit"."""
    if name not in LIMITS:
        return None, f"no limit named {name!r}; limits(op='status') lists them"
    if raw is None or raw == "" or raw == "default":
        return None, None
    lim = LIMITS[name]
    n = _num(raw)
    if n is None or n < 0:
        return None, f"{name} takes a number of {lim.unit} (0 for no limit), got {raw!r}"
    if lim.integer and not float(n).is_integer():
        return None, f"{name} takes a whole number of {lim.unit}, got {raw!r}"
    if n == 0 and lim.no_off:
        return None, f"{name} cannot be 0 (no limit): {lim.no_off}"
    return (int(n) if lim.integer else round(n, 4)), None


def in_force_after(name: str, stored: Any) -> Any:
    """What state() would return once `stored` is written. Writing None puts
    the coded default back, and the operator's env with it."""
    lim = LIMITS[name]
    if stored is None:
        if lim.default is None:
            return DERIVED
        e = _num(os.environ.get(lim.env)) if lim.env else None
        n = (e / lim.env_scale if e > 0 else e) if e is not None else lim.default
    else:
        n = _num(stored)
    if n is None or n <= 0:
        return None
    return float(int(n)) if lim.integer else float(n)


# ── reading and writing the store ───────────────────────────────────────────

async def read_bp(pool, entity_id: int) -> dict:
    raw = await pool.fetchval("SELECT behavioral_params FROM entity_registry WHERE id = $1",
                              int(entity_id))
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            raw = {}
    return raw if isinstance(raw, dict) else {}


async def load(pool, entity_id: Optional[int]) -> dict:
    """Refresh the held values from the database. Never raises."""
    global _SNAPSHOT, _LOADED_AT
    if pool is None or entity_id is None:
        return _SNAPSHOT
    try:
        bp = await read_bp(pool, entity_id)
    except Exception as e:  # noqa: BLE001
        log.warning("limits not loaded (the last values stay): %s", e)
        return _SNAPSHOT
    _SNAPSHOT = {n: v for n in LIMITS if (v := _stored(n, bp)) is not None}
    _LOADED_AT = time.monotonic()
    return _SNAPSHOT


async def fresh(pool, entity_id: Optional[int]) -> None:
    """load() when the held values are older than _STALE_S, for loops that run
    outside a turn and have no behavioral_params of their own."""
    if _LOADED_AT is None or time.monotonic() - _LOADED_AT > _STALE_S:
        await load(pool, entity_id)


async def write(pool, entity_id: int, name: str, raw: Any) -> dict:
    """Store the person's value (None: back to the default). The callers are
    the person's route and the apply of a card the person decided, never a
    Host tool. Each statement edits only its own key, so a concurrent writer
    of another key is not undone."""
    stored, why = check_new_value(name, raw)
    if why:
        return {"ok": False, "error": why}
    lim = LIMITS[name]
    before = state(name, await read_bp(pool, entity_id))
    if lim.off_key:
        # The dollar figure and its off switch move together: a number sets
        # the figure and clears the switch, 0 clears the figure and turns the
        # built-in one off, None (default) clears both.
        figure = stored if stored else None
        off = 0 if stored == 0 else None
        await pool.execute(
            "UPDATE entity_registry SET behavioral_params = (COALESCE(behavioral_params, "
            "'{}'::jsonb) - $2::text - $3::text) || jsonb_strip_nulls(jsonb_build_object("
            "$2::text, $4::jsonb, $3::text, $5::jsonb)) WHERE id = $1",
            int(entity_id), lim.bp_key, lim.off_key,
            json.dumps(figure), json.dumps(off))
    elif lim.bp_key:
        if stored is None:
            await pool.execute(
                "UPDATE entity_registry SET behavioral_params = "
                "COALESCE(behavioral_params, '{}'::jsonb) - $2::text WHERE id = $1",
                int(entity_id), lim.bp_key)
        else:
            await pool.execute(
                "UPDATE entity_registry SET behavioral_params = COALESCE(behavioral_params, "
                "'{}'::jsonb) || jsonb_build_object($2::text, $3::jsonb) WHERE id = $1",
                int(entity_id), lim.bp_key, json.dumps(stored))
    elif stored is None:
        await pool.execute(
            "UPDATE entity_registry SET behavioral_params = COALESCE(behavioral_params, "
            "'{}'::jsonb) || jsonb_build_object($2::text, COALESCE(behavioral_params->$2::text, "
            "'{}'::jsonb) - $3::text) WHERE id = $1",
            int(entity_id), LIMITS_KEY, name)
    else:
        await pool.execute(
            "UPDATE entity_registry SET behavioral_params = COALESCE(behavioral_params, "
            "'{}'::jsonb) || jsonb_build_object($2::text, COALESCE(behavioral_params->$2::text, "
            "'{}'::jsonb) || jsonb_build_object($3::text, $4::jsonb)) WHERE id = $1",
            int(entity_id), LIMITS_KEY, name, json.dumps(stored))
    await load(pool, entity_id)
    after = state(name, None)
    try:
        from server import host_events
        await host_events.emit(pool, entity_id, "limit_changed", {"name": name})
    except Exception as e:  # noqa: BLE001 — the setting already stands
        log.debug("limit_changed not emitted: %s", e)
    return {"ok": True, "name": name, "before": shown_state(name, before),
            "after": shown_state(name, after), "set_by": source(name, None)}


async def record_hit(pool, entity_id: Optional[int], name: str, what: str,
                     thread_key: Optional[str] = None) -> None:
    """Note that `name` stopped `what`, for the Limits page's "last hit".
    Never raises."""
    if pool is None or entity_id is None or name not in LIMITS:
        return
    try:
        await pool.execute(
            "INSERT INTO limit_hits (entity_id, name, value, what, thread_key) "
            "VALUES ($1, $2, $3, $4, $5)",
            int(entity_id), name, value(name, None), str(what or "")[:300], thread_key)
    except Exception as e:  # noqa: BLE001
        log.debug("limit hit not recorded (%s): %s", name, e)


def note_hit(entity_id: Optional[int], name: str, what: str,
             thread_key: Optional[str] = None) -> None:
    """record_hit() from code that cannot await it there: fire and forget on
    the running loop, with the kernel's pool. Never raises."""
    try:
        import asyncio
        from server import brain_service
        pool = brain_service.get_pool()
        asyncio.get_running_loop().create_task(
            record_hit(pool, entity_id, name, what, thread_key))
    except Exception as e:  # noqa: BLE001
        log.debug("limit hit not queued (%s): %s", name, e)


async def last_hits(pool, entity_id: int) -> dict:
    try:
        rows = await pool.fetch(
            "SELECT DISTINCT ON (name) name, at, what, value FROM limit_hits "
            "WHERE entity_id = $1 ORDER BY name, at DESC", int(entity_id))
    except Exception:  # noqa: BLE001
        return {}
    return {r["name"]: {"at": r["at"].isoformat() if r["at"] else None, "what": r["what"],
                        "value": r["value"]} for r in rows}


# ── the Host's proposal, and the person's click ─────────────────────────────

async def propose(pool, entity_id: int, name: str, raw: Any, reason: str = "") -> dict:
    """File the card that asks the person to change a limit. The card is the
    whole effect: nothing changes until the person picks 'apply'."""
    stored, why = check_new_value(name, raw)
    if why:
        return {"status": "error", "error": why}
    lim = LIMITS[name]
    current = state(name, await read_bp(pool, entity_id))
    proposed = in_force_after(name, stored)
    if proposed == current:
        return {"status": "error", "error": f"{name} is already {shown_state(name, current)}"}
    open_id = await pool.fetchval(
        "SELECT id FROM entity_tickets WHERE entity_id = $1 AND status = 'open' "
        "AND metadata->>'kind' = $2 AND metadata->$3::text->>'name' = $4 "
        "ORDER BY id DESC LIMIT 1",
        int(entity_id), PROPOSAL_KIND, PAYLOAD_KEY, name)
    if open_id:
        return {"status": "error",
                "error": f"a card for {name} is already waiting on the person (ticket {open_id})"}
    why_txt = " ".join(str(reason or "").split())[:REASON_MAX]
    sitrep = (f"Change the limit on {lim.label[0].lower() + lim.label[1:]} from "
              f"{shown_state(name, current)} to {shown_state(name, proposed)}?"
              + (f" {why_txt}" if why_txt else ""))
    options = [
        {"id": OPT_APPLY, "label": f"Set it to {shown_state(name, proposed)}",
         "consequence": f"From now on the limit is {shown_state(name, proposed)}. You can change "
                        "it again in Settings > Limits.", "recommend": True},
        {"id": OPT_KEEP, "label": "Keep it as it is",
         "consequence": f"It stays {shown_state(name, current)}. {lim.on_hit}"},
    ]
    from server.routes import tickets as _tk
    t = await _tk.create_ticket_service(
        pool, entity_id, source="runtime", sitrep=sitrep, options=options,
        context={"reason": why_txt, "limit": name}, priority=60,
        metadata={"kind": PROPOSAL_KIND,
                  PAYLOAD_KEY: {"name": name, "value": stored, "base": current},
                  "user_only": True, "no_pattern": True})
    tid = t.get("id") or t.get("ticket_id")
    return {"status": "ok", "result": {
        "ticket_id": tid, "ask": {"ticket_id": tid, "sitrep": sitrep, "options": options},
        "note": ("The card is up. Only the person's click changes the limit. To pick the "
                 "stopped work up again once it changes, schedule_task with wake_on="
                 f"{{\"event\": \"limit_changed\", \"match\": {{\"name\": \"{name}\"}}}}."),
    }}


async def apply_proposal(pool, entity_id: int, metadata: dict, *, option_id: str,
                         ticket_id: int) -> dict:
    """The person decided a limit card. 'apply' writes the value, unless the
    limit changed after the card was filed: then nothing is written and the
    result says so, because the person decided on what the card showed."""
    p = (metadata or {}).get(PAYLOAD_KEY) or {}
    name = p.get("name")
    if name not in LIMITS:
        return {"applied": False, "why": "unknown limit"}
    if option_id != OPT_APPLY:
        return {"applied": False, "why": "kept as it is"}
    current = state(name, await read_bp(pool, entity_id))
    if current != p.get("base"):
        return {"applied": False,
                "why": f"{name} changed after the card was filed (it is now "
                       f"{shown_state(name, current)}), so nothing was written"}
    res = await write(pool, entity_id, name, p.get("value"))
    log.info("ticket %s: limit %s %s", ticket_id, name,
             f"{res.get('before')} -> {res.get('after')}" if res.get("ok") else res.get("error"))
    return {"applied": bool(res.get("ok")), **res}
