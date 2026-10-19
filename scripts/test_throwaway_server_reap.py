#!/usr/bin/env python3
"""A gate killed before its cleanup must not leave its throwaway Postgres running.

scripts/kernel_schema.py throwaway_server() starts a container through
scripts/peer_test_db.sh and removes it in a `finally`. SIGKILL (hang_watch's
first signal on a hang), SIGABRT, or SIGTERM (Python's default action ends the
process without running `finally`) skip that, and on main nothing labelled or
reaped what was left: the container ran until someone noticed.

So every throwaway container is labelled with its creator (pid, PID namespace,
process start time), and each throwaway_server() first removes the labelled
containers whose creator is gone. Gates run in parallel, so a container whose
creator is ALIVE is never touched, and neither is one from another PID
namespace (its pid means nothing here) or one without the label.

Each server is reached over its unix socket, in a folder of its own, and
publishes no TCP port (under the CI runner's firewall a TCP loopback
connection that reused a server-side TIME_WAIT 4-tuple was refused). Section B
also holds that: no port, no network, the DSN names the container's own socket
folder, and the folder goes when the container goes, by the gate's own cleanup
or by the next gate's reap.

  A. the selection, with a stand-in for "is this process alive": only a dead
     creator in this PID namespace is reaped
  B. live, on real docker: gates A and D are SIGKILLed with their containers
     up, gate B is still running; the next gate C reaps A's and D's
     containers and folders (D's made under a parent C does not look in, found
     from its container's socket label) and leaves B's
  C. a server that stops serving explains itself, with a fake docker
     and psql on PATH: an error under a gone or exited server becomes a
     SchemaError carrying docker's state, log tail, events and labels, and
     its socket folder and the socket in it, raised from the original; an
     error under a live server is left as it is, unless it is a connection
     this server's own socket did not take; a normal end costs one `docker
     inspect`; docker hanging or refusing never replaces the error, and a
     cleanup docker hangs on gives up in its time limit; a start that fails
     says what docker and its socket folder say
  D. live, on real docker: a gate's own server stopped under a kernel_schema
     step explains itself with what docker really recorded
  E. peer_test_db.sh empties, removes, opens up (0777) or mounts only a
     socket folder made for its server: a foreign one, as a live
     server's own is, given to it or named by a container's label, is left
     exactly as it was, and so is one reached through a symlink s given with
     a / at its end; its own are made, used and removed as before
  F. the reap removes the folder a gone gate's container's socket label
     names, wherever it was made, and only a folder throwaway_server makes

Needs docker and psql (scripts/peer_test_db.sh). Smoke runs it, and so does
pre_merge_gates.sh (it reads smoke's list); the database image is smoke's, or
built from this tree's Dockerfile.postgres when absent. Exits 2 and says so
without docker, which both runners count as a failure.
"""

import contextlib
import hashlib
import io
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(os.environ.get("APP_ROOT") or Path(__file__).resolve().parents[1])
sys.path.insert(0, str(ROOT / "scripts"))

import kernel_schema as ks  # noqa: E402

FAILS: list[str] = []


def check(name, cond, extra=""):
    if cond:
        print(f"  ok    {name}")
    else:
        FAILS.append(name)
        print(f"[throwaway-reap] FAIL {name} {extra}")


# ── A. the selection ──────────────────────────────────────────────────────
sel = getattr(ks, "throwaway_leftovers", None)
check("A0 kernel_schema.throwaway_leftovers exists", callable(sel))
if callable(sel):
    ns, boot = str(ks.PIDNS), getattr(ks, "BOOT_ID", "")
    check("A0b this machine has a boot id to label with", bool(boot), boot)
    rows = [
        f"dead-here\t101\t{ns}\t5000\t{boot}",       # creator gone, this namespace: reap
        f"alive-here\t102\t{ns}\t6000\t{boot}",      # creator alive: never
        f"reused-pid\t103\t{ns}\t7000\t{boot}",      # pid alive but started later: a new process, reap
        f"other-ns\t104\t999\t8000\t{boot}",         # another PID namespace: not judged here
        "no-label\t\t\t\t",                          # not provably ours
        f"junk-pid\tabc\t{ns}\t1\t{boot}",
        # Same pid, namespace inode and start, labelled on another boot: the
        # initial PID namespace has one inode number on every machine, so this
        # is a gate on another machine sharing the daemon (or before a reboot).
        f"other-boot\t101\t{ns}\t5000\tnot-this-boot",
        f"no-boot\t101\t{ns}\t5000\t",
        # The pid exists but its /proc stat cannot be read (hidepid, another
        # user): alive, start unknown -- never reaped.
        f"hidden-alive\t105\t{ns}\t5000\t{boot}",
    ]
    starts = {102: "6000", 103: "9999", 105: ""}   # what /proc says now; 101 and 104 are gone
    got = sel(rows, start_of=lambda pid: starts.get(pid))
    check("A1 only a creator that is gone (or whose pid now belongs to a newer process) is reaped",
          sorted(got) == ["dead-here", "reused-pid"], got)
    check("A2 a container whose creator is alive is never reaped",
          "alive-here" not in got)
    check("A3 another PID namespace and unlabelled containers are left be",
          "other-ns" not in got and "no-label" not in got and "junk-pid" not in got)
    check("A5 a container labelled on another boot (another machine on a shared daemon) is left be",
          "other-boot" not in got and "no-boot" not in got, got)
    check("A6 a live pid whose start cannot be read is never reaped", "hidden-alive" not in got, got)
    got2 = sel([f"nostart-alive\t102\t{ns}\t\t{boot}", f"nostart-alive2\t102\t{ns}\tNone\t{boot}",
                f"nostart-dead\t101\t{ns}\t\t{boot}"], start_of=lambda pid: starts.get(pid))
    check("A4 with no start time recorded, a live pid is never reaped and a dead one is",
          got2 == ["nostart-dead"], got2)
    # _proc_start on real processes: this one is alive with a start time; a
    # child that has exited and been waited for is gone.
    _gone = subprocess.Popen([sys.executable, "-c", "pass"])
    _gone.wait()
    check("A7 _proc_start: this process reads as alive with its start time, an exited one as gone",
          (ks._proc_start(os.getpid()) or "").isdigit() and ks._proc_start(_gone.pid) is None)

# ── C. a server that stops serving explains itself (fakes) ────────────────
# A docker that serves peer_test_db.sh's start and stop (the socket label,
# rm, run) and kernel_schema's questions (inspect, logs, events, ps) from a
# JSON state file the checks below change mid-gate, and logs every call; a
# psql that answers or refuses as the state says. `run` leaves a socket in the
# folder it mounts, as the image's server makes one there, and `rm` leaves it
# there, as a server killed by `docker rm -f` does. The socket folders are
# made under the fake's own folder (CSERIES_GATE_SHARED_DIR). DOCKER_HOST points
# nowhere, so a real docker reached instead reaches nothing.
FAKE_DOCKER = r'''#!/usr/bin/env python3
import hashlib, json, os, socket, sys, time
state_file = os.environ["KS_FAKE_DOCKER_STATE"]
args = sys.argv[1:]
with open(os.environ["KS_FAKE_DOCKER_LOG"], "a") as f:
    f.write(json.dumps(args) + "\n")
with open(state_file) as f:
    st = json.load(f)
cmd = args[0] if args else ""
if cmd in st.get("hang", []) and st.get("release"):
    # Hang until the check releases it (at most 20 s), saying when it began;
    # then act on the state as it is by then, and say when that is done.
    with open(st["release"] + ".at", "w") as f:
        f.write(repr(time.time()))
    end = time.time() + 20
    while time.time() < end and not os.path.exists(st["release"]):
        time.sleep(0.05)
    with open(state_file) as f:
        st = json.load(f)
elif cmd in st.get("hang", []):
    time.sleep(30)
if cmd in st.get("refuse", []):
    sys.stderr.write("Cannot connect to the Docker daemon at unix:///nowhere. Is the docker daemon running?\n")
    sys.exit(1)
cs = st.setdefault("containers", {})


def save():
    with open(state_file, "w") as f:
        json.dump(st, f)


def opt(flag):
    return [args[i + 1] for i, a in enumerate(args[:-1]) if a == flag]


if cmd == "rm":
    for n in args[1:]:
        cs.pop(n, None)
    save()
    if st.get("release"):
        open(st["release"] + ".done", "w").close()
    sys.exit(0)
if cmd == "run":
    if st.get("run_fails"):
        sys.stderr.write(st["run_fails"] + "\n")
        sys.exit(125)
    n = opt("--name")[0]
    src = opt("-v")[0].rsplit(":", 1)[0]
    s = socket.socket(socket.AF_UNIX)
    s.bind(os.path.join(src, ".s.PGSQL.5432"))
    s.close()
    cs[n] = {"Id": hashlib.sha256(n.encode()).hexdigest(),
             "State": {"Status": "running", "Running": True, "Pid": 0, "ExitCode": 0, "OOMKilled": False, "Error": "",
                       "StartedAt": "2026-10-09T01:19:56.4Z", "FinishedAt": "0001-01-01T00:00:00Z"},
             "Config": {"Labels": dict(lb.split("=", 1) for lb in opt("--label"))},
             "network": (opt("--network") or [""])[0], "mounts": opt("-v"), "logs": []}
    save()
    sys.exit(0)
if cmd in ("inspect", "logs"):
    n = args[-1]
    if n not in cs:
        sys.stderr.write(("Error: No such object: " if cmd == "inspect"
                          else "Error response from daemon: No such container: ") + n + "\n")
        sys.exit(1)
    c = cs[n]
    if cmd == "logs":
        tail = opt("--tail")
        print("\n".join(c["logs"][-int(tail[0]):] if tail else c["logs"]))
    elif "-f" in args:          # peer_test_db.sh: the socket folder's label
        print(c["Config"]["Labels"].get("cseries.gate.socket", ""))
    elif "--format" in args:
        print(c["State"]["Status"])
    else:
        print(json.dumps([{"Id": c.get("Id", ""), "Name": "/" + n, "State": c["State"], "Config": c["Config"]}]))
    sys.exit(0)
if cmd == "events":
    want = [f.split("=", 1)[1] for f in opt("--filter") if f.startswith("container=")]
    for ns, action, sig, code, n in st.get("events", []):
        if n in want:
            print("\t".join((ns, action, sig, code)))
    sys.exit(0)
if cmd == "ps":                 # the reap at start finds nothing
    sys.exit(0)
sys.stderr.write("fake docker: %r is not served\n" % (args,))
sys.exit(1)
'''
FAKE_PSQL = r'''#!/usr/bin/env python3
import json, os, sys
if json.load(open(os.environ["KS_FAKE_DOCKER_STATE"])).get("psql_ok", True):
    print(1)
    sys.exit(0)
host = (sys.argv[1:2] or [""])[0].partition("host=")[2].partition("&")[0]
sys.stderr.write('psql: error: connection to server on socket "%s/.s.PGSQL.5432" failed: Connection refused\n'
                 % host)
sys.exit(2)
'''


@contextlib.contextmanager
def fake_docker():
    d = tempfile.mkdtemp(prefix="ks-explain-")
    state, log = Path(d) / "state.json", Path(d) / "calls.log"
    state.write_text("{}")
    log.write_text("")
    for nm, body in (("docker", FAKE_DOCKER), ("psql", FAKE_PSQL)):
        (Path(d) / nm).write_text(body)
        (Path(d) / nm).chmod(0o755)
    keys = ("PATH", "DOCKER_HOST", "KS_FAKE_DOCKER_STATE", "KS_FAKE_DOCKER_LOG", "CSERIES_GATE_SHARED_DIR")
    saved = {k: os.environ.get(k) for k in keys}
    os.environ.update(PATH=f"{d}:{os.environ.get('PATH') or '/usr/bin:/bin'}",
                      DOCKER_HOST=f"unix://{d}/no-daemon.sock", CSERIES_GATE_SHARED_DIR=d,
                      KS_FAKE_DOCKER_STATE=str(state), KS_FAKE_DOCKER_LOG=str(log))
    try:
        yield state, log
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(d, ignore_errors=True)


def edit(state, fn):
    s = json.loads(state.read_text())
    fn(s)
    state.write_text(json.dumps(s))


SOCKS: dict[str, str] = {}      # each server's socket folder, as its DSN names it


def attempt(name, body):
    """What leaves `with throwaway_server(name)` around `body`, or None."""
    try:
        with ks.throwaway_server(name=name) as dsn:
            from urllib.parse import parse_qsl, urlsplit
            SOCKS[name] = dict(parse_qsl(urlsplit(dsn).query)).get("host", "")
            body(dsn)
    except Exception as e:
        return e
    return None


def stop_as(name, code, oom=False, logs=()):
    def f(s):
        c = s["containers"][name]
        c["State"].update(Status="exited", Running=False, ExitCode=code, OOMKilled=oom,
                          FinishedAt="2026-10-09T01:20:39.063Z")
        c["logs"] = list(logs)
    return f


def raising(err, *changes):
    """A body that makes `changes` to the fake's state, then raises `err`, or
    what `err(socket folder)` makes when it is a function."""
    def body(dsn):
        for ch in changes:
            edit(STATE, ch)
        if isinstance(err, BaseException):
            raise err
        from urllib.parse import parse_qsl, urlsplit
        raise err(dict(parse_qsl(urlsplit(dsn).query))["host"])
    return body


def refused_at(sock, why="Connection refused", lead="psql: error: "):
    return f'{lead}connection to server on socket "{sock}/.s.PGSQL.5432" failed: {why}'


T0 = time.time_ns()
me = str(os.getpid())
UG = f"{os.getuid()}:{os.getgid()}"
with fake_docker() as (STATE, CALLS):
    # C1: the container is removed under the gate.
    n1 = f"ks-explain-{me}-gone"
    orig = []

    def gone(s):
        s["containers"].pop(n1, None)
        s["events"] = [[str(T0), "exec_create: pg_isready -q", "", "", n1], [str(T0 + 10**9), "kill", "9", "", n1],
                       [str(T0 + 11 * 10**8), "die", "", "137", n1], [str(T0 + 15 * 10**8), "destroy", "", "", n1]]

    def schema_failed(sock):
        orig.append(ks.SchemaError("entrypoint.sh's schema section failed (exit 2):\n"
                                   + refused_at(sock, "No such file or directory")))
        return orig[-1]
    e = attempt(n1, raising(schema_failed, gone))
    msg = str(e)
    check("C1 an error under a removed server becomes a SchemaError raised from that error",
          isinstance(e, ks.SchemaError) and orig and e is not orig[0] and e.__cause__ is orig[0], repr(e)[:300])
    check("C1b ...naming the server, that it is gone, and the error it was found after",
          f"the throwaway server {n1} is gone: docker has no such container, found after this error: "
          "SchemaError: entrypoint.sh's schema section failed (exit 2):" in msg, msg[:400])
    check("C1c ...ending on its state: the container does not exist",
          msg.splitlines()[-1].startswith("  state: the container does not exist"), msg[-300:])
    check("C1d ...with the kill and the death docker still holds, and no exec noise",
          "kill signal 9" in msg and "die exit 137" in msg and "destroy" in msg and "exec_create" not in msg, msg)
    check("C1e ...and its socket folder, the one its DSN names, with the socket a killed server leaves there",
          bool(SOCKS.get(n1)) and f"\n  socket folder {SOCKS.get(n1)}: exists, a folder, owner {UG}, mode 0777; "
          f"its socket .s.PGSQL.5432: exists, a socket, owner {UG}, mode " in msg, msg)
    check("C1f ...and no port or TCP address: it has none",
          "host port" not in msg and "127.0.0.1" not in msg and "TIME_WAIT" not in msg, msg)

    # C2: the container exited (OOMKilled) and left 35 log lines.
    n2 = f"ks-explain-{me}-exited"
    e = attempt(n2, raising(ks.SchemaError("ERROR: migration v213_task_run_threads failed"),
                            stop_as(n2, 137, oom=True, logs=[f"log line {i}" for i in range(1, 36)])))
    msg = str(e)
    logged = [ln for ln in msg.splitlines() if ln.startswith("    | ")]
    check("C2 an exited server: not running, with Status, ExitCode, OOMKilled, StartedAt and FinishedAt",
          isinstance(e, ks.SchemaError) and f"the throwaway server {n2} is not running: docker says exited" in msg
          and "state: exists, Status exited, ExitCode 137, OOMKilled true, StartedAt 2026-10-09T01:19:56.4Z, "
              "FinishedAt 2026-10-09T01:20:39.063Z" in msg.splitlines()[-1], msg[-500:])
    check("C2b ...exactly the last 30 lines of its log", logged == [f"    | log line {i}" for i in range(6, 36)],
          logged[:2])
    check("C2c ...and the labels it was started with: this process as its maker, and its socket folder",
          f"cseries.gate.pid={me}" in msg and "cseries.gate.throwaway=kernel-schema" in msg
          and f"cseries.gate.pidns={ks.PIDNS}" in msg and f"cseries.gate.socket={SOCKS.get(n2)}" in msg, msg)
    check("C2d ...and no memory.events line: on the CI runner's docker a container has no cgroup of its own",
          "memory.events" not in msg, msg)

    # C3: a live server: the gate's own error leaves unchanged.
    n3 = f"ks-explain-{me}-alive"
    boom = ValueError("boom")
    e = attempt(n3, raising(boom))
    check("C3 an error while the server still answers leaves exactly as it was",
          e is boom and e.__cause__ is None, repr(e))
    # C3b: running, but psql gets no answer from its socket.
    n3b = f"ks-explain-{me}-refuses"
    e = attempt(n3b, raising(ks.SchemaError("psql: error: ... Connection refused"),
                             lambda s: s.update(psql_ok=False)))
    edit(STATE, lambda s: s.update(psql_ok=True))
    check("C3b a running container whose server does not answer explains itself too",
          isinstance(e, ks.SchemaError)
          and f"the throwaway server {n3b} does not answer: {refused_at(SOCKS.get(n3b))}" in str(e)
          and "Status running" in str(e), str(e)[:400])

    # C3d: its socket refused, yet running and answering again when asked
    # (seen on CI: the containers ran on until the gates' own cleanup).
    n3d = f"ks-explain-{me}-refusedonce"
    orig3d = []

    def refused_once(sock):
        orig3d.append(ks.SchemaError(refused_at(sock)))
        return orig3d[-1]
    e = attempt(n3d, raising(refused_once))
    check("C3d a connection its socket refused explains itself even when the server answers again",
          isinstance(e, ks.SchemaError) and orig3d and e.__cause__ is orig3d[0]
          and f"the throwaway server {n3d} did not take a connection on its socket, and answers again now" in str(e)
          and "state: exists, Status running" in str(e).splitlines()[-1], str(e)[:400])
    n3e = f"ks-explain-{me}-nosocket"
    e = attempt(n3e, raising(lambda sock: ks.SchemaError(refused_at(sock, "No such file or directory"))))
    check("C3e ...and one that found no socket there",
          isinstance(e, ks.SchemaError) and "did not take a connection on its socket" in str(e), repr(e)[:300])
    n3f = f"ks-explain-{me}-otherserver"
    other = ks.SchemaError(refused_at(f"/srv/elsewhere/.ks-kernel-schema-1-{me}/s"))
    e = attempt(n3f, raising(other))
    check("C3f a refusal naming another server's socket leaves exactly as it was (this one still answers)",
          e is other, repr(e)[:300])

    # C8: the socket line, read from real files: a folder, a socket made in
    # it, a missing one, and a symlink named as one, never followed.
    sl = getattr(ks, "_socket_line", None)
    sand = Path(tempfile.mkdtemp(prefix="ks-sockline-"))
    try:
        sdir = sand / "s"
        sdir.mkdir()
        sdir.chmod(0o777)
        empty = sl(sdir) if callable(sl) else "(no _socket_line)"
        with contextlib.closing(socket.socket(socket.AF_UNIX)) as lsn:
            here = os.getcwd()      # bound by name from inside sdir: see plant() on TMPDIR's length
            os.chdir(sdir)
            try:
                lsn.bind(".s.PGSQL.5432")
            finally:
                os.chdir(here)
            lsn.listen(1)
            smode = f"{os.lstat(sdir / '.s.PGSQL.5432').st_mode & 0o7777:04o}"
            there = sl(sdir) if callable(sl) else ""
        (sand / "link").symlink_to(sdir)
        linked = sl(sand / "link") if callable(sl) else ""
        nowhere = sl(sand / "never-made") if callable(sl) else ""
    finally:
        shutil.rmtree(sand, ignore_errors=True)
    check("C8 the socket line names a folder with its owner and mode, and a socket missing from it",
          empty == f"  socket folder {sdir}: exists, a folder, owner {UG}, mode 0777; its socket .s.PGSQL.5432: missing",
          empty)
    check("C8b ...and a socket in it, with its owner and mode",
          there == f"  socket folder {sdir}: exists, a folder, owner {UG}, mode 0777; its socket .s.PGSQL.5432: "
                   f"exists, a socket, owner {UG}, mode {smode}", there)
    check("C8c ...a symlink as a symlink, and a folder never made as missing",
          linked.startswith(f"  socket folder {sand / 'link'}: exists, a symlink, owner {UG}, mode ")
          and nowhere == f"  socket folder {sand / 'never-made'}: missing; its socket .s.PGSQL.5432: missing",
          (linked, nowhere))

    # C4: the gate ends normally after its server went: still reported.
    n4 = f"ks-explain-{me}-endgone"
    e = attempt(n4, lambda _dsn: edit(STATE, lambda s: s["containers"].pop(n4, None)))
    check("C4 a gate that ends normally after its server went raises with the state",
          isinstance(e, ks.SchemaError) and "is gone" in str(e) and "found when the gate was done with it" in str(e)
          and str(e).splitlines()[-1].startswith("  state: the container does not exist"), repr(e)[:300])

    # C5: a normal end on a live server costs one docker inspect.
    n5 = f"ks-explain-{me}-cheap"
    mark = []
    e = attempt(n5, lambda _dsn: mark.append(len(CALLS.read_text().splitlines())))
    after = [json.loads(ln) for ln in CALLS.read_text().splitlines()[mark[0]:]] if mark else []
    check("C5 a normal end on a live server raises nothing and asks docker once (inspect), then stops it "
          "(peer_test_db.sh --stop: its socket label, then rm)",
          e is None and after == [["inspect", "--format", "{{.State.Status}}", n5],
                                  ["inspect", "-f", '{{index .Config.Labels "cseries.gate.socket"}}', n5],
                                  ["rm", "-f", n5]], (e, after))

    # C6: docker hangs on logs and refuses events: each becomes a line, in its
    # time limit, and the SchemaError still comes from the gate's error.
    n6 = f"ks-explain-{me}-dockerdown"
    orig6 = ks.SchemaError("could not create a database on the test server: psql: error")
    timeout_was = getattr(ks, "DOCKER_TIMEOUT", None)
    ks.DOCKER_TIMEOUT = 1.0
    t = time.monotonic()
    try:
        e = attempt(n6, raising(orig6, stop_as(n6, 0), lambda s: s.update(hang=["logs"], refuse=["events"])))
    finally:
        ks.DOCKER_TIMEOUT = timeout_was
        edit(STATE, lambda s: s.update(hang=[], refuse=[]))
    took, msg = time.monotonic() - t, str(e)
    check("C6 docker hanging or refusing never replaces the error: each question is a line, in its time limit",
          isinstance(e, ks.SchemaError) and e.__cause__ is orig6 and "docker did not answer in 1s" in msg
          and "docker events: Cannot connect to the Docker daemon" in msg and "Status exited, ExitCode 0" in msg
          and f"socket folder {SOCKS.get(n6)}: exists" in msg and took < 20, (round(took, 1), msg[:600]))

    # C9: an error whose own text cannot be read (its __str__ raises) is never
    # replaced by that failure: kept as it was on a live server, raised from
    # on a removed one.
    class Unreadable(Exception):
        def __str__(self):
            raise AttributeError("no detail was set")
    unreadable = Unreadable()
    e = attempt(f"ks-explain-{me}-unreadable", raising(unreadable))
    check("C9 an error whose text cannot be read leaves exactly as it was under a live server",
          e is unreadable, repr(type(e)))
    n9 = f"ks-explain-{me}-unreadable-gone"
    unreadable = Unreadable()
    e = attempt(n9, raising(unreadable, lambda s: s["containers"].pop(n9, None)))
    check("C9b ...and under a removed server becomes a SchemaError raised from it",
          isinstance(e, ks.SchemaError) and e.__cause__ is unreadable
          and f"the throwaway server {n9} is gone" in str(e), repr(type(e)))

    # C10: docker hangs on the removal at the end. The cleanup gives up in its
    # time limit with one line naming the server it leaves (the reaper removes
    # it once this process is gone), and the gate's own error, or its normal
    # end and result, leaves exactly as it was. The hung fake removal is
    # released after each attempt and finishes before the next check.
    timeout_was = getattr(ks, "DOCKER_TIMEOUT", None)
    ks.DOCKER_TIMEOUT = 1.0
    try:
        for how, n10, err in (("error", f"ks-explain-{me}-stophangs", ValueError("the gate's own error")),
                              ("normal end", f"ks-explain-{me}-stophangs-ok", None)):
            release, result = STATE.parent / f"release-{n10}", []

            def body(_dsn, err=err, release=release, result=result):
                edit(STATE, lambda s: s.update(hang=["rm"], release=str(release)))
                if err is not None:
                    raise err
                result.append("the gate's result")
            buf = io.StringIO()
            with contextlib.redirect_stderr(buf):
                e = attempt(n10, body)
            back = time.time()
            release.write_text("")
            done, deadline = Path(f"{release}.done"), time.monotonic() + 20
            while not done.exists() and time.monotonic() < deadline:
                time.sleep(0.05)
            edit(STATE, lambda s: s.update(hang=[], release=""))
            at = Path(f"{release}.at")
            took = back - float(at.read_text()) if at.exists() else -1.0
            left = [ln for ln in buf.getvalue().splitlines() if ln.startswith("left the throwaway server")]
            check(f"C10 ({how}) docker hanging on the removal: the gate's {'error' if err else 'result'} leaves as it was",
                  e is err and (err is not None or result == ["the gate's result"]), (repr(e), result))
            check(f"C10b ({how}) ...the cleanup returns within its time limit (1 s here; measured {took:.1f}s)",
                  0 <= took < 5, took)
            check(f"C10c ({how}) ...with one line naming the server it leaves behind",
                  len(left) == 1 and f"left the throwaway server {n10} behind" in left[0]
                  and "did not finish in 1s" in left[0], buf.getvalue()[-400:])
    finally:
        ks.DOCKER_TIMEOUT = timeout_was
        edit(STATE, lambda s: s.update(hang=[], release=""))

    # C7: a start that fails says so, with what docker and the socket folder say.
    n7 = f"ks-explain-{me}-nostart"
    edit(STATE, lambda s: s.update(run_fails="docker: Error response from daemon: failed to create task: "
                                             "mount source path is not a directory"))
    e = attempt(n7, lambda _dsn: None)
    edit(STATE, lambda s: s.update(run_fails=""))
    msg = str(e)
    check("C7 a start that fails says so, with docker's own error, its socket folder and its state",
          isinstance(e, ks.SchemaError) and "did not start" in msg and "mount source path is not a directory" in msg
          and f"What docker says about {n7}:" in msg and f".ks-{n7}/s: exists, a folder" in msg
          and "its socket .s.PGSQL.5432: missing" in msg
          and msg.splitlines()[-1].startswith("  state: the container does not exist"), msg[:900])

# ── E. peer_test_db.sh acts only on a socket folder made for its server ───
# A folder it was not made for, as a live server's own is (its socket
# and lock file in it, mode 0755), given as PEER_TEST_PG_SOCKET_DIR or named by
# a container's label, is never emptied, removed, opened up or mounted. With
# section C's fake docker and psql.
HELPER = str(ROOT / "scripts" / "peer_test_db.sh")


def snapshot(d: Path) -> tuple:
    """What would show the folder `d` was touched: its mode and owner, and each
    entry's name, mode and (for a file) content."""
    if not d.exists():
        return ("(gone)",)
    out = [(oct(d.lstat().st_mode), d.lstat().st_uid)]
    for p in sorted(d.iterdir()):
        out.append((p.name, oct(p.lstat().st_mode), p.read_text() if p.is_file() else None))
    return tuple(out)


def marker_of(d: Path) -> str | None:
    """What the marker in the folder `d` says, or None when it has none."""
    try:
        return (d / ".cseries-gate-server").read_text()
    except OSError:
        return None


def plant(d: Path, mode: int = 0o755) -> Path:
    """A folder as a live server's socket folder is: its socket, and its lock
    file naming its postmaster. The socket is bound from inside `d`, by its
    name alone: a unix socket address holds 107 bytes, and on the CI runner
    TMPDIR is <job>/tmp/tmp.XXXXXXXXXX/scratch (67 bytes), which put the full
    path over it (OSError: AF_UNIX path too long)."""
    d.mkdir(parents=True)
    here = os.getcwd()
    os.chdir(d)
    try:
        with contextlib.closing(socket.socket(socket.AF_UNIX)) as s:
            s.bind(".s.PGSQL.5432")
    finally:
        os.chdir(here)
    (d / ".s.PGSQL.5432.lock").write_text("4242\n/var/lib/postgresql/data\n")
    d.chmod(mode)
    return d


with fake_docker() as (STATE, CALLS):
    # Under the shorter of TMPDIR and /tmp: peer_test_db.sh refuses a socket
    # path over 107 bytes, and on the CI runner TMPDIR alone is 67 (see
    # plant()). Only the fake docker sees this folder, so the runner's private
    # /tmp does here; the `finally` below removes it.
    yard = Path(tempfile.mkdtemp(prefix="ksf-", dir=min((tempfile.gettempdir(), "/tmp"), key=len)))

    def helper(name, *args, sock=None):
        env = {**os.environ, "PEER_TEST_PG_NAME": name, "PEER_TEST_PG_IMAGE": "fake-image", "TMPDIR": str(yard)}
        env.pop("PEER_TEST_PG_SOCKET_DIR", None)
        if sock is not None:
            env["PEER_TEST_PG_SOCKET_DIR"] = str(sock)
        mark = len(CALLS.read_text().splitlines())
        r = subprocess.run(["bash", HELPER, *args], env=env, capture_output=True, text=True, timeout=120)
        return r, [json.loads(ln) for ln in CALLS.read_text().splitlines()[mark:]]
    try:
        live = plant(yard / "run-postgresql")           # a live server's own folder, as /var/run/postgresql is
        unmarked = plant(yard / "unmarked" / "s")      # named s, in a 0700 folder of this user's, no marker
        (yard / "unmarked").chmod(0o700)
        others = plant(yard / "others" / "s")          # in a folder made for another server
        (yard / "others" / ".cseries-gate-server").write_text("some-other-server\n")
        (yard / "others").chmod(0o700)
        n = f"ks-foreign-{me}"
        for what, d in (("a live server's own folder", live), ("a folder named s with no marker", unmarked),
                        ("a folder made for another server", others)):
            before = (snapshot(d), snapshot(d.parent))
            r, calls = helper(n, sock=d)
            check(f"E1 ({what}) as PEER_TEST_PG_SOCKET_DIR: peer_test_db.sh refuses it and mounts nothing",
                  r.returncode != 0 and f"refusing the socket folder {d}" in r.stderr
                  and not any(c[:1] == ["run"] for c in calls), (r.returncode, r.stderr[-300:], calls))
            r, calls = helper(n, "--stop", sock=d)
            check(f"E2 ({what}) ...and --stop leaves it as it is", r.returncode == 0
                  and f"left {d} as it is" in r.stderr, (r.returncode, r.stderr[-300:]))
            check(f"E3 ({what}) ...its socket, lock file and mode untouched, and the folder holding it",
                  (snapshot(d), snapshot(d.parent)) == before, (before, snapshot(d)))

        # A container whose label names a folder not made for it.
        before = snapshot(live)
        edit(STATE, lambda s: s["containers"].update({f"{n}-labelled": {
            "State": {"Status": "running"}, "Config": {"Labels": {"cseries.gate.socket": str(live)}}, "logs": []}}))
        r, calls = helper(f"{n}-labelled", "--stop")
        check("E4 a container whose label names a folder not made for it is removed, and the folder left as it is",
              ["rm", "-f", f"{n}-labelled"] in calls and snapshot(live) == before
              and f"left {live} as it is" in r.stderr, (calls, r.stderr[-300:]))

        # Its own folders: made with the marker, used, and removed by --stop.
        r, calls = helper(f"{n}-own")
        own = Path(r.stdout.partition("host=")[2].partition("&")[0])
        made = own.parent.is_dir() and marker_of(own.parent) == f"{n}-own\n"
        r2, _ = helper(f"{n}-own", "--stop")
        check("E5 a folder of its own (PEER_TEST_PG_SOCKET_DIR unset) is made with the marker, mounted, and "
              "removed by --stop", r.returncode == 0 and made and own.name == "s"
              and any(c[:1] == ["run"] and f"{own}:/var/run/postgresql" in c for c in calls)
              and not own.parent.exists(), (r.returncode, r.stderr[-300:], own, r2.stderr[-200:]))
        given = yard / "made-here" / "s"
        r, calls = helper(f"{n}-given", sock=given)
        made = marker_of(given.parent) == f"{n}-given\n" and oct(given.parent.stat().st_mode)[-3:] == "700"
        r2, _ = helper(f"{n}-given", "--stop", sock=given)
        check("E6 a PEER_TEST_PG_SOCKET_DIR whose folder is missing is made here, for this server (0700, the "
              "marker), and removed by --stop", r.returncode == 0 and made and not given.parent.exists(),
              (r.returncode, r.stderr[-300:], r2.stderr[-200:]))

        # A symlink named s, in a marked owner-only folder of this user's own,
        # pointing at a live server's folder, given with a / at its end: -L
        # follows a symlink there, so this passed every check, and the live
        # folder lost its socket and lock file, went 0777 and was mounted.
        n7 = f"{n}-slash"
        behind = plant(yard / "behind-the-link")
        linked = yard / "linked"
        linked.mkdir(mode=0o700)
        (linked / ".cseries-gate-server").write_text(f"{n7}\n")
        (linked / "s").symlink_to(behind)
        slashed = f"{linked}/s/"
        before = (snapshot(behind), snapshot(linked), os.readlink(linked / "s"))
        r, calls = helper(n7, "--stop", sock=slashed)
        check("E7 a symlink s in a marked folder of its own, given as .../s/: --stop leaves the folder it points "
              "at as it is", r.returncode == 0 and f"left {slashed} as it is" in r.stderr
              and snapshot(behind) == before[0], (r.returncode, r.stderr[-300:], before[0], snapshot(behind)))
        r, calls = helper(n7, sock=slashed)
        check("E7b ...and a start refuses it and mounts nothing", r.returncode != 0
              and f"refusing the socket folder {slashed}" in r.stderr and not any(c[:1] == ["run"] for c in calls),
              (r.returncode, r.stderr[-300:], calls))
        check("E7c ...its socket, lock file and mode untouched, and the symlink and the marked folder",
              (snapshot(behind), snapshot(linked), os.readlink(linked / "s")) == before, (before, snapshot(behind)))
        # //, /./ and /../, each on a fresh marked folder whose s is not a
        # symlink, so that nothing but the spelling refuses them: nothing is
        # made or mounted.
        plains = [yard / f"plain-{i}" for i in range(3)]
        for plain in plains:
            plain.mkdir(mode=0o700)
            (plain / ".cseries-gate-server").write_text(f"{n7}\n")
        for plain, spelled in zip(plains, (f"{plains[0]}//s", f"{plains[1]}/./s",
                                           f"{linked}/../{plains[2].name}/s")):
            r, calls = helper(n7, sock=spelled)
            left = sorted(p.name for p in plain.iterdir()) if plain.is_dir() else None
            check(f"E7d {spelled[len(str(yard)):]} is refused, and nothing is made or mounted", r.returncode != 0
                  and f"refusing the socket folder {spelled}" in r.stderr and not any(c[:1] == ["run"] for c in calls)
                  and left == [".cseries-gate-server"], (r.returncode, r.stderr[-300:], calls, left))
    finally:
        shutil.rmtree(yard, ignore_errors=True)

# ── F. the reap removes the folder a gone gate's label names, wherever it is ─
# With gate_reap_fakes' docker; the maker pids are above the kernel's
# pid limit, so no process has them and kill(pid, 0) finds none.
from gate_reap_fakes import FakeDocker, maker  # noqa: E402

fdir = Path(tempfile.mkdtemp(prefix="ks-reaplabel-"))
try:
    fake = FakeDocker(fdir / "docker")
    gone_pid, ns, boot = 5_000_201, str(ks.PIDNS), getattr(ks, "BOOT_ID", "")
    n_else, n_foreign, n_alive = f"ks-f-{me}-elsewhere", f"ks-f-{me}-foreign", f"ks-f-{me}-alive"
    elsewhere = plant(fdir / "another-parent" / f".ks-{n_else}" / "s", 0o777).parent
    foreign = plant(fdir / "foreign" / "s")             # labelled, but not a folder named .ks-<name>
    alive = plant(fdir / "alive-parent" / f".ks-{n_alive}" / "s", 0o777).parent
    fake.load({
        n_else: {"labels": {**maker(ks.THROWAWAY_KIND, gone_pid, ns, "500", boot),
                            "cseries.gate.socket": str(elsewhere / "s")}},
        n_foreign: {"labels": {**maker(ks.THROWAWAY_KIND, gone_pid, ns, "500", boot),
                               "cseries.gate.socket": str(foreign)}},
        n_alive: {"labels": {**ks.maker_labels(ks.THROWAWAY_KIND), "cseries.gate.socket": str(alive / "s")}},
    })
    before = snapshot(foreign)
    with fake.in_process(), contextlib.redirect_stderr(io.StringIO()):
        got = ks.reap_throwaway_leftovers()
    check("F1 a gone gate's container is reaped with the folder its label names, under a parent this gate "
          "does not look in", n_else in got and n_else in fake.removed() and not elsewhere.exists(), (got, elsewhere))
    check("F2 a label naming a folder throwaway_server does not make (not .ks-<name>) costs only the container",
          n_foreign in fake.removed() and snapshot(foreign) == before, (fake.removed(), snapshot(foreign)))
    check("F3 a live gate's container and folder are kept", n_alive not in fake.removed() and alive.is_dir(),
          fake.removed())
finally:
    shutil.rmtree(fdir, ignore_errors=True)

# ── B. live ───────────────────────────────────────────────────────────────
if subprocess.run(["docker", "version"], capture_output=True).returncode != 0:
    print("docker is not reachable: the live half cannot run (exit 2)")
    sys.exit(2 if not FAILS else 1)
# The database image: PEER_TEST_PG_IMAGE (smoke passes its own), else the tag
# peer_test_db.sh defaults to, else the content-hash tag
# test_restore_carry_real_schema.py builds from this tree's Dockerfile.postgres
# (the same tag, so the two gates share one image), built here when absent.
# A missing image is never a skip: this gate runs in pre_merge_gates.sh and
# smoke, and a skip there would read as a pass.
_PG_FILES = ("Dockerfile.postgres", "entrypoint-postgres.sh", "LICENSE", "NOTICE",
             "THIRD-PARTY-NOTICES.md")
_carry = "cseries-postgres-carry:" + hashlib.sha256(
    b"".join((ROOT / f).read_bytes() for f in _PG_FILES)).hexdigest()[:12]
_have = lambda img: subprocess.run(["docker", "image", "inspect", img],  # noqa: E731
                                   capture_output=True).returncode == 0
_image = os.environ.get("PEER_TEST_PG_IMAGE") or next(
    (i for i in ("cseries-postgres:smoke", _carry) if _have(i)), "")
if not _image:
    _image = _carry
    with tempfile.TemporaryDirectory(prefix="reap-pgctx-") as ctx:
        for f in _PG_FILES:
            shutil.copy(ROOT / f, Path(ctx) / f)
        print(f"building {_image} from Dockerfile.postgres")
        b = subprocess.run(["docker", "build", "-q", "-f", str(Path(ctx) / "Dockerfile.postgres"),
                            "-t", _image, ctx], capture_output=True, text=True)
    if b.returncode != 0:
        print(f"FAIL: the database image did not build: {b.stderr.strip()[-400:]}")
        sys.exit(1)
elif not _have(_image):
    print(f"FAIL: PEER_TEST_PG_IMAGE={_image} is not here")
    sys.exit(1)
os.environ["PEER_TEST_PG_IMAGE"] = _image      # the gates below inherit it

CHILD = f"""
import sys
sys.path.insert(0, {str(ROOT / 'scripts')!r})
import kernel_schema as ks
with ks.throwaway_server(name=sys.argv[1]) as dsn:
    print("UP", dsn, flush=True)
    sys.stdin.read()          # until the parent closes stdin
"""

tag = f"ks-reap-{os.getpid()}"
names = {k: f"{tag}-{k}" for k in "abcd"}
procs: dict[str, subprocess.Popen] = {}
dsns: dict[str, str] = {}
# Gate D makes its folder under a parent no other gate here looks in:
# next to this checkout, where the docker daemon sees it, as another checkout's
# or another CSERIES_GATE_SHARED_DIR's would be.
ELSEWHERE = ROOT.parent / f".ksr-{os.getpid()}"


def start(k, env=None):
    procs[k] = subprocess.Popen([sys.executable, "-c", CHILD, names[k]], stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                env={**os.environ, **(env or {})})


def wait_up(k):
    line = procs[k].stdout.readline()
    if not line.startswith("UP"):
        raise SystemExit(f"gate {k} never came up: {line!r} {procs[k].stderr.read()[-800:]}")
    dsns[k] = line.split(None, 1)[1].strip()


def running(name):
    r = subprocess.run(["docker", "ps", "-a", "--filter", f"name=^{name}$", "--format", "{{.Names}}"],
                       capture_output=True, text=True)
    return name in r.stdout.split()


def folder(k) -> Path:
    """The folder gate k's server was made in: the one holding its socket
    folder (a path that is not there when its DSN names no socket folder)."""
    from urllib.parse import parse_qsl, urlsplit
    host = dict(parse_qsl(urlsplit(dsns[k]).query)).get("host", "")
    return Path(host).parent if host.startswith("/") else Path("/nonexistent/no-socket-folder")


def posture(k) -> tuple:
    """(published ports, network mode, bind mounts as (source, target), the DSN's
    host and its TCP port) of gate k's server."""
    import json
    from urllib.parse import parse_qsl, urlsplit
    c = json.loads(subprocess.run(["docker", "inspect", names[k]], capture_output=True, text=True).stdout)[0]
    u = urlsplit(dsns[k])
    return (c["HostConfig"].get("PortBindings") or {}, c["HostConfig"].get("NetworkMode"),
            [(m["Source"], m["Destination"]) for m in c.get("Mounts") or []],
            dict(parse_qsl(u.query)).get("host"), u.hostname, u.port)


try:
    start("a")
    start("b")
    wait_up("a")
    wait_up("b")
    check("B0 both gates' containers are up", running(names["a"]) and running(names["b"]))
    _p = posture("a")
    check("B0b a server publishes no TCP port, has no network, and its DSN names no TCP host or port, "
          "only its own socket folder, bind-mounted as the container's /var/run/postgresql",
          _p[0] == {} and _p[1] == "none" and _p[4] is None and _p[5] is None
          and _p[2] == [(_p[3], "/var/run/postgresql")] and folder("a") != folder("b"), _p)
    _r = subprocess.run(["psql", dsns["a"], "-XAtc", "SELECT inet_server_addr() IS NULL"],
                        capture_output=True, text=True)
    check("B0c ...and a connection to it is a unix socket one", _r.stdout.strip() == "t", _r.stderr[-300:])
    ELSEWHERE.mkdir()
    start("d", {"CSERIES_GATE_SHARED_DIR": str(ELSEWHERE)})
    wait_up("d")
    for k in "ad":
        procs[k].send_signal(signal.SIGKILL)
        procs[k].wait()                        # reaped, as hang_watch reaps it
    check("B1 SIGKILL skips the cleanup: gate A's container is still there", running(names["a"]))
    check("B1b ...and its socket folder", folder("a").is_dir(), folder("a"))
    check("B1c gate D's folder is under the other parent, and still there",
          folder("d").parent == ELSEWHERE and folder("d").is_dir() and running(names["d"]), folder("d"))
    start("c")
    wait_up("c")
    check("B2 the next gate reaps the container its dead creator left", not running(names["a"]))
    check("B2b ...and the socket folder with it", not folder("a").exists(), folder("a"))
    check("B2c ...and a folder made under a parent it does not look in, found from the container's "
          "socket label", not running(names["d"]) and not folder("d").exists(), folder("d"))
    check("B3 ...and leaves the container of a gate that is still running", running(names["b"]))
    check("B3b ...and its socket folder", folder("b").is_dir(), folder("b"))
    for k in "bc":
        procs[k].stdin.close()
        procs[k].wait(timeout=120)
    check("B4 gates that end normally remove their own", not running(names["b"]) and not running(names["c"]))
    check("B4b ...and their socket folders", not folder("b").exists() and not folder("c").exists(),
          (folder("b"), folder("c")))
finally:
    for p in procs.values():
        if p.poll() is None:
            p.kill()
            p.wait()
    for n in names.values():
        subprocess.run(["docker", "rm", "-f", n], capture_output=True)
    shutil.rmtree(ELSEWHERE, ignore_errors=True)

# ── D. live: a server stopped under a kernel_schema step explains itself ─
# This process's own throwaway server, found by its maker labels, is stopped
# with `docker stop` inside the gate; the next kernel_schema step fails, and
# what the SchemaError says is compared with what docker itself recorded.
sd = getattr(ks, "server_down", None)
check("D0 real docker's answer for a container that does not exist reads as gone",
      callable(sd) and sd(f"{tag}-absent") == "is gone: docker has no such container")
mine = ["docker", "ps", "-aq", "--filter", f"label=cseries.gate.pid={os.getpid()}",
        "--filter", "label=cseries.gate.throwaway=kernel-schema"]
seen: dict[str, str] = {}
err, left = None, ["(not looked at)"]
try:
    with ks.throwaway_server() as dsn:
        from urllib.parse import parse_qsl, urlsplit
        seen["sock"] = dict(parse_qsl(urlsplit(dsn).query)).get("host", "")
        cid = subprocess.run(mine, capture_output=True, text=True).stdout.split()
        seen["id"] = cid[0] if len(cid) == 1 else ""
        seen["name"] = subprocess.run(["docker", "inspect", "-f", "{{.Name}}", seen["id"]], capture_output=True,
                                      text=True).stdout.strip().lstrip("/")
        subprocess.run(["docker", "stop", "-t", "30", seen["id"]], capture_output=True, timeout=120)
        seen["code"] = subprocess.run(["docker", "inspect", "-f", "{{.State.ExitCode}}", seen["id"]],
                                      capture_output=True, text=True).stdout.strip()
        with ks.kernel_schema_db(dsn):
            pass
except Exception as e:
    err = e
finally:
    if seen.get("id"):
        left = subprocess.run(mine, capture_output=True, text=True).stdout.split()
        subprocess.run(["docker", "rm", "-f", seen["id"]], capture_output=True)
msg = str(err)
check("D1 a step under a stopped server raises a SchemaError from the step's own error",
      isinstance(err, ks.SchemaError) and isinstance(err.__cause__, ks.SchemaError) and bool(seen.get("name")),
      repr(err)[:400])
check("D2 ...with the Status and ExitCode docker recorded",
      f"the throwaway server {seen.get('name')} is not running: docker says exited" in msg
      and f"state: exists, Status exited, ExitCode {seen.get('code')}, OOMKilled false" in msg, msg[-600:])
check("D3 ...its labels, naming this process as its maker and the socket folder its DSN names",
      f"cseries.gate.pid={os.getpid()}" in msg and "cseries.gate.throwaway=kernel-schema" in msg
      and bool(seen.get("sock")) and f"cseries.gate.socket={seen.get('sock')} " in msg, msg[-600:])
check("D4 ...its log tail and the stop docker recorded",
      "\n    | " in msg and " stop" in msg.split("events docker still holds for it")[-1].splitlines()[0], msg)
check("D5 ...and that socket folder, there with its owner and mode, and whether its socket is",
      bool(seen.get("sock")) and f"\n  socket folder {seen.get('sock')}: exists, a folder, owner " in msg
      and "mode 0777; its socket .s.PGSQL.5432: " in msg, msg[-900:])
check("D6 the gate's own cleanup still removed it, and its socket folder",
      seen.get("id") and not left and bool(seen.get("sock")) and not Path(seen["sock"]).exists(), seen)

if FAILS:
    print(f"\n{len(FAILS)} FAIL")
    sys.exit(1)
print("\nall pass")
