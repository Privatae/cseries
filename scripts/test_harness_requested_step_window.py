#!/usr/bin/env python3
"""A tool step that asks for a long timeout gets it; opencode's running steps
are seen; a spinning or idle step still stops.

A step the model asked for through the CLI's own tool contract is legitimate
work, so its window is max(HARNESS_TOOL_STEP_MINUTES, min(request,
HARNESS_TOOL_STEP_MAX_MINUTES) + margin), still judged by CPU and I/O
liveness within it. Where the request is seen: claude-code's Bash call (the
SDK shows it before the result); opencode's running step, from opencode's own
database, this attempt's session only (it prints a tool only when finished);
grok prints no tool events at all, so its running command is seen as a live
child and held to grok's own 10 h maximum. Also pinned: a stale
row from a killed run, a huge or malformed request, a raising check, the
single-run fallback's edges, a claude-code subagent, and the audit baseline.

Scaled down: tool-step window 3 s, margin 1 s, idle window 1.2 s.

Run: APP_ROOT=<repo> python3 scripts/test_harness_requested_step_window.py
"""
import dataclasses
import json
import os
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

FAILS = []


def check(name: str, ok: bool, detail: str = ""):
    tag = "ok  " if ok else "FAIL"
    print(f"[step-window] {tag} {name}" + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILS.append(name)


class Req:
    def __init__(self, body: dict):
        self.body = json.dumps(body).encode()


def main() -> int:
    app_root = os.environ.get("APP_ROOT", "/app")
    sys.path.insert(0, os.path.join(app_root, "cli"))
    sys.path.insert(0, os.path.join(app_root, "system-apps"))
    from _harness_runtime import harness_core as hc
    from _harness_runtime import specs
    from _harness_runtime import events

    print("== the timeout a running call asks for, where a CLI shows one ==")
    rs = getattr(hc, "requested_step_seconds", None)
    check("claude-code's Bash timeout is milliseconds",
          rs is not None and rs("claude-code", {"timeout": 600_000}) == 600)
    check("opencode's shell timeout is milliseconds",
          rs is not None and rs("opencode", {"timeout": 9_000}) == 9)
    check("grok prints no tool events, so no request is read from one",
          rs is not None and rs("grok", {"timeout": 36_000_000}) == 0)
    hc.TOOL_STEP_MAX_MINUTES = 600
    check("a request is capped at HARNESS_TOOL_STEP_MAX_MINUTES (10 h)",
          rs is not None and rs("opencode", {"timeout": 10 ** 12}) == 36_000,
          str(rs and rs("opencode", {"timeout": 10 ** 12})))
    check("...and a NaN or non-number is ignored",
          rs is not None and rs("opencode", {"timeout": float("nan")}) == 0
          and rs("opencode", {"timeout": "9000"}) == 0)
    hc.TOOL_STEP_MAX_MINUTES = 0
    check("an infinite request is ignored, even with the ceiling off",
          rs is not None and rs("opencode", {"timeout": float("inf")}) == 0,
          str(rs and rs("opencode", {"timeout": float("inf")})))
    try:
        big = rs("opencode", {"timeout": 10 ** 400}) if rs else None
    except Exception as e:  # noqa: BLE001
        big = repr(e)
    check("an integer too large for a float is ignored, not raised", big == 0, str(big))
    hc.TOOL_STEP_MAX_MINUTES = 600

    tmp = Path(tempfile.mkdtemp(prefix="stepwin-"))
    py = sys.executable

    def _parse(harness_id, line, seq, state=None):
        raw = (line or "").strip()
        if raw.startswith("CALL "):
            return [{"ts": time.time(), "seq": seq, "kind": "tool_call",
                     "payload": {"tool": "bash", "args": json.loads(raw[5:])}}]
        kinds = {"TOOL_RESULT": "tool_result", "TEXT": "text"}
        if raw in kinds:
            return [{"ts": time.time(), "seq": seq, "kind": kinds[raw], "payload": {"text": raw}}]
        return [{"ts": time.time(), "seq": seq, "kind": "text",
                 "payload": {"text": raw, "raw": True}}]
    real_normalize = events.normalize_line
    events.normalize_line = _parse

    KM = specs.ALL_SPECS["kimi"]
    hc._BARE_KEY_BASE_CACHE.clear()
    hc._BARE_KEY_BASE_PROBE = lambda b, k: b == KM.bare_key_base_url
    kp = tmp / "kp"
    kp.write_text("sk-moonshot-1")
    hc.IDLE_FLOOR_S = 0.5
    hc.ALIVE_FRAME_S = 0.6
    hc.TOOL_STEP_MINUTES = 0.05            # 3 s
    hc.TOOL_STEP_MARGIN_MINUTES = 1 / 60   # 1 s
    hc.PROGRESS_MINUTES = 0.05

    def run(name, script, spec_id="kimi"):
        stub = tmp / name
        stub.write_text("#!/bin/sh\n" + script)
        stub.chmod(0o755)
        spec = dataclasses.replace(KM, id=spec_id, binary=stub, creds_path=kp)
        proj = tmp / f"proj-{name}"
        proj.mkdir(exist_ok=True)
        body = {"prompt": "x", "project_dir": str(proj), "model": "k3",
                "idle_minutes": 0.02, "max_minutes": 0.02}
        t0 = time.time()
        fr = list(hc.make_run_stream(spec)(Req(body)))
        st = [f for f in fr if f.get("kind") == "status"]
        return (st[-1]["payload"] if st else {}), time.time() - t0

    spin = f"{py} -c 'import time\nt=time.time()\nwhile time.time()-t<{{s}}: pass'"

    print("== opencode: a running step is read from opencode's own database ==")
    data = tmp / "xdg"
    (data / "opencode").mkdir(parents=True)
    dbp = data / "opencode" / "opencode.db"
    con = sqlite3.connect(dbp)
    con.execute("CREATE TABLE part (id TEXT PRIMARY KEY, message_id TEXT, "
                "session_id TEXT, time_created INTEGER, time_updated INTEGER, data TEXT)")
    con.commit()
    con.close()
    os.environ["XDG_DATA_HOME"] = str(data)
    orf = getattr(hc, "opencode_running_tools", None)
    check("no running step in an empty database",
          orf is not None and orf({"XDG_DATA_HOME": str(data)}, "ses_A", time.time() - 60) == [])

    def _put(pid, sid, state):
        c = sqlite3.connect(dbp)
        c.execute("INSERT OR REPLACE INTO part VALUES (?,?,?,?,?,?)",
                  (pid, "m", sid, 0, 0, json.dumps({"type": "tool", "tool": "bash",
                                                    "state": state})))
        c.commit()
        c.close()

    def _clear():
        c = sqlite3.connect(dbp)
        c.execute("DELETE FROM part")
        c.commit()
        c.close()
    env_oc = {"XDG_DATA_HOME": str(data)}
    now = time.time()
    _put("old", "ses_OLD", {"status": "running", "input": {"command": "npm test"},
                            "time": {"start": int((now - 3 * 3600) * 1000)}})
    check("a row left running by an earlier, killed run is not this run's step",
          orf(env_oc, None, now) == [] and orf(env_oc, "ses_NEW", now) == []
          and orf(env_oc, "ses_OLD", now) == [])
    _put("bad1", "ses_A", {"status": "running", "input": {"timeout": 9000}, "time": "x"})
    _put("bad2", "ses_A", {"status": "running", "input": {"timeout": 9000},
                           "time": {"start": "soon"}})
    _put("bad3", "ses_A", "not a dict")
    _put("fut", "ses_A", {"status": "running", "input": {"timeout": 9000},
                          "time": {"start": int((now + 3600) * 1000)}})
    try:
        got = orf(env_oc, "ses_A", now - 60)
        ok = got == []
    except Exception as e:  # noqa: BLE001
        got, ok = repr(e), False
    check("malformed rows and a start in the future are skipped, never raised",
          ok, str(got))
    _clear()
    _put("huge", "ses_A", {"status": "running", "input": {"timeout": 10 ** 400},
                           "time": {"start": int(time.time() * 1000)}})
    try:
        got = orf(env_oc, "ses_A", time.time() - 60)
        ok = isinstance(got, list)
    except Exception as e:  # noqa: BLE001
        got, ok = repr(e), False
    check("a row whose timeout is too large for a float is read without raising",
          ok, str(got))
    _clear()
    _put("hugestart", "ses_A", {"status": "running", "input": {"timeout": 9000},
                                "time": {"start": 10 ** 400}})
    try:
        got = orf(env_oc, "ses_A", time.time() - 60)
        ok = got == []
    except Exception as e:  # noqa: BLE001
        got, ok = repr(e), False
    check("a row whose start is too large for a float is skipped, never raised",
          ok, str(got))
    _clear()
    _put("other", "ses_OTHER", {"status": "running", "input": {"timeout": 9000},
                                "time": {"start": int(time.time() * 1000)}})
    check("a running row of ANOTHER session started during this attempt (a second "
          "run in the pod) is not this run's", orf(env_oc, "ses_A", time.time() - 60) == [],
          str(orf(env_oc, "ses_A", time.time() - 60)))
    _clear()
    seen_sql = []
    real_connect = sqlite3.connect

    def _tracing(*a, **k):
        con = real_connect(*a, **k)
        con.set_trace_callback(seen_sql.append)
        return con
    hc.sqlite3.connect = _tracing
    try:
        orf(env_oc, "ses_A", now - 60)
    finally:
        hc.sqlite3.connect = real_connect
    check("the session and status are filtered in the SQL itself",
          any("session_id" in q and "running" in q for q in seen_sql), str(seen_sql)[:200])
    writer = (f"{py} -c 'import sqlite3,json,time,sys\n"
              f"c=sqlite3.connect(\"{dbp}\")\n"
              "d={{\"type\":\"tool\",\"tool\":\"bash\",\"state\":{{\"status\":sys.argv[1],"
              "\"input\":{{\"command\":\"make\",\"timeout\":{ms}}},"
              "\"time\":{{\"start\":int(time.time()*1000)}}}}}}\n"
              "c.execute(\"INSERT OR REPLACE INTO part VALUES (?,?,?,?,?,?)\","
              "(\"p1\",\"m1\",\"ses_A\",0,0,json.dumps(d)))\nc.commit()' {status}")
    # The step asks for 20 s, far past the 3 s default window, so the check
    # still fails if the request is ignored. With 9 s the 6 s spin plus two
    # interpreter starts left about 3 s of slack, and under the parallel
    # pre-merge gate that slack ran out (it failed inside the full
    # gate on two unrelated trees, passed alone on both).
    final, el = run("opencode-long", 'echo \'{"type":"step_start","sessionID":"ses_A"}\'\n'
                    "echo TEXT\n" + writer.format(status="running", ms=20000) + "\n"
                    + spin.format(s=6) + "\n" + writer.format(status="completed", ms=20000)
                    + "\necho TOOL_RESULT\necho TEXT\nexit 0\n", spec_id="opencode")
    check("a busy 6 s opencode step that asked for 20 s (invisible in its output) "
          "completes", final.get("state") == "complete" and final.get("ok") is True,
          f"{final.get('state')} {final.get('error')!r} el={el:.1f}")
    final, el = run("opencode-spin", 'echo \'{"type":"step_start","sessionID":"ses_A"}\'\n'
                    "echo TEXT\n" + writer.format(status="running", ms=9000) + "\n"
                    + spin.format(s=30) + "\n", spec_id="opencode")
    err = final.get("error") or ""
    check("a running opencode step that keeps spinning past its request stops as a "
          "tool step", final.get("state") == "timeout" and 9 <= el < 16 and "tool step" in err,
          f"el={el:.1f} {err!r}")

    print("== end to end: a stale row from a killed run does not stop a new one ==")
    _put("old", "ses_OLD", {"status": "running", "input": {"command": "npm test"},
                            "time": {"start": int((time.time() - 3 * 3600) * 1000)}})
    final, el = run("oc-stale", f"{py} -c 'import time\nt=time.time()\nwhile time.time()-t<2: pass'\n"
                    'echo \'{"type":"step_start","sessionID":"ses_NEW"}\'\necho TEXT\nexit 0\n',
                    spec_id="opencode")
    check("a new opencode run beside a 3-hour-old stale row completes",
          final.get("state") == "complete" and final.get("ok") is True,
          f"{final.get('state')} {final.get('error')!r} el={el:.1f}")
    _clear()

    print("== end to end: a huge request is held to the ceiling ==")
    hc.TOOL_STEP_MAX_MINUTES = 0.1          # 6 s stand-in for 10 h
    final, el = run("oc-huge", 'echo \'{"type":"step_start","sessionID":"ses_A"}\'\n'
                    "echo TEXT\n" + writer.format(status="running", ms=1000000000000) + "\n"
                    + spin.format(s=30) + "\n", spec_id="opencode")
    err = final.get("error") or ""
    check("a spinning step that asked for 10^12 ms stops at the ceiling plus margin (~7 s)",
          final.get("state") == "timeout" and 6 <= el < 12 and "tool step" in err,
          f"el={el:.1f} {err!r}")
    check("the message names the window actually applied (7 s here)",
          "after 7 s" in err, err)
    hc.TOOL_STEP_MAX_MINUTES = 600
    _clear()

    print("== a watchdog check that raises does not kill the watchdog ==")
    real_sample = hc.sample_run_tree
    hc.sample_run_tree = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
    try:
        final, el = run("wd-raises", "echo TEXT\nsleep 30\n")
    finally:
        hc.sample_run_tree = real_sample
    check("with every check raising, the run is still stopped (fallback stops), not left "
          "running", final.get("state") == "timeout" and el < 12,
          f"{final.get('state')} el={el:.1f} {final.get('error')!r}")

    print("== grok's commands, in the shape measured on grok 0.2.112 ==")
    # Each command runs in its own wrapper, grok's direct child leading a new
    # session (setsid), started ~0.2 s after grok's last line (docs/evidence).
    hc.TOOL_STEP_MAX_MINUTES = 0.15         # 9 s stand-in for grok's 10 h
    final, el = run("grok-long", "echo TEXT\nsleep 0.2\nsetsid -w " + spin.format(s=6)
                    + "\necho TEXT\nexit 0\n", spec_id="grok")
    check("a 6 s busy grok command (no tool events; progress window 3 s) completes",
          final.get("state") == "complete" and final.get("ok") is True,
          f"{final.get('state')} {final.get('error')!r} el={el:.1f}")
    hc.TOOL_STEP_MAX_MINUTES = 0.1          # 6 s stand-in; window 7 s with the margin
    final, el = run("grok-bg", "echo TEXT\nsleep 0.2\nsetsid -w " + spin.format(s=60) + " &\n"
                    "i=0\nwhile [ $i -lt 24 ]; do echo TEXT; sleep 0.5; i=$((i+1)); done\n"
                    "exit 0\n", spec_id="grok")
    check("grok working (events flowing) beside a busy background child for 12 s, "
          "past the 7 s cap, completes", final.get("state") == "complete" and final.get("ok") is True,
          f"{final.get('state')} {final.get('error')!r} el={el:.1f}")
    final, el = run("grok-spin", "echo TEXT\nsleep 0.2\nsetsid -w " + spin.format(s=60) + "\n",
                    spec_id="grok")
    err = final.get("error") or ""
    check("grok silent with a busy child is held to its own maximum (~7 s)",
          final.get("state") == "timeout" and 6 <= el < 13 and "tool step" in err
          and "own maximum" in err, f"el={el:.1f} {err!r}")
    final, el = run("grok-silent", "echo TEXT\nexec sleep 60\n", spec_id="grok")
    check("grok silent with no child is stopped at the idle / model-wait window",
          final.get("state") == "timeout" and el < 5, f"el={el:.1f} {final.get('error')!r}")
    final, el = run("grok-idle", "echo TEXT\nsleep 0.2\nsetsid -w sleep 60\n", spec_id="grok")
    err = final.get("error") or ""
    check("one whose command does nothing stops at the idle window",
          final.get("state") == "timeout" and el < 5 and "stopped as stuck" in err,
          f"el={el:.1f} {err!r}")

    print("== grok 1.0.46 reports its commands: the 10 h hold still applies ==")
    # grok >= 1.0 prints tool_call / tool_call_update, so a command is an OPEN
    # reported step — with no requested timeout to read. Its live wrapper must
    # still earn grok's own maximum, not TOOL_STEP_MINUTES (3 s here).
    hc.TOOL_STEP_MAX_MINUTES = 0.15         # 9 s stand-in for grok's 10 h
    final, el = run("grok-rep-long", "echo TEXT\nsleep 0.2\necho 'CALL {\"command\":\"make\"}'\n"
                    "setsid -w " + spin.format(s=6) + "\necho TOOL_RESULT\necho TEXT\nexit 0\n",
                    spec_id="grok")
    check("a REPORTED 6 s busy grok command (tool step window 3 s) completes under the hold",
          final.get("state") == "complete" and final.get("ok") is True,
          f"{final.get('state')} {final.get('error')!r} el={el:.1f}")
    hc.TOOL_STEP_MAX_MINUTES = 0.1          # 6 s stand-in; window 7 s with the margin
    final, el = run("grok-rep-spin", "echo TEXT\nsleep 0.2\necho 'CALL {\"command\":\"make\"}'\n"
                    "setsid -w " + spin.format(s=60) + "\n", spec_id="grok")
    err = final.get("error") or ""
    check("a REPORTED grok command spinning past its maximum still stops (~7 s)",
          final.get("state") == "timeout" and 6 <= el < 13 and "tool step" in err,
          f"el={el:.1f} {err!r}")
    # The hold ends when the wrapper exits, but the step stays open until grok
    # prints its result. The ordinary window must count from when the command
    # was last seen alive, not from the call: a command that ran past
    # TOOL_STEP_MINUTES under the hold must not be cut in the gap before its
    # result (grok busy in its own process here, so the idle stop stays out).
    hc.TOOL_STEP_MAX_MINUTES = 0.15         # 9 s stand-in for grok's 10 h
    final, el = run("grok-rep-gap", "echo TEXT\nsleep 0.2\necho 'CALL {\"command\":\"make\"}'\n"
                    "setsid -w " + spin.format(s=5) + "\n" + spin.format(s=1.5)
                    + "\necho TOOL_RESULT\necho TEXT\nexit 0\n", spec_id="grok")
    check("a REPORTED grok command held past the step window is not cut before its result",
          final.get("state") == "complete" and final.get("ok") is True,
          f"{final.get('state')} {final.get('error')!r} el={el:.1f}")
    hc.TOOL_STEP_MAX_MINUTES = 600

    print("== whose connection a silent grok holds names the stop ==")
    import socket
    import threading
    held = []
    lsock = socket.socket()     # a stand-in model endpoint in THIS process: loopback only
    lsock.bind(("127.0.0.1", 0))
    lsock.listen(8)

    def _accept():
        while True:
            try:
                held.append(lsock.accept()[0])     # accepts, never answers, never closes
            except OSError:
                return
    threading.Thread(target=_accept, daemon=True).start()
    port = lsock.getsockname()[1]
    # grok is one process (exec), so its own pid holds its model connection.
    (tmp / "grok_own.py").write_text(
        "import socket, subprocess, time\n"
        f"s = socket.create_connection(('127.0.0.1', {port}))\n"
        "print('TEXT', flush=True)\ntime.sleep(0.2)\n"
        "subprocess.Popen(['sleep', '60'], start_new_session=True)\ntime.sleep(60)\n")
    (tmp / "conn.py").write_text(
        f"import socket, time\ns = socket.create_connection(('127.0.0.1', {port}))\n"
        "time.sleep(60)\n")
    (tmp / "grok_cmd.py").write_text(
        "import subprocess, sys, time\nprint('TEXT', flush=True)\ntime.sleep(0.2)\n"
        f"subprocess.Popen([sys.executable, '{tmp}/conn.py'], start_new_session=True)\n"
        "time.sleep(60)\n")
    final, el = run("grok-own-conn", f"exec {py} {tmp}/grok_own.py\n", spec_id="grok")
    err = final.get("error") or ""
    check("a silent grok holding its OWN model connection beside an idle command says it "
          "waited on the model", final.get("state") == "timeout" and "waited on the model" in err
          and "holding a network connection" not in err, f"el={el:.1f} {err!r}")
    final, el = run("grok-cmd-conn", f"exec {py} {tmp}/grok_cmd.py\n", spec_id="grok")
    err = final.get("error") or ""
    check("...while a connection held by its COMMAND is still named as the command's",
          final.get("state") == "timeout" and "holding a network connection" in err
          and "waited on the model" not in err, f"el={el:.1f} {err!r}")
    # grok >= 1.0 REPORTED the step: it said it is inside a command, so its own
    # idle connection is not a model wait, and the stop must not say it is.
    (tmp / "grok_own_rep.py").write_text(
        "import socket, subprocess, time\n"
        f"s = socket.create_connection(('127.0.0.1', {port}))\n"
        "print('TEXT', flush=True)\ntime.sleep(0.2)\n"
        "print('CALL {\"command\": \"sleep 60\"}', flush=True)\n"
        "subprocess.Popen(['sleep', '60'], start_new_session=True)\ntime.sleep(60)\n")
    final, el = run("grok-own-rep", f"exec {py} {tmp}/grok_own_rep.py\n", spec_id="grok")
    err = final.get("error") or ""
    check("a grok inside a REPORTED command step is never stopped as a model wait",
          final.get("state") == "timeout" and "waited on the model" not in err
          and "holding a network connection" in err, f"el={el:.1f} {err!r}")
    lsock.close()

    print("== a line that cannot be read or parsed fails the run, never 'complete' ==")
    events.normalize_line = real_normalize
    try:
        final, el = run("deep-json", f"{py} -c 'print(\"[\" * 200000 + \"]\" * 200000)'\n"
                        "echo '{\"type\": \"after\"}'\nexit 0\n")
        err = final.get("error") or ""
        check("a deeply nested JSON line (json.loads raises RecursionError) ends ok false "
              "with the reason", final.get("ok") is False and "failed" in err
              and any(w in err for w in ("RecursionError", "recursion", "Stack overflow")),
              f"{final.get('state')} ok={final.get('ok')} {err!r}")
        final, el = run("not-utf8", "printf 'TEXT\\n\\377\\376\\n'\necho TEXT\nexit 0\n")
        err = final.get("error") or ""
        check("a line that is not UTF-8 ends ok false with the reason, not a quiet end of "
              "output", final.get("ok") is False and "UnicodeDecodeError" in err,
              f"{final.get('state')} ok={final.get('ok')} {err!r}")
    finally:
        events.normalize_line = _parse

    print("== who counts as a grok command, and a reused pid (a fake /proc) ==")
    fk = tmp / "fakeproc-r9"

    def _st(pid, ppid, sid, start=0, state="S"):
        d = fk / str(pid)
        d.mkdir(parents=True, exist_ok=True)
        rest = [state, ppid, sid, sid] + [0] * 15 + [start] + [0] * 20
        (d / "stat").write_text(f"{pid} (x) " + " ".join(map(str, rest)) + "\n")
    _st(1, 0, 1)
    _st(100, 1, 100, 10)                     # grok
    _st(500, 100, 500, 1000)                 # a command wrapper: grok's child, its own session
    _st(501, 100, 100, 1001)                 # a helper grok started, left in grok's session
    _st(502, 100, 502, 1002, "Z")            # a finished wrapper not yet reaped
    got = sorted(pid for pid, _st0 in hc.direct_commands(100, str(fk)))
    check("a grok child that does not lead its own session is never a command",
          got == [500], str(got))
    import shutil
    shutil.rmtree(fk)
    sids: set = set()
    base = ((1, 0, 1), (os.getpid(), 1, os.getsid(0)), (500, os.getpid(), 500))
    marker = "m-r9"

    def _strays(extra):
        shutil.rmtree(fk, ignore_errors=True)
        for pid, ppid, sid in base + extra:
            _st(pid, ppid, sid)
        with hc._LIVE_LOCK:
            hc._LIVE_ATTEMPTS.add(marker)
        try:
            return hc._pod_strays(set(), marker, proc_root=str(fk), in_container_fn=lambda: True,
                                  recheck_fn=lambda: True, cli_pid=500, outside_sids=sids)
        finally:
            with hc._LIVE_LOCK:
                hc._LIVE_ATTEMPTS.discard(marker)
    first = _strays(((99020, 0, 99020), (99026, 99020, 99020)))   # an exec into the container + its child
    seen_first = set(sids)
    gone = _strays(())                                             # all of that session exits
    reused = _strays(((99020, 1, 99020),))   # pid 99020 reused by a run tool that setsid()s
    check("a session seen from outside is forgotten once empty, so a run tool that "
          "reuses its number is claimed", 99020 in seen_first and 99020 not in first
          and 99026 not in first and 99020 in reused, f"first={sorted(first)} "
          f"seen={sorted(seen_first)} gone={sorted(gone)} reused={sorted(reused)}")

    print("== the tool-step stop names the window it applied, not the margin's ==")
    hc.TOOL_STEP_MARGIN_MINUTES = 5 / 60    # 5 s margin, above the 3 s window
    final, el = run("n3-teeth", "echo TEXT\necho 'CALL {}'\n" + spin.format(s=30) + "\n")
    err = final.get("error") or ""
    check("a step that asked for nothing stops at 3 s and says 3 s (not 3 s + margin)",
          final.get("state") == "timeout" and el < 8 and "after 3 s" in err, f"el={el:.1f} {err!r}")
    hc.TOOL_STEP_MARGIN_MINUTES = 1 / 60

    print("== the fallback says the watchdog could not read activity ==")
    real_sample = hc.sample_run_tree
    hc.sample_run_tree = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
    try:
        final, el = run("fb-msg", "echo TEXT\nexec sleep 30\n")
    finally:
        hc.sample_run_tree = real_sample
    err = final.get("error") or ""
    check("the fallback stop says the activity could not be read, and names its window",
          "could not read the run's activity" in err and "used no CPU" not in err
          and " s " in err, err)

    print("== who the single-run fallback may claim (a fake /proc) ==")
    fake = tmp / "fakeproc"
    me = os.getpid()
    my_sid = os.getsid(0)

    def _proc(pid, ppid, sid):
        d = fake / str(pid)
        d.mkdir(parents=True, exist_ok=True)
        (d / "stat").write_text(f"{pid} (x) S {ppid} {pid} {sid} 0 0 0 0 0 0 0 0 0 0 0 0 0\n")
    for pid, ppid, sid in ((1, 0, 1), (me, 1, my_sid), (500, me, 500),   # cli
                           (501, 1, 501),                                   # escaped tool
                           (600, 0, 600), (601, 600, 600),                  # exec probe + child
                           (700, me, my_sid), (701, 700, 701),              # login + grandchild
                           (800, 1, my_sid)):                               # re-parented probe kid
        _proc(pid, ppid, sid)
    marker = "m-n4"
    with hc._LIVE_LOCK:
        hc._LIVE_ATTEMPTS.add(marker)
    try:
        got = hc._pod_strays(set(), marker, proc_root=str(fake),
                             in_container_fn=lambda: True, recheck_fn=lambda: True,
                             cli_pid=500)
    finally:
        with hc._LIVE_LOCK:
            hc._LIVE_ATTEMPTS.discard(marker)
    check("claims the CLI and the escaped tool, and nothing started from outside the "
          "container or by the server's other work", got == {500, 501}, str(sorted(got)))
    # The layout measured in the pinned grok image under docker: an exec'd
    # shell has ppid 0 and its own session; a child it leaves behind is
    # re-parented to pid 1 and keeps that session after the shell is gone.
    fake2 = tmp / "fakeproc2"
    fake = fake2

    def _proc2(pid, ppid, sid):
        d = fake2 / str(pid)
        d.mkdir(parents=True, exist_ok=True)
        (d / "stat").write_text(f"{pid} (x) S {ppid} {pid} {sid} 0 0 0 0 0 0 0 0 0 0 0 0 0\n")
    for pid, ppid, sid in ((1, 0, 1), (me, 1, my_sid), (500, me, 500),
                           (20, 0, 20), (26, 20, 20)):          # exec shell + its child
        _proc2(pid, ppid, sid)
    seen_sids: set = set()
    with hc._LIVE_LOCK:
        hc._LIVE_ATTEMPTS.add(marker)
    try:
        first = hc._pod_strays(set(), marker, proc_root=str(fake2), in_container_fn=lambda: True,
                               recheck_fn=lambda: True, cli_pid=500, outside_sids=seen_sids)
        import shutil
        shutil.rmtree(fake2 / "20")                              # the exec finished
        _proc2(26, 1, 20)                                        # its child, re-parented
        later = hc._pod_strays(set(), marker, proc_root=str(fake2), in_container_fn=lambda: True,
                               recheck_fn=lambda: True, cli_pid=500, outside_sids=seen_sids)
    finally:
        with hc._LIVE_LOCK:
            hc._LIVE_ATTEMPTS.discard(marker)
    check("a child left by a finished exec into the container is spared (its session was seen "
          "rooted outside the container)", 26 not in first and 26 not in later and later == {500},
          f"first={sorted(first)} later={sorted(later)}")

    print("== the audit excuses opencode's `part` only in its one file ==")
    sys.path.insert(0, os.path.join(app_root, "scripts"))
    import audit_orphan_tables as aot
    plain, only_in = aot._split_qualified({"tables", "part @ system-apps/_harness_runtime/harness_core.py"})
    excused = lambda n, files: n in plain or (n in only_in and set(files) <= only_in[n])  # noqa: E731
    check("the baseline excuses `part` named by the harness's opencode reader",
          excused("part", {"system-apps/_harness_runtime/harness_core.py"}))
    check("...and not a Host table named `part` anywhere else",
          not excused("part", {"server/routes/foo.py"})
          and not excused("part", {"system-apps/_harness_runtime/harness_core.py",
                                   "server/routes/foo.py"}))

    print("== claude-code: a Bash call's own timeout widens its step ==")
    import asyncio
    import importlib.util
    sp = importlib.util.spec_from_file_location(
        "cch_stepwin", os.path.join(app_root, "system-apps/claude-code-harness/bin/main.py"))
    CCH = importlib.util.module_from_spec(sp)
    sp.loader.exec_module(CCH)
    CCH._SDK_AVAILABLE = True
    CCH._get_token = lambda: "tok"
    CCH._ensure_claude_config = lambda: None
    CCH._SdkOptions = lambda **kw: kw
    CCH.worktree_diff = lambda d: ""
    CCH._current_creds = lambda: None
    CCH.IDLE_FLOOR_S = 0.1
    CCH._ALIVE_FRAME_S = 0.3
    CCH._stop_run_tree = lambda marker, **k: []
    CCH.TOOL_STEP_MINUTES = 0.03           # 1.8 s
    CCH.TOOL_STEP_MARGIN_MINUTES = 1 / 60  # 1 s
    ticks = [0]

    class _S:
        def __init__(self, cpu):
            self.cpu_ticks, self.io_bytes, self.net = cpu, None, False

    def _busy(pids, *a, **k):
        ticks[0] += 1000
        return _S(ticks[0])
    CCH.sample_pids = _busy

    def _blk(cls, **kw):
        o = type(cls, (), {})()
        for k, v in kw.items():
            setattr(o, k, v)
        return o

    def _script(steps):
        async def _q(prompt, options):
            for st in steps:
                if isinstance(st, (int, float)):
                    await asyncio.sleep(st)
                else:
                    yield st
        return _q
    call = _blk("AssistantMessage", content=[_blk("ToolUseBlock", id="t1", name="Bash",
                                                  input={"command": "make", "timeout": 6000})])
    res = _blk("UserMessage", content=[_blk("ToolResultBlock", tool_use_id="t1")])
    done = _blk("ResultMessage", is_error=False, model="m", usage=None, total_cost_usd=0)
    CCH._sdk_query = _script([call, 4, res, done])
    fr = list(CCH.run_stream(Req({"prompt": "x", "project_dir": tempfile.mkdtemp(),
                                  "idle_minutes": 0.005, "max_minutes": 0.01})))
    p = fr[-1]["payload"]
    check("claude-code: a busy 4 s Bash step that asked for 6 s completes (window 1.8 s "
          "otherwise)", p.get("state") == "complete" and p.get("ok") is True,
          f"{p.get('state')} {p.get('error')!r}")

    print("== a claude-code subagent's own events are progress for its step ==")
    task = _blk("AssistantMessage", content=[_blk("ToolUseBlock", id="task1", name="Task",
                                                  input={"prompt": "explore"})])
    steps = [task]
    for i in range(15):
        steps += [0.4,
                  _blk("AssistantMessage", parent_tool_use_id="task1",
                       content=[_blk("ToolUseBlock", id=f"s{i}", name="Read",
                                     input={"file_path": "x"})]),
                  _blk("UserMessage", parent_tool_use_id="task1",
                       content=[_blk("ToolResultBlock", tool_use_id=f"s{i}")])]
    steps += [_blk("UserMessage", content=[_blk("ToolResultBlock", tool_use_id="task1")]), done]
    CCH._sdk_query = _script(steps)
    fr = list(CCH.run_stream(Req({"prompt": "x", "project_dir": tempfile.mkdtemp(),
                                  "idle_minutes": 0.005, "max_minutes": 0.01})))
    p = fr[-1]["payload"]
    check("a subagent working 6 s (an event every 0.4 s) under a 1.8 s step window "
          "completes", p.get("state") == "complete" and p.get("ok") is True,
          f"{p.get('state')} {p.get('error')!r}")
    CCH._sdk_query = _script([task, 30])
    fr = list(CCH.run_stream(Req({"prompt": "x", "project_dir": tempfile.mkdtemp(),
                                  "idle_minutes": 0.005, "max_minutes": 0.01})))
    p = fr[-1]["payload"]
    check("...while a subagent step with no events of its own still stops at the window",
          p.get("state") == "timeout" and "tool step" in (p.get("error") or ""),
          f"{p.get('state')} {p.get('error')!r}")

    print()
    if FAILS:
        print(f"FAIL ({len(FAILS)}): {FAILS}")
        return 1
    print("all green")
    return 0


if __name__ == "__main__":
    sys.exit(main())
