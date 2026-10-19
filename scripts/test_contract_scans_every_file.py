#!/usr/bin/env python3
"""Gate: the app-SDK build contract and the /api conformance gate read EVERY
source file, at any size, with bounded memory.

The defect: build_contract._scan_app_sources and api_contract._extract_api_calls
read at most 400 files and skipped any file over 2 MB, SILENTLY — so a
violation in the 401st file, or in a bundled app.js bigger than 2 MB, passed
the gate. Now every file is scanned; a big one is read in 2 MB windows with
64 KB of context either side, and a match counts once, in the window it
starts in.

What this pins (a temp project tree, no server):
  1. a violation in a file past the 400th is found (build and api gates);
  2. a violation past the first 2 MB of a 5 MB file is found;
  3. a violation that straddles a window edge is found exactly once;
  4. a fake /api call inside a block comment that straddles a window edge is
     not reported (no false block from windowing);
  5. a big file is read in bounded windows, never whole.

Run: APP_ROOT=<repo> python3 scripts/test_contract_scans_every_file.py
"""
import os
import sys
import tempfile
from pathlib import Path

_ROOT = Path(os.environ.get("APP_ROOT") or Path(__file__).resolve().parents[1])
sys.path[:0] = [str(_ROOT)]

FAILED: list[str] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"[contract-scan] ok   {label}")
    else:
        FAILED.append(label)
        print(f"[contract-scan] FAIL {label}" + (f" — {detail}" if detail else ""))


class Manifest:
    permissions: list = []


def main() -> int:
    from server.applications import build_contract as BC
    from server.applications import api_contract as AC

    contract = {"routes": [{"method": "GET", "path": "/api/ok", "scope": None}]}
    tmp = Path(tempfile.mkdtemp(prefix="contract-scan-", dir=os.environ.get("TMPDIR") or None))

    # ── 1. a file past the 400th ─────────────────────────────────────────
    many = tmp / "many"
    (many / "src").mkdir(parents=True)
    for i in range(420):
        (many / "src" / f"m{i:04d}.js").write_text(f"export const v{i} = {i};\n")
    (many / "src" / "z_last.js").write_text(
        "localStorage.setItem('k', 1);\nfetch('https://late.example/x');\nfetch('/api/late');\n")
    errs = BC._scan_app_sources(many, [])
    check("build gate: a violation in the 421st file is found",
          any("z_last.js: uses localStorage" in e for e in errs)
          and any("late.example" in e for e in errs), f"{errs[:3]}")
    cerrs = AC.conformance_against(many, Manifest(), contract)
    check("api gate: a call in the 421st file is checked", any("/api/late" in e for e in cerrs),
          f"{cerrs[:3]}")

    # ── 2-4. a 5 MB bundle ───────────────────────────────────────────────
    big = tmp / "big"
    big.mkdir()
    W = 2_000_000
    filler = "var a=1;\n"

    def pad_to(buf: list, n: int) -> None:
        size = sum(len(x) for x in buf)
        need = n - size
        buf.append(filler * (need // len(filler)) + " " * (need % len(filler)))
    parts: list[str] = []
    # a block comment holding a fake call, straddling the first window edge
    pad_to(parts, W - 40)
    parts.append("/* fetch('/api/fake_in_comment') padding padding padding padding */\n")
    # a real violation straddling the second window edge
    pad_to(parts, 2 * W - 10)
    parts.append("window.parent.postMessage('x','*');\n")
    # violations deep in the file, past the first 2 MB
    pad_to(parts, 4 * W + 500_000)
    parts.append("sessionStorage.getItem('k');\nfetch('https://deep.example/x');\nfetch('/api/deep');\n")
    (big / "app.js").write_text("".join(parts))

    import server.applications.tree_walk as TW
    reads: list[int] = []
    real_pread = os.pread

    def spy_pread(fd, n, off):
        reads.append(n)
        return real_pread(fd, n, off)
    TW.os.pread = spy_pread
    try:
        errs = BC._scan_app_sources(big, [])
        cerrs = AC.conformance_against(big, Manifest(), contract)
    finally:
        TW.os.pread = real_pread
    check("build gate: a violation past the first 2 MB of a 5 MB file is found",
          any("app.js: uses localStorage/sessionStorage" in e for e in errs)
          and any("deep.example" in e for e in errs), f"{errs}")
    check("api gate: a call past the first 2 MB is checked", any("/api/deep" in e for e in cerrs),
          f"{cerrs}")
    parent = [e for e in errs if "window.parent" in e]
    check("a violation straddling a window edge is reported exactly once",
          len(parent) == 1, f"{parent}")
    check("a fake call inside a comment that straddles a window edge is not reported",
          not any("fake_in_comment" in e for e in cerrs), f"{cerrs}")
    ctx = getattr(TW, "SCAN_CONTEXT_BYTES", 0)
    check("the big file is read in bounded windows, never whole",
          bool(reads) and max(reads) <= W + 2 * ctx, f"reads={reads[:6]}")

    if FAILED:
        print(f"FAIL: contract scan — {len(FAILED)} check(s): " + "; ".join(FAILED))
        return 1
    print("OK: contract scan — every file is read, at any size, in bounded windows")
    return 0


if __name__ == "__main__":
    sys.exit(main())
