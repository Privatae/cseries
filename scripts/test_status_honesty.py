#!/usr/bin/env python3
"""Ratchet: status must never lie.

Two invariants, each of which regressed silently before:

1. GUARDED TERMINAL WRITES — every `UPDATE entity_tasks SET status='<terminal>'`
   in agent_dispatch.py must carry a status guard in its WHERE clause. An
   unguarded completion write let a monitor-timed-out agent flip back to
   'completed' AFTER its failure was delivered ("timed out but says
   completed"). Checked lexically over the source so a new write can't ship
   without the guard.

2. TERMINAL ROLLUP — review's session rollup must land on a terminal status
   once a session is over (manifest stamped or budget fully elapsed), even
   when per-harness rows are stuck on running/queued (crash, cancel, kernel
   restart). A "running" row used to hold the rollup — and the SSE stream —
   open forever. Checked behaviorally against the real functions.

Runs in-tree (like test_native_bundles_fresh.py). Exit 0 = both hold.
"""
from __future__ import annotations

import re
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
failures: list[str] = []


# ── 1. Guarded terminal writes in agent_dispatch.py ─────────────────────────
src = (REPO / "brain/mastercode/runtime/agent_dispatch.py").read_text()
# Collect each execute(...) SQL string that sets a terminal status. The SQL is
# built from adjacent string literals; concatenate quoted fragments per
# statement by scanning execute blocks.
stmt_re = re.compile(r"execute\(\s*((?:\"(?:[^\"\\]|\\.)*\"\s*)+)", re.S)
term_re = re.compile(r"SET\s+status\s*=\s*'(completed|failed|cancelled)'", re.I)
checked = 0
for m in stmt_re.finditer(src):
    sql = "".join(re.findall(r"\"((?:[^\"\\]|\\.)*)\"", m.group(1)))
    if not term_re.search(sql):
        continue
    checked += 1
    if not re.search(r"WHERE.*\bstatus\b", sql, re.I | re.S):
        failures.append(
            "agent_dispatch.py: terminal status write WITHOUT a status guard "
            f"in WHERE — {sql[:120]!r}…")
if checked < 5:
    failures.append(
        f"agent_dispatch.py: only {checked} terminal writes found — the "
        "lexical scan no longer matches the source; update this ratchet, "
        "don't delete it.")

# ── 2. Terminal rollup once the session is over ──────────────────────────────
sys.path.insert(0, str(REPO))
try:
    from server.routes.review import _rollup_session_status, _session_is_over

    stuck = {"a": {"state": "running"}, "b": {"state": "done"}}
    if _rollup_session_status(stuck, session_over=False) != "running":
        failures.append("rollup: live session with a running row must be 'running'")
    if _rollup_session_status(stuck, session_over=True) in ("running", "queued"):
        failures.append(
            "rollup: session_over=True with a stuck running row must be "
            "TERMINAL (it reported a live status — the eternal-spinner bug)")
    all_stuck = {"a": {"state": "running"}, "b": {"state": "queued"}}
    if _rollup_session_status(all_stuck, session_over=True) != "failed":
        failures.append("rollup: session_over with only stuck rows must be 'failed'")

    if not _session_is_over({"completed_at": 123.0}):
        failures.append("_session_is_over: a stamped manifest must be over")
    if _session_is_over({"started_at": time.time(), "max_minutes": 10}):
        failures.append("_session_is_over: a fresh in-budget run must NOT be over")
    # The restart safety net is SILENCE: a
    # session nothing has written to for longer than any live run can stay
    # silent is over. Elapsed time alone never is (a working build ran past
    # max_minutes × variants + 15 min and was shown as failed).
    if not _session_is_over(
            {"started_at": time.time() - 3 * 3600, "max_minutes": 10}):
        failures.append(
            "_session_is_over: a session silent past every live window must be "
            "over (the restart safety net)")
except Exception as e:  # import failure = the ratchet can't run; that's a FAIL
    failures.append(f"rollup checks could not run: {e!r}")

# ── 3. Caps ratchet: liveness over wall clocks ───────────────────────────────
# (a) The dispatched-agent stuck scan must key on the heartbeat, not age-since-start —
#     the age-only scan guillotined every legitimately-long agent at 10 min.
if "last_heartbeat_at" not in src.split("task_type='agent' AND status='in_progress'", 1)[-1][:600]:
    failures.append(
        "agent_dispatch.py: the agent stuck scan no longer references "
        "last_heartbeat_at — wall-clock kills of live agents are back")
# (b) The build timeout ceiling must stay 4h — the 30-min guillotine
#     force-failed real builds and stranded their output.
tools_src = (REPO / "brain/mastercode/runtime/tools.py").read_text()
# `timeout` bounds one hung model call, not
# the build, and the bound is named once (BUILD_MODEL_WAIT_MAX_S).
if ("BUILD_MODEL_WAIT_MAX_S = 14400" not in tools_src
        or tools_src.count('min(int(a.get("timeout") or BUILD_MODEL_WAIT_S),') < 2):
    failures.append(
        "tools.py: build timeout ceiling shrank below 14400s (found "
        f"{tools_src.count('14400')} refs) — the 30-min build guillotine is back")
# (c) The chat agent-card poll must have NO tick cap — the 120-tick (~6 min)
#     cap froze "Running" badges forever. Both frontends carry the card.
for tc in ("workbench-web/src/components/chat/tool-card.tsx",
           "circles-web/src/components/chat/tool-card.tsx"):
    tsrc = (REPO / tc).read_text()
    if re.search(r"if\s*\(\+\+ticks\s*<\s*\d+\)\s*timer", tsrc):
        failures.append(f"{tc}: the agent status poll has a tick CAP again — "
                        "it will silently stop watching and freeze the badge")

# ── 4. Restart-safety ratchet: no orphans survive boot ──────────────────────
# (a) Boot reaper: in-flight agent/build rows are process-local state — a
#     fresh process must fail+deliver them immediately, not hours later.
if "async def reap_orphans_on_boot" not in src:
    failures.append("agent_dispatch.py: reap_orphans_on_boot is gone — a "
                    "restart mid-build shows a live card + locks the project for hours")
if "reap_orphans_on_boot()" not in (REPO / "server/brain_service.py").read_text():
    failures.append("brain_service.py: boot orphan reap is no longer called at startup")
# (a2) The build's OTHER record. Failing the entity_tasks row while leaving the
#      session's per_harness_state.json on 'running' is not restart-safety, it
#      is two records disagreeing — and the bench reads the one that said the
#      run was alive (one build was still "running"
#      37 min after a roll recreated the pod). Both halves, or neither.
review_src = (REPO / "server/routes/review.py").read_text()
if "def reap_orphaned_sessions_on_boot" not in review_src:
    failures.append("review.py: reap_orphaned_sessions_on_boot is gone — a roll "
                    "mid-build leaves per_harness_state.json reading 'running' forever")
if "reap_orphaned_sessions_on_boot()" not in src:
    failures.append("agent_dispatch.py: the boot reap no longer sweeps review "
                    "sessions — the task row gets failed but the session file does not")
# The two reapers must tell the user ONE story; a diverging string is how a
# single restart reads as two unrelated faults.
_REAP_MSG = "kernel restarted mid-run — the running task did not survive"
for _f, _s in (("agent_dispatch.py", src), ("review.py", review_src)):
    if _REAP_MSG not in _s:
        failures.append(f"{_f}: the orphan-reap message drifted — both reapers "
                        f"must say {_REAP_MSG!r} so one restart reads as one fault")
# (b) Paused embed jobs must requeue at boot — their resume timer is in-memory.
embed_src = (REPO / "server/routes/embed.py").read_text()
if embed_src.count("status IN ('running','paused')") < 2:
    failures.append("embed.py: _reset_orphans no longer requeues 'paused' jobs — "
                    "a restart during the pause window orphans the job forever")
# (c) Ladder climbs: strong task refs + orphan reap on read.
ladder_src = (REPO / "server/routes/ladder.py").read_text()
if "_CLIMB_TASKS" not in ladder_src:
    failures.append("ladder.py: climb tasks have no strong refs — GC can kill a "
                    "climb mid-flight and freeze the run at 'running'")
if "_reap_if_orphaned" not in ladder_src:
    failures.append("ladder.py: orphaned 'running' runs are no longer reaped at read")
if ladder_src.count("create_task(_climb") > 1:
    failures.append("ladder.py: a climb is spawned outside _spawn_climb — "
                    "unregistered/unreferenced climb tasks are back")
# (d) The harness pod release must sit in a shielded finally — a cancelled run
#     used to orphan the pod on metered compute.
review_src = (REPO / "server/routes/review.py").read_text()
# The single build releases through the holder count
# (_release_harness_when_last — the LAST lane out of a shared pod sends the
# one release); the call is the same shielded await in the same finally.
if 'asyncio.shield(_release_harness_when_last(harness_id))' not in review_src:
    failures.append("review.py: harness.release left the shielded finally — "
                    "cancelled runs orphan their pod again")

# ── 5. Full-read ratchet: the host can READ uploads ──────────────────────────
# context_ingest must expose file-level read ops AND they must classify as
# read-safe against the real schema — otherwise the host's read-safe gate
# denies them and the host is back to "could only read metadata".
try:
    sys.path.insert(0, str(REPO / "brain"))
    from mastercode.public_tool_registry import PUBLIC_TOOL_SCHEMAS
    from mastercode.runtime.tool_access import read_safe_ops
    ci_ops = read_safe_ops("context_ingest", PUBLIC_TOOL_SCHEMAS["context_ingest"])
    missing = {"read", "files", "search", "list"} - ci_ops
    if missing:
        failures.append(
            f"context_ingest: ops {sorted(missing)} are not read-safe against "
            "the real schema — the host's read-safe gate will deny them and the host "
            "can't read uploaded documents directly again")
except Exception as e:
    failures.append(f"full-read checks could not run: {e!r}")

# ── 6. Context-corpus honesty: counts true, reads clean ──────────────────────
# (a) Embed coverage/status must exclude binary file-lane rows — counting the
#     never-embeddable made "missing/unembedded" permanently positive.
for _i, _seg in enumerate(embed_src.split("SELECT COUNT(*) AS total,")[1:], 1):
    if "FROM user_context" in _seg[:300] and "NOT_FILE_KIND_SQL" not in _seg[:400]:
        failures.append(
            f"embed.py: user_context coverage count #{_i} lost NOT_FILE_KIND_SQL "
            "— phantom 'missing' counts are back")
cm_src = (REPO / "server/routes/context_manager.py").read_text()
if cm_src.count("NOT_FILE_KIND_SQL") < 2:
    failures.append("context_manager.py: embedding-status counts lost "
                    "NOT_FILE_KIND_SQL — the 'unembedded' lens can never clear again")
# (b) Re-upload must supersede prior raw copies, and the host's document read
#     must scope active rows (never dismissed) — otherwise duplicates stack
#     and a filename read interleaves copies.
ctx_src = (REPO / "server/routes/context.py").read_text()
if "superseded" not in ctx_src:
    failures.append("context.py: re-upload supersede is gone — duplicate copies stack again")
if 'status="active"' not in tools_src:
    failures.append("tools.py: context_ingest read no longer scopes status='active' — "
                    "dismissed/superseded copies leak into document reads")

# ── 7. Host-knowledge ratchet: the briefing
#     matches the backend. Federated recall REPLACED the three-store
#     ladder ("recall NEVER sees uploaded documents") with ONE search verb,
#     by decided design — the briefing must now teach THAT doctrine plus the
#     honesty rules that survive it: unavailable is never empty, and a
#     missing document is a conversation with the user, never an agent hunt.
#
#     Brief prompt: the verb rules live ONCE, in the verb
#     schemas the Host holds on every turn (_orchestration_verbs); the
#     orchestrator briefing no longer restates them. So this checks the
#     doctrine where it now lives, that the briefing does not carry a second
#     copy, and the behaviour the rule promised: an unavailable store is
#     named in recall's answer, never rendered as empty.
session_src = (REPO / "brain/mastercode/runtime/session.py").read_text()
try:
    from mastercode.runtime.session import _build_orchestrator_prompt, _orchestration_verbs
    _verbs = {(t.get("function") or {}).get("name"): (t.get("function") or {}).get("description") or ""
              for t in _orchestration_verbs()}
    _recall = _verbs.get("recall") or ""
    if not ("everything you hold" in _recall and "documents" in _recall and "memory" in _recall):
        failures.append("session.py: the recall verb no longer says it is ONE search over every "
                        "store (memory and documents included) — hosts will flail across stores again")
    if "never shown as empty" not in _recall:
        failures.append("session.py: the recall verb lost the unavailable-is-not-empty rule")
    _dispatch = _verbs.get("dispatch") or ""
    if not ("you do not own" in _dispatch and "your data" in _dispatch and "`read`" in _dispatch):
        failures.append("session.py: the dispatch verb no longer says the Host reads its own data "
                        "itself and dispatches only for content it does not own — agent hunts "
                        "for the user's own documents are back")
    _brief = _build_orchestrator_prompt([])
    for _rule in ("never shown as empty", "never treat unavailable as empty", "you do not own"):
        if _rule in _brief:
            failures.append(f"session.py: the orchestrator briefing restates a verb rule "
                            f"({_rule!r}); the verb schema is its one copy")
    # The behaviour: recall's text names an unavailable store, never "no matches".
    from mastercode.memory_retrieval import format_unified_text
    # Checked per store, on the section that carries the store's reason: two
    # stores render through format_recall_text and two through
    # format_unified_text's own loop, and a count over the whole answer let
    # one of them read "## Projects\nno matches (<reason>)" and still pass.
    _down = {"memory": "embedder down (gate)",
             "documents": "documents index down (gate)",
             "projects": "projects table missing (gate)",
             "code": "code index down (gate)"}
    _txt = format_unified_text({
        "query": "q", "stores": ["memory", "documents", "projects", "code", "skills"],
        **{_s: {"unavailable": _why} for _s, _why in _down.items()},
        "skills": {"results": [], "searched": 3},
        "unavailable": list(_down)})
    _secs = _txt.split("\n\n")
    for _s, _why in _down.items():
        _sec = next((_x for _x in _secs if _why in _x), "")
        if not _sec:
            failures.append(f"memory_retrieval.format_unified_text: the {_s} store's "
                            f"reason ({_why!r}) is not in recall's answer")
        elif not ("UNAVAILABLE" in _sec.split("\n", 1)[0] and "did NOT run" in _sec
                  and "no matches" not in _sec.split("do not treat")[0]):
            failures.append(f"memory_retrieval.format_unified_text: the unavailable {_s} "
                            f"store reads as empty, not UNAVAILABLE / did NOT run: {_sec!r}")
    if "## Skills\nno matches" not in _txt:
        failures.append("memory_retrieval.format_unified_text: a store that ran and matched "
                        "nothing is no longer told apart (the control)")
except Exception as e:  # noqa: BLE001 — a check that cannot run is a failure
    failures.append(f"host-knowledge checks could not run: {e!r}")
# Every store unavailable: the verb refuses as UNAVAILABLE, never answers empty.
_recall_branch = session_src.split('elif func_name == "recall":', 1)[-1][:4000]
if not ("raise RecallUnavailable" in _recall_branch
        and "do not treat this as" in _recall_branch):
    failures.append("session.py: recall no longer refuses as UNAVAILABLE when every store "
                    "failed — an outage would read as 'no matches'")
pb_src = (REPO / "brain/mastercode/runtime/prompt_builder.py").read_text()
if "bp.get(\"timezone\")" not in pb_src:
    failures.append("prompt_builder.py: user-timezone rendering is gone — the "
                    "host is back to raw UTC ('an hour ago' = 'last night')")

if failures:
    print("FAILED: status honesty ratchet")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
print(f"PASSED: {checked} guarded terminal writes; rollup lands terminal when the session is over")
