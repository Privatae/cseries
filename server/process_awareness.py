"""server.process_awareness — what is running on this machine, and what it IS.

The host already had a resources panel (`/api/system/resources`), and it could
say "132 MB, pid 7". It could not say *what pid 7 is*, whether that number is
even the right number, or whether the process has done anything in the last ten
minutes. So a host asked "why is my machine full?" could only read numbers back
out. This module is the layer that turns those numbers into a sentence, and
gives the host one lever to act on them.

Three things it owes the host, in order of how often they are got wrong:

1. **The right metric.** `/api/system/resources` reports RSS, and RSS
   double-counts every shared page in every process that maps it. Measured on
   one Host: four idle PostgreSQL backends read 22.6 / 20.5 /
   17.8 / 16.5 MB RSS — ~77 MB, which is what the audit showed. Their PSS sums
   to 20 MB and their PRIVATE pages to 8 MB, because `shared_buffers` is 32 MB
   and every backend maps it. Summing RSS across that container reports 152 MB
   against a cgroup that is actually charged 60 MB: over by 2.5x. A host told
   "postgres is using 77 MB of your 384 MB" would be acting on a number that is
   wrong by an order of magnitude in the only direction that matters. So every
   process here carries `pss_bytes` and `private_bytes`, and `measured` names
   which metric was actually readable — never a bare number with no provenance.

   The useful one for "what do I get back if this dies" is PRIVATE. PSS is the
   fair-share number for "who is responsible for this container's total". Both
   are reported because they answer different questions.

2. **Identity.** Every process is matched against a baseline of what is
   *supposed* to be running in an entity pod, and carries `what` (a plain
   sentence), `role`, and `expected`. A process that matches nothing is
   `expected: False` — which is the whole point: the rules below only escalate
   things that are not meant to be there.

3. **Change over time.** A single sample cannot tell idle from busy. The
   sampler keeps a short ring of CPU-time readings per process, so "this has
   burned no CPU for 14 minutes" is a measurement rather than an impression.

**Scope, honestly.** This container can see and act on its OWN processes only.
The pod deliberately has no shared PID namespace, so the database container's
processes arrive over the stats file it publishes (read-only, and not killable
from here — `killable` says so and `kill_blocked_reason` says why).

The hub is a third container and this module does NOT cover it: `snapshot()`
returns `entity` and `database` and nothing else. The hub self-reports over a
loopback API and `/api/system/resources` already relays that, but it publishes
a container total rather than per-process rows, so there is nothing to identify
or act on here yet. Said plainly because the alternative — a doc that implies
three containers over a snapshot that reads two — is the same class of wrong
number this module exists to stop, one level up.

**No database.** Nothing in this module reads or writes PostgreSQL. That is a
requirement, not an accident: the support lane (`server/routes/support.py`)
attaches this snapshot to a bug report, and a host whose DB is the thing that
is broken still has to be able to file one. Raw container signal only.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import time
from pathlib import Path

log = logging.getLogger("privatae.process_awareness")

_PAGE = os.sysconf("SC_PAGE_SIZE")
_TICK = os.sysconf("SC_CLK_TCK") or 100

# ── Tunables ───────────────────────────────────────────────────────────────
# Not asked of the user and not magic: each is derived below from something
# measurable about an entity pod, and each is env-overridable for the host that
# wants a different answer.

# How often the sampler reads /proc. 30s gives 20 usable samples inside the
# 10-minute idle window — enough that one raced read cannot flip a verdict —
# while costing one smaps_rollup read per process per 30s.
SAMPLE_INTERVAL_S = int(os.environ.get("PROC_SAMPLE_INTERVAL_S", "30"))

# Ring depth. 40 samples x 30s = 20 minutes of history, so the default
# 10-minute idle question is answered from the middle of the ring rather than
# from its edge.
HISTORY_DEPTH = int(os.environ.get("PROC_HISTORY_DEPTH", "40"))

# "Hasn't changed for n minutes." Idle means zero CPU delta across the window.
# 10 minutes is long enough that a slow poll loop (the pod's own background
# workers run on 15s and 60s cadences) never reads as idle, and short enough
# that a wedged sidecar is caught inside one working session.
IDLE_AFTER_S = int(os.environ.get("PROC_IDLE_AFTER_S", "600"))

# "Heavy." The entity container requests 384 MiB; 64 MiB is a sixth of that —
# a slice big enough that reclaiming it changes how the machine feels, and big
# enough that ordinary short-lived shells never trip it.
HEAVY_PRIVATE_BYTES = int(os.environ.get("PROC_HEAVY_PRIVATE_BYTES", str(64 << 20)))

# Grace between SIGTERM and SIGKILL on a terminate request.
TERM_GRACE_S = float(os.environ.get("PROC_TERM_GRACE_S", "5"))

# When the database container's published stats stop being trustworthy. Held at
# the same 30s as `_DB_STATS_MAX_AGE_S` (server/routes/system.py:446), against a
# writer whose own loop is 5s — six missed cycles. Duplicated rather than
# imported on purpose: this module must not import a route module (system.py
# imports THIS one), and the value is a property of the writer's cadence, which
# both readers observe independently. If one moves, move both.
DB_STATS_MAX_AGE_S = int(os.environ.get("PROC_DB_STATS_MAX_AGE_S", "30"))


# ── The baseline: what is SUPPOSED to be running ───────────────────────────
# The roles a process in this pod can hold, and how each is RECOGNISED.
#
# RECOGNISED BY WHAT A PROCESS CANNOT CHOOSE, NEVER BY ITS COMMAND LINE. This
# used to be a substring match over cmdline: any process whose argv contained
# `entrypoint`, `tini`, `server.launch` or `postgres: ` was "expected" and
# refused by terminate() — and argv is whatever the process says it is
# (`exec -a`, prctl, a script named entrypoint-anything). Anything started
# inside this container could make itself unkillable from the host and
# invisible to the "unexpected process" rules by picking its own name.
#
# Now, for a process this container can signal (same uid — the container drops
# every capability, so it can signal nothing else):
#   init        — pid 1. The kernel assigns it.
#   kernel      — this process: terminate() and snapshot() run INSIDE the
#                 kernel (entrypoint.sh `exec python3 -m server.launch`, no
#                 worker processes), so os.getpid() is the kernel.
#   entrypoint  — an ancestor of the kernel (the startup script, when it did
#                 not exec). Parentage is the kernel's record, not the child's.
#   unknown     — everything else. There is no same-uid postgres role: the
#                 database server is not in this image, and an executable's
#                 filename is chosen by whoever copied it there.
# A process owned by ANOTHER uid cannot be signalled from here at all; it is
# `other-user` — shown, unkillable, never expected — whatever it calls itself.
#
# Adding a legitimate long-running process to the pod still means adding it
# here — deliberately, so the set of things the host tolerates stays written
# down — but by a property it cannot forge.
_BASELINE: tuple[tuple[str, str, str, str, bool], ...] = (
    # (role, name-substring, plain-English `what`, owner, killable)
    # The name-substring is used ONLY to label the database container's
    # published stats rows, which this container cannot signal.
    ("init", "tini", "The container's init process — reaps orphaned children. "
     "Killing it ends the container.", "container", False),
    ("kernel", "server.launch", "Privatae Host itself — the kernel: the API, the "
     "brain runtime, and every background worker. This is the process the "
     "machine exists to run.", "cseries", False),
    # Bare "entrypoint" on purpose: the kernel container runs `entrypoint.sh`
    # and the database container runs `entrypoint-postgres.sh`, and matching
    # the former literally left the latter reading as an unexpected process on
    # every pod (seen).
    ("entrypoint", "entrypoint", "The container's startup script — the parent "
     "of the process this container exists to run.", "container", False),
    ("postgres-main", "postgres -D", "PostgreSQL's postmaster — the database "
     "supervisor. Every other postgres process is its child.", "database", False),
    ("postgres-backend", "postgres: entity", "One database connection held open "
     "by the kernel's connection pool. Most of what it appears to use is the "
     "shared buffer cache, counted once per connection — see private_bytes for "
     "what it actually costs.", "database", False),
    ("postgres-aux", "postgres: ", "A PostgreSQL maintenance worker "
     "(checkpointer, autovacuum, WAL writer, and similar).", "database", False),
)

# Processes that must never be terminated through this surface, whatever the
# rules say. pid 1 ends the container; the kernel is the thing serving the
# request. Both are refused with a reason rather than silently ignored.
_PROTECTED_ROLES = frozenset({"init", "kernel", "entrypoint",
                              "postgres-main", "postgres-backend", "postgres-aux"})

_ROLE = {role: (what, owner, killable) for role, _n, what, owner, killable in _BASELINE}

_UNKNOWN = ("unknown",
            "Not part of this machine's expected process set. It was started by "
            "something running inside the container — an app, a build, or a "
            "shell.", "unattributed", True)


def _role(role: str) -> tuple[str, str, str, bool]:
    what, owner, killable = _ROLE[role]
    return role, what, owner, killable


def _label_by_name(cmdline: str, comm: str) -> tuple[str, str, str, bool]:
    """(role, what, owner, killable) from a process's NAME. For description
    only: a row this container cannot signal (another uid, or the database
    container's stats). Never the basis for protecting a process it can."""
    hay = cmdline or comm or ""
    for role, needle, what, owner, killable in _BASELINE:
        if needle in hay:
            return role, what, owner, killable
    return _UNKNOWN


def _identify(pid: int, cmdline: str, comm: str) -> tuple[str, str, str, bool]:
    """(role, what, owner, killable) for one process IN THIS CONTAINER, from
    properties the process cannot choose (see the baseline note). Never raises."""
    if pid == 1:
        return _role("init")
    if pid == os.getpid():
        return _role("kernel")
    uid = _read_uid(pid)
    if uid is not None and uid != os.getuid():
        # ANOTHER UID IS NEVER LABELLED BY ITS NAME. The database server is not
        # in this image (Dockerfile.linux: postgresql-client-17 only), so no
        # legitimate process here runs as another user — and a name would let
        # one read as "expected" and slip past the findings rules. It is shown,
        # never signalled (this container holds no capability to), never
        # expected.
        return ("other-user",
                "Runs as another user inside this container. This container "
                "cannot signal it, so it is shown and never stopped from here.",
                "unattributed", False)
    if pid in _kernel_ancestors():
        return _role("entrypoint")
    # NO SAME-UID POSTGRES ROLE. The server is not in this image
    # (Dockerfile.linux: postgresql-client-17 only; the server is the
    # cseries-postgres container), so a same-uid process here running "postgres"
    # is not the database — and an executable's FILENAME is as much the
    # process's choice as its argv (copy any binary to /tmp/x/postgres). The
    # first version of this rewrite trusted the basename and was defeated
    # exactly that way.
    return _UNKNOWN


def _read_uid(pid: int) -> int | None:
    """The real uid from /proc/<pid>/status (readable for every process,
    dumpable or not). None if the process is gone."""
    try:
        for line in Path(f"/proc/{pid}/status").read_text().splitlines():
            if line.startswith("Uid:"):
                return int(line.split()[1])
    except Exception:
        pass
    return None


def _read_state(pid: int) -> str:
    """The one-letter state from /proc/<pid>/stat ('R', 'S', 'Z'…), or ""."""
    try:
        raw = Path(f"/proc/{pid}/stat").read_text()
        return raw[raw.rindex(")") + 2:].split()[0]
    except Exception:
        return ""


def _exited(pid: int) -> bool:
    """A zombie whose every thread is gone. State 'Z' alone is not enough:
    Linux shows a process as Z once its MAIN thread has exited while other
    threads still run, and that process is alive."""
    if _read_state(pid) != "Z":
        return False
    try:
        for line in Path(f"/proc/{pid}/status").read_text().splitlines():
            if line.startswith("Threads:"):
                return int(line.split()[1]) <= 1
    except Exception:
        pass
    return True


def _read_ppid(pid: int) -> int | None:
    try:
        raw = Path(f"/proc/{pid}/stat").read_text()
        return int(raw[raw.rindex(")") + 2:].split()[1])
    except Exception:
        return None


def _kernel_ancestors() -> frozenset[int]:
    """Every ancestor of the kernel below pid 1. Bounded so a corrupt read
    terminates."""
    out: set[int] = set()
    cur = os.getppid()
    for _ in range(16):
        if not cur or cur <= 1 or cur in out:
            break
        out.add(cur)
        cur = _read_ppid(cur)
    return frozenset(out)


# ── /proc reads ────────────────────────────────────────────────────────────

def _read_smaps_rollup(pid: int) -> tuple[int | None, int | None, str]:
    """(pss_bytes, private_bytes, metric_label) from /proc/<pid>/smaps_rollup.

    smaps_rollup is one pre-summed block (Linux 4.14+), so this is a single
    small read rather than a walk of every mapping. Unreadable for a process
    owned by another uid, and gone entirely if the process exits mid-read —
    both return None so the caller degrades to RSS *and says so*.
    """
    try:
        pss = priv = 0
        seen = False
        with open(f"/proc/{pid}/smaps_rollup", "rb") as fh:
            for raw in fh:
                line = raw.decode("ascii", "replace")
                key, _, rest = line.partition(":")
                if key == "Pss":
                    pss = int(rest.split()[0]) * 1024
                    seen = True
                elif key in ("Private_Clean", "Private_Dirty"):
                    priv += int(rest.split()[0]) * 1024
        if seen:
            return pss, priv, "pss"
    except Exception:
        pass
    return None, None, "rss"


def _read_stat(pid: int) -> tuple[int | None, int | None, str]:
    """(cpu_ticks, starttime_ticks, comm) from /proc/<pid>/stat.

    comm can contain spaces and parentheses, so the numeric fields are parsed
    from after the LAST ')' — splitting the whole line on whitespace mis-parses
    any process whose name has a space in it, which is most postgres workers.
    """
    try:
        raw = Path(f"/proc/{pid}/stat").read_text()
    except Exception:
        return None, None, ""
    try:
        close = raw.rindex(")")
        comm = raw[raw.index("(") + 1:close]
        # After the ') ' the first field is state, so stat's field 14 (utime)
        # and 15 (stime) are indices 11 and 12 here, and 22 (starttime) is 19.
        fields = raw[close + 2:].split()
        cpu = int(fields[11]) + int(fields[12])
        starttime = int(fields[19])
        return cpu, starttime, comm
    except Exception:
        return None, None, ""


def _read_cmdline(pid: int) -> str:
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
        return " ".join(p for p in raw.decode("utf-8", "replace").split("\x00") if p).strip()
    except Exception:
        return ""


def _boot_time() -> float:
    """Wall-clock seconds at boot, for turning starttime ticks into an age."""
    try:
        for line in Path("/proc/stat").read_text().splitlines():
            if line.startswith("btime "):
                return float(line.split()[1])
    except Exception:
        pass
    return 0.0


# ── The sampler ────────────────────────────────────────────────────────────
# Module-level so the ring survives across requests. Keyed by (pid, starttime)
# so a recycled pid can never inherit the previous process's history — pid
# reuse would otherwise make a fresh process look like it had been idle for
# twenty minutes.

_history: dict[tuple[int, int], dict] = {}
_last_sweep: float = 0.0


def _sample_once() -> None:
    """One pass over /proc. Fail-soft per process: a raced exit is normal."""
    global _last_sweep
    now = time.time()
    live: set[tuple[int, int]] = set()

    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        pid = int(entry)
        cpu, starttime, comm = _read_stat(pid)
        if cpu is None or starttime is None:
            continue  # exited between listdir and read
        key = (pid, starttime)
        live.add(key)
        rec = _history.get(key)
        if rec is None:
            rec = {"comm": comm, "cmdline": _read_cmdline(pid), "samples": []}
            _history[key] = rec
        elif not rec["cmdline"]:
            # A process can exec after we first saw it (a shell becoming the
            # thing it runs). Re-read until we have a cmdline to name it by.
            rec["cmdline"] = _read_cmdline(pid)
            rec["comm"] = comm or rec["comm"]
        rec["samples"].append((now, cpu))
        if len(rec["samples"]) > HISTORY_DEPTH:
            del rec["samples"][:-HISTORY_DEPTH]

    for gone in [k for k in _history if k not in live]:
        del _history[gone]
    _last_sweep = now


def _idle_for(key: tuple[int, int]) -> float | None:
    """Seconds this process has burned no CPU, or None if not yet knowable.

    Walks back from the newest sample to the oldest one whose CPU total still
    equals the newest. Returns None with fewer than two samples — "I don't know
    yet" is a legitimate answer and must not render as "idle for 0s", which a
    rule would read as busy.
    """
    rec = _history.get(key)
    if not rec or len(rec["samples"]) < 2:
        return None
    samples = rec["samples"]
    newest_ts, newest_cpu = samples[-1]
    oldest_matching_ts = newest_ts
    for ts, cpu in reversed(samples):
        if cpu != newest_cpu:
            break
        oldest_matching_ts = ts
    return max(0.0, newest_ts - oldest_matching_ts)


# ── Sandboxes ──────────────────────────────────────────────────────────────

def _sidecar_pids() -> dict[int, str]:
    """pid -> app_id for every app sandbox the supervisor is running.

    These are the pod's sandboxes: a `process`-runtime app gets its own
    supervised child (`Supervisor.list_running`, supervisor.py:123), and it is
    exactly the class of thing that can sit heavy and idle after the user
    closes the window. Empty when no sidecars run, which is the common case.
    """
    out: dict[int, str] = {}
    try:
        from server.applications.supervisor import get_supervisor
        sup = get_supervisor()
        if sup is None:
            return out
        for rs in sup.list_running():
            if rs.pid is not None:
                out[rs.pid] = rs.app_id
    except Exception as e:
        log.debug("supervisor read failed: %s", e)
    return out


# ── Snapshot ───────────────────────────────────────────────────────────────

def snapshot(*, include_db: bool = True) -> dict:
    """Every process this container can see, identified and measured.

    Reads /proc directly — no DB, no network, no privileges beyond the
    container's own namespace. Safe to call from the support lane when the
    database is the thing that is broken.
    """
    if not _history:
        # First caller primes the ring so a snapshot taken before the sampler's
        # first tick still names processes; idle_for stays None until there are
        # two samples, which is the honest answer.
        try:
            _sample_once()
        except Exception as e:
            log.warning("initial sample failed: %s", e)

    sidecars = _sidecar_pids()
    btime = _boot_time()
    now = time.time()
    procs: list[dict] = []

    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        pid = int(entry)
        cpu, starttime, comm = _read_stat(pid)
        if cpu is None or starttime is None:
            continue
        cmdline = _read_cmdline(pid)
        pss, priv, metric = _read_smaps_rollup(pid)
        rss = None
        try:
            rss = int(Path(f"/proc/{pid}/statm").read_text().split()[1]) * _PAGE
        except Exception:
            pass

        role, what, owner, killable = _identify(pid, cmdline, comm)
        app_id = sidecars.get(pid)
        if app_id:
            # A supervised sandbox outranks the baseline match: it IS expected,
            # it belongs to a named app, and it is stopped through the
            # supervisor rather than by signal.
            role, owner, killable = "app-sandbox", f"app:{app_id}", True
            what = (f"The sandbox for the `{app_id}` app — its own process, "
                    "started when you opened the app.")

        age = None
        if btime and starttime:
            age = max(0.0, now - (btime + starttime / _TICK))

        procs.append({
            "pid": pid,
            # The kernel's own start-tick for this pid. Carried so anything
            # keyed on "this process" — the sample ring, the alert dedup, the
            # pre-signal recheck — can tell it apart from the NEXT process to
            # be handed the same pid. A pid on its own is not an identity.
            "starttime": starttime,
            "name": (cmdline or comm)[:120],
            "role": role,
            "what": what,
            "owner": owner,
            "app_id": app_id,
            # A process running as another uid is not on the baseline either;
            # it is only unkillable from here, which is a different fact.
            "expected": role not in ("unknown", "other-user"),
            "container": "entity",
            # Three numbers, each labeled. private_bytes is what you get back
            # if this process dies; pss_bytes is its fair share of the
            # container's total; rss_bytes is kept because it is what every
            # other tool shows, and it is the one that over-reports.
            "private_bytes": priv,
            "pss_bytes": pss,
            "rss_bytes": rss,
            "measured": metric,
            "cpu_seconds": round(cpu / _TICK, 2),
            "idle_for_s": _idle_for((pid, starttime)),
            "age_s": None if age is None else round(age),
            "killable": killable,
            "kill_blocked_reason": None if killable else _kill_block_reason(role),
        })

    procs.sort(key=lambda p: (p["private_bytes"] or p["rss_bytes"] or 0), reverse=True)

    out = {
        "container": "entity",
        "sampled_at": _last_sweep or None,
        "sample_interval_s": SAMPLE_INTERVAL_S,
        "history_seconds": HISTORY_DEPTH * SAMPLE_INTERVAL_S,
        "idle_after_s": IDLE_AFTER_S,
        "heavy_private_bytes": HEAVY_PRIVATE_BYTES,
        "processes": procs,
        "totals": _totals(procs),
    }
    if include_db:
        out["database"] = _db_processes()
    return out


def _kill_block_reason(role: str) -> str:
    if role in ("init", "entrypoint"):
        return "This is the container's own startup process — ending it stops the machine."
    if role == "kernel":
        return "This is Privatae Host itself. Stopping it takes the whole machine down; restart it instead."
    if role == "other-user":
        return ("It runs as another user, and this container holds no capability "
                "to signal another user's process.")
    if role.startswith("postgres"):
        return ("This runs in the database container, which has its own process "
                "space — it cannot be signalled from here, and stopping a database "
                "process by hand risks the data.")
    return "Protected."


def _totals(procs: list[dict]) -> dict:
    """Sum the metrics AND say which processes had to be left out of each.

    A sum over a metric that was unreadable for some processes is a partial
    sum, and a partial sum presented as a total is the same class of wrong
    number this module exists to stop.
    """
    priv = sum(p["private_bytes"] or 0 for p in procs)
    pss = sum(p["pss_bytes"] or 0 for p in procs)
    rss = sum(p["rss_bytes"] or 0 for p in procs)
    missing = sum(1 for p in procs if p["pss_bytes"] is None)
    return {
        "private_bytes": priv,
        "pss_bytes": pss,
        "rss_bytes_naive_sum": rss,
        "processes_without_pss": missing,
        "partial": missing > 0,
        # Stated rather than left for the reader to notice: this is the gap
        # that made four idle backends look like 77 MB.
        "note": ("rss_bytes_naive_sum double-counts shared pages once per "
                 "process and is always the largest of the three. Use "
                 "private_bytes for what stopping something would return, and "
                 "pss_bytes to attribute the container's total."),
    }


def _db_processes() -> dict:
    """The database container's processes, from the stats file it publishes.

    No shared PID namespace, so these are observed and never actionable. Reads
    the `procx` records (pid/rss/pss/private) when the DB image is new enough
    to publish them, and falls back to the older `proc` records (rss only) —
    the two images ship on independent cadences, so a kernel that only handled
    the new shape would blank the database out on every pod that had not rolled
    yet.
    """
    sock = os.environ.get("PG_SOCKET_DIR", "/var/run/postgresql")
    try:
        raw = Path(sock, ".container-stats").read_text()
    except Exception:
        return {"present": False, "reason": "no stats file from the database container"}

    # Merged per PID rather than "all the new ones, else all the old ones".
    # The writer emits `proc` for every process but `procx` only when BOTH
    # smaps numbers read as plain digits, so a process that exits between its
    # statm and its smaps_rollup read produces a proc line and no procx line.
    # Choosing whole lists would drop that pid from the panel for a cycle;
    # keyed by pid, it simply falls back to RSS and says so in `measured`.
    by_pid: dict[int, dict] = {}
    ts = None
    for line in raw.splitlines():
        key, _, rest = line.partition(" ")
        if key == "ts":
            try:
                ts = int(rest.strip())
            except ValueError:
                pass
        elif key == "procx":
            parts = rest.split(" ", 4)
            if len(parts) == 5 and all(x.isdigit() for x in parts[:4]):
                by_pid[int(parts[0])] = _db_proc(int(parts[0]), int(parts[1]),
                                                 int(parts[2]), int(parts[3]), parts[4])
        elif key == "proc":
            parts = rest.split(" ", 2)
            if len(parts) == 3 and parts[0].isdigit() and parts[1].isdigit():
                pid = int(parts[0])
                # procx is the better record and may have been read already;
                # never let the RSS-only line overwrite it.
                if pid not in by_pid or by_pid[pid]["measured"] != "pss":
                    by_pid[pid] = _db_proc(pid, int(parts[1]), None, None, parts[2])

    procs = list(by_pid.values())
    if not procs:
        return {"present": False, "reason": "stats file carried no process records"}

    # Same rule /api/system/resources applies to this file (_db_stats,
    # server/routes/system.py:526): past the cutoff the writer is not running,
    # and the honest report is ABSENT rather than a frozen snapshot with an age
    # label the reader may not look at. A stale process list is exactly the kind
    # of thing a host would act on — killing something that exited ten minutes
    # ago — so the two readers must not disagree about when to stop trusting it.
    age = None if ts is None else max(0, int(time.time()) - ts)
    if age is None:
        return {"present": False, "reason": "stats file carried no timestamp"}
    if age > DB_STATS_MAX_AGE_S:
        return {"present": False, "age_s": age,
                "reason": f"stats {age}s stale — the database container's "
                          "stats writer is not running"}
    procs.sort(key=lambda p: (p["private_bytes"] or p["rss_bytes"] or 0), reverse=True)
    return {"present": True, "age_s": age, "container": "postgres",
            "processes": procs, "totals": _totals(procs)}


def _db_proc(pid: int, rss: int, pss: int | None, priv: int | None, name: str) -> dict:
    name = name.strip()[:120]
    role, what, owner, _killable = _label_by_name(name, name)
    return {
        "pid": pid, "starttime": None, "name": name, "role": role, "what": what,
        "owner": owner,
        "app_id": None, "expected": role != "unknown", "container": "postgres",
        "private_bytes": priv, "pss_bytes": pss, "rss_bytes": rss,
        "measured": "pss" if pss is not None else "rss",
        "cpu_seconds": None, "idle_for_s": None, "age_s": None,
        "killable": False,
        "kill_blocked_reason": _kill_block_reason(
            role if role.startswith("postgres") else "postgres-aux"),
    }


# ── The rules ──────────────────────────────────────────────────────────────

def findings(snap: dict | None = None) -> list[dict]:
    """Everything worth telling the host about, most costly first.

    Deliberately narrow. The pod runs a couple of dozen background workers and
    a handful of supervised sidecars, all of them legitimate; a rule that fires
    on any of them trains the host to ignore this surface. So a finding needs
    BOTH halves — heavy AND idle — and, for the escalating kind, it has to be
    something that is not in the baseline at all.
    """
    snap = snap or snapshot()
    out: list[dict] = []

    for p in snap.get("processes", []):
        cost = p["private_bytes"] if p["private_bytes"] is not None else p["rss_bytes"]
        idle = p["idle_for_s"]
        if cost is None or idle is None:
            continue  # not measurable yet — never guess a verdict
        heavy = cost >= HEAVY_PRIVATE_BYTES
        stale = idle >= IDLE_AFTER_S

        if heavy and stale and not p["expected"]:
            out.append(_finding(
                p, cost, idle, "heavy_idle_unexpected", "alert",
                f"`{p['name'][:60]}` is holding {_mb(cost)} and has done nothing "
                f"for {_mins(idle)}. It is not part of this machine's expected "
                f"process set, so nothing is waiting on it."))
        elif heavy and stale and p["role"] == "app-sandbox":
            out.append(_finding(
                p, cost, idle, "idle_app_sandbox", "notice",
                f"The `{p['app_id']}` app's sandbox is holding {_mb(cost)} and has "
                f"done nothing for {_mins(idle)}. Closing the app frees it."))
        elif stale and not p["expected"] and cost >= (HEAVY_PRIVATE_BYTES // 4):
            out.append(_finding(
                p, cost, idle, "idle_unexpected", "notice",
                f"`{p['name'][:60]}` has done nothing for {_mins(idle)} and is not "
                f"part of the expected process set. It is only holding "
                f"{_mb(cost)}, so this is untidiness rather than a problem."))

    out.sort(key=lambda f: (f["severity"] != "alert", -(f["cost_bytes"] or 0)))
    return out


def _finding(p: dict, cost: int, idle: float, kind: str, severity: str, text: str) -> dict:
    return {
        "kind": kind, "severity": severity, "pid": p["pid"],
        "starttime": p.get("starttime"), "name": p["name"],
        "app_id": p["app_id"], "role": p["role"], "what": p["what"],
        "cost_bytes": cost, "cost_metric": p["measured"],
        "idle_for_s": round(idle), "killable": p["killable"],
        "kill_blocked_reason": p["kill_blocked_reason"], "text": text,
    }


def _mb(n: int) -> str:
    return f"{n / (1 << 20):.0f} MB"


def _mins(s: float) -> str:
    m = int(s // 60)
    return "under a minute" if m < 1 else (f"{m} minutes" if m != 1 else "1 minute")


# ── Acting on it ───────────────────────────────────────────────────────────

async def _close_sandbox(app_id: str) -> bool:
    """Ask the supervisor to stop one app's sandbox.

    Goes through `Supervisor.close_app` (supervisor.py:294), which drops the
    window refcount and terminates under one lock. Signalling the sidecar
    directly would leave the supervisor still holding the app in `_by_app` and
    believing it was up.

    Returns True when the supervisor owned it and the sidecar is now gone.
    """
    from server.applications.supervisor import get_supervisor
    sup = get_supervisor()
    if sup is None:
        return False
    running = sup.get(app_id)
    if running is not None and running.resident:
        # A resident (always-on) app is idle BY DESIGN between the moments it
        # works — a poller sleeping between polls looks exactly like a wedged
        # sandbox from here. The user switched it on; only the user or the
        # host switches it off (stop_app / the runtime route), never the sweep.
        log.info("idle reclaim skipped %s: resident sidecar", app_id)
        return False
    return await sup.close_app(app_id)


async def terminate(pid: int, *, force: bool = False) -> dict:
    """Stop one process. Refuses anything the baseline marks protected.

    An app sandbox goes through the supervisor so its refcount and window state
    stay consistent. Everything else gets SIGTERM, then SIGKILL after
    TERM_GRACE_S if it is still there and `force` was asked for.
    """
    if pid == 1:
        return {"ok": False, "pid": pid, "error": _kill_block_reason("init")}
    if pid == os.getpid():
        return {"ok": False, "pid": pid, "error": _kill_block_reason("kernel")}

    cpu, starttime, comm = _read_stat(pid)
    if cpu is None:
        return {"ok": False, "error": f"No process {pid} is running."}
    if _exited(pid):
        # Already dead, waiting for its parent to collect it. Saying so beats
        # the pre-signal check's "the number has been reused", which would be
        # false.
        return {"ok": True, "pid": pid, "result": f"pid {pid} has already exited; "
                "it is waiting for its parent to collect it. Nothing to stop."}
    cmdline = _read_cmdline(pid)
    app_id = _sidecar_pids().get(pid)

    def _still_the_same() -> bool:
        """Is `pid` STILL the process we decided it was safe to stop?

        Everything between here and the signal is a window in which the target
        can exit and the kernel can hand its pid to something new — and the new
        thing has not been through the refusal set. starttime is the disambiguator
        the ring already keys on for exactly this reason (a pid alone would let a
        recycled process inherit the previous one's history, and here it would let
        it inherit the previous one's permission to be killed).
        """
        # A ZOMBIE IS DEAD. It keeps its pid and starttime until its parent
        # reaps it — and when the parent is the kernel (a subprocess it has not
        # waited on yet), that can be a while — so comparing starttime alone
        # read a process SIGTERM had already ended as "ignored SIGTERM" and
        # asked the user to force-kill a corpse.
        return _read_stat(pid)[1] == starttime and not _exited(pid)

    if app_id is None:
        role, _what, _owner, killable = _identify(pid, cmdline, comm)
        if not killable or role in _PROTECTED_ROLES:
            return {"ok": False, "pid": pid, "role": role,
                    "error": _kill_block_reason(role)}
    else:
        role = "app-sandbox"
        try:
            if await _close_sandbox(app_id):
                return {"ok": True, "pid": pid, "app_id": app_id, "role": role,
                        "method": "supervisor",
                        "result": f"Closed the `{app_id}` app and freed its sandbox."}
        except Exception as e:
            log.warning("supervisor close of %s failed (%s) — falling back to signal",
                        app_id, e)

    if not _still_the_same():
        return {"ok": False, "pid": pid,
                "error": f"pid {pid} is no longer the process that was checked — "
                         "it exited and the number has been reused. Nothing was "
                         "signalled; read the list again."}
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return {"ok": True, "pid": pid, "result": "Already gone."}
    except PermissionError:
        return {"ok": False, "pid": pid,
                "error": "This container is not allowed to signal that process."}

    deadline = time.monotonic() + TERM_GRACE_S
    while time.monotonic() < deadline:
        await asyncio.sleep(0.2)
        if not _still_the_same():
            return {"ok": True, "pid": pid, "role": role, "method": "SIGTERM",
                    "result": f"Stopped pid {pid}."}

    if not force:
        return {"ok": False, "pid": pid, "role": role, "needs_force": True,
                "error": (f"pid {pid} ignored SIGTERM after {TERM_GRACE_S:.0f}s. "
                          "Ask again with force to stop it outright.")}
    if not _still_the_same():
        return {"ok": True, "pid": pid, "role": role, "method": "SIGTERM",
                "result": f"Stopped pid {pid}."}
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    except PermissionError:
        return {"ok": False, "pid": pid,
                "error": "This container is not allowed to signal that process."}
    await asyncio.sleep(0.2)
    # SIGKILL cannot be caught, but it also cannot reap: a process whose parent
    # never wait()s stays visible as a zombie, and one wedged in uninterruptible
    # IO does not leave until that IO returns. Reporting "force-stopped" while
    # the row is still in the list would send the host back to a panel that
    # contradicts the answer it was just given, so check instead of assuming.
    if _still_the_same():
        return {"ok": False, "pid": pid, "role": role, "method": "SIGKILL",
                "error": (f"pid {pid} was sent SIGKILL and is still present. It is "
                          "either a zombie its parent has not collected, or stuck "
                          "in a system call that has to finish first. Nothing more "
                          "can be done to it from here.")}
    return {"ok": True, "pid": pid, "role": role, "method": "SIGKILL",
            "result": f"Force-stopped pid {pid}."}


# ── The standing op ────────────────────────────────────────────────────────

_alerted: dict[tuple[int, int | None, str], float] = {}
# Re-alerting on the same still-idle process every sweep is how a useful signal
# becomes noise the host filters out. One notification per process per rule, and
# the entry is dropped once the finding clears.
#
# Keyed on starttime as well as pid, for the same reason the sample ring is: a
# pid is reused, and on (pid, kind) alone a NEW process that inherited the
# number and tripped the same rule would be read as the old one still sitting
# there — and suppressed for six hours. Suppressing a real alert is the more
# expensive direction of that mistake.
_REALERT_AFTER_S = 6 * 3600


async def _alert(finding: dict) -> None:
    """Push one finding into the host's inbox as a system notification.

    Same surface the supervisor's crash notice uses — `handle_inbox_append`
    (server/bridge/memory_handlers.py:308), message_kind 'system_notification',
    which is the lane the user actually reads.
    """
    try:
        from server.bridge.memory_handlers import handle_inbox_append
    except Exception as e:
        log.debug("inbox unavailable (%s) — finding not surfaced", e)
        return
    body = finding["text"]
    if finding["killable"]:
        body += ("\n\nYou can stop it from the machine panel, or ask me to — "
                 f"it is pid {finding['pid']}.")
    try:
        await handle_inbox_append({
            "subject": f"Idle process holding {_mb(finding['cost_bytes'])}",
            "body": body,
            "message_kind": "system_notification",
            "payload": {"type": "idle_process", **finding},
        })
    except Exception as e:
        log.warning("idle-process notification failed: %s", e)


async def run_sampler_loop() -> None:
    """Forever-loop, dispatched at lifespan beside the pod's other workers.

    Samples /proc, evaluates the rules, and notifies once per finding. Never
    raises out: process bookkeeping must not be able to take the event loop
    down. Costs one /proc walk every SAMPLE_INTERVAL_S.
    """
    log.info("process awareness online — sampling every %ds, idle after %ds, "
             "heavy at %s", SAMPLE_INTERVAL_S, IDLE_AFTER_S,
             _mb(HEAVY_PRIVATE_BYTES))
    # This loop reads no database, so it adds no pool pressure — but it is a
    # periodic worker dispatched from the same lifespan block as the rest, and a
    # new worker that opts out of the convention is how the convention rots.
    from server.loop_splay import splay
    await splay(SAMPLE_INTERVAL_S, "process_awareness")
    while True:
        try:
            await asyncio.sleep(SAMPLE_INTERVAL_S)
            _sample_once()
            now = time.time()
            open_keys = set()
            for f in findings():
                if f["severity"] != "alert":
                    continue
                key = (f["pid"], f.get("starttime"), f["kind"])
                open_keys.add(key)
                if now - _alerted.get(key, 0.0) < _REALERT_AFTER_S:
                    continue
                _alerted[key] = now
                await _alert(f)
            # Forget processes that no longer trip a rule, so the same pid
            # alerting again later is a real event rather than a suppressed one.
            for gone in [k for k in _alerted if k not in open_keys]:
                del _alerted[gone]
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.warning("process sampler tick failed (non-fatal): %s", e)


# ── The support report ─────────────────────────────────────────────────────

def support_report() -> str:
    """A plain-text container report for a bug the host is filing.

    READS NO DATABASE, by construction — /proc and one stats file, nothing
    else. That is what makes it attachable when the database is the fault being
    reported, and it is why this lives here rather than in the support route's
    context assembler, which is entirely DB reads.

    Carries no file contents and no memory: process names, the numbers, and
    what each process is.

    It is NOT self-sanitising, and the caller must not assume it is. A process
    name is its argv, which is one of the classic places a credential ends up
    (`psql postgresql://user:pass@host/db`, a curl carrying a bearer header, a
    `bash -c` one-liner). The support lane runs this through its `_redact`
    before the text reaches the approval screen; any future caller owes the
    same. Sanitising here instead would be worse — it would put a second,
    diverging redactor in the tree and let the support lane believe it had
    already been done.
    """
    try:
        snap = snapshot()
    except Exception as e:
        return f"container signal unavailable: {e}"

    lines: list[str] = []
    t = snap["totals"]
    lines.append(f"container: host  (history {snap['history_seconds']}s, "
                 f"sampled every {snap['sample_interval_s']}s)")
    lines.append(f"private={_mb(t['private_bytes'])} pss={_mb(t['pss_bytes'])} "
                 f"naive-rss-sum={_mb(t['rss_bytes_naive_sum'])}"
                 + (f"  [PARTIAL: {t['processes_without_pss']} processes without PSS]"
                    if t["partial"] else ""))
    lines.append("")
    header = f"{'pid':>7}  {'private':>10}  {'pss':>10}  {'cpu_s':>8}  {'idle_s':>7}  role / name"
    lines.append(header)
    for p in snap["processes"]:
        lines.append(_report_row(p))

    db = snap.get("database") or {}
    lines.append("")
    if db.get("present"):
        lines.append(f"container: postgres  (stats {db.get('age_s')}s old)")
        if any(p["measured"] == "rss" for p in db["processes"]):
            lines.append("  note: this database image publishes RSS only — the "
                         "column marked ~ is RSS, which counts the 32 MB shared "
                         "buffer cache once per backend and so over-reports.")
        lines.append(header)
        for p in db["processes"]:
            lines.append(_report_row(p))
    else:
        lines.append(f"container: postgres — not reporting ({db.get('reason')})")

    found = findings(snap)
    lines.append("")
    lines.append(f"findings: {len(found)}")
    for item in found:
        lines.append(f"  [{item['severity']}] {item['kind']}: {item['text']}")
    return "\n".join(lines)


def _report_row(p: dict) -> str:
    def mb(v):
        return "-" if v is None else f"{v >> 20} MB"

    # A DB image that predates the procx records carries RSS only. Rendering
    # those rows as two dashes would read as "the database is using nothing",
    # which is the opposite of true — show the number that exists and mark it
    # as the metric that over-reports.
    priv, pss = mb(p["private_bytes"]), mb(p["pss_bytes"])
    if p["private_bytes"] is None and p["pss_bytes"] is None and p["rss_bytes"] is not None:
        priv, pss = "-", f"{p['rss_bytes'] >> 20} MB~"
    return (f"{p['pid']:>7}  {priv:>10}  {pss:>10}  "
            f"{('-' if p['cpu_seconds'] is None else p['cpu_seconds']):>8}  "
            f"{('-' if p['idle_for_s'] is None else round(p['idle_for_s'])):>7}  "
            f"{p['role']} / {p['name'][:64]}")
