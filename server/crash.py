"""Crash artifacts: bounded, Host-visible, never platform-collected.

THE RULE. A core dump is a full process memory image, so it
is USER data the platform can never look at — which makes it useless to us
rather than merely sensitive. Therefore:

  1. Crash FILES surface to the HOST, in this pod, for the user's own
     troubleshooting. This module is that surface.
  2. If a PLATFORM problem surfaces from one, the HOST opens a support channel
     to the platform (bridge op `platform.report_crash`), carrying METADATA
     ONLY. Direction is host -> platform, host-initiated.
  3. The platform never collects, backs up or ships the artifact. Not offsite,
     not under a platform key, not "just in case".

WHAT THIS MODULE DOES NOT DO: it never opens a dump. Every function here works
from the directory entry (name, size, mtime). `read`/`cat`/`hash` of a core
file does not exist and must not be added — the Host reads the file in-pod if
the user asks it to, and that read never crosses the bridge.

WHY RELOCATION IS NEEDED AT ALL. `kernel.core_pattern` on the pool nodes is the
bare relative name `core` with `core_uses_pid=1` (measured), so the kernel
writes `core.<pid>` into the CRASHING PROCESS'S current working directory, wherever that happens to be. That is how two 5.7 GB dumps
came to sit in `host-sdk/scratch` on one Host for six days and be found only
when a backup ran out of disk mid-copy. core_pattern cannot be fixed per-pod: it is one
kernel-wide value (not namespaced), and a pipe handler is no help either
because core(5) is explicit that the handler "runs in the initial namespaces
(PID, mount, user, and so on) and not in the namespaces of the crashing
process". So the dump lands where the kernel puts it and we move it, once, into
one directory the Host knows about and the backup excludes by path.

SIZING, derived rather than picked:
  * Per dump: RLIMIT_CORE 2 GiB, set hard=soft in entrypoint.sh so nothing in
    the tree can raise it. Above the bundled CLI's own V8 heap ceiling
    (the since-deleted host_sdk_engine.py set NODE_HEAP_OPTS = --max-old-space-size=1536), so the one
    crash class we have actually measured produces a COMPLETE dump instead of a
    truncated one gdb cannot open; below the retained budget, so a single dump
    can never exhaust it alone.
  * Retained: 4 GiB across this directory, newest-wins. That is
    systemd-coredump's documented `MaxUse=` ceiling ("10% of the total disk
    size, capped at 4GiB", coredump.conf(5)). With three entities per pool node
    today that is <= 12 GiB of a 290 GB volume, which lands on the same 10%
    rule again at node level.

NAMING IS LOAD-BEARING. Relocated dumps keep a `core.<digits>...` name
(`core.<pid>.<epoch>`) on purpose: the SHIPPED backup exclusion is the glob
`core.[0-9]*` (the platform's entity backup script), so a dump stays excluded by
NAME even before the by-PATH exclusion for this directory lands, and in either
order. Do not rename these to `core.<app>.<pid>` — that breaks the name match
and the artifact ships offsite under the platform key, which is the exact
defect this ticket exists to close. Component attribution lives in INDEX_NAME
instead, which is platform-shaped metadata and carries no user content.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from pathlib import Path

log = logging.getLogger("privatae.pc.crash")

# The apps PVC mount inside the entity container (verified live with
# `df -h /var/lib/cseries` in two Hosts; it is also what the platform's
# entity backup tars). Overridable so the unit tests can
# point the whole module at a tmpdir instead of monkeypatching module globals.
DATA_ROOT = Path(os.environ.get("CSERIES_CRASH_ROOT", "/var/lib/cseries"))
CRASH_DIRNAME = "crash"
CRASH_DIR = DATA_ROOT / CRASH_DIRNAME
INDEX_NAME = "index.json"

# 4 GiB retained, newest-wins — systemd-coredump's MaxUse cap. See module docstring.
RETAIN_MAX_BYTES = 4 << 30

# Below this a file named like a dump is not one. The floor exists because a
# bare name match already false-positived on jsonschema's
# `.../vocabularies/core` (1.5 KB) in the backup excludes; a real dump is
# megabytes to gigabytes.
MIN_DUMP_BYTES = 1 << 20

# What the kernel actually writes: `core.<pid>`. core_uses_pid=1 on every pool
# node (measured on four of them), so the kernel
# NEVER writes a bare `core` here — matching that name bought nothing and was
# pure false-positive surface against real files (jsonschema ships
# `vocabularies/core`; @babel/core is a package). Our own relocated names add
# `.<epoch>`, so the expression has to keep matching those too or retention
# would ignore everything it had already moved.
_DUMP_RE = re.compile(r"^core\.\d+(\.\d+)*$")

# ELF header: 0x7f E L F, then class/data/version, and e_type at offset 16 as a
# 2-byte little- or big-endian value. ET_CORE is 4.
_ELF_MAGIC = b"\x7fELF"
_ET_CORE = 4


def _is_core_file(path: Path) -> bool:
    """True only for a real ELF core dump.

    THE NAME IS NOT ENOUGH, and getting this wrong destroys user data. A 5 MB
    build artifact named `core.1` under a project tree matches the name rule,
    the size floor, and would then be MOVED into the crash dir and later deleted
    by retention — the platform quietly eating a user's file, which is worse
    than the problem this module exists to solve. So the name selects candidates
    and the ELF header decides.

    This reads 18 bytes of HEADER to classify the file. That is not reading the
    dump: it never looks past the ELF type field, never returns bytes to any
    caller, and exists precisely so that a file which is NOT a dump is left
    alone. Nothing else in this module opens a candidate at all.
    """
    try:
        with open(path, "rb") as fh:
            head = fh.read(18)
    except OSError:
        return False
    if len(head) < 18 or head[:4] != _ELF_MAGIC:
        return False
    little = head[5] == 1                       # EI_DATA: 1 = LSB, 2 = MSB
    e_type = int.from_bytes(head[16:18], "little" if little else "big")
    return e_type == _ET_CORE

# Directories never worth walking for a dump: large, and a process whose cwd is
# inside one is not a thing that happens. Keeps the scan bounded on a PVC that
# may hold big project trees.
_SKIP_DIRS = frozenset({
    ".git", "node_modules", ".venv", "venv", "__pycache__", ".cache",
    "python-libs", ".vscode-server", "pgdata", CRASH_DIRNAME,
})

# How deep to walk each writable root. A crashing process's cwd is a working
# directory, not a leaf of a deep tree; 3 covers every observed case
# (host-sdk/scratch is depth 2) without turning boot into a full-volume walk.
_MAX_DEPTH = 3


def _writable_roots() -> list[Path]:
    """Every mount this container can write, read from /proc/self/mounts.

    Derived, not hardcoded, on purpose: the entity container runs
    readOnlyRootFilesystem=True and its writable set is a list of mounts in the
    controller's pod spec (emptyDirs /tmp, /run/postgresql, /etc/privatae/certs,
    /home/privatae, plus the apps PVC at DATA_ROOT and its subPaths). That list
    is not ours to keep in sync — a hardcoded copy here would go stale the first
    time a mount is added and we would silently stop finding dumps in the new
    one.
    """
    # DATA_ROOT is ALWAYS scanned, whether or not it happens to be its own mount
    # point. Deriving the whole list from /proc/self/mounts alone looked right
    # and silently found nothing when the data root was a plain directory rather
    # than a mount — caught by scripts/test_crash_surface.py, which is exactly
    # the case a hermetic test creates and a production pod hides.
    roots: list[Path] = [DATA_ROOT]
    try:
        with open("/proc/self/mounts", encoding="utf-8") as fh:
            for line in fh:
                parts = line.split()
                if len(parts) < 4:
                    continue
                target, opts = parts[1], parts[3].split(",")
                if "rw" not in opts:
                    continue
                # Kernel/virtual filesystems: a dump cannot land in them.
                if target.startswith(("/proc", "/sys", "/dev")):
                    continue
                cand = Path(target)
                # Never walk an ANCESTOR of the data root — "/" is rw in plenty
                # of contexts and walking it would turn a crash sweep into a
                # whole-filesystem crawl.
                if _is_within(DATA_ROOT, cand):
                    continue
                if not cand.is_dir():
                    continue
                roots.append(cand)
    except OSError as e:
        log.warning("crash: cannot read /proc/self/mounts (%s) — data root only", e)
    # Order matters, and not for tidiness. DATA_ROOT holds the crash dir, so a
    # dump found under DATA_ROOT relocates with a same-filesystem rename() —
    # atomic and free. A dump found first under an /app/* subPath of the SAME
    # PVC is the identical file reached through a bind mount, but os.replace
    # across the mount boundary raises EXDEV and falls back to copying up to
    # 2 GiB. Sorting purely by path depth put /app/workspace (3 parts) ahead of
    # /var/lib/cseries (4), so the expensive path won every time. DATA_ROOT first,
    # then shallowest.
    roots.sort(key=lambda p: (p != DATA_ROOT, len(p.parts)))
    keep: list[Path] = []
    for r in roots:
        if not any(_is_within(r, k) for k in keep):
            keep.append(r)
    return keep


def _is_within(child: Path, parent: Path) -> bool:
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False


def _looks_like_dump(path: Path) -> bool:
    if not _DUMP_RE.match(path.name):
        return False
    # Never follow a symlink into somewhere else and never "relocate" a device
    # node: only a real file that is big enough to be a memory image.
    if path.is_symlink() or not path.is_file():
        return False
    try:
        if path.lstat().st_size < MIN_DUMP_BYTES:
            return False
    except OSError:
        return False
    return _is_core_file(path)


def _walk_for_dumps(root: Path) -> list[Path]:
    found: list[Path] = []
    root_depth = len(root.parts)
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        here = Path(dirpath)
        if len(here.parts) - root_depth >= _MAX_DEPTH:
            dirnames[:] = []
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
        for fn in filenames:
            if not _DUMP_RE.match(fn):
                continue
            p = here / fn
            if _looks_like_dump(p):
                found.append(p)
    return found


def _load_index() -> dict:
    try:
        with open(CRASH_DIR / INDEX_NAME, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_index(index: dict) -> None:
    try:
        CRASH_DIR.mkdir(parents=True, exist_ok=True)
        tmp = CRASH_DIR / (INDEX_NAME + ".tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(index, fh, sort_keys=True)
        os.replace(tmp, CRASH_DIR / INDEX_NAME)
    except OSError as e:
        log.warning("crash: cannot write index (%s)", e)


def relocate(*, component: str | None = None) -> list[dict]:
    """Move every stray dump into CRASH_DIR. Returns metadata for the moved set.

    `component` attributes this sweep to an app id when the caller knows one
    (the supervisor reaping a named sidecar). It is recorded in the index, never
    in the filename — see the module docstring on why the name must stay
    `core.<digits>`.

    A rename() within the PVC is atomic and free. Across filesystems (a dump in
    the /tmp emptyDir) os.replace raises EXDEV, so we fall back to a copy and
    only unlink once the copy is complete — a crash mid-move must never lose the
    user's artifact.
    """
    CRASH_DIR.mkdir(parents=True, exist_ok=True)
    index = _load_index()
    moved: list[dict] = []
    now = int(time.time())

    for root in _writable_roots():
        for src in _walk_for_dumps(root):
            if _is_within(src, CRASH_DIR):
                continue
            try:
                size = src.lstat().st_size
            except OSError:
                continue
            bits = src.name.split(".")
            pid = bits[1] if len(bits) > 1 else "0"
            dest = CRASH_DIR / f"core.{pid}.{now}"
            n = 1
            while dest.exists():
                dest = CRASH_DIR / f"core.{pid}.{now}.{n}"
                n += 1
            if not _move(src, dest):
                continue
            meta = {
                "name": dest.name,
                "bytes": int(size),
                "mtime": now,
                # Where it was found: the crashing process's working directory.
                # Platform-shaped (a directory the platform itself laid out),
                # never file content.
                "found_in": str(src.parent),
                "component": component,
            }
            index[dest.name] = meta
            moved.append(meta)
            log.warning(
                "crash: relocated a %d-byte dump from %s to %s — it belongs to the "
                "Host; the platform never reads or ships it",
                size, src.parent, dest,
            )

    if moved:
        _save_index(index)
        enforce_retention()
    return moved


def _move(src: Path, dest: Path) -> bool:
    """rename() when we can, copy-then-unlink across a filesystem boundary."""
    try:
        os.replace(src, dest)
        return True
    except OSError as e:
        if getattr(e, "errno", None) != 18:  # EXDEV: cross-device link
            log.warning("crash: cannot relocate %s (%s)", src, e)
            return False
    try:
        with open(src, "rb") as fin, open(dest, "wb") as fout:
            while chunk := fin.read(1 << 20):
                fout.write(chunk)
        os.unlink(src)
        return True
    except OSError as e:
        log.warning("crash: cross-device relocate of %s failed (%s)", src, e)
        try:
            dest.unlink()
        except OSError:
            pass
        return False


def enforce_retention() -> list[str]:
    """Trim CRASH_DIR to RETAIN_MAX_BYTES, newest-wins. Returns deleted names."""
    entries = sorted(_scan_dir(), key=lambda e: e["mtime"], reverse=True)
    total = 0
    deleted: list[str] = []
    index = _load_index()
    for e in entries:
        total += e["bytes"]
        if total <= RETAIN_MAX_BYTES:
            continue
        try:
            (CRASH_DIR / e["name"]).unlink()
        except OSError as err:
            log.warning("crash: cannot delete %s (%s)", e["name"], err)
            total -= e["bytes"]
            continue
        deleted.append(e["name"])
        index.pop(e["name"], None)
        log.info(
            "crash: retention dropped %s (%d bytes) — over the %d-byte "
            "newest-wins budget", e["name"], e["bytes"], RETAIN_MAX_BYTES,
        )
    if deleted:
        _save_index(index)
    return deleted


def _scan_dir() -> list[dict]:
    """Directory entries in CRASH_DIR that are dumps. Never opens a file."""
    out: list[dict] = []
    try:
        names = os.listdir(CRASH_DIR)
    except OSError:
        return out
    for name in names:
        if not _DUMP_RE.match(name):
            continue
        p = CRASH_DIR / name
        if p.is_symlink() or not p.is_file():
            continue
        try:
            st = p.lstat()
        except OSError:
            continue
        out.append({"name": name, "bytes": int(st.st_size), "mtime": int(st.st_mtime)})
    return out


def list_dumps() -> list[dict]:
    """Metadata for every dump the Host can see. NAME, SIZE, TIME ONLY.

    The `path` returned is relative to the pod's data root, so it is
    platform-shaped and cannot carry a fragment of the user's own content in a
    filename. Contents are never read here, by anything, ever.
    """
    index = _load_index()
    out = []
    for e in sorted(_scan_dir(), key=lambda e: e["mtime"], reverse=True):
        meta = index.get(e["name"]) or {}
        out.append({
            "name": e["name"],
            "path": f"{CRASH_DIRNAME}/{e['name']}",
            "bytes": e["bytes"],
            "mtime": e["mtime"],
            "component": meta.get("component"),
            "found_in": meta.get("found_in"),
        })
    return out


def delete(name: str) -> bool:
    """Delete one dump by name. The user's call, made through their Host."""
    # Name-only: no separators, no traversal, and it must look like a dump. A
    # delete op that accepts a path is a delete-anything op.
    if "/" in name or "\\" in name or not _DUMP_RE.match(name):
        return False
    try:
        (CRASH_DIR / name).unlink()
    except OSError:
        return False
    index = _load_index()
    if index.pop(name, None) is not None:
        _save_index(index)
    log.info("crash: %s deleted at the Host's request", name)
    return True


def total_bytes() -> int:
    return sum(e["bytes"] for e in _scan_dir())


def dump_present_in(directory: str | Path) -> bool:
    """Cheap check: does this one directory hold a dump right now?

    A listdir, not a walk. It exists so a hot path (every SDK turn) can ask
    "did that leave a dump?" for the cost of one syscall instead of scanning
    the volume, and only pay for the real sweep when the answer is yes.
    """
    try:
        names = os.listdir(directory)
    except OSError:
        return False
    d = Path(directory)
    return any(_DUMP_RE.match(n) and _looks_like_dump(d / n) for n in names)


async def sweep(component: str | None = None) -> list[dict]:
    """relocate() + enforce_retention(), off the event loop.

    Every caller is async (lifespan startup, the supervisor reap, the SDK engine
    after a non-zero exit) and relocate() does real filesystem work: an os.walk
    over the writable roots and, on a cross-filesystem move, a copy of up to
    RLIMIT_CORE bytes. Running that inline blocked the event loop for as long as
    it took to copy 2 GiB, which stalls the kernel for every other request in
    the pod. asyncio.to_thread keeps it off the loop.
    """
    import asyncio
    moved = await asyncio.to_thread(relocate, component=component)
    if not moved:
        # relocate() only trims when it moved something; a boot with nothing to
        # move still needs the budget enforced, because the dir may already be
        # over it from before the restart.
        await asyncio.to_thread(enforce_retention)
    return moved


async def sweep_at_boot() -> list[dict]:
    """The boot sweep. Fail-soft: never stop the server from starting.

    THIS IS THE PATH THAT MATTERS MOST, and the first cut of this change did not
    have it. entrypoint.sh used to `rm -f <root>/core.[0-9]*` at boot; that line
    was removed on the grounds that server/crash.py handles it, and then the
    only caller of relocate() was the supervisor reaping a SUPERVISED sidecar.
    The crash class actually measured on this platform — the bundled CLI dying
    in host-sdk/scratch (the since-deleted host_sdk_engine.py), which was not a supervised sidecar
    — was therefore never relocated, never listed, never bounded and never
    surfaced. Post-change that would have been strictly worse than before: the
    old sweep at least deleted them.
    """
    try:
        moved = await sweep()
    except Exception as e:  # noqa: BLE001 — a boot sweep must never block boot
        log.warning("crash: boot sweep failed (%s)", e)
        return []
    if moved:
        log.warning(
            "crash: boot sweep relocated %d dump(s) (%d bytes) into %s — they "
            "belong to the Host and are listed at /api/system/crash",
            len(moved), sum(m["bytes"] for m in moved), CRASH_DIR,
        )
    return moved


async def sweep_forever(interval_seconds: int = 900) -> None:
    """Background loop: a full bounded sweep every interval.

    WHY A LOOP AND NOT JUST MORE HOOKS. A hand-maintained list of spawn sites is
    a list that goes stale, and the failure mode is silent — an unbounded pile
    of 2 GiB files that no lister shows. The first cut of this change proved the
    point by hooking a function with no production caller while the real lane
    went uncovered.

    WHY A FULL WALK AND NOT A CHEAP PROBE. This ran `dump_present_in(DATA_ROOT)`
    first and only walked on a hit. That probe is a TOP-LEVEL listdir, so it saw
    a lane only if its cwd was exactly DATA_ROOT — which quietly defeated the
    whole purpose of having a catch-all:
      * skills/deep_research.py:281 accepts any cwd UNDER the data root, not
        just the root itself;
      * the in-pod claude-code sidecar spawned the CLI with cwd
        /app/workspace/..., which the probe cannot see at all (that route
        is retired).
    A dump from either was reachable only by the boot sweep or by the sidecar
    dying. So the loop now runs relocate() itself.

    SCOPE THIS ACTUALLY COVERS: every writable mount reported by
    /proc/self/mounts (DATA_ROOT first, then the pod's emptyDirs), walked to
    _MAX_DEPTH with _SKIP_DIRS pruned — the same bounded walk the boot sweep and
    the supervisor use. It does NOT cover a dump deeper than _MAX_DEPTH, or one
    inside a skipped directory; those are deliberate limits on cost, not
    oversights.

    COST: measured on the node side by the node monitor's equivalent walk at
    ~0.6 s over a whole PV, on a thread, once per interval. 15 minutes matches
    that monitor's own pass, so at worst the pod and the operator notice together.
    """
    import asyncio
    while True:
        try:
            await asyncio.sleep(interval_seconds)
            moved = await sweep()
            if moved:
                log.warning(
                    "crash: periodic sweep relocated %d dump(s) (%d bytes) — "
                    "a lane with no direct hook produced one",
                    len(moved), sum(m["bytes"] for m in moved),
                )
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 — a housekeeping loop never dies
            log.warning("crash: periodic sweep failed (%s)", e)

