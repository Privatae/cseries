"""The brain tools: their handlers and the helpers only they use.

Moved out of runtime/tools.py, which re-exports every name defined here. A tool's
category in the tool registry (public_tool_registry `_meta.category`) names the module
its handler lives in."""

from mastercode.runtime.tool_call_ledger import refused as _refused, refused_if_caller as _refused_if_caller


async def _exec_personality_state(args: dict) -> dict:
    """Read the host's OWN personality settings: the ten slider weights the
    person set, personality_mode, skill weights and the master directive.
    Calls the brain SERVICE directly (no HTTP shim), read-only over the host's
    own entity_registry row: the same shape as GET /api/brain/personality.

    The weights are the person's. Nothing moves them but the person: the
    sliders in Settings → Response Shaping, or asking in words. There is no
    drift, no envelope and no reset any more, so there is nothing here to
    check."""
    try:
        from server import brain_service
        from server.routes import brain as _brain
        pool = brain_service.get_pool()
        if pool is None:
            return {"status": "error", "result": "personality_state failed: pool unavailable"}
        eid = brain_service._entity_id
        res = dict(await _brain.get_personality_service(pool, eid))
        res["note"] = (
            "These weights are what the user set and they never move on their own. "
            "They change only when the user moves the sliders in Settings → Response "
            "Shaping or asks you in words. What you learn from the user lands in "
            "your memory (preferences and rules), never in these weights."
        )
        return {"status": "ok", "result": res}
    except Exception as e:
        return _refused_if_caller(e, f"personality_state failed: {getattr(e, 'detail', None) or e}")


async def _exec_explanation_level(args: dict) -> dict:
    """The user's explanation level — read, or write when the
    user asked in words. Both go through the SAME service that Settings →
    Response Shaping's PUT /api/brain/personality/explanation uses, so the
    user is still the one moving their setting. Never inferred."""
    try:
        from server import brain_service
        from server.routes import brain as _brain
        from mastercode.runtime.prompt_builder import EXPLANATION_LEVELS
        pool = brain_service.get_pool()
        if pool is None:
            return {"status": "error", "result": "explanation_level failed: pool unavailable"}
        eid = brain_service._entity_id
        op = (args or {}).get("op") or "get"
        if op == "set":
            level = (args or {}).get("level")
            res = await _brain.set_explanation_level_service(pool, eid, level)
            if res.get("error"):
                return {"status": "error", "result": f"explanation_level failed: {res['error']}"}
            label = EXPLANATION_LEVELS[res["level"]][0]
            return {"status": "ok", "result": {
                "level": res["level"], "label": label,
                "note": "Set at the user's request. Tell them the new position in plain words; "
                        "it changes again only when they ask or move it in Settings.",
            }}
        res = await _brain.get_personality_service(pool, eid)
        lvl = res.get("explanation_level") or "new"
        positions = {}
        for key in EXPLANATION_LEVELS:
            positions[key] = EXPLANATION_LEVELS[key][0]
        return {"status": "ok", "result": {
            "level": lvl, "label": EXPLANATION_LEVELS[lvl][0],
            "positions": positions,
            "note": "A user-owned setting. It is not inferred and you never change it on your own.",
        }}
    except Exception as e:
        return _refused_if_caller(e, f"explanation_level failed: {getattr(e, 'detail', None) or e}")


# _exec_memory_timeline retired: it browsed entity_journal, whose writer was
# disabled ("Journal feed DEPRECATED", session.py), so the tool advertised a
# feed nothing had written to for three months. The table and its routes are
# gone too (migrations/v237_dead_tables.sql).


async def _exec_entity_tasks(args: dict) -> dict:
    """View + manage the host's OWN task queue: op='list' (default) lists tasks +
    per-status counts (optional status filter, limit); op='cancel'/'delete' acts on
    one task (task_id); op='purge' cancels-or-deletes a whole status bucket (status,
    action). Calls the brain SERVICE directly (no HTTP shim) — all scoped to the
    host's own entity_tasks (task_id + entity_id). Shapes match the /api/brain/
    manage/tasks routes."""
    a = args or {}
    try:
        from server import brain_service
        from server.routes import brain as _brain
        pool = brain_service.get_pool()
        if pool is None:
            return {"status": "error", "result": "entity_tasks failed: pool unavailable"}
        eid = brain_service._entity_id
        op = (a.get("op") or "list").strip().lower()
        if op in ("list", "view", "tasks"):
            limit = min(int(a.get("limit") or 100), 500)
            # The project filter (a call with project_id must not be refused
            # as an unknown field). None = every task.
            pid = a.get("project_id")
            if pid is not None:
                try:
                    pid = int(pid)
                except (TypeError, ValueError):
                    return _refused(f"project_id must be a project's number, not {pid!r}")
            res = await _brain.list_tasks_service(pool, eid, status=a.get("status"), limit=limit,
                                                  offset=max(0, int(a.get("offset") or 0)),
                                                  project_id=pid)
        elif op == "cancel":
            tid = a.get("task_id")
            if tid is None:
                return _refused("task_id required for op='cancel'")
            res = await _brain.cancel_task_service(pool, eid, int(tid),
                                                   actor="entity_tasks_tool")
        elif op == "delete":
            tid = a.get("task_id")
            if tid is None:
                return _refused("task_id required for op='delete'")
            res = await _brain.delete_task_service(pool, eid, int(tid))
        elif op == "purge":
            res = await _brain.purge_tasks_service(
                pool, eid,
                status=(a.get("status") or "queued"),
                action=(a.get("action") or "cancel"),
                actor="entity_tasks_tool")
        else:
            return _refused(f"unknown op '{op}' — use list|cancel|delete|purge")
        return {"status": "ok", "result": res}
    except Exception as e:
        return _refused_if_caller(e, f"entity_tasks failed: {getattr(e, 'detail', None) or e}")


async def _exec_turn_type(args: dict, entity_id: int, pool, *, thread_id=None) -> dict:
    """The Host's turn types.
    list / show read; draft writes an INERT row and puts ONE approval card up;
    change / retire ask to change or retire an active type. The card's words
    are the platform's, from the validated profile; only the person's click
    applies anything (runtime/turn_type_approval.apply_proposal, called by the
    ticket-decide route). There is no op that activates, widens or retires.

    op='root' opens or continues the Host's root thread with `request`
    (runtime/root_turn.open_root): the built-in type root commands run in,
    as a turn of its own the person sees in the thread list."""
    from mastercode.runtime import turn_type_approval as _tta
    a = args or {}
    op = str(a.get("op") or "list").strip().lower()
    reason = str(a.get("reason") or "")
    try:
        if op == "root":
            from mastercode.runtime import root_turn as _root
            return await _root.open_root(pool, entity_id, str(a.get("request") or ""),
                                         from_thread_id=thread_id)
        if op == "list":
            return {"status": "ok", "result": {"types": await _tta.list_types(pool, entity_id)}}
        if op == "show":
            t = await _tta.show_type(pool, entity_id, str(a.get("type_id") or ""))
            return ({"status": "ok", "result": t} if t else
                    {"status": "error", "result": f"no turn type {a.get('type_id')!r}"})
        if op == "draft":
            return await _tta.draft(pool, entity_id, str(a.get("name") or ""), a.get("spec"), reason)
        if op == "change":
            return await _tta.request_change(pool, entity_id, str(a.get("type_id") or ""),
                                             a.get("spec"), reason)
        if op == "retire":
            return await _tta.request_retire(pool, entity_id, str(a.get("type_id") or ""), reason)
    except Exception as e:  # noqa: BLE001
        return _refused_if_caller(e, f"turn_type failed: {getattr(e, 'detail', None) or e}")
    return _refused("op must be list, show, draft, change, retire or root")
