#!/usr/bin/env python3
"""End-to-end check of the harness streaming path: a real HarnessSpec driving a
real subprocess through the real sidecar HTTP server, consumed as NDJSON.

Proves the thing the design hinges on: frames arrive WHILE the process is still
running, not in one lump at the end.
"""
import json
import os
import sys
import threading
import time
import urllib.request

# cseries_sidecar reads BIND_HOST/PORT at IMPORT time, so these must be set before
# the import below, not after.
PORT = 18853
os.environ["PORT"] = str(PORT)
os.environ["BIND_HOST"] = "127.0.0.1"

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.dirname(_HERE)
for _p in (os.path.join(_REPO, "cli"), os.path.join(_REPO, "system-apps")):
    if os.path.isdir(_p) and _p not in sys.path:
        sys.path.insert(0, _p)

import logging
logging.disable(logging.CRITICAL)

from cseries_sidecar import sidecar                      # noqa: E402
from _harness_runtime import harness_core              # noqa: E402
from _harness_runtime.harness_core import HarnessSpec  # noqa: E402
from pathlib import Path                               # noqa: E402

FAILS = []
def check(n, ok, d=""):
    print(("ok   " if ok else "FAIL ") + n + (("  -- " + str(d)) if not ok and d else ""))
    if not ok:
        FAILS.append(n)

# A spec whose "binary" is a python one-liner emitting grok-style
# streaming-json with a delay between frames, so we can prove liveness.
SCRIPT = (
    "import sys,time,json\n"
    "for i in range(3):\n"
    "    print(json.dumps({'type':'assistant','message':{'content':[{'type':'text','text':'chunk%d'%i}]}}), flush=True)\n"
    "    time.sleep(0.6)\n"
    "print('a plain log line codex-style', flush=True)\n"
    "print(json.dumps({'type':'result','result':'done'}), flush=True)\n"
)

def _argv(spec, prompt, project_dir, model, effort=None):
    return [sys.executable, "-c", SCRIPT]

Path("/tmp/itest_creds.txt").write_text("x")

SPEC = HarnessSpec(
    id="grok", display="itest", logger=logging.getLogger("itest"),
    log_format="%(message)s", binary=Path(sys.executable),
    workspace=Path("/tmp"), cache_dir=None, baked=True,
    cache_valid=None, acquire=None, release_tag=None,
    creds_path=Path("/tmp/itest_creds.txt"), creds_shape="plaintext_or_empty",
    auth_error="no creds", build_argv=_argv,
    build_env=lambda spec, creds: {**os.environ},
    run_cwd=False, timeout_error=lambda m: "timeout", status_tail=(),
    run_auth_ok=lambda spec, creds: True,
    default_max_minutes=1,
)

sidecar.handler("POST", "/run/stream")(harness_core.make_run_stream(SPEC))

# serve() installs a SIGTERM handler, which only works in the main thread — so
# the server runs as a CHILD PROCESS and this process is the client.
import subprocess as _sp
if os.environ.get("ITEST_ROLE") == "server":
    sidecar.serve(port=PORT)
    raise SystemExit(0)

_child = _sp.Popen([sys.executable, os.path.abspath(__file__)],
                   env={**os.environ, "ITEST_ROLE": "server"},
                   stdout=_sp.DEVNULL, stderr=_sp.PIPE, text=True)
import socket as _s
_up = False
for _ in range(60):
    time.sleep(0.25)
    if _child.poll() is not None:
        print("server child exited:", (_child.stderr.read() or "")[-600:])
        raise SystemExit(1)
    try:
        _c = _s.create_connection(("127.0.0.1", PORT), timeout=0.25); _c.close(); _up = True; break
    except OSError:
        continue
if not _up:
    _child.kill(); print("server never came up"); raise SystemExit(1)

req = urllib.request.Request(
    "http://127.0.0.1:%d/run/stream" % PORT,
    data=json.dumps({"prompt": "go", "project_dir": "/tmp"}).encode(),
    headers={"Content-Type": "application/json"})

frames, arrival = [], []
t0 = time.time()
with urllib.request.urlopen(req, timeout=60) as r:
    check("content-type is ndjson",
          "ndjson" in (r.headers.get("Content-Type") or ""), r.headers.get("Content-Type"))
    for raw in r:
        line = raw.decode().strip()
        if not line:
            continue
        frames.append(json.loads(line))
        arrival.append(time.time() - t0)

print("\nframes: %d" % len(frames))
for f, a in zip(frames, arrival):
    p = f.get("payload") or {}
    print("  +%.2fs  %-8s %s" % (a, f.get("kind"), str(p.get("text") or p.get("state") or "")[:48]))

check("got frames", len(frames) >= 5, len(frames))
kinds = [f["kind"] for f in frames]
check("text frames classified", kinds.count("text") >= 3, kinds)
check("plain log line survived as text",
      any((f.get("payload") or {}).get("text", "").startswith("a plain log line")
          for f in frames), kinds)
check("terminal status frame present", kinds[-1] == "status", kinds[-1])
term = frames[-1]["payload"]
check("terminal carries /run fields",
      {"ok", "exit_code", "transcript", "elapsed_seconds", "diff"} <= set(term),
      sorted(term))
check("terminal ok=True", term.get("ok") is True, term.get("ok"))
check("transcript accumulated", "chunk0" in term.get("transcript", ""), term.get("transcript", "")[:60])
check("seq is monotonic", [f["seq"] for f in frames] == list(range(len(frames))),
      [f["seq"] for f in frames])

# THE point: frames must arrive spread out, not all at the end.
spread = arrival[-1] - arrival[0]
check("frames arrived LIVE (spread over >1s, not one lump)", spread > 1.0,
      "spread=%.2fs" % spread)
check("first frame arrived before the process finished",
      arrival[0] < arrival[-1] - 1.0,
      "first=%.2fs last=%.2fs" % (arrival[0], arrival[-1]))

print()
if FAILS:
    print("FAILED (%d): %s" % (len(FAILS), FAILS))
    _child.kill()
    sys.exit(1)
_child.kill()
print("PASS: streaming is live end to end — Popen loop -> normalizer -> NDJSON over HTTP")
sys.exit(0)
