#!/usr/bin/env python3
"""Gate: the app-side bounds that stay say when they bind, and the quality
cuts are gone.

WHY THIS EXISTS.
  - The store licence gate hashes at most 2,000 files / 50 MiB of an
    installed app (licensing._MAX_FILES_PER_APP/_MAX_BYTES_PER_APP: a
    bound on kernel hashing work, kept). The files past it were dropped
    with a bare `break` and nothing said so.
  - "Make my desktop look like <site>" read at most 4 linked stylesheets
    (site_style.MAX_STYLESHEETS); a palette in the fifth was missed
    silently, although the byte totals already bound the cost.
  - A store build's attestation listed the newest 500 commits and
    counted ONLY those: older commits were missing from the authorship
    summary, with no truncation mark.

Container-free: temp folders, a local git repo, stubbed fetches.
Measured red against the tree before the fix (APP_ROOT=<pristine export>).
"""
from __future__ import annotations

import asyncio
import os
import subprocess
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


WORK = Path(tempfile.mkdtemp(prefix="caps-g2-records-")).resolve()
# The suite's own temp dir, removed when the interpreter leaves — pass, fail or
# raise (every manual run used to leave it behind).
import atexit as _atexit  # noqa: E402
import shutil as _shutil  # noqa: E402
_atexit.register(_shutil.rmtree, WORK, True)


print("1. the licence index says how many files it did not compare")
from server.applications import licensing as L           # noqa: E402

app = WORK / "app"
app.mkdir()
for i in range(L._MAX_FILES_PER_APP + 500):
    (app / f"f{i:05d}.js").write_text(f"// file {i}\n" + "x" * 80)
left: dict = {}
_stats = {"n": 0}
_real_stat = Path.stat


def _counting_stat(self, *a, **kw):
    _stats["n"] += 1
    return _real_stat(self, *a, **kw)


Path.stat = _counting_stat
try:
    try:
        hashed = L.hash_tree(app, unhashed=left)
    except TypeError as e:
        hashed = L.hash_tree(app)
        left = {"error": repr(e)}
finally:
    Path.stat = _real_stat
check("1: the hashing work stays bounded", len(hashed) == L._MAX_FILES_PER_APP, str(len(hashed)))
check("1: reaching the bound is said, naming the first file not compared",
      left.get("bound_reached") is True
      and left.get("from") == f"f{L._MAX_FILES_PER_APP:05d}.js", str(left))
check("1: the walk stops at the bound — the 500 files past it are not stat'ed",
      _stats["n"] <= L._MAX_FILES_PER_APP + 1, str(_stats))
src = (REPO / "server" / "applications" / "licensing.py").read_text()
seg = src.split("async def foreign_app_index", 1)[-1].split("\ndef ", 1)[0]
check("1: the index row carries the count and a log line names the app",
      '"unhashed"' in seg and "log.warning" in seg)


print("2. every linked stylesheet is read until the CSS budget; the rest are named")
from mastercode.runtime import tools as T                 # noqa: E402
from mastercode.runtime import url_reader as UR            # noqa: E402
from server.applications import artifacts as AR            # noqa: E402
from server.applications import site_style as SS           # noqa: E402

fetched: list = []


def _page(n_links: int) -> str:
    return ("<html><head><title>Shop</title>"
            + "".join(f'<link rel="stylesheet" href="/s{i}.css">' for i in range(n_links))
            + "</head><body><h1>Hi</h1></body></html>")


PAGES: dict = {}


async def _fetch_raw(url, *a, **k):
    fetched.append(url)
    if url in PAGES:
        return {"ok": True, "body": PAGES[url], "final_url": url}
    if url.endswith(".css"):
        i = int(url.rsplit("/s", 1)[-1].split(".")[0])
        css = ("a, button, .btn-primary { background: #d4145a; color: #ffffff }"
               if i == 5 else f".x{i} {{ margin: {i}px }}")
        return {"ok": True, "body": css, "final_url": url}
    return {"ok": False, "error": "not found"}


async def _create(pool, eid, *, name, files, description=""):
    return {"artifact_id": 901, "name": name}

UR.fetch_raw = _fetch_raw
AR.create_artifact = _create

PAGES["https://shop.example/"] = _page(10)
res = asyncio.run(T._desktop_from_url(None, 9003, "https://shop.example/", ""))
check("2: all 10 linked stylesheets are read", res.get("stylesheets_read") == 10, str(res.get("stylesheets_read")))
check("2: the accent in the sixth sheet is taken",
      str((res.get("taken_from_the_site") or {}).get("accent") or "").lower() == "#d4145a",
      str(res.get("taken_from_the_site")))
fetched.clear()
n = (SS.MAX_STYLESHEETS or 4) + 8
PAGES["https://many.example/"] = _page(n)
res = asyncio.run(T._desktop_from_url(None, 9003, "https://many.example/", ""))
check("2: a page linking endless sheets stops at the fetch backstop",
      sum(1 for u in fetched if u.endswith(".css")) == SS.MAX_STYLESHEETS,
      str(sum(1 for u in fetched if u.endswith(".css"))))
check("2: and names the sheets it did not read",
      len(res.get("stylesheets_not_read") or []) == 8 and "not read" in str(res.get("stylesheets_note")),
      str({k: res.get(k) for k in ("stylesheets_not_read", "stylesheets_note")})[:300])
check("2: the fetch backstop sits well above a real page's sheets", SS.MAX_STYLESHEETS >= 32,
      str(SS.MAX_STYLESHEETS))
big_ext = [".card { margin: 1px }" + ("/*" + "p" * 300_000 + "*/") for _ in range(3)]
own = "<style>body { background: #fafafa; color: #111111 }</style>"
st = SS.extract("<html><head>" + own + "</head><body></body></html>", big_ext)
check("2: the page's own <style> survives when linked sheets fill the CSS budget",
      str(st.get("background") or "").lower() == "#fafafa", str(st.get("background")))


print("3. the attestation counts every commit and marks a listed-only part")
from server.applications import attestation as AT          # noqa: E402

g = WORK / "proj"
g.mkdir()
env = {**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null",
       "GIT_AUTHOR_NAME": "Pat", "GIT_AUTHOR_EMAIL": "pat@example.com",
       "GIT_COMMITTER_NAME": "Pat", "GIT_COMMITTER_EMAIL": "pat@example.com"}
subprocess.run(["git", "init", "-q", "-b", "main", str(g)], check=True, env=env)
# 520 commits through fast-import: one process, no per-commit fork.
lines = []
for i in range(1, 521):
    msg = f"edit {i}".encode()
    lines.append(b"commit refs/heads/main\n")
    lines.append(f"mark :{i}\n".encode())
    lines.append(f"author Pat <pat@example.com> {1_700_000_000 + i} +0000\n".encode())
    lines.append(f"committer Pat <pat@example.com> {1_700_000_000 + i} +0000\n".encode())
    lines.append(f"data {len(msg)}\n".encode() + msg + b"\n")
    if i > 1:
        lines.append(f"from :{i - 1}\n".encode())
    body = f"v{i}\n".encode()
    lines.append(f"M 644 inline index.html\ndata {len(body)}\n".encode() + body + b"\n")
subprocess.run(["git", "-C", str(g), "fast-import", "--quiet"], input=b"".join(lines),
               check=True, env=env)
subprocess.run(["git", "-C", str(g), "checkout", "-q", "main"], check=True, env=env)
_git_calls: list = []
_real_git = AT._git


def _recording_git(project_dir, *args):
    _git_calls.append(args)
    return _real_git(project_dir, *args)


AT._git = _recording_git
try:
    facts = AT.collect_git(g, host_email="host@example.com", host_name="host")
finally:
    AT._git = _real_git
check("3: the total comes from git rev-list --count, not a listed log",
      any(a[:2] == ("rev-list", "--count") for a in _git_calls), str(_git_calls))
check("3: every log read into memory is bounded by --max-count",
      all(any(str(x).startswith("--max-count=") for x in a) for a in _git_calls
          if a and a[0] == "log"), str(_git_calls))
summ = facts.get("authorship_summary") or {}
check("3: the listed commits stay bounded", len(facts.get("authorship") or []) == AT._AUTHORSHIP_MAX,
      str(len(facts.get("authorship") or [])))
check("3: every commit is counted in the summary", summ.get("user_commits") == 520, str(summ))
check("3: the record says how many commits there are, and that the list is cut",
      facts.get("authorship_total") == 520 and facts.get("authorship_truncated") is True,
      str({k: facts.get(k) for k in ("authorship_total", "authorship_truncated")}))
rec = AT.build({"id": "x", "name": "x", "version": "1.0.0"}, {"index.html": b"<h1>x</h1>"},
               git=facts, sdk=None)
check("3: both reach the built record",
      rec.get("authorship_total") == 520 and rec.get("authorship_truncated") is True,
      str({k: rec.get(k) for k in ("authorship_total", "authorship_truncated")}))

print()
if FAILS:
    print(f"FAIL: {len(FAILS)} check(s): {FAILS}")
    sys.exit(1)
print("PASS: kept bounds say when they bind; the quality cuts are gone")
