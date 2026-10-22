#!/usr/bin/env python3
"""Gate: there is no consolidator and memory forms in the turn.

The cron is off; the host does everything it needs within the turn.
close_turn writes the preferences, the thread goal, the
topic and title and the handoff. What removing the 30-minute loop would
otherwise break was mapped first; each item has a check here:

  1. boot: no consolidator loop and no archive pass start; the deterministic
     thread audit keeps its own clock
  2. history: the thread's goal and the last close_turn handoff ride, dated
     (close_turn.thread_state), and the trim spares that message. No rolling
     summary rides any scope: no model outside the Host's turn writes what the
     Host reads (the summary writer and its janitor are retired, v241). Turns
     that fall out of the window ride as their own close_turn notes
     (evicted_record), built only when they fell out
  3. the table of contents names no summary
  4. "Remember this:" is kept word for word when it arrives, never for a
     variant, a scheduled run or an ephemeral session
  5. close_turn: 'remember / correct' is in the rule; no similarity test
     retires a held preference (a topic is the Host's call: memory_slots)
  6. the Memory app and the weekly note say memory forms in the turn instead
     of raising an alarm about a pass that is not supposed to run

No DB, no network. The real-database halves are in test_memory_integrity_db.py.

  APP_ROOT=<repo> PYTHONPATH=brain:. python3 scripts/test_consolidator_off.py
"""
import ast
import asyncio
import json
import os
import sys
from turn_source import turn_source  # session.py and the modules split out of it

APP_ROOT = os.environ.get("APP_ROOT", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, APP_ROOT)
sys.path.insert(0, os.path.join(APP_ROOT, "brain"))
fails: list[str] = []


def check(name, ok, detail=""):
    print(("  ok    " if ok else "  FAIL  ") + name + (f"  — {detail}" if detail and not ok else ""))
    if not ok:
        fails.append(name)


def src(rel):
    return open(os.path.join(APP_ROOT, rel), encoding="utf-8").read()


# ── 1. boot ─────────────────────────────────────────────────────────────────
check("no consolidator module ships, nor the rolling summary or its policy",
      not os.path.exists(os.path.join(APP_ROOT, "brain/mastercode/turn_consolidator.py"))
      and not os.path.exists(os.path.join(APP_ROOT, "brain/mastercode/batch_consolidator.py"))
      and not os.path.exists(os.path.join(APP_ROOT, "brain/mastercode/thread_consolidator.py"))
      and not os.path.exists(os.path.join(APP_ROOT, "brain/mastercode/summary_policy.py")))
main_src = src("server/main.py")
tree = ast.parse(main_src)
check("boot: no consolidator loop and no archive pass start",
      "turn_consolidator" not in main_src and "archive_retired_memory" not in main_src
      and "cache_lifecycle" not in main_src)
_audit = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
          and ast.unparse(n.func) == "_ta.run_audit_loop"]
check("boot: the deterministic thread audit keeps its own clock (one loop, 1800 s)",
      len(_audit) == 1 and ast.unparse(_audit[0].args[-1]) == "1800",
      [ast.unparse(a) for a in _audit])

# ── 2. history: goal + last handoff, never a frozen summary ──────────────────
from mastercode import context_budget as cb  # noqa: E402
from mastercode.runtime import close_turn as ct  # noqa: E402


class _Pool:
    def __init__(self, goal=None, handoff=None):
        self.goal, self.handoff, self.sql = goal, handoff, []

    async def fetchrow(self, sql, *a):
        from datetime import datetime, timezone
        self.sql.append(" ".join(sql.split()))
        at = datetime(2026, 10, 19, tzinfo=timezone.utc)
        if "memory_type = 'goal'" in sql:
            return {"content": self.goal, "created_at": at, "untrusted": False} if self.goal else None
        if "metadata->'close_turn'->'handoff'" in sql:
            return ({"handoff": json.dumps(self.handoff), "created_at": at, "untrusted": False}
                    if self.handoff is not None else None)
        raise AssertionError(sql)


SC = {"kind": "thread", "ref": "0f8b1c3e-2d4a-4b5c-9e6f-7a8b9c0d1e2f"}
p = _Pool(goal="Ship the new desktop",
          handoff={"next_step": "wire the taskbar", "open_work": ["icons"],
                   "identifiers": ["artifact:9f2", "  /srv/a b.txt"]})
state = asyncio.run(ct.thread_state(p, 3, SC))
check("thread_state: the goal, the next step, open work and identifiers verbatim",
      state and state.startswith(cb.THREAD_STATE_PREFIX) and "Ship the new desktop" in state
      and "wire the taskbar" in state and "icons" in state and "  /srv/a b.txt" in state, state)
check("thread_state: the goal and the handoff say when they were written",
      "Goal (set Oct 19, 2026): Ship the new desktop" in state
      and "Next step (from the last turn, Oct 19, 2026): wire the taskbar" in state, state)
check("thread_state: read from this thread's own rows only",
      all("metadata->'source_context'->>'kind' = $2" in q and "->>'ref' = $3" in q for q in p.sql)
      and any("metadata->>'role' = 'assistant'" in q for q in p.sql), p.sql)
check("thread_state: nothing to say -> None (no empty message rides)",
      asyncio.run(ct.thread_state(_Pool(), 3, SC)) is None)
check("thread_state: no scope -> None", asyncio.run(ct.thread_state(_Pool(goal="x"), 3, None)) is None)
check("trim: the thread-state message and the evicted-turn notes are spared",
      cb._is_summary({"role": "system", "content": cb.THREAD_STATE_PREFIX + "\nGoal: x"})
      and cb._is_summary({"role": "system", "content": cb.RECORD_PREFIX + "\n- x"}))

sess_src = turn_source()
stree = ast.parse(sess_src)
check("history: no rolling summary is read for any scope",
      "get_live_summary" not in sess_src and "summary_policy" not in sess_src
      and "dispatch_topic_summary" not in sess_src and "maybe_consolidate" not in sess_src)
state_if = [n for n in ast.walk(stree) if isinstance(n, ast.If) and ast.unparse(n.test) == "True"
            and "_thread_state(" in ast.unparse(n.body)]
check("history: the thread state is read on every scoped turn", len(state_if) == 1,
      [ast.unparse(n.test) for n in state_if])
_rec = sess_src[sess_src.index("kept = _fit_window(eligible, max_hist)"):
                sess_src.index("_hist_rows = eligible")]
check("history: the evicted-turn notes ride only when turns fell out, after a fit that reserves them",
      "_er.evicted_record(" in _rec and "if _probe:" in _rec
      and "max_hist = max(max_hist - _er.RECORD_MAX_CHARS, 0)" in _rec
      and _rec.index("if _probe:") < _rec.index("_record_msg = {"))
check("ledger: history says which of the two rode",
      '"thread_state": bool(_loc.get("_thread_state_block"))' in sess_src
      and '"evicted_record": bool(_loc.get("_record_msg"))' in sess_src)
# The thread state changes with every handoff: it rides in the per-turn block
# after history, never as a message ahead of the cached prefix.
_ts_if = [n for n in ast.walk(stree) if isinstance(n, ast.If) and ast.unparse(n.test) == "_thread_state_block"]
check("cache: the thread state joins the per-turn block after history, not the history messages",
      len(_ts_if) == 1 and "_dyn_sys = " in ast.unparse(_ts_if[0].body)
      and sess_src.index("grok_messages.extend(history_msgs)") < sess_src.index("if _thread_state_block:")
      < sess_src.index("grok_messages.append({'role': 'system', 'content': _dyn_sys})"
                       if "grok_messages.append({'role'" in sess_src
                       else 'grok_messages.append({"role": "system", "content": _dyn_sys})'))

# The Next-turn context panel shows what rides: the thread state; no summary.
cbtree = ast.parse(src("brain/mastercode/context_budget.py"))
btc = [n for n in ast.walk(cbtree) if isinstance(n, ast.AsyncFunctionDef) and n.name == "build_thread_context"][0]
_btc = ast.unparse(btc)
check("panel: the thread state is reported; no rolling summary",
      "thread_state(" in _btc and "get_live_summary" not in _btc and "summary_on" not in _btc)
for _app in ("circles-web/src/components/chat/conversation-depth.tsx",
             "workbench-web/src/components/workbench/conversation-depth.tsx"):
    _t = src(_app)
    check(f"panel ({_app.split('/')[0]}): shows where the thread stands; no Memory-model link",
          "info.thread_state &&" in _t and "Where this thread stands" in _t
          and "Consolidator model is set" not in _t and "openOrchestrator" not in _t)

# The post-turn stash existed for the consolidator's janitor; both are gone.
check("tool results: the turn stashes none (the janitor that reviewed them is gone)",
      not [n for n in ast.walk(stree) if isinstance(n, ast.Call)
           and ast.unparse(n.func).endswith("stash_tool_artifacts")])

# ── 3. the table of contents ────────────────────────────────────────────────
kt_src = src("brain/mastercode/knowledge_toc.py")
check("toc: no summary is read or named (none is written)",
      "get_live_summary" not in kt_src and "summary_age" not in kt_src and "_segment_topics" not in kt_src)

# ── 4. "Remember this:" when it arrives ─────────────────────────────────────
teach_if = [n for n in ast.walk(stree) if isinstance(n, ast.If)
            and "EPHEMERAL_CONTEXTS" in ast.unparse(n.test) and "_detect_teaching" in ast.unparse(n.body)]
check("remember this: kept on arrival, never for an ephemeral session, a variant or a scheduled run",
      len(teach_if) == 1 and "enabled()" not in ast.unparse(teach_if[0].test)
      and "_CLOSE_TURN_NO_WRITES" in ast.unparse(teach_if[0].test)
      and "_store_teaching_memory" in ast.unparse(teach_if[0].body)
      and "_detect_teaching" in ast.unparse(teach_if[0].body))
_tb = ast.unparse(teach_if[0].body)
check("remember this: in PII mode the rule goes through the PII queue, never stored raw",
      "_pii.submit(t['content']" in _tb and "_write_taught" in _tb
      and _tb.count("_store_teaching_memory(") == 1
      and "{**t, 'content': text}" in _tb)
from mastercode.memory_engine import _detect_teaching  # noqa: E402
got = _detect_teaching([{"role": "user", "content": "Remember this: invoices go out on the 1st"}])
check("remember this: the trigger is found and the words after it kept exactly",
      got and got[0]["content"] == "invoices go out on the 1st")

# ── 5. close_turn: remember / correct, and near wordings ────────────────────
from mastercode.runtime import session as S  # noqa: E402
check("rule: close_turn is told to keep what the user asked it to remember or corrected",
      "asked you to remember or corrected" in S._CLOSE_TURN_RULE)


class _NearPool:
    """A near row (id 55, cosine 0.93) the old merge test would have
    retired, and the insert of the new wording."""
    def __init__(self):
        self.calls, self.inserted, self.superseded = [], [], []

    async def fetch(self, sql, *a):
        s = " ".join(sql.split())
        self.calls.append(s)
        if "content = $2" in s:
            return []                                          # no exact match
        if "<=>" in s:
            return [{"id": 55, "content": "prefers dark themes in every app", "sim": 0.93}]
        raise AssertionError(s)

    async def fetchval(self, sql, *a):
        if "FROM circles_threads" in sql:
            return 1
        return None

    async def fetchrow(self, sql, *a):
        if sql.strip().startswith("INSERT INTO entity_memory"):
            self.inserted.append(a)
            return {"id": 77}
        raise AssertionError(sql)

    async def execute(self, sql, *a):
        s = " ".join(sql.split())
        self.calls.append(s)
        if s.startswith("UPDATE entity_memory SET t_valid_until = NOW()"):
            self.superseded.append((a[1], a[2]))
        return "UPDATE 1"


async def _vec(*a, **k):
    return "[" + ",".join(["0.1"] * 4) + "]"


async def _col(pool, eid):
    return "embedding", 4

ct._embed = _vec
import mastercode.memory_engine as ME  # noqa: E402
ME.active_embedding_column = _col

pref = {"text": "prefers dark themes in every single app", "reason": "said so",
        "scope": {"kind": "global"}}
np_ = _NearPool()
r = asyncio.run(ct._write_preference(np_, 3, pref, session_id="s", turn_scope=SC))
check("no similarity: a near wording with no topic is a row of its own; the held one is left alone",
      r.get("status") == "stored" and not r.get("replaced") and not np_.superseded and np_.inserted
      and not any("<=>" in q for q in np_.calls), r)
_wp = ast.unparse([n for n in ast.walk(ast.parse(src("brain/mastercode/runtime/close_turn.py")))
                   if isinstance(n, ast.AsyncFunctionDef) and n.name == "_write_preference"][0])
check("no similarity: close_turn has no embedding-decided replace any more",
      "_near_preferences" not in src("brain/mastercode/runtime/close_turn.py") and "_covers(" not in _wp)
check("different words are never answered 'already held'",
      "_embed(" in _wp and "already_held" not in _wp.split("_embed(", 1)[1].split("write_in_slot", 1)[0])

# ── 6. the Memory app and the weekly note ───────────────────────────────────
brain_src = src("server/routes/brain.py")
btree = ast.parse(brain_src)
_form = [n for n in ast.walk(btree) if isinstance(n, ast.Assign)
         and [ast.unparse(t) for t in n.targets] == ["formation"]]
check("Memory app: formation is 'in the turn', never stale, and no pass is read",
      len(_form) == 1 and ast.literal_eval(_form[0].value).get("in_turn") is True
      and ast.literal_eval(_form[0].value).get("stale") is False
      and "memory_pass_log" not in brain_src, [ast.unparse(f) for f in _form])
oracle = src("system-apps/oracle/app.js")
check("Memory app: the card says memory forms in the turn, before any staleness test",
      "if (f.in_turn)" in oracle and oracle.index("if (f.in_turn)") < oracle.index("if (f.stale)"))
from mastercode.runtime import host_changelog as hc  # noqa: E402
from datetime import datetime, timezone  # noqa: E402
_, body, _ = hc.render_changelog({"memory": {"in_turn": True, "passes_ok": 0, "healthy": False}},
                                 datetime(2026, 9, 23, tzinfo=timezone.utc),
                                 datetime(2026, 9, 30, tzinfo=timezone.utc))
check("weekly note: 'memory forms in the turn', never 'consolidation did not run'",
      "Memory forms in the turn" in body and "did not run" not in body, body)

print(f"\n{len(fails)} failure(s)" if fails else "\nall checks passed")
sys.exit(1 if fails else 0)
