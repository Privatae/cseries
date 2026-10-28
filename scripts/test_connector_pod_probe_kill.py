#!/usr/bin/env python3
"""Gate: a connector example gate's own kill of a pod never cuts short the
kubelet probe that check M reads.

The Telegram, Slack and Discord example gates run the connector in a pod
(connector_gate_kit.Pod, and the Telegram gate's own Pod) with the kubelet's
readiness probe exec'd in it every PROBE_PERIOD_S, and check "M the kubelet's
probe answered in every pod" asks for at least one probe per pod and every
probe 0. The gates kill each pod when its case is done, and the Telegram gate
kills one mid-reply seconds after it starts. The kill used to drop the probe
on its way, so a pod killed before its first `docker exec` came back had no
probe at all and M failed though no probe had failed (probe lists
[[0 x48], [0], [], [0 x48]] on a loaded machine).

Each Pod class is driven with a stand-in `docker` on PATH (a probe takes as
long and exits as the case says; `rm` removes the container, after which an
exec still out exits 1, as docker's does):

  A  killed while its first probe is out: the probe comes back from the live
     container and counts, then the container goes
  B  a probe that fails (exit 7) is kept, so M still fails on it
  C  a probe still out after PROBE_SETTLE_S does not hold the kill: the
     container goes, the probe the removal cut short does not count, and the
     pod has no answer (M fails on it)
  D  the cleanup's stop() while a probe is out: the probe the removal cut
     short does not count
  E  a pod that lives two periods is probed twice, every probe 0
  F  check M in each of the three gates still asks for a probe in every pod
     and every probe 0, and reads them once every pod was killed through
     kill() (a pod still running could have its first probe still out)

No container, no database, no network.
"""
from __future__ import annotations

import asyncio
import importlib.util
import os
import pathlib
import re
import sys
import tempfile
import time

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "brain"), str(ROOT / "scripts")]

import connector_gate_kit as kit  # noqa: E402

fails: list[str] = []


def check(name: str, ok: bool, detail="") -> None:
    print(("  PASS  " if ok else "  FAIL  ") + name + ("" if ok else f"  -- {detail}"))
    if not ok:
        fails.append(name)


_TMP = tempfile.TemporaryDirectory(prefix="pod-probe-kill-")
BIN = pathlib.Path(_TMP.name, "bin")
BIN.mkdir()
LOG = pathlib.Path(_TMP.name, "docker.log")
GONE = pathlib.Path(_TMP.name, "gone")
# The stand-in docker: `exec` sleeps FAKE_PROBE_S, then exits FAKE_PROBE_RC,
# or 1 when the container was removed meanwhile; `rm` removes it.
(BIN / "docker").write_text(f"""#!/bin/sh
now() {{ python3 -c 'import time; print(time.monotonic())'; }}
case "$1" in
  exec)
    echo "exec-start $(now)" >> {LOG}
    sleep "${{FAKE_PROBE_S:-0}}"
    if [ -e {GONE} ]; then echo "exec-cut $(now)" >> {LOG}; exit 1; fi
    echo "exec-end $(now)" >> {LOG}
    exit "${{FAKE_PROBE_RC:-0}}" ;;
  rm) echo "rm $(now)" >> {LOG}; touch {GONE}; exit 0 ;;
  *) exit 0 ;;
esac
""")
(BIN / "docker").chmod(0o755)
os.environ["PATH"] = f"{BIN}:{os.environ['PATH']}"


def telegram_pod_class():
    spec = importlib.util.spec_from_file_location(
        "_tg_example_for_probe_gate", ROOT / "scripts" / "test_connector_telegram_example.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.Pod


class _Net:
    def close(self, pods) -> None:
        pass


def make(cls):
    pod = object.__new__(cls)
    pod.name, pod.port = "probe-gate-pod", 1
    pod._stop, pod._logs, pod.probes, pod.net = asyncio.Event(), None, [], _Net()
    pod._killed, pod._prober = False, None
    return pod


def start(pod):
    # What the gates' start_pod calls; a tree without probing() had the
    # gates create the task themselves.
    if hasattr(pod, "probing"):
        return pod.probing()
    return asyncio.create_task(pod.probe_forever())


async def kill(pod) -> None:
    # What the gates' cases call to kill a pod; a tree without kill() had
    # them call stop() in a thread.
    if hasattr(pod, "kill"):
        await pod.kill()
    else:
        await asyncio.to_thread(pod.stop)


def events() -> list[tuple[str, float]]:
    out = []
    for ln in LOG.read_text().splitlines() if LOG.exists() else []:
        k, t = ln.split()
        out.append((k, float(t)))
    return out


def fresh(probe_s: float, rc: int = 0) -> None:
    os.environ["FAKE_PROBE_S"], os.environ["FAKE_PROBE_RC"] = str(probe_s), str(rc)
    LOG.unlink(missing_ok=True)
    GONE.unlink(missing_ok=True)


def m_ok(pods) -> bool:
    # check M as the three gates write it
    return all(p.probes and not any(p.probes) for p in pods)


async def drive(label: str, cls) -> None:
    print(f"[{label}]")
    # A
    fresh(1.5)
    pod = make(cls)
    task = start(pod)
    await asyncio.sleep(0.3)
    await kill(pod)
    await asyncio.wait({task}, timeout=10)
    ev = events()
    ends = [t for k, t in ev if k == "exec-end"]
    rms = [t for k, t in ev if k == "rm"]
    check(f"A {label}: killed while its first probe is out, the probe comes back from the live "
          "container and counts", pod.probes == [0] and m_ok([pod]), (pod.probes, ev))
    check(f"A {label}: ...and the container goes after it",
          bool(ends and rms) and rms[0] > ends[0], ev)
    # B
    fresh(0.3, 7)
    pod = make(cls)
    task = start(pod)
    await asyncio.sleep(0.05)
    await kill(pod)
    await asyncio.wait({task}, timeout=10)
    check(f"B {label}: a failing probe is kept, and M fails on it",
          pod.probes == [7] and not m_ok([pod]), pod.probes)
    # C
    settle = getattr(kit, "PROBE_SETTLE_S", None)
    if settle is not None:
        kit.PROBE_SETTLE_S = 0.5
    try:
        fresh(2.5)
        pod = make(cls)
        task = start(pod)
        t0 = time.monotonic()
        await asyncio.sleep(0.05)
        await kill(pod)
        took = time.monotonic() - t0
        await asyncio.wait({task}, timeout=10)
    finally:
        if settle is not None:
            kit.PROBE_SETTLE_S = settle
    ev = events()
    check(f"C {label}: a probe still out after PROBE_SETTLE_S does not hold the kill, is cut "
          "short by the removal and does not count; M fails on the pod",
          took < 2.0 and pod.probes == [] and not m_ok([pod])
          and [k for k, _ in ev] == ["exec-start", "rm", "exec-cut"], (took, pod.probes, ev))
    # D
    fresh(1.0)
    pod = make(cls)
    task = start(pod)
    await asyncio.sleep(0.2)
    await asyncio.to_thread(pod.stop)
    await asyncio.wait({task}, timeout=10)
    check(f"D {label}: the cleanup's stop() while a probe is out: the cut-short probe does not "
          "count", pod.probes == [] and [k for k, _ in events()] == ["exec-start", "rm", "exec-cut"],
          (pod.probes, events()))
    # E
    fresh(0.05)
    pod = make(cls)
    task = start(pod)
    period = getattr(kit, "PROBE_PERIOD_S", 5)
    await asyncio.sleep(period + 2)
    await kill(pod)
    await asyncio.wait({task}, timeout=10)
    check(f"E {label}: a pod alive two periods is probed twice, every probe 0",
          pod.probes == [0, 0] and m_ok([pod]), pod.probes)


def part_f() -> None:
    print("[F]")
    for gate in ("telegram", "slack", "discord"):
        src = (ROOT / "scripts" / f"test_connector_{gate}_example.py").read_text()
        m = re.search(r'check\("M the kubelet\'s probe answered in every pod",\s*all\(p\.probes and '
                      r'not any\(p\.probes\)\s*for p in pods\)', src)
        check(f"F {gate}: check M asks for a probe in every pod and every probe 0", bool(m))
        check(f"F {gate}: every case kills its pod through kill(), never stop() directly",
              "await asyncio.to_thread(pod.stop)" not in src and src.count("await pod.kill()") >= 2)
        # A pod still running when M reads its probes may have its first one
        # still out (the defect killed pods had): no pod starts after the
        # last kill() and before M.
        at_m = src.find("check(\"M the kubelet's probe answered in every pod\"")
        last_kill = src.rfind("await pod.kill()", 0, at_m)
        check(f"F {gate}: check M reads the probes once every pod was killed through kill()",
              at_m > 0 and last_kill > 0 and "start_pod()" not in src[last_kill:at_m],
              src[last_kill:at_m][-300:] if last_kill > 0 else "no kill() before M")


async def main() -> None:
    await drive("connector_gate_kit.Pod", kit.Pod)
    await drive("the Telegram gate's Pod", telegram_pod_class())


if __name__ == "__main__":
    print("# a connector example gate's kill of a pod never cuts short the probe check M reads\n")
    asyncio.run(main())
    part_f()
    print()
    print("ALL GATES PASSED" if not fails else f"FAILED ({len(fails)}): " + repr(fails))
    sys.exit(1 if fails else 0)
