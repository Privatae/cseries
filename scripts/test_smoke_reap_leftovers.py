#!/usr/bin/env python3
"""Gate: a smoke run removes the pods and volumes an earlier, dead smoke run
left, and never a live run's (2026-10-06, 2026-10-08).

Measured 2026-10-06: cancelled CI runs' smoke pods (app + Postgres) were still
up 13 minutes, 52 minutes and 3 days later; smoke_test.sh reaped only volumes,
and skipped every volume a container still used. Then the
reaper that fixed that judged a run alive only by a readable
/proc/<pid>/cmdline, and the CI runner's jobs, three at once under
ProtectProc=invisible, cannot see each other's processes there: one CI run's
smoke job removed another run's live pod and both its volumes, and that run
failed on "No such container". The reaper now uses kernel_schema.py's rule through
scripts/gate_maker.sh, on the labels smoke_test.sh puts on everything it makes.

This drives the real scripts/smoke_reap_leftovers.sh against a fake `docker`
on PATH (scripts/gate_reap_fakes.py), a stand-in /proc (GATE_MAKER_PROC) and a
bash `kill` that answers -0 from a table and sends nothing:
  1. a dead run's four containers (app, pg, strict, strict-pg) are removed
  2. a live smoke run's containers are left alone
  3. a pid now held by a newer process does not keep a leftover alive
  4. containers and volumes not labelled as smoke's are never touched,
     whatever their names
  5. a dead run's volumes are removed once no container holds them; a live
     run's stay
  6. smoke_test.sh runs it before it starts its own pod, and labels every
     container and volume it makes
  7. a live run /proc does not show, which kill -0 finds, keeps its pod and
     volumes, and so does one kill -0 answers with "Operation not permitted"
  8. each removal names the container or volume, the pid of the run that made
     it, and why
The containers here carry the old pid-only names, which the reaper before
this one read, so the same scenario shows what it did; the reaper now reads
only the labels.
"""
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(os.environ.get("APP_ROOT") or Path(__file__).resolve().parents[1])
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import kernel_schema as ks  # noqa: E402
from gate_reap_fakes import PID_BASE, FakeDocker, FakeProc, bash_kill, maker  # noqa: E402

FAILS = []


def check(name, ok, detail=""):
    print(("PASS  " if ok else "FAIL  ") + name + ("" if ok or not detail else f"  {str(detail)[:700]}"))
    if not ok:
        FAILS.append(name)


NS, BOOT = str(ks.PIDNS), ks.BOOT_ID
if not ks.PIDNS or not BOOT:
    print("cannot run: no PID namespace or boot id here, so the rule judges nothing (exit 2)")
    sys.exit(2)

# D: dead. L: alive, /proc shows it. H: alive, /proc does not show it (another
# CI job). E: kill -0 says "Operation not permitted". O: its pid is a newer
# process now. U: an unlabelled leftover with a dead pid.
D, L, H, E, O, U = (PID_BASE + i for i in range(1, 7))
KILL = {D: "esrch", L: "alive", H: "alive", E: "eperm", O: "alive", U: "esrch"}


def smoke(pid) -> dict:
    return {"labels": maker("smoke", pid, NS, "500", BOOT)}


containers = {
    f"cseries-smoke-{D}": {**smoke(D), "volumes": [f"cseries-smoke-pgsock-{D}"]},
    f"cseries-smoke-pg-{D}": {**smoke(D), "volumes": [f"cseries-smoke-pgsock-{D}", f"cseries-smoke-pgdata-{D}"]},
    f"cseries-smoke-strict-{D}": smoke(D),
    f"cseries-smoke-strict-pg-{D}": {**smoke(D), "volumes": [f"cseries-smoke-strict-pgdata-{D}"]},
    f"cseries-smoke-{L}": {**smoke(L), "volumes": [f"cseries-smoke-pgsock-{L}"]},
    f"cseries-smoke-pg-{L}": {**smoke(L), "volumes": [f"cseries-smoke-pgsock-{L}", f"cseries-smoke-pgdata-{L}"]},
    f"cseries-smoke-{H}": {**smoke(H), "volumes": [f"cseries-smoke-pgsock-{H}"]},
    f"cseries-smoke-pg-{H}": {**smoke(H), "volumes": [f"cseries-smoke-pgsock-{H}", f"cseries-smoke-pgdata-{H}"]},
    f"cseries-smoke-{E}": smoke(E),
    f"cseries-smoke-{O}": smoke(O),
    f"cseries-smoke-{U}": {},
    "kernel-schema-123-456": {"labels": maker("kernel-schema", D, NS, "500", BOOT)},
    "cseries-smoke-notapid": {},
    "my-app": {},
}
volumes = {v: smoke(int(v.rsplit("-", 1)[1])) for v in (
    f"cseries-smoke-pgsock-{D}", f"cseries-smoke-pgdata-{D}", f"cseries-smoke-strict-pgdata-{D}",
    f"cseries-smoke-pgsock-{L}", f"cseries-smoke-pgdata-{L}", f"cseries-smoke-pgsock-{H}", f"cseries-smoke-pgdata-{H}")}
volumes[f"cseries-smoke-pgdata-{U}"] = {}

with tempfile.TemporaryDirectory() as td:
    td = Path(td)
    docker = FakeDocker(td / "docker")
    docker.load(containers, volumes)
    proc = FakeProc(td / "proc")
    proc.show(L, "S", "500", "smoke_test.sh")
    proc.show(O, "S", "900", "sleep")
    kill_log = td / "kill.log"
    env = docker.env({**os.environ, "GATE_MAKER_PROC": str(proc.root)})
    if not docker.is_the_docker(env):
        print(f"FAIL: `docker` on PATH is not the fake ({env['PATH']}); nothing was run")
        sys.exit(1)
    # Run as smoke_test.sh runs it (its own bash), with the stand-in kill
    # exported to it: bash runs a function before its builtin.
    r = subprocess.run(["bash", "-c", bash_kill(KILL, kill_log) + 'export -f kill\nbash "$1"', "reap",
                        str(SCRIPTS / "smoke_reap_leftovers.sh")],
                       capture_output=True, text=True, env=env, timeout=60)
    left_c, left_v = docker.left()
    calls = docker.calls()
    out = r.stdout

    check("the script exits 0", r.returncode == 0, r.stderr[-300:])
    check("1: a dead run's app, pg, strict and strict-pg containers are removed",
          not {n for n in containers if n.endswith(f"-{D}") and n.startswith("cseries-smoke-")} & left_c,
          f"left={sorted(left_c)}")
    check("2: a live smoke run's containers stay", {f"cseries-smoke-{L}", f"cseries-smoke-pg-{L}"} <= left_c)
    check("3: a pid now held by a newer process does not keep a leftover alive", f"cseries-smoke-{O}" not in left_c)
    check("4: containers and volumes not labelled as smoke's are never touched, whatever their names",
          {f"cseries-smoke-{U}", "kernel-schema-123-456", "cseries-smoke-notapid", "my-app"} <= left_c
          and f"cseries-smoke-pgdata-{U}" in left_v
          and not {f"cseries-smoke-{U}", "kernel-schema-123-456", "cseries-smoke-notapid", "my-app",
                   f"cseries-smoke-pgdata-{U}"} & set(docker.removed()), docker.removed())
    check("5: a dead run's volumes go once their containers are gone",
          not {v for v in left_v if v.endswith(f"-{D}")}, f"left={sorted(left_v)}")
    check("5: a live run's volumes stay", {f"cseries-smoke-pgsock-{L}", f"cseries-smoke-pgdata-{L}"} <= left_v)
    check("7: a live run /proc does not show, which kill -0 finds, keeps its pod and its volumes",
          {f"cseries-smoke-{H}", f"cseries-smoke-pg-{H}"} <= left_c
          and {f"cseries-smoke-pgsock-{H}", f"cseries-smoke-pgdata-{H}"} <= left_v, (sorted(left_c), sorted(left_v)))
    check('7: ...and so does one kill -0 answers with "Operation not permitted"', f"cseries-smoke-{E}" in left_c)
    line = next((ln for ln in out.splitlines() if f"container cseries-smoke-{D}:" in ln), "")
    check("8: each removal names the container, the pid of the run that made it, and why",
          f"pid {D}" in line and "no such process" in line, out[-600:])
    vline = next((ln for ln in out.splitlines() if f"volume cseries-smoke-pgdata-{D}:" in ln), "")
    check("8: ...and each volume removal too", f"pid {D}" in vline and "no such process" in vline, out[-600:])
    check("no docker call the fake does not know", not any(
        c[:1] not in (["ps"], ["rm"]) and c[:2] not in (["volume", "ls"], ["volume", "rm"]) for c in calls), calls)
    kills = kill_log.read_text().splitlines() if kill_log.exists() else []
    check("kill was only ever asked -0", all(k.startswith("kill -0 ") for k in kills), kills)

smoke_sh = (SCRIPTS / "smoke_test.sh").read_text(encoding="utf-8")
reap_at = smoke_sh.find('bash "$SMOKE_REPO/scripts/smoke_reap_leftovers.sh"')
first_pod = smoke_sh.find('docker run --name "$PGNAME"')
check("6: smoke_test.sh reaps before it starts its own pod", 0 < reap_at < first_pod, f"reap@{reap_at} pod@{first_pod}")
check("6: ...after SMOKE_REPO is set", 0 < smoke_sh.find("SMOKE_REPO=") < reap_at)
labelled_at = smoke_sh.find("gate_maker_args smoke")
check("6: it takes this run's maker labels from scripts/gate_maker.sh before it makes anything",
      0 < smoke_sh.find('source "$SMOKE_REPO/scripts/gate_maker.sh"') < labelled_at
      < smoke_sh.find("docker volume create") and "SMOKE_LABELS=(\"${GATE_MAKER_ARGS[@]}\")" in smoke_sh)
makes = [ln.strip() for ln in smoke_sh.splitlines()
         if re.match(r'\s*docker (run --name "\$\w+"|volume create)', ln)]
check("6: every container and volume it makes carries them",
      len(makes) == 7 and all('"${SMOKE_LABELS[@]}"' in m for m in makes)
      and re.search(r'--name "\$CNAME"\s*\n\s*"\$\{SMOKE_LABELS\[@\]\}"', smoke_sh), makes)
names = re.findall(r'^\s*(?:CNAME|PGNAME|PGSOCK_VOL|PGDATA_VOL|SCNAME|SPGNAME|SPGSOCK_VOL|SPGDATA_VOL)="([^"]*)"',
                   smoke_sh, re.M)
check("6: its names hold the start time, so the reaper before this one (pid-only names) matches none",
      len(names) == 8 and all(n.endswith("-$SMOKE_RUN") for n in names), names)

print()
if FAILS:
    print(f"FAIL: {len(FAILS)} check(s)")
    sys.exit(1)
print("PASS: a smoke run removes what a dead one left, and nothing else")
