#!/usr/bin/env python3
"""Gate for the tool-trail context lane (runtime/tool_trail.py).

Origin: two fabrications in one thread (one of them the
"task 299" quote for task 302). Mechanism: tool results do not survive a turn
boundary — history is prose, and the result's `next_step` ("when they say
they have entered it, call op='login_status'") was consumed nowhere durable —
so the model inferred the outcome from the user's "done". The lane persists a
bounded digest + next_step on the per-call chip the turn row already carries,
and renders the recent calls + still-PENDING steps into the next turn.

Checks:
  1. digest_tool_action carries op/provider/harness, the nested next_step,
     an allow-listed digest (task_id, url, user_code, done …) — and never
     tokens/creds.
  2. render_tool_trail shows the login's next step as PENDING while no later
     call on the same tool+provider exists, and drops it once login_status
     for that provider has run (done or not); a call that came back done
     carries no pending step; a failed call's next_step is not a promise.
  3. The build chip's task id is in the block (the "task 299 vs 302" case).
  4. Older turns fall off (TOOL_TRAIL_MAX_TURNS) but still resolve pending
     steps of earlier calls; empty input renders "".
  5. Wiring: session.py folds the digest into ta_entry + the persisted chip
     and appends the rendered block after history; memory_engine exposes
     recall_tool_trail scoped like recall_recent_turns.

Run from the repo root:  python3 scripts/test_tool_trail.py
"""
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(os.environ.get("APP_ROOT") or Path(__file__).resolve().parents[1])
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "brain"))

from mastercode.runtime import tool_trail as tt  # noqa: E402

FAILS = 0


def check(label: str, cond: bool, detail: str = "") -> None:
    global FAILS
    print(("  ok   " if cond else "  FAIL ") + label + (f"  [{detail}]" if detail and not cond else ""))
    if not cond:
        FAILS += 1


T0 = datetime(2026, 8, 20, 22, 11, 4, tzinfo=timezone.utc)

LOGIN_NEXT = ("Give the user BOTH the link and this one-time code, exactly as written: "
              "2N41-F3AX9 . They open the link in their own browser, sign in, and type "
              "the code on that page. When they say they have entered it, call "
              "op='login_status', provider='codex' — that reads the grant out of the pod "
              "and saves it to this host.")

login_result = {"status": "ok", "result": {
    "harness": "codex", "ok": True, "done": False, "url": "https://auth.openai.com/codex/device",
    "user_code": "2N41-F3AX9", "login_waiting": True,
    "rotated_creds": {"credentials.json": "{\"access_token\": \"SECRET\"}"},
    "next_step": LOGIN_NEXT}}
login_status_done = {"status": "ok", "result": {
    "harness": "codex", "ok": True, "done": True, "persisted": True,
    "next_step": "Signed in, and the credential is saved to this host — nothing further for them to paste anywhere."}}
login_status_wait = {"status": "ok", "result": {
    "harness": "codex", "ok": True, "done": False, "login_waiting": True,
    "next_step": "Not signed in yet. …"}}
build_result = {"status": "ok", "result": {
    "task_id": 302, "project_id": 9207, "harness": "codex", "model": "gpt-5.3-codex",
    "effort": None, "state": "running",
    "note": "Follow-up build (seeded rerun) started on the 'codex' harness in the background — …"}}
err_result = {"status": "error", "result": "login failed: could not start codex: no url",
              "next_step": "Start again with op='login' for a fresh link."}


def main() -> int:
    # ── 1. digest ──────────────────────────────────────────────────────────
    d = tt.digest_tool_action("harness_connections", {"op": "login", "provider": "codex"}, login_result)
    check("digest: op + provider from args, next_step from the NESTED result",
          d.get("op") == "login" and d.get("provider") == "codex"
          and d.get("next_step", "").startswith("Give the user BOTH"), repr(d)[:200])
    dg = d.get("digest") or {}
    check("digest: allow-listed result keys (url, user_code, done, login_waiting) present",
          dg.get("url") == "https://auth.openai.com/codex/device" and dg.get("user_code") == "2N41-F3AX9"
          and dg.get("done") is False and dg.get("login_waiting") is True, repr(dg))
    check("digest: never carries credentials (rotated_creds is not allow-listed)",
          "rotated_creds" not in dg and "SECRET" not in repr(d), repr(d)[:200])
    b = tt.digest_tool_action("build", {"action": "continue", "harness": "codex", "project_id": 9207}, build_result)
    check("digest: build chip carries action as op + task_id/project_id/harness/model/state",
          b.get("op") == "continue" and b.get("harness") == "codex"
          and b["digest"].get("task_id") == 302 and b["digest"].get("project_id") == 9207
          and b["digest"].get("state") == "running" and b["digest"].get("model") == "gpt-5.3-codex"
          and "next_step" not in b, repr(b))
    e = tt.digest_tool_action("harness_connections", {"op": "login", "provider": "codex"}, err_result)
    check("digest: an error result records status + error text",
          e["digest"].get("status") == "error" and "could not start codex" in e["digest"].get("error", ""), repr(e))
    # An operate call's chip carries the INNER tool and reads its op from the
    # inner args (the record of a desktop write was once
    # `operate → ok` with no op, while the refused direct call said set_desktop).
    o = tt.digest_tool_action("operate", {"intent": "Setting the desktop", "tool": "artifact",
                                          "args": {"op": "set_desktop", "artifact_id": 5}},
                              {"status": "ok", "result": {"artifact_id": 5, "ok": True}})
    check("digest: operate carries operate_tool + the inner op",
          o.get("operate_tool") == "artifact" and o.get("op") == "set_desktop", repr(o))
    check("render: an operate chip reads `operate operate_tool=artifact op=set_desktop`",
          "operate operate_tool=artifact op=set_desktop" in tt._fmt_call({"tool": "operate", **o}))
    # A write's result has no preview_url; the reloaded chat card finds the
    # artifact by this key (circles-web lib/artifact-ref.ts).
    w = tt.digest_tool_action("operate", {"tool": "artifact", "args": {"op": "write", "artifact_id": 41}},
                              {"status": "ok", "result": {"artifact_id": 41, "files_written": ["index.html"]}})
    check("digest: an artifact write keeps artifact_id for the chat thumbnail",
          (w.get("digest") or {}).get("artifact_id") == 41, repr(w))
    # Two operate calls are the same action only when they operated the same
    # tool.
    # Provider-free chips on purpose: with a provider on one side the older
    # provider rule already separates them and the operate_tool key is not
    # what the test exercises (a pair with a provider survives deleting
    # the key).
    op_a = {"tool": "operate", "operate_tool": "artifact", "op": "set_desktop"}
    op_b = {"tool": "operate", "operate_tool": "install_app", "op": "install"}
    op_c = {"tool": "operate", "operate_tool": "artifact", "op": "write"}
    check("same_target: operate chips on different inner tools are different targets",
          not tt._same_target(op_a, op_b) and tt._same_target(op_a, op_c))
    pend = {"created_at": T0, "tool_calls": [{**op_a, "status": "ok", "next_step": "open the Artifacts window when they confirm"}]}
    later = {"created_at": T0 + timedelta(minutes=2), "tool_calls": [{**op_b, "status": "ok"}]}
    check("render: a later operate on ANOTHER tool leaves an operate write's step pending",
          "PENDING next step:" in tt.render_tool_trail([pend, later]))
    resolved = {"created_at": T0 + timedelta(minutes=3), "tool_calls": [{**op_c, "status": "ok"}]}
    check("render: a later operate on the SAME tool resolves it",
          "PENDING next step:" not in tt.render_tool_trail([pend, later, resolved]))
    check("digest: a prose/non-dict result yields only the arg fields",
          tt.digest_tool_action("recall", {"query": "x"}, "some text") == {}
          and tt.digest_tool_action("x", None, None) == {})
    long_ns = {"status": "ok", "next_step": "y" * 2000}
    check("digest: next_step is clipped",
          len(tt.digest_tool_action("t", {}, long_ns)["next_step"]) <= tt._NEXT_STEP_MAX)

    # ── 2/3. render: the exact recorded sequence ───────────────────────────
    def chip(tool, args, result, status="ok"):
        c = {"tool": tool, "args_summary": "", "status": status,
             "error": (result.get("result") if isinstance(result, dict)
                       and result.get("status") == "error" else None)}
        c.update(tt.digest_tool_action(tool, args, result))
        return c

    t_login = {"created_at": T0, "tool_calls": [
        chip("harness_connections", {"op": "login", "provider": "codex"}, login_result)]}
    # The turn that fabricated had zero tool calls — it is simply absent from the trail.
    blk = tt.render_tool_trail([t_login])
    check("render: login shown with url + user_code, and its next step is PENDING",
          "harness_connections op=login provider=codex" in blk
          and "user_code=2N41-F3AX9" in blk and "PENDING next step:" in blk
          and "login_status" in blk and "1 pending step" in blk, blk)
    check("render: the header tells the host a pending step is not done by the user's word",
          "not memory" in blk and "PENDING" in blk and "the user said" in blk)
    check("render: never leaks credentials", "SECRET" not in blk)

    t_status_wait = {"created_at": T0 + timedelta(minutes=3), "tool_calls": [
        chip("harness_connections", {"op": "login_status", "provider": "codex"}, login_status_wait)]}
    blk2 = tt.render_tool_trail([t_login, t_status_wait])
    check("render: a later login_status (not done) RESOLVES the login's pending step, "
          "and its own 'not signed in yet' step is pending in its place",
          blk2.count("PENDING next step:") == 1 and "Not signed in yet" in blk2
          and "Give the user BOTH" not in blk2, blk2)

    t_status_done = {"created_at": T0 + timedelta(minutes=6), "tool_calls": [
        chip("harness_connections", {"op": "login_status", "provider": "codex"}, login_status_done)]}
    t_build = {"created_at": T0 + timedelta(minutes=10), "tool_calls": [
        chip("build", {"action": "continue", "harness": "codex", "project_id": 9207}, build_result)]}
    blk3 = tt.render_tool_trail([t_login, t_status_wait, t_status_done, t_build])
    check("render: after login_status came back done, NO pending step remains; done/persisted shown",
          "PENDING next step:" not in blk3 and "pending step" not in blk3
          and "done=True" in blk3 and "persisted=True" in blk3, blk3)
    # A turn ABOUT the host widens the window (prefire_tools.is_self_turn):
    # the default three turns drop the login; a self turn keeps
    # it, with the refusal text a self-review has to quote.
    check("render: default window is the newest three turns (the login has fallen off)",
          "op=login " not in blk3 and "op=login\n" not in blk3
          and "user_code=2N41-F3AX9" not in blk3, blk3)
    denied = {"status": "denied", "reason": "capability_denied",
              "result": "'memory_curate' needs capability write_memory; this host holds read_context, read_memory."}
    t_denied = {"created_at": T0 + timedelta(minutes=1), "tool_calls": [
        chip("memory_curate", {"op": "promote"}, denied, status="denied")]}
    t_denied["tool_calls"][0]["error"] = denied["result"]
    wide = tt.render_tool_trail([t_login, t_denied, t_status_wait, t_status_done, t_build],
                                max_turns=12, max_calls=40)
    check("render: a widened window keeps the oldest turn AND the refusal's own words",
          "user_code=2N41-F3AX9" in wide and "memory_curate op=promote → denied" in wide
          and "needs capability write_memory" in wide, wide)
    check("render: widening never resurrects a resolved step as pending",
          "PENDING next step:" not in wide, wide)
    check("render: the build chip states task_id=302 (the 'task 299' fabrication has a record to read)",
          "build op=continue harness=codex" in blk3 and "task_id=302" in blk3
          and "project_id=9207" in blk3, blk3)
    check("render: only the newest TOOL_TRAIL_MAX_TURNS turns are listed (the first login fell off)",
          "op=login provider=codex" not in blk3.replace("op=login_status", "")
          and blk3.count("\n- [") == 3, blk3)

    # A different provider's later call must NOT resolve codex's pending step.
    t_other = {"created_at": T0 + timedelta(minutes=2), "tool_calls": [
        chip("harness_connections", {"op": "login_status", "provider": "grok"}, login_status_wait)]}
    blk4 = tt.render_tool_trail([t_login, t_other])
    check("render: a later call on ANOTHER provider does not resolve codex's pending step",
          blk4.count("PENDING next step:") == 2 and "Give the user BOTH" in blk4, blk4)

    # A later call on the same tool WITHOUT a
    # provider (op=list / op=matrix) is a different action — it must not
    # resolve the login's step. "connect codex → what else is connected? →
    # done" is that thread's shape.
    t_list = {"created_at": T0 + timedelta(minutes=2), "tool_calls": [
        chip("harness_connections", {"op": "list"}, {"status": "ok", "result": {"providers": []}})]}
    blk_a = tt.render_tool_trail([t_login, t_list])
    check("render: a later provider-less call on the same tool does NOT resolve the login's pending step",
          blk_a.count("PENDING next step:") == 1 and "Give the user BOTH" in blk_a
          and "harness_connections op=list" in blk_a, blk_a)
    # …but a later call that names the same provider (any op) does.
    blk_a2 = tt.render_tool_trail([t_login, t_status_wait, t_list])
    check("render: a later same-provider call still resolves it even with an unrelated call after",
          "Give the user BOTH" not in blk_a2 and blk_a2.count("PENDING next step:") == 1, blk_a2)

    # The call clip must keep the NEWEST
    # calls and never drop a pending one — a 100-tool turn is normal here.
    many = {"created_at": T0 - timedelta(minutes=5), "tool_calls": [
        chip("recall", {"query": f"q{i}"}, {"status": "ok", "result": "…"}) for i in range(tt.TOOL_TRAIL_MAX_CALLS + 1)]}
    blk_b = tt.render_tool_trail([many, t_login])
    check("render: with more calls than the clip, the newest (the login) is shown and its step is PENDING; "
          "the oldest are elided with a count",
          "harness_connections op=login provider=codex" in blk_b and "PENDING next step:" in blk_b
          and "earlier call" in blk_b and blk_b.count("\n- [") == tt.TOOL_TRAIL_MAX_CALLS, blk_b)
    many_after = {"created_at": T0 + timedelta(minutes=1), "tool_calls": [
        chip("recall", {"query": f"q{i}"}, {"status": "ok", "result": "…"}) for i in range(tt.TOOL_TRAIL_MAX_CALLS + 5)]}
    blk_b2 = tt.render_tool_trail([t_login, many_after])
    check("render: a pending step older than the clip window is still shown (pending is never clipped)",
          "harness_connections op=login provider=codex" in blk_b2 and "PENDING next step:" in blk_b2
          and blk_b2.count("\n- [") == tt.TOOL_TRAIL_MAX_CALLS, blk_b2)

    # A failed call's next_step is advice, not a promise → not pending.
    t_err = {"created_at": T0, "tool_calls": [
        chip("harness_connections", {"op": "login", "provider": "codex"}, err_result, status="error")]}
    blk5 = tt.render_tool_trail([t_err])
    check("render: a failed call shows its error and carries no PENDING step",
          "→ error" in blk5 and "could not start codex" in blk5
          and "PENDING next step:" not in blk5, blk5)

    # A chip with only a 'done' step (login_code done) → listed, nothing pending.
    check("render: empty / no-call input renders nothing",
          tt.render_tool_trail([]) == "" and tt.render_tool_trail([{"created_at": T0, "tool_calls": []}]) == ""
          and tt.render_tool_trail(None) == "")

    # Rows straight from the DB shape (metadata JSON string / dict).
    rows = [{"created_at": T0.isoformat(), "metadata": '{"role":"assistant","tool_calls":[{"tool":"recall","status":"ok"}]}'},
            {"created_at": T0, "metadata": {"role": "assistant"}},
            {"created_at": T0, "tool_calls": [{"tool": "build", "status": "ok", "digest": {"task_id": 7}}]}]
    turns = tt.trail_from_rows(rows)
    check("trail_from_rows: JSON-string metadata, dict metadata without calls (dropped), direct tool_calls",
          len(turns) == 2 and turns[0]["tool_calls"][0]["tool"] == "recall"
          and turns[1]["tool_calls"][0]["digest"]["task_id"] == 7, repr(turns))
    check("render: ISO-string created_at renders a clock time",
          "[22:11 UTC]" in tt.render_tool_trail(turns))

    # ── 5. wiring (source-tree) ────────────────────────────────────────────
    sess = (ROOT / "brain/mastercode/runtime/session.py").read_text(encoding="utf-8")
    me = (ROOT / "brain/mastercode/memory_engine.py").read_text(encoding="utf-8")
    check("session.py folds digest_tool_action into ta_entry",
          "digest_tool_action(func_name, func_args, tool_result)" in sess)
    check("session.py persists op/operate_tool/read_tool/provider/harness/next_step/digest on the chip",
          '("op", "operate_tool", "read_tool", "provider", "harness", "next_step", "digest")' in sess)
    check("session.py builds the block from recall_tool_trail + render_tool_trail after history, "
          "skips replay (history_before), and logs it as tool_trail",
          "recall_tool_trail(" in sess and "_tool_trail_block = render_tool_trail(" in sess
          and 'log_injected_block(self, sess, "tool_trail", _tool_trail_block)' in sess
          and sess.index("grok_messages.extend(history_msgs)") < sess.index("_tool_trail_block = render_tool_trail(")
          < sess.index('grok_messages.append({"role": "system", "content": _dyn_sys})'))
    check("session.py counts the block in the context ledger",
          '("tool_trail", _loc.get("_tool_trail_block"))' in sess)
    check("memory_engine.recall_tool_trail exists, uses the shared scope clause, assistant rows with calls only",
          "async def recall_tool_trail(" in me
          and me.index("async def recall_tool_trail(") < me.index("metadata->>'role' = 'assistant'")
          < me.index("metadata->'tool_calls' <> '[]'::jsonb"))

    print(f"[tool-trail] {'PASS (0 failing)' if not FAILS else f'FAIL ({FAILS} failing)'}")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
