#!/usr/bin/env python3
"""The weekly "state of the host" changelog: pushed, dated, deterministic.

WHAT THIS PROTECTS.
A fresh host told a stranger that every one of its feedback loops was silent:
rules, harness track record, memory consolidation all accrue where nobody
looks. runtime/host_changelog.py is the answer — ONE deterministic op that
reads the host's own tables since the last changelog and writes ONE Inbox
message — seeded for every host at boot as a platform default.

  1. RENDERING. Both shapes ("nothing changed" and a busy week) come out as
     plain, dated, attributable text; promotions to hard rules are listed;
     the not-set-up section names the setup step; none of the banned outage
     vocabulary appears and personality drift is never reported.
  2. DELIVERY. The op writes one system_notification with payload.event =
     host_changelog and the counts; deliver=false previews without writing;
     the next run reads "since" from that row.
  3. THE STANDING TASK. ensure_weekly_changelog_task seeds exactly one weekly
     op-lane task with the right payload, and NEVER re-seeds when any row for
     the op already exists — including a CANCELLED one (a Stop is a decision).
  4. THE WIRING. The op is a registered schema, in PUBLIC_META_TOOL_NAMES,
     META_TOOL_CAPS, DIRECT_OP_TOOLS and UNATTENDED_OP_TOOLS (so the runner's
     fitness check passes); the tools.py dispatcher routes to it; main.py
     seeds it beside the offline runner.

Symbols under test are defined in brain/mastercode/runtime/host_changelog.py
(CHANGELOG_TOOL / CHANGELOG_EVENT, run_changelog, _exec_host_changelog,
ensure_weekly_changelog_task); the registries in public_tool_registry.py
(PUBLIC_TOOL_SCHEMAS, PUBLIC_META_TOOL_NAMES) and runtime/meta_tool_caps.py
(META_TOOL_CAPS). Symbol names, not line numbers: lines move.

WHAT THIS DOES NOT CATCH. The SQL runs against a fake pool that answers by
statement shape, so column names are checked by reading, not by Postgres. The
in-image run and the first live week are where the statements meet the DB.

Container-free. Run: APP_ROOT=<repo> python3 scripts/test_host_changelog.py
"""
import asyncio
import os
import sys
import types
from datetime import datetime, timedelta, timezone
from tools_source import tools_source  # tools.py and the modules split out of it

FAILED = 0


def check(label: str, ok: bool, detail: str = "") -> None:
    global FAILED
    if ok:
        print(f"  ok   {label}")
        return
    FAILED += 1
    print(f"  FAIL {label}")
    if detail:
        print("       " + detail.replace("\n", "\n       "))


def _read(repo: str, rel: str) -> str:
    with open(os.path.join(repo, rel), encoding="utf-8") as fh:
        return fh.read()


NOW = datetime(2026, 9, 9, 9, 0, tzinfo=timezone.utc)
BANNED = ("lane down", "system-wide", "outage", "model=:", "drift")


class FakePool:
    """Answers each statement by a substring of its SQL. `busy` flips between
    the quiet week and the eventful one; `calendar_rows` drives the seed."""

    def __init__(self, busy: bool, calendar_rows: int = 0, last_changelog=None):
        self.busy = busy
        self.calendar_rows = calendar_rows
        self.last_changelog = last_changelog
        self.sql_log: list[str] = []

    async def fetchval(self, sql, *params):
        self.sql_log.append(sql)
        if "FROM inbox_messages" in sql:
            return self.last_changelog
        if "FROM entity_memory" in sql:
            return 12 if self.busy else 0
        if "FROM calendar_events" in sql and "payload->'op'->>'tool' = $2" in sql:
            return self.calendar_rows
        if "FROM calendar_events" in sql:
            return 0  # no OTHER standing tasks
        return 0

    async def fetchrow(self, sql, *params):
        self.sql_log.append(sql)
        return None

    async def fetch(self, sql, *params):
        self.sql_log.append(sql)
        if "FROM entity_memory" in sql and "rule_kind" in sql:
            return ([{"kind": "taught", "n": 3}, {"kind": "learned", "n": 2}]
                    if self.busy else [])
        if "FROM harness_usage" in sql:
            return ([{"harness": "kimi", "model": "kimi-k2", "runs": 14, "failed": 3,
                      "cost_ud": 1_540_000}] if self.busy else [])
        if "FROM projects p" in sql:
            return [{"name": "Trading software"}]
        return []


def _stub_modules(inbox_calls: list, calendar_calls: list, configured: bool):
    """Stand in for the pod-only modules the changelog imports lazily."""
    for pkg in ("server", "server.bridge", "server.routes"):
        sys.modules.setdefault(pkg, types.ModuleType(pkg))

    mh = types.ModuleType("server.bridge.memory_handlers")

    async def handle_inbox_append(payload):
        inbox_calls.append(payload)
        return {"ok": True, "message_id": "m1"}

    async def handle_calendar_append(payload):
        calendar_calls.append(payload)
        return {"ok": True, "event_id": "e1", "event_at": payload.get("event_at"),
                "status": "scheduled"}

    mh.handle_inbox_append = handle_inbox_append
    mh.handle_calendar_append = handle_calendar_append
    sys.modules["server.bridge.memory_handlers"] = mh

    emb = types.ModuleType("server.routes.embedding")

    async def current():
        return {"configured": configured,
                "model": "qwen3-embedding:4b" if configured else None}

    emb.current = current
    sys.modules["server.routes.embedding"] = emb

    cb = types.ModuleType("mastercode.context_backends")

    async def get_backend_config(pool, entity_id):
        return {"backend": "pg"}

    cb.get_backend_config = get_backend_config
    sys.modules["mastercode.context_backends"] = cb


def main() -> int:
    repo = os.environ.get("APP_ROOT") or os.path.dirname(
        os.path.dirname(os.path.abspath(__file__)))
    sys.path.insert(0, repo)
    sys.path.insert(0, os.path.join(repo, "brain"))

    print("host changelog — pushed, dated, deterministic")

    inbox: list = []
    calendar: list = []
    _stub_modules(inbox, calendar, configured=False)
    from mastercode.runtime import host_changelog as hc  # noqa: E402

    # ── 1. Rendering: the quiet week ─────────────────────────────────────
    quiet = asyncio.run(hc.run_changelog(FakePool(busy=False), 7, deliver=False, now=NOW))
    check("quiet week is not 'changed'", quiet["changed"] is False)
    check("quiet week subject says nothing changed",
          "nothing changed" in quiet["subject"].lower(), quiet["subject"])
    check("quiet week body says so in words and still counts as information",
          "Nothing changed this week" in quiet["body"] and "information too" in quiet["body"])
    check("quiet week window is the 7-day look-back",
          quiet["since"] == (NOW - timedelta(days=7)).isoformat(), quiet["since"])
    body_l = quiet["body"].lower()
    for w in BANNED:
        check(f"quiet body never says {w!r}", w not in body_l)
    check("not-set-up section names embeddings as a setup step, not a fault",
          "aren't set up yet" in quiet["body"] and "Orchestrator" in quiet["body"])
    check("not-set-up section names the plan-less project",
          "Trading software" in quiet["body"] and "no plan yet" in quiet["body"])
    check("not-set-up section mentions storage on the default and standing tasks",
          "platform default" in quiet["body"] and "standing tasks" in body_l)
    check("closing line says nothing moved on its own and how to stop the note",
          "fixed at what you set" in quiet["body"] and "Agents app" in quiet["body"])
    check("deliver=false wrote nothing", inbox == [] and quiet["delivered"] is False)

    # ── 1b. Rendering: the eventful week ─────────────────────────────────
    last = NOW - timedelta(days=6)
    busy_pool = FakePool(busy=True, last_changelog=last)
    busy = asyncio.run(hc.run_changelog(busy_pool, 7, now=NOW))
    check("eventful week is 'changed'", busy["changed"] is True)
    check("since = the previous changelog's sent_at",
          busy["since"] == last.isoformat(), busy["since"])
    b = busy["body"]
    # Rules are preferences marked rule_kind; the changelog counts
    # them by kind, in plain words, and never names an identifier.
    check("rules added are counted by kind, in plain words",
          "3 new rule(s) you taught it" in b
          and "2 new rule(s) it learned from what you corrected" in b
          and "rule_kind" not in b and "learned)" not in b, b[:600])
    check("the rules come from entity_memory, never the retired corrections table",
          any("FROM entity_memory" in q and "rule_kind" in q for q in busy_pool.sql_log)
          and not any("behavioral_corrections" in q for q in busy_pool.sql_log))
    check("harness section reads the per-pod table without an entity filter",
          "WHERE created_at > $1 AND created_at <= $2" in
          _read(repo, "brain/mastercode/runtime/host_changelog.py"))
    check("projects line claims only what the code does (ask, not auto-offer)",
          "ask your host to draft one" in quiet["body"] and "open one and your host" not in quiet["body"])
    check("no promotion to hard rules is claimed (the promoter and its log are gone)",
          "promoted to HARD" not in b and "No rules promoted" not in b)
    check("harness runs carry engine, failures and cost",
          "kimi (kimi-k2): 14 run(s), 3 failed, $1.54" in b, b)
    # Memory forms in the turn (the batch consolidator and its loop are
    # gone), so the note says so instead of
    # grading a loop that does not exist.
    check("memory section reports entries and that memory forms in the turn",
          "12 new memory entries" in b and "Memory forms in the turn" in b
          and "Consolidation" not in b, b)
    check("no statement reads memory_pass_log (v235 drops the table)",
          not any("memory_pass_log" in q for q in busy_pool.sql_log),
          "\n".join(q for q in busy_pool.sql_log if "memory_pass_log" in q)[:300])
    for w in BANNED:
        check(f"busy body never says {w!r}", w not in b.lower())

    # ── 2. Delivery ──────────────────────────────────────────────────────
    check("one inbox row was written", len(inbox) == 1, str(len(inbox)))
    msg = inbox[0] if inbox else {}
    check("inbox row is a system_notification with payload.event host_changelog",
          msg.get("message_kind") == "system_notification"
          and (msg.get("payload") or {}).get("event") == hc.CHANGELOG_EVENT)
    counts = (msg.get("payload") or {}).get("counts") or {}
    check("payload counts match the sections",
          counts.get("rules_added") == 5 and "rules_promoted" not in counts
          and counts.get("harness_runs") == 14 and counts.get("harness_failed") == 3
          and counts.get("new_memories") == 12, str(counts))
    check("delivered flag set", busy["delivered"] is True)

    # The executor shape the dispatcher and the op lane see.
    bs = types.ModuleType("server.brain_service")
    bs._pool = FakePool(busy=False)
    bs.get_pool = lambda: bs._pool
    bs._entity_id = 7
    sys.modules["server.brain_service"] = bs
    sys.modules["server"].brain_service = bs
    ex = asyncio.run(hc._exec_host_changelog({"deliver": False}))
    check("executor returns ok with the composed changelog",
          ex.get("status") == "ok" and "body" in (ex.get("result") or {}), str(ex)[:200])

    # ── 3. The standing task ─────────────────────────────────────────────
    seeded = asyncio.run(hc.ensure_weekly_changelog_task(FakePool(busy=False, calendar_rows=0), 7))
    check("seed writes exactly one task when none exists",
          seeded["seeded"] is True and len(calendar) == 1, str(seeded))
    task = calendar[0] if calendar else {}
    check("seeded task is a weekly scheduled_task on the op lane",
          task.get("event_type") == "scheduled_task" and task.get("recurrence") == "weekly"
          and task.get("status") == "scheduled"
          and (task.get("payload") or {}).get("op") == {"tool": hc.CHANGELOG_TOOL, "args": {}},
          str(task))
    check("seeded task is attributed to the platform default",
          (task.get("payload") or {}).get("seeded_by") == "platform_default")
    week_out = (datetime.now(timezone.utc) + timedelta(days=7)).date().isoformat()
    check("first fire is one week out",
          str(task.get("event_at", "")).startswith(week_out), str(task.get("event_at")))
    again = asyncio.run(hc.ensure_weekly_changelog_task(FakePool(busy=False, calendar_rows=1), 7))
    check("seed is idempotent — an existing row (any status) means no write",
          again["seeded"] is False and len(calendar) == 1 and "cancel" in again.get("reason", ""),
          str(again))
    probe = FakePool(busy=False, calendar_rows=1)
    asyncio.run(hc.ensure_weekly_changelog_task(probe, 7))
    exists_sql = next((s for s in probe.sql_log if "payload->'op'->>'tool' = $2" in s), "")
    check("seed's existence check has NO status filter (a cancelled task stays cancelled)",
          exists_sql and "status" not in exists_sql, exists_sql)

    # ── 4. The wiring ────────────────────────────────────────────────────
    from mastercode.runtime import direct_ops as dops  # noqa: E402
    check("host_changelog is a direct op", "host_changelog" in dops.DIRECT_OP_TOOLS)
    check("host_changelog is schedulable (unattended)", dops.is_unattended_op_tool("host_changelog"))
    from mastercode import public_tool_registry as reg  # noqa: E402
    check("host_changelog has a public schema",
          "host_changelog" in reg.PUBLIC_TOOL_SCHEMAS
          and reg.PUBLIC_TOOL_SCHEMAS["host_changelog"]["function"]["name"] == "host_changelog")
    check("host_changelog is a meta tool name", "host_changelog" in reg.PUBLIC_META_TOOL_NAMES)
    from mastercode.runtime import meta_tool_caps as caps  # noqa: E402
    check("host_changelog carries read + write_memory bits",
          caps.META_TOOL_CAPS["host_changelog"]["required_bits"] == (1 | 2 | 8))
    desc = reg.PUBLIC_TOOL_SCHEMAS["host_changelog"]["function"]["description"]
    check("schema says it never reports personality drift",
          "never reports personality drift" in desc)
    src_tools = tools_source(repo)
    check("tools.py dispatches host_changelog to the module",
          'elif name == "host_changelog":' in src_tools
          and "from mastercode.runtime.host_changelog import _exec_host_changelog" in src_tools)
    # The promoter is retired and so is its audit log
    # (v237), so the changelog reads no promotions at all.
    check("the rule promoter is retired (module gone, loop not dispatched)",
          not os.path.exists(os.path.join(repo, "brain/mastercode/constitution/rule_promoter.py"))
          and "run_rule_promoter_loop" not in _read(repo, "server/main.py"))
    check("the changelog reads no promotions (their audit log is gone)",
          "rule_promoter" not in _read(repo, "brain/mastercode/runtime/host_changelog.py")
          and "entity_behavioral_log" not in _read(repo, "brain/mastercode/runtime/host_changelog.py"))
    src_main = _read(repo, "server/main.py")
    check("main.py seeds the weekly task beside the offline runner",
          "ensure_weekly_changelog_task(brain_pool, relay_entity_id)" in src_main
          and src_main.find("run_offline_tasks_loop(brain_pool, relay_entity_id)")
          < src_main.find("ensure_weekly_changelog_task(brain_pool, relay_entity_id)"))

    print()
    if FAILED:
        print(f"FAIL host_changelog: {FAILED} check(s) failed")
        return 1
    print("PASS host_changelog: pushed, dated, deterministic, seeded once")
    return 0


if __name__ == "__main__":
    sys.exit(main())
