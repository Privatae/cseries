"""
runtime/agent_dispatch.py — the dispatched-agent layer.

The host orchestrator calls `dispatch()` to spawn an in-container scoped agent as
a BACKGROUND task: the call returns immediately so the host keeps chatting, the
agent runs `run_scoped_agent()` on its own, and the outcome lands in
`entity_tasks` — the registry the taskbar tray reads. The user is governor:
`cancel()` is the veto.

  Registry = entity_tasks (task_type='agent', assigned_to='lane_b')
  Engine   = runtime.scoped_agent.run_scoped_agent (guarded, scoped tool loop)
  Cost     = est_cost_usd from the agent's own token counts priced against the
             platform's model catalog (inference_cost_log has no session_id to
             attribute by; the system already treats cost as an estimate, so
             this is consistent + honest). None when the catalog cannot price
             the model, and BOTH surfaces render that word rather than an
             empty cell: the Workbench bench row
             (workbench-web/src/components/workbench/ops-pane.tsx) and the
             chat agent card (workbench-web + circles-web
             src/components/chat/tool-card.tsx). Never an invented rate.

The harness pool (external coding harnesses) is a separate dispatch path for code/build
work — never reached from here. A dispatched agent cannot execute code (enforced in
scoped_agent), so the worst a dispatched agent can do is make a mistake, not
break anything.
"""

import asyncio
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from mastercode.brain.task_manager import TaskManager
from mastercode.runtime.scoped_agent import agent_round_budget, run_scoped_agent

log = logging.getLogger("cseries.mastercode.agent_dispatch")


async def _as_agent(coro):
    """Run an agent under runtime/turn_taint's agent state, which never admits a
    root command, and give the caller's own state back after. dispatch_sync
    awaits the agent inside the Host's turn, so the reset is what keeps the
    Host's state its own."""
    from mastercode.runtime import turn_pictures, turn_taint
    tok = turn_taint.begin_agent()
    # An agent's tools answer in words: no picture they offer reaches the
    # Host's turn this agent was dispatched from (runtime/turn_pictures).
    pic_tok = turn_pictures.begin_agent()
    try:
        return await coro
    finally:
        turn_pictures.end(pic_tok)
        turn_taint.end_turn(tok)



def _est_cost(model_id: str, tokens_in: int, tokens_out: int) -> float | None:
    """The tray's $ estimate for one agent run, from the SAME catalog the rest
    of the platform prices against — or None when that catalog cannot price the
    model, in which case the surfaces say "unpriced" and no number is invented.
    They are named here because "shows unpriced" is a claim about code that
    lives in another repo tree and it has to be checkable:
    workbench-web/src/components/workbench/ops-pane.tsx renders the bench row,
    and workbench-web + circles-web src/components/chat/tool-card.tsx render
    the chat card (workbench-web/src/lib/types.ts and circles-web/src/lib/api.ts
    both type est_cost_usd as `number | null`). A run still IN FLIGHT stays
    blank on all three — no cost reported yet is not the same fact as no rate.

    WHAT THIS REPLACED, and why it had to go. This was a hardcoded
    `_RATE_PER_M` table of three families — haiku/sonnet/opus — with a
    `_DEFAULT_RATE` of (1.0, 5.0) per 1M applied to everything else. Three
    separate defects, all of the same kind:
      * a SECOND pricing source. The catalog is the platform's rate card
        (server/routes/inference.py:cached_model_prices) and a table written by
        hand beside it drifts from the vendor's published rate the moment
        either changes, with nothing to notice.
      * it priced by SUBSTRING. Any model id containing "opus" got Opus's rate;
        any model from any other vendor got the invented (1.0, 5.0) default.
      * the default WAS an invented number, for a model the platform may have
        no rate for at all — the exact thing the estimate lane refuses
        everywhere else (v113's 'unpriced', and cached_model_prices()'s refusal
        to price subscription rows at zero).

    Imported lazily, the way provider_gateway does it (:244): mastercode must
    not import server/ at module load. A cold catalog or an absent server
    package therefore degrades to None — the tray shows no dollars rather than
    wrong ones. Reads the already-warmed caches; never fetches.

    Cache counts are not passed because a scoped-agent run does not report
    them: run_scoped_agent returns tokens_in/tokens_out only, so the cache
    kwargs would be a guess. Zero is the default there, which yields exactly
    the whole-input-at-input-rate number — the honest reading of what this
    caller actually knows."""
    try:
        from server.routes.inference import estimate_call_cost_usd
        return estimate_call_cost_usd(model_id, int(tokens_in or 0),
                                      int(tokens_out or 0))
    except Exception as e:                    # noqa: BLE001 — telemetry only
        log.debug("tray cost estimate unavailable for %s: %s", model_id, e)
        return None


def _cost_str(usd) -> str:
    """A cost for a LOG LINE. `None` is not a dollar amount and must not be
    formatted as one — these lines carried %.5f, which raises on None."""
    return "unpriced" if usd is None else f"${float(usd):.5f}"


def _is_validator_run(tool_actions) -> bool:
    """A run that touched the bug cache's `read` or `verdict` op is a bug-lane
    validator: none of its own words may reach the dispatching host."""
    for ta in tool_actions or []:
        if isinstance(ta, dict) and ta.get("tool") == "bug_review":
            if (ta.get("args") or {}).get("op") in ("read", "verdict"):
                return True
    return False


def _blocked_error_text(out: dict, record: dict, verdict) -> str:
    """The row's `error` for a non-pass run — the text deliver_agent_result
    hands the host. A 'blocked' run leads with blocked_reason: it names the
    wall and carries the retry-grant line, so the host re-dispatches
    mechanically instead of re-deriving the grant from the agent's prose
    (which is what it got wrong the first time). Other verdicts keep the
    run's own error/result, as before."""
    reason = str(record.get("blocked_reason") or "")
    body = str(out.get("error") or out.get("result") or "")
    if _is_validator_run(record.get("tool_actions") or out.get("tool_actions") or []):
        # A bug-lane validator's raw text is another Host's report filtered
        # through a model that may have been fooled by it — and a fooled
        # validator that reaches for a tool outside its scope is exactly a
        # BLOCKED run. Its `error` carries the fixed sentence _finalize_record
        # put in `result`, never `out["result"]` / `out["error"]`.
        body = str(record.get("result") or "validator run ended without a verdict")
    if verdict == "blocked" and reason:
        text = reason + (f"\n\nAgent's report: {body}" if body else "")
    else:
        text = body or reason or f"agent finished with verdict '{verdict}'"
    # Whole, never cut silently.
    # `error` is TEXT; what reaches a transcript is bounded, honestly marked,
    # by the tool-result wall, not here.
    return text


def _finalize_record(out: dict, provider, model, system_preamble=None) -> dict:
    """Build the registry record (result + tokens + est cost + the prompt/model/
    verdict the host learns from) from a run_scoped_agent return. Shared by the
    async and sync dispatch paths. The model that actually ran comes back on the
    run itself (the route resolution happens inside run_scoped_agent); the
    args are a fallback for explicit-pick callers."""
    eff_model = out.get("model") or model or ""
    tin = int(out.get("tokens_in") or 0)
    tout = int(out.get("tokens_out") or 0)
    _res = out.get("result", "")
    # VERDICT — heuristic: a run that returned a non-empty
    # result without erroring passed; an error/empty result failed. A lane that
    # couldn't serve at all (subscription disconnected, unconfigured route) is
    # 'unavailable' — an infrastructure miss that must NOT count against the
    # model in model_stats, or the host learns to avoid a model for a reason
    # that had nothing to do with the model.
    # A code-exec tool the host wrongly put in this dispatched-agent scope was
    # stripped (or blocked at exec) by scoped_agent's security wall. That is an
    # ORCHESTRATION mistake — the host should have used its own `build` tool —
    # not a model outcome. It must NEVER score 'pass' (or the host learns "this
    # model works" from a run that silently no-op'd), and the reason must be
    # visible so the tray shows WHY. Verdict 'blocked'; excluded from pass_rate.
    # denied_tools ALSO carries names refused by the
    # grant wall (called out of grant, or a grant that never covered the
    # tool's capabilities) — same verdict, a different reason. blocked_reason
    # is scoped_agent's one-sentence account of WHICH wall fired; the tray
    # renders it verbatim instead of assuming code-exec (a grant refusal was captioned "code work runs on the host's build lane").
    denied_tools = sorted(out.get("denied_tools") or [])
    if out.get("lane_unavailable"):
        verdict = "unavailable"
    elif denied_tools:
        verdict = "blocked"
    else:
        verdict = "pass" if (_res and not out.get("error")) else "fail"
    # A bug-lane validator's final text is another Host's report filtered
    # through a model that may have been fooled by it. It is never stored:
    # agent_status(task_id) hands `result` to the host word for word, so the
    # stored result is a fixed sentence derived from the run's own actions
    # (runtime/bug_review.py; session._bug_verdict_number).
    _bug_actions = [ta for ta in (out.get("tool_actions") or [])
                    if isinstance(ta, dict) and ta.get("tool") == "bug_review"]
    if _bug_actions:
        _verdicts = {str((ta.get("args") or {}).get("number"))
                     for ta in _bug_actions
                     if (ta.get("args") or {}).get("op") == "verdict" and ta.get("result_status") == "ok"}
        _read = next((str((ta.get("args") or {}).get("number")) for ta in _bug_actions
                      if (ta.get("args") or {}).get("op") in ("read", "verdict")), "?")
        if len(_verdicts) == 1:
            _res = f"Verdict recorded for bug report #{next(iter(_verdicts))}."
            verdict = "pass" if not denied_tools else verdict
        else:
            _res = f"Validator for bug report #{_read} finished without recording a verdict."
            # A validator that recorded no verdict did not do its one job; it
            # never scores 'pass' (that would inflate its model's pass rate).
            verdict = "fail" if verdict == "pass" else verdict
    return {
        "result": _res,
        "tool_actions": out.get("tool_actions", []),
        "tokens": int(out.get("tokens") or 0),
        "tokens_in": tin,
        "tokens_out": tout,
        "rounds": int(out.get("rounds") or 0),
        "est_cost_usd": _est_cost(eff_model, tin, tout),
        "model": eff_model,                        # the model that actually ran
        "system_prompt": (system_preamble or ""),  # the host-written prompt used
        "verdict": verdict,
        "denied_tools": denied_tools,              # why a 'blocked' run was blocked
        "blocked_reason": (str(out.get("blocked_reason") or "")
                           if denied_tools else ""),
    }


# Tools whose RESULTS are never previewed in an agent's activity trail. The
# trail is what agent_status(task_id) hands the dispatching host, so a preview
# of a validator's `bug_review read` would put another Host's report text in
# front of the host before any verdict — the one thing the bug lane's wall
# exists to prevent (runtime/bug_review.py). Args are still recorded (an op
# name and a number), and the full event log stays on disk for the record.
_NO_RESULT_PREVIEW: frozenset[str] = frozenset({"bug_review"})


# The row a delivery is built from — _deliver's query, shared with the gate
# that builds the same record (scripts/test_pipeline_db.py).
DELIVERY_ROW_SQL = ("SELECT task_id, entity_id, title, description, task_type, status, "
                    "result, error FROM entity_tasks WHERE task_id=$1")


def delivery_record(row, res: dict) -> dict:
    """The record a finished agent row is delivered from: the registry row's
    columns plus the typed fields of its result, copied BY NAME — a key the
    task row carries but this function does not name never reaches the
    delivery. One function so the gates can build the record the delivery
    really sees (scripts/test_pipeline_db.py) instead of the raw row."""
    return {
        "task_id": row["task_id"],
        "entity_id": row["entity_id"],
        "title": row["title"],
        "task_prompt": row["description"],
        "task_type": row["task_type"],
        "status": row["status"],
        "error": row["error"],
        "result": res.get("result", ""),
        # The run's own action record. deliver_agent_result reads it to
        # recognise a bug-lane validator run (session._bug_verdict_number /
        # _bug_validator_number) and deliver a fixed sentence instead of
        # the agent's words; without this key those helpers saw nothing
        # and every branch that depends on them was dead.
        "tool_actions": res.get("tool_actions") or [],
        "est_cost_usd": res.get("est_cost_usd", 0.0),
        "origin": res.get("origin") or {},
        # dispatched agent: the model that ran + the host-written prompt (W1).
        # harness build: the harness + its model — so the failure report
        # can say WHICH builder failed and name the alternatives.
        "model": res.get("model") or "",
        "system_prompt": res.get("system_prompt") or "",
        "harness": res.get("harness") or "",
        "project_id": res.get("project_id"),
        "phase_number": res.get("phase_number"),
        "phase_title": res.get("phase_title"),
        # What the landed result IS: the static render's
        # URL under its honest name, and the OBSERVED app state
        # (installed / running). The delivery derives its one
        # run-state sentence from `app`; nothing here is prose.
        "files_preview_url": res.get("files_preview_url"),
        "app": res.get("app") if isinstance(res.get("app"), dict) else None,
        # What the phase EARNED (v124): the pod's acceptance verdict and
        # the status the phase was actually written with. Data the
        # delivery states; the host cannot report a proof that is not
        # here.
        "acceptance": (res.get("acceptance")
                       if isinstance(res.get("acceptance"), dict) else None),
        "acceptance_declared": bool(res.get("acceptance_declared")),
        # The scoped reviewer's verdict on the landed diff (the diff_review
        # envelope). The delivery states it as a fact line
        # (diff_review.review_facts); absent = the diff was not reviewed.
        "review": (res.get("review")
                   if isinstance(res.get("review"), dict) else None),
        # A landed desktop design, loaded in the browser (desktop_render
        # landed_tree_verdict): stated by session._desktop_render_facts.
        "desktop_render": (res.get("desktop_render")
                           if isinstance(res.get("desktop_render"), dict) else None),
        "phase_status": res.get("phase_status"),
        # What auto-deliver did with this landing (tools._run_phase_build,
        # merged onto the row before _deliver): the merge, and the ship with
        # its proof task — or why either was held. Without them the delivery
        # stated the pre-ship `app` snapshot ("not installed") seconds after
        # the platform had merged, installed and proven it.
        "auto_merge": res.get("auto_merge") if isinstance(res.get("auto_merge"), dict) else None,
        "auto_ship": res.get("auto_ship") if isinstance(res.get("auto_ship"), dict) else None,
        # A watched MCP call (runtime/mcp_watch.py): the call, its arguments,
        # the words it repeated on, how many calls ran and why it ended. The
        # brief the Host carries on from is built from it (mcp_watch.brief).
        "watch": res.get("watch") if isinstance(res.get("watch"), dict) else None,
        # Why the approved-plan chain stopped after this phase, if it
        # did (tools._chain_stop). Carried so the delivery can SAY it —
        # a chain that halts silently reads as a crash.
        "chain_stopped": res.get("chain_stopped"),
        # The pipeline's decision (runtime/pipeline.py): one
        # summary per configured check, the decision's reason, and whether the
        # project runs its own pipeline — the proof line states `phase_why`
        # instead of the default pipeline's explanations when it does
        # (session._acceptance_facts).
        "checks": res.get("checks") if isinstance(res.get("checks"), list) else None,
        "phase_why": res.get("phase_why") if isinstance(res.get("phase_why"), list) else [],
        "pipeline_custom": bool(res.get("pipeline_custom")),
        "changed_files": res.get("changed_files") or [],
        # The platform's contract check on the landed files (_factory_build_task_finish).
        "violations": res.get("violations") or [],
        # fan-out: the review session + each harness's
        # terminal state, so the report can say who landed what and
        # point at review.compare instead of re-deriving it.
        "session_id": res.get("session_id"),
        "harnesses": res.get("harnesses") or [],
        "per_harness": res.get("per_harness") or {},
        "harnesses_done": res.get("harnesses_done") or [],
        "harnesses_failed": res.get("harnesses_failed") or [],
        # A build someone KILLED (review.kill_session_runs, stamped by
        # tools._factory_build_task_finish): who. The delivery states the
        # stop and wakes no turn (session.kill_of) — a failure brief would
        # tell the Host to run it again.
        "killed_by": res.get("killed_by"),
        # The dispatch round this agent belongs to (mark_round), so the
        # delivery can be a short pointer and the last one to land
        # wakes the host (see _maybe_continue_round).
        "round": res.get("round") if isinstance(res.get("round"), dict) else None,
        # app_verify rows: the proof's typed envelope and
        # the repair decision — what the delivery states, never infers.
        "app_id": res.get("app_id"),
        "verify": res.get("verify") if isinstance(res.get("verify"), dict) else None,
        "repair": res.get("repair") if isinstance(res.get("repair"), dict) else None,
        "repair_round": res.get("repair_round"),
        "max_repairs": res.get("max_repairs"),
    }


# How long a report waits for an earlier one in the same thread. Covers a
# failure report's investigation call (the slowest delivery) with room over.
DELIVERY_ORDER_WAIT_S = 180


def delivery_thread_key(origin) -> tuple:
    """The thread a report is written into, as a lock key: origin surface,
    project, Circles thread. `origin` may arrive as a JSON string (older
    rows). Unknown shapes share one key, which only serialises them."""
    if isinstance(origin, str):
        try:
            origin = json.loads(origin)
        except (TypeError, ValueError):
            origin = {}
    o = origin if isinstance(origin, dict) else {}
    return (o.get("app_id") or "circles", o.get("project_id"), o.get("thread_id"))


def no_sign_of_life_text(stuck_after_seconds) -> str:
    """The failure the liveness reaper records. The rule is a NO-HEARTBEAT
    window, not a run-time cap: a running agent stamps a heartbeat at every
    tool call and every model round (scoped_agent._heartbeat), so this fires
    only when neither happened for the whole window."""
    try:
        secs = int(stuck_after_seconds)
    except (TypeError, ValueError):
        secs = 0
    span = (f"{secs // 60} minutes" if secs >= 120 and secs % 60 == 0
            else f"{secs} seconds")
    return (f"no sign of life for {span}: no tool call and no model round started "
            "in that time, so the run was taken to be stuck and stopped. Work it "
            "finished before then is in its activity feed.")


class AgentDispatch:
    """Spawns + tracks in-container scoped agents. One instance per
    runtime (hold it on EntityRuntime) so the in-flight map survives across
    chat turns and `cancel()` can reach a running agent."""

    # FLOOR for the factory-build stuck horizon. The scan scales per-row —
    # GREATEST(this, 2x the run's own recorded timeout_s) — so a build with a
    # raised budget (the timeout ceiling is 4h) is never reaped
    # mid-run, while a wedged run with no recorded budget still reaps here.
    FACTORY_STUCK_AFTER_SECONDS = 4 * 60 * 60  # 4h floor

    def __init__(self, pool):
        self.pool = pool
        # task_id -> the background asyncio.Task (kept so it isn't GC'd and so
        # a user veto can cancel it).
        self._running: dict[int, asyncio.Task] = {}
        # Back-reference to the EntityRuntime, set by brain_service after the
        # runtime is built. The async lifecycle uses it to wake the host on
        # completion (runtime.deliver_agent_result). None until wired → the
        # delivery + monitor paths no-op safely (the registry record still
        # lands, so nothing is lost).
        self.runtime = None
        # One delivery at a time per thread (delivery_thread_key). asyncio.Lock
        # wakes waiters in the order they queued, so reports land in the order
        # their tasks were claimed.
        self._thread_locks: dict[tuple, asyncio.Lock] = {}
        # A report that must follow another (origin.after_task_id: the proof
        # an auto-ship started follows its build's report). _delivering holds
        # the task ids whose report is being written now; _delivered_ev wakes
        # the reports waiting on one.
        self._delivering: set[int] = set()
        self._delivered_ev: dict[int, asyncio.Event] = {}

    async def dispatch(
        self, *,
        entity_id: int,
        title: str,
        task_prompt: str,
        tool_scope: list[str],
        grant: list[str] | None = None,
        model: str | None = None,
        provider: str | None = None,
        system_preamble: str | None = None,
        origin: dict | None = None,
        # Turn types: the dispatching turn's profile, handed to
        # run_scoped_agent so the agent is capped at the turn. None = untyped.
        turn_type: dict | None = None,
        # Where the agent's model came from (mastercode/agent_work
        # .choose_agent_model): named, a preference for the kind of work, or
        # the Host's own route. Seeded on the record; the Agents app shows it.
        model_choice: dict | None = None,
    ) -> dict:
        """Spawn a scoped agent in the background. Returns IMMEDIATELY with the
        agent handle {task_id, status:'running', session_id}; the host does not
        wait. The outcome lands in entity_tasks for the tray.

        `origin` carries the dispatch's back-ref so completion can deliver the
        result to the user without the host re-deriving it: the originating
        chat session, the user's original request, app_id/project_id and the
        source_context the chat turn was tagged with. It is seeded into the
        record's `result` jsonb at creation (so an agent that times out before
        it ever writes a record is still deliverable) and preserved across the
        completion write."""
        # Rounds: the host's own runaway backstop
        # (turn_allowance.backstop_rounds), never a per-task ceiling and never
        # the model's (agent_round_budget).
        _rb = await agent_round_budget(self.pool, entity_id)
        max_rounds = _rb["rounds"]
        task_id, agent_session = await self._create_agent_task(entity_id, title, task_prompt)

        # Seed the record up front: the host-written prompt (so a RUNNING agent
        # shows what it's doing, not just its task line) + origin when the run is
        # async-delivered. The monitor's stuck-timeout path keys on
        # `result ? 'origin'`, so origin is added ONLY when present (a
        # dispatch_sync-style run has none and must NOT be auto-delivered).
        try:
            _seed = {"system_prompt": system_preamble or "",
                     "rounds": {"backstop": _rb["backstop"]}}
            if model_choice:
                _seed["model_choice"] = model_choice
            if origin:
                _seed["origin"] = origin
            await self.pool.execute(
                "UPDATE entity_tasks SET result = COALESCE(result,'{}'::jsonb) || $1::jsonb "
                "WHERE task_id = $2",
                json.dumps(_seed), task_id,
            )
        except Exception as e:
            log.warning("dispatch: failed to seed record for task=%d: %s", task_id, e)

        bg = asyncio.create_task(self._run_and_record(
            task_id=task_id, entity_id=entity_id, task_prompt=task_prompt,
            tool_scope=tool_scope, grant=grant, model=model, provider=provider,
            session_id=agent_session, system_preamble=system_preamble,
            max_rounds=max_rounds,
            origin=origin, turn_type=turn_type,
        ))
        self._running[task_id] = bg
        bg.add_done_callback(lambda _t, tid=task_id: self._running.pop(tid, None))
        log.info(
            "dispatch: spawned agent task=%d eid=%d scope=%s",
            task_id, entity_id, sorted(tool_scope or []),
        )
        return {"task_id": task_id, "status": "running", "session_id": agent_session,
                "round_backstop": max_rounds}

    async def mark_round(self, task_ids: list[int]) -> None:
        """Stamp one dispatch ROUND on every agent it spawned: the agents the
        host fired in a single tool round (the parallel research stage of
        the build workflow). The record's top-level `round` = {anchor,
        task_ids}; the anchor (lowest id) is where the one-shot continuation
        claim is taken. Top-level on purpose: the terminal write merges
        `old || new` and `new` carries a fresh `origin`, so anything nested
        under origin would be lost when the agent finishes."""
        ids = sorted({int(t) for t in (task_ids or []) if str(t).lstrip("-").isdigit()})
        if not ids:
            return
        await self.pool.execute(
            "UPDATE entity_tasks SET result = jsonb_set(COALESCE(result,'{}'::jsonb), "
            "'{round}', $1::jsonb, true) WHERE task_id = ANY($2::int[])",
            json.dumps({"anchor": ids[0], "task_ids": ids}), ids,
        )

    async def project_has_plan(self, project_id: int) -> bool:
        """Does this project already have a plan (any project_phases row)?

        The round machinery (mark_round → pointer delivery → continuation
        brief) exists for ONE stage of the build workflow: the parallel
        research agents fired BEFORE a plan exists, whose reports the host
        must synthesize into docs/research.md and a plan. Once a project has
        phases, an agent the host dispatches from its thread is task-level
        work — "verify what /ui/ serves" — and its findings belong in the
        chat, phrased, exactly as a Circles dispatch is delivered.

        Measured on one Host's project: with the stamp applied to every project-thread dispatch,
        an agent's verbatim probe report reached the chat as one pointer
        line ("the full report is on the bench — I'm picking up from it
        now"), the continuation brief then told the host NOT to relay it and
        to "take the next step the build workflow calls for", and the host
        re-read six files itself and asked the user to do the check by hand.
        Dispatches in that thread fell from 6/day to 1/day over the three
        days the stamp was live, and the host began writing "sending an
        agent" without calling `dispatch` at all.

        Fails toward True (no stamp, phrased delivery): if the read fails the
        user still sees the agent's findings, which is the outcome the stamp
        was costing them.
        """
        try:
            return bool(await self.pool.fetchval(
                "SELECT EXISTS(SELECT 1 FROM project_phases WHERE project_id = $1)",
                int(project_id),
            ))
        except Exception as e:
            log.warning("round: plan-state read failed for project %s (%s) — "
                        "treating as planned, no round stamp", project_id, e)
            return True

    async def _maybe_continue_round(self, task_id: int, res: dict, origin: dict) -> None:
        """After an agent's result is delivered: if it was the LAST agent of
        its round to be delivered, wake the host ONCE to continue the
        workflow (runtime.continue_after_round). Before this, a round of N
        research agents ended as N reports in the thread and nothing acted
        on them — the SOP's synthesis + plan step never ran until the user
        typed again (two reports, then silence; the project stayed a
        stub).

        The claim is one atomic UPDATE on the anchor row: it succeeds only
        when every task in the round already carries delivered_at and the
        anchor has not been continued yet, so two agents finishing together
        cannot both win. Project-scoped rounds only — a Circles dispatch
        ("send a note") has no workflow to continue."""
        rnd = res.get("round") if isinstance(res, dict) else None
        if not isinstance(rnd, dict):
            return
        ids = [int(t) for t in (rnd.get("task_ids") or []) if str(t).isdigit()]
        anchor = rnd.get("anchor")
        if not ids or anchor is None or (origin or {}).get("project_id") is None:
            return
        try:
            claimed = await self.pool.fetchval(
                "UPDATE entity_tasks SET result = jsonb_set(COALESCE(result,'{}'::jsonb), "
                "'{round_continued_at}', to_jsonb(now()::text), true) "
                "WHERE task_id = $1 "
                "AND NOT (COALESCE(result,'{}'::jsonb) ? 'round_continued_at') "
                "AND NOT EXISTS (SELECT 1 FROM entity_tasks t WHERE t.task_id = ANY($2::int[]) "
                "                AND NOT (COALESCE(t.result,'{}'::jsonb) ? 'delivered_at')) "
                "RETURNING task_id",
                int(anchor), ids,
            )
        except Exception as e:
            log.warning("round: continuation claim failed (task=%s): %s", task_id, e)
            return
        if claimed is None:
            return
        if self.runtime is None or not hasattr(self.runtime, "continue_after_round"):
            log.warning("round: no runtime to continue on (anchor=%s)", anchor)
            return
        log.info("round: all %d agents delivered (anchor=%s) — waking the host to continue",
                 len(ids), anchor)
        asyncio.create_task(self.runtime.continue_after_round(dict(origin or {}), ids, int(anchor)))

    async def _create_agent_task(self, entity_id: int, title: str,
                                 task_prompt: str) -> tuple[int, str]:
        """Create the entity_tasks registry row for a dispatched agent and return
        (task_id, agent_session). Shared by dispatch() and dispatch_sync()."""
        tm = TaskManager(self.pool, entity_id)
        task_id = await tm.create_task(title=title, task_type="agent",
                                       assigned_to="lane_b")
        await tm.update_task_status(task_id, "in_progress")
        agent_session = f"agent:{task_id}"
        # The brief is stored WHOLE: it is the user's own record of what was
        # asked, read back by agent_status and the Agents app (a cut copy
        # would differ from the full text the agent itself got). description
        # is TEXT.
        await self.pool.execute(
            "UPDATE entity_tasks SET description = $1, session_id = $2 WHERE task_id = $3",
            task_prompt or "", agent_session, task_id,
        )
        return task_id, agent_session

    async def dispatch_sync(
        self, *,
        entity_id: int,
        title: str,
        task_prompt: str,
        tool_scope: list[str],
        grant: list[str] | None = None,
        model: str | None = None,
        provider: str | None = None,
        system_preamble: str | None = None,
        turn_type: dict | None = None,
    ) -> dict:
        """Synchronous dispatch: spawn a scoped agent, AWAIT it, record the
        outcome, and return {task_id, status, result, est_cost_usd, tokens,
        tool_actions, rounds}. The host's `dispatch` tool uses this for an inline
        answer in the same turn; the fire-and-forget `dispatch()` is for
        background work. Still registry-recorded → visible in the tray."""
        # Same rounds rule as dispatch() (agent_round_budget).
        max_rounds = (await agent_round_budget(self.pool, entity_id))["rounds"]
        task_id, agent_session = await self._create_agent_task(entity_id, title, task_prompt)
        try:
            out = await _as_agent(run_scoped_agent(
                self.pool,
                entity_id=entity_id,
                task_prompt=task_prompt,
                tool_scope=tool_scope,
                grant=grant,
                model=model,
                provider=provider,
                system_preamble=system_preamble,
                session_id=agent_session,
                max_rounds=max_rounds,
                turn_type=turn_type,
            ))
        except Exception as e:
            log.error("dispatch_sync: agent task=%d failed: %s", task_id, e, exc_info=True)
            # Per-action audit line (one per terminal state).
            log.info(
                "AGENT_ACTION task=%d eid=%d status=failed tokens=0 est_cost=$0.00000 rounds=0 tools=%s error=%s",
                task_id, entity_id, sorted(tool_scope or []), str(e)[:200],
            )
            await self.pool.execute(
                "UPDATE entity_tasks SET status='failed', error=$1, completed_at=NOW() "
                "WHERE task_id=$2 AND status IN ('queued','in_progress')",
                str(e)[:2000], task_id,
            )
            return {"task_id": task_id, "status": "failed", "result": "", "error": str(e)}
        record = _finalize_record(out, provider, model, system_preamble)
        # Status follows outcome (same rule as _run_and_record):
        # verdict pass → completed; fail / blocked / unavailable → failed.
        _verdict = record.get("verdict")
        _terminal = "completed" if _verdict == "pass" else "failed"
        _err_text = "" if _terminal == "completed" else _blocked_error_text(
            out, record, _verdict)
        # Guarded terminal write: a row the monitor already timed out (or a user
        # cancelled) must NEVER flip back — the failure was already delivered,
        # and a late overwrite makes the registry lie ("timed out but says
        # completed").
        wrote = await self.pool.execute(
            "UPDATE entity_tasks SET status=$3, error=NULLIF($4,''), result=$1::jsonb, "
            "completed_at=NOW() WHERE task_id=$2 AND status IN ('queued','in_progress')",
            json.dumps(record), task_id, _terminal, _err_text,
        )
        if str(wrote).endswith(" 0"):
            log.warning("dispatch_sync: task=%d finished after being marked "
                        "terminal elsewhere — completion write skipped", task_id)
        log.info("dispatch_sync: agent task=%d %s (verdict=%s tokens=%d est=%s)",
                 task_id, _terminal, _verdict, record["tokens"],
                 _cost_str(record["est_cost_usd"]))
        # Per-action audit line (one per terminal state).
        log.info(
            "AGENT_ACTION task=%d eid=%d status=%s verdict=%s tokens=%d est_cost=%s rounds=%d tools=%s",
            task_id, entity_id, _terminal, _verdict, record["tokens"],
            _cost_str(record["est_cost_usd"]),
            record["rounds"], sorted(tool_scope or []),
        )
        return {"task_id": task_id, "status": _terminal, "error": _err_text or None, **record}

    # Rolling live-activity feed cap: newest N tool frames, short previews only,
    # so the registry row stays small however long an agent runs.
    _ACTIVITY_CAP = 50
    _ACTIVITY_PREVIEW = 200

    # Per-agent transcript root. The registry row keeps its 50-frame/200-char
    # summary (other readers depend on it staying small); this is the full
    # stream the Agents view replays from.
    AGENT_RUNS_ROOT = Path("/var/lib/cseries/agent-runs")

    def _append_event(self, task_id: int, event: dict) -> None:
        """Append one normalized frame to <task_id>.events.jsonl.

        Best-effort by contract: transcript persistence must never fail a run,
        so every error here is swallowed (the run's terminal record is still
        authoritative). Same shape the harness runtime normalizes vendor output
        to (system-apps/_harness_runtime/events.py), so both views share
        renderers:
            {ts, seq, kind, payload}
        """
        try:
            self.AGENT_RUNS_ROOT.mkdir(parents=True, exist_ok=True)
            p = self.AGENT_RUNS_ROOT / ("%d.events.jsonl" % task_id)
            with open(p, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(event, default=str) + "\n")
        except Exception as e:                        # noqa: BLE001
            log.debug("agent event append failed (task=%s): %s", task_id, e)

    def _activity_recorder(self, task_id: int):
        """A progress_cb for run_scoped_agent that persists each tool
        start/result frame onto the agent's entity_tasks.result jsonb as a
        rolling, size-capped `activity` array — the bench's live feed (same
        write pattern as the heartbeat: keyed to the running record,
        best-effort, never fails the run). Args/results are truncated to a
        short preview; the full outcome still lands in the terminal record."""
        async def _record(event: dict) -> None:
            # Full transcript first — the JSONL is unbounded and is what
            # the Agents view streams/replays. The rolling `activity`
            # array below stays capped for the registry row.
            self._append_event(task_id, {
                "ts": datetime.now(timezone.utc).timestamp(),
                "kind": str(event.get("event") or ""),
                "payload": {k: v for k, v in event.items() if k != "event"},
            })
            kind = event.get("event")
            if kind not in ("tool_call_start", "tool_call_result"):
                return
            entry: dict = {
                "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "event": "start" if kind == "tool_call_start" else "result",
                "tool": str(event.get("tool") or "")[:80],
            }
            try:
                if kind == "tool_call_start":
                    entry["args"] = json.dumps(
                        event.get("args") or {}, default=str)[:self._ACTIVITY_PREVIEW]
                else:
                    entry["status"] = str(event.get("status") or "ok")[:20]
                    if entry["tool"] in _NO_RESULT_PREVIEW:
                        # A validator agent's read is another Host's report —
                        # text the dispatching host must never see before a
                        # verdict. agent_status(task_id) returns this list to
                        # the host, so the preview is the leak.
                        entry["result"] = "(withheld: another Host's report text)"
                    else:
                        entry["result"] = json.dumps(
                            event.get("result"), default=str)[:self._ACTIVITY_PREVIEW]
            except (TypeError, ValueError):
                pass
            try:
                await self.pool.execute(
                    "UPDATE entity_tasks SET result = jsonb_set("
                    "COALESCE(result,'{}'::jsonb), '{activity}', "
                    "(CASE WHEN jsonb_array_length(COALESCE(result->'activity','[]'::jsonb)) >= $3 "
                    "THEN COALESCE(result->'activity','[]'::jsonb) - 0 "
                    "ELSE COALESCE(result->'activity','[]'::jsonb) END) || $1::jsonb, true) "
                    "WHERE task_id=$2 AND status IN ('queued','in_progress')",
                    json.dumps(entry), task_id, self._ACTIVITY_CAP,
                )
            except Exception as e:
                log.debug("dispatch: activity write skipped (task=%d): %s", task_id, e)
        return _record

    async def _run_and_record(
        self, *, task_id, entity_id, task_prompt, tool_scope,
        model, provider, session_id, system_preamble, max_rounds,
        origin=None, grant: list[str] | None = None,
        turn_type: dict | None = None,
    ):
        """Background body: run the agent, then write the outcome to the registry
        and wake the host to deliver it. Never raises out (a failed agent records
        'failed', it does not crash the runtime). A veto (cancel) propagates
        CancelledError — cancel() owns the record update in that case and there
        is no delivery for a vetoed agent.

        The completion write MERGES onto the existing result jsonb (old || new),
        so a `delivered_at` stamped by a racing monitor delivery, the live
        `activity` feed, and the heartbeat keys all survive the terminal write —
        no double-delivery, no vanished feed."""
        tm = TaskManager(self.pool, entity_id)
        try:
            out = await _as_agent(run_scoped_agent(
                self.pool,
                entity_id=entity_id,
                task_prompt=task_prompt,
                tool_scope=tool_scope,
                grant=grant,
                model=model,
                provider=provider,
                system_preamble=system_preamble,
                session_id=session_id,
                max_rounds=max_rounds,
                turn_type=turn_type,
                # Live activity: every tool_call_start/result frame lands on the
                # record while the agent RUNS, so the bench can show WHAT it is
                # doing, not just that it runs (the heartbeat only says
                # "alive"). Rolling + size-capped — see _activity_recorder.
                progress_cb=self._activity_recorder(task_id),
            ))
            record = _finalize_record(out, provider, model, system_preamble)
            if origin:
                record["origin"] = origin
            # STATUS FOLLOWS OUTCOME. run_scoped_agent RETURNS on
            # a bad grant, an unreachable lane, a model error, a code-exec
            # block or an exhausted round cap — it does not raise — so every
            # one of those used to land here as status='completed' with only
            # `verdict` knowing the truth; the delivery then phrased it as a
            # success and the tool cards painted it green ("said completed
            # while it had failed"). A run whose verdict is
            # fail / blocked / unavailable is written FAILED, with the run's
            # own error (or its result text, which IS the error on those
            # paths) as the row's error.
            _verdict = record.get("verdict")
            _terminal = "completed" if _verdict == "pass" else "failed"
            _err_text = ""
            if _terminal == "failed":
                _err_text = _blocked_error_text(out, record, _verdict)
            # Guarded terminal write (same rule as dispatch_sync): if the monitor
            # already timed this row out — or a veto landed — the run is
            # terminal and delivered; a late overwrite would make the registry
            # contradict what the user was told.
            wrote = await self.pool.execute(
                "UPDATE entity_tasks SET status=$3, error=NULLIF($4,''), completed_at=NOW(), "
                "result = COALESCE(result,'{}'::jsonb) || $1::jsonb "
                "WHERE task_id=$2 AND status IN ('queued','in_progress')",
                json.dumps(record), task_id, _terminal, _err_text,
            )
            if str(wrote).endswith(" 0"):
                log.warning("dispatch: task=%d finished after being marked "
                            "terminal elsewhere — completion write + delivery skipped", task_id)
                return
            log.info(
                "dispatch: agent task=%d %s (verdict=%s tokens=%d est=%s)",
                task_id, _terminal, _verdict, record["tokens"],
                _cost_str(record["est_cost_usd"]),
            )
            # Per-action audit line (one per terminal state).
            log.info(
                "AGENT_ACTION task=%d eid=%d status=%s verdict=%s tokens=%d est_cost=%s rounds=%d tools=%s",
                task_id, entity_id, _terminal, _verdict, record["tokens"],
                _cost_str(record["est_cost_usd"]),
                record["rounds"], sorted(tool_scope or []),
            )
            if origin:
                await self._deliver(task_id)
        except asyncio.CancelledError:
            # User veto. cancel() already moved the record to 'cancelled'.
            log.info("dispatch: agent task=%d cancelled", task_id)
            raise
        except Exception as e:
            log.error("dispatch: agent task=%d failed: %s", task_id, e, exc_info=True)
            # Per-action audit line (one per terminal state).
            log.info(
                "AGENT_ACTION task=%d eid=%d status=failed tokens=0 est_cost=$0.00000 rounds=0 tools=%s error=%s",
                task_id, entity_id, sorted(tool_scope or []), str(e)[:200],
            )
            try:
                # Record the prompt/model/verdict on failure too (merged, so the
                # seeded origin survives) — the host needs to SEE what it tried in
                # order to re-dispatch with revised instructions.
                await self.pool.execute(
                    "UPDATE entity_tasks SET status='failed', error=$1, completed_at=NOW(), "
                    "result = COALESCE(result,'{}'::jsonb) || $2::jsonb "
                    "WHERE task_id=$3 AND status IN ('queued','in_progress')",
                    str(e)[:2000],
                    json.dumps({
                        "system_prompt": system_preamble or "",
                        "model": model or "",
                        "verdict": "fail",
                    }),
                    task_id,
                )
            except Exception as _db_err:
                log.error("dispatch: failed to record failure for task=%d: %s", task_id, _db_err)
            # The host still follows up on a failure (it tells the user the task
            # didn't complete). Best-effort — _deliver never raises out.
            if origin:
                await self._deliver(task_id)

    async def cancel(self, entity_id: int, task_id: int, *,
                     actor: str = "user_veto") -> dict:
        """Cancel a running dispatched agent and move its record to 'cancelled'.
        Idempotent — cancelling a finished agent is a no-op.
        Scoped to task_type='agent': a factory_build row shares the feed but
        cancelling it wouldn't stop the sidecar — marking it cancelled while
        the build keeps running would be a lie (the bench hides the veto on
        build cards; this is the backend wall behind that).

        `actor` is WHO cancelled — 'user_veto' (workbench Kill), or
        'entity_tasks_tool' / 'task_manage_api' (the generic task-queue
        surfaces, routed here so the running task actually stops). Recorded
        on the row as result.cancelled_by: a task once died with
        no attributable trail because nothing wrote the actor and the pod's
        process log didn't survive a restart.

        A watched MCP call (task_type 'mcp_watch', runtime/mcp_watch.py) is
        in scope too: it runs in this process like an agent, so cancelling it
        really stops it."""
        from mastercode.runtime import mcp_watch
        bg = self._running.get(task_id) or mcp_watch.running(task_id)
        if bg is not None and not bg.done():
            bg.cancel()
        await self.pool.execute(
            "UPDATE entity_tasks SET status='cancelled', completed_at=NOW(), "
            "result = jsonb_set(COALESCE(result,'{}'::jsonb), "
            "'{cancelled_by}', to_jsonb($3::text), true) "
            "WHERE task_id=$1 AND entity_id=$2 AND task_type IN ('agent', $4) "
            "AND status IN ('queued','in_progress')",
            task_id, entity_id, actor, mcp_watch.TASK_TYPE,
        )
        log.info("dispatch: agent task=%d cancelled by %s (eid=%d)",
                 task_id, actor, entity_id)
        return {"task_id": task_id, "status": "cancelled", "cancelled_by": actor}

    # -----------------------------------------------------------------
    # Async lifecycle: deliver completed agents back to the user
    # -----------------------------------------------------------------

    async def _deliver(self, task_id: int, *, deliver_prior: bool = False) -> None:
        """Wake the host to deliver a terminal agent's outcome to the user.

        Called from _run_and_record (both completion + failure) and from the
        monitor loop (the safety net). At-most-once: the delivered_at marker is
        CLAIMED atomically before the host turn runs, so a monitor tick and the
        in-process completion can never both deliver the same agent. If the host
        delivery raises, the claim is released so the monitor retries — no
        orphan. Never raises out."""
        if self.runtime is None:
            return
        try:
            await self._wait_for_prior_report(task_id, deliver_prior=deliver_prior)
            row = await self.pool.fetchrow(
                DELIVERY_ROW_SQL,
                task_id,
            )
            if row is None:
                return
            res = row["result"]
            if isinstance(res, str):
                try:
                    res = json.loads(res)
                except Exception:
                    res = {}
            res = res if isinstance(res, dict) else {}
            if res.get("delivered_at"):
                return  # already delivered (fast path before the atomic claim)
            if not res.get("origin"):
                return  # no back-ref → a dispatch_sync run, not async-delivered

            # Atomic claim — stamp delivered_at iff still absent. RETURNING tells
            # us whether THIS caller won the claim.
            claimed = await self.pool.fetchval(
                "UPDATE entity_tasks SET result = jsonb_set(COALESCE(result,'{}'::jsonb), "
                "'{delivered_at}', to_jsonb(now()::text), true) "
                "WHERE task_id=$1 AND status IN ('completed','failed') "
                "AND NOT (COALESCE(result,'{}'::jsonb) ? 'delivered_at') "
                # A build still finishing its delivery (waiting on the forge's
                # CI before it merges and installs, tools._hold_report) holds
                # its report until then; the stamp expires, so a restart
                # mid-wait still delivers.
                "AND COALESCE((result->>'deliver_after')::timestamptz, '-infinity') <= now() "
                "RETURNING task_id",
                task_id,
            )
            if claimed is None:
                return  # lost the claim (another caller delivered) or not terminal

            # Everything after the claim sits inside one try/finally: an error
            # or a cancellation anywhere here (the lock wait included) releases
            # the claim for the monitor, the lock, and whoever waits on this
            # report — a claimed-but-unwritten report is lost for good.
            self._delivering.add(task_id)
            lock = None
            locked = False
            delivered = False
            try:
                record = delivery_record(row, res)
                # Reports into one thread are written in the order their tasks
                # were claimed. Once a
                # build's report (then a ~9 s phrasing model call) landed
                # after the fixed-text proof its auto-ship started, so the
                # proof's "installed and running" came first and the build's
                # older "not installed" after it. The phrasing call is gone
                # (the Host carries on from the result itself),
                # and the order still holds for the rows written here.
                lock = self._thread_locks.setdefault(
                    delivery_thread_key(record.get("origin")), asyncio.Lock())
                try:
                    await asyncio.wait_for(lock.acquire(), timeout=DELIVERY_ORDER_WAIT_S)
                    locked = True
                except asyncio.TimeoutError:
                    # Never hold a report back for good behind a stuck one.
                    log.warning("deliver: task=%d waited %ss for its thread; delivering anyway",
                                task_id, DELIVERY_ORDER_WAIT_S)
                # A result a holding turn was handed and never read comes back
                # through here (redeliver_unread), with whether to wake a turn
                # for it kept on the row — so a monitor retry honours it too.
                _ur = res.get("unread_redelivery")
                if isinstance(_ur, dict):
                    await self.runtime.deliver_agent_result(
                        record, _unread_wake=bool(_ur.get("wake")))
                else:
                    await self.runtime.deliver_agent_result(record)
                delivered = True
                log.info("deliver: agent task=%d delivered to host", task_id)
                try:
                    await self._maybe_continue_round(task_id, res, record["origin"])
                except Exception as _rc:
                    log.warning("deliver: round continuation check failed (task=%d): %s",
                                task_id, _rc)
            except Exception as e:
                log.error("deliver: host delivery failed for task=%d: %s",
                          task_id, e, exc_info=True)
            finally:
                if locked:
                    lock.release()
                if not delivered:
                    # Release the claim so the monitor retries — never leave an
                    # agent marked-delivered when the user never got it. Unless
                    # the report row already exists (interrupted after the
                    # write, in the inbox/broadcast tail): a retry would post
                    # it twice. deliver_agent_result tags it task_id + origin.
                    # In PII mode the row write is queued, so this can miss it
                    # and fall back to the retry (a possible duplicate, never
                    # a loss).
                    try:
                        written = await self.pool.fetchval(
                            "SELECT 1 FROM entity_memory WHERE memory_type='turn' "
                            "AND metadata->>'origin' = 'agent' "
                            "AND metadata->>'task_id' = $1::text LIMIT 1",
                            str(task_id))
                        delivered = bool(written)
                    except BaseException as _w:  # noqa: BLE001 — unknown: retry
                        log.debug("deliver: written-row check failed for task=%d: %s",
                                  task_id, _w)
                if not delivered:
                    try:
                        await self.pool.execute(
                            "UPDATE entity_tasks SET result = result - 'delivered_at' "
                            "WHERE task_id=$1",
                            task_id,
                        )
                    except BaseException as _u:  # noqa: BLE001 — incl. a second cancel
                        log.error("deliver: un-claim failed for task=%d: %s", task_id, _u)
                self._delivering.discard(task_id)
                ev = self._delivered_ev.pop(task_id, None)
                if ev is not None:
                    ev.set()
        except Exception as e:
            log.error("deliver: unexpected error for task=%d: %s", task_id, e, exc_info=True)

    async def redeliver_unread(self, task_id: int, *, wake: bool) -> None:
        """A result handed to a holding turn that ended before reading it (a
        Stop, a crash, the turn ceiling — session._deliver_unread). It goes
        back through the ONE delivery door: the
        claim is released with the wake decision kept on the row, then taken
        again by _deliver, which writes the row under the thread's order lock
        and lets delivered_at stand only once it has — a failed write is
        un-claimed and the monitor retries it, never a dropped result."""
        try:
            await self.pool.execute(
                "UPDATE entity_tasks SET result = (COALESCE(result,'{}'::jsonb) - 'delivered_at') "
                "|| jsonb_build_object('unread_redelivery', "
                "jsonb_build_object('wake', $2::boolean)) WHERE task_id = $1",
                int(task_id), bool(wake))
        except Exception as e:  # noqa: BLE001
            log.error("deliver: task=%s could not be released for re-delivery: %s", task_id, e)
            return
        await self._deliver(int(task_id))

    async def _wait_for_prior_report(self, task_id: int, *, deliver_prior: bool = False) -> None:
        """Hold this task's report until the one it follows is written
        (origin.after_task_id). Runs BEFORE the claim and the thread lock, so
        the report it waits on can take both. Bounded by DELIVERY_ORDER_WAIT_S;
        never raises.

        deliver_prior=True (the boot reap): nothing else in this process will
        deliver the earlier report yet — the monitor starts after the reap —
        so deliver it here instead of waiting. Only safe where the earlier
        task's row is final; during a live build its auto-deliver results are
        still being written onto it."""
        try:
            origin = await self.pool.fetchval(
                "SELECT result->'origin' FROM entity_tasks WHERE task_id=$1", task_id)
            if isinstance(origin, str):
                origin = json.loads(origin)
            if isinstance(origin, str):  # stored double-encoded on older rows
                origin = json.loads(origin)
            after = (origin or {}).get("after_task_id") if isinstance(origin, dict) else None
            if after is None:
                return
            after = int(after)
            if after not in self._delivering:
                # A row with no origin is never delivered (see _deliver), so
                # there is nothing to wait for.
                done = await self.pool.fetchval(
                    "SELECT (result ? 'delivered_at') OR NOT (result ? 'origin') "
                    "FROM entity_tasks WHERE task_id=$1", after)
                if done or done is None:
                    return  # already written (or no such row): nothing to wait for
                if deliver_prior:
                    await self._deliver(after)
                    return
            ev = self._delivered_ev.setdefault(after, asyncio.Event())
            await asyncio.wait_for(ev.wait(), timeout=DELIVERY_ORDER_WAIT_S)
        except asyncio.TimeoutError:
            log.warning("deliver: task=%d waited %ss for the report it follows; delivering",
                        task_id, DELIVERY_ORDER_WAIT_S)
        except Exception as e:  # noqa: BLE001 — ordering is best-effort, delivery is not
            log.debug("deliver: prior-report wait skipped for task=%d: %s", task_id, e)

    async def reap_orphans_on_boot(self) -> None:
        """Restart safety net — run ONCE at startup, before the monitor loop.

        Agent and factory-build tasks live only in THIS process's memory
        (self._running / tools._FACTORY_BUILD_TASKS), so at boot every
        'queued'/'in_progress' registry row is by definition an orphan from a
        dead process. Without this sweep an orphaned build showed a live
        "running" card AND held the per-project in-flight guard for hours
        (until the stuck horizon), and an orphaned agent spun forever —
        restart-safety must not wait on a wall clock when the truth is knowable
        immediately. Mark them failed and deliver (delivery no-ops for rows
        without an origin back-ref, same as the monitor)."""
        try:
            rows = await self.pool.fetch(
                "UPDATE entity_tasks SET status='failed', completed_at=NOW(), "
                "error='kernel restarted mid-run — the running task did not survive' "
                # app_verify: a proof is a detached in-process
                # task (app_verify._PROOF_TASKS) exactly like the other three,
                # so it cannot survive a restart either. It was missing here,
                # leaving only the 4h stuck floor to cover it — and once
                # start_verify refuses to open a second proof while one is in
                # flight, an orphaned row stops being a stale bench card and
                # starts blocking the app's next delivery proof for those 4
                # hours. That is the same "held the in-flight guard for hours"
                # failure this boot reap was written for.
                # mcp_watch: a watched MCP call is an in-process
                # task too (mcp_watch._running). Reaped and delivered, the
                # Host learns its watch ended and can start it again.
                "WHERE task_type IN ('agent','factory_build','fan_out','app_verify','mcp_watch') "
                "AND status IN ('queued','in_progress') "
                "RETURNING task_id",
            )
        except Exception as e:
            log.warning("boot reap: scan failed (non-fatal): %s", e)
            return
        # A phase can only be in_progress while a build runs in THIS process,
        # so at boot every in_progress phase (and its implementing steps) is
        # an orphan too — without this the workflow pane showed a phase
        # "in progress" forever after a restart.
        try:
            n = await self.pool.execute(
                "UPDATE project_phases SET status='failed' WHERE status='in_progress'")
            await self.pool.execute(
                "UPDATE phase_steps SET status='failed' WHERE status='implementing'")
            if not str(n).endswith(" 0"):
                log.warning("boot reap: %s — phases in_progress at startup marked failed", n)
        except Exception as e:
            log.warning("boot reap: phase reset failed (non-fatal): %s", e)
        # The build's OTHER record. The rows above are the registry; a fan-out
        # or build ALSO keeps per_harness_state.json in its review-session dir,
        # and nothing here used to touch it — so a roll that killed a run left
        # the task correctly 'failed' while the session file still read
        # 'running' (still "running"
        # 37 minutes after the pod was recreated). Same sweep, same wording, so
        # the two records cannot tell the user different stories.
        # Imported lazily: agent_dispatch is runtime-side and review is a
        # server route — a module-level import would couple them at startup.
        try:
            from server.routes.review import reap_orphaned_sessions_on_boot
            reap_orphaned_sessions_on_boot()
        except Exception as e:
            log.warning("boot reap: session reap failed (non-fatal): %s", e)
        for r in rows:
            log.warning("boot reap: task=%d was in-flight at startup — marked "
                        "failed, delivering", r["task_id"])
            # deliver_prior: a proof reaped here would otherwise wait out its
            # bound for a build report nothing delivers until the monitor
            # starts — after this loop. Every row is final after a restart.
            await self._deliver(r["task_id"], deliver_prior=True)
        # A watched call's answer that a turn was carrying when the process
        # died (session._deliver_watch_result keeps `wake_pending` on the row
        # until that turn has ended): nothing about it was written, so it is
        # delivered again, with a line in its brief saying so. `redelivered`
        # counts the restarts it was carried across: past
        # mcp_watch.RESTART_WAKES it goes the way an answer whose turn the
        # person stopped goes (unread_redelivery wake=false: written in the
        # thread, no turn woken), so an answer whose turn brings the process
        # down cannot wake that turn at every boot.
        from mastercode.runtime import mcp_watch
        try:
            carried = await self.pool.fetch(
                "UPDATE entity_tasks t SET result = jsonb_set("
                "(t.result - 'delivered_at' - 'wake_pending' - 'unread_redelivery'), "
                "'{watch,redelivered}', to_jsonb(c.n), true) "
                "|| CASE WHEN c.n > $1 THEN jsonb_build_object('unread_redelivery', "
                "jsonb_build_object('wake', false)) ELSE '{}'::jsonb END "
                "FROM (SELECT task_id, COALESCE(CASE jsonb_typeof(result->'watch'->'redelivered') "
                "WHEN 'number' THEN (result->'watch'->>'redelivered')::int "
                "WHEN 'boolean' THEN 1 END, 0) + 1 AS n FROM entity_tasks "
                "WHERE task_type = 'mcp_watch' AND status IN ('completed','failed') "
                "AND result ? 'wake_pending') c "
                "WHERE t.task_id = c.task_id RETURNING t.task_id, c.n",
                mcp_watch.RESTART_WAKES)
        except Exception as e:
            log.warning("boot reap: watch answers in flight not re-delivered: %s", e)
            carried = []
        for r in carried:
            log.warning("boot reap: watch task=%d's answer was in a turn at startup (restart "
                        "%d) — delivering it again%s", r["task_id"], r["n"],
                        "" if r["n"] <= mcp_watch.RESTART_WAKES else ", written and not woken")
            await self._deliver(r["task_id"], deliver_prior=True)

    async def run_monitor_loop(self, *, interval_seconds: int = 30,
                               stuck_after_seconds: int = 600) -> None:
        """No-orphans safety net. Every `interval_seconds`: deliver any
        completed/failed-but-undelivered async agent (covers a dropped
        in-process handler), and mark + deliver any async agent stuck
        in_progress past `stuck_after_seconds`. Idempotent + self-healing;
        a tick error is logged, never fatal."""
        log.info(
            "AgentDispatch monitor loop started (interval=%ds, stuck_after=%ds)",
            interval_seconds, stuck_after_seconds,
        )
        while True:
            try:
                await asyncio.sleep(interval_seconds)
                await self._monitor_tick(stuck_after_seconds)
            except asyncio.CancelledError:
                log.info("AgentDispatch monitor loop cancelled")
                raise
            except Exception as e:
                log.error("AgentDispatch monitor tick failed (non-fatal): %s", e, exc_info=True)

    async def _monitor_tick(self, stuck_after_seconds: int) -> None:
        """One pass of the monitor. Scoped to async agents (`result ? 'origin'`)
        so dispatch_sync runs are never touched."""
        if self.runtime is None:
            return

        # (b) Stuck in_progress past the timeout → cancel + mark failed, then
        #     deliver a status. COALESCE(started_at, created_at) guards a NULL
        #     started_at.
        try:
            # Stuck = NO SIGN OF LIFE for the window — not "has run this long".
            # The old age-since-start scan guillotined every legitimately-long
            # agent at the window (10 min) while ignoring the heartbeat the
            # runner stamps per tool call AND per model round (scoped_agent
            # _heartbeat). Caps protect the platform from wedges, never
            # throttle real work: a heartbeating agent runs as long as it
            # needs; a silent one is dead and gets reaped.
            stuck = await self.pool.fetch(
                "SELECT task_id FROM entity_tasks "
                "WHERE task_type='agent' AND status='in_progress' "
                "AND result IS NOT NULL AND (result ? 'origin') "
                "AND GREATEST("
                "  COALESCE(NULLIF(result->>'last_heartbeat_at','')::timestamptz, 'epoch'::timestamptz),"
                "  COALESCE(started_at, created_at)"
                ") < NOW() - make_interval(secs => $1)",
                float(stuck_after_seconds),
            )
        except Exception as e:
            log.warning("monitor: stuck scan failed: %s", e)
            stuck = []
        for r in stuck:
            tid = r["task_id"]
            bg = self._running.get(tid)
            if bg is not None and not bg.done():
                bg.cancel()
            try:
                # The error SAYS what happened:
                # the agent showed no sign of life for the window. "timed out"
                # read as a wall clock on a long run, which this is not.
                await self.pool.execute(
                    "UPDATE entity_tasks SET status='failed', error=$2, "
                    "completed_at=NOW() WHERE task_id=$1 AND status='in_progress'",
                    tid, no_sign_of_life_text(stuck_after_seconds),
                )
                log.warning("monitor: task=%d no sign of life for %ds — marked failed",
                            tid, stuck_after_seconds)
            except Exception as e:
                log.warning("monitor: failed to mark task=%d timed out: %s", tid, e)
                continue
            await self._deliver(tid)

        # (a) Completed/failed but undelivered → deliver (dropped handler, or a
        #     freshly-timed-out run from this same tick). Covers BOTH dispatched
        #     agents AND factory builds (harness pool): a build now runs as a detached
        #     kernel task and carries an `origin` back-ref, so its
        #     terminal outcome flows through the SAME _deliver path — the host
        #     reports the build in the originating project thread and, on
        #     success, offers the disposition. NOTHING terminal goes undelivered.
        try:
            pending = await self.pool.fetch(
                "SELECT task_id FROM entity_tasks "
                "WHERE task_type IN ('agent','factory_build','fan_out','app_verify','mcp_watch') "
                "AND status IN ('completed','failed') "
                "AND result IS NOT NULL AND (result ? 'origin') "
                "AND NOT (result ? 'delivered_at') "
                "AND COALESCE((result->>'deliver_after')::timestamptz, '-infinity') <= now() "
                "ORDER BY completed_at ASC NULLS LAST LIMIT 50",
            )
        except Exception as e:
            log.warning("monitor: pending scan failed: %s", e)
            pending = []
        for r in pending:
            await self._deliver(r["task_id"])

        # (c) Stuck factory builds: a build runs as a detached kernel task, so
        #     a kernel crash mid-build (or a wedged pod run) leaves the row
        #     in_progress forever — a "running" card that lies. Builds
        #     legitimately run long, so the horizon is generous (4h, well past
        #     the 30min pod cap). Past it: cancel the detached task if this
        #     process still holds it, mark failed, AND deliver. Covers
        #     origin-less rows too (main's behavior) — _deliver simply no-ops
        #     without an origin back-ref, but the row stops lying.
        #     A fan-out row is the same shape: a detached watcher
        #     in tools._FAN_OUT_TASKS, a timeout_s sized to the session budget
        #     (+ review's 900 s overhead), and the same reap + deliver.
        try:
            fstuck = await self.pool.fetch(
                "SELECT task_id FROM entity_tasks "
                # app_verify: a proof is a detached task too
                # (mastercode.runtime.app_verify._PROOF_TASKS), sized by its
                # own window (result.timeout_s), reaped + delivered the same way.
                "WHERE task_type IN ('factory_build','fan_out','app_verify') AND status='in_progress' "
                # Horizon scales with the run's OWN budget (2x, floor $1): a
                # 4h-budget build is not stuck at the 4h floor; a wedged one
                # with no recorded budget still reaps at the floor.
                "AND COALESCE(started_at, created_at) < NOW() - make_interval("
                "secs => GREATEST($1, 2.0 * COALESCE((result->>'timeout_s')::float, 0)))",
                float(self.FACTORY_STUCK_AFTER_SECONDS),
            )
        except Exception as e:
            log.warning("monitor: factory-build stuck scan failed: %s", e)
            fstuck = []
        for r in fstuck:
            tid = r["task_id"]
            # A build THIS process is still running is never reaped: its
            # liveness is the run's own (the
            # harness's silence window, a phase's declared checks), and a
            # phase whose checks run past the horizon was reaped mid-work by
            # this wall clock. The horizon is for rows no live task holds —
            # the kernel-restart case it was written for.
            try:
                from mastercode.runtime.tools import _FACTORY_BUILD_TASKS, _FAN_OUT_TASKS
                from mastercode.runtime.app_verify import _PROOF_TASKS
                bg = {**_FACTORY_BUILD_TASKS, **_FAN_OUT_TASKS, **_PROOF_TASKS}.get(tid)
                if bg is not None and not bg.done():
                    log.info("monitor: build task=%d is past the stuck horizon but its "
                             "task is alive in this process — not reaped", tid)
                    continue
            except Exception as e:
                log.warning("monitor: build-task liveness lookup failed for %d: %s", tid, e)
            try:
                await self.pool.execute(
                    "UPDATE entity_tasks SET status='failed', "
                    "error='build stuck (kernel restarted or pod run wedged)', "
                    "completed_at=NOW() WHERE task_id=$1 AND status='in_progress'",
                    tid,
                )
                log.warning("monitor: factory build task=%d stuck — marked failed", tid)
            except Exception as e:
                log.warning("monitor: failed to mark build task=%d stuck: %s", tid, e)
                continue
            # The PHASE follows its row. A reaped
            # build whose task is gone (a kernel restart) left its phase
            # 'in_progress' with nothing building it: the door then said
            # "already building" and every phase waiting on it never started.
            # Failed, it is buildable again — only if no other live build of
            # that phase exists.
            try:
                await self.pool.execute(
                    "UPDATE project_phases SET status='failed' "
                    "WHERE id = (SELECT (result->>'phase_id')::int FROM entity_tasks "
                    "            WHERE task_id=$1 AND task_type='factory_build') "
                    "AND status='in_progress' "
                    "AND NOT EXISTS (SELECT 1 FROM entity_tasks t WHERE t.task_type='factory_build' "
                    "  AND t.status='in_progress' AND t.task_id <> $1 "
                    "  AND (t.result->>'phase_id')::int = project_phases.id)",
                    tid,
                )
            except Exception as e:
                log.warning("monitor: phase release for reaped build task=%d failed: %s", tid, e)
            await self._deliver(tid)

    async def list_agents(self, entity_id: int, *, limit: int = 20,
                          active_only: bool = False) -> list[dict]:
        """Registry read for the taskbar tray + the bench: the entity's dispatched
        agents AND factory builds (harness pool) (task_type='factory_build' — recorded
        by the `build` tool so a build is visible while the pool harness
        works), newest first, each with status / output / cost /
        failure. model_stats stays agents-only — a factory build says nothing
        about the model the agent ran on."""
        # age_s / hb_age_s are computed SERVER-SIDE so the tray never has to
        # parse naive timestamps across timezones: airtime for a running agent and
        # seconds-since-last-heartbeat for the liveness/stall indicator.
        q = (
            "SELECT task_id, title, description, task_type, status, result, error, "
            "created_at, started_at, completed_at, "
            "EXTRACT(EPOCH FROM (NOW() - COALESCE(started_at, created_at)))::int AS age_s, "
            "CASE WHEN result ? 'last_heartbeat_at' "
            "THEN EXTRACT(EPOCH FROM (NOW() - (result->>'last_heartbeat_at')::timestamptz))::int "
            "ELSE NULL END AS hb_age_s "
            # app_verify rows ride the same registry: the bench
            # shows the 'prove: <app>' card and the host's agent_status can
            # read the proof envelope — the seed a failed proof leaves for it.
            "FROM entity_tasks WHERE entity_id=$1 "
            "AND task_type IN ('agent','factory_build','fan_out','app_verify') "
        )
        if active_only:
            q += "AND status IN ('queued','in_progress') "
        q += "ORDER BY created_at DESC LIMIT $2"
        try:
            rows = await self.pool.fetch(q, entity_id, limit)
        except Exception as e:
            log.warning("dispatch: list_agents failed (eid=%d): %s", entity_id, e)
            return []
        return [self._row_to_agent(r) for r in rows]

    @staticmethod
    def _row_to_agent(r) -> dict:
        res = r["result"]
        if isinstance(res, str):
            try:
                res = json.loads(res)
            except Exception:
                res = None
        res = res if isinstance(res, dict) else {}
        return {
            "task_id": r["task_id"],
            "title": r["title"],
            "task": r["description"],
            # 'agent' (dispatched agent) or 'factory_build' (harness pool) — the bench renders
            # builds distinctly and hides the veto (killing the row wouldn't
            # stop the sidecar). Absent key (older callers) reads as 'agent'.
            "task_type": r["task_type"] if "task_type" in r.keys() else "agent",
            "status": r["status"],
            # A factory build's project scope, so the bench can show it under
            # the selected project as well as 'all work'.
            "project_id": res.get("project_id"),
            # WHERE this run's report was delivered. A factory
            # build stamps project_id at the top level; a DISPATCHED agent
            # carries its scope only in result.origin — the same dict
            # deliver_agent_result routes the delivery turn by. Without these
            # the bench could say a task finished but never which thread its
            # report went to, so the Workbench polled the thread that happened
            # to be open and, finding nothing, told the user the report "could
            # not be shown here" about one that had landed correctly
            # elsewhere. A notice that fires wrongly is worse than no notice.
            # Named `origin_*` on purpose: `app_id` below is already the app a
            # proof row is ABOUT, which is a different thing.
            "origin_app_id": (res.get("origin") or {}).get("app_id"),
            "origin_project_id": (res.get("origin") or {}).get("project_id"),
            # Factory builds: WHICH coding harness this build runs/ran on —
            # stamped at open (tools._factory_build_task_start) and confirmed
            # at finish, so the card never has to infer it from error prose.
            "harness": res.get("harness"),
            # Fan-out rows (task_type 'fan_out'): the review session the row
            # mirrors and the variant list, so a chat chip that only knows its
            # task_id (a reloaded one written before the chip carried
            # session_id) can still mount one terminal per harness.
            "session_id": res.get("session_id") if res.get("run_kind") == "fan_out" else None,
            "harnesses": res.get("harnesses") if res.get("run_kind") == "fan_out" else None,
            "result": res.get("result"),
            "tool_actions": res.get("tool_actions", []),
            "tokens": res.get("tokens", 0),
            "est_cost_usd": res.get("est_cost_usd", 0.0),
            "rounds": res.get("rounds", 0),
            # Factory builds (tools._build_usage_for_task): the BILLED cost and
            # where the token count came from. `cost_usd` is the compute price
            # the bridge recorded for the monthly bill (cost_method 'billed');
            # est_cost_usd above is an agent's estimate. `tokens` is None when
            # the harness reported no usage.
            "cost_usd": res.get("cost_usd"),
            "cost_method": res.get("cost_method"),
            "token_source": res.get("token_source"),
            # What the host learns from: the prompt it wrote, the model that ran,
            # and the pass/fail verdict (present once the run reaches a terminal
            # state; system_prompt is also seeded at dispatch for live view).
            "system_prompt": res.get("system_prompt", ""),
            "model": res.get("model", ""),
            # Where that model came from (mastercode/agent_work): {from, work,
            # model, label, preference?} — named on the dispatch, the
            # preference for the kind of untrusted work, or the Host's own.
            "model_choice": res.get("model_choice"),
            "verdict": res.get("verdict"),
            # Present on a 'cancelled' row: WHO cancelled it (user_veto /
            # entity_tasks_tool / task_manage_api) — a kill must be attributable.
            "cancelled_by": res.get("cancelled_by"),
            # Present on a 'blocked' verdict: the tool(s) the dispatch could not
            # honour (code-exec stripped, out of grant, or grant too narrow) and
            # scoped_agent's one-sentence reason — the tray shows WHY it blocked.
            "denied_tools": res.get("denied_tools", []),
            "blocked_reason": res.get("blocked_reason") or "",
            # The live tool-by-tool feed (rolling, capped — _activity_recorder):
            # what a RUNNING agent is doing, and the trail a finished one left.
            "activity": res.get("activity", []),
            # Factory builds: build-contract violations left after the fix loop.
            "violations": res.get("violations", []),
            # Factory builds: the SAVE POINT this phase produced — the commit it
            # made on host/build and the rolling PR that commit is now in
            # (project_git.sync_project_repo, merged onto the row by
            # tools._run_phase_build). It has been written since the pipeline
            # shipped and projected nowhere, so a phase could commit and open a
            # PR with the bench showing no trace of either — which reads as
            # "nothing happened" ("no PR, nothing").
            "repo_sync": res.get("repo_sync"),
            # app_verify rows: the proof envelope + the repair
            # decision, so the bench card and agent_status show the verdict,
            # the log tail and the seed the host dispatches from.
            "app_id": res.get("app_id"),
            "verify": res.get("verify"),
            "repair": res.get("repair"),
            # Set only on the phase that COMPLETED the plan, and only with
            # auto-merge on: whether the host merged that PR, or why it didn't.
            "auto_merge": res.get("auto_merge"),
            # Liveness: last activity stamp + the tool it was running, so the
            # tray can show a running agent is progressing and flag a stall.
            "last_heartbeat_at": res.get("last_heartbeat_at"),
            "hb_tool": res.get("hb_tool"),
            "age_s": r["age_s"] if "age_s" in r.keys() else None,
            "hb_age_s": r["hb_age_s"] if "hb_age_s" in r.keys() else None,
            "delivered_at": res.get("delivered_at"),
            "error": r["error"],
            "created_at": r["created_at"].isoformat() if r["created_at"] else None,
            "started_at": r["started_at"].isoformat() if r["started_at"] else None,
            "completed_at": r["completed_at"].isoformat() if r["completed_at"] else None,
        }

    # The keys a LIST row keeps. Everything else on a _row_to_agent record is
    # payload — result, system_prompt, activity, tool_actions, verify/repair
    # envelopes — that belongs to ONE task's drill-in, not to a queue listing.
    _SUMMARY_KEYS = (
        "task_id", "title", "task_type", "status", "project_id",
        "origin_app_id", "origin_project_id", "harness", "session_id",
        "harnesses", "model", "model_choice", "verdict", "tokens", "est_cost_usd", "rounds",
        "cost_usd", "cost_method", "token_source",
        "cancelled_by", "denied_tools", "blocked_reason", "app_id",
        "auto_merge", "last_heartbeat_at", "hb_tool", "age_s", "hb_age_s",
        "delivered_at", "created_at", "started_at", "completed_at",
    )
    _PREVIEW_CHARS = 300

    @classmethod
    def summarize_agent(cls, row: dict) -> dict:
        """A compact listing row: the summary keys, a bounded preview of the
        task text / result / error, and the sizes of what was left out. The
        host's `agent_status` list carried every row in full and measured
        130 KB for ten rows — truncated by the tool
        result cap before the host could read the one field it wanted. The
        full record is one call away: agent_status(task_id=N)."""
        if not isinstance(row, dict):
            return {}
        out = {k: row.get(k) for k in cls._SUMMARY_KEYS if k in row}

        def _clip(v) -> str:
            s = v if isinstance(v, str) else (
                json.dumps(v, default=str) if v not in (None, "", [], {}) else "")
            return s if len(s) <= cls._PREVIEW_CHARS else s[:cls._PREVIEW_CHARS - 1] + "…"

        out["task"] = _clip(row.get("task"))
        out["result_preview"] = _clip(row.get("result"))
        _res = row.get("result")
        out["result_chars"] = len(_res if isinstance(_res, str)
                                  else json.dumps(_res, default=str)) if _res not in (None, "") else 0
        # A blocked run's error LEADS with blocked_reason (the retry line the
        # host acts on); the list row already carries blocked_reason beside
        # it, so the preview shows what follows — the agent's own report.
        _err = row.get("error")
        _br = row.get("blocked_reason")
        if isinstance(_err, str) and isinstance(_br, str) and _br and _err.startswith(_br):
            _err = _err[len(_br):].lstrip(" \n")
        out["error"] = _clip(_err)
        out["activity_count"] = len(row.get("activity") or [])
        out["tool_actions_count"] = len(row.get("tool_actions") or [])
        return out

    async def model_stats(self, entity_id: int, *, limit: int = 200) -> list[dict]:
        """Per-model outcome rollup over recent terminal dispatched-agent runs — the host's
        substrate for 'which model is best for this kind of task' (W2). Aggregates
        the model + verdict + tokens + est_cost stored on each run record (W1).
        Best model first (highest pass-rate, then fewest avg tokens).
        Verdict 'unavailable' (the lane couldn't serve — subscription
        disconnected, unconfigured route) and 'blocked' (a code-exec tool put
        in a dispatched-agent scope — an orchestration mistake) are each counted
        separately and EXCLUDED from pass_rate: neither says anything about the
        model."""
        try:
            rows = await self.pool.fetch(
                "SELECT result FROM entity_tasks WHERE entity_id=$1 AND task_type='agent' "
                "AND status IN ('completed','failed') ORDER BY created_at DESC LIMIT $2",
                entity_id, limit,
            )
        except Exception as e:
            log.warning("dispatch: model_stats failed (eid=%d): %s", entity_id, e)
            return []
        agg: dict[str, dict] = {}
        for r in rows:
            res = r["result"]
            if isinstance(res, str):
                try:
                    res = json.loads(res)
                except Exception:
                    res = None
            if not isinstance(res, dict) or not res.get("model"):
                continue
            a = agg.setdefault(res["model"], {
                "model": res["model"], "runs": 0, "passes": 0, "fails": 0,
                "unavailable": 0, "blocked": 0, "_tokens": 0, "_cost": 0.0,
            })
            verdict = res.get("verdict")
            if verdict == "unavailable":
                # Infra miss — visible, but never scored for or against the model.
                a["unavailable"] += 1
                continue
            if verdict == "blocked":
                # Orchestration mistake (a code-exec tool in a dispatched-agent scope) — like
                # 'unavailable', it says nothing about the model, so it's counted
                # separately and EXCLUDED from pass_rate.
                a["blocked"] += 1
                continue
            a["runs"] += 1
            if verdict == "pass":
                a["passes"] += 1
            else:
                a["fails"] += 1
            a["_tokens"] += int(res.get("tokens") or 0)
            a["_cost"] += float(res.get("est_cost_usd") or 0.0)
        out = []
        for a in agg.values():
            if not a["runs"] and not a["unavailable"] and not a["blocked"]:
                continue
            runs = a["runs"] or 1
            out.append({
                "model": a["model"], "runs": a["runs"],
                "passes": a["passes"], "fails": a["fails"],
                "unavailable": a["unavailable"],
                "blocked": a["blocked"],
                "pass_rate": round(a["passes"] / runs, 2),
                "avg_tokens": int(a["_tokens"] / runs),
                "avg_cost_usd": round(a["_cost"] / runs, 6),
            })
        out.sort(key=lambda x: (-x["pass_rate"], x["avg_tokens"]))
        return out
