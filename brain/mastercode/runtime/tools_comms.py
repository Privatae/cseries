"""The comms tools: their handlers and the helpers only they use.

Moved out of runtime/tools.py, which re-exports every name defined here. A tool's
category in the tool registry (public_tool_registry `_meta.category`) names the module
its handler lives in."""

import json

from mastercode.runtime.tool_call_ledger import refused as _refused, refused_if_caller as _refused_if_caller


async def _exec_inbox_read(args):
    a = args or {}
    message_id = (a.get("message_id") or "").strip()
    if not message_id:
        return _refused("message_id (uuid) required")
    try:
        # Reuse the bridge memory handler verbatim — it is the in-pod
        # service the /api/inbox/read route delegates to. It resolves
        # its own pool + entity_id and scopes the read to this host as
        # recipient, returning an {ok, ...message} envelope. Reading a
        # message also marks it read (UPDATE ... SET read_at).
        from server.bridge.memory_handlers import _entity_id, handle_inbox_read
        # A person's chat is the Host's to read only through the reading
        # switch (server/circles_reading.py visible_sql), the same predicate
        # host_message reads by. Without this, an id the model saw while the
        # switch was on (op='thread' returns message ids) stays readable here
        # after the switch is off or the person cleared the chat. Refused as
        # "not found", the same as an id that is not there.
        from server import brain_service as _bs
        from server.circles_reading import visible_sql
        import uuid as _uuid
        _pool = _bs.get_pool()
        try:
            _uuid.UUID(message_id)
            _valid = True
        except ValueError:
            _valid = False      # handle_inbox_read refuses it in its own words
        if _pool is not None and _valid:
            _closed = await _pool.fetchval(
                "SELECT 1 FROM inbox_messages m WHERE m.message_id = $1::uuid "
                f"AND m.to_entity_id = $2 AND NOT {visible_sql('m', '$2')}",
                message_id, int(_entity_id()))
            if _closed:
                return _refused("not found")
        env = await handle_inbox_read({"message_id": message_id})
        if not env.get("ok", False):
            # An id that is not there or is malformed is the model's input,
            # answered exactly as a closed one above, so the two cannot be told
            # apart. A pool or host-id hiccup (retryable) stays the platform's.
            if not env.get("retryable"):
                return _refused(env.get("error") or "not found")
            return {"status": "error", "result": env.get("error") or "inbox_read failed"}
        env.pop("ok", None)
        return {"status": "ok", "result": env}
    except Exception as e:
        return _refused_if_caller(e, f"inbox_read failed: {getattr(e, 'detail', None) or e}")


async def _exec_circles_manage(args: dict) -> dict:
    """Circles (network communications) op-dispatch for the host.

    One tool, several read/write ops over the host's Circles surface —
    the same module-level helpers the /api/circles routes use (NOT the
    route handlers): self-thread + host<->host DM threads are pod-local
    (entity_memory / inbox_messages), group circles fan out to the bridge.

      list_conversations  -> sidebar payload (self pin + entity DM threads + circles)
      read_thread         -> messages for one thread (self | entity | circle)
      list_circles        -> the host's group circles
      create_circle       -> create a new group circle (write)
      send_circle_message -> post a message into a group circle (write)
    """
    a = args or {}
    op = (a.get("op") or "").strip()
    if not op:
        return _refused("op is required (one of: list_conversations, read_thread, list_circles, create_circle, send_circle_message)")
    try:
        from server import brain_service
        from server.routes import circles as cir
        pool = brain_service.get_pool()
        if pool is None:
            return {"status": "error", "result": "pool unavailable"}
        eid = cir._entity_id()
        if eid is None:
            return {"status": "error", "result": "host not initialized"}

        if op == "list_conversations":
            self_pane = {
                "id": "self", "kind": "self",
                "display_name": await cir._entity_display_name(),
                "unread_count": 0,
            }
            entity_items = await cir._list_entity_threads(eid)
            circles_data = await cir._bridge_call("circles.list", {"entity_id": eid}) or {}
            circle_items = [
                {
                    "id": c.get("id"),
                    "display_name": c.get("circle_name") or c.get("name") or f"circle {c.get('id')}",
                    "unread_count": 0,
                }
                for c in (circles_data.get("circles") or circles_data.get("items") or [])
            ]
            return {"status": "ok", "result": {
                "self": self_pane, "entities": entity_items, "circles": circle_items,
            }}

        if op == "read_thread":
            kind = (a.get("kind") or "").strip()
            thread_id = a.get("thread_id")
            if kind not in ("self", "entity", "circle"):
                return _refused("kind must be one of: self, entity, circle")
            if kind == "self":
                return {"status": "ok", "result": {
                    "messages": await cir._read_self_thread(),
                    "members": [], "policy": {"private": True},
                }}
            if kind == "entity":
                try:
                    peer_id = int(thread_id)
                except (TypeError, ValueError):
                    return _refused("thread_id (the other host's id) is required for kind 'entity' (a host DM thread)")
                return {"status": "ok", "result": await cir._read_entity_thread(peer_id)}
            # circle
            try:
                circle_id = int(thread_id)
            except (TypeError, ValueError):
                return _refused("thread_id (circle id) is required for a circle thread")
            data = await cir._bridge_call("circles.messages", {"circle_id": circle_id, "limit": 200})
            if not data:
                return {"status": "ok", "result": {"messages": [], "members": [], "policy": {}}}
            self_name = await cir._entity_display_name()
            messages = []
            for m in reversed(data.get("messages") or []):
                sender = m.get("sender_entity_id")
                is_self = sender == eid
                messages.append({
                    "author_name": self_name if is_self else (m.get("sender_name") or f"Host {sender}"),
                    "author_kind": "self" if is_self else "entity",
                    "body": m.get("content") or "",
                    "created_at": m.get("created_at"),
                })
            return {"status": "ok", "result": {
                "messages": messages,
                "members": data.get("members", []),
                "policy": data.get("policy", {}),
            }}

        if op == "list_circles":
            circles_data = await cir._bridge_call("circles.list", {"entity_id": eid}) or {}
            items = [
                {
                    "id": c.get("id"),
                    "display_name": c.get("circle_name") or c.get("name") or f"circle {c.get('id')}",
                }
                for c in (circles_data.get("circles") or circles_data.get("items") or [])
            ]
            return {"status": "ok", "result": {"circles": items, "count": len(items)}}

        if op == "create_circle":
            name = (a.get("name") or "").strip()
            if not name:
                return _refused("name is required to create a circle")
            data = await cir._bridge_call("circles.create", {
                "entity_id": eid,
                "name": name[:120],
                "description": (a.get("description") or "")[:500],
            })
            if not data or not data.get("id"):
                return {"status": "error", "result": "bridge couldn't create the circle (platform bridge unavailable)"}
            return {"status": "ok", "result": {
                "id": data.get("id"),
                "display_name": data.get("circle_name") or name,
            }}

        if op == "send_circle_message":
            text = (a.get("body") or "").strip()
            if not text:
                return _refused("body is required to send a circle message")
            try:
                circle_id = int(a.get("circle_id"))
            except (TypeError, ValueError):
                return _refused("circle_id is required to send a circle message")
            data = await cir._bridge_call("circles.message", {
                "circle_id": circle_id, "content": text,
            })
            if not data:
                return {"status": "error", "result": "bridge couldn't deliver the circle message (platform bridge unavailable)"}
            return {"status": "ok", "result": data}

        return _refused(f"unknown op: {op}")
    except Exception as e:
        return _refused_if_caller(e, f"circles_manage failed: {getattr(e, 'detail', None) or e}")


async def _exec_calendar_read(args):
    """Read one calendar event, or (op='runs') its execution history.

    The runs half is a WIRING fix, not a new capability: calendar_event_runs
    has existed since v072, the offline runner writes a row per fire, and both
    `handle_calendar_runs` and GET /api/calendar/{id}/runs already read it —
    but no host tool ever called them, so the host could not see whether its
    own scheduled work was succeeding. Standing ops are created without asking
    the user, which makes 'can anyone see the outcome' load-bearing rather
    than a convenience.
    """
    a = args or {}
    op = (a.get("op") or "read").strip().lower()
    _OPS = {"read", "runs"}
    if op not in _OPS:
        return _refused(f"op must be one of {sorted(_OPS)} (got {op!r})")
    event_id = (a.get("event_id") or "").strip() if isinstance(a.get("event_id"), str) else a.get("event_id")
    if not event_id:
        return _refused("event_id is required")
    try:
        if op == "runs":
            from server.bridge.memory_handlers import handle_calendar_runs
            env = await handle_calendar_runs({"event_id": event_id})
            if not env.get("ok", False):
                return {"status": "error", "result": env.get("error") or "calendar_read runs failed"}
            return {"status": "ok", "result": env}
        from server.bridge.memory_handlers import handle_calendar_read
        env = await handle_calendar_read({"event_id": event_id})
        if not env.get("ok", False):
            return {"status": "error", "result": env.get("error") or "calendar_read failed"}
        return {"status": "ok", "result": env}
    except Exception as e:
        return _refused_if_caller(e, f"calendar_read failed: {getattr(e, 'detail', None) or e}")


async def _exec_task_template(args: dict, *, turn_type: dict | None = None,
                              session_id: str | None = None,
                              interaction_context: str | None = None,
                              thread_id: str | None = None) -> dict:
    """Manage the host's reusable scheduled-task TEMPLATES (runbooks) — a saved
    name + prompt and/or ordered steps + optional default recurrence that
    schedule_task can stamp out (schedule_task op='create' template_id=<id>).
    Strictly entity-scoped (server.brain_service pool + _entity_id; no raw
    secrets). Ops:
      create -> save a template (name + prompt and/or steps[] + optional recurrence)
      list   -> the host's own templates
      delete -> remove one of the host's own templates by id
    """
    a = args or {}
    op = (a.get("op") or "list").strip()
    _OPS = {"create", "list", "delete"}
    if op not in _OPS:
        return _refused(f"op must be one of {sorted(_OPS)} (got {op!r})")
    try:
        from server import brain_service
        pool = brain_service.get_pool()
        if pool is None:
            return {"status": "error", "result": "task_template failed: pool unavailable"}
        eid = brain_service._entity_id

        if op == "create":
            name = (a.get("name") or "").strip()
            if not name:
                return _refused("name is required for op='create'")
            prompt = (a.get("prompt") or "").strip()
            steps = a.get("steps") or []
            if not isinstance(steps, list):
                return _refused("steps must be a list of strings")
            steps = [str(s) for s in steps if str(s).strip()]
            if not prompt and not steps:
                return _refused("provide a prompt and/or at least one step for op='create'")
            recurrence = (a.get("recurrence") or "").strip() or None
            if recurrence:
                from mastercode.brain.entity_clock import _recurrence_seconds
                if _recurrence_seconds(recurrence) is None:
                    return _refused("recurrence must be one of: hourly, daily, weekly, monthly, or every:<seconds>")
            import json as _json
            # A template written by a TYPED turn carries that type into every
            # task later stamped from it (v185). A closed turn writes
            # none.
            _tpl_type = None
            if turn_type is not None:
                if turn_type.get("status") != "active" or not turn_type.get("id"):
                    return _refused("this turn holds nothing, so it cannot write a template")
                _tpl_type = str(turn_type["id"])
            row = await pool.fetchrow(
                "INSERT INTO entity_task_templates (entity_id, name, prompt, steps, recurrence, type_id) "
                "VALUES ($1, $2, $3, $4::jsonb, $5, $6::uuid) RETURNING id, created_at",
                int(eid), name[:200], (prompt or None), _json.dumps(steps), recurrence, _tpl_type)
            # A template is instructions carried forward, so it is written
            # into the thread it was saved in, the way a scheduled task's plan
            # is (turn types: nothing runs later that the thread did not show).
            _ic = (interaction_context or "").strip()
            _proj = None
            if _ic.startswith("project:"):
                try:
                    _proj = int(_ic.split(":", 1)[1])
                except (TypeError, ValueError):
                    _proj = None
            _origin = {k: v for k, v in (
                ("interaction_context", _ic or None), ("session_id", session_id),
                ("project_id", _proj),
                ("thread_id", (str(thread_id).strip() or None) if thread_id else None),
            ) if v is not None}
            from mastercode.brain.offline_task_runner import post_to_origin_thread
            _posted = await post_to_origin_thread(
                pool, int(eid), _origin or None,
                (f"Saved runbook template **{name[:200]}** (#{row['id']})"
                 + (f", repeating {recurrence} when scheduled" if recurrence else "")
                 + ". When it runs I'll pick up from here:\n"
                 + _planned_text(prompt=prompt, steps=steps)),
                extra_tags={"task_template_id": row["id"]},
                fallback_session=f"task_template:{row['id']}")
            if not _posted:
                await pool.execute(
                    "DELETE FROM entity_task_templates WHERE id=$1 AND entity_id=$2",
                    row["id"], int(eid))
                return {"status": "error", "result": (
                    "the template was not saved: it could not be written into this "
                    "thread, and saved instructions are only what the thread shows.")}
            return {"status": "ok", "result": {
                "id": row["id"], "name": name,
                "steps": len(steps), "recurrence": recurrence,
                "created_at": row["created_at"].isoformat() if row["created_at"] else None}}

        if op == "list":
            rows = await pool.fetch(
                "SELECT id, name, prompt, steps, recurrence, created_at "
                "FROM entity_task_templates WHERE entity_id=$1 "
                "ORDER BY created_at DESC LIMIT 200",
                int(eid))
            out = []
            for r in rows:
                steps = r["steps"]
                if isinstance(steps, str):
                    try:
                        steps = json.loads(steps)
                    except Exception:
                        steps = []
                out.append({
                    "id": r["id"], "name": r["name"], "prompt": r["prompt"],
                    "steps": steps or [], "recurrence": r["recurrence"],
                    "created_at": r["created_at"].isoformat() if r["created_at"] else None})
            return {"status": "ok", "result": {"templates": out}}

        # op == "delete"
        tid = a.get("template_id")
        if tid in (None, ""):
            return _refused("template_id is required for op='delete'")
        try:
            tid = int(tid)
        except (TypeError, ValueError):
            return _refused("template_id must be an integer")
        res = await pool.execute(
            "DELETE FROM entity_task_templates WHERE id=$1 AND entity_id=$2",
            tid, int(eid))
        if res.endswith(" 0"):
            return _refused(f"no task_template with id {tid} for this host")
        return {"status": "ok", "result": {"deleted": tid}}
    except Exception as e:
        return _refused_if_caller(e, f"task_template failed: {getattr(e, 'detail', None) or e}")


async def _exec_reachout(args: dict, *, turn_type: dict | None = None) -> dict:
    """How the host reaches its person — channels, triggers, and a test fire.

    A doorbell, not a conversation: a short
    message on a channel the USER owns, carrying a link back into the thread
    that needs them. Every rule lives in server/reachout/service.py, which the
    /api/reachout routes call too — so this tool and the Circles pane can never
    end up enforcing two different things.

    Ops:
    BUILDING a channel is NOT here — it is `reachout_channel`
    (_exec_reachout_channel below), which is deliberately in neither
    DIRECT_OP_TOOLS nor UNATTENDED_OP_TOOLS. Everything left on this tool
    either reads, subscribes an EXISTING channel to an event, fires one the
    user already approved, or removes one; none of them can point a credential
    at a new destination, which is why they are safe to run with nobody there.

    Ops:
      list           channels, triggers, the last 20 fires, and the event
                     vocabulary a trigger can match.
      add_trigger    channel + label + match_kind (+ match_event, match_jsonb).
      test           fire one channel once with a synthetic message.
      remove_channel / remove_trigger — the host may remove what IT created; a
                     row the user created is theirs, and the refusal says where.
    """
    a = args or {}
    op = (a.get("op") or "list").strip()
    _OPS = {"list", "add_trigger", "test", "remove_channel", "remove_trigger"}
    if op not in _OPS:
        return _refused(f"op must be one of {sorted(_OPS)} (got {op!r})")
    try:
        from server import brain_service
        from server.reachout import service as _ro
        pool = brain_service.get_pool()
        if pool is None:
            return {"status": "error", "result": "reachout failed: pool unavailable"}
        eid = brain_service._entity_id

        if op == "list":
            return {"status": "ok", "result": await _ro.list_state(pool, int(eid))}

        # A runbook channel fires a task. From a TYPED turn it may only be
        # set off (test) or subscribed (add_trigger) when it was built under
        # this turn's type — otherwise a typed turn could run an untyped (or
        # another type's) runbook it could not have written.
        if turn_type is not None and op in ("add_trigger", "test") and a.get("channel") not in (None, ""):
            _ch = a.get("channel")
            if isinstance(_ch, int) or (isinstance(_ch, str) and str(_ch).isdigit()):
                _crow = await pool.fetchrow(
                    "SELECT kind, action FROM reachout_channels WHERE id=$1 AND entity_id=$2",
                    int(_ch), int(eid))
            else:
                _crow = await pool.fetchrow(
                    "SELECT kind, action FROM reachout_channels WHERE name=$1 AND entity_id=$2",
                    str(_ch).strip(), int(eid))
            _act = _crow["action"] if _crow is not None else None
            if isinstance(_act, str):
                try:
                    _act = json.loads(_act)
                except ValueError:
                    _act = {}
            _act = _act if isinstance(_act, dict) else {}
            # The transport dispatches on action.type (server/reachout/
            # transports.py TRANSPORTS), so that is what makes it a runbook —
            # not the channel's kind label.
            if _crow is not None and str(_act.get("type") or "").strip() == "runbook":
                # The type the fired task runs under, decided the same way
                # _send_runbook decides it: the channel's, else its template's.
                _act_type = str(_act.get("type_id") or "")
                if not _act_type:
                    try:
                        _tpl_tid = await pool.fetchval(
                            "SELECT type_id FROM entity_task_templates "
                            "WHERE id=$1 AND entity_id=$2",
                            int(_act.get("template_id")), int(eid))
                    except (TypeError, ValueError):
                        _tpl_tid = None
                    _act_type = str(_tpl_tid or "")
                if turn_type.get("status") != "active" or _act_type != str(turn_type.get("id")):
                    return {"status": "denied", "reason": "type_denied", "result": (
                        f"the runbook channel {_ch!r} was not built under this turn's type, "
                        "so a turn of this type cannot set it off. Build one here with "
                        "reachout_channel, or ask the user.")}

        if op == "add_trigger":
            ch = a.get("channel")
            if ch in (None, ""):
                return _refused("channel is required for op='add_trigger' "
                                  "(its name, or its id from op='list')")
            # A trigger on a RUNBOOK channel runs a model turn each time it
            # fires, with nobody watching: the person's card adds it
            # (runtime/scheduled_approval). Other channels
            # (email, a webhook, the log) send a notice and run no model.
            # The channel is found exactly as add_trigger finds it
            # (service._resolve_channel), so the one checked here is the one
            # the trigger would be added to; an unknown channel falls through
            # to add_trigger's own refusal.
            try:
                _rch = await _ro._resolve_channel(pool, int(eid), ch)
            except _ro.ReachoutError:
                _rch = None
            _trow = (await pool.fetchrow(
                "SELECT action FROM reachout_channels WHERE id=$1 AND entity_id=$2",
                int(_rch["id"]), int(eid)) if _rch is not None else None)
            _tact = _trow["action"] if _trow is not None else None
            if isinstance(_tact, str):
                try:
                    _tact = json.loads(_tact)
                except ValueError:
                    _tact = {}
            # Stripped as the dispatcher strips it (service.fire: TRANSPORTS
            # is keyed on action.type.strip()), and validate_action stores the
            # type unstripped, so " runbook" runs a runbook too.
            if isinstance(_tact, dict) and str(_tact.get("type") or "").strip() == "runbook":
                _label = (a.get("label") or "").strip()
                _mkind = (a.get("match_kind") or "").strip()
                if not _label or not _mkind:
                    # add_trigger's own refusals, said now rather than on the
                    # person's click.
                    return _refused("label and match_kind are required for op='add_trigger'")
                from mastercode.runtime import scheduled_approval as _sa
                return await _sa.propose_trigger(
                    pool, int(eid), channel=_rch.get("name") or ch,
                    channel_id=int(_rch["id"]), label=_label, match_kind=_mkind,
                    match_event=(a.get("match_event") or None),
                    match_jsonb=(a.get("match_jsonb") or None))
            row = await _ro.add_trigger(
                pool, int(eid), channel=ch, label=a.get("label") or "",
                match_kind=a.get("match_kind") or "",
                match_event=(a.get("match_event") or None),
                match_jsonb=(a.get("match_jsonb") or None),
                created_by="host")
            out = dict(row)
            if out.get("created_at") is not None:
                out["created_at"] = out["created_at"].isoformat()
            return {"status": "ok", "result": out}

        if op == "test":
            ch = a.get("channel")
            if ch in (None, ""):
                return _refused("channel is required for op='test'")
            res = await _ro.test_fire(pool, int(eid), ch)
            return {"status": "ok", "result": res}

        if op == "remove_channel":
            ch = a.get("channel")
            if ch in (None, ""):
                return _refused("channel is required for op='remove_channel'")
            return {"status": "ok",
                    "result": await _ro.remove_channel(pool, int(eid), ch, actor="host")}

        # op == "remove_trigger"
        tid = a.get("trigger_id")
        try:
            tid = int(tid)
        except (TypeError, ValueError):
            return _refused("trigger_id is required for op='remove_trigger' "
                              "(an integer, from op='list')")
        return {"status": "ok",
                "result": await _ro.remove_trigger(pool, int(eid), tid, actor="host")}
    except Exception as e:
        # ReachoutError carries the sentence the host should repeat to the user
        # (why the channel was refused, and what to do about it), so it is
        # surfaced verbatim rather than wrapped in a traceback.
        return _refused_if_caller(e, f"reachout: {getattr(e, 'detail', None) or e}")


async def _exec_reachout_channel(args: dict, *, turn_type: dict | None = None) -> dict:
    """BUILD a way for the host to reach its person. Its own tool, on purpose.

    WHY THIS IS NOT AN OP ON `reachout`. Creating an `http_call` channel binds
    one of the host's sealed credentials to an outbound destination, and the
    vault broker will then send that credential there on any later fire —
    including fires with nobody present, which is the entire point of the
    feature. That is a WIDEN, and it is the same class of thing
    `keys_manage` is kept out of the deterministic lane for (direct_ops.py,
    "credential bytes: keys_manage, connect_compute — credential handling keeps
    its dedicated gates + UI path").

    Two walls, and they close different holes:

      THE LANE. This name is in neither DIRECT_OP_TOOLS nor
      UNATTENDED_OP_TOOLS, so `run_direct_op` refuses it outright ("not a
      registered deterministic op") and `offline_task_runner._run_op` never
      reaches it. A scheduled `op_tool` cannot name it. That is structural and
      needs nobody to remember anything.

      THE SECOND FACTOR. server/reachout/service.add_channel demands a
      step-up code for an http_call action, through the same verifier the NAC
      widens use. This is the wall that does not depend on the lane table
      staying right: a TOTP is time-based and lives on the user's
      authenticator, so no stored payload can carry one that still verifies
      later.

    Splitting the tool rather than gating inside one was the available choice:
    `run_direct_op` reaches tools through `_execute_meta_tool` with
    `interaction_context=None` — the SAME arguments the chat path's generic
    meta branch passes (runtime/session.py) — so a handler cannot tell which
    lane invoked it, and a check written as if it could would have been
    decoration.
    """
    a = args or {}
    try:
        from server import brain_service
        from server.reachout import service as _ro
        pool = brain_service.get_pool()
        if pool is None:
            return {"status": "error", "result": "reachout_channel failed: pool unavailable"}
        eid = brain_service._entity_id
        # One `quiet` object rather than three top-level arguments: fewer names
        # on the wire, and the three only ever make sense together (a `from`
        # with no `to` is not a window).
        _q = a.get("quiet") if isinstance(a.get("quiet"), dict) else {}
        # A runbook channel fires a task template. From a TYPED turn, a
        # template saved under ANOTHER type is refused here, as schedule_task
        # refuses it: otherwise text written under one type would run as a
        # wake-up of this one. The channel's own
        # type is stamped by turn_types.stamp_scheduled_work at the gate.
        if turn_type is not None:
            _act = a.get("action")
            if isinstance(_act, str):
                try:
                    _act = json.loads(_act)
                except ValueError:
                    _act = {}
            _act = _act if isinstance(_act, dict) else {}
            if str(_act.get("type") or "").strip() == "runbook":
                try:
                    _tpl_type = await pool.fetchval(
                        "SELECT type_id FROM entity_task_templates "
                        "WHERE id=$1 AND entity_id=$2",
                        int(_act.get("template_id")), int(eid))
                except (TypeError, ValueError):
                    _tpl_type = None   # add_channel refuses a bad template_id
                if _tpl_type and str(_tpl_type) != str(turn_type.get("id") or ""):
                    return {"status": "denied", "reason": "type_denied", "result": (
                        f"template {_act.get('template_id')} was saved under another "
                        "turn type, so a turn of this type cannot build a runbook on it.")}
        row = await _ro.add_channel(
            pool, int(eid),
            name=a.get("name") or "", kind=a.get("kind") or "",
            action=a.get("action"),
            cooldown_s=a.get("cooldown_s"),
            quiet_from=(_q.get("from") or None),
            quiet_to=(_q.get("to") or None),
            quiet_tz=(_q.get("tz") or None),
            created_by="host",
            stepup_code=a.get("stepup_code"))
        out = dict(row)
        if out.get("created_at") is not None:
            out["created_at"] = out["created_at"].isoformat()
        return {"status": "ok", "result": out}
    except Exception as e:
        return _refused_if_caller(e, f"reachout_channel: {getattr(e, 'detail', None) or e}")


def _planned_text(*, prompt: str = "", steps=None, op_tool: str = "",
                  op_args=None) -> str:
    """What a scheduled task will do, in the words it was given — the text
    written into the thread when the task is set (turns types: the thread is
    the chain of thought; nothing runs later that the thread did not show)."""
    if op_tool:
        return (f"run `{op_tool}` with {json.dumps(op_args or {}, sort_keys=True)}"
                " (a fixed op, no model)")
    _steps = [str(s).strip() for s in (steps or []) if str(s).strip()]
    if _steps:
        return "\n".join(f"{i}. {s}" for i, s in enumerate(_steps, 1))
    return str(prompt or "").strip() or "(nothing written)"


async def _exec_schedule_task(args: dict, *, session_id: str | None = None,
                              interaction_context: str | None = None,
                              turn_type: dict | None = None,
                              thread_id: str | None = None) -> dict:
    """Create + manage the host's own SCHEDULED tasks — calendar events with a future
    fire time and optional recurrence, fired offline by the task runner. Create writes
    through the calendar service (handle_calendar_append) so the row carries recurrence;
    the runner re-arms recurring events to their next occurrence.

    `session_id` / `interaction_context` are the ORIGIN, handed over by the
    dispatcher. Until they were, every fired task reported into
    the Circles self-thread no matter where it was promised — so a "I'll check
    on the bot in twenty minutes" said in a Workbench project thread came back
    somewhere the user was not looking. They are recorded on the row
    (related_project_id + payload.origin) and the runner reports through them.

    A fired task IS auto-run, by the offline runner.
    brain/offline_task_runner.run_offline_tasks_loop is dispatched per entity from
    server/main.py's lifespan — the one create_task site for it in the repo — gated
    only on `relay_entity_id > 0`, with no per-entity rollout flag. (Deliberately no
    line number: this docstring pinned one, and a pointer comment added one commit
    later pushed the gate nine lines down. The symbol name is the durable reference.)
    It deliberately does NOT boot UnifiedBrain: it polls
    calendar_events for due tasks, atomically claims each one (so two overlapping ticks
    cannot double-run it), runs its prompt through brain_service.send_message — the same
    governed turn pipeline as live chat, with identity, tools and the CEIGAS gate —
    reports into THE THREAD IT WAS SET IN (payload.origin; a Circles task that names no
    thread, such as the platform's own standing tasks, gets a topic of its own the first
    time it writes, and keeps it: offline_task_runner.ensure_origin_thread), re-arms recurrence,
    and appends a calendar_event_runs row per run.

    This block said the opposite for months. What it used to claim, and why it was
    wrong, is in git log and tests/test_schedule_task_doc_truth.py — not re-quoted
    here, because this file is one a model reads.

    Ops:
      create -> schedule a task (title, fire_at ISO, optional recurrence, prompt/steps;
                optional wake_on {event, match}: run when that platform event
                happens, with fire_at as the deadline — server/host_events.py)
      list   -> upcoming scheduled tasks (status='scheduled')
      cancel -> cancel a scheduled task by event_id
    """
    a = args or {}
    op = (a.get("op") or "list").strip()
    _OPS = {"create", "list", "cancel"}
    if op not in _OPS:
        return _refused(f"op must be one of {sorted(_OPS)} (got {op!r})")
    try:
        from server import brain_service
        pool = brain_service.get_pool()
        if pool is None:
            return {"status": "error", "result": "schedule_task failed: pool unavailable"}
        eid = brain_service._entity_id

        if op == "create":
            title = (a.get("title") or "").strip()
            fire_at = (a.get("fire_at") or "").strip()
            if not title:
                return _refused("title is required for op='create'")
            # A thread the person deleted gets no new task: the delete
            # cancelled the ones set in it, and one written as it landed
            # would only be refused at every fire.
            if thread_id:
                from mastercode.circles_threads import live_thread_id
                if await live_thread_id(pool, int(eid), thread_id) is None:
                    return _refused("this thread was deleted, so nothing is scheduled in it")
            if not fire_at:
                return _refused("fire_at (ISO timestamp) is required for op='create'")
            recurrence = (a.get("recurrence") or "").strip() or None

            # ── Standing-work lane ──────────────────────────
            # op_tool schedules ONE deterministic registry op that fires with
            # no model in the loop. Exclusive with prompt/steps/template_id:
            # a task is either the host's judgement on a schedule, or a fixed
            # op on a schedule, and silently preferring one over the other
            # would hide which lane the work actually ran in.
            op_tool = (a.get("op_tool") or "").strip()
            op_args = a.get("op_args")
            if op_tool:
                if a.get("prompt") or a.get("input") or a.get("steps") or \
                        a.get("template_id") not in (None, ""):
                    return _refused("op_tool is exclusive with prompt / steps / template_id — "
                        "a scheduled task either runs a deterministic op or runs "
                        "a governed turn, never both.")
                if op_args is not None and not isinstance(op_args, dict):
                    return _refused("op_args must be an object (the op's exact arguments).")
                # Fitness, not permission: does this op complete with nobody
                # there? The authoritative check runs again at fire time in the
                # offline runner — this copy exists so the host is told NOW
                # rather than discovering it from a failed run at 03:00.
                from mastercode.runtime.direct_ops import (
                    UNATTENDED_OP_TOOLS, is_unattended_op_tool,
                )
                if not is_unattended_op_tool(op_tool):
                    return _refused(f"`{op_tool}` cannot be scheduled: it does not complete "
                        "without someone present (its result is something a "
                        "person has to act on). Schedulable ops today: "
                        + ", ".join(sorted(UNATTENDED_OP_TOOLS)) + ".")

            # Optional runbook TEMPLATE: stamp out its prompt + steps into the
            # task payload, entity-scoped. The caller's own prompt/steps win;
            # the template's recurrence is inherited only if the caller didn't
            # pass one. (s1-runbook)
            t_prompt = a.get("prompt") or a.get("input") or ""
            t_steps = a.get("steps") or []
            template_id = a.get("template_id")
            _template_type = None
            _template_name = None
            if template_id not in (None, ""):
                try:
                    tid = int(template_id)
                except (TypeError, ValueError):
                    return _refused("template_id must be an integer")
                trow = await pool.fetchrow(
                    "SELECT name, prompt, steps, recurrence, type_id FROM entity_task_templates "
                    "WHERE id=$1 AND entity_id=$2",
                    tid, int(eid))
                if trow is None:
                    return _refused(f"no task_template with id {tid} for this host")
                tsteps = trow["steps"]
                if isinstance(tsteps, str):
                    try:
                        tsteps = json.loads(tsteps)
                    except Exception:
                        tsteps = []
                if not t_prompt:
                    t_prompt = trow["prompt"] or ""
                if not t_steps:
                    t_steps = tsteps or []
                if not recurrence:
                    recurrence = (trow["recurrence"] or "").strip() or None
                _template_type = str(trow["type_id"]) if trow["type_id"] else None
                _template_name = trow["name"]

            # ── wake_on: run when something HAPPENS, fire_at is the deadline ──
            # (a Host scheduled a turn when it should have been monitoring).
            # The platform pulls the task forward at the one place the state
            # changes (server/host_events.emit); a task the event never reaches
            # runs at fire_at, and its pointer line says which of the two it was.
            # Checked after the template, because a template's recurrence is
            # inherited above.
            _wake = None
            if a.get("wake_on") not in (None, "", {}):
                from server.host_events import check_wake_on
                _wake, _why = check_wake_on(a.get("wake_on"))
                if _why:
                    return _refused(_why)
                if recurrence:
                    return _refused("wake_on cannot repeat: a wake fires once, when the thing "
                        "happens or at its deadline. Drop the recurrence (set a new wake "
                        "from the turn it starts if you need another), or drop wake_on "
                        "for a fixed schedule.")
                if op_tool:
                    return _refused("wake_on runs a turn, so it can say whether the thing "
                        "happened or the deadline came first; a fixed op cannot tell the "
                        "two apart. Use prompt (or steps) with wake_on.")

            # ── The governed-turn spend ceiling, refused at AUTHORING time ──
            # A prompt/steps task runs a full model turn on every fire. The
            # runner enforces a per-task ceiling at fire time
            # (offline_task_runner.max_runs_per_day / .runs_in_last_24h) because
            # this tool is not the only writer of a scheduled_task payload —
            # POST /api/calendar wraps handle_calendar_append directly. What
            # fire-time enforcement CANNOT do is tell the host NOW, so this
            # refuses the contradiction up front: a recurrence firing more often
            # than the ceiling allows would spend the first N turns of each day
            # and skip the rest, which is a schedule nobody chose. Naming both
            # numbers is the point — raising the ceiling and loosening the
            # recurrence are both real answers, and the host can pick.
            #
            # The OP lane is exempt: it runs no model, so there is nothing for a
            # spend ceiling to protect, and capping it would only break standing
            # work that already costs nothing. Passing one anyway is REFUSED
            # rather than accepted-and-dropped — it was validated here and then
            # silently not written to the payload, so the host would have been
            # told a limit was set that nothing anywhere would ever enforce.
            # The job's own budget, in dollars, when the person named one
            # ("spend up to $5 on this"): the runner stops the task once its
            # priced runs reach it (offline_task_runner.budget_state). It can
            # only narrow what runs; the person's limits still apply on top.
            _budget = a.get("budget_usd")
            if _budget is not None:
                if op_tool:
                    return _refused("budget_usd does not apply to op_tool: the op lane runs no "
                        "model and spends nothing.")
                try:
                    _budget = round(float(_budget), 4)
                except (TypeError, ValueError):
                    return _refused("budget_usd must be a number of dollars")
                if not _budget > 0:
                    return _refused("budget_usd must be more than $0; leave it out for no budget")
            _max_runs = a.get("max_runs_per_day")
            if _max_runs is not None:
                if op_tool:
                    return _refused("max_runs_per_day does not apply to op_tool: the op lane "
                        "runs no model, so it spends nothing and has no ceiling.")
                try:
                    _max_runs = int(_max_runs)
                except (TypeError, ValueError):
                    return _refused("max_runs_per_day must be a whole number of model turns a day")
                if _max_runs < 1:
                    return _refused("max_runs_per_day must be at least 1 model turn a day")
                # The person's limit is the most a task may carry (runtime/
                # user_limits, 2026-10-20): the Host can ask for less, and asks
                # the person for more.
                from mastercode.brain.offline_task_runner import person_turns_per_day
                if not op_tool and _max_runs > person_turns_per_day():
                    from mastercode.runtime import user_limits as _ul
                    return _refused(
                        f"max_runs_per_day {_max_runs} is above the person's limit of "
                        f"{person_turns_per_day()} model turns a day for one scheduled task. "
                        + _ul.hit_line("scheduled.turns_per_day"))

            if not op_tool:
                # The budget is in TURNS. The steps lane runs ONE GOVERNED TURN
                # PER STEP, so a fire of a 20-step task costs 20 turns, not 1 —
                # counting fires here would wave through 20x what the ceiling
                # says while every text the host reads calls them model turns.
                from mastercode.brain.offline_task_runner import person_turns_per_day
                _ceiling = _max_runs or person_turns_per_day()
                _limit = person_turns_per_day()
                _per_fire = len([s for s in (t_steps or []) if str(s).strip()]) or 1
                _step_note = (f" This task has {_per_fire} steps, and each step is a "
                              f"separate turn, so one fire costs {_per_fire} turns."
                              if _per_fire > 1 else "")
                # A single fire that cannot fit inside the whole daily ceiling is
                # a task that would never run once. Caught here regardless of
                # recurrence, because a one-shot hits it too.
                if _per_fire > _ceiling:
                    return _refused(f"One fire of this task runs {_per_fire} model turns (one per "
                        f"step), which is more than its ceiling of {_ceiling} turns a "
                        f"day — so it would never run at all. "
                        + (f"Raise max_runs_per_day to at least {_per_fire}, or use fewer steps."
                           if _per_fire <= _limit else
                           f"Use fewer steps, or ask the person to raise their limit "
                           f"scheduled.turns_per_day (now {_limit}, the most max_runs_per_day "
                           f"may be) with limits(op='propose')."))
                if recurrence:
                    from mastercode.brain.entity_clock import _recurrence_seconds
                    if _recurrence_seconds(recurrence) is None:
                        return _refused("recurrence must be one of: hourly, daily, weekly, monthly, or every:<seconds>")
                    _period = _recurrence_seconds(recurrence)
                    _fires = 86400 // _period if _period else 0
                    _implied = _fires * _per_fire
                    if _implied > _ceiling:
                        _slowest = -(-86400 * _per_fire // _ceiling)
                        return _refused(f"'{recurrence}' fires {_fires} times a day, which is "
                            f"{_implied} model turns, and this task's ceiling is "
                            f"{_ceiling} turns a day.{_step_note} Turns past the ceiling "
                            f"are skipped, so this schedule would stop partway through "
                            f"every day. Either "
                            + (f"raise max_runs_per_day to at least {_implied}"
                               if _implied <= _limit else
                               f"ask the person to raise their limit scheduled.turns_per_day "
                               f"(now {_limit}, the most max_runs_per_day may be) to at least "
                               f"{_implied} with limits(op='propose')")
                            + f", or space the schedule out to at least "
                            f"every:{_slowest} seconds. If the work is identical each "
                            f"time, op_tool runs it with no model at all and has no "
                            f"ceiling.")
            elif recurrence:
                from mastercode.brain.entity_clock import _recurrence_seconds
                if _recurrence_seconds(recurrence) is None:
                    return _refused("recurrence must be one of: hourly, daily, weekly, monthly, or every:<seconds>")
            # WHERE this was promised. A timer set mid-conversation owes its
            # answer to that conversation; `related_project_id` has been a
            # column on calendar_events all along and nothing ever wrote it
            # from here, so every fired task landed in the Circles self-thread
            # regardless of origin.
            _origin_project = None
            _ic = (interaction_context or "").strip()
            if _ic.startswith("project:"):
                try:
                    _origin_project = int(_ic.split(":", 1)[1])
                except (TypeError, ValueError):
                    _origin_project = None
            _origin = {k: v for k, v in (
                ("interaction_context", _ic or None),
                ("session_id", session_id),
                ("project_id", _origin_project),
                # The Circles thread it was set in: the plan is written
                # there now, and the fired turn thinks and reports there.
                ("thread_id", str(thread_id).strip() or None if thread_id else None),
            ) if v is not None}
            _body = ({"op": {"tool": op_tool, "args": op_args or {}}}
                     if op_tool else
                     {"prompt": t_prompt, "steps": t_steps})
            if _origin:
                _body["origin"] = _origin
            # A task written in a turn that read untrusted content carries
            # that into the turn it fires (runtime/turn_taint);
            # one written in a clean turn fires clean.
            from mastercode.runtime import turn_taint as _turn_taint
            _body = _turn_taint.stamp(_body)
            if _wake:
                _body["wake_on"] = _wake
            # Only the turn lane carries a ceiling — writing one onto an op
            # payload would suggest the op lane is capped, and it is not.
            if _max_runs is not None and not op_tool:
                _body["max_runs_per_day"] = _max_runs
            if _budget is not None:
                _body["budget_usd"] = _budget
            # ── The task's turn type ─────
            # A typed turn schedules under its OWN type only, never another
            # and never none: a wake-up is the same chain of thought
            # continuing later, inside the same walls. Across types only the
            # platform-written timeline crosses (no hidden
            # notes between turns — a note is the threat). An untyped turn
            # may name an active type or none. A closed turn schedules
            # nothing. The runner checks the type again at fire time.
            _want_type = str(a.get("type_id") or "").strip() or None
            _own_type = str((turn_type or {}).get("id") or "") or None if turn_type else None
            if turn_type is not None and turn_type.get("status") == "closed":
                return {"status": "error", "result": (
                    "this turn holds nothing (its turn type could not be read), "
                    "so it cannot schedule anything")}
            if turn_type is not None and _want_type and _want_type != _own_type:
                return _refused("a scheduled task runs under the type of the turn that sets it. "
                    "This turn cannot set work for another type; say in the thread "
                    "what that work is, and the user can take it there.")
            if turn_type is not None and _template_type and _template_type != _own_type:
                return _refused(f"template {template_id} was saved under another turn type, so a "
                    "turn of this type cannot run it.")
            # An untyped turn stamping a typed template keeps the template's type.
            _stamp = _own_type or _want_type or _template_type
            if _stamp:
                from mastercode.runtime.turn_types import load_turn_type_by_id
                _t = await load_turn_type_by_id(pool, int(eid), _stamp)
                if _t.get("status") != "active":
                    return _refused(f"type_id {_stamp!r}: {_t.get('closed_reason')}")
                if _want_type and _template_type and _want_type != _template_type:
                    return _refused(f"template {template_id} was saved under another turn type "
                        "than the type_id given.")
                _body["type_id"] = str(_t["id"])
            # The plan goes into the thread FIRST, in the words it was given,
            # and the row is inserted only once that write has landed: the
            # thread is the chain of thought, and nothing may fire that the
            # thread did not show (a row inserted first could be claimed by
            # the runner while the plan write was still in flight). The event
            # id is chosen here so the plan entry
            # and the row name each other; the runner checks the entry is
            # there before it runs anything (plan_event_id).
            import uuid as _uuid
            _event_uuid = str(_uuid.uuid4())
            _body["plan_in_thread"] = True
            # A plan write that is only QUEUED (PII mode) may not land; the
            # runner looks the entry up by this id before the task runs. A
            # direct write has landed by the time the row exists, so it needs
            # no lookup (and no per-fire scan of the turn rows).
            from mastercode import pii_queue as _pii
            if _pii.enabled():
                _body["plan_event_id"] = _event_uuid
            _plan = _planned_text(prompt=t_prompt, steps=t_steps,
                                  op_tool=op_tool, op_args=op_args)
            _from_tpl = (f" (from template {_template_name})" if _template_name else "")
            from server.host_events import describe as _describe_event
            _plan_msg = ((f"Scheduled **{title}** to run when "
                          f"{_describe_event(_wake['event'], _wake['match'])} happens, "
                          f"or at {fire_at} if it has not by then"
                          if _wake else f"Scheduled **{title}** for {fire_at}")
                         + (f", repeating {recurrence}" if recurrence else "")
                         + f"{_from_tpl}. When it fires "
                         + ("it will " if op_tool else "I'll pick up from here:\n")
                         + _plan)
            _append = {
                "event_id": _event_uuid,
                "event_type": "scheduled_task",
                "summary": title,
                "event_at": fire_at,
                "status": "scheduled",
                "source": "entity",
                "recurrence": recurrence,
                "related_project_id": _origin_project,
                "payload": _body,
            }
            # A RECURRING task that runs a model is the person's to start
            # (runtime/scheduled_approval): validated as
            # above, then a card instead of the row. Their click writes this
            # same plan and row. One-time tasks and the op lane are set as before.
            from mastercode.runtime import scheduled_approval as _sa
            if _sa.needs_approval(recurrence=recurrence, op_tool=op_tool, op_args=op_args):
                from mastercode.brain.offline_task_runner import person_turns_per_day
                # An op that sets a model task on every fire is counted by the
                # steps of the task it sets (scheduled_approval.model_turn).
                _turn_steps = ((_sa.model_turn(op_tool, op_args) or {}).get("steps")
                               if op_tool else t_steps)
                return await _sa.propose(
                    pool, int(eid), title=title, recurrence=recurrence, fire_at=fire_at,
                    plan_msg=_plan_msg, append=_append, origin=_origin or None,
                    turns_per_fire=len([x for x in (_turn_steps if isinstance(_turn_steps, list)
                                                     else []) if str(x).strip()]) or 1,
                    turns_per_day=(_max_runs or person_turns_per_day()),
                    budget_usd=_budget)
            from mastercode.brain.offline_task_runner import post_to_origin_thread
            _posted = await post_to_origin_thread(
                pool, int(eid), _origin or None, _plan_msg,
                # calendar_events.event_id (a uuid); the runner's reports
                # carry its integer id as scheduled_task_id.
                extra_tags={"scheduled_event_id": _event_uuid,
                            "scheduled_plan": True},
                fallback_session=f"scheduled_task:{_event_uuid}")
            if not _posted:
                return {"status": "error", "result": (
                    "the task was not set: its plan could not be written into this "
                    "thread, and a task runs only what the thread shows.")}
            from server.bridge.memory_handlers import handle_calendar_append
            env = await handle_calendar_append(_append)
            if not env.get("ok"):
                _why = env.get("error") or "schedule_task create failed"
                # The plan is already in the thread; say plainly it was not set.
                await post_to_origin_thread(
                    pool, int(eid), _origin or None,
                    f"(Not set after all — **{title}**: {_why}.)",
                    extra_tags={"scheduled_event_id": _event_uuid,
                                "scheduled_plan_withdrawn": True},
                    fallback_session=f"scheduled_task:{_event_uuid}")
                return {"status": "error", "result": _why}
            return {"status": "ok", "result": {
                "scheduled": title, "event_id": env.get("event_id"),
                # The plan is in the thread now; the user saw it.
                "plan_in_thread": True,
                "fire_at": env.get("event_at"), "recurrence": recurrence,
                # Waiting for something to happen: fire_at is then the
                # deadline, and the turn runs at whichever comes first.
                **({"wake_on": _wake, "deadline": env.get("event_at")} if _wake else {}),
                # The turn type it fires under; None = untyped.
                "type_id": _body.get("type_id"),
                # Say WHERE the answer will appear, so the host can tell the
                # user rather than guess — and so a wrong thread is visible
                # in the tool result instead of at fire time.
                # Derived the SAME way the runner picks the room, or this
                # is the "tool result says X while Y happens" shape the rest
                # of this branch exists to remove: keyed on the project alone
                # it told a General-thread timer "Circles".
                "reports_into": (
                    f"project {_origin_project} thread"
                    if _origin_project is not None else
                    ("your Circles thread"
                     if _ic in ("", "chat", "circles") else
                     f"your {_ic} thread")),
                # Name the lane back to the host so "did that cost tokens?" is
                # answerable from the tool result, not inferred from the args.
                "lane": "op" if op_tool else "turn",
                **({"op_tool": op_tool} if op_tool else {})}}

        if op == "list":
            rows = await pool.fetch(
                "SELECT event_id, title, start_time, recurrence, payload FROM calendar_events "
                "WHERE entity_id=$1 AND status='scheduled' AND event_type='scheduled_task' "
                "ORDER BY start_time ASC LIMIT 100",
                int(eid))

            def _payload(raw) -> dict:
                """The payload column is JSONB and asyncpg may hand it back as
                a string, so both shapes are read (same as the offline runner)."""
                p = raw or {}
                if isinstance(p, str):
                    try:
                        p = json.loads(p)
                    except Exception:
                        return {}
                return p if isinstance(p, dict) else {}

            def _lane(p: dict) -> tuple[str, str | None]:
                """Which lane this standing task runs in, for the list view."""
                spec = p.get("op")
                if isinstance(spec, dict) and str(spec.get("tool") or "").strip():
                    return "op", str(spec["tool"]).strip()
                return "turn", None

            out = []
            for r in rows:
                _p = _payload(r["payload"])
                lane, op_tool_name = _lane(_p)
                item = {"event_id": str(r["event_id"]), "title": r["title"],
                        "fire_at": r["start_time"].isoformat() if r["start_time"] else None,
                        "recurrence": r["recurrence"],
                        # Visibility is the counterpart of not asking: a standing
                        # op is created without a prompt, so it must be legible
                        # everywhere it is listed — including which lane it runs
                        # in and therefore whether it spends tokens.
                        "lane": lane}
                if op_tool_name:
                    item["op_tool"] = op_tool_name
                if isinstance(_p.get("wake_on"), dict):
                    # fire_at is its deadline; woke_on is set once the event
                    # came and the task is due now.
                    item["wake_on"] = _p["wake_on"]
                    if isinstance(_p.get("woke_on"), dict):
                        item["woke_on"] = _p["woke_on"]
                out.append(item)
            return {"status": "ok", "result": {"scheduled": out}}

        # op == "cancel"
        event_id = (a.get("event_id") or "").strip()
        if not event_id:
            return _refused("event_id is required for op='cancel'")
        res = await pool.execute(
            "UPDATE calendar_events SET status='cancelled' "
            "WHERE event_id=$1::uuid AND entity_id=$2 AND status='scheduled'",
            event_id, int(eid))
        if res.endswith(" 0"):
            return _refused(f"no scheduled task with event_id {event_id!r}")
        return {"status": "ok", "result": {"cancelled": event_id}}
    except Exception as e:
        return _refused_if_caller(e, f"schedule_task failed: {getattr(e, 'detail', None) or e}")


async def _exec_phone_alias(args: dict) -> dict:
    """Phone-alias ops — the host's human-friendly, phone-number-style handle.
    A phone alias is identity-only (kind='phone' in the platform handle
    registry); it is NOT telephony and NOT public DNS. The bridge fixes
    entity_id from the mTLS cert, so a host only ever reads/sets its OWN
    alias. op:
      get    → read this host's current alias (no args)
      set    → claim/replace this host's alias (alias required)
      remove → drop this host's alias (no args)
      lookup → resolve a phone number → host identity (display name + uuid),
               NO network endpoint (alias required); learn WHO it is, then
               send a contact request.
    """
    a = args or {}
    op = (a.get("op") or "").strip()
    if op not in {"get", "set", "remove", "lookup"}:
        return _refused("phone_alias: op must be one of get|set|remove|lookup")
    if op in {"set", "lookup"} and not (a.get("alias") or "").strip():
        return _refused(f"phone_alias: '{op}' requires 'alias'")
    try:
        from server.routes import phone_alias as _pa
        if op == "get":
            res = await _pa._bridge("phone.alias.get", {})
        elif op == "set":
            res = await _pa._bridge("phone.alias.set", {"alias": a["alias"].strip()})
        elif op == "remove":
            res = await _pa._bridge("phone.alias.remove", {})
        else:  # lookup
            res = await _pa._bridge("phone.alias.lookup", {"alias": a["alias"].strip()})
        return {"status": "ok", "result": res}
    except Exception as e:
        return _refused_if_caller(e, f"phone_alias failed: {getattr(e, 'detail', None) or e}")


async def _exec_tickets(args: dict, *, agent_grant: int | None = None) -> dict:
    """Triage / escalation-queue op-dispatch for the host — its OWN ticket queue.

    A ticket is the host asking the user to decide something it won't decide on
    its own (a quote, a confidentiality call, an email needing a human reply).
    One tool, several ops over the SAME entity-scoped service the /api/tickets
    routes use (NOT the route handlers) — every read/write is `entity_id = <eid>`,
    so the host only ever touches its own queue:

      list     -> the host's DECISION tickets as summary rows (status/circle/source/
                  limit/offset) — op='get' reads one whole; support is support_list
      get      -> one ticket in full by id
      badges   -> open-ticket counts by circle + grand total (sidebar badges)
      decide   -> resolve an OPEN ticket by picking an option_id (and_similar=True
                  crystallizes a reusable decision pattern); writes the queue
      dismiss  -> close an OPEN ticket without a decision (no pattern); writes the queue
      import   -> bring a ticket bundle the user uploaded into Circles tickets +
                  Workbench phases + one archive document (runtime/ticket_import.py);
                  the Host's own act, never a dispatched agent's (agent_grant)

    The decide/dismiss writes + their entity-scoping + linked-session cleanup live
    in the service, so this tool shares ONE path with the routes.
    """
    a = args or {}
    op = (a.get("op") or "").strip()
    if not op:
        return _refused("op is required (one of: list, get, badges, decide, dismiss, import, "
            "support_open, support_update, support_list, support_get, support_reply, support_close)")
    try:
        from server import brain_service
        from server.routes import tickets as t
        pool = brain_service.get_pool()
        if pool is None:
            return {"status": "error", "result": "pool unavailable"}
        eid = brain_service._entity_id

        if op == "list":
            # Decisions only, as summary rows (tickets.ticket_summary): the list
            # finds a ticket, op='get' reads it. Support tickets are support_list.
            res = await t.list_tickets_service(
                pool, eid,
                status_filter=a.get("status"),
                circle_id=(int(a["circle_id"]) if a.get("circle_id") is not None else None),
                source=a.get("source"),
                limit=int(a.get("limit") or 50),
                offset=int(a.get("offset") or 0),
            )
            return {"status": "ok", "result": {
                "tickets": [t.ticket_summary(r) for r in res.get("tickets") or []],
                "offset": res.get("offset"), "next_offset": res.get("next_offset"),
                "note": ("decision tickets only, summarized — op='get' (ticket_id) reads one in "
                         "full; support tickets (drafts included) are op='support_list'"),
            }}

        if op == "badges":
            res = await t.badge_counts_service(pool, eid)
            return {"status": "ok", "result": res}

        if op == "get":
            if a.get("ticket_id") is None:
                return _refused("ticket_id is required for op='get'")
            res = await t.get_ticket_service(pool, eid, int(a["ticket_id"]))
            return {"status": "ok", "result": res}

        if op == "decide":
            if a.get("ticket_id") is None:
                return _refused("ticket_id is required for op='decide'")
            if not a.get("option_id"):
                return _refused("option_id is required for op='decide'")
            res = await t.decide_ticket_service(
                pool, eid, int(a["ticket_id"]),
                option_id=a.get("option_id"),
                and_similar=bool(a.get("and_similar")),
                note=a.get("note", ""),
                pattern_action_class=a.get("pattern_action_class"),
                pattern_conditions=a.get("pattern_conditions"),
                # The HOST is deciding. A ticket locked user_only (a route
                # proposal the host itself filed) is refused here: the host
                # cannot answer its own question and call it the user's.
                actor="host",
            )
            return {"status": "ok", "result": res}

        if op == "dismiss":
            if a.get("ticket_id") is None:
                return _refused("ticket_id is required for op='dismiss'")
            # actor='host' below: a user_only ticket (the host's own route
            # proposal) cannot be withdrawn by the host either.
            res = await t.dismiss_ticket_service(
                pool, eid, int(a["ticket_id"]), note=a.get("note", ""), actor="host",
            )
            return {"status": "ok", "result": res}

        if op == "import":
            from mastercode.runtime import ticket_import as _ti
            return await _ti.run(a, pool=pool, entity_id=int(eid), agent_grant=agent_grant)

        # ── Support half (server/routes/support.py): platform-help tickets.
        # support_open only DRAFTS — the user approves the shared context in the
        # Tickets pane before anything crosses the bridge.
        if op.startswith("support_"):
            from server.routes import support as sp
            try:
                if op == "support_open":
                    for req in ("category", "title", "problem"):
                        if not a.get(req):
                            return _refused(f"{req} is required for op='support_open'")
                    res = await sp.open_service(
                        pool, eid,
                        category=str(a.get("category")), title=str(a.get("title")),
                        problem=str(a.get("problem")), context=str(a.get("context") or ""),
                        recommendation=str(a.get("recommendation") or ""),
                        pushback=str(a.get("pushback") or ""),
                        repro_steps=str(a.get("repro_steps") or ""),
                        expected=str(a.get("expected") or ""), actual=str(a.get("actual") or ""),
                        severity=str(a.get("severity") or "normal"),
                        project_id=(int(a["project_id"]) if a.get("project_id") is not None else None),
                        files=[str(f) for f in (a.get("files") or [])],
                        harness_runs=[int(x) for x in (a.get("harness_runs") or [])],
                        harness_session=str(a.get("harness_session") or ""),
                    )
                    return {"status": "ok", "result": res}
                if op == "support_list":
                    res = await sp.list_service(
                        pool, eid, status_filter=a.get("status"), category=a.get("category"),
                        project_id=(int(a["project_id"]) if a.get("project_id") is not None else None),
                        limit=int(a.get("limit") or 50),
                    )
                    return {"status": "ok", "result": res}
                if a.get("ticket_id") is None:
                    return _refused(f"ticket_id is required for op={op!r}")
                tid = int(a["ticket_id"])
                if op == "support_get":
                    return {"status": "ok", "result": await sp.get_service(pool, eid, tid)}
                if op == "support_update":
                    # A DRAFT's card is rewritten in place and its shared context
                    # assembled again; only the fields passed change.
                    fields = {k: str(a[k]) for k in sp.UPDATE_FIELDS if a.get(k) is not None}
                    if a.get("files") is not None:
                        fields["files"] = [str(f) for f in (a.get("files") or [])]
                    if a.get("harness_runs") is not None:
                        fields["harness_runs"] = [int(x) for x in (a.get("harness_runs") or [])]
                    if a.get("harness_session") is not None:
                        fields["harness_session"] = str(a.get("harness_session") or "")
                    res = await sp.update_service(pool, eid, tid, **fields)
                    return {"status": "ok", "result": res}
                if op == "support_reply":
                    if not a.get("body"):
                        return _refused("body is required for op='support_reply'")
                    res = await sp.reply_service(pool, eid, tid, body=str(a["body"]), author="host")
                    return {"status": "ok", "result": res}
                if op == "support_close":
                    return {"status": "ok", "result": await sp.close_service(pool, eid, tid)}
            except sp.SupportError as e:
                return _refused_if_caller(e, f"tickets failed: {e.detail}")

        return _refused(f"unknown op {op!r} (one of: list, get, badges, decide, dismiss, import, "
            "support_open, support_update, support_list, support_get, support_reply, support_close)")
    except t.TicketError as e:
        return _refused_if_caller(e, f"tickets failed: {e.detail}")
    except Exception as e:
        return _refused_if_caller(e, f"tickets failed: {getattr(e, 'detail', None) or e}")


async def _exec_host_message(args: dict) -> dict:
    """Direct-message another host. One op-dispatch tool over the host's DM surface —
    the SAME service the /api/messages routes use (server.routes.messages), called
    directly (no HTTP shim, no route handler).

      send          -> send a DM (to_entity_id or to_handle, body, topic_id) — keeps our
                       copy + delivers; returns {message_id, thread_id, delivered, topic}
      conversations -> list the host's DM threads (peer + last + unread)
      thread        -> the full back-and-forth in one thread by thread_id (marks received read)
      open          -> resolve a contact to its DM thread id (may have no messages yet)

    A DM threads identically on both hosts (deterministic thread id from the two
    entity ids). Sending to a stranger may be blocked by their domain policy — the
    result's `delivered`/`error` say so; the send still succeeds locally.
    """
    a = args or {}
    op = (a.get("op") or "").strip().lower()
    if not op:
        return _refused("op is required (one of: send, conversations, thread, open)")
    try:
        from server import brain_service
        from server.routes import messages as msg
        pool = brain_service.get_pool()
        if pool is None:
            return {"status": "error", "result": "host_message failed: pool unavailable"}
        eid = brain_service._entity_id
        if not eid:
            return {"status": "error", "result": "host_message failed: host id unknown"}
        eid = int(eid)

        if op == "send":
            body = (a.get("body") or "").strip()
            if not body:
                return _refused("body is required to send a message")
            # topic_id: the topic the message goes into;
            # omitted, the topic of the newest message from that Host.
            res = await msg.send_message_service(
                pool, eid,
                to_entity_id=a.get("to_entity_id"), to_handle=a.get("to_handle"),
                to_display_name=a.get("to_display_name"), body=body,
                topic_id=a.get("topic_id"))
            return {"status": "ok", "result": res}

        if op == "conversations":
            res = await msg.conversations_service(pool, eid)
            return {"status": "ok", "result": res}

        if op == "thread":
            thread_id = (a.get("thread_id") or "").strip()
            if not thread_id:
                return _refused("thread_id is required to read a thread (see op='conversations' or 'open')")
            # One page, newest first; `before` = a next_before it returned
            # reads the page before (R4-1: a long thread is never one answer).
            # Each message names its topic; topic_id reads one topic.
            res = await msg.thread_service(pool, eid, thread_id,
                                           limit=a.get("limit"), before=a.get("before"),
                                           topic_id=a.get("topic_id"))
            return {"status": "ok", "result": res}

        if op == "open":
            res = await msg.open_conversation_service(
                pool, eid,
                to_entity_id=a.get("to_entity_id"), to_handle=a.get("to_handle"),
                to_display_name=a.get("to_display_name"))
            return {"status": "ok", "result": res}

        return _refused(f"unknown op '{op}' — use send|conversations|thread|open")
    except Exception as e:
        # MessageError carries .detail; any other failure falls through to its str.
        return _refused_if_caller(e, f"host_message failed: {getattr(e, 'detail', None) or e}")


async def _exec_contacts_manage(args: dict) -> dict:
    """Contacts (address book + connection requests) op-dispatch for the host.

    One tool, several read/write ops over the host's pod-local address book —
    the SAME service helpers the /api/contacts routes use (NOT the route
    handlers). The address book is pod-local (contacts table); accepting a
    request also writes the per-peer domain_policy allow and best-effort
    delivers an endpoint_grant reply through the bridge.

      list          -> the address book (optional status / text filter)
      card          -> the contact card for other Hosts (host_ids): name,
                       address, number, relationship — the SAME resolver the
                       dashboard shows (server/hosts/card.py), from this
                       Host's own records only
      add           -> add or upsert a contact (write)
      update        -> edit a contact's display_name / notes / status (write)
      remove        -> delete a contact (write)
      accept_request-> accept an inbound connection request: record contact +
                       allow + best-effort grant reply (write)
      deny_request  -> deny an inbound connection request: archive it, no
                       contact, no allow (write)
      request_by_phone -> resolve a phone alias to a host identity + send it a
                       connection request (write)
      request_peer  -> send a connection request to a host you share a MACHINE
                       with (peer_entity_id, from get_network). Only a host one
                       of your machines currently reports serving qualifies —
                       the peer list IS the authorization (write)
    """
    a = args or {}
    op = (a.get("op") or "").strip()
    if not op:
        return _refused("op is required (one of: list, card, add, update, remove, "
            "accept_request, deny_request, request_by_phone, request_peer)")
    try:
        from server import brain_service
        from server.routes import contacts as c
        pool = brain_service.get_pool()
        if pool is None:
            return {"status": "error", "result": "pool unavailable"}

        if op == "list":
            res = await c.list_contacts_service(
                pool,
                status=(a.get("status") or None),
                q=(a.get("q") or None),
                limit=int(a.get("limit") or 200),
            )
            return {"status": "ok", "result": res}

        if op == "card":
            ids = a.get("host_ids")
            if not isinstance(ids, list) or not ids:
                return _refused("host_ids (a list of Host ids) is required for card")
            from server.hosts.card import host_cards
            cards = await host_cards(pool, brain_service._entity_id, ids)
            return {"status": "ok", "result": {"cards": {str(k): v for k, v in cards.items()}}}

        if op == "add":
            res = await c.create_contact_service(
                pool,
                handle=(a.get("handle") or ""),
                display_name=a.get("display_name"),
                kind=(a.get("kind") or "host"),
                notes=a.get("notes"),
                status=(a.get("status") or "active"),
            )
            return {"status": "ok", "result": res}

        if op == "update":
            cid = a.get("contact_id")
            if cid is None:
                return _refused("contact_id is required for update")
            # Only the keys the caller actually passed are treated as set, so an
            # omitted field is left untouched (same as the PATCH route's body).
            provided = {k for k in ("display_name", "notes", "status") if k in a}
            if not provided:
                return _refused("nothing to update (pass display_name, notes, or status)")
            res = await c.update_contact_service(
                pool, contact_id=int(cid),
                display_name=a.get("display_name"),
                notes=a.get("notes"),
                status=a.get("status"),
                _provided=provided,
            )
            return {"status": "ok", "result": res}

        if op == "remove":
            cid = a.get("contact_id")
            if cid is None:
                return _refused("contact_id is required for remove")
            res = await c.delete_contact_service(pool, contact_id=int(cid))
            return {"status": "ok", "result": res}

        if op == "accept_request":
            res = await c.accept_request_service(
                pool,
                handle=(a.get("handle") or ""),
                message_id=a.get("message_id"),
                display_name=a.get("display_name"),
                from_entity_id=a.get("from_entity_id"),
            )
            return {"status": "ok", "result": res}

        if op == "deny_request":
            res = await c.deny_request_service(
                pool,
                handle=a.get("handle"),
                message_id=a.get("message_id"),
            )
            return {"status": "ok", "result": res}

        if op == "request_by_phone":
            alias = (a.get("alias") or "").strip()
            if not alias:
                return _refused("alias (phone number) is required")
            res = await c.request_by_phone_service(pool, alias=alias, note=a.get("note"))
            return {"status": "ok", "result": res}

        if op == "request_peer":
            pid = a.get("peer_entity_id")
            if pid in (None, ""):
                return _refused("peer_entity_id is required — read it from get_network, which lists "
                    "the peers each of your machines reports")
            res = await c.request_peer_service(pool, peer_entity_id=pid, note=a.get("note"))
            return {"status": "ok", "result": res}

        return _refused(f"unknown op {op!r} — use list, add, update, remove, "
            "accept_request, deny_request, request_by_phone, or request_peer")
    except c.ContactsError as e:
        return _refused_if_caller(e, f"contacts_manage failed: {e.detail}")
    except Exception as e:
        return _refused_if_caller(e, f"contacts_manage failed: {getattr(e, 'detail', None) or e}")
