"""runtime/scheduled_approval.py — the person approves recurring work that
spends. Work that runs with nobody watching is spending the person did not
see, so the Host may not start recurring model work by itself.

A RECURRING task on the prompt lane runs a model turn on every fire, with
nobody watching, for as long as it stands. The Host can no longer start one
by itself: schedule_task validates it exactly as before, then files this card
instead of setting it. The person's 'apply' does what the create would have
done, in the same order (the plan into the thread, then the row); 'keep'
sets nothing.

NOT gated, on purpose: a one-time task (a follow-up the Host sets in a
conversation, a check in ten minutes), and the op lane, which runs no model
and spends nothing. Both stay as they were.

The card is locked to the person (`user_only`, `no_pattern`): the Host cannot
decide it, and no "and similar" pattern can approve the next one for them.
"""
from __future__ import annotations

import logging
from typing import Optional

log = logging.getLogger("mastercode.scheduled_approval")

PROPOSAL_KIND = "recurring_task_proposal"   # = server/routes/tickets.RECURRING_TASK_PROPOSAL_KIND
PAYLOAD_KEY = "recurring_task"
OPT_APPLY = "apply"
OPT_KEEP = "keep"


_NEST_LIMIT = 8


def model_turn(op_tool: Optional[str], op_args=None, *, _depth: int = 0) -> Optional[dict]:
    """The create args of the model turn a fire ends in, or None when it runs
    no model. A prompt-lane task is its own ({} at the top). schedule_task is
    itself an unattended op (direct_ops.UNATTENDED_OP_TOOLS), so an op-lane
    task whose op is schedule_task create sets a task on every fire, and is
    followed down to what that one runs: a recurring op that sets a one-time
    prompt task runs a model turn on every fire. Past the nesting limit it is
    counted as a model turn."""
    tool = (op_tool or "").strip()
    if not tool:
        return {}
    a0 = op_args if isinstance(op_args, dict) else {}
    # bug_review op='pending' with wake=true starts a validation turn (a
    # model) whenever reports are waiting (runtime/bug_review.run): on a
    # recurrence, that is repeating model work.
    if tool == "bug_review":
        wakes = (str(a0.get("op") or "").strip() == "pending" and bool(a0.get("wake")))
        return {"prompt": "the bug review validation turn"} if wakes else None
    if tool != "schedule_task":
        return None
    a = op_args if isinstance(op_args, dict) else {}
    if str(a.get("op") or "list").strip() != "create":
        return None
    if _depth >= _NEST_LIMIT:
        return a
    inner = model_turn(a.get("op_tool"), a.get("op_args"), _depth=_depth + 1)
    return a if inner == {} else inner


def needs_approval(*, recurrence: Optional[str], op_tool: Optional[str],
                   op_args=None) -> bool:
    """A recurring task that runs a model turn, directly or through the
    schedule_task op it fires. Everything else is set as before."""
    return bool(recurrence) and model_turn(op_tool, op_args) is not None


async def propose(pool, entity_id: int, *, title: str, recurrence: str, fire_at: str,
                  plan_msg: str, append: dict, origin: Optional[dict],
                  turns_per_fire: int, turns_per_day: Optional[int],
                  budget_usd: Optional[float]) -> dict:
    """File the card. `append` is the exact calendar row the create would have
    written, and `plan_msg` the exact plan line; apply writes both."""
    bounds = [f"each run is {turns_per_fire} model turn{'s' if turns_per_fire != 1 else ''}"]
    # A person with no limit reads as user_limits.person_or's `off` (10**9),
    # which is no number to show them.
    if turns_per_day and turns_per_day < 10 ** 9:
        bounds.append(f"at most {turns_per_day} model turns a day")
    if budget_usd:
        bounds.append(f"it stops once it has spent ${budget_usd:.2f}")
    bounds.append("each run's cost is reported in the thread")
    sitrep = (f"Your Host wants to run **{title}** by itself, repeating {recurrence}, "
              f"starting {fire_at}: " + "; ".join(bounds) + ". It runs only if you let it.")
    options = [
        {"id": OPT_APPLY, "label": "Let it run",
         "consequence": ("The task is set and runs on its schedule until you stop it in the "
                         "Calendar or Agents app."), "recommend": True},
        {"id": OPT_KEEP, "label": "Don't",
         "consequence": "Nothing is scheduled; your Host is told."},
    ]
    from server.routes import tickets as _tk
    t = await _tk.create_ticket_service(
        pool, entity_id, source="runtime", sitrep=sitrep, options=options,
        context={"plan": plan_msg[:4000], "limit": "scheduled.turns_per_day"}, priority=60,
        metadata={"kind": PROPOSAL_KIND,
                  PAYLOAD_KEY: {"title": title, "append": append, "plan_msg": plan_msg,
                                "origin": origin or None},
                  "user_only": True, "no_pattern": True})
    tid = t.get("id") or t.get("ticket_id")
    return {"status": "ok", "result": {
        "scheduled": None, "awaiting_approval": True, "ticket_id": tid,
        "ask": {"ticket_id": tid, "sitrep": sitrep, "options": options},
        "note": ("Not set yet: a recurring task that runs a model needs the person's "
                 "approval, and the card is up. Tell them what it does and what it may "
                 "spend. Their click sets it, with its plan in this thread; you cannot "
                 "decide the card.")}}


async def apply_proposal(pool, entity_id: int, metadata: dict, *, option_id: str,
                         ticket_id: int) -> dict:
    """The person decided the card. 'apply' writes the plan into the thread,
    then the row, as schedule_task's create does; 'keep' writes nothing."""
    p = (metadata or {}).get(PAYLOAD_KEY) or {}
    if option_id != OPT_APPLY:
        return {"applied": False, "why": "not set: the person chose not to run it"}
    append = p.get("append") if isinstance(p.get("append"), dict) else None
    if not append:
        return {"applied": False, "why": "the card carries no task"}
    # The thread it was asked in may have been deleted since; schedule_task
    # refuses a create there (the delete cancelled what was set in it), so
    # the click on a card filed before the delete sets nothing either.
    _thread = ((p.get("origin") or {}) if isinstance(p.get("origin"), dict) else {}).get("thread_id")
    if _thread:
        from mastercode.circles_threads import live_thread_id
        if await live_thread_id(pool, int(entity_id), _thread) is None:
            return {"applied": False,
                    "why": "the thread it was asked in was deleted, so nothing was set"}
    from mastercode.brain.offline_task_runner import post_to_origin_thread
    from server.bridge.memory_handlers import handle_calendar_append
    event_uuid = str(append.get("event_id") or "")
    posted = await post_to_origin_thread(
        pool, int(entity_id), p.get("origin"), str(p.get("plan_msg") or ""),
        extra_tags={"scheduled_event_id": event_uuid, "scheduled_plan": True},
        fallback_session=f"scheduled_task:{event_uuid}")
    if not posted:
        return {"applied": False,
                "why": "the plan could not be written into the thread, so nothing was set"}
    env = await handle_calendar_append(append)
    if not env.get("ok"):
        why = env.get("error") or "the task could not be set"
        await post_to_origin_thread(
            pool, int(entity_id), p.get("origin"),
            f"(Not set after all — **{p.get('title')}**: {why}.)",
            extra_tags={"scheduled_event_id": event_uuid, "scheduled_plan_withdrawn": True},
            fallback_session=f"scheduled_task:{event_uuid}")
        return {"applied": False, "why": why}
    log.info("ticket %s: recurring task %r set (%s)", ticket_id, p.get("title"), env.get("event_id"))
    return {"applied": True, "event_id": env.get("event_id"), "fire_at": env.get("event_at")}


# ── a runbook trigger: standing model work set off by events ────────────────
# A reach-out trigger on a RUNBOOK channel starts a model turn every time a
# matching Inbox message arrives, and some arrive on a clock (the weekly
# changelog, a recurring inbox_send op). Like a recurring task, it is the
# person's to start: reachout op='add_trigger' on a runbook channel files this
# card, and only their click adds the trigger.

TRIGGER_KIND = "runbook_trigger_proposal"   # = server/routes/tickets.RUNBOOK_TRIGGER_PROPOSAL_KIND
TRIGGER_KEY = "runbook_trigger"


async def propose_trigger(pool, entity_id: int, *, channel, label: str, match_kind: str,
                          match_event, match_jsonb, channel_id: Optional[int] = None) -> dict:
    on = match_kind + (f" / {match_event}" if match_event else "")
    sitrep = (f"Your Host wants the runbook **{channel}** to run by itself each time a "
              f"\"{on}\" message arrives in your Inbox ({label or 'no label'}). Each time "
              "is a model turn, and some of those messages come on a schedule. It runs only "
              "if you let it.")
    options = [
        {"id": OPT_APPLY, "label": "Let it run",
         "consequence": "The trigger is added; remove it any time in the Inbox app's reach-out "
                        "settings or by asking your Host.", "recommend": True},
        {"id": OPT_KEEP, "label": "Don't", "consequence": "No trigger is added."},
    ]
    from server.routes import tickets as _tk
    t = await _tk.create_ticket_service(
        pool, entity_id, source="runtime", sitrep=sitrep, options=options,
        context={"channel": str(channel)}, priority=60,
        metadata={"kind": TRIGGER_KIND,
                  # channel_id pins the click to the channel checked when the
                  # card was filed: a name could name another channel by then.
                  TRIGGER_KEY: {"channel": channel, "channel_id": channel_id,
                                "label": label, "match_kind": match_kind,
                                "match_event": match_event, "match_jsonb": match_jsonb},
                  "user_only": True, "no_pattern": True})
    tid = t.get("id") or t.get("ticket_id")
    return {"status": "ok", "result": {
        "awaiting_approval": True, "ticket_id": tid,
        "ask": {"ticket_id": tid, "sitrep": sitrep, "options": options},
        "note": ("Not added yet: a trigger that runs a runbook needs the person's approval, "
                 "and the card is up. Their click adds it; you cannot decide the card.")}}


async def apply_trigger(pool, entity_id: int, metadata: dict, *, option_id: str,
                        ticket_id: int) -> dict:
    p = (metadata or {}).get(TRIGGER_KEY) or {}
    if option_id != OPT_APPLY:
        return {"applied": False, "why": "not added: the person chose not to"}
    from server.reachout import service as _ro
    try:
        row = await _ro.add_trigger(
            pool, int(entity_id),
            channel=(int(p["channel_id"]) if p.get("channel_id") is not None else p.get("channel")),
            label=p.get("label") or "",
            match_kind=p.get("match_kind") or "", match_event=p.get("match_event"),
            match_jsonb=p.get("match_jsonb"), created_by="host")
    except Exception as e:  # noqa: BLE001 — said on the ticket
        return {"applied": False, "why": str(e)[:300]}
    log.info("ticket %s: runbook trigger added on %r", ticket_id, p.get("channel"))
    return {"applied": True, "trigger_id": dict(row).get("id")}
