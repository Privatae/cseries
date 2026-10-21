#!/usr/bin/env python3
"""What scheduled work spends, said where the person reads it.

Background work spends money the person does not see unless it is shown. A
scheduled run now records what its turns cost (calendar_event_runs.cost_usd,
v251), its report in the thread says it, calendar_read op='runs' and the
Agents app return it, and a job the person gave a budget ("spend up to $5")
stops when its priced runs reach it.

  C  a run's cost is stored and read back by calendar_read; null stays null
     (unpriced), never 0;
  R  the thread report carries the cost; a budget stop reads "stopped", not
     "failed" or "skipped";
  B  budget_state counts priced runs only; a reached budget stops the task
     before its turn, for good;
  A  the Agents list gives the day's priced spend and counts the unpriced runs;
  S  schedule_task takes budget_usd on the prompt lane only, a positive number;
  V  a recurring task that runs a model is set only on the person's click;
     a one-time task and a no-model op are set at once;
  T  a reach-out trigger on a runbook channel is added only on the person's click;
  K  the housekeeping skill exists, is off until the person asks, and never
     starts work or changes a limit.

Real database: the run rows, the sums and the Agents query are SQL.

Run:  python3 scripts/test_scheduled_spend_pg.py
      (TEST_PG_DSN / PEER_TEST_DSN / TEST_DSN names a server; without one it
      starts a throwaway one through docker, as the other _pg gates do.)
"""
import asyncio
import json
import contextlib
import os
import subprocess
import sys
from pathlib import Path

_ROOT = os.environ.get("APP_ROOT") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path[:0] = [_ROOT, os.path.join(_ROOT, "brain"), os.path.join(_ROOT, "scripts")]
ROOT = Path(_ROOT)

SERVER = os.environ.get("TEST_PG_DSN") or os.environ.get("PEER_TEST_DSN") or os.environ.get("TEST_DSN")
if not SERVER and subprocess.run(["docker", "info"], capture_output=True).returncode != 0:
    print("SKIP-LOUD: no test server and docker unavailable — this gate needs a real database.")
    sys.exit(2)

FAILS = []
CHECKS = 0


def check(cond, msg, extra=""):
    global CHECKS
    CHECKS += 1
    print(("  PASS  " if cond else "  FAIL  ") + msg + (("\n        " + str(extra)[:400]) if extra else ""))
    if not cond:
        FAILS.append(msg)


async def main(dsn: str) -> None:
    import asyncpg
    from server import brain_service as BS
    from server.bridge import memory_handlers as MH
    from mastercode.brain import offline_task_runner as OTR
    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=4)
    saved = (BS._pool, BS._entity_id, OTR.post_to_origin_thread)
    BS._pool, BS._entity_id = pool, 1
    said: list[str] = []

    async def _capture(pool_, entity_id, origin, text, **kw):
        said.append(text)
        return True
    OTR.post_to_origin_thread = _capture
    try:
        r = await MH.handle_calendar_append({
            "event_type": "scheduled_task", "summary": "invented nightly job", "status": "scheduled",
            "payload": {"prompt": "invented", "budget_usd": 0.5}})
        uid = r["event_id"]
        pk = await pool.fetchval("SELECT id FROM calendar_events WHERE event_id = $1::uuid", uid)

        print("C. a run's cost is stored and read back")
        await OTR._record_run(pool, pk, 1, True, "invented output", None, 0.2)
        await OTR._record_run(pool, pk, 1, True, "invented output", None, None)
        runs = (await MH.handle_calendar_runs({"event_id": uid}))["runs"]
        costs = sorted([x["cost_usd"] for x in runs], key=lambda v: (v is None, v or 0))
        check(costs == [0.2, None], "calendar_read runs carry cost_usd, and an unpriced run stays null",
              runs)

        print("A. the Agents list gives the day's priced spend")
        from server.routes import agents as AG
        lst = (await AG.list_scheduled())["scheduled"]
        row = next((x for x in lst if x["event_id"] == uid), None)
        check(row is not None and row.get("cost_24h_usd") == round(0.2, 4)
              and row.get("unpriced_runs_24h") == 1,
              "cost_24h_usd sums the priced runs and unpriced_runs_24h counts the rest", row)

        print("R. the thread report says what the run cost, and a budget stop says 'stopped'")
        said.clear()
        await OTR._report_and_finalize(pool, 1, pk, "invented nightly job", "daily", None, True,
                                       "invented result", None, report_on_success=True,
                                       origin=None, cost_usd=0.1234)
        check(bool(said) and "(This run cost $0.1234.)" in said[-1],
              "a priced run's report carries its cost", said[-1:])
        said.clear()
        await OTR._report_and_finalize(pool, 1, pk, "invented nightly job", None, None, False, "",
                                       "it reached its budget", report_on_success=True,
                                       origin=None, stopped=True)
        check(bool(said) and "stopped: it reached its budget" in said[-1]
              and "failed" not in said[-1] and "skipped" not in said[-1],
              "a budget stop reads 'stopped'", said[-1:])

        print("B. the job's budget counts priced runs and stops the task")
        b, spent = await OTR.budget_state(pool, pk, 1, {"budget_usd": 0.5})
        check(b == 0.5 and abs(spent - (0.2 + 0.1234)) < 1e-9,
              "budget_state: the budget and what the priced runs spent", (b, spent))
        check((await OTR.budget_state(pool, pk, 1, {}))[0] is None, "no budget named: none in force")
        check((await OTR.budget_state(pool, pk, 1, {"budget_usd": 0}))[0] is None,
              "a budget of 0 is no budget, never 'stop at once'")
        src = (ROOT / "brain/mastercode/brain/offline_task_runner.py").read_text()
        check("if _budget is not None and _budget_spent >= _budget:" in src
              and "raise BudgetReached(" in src and "recurrence = None" in src,
              "a reached budget stops the task before its turn and between steps, for good")

        print("S. schedule_task takes budget_usd on the prompt lane only")
        tsrc = (ROOT / "brain/mastercode/runtime/tools.py").read_text()
        check("budget_usd does not apply to op_tool" in tsrc
              and "budget_usd must be more than $0" in tsrc
              and '_body["budget_usd"] = _budget' in tsrc,
              "budget_usd is refused on the op lane and at $0, and stored on the payload")
        from mastercode import public_tool_registry as reg
        props = reg.PUBLIC_TOOL_SCHEMAS["schedule_task"]["function"]["parameters"]["properties"]
        check("budget_usd" in props and props["budget_usd"]["type"] == "number",
              "schedule_task's schema offers budget_usd")

        print("V. a recurring task that runs a model waits for the person's card")
        from mastercode.runtime import tools as T
        from server.routes import tickets as TK
        from mastercode.runtime.direct_ops import UNATTENDED_OP_TOOLS

        async def _rows(title):
            return await pool.fetchval(
                "SELECT count(*) FROM calendar_events WHERE summary = $1", title)
        said.clear()
        res = await T._exec_schedule_task({"op": "create", "title": "invented daily look back",
                                           "fire_at": "2026-11-01T06:00:00Z",
                                           "recurrence": "daily", "prompt": "invented"})
        tid = (res.get("result") or {}).get("ticket_id")
        md = json.loads(await pool.fetchval(
            "SELECT metadata::text FROM entity_tickets WHERE id = $1", tid) or "{}")
        check(res.get("status") == "ok" and (res.get("result") or {}).get("awaiting_approval")
              and md.get("kind") == "recurring_task_proposal" and md.get("user_only")
              and md.get("no_pattern"),
              "a recurring prompt task files one card locked to the person", (res, md))
        check(await _rows("invented daily look back") == 0 and not said,
              "...and sets nothing, and writes no plan, until they decide", said)
        try:
            await TK.decide_ticket_service(pool, 1, tid, option_id="apply", actor="host",
                                           deliver_answer=False)
            check(False, "the Host cannot let its own recurring task run")
        except TK.TicketError as e:
            check(e.status == 403, "the Host cannot let its own recurring task run", e.detail)
        await TK.decide_ticket_service(pool, 1, tid, option_id="apply", actor="user",
                                       deliver_answer=False)
        check(await _rows("invented daily look back") == 1 and any(
                  "Scheduled **invented daily look back**" in x for x in said),
              "the person's 'Let it run' writes the plan into the thread and sets the task", said)
        res = await T._exec_schedule_task({"op": "create", "title": "invented hourly check",
                                           "fire_at": "2026-11-01T06:00:00Z",
                                           "recurrence": "hourly", "prompt": "invented"})
        tid = res["result"]["ticket_id"]
        await TK.decide_ticket_service(pool, 1, tid, option_id="keep", actor="user",
                                       deliver_answer=False)
        check(await _rows("invented hourly check") == 0, "'Don't' sets nothing")
        res = await T._exec_schedule_task({"op": "create", "title": "invented one check",
                                           "fire_at": "2026-11-01T06:00:00Z", "prompt": "invented"})
        check(res.get("status") == "ok" and await _rows("invented one check") == 1,
              "a one-time follow-up is set at once, as before", res)
        op = sorted(UNATTENDED_OP_TOOLS)[0]
        res = await T._exec_schedule_task({"op": "create", "title": "invented standing op",
                                           "fire_at": "2026-11-01T06:00:00Z",
                                           "recurrence": "daily", "op_tool": op, "op_args": {}})
        check(not (res.get("result") or {}).get("awaiting_approval"),
              "a recurring op (no model, nothing spent) needs no card", res)

        print("T. a trigger that runs a runbook waits for the person's card")
        await pool.execute(
            "INSERT INTO reachout_channels (entity_id, name, kind, action, created_by) "
            "VALUES (1, 'invented-runbook', 'runbook', $1::jsonb, 'host'), "
            "       (1, 'invented-log', 'log', $2::jsonb, 'host')",
            json.dumps({"type": "runbook", "template_id": 1}), json.dumps({"type": "log"}))

        async def _triggers(ch):
            return await pool.fetchval(
                "SELECT count(*) FROM reachout_triggers t JOIN reachout_channels c "
                "ON c.id = t.channel_id WHERE c.name = $1", ch)
        res = await T._exec_reachout({"op": "add_trigger", "channel": "invented-runbook",
                                      "label": "invented", "match_kind": "system_notification"})
        tid = (res.get("result") or {}).get("ticket_id")
        check((res.get("result") or {}).get("awaiting_approval") and await _triggers("invented-runbook") == 0,
              "add_trigger on a runbook channel files a card and adds nothing", res)
        await TK.decide_ticket_service(pool, 1, tid, option_id="apply", actor="user",
                                       deliver_answer=False)
        check(await _triggers("invented-runbook") == 1, "the person's click adds the trigger")
        res = await T._exec_reachout({"op": "add_trigger", "channel": "invented-log",
                                      "label": "invented", "match_kind": "system_notification"})
        check(res.get("status") == "ok" and not (res.get("result") or {}).get("awaiting_approval")
              and await _triggers("invented-log") == 1,
              "a channel that runs no model is added at once", res)

        print("K. housekeeping is a skill the person turns on")
        hk = " ".join((ROOT / "default-skills/housekeeping.md").read_text().split())
        check("It is off until the person turns it on" in hk
              and "Start new work" in hk and "Change a limit" in hk,
              "housekeeping: off until asked, never starts work, never changes a limit")
    finally:
        BS._pool, BS._entity_id, OTR.post_to_origin_thread = saved
        await pool.close()


if __name__ == "__main__":
    from kernel_schema import kernel_schema_db, throwaway_server
    with (contextlib.nullcontext(SERVER) if SERVER else throwaway_server()) as _srv, \
            kernel_schema_db(_srv, entity_id=1) as _dsn:
        asyncio.run(main(_dsn))
    print(f"\n{CHECKS - len(FAILS)}/{CHECKS} passed")
    sys.exit(1 if FAILS else 0)
