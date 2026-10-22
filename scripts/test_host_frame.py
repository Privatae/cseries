#!/usr/bin/env python3
"""Gate: the FRAME — what the platform knows about THIS turn.
No DB, no network.

  1. validate_frame accepts exactly {view, pills}, capped, and nothing else.
  2. frame_facts derives thread position, project age, attachments, handler,
     clock and summary age from real rows — with the SAME scope predicate the
     history window uses, and never counting the message being sent.
  3. render_frame prints only what is known, one screen, and carries the
     project binding (project_id=N) that used to live in surface_role.
  4. The `frame` block is PER_TURN and the first block after the cache split.
  5. The clock LEFT the cached static prefix: comm_context is byte-stable
     across two builds a minute apart.
  6. The field travels: routes validate it, brain_service forwards it, the
     runtime stores it on the session and adds the send-time facts; Circles
     sends its composer pills as frame.pills.

  PYTHONPATH=brain:. python3 scripts/test_host_frame.py
"""
import asyncio
import inspect
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from turn_source import turn_source  # session.py and the modules split out of it

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, os.environ.get("CSERIES_BRAIN", str(REPO / "brain")))
sys.path.insert(0, str(REPO))
fails = 0


def check(name, ok, detail=""):
    global fails
    print(("  ok    " if ok else "  FAIL  ") + name + (f"  — {detail}" if detail and not ok else ""))
    if not ok:
        fails += 1


from mastercode.runtime import frame as F  # noqa: E402
from mastercode.runtime import prompt_schema as ps  # noqa: E402
from mastercode.runtime.prompt_builder import _resolve_communication_context  # noqa: E402

# ── 1. validate_frame ───────────────────────────────────────────────────────
check("not a dict → None", F.validate_frame("x") is None and F.validate_frame(None) is None)
check("empty / junk keys → None", F.validate_frame({}) is None and F.validate_frame({"evil": 1}) is None)
v = F.validate_frame({"view": "  Artifacts window,\n artifact 12  ", "pills": ["a", "", 3, " b  c "] + ["p"] * 20,
                      "system_turn": True, "project_id": 9})
check("view is one trimmed line, capped", v["view"] == "Artifacts window, artifact 12")
check("pills: strings only, trimmed, capped at 10", v["pills"][:3] == ["a", "b c", "p"] and len(v["pills"]) == 10, repr(v))
check("platform-owned keys cannot be injected from the request",
      "system_turn" not in v and "project_id" not in v, repr(v))
check("a 1,000-char pill is cut to 300", len(F.validate_frame({"pills": ["x" * 1000]})["pills"][0]) == 300)
check("a 500-char view is cut to 120", len(F.validate_frame({"view": "y" * 500})["view"]) == 120)


# ── 2. frame_facts on stub rows ─────────────────────────────────────────────
NOW = datetime(2026, 9, 6, 21, 5, tzinfo=timezone.utc)


class _Sess:
    def __init__(self, ic, frame=None, history_before=None):
        self.entity_id = 9003
        self.session_id = "s-1"
        self.interaction_context = ic
        self.frame = frame
        self.history_before = history_before


class _Pool:
    def __init__(self, *, project=None, turns=None, bp=None, summary_at=None):
        self.project, self.turns, self.bp, self.summary_at = project, turns, bp, summary_at
        self.queries: list[tuple[str, tuple]] = []

    async def fetchrow(self, q, *a):
        self.queries.append((q, a))
        if "FROM projects" in q:
            return self.project
        if "FROM entity_memory" in q:
            return self.turns
        if "FROM entity_registry" in q:
            return {"operator_id": "alex@example.com", "behavioral_params": json.dumps(self.bp or {})}
        if "FROM entity_context" in q:
            return {"updated_at": self.summary_at} if self.summary_at else None
        return None


pool = _Pool(project={"name": "weather", "status": "active", "created_at": NOW - timedelta(minutes=3)},
             turns={"user_turns": 0, "total": 0, "last_at": None},
             bp={"timezone": "America/Los_Angeles", "handler_name": "Alex"},
             summary_at=NOW - timedelta(days=9))
sess = _Sess("project:9198", frame={"app_id": "workbench", "project_id": 9198,
                                   "attachments": ["image", "document", "image"], "pills": ["artifact 12"]})
facts = asyncio.run(F.frame_facts(pool, sess, user_message="hello", now=NOW))
check("surface from the context", facts["surface"] == "Workbench")
check("project: id from the context, name + age from the row",
      facts["project"]["id"] == 9198 and facts["project"]["name"] == "weather" and 170 < facts["project"]["age_s"] < 190, repr(facts["project"]))
check("thread: zero prior user turns", facts["user_turns_before"] == 0)
check("comm: handler + timezone", facts["comm"]["handler_name"] == "Alex" and facts["comm"]["user_tz"] == "America/Los_Angeles")
check("no summary age: the rolling summary is no longer rendered anywhere (2026-09-30)",
      "summary_age_days" not in facts)
tq = next((q, a) for q, a in pool.queries if "FROM entity_memory" in q)
check("the turn count uses the history window's scope (project_id = $N::int, live rows only)",
      "project_id = $2::int" in tq[0] and "memory_type = 'turn'" in tq[0] and tq[1][1] == 9198, tq[0])
check("the message being sent is excluded from the count", "content <> $3" in tq[0] and tq[1][2] == "hello", tq[0])
check("a user-turn filter counts USER turns", "metadata->>'role' = 'user'" in tq[0])

# Circles: app scope, continuing thread, replay cutoff honoured.
pool2 = _Pool(turns={"user_turns": 6, "total": 12, "last_at": NOW - timedelta(minutes=20)})
before = NOW - timedelta(minutes=5)
facts2 = asyncio.run(F.frame_facts(pool2, _Sess("chat", frame={"app_id": "circles"}, history_before=before), user_message="hi", now=NOW))
tq2 = next((q, a) for q, a in pool2.queries if "FROM entity_memory" in q)
check("circles: app scope + project_id IS NULL, continuing thread, last message age",
      facts2["surface"] == "Circles" and "project_id IS NULL" in tq2[0] and "circles" in tq2[1]
      and facts2["user_turns_before"] == 6 and 1190 < facts2["last_turn_age_s"] < 1210, tq2[0])
check("a replay's history_before cuts the count too", "created_at < $2" in tq2[0] and tq2[1][1] == before, tq2[0])
check("no project row queried for circles", not any("FROM projects" in q for q, _ in pool2.queries))

# Workbench with no project: every Workbench thread is a project since
# the Start popup replaced the General thread, so the frame has
# no General-thread fact and never describes one.
facts3 = asyncio.run(F.frame_facts(_Pool(turns={"user_turns": 2, "total": 4, "last_at": None}),
                                   _Sess("workbench", frame={"app_id": "workbench", "project_id": None}), now=NOW))
check("workbench with no project carries no general_thread fact and no project",
      "general_thread" not in facts3 and "project" not in facts3 and facts3["surface"] == "Workbench", repr(facts3))
check("…and renders the surface line alone (no 'General thread')",
      "General thread" not in F.render_frame(facts3) and "project_id=" not in F.render_frame(facts3), F.render_frame(facts3))


# A pool that fails everything still yields the surface + clock.
class _Dead:
    async def fetchrow(self, q, *a):
        raise RuntimeError("db down")


facts4 = asyncio.run(F.frame_facts(_Dead(), _Sess("chat", frame={"app_id": "circles"}), now=NOW))
check("every source degrades on its own", facts4["surface"] == "Circles" and "user_turns_before" not in facts4
      and facts4.get("comm", {}).get("handler_name") is None)

# ── 3. render_frame ─────────────────────────────────────────────────────────
r = F.render_frame(facts)
check("header", r.startswith("## This Turn"))
check("one line: surface · project (created; first turn) · new thread",
      'Surface: Workbench · project #9198 "weather" (created 3 min ago; this is its first turn) · new thread — this is the first message' in r, r)
check("the project binding moved here from surface_role", "project_id=9198" in r and "\"This project\" means project_id=9198" in r)
check("attachments counted by kind", "Attached this turn: 2 images, 1 document." in r, r)
check("a document's content is placed in the message this turn",
      "Each attached document is in the user's message this turn" in r, r)
r_img = F.render_frame({"surface": "Circles", "attachments": ["image"]})
check("…and only when a document is attached", "user's message this turn" not in r_img, r_img)
check("pills named as references", "References the user attached from an app: [artifact 12]." in r, r)
check("handler + the clock in the user's timezone, UTC beside it",
      "With: Alex (verified handler) · Now: 2026-09-06 14:05 PDT (Sunday) — the user's local time (America/Los_Angeles); UTC 21:05" in r, r)
check("no summary age note (nothing renders that summary any more)",
      "rolling conversation summary" not in r)
check("one screen", len(r) < 1200, str(len(r)))
r2 = F.render_frame(facts2)
check("continuing thread: turn index + last message age", "turn 7 of a continuing thread (last message 20 min ago)" in r2, r2)
check("circles: no project binding, no attachments line", "project_id=" not in r2 and "Attached" not in r2, r2)
r3 = F.render_frame({"surface": "Workbench", "system_turn": True, "view": "Ops pane", "artifact_id": "a-7"})
check("system turn + open view + artifact in focus render", "platform-started turn" in r3 and "Open view: Ops pane." in r3
      and "Artifact in focus: a-7." in r3, r3)
check("no facts at all still renders a header", F.render_frame({}).startswith("## This Turn"))
r5 = F.render_frame({"surface": "Circles", "comm": {"handler_name": "Alex", "handler_verified": True}, "now": NOW})
check("no timezone: UTC with a note", "UTC (Sunday) — no user timezone is set" in r5, r5)
r6 = F.render_frame({"surface": "Workbench", "project": {"id": 5, "age_s": 100, "name": "x"}, "user_turns_before": 3})
check("a young project with prior turns is 'created' but not 'first turn'", "(created 1 min ago)" in r6 and "first turn" not in r6, r6)

# ── 4. schema ───────────────────────────────────────────────────────────────
per_turn = [b for b in ps.PROMPT_SCHEMA if b.freshness is ps.Freshness.PER_TURN]
check("frame is a PER_TURN block, gate always", any(b.name == "frame" and b.gate is ps.always for b in per_turn))
check("frame is the FIRST per-turn block (first after the cache split)", per_turn and per_turn[0].name == "frame",
      repr([b.name for b in per_turn]))


# ── 5. the clock left the cached prefix ─────────────────────────────────────
class _CommPool:
    async def fetchrow(self, q, *a):
        if "FROM entity_registry" in q:
            return {"operator_id": "alex@example.com",
                    "behavioral_params": {"timezone": "America/Los_Angeles", "handler_name": "Alex"}}
        return None


c1 = asyncio.run(_resolve_communication_context(_CommPool(), 3))
check("comm_context names the handler and the timezone rule", "Alex" in c1 and "America/Los_Angeles" in c1)
check("comm_context carries no clock", "Current time" not in c1 and "UTC now" not in c1 and "Today is" not in c1, c1)
cc_src = inspect.getsource(ps._b_comm_context)
check("_b_comm_context no longer appends the date / the summary age",
      "strftime(" not in cc_src and "conversation_summary.md" not in cc_src and "updated_at" not in cc_src)

# ── 6. the field travels ────────────────────────────────────────────────────
sess_src = turn_source(REPO)
check("EntitySession has a frame field", "frame: Optional[dict] = None" in sess_src)
check("send_message accepts frame and hands it to the transient builder",
      "        frame: dict | None = None,\n" in sess_src
      and "turn_type_id: str | None = None,\n" in sess_src
      and "may_start_work: bool = True,\n    ) -> dict:" in sess_src
      and "history_context=history_context, frame=frame," in sess_src)
check("the transient builder stores app_id + project_id on the frame",
      'frame={**(frame or {}), "app_id": app_id, "project_id": project_id}' in sess_src)
check("send-time facts (attachment kinds, artifact, system turn) are added before the per-turn build",
      '_fr["attachments"] = _kinds' in sess_src and sess_src.index('_fr["attachments"] = _kinds') < sess_src.index("_pt_block = await build_per_turn_prompt"))
check("the frame is built fresh each turn from the platform keys, never from last turn's dict (durable sessions)",
      '_fr: dict = {k: _base[k] for k in ("app_id", "project_id")' in sess_src and "dict(getattr(sess, \"frame\", None) or {})\n            _kinds" not in sess_src)
check("client-sent attachment type is normalised to image/document/file",
      '_kinds.append(_t if _t in ("image", "document") else "file")' in sess_src)
check("a client-sent artifact id is a short token before it reaches the prompt",
      're.sub(r"[^\\w.:-]", "", str(artifact_id))[:80]' in sess_src)
check("the durable path also takes the request's frame", "_req = frame if isinstance(frame, dict) else _base" in sess_src)
bs = (REPO / "server/brain_service.py").read_text()
check("brain_service forwards frame to the runtime", "frame: Optional[dict] = None," in bs and "            frame=frame,\n            turn_type_id=turn_type_id,\n            may_start_work=may_start_work,\n        ))" in bs)
for route in ("server/routes/brain.py", "server/routes/circles.py"):
    src = (REPO / route).read_text()
    check(f"{route}: validates the request field and forwards it",
          'validate_frame(body.get("frame"))' in src and "frame=frame," in src)
api = (REPO / "circles-web/src/lib/api.ts").read_text()
check("circles-web sends composer pills as frame.pills", "payload.frame = { pills: opts.pills }" in api)
comp = (REPO / "circles-web/src/cards/composer.tsx").read_text()
check("the composer hands the pills out beside the text (still folded into the text too)",
      "onSend(withPills, sending, reached, sentPills)" in comp and "onSend(m.text, sending, reached, sentPills)" in comp)
back = (REPO / "circles-web/src/cards/use-host-thread.ts").read_text()
check("your Host's back sends them with the turn",
      "pills: opts.pills && opts.pills.length ? opts.pills : undefined," in back)
hook = (REPO / "circles-web/src/hooks/use-circles-chat.ts").read_text()
check("the chat hook threads pills through SendOptions", "pills?: string[];" in hook and "pills: opts?.pills," in hook)

print("\nPASS" if not fails else f"\nFAIL: {fails} check(s)")
sys.exit(1 if fails else 0)
