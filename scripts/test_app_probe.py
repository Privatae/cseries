#!/usr/bin/env python3
"""Gate for app_probe — the host interrogating its own running app.

The point of the platform is that the Host can look after its own app:
monitor it and make changes until the desired result is achieved, without a
person relaying between the platform and the Host.

The measured gap. The host's ENTIRE surface for a running app was four tools —
system.app_status, system.start_app, system.stop_app, system.restart_app —
plus workspace_file, which is jailed to /var/lib/cseries/projects/<id>: the
project SOURCE, not the running app's data. Verified against the full 59-tool
registry inventory and a scoped semantic search; nothing let the host make a
request to its own app.

Two questions about one user's app, neither answerable by the host:

  "why are no trades coming through" — the app's own API said
  {"current":0,"max":3,"total_stake":0.0} and its trades table had 0 rows.
  Answering it needed a call to /api/v1/count.

  "why is the bot UI blank" — the shell was served with <base href=
  "/ui/ui/">, so the bot UI's relative Vite chunk resolved one level too
  deep and came back as the HTML shell with content-type text/html instead of
  application/javascript. A dynamic import of an HTML document throws, so
  nothing mounts. Finding it needed the RESPONSE HEADERS of one URL.

Both were answered with curl from outside the platform, which is the thing
this test exists to stop being necessary.

Run: APP_ROOT=<repo> python3 scripts/test_app_probe.py
"""
import asyncio
import os
import sys
import types
from pathlib import Path
from tools_source import tools_source  # tools.py and the modules split out of it

_ROOT = Path(os.environ.get("APP_ROOT") or Path(__file__).resolve().parents[1])
sys.path.insert(0, str(_ROOT / "brain"))
sys.path.insert(0, str(_ROOT))

_FAILURES: list[str] = []


def check(label: str, cond: bool, extra: str = "") -> None:
    print(f"[app-probe] {'ok  ' if cond else 'FAIL'} {label}"
          f"{('  ' + extra) if extra and not cond else ''}")
    if not cond:
        _FAILURES.append(label)


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


from mastercode.runtime import tools as T  # noqa: E402
from server import brain_service as _bs  # noqa: E402
from server.applications import registry as _reg, storage as _storage  # noqa: E402
from server.applications import supervisor as _sup  # noqa: E402

import atexit  # noqa: E402
import shutil  # noqa: E402
import tempfile  # noqa: E402
_tmp = Path(tempfile.mkdtemp(prefix="app-probe-"))
# Remove it on the way out — EVERY way out. This probe has two exits (sys.exit(1)
# on failure, falling off the end on success) plus whatever raises, and it writes
# a 64 MiB big.bin into the directory further down, so one leak per run adds up
# fast. One development machine collected 287 orphaned app-probe-* directories,
# 15 GiB, from three days of runs. That box's /tmp is TMPFS backed by its 23 GiB
# of RAM, so the cost landed on MEMORY rather than disk — it never presented as a
# disk problem and sat at 17 of 22 GiB used with 580 MiB of RAM free. atexit
# rather than try/finally because the exits are scattered through a flat script.
atexit.register(shutil.rmtree, _tmp, True)   # True = ignore_errors
_storage.APPDATA_ROOT = _tmp / "appdata"

_bs.get_pool = lambda: object()


async def _installed(pool, app_id):
    return types.SimpleNamespace(id=app_id) if app_id == "podapp" else None


_reg.get = _installed

# The app's own data dir, with a real config and a nested file.
_app_dir = Path(_storage.workspace_dir("podapp"))
(_app_dir / _sup.LOG_SUBDIR).mkdir(parents=True, exist_ok=True)
(_app_dir / "config.json").write_text('{"dry_run": true, "stake_amount": 50}')
(_app_dir / _sup.LOG_SUBDIR / _sup.LOG_FILENAME).write_text(
    "\n".join([f"line {i}" for i in range(200)]
              + ["2026-09-04 14:06 - ERROR - boom", "heartbeat RUNNING"]))

# ── 1. the app is the host's own, or it is not addressable ─────────────────
r = run(T._exec_app_probe({"app_id": "someone-elses-app"}))
check("an app this host does not own is refused",
      r["status"] == "error" and "not installed" in r["result"], repr(r))
check("app_id is required", run(T._exec_app_probe({}))["status"] == "error")
check("an unknown op is refused",
      run(T._exec_app_probe({"app_id": "podapp", "op": "nope"}))["status"] == "error")

# ── 2. files: the running app's OWN data, jailed ───────────────────────────
r = run(T._exec_app_probe({"app_id": "podapp", "op": "files"}))
names = {e["name"] for e in r["result"]["entries"]}
check("files lists the app's own data directory",
      r["status"] == "ok" and "config.json" in names, repr(r)[:200])
r = run(T._exec_app_probe({"app_id": "podapp", "op": "files", "path": "config.json"}))
check("...and reads what the app is ACTUALLY running with",
      r["status"] == "ok" and '"stake_amount": 50' in r["result"]["content"], repr(r)[:200])
for esc in ("../../../etc/passwd", "/etc/passwd", "../"):
    r = run(T._exec_app_probe({"app_id": "podapp", "op": "files", "path": esc}))
    check(f"...and refuses to walk out of it ({esc!r})",
          r["status"] == "error", repr(r)[:160])

# ── 3. logs: a window and a filter, not a fixed tail ───────────────────────
r = run(T._exec_app_probe({"app_id": "podapp", "op": "logs", "limit": 5}))
check("logs returns the newest N lines", r["status"] == "ok"
      and len(r["result"]["lines"]) == 5 and r["result"]["lines"][-1] == "heartbeat RUNNING",
      repr(r)[:200])
r = run(T._exec_app_probe({"app_id": "podapp", "op": "logs", "contains": "error"}))
check("...and filters, so the host can find the one line that matters",
      r["status"] == "ok" and r["result"]["matched"] == 1
      and "boom" in r["result"]["lines"][0], repr(r)[:200])

# ── 4. request: the half that answers both questions above ─────────────────
class _Resp:
    def __init__(self, status, ctype, body):
        self.status = status
        self.headers = {"content-type": ctype}
        self.content = types.SimpleNamespace(
            read=lambda n: asyncio.sleep(0, result=body))

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class _Session:
    routes: dict = {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def get(self, url, **kw):
        for suffix, resp in self.routes.items():
            if url.endswith(suffix):
                return resp
        return _Resp(404, "text/plain", b"nope")


_fake_aiohttp = types.SimpleNamespace(
    ClientSession=lambda *a, **k: _Session(),
    ClientTimeout=lambda **k: None)
sys.modules["aiohttp"] = _fake_aiohttp

_sup.get_supervisor = lambda: types.SimpleNamespace(
    get=lambda app_id: types.SimpleNamespace(
        is_alive=lambda: True, base_url="http://10.0.0.9:8850"))

# "why are no trades coming through" — ask the app, don't read tea leaves.
_Session.routes = {
    "/api/v1/count": _Resp(200, "application/json",
                           b'{"current":0,"max":3,"total_stake":0.0}'),
    # THE UI-CHUNK BUG: the chunk the browser resolves comes back as the HTML
    # shell. 200, and completely broken.
    "/ui/ui/assets/pages-CQOXHZWl.js": _Resp(
        200, "text/html", b'<!DOCTYPE html>\n<html><head><base href="/ui/ui/">'),
    "/ui/assets/pages-CQOXHZWl.js": _Resp(
        200, "application/javascript", b"import{D as e}from'./x.js'"),
}

r = run(T._exec_app_probe({"app_id": "podapp", "path": "/api/v1/count"}))
check("request answers 'why no trades' from the app's OWN api",
      r["status"] == "ok" and r["result"]["http_status"] == 200
      and '"current":0' in r["result"]["body"], repr(r)[:220])

bad = run(T._exec_app_probe(
    {"app_id": "podapp", "path": "/ui/ui/assets/pages-CQOXHZWl.js"}))
good = run(T._exec_app_probe(
    {"app_id": "podapp", "path": "/ui/assets/pages-CQOXHZWl.js"}))
check("request surfaces THE blank-page bug: a script served as text/html",
      bad["result"]["http_status"] == 200
      and bad["result"]["content_type"] == "text/html", repr(bad)[:220])
check("...distinguishable from the path that actually works",
      good["result"]["content_type"] == "application/javascript", repr(good)[:220])
check("...so a 200 alone is never the answer — the header is",
      bad["result"]["http_status"] == good["result"]["http_status"])

# The address comes from the app. The host cannot aim this anywhere.
r = run(T._exec_app_probe({"app_id": "podapp", "path": "http://example.test/x"}))
check("an absolute URL is refused — probes reach your app, nothing else",
      r["status"] == "error" and "not a URL" in r["result"], repr(r)[:160])
r = run(T._exec_app_probe({"app_id": "podapp", "path": "/api/v1/count"}))
check("...and the origin is the supervisor's own base_url",
      r["result"]["url"].startswith("http://10.0.0.9:8850/"), repr(r)[:160])

# An app that is down says so, instead of a confusing connection error.
_sup.get_supervisor = lambda: types.SimpleNamespace(get=lambda a: None)
r = run(T._exec_app_probe({"app_id": "podapp", "path": "/"}))
check("a stopped app says it is stopped, and names the way to start it",
      r["status"] == "error" and "not running" in r["result"]
      and "system.start_app" in r["result"], repr(r)[:200])

# ── 4b. Resource shape (review measured both of these) ──────────────────────
# B3: read_bytes() pulled the WHOLE file in before slicing — a 300MB file in
# an app's data dir raised peak RSS by 300MiB, and the pod's memory is the
# user's plan budget. A trading app's sqlite lives exactly there.
# The file must be MUCH larger than the read cap, or this check cannot do its
# job. It was PROBE_BODY_MAX*3 (60KB) against a threshold of 58KB, so a real
# slurp would show ~60KB of growth while ordinary allocator/lazy-import noise
# shows ~128KB — the noise floor sat ABOVE the signal, and the check reported a
# bounded read as a slurp. Measured 2026-09-04: fails 3 runs in 4 on a tree that
# merely imports more modules, passes on a smaller one, with the read bounded
# (tools.py: `_fh.read(_PROBE_BODY_MAX + 1)`) in both. 64MB makes a slurp
# unmistakable and leaves the threshold an order of magnitude clear of noise.
# WRITE IT IN CHUNKS. ru_maxrss is a HIGH-WATER MARK, so if the test itself
# builds the file as one `b"x" * 64MB` buffer, the peak is already 64MB before
# `_before` is sampled and a subsequent 64MB slurp can never exceed it — the
# check reads green no matter what the code does. Verified 2026-09-04 by
# injecting a real `_fh.read()` slurp into tools.py: with a single-buffer write
# the gate stayed GREEN; writing in 1MB chunks it goes RED, which is the only
# version of this check worth having.
_BIG_BYTES = 64 * 1024 * 1024
_CHUNK = 1024 * 1024
_big = _app_dir / "big.bin"
with open(_big, "wb") as _fh:
    for _ in range(_BIG_BYTES // _CHUNK):
        _fh.write(b"x" * _CHUNK)
import resource  # noqa: E402
_before = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
r = run(T._exec_app_probe({"app_id": "podapp", "op": "files", "path": "big.bin"}))
_grew_kb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss - _before
check("a large file is read BOUNDED, not slurped then sliced",
      r["status"] == "ok" and r["result"]["truncated"] is True
      and len(r["result"]["content"]) == T._PROBE_BODY_MAX
      and _grew_kb < 8 * 1024,
      f"rss grew {_grew_kb}KB reading a {_BIG_BYTES // (1024*1024)}MB file "
      f"(a slurp would show ~65536KB)")
check("...and the real size is still reported",
      r["result"]["bytes"] == _BIG_BYTES)

# N1: aiohttp's StreamReader.read(n) returns as soon as the buffer is
# non-empty, so one call yielded 1,009 bytes of a 15,150-byte body and
# reported truncated:false. A partial document labelled complete is worse
# than none, because the host reasons about what it was handed.
class _Chunked:
    """Hands back the body in small pieces, the way a real socket does."""

    def __init__(self, status, ctype, body, chunk=1009):
        self.status = status
        self.headers = {"content-type": ctype}
        self._rest = body
        self._chunk = chunk
        self.content = types.SimpleNamespace(read=self._read)

    async def _read(self, n):
        take = min(n, self._chunk, len(self._rest))
        out, self._rest = self._rest[:take], self._rest[take:]
        return out

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


_full = b"<!DOCTYPE html>" + b"y" * 15_000
_Session.routes = {"/big.html": _Chunked(200, "text/html", _full)}
_sup.get_supervisor = lambda: types.SimpleNamespace(
    get=lambda app_id: types.SimpleNamespace(
        is_alive=lambda: True, base_url="http://10.0.0.9:8850"))
r = run(T._exec_app_probe({"app_id": "podapp", "path": "/big.html"}))
check("a chunked response is read to the END, not to the first chunk",
      r["result"]["bytes"] == len(_full) and r["result"]["truncated"] is False
      and len(r["result"]["body"]) == len(_full),
      f'got {r["result"]["bytes"]}B of {len(_full)}B, truncated={r["result"]["truncated"]}')

# ...and a body genuinely over the cap is still flagged.
_Session.routes = {"/huge": _Chunked(200, "text/html",
                                     b"z" * (T._PROBE_BODY_MAX + 5000))}
r = run(T._exec_app_probe({"app_id": "podapp", "path": "/huge"}))
check("...while a body over the cap IS flagged truncated",
      r["result"]["truncated"] is True
      and len(r["result"]["body"]) == T._PROBE_BODY_MAX, repr(r)[:160])

# ── 5. wired, gated, and offered to the host ───────────────────────────────
caps = (_ROOT / "brain/mastercode/runtime/meta_tool_caps.py").read_text()
reg = (_ROOT / "brain/mastercode/public_tool_registry.py").read_text()
tools_src = tools_source(_ROOT)
# Per-op gating (review, 2026-09-04): the tool's union is 1|16 because a GET's
# side effects are the app author's choice, and files/logs are declared
# read-safe so reading a log still costs only a read.
check("the tool union demands the same bit as start/stop/restart",
      '"required_bits": 1 | 16,' in caps.split('"app_probe":')[1][:120])
check("...while files and logs are declared read-safe at 1",
      '"app_probe":            {"files": 1, "logs": 1},' in caps)
from mastercode.runtime.meta_tool_caps import READ_SAFE_OPS, op_param_for  # noqa: E402
check("...and 'request' is deliberately NOT read-safe",
      "request" not in READ_SAFE_OPS["app_probe"])
check("...on the op param the tool actually uses",
      op_param_for("app_probe") == "op")
check("...dispatched", 'elif name == "app_probe"' in tools_src)
check("...and offered on both chat surfaces",
      '"app_probe": {' in reg
      and '"surfaces": ["circles", "workbench"]' in reg.split('"app_probe": {')[1][:400])
# Assert the ASSEMBLED description the host is handed, not the source text —
# the sentences span string-literal boundaries, so grepping the file tests the
# formatting rather than the contract.
from mastercode.public_tool_registry import PUBLIC_TOOL_SCHEMAS as _SCHEMAS  # noqa: E402
_desc = _SCHEMAS["app_probe"]["function"]["description"]
check("the host is told to read content_type, not just the status code",
      "content_type" in _desc and "A 200 is not proof" in _desc, _desc[:160])
check("...and that this reads its OWN apps only",
      "Your own apps only" in _desc)
check("...and that probing never changes the app",
      "build and ship a new version" in _desc)

print()
if _FAILURES:
    print(f"{len(_FAILURES)} FAILED: {_FAILURES}")
    sys.exit(1)
print("[app-probe] ALL OK")
