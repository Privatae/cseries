#!/usr/bin/env python3
"""Process awareness: the host's view of its own machine must stay honest.

WHAT THIS PROTECTS (built 2026-08-31).

The audit that produced this module found that entity-9128's panel reported four
idle PostgreSQL backends as ~77 MB. Their private pages came to 8 MB. The gap
was not an arithmetic bug — it was RSS, which counts every SHARED page in full
in every process that maps it, and PostgreSQL's backends all map the same 32 MB
`shared_buffers`. Summing that container's RSS gave 152 MB against a cgroup
charged 60 MB.

That is the failure class this gate exists for, and it has a shape worth naming:
a number with no provenance reads as a fact. A host acting on "postgres is
using 77 MB of your 384 MB" would be reasoning from something wrong by 8x, in
the direction that causes action. So the invariants below are all about the
numbers being LABELED and the labels being true.

  1. EVERY process carries `measured`, and a row whose PSS was unreadable must
     say `rss` rather than silently presenting RSS in the pss field.
  2. The totals must expose the naive RSS sum under a name that says what it is
     (`rss_bytes_naive_sum`) and must flag `partial` when any row was missing
     PSS — a partial sum presented as a total is the original bug in a hat.
  3. `idle_for_s` must be None with fewer than two samples. "I don't know yet"
     and "idle for 0 seconds" are different claims, and a rule reading the
     second as the first would call every fresh process busy forever.
  4. The kill surface must refuse the machine's load-bearing processes BY ROLE,
     with a reason. Refusing by pid would be defeated by a restart; refusing
     silently teaches the host nothing about its own machine.
  5. The support report must read no database. That is the whole point of it:
     a host whose DB is the fault being reported still has to be able to file.

WHAT THIS DOES NOT CATCH. It runs the module against THIS machine's /proc, so
it proves the parsing and the labeling, not the entity-pod baseline (those
roles only match in a real pod, where this gate has nothing to assert against).
The kill path is checked for refusal only — this gate never signals anything.

Container-free. Run: APP_ROOT=<repo> python3 scripts/test_process_awareness.py
"""
import asyncio
import os
import sys
import time

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


def _exit_notice_behaviour() -> tuple[bool, bool]:
    """(notice_on_deliberate_stop, notice_on_real_crash) from the real class.

    Local children only — no container, no network, nothing installed.
    """
    import asyncio
    from server.applications import supervisor as S

    async def run(app_id: str, argv: list[str], stop: bool) -> bool:
        sup = S.Supervisor()
        proc = await asyncio.create_subprocess_exec(sys.executable, "-c", *argv)
        rs = S.RunningSidecar(window_id="w1", app_id=app_id, port=1, process=proc,
                              started_at=0.0, install_dir=".")
        sup._by_app[app_id] = rs
        sup._refs[app_id] = {"w1"}
        seen: list[str] = []
        sup._surface_exit = lambda r, **_k: seen.append(r.app_id) or asyncio.sleep(0)
        reaper = asyncio.create_task(sup._reap(rs))
        if stop:
            await sup.close_app(app_id)
        await asyncio.wait_for(reaper, timeout=15)
        return bool(seen)

    deliberate = asyncio.run(run(
        "probe",
        ["import signal,time; signal.signal(signal.SIGTERM, signal.SIG_DFL); time.sleep(300)"],
        stop=True))
    crash = asyncio.run(run("crasher", ["import os; os.abort()"], stop=False))
    return deliberate, crash


def main() -> int:
    repo = os.environ.get("APP_ROOT") or os.path.dirname(
        os.path.dirname(os.path.abspath(__file__)))
    sys.path.insert(0, repo)
    sys.path.insert(0, os.path.join(repo, "brain"))

    print("process awareness — the host's view of its own machine")

    from server import process_awareness as pa  # noqa: E402

    src_pa = open(os.path.join(repo, "server", "process_awareness.py"),
                  encoding="utf-8").read()

    # ── 1. Provenance on every number ───────────────────────────────────
    pa._sample_once()
    snap = pa.snapshot(include_db=False)
    procs = snap["processes"]
    check("the snapshot found processes", bool(procs),
          "Nothing to assert against — /proc read as empty.")

    check("every process names the metric it was measured with",
          all(p.get("measured") in ("pss", "rss") for p in procs),
          "A number with no provenance reads as a fact. `measured` is what\n"
          "stops a fallback RSS reading being presented as a fair share.")

    check("a row that fell back to RSS carries no PSS",
          all(p["pss_bytes"] is None for p in procs if p["measured"] == "rss"),
          "smaps_rollup was unreadable for this row, so any pss_bytes on it\n"
          "would be invented. This is the exact substitution the module exists\n"
          "to prevent.")

    check("a measured row carries both PSS and private",
          all(p["pss_bytes"] is not None and p["private_bytes"] is not None
              for p in procs if p["measured"] == "pss"),
          "Private is the 'what do I get back' number and PSS is the "
          "'whose share is this' number; a measured row owes both.")

    # ── 2. The totals must not hide the gap ─────────────────────────────
    t = snap["totals"]
    for key in ("private_bytes", "pss_bytes", "rss_bytes_naive_sum",
                "processes_without_pss", "partial", "note"):
        check(f"totals carry `{key}`", key in t)

    check("the RSS sum is >= the PSS sum",
          t["rss_bytes_naive_sum"] >= t["pss_bytes"],
          f"rss={t['rss_bytes_naive_sum']} pss={t['pss_bytes']} — RSS counts "
          "shared pages in full and can never be the smaller of the two. If "
          "this trips, the two are being read from different places.")

    check("`partial` agrees with the count of unmeasured rows",
          t["partial"] == (t["processes_without_pss"] > 0),
          "A partial sum presented as a total is the original defect.")

    # ── 3. Idle is a measurement, not a default ─────────────────────────
    # A fresh ring has one sample per process, so nothing is yet knowable.
    pa._history.clear()
    pa._sample_once()
    first = pa.snapshot(include_db=False)
    check("one sample yields idle_for_s = None, never 0",
          all(p["idle_for_s"] is None for p in first["processes"]),
          "'I don't know yet' and 'idle for 0s' are different claims. A rule\n"
          "reading the second as the first calls every process busy forever.")

    time.sleep(1.1)
    pa._sample_once()
    second = pa.snapshot(include_db=False)
    check("a second sample makes idle measurable",
          any(p["idle_for_s"] is not None for p in second["processes"]),
          "Two samples is the minimum for a delta; if nothing became "
          "measurable the ring is not accumulating.")

    check("no finding is raised from an unmeasurable row",
          all(f["idle_for_s"] is not None for f in pa.findings(second)),
          "A rule that fires on a None idle time is guessing a verdict.")

    # ── 4. The kill surface refuses by role, with a reason ──────────────
    for pid, what in ((1, "the container's init"), (os.getpid(), "the caller itself")):
        res = asyncio.run(pa.terminate(pid))
        check(f"terminate refuses pid {pid} ({what})",
              res.get("ok") is False and bool(res.get("error")),
              f"Returned {res!r}. Refusing without saying why teaches the host "
              "nothing about its own machine.")

    check("every protected role has a reason string",
          all(pa._kill_block_reason(r) for r in pa._PROTECTED_ROLES),
          "A refusal with an empty reason is a bare 403 in a trench coat.")

    db_row = pa._db_proc(9, 1, None, None, "postgres: entity hostmem [local] idle")
    check("database processes are never killable",
          not db_row["killable"] and bool(db_row["kill_blocked_reason"]),
          "The DB runs in a sibling container with its own PID namespace — it\n"
          "cannot be signalled from here, and pretending otherwise would hand\n"
          "the host a lever that does nothing.")
    check("a database row is identified, not left unknown",
          db_row["role"] == "postgres-backend" and db_row["expected"],
          f"Got role={db_row['role']!r}. An unexplained postgres row is what "
          "sent this audit looking for a leak that was not there.")

    check("terminate re-checks start time immediately before signalling",
          "_still_the_same()" in src_pa,
          "Between the refusal decision and the signal the target can exit and\n"
          "the kernel can hand its pid to something new — which has not been\n"
          "through the refusal set. starttime is the disambiguator.")
    check("the SIGKILL path verifies death instead of claiming it",
          'method": "SIGKILL"' in src_pa and "still present" in src_pa,
          "SIGKILL cannot be caught but cannot reap: a zombie or a process\n"
          "wedged in uninterruptible IO stays listed. Reporting success would\n"
          "send the host back to a panel that contradicts the answer.")
    check("the alert dedup key carries start time",
          'f.get("starttime")' in src_pa,
          "On (pid, kind) alone a NEW process inheriting the number and\n"
          "tripping the same rule reads as the old one and is suppressed for\n"
          "six hours. Suppressing a real alert is the costly direction.")
    check("the database stats read refuses stale data",
          "DB_STATS_MAX_AGE_S" in src_pa and "stale" in src_pa,
          "/api/system/resources refuses this same file past 30s. A frozen\n"
          "process list is what a host would act on — killing something that\n"
          "exited ten minutes ago.")
    check("database rows are merged per pid, not whole-list",
          "by_pid" in src_pa,
          "The writer emits procx only when both smaps numbers read clean, so\n"
          "a process racing its own exit yields proc-without-procx. Choosing\n"
          "whole lists drops that pid from the panel for a cycle.")

    sup_src = open(os.path.join(repo, "server", "applications", "supervisor.py"),
                   encoding="utf-8").read()
    check("the supervisor exposes a public close_app",
          "async def close_app" in sup_src,
          "Without it the caller loops close_window over a COPY of private\n"
          "_refs, dropping the lock between windows — a window opening\n"
          "mid-close leaves a live ref to a sidecar believed closed.")
    check("close_app pops both maps under one lock",
          "self._refs.pop(app_id, None)" in sup_src
          and "self._by_app.pop(app_id, None)" in sup_src)
    check("the sandbox path no longer reads private _refs",
          "_refs" not in src_pa,
          "Reaching into supervisor internals is the fragility this replaced.")

    # Behavioural, not textual: spawn real children and drive the real
    # Supervisor. A sidecar with no SIGTERM handler dies at rc=-15, which by
    # returncode alone is indistinguishable from a crash — so BOTH directions
    # have to be pinned. Over-suppressing is the worse failure of the two: a
    # silent real crash beats a false alarm only until something actually
    # breaks.
    intentional, genuine = _exit_notice_behaviour()
    check("a deliberate stop raises no crash notice",
          intentional is False,
          "Reclaiming an idle sandbox would tell the user their app 'exited\n"
          "unexpectedly' — every time — which teaches them to ignore the\n"
          "notice that matters.")
    check("a genuine crash STILL raises one",
          genuine is True,
          "The suppression must key on 'we asked for this', never on the exit\n"
          "code. If this fails, real crashes have gone silent.")

    check("the kernel is never killable",
          pa._identify(os.getpid(), "", "")[0] == "kernel"
          and pa._identify(os.getpid(), "", "")[3] is False,
          "Stopping the kernel takes the machine down with the request. The "
          "kernel is THIS process — identified by pid, never by its name.")
    check("both containers' entrypoints are labelled from the database stats",
          pa._label_by_name("/bin/sh /app/entrypoint-postgres.sh", "sh")[0] == "entrypoint"
          and pa._label_by_name("/bin/sh /app/entrypoint.sh", "sh")[0] == "entrypoint",
          "Matching `entrypoint.sh` literally left entrypoint-postgres.sh "
          "reading as an unexpected process on every pod. (Name labels are for "
          "rows this container cannot signal; scripts/test_process_identity.py "
          "pins that a NAME never protects a process it can.)")

    # ── 5. The report reads no database ─────────────────────────────────
    for needle in ("asyncpg", "brain_service", "get_pool", "pool.fetch",
                   "pool.execute", "SELECT "):
        check(f"the module never touches the database (`{needle}`)",
              needle not in src_pa,
              "The support lane attaches this when the DATABASE is the fault\n"
              "being reported. One pool read here and that ticket cannot be\n"
              "filed by the host that most needs to file it.")

    report = pa.support_report()
    check("the support report renders", bool(report) and "container:" in report,
          f"Got {report[:120]!r}")
    check("the report states which metric each column is",
          "private" in report and "pss" in report and "naive-rss-sum" in report,
          "The report is read by someone who was not here. Unlabeled columns "
          "are how the 77 MB reading happened in the first place.")

    # ── 6. The wiring the surface is useless without ────────────────────
    scopes = open(os.path.join(repo, "server", "applications", "scopes.py"),
                  encoding="utf-8").read()
    check("write:processes is a declarable permission",
          '"write:processes"' in scopes,
          "The terminate route gates on it; undeclared, no app or host grant "
          "can ever hold it.")

    caps = open(os.path.join(repo, "brain", "mastercode", "runtime",
                             "meta_tool_caps.py"), encoding="utf-8").read()
    check("write:processes maps to a capability bit",
          '"write:processes"' in caps,
          "kernel_op fail-closes on a 0-bit scope, so an unmapped scope makes "
          "the route permanently unreachable by the host.")

    kops = open(os.path.join(repo, "brain", "mastercode", "runtime",
                             "kernel_ops.py"), encoding="utf-8").read()
    check("the process routes are exposed to the host",
          '"/api/system/processes"' in kops,
          "Without exposure the host can see the panel and never act on it, "
          "which is the gap this whole op was built to close.")

    sysroutes = open(os.path.join(repo, "server", "routes", "system.py"),
                     encoding="utf-8").read()
    check("the read route gates on read:processes",
          'requires_permission("read:processes")' in sysroutes)
    check("the terminate route gates on write:processes",
          'requires_permission("write:processes")' in sysroutes,
          "Reading what runs and ending it are different powers; one scope for "
          "both would hand every panel the kill lever.")

    support = open(os.path.join(repo, "server", "routes", "support.py"),
                   encoding="utf-8").read()
    check("the support lane attaches container signal",
          "support_report" in support,
          "The DB-free bug report is the half of this that reaches a human.")
    check("the container item is REDACTED like every other item",
          "_redact(report)" in support,
          "A process name is its argv, and argv is where credentials end up:\n"
          "`psql postgresql://u:p@h/db`, a curl with a bearer header. Assembly\n"
          "is the ONLY redaction point — send_service ships stored content\n"
          "verbatim — so an unredacted item puts a live secret on the approval\n"
          "screen and then on the platform.")
    check("redaction is not swallowed by the availability try/except",
          "report = support_report()" in support
          and support.index("except Exception as e:\n        log.debug(\"container signal")
          < support.index("_redact(report)"),
          "_redact raises SupportError(503) when the redactor cannot load, and\n"
          "that must fail the whole assembly as it does for every other item.\n"
          "Inside the try it becomes the one item that silently disappears.")

    main_py = open(os.path.join(repo, "server", "main.py"), encoding="utf-8").read()
    check("the sampler is dispatched at lifespan",
          "run_sampler_loop" in main_py,
          "Without the loop there is no history, so idle_for_s is None forever "
          "and no rule can ever fire.")

    pg = open(os.path.join(repo, "entrypoint-postgres.sh"), encoding="utf-8").read()
    check("the database publishes PSS beside RSS",
          "procx" in pg and "smaps_rollup" in pg,
          "Without this the DB half of the panel is RSS-only — which is the "
          "number the audit proved wrong by 8x.")
    check("the database still publishes the legacy proc record",
          'echo "proc ${_p#/proc/}' in pg,
          "The kernel and DB images ship on independent cadences. Dropping\n"
          "`proc` in the same release that adds `procx` blanks the database\n"
          "out on every pod whose kernel has not rolled yet.")

    print(f"\n{'PASS' if not FAILED else str(FAILED) + ' FAILED'}")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
