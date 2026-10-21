"""The machine guard: the containers this runtime starts never take the
memory the machine itself needs.

The numbers are the bastion's coder guard, so the two programs that start
containers on an owner's machines decide the same way. A machine of total
MiB keeps a reserve for itself: GKE's node reservation (255 MiB under 1 GiB;
otherwise 25% of the first 4 GiB, 20% of the next 4, 10% of the next 8, 6% of
the next 112 and 2% above 128 GiB) plus the kubelet's 100 MiB eviction floor.
What is left is the machine's one memory budget, and the Host and its
database hold their share of it first: each one's MemoryMax, as systemd holds
it for cseries-host.service and cseries-postgres.service (the Host's from
10-memory.conf, which configure writes; the database's 512 MiB). The rest is
what this runtime admits containers into: every running container's limit,
plus every wake in flight, must fit in it, and the machine must not already
be stalling (PSI memory "full" avg10 under 60%). So no MiB is promised twice:
a container admitted here never takes memory the Host or its database is
allowed to use.

A watchdog round every 2 s stops one container, through its cgroup, when the
machine has under 100 MiB available, or when memory pressure has stayed over
60% for 30 s and the last stop has had 10 s to show (systemd-oomd's
defaults). The order is coding engines first, then the browser, then apps the
owner opens, and apps that run all the time last; newest first within each,
because the newest has done the least work. The limits already keep the
containers inside the budget, so this acts when something outside them grew
(the Host, its database, the system).

Disk. The Host's database lives on the same disk as every container's volumes
(the cseries_pgdata volume, under /var/lib/containers), and an app's data
volume has no size limit, so one app could fill it. A wake is refused while
the disk has under DISK_WAKE_FLOOR free, with the space each app's data takes
named in the refusal. Under DISK_STOP_FLOOR the watchdog stops, one a round,
whatever is writing to that disk: coding engines (newest first; their
per-wake volumes go with them), the browser, then apps with a data volume, the
one whose data grew most recently first. It never stops the Host or its
database, which it did not start. A stop keeps the app's data: nothing is
deleted unless the owner chooses it (workspace.delete_app_data).
"""

import json
import math
import os
import subprocess
import time

from . import common as C

EVICT_FLOOR_MB = 100
PRESSURE_LIMIT = 60.0
PRESSURE_FOR_S = 30.0
PRESSURE_SETTLE_S = 10.0
WATCH_EVERY_S = 2.0
DISK_WAKE_FLOOR = 2 << 30
# What the Host's database may need from the disk with nothing wrong, by its own
# settings (entrypoint-postgres.sh write_pg_conf): max_wal_size = 256MB of WAL
# between checkpoints, plus max_slot_wal_keep_size = 1024MB a replication slot
# may hold for a mirror whose machine is off. PostgreSQL stops (PANIC) when a
# WAL write finds no space, so the stop floor is never under this. The old
# floor, 1 GiB, was.
POSTGRES_DISK_RESERVE = (256 + 1024) << 20
DISK_STOP_FLOOR = max(1 << 30, POSTGRES_DISK_RESERVE)
# How often the watchdog measures the apps' data while the disk is short
# (under twice the wake floor), so it knows whose data grew last.
DATA_SAMPLE_EVERY_S = 30.0
DATA_WATCH_BELOW = 2 * DISK_WAKE_FLOOR


def fmt_bytes(n):
    """A size in plain words: 512 KiB, 37 MiB, 1.4 GiB."""
    n = int(n or 0)
    if n >= 1 << 30:
        return f"{n / (1 << 30):.1f} GiB"
    if n >= 1 << 20:
        return f"{n >> 20} MiB"
    return f"{max(0, n) >> 10} KiB"


def data_words(data):
    """Each app's data, largest first, in words: "telegram 1.2 GiB, weather 30
    MiB (removed; its data is kept)". data: [{app_id, bytes, installed,
    complete}]."""
    parts = []
    for d in sorted(data, key=lambda d: -int(d.get("bytes") or 0)):
        size = ("at least " if not d.get("complete", True) else "") + fmt_bytes(d.get("bytes"))
        parts.append(f"{d['app_id']} {size}" + ("" if d.get("installed") else
                                                " (removed; its data is kept)"))
    return ", ".join(parts)

_TIERS = ((4 << 10, 0.25), (8 << 10, 0.20), (16 << 10, 0.10), (128 << 10, 0.06), (1 << 31, 0.02))


def reserve_mb(total_mb):
    """What a machine of total_mb keeps for itself (the bastion's
    coderReserveMB)."""
    if total_mb < 1024:
        return 255 + EVICT_FLOOR_MB
    r, low = 0.0, 0
    for up_to, frac in _TIERS:
        if total_mb <= low:
            break
        r += (min(total_mb, up_to) - low) * frac
        low = up_to
    return int(math.ceil(r)) + EVICT_FLOOR_MB


def budget_mb(total_mb):
    return total_mb - reserve_mb(total_mb)


# The units whose own ceilings come out of the budget before any container's.
HELD_UNITS = ("cseries-host.service", "cseries-postgres.service")
# Their ceilings change only when configure writes a drop-in and the unit is
# restarted, so they are read again at most this often.
HELD_READ_EVERY_S = 5.0


def parse_held(text, units=HELD_UNITS):
    """MiB the units' MemoryMax hold together, from `systemctl show -p Id -p
    LoadState -p MemoryMax <units>`; None when one of them may take
    everything. A unit not on this machine (LoadState not-found: a guest not
    configured yet) holds nothing. A loaded unit with no limit (infinity), or
    a value that is not a number, may take all the memory there is, so nothing
    is left to admit into."""
    blocks = {}
    for block in text.split("\n\n"):
        kv = dict(line.split("=", 1) for line in block.splitlines() if "=" in line)
        if kv.get("Id"):
            blocks[kv["Id"]] = kv
    held = 0
    for unit in units:
        kv = blocks.get(unit)
        if kv is None:
            return None
        if kv.get("LoadState") == "not-found":
            continue
        v = kv.get("MemoryMax", "")
        if not (v.isascii() and v.isdigit()):
            return None
        held += -(-int(v) // (1 << 20))
    return held


def read_held(units=HELD_UNITS, booted="/run/systemd/system"):
    # Not a system systemd runs (sd_booted's test): no unit runs here, so none
    # holds anything. Every machine the runtime is installed on runs systemd.
    if not os.path.isdir(booted):
        return 0
    try:
        r = subprocess.run(["systemctl", "show", "-p", "Id", "-p", "LoadState", "-p", "MemoryMax",
                            *units], capture_output=True, text=True, timeout=5, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    return parse_held(r.stdout, units) if r.returncode == 0 else None


def parse_meminfo(text):
    """(MemTotal, MemAvailable) in MiB, or (None, None)."""
    total = avail = None
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[1].isdigit():
            if parts[0] == "MemTotal:":
                total = int(parts[1]) // 1024
            elif parts[0] == "MemAvailable:":
                avail = int(parts[1]) // 1024
    return total, avail


def parse_psi_full(text):
    """The "full avg10" share from a /proc/pressure file, or None."""
    for line in text.splitlines():
        parts = line.split()
        if parts and parts[0] == "full":
            for kv in parts[1:]:
                if kv.startswith("avg10="):
                    try:
                        return float(kv[len("avg10="):])
                    except ValueError:
                        return None
    return None


def over_plan_text(needs_mi, headroom_mi, reason=None):
    """A refusal in the shape the Host reads from the platform operator
    (harness_backend._OVER_PLAN_RE, installer._over_plan_detail): the bridge's
    `operator /harness/wake 402: {json}`. `scope: machine` tells the Host it
    was this machine, not a plan, that had no room."""
    detail = {"error": "capability_over_plan", "needs_mi": int(needs_mi),
              "headroom_mi": max(0, int(headroom_mi)), "upgrade_to": None, "pool_add_mi": None,
              "scope": "machine"}
    if reason:
        detail["reason"] = reason
    return "operator /harness/wake 402: " + json.dumps({"detail": detail}, sort_keys=True)


def read_text(path):
    try:
        with open(path, encoding="ascii", errors="replace") as f:
            return f.read()
    except OSError:
        return ""


def disk_free(path):
    try:
        st = os.statvfs(path)
    except OSError:
        return None
    return st.f_bavail * st.f_frsize


MAX_WALK_ENTRIES = 1_000_000


def dir_bytes(path, max_entries=MAX_WALK_ENTRIES):
    """(bytes the tree at path takes on disk, whether every entry was counted).
    Allocated blocks, so a sparse file counts what it holds; symlinks are not
    followed; past max_entries the count stops and is a floor."""
    total, seen = 0, 0
    stack = [path]
    while stack:
        d = stack.pop()
        try:
            it = os.scandir(d)
        except OSError:
            continue
        with it:
            for e in it:
                seen += 1
                if seen > max_entries:
                    return total, False
                try:
                    st = e.stat(follow_symlinks=False)
                except OSError:
                    continue
                total += getattr(st, "st_blocks", 0) * 512 or st.st_size
                if e.is_dir(follow_symlinks=False):
                    stack.append(e.path)
    return total, True


class Guard:
    """The arithmetic and the watchdog's decision. The server owns the
    units; each is a dict with at least kind ("app"/"harness"), ident,
    memory_mi, started (monotonic) and resident (bool)."""

    def __init__(self, meminfo_path="/proc/meminfo", psi_path="/proc/pressure/memory",
                 disk_path="/var/lib/containers", total_mb=None, held=read_held,
                 clock=time.monotonic):
        self.meminfo_path = meminfo_path
        self.psi_path = psi_path
        self.disk_path = disk_path
        # Other places the runtime writes to: the engine builds' own store
        # (build.Builder.bind). Each floor holds for the fullest of them.
        self.disk_paths = []
        self._total = total_mb
        # held(): the MiB the Host's and its database's ceilings take, or
        # None when one may take everything (parse_held). A test gives its own.
        self._held = held
        self._held_at = None
        self._held_mb = None
        self.clock = clock
        self.over_since = None
        self.last_stop = None

    def total_mb(self):
        if self._total is not None:
            return self._total
        total, _ = parse_meminfo(read_text(self.meminfo_path))
        return total or 0

    def held_mb(self):
        """What the Host's and its database's own ceilings take of the budget
        (None: one of them may take all of it)."""
        now = self.clock()
        if self._held_at is None or now - self._held_at >= HELD_READ_EVERY_S:
            self._held_mb = self._held()
            self._held_at = now
        return self._held_mb

    def budget(self):
        """What containers are admitted into: the machine's budget less the
        Host's and its database's ceilings, never under 0."""
        held = self.held_mb()
        if held is None:
            return 0
        return max(0, budget_mb(self.total_mb()) - held)

    def used_mb(self, units, in_flight=()):
        return sum(int(u["memory_mi"]) for u in units) + sum(int(m) for m in in_flight)

    def headroom(self, units, in_flight=()):
        return self.budget() - self.used_mb(units, in_flight)

    def pressure(self):
        return parse_psi_full(read_text(self.psi_path))

    def refusal(self, needs_mi, units, in_flight=()):
        """None when a container of needs_mi fits now, else the 402 text."""
        head = self.headroom(units, in_flight)
        if needs_mi > head:
            return over_plan_text(needs_mi, head)
        p = self.pressure()
        if p is not None and p >= PRESSURE_LIMIT:
            return over_plan_text(needs_mi, 0, reason="pressure")
        return None

    def watch_disk(self, path):
        if path not in self.disk_paths:
            self.disk_paths.append(path)

    def free(self):
        """The fewest bytes free on any disk the runtime writes to (None when
        none can be read)."""
        got = [f for f in (disk_free(p) for p in [self.disk_path, *self.disk_paths]) if f is not None]
        return min(got) if got else None

    def disk_short(self):
        """The bytes free when they are under the wake floor, else None."""
        free = self.free()
        if free is not None and free < DISK_WAKE_FLOOR:
            return free
        return None

    @staticmethod
    def disk_text(free, data=()):
        """The refusal a wake gets on a short disk: the floor, what is free,
        and what each app's data takes, so the owner can see where the space
        went and what deleting would give back."""
        text = (f"this machine has {free >> 20} MiB of disk free for containers, under the "
                f"{DISK_WAKE_FLOOR >> 30} GiB it keeps free before it starts another, so the "
                f"Host's database always has room")
        if data:
            text += (f". App data on this machine: {data_words(data)}. An app's data is "
                     "deleted only when you choose it, in the App Store")
        return text

    def disk_refusal(self, data=()):
        free = self.disk_short()
        return None if free is None else self.disk_text(free, data)

    @staticmethod
    def stop_order(units):
        """Who goes first: coding engines, the browser, apps, resident apps;
        newest first within each."""
        def klass(u):
            if u["kind"] == "harness":
                return 1 if u["ident"] == C.BROWSER else 0
            return 3 if u.get("resident") else 2
        return sorted(units, key=lambda u: (klass(u), -float(u.get("started") or 0)))

    @staticmethod
    def disk_stop_order(units):
        """Who goes first when the disk is short, of the units that write to
        it: coding engines (newest first), the browser, then apps with a data
        volume, the one whose data grew most recently first (then the most
        data, then an app the owner opens before one that runs all the time,
        then the newest). An app without a data volume (a helper: read-only
        root, its /tmp in memory) writes nothing there and is left running."""
        def key(u):
            started = -float(u.get("started") or 0)
            if u["kind"] == "harness":
                return (1 if u["ident"] == C.BROWSER else 0, 0, 0.0, 0, 0, started)
            grew = u.get("data_grew_at")
            return (2, 0 if grew is not None else 1, -float(grew or 0),
                    -int(u.get("data_bytes") or 0), 1 if u.get("resident") else 0, started)
        return sorted([u for u in units if u["kind"] == "harness" or u.get("has_data")], key=key)

    def round(self, now, units, avail_mb=None, psi=None, disk=None):
        """(unit to stop, why) or (None, None). The inputs are read by the
        caller (and given in a test)."""
        over = psi is not None and psi >= PRESSURE_LIMIT
        if not over:
            self.over_since = None
        elif self.over_since is None:
            self.over_since = now
        live = [u for u in units if u.get("running")]
        if not live:
            return None, None
        why = None
        if avail_mb is not None and avail_mb < EVICT_FLOOR_MB:
            why = (f"this machine had {avail_mb} MiB of memory free, under the "
                   f"{EVICT_FLOOR_MB} MiB it keeps")
        elif over and now - self.over_since >= PRESSURE_FOR_S and (
                self.last_stop is None or now - self.last_stop >= PRESSURE_SETTLE_S):
            why = (f"this machine stalled on memory {psi:.0f}% of the time for "
                   f"{PRESSURE_FOR_S:.0f} s (the limit is {PRESSURE_LIMIT:.0f}%)")
        elif disk is not None and disk < DISK_STOP_FLOOR:
            writers = self.disk_stop_order(live)
            if writers:
                self.last_stop = now
                return writers[0], (f"this machine had {disk >> 20} MiB of disk free, under the "
                                    f"{DISK_STOP_FLOOR >> 20} MiB it keeps for the Host's database")
            return None, None
        if why is None:
            return None, None
        self.last_stop = now
        return self.stop_order(live)[0], why
