"""Facts a delivered result is checked against before the Host reports it.

Moved out of runtime/session.py, which re-exports every name defined here."""

import json


def _describe_failed_tools(actions: list) -> str:
    """`name(status): reason` for each failed tool in a round, for the log.

    The all-tools-failed warning used to print a COUNT and nothing else. A
    Host once logged three of those rounds and which tool failed was
    unrecoverable after the fact: there was no tool-call table in the entity DB
    then (only inference_cost_log; tool_call_log arrived with v157) and the
    pod log carries no denial line, so the only record of
    the name was this line — which did not print it.

    Reasons are clipped hard: this is a log line, and a tool that fails with a
    multi-KB payload must not push the rest of the round out of the buffer."""
    if not actions:
        return "(no tool actions recorded)"
    bits = []
    for ta in actions:
        if not isinstance(ta, dict):
            continue
        name = str(ta.get("tool") or "?")
        status = str(ta.get("result_status") or "?")
        reason = " ".join(str(ta.get("error_detail") or "").split())[:160]
        bits.append(f"{name}({status})" + (f": {reason}" if reason else ""))
    return "; ".join(bits) or "(no tool actions recorded)"


def _inject_context_for(entity: dict) -> bool:
    """The PER_TURN memory block's gate (prompt_schema Block("memory", …,
    inject_context)): on when the entity's response_style is
    'conversational'. ONE derivation for every EntitySession constructor —
    start_session computed it, but the transient chat-path session (the
    no-sessions architecture) and the DB rehydration left the
    dataclass default False, so the "What You Remember / Relevant Right Now"
    block — the whole per-turn pre-fire recall — never rendered on a chat
    turn. Measured: for months no prompt_log row carried
    a `memory` block; every turn ran without it and the
    model fell back to calling `recall` itself (and quoting month-old rows
    as current)."""
    bp = (entity or {}).get("behavioral_params") or {}
    if isinstance(bp, str):
        try:
            bp = json.loads(bp)
        except (json.JSONDecodeError, TypeError):
            bp = {}
    if not isinstance(bp, dict):
        bp = {}
    return bp.get("response_style", "") == "conversational"


# ---------------------------------------------------------------
# Entity Runtime
# ---------------------------------------------------------------


def _desktop_render_facts(record: dict) -> str:
    """The browser render check of a landed desktop design, as a fact line —
    phase builds and one-shot runs alike (desktop_render.landed_tree_verdict,
    behind the browser-proof switch). "" when no such check ran."""
    v = record.get("desktop_render")
    if not isinstance(v, dict):
        return ""
    results = [r for r in (v.get("results") or []) if isinstance(r, dict)]
    if not v.get("ran"):
        return ("Desktop render check: NOT CHECKED — the landed desktop design could not "
                f"be loaded in a browser ({v.get('reason') or 'no reason given'}). Say so; "
                "do not call it working.\n")
    bad = [r for r in results if not r.get("ok")]
    if not bad:
        names = ", ".join(str(r.get("name")) for r in results)
        return f"Desktop render check: passed — {names} rendered in a browser.\n"
    lines = "".join(f"  {r.get('name')}: {str(r.get('output_tail') or '').strip()}\n"
                    for r in bad)
    return ("Desktop render check: FAILED — this desktop design does not render in a "
            "browser, so it will be refused if it is set as the desktop or published. "
            "Fix the line named here before offering it:\n" + lines)


def _acceptance_facts(record: dict) -> str:
    """The checks line of a build delivery: what the checks the phase or run
    listed reported, read from the pod's verdict on the registry row — never
    from the harness's words. Information only: a build's outcome is whether
    it landed. "" when nothing was listed."""
    if (record.get("phase_status") or "").strip() == "split_conflict":
        # Parallel phases: the run built, its land was refused because
        # it collided with a parallel phase's work. The reason and the
        # re-split ride chain_stopped below.
        return ("Nothing from this phase landed: its changes collided with work "
                "another phase landed while it was building. That is the plan's "
                "split, not a coding failure; the phase is back to ready and was "
                "not retried.\n")
    acc = record.get("acceptance") if isinstance(record.get("acceptance"), dict) else {}
    results = acc.get("results") or []
    if not results:
        if record.get("acceptance_declared") and acc.get("reason"):
            return (f"Checks listed for this build did not run "
                    f"({acc['reason']}). They are information only.\n")
        return ""
    n_ok = sum(1 for r in results if r.get("ok"))
    lines = [f"Checks listed for this build (information only — the outcome is "
             f"whether it landed): {n_ok} of {len(results)} passed."]
    for r in results:
        if not r.get("ok"):
            lines.append(f"  - did not pass: `{r.get('cmd')}` exited {r.get('exit')} "
                         f"(expected {r.get('expect_exit', 0)})")
    return "\n".join(lines) + "\n"


def _verify_delivery(record: dict) -> tuple[str, str, str]:
    """The delivered turn for an app_verify row (mastercode.runtime.app_verify):
    (content, toast_summary, toast_level). DETERMINISTIC — no
    phrasing model call. Every sentence here is a typed field of the proof
    envelope read back; there is no fact to phrase, only facts to state. The
    origin case is the host telling the user to "open the live app" three
    times over an app that had never started."""
    app_id = record.get("app_id") or "the app"
    v = record.get("verify") if isinstance(record.get("verify"), dict) else {}
    rep = record.get("repair") if isinstance(record.get("repair"), dict) else {}
    ok = (record.get("status") == "completed") and v.get("verdict") == "delivered"
    ver = f" v{v['version']}" if v.get("version") else ""
    if ok:
        life = v.get("lifecycle")
        how = ("It is resident, so it keeps running with no window open — the "
               "switch in the App Store stops it."
               if life == "resident" else
               "Open it from the desktop; it starts when you do."
               if v.get("kind") == "process" else
               "Open it from the desktop.")
        content = (f"Delivered: {app_id}{ver} is installed and PROVEN running — "
                   f"the platform started it and its own ready check answered ok with "
                   f"a clean log ({v.get('reason')}, {v.get('elapsed_s') or 0}s). {how}")
        return content, f"Delivered: {app_id}", "success"
    reason = v.get("reason") or record.get("error") or "not proven"
    if v.get("verdict") == "not checked":
        # The platform never woke the app's pod, so whether it runs was not
        # measured. Not a failure of the app — and no repair round was spent.
        return (f"Not checked: {app_id}{ver} was installed, but the platform did not "
                f"bring it up, so whether it runs was not measured — {reason}. This is "
                "not a failure of the app and no repair round was spent.",
                f"Not checked: {app_id}", "warning")
    errs = [str(x) for x in (v.get("log_errors") or [])][-3:]
    ex = v.get("exit") if isinstance(v.get("exit"), dict) else None
    lines = [f"Not delivered: {app_id}{ver} was installed and started, but did not "
             f"come up — {reason}."]
    if ex and ex.get("code") not in (None, 0):
        lines.append(f"The process exited with code {ex.get('code')}.")
    if errs:
        lines.append("Last error lines the app printed:\n" + "\n".join(f"  {e}" for e in errs))
    if rep.get("start"):
        from mastercode.runtime.app_verify import MAX_AUTO_REPAIRS as _max
        lines.append(f"I have started repair round {rep.get('round')} of "
                     f"{record.get('max_repairs') or _max} on the "
                     f"'{rep.get('harness')}' engine, seeded with that log; when it "
                     "lands I re-ship and prove it again. Nothing is needed from "
                     "you; it goes on by itself.")
    else:
        why = rep.get("why") or "no automatic repair was started"
        lines.append(f"No automatic repair: {why}.")
        if v.get("seed"):
            # "seed" stays in the words: it is where the host finds the brief
            # (the task's verify.seed).
            # No "say the word": whether to spend one more repair
            # build is the person's call, asked in the question box by the Host
            # it is handed to (deliver_agent_result).
            lines.append("I have a repair brief written from that log, saved with "
                         "this task as its repair seed.")
    return "\n\n".join(lines), f"Not delivered: {app_id}", "error"


def _auto_deliver_facts(record: dict) -> str:
    """What Auto-Approve (the project's switch; config key auto_merge) did with
    a landed build, stated from the typed auto_merge / auto_ship results
    (tools._run_phase_build) — "" when it is off. A held delivery says why, in
    plain words."""
    m = record.get("auto_merge") if isinstance(record.get("auto_merge"), dict) else None
    sh = record.get("auto_ship") if isinstance(record.get("auto_ship"), dict) else None
    if not m and not sh:
        return ""
    lines = []
    if m and m.get("held"):
        lines.append(f"Auto-Approve was held: {m['held']}.")
    else:
        if m:
            pr = f" Code Approval #{m['pr']}" if m.get("pr") else " the change"
            lines.append(f"Under Auto-Approve the Host merged{pr} into the project's Code Repository."
                         if m.get("merged") else
                         f"Auto-Approve did NOT merge{pr}: {m.get('reason') or 'no reason recorded'}.")
        if sh:
            if sh.get("shipped"):
                app = sh.get("app_id") or "the app"
                if sh.get("verify_task_id") is not None:
                    lines.append(f"Auto-Approve installed {app}. The platform is now proving "
                                 "it runs; that result arrives in this thread as its own message.")
                else:
                    # ship_and_verify can install without starting a proof
                    # (installed id unknown, or start_verify returned none).
                    lines.append(f"Auto-Approve installed {app}, but no proof that it runs was "
                                 f"started ({sh.get('reason') or 'no reason recorded'}).")
            elif sh.get("reason"):
                lines.append(f"Auto-Approve did not install anything: {sh['reason']}.")
    return "\n".join(lines) + "\n"


def _compare_delivery(record: dict) -> tuple[str, str, str]:
    """The delivered turn for a finished compare (a fan_out row that ran
    several variants): (content, toast_summary, toast_level). DETERMINISTIC,
    like _verify_delivery. Every line is a typed field of per_harness read
    back, and the next step names the buttons on screen. The phrasing call
    this replaces told the user to "Run review.compare on this session" and
    to apply the pick "with review.adopt": its prompt named those tools, so the reply did too."""
    per = record.get("per_harness") or {}
    rows, landed = [], []
    for vid, e in per.items():
        e = e or {}
        name = f"**{e.get('model') or vid}** ({vid})" if e.get("model") else f"**{vid}**"
        if e.get("state") == "done":
            landed.append(vid)
            bits = []
            secs = e.get("elapsed_seconds")
            if secs:
                s = float(secs)
                bits.append(f"{s / 60:.0f} min" if s >= 90 else f"{s:.0f} s")
            if e.get("diff_bytes"):
                kb = int(e["diff_bytes"]) / 1024
                bits.append(f"{kb:.0f} KB of changes" if kb >= 1 else "under 1 KB of changes")
            elif e.get("diff_bytes") is not None:
                # A recorded zero: the run exited cleanly and wrote nothing.
                # "finished" alone read as a result (a Host once told the user "opencode: finished, 8 min" for an empty
                # run). None means the size was never recorded — say nothing.
                bits.append("but it changed no files")
            chk = e.get("checks") if isinstance(e.get("checks"), dict) else None
            if chk:
                bits.append({True: "its checks passed",
                             False: "its checks failed"}.get(chk.get("ok"),
                                                             "its checks did not run"))
            rows.append(f"- {name}: finished" + (f", {', '.join(bits)}" if bits else ""))
        else:
            rows.append(f"- {name}: failed. {e.get('error') or 'No error was recorded.'}")
    # The buttons named below exist in the Workbench only (compare-terminals,
    # run-terminal); a compare asked in Circles gets the words without them.
    origin = record.get("origin")
    if isinstance(origin, str):
        try:
            origin = json.loads(origin)
        except ValueError:
            origin = {}
    origin = origin if isinstance(origin, dict) else {}
    in_workbench = origin.get("app_id") == "workbench"
    n, k = len(per), len(landed)
    head = (f"All {n} versions finished." if k == n else
            f"None of the {n} versions finished." if k == 0 else
            f"{k} of {n} versions finished.")
    parts = [head, "\n".join(rows)]
    # Facts only. This text used to end "Tell me which one to
    # use and I'll make it …" — platform text asking for a reply. Which
    # version is used is the person's choice, and it is asked in the question
    # box: the Host is handed this result (deliver_agent_result) and puts the
    # pick there, and lands the answer.
    if k > 1 and in_workbench:
        parts.append("Each one has an **open version** button on its card above.")
    elif k == 1 and in_workbench:
        parts.append("It has an **open version** button on its card above.")
    elif k == 0:
        parts.append("Nothing changed in the project.")
    level = "success" if k == n else "warning" if k else "error"
    return "\n\n".join(parts), f"{head}", level
