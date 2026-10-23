#!/usr/bin/env python3
"""Gate: an artifact's history keeps every file and every revision, and a
restore never deletes what a revision did not record.

WHY THIS EXISTS. A revision left out every file over 1 MiB
(_HISTORY_FILE_MAX), and revert empties the workspace before writing the
revision back, so Restore DELETED every image, font or bundled script over
1 MiB from the artifact, silently. Only the newest 20 revisions were kept
(_HISTORY_KEEP), so one design session used up the undo history. The tune
edit was refused past 2,048 characters, the artifact list stopped at 200 with
no offset, and the history had no paging. Revision ids were exactly four
digits, so revision 10000 could never be rendered or restored.

What this pins (all container-free: temp folders and a stub pool):
  1. a revision holds a file over 1 MiB, byte for byte;
  2. an unchanged large file is stored ONCE (the next revision hard-links it);
  3. restoring a revision brings the large file back, and restoring a
     revision recorded before this fix (no "complete" mark, the big file
     left out) KEEPS the big file and names it;
  4. 25 writes keep 25 revisions; the history pages with total/next_offset;
  5. revisions go only when the disk cannot take the next one, and then
     only the shortest run of oldest revisions whose OWN bytes (not shared
     by hard link with a kept revision) cover the shortfall (the revision
     that pruned them says so); a volume full of other data never wipes the
     history (25 revisions at 96% full once lost 23); a
     change the disk cannot hold is not recorded and the answer says so;
     never the one the next links to, never a log name outside .history;
  6. revision 10000 is a revision;
  7. a tune edit of 3,000 characters on a 10 KB page applies; one longer
     than the page is refused with the page's length named;
  8. the artifact list takes an offset and reports the true total.

Measured red against the tree before the fix (APP_ROOT=<pristine export>).
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

REPO = Path(os.environ.get("APP_ROOT") or Path(__file__).resolve().parents[1])
sys.path.insert(0, str(REPO / "brain"))
sys.path.insert(0, str(REPO))

FAILS: list = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILS.append(name)


def run(coro):
    return asyncio.run(coro)


WORK = Path(tempfile.mkdtemp(prefix="caps-g2-history-")).resolve()
# The suite's own temp dir, removed when the interpreter leaves — pass, fail or
# raise (every manual run used to leave it behind).
import atexit as _atexit  # noqa: E402
import shutil as _shutil  # noqa: E402
_atexit.register(_shutil.rmtree, WORK, True)

from server.routes import applications as A          # noqa: E402
from server.applications import artifacts as AR      # noqa: E402

PROJECTS = WORK / "projects"
PROJECTS.mkdir()
A.PROJECTS_ROOT = str(PROJECTS)


class _Pool:
    def __init__(self, total: int = 0):
        self.total = total
        self.sql: list = []

    async def fetchrow(self, *a):
        return {"id": int(a[1]) if len(a) > 1 else 0, "kind": "artifact"}

    async def fetchval(self, *a):
        self.sql.append(a)
        return self.total

    async def fetch(self, *a):
        self.sql.append(a)
        offset = a[-1] if a and isinstance(a[-1], int) else 0
        n = max(0, min(a[-2] if len(a) > 2 and isinstance(a[-2], int) else 0, self.total - offset))
        return [{"id": 9000 + i, "name": f"a{i}", "description": "", "status": "draft",
                 "disposable": True, "created_at": None, "updated_at": None}
                for i in range(n)]

    async def execute(self, *a):
        return "UPDATE 1"


def snap(ws: Path):
    return AR._snapshot(ws, actor="test", op="write")


BIG = os.urandom(1_048_576 * 2 + 17)          # 2 MiB + a bit: over the old cap

# ── 1-3. every file, stored once, restored ────────────────────────────────
print("1. a revision holds every file, whatever its size")
W = PROJECTS / "501"
W.mkdir()
(W / "index.html").write_text("<h1>one</h1>")
(W / "assets").mkdir()
(W / "assets" / "hero.bin").write_bytes(BIG)
r1 = snap(W)
h1 = W / ".history" / str(r1) / "assets" / "hero.bin"
check("1: the revision holds the file over 1 MiB", h1.is_file(), str(r1))
check("1: byte for byte", h1.is_file() and h1.read_bytes() == BIG)

print("2. an unchanged large file is stored once")
(W / "index.html").write_text("<h1>two</h1>")
r2 = snap(W)
h2 = W / ".history" / str(r2) / "assets" / "hero.bin"
check("2: the next revision holds it too", h2.is_file(), str(r2))
check("2: as the same stored copy (a hard link, not a second copy)",
      h1.is_file() and h2.is_file() and os.stat(h1).st_ino == os.stat(h2).st_ino)
log2 = AR._history_log(W)
check("2: the log line marks the revision complete",
      bool(log2) and log2[-1].get("complete") is True, json.dumps(log2[-1:]))

print("3. restore brings the large file back; an old revision never deletes it")
(W / "assets" / "hero.bin").unlink()
(W / "index.html").write_text("<h1>three</h1>")
snap(W)
try:
    run(AR.revert_artifact(_Pool(), 9003, 501, str(r1)))
    ok = True
except Exception as e:  # noqa: BLE001
    ok = repr(e)
check("3: revert ran", ok is True, str(ok))
hero = W / "assets" / "hero.bin"
check("3: the file over 1 MiB is back after Restore", hero.is_file() and hero.read_bytes() == BIG)
check("3: the page is the revision's", (W / "index.html").read_text() == "<h1>one</h1>")
check("3: the stored copy was not touched by the restore (no link into the workspace)",
      h1.is_file() and hero.is_file() and h1.read_bytes() == BIG
      and os.stat(hero).st_ino != os.stat(h1).st_ino)

# A revision recorded before this fix: no "complete" mark, the big file not in it.
L = PROJECTS / "502"
(L / ".history" / "0001").mkdir(parents=True)
(L / ".history" / "0001" / "index.html").write_text("old page")
(L / ".history" / "log.jsonl").write_text(json.dumps(
    {"rev": "0001", "digest": "legacy", "files": 1}) + "\n")
(L / "index.html").write_text("new page")
(L / "font.woff2").write_bytes(BIG)
(L / "small.css").write_text("body{}")
try:
    res = run(AR.revert_artifact(_Pool(), 9003, 502, "0001"))
except Exception as e:  # noqa: BLE001
    res = {"error": repr(e)}
check("3: restoring a pre-fix revision KEEPS the big file it never recorded",
      (L / "font.woff2").is_file() and (L / "font.woff2").read_bytes() == BIG, str(res)[:300])
check("3: ... and says which files it kept",
      res.get("kept") == ["font.woff2"] and "font.woff2" in str(res.get("note")), str(res)[:300])
check("3: ... while a small file the revision lacks is still removed (exact restore)",
      not (L / "small.css").exists() and (L / "index.html").read_text() == "old page")

# ── 4. every revision kept, history pages ────────────────────────────────
print("4. revisions are kept, not pruned by count; history pages")
K = PROJECTS / "503"
K.mkdir()
for i in range(25):
    (K / "index.html").write_text(f"<p>{i}</p>")
    snap(K)
revs = [e["rev"] for e in AR._history_log(K)]
check("4: 25 writes keep 25 revisions", len(revs) == 25, str(len(revs)))
check("4: the first revision is still restorable",
      (K / ".history" / "0001" / "index.html").is_file())
try:
    page = run(AR.list_history(_Pool(), 9003, 503, limit=10, offset=20))
except TypeError as e:
    page = {"error": repr(e)}
check("4: history pages with the true total",
      page.get("total") == 25 and len(page.get("history") or []) == 5
      and page.get("next_offset") is None, str(page)[:300])
try:
    first = run(AR.list_history(_Pool(), 9003, 503, limit=10))
except TypeError as e:
    first = {"error": repr(e)}
check("4: the first page says where the next starts",
      first.get("next_offset") == 10 and (first.get("history") or [{}])[0].get("rev") == "0025",
      str(first)[:300])

# ── 5. pruned only for disk, only what frees room, and says so ───────────
print("5. revisions go only when the disk cannot take the next one")
D = PROJECTS / "504"
(D / "victim").mkdir(parents=True)
(D / "victim" / "keep.txt").write_text("keep")
(D / ".history").mkdir()
for n in range(1, 6):
    (D / ".history" / f"{n:04d}").mkdir()
    # 96 KiB each, held by that revision alone (st_nlink == 1)
    (D / ".history" / f"{n:04d}" / "blob.bin").write_bytes(os.urandom(98_304))
(D / ".history" / "log.jsonl").write_text("".join(
    json.dumps({"rev": r, "digest": f"d{r}"}) + "\n"
    for r in ["../victim", "0001", "0002", "0003", "0004", "0005"]))
(D / "index.html").write_text("fresh")
real_room = getattr(AR, "_disk_room", None)


def _room(free, total):
    def f(_ws):
        return free, total
    return f


# A 10,000,000-byte volume keeps 500,000 free; 350,000 are free, so the next
# revision is ~150,000 short. One old revision frees ~96 KiB, two free enough.
if real_room is not None:
    AR._disk_room = _room(350_000, 10_000_000)
try:
    r6 = snap(D)
finally:
    if real_room is not None:
        AR._disk_room = real_room
log6 = AR._history_log(D)
check("5: the snapshot was taken", r6 == "0006", str(r6))
check("5: the oldest revisions went, and only as many as the shortfall needed",
      not (D / ".history" / "0001").exists() and not (D / ".history" / "0002").exists()
      and (D / ".history" / "0003").exists(), str(sorted(os.listdir(D / ".history"))))
check("5: never the revision the next one links to",
      (D / ".history" / "0005").exists())
check("5: a log name outside .history removes nothing",
      (D / "victim" / "keep.txt").exists())
check("5: the new revision says which revisions the disk took",
      bool(log6) and log6[-1].get("pruned_for_space") == ["0001", "0002"], json.dumps(log6[-1:]))
check("5: the log no longer lists a revision that is gone",
      not any(e.get("rev") in ("0001", "0002") for e in log6))

# The repro: the volume is 96% full of OTHER data, so
# removing this artifact's tiny revisions frees nothing — none may go.
print("5b. a full volume of other data never wipes the history")
P = PROJECTS / "601"
P.mkdir()
for i in range(25):
    (P / "index.html").write_text(f"<p>{i}</p>")
    snap(P)
check("5b: 25 revisions before", len(AR._history_log(P)) == 25)
if real_room is not None:
    AR._disk_room = _room(40_000_000, 1_000_000_000)
try:
    (P / "index.html").write_text("<p>one more edit</p>")
    r_p = snap(P)
finally:
    if real_room is not None:
        AR._disk_room = real_room
logp = AR._history_log(P)
check("5b: no revision was deleted (they would free ~nothing)",
      len(logp) == 26 and logp[0].get("rev") == "0001" and not logp[-1].get("pruned_for_space"),
      f"{len(logp)} left, pruned={logp[-1].get('pruned_for_space') if logp else None}")
check("5b: the edit is still recorded, and the revision says the volume is past its reserve",
      r_p == "0026" and "past the" in str(logp[-1].get("disk_note", "")), json.dumps(logp[-1:])[:300])
check("5b: the op that caused it can say so (the history note)",
      "past the" in getattr(AR, "_take_history_note", lambda _w: "")(P))

# Revisions that share one big file (hard links) free nothing by going:
# the newest revision still holds it.
print("5c. bytes shared with a kept revision are not counted as freed")
S = PROJECTS / "602"
S.mkdir()
(S / "big.bin").write_bytes(BIG)
for i in range(4):
    (S / "index.html").write_text(f"v{i}")
    snap(S)
if real_room is not None:
    # ~1 MiB short: only the 2 MiB file shared by every revision could cover it
    # 100,000,000-byte volume keeps 5,000,000 free; 1 MiB short of that.
    AR._disk_room = _room(5_000_000 - 1_048_576, 100_000_000)
try:
    (S / "index.html").write_text("v-next")
    r_s = snap(S)
finally:
    if real_room is not None:
        AR._disk_room = real_room
logs = AR._history_log(S)
check("5c: no revision went — the big file they share is still held by the newest",
      len(logs) == 5 and not logs[-1].get("pruned_for_space"), json.dumps(logs[-1:])[:300])

# The disk cannot physically hold the change and removing every older
# revision would not make room: the change is NOT recorded, and that is said.
print("5d. a change the disk cannot hold is not recorded, and the answer says so")
F = PROJECTS / "603"
F.mkdir()
(F / "index.html").write_text("a")
snap(F)
(F / "index.html").write_text("b" * 50_000)
if real_room is not None:
    AR._disk_room = _room(10_000, 1_000_000)
try:
    r_f = snap(F)
finally:
    if real_room is not None:
        AR._disk_room = real_room
note_f = AR._take_history_note(F) if hasattr(AR, "_take_history_note") else ""
check("5d: no revision is written that the disk cannot hold",
      r_f is None and len(AR._history_log(F)) == 1, str(r_f))
check("5d: and it is said plainly", "NOT recorded" in note_f, note_f)

# ── 6. revision ids past four digits ─────────────────────────────────────
print("6. revision 10000 is a revision")
check("6: 10000 is revision-shaped", bool(AR._REV_RE.match("10000")))
check("6: 9999 still is", bool(AR._REV_RE.match("9999")))
check("6: a path is not", not AR._REV_RE.match("../0001"))

# ── 7. tune edits bounded by the page ────────────────────────────────────
print("7. a tune edit is bounded by the page it edits")
T = PROJECTS / "505"
T.mkdir()
para = "lorem ipsum " * 50
(T / "index.html").write_text("<html><body><p>" + para + "</p>" + ("<i>pad</i>" * 900) + "</body></html>")
AR._BRIDGE_RATE.clear()
long_after = "dolor sit amet " * 200        # 3,000 chars
try:
    res7 = run(AR.apply_bridge_edit(_Pool(), 9003, 505, entry="index.html",
                                    before=para, after=long_after))
except AR.ArtifactError as e:
    res7 = {"error": f"{e.status} {e.detail}"}
check("7: a 3,000-character edit on a 10 KB page applies",
      res7.get("applied") is True and long_after in (T / "index.html").read_text(), str(res7))
page_len = len((T / "index.html").read_text())
try:
    run(AR.apply_bridge_edit(_Pool(), 9003, 505, entry="index.html",
                             before="dolor sit amet dolor sit amet dolor sit amet",
                             after="x" * (page_len + 10)))
    refused = ""
except AR.ArtifactError as e:
    refused = f"{e.status} {e.detail}"
check("7: an edit longer than the page itself is refused, naming the page's length",
      refused.startswith("400") and str(page_len) in refused, refused)

# ── 8. the artifact list pages ───────────────────────────────────────────
print("8. the artifact list takes an offset and reports the true total")
pool = _Pool(total=450)
try:
    lst = run(AR.list_artifacts(pool, 9003, limit=200, offset=400))
except TypeError as e:
    lst = {"error": repr(e)}
check("8: the list reports the true total", lst.get("total") == 450, str(lst)[:200])
check("8: the offset reaches the query",
      any("OFFSET" in str(a[0]) and 400 in a for a in pool.sql), str(pool.sql)[:300])
check("8: the last page holds the rest and says there is no next",
      len(lst.get("artifacts") or []) == 50 and "next_offset" in lst and lst["next_offset"] is None,
      str(lst)[:200])
try:
    mid = run(AR.list_artifacts(_Pool(total=450), 9003, limit=200, offset=0))
except TypeError as e:
    mid = {"error": repr(e)}
check("8: a full page points at the next", mid.get("next_offset") == 200, str(mid)[:200])
src = (REPO / "server" / "routes" / "artifacts.py").read_text()
check("8: the list route takes an offset",
      "offset: int = Query(0, ge=0)" in src.split("async def list_artifacts", 1)[-1][:200])
app_js = (REPO / "system-apps" / "artifacts" / "app.js").read_text()
check("8: the Artifacts window loads every page",
      "next_offset" in app_js and "before.slice(0,2048)" not in app_js)

print()
if FAILS:
    print(f"FAIL: {len(FAILS)} check(s): {FAILS}")
    sys.exit(1)
print("PASS: artifact history keeps every file and revision; lists page")
