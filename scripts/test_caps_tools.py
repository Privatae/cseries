#!/usr/bin/env python3
"""Gate: Host tool limits that cut legitimate work.

A Host handles continuous 24/7 work on large data, efficiently. Each block
below pins one limit that refused or silently cut
work the user asked for, and that now pages, streams or says so:

  - orient refused more than 6 NAMED groups, though open=['all'] was fine
  - a dispatched agent's brief was STORED cut at 4,000 chars (the record
    the Host and the Agents app read), while the agent got it whole
  - a non-pass agent's report to the Host was cut at 2,000 chars
  - gitea op='read' refused any file over 1 MiB, even one window of it
  - github op='read' refused any file over 1 MiB (GitHub serves 1-100 MiB
    raw); its remedy pointed at gitea, which had the same refusal
  - local_grep cut every matched line at 200 chars silently, and had no
    way past its 50 hits
  - list_projects had no offset: projects past the first 50 unreachable
  - import_repo killed a clone at 180 s of wall clock, however fast it
    was still arriving
  (The acceptance timeout clamp is fixed in the build path — its gate proves it.)
  - a PDF whose read budget ran out came back as its first pages with
    nothing saying the rest was unread; an Office member over its budget
    read "could not be read (ValueError)"
  - rate_limiter.py was dead code that a live comment cited as a budget

No network, no DB, no pod: fake pools, patched transports, temp dirs, one
local subprocess (python3) for the clone liveness.

Run: APP_ROOT=<repo> python3 scripts/test_caps_tools.py
"""
import asyncio
import io
import os
import sys
import tempfile
import zipfile
from pathlib import Path

REPO = os.environ.get("APP_ROOT") or str(Path(__file__).resolve().parent.parent)
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.join(REPO, "brain"))

_fail = 0


def check(label, cond, detail=""):
    global _fail
    if cond:
        print(f"[caps-g2-tools] ok   {label}")
    else:
        _fail += 1
        print(f"[caps-g2-tools] FAIL {label}" + (f" — {detail}" if detail else ""))


def run(coro):
    return asyncio.run(coro)


SCRATCH = Path(tempfile.mkdtemp(prefix="caps-g2-tools-", dir=os.environ.get("TMPDIR") or None))
# The suite's own temp dir, removed when the interpreter leaves — pass, fail or
# raise (every manual run used to leave it behind).
import atexit as _atexit  # noqa: E402
import shutil as _shutil  # noqa: E402
_atexit.register(_shutil.rmtree, SCRATCH, True)

from mastercode.runtime import tools as T  # noqa: E402


# ── orient ──────────────────────────────────────────────────────────
# orient (and its per-call group cap) was deleted with groups mode;
# scripts/test_host_map.py checks it is gone.


# ── agent dispatch ─────────────────────────────────────────
from mastercode.runtime import agent_dispatch as AD  # noqa: E402


class _TM:
    def __init__(self, pool, entity_id):
        pass

    async def create_task(self, **k):
        return 77

    async def update_task_status(self, *a):
        return None


class _Pool:
    def __init__(self):
        self.calls = []

    async def execute(self, sql, *args):
        self.calls.append((sql, args))


AD.TaskManager = _TM
brief = "Read the whole spec and do every step. " * 300          # ~11.7k chars
pool = _Pool()
run(AD.AgentDispatch(pool)._create_agent_task(3, "t", brief))
stored = next((a[0] for s, a in pool.calls if "description" in s), "")
check("the dispatched brief is stored whole (was: cut at 4,000)",
      stored == brief, f"stored {len(stored)} of {len(brief)}")
report = "finding: " + "x" * 6000
txt = AD._blocked_error_text({"error": report}, {}, "fail")
check("a non-pass agent's report reaches the Host whole (was: cut at 2,000)",
      txt == report, f"{len(txt)} of {len(report)}")


# ── repo reads past 1 MiB ──────────────────────────────────
big = b"".join(b"row %d\n" % i for i in range(1, 250_001))       # ~2.1 MiB
check("the fixture is past the 1 MiB inline size", len(big) > 1_048_576)


async def _chunks(data, n=65536):
    for i in range(0, len(data), n):
        yield data[i:i + n]

window = getattr(T, "_read_window", None)
if window is None:
    check("a streamed window reader exists (tools._read_window)", False)
else:
    w = run(window(_chunks(big), 200_000, 200_002))
    check("a window deep in a large file is exactly those lines",
          w.get("content") == "row 200000\nrow 200001\nrow 200002", w.get("content", "")[:80])
    w = run(window(_chunks(big), None, None))
    check("no window → one page of whole lines with next_start_line",
          w.get("first_line") == 1 and w.get("next_start_line") == w.get("last_line", 0) + 1
          and len(w.get("content", "")) <= T._REPO_READ_PAGE_CHARS, {k: v for k, v in w.items() if k != "content"})
    w = run(window(_chunks(b"\xff\xfe\x00binary"), None, None))
    check("binary bytes are still refused", w.get("binary") is True)

# github: contents API says 2 MiB, encoding none → the raw stream is read
from mastercode.runtime import github_lane as GL  # noqa: E402
_real = (T._gh_get, T._github_repo_token, getattr(GL, "api_stream_raw", None))
raw_paths = []


async def _gh_get(path, token, timeout_s=20):
    if path.endswith("/repos/acme/big"):
        return 200, {"default_branch": "main", "private": False}, {}
    return 200, {"type": "file", "size": len(big), "encoding": "none", "content": ""}, {}


async def _no_token(pool, eid):
    return None


async def _raw(path, token, chunk_bytes=65536):
    raw_paths.append(path)
    async for c in _chunks(big):
        yield c

T._gh_get, T._github_repo_token = _gh_get, _no_token
GL.api_stream_raw = _raw
try:
    r = run(T._exec_github_repo({"op": "read", "owner_repo": "acme/big", "path": "data.csv",
                                 "start_line": 100, "end_line": 101}))
    res = r.get("result") if isinstance(r.get("result"), dict) else {}
    check("github reads a window of a 2 MiB file through the raw stream",
          r.get("status") == "ok" and res.get("content") == "row 100\nrow 101" and raw_paths,
          r if r.get("status") != "ok" else res.get("content"))
finally:
    T._gh_get, T._github_repo_token = _real[0], _real[1]
    if _real[2] is not None:
        GL.api_stream_raw = _real[2]


# ── local_grep ──────────────────────────────────────────────────────
root = SCRATCH / "codespace"
(root / "apps").mkdir(parents=True)
long_line = "a" * 3000 + "NEEDLE" + "b" * 2000
(root / "apps" / "bundle.min.js").write_text(long_line + "\n")
(root / "apps" / "many.txt").write_text("".join(f"NEEDLE {i}\n" for i in range(60)))
T._ENTITY_CODESPACE_ROOT = str(root)
r = run(T._exec_local_grep({"pattern": "NEEDLE", "path": "apps/bundle.min.js"}))
m = (r.get("result") or {}).get("matches") or [{}]
check("a long matched line is not cut silently — the excerpt holds the match and says cut",
      "NEEDLE" in m[0].get("content", "") and m[0].get("cut") is True
      and m[0].get("line_chars") == len(long_line), {k: v for k, v in m[0].items() if k != "content"})
r = run(T._exec_local_grep({"pattern": "NEEDLE", "path": "apps/many.txt", "max_results": 50}))
res = r.get("result") or {}
check("hits page — total_hits is the true count and next_offset is set",
      res.get("total_hits") == 60 and res.get("next_offset") == 50, {k: v for k, v in res.items() if k != "matches"})
r = run(T._exec_local_grep({"pattern": "NEEDLE", "path": "apps/many.txt", "max_results": 50, "offset": 50}))
check("…and offset reaches the rest", len((r.get("result") or {}).get("matches") or []) == 10)


# ── list_projects ───────────────────────────────────────────────────
class _ProjPool:
    def __init__(self):
        self.sql, self.params = "", ()

    async def fetch(self, sql, *params):
        self.sql, self.params = sql, params
        return [{"id": 100 + i, "name": f"p{i}", "description": "", "status": "active",
                 "kind": "artifact", "created_at": None, "updated_at": None, "phase_count": 0,
                 "phases_ready": 0, "phases_complete": 0, "phases_open": 0, "last_phase_at": None,
                 "next_phase_status": None, "total_matched": 130} for i in range(50)]

pp = _ProjPool()
r = run(T._exec_list_projects({"limit": 50, "offset": 50}, 7, pp))
res = r.get("result") or {}
check("list_projects passes offset to the query",
      "OFFSET" in pp.sql and pp.params[-1] == 50, (pp.params, pp.sql[-80:]))
check("…reports the true total and next_offset",
      res.get("total") == 130 and res.get("next_offset") == 100, {k: v for k, v in res.items() if k != "projects"})


# ── import_repo clone liveness ──────────────────────────────────────
live = getattr(T, "_run_with_progress_liveness", None)
if live is None:
    check("the clone is judged by progress liveness (tools._run_with_progress_liveness)", False)
else:
    talk = [sys.executable, "-c",
            "import sys,time\nfor i in range(8):\n sys.stderr.write('Receiving %d%%\\r' % i); sys.stderr.flush(); time.sleep(0.25)"]
    rc, tail, stalled = run(live(talk, idle_s=1.0))
    check("a process that keeps reporting runs past the idle window (2 s > 1 s)",
          rc == 0 and not stalled, (rc, tail, stalled))
    mute = [sys.executable, "-c", "import time; time.sleep(5)"]
    rc, tail, stalled = run(live(mute, idle_s=0.5))
    check("a silent one is stopped as stalled", stalled is True, (rc, tail, stalled))
src = (Path(REPO) / "brain/mastercode/runtime/tools.py").read_text()
i = src.find("async def _exec_import_repo")
check("import_repo has no 180 s wall clock",
      i > 0 and "timeout=180" not in src[i:i + 6000] and "_run_with_progress_liveness" in src[i:i + 6000])


# ── acceptance timeout clamp ────────────────────────────────────────
# Fixed in the build path: _normalize_acceptance is there, and a timeout past
# the pod's maximum is refused by name instead of clamped. The build gate
# proves it; this one no longer touches that function.


# ── document readers say when a budget stopped them ─────────────────
from server.readers import docread as DR, pdftext as PT  # noqa: E402


def _pdf(pages):
    objs = []
    kids = " ".join(f"{3 + 2 * i} 0 R" for i in range(pages))
    objs.append("<< /Type /Catalog /Pages 2 0 R >>")
    objs.append(f"<< /Type /Pages /Kids [{kids}] /Count {pages} >>")
    for i in range(pages):
        objs.append(f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
                    f"/Resources << /Font << /F1 {3 + 2 * pages} 0 R >> >> /Contents {4 + 2 * i} 0 R >>")
        stream = f"BT /F1 12 Tf 72 720 Td (Page {i} has some words on it) Tj ET"
        objs.append(f"<< /Length {len(stream)} >>\nstream\n{stream}\nendstream")
    objs.append("<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    out = io.BytesIO()
    out.write(b"%PDF-1.4\n")
    offs = []
    for n, body in enumerate(objs, 1):
        offs.append(out.tell())
        out.write(f"{n} 0 obj\n{body}\nendobj\n".encode())
    xref = out.tell()
    out.write(f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode())
    for o in offs:
        out.write(f"{o:010d} 00000 n \n".encode())
    out.write(f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode())
    return out.getvalue()

doc = _pdf(40)
name, full = DR.read(doc, "application/pdf")
check("the fixture PDF reads (40 pages of text)", "Page 39" in full, full[-120:])
_ops = PT.MAX_OPS
PT.MAX_OPS = 30                     # the budget runs out a few pages in
try:
    name, part = DR.read(doc, "application/pdf")
finally:
    PT.MAX_OPS = _ops
check("a PDF stopped by its read budget SAYS so on its last line",
      "Page 39" not in part and "stopped reading here" in part.strip().splitlines()[-1], part[-200:])

buf = io.BytesIO()
with zipfile.ZipFile(buf, "w") as zf:
    zf.writestr("[Content_Types].xml", "<Types/>")
    zf.writestr("word/document.xml", "<w:document xmlns:w='x'><w:body><w:p><w:r><w:t>"
                + "x" * 4000 + "</w:t></w:r></w:p></w:body></w:document>")
_cap = DR.ZIP_MEMBER_CAP
DR.ZIP_MEMBER_CAP = 1024
try:
    try:
        DR.read(buf.getvalue(), DR.DOCX)
        msg = "(read without refusal)"
    except DR.Unreadable as e:
        msg = str(e)
finally:
    DR.ZIP_MEMBER_CAP = _cap
check("an Office member over its budget is refused in words, naming the limit",
      "too large" in msg and "ValueError" not in msg, msg)


# ── dead rate limiter ───────────────────────────────────────────────
check("brain/mastercode/rate_limiter.py (imported by nothing) is gone",
      not (Path(REPO) / "brain/mastercode/rate_limiter.py").exists())
otr = (Path(REPO) / "brain/mastercode/brain/offline_task_runner.py").read_text()
check("the scheduled-task ceiling no longer cites it as a live budget",
      "DEFAULT_DAILY_LIMIT = 1000" not in otr)


print(f"\n[caps-g2-tools] {'PASS' if not _fail else 'FAIL'} ({_fail} failing)")
sys.exit(1 if _fail else 0)
