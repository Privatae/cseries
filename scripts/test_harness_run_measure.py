#!/usr/bin/env python3
"""Per-run measurement in the harness's final frame and /run result.

The final status frame (state complete or timeout) and the /run body carry five fields:
  peak_mem_bytes  int | null    cgroup v2 memory.peak, read at the end
  peak_anon_bytes int | null    highest anon + shmem from memory.stat, sampled
                                through the run (page cache left out)
  cpu_s           number | null (usage_usec end - start) / 1e6, 3 decimals
  wall_s          number        time since the run started, 2 decimals
  measure_scope   "container"
A value that cannot be read is null, never 0. No other frame changes, and a
pre-flight refusal carries none of them.

Driven through the real handlers against a FAKE cgroup directory
(CSERIES_HARNESS_CGROUP_DIR): the shared runtime with a stub CLI that bumps
cpu.stat while it runs, and the claude-code harness with a stand-in SDK that
does the same. No pod, no network, no vendor login.

Run: APP_ROOT=<repo> python3 scripts/test_harness_run_measure.py
"""
import asyncio
import dataclasses
import importlib.util
import json
import os
import sys
import tempfile
from pathlib import Path

FAILS = []


def check(name: str, ok: bool, detail: str = ""):
    tag = "ok  " if ok else "FAIL"
    print(f"[run-measure] {tag} {name}" + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILS.append(name)


class FakeRequest:
    def __init__(self, body: bytes):
        self.body = body


def _blk(clsname, **kw):
    o = type(clsname, (), {})()
    for k, v in kw.items():
        setattr(o, k, v)
    return o


FIELDS = ("peak_mem_bytes", "peak_anon_bytes", "cpu_s", "wall_s", "measure_scope")

MIB = 1024 * 1024


def _stat_text(anon, file, shmem) -> str:
    # The real file's shape (cgroup v2 memory.stat, abridged), including the
    # anon_thp / shmem_thp / file_* lines a prefix match would wrongly pick up.
    return (f"anon {anon}\nfile {file}\nkernel 1048576\nkernel_stack 65536\n"
            f"shmem {shmem}\nfile_mapped 4096\nfile_dirty 0\nanon_thp 999999999\n"
            f"shmem_thp 888888888\ninactive_anon {anon}\nactive_file {file}\n")


def _write_cgroup(d: Path, usage_usec, peak, anon=None, file=0, shmem=0) -> None:
    (d / "cpu.stat").write_text(
        f"usage_usec {usage_usec}\nuser_usec 1\nsystem_usec 2\n")
    (d / "memory.peak").write_text(f"{peak}\n")
    if anon is not None:
        (d / "memory.stat").write_text(_stat_text(anon, file, shmem))


def main() -> int:
    app_root = os.environ.get("APP_ROOT", "/app")
    sys.path.insert(0, os.path.join(app_root, "cli"))
    sys.path.insert(0, os.path.join(app_root, "system-apps"))
    tmp = Path(tempfile.mkdtemp(prefix="run-measure-"))
    cg = tmp / "cgroup"
    cg.mkdir()

    try:
        from _harness_runtime import measure as M
    except ImportError as exc:
        check("_harness_runtime.measure exists", False, str(exc))
        print(f"[run-measure] FAIL ({len(FAILS)} failing): {FAILS}")
        return 1
    M.CGROUP_DIR = str(cg)

    print("== 1. the cgroup readers ==")
    _write_cgroup(cg, 5_000_000, 123_456_789)
    check("cpu_usage_usec reads the usage_usec line", M.cpu_usage_usec() == 5_000_000)
    check("memory_peak reads memory.peak as bytes", M.memory_peak() == 123_456_789)
    (cg / "cpu.stat").write_text("user_usec 1\nsystem_usec 2\n")
    check("cpu.stat without usage_usec -> None", M.cpu_usage_usec() is None)
    (cg / "cpu.stat").write_text("usage_usec lots\n")
    check("unparsable usage_usec -> None", M.cpu_usage_usec() is None)
    (cg / "memory.peak").write_text("max\n")
    check("unparsable memory.peak -> None", M.memory_peak() is None)
    os.unlink(cg / "cpu.stat")
    os.unlink(cg / "memory.peak")
    check("missing files -> None, never 0",
          M.cpu_usage_usec() is None and M.memory_peak() is None)
    (cg / "memory.peak").mkdir()
    check("an unreadable memory.peak (a directory) -> None", M.memory_peak() is None)
    (cg / "memory.peak").rmdir()

    # memory.stat: anon + shmem. `file` is page cache.
    (cg / "memory.stat").write_text(_stat_text(100 * MIB, 900 * MIB, 5 * MIB))
    check("memory_anon reads anon + shmem, not file, not *_thp",
          M.memory_anon() == 105 * MIB, repr(M.memory_anon()))
    (cg / "memory.stat").write_text("file 5\nshmem 3\n")
    check("memory.stat without an anon line -> None", M.memory_anon() is None)
    (cg / "memory.stat").write_text("anon lots\nshmem 3\n")
    check("unparsable anon -> None", M.memory_anon() is None)
    os.unlink(cg / "memory.stat")
    check("missing memory.stat (cgroup v1, Docker Desktop) -> None, never 0",
          M.memory_anon() is None)

    _write_cgroup(cg, 1_000_000, 10)
    rm = M.RunMeasure()
    _write_cgroup(cg, 3_234_567, 2_000_000_000)
    f = rm.fields()
    check("RunMeasure.fields: the five fields, in contract order",
          list(f) == list(FIELDS), repr(f))
    check("cpu_s = (end - start) / 1e6, rounded to 3 decimals", f["cpu_s"] == 2.235, repr(f))
    check("peak_mem_bytes is the end read", f["peak_mem_bytes"] == 2_000_000_000, repr(f))
    check("measure_scope is 'container'", f["measure_scope"] == "container")
    check("wall_s is a number rounded to 2 decimals",
          isinstance(f["wall_s"], float) and f["wall_s"] >= 0
          and round(f["wall_s"], 2) == f["wall_s"], repr(f))
    _write_cgroup(cg, 10, 10)
    check("cpu going BACKWARDS (a different cgroup) -> cpu_s None",
          rm.fields()["cpu_s"] is None)
    os.unlink(cg / "cpu.stat")
    rm2 = M.RunMeasure()
    _write_cgroup(cg, 9_000_000, 10)
    check("cpu unreadable at start -> cpu_s None (not the end value)",
          rm2.fields()["cpu_s"] is None)
    check("memory.stat never readable -> peak_anon_bytes None",
          rm2.fields()["peak_anon_bytes"] is None)

    # The sampler: a spike that is gone again by the time the run ends is
    # still the run's peak, because it is sampled while the run is live.
    M.SAMPLE_INTERVAL_S = 0.02
    _write_cgroup(cg, 1, 1, anon=10 * MIB)
    rm3 = M.RunMeasure()
    import time as _t
    _t.sleep(0.15)
    _write_cgroup(cg, 1, 1, anon=400 * MIB, shmem=20 * MIB)
    _t.sleep(0.15)
    _write_cgroup(cg, 1, 1, anon=30 * MIB)
    _t.sleep(0.1)
    f3 = rm3.fields()
    check("a mid-run anon spike is the peak, not the end read",
          f3["peak_anon_bytes"] == 420 * MIB, repr(f3["peak_anon_bytes"]))
    th = getattr(rm3, "_thread", None)
    if th is not None:
        th.join(timeout=2)
    check("the sampler thread stops at fields()", th is not None and not th.is_alive())
    # A run that never reaches its final frame (a client that went away) must
    # not leave a sampler behind: dropping the measure stops it.
    rm4 = M.RunMeasure()
    th4 = getattr(rm4, "_thread", None)
    del rm4
    import gc as _gc
    _gc.collect()
    if th4 is not None:
        th4.join(timeout=2)
    check("a dropped RunMeasure's sampler stops on its own",
          th4 is not None and not th4.is_alive())
    M.SAMPLE_INTERVAL_S = 0.05

    print("== 2. shared runtime (harness_core): /run and /run/stream ==")
    from _harness_runtime import harness_core as hc
    from _harness_runtime import specs
    KM = specs.KIMI
    os.environ["KIMI_HOME"] = str(tmp / "kimi-home")
    hc._BARE_KEY_BASE_CACHE.clear()
    hc._BARE_KEY_BASE_PROBE = (lambda base, key: base == KM.bare_key_base_url)
    kp = tmp / "kimi-bare"
    kp.write_text("sk-moonshot-1")
    # The stub CLI is the "run": while it runs, the container's CPU counter
    # rises by 1.5 s and its peak memory reaches 300 MB.
    stub = tmp / "kimi-stub"
    stub.write_text(
        "#!/bin/sh\n"
        f"printf 'usage_usec 2500000\\nuser_usec 1\\n' > {cg}/cpu.stat\n"
        f"printf '300000000\\n' > {cg}/memory.peak\n"
        "echo did the work\n")
    stub.chmod(0o755)
    km = dataclasses.replace(KM, binary=stub, creds_path=kp)

    _write_cgroup(cg, 1_000_000, 50_000_000)
    out = hc.make_run(km)(FakeRequest(json.dumps(
        {"prompt": "fix the bug", "project_dir": str(tmp / "p1"), "model": "k3"}).encode()))
    check("/run succeeds", isinstance(out, dict) and out.get("ok") is True, repr(out)[:300])
    out = out if isinstance(out, dict) else {}
    check("/run carries the five measurement fields",
          all(k in out for k in FIELDS), repr({k: out.get(k) for k in FIELDS}))
    check("/run cpu_s is the run's CPU (1.5 s)", out.get("cpu_s") == 1.5, repr(out.get("cpu_s")))
    check("/run peak_mem_bytes is memory.peak at the end",
          out.get("peak_mem_bytes") == 300_000_000, repr(out.get("peak_mem_bytes")))
    check("/run measure_scope is 'container'", out.get("measure_scope") == "container")
    check("/run has no `state` (popped as before)", "state" not in out)

    # File-heavy vs anon-heavy, through the real handler. The file-heavy shape
    # is the one measured (npm-install style): 606.8 MiB peak
    # against 186.1 MiB anon peak; the rest was page cache.
    file_stub = tmp / "kimi-file-heavy"
    file_stub.write_text(
        "#!/bin/sh\n"
        f"printf '{_stat_text(int(186.1 * MIB), int(420.7 * MIB), 0)}' > {cg}/memory.stat\n"
        f"printf '{int(606.8 * MIB)}\\n' > {cg}/memory.peak\n"
        "sleep 0.3\n"
        "echo did the work\n")
    file_stub.chmod(0o755)
    _write_cgroup(cg, 1_000_000, 50 * MIB, anon=20 * MIB, file=30 * MIB)
    out = hc.make_run(dataclasses.replace(km, binary=file_stub))(FakeRequest(json.dumps(
        {"prompt": "npm install", "project_dir": str(tmp / "p5"), "model": "k3"}).encode()))
    out = out if isinstance(out, dict) else {}
    check("file-heavy run: peak_mem_bytes is memory.peak (cache included)",
          out.get("peak_mem_bytes") == int(606.8 * MIB), repr(out.get("peak_mem_bytes")))
    check("file-heavy run: peak_anon_bytes is the anon peak, cache left out",
          out.get("peak_anon_bytes") == int(186.1 * MIB), repr(out.get("peak_anon_bytes")))

    anon_stub = tmp / "kimi-anon-heavy"
    anon_stub.write_text(
        "#!/bin/sh\n"
        f"printf '{_stat_text(900 * MIB, 10 * MIB, 50 * MIB)}' > {cg}/memory.stat\n"
        f"printf '{1000 * MIB}\\n' > {cg}/memory.peak\n"
        "sleep 0.4\n"
        # freed before the run ends: only a sample taken during the run sees it
        f"printf '{_stat_text(40 * MIB, 10 * MIB, 0)}' > {cg}/memory.stat\n"
        "echo did the work\n")
    anon_stub.chmod(0o755)
    _write_cgroup(cg, 1_000_000, 50 * MIB, anon=20 * MIB, file=30 * MIB)
    out = hc.make_run(dataclasses.replace(km, binary=anon_stub))(FakeRequest(json.dumps(
        {"prompt": "build", "project_dir": str(tmp / "p6"), "model": "k3"}).encode()))
    out = out if isinstance(out, dict) else {}
    check("anon-heavy run: peak_anon_bytes is the mid-run anon + shmem peak (950 MiB)",
          out.get("peak_anon_bytes") == 950 * MIB, repr(out.get("peak_anon_bytes")))

    _write_cgroup(cg, 1_000_000, 50_000_000)
    frames = list(hc.make_run_stream(km)(FakeRequest(json.dumps(
        {"prompt": "fix the bug", "project_dir": str(tmp / "p2"), "model": "k3"}).encode())))
    statuses = [fr for fr in frames if fr.get("kind") == "status"]
    final = statuses[-1]["payload"] if statuses else {}
    check("stream: the final frame is complete and carries the fields",
          final.get("state") == "complete" and all(k in final for k in FIELDS)
          and final.get("cpu_s") == 1.5 and final.get("peak_mem_bytes") == 300_000_000,
          repr({k: final.get(k) for k in ("state",) + FIELDS}))
    check("stream: no other frame carries them",
          not any(k in (fr.get("payload") or {}) for fr in frames[:-1] for k in FIELDS))

    out = hc.make_run(km)(FakeRequest(b'{"prompt": "  "}'))
    check("a pre-flight refusal carries no measurement",
          isinstance(out, tuple) and out[1] == 400
          and not any(k in out[0] for k in FIELDS), repr(out))

    # Unreadable cgroup: nulls, but wall_s still a number.
    hc_cg = M.CGROUP_DIR
    M.CGROUP_DIR = str(tmp / "no-such-cgroup")
    out = hc.make_run(dataclasses.replace(km, binary=Path("/bin/echo")))(FakeRequest(json.dumps(
        {"prompt": "x", "project_dir": str(tmp / "p3"), "model": "k3"}).encode()))
    check("unreadable cgroup: peak_mem_bytes, peak_anon_bytes and cpu_s are null, wall_s a number",
          isinstance(out, dict) and out.get("peak_mem_bytes") is None
          and "peak_anon_bytes" in out and out.get("peak_anon_bytes") is None
          and out.get("cpu_s") is None and isinstance(out.get("wall_s"), float)
          and out.get("measure_scope") == "container",
          repr({k: (out or {}).get(k) for k in FIELDS}))
    M.CGROUP_DIR = hc_cg

    # A retried run is measured across BOTH attempts, the way elapsed_seconds
    # is: the start reading is taken once, before attempt 1. Attempt 1 takes
    # the counter from 1.0 s to 2.0 s and dies with kimi's transient provider
    # failure (harness_core._retryable_failure); attempt 2 takes it to 3.5 s
    # and succeeds. Measured from before attempt 1 that is 2.5 s; a start
    # reading taken per attempt would report 1.5 s.
    cut = ("error: failed to run prompt: provider.connection_error: "
           "Connection error.")
    cnt = tmp / "retry-count"
    rstub = tmp / "kimi-retry-stub"
    rstub.write_text(
        "#!/bin/sh\n"
        f"n=$(cat {cnt} 2>/dev/null || echo 0); n=$((n+1)); echo $n > {cnt}\n"
        "if [ $n -le 1 ]; then\n"
        f"  printf 'usage_usec 2000000\\n' > {cg}/cpu.stat\n"
        f"  echo '{cut}' >&2\n"
        "  exit 1\n"
        "fi\n"
        f"printf 'usage_usec 3500000\\n' > {cg}/cpu.stat\n"
        f"printf '400000000\\n' > {cg}/memory.peak\n"
        "echo did the work\n")
    rstub.chmod(0o755)
    _write_cgroup(cg, 1_000_000, 50_000_000)
    out = hc.make_run(dataclasses.replace(km, binary=rstub))(FakeRequest(json.dumps(
        {"prompt": "fix the bug", "project_dir": str(tmp / "p4"), "model": "k3"}).encode()))
    check("retried /run: the stub really ran twice and the run succeeded",
          isinstance(out, dict) and out.get("ok") is True and out.get("retries") == 1
          and cnt.read_text().strip() == "2", repr(out)[:300])
    check("retried /run: cpu_s spans both attempts (2.5 s, not attempt 2's 1.5 s)",
          isinstance(out, dict) and out.get("cpu_s") == 2.5,
          repr((out or {}).get("cpu_s")) if isinstance(out, dict) else repr(out))

    print("== 3. claude-code harness (its own runner) ==")
    path = os.path.join(app_root, "system-apps/claude-code-harness/bin/main.py")
    spec = importlib.util.spec_from_file_location("cch_main_measure", path)
    CCH = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(CCH)
    CCH._SDK_AVAILABLE = True
    CCH._get_token = lambda: "tok"
    CCH._ensure_claude_config = lambda: None
    CCH._SdkOptions = lambda **kw: kw
    CCH.worktree_diff = lambda d: ""
    CCH._current_creds = lambda: None
    CCH.IDLE_FLOOR_S = 0.1
    CCH._ALIVE_FRAME_S = 0.3
    CCH._stop_run_tree = lambda marker, **k: []
    CCH.sample_pids = lambda pids, *a, **k: None

    def _script(steps):
        async def _fake_query(prompt, options):
            for step in steps:
                if callable(step):
                    step()
                elif isinstance(step, (int, float)):
                    await asyncio.sleep(step)
                else:
                    yield step
        return _fake_query

    def _burn():
        _write_cgroup(cg, 4_250_000, 700_000_000, anon=300 * MIB, file=350 * MIB)

    done = _blk("ResultMessage", is_error=False, model="claude-opus-5-5",
                usage=None, total_cost_usd=0.1)
    said = _blk("AssistantMessage", content=[_blk("TextBlock", text="ok")])

    _write_cgroup(cg, 1_000_000, 80_000_000)
    CCH._sdk_query = _script([said, _burn, done])
    frames = list(CCH.run_stream(FakeRequest(json.dumps(
        {"prompt": "say ok", "project_dir": tempfile.mkdtemp()}).encode())))
    final = frames[-1]["payload"]
    check("claude-code stream: final frame carries the fields",
          final.get("state") == "complete" and final.get("cpu_s") == 3.25
          and final.get("peak_mem_bytes") == 700_000_000
          and final.get("peak_anon_bytes") == 300 * MIB
          and final.get("measure_scope") == "container"
          and isinstance(final.get("wall_s"), float),
          repr({k: final.get(k) for k in ("state",) + FIELDS}))
    check("claude-code stream: no other frame carries them",
          not any(k in (fr.get("payload") or {}) for fr in frames[:-1] for k in FIELDS))

    _write_cgroup(cg, 1_000_000, 80_000_000)
    CCH._sdk_query = _script([said, _burn, done])
    out = CCH.run(FakeRequest(json.dumps(
        {"prompt": "say ok", "project_dir": tempfile.mkdtemp()}).encode()))
    check("claude-code /run carries the fields",
          isinstance(out, dict) and out.get("cpu_s") == 3.25
          and out.get("peak_mem_bytes") == 700_000_000 and "state" not in out,
          repr(out)[:300])

    # A stopped run (504): the body is the same final payload.
    _write_cgroup(cg, 1_000_000, 80_000_000)
    tool = _blk("AssistantMessage", content=[
        _blk("ToolUseBlock", id="t1", name="Bash", input={"command": "sleep 99"})])
    CCH._sdk_query = _script([tool, _burn, 30])
    out = CCH.run(FakeRequest(json.dumps(
        {"prompt": "x", "max_minutes": 0.01, "idle_minutes": 0.005,
         "project_dir": tempfile.mkdtemp()}).encode()))
    res, status = (out if isinstance(out, tuple) else (out, 200))
    check("claude-code 504 body carries the fields",
          status == 504 and res.get("cpu_s") == 3.25
          and res.get("peak_mem_bytes") == 700_000_000,
          f"{status} " + repr({k: res.get(k) for k in FIELDS}))

    CCH._get_token = lambda: ""
    out = CCH.run(FakeRequest(b'{"prompt": "x"}'))
    check("claude-code pre-flight 401 carries no measurement",
          isinstance(out, tuple) and out[1] == 401
          and not any(k in out[0] for k in FIELDS), repr(out)[:200])

    if FAILS:
        print(f"[run-measure] FAIL ({len(FAILS)} failing): {FAILS}")
        return 1
    print("[run-measure] PASS (0 failing)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
