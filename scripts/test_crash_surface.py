#!/usr/bin/env python3
"""Crash-artifact surface gate.

The rule these pin: a core dump is the USER's data, the
platform can never look at it, so the artifact surfaces to the HOST and the
platform never collects, backs up or ships it. Every test below is a way that
rule could be broken silently.

The incident behind it: two 5.7 GB dumps of the bundled claude binary sat in
one Host's apps PVC in `host-sdk/scratch` for days, were shipped offsite
under the platform key, and filled the mgmt root disk mid-backup.
The fix that had already shipped for the FIRST occurrence (`ulimit -c 0`)
prevented dumps by destroying them, which is the opposite of the
rule — so these tests pin BOUNDED, not OFF.

Pure and hermetic: everything runs against a tmpdir via CSERIES_CRASH_ROOT. No
pod, no network, no cluster.

Run: python3 scripts/test_crash_surface.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

FAILS: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"[crash] {'PASS' if ok else 'FAIL'} {name}" + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILS.append(name)


def _fresh(tmp: Path):
    """Reload server.crash pointed at a clean tmpdir."""
    os.environ["CSERIES_CRASH_ROOT"] = str(tmp)
    for mod in [m for m in list(sys.modules) if m.startswith("server.crash")]:
        del sys.modules[mod]
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    import importlib
    import server.crash as crash
    importlib.reload(crash)
    return crash


# A minimal but REAL ELF64 little-endian ET_CORE header. The module gates on
# this, so fixtures that are just zero bytes would (correctly) be ignored and
# every relocate test would pass for the wrong reason.
# NOTE the explicit `+` before the padding. Adjacent bytes literals concatenate
# BEFORE `*` binds, so `b"\x7fELF" b"\x02" b"\x00" * 9` repeats the whole prefix
# nine times and produces a 74-byte non-header. The module then (correctly)
# refused it and three relocate tests failed for a reason that had nothing to do
# with the module.
_ELF_CORE_HEADER = (
    b"\x7fELF"                      # magic
    + b"\x02"                       # EI_CLASS  = ELFCLASS64
    + b"\x01"                       # EI_DATA   = ELFDATA2LSB
    + b"\x01"                       # EI_VERSION
    + b"\x00" * 9                   # EI_OSABI, EI_ABIVERSION, EI_PAD
    + (4).to_bytes(2, "little")     # e_type = ET_CORE
)
assert len(_ELF_CORE_HEADER) == 18, len(_ELF_CORE_HEADER)


def _dump(path: Path, mb: int = 2) -> None:
    """A file that really is an ELF core, so the ELF gate accepts it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(_ELF_CORE_HEADER)
        fh.write(b"\0" * ((mb << 20) - len(_ELF_CORE_HEADER)))


def main() -> None:
    # ── 1. the real incident shape: a dump in the SDK scratch cwd is found ──
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        crash = _fresh(tmp)
        _dump(tmp / "host-sdk" / "scratch" / "core.837", 2)
        moved = crash.relocate(component="claude-code")
        check("relocates a dump out of the SDK scratch cwd", len(moved) == 1, str(moved))
        listed = crash.list_dumps()
        check("the relocated dump is listed once", len(listed) == 1, str(listed))
        if listed:
            e = listed[0]
            check("listed name stays core.<digits> so the SHIPPED backup "
                  "name-exclude core.[0-9]* keeps matching",
                  e["name"].startswith("core.") and e["name"].split(".")[1].isdigit(), e["name"])
            check("path is platform-shaped and relative", e["path"] == f"crash/{e['name']}", e["path"])
            check("component attribution survives", e["component"] == "claude-code", str(e))
            check("size is reported", e["bytes"] == 2 << 20, str(e["bytes"]))
        check("the original path no longer holds the dump",
              not (tmp / "host-sdk" / "scratch" / "core.837").exists())

        # ── 2. metadata only: no key may carry file content ──
        if listed:
            keys = set(listed[0])
            check("list returns metadata keys only",
                  keys == {"name", "path", "bytes", "mtime", "component", "found_in"}, str(keys))
        check("there is no content-read function on the module",
              not any(hasattr(crash, n) for n in
                      ("read_dump", "cat_dump", "hash_dump", "read", "cat")),
              "a read/cat/hash of a dump would put user memory on the wire")

    # ── 2b. a big NON-ELF file named core.N is a USER FILE, not a dump ──
    # The name + size rules alone would have moved a build artifact called
    # core.1 into the crash dir and let retention delete it later — the platform
    # quietly eating a user's file, which is worse than the problem this module
    # solves. The ELF ET_CORE check is what stops that.
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        crash = _fresh(tmp)
        artifact = tmp / "projects" / "mygame" / "core.1"
        artifact.parent.mkdir(parents=True, exist_ok=True)
        artifact.write_bytes(b"MZ\x90\x00" + b"\x11" * (5 << 20))   # 5 MB, not ELF
        elf_but_not_core = tmp / "projects" / "mygame" / "core.2"
        elf_but_not_core.write_bytes(
            b"\x7fELF\x02\x01\x01" + b"\x00" * 9
            + (2).to_bytes(2, "little")            # e_type = ET_EXEC, not ET_CORE
            + b"\x00" * (2 << 20))
        moved = crash.relocate()
        check("a 5 MB non-ELF file named core.1 is NOT relocated", moved == [], str(moved))
        check("...and is still where the user left it", artifact.exists())
        check("an ELF that is not ET_CORE is NOT relocated", elf_but_not_core.exists())
        check("neither is listed", crash.list_dumps() == [])

    # ── 3. a real file merely NAMED core is not a dump ──
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        crash = _fresh(tmp)
        # the exact false positive that shipped once: jsonschema's vocabulary file
        vocab = tmp / "node_modules" / "jsonschema_specifications" / "vocabularies" / "core"
        vocab.parent.mkdir(parents=True, exist_ok=True)
        vocab.write_bytes(b"x" * 1500)
        small = tmp / "core.12"
        small.write_bytes(b"x" * 1024)
        crash.relocate()
        check("a 1.5 KB file named 'core' is left alone", vocab.exists())
        check("bare 'core' is not even a candidate (core_uses_pid=1 → kernel "
              "writes core.<pid>)", crash._DUMP_RE.match("core") is None)
        check("a sub-1MiB core.N is left alone (below the dump floor)", small.exists())
        check("neither was listed as a dump", crash.list_dumps() == [])

    # ── 4. retention is newest-wins and bounded ──
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        crash = _fresh(tmp)
        crash.RETAIN_MAX_BYTES = 6 << 20   # 3 dumps of 2 MiB fit; the 4th must not
        crash.CRASH_DIR.mkdir(parents=True, exist_ok=True)
        import time
        for i in range(4):
            p = crash.CRASH_DIR / f"core.{100 + i}.{1000 + i}"
            _dump(p, 2)
            os.utime(p, (1000 + i, 1000 + i))
        dropped = crash.enforce_retention()
        names = {e["name"] for e in crash.list_dumps()}
        check("retention drops the oldest past the budget", len(dropped) == 1, str(dropped))
        check("retention keeps the NEWEST", "core.103.1003" in names, str(names))
        check("retention dropped the oldest", "core.100.1000" not in names, str(names))
        check("total is inside the budget", crash.total_bytes() <= crash.RETAIN_MAX_BYTES)

    # ── 5. delete is name-only and cannot traverse ──
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        crash = _fresh(tmp)
        crash.CRASH_DIR.mkdir(parents=True, exist_ok=True)
        _dump(crash.CRASH_DIR / "core.55.999", 2)
        secret = tmp / "user-files" / "taxes.xlsx"
        secret.parent.mkdir(parents=True, exist_ok=True)
        secret.write_bytes(b"private")
        for bad in ("../user-files/taxes.xlsx", "/etc/passwd", "crash/core.55.999",
                    "taxes.xlsx", "core.55.999/../../user-files/taxes.xlsx"):
            check(f"delete refuses {bad!r}", crash.delete(bad) is False)
        check("the user file is untouched", secret.exists())
        check("delete works on a real dump name", crash.delete("core.55.999") is True)
        check("and it is gone", crash.list_dumps() == [])

    # ── 5b. the BOOT path exists and is size-based ──
    # The review that caught this was right: relocate() had exactly one caller,
    # the supervisor reaping a supervised sidecar. The crash class actually
    # measured here (the bundled CLI in host-sdk/scratch) is not one, and the
    # old shell sweep had been removed on the grounds that the server handled
    # it — so nothing collected it, nothing listed it, nothing bounded it.
    import asyncio
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        crash = _fresh(tmp)
        _dump(tmp / "host-sdk" / "scratch" / "core.4242", 2)
        moved = asyncio.run(crash.sweep_at_boot())
        check("boot sweep relocates a dump nobody supervised", len(moved) == 1, str(moved))
        check("and it is listed afterwards", len(crash.list_dumps()) == 1)
        # Fail-soft: sweep_at_boot must swallow anything relocate() throws,
        # because it runs in the lifespan and a raise there would stop the pod
        # from starting over a housekeeping error. Induce the failure rather
        # than pointing at a bogus root — a bogus root is still a VALID root:
        # the first version of this test pointed at /nonexistent-xyz, and
        # _writable_roots() also scans /tmp, so it happily created the directory
        # and moved a real dump into it. It proved nothing about fail-soft and
        # left a 2 MB file at the filesystem root.
        _real_relocate = crash.relocate
        def _boom(**_kw):
            raise OSError("induced: disk on fire")
        crash.relocate = _boom
        try:
            check("boot sweep is fail-soft when relocate raises",
                  asyncio.run(crash.sweep_at_boot()) == [])
        finally:
            crash.relocate = _real_relocate

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        crash = _fresh(tmp)
        crash.RETAIN_MAX_BYTES = 6 << 20
        crash.CRASH_DIR.mkdir(parents=True, exist_ok=True)
        for i in range(4):
            q = crash.CRASH_DIR / f"core.{200 + i}.{2000 + i}"
            _dump(q, 2)
            os.utime(q, (2000 + i, 2000 + i))
        asyncio.run(crash.sweep_at_boot())
        names = {e["name"] for e in crash.list_dumps()}
        check("boot enforces the budget even with nothing to move", len(names) == 3, str(names))
        # The floor this replaced was count-based ("keep the two newest") and
        # would have destroyed a third dump the user was told would be kept.
        check("retention is SIZE-based, not a count of two", "core.201.2001" in names, str(names))

    # ── 5c. the cheap probe the SDK turn hook relies on ──
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        crash = _fresh(tmp)
        scratch = tmp / "host-sdk" / "scratch"
        scratch.mkdir(parents=True)
        check("dump_present_in is False on an empty scratch dir",
              crash.dump_present_in(scratch) is False)
        check("dump_present_in is False on a missing dir",
              crash.dump_present_in(tmp / "nope") is False)
        _dump(scratch / "core.9", 2)
        check("dump_present_in is True once a real dump lands",
              crash.dump_present_in(scratch) is True)
        (scratch / "core.10").write_bytes(b"not elf" + b"\x00" * (2 << 20))
        check("dump_present_in ignores a non-ELF lookalike",
              crash.dump_present_in(tmp / "nope") is False)

    # ── 5d. the sweep is WIRED, and to lanes that actually exist ──
    # (The first cut hooked a dead engine lane; that engine is gone.)
    root = Path(__file__).resolve().parent.parent
    main_src = (root / "server" / "main.py").read_text()
    check("boot sweep is scheduled in the lifespan",
          "sweep_at_boot()" in main_src)
    check("periodic sweep is scheduled too (covers lanes with no direct hook)",
          "sweep_forever()" in main_src)
    check("both sweep tasks are held and cancelled at shutdown",
          "_crash_tasks" in main_src and "_t.cancel()" in main_src)
    # The periodic sweep must do the real bounded WALK, not a top-level probe.
    # A listdir of DATA_ROOT only sees a lane whose cwd is exactly the data
    # root; deep_research accepts any cwd under it and the in-pod claude-code
    # sidecar uses /app/workspace/... — both invisible to a probe, which
    # defeated the point of having a catch-all at all.
    crash_src = (root / "server" / "crash.py").read_text()
    loop = crash_src[crash_src.index("async def sweep_forever"):]
    loop = loop[:loop.index("\nasync def ") if "\nasync def " in loop[10:] else len(loop)]
    check("periodic sweep runs the full walk, not a top-level probe",
          "await sweep()" in loop and "dump_present_in" not in loop.split('"""')[2],
          "sweep_forever still gates on dump_present_in")
    # It must not sit behind the brain-pool guard: crash housekeeping needs no
    # database, and a pod with a dead pool is when dumps are most likely.
    # Match the CODE line, not the word: the comment above the sweep quotes
    # `if brain_pool is not None:` to explain why it moved, and a naive
    # substring search finds that comment first. (It did — this assertion
    # failed on its own explanation.)
    lines = main_src.split("\n")
    guard_ln = next(i for i, l in enumerate(lines)
                    if l.strip() == "if brain_pool is not None:")
    # `in`, not endswith: the schedule moved into a _crash_tasks list, so the
    # line now ends with a comma. endswith broke on that and the suite raised
    # StopIteration instead of failing a check — which is how it reached a push.
    sweep_ln = next(i for i, l in enumerate(lines)
                    if "_crash.sweep_at_boot()" in l and "create_task" in l)
    check("sweeps are scheduled BEFORE/outside the brain_pool guard",
          sweep_ln < guard_ln,
          f"sweep at line {sweep_ln + 1} is inside the pool guard at "
          f"{guard_ln + 1} — a pool-less pod stops bounding dumps")
    # (skill_harness.py — the in-kernel SDK CLI lane — and host_sdk_engine.py
    #  are retired; research runs on the routed model with the
    #  provider's own web search, so there is no in-kernel Node CLI lane left
    #  to hook. The harness pods keep their own crash reporting.)
    smoke_src = (root / "scripts" / "smoke_test.sh").read_text()
    check("the ship gate does not use pgrep (absent from the kernel image)",
          "pgrep" not in smoke_src.split("# NO pgrep")[0] and "pgrep -f \"uvicorn" not in smoke_src)

    # ── 6. the shell bound RESOLVES to 2 GiB, in the shell that will run it ──
    # Asserted by EXECUTION, not by grepping a literal. `ulimit -c` takes blocks
    # and the block size is implementation-defined (measured: dash
    # 512, bash 1024), so a hardcoded 2097152 silently meant 1 GiB under
    # /bin/sh — half the V8 heap ceiling the bound is sized against, i.e. a
    # truncated dump for the exact crash class this exists for. Grepping the
    # literal is what let that through; running it is what catches it.
    import re as _re
    import subprocess
    root = Path(__file__).resolve().parent.parent
    for name in ("entrypoint.sh", "entrypoint-postgres.sh"):
        txt = (root / name).read_text()
        block = _re.search(
            r"CORE_TARGET_BYTES=.*?\nfi\n",
            txt, _re.S)
        check(f"{name} probes the block size instead of hardcoding it", block is not None)
        if not block:
            continue
        check(f"{name} does not set RLIMIT_CORE to 0",
              _re.search(r"^ulimit -c 0( |$)", txt, _re.M) is None)
        import shutil
        for shell in ("dash", "sh", "bash"):
            # FAIL, do not skip. Silently passing because a shell is absent is
            # how a bound nobody verified reads as green.
            if shutil.which(shell) is None:
                check(f"{name}: {shell} present to verify the bound under", False,
                      "shell missing — the bound is UNVERIFIED for it")
                continue
            probe = subprocess.run(
                [shell, "-c", block.group(0) +
                 "\nawk '/Max core file size/ {print $5, $6}' /proc/self/limits"],
                capture_output=True, text=True)
            check(f"{name} runs cleanly under {shell}", probe.returncode == 0,
                  probe.stderr.strip()[:120])
            got = probe.stdout.split()
            check(f"{name} resolves to exactly 2 GiB soft AND hard under {shell}",
                  got[:2] == ["2147483648", "2147483648"], " ".join(got))

    print()
    print(f"[crash] {'PASS' if not FAILS else 'FAIL'} — {len(FAILS)} failing: {FAILS}" if FAILS
          else "[crash] PASS — all crash-surface invariants hold")
    sys.exit(1 if FAILS else 0)


if __name__ == "__main__":
    # A non-assertion error (StopIteration from a matcher that stopped matching,
    # an OSError, an import) must exit NON-ZERO and say so. It used to escape
    # main() as a bare traceback, which reads as noise next to a "shell: all OK"
    # line and let a broken suite through to a push. Measured.
    try:
        main()
    except SystemExit:
        raise
    except BaseException as _e:
        import traceback
        traceback.print_exc()
        print(f"\n[crash] FAIL — the suite itself raised {type(_e).__name__}: {_e}")
        sys.exit(2)
