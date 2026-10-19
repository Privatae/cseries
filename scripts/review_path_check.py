#!/usr/bin/env python3
"""
review_path_check.py — functional end-to-end check of the multi-harness
coding spine, driven against the LIVE running kernel exactly the way a
real external caller (Codespace UI / skill loopback) hits it.

Why HTTP and not in-process imports
-----------------------------------
The sidecar Supervisor is a singleton created ONLY inside the uvicorn
process (main.py: set_supervisor(Supervisor()) during lifespan). A
separate `python3` process — e.g. one we kubectl-exec in — has no
supervisor, so review.py's get_supervisor() returns None and every
harness dispatch dies with "503 supervisor unavailable". That is a
checker artifact, not the real path.

So this check talks to the RUNNING server on [::1]:8800 and uses the
INTERNAL_API_SECRET loopback header (middleware.py grants full
permission for x-cseries-internal-auth) — the same door the MCP factory
tools use. It must run INSIDE the entity pod/container (where :8800 and
the secret live). The background fan_out task then runs in the real
uvicorn process with the real supervisor.

Why this exists
---------------
smoke_test.sh proves the /api/review/* routes are *registered* and the
tool *names* exist; it never runs the path. This check runs it, and
turns the SILENT failure sites in the orchestration core into LOUD red
lines:

  * review.py:346  detached create_task runner has no done-callback —
                   if it dies the session sits "running" forever and the
                   poll endpoint never shows an error. → HOP 2 fails on
                   timeout.
  * review.py:158-164  git-diff discards stderr / ignores return code,
                   so a broken git step looks like "0 changes." → HOP 3
                   fails when every harness comes back empty.
  * review.py:166-175  ok:true + diff_bytes:0 is ambiguous. → HOP 3 WARN.
  * review.py:585,601-603  compare falls back to the first candidate on
                   LLM error/garbage, returning ok:true. → HOP 4 WARN.

Exit 0 = every hard hop green; non-zero = a hop failed. WARNs print
yellow but do not fail the check.
"""

import asyncio
import json
import os
import sys
import time
import uuid

import aiohttp

BASE = os.environ.get("CSERIES_KERNEL_URL", "http://[::1]:8800")
SECRET = os.environ.get("INTERNAL_API_SECRET", "")
POLL_TIMEOUT_S = int(os.environ.get("CHECK_TIMEOUT_S", "480"))
POLL_INTERVAL_S = 3
MAX_MINUTES = int(os.environ.get("CHECK_MAX_MINUTES", "5"))

# Trivial task — small batch on purpose: cheap on the user's linked
# subscription quota, and a one-line file makes the diff assertion
# unambiguous. Every harness is told to create the same file so compare
# has competing candidates for one path.
TASK_PROMPT = (
    "Create a single new file named hello.py containing exactly one line:\n"
    'print("hello from cseries multi-harness check")\n'
    "Do not create any other files. Do not explain."
)


def red(s):    print(f"\033[31m{s}\033[0m", flush=True)
def green(s):  print(f"\033[32m{s}\033[0m", flush=True)
def yellow(s): print(f"\033[33m{s}\033[0m", flush=True)
def plain(s):  print(s, flush=True)


async def main() -> int:
    if not SECRET:
        red("FAIL  INTERNAL_API_SECRET not in env — run this INSIDE the entity "
            "pod/container (the loopback door needs it).")
        return 1

    hdr = {"x-cseries-internal-auth": SECRET, "Content-Type": "application/json"}
    failed = 0
    warned = 0
    plain("── multi-harness review path check (live kernel) ──")

    async with aiohttp.ClientSession(headers=hdr) as s:
        # Pre-flight: server reachable.
        try:
            async with s.get(f"{BASE}/", timeout=aiohttp.ClientTimeout(total=8)) as r:
                if r.status >= 500:
                    red(f"FAIL  pre-flight — kernel {BASE} returned {r.status}")
                    return 1
        except Exception as e:
            red(f"FAIL  pre-flight — kernel {BASE} unreachable: {e}")
            return 1
        green(f"OK    pre-flight — kernel reachable at {BASE}")

        work = f"/tmp/review-check-{uuid.uuid4().hex[:8]}"

        # ── HOP 1: fan_out returns a session + fires the background task ─
        try:
            async with s.post(
                f"{BASE}/api/review/fan_out",
                data=json.dumps({"prompt": TASK_PROMPT, "project_dir": work,
                                 "harnesses": None, "max_minutes": MAX_MINUTES}),
                timeout=aiohttp.ClientTimeout(total=30),
            ) as r:
                body = await r.json()
                if r.status == 409:
                    red(f"FAIL  HOP 1 — no harnesses installed: {body.get('detail')}")
                    return 1
                if r.status != 200:
                    red(f"FAIL  HOP 1 fan_out → HTTP {r.status}: {body}")
                    return 1
        except Exception as e:
            red(f"FAIL  HOP 1 fan_out raised: {e}")
            return 1
        session_id = body.get("session_id")
        dispatched = body.get("harnesses_dispatched") or []
        if not session_id or not dispatched:
            red(f"FAIL  HOP 1 fan_out returned no session/harnesses: {body}")
            return 1
        green(f"OK    HOP 1 fan_out — session {session_id[:12]} → {dispatched}")
        if "grok" not in dispatched:
            yellow("WARN  grok is not in the fan_out set (not connected, or not in "
                   "HARNESS_APP_IDS). A 'done' grok build does "
                   "NOT join multi-harness review until it's wired in.")
            warned += 1

        # ── HOP 2: background task actually COMPLETES (dead-task site) ──
        t0 = time.time()
        deadline = time.time() + POLL_TIMEOUT_S
        sess = None
        while time.time() < deadline:
            async with s.get(f"{BASE}/api/review/sessions/{session_id}",
                             timeout=aiohttp.ClientTimeout(total=15)) as r:
                sess = await r.json()
            if sess.get("manifest", {}).get("completed_at"):
                break
            await asyncio.sleep(POLL_INTERVAL_S)
        else:
            per = (sess or {}).get("per_harness", {})
            red(f"FAIL  HOP 2 background task never completed in {POLL_TIMEOUT_S}s — "
                f"stuck at {[(h, r.get('state')) for h, r in per.items()]}. This is "
                "the silent dead-background-task site (review.py:346).")
            return 1
        green(f"OK    HOP 2 background completed in {round(time.time() - t0)}s")

        # ── HOP 3: per-harness terminal state + diff sanity ─────────────
        per = sess.get("per_harness", {})
        results = sess.get("manifest", {}).get("results", {})
        produced = 0
        for h in dispatched:
            row = per.get(h, {})
            rb = results.get(h, {})
            state = row.get("state")
            nbytes = row.get("diff_bytes", rb.get("diff_bytes"))
            if state not in ("done", "failed"):
                red(f"FAIL  HOP 3 [{h}] non-terminal state '{state}' after completion.")
                failed = 1
                continue
            if state == "failed":
                # The reason can live in several places. manifest.results
                # carries the dispatch 'error' (HTTP-layer failures). The
                # harness's OWN error (e.g. "codex not connected") is
                # currently DROPPED by review.py:166-175 (Bug B) — when
                # everything below is empty, that drop is why.
                reason = (rb.get("error") or row.get("error")
                          or str(rb.get("stderr_tail") or "")[:200]
                          or "(no reason captured — review.py:166-175 drops the "
                             "harness's own error; see Bug B)")
                red(f"FAIL  HOP 3 [{h}] failed — exit={rb.get('exit_code')} err={reason}")
                failed = 1
                continue
            if not nbytes:
                yellow(f"WARN  HOP 3 [{h}] state=done but diff_bytes=0 — 'made no "
                       "changes' is indistinguishable from a swallowed git-diff "
                       "error (review.py:158-175). Inspect.")
                warned += 1
            else:
                produced += 1
                green(f"OK    HOP 3 [{h}] done — {nbytes} diff bytes, "
                      f"model={rb.get('model')}, {rb.get('elapsed_seconds')}s")
        if produced == 0:
            red("FAIL  HOP 3 — NO harness produced any diff. Either every harness "
                "no-opped or the git-diff step is silently failing for all of them "
                "(review.py:158-164). Path cannot be verified.")
            return 1

        # ── HOP 4: compare shows the candidates (no model pick) ─────
        try:
            async with s.post(f"{BASE}/api/review/compare",
                              data=json.dumps({"session_id": session_id}),
                              timeout=aiohttp.ClientTimeout(total=180)) as r:
                cmp = await r.json()
                if r.status != 200:
                    red(f"FAIL  HOP 4 compare → HTTP {r.status}: {cmp}")
                    return 1
        except Exception as e:
            red(f"FAIL  HOP 4 compare raised: {e}")
            return 1
        # compare SHOWS candidates since 2026-09-22 (no model picks). This
        # check stands in for the user: for each file, the first variant
        # that touched it — mechanical, never a ranking.
        cands = cmp.get("candidates") or []
        files = cmp.get("files") or {}
        if not cands:
            red(f"FAIL  HOP 4 compare returned no candidates despite {produced} diff(s): {cmp}")
            return 1
        if "picks" in cmp:
            red(f"FAIL  HOP 4 compare returned a pick — a model must never choose: {cmp}")
            return 1
        picks = {path: vids[0] for path, vids in files.items() if vids}
        if not picks:
            red(f"FAIL  HOP 4 compare listed no touched files despite {produced} diff(s): {cmp}")
            return 1
        green(f"OK    HOP 4 compare — {len(cands)} candidate(s); stand-in picks={picks}")

        # ── HOP 5: propose_commit stages the picks (no auto-commit) ─────
        try:
            async with s.post(f"{BASE}/api/review/propose_commit",
                              data=json.dumps({"session_id": session_id, "picks": picks}),
                              timeout=aiohttp.ClientTimeout(total=60)) as r:
                pc = await r.json()
                if r.status != 200:
                    red(f"FAIL  HOP 5 propose_commit → HTTP {r.status}: {pc}")
                    return 1
        except Exception as e:
            red(f"FAIL  HOP 5 propose_commit raised: {e}")
            return 1
        applied = pc.get("applied", [])
        pc_failed = pc.get("failed", [])
        staged = pc.get("staged_diff", "")
        if pc_failed:
            red(f"FAIL  HOP 5 propose_commit could not apply: {pc_failed}")
            failed = 1
        if not applied:
            red(f"FAIL  HOP 5 propose_commit staged nothing despite picks {picks}: {pc}")
            return 1
        if not str(staged).strip():
            yellow("WARN  HOP 5 applied files but staged_diff is empty — preview "
                   "would show the user nothing.")
            warned += 1
        green(f"OK    HOP 5 propose_commit — staged {applied} ({len(str(staged))} diff bytes)")

    plain("")
    if failed:
        red(f"REVIEW PATH CHECK FAILED — {warned} warning(s). Do not push.")
        return 1
    if warned:
        yellow(f"REVIEW PATH CHECK PASSED with {warned} warning(s) — review the "
               "WARN lines above (silent-failure-prone spots).")
    else:
        green("REVIEW PATH CHECK PASSED — every hop green, no silent spots tripped.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
