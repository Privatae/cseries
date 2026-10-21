#!/usr/bin/env python3
"""The local runtime's machine guard (vm/runtime/lib/cseries_runtime/guard.py).

  * the reserve and budget match the bastion's coder guard vectors
    (client-go coders_linux_test.go TestCoderReserveFollowsTheGKETiers): the
    3.7 GiB machine keeps 1055 MiB;
  * a container that does not fit is refused with the operator's 402 text,
    which the Host's own parsers read back into needs_mi and headroom_mi;
  * a machine already stalling (PSI full avg10 >= 60%) refuses a new one;
  * the watchdog stops one container when free memory is under 100 MiB, or
    after 30 s of pressure with 10 s between stops; coding engines first, then
    the browser, then apps, resident apps last, newest first within each;
  * the disk floors: a wake is refused under 2 GiB free, naming each app's
    data; under the stop floor (never less than the database's own WAL
    allowance, 1280 MiB) engines go first, then the browser, then apps with a
    data volume, the one whose data grew last first;
  * PSI and meminfo parse from fixture text.

Container-free: python3 scripts/test_runtime_guard.py
"""
import json
import os
import re
import sys
import tempfile

ROOT = os.environ.get("APP_ROOT") or os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(ROOT, "vm/runtime/lib"))
from cseries_runtime import guard as G  # noqa: E402

failed = 0


def check(cond, what, detail=""):
    global failed
    if cond:
        print(f"OK    {what}")
    else:
        failed = 1
        print(f"FAIL  {what} {detail}")


for total, want in ((512, 355), (3819, 1055), (4096, 1124), (8192, 1944), (16384, 2763),
                    (131072, 9644), (262144, 12266)):
    check(G.reserve_mb(total) == want and G.budget_mb(total) == total - want,
          f"reserve for {total} MiB is {want} (the bastion's vector)", G.reserve_mb(total))

MEMINFO = "MemTotal:        3910656 kB\nMemFree:          123456 kB\nMemAvailable:    2097152 kB\n"
check(G.parse_meminfo(MEMINFO) == (3819, 2048), "meminfo parses to MiB", G.parse_meminfo(MEMINFO))
check(G.parse_meminfo("garbage") == (None, None), "unreadable meminfo gives nothing")
PSI = "some avg10=12.50 avg60=3.00 avg300=1.00 total=100\nfull avg10=61.25 avg60=2.00 avg300=0.50 total=50\n"
check(G.parse_psi_full(PSI) == 61.25, "PSI full avg10 parses", G.parse_psi_full(PSI))
check(G.parse_psi_full("some avg10=1.00 avg60=0 avg300=0 total=1\n") is None, "no full line gives nothing")

# ── fitting ─────────────────────────────────────────────────────────────────

with tempfile.TemporaryDirectory() as tmp:
    psi = os.path.join(tmp, "psi")
    open(psi, "w").write("full avg10=0.00 avg60=0.00 avg300=0.00 total=0\n")
    g = G.Guard(total_mb=3819, psi_path=psi)
    budget = 3819 - 1055
    units = [{"kind": "harness", "ident": "codex", "memory_mi": 1024},
             {"kind": "app", "ident": "telegram", "memory_mi": 192}]
    check(g.headroom(units) == budget - 1216, "headroom is the budget less every limit", g.headroom(units))
    check(g.refusal(1024, units) is None, "a 1 GiB engine fits the 3.7 GiB machine beside them")
    check(g.refusal(1024, units, in_flight=[1024]) is not None, "a wake in flight counts against the budget")
    r = g.refusal(4864, units)
    check(r is not None and r.startswith("operator /harness/wake 402: "),
          "eight slots (1024 + 7 x 576 MiB) do not fit and are refused in the operator's shape", r)
    m = re.search(r"/harness/wake 402: (\{.*\})\s*$", r, re.S)
    d = json.loads(m.group(1))["detail"] if m else {}
    check(d.get("error") == "capability_over_plan" and d.get("needs_mi") == 4864 and
          d.get("headroom_mi") == budget - 1216 and d.get("scope") == "machine" and d.get("upgrade_to") is None,
          "the Host's own regex reads needs_mi, headroom_mi and the machine scope back", d)
    # installer._over_plan_detail reads from the first brace: the same detail.
    body = json.loads(r[r.find("{"):])
    check(body["detail"] == d, "installer._over_plan_detail's reading gives the same numbers")
    open(psi, "w").write(PSI)
    r = g.refusal(64, [])
    check(r is not None and '"reason": "pressure"' in r,
          "a machine stalling on memory refuses even a small container", r)

# ── the watchdog ────────────────────────────────────────────────────────────


def u(kind, ident, started, resident=False):
    return {"kind": kind, "ident": ident, "memory_mi": 128, "started": started,
            "resident": resident, "running": True}


units = [u("app", "telegram", 1, resident=True), u("app", "photocraft", 5), u("harness", "browser", 3),
         u("harness", "codex", 2), u("harness", "opencode", 4)]
order = [x["ident"] for x in G.Guard.stop_order(units)]
check(order == ["opencode", "codex", "browser", "photocraft", "telegram"],
      "stop order: engines newest first, then the browser, then apps, resident last", order)

g = G.Guard(total_mb=3819)
check(g.round(100.0, units, avail_mb=500, psi=10.0) == (None, None), "a healthy machine stops nothing")
v, why = g.round(101.0, units, avail_mb=80, psi=10.0)
check(v and v["ident"] == "opencode" and "80 MiB" in why, "under 100 MiB free, the newest engine is stopped",
      (v, why))
g = G.Guard(total_mb=3819)
check(g.round(0.0, units, avail_mb=900, psi=70.0) == (None, None), "pressure starts its 30 s clock")
check(g.round(29.0, units, avail_mb=900, psi=70.0) == (None, None), "29 s of pressure stops nothing")
v, why = g.round(30.5, units, avail_mb=900, psi=70.0)
check(v and v["ident"] == "opencode" and "stalled" in why, "30 s of pressure stops the newest engine", why)
rest = [x for x in units if x["ident"] != "opencode"]
check(g.round(35.0, rest, avail_mb=900, psi=70.0) == (None, None),
      "the next stop waits 10 s for the last one to show")
v, _ = g.round(41.0, rest, avail_mb=900, psi=70.0)
check(v and v["ident"] == "codex", "then the next engine goes", v)
check(g.round(42.0, rest, avail_mb=900, psi=5.0) == (None, None) and g.over_since is None,
      "pressure that ends resets its clock")
only_apps = [u("app", "telegram", 1, resident=True), u("app", "deckcraft", 2)]
v, _ = G.Guard(total_mb=3819).round(1.0, only_apps, avail_mb=50)
check(v and v["ident"] == "deckcraft", "an app the owner opened goes before a resident one", v)
check(G.Guard(total_mb=3819).round(1.0, [], avail_mb=10) == (None, None), "nothing running, nothing to stop")

# ── disk ────────────────────────────────────────────────────────────────────

v, why = G.Guard(total_mb=3819).round(1.0, units, avail_mb=900, psi=1.0, disk=900 << 20)
check(v and v["ident"] == "opencode" and "disk" in why, "under the disk floor the newest engine goes first", why)

# The Host's database is on this disk. Its own settings let its WAL grow by
# max_wal_size between checkpoints plus max_slot_wal_keep_size for an offline
# mirror, with nothing wrong, and a WAL write that finds no room stops it: the
# stop floor is never under that (the old 1 GiB floor was).
pg = open(os.path.join(ROOT, "entrypoint-postgres.sh"), encoding="utf-8").read()


def pg_mb(name):
    m = re.search(rf"^{name} = (\d+)MB$", pg, re.M)
    return int(m.group(1)) if m else None


wal, slot = pg_mb("max_wal_size"), pg_mb("max_slot_wal_keep_size")
check(wal and slot and getattr(G, "POSTGRES_DISK_RESERVE", 0) == (wal + slot) << 20 and
      G.DISK_STOP_FLOOR >= (wal + slot) << 20 and G.DISK_WAKE_FLOOR > G.DISK_STOP_FLOOR,
      "the disk stop floor keeps the database's own WAL allowance free, and the wake floor is above it",
      (wal, slot, G.DISK_STOP_FLOOR >> 20))


def app(ident, started, *, data=None, grew=None, resident=False, has_data=True):
    x = u("app", ident, started, resident=resident)
    x.update(has_data=has_data, data_bytes=data, data_grew_at=grew)
    return x


apps = [app("telegram", 1, data=900 << 20, grew=50.0, resident=True),
        app("notes", 2, data=10 << 20, grew=80.0),
        app("photos", 3, data=2 << 30, grew=None),
        app("weather", 4, has_data=False)]
order = [x["ident"] for x in G.Guard.disk_stop_order(apps + [u("harness", "browser", 9),
                                                             u("harness", "codex", 2)])]
check(order == ["codex", "browser", "notes", "telegram", "photos"],
      "on a short disk: engines, the browser, then the app whose data grew most recently; "
      "an app with no data volume is left running", order)
v, why = G.Guard(total_mb=3819).round(1.0, apps, avail_mb=900, psi=1.0, disk=900 << 20)
check(v and v["ident"] == "notes" and "Host's database" in why,
      "with no engine running, the disk floor stops the app whose data grew last", (v, why))
v, _ = G.Guard(total_mb=3819).round(1.0, apps, avail_mb=900, psi=1.0, disk=1100 << 20)
check(v and v["ident"] == "notes", "and does so at 1100 MiB free, inside the database's reserve", v)
check(G.Guard(total_mb=3819).round(1.0, apps, avail_mb=900, psi=1.0, disk=3 << 30) == (None, None),
      "above the floor nothing is stopped for the disk")
check(G.Guard(total_mb=3819).round(1.0, [apps[3]], avail_mb=900, psi=1.0, disk=500 << 20) == (None, None),
      "an app that cannot write to the disk is not stopped for it")
txt = G.Guard.disk_text(1500 << 20, [{"app_id": "notes", "bytes": 10 << 20, "installed": True},
                                     {"app_id": "photos", "bytes": 3 << 29, "installed": False,
                                      "complete": False}])
check("1500 MiB of disk free" in txt and "2 GiB" in txt and "photos at least 1.5 GiB (removed" in txt and
      "notes 10 MiB" in txt and txt.index("photos") < txt.index("notes") and "App Store" in txt,
      "a wake's refusal names the floor, what is free, and each app's data, largest first", txt)

with tempfile.TemporaryDirectory() as tmp:
    os.makedirs(os.path.join(tmp, "a", "b"))
    with open(os.path.join(tmp, "a", "b", "f"), "wb") as f:
        f.write(os.urandom(256 << 10))
    with open(os.path.join(tmp, "g"), "wb") as f:
        f.write(os.urandom(64 << 10))
    big = os.path.join(tmp, "elsewhere")
    os.makedirs(big)
    with open(os.path.join(big, "h"), "wb") as f:
        f.write(os.urandom(1 << 20))
    os.symlink(big, os.path.join(tmp, "a", "link"))
    sub = os.path.join(tmp, "a")
    n, complete = G.dir_bytes(sub)
    check(complete and (256 << 10) <= n < (1 << 20), "dir_bytes counts what a tree holds, never through a link",
          n)
    n2, complete2 = G.dir_bytes(tmp, max_entries=2)
    check(not complete2, "past its entry cap the count says it is a floor", (n2, complete2))


class FakeStat:
    f_bavail = (1 << 30) // 4096
    f_frsize = 4096


orig = os.statvfs
os.statvfs = lambda p: FakeStat()
try:
    r = G.Guard(total_mb=3819).disk_refusal()
    check(r is not None and "1024 MiB of disk free" in r, "under 2 GiB of disk a wake is refused", r)
    FakeStat.f_bavail = (3 << 30) // 4096
    check(G.Guard(total_mb=3819).disk_refusal() is None, "with 3 GiB free a wake goes ahead")
finally:
    os.statvfs = orig

# ── one memory budget: the Host's and its database's ceilings come first ─────

SHOW = ("Id=cseries-host.service\nLoadState=loaded\nMemoryMax=644874240\n\n"
        "Id=cseries-postgres.service\nLoadState=loaded\nMemoryMax=536870912\n")
check(G.parse_held(SHOW) == 615 + 512, "the Host's and the database's MemoryMax, in MiB, together",
      G.parse_held(SHOW))
check(G.parse_held(SHOW.replace("MemoryMax=644874240", "MemoryMax=644874241")) == 616 + 512,
      "a part of a MiB counts as a whole one")
NOTFOUND = ("Id=cseries-host.service\nLoadState=not-found\nMemoryMax=infinity\n\n"
            "Id=cseries-postgres.service\nLoadState=not-found\nMemoryMax=infinity\n")
check(G.parse_held(NOTFOUND) == 0, "units not on this machine (a guest not configured yet) hold nothing")
for text, why in ((SHOW.replace("MemoryMax=644874240", "MemoryMax=infinity"), "a Host with no limit"),
                  (SHOW.replace("MemoryMax=536870912", "MemoryMax=5e8"), "a value that is not a count"),
                  (SHOW.split("\n\n")[0], "an answer missing a unit"), ("", "no answer")):
    check(G.parse_held(text) is None, f"{why} may take everything: nothing is admitted", G.parse_held(text))
with tempfile.TemporaryDirectory() as tmp:
    check(G.read_held(booted=os.path.join(tmp, "no-systemd")) == 0,
          "a system systemd does not run has no units, and they hold nothing")

# The same read against systemd itself, where this runs as root on a system
# systemd runs: two transient units with the ceilings the Host's and the
# database's have on a 4 GiB system. Elsewhere it says it was not run.
if os.geteuid() == 0 and os.path.isdir("/run/systemd/system"):
    import subprocess as _sp  # noqa: E402
    names = (f"cseries-guard-test-{os.getpid()}-h.service", f"cseries-guard-test-{os.getpid()}-p.service")
    started = all(_sp.run(["systemd-run", "--quiet", f"--unit={n}", "-p", f"MemoryMax={mx}", "sleep", "30"],
                          capture_output=True, check=False).returncode == 0
                  for n, mx in zip(names, ("615M", "512M")))
    try:
        if started:
            check(G.read_held(units=names) == 615 + 512, "read from systemd: the two units' MemoryMax together",
                  G.read_held(units=names))
            check(G.read_held(units=(names[0], "cseries-guard-test-none.service")) == 615,
                  "read from systemd: a unit not loaded here holds nothing")
        else:
            print("SKIP  read from systemd: systemd-run could not start a transient unit")
    finally:
        _sp.run(["systemctl", "stop", *names], capture_output=True, check=False)
else:
    print("SKIP  read from systemd: not root on a system systemd runs")

t = [0.0]
reads = []


def held():
    reads.append(t[0])
    return 615 + 512
g = G.Guard(total_mb=4096, held=held, clock=lambda: t[0])
check(g.budget() == 4096 - 1124 - 615 - 512 and g.held_mb() == 1127,
      "the budget is the machine's less the Host's and the database's ceilings", (g.budget(), g.held_mb()))
units = [{"kind": "harness", "ident": "browser", "memory_mi": 1024}]
check(g.headroom(units) == 4096 - 1124 - 1127 - 1024 and g.refusal(1024, units) is not None,
      "a second 1 GiB container beside the browser does not fit what the Host leaves", g.headroom(units))
check(g.refusal(512, units) is None, "half that does", g.headroom(units))
t[0] = 4.9
g.budget()
t[0] = 5.0
g.budget()
check(reads == [0.0, 5.0], "the ceilings are read again only after 5 s", reads)
check(G.Guard(total_mb=4096, held=lambda: None).budget() == 0,
      "a Host that may take everything leaves nothing to admit into")
check(G.Guard(total_mb=1024, held=lambda: 1024).budget() == 0, "the budget is never under 0")
r = G.Guard(total_mb=4096, held=lambda: 1127).refusal(2048, [])
check(r is not None and json.loads(r[r.find("{"):])["detail"]["headroom_mi"] == 4096 - 1124 - 1127,
      "the refusal's headroom is what the Host leaves", r)

# The Host's ceiling the cseries command (packages/cseries/src/host.js) and a
# cloud box (cseries_cloud.host_memory_max) give a guest with the runtime are
# one rule, and with it the Host, the database, the runtime's containers and
# the system's reserve fit in the machine together.
import subprocess  # noqa: E402
sys.path.insert(0, os.path.join(ROOT, "vm/cloud/overlay/usr/local/lib/cseries-cloud"))
import cseries_cloud as CC  # noqa: E402
KB = [3910656, 8126464, 4 * 1024 * 1024 - 123, 16316412] + [g_ * 1024 * 1024 for g_ in
                                                            (1, 2, 3, 4, 6, 8, 12, 16, 24, 32, 64, 128, 256)]
js = subprocess.run(["node", "-e", "const h = require(process.argv[1]); console.log(JSON.stringify("
                     "process.argv.slice(2).map((k) => [h.memoryMax(k, {runtime: true}), h.containersMax(k)])))",
                     os.path.join(ROOT, "packages/cseries/src/host.js"), *map(str, KB)],
                    capture_output=True, text=True, check=False)
got = json.loads(js.stdout) if js.returncode == 0 else None
check(got is not None, "host.js answers for every size", js.stderr[-300:])
for kb, (js_max, js_apps) in zip(KB, got or []):
    py_max = CC.host_memory_max(kb)
    total = kb // 1024
    host_mi = int(py_max[:-1])
    budget = G.Guard(total_mb=total, held=lambda: host_mi + 512).budget()
    # On a system too small for the Host's floor beside the database and the
    # reserve (1 GiB), the floor stands and the containers get nothing.
    check(js_max == py_max and abs(budget - js_apps) <= 2
          and budget == max(0, total - G.reserve_mb(total) - host_mi - 512)
          and (budget == 0 or host_mi + 512 + budget + G.reserve_mb(total) <= total),
          f"{kb} kB: the Host {py_max} from both, the containers {budget} MiB, and all of it fits",
          (js_max, py_max, js_apps, budget, G.reserve_mb(total)))

# The busiest live Host measured (7-day max working set 901 MiB; host.js
# HOST_NEED) fits under the ceiling a cloud box gives its Host from 3 GiB up.
for g_ in (3, 4, 6, 8, 16):
    got_ = int(CC.host_memory_max(g_ * 1024 * 1024)[:-1])
    check(901 <= got_ <= 2048, f"a {g_} GiB box: the Host's {got_} MiB holds the busiest measured Host", got_)

print()
print("FAIL" if failed else "all runtime guard checks passed")
sys.exit(failed)
