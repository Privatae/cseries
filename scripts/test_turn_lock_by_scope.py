#!/usr/bin/env python3
"""Gate: one conversation, one turn, whoever is typing.

server/turn_registry.compose_thread_key keyed the turn lock by the SENDING
APP: "{app}|{entity}|{project}" plus "|{thread}". Two apps sending into the
same Circles thread or the same project got two keys, so two turns could run
in one conversation at once instead of the second message becoming a nudge
into the first. A messaging connector is exactly such a second app. The key
now names the conversation: thread|{entity}|{thread}, project|{entity}|{project},
and a turn that thinks in another app's conversation (history_context) shares
that conversation's key.

  thread     a connector-style app id sending into Circles thread T while a
             Circles turn runs in T is a nudge into that turn: same turn id,
             one turn running, the nudge on the running turn's stream
  project    the same for project P against a Workbench turn in P
  original   a send with another app id and history_context="chat" (the
             original Circles conversation: memory scope app:circles,
             memory_engine.derive_source_context) is a nudge into a plain
             Circles send with no thread; history_context="project:N" is
             project N
  unchanged  a key with no thread, no project and no history_context is
             byte-identical to before, and such an app keeps its own lane.
             (This one cannot be red first: it asserts that nothing changed.)
  lookup     GET /api/circles/turns/active, GET /api/brain/turns/active and
             DELETE /api/circles/threads/{id} find a turn ANOTHER app started
             under the scope key, and cancel stops it
  carried    a turn that thinks in another conversation keeps it wherever
             its words go next: the room the send path records, the
             follow-up a requeued nudge runs as, and the turn
             session.answer_after_turn starts from that room
  wake-up    the person's answer to a fired wake-up that stopped reading
  answer     (its registry room: app scheduled_task, history_context the
             conversation it was set in) runs as the person's own turn in the
             room that wake-up reports into, never borrowed from it: project
             N is a Workbench turn in N, a Circles thread or the original
             conversation a Circles turn there, with no history_context
  variants  Circles' replay and A/B, which ran a turn beside the lock, are
             gone, and stay gone.

Real brain_service.send_message, the real registry and the real route
functions; the turn body (_send_message_inner) is a stub with the real
signature that holds until released. No DB, no model, no network.

Run: APP_ROOT=<repo> python3 scripts/test_turn_lock_by_scope.py
"""
import ast
import asyncio
import functools
import json
import os
import sys
import types
from pathlib import Path

_ROOT = Path(os.environ.get("APP_ROOT") or Path(__file__).resolve().parents[1])
sys.path.insert(0, str(_ROOT / "brain"))
sys.path.insert(0, str(_ROOT))

# circles.py imports server.main at module scope, and server.main mounts
# /app/dashboard/base (absent outside the image). Nothing here needs it.
sys.modules.setdefault("server.main", types.ModuleType("server.main"))

from server import brain_service as BS  # noqa: E402
from server import turn_registry as TR  # noqa: E402
from server.routes import brain as BR  # noqa: E402
from server.routes import circles as CR  # noqa: E402

FAILS: list = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"[turn-lock-by-scope] {'ok  ' if ok else 'FAIL'} {name}"
          + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILS.append(name)


EID = 3
THREAD = "0b1c2d3e-4f50-4617-8293-a4b5c6d7e8f9"   # canonical, as the send path carries it
PROJECT = 251
CONNECTOR = "telegram-connector"

REAL_INNER = BS._send_message_inner
CALLS: list = []          # kwargs of every turn body started
STATE = types.SimpleNamespace(release=None)


@functools.wraps(REAL_INNER)   # the real signature, so _followup_maker binds as in production
def _stub_inner(*args, **kwargs):
    CALLS.append(dict(kwargs))

    async def _body():
        yield '0:"working"\n'
        await STATE.release.wait()
        yield '0:" done"\n'
    return _body()


BS._send_message_inner = _stub_inner
BS._entity_id = EID
BS._pool = None           # the deleted-thread check in send_message needs a pool; none here


def fresh() -> TR.TurnRegistry:
    TR._registry = None
    STATE.release = asyncio.Event()
    CALLS.clear()
    return TR.get_turn_registry()


async def finish(reg: TR.TurnRegistry) -> None:
    STATE.release.set()
    for rec in list(reg._by_id.values()):
        try:
            await asyncio.wait_for(rec.done.wait(), 5)
        except asyncio.TimeoutError:
            rec.task.cancel()


async def send(**kw) -> dict:
    """One brain_service.send_message call: its first frame is the turn
    metadata (mode + turn id). The tail is then dropped, which never touches
    the turn."""
    agen = BS.send_message(entity_id=EID, session_id=None, **kw)
    frame = await asyncio.wait_for(agen.__anext__(), 5)
    await agen.aclose()
    if frame.startswith("3:"):
        return {"error": json.loads(frame[2:])}
    items = json.loads(frame[2:])
    return items[0] if items else {}


def running(reg: TR.TurnRegistry) -> list:
    return [r for r in reg._by_id.values() if not r.done.is_set()]


def nudges_on(reg: TR.TurnRegistry, turn_id: str) -> list:
    rec = reg.get(turn_id)
    out = []
    for f in (rec.frames if rec else []):
        if f.startswith("2:") and '"nudge"' in f:
            out.extend(i.get("text") for i in json.loads(f[2:]) if i.get("type") == "nudge")
    return out


def compose(*a, **kw):
    try:
        return TR.compose_thread_key(*a, **kw)
    except Exception as e:  # noqa: BLE001 — the unpatched signature has no history_context
        return f"<raised {type(e).__name__}: {e}>"


async def main() -> None:
    # ── thread ────────────────────────────────────────────────────────
    reg = fresh()
    first = await send(message="from circles", app_id="circles", thread_id=THREAD)
    second = await send(message="from telegram", app_id=CONNECTOR, thread_id=THREAD)
    check("thread: a Circles turn in T starts",
          first.get("mode") == "started", repr(first))
    check("thread: a connector message into T while it runs is a nudge into that turn",
          second.get("mode") == "nudged" and second.get("turn_id") == first.get("turn_id"),
          f"first={first.get('turn_id')} second={second}")
    check("thread: one turn runs in T, not two",
          len(running(reg)) == 1, f"{len(running(reg))} running")
    check("thread: the nudge is on the running turn's stream",
          "from telegram" in nudges_on(reg, first.get("turn_id")),
          repr(nudges_on(reg, first.get("turn_id"))))
    check("thread: the key is the thread's, whatever the app",
          compose(EID, CONNECTOR, None, thread_id=THREAD)
          == compose(EID, "circles", None, thread_id=THREAD)
          == f"thread|{EID}|{THREAD}",
          compose(EID, CONNECTOR, None, thread_id=THREAD))
    await finish(reg)

    # ── project ───────────────────────────────────────────────────────
    reg = fresh()
    first = await send(message="from workbench", app_id="workbench", project_id=PROJECT)
    second = await send(message="from telegram", app_id=CONNECTOR, project_id=PROJECT)
    check("project: a connector message into P while a Workbench turn runs is a nudge into it",
          second.get("mode") == "nudged" and second.get("turn_id") == first.get("turn_id"),
          f"first={first.get('turn_id')} second={second}")
    check("project: one turn runs in P, not two",
          len(running(reg)) == 1, f"{len(running(reg))} running")
    check("project: the key is the project's, whatever the app",
          compose(EID, CONNECTOR, PROJECT) == compose(EID, "workbench", PROJECT)
          == f"project|{EID}|{PROJECT}", compose(EID, CONNECTOR, PROJECT))
    await finish(reg)

    # ── original conversation (history_context) ──────────────────────
    reg = fresh()
    first = await send(message="from circles", app_id="circles")
    second = await send(message="from telegram", app_id=CONNECTOR, history_context="chat")
    check("original: another app sending with history_context='chat' while a plain Circles "
          "turn runs is a nudge into it",
          second.get("mode") == "nudged" and second.get("turn_id") == first.get("turn_id"),
          f"first={first.get('turn_id')} second={second}")
    check("original: one turn runs in the original conversation, not two",
          len(running(reg)) == 1, f"{len(running(reg))} running")
    await finish(reg)
    check("original: history_context 'chat' and 'circles' key as Circles' own send does",
          compose(EID, CONNECTOR, None, history_context="chat")
          == compose(EID, CONNECTOR, None, history_context="circles")
          == compose(EID, "circles", None) == f"circles|{EID}|none",
          compose(EID, CONNECTOR, None, history_context="chat"))
    check("original: history_context 'project:N' is project N",
          compose(EID, "scheduled_task", None, history_context=f"project:{PROJECT}")
          == compose(EID, "workbench", PROJECT),
          compose(EID, "scheduled_task", None, history_context=f"project:{PROJECT}"))
    check("original: a thread or a project wins over history_context, as in "
          "derive_source_context",
          compose(EID, CONNECTOR, None, thread_id=THREAD, history_context="chat")
          == f"thread|{EID}|{THREAD}"
          and compose(EID, CONNECTOR, PROJECT, history_context="chat")
          == f"project|{EID}|{PROJECT}",
          compose(EID, CONNECTOR, PROJECT, history_context="chat"))

    # ── unchanged ─────────────────────────────────────────────────────
    check("unchanged: keys with no thread, no project and no history_context are byte-identical",
          compose(EID, "circles", None) == "circles|3|none"
          and compose(EID, "workbench", None) == "workbench|3|none"
          and compose(EID, "vscode", None) == "vscode|3|none"
          and compose(EID, None, None) == "chat|3|none"
          and compose(EID, CONNECTOR, None) == f"{CONNECTOR}|3|none",
          repr([compose(EID, a, None) for a in ("circles", "workbench", "vscode", None)]))
    reg = fresh()
    first = await send(message="from circles", app_id="circles")
    own = await send(message="from an app", app_id=CONNECTOR)
    check("unchanged: an app with no thread, project or history_context keeps its own lane",
          own.get("mode") == "started" and own.get("turn_id") != first.get("turn_id"),
          repr(own))
    await finish(reg)

    # ── lookup / cancel ───────────────────────────────────────────────
    reg = fresh()
    started = await send(message="from telegram", app_id=CONNECTOR, thread_id=THREAD)
    found = (await CR.turn_active(thread=THREAD.upper())).get("turn") or {}
    check("lookup: GET /api/circles/turns/active?thread=T finds the turn a connector started in T",
          found.get("turn_id") == started.get("turn_id"), f"found={found.get('turn_id')}")
    if found.get("turn_id"):
        out = await CR.turn_cancel(found["turn_id"])
        after = (await CR.turn_active(thread=THREAD)).get("turn")
        check("lookup: cancelling the turn it found stops it",
              out.get("ok") and out.get("status") == "cancelled" and after is None,
              f"cancel={out} after={after}")
    else:
        check("lookup: cancelling the turn it found stops it", False, "nothing found to cancel")
    await finish(reg)

    reg = fresh()
    started = await send(message="from telegram", app_id=CONNECTOR, project_id=PROJECT)
    wb = types.SimpleNamespace(state=types.SimpleNamespace(
        app_token=types.SimpleNamespace(app_id="workbench")))
    found = (await BR.brain_turn_active(wb, project_id=PROJECT)).get("turn") or {}
    check("lookup: the Workbench's GET /api/brain/turns/active?project_id=P finds the turn a "
          "connector started in P",
          found.get("turn_id") == started.get("turn_id"), f"found={found.get('turn_id')}")
    if found.get("turn_id"):
        out = await BR.brain_turn_cancel(found["turn_id"])
        after = (await BR.brain_turn_active(wb, project_id=PROJECT)).get("turn")
        check("lookup: the Workbench's stop cancels it",
              out.get("ok") and out.get("status") == "cancelled" and after is None,
              f"cancel={out} after={after}")
    else:
        check("lookup: the Workbench's stop cancels it", False, "nothing found to cancel")
    await finish(reg)

    reg = fresh()
    started = await send(message="from telegram", app_id=CONNECTOR, history_context="chat")
    found = (await CR.turn_active(thread=None)).get("turn") or {}
    check("lookup: GET /api/circles/turns/active (original conversation) finds a turn sent "
          "with history_context='chat'",
          found.get("turn_id") == started.get("turn_id"), f"found={found.get('turn_id')}")
    await finish(reg)

    reg = fresh()
    started = await send(message="from telegram", app_id=CONNECTOR, thread_id=THREAD)
    _orig_delete, _orig_pool = CR._ct.delete_thread, CR._threads_pool

    async def _deleted(pool, eid, tid):
        return {"cancelled_ids": []}
    CR._ct.delete_thread, CR._threads_pool = _deleted, (lambda: object())
    try:
        await CR.delete_circles_thread(THREAD)
    finally:
        CR._ct.delete_thread, CR._threads_pool = _orig_delete, _orig_pool
    rec = reg.get(started.get("turn_id") or "")
    check("lookup: deleting thread T in Circles stops the turn a connector is running in it",
          rec is not None and rec.status == "cancelled",
          f"status={getattr(rec, 'status', None)}")
    await finish(reg)

    # ── carried: the conversation follows the turn's words ────────────
    reg = fresh()
    started = await send(message="from telegram", app_id=CONNECTOR, history_context="chat")
    rec = reg.get(started.get("turn_id") or "")
    room = (rec.room or {}) if rec else {}
    check("carried: the room the send path records keeps history_context",
          room.get("history_context") == "chat", repr(room))
    if rec is not None and rec.make_followup is not None:
        rec.make_followup("a nudge nobody read")
        check("carried: a requeued nudge runs as a follow-up in the same conversation",
              CALLS and CALLS[-1].get("history_context") == "chat"
              and CALLS[-1].get("app_id") == CONNECTOR,
              repr(CALLS[-1] if CALLS else None))
    else:
        check("carried: a requeued nudge runs as a follow-up in the same conversation",
              False, "no follow-up maker on the record")
    await finish(reg)

    reg = fresh()
    from mastercode.runtime import session as S
    ok = await S.answer_after_turn(
        EID, {"app_id": CONNECTOR, "project_id": None, "thread_id": None,
              "history_context": "chat"}, "my answer")
    new = [r for r in reg._by_id.values() if r.user_message == "my answer"]
    check("carried: an answer after a stopped turn starts in that turn's conversation",
          ok and len(new) == 1 and new[0].thread_key == compose(EID, "circles", None)
          and CALLS and CALLS[-1].get("history_context") == "chat",
          f"ok={ok} keys={[r.thread_key for r in new]} "
          f"hc={CALLS[-1].get('history_context') if CALLS else None}")
    await finish(reg)

    # ── wake-up answer: a person's answer runs IN the wake-up's room ──
    # A fired wake-up runs on the hidden scheduled_task lane, thinking in the
    # conversation it was set in (history_context) under its own key. Once it
    # has stopped reading, the person's words start a turn of their own from
    # the room the registry recorded (routes/circles.nudge ->
    # answer_after_turn). That turn is the person's, in the room the wake-up
    # reports into (wakeup_delivery_room): never borrowed from it, because a
    # borrowed turn writes its rows outside the conversation it thinks in
    # (session.py _row_project_id). A Tickets-pane answer to the same ask
    # already lands there, from the room escalate records.
    for label, hc, tid, want in (
            ("a project timer", f"project:{PROJECT}", None,
             {"app_id": "workbench", "project_id": PROJECT, "thread_id": None,
              "key": f"project|{EID}|{PROJECT}"}),
            ("a Circles-thread timer", "chat", THREAD,
             {"app_id": "circles", "project_id": None, "thread_id": THREAD,
              "key": f"thread|{EID}|{THREAD}"}),
            ("an original-conversation timer", "chat", None,
             {"app_id": "circles", "project_id": None, "thread_id": None,
              "key": f"circles|{EID}|none"})):
        reg = fresh()
        await send(message="the timer's brief", app_id="scheduled_task", history_context=hc,
                   thread_id=tid, thread_key=f"wakeup|{EID}|77", system_turn=True)
        woke = next((r for r in reg._by_id.values() if r.thread_key == f"wakeup|{EID}|77"), None)
        ok = bool(woke) and await S.answer_after_turn(EID, dict(woke.room or {}), "my answer")
        new = [r for r in reg._by_id.values() if r.user_message == "my answer"]
        got = CALLS[-1] if (ok and CALLS) else {}
        check(f"wake-up answer: {label}'s answer runs as the person's own turn in the room "
              "it reports into, not borrowed from it",
              ok and len(new) == 1 and new[0].thread_key == want["key"]
              and got.get("app_id") == want["app_id"]
              and got.get("project_id") == want["project_id"]
              and got.get("thread_id") == want["thread_id"]
              and got.get("history_context") is None,
              f"ok={ok} keys={[r.thread_key for r in new]} app={got.get('app_id')} "
              f"project={got.get('project_id')} thread={got.get('thread_id')} "
              f"hc={got.get('history_context')!r}")
        await finish(reg)

    # ── variants: gone ───────────────────────────────────────────────
    # Circles' replay and A/B ran a turn beside the running one, outside the
    # lock; they left with the rail, so nothing in the Circles routes runs a
    # turn but /messages.
    tree = ast.parse((_ROOT / "server/routes/circles.py").read_text(encoding="utf-8"))
    names = {n.name for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    check("variants: Circles has no replay or A/B road to a turn beside the lock",
          not ({"_replay", "_run_one_ab_side", "_ab_fan", "replay", "ab_fan"} & names), sorted(names & {"_replay", "_run_one_ab_side", "_ab_fan"}))


if __name__ == "__main__":
    asyncio.run(main())
    if FAILS:
        print(f"[turn-lock-by-scope] {len(FAILS)} FAILED")
        sys.exit(1)
    print("[turn-lock-by-scope] all checks passed")
    sys.exit(0)
