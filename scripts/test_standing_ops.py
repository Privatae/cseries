#!/usr/bin/env python3
"""Standing work: the deterministic scheduling lane must stay deterministic.

WHAT THIS PROTECTS.
A scheduled task can now carry {"op": {"tool", "args"}} and fire ONE registry
op through run_direct_op — no model, no turn, no tokens. The whole value of
that lane is that it never quietly becomes a model call again, and there are
exactly two ways it could:

  1. THE TITLE FALLBACK. offline_task_runner falls back to
     `prompt = f"Run the scheduled task: {title}"` when a payload carries
     neither prompt nor steps. An op payload carries neither. If that fallback
     is not guarded on the op branch, every standing op silently becomes a full
     governed turn on that sentence AND is recorded ok=true — a green tick over
     precisely the anti-pattern the lane exists to remove. This is not
     hypothetical: it is what the first draft of the design doc got wrong, and
     it is what an older pod image does with a new payload.

  2. THE FIRE-TIME CHECK. `_exec_schedule_task` is not the only writer of a
     scheduled_task payload — POST /api/calendar wraps handle_calendar_append
     directly for the Circles compose modal and the PME dispatch path. So the
     authoring-time check is UX fail-fast and the runner's own check is the
     guard. Delete the runner's copy and the tool's copy becomes bypassable.

It also holds the FITNESS line. UNATTENDED_OP_TOOLS answers "does this op
complete with nobody present?" — it is NOT a safety gate (the CEIGAS caps gate
owns authorization, at fire time, fail-closed) and it must not grow into one.
Ops whose result a person has to act on stay out because they would not work:
open_app opens a window nobody sees; harness_connections op='login' returns a
vendor URL into the void.

WHAT THIS DOES NOT CATCH. It reads source text for the two structural
couplings; it does not execute the runner (no DB, no pool, no entity). A
refactor that preserves the strings but breaks the ordering would pass. The
durable protection is the comment at the fallback site; this is the cheap
tripwire for the common case and an honest statement of its own limits.

Container-free. Run: APP_ROOT=<repo> python3 scripts/test_standing_ops.py
"""
import os
import sys

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


def main() -> int:
    repo = os.environ.get("APP_ROOT") or os.path.dirname(
        os.path.dirname(os.path.abspath(__file__)))
    sys.path.insert(0, repo)
    sys.path.insert(0, os.path.join(repo, "brain"))

    print("standing ops — the deterministic lane")

    # ── 1. The fitness set ──────────────────────────────────────────────
    # Imported defensively so a tree WITHOUT the lane reports every missing
    # piece as a named failure instead of dying on a traceback. A gate you
    # cannot watch fail line by line is a gate nobody trusts.
    import mastercode.runtime.direct_ops as _dops  # noqa: E402

    DIRECT_OP_TOOLS = getattr(_dops, "DIRECT_OP_TOOLS", frozenset())
    UNATTENDED_OP_TOOLS = getattr(_dops, "UNATTENDED_OP_TOOLS", None)
    is_unattended_op_tool = getattr(_dops, "is_unattended_op_tool", None)

    check("direct_ops exports UNATTENDED_OP_TOOLS",
          UNATTENDED_OP_TOOLS is not None,
          "The fitness classification does not exist: nothing distinguishes an\n"
          "op that completes with nobody present from one that needs a person.")
    check("direct_ops exports is_unattended_op_tool",
          callable(is_unattended_op_tool),
          "Without the predicate there is no fire-time guard.")
    if UNATTENDED_OP_TOOLS is None:
        UNATTENDED_OP_TOOLS = frozenset()
    if not callable(is_unattended_op_tool):
        def is_unattended_op_tool(_n):  # noqa: E306
            return False

    check("UNATTENDED_OP_TOOLS is a subset of DIRECT_OP_TOOLS",
          UNATTENDED_OP_TOOLS <= DIRECT_OP_TOOLS,
          "An op that needs a model cannot be a standing op. Stray: "
          f"{sorted(UNATTENDED_OP_TOOLS - DIRECT_OP_TOOLS)}")

    check("the set is not empty",
          bool(UNATTENDED_OP_TOOLS),
          "An empty set means the host has no standing-op capability at all.")

    # These are the defining examples in the design doc. They are excluded for
    # FITNESS (their result needs a person), never for danger — if one is added,
    # the reason in the doc has to change too.
    for name, why in (
        ("open_app", "opens a window on the user's screen — nobody is there at 03:00"),
        ("harness_connections", "op='login' hands back a vendor URL for the user to visit"),
    ):
        check(f"{name} is not schedulable",
              not is_unattended_op_tool(name),
              f"{name} does not complete with nobody present: {why}.")

    # ── 2. The title fallback must be guarded on the op branch ──────────
    runner_path = os.path.join(repo, "brain", "mastercode", "brain",
                               "offline_task_runner.py")
    runner = open(runner_path, encoding="utf-8").read()

    check("the title fallback is guarded on the op branch",
          "if not op_tool and not prompt and not steps:" in runner,
          "offline_task_runner's fallback to `Run the scheduled task: <title>`\n"
          "is no longer guarded on op_tool. An op payload carries neither a\n"
          "prompt nor steps, so it will now fall through to a FULL governed\n"
          "model turn and be recorded ok=true — silent token burn wearing a\n"
          "green tick, on the exact lane built to avoid it.")

    check("the op branch returns before the turn path",
          "await _report_and_finalize(pool, entity_id, event_id, title, recurrence," in runner
          # Matched WITHOUT the closing paren: the call gained an `origin`
          # kwarg (a fired task reports into the thread it was
          # set in), and the property this guards is that the op branch
          # finalizes and returns, not the argument list's exact shape.
          and "report_on_success=False," in runner
          and "        return\n" in runner,
          "The op branch must finalize and RETURN. If it falls through, a\n"
          "refused or failed op becomes a model turn — turning 'this op is not\n"
          "schedulable' into a token spend.")

    # ── 3. The fire-time guard ──────────────────────────────────────────
    op_fn = runner.split("async def _run_op(", 1)
    check("_run_op exists", len(op_fn) == 2,
          "The deterministic execution path is gone.")
    if len(op_fn) == 2:
        body = op_fn[1].split("\nasync def ", 1)[0]
        guard = body.find("is_unattended_op_tool")
        call = body.find("run_direct_op(")
        check("the fitness check runs BEFORE run_direct_op at fire time",
              guard != -1 and call != -1 and guard < call,
              "The runner's own check is the guard — POST /api/calendar writes\n"
              "scheduled_task payloads without passing through schedule_task,\n"
              "so an authoring-only check is bypassable by that route.")
        check("a denied op is recorded as a failed run",
              'status == "ok"' in body,
              "run_direct_op returns {'status': 'denied'} without raising. If\n"
              "that is not turned into a failed run, a denial is recorded as a\n"
              "success and the run history lies.")

    # ── 4. The host is TOLD about the lane ──────────────────────────────
    from mastercode import public_tool_registry as reg  # noqa: E402

    sched = reg.PUBLIC_TOOL_SCHEMAS["schedule_task"]["function"]
    props = sched["parameters"]["properties"] or {}
    check("schedule_task advertises op_tool + op_args",
          "op_tool" in props and "op_args" in props,
          "A tool's description is the only thing a model reads before choosing\n"
          "it. An undocumented lane is an unused lane.")

    cal = reg.PUBLIC_TOOL_SCHEMAS["calendar_read"]["function"]
    check("calendar_read advertises op='runs'",
          "runs" in (cal["parameters"]["properties"].get("op", {}).get("enum") or []),
          "Standing ops are created without asking the user, so the outcome has\n"
          "to be readable. calendar_event_runs has existed since v072 with no\n"
          "host tool able to read it.")

    # ── 5. The prompt marks the deterministic lane ──────────────────────
    from mastercode.runtime.prompt_builder import _build_tool_inventory  # noqa: E402

    rendered = _build_tool_inventory([
        {"name": "schedule_task", "category": "comms", "priority": 1,
         "purpose": "x", "requires": "write:calendar"},
        {"name": "research", "category": "comms", "priority": 2, "purpose": "y"},
    ]) or ""
    sched_line = next((ln for ln in rendered.splitlines()
                       if ln.startswith("- **schedule_task**")), "")
    research_line = next((ln for ln in rendered.splitlines()
                          if ln.startswith("- **research**")), "")
    check("a deterministic tool renders the no-model mark",
          "no model" in sched_line,
          "is_direct_op_tool calls itself a predicate for the catalogs/prompt.\n"
          "If the prompt does not use it, the host cannot see the cheap lane —\n"
          f"got: {sched_line!r}")
    check("a model-in-the-tool tool does NOT render it",
          "no model" not in research_line,
          "research runs a model by definition; marking it would teach the\n"
          f"host the opposite of the truth — got: {research_line!r}")
    check("the dispatch grant survives beside it",
          "dispatch grant: write:calendar" in sched_line,
          "The grant is what the host writes `grant` from; losing it re-opens\n"
          "the refused-at-the-door failure the suffix was added for.")

    # ── 6. BEHAVIOUR — the branch actually runs, and actually refuses ───
    # The checks above read source text. These drive the real functions with a
    # stubbed registry, which is what catches an ordering change a string match
    # would sail past.
    import asyncio  # noqa: E402
    import mastercode.brain.offline_task_runner as runner_mod  # noqa: E402

    if not hasattr(runner_mod, "_run_op"):
        # Same reason as the defensive import above: on a tree without the lane
        # this must read as a named absence, not an AttributeError.
        check("the deterministic execution path is drivable", False,
              "offline_task_runner._run_op does not exist, so none of the\n"
              "behavioural checks below can run. The lane is absent.")
        print(f"\n{'PASS' if not FAILED else str(FAILED) + ' FAILED'}")
        return 1 if FAILED else 0

    calls: list[tuple] = []

    async def fake_run_direct_op(name, args, **kw):
        calls.append((name, args))
        return {"status": "ok", "result": {"did": name}}

    async def fake_denied(name, args, **kw):
        calls.append((name, args))
        return {"status": "denied", "result": f"`{name}` is not a registered op."}

    _real = _dops.run_direct_op
    try:
        # 6a. an unschedulable op is refused WITHOUT reaching the registry
        _dops.run_direct_op = fake_run_direct_op
        calls.clear()
        ok, _content, err = asyncio.run(
            runner_mod._run_op(None, 1, "open_app", {"app_id": "circles"}))
        check("an unschedulable op fails and never reaches the registry",
              (not ok) and (not calls) and "UnattendedOpRefused" in (err or ""),
              f"ok={ok!r} registry_calls={calls!r} err={err!r}")

        # 6b. a schedulable op runs, with the args frozen at authoring
        calls.clear()
        ok, content, err = asyncio.run(
            runner_mod._run_op(None, 1, "inbox_send", {"body": "hi"}))
        check("a schedulable op runs with its frozen args",
              ok and calls == [("inbox_send", {"body": "hi"})] and err is None,
              f"ok={ok!r} calls={calls!r} err={err!r} content={content!r}")

        # 6c. a DENIED op is a failed run, not a quiet success
        _dops.run_direct_op = fake_denied
        ok, _content, err = asyncio.run(
            runner_mod._run_op(None, 1, "inbox_send", {"body": "hi"}))
        check("a denied op is a FAILED run",
              (not ok) and "denied" in (err or ""),
              "run_direct_op returns a denial without raising; recording it as\n"
              f"a success would make the run history lie. ok={ok!r} err={err!r}")
    finally:
        _dops.run_direct_op = _real

    # ── 7. BEHAVIOUR — an op payload never becomes a model turn ─────────
    # The expensive regression, driven rather than grepped. If _run_one falls
    # through to the turn path, it reaches `from server import brain_service`
    # and cannot come back ok=True with the registry stub untouched.
    class _FakePool:
        def __init__(self):
            self.runs = []

        async def execute(self, sql, *a):
            self.runs.append((sql.split()[0].upper(), a))
            return "UPDATE 1"

    recorded: list[dict] = []

    async def fake_record_run(pool, event_id, entity_id, ok, excerpt, error,
                              cost_usd=None, priced_usd=None):
        recorded.append({"ok": ok, "excerpt": excerpt, "error": error})

    async def fake_finalize(*a, **kw):
        return None

    posts: list[str] = []

    # **kw so the stub tracks the real signature: _report_and_finalize
    # now passes project_id and created_at too, because a fired task
    # reports into the thread it was set in.
    async def fake_write_turn_row(pool, eid, sid, role, text, **kw):
        posts.append(text)
        post_tags.append(kw.get("tags") or {})

    post_tags: list[dict] = []
    # A standing op seeded with no origin reports into a topic of its own:
    # there is no original conversation. The fake pool
    # cannot create the topic, so the helper that does is stood in for.
    import mastercode.circles_threads as _ctm  # noqa: E402
    TOPIC = "11111111-2222-4333-8444-555555555555"

    async def fake_home(pool, eid, thread_id, *, title):
        return TOPIC

    import mastercode.memory_engine as _mem  # noqa: E402
    _r, _f, _w = runner_mod._record_run, runner_mod._finalize, _mem.write_turn_row
    _h = _ctm.home_thread
    _real2 = _dops.run_direct_op
    try:
        runner_mod._record_run = fake_record_run
        runner_mod._finalize = fake_finalize
        _mem.write_turn_row = fake_write_turn_row
        _dops.run_direct_op = fake_run_direct_op
        _ctm.home_thread = fake_home

        row = {"id": 7, "title": "nightly digest", "recurrence": "daily",
               "start_time": None,
               "payload": {"op": {"tool": "inbox_send", "args": {"body": "b"}}}}
        calls.clear()
        recorded.clear()
        posts.clear()
        asyncio.run(runner_mod._run_one(_FakePool(), 1, row))

        check("an op payload runs the op, not a turn",
              recorded and recorded[0]["ok"] is True and calls == [("inbox_send", {"body": "b"})],
              "An op payload must reach the registry and be recorded ok. If it\n"
              "fell through to the title fallback it would have run a governed\n"
              f"turn instead. recorded={recorded!r} calls={calls!r}")
        check("a successful op does not spam the Circles thread",
              posts == [],
              "A deterministic op that fires hourly and works would write a chat\n"
              "message every hour. Success belongs in the run history; only a\n"
              f"failure should speak. posts={posts!r}")

        # and the failure DOES speak
        _dops.run_direct_op = fake_denied
        recorded.clear()
        posts.clear()
        asyncio.run(runner_mod._run_one(_FakePool(), 1, row))
        check("a failing op still tells the user",
              recorded and recorded[0]["ok"] is False and len(posts) == 1,
              "Quiet on success, loud on failure — a standing op that breaks\n"
              f"silently is worse than one that never ran. posts={posts!r}")
        check("...in a topic of its own, never the old original conversation",
              post_tags and post_tags[-1].get("source_context") == {"kind": "thread", "ref": TOPIC},
              f"tags={post_tags[-1:]!r}")
    finally:
        runner_mod._record_run, runner_mod._finalize = _r, _f
        _mem.write_turn_row = _w
        _dops.run_direct_op = _real2
        _ctm.home_thread = _h

    # ── 8. The Agents app — the user's half of "never ask, always show" ──
    # The host creates standing work without asking, so the user must be
    # able to SEE it and STOP it. That makes this surface part of the contract,
    # not decoration.
    from server.routes.agents import _lane_of  # noqa: E402

    check("the scheduled lanes are told apart correctly",
          _lane_of({"op": {"tool": "inbox_send"}}) == ("op", "inbox_send")
          and _lane_of({"prompt": "x"}) == ("turn", None)
          and _lane_of('{"op":{"tool":"calendar_log"}}') == ("op", "calendar_log")
          and _lane_of("not json") == ("turn", None),
          "asyncpg may hand back JSONB as a string; both shapes must read the\n"
          "same, and unparseable payloads must fall to the turn lane, never\n"
          "silently become an op.")

    agents_route = open(os.path.join(repo, "server", "routes", "agents.py"),
                        encoding="utf-8").read()
    check("the app can list standing work",
          '@router.get("/scheduled"' in agents_route,
          "Without it the user cannot see what the host started on its own.")
    check("the app can stop standing work in one action",
          '@router.post("/scheduled/{event_id}/cancel"' in agents_route,
          "The off switch IS the user's control surface — it replaces the\n"
          "approval prompt, so it is not optional.")

    app_js = open(os.path.join(repo, "system-apps", "agents", "app.js"),
                  encoding="utf-8").read()
    # Comment-stripped for the same reason test_gate_coverage.py strips smoke:
    # this file DISCUSSES "$0" in its header, and an unstripped match would fail
    # on the prose that explains the rule.
    import re as _re  # noqa: E402
    app_code = _re.sub(r"/\*.*?\*/", "", app_js, flags=_re.S)
    app_code = "\n".join(ln.split("//", 1)[0] for ln in app_code.splitlines())
    check("an op is labelled 'no model', not a dollar amount",
          "'no model'" in app_code and "$0" not in app_code,
          "est_cost_usd is NOT NULL DEFAULT 0.0, so $0 already means 'we do not\n"
          "know' as often as it means free (v113 cost_method). Rendering an op\n"
          "as $0 puts the whole point of the lane behind a number the user has\n"
          "learned to ignore.")
    check("every standing row carries a Stop control",
          "stopStanding" in app_js and "'stop'" in app_js,
          "Seeing it is half the contract; ending it is the other half.")

    app_yaml = open(os.path.join(repo, "system-apps", "agents", "app.yaml"),
                    encoding="utf-8").read()
    check("the app declares the calendar scopes it now uses",
          "read:calendar" in app_yaml and "write:calendar" in app_yaml,
          "GET /api/agents/scheduled takes read:calendar and its cancel takes\n"
          "write:calendar (since 2026-10-01), so without these the standing\n"
          "lanes 403 and the Stop button does nothing.")

    print(f"\n{'PASS' if not FAILED else str(FAILED) + ' FAILED'}")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
