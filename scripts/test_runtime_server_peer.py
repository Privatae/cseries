#!/usr/bin/env python3
"""The local runtime's socket (vm/runtime/lib/cseries_runtime/server.py):
who may talk to it, and the line protocol.

A real unix socket served by the real Runtime. The peer is this process,
so the checks are run with this process as the allowed peer and as a
stranger:

  * the right uid in the right cgroup is answered;
  * the right uid in another cgroup is refused 403 (on WSL the owner's own
    user has the Host's uid, so the uid alone cannot be the check);
  * another uid is refused 403, whatever its cgroup;
  * a refused peer gets nothing but the refusal: its request is not read;
  * one JSON line in, one JSON line out, in the bridge's envelope; a line
    that is not JSON, or over 1 MiB, answers 400;
  * the readiness probe (root, on the machine) never follows an app's
    redirect: the 3xx is the answer.

Container-free: python3 scripts/test_runtime_server_peer.py
"""
import asyncio
import json
import os
import sys
import subprocess
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.environ.get("APP_ROOT") or os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, os.path.join(ROOT, "vm/runtime/lib"))
from cseries_runtime import common as C  # noqa: E402
from cseries_runtime import guard as G  # noqa: E402
from cseries_runtime import server as S  # noqa: E402

failed = 0


def check(cond, what, detail=""):
    global failed
    if cond:
        print(f"OK    {what}")
    else:
        failed = 1
        print(f"FAIL  {what} {detail}")


def own_cgroup():
    with open("/proc/self/cgroup") as f:
        for line in f:
            if line.startswith("0::"):
                return line[3:].strip()
    return ""


async def ask(path, payload):
    """Send payload and read the one answer line. The answer is read while
    the request is still going out: a refused peer is answered at once and
    its request never read."""
    r, w = await asyncio.open_unix_connection(path, limit=4 << 20)
    reading = asyncio.ensure_future(asyncio.wait_for(r.readline(), timeout=10))
    try:
        w.write(payload)
        await w.drain()
    except (ConnectionError, OSError):
        pass
    try:
        line = await reading
    except (ConnectionError, OSError):
        line = b""
    finally:
        w.close()
    return json.loads(line) if line else None


async def scenario(uid, cgroups, tmp):
    paths = C.Paths(tmp)
    rt = S.Runtime(paths, guard=G.Guard(total_mb=8192, held=lambda: 0, psi_path=os.devnull), peer_uid=uid, peer_cgroups=cgroups,
                   chown=lambda *a: None, machine="x86_64")
    sock_path = os.path.join(tmp, "runtime.sock")
    sock = S.listening_socket(sock_path)
    server = await rt.serve(sock)
    try:
        out = {}
        out["status"] = await ask(sock_path, b'{"op": "runtime.status"}\n')
        out["garbage"] = await ask(sock_path, b"not json\n")
        out["long"] = await ask(sock_path, b'{"op": "runtime.status", "x": "' + b"a" * (C.MAX_LINE + 10) + b'"}\n')
        out["unknown"] = await ask(sock_path, b'{"op": "shell", "params": {}}\n')
        return out
    finally:
        server.close()
        await server.wait_closed()


me = os.getuid()
cg = own_cgroup()
check(bool(cg), "this process has a cgroup v2 path to test with", cg)

with tempfile.TemporaryDirectory() as tmp:
    out = asyncio.run(scenario(me, (cg.rstrip("/") + "/",), tmp))
    st = out["status"]
    check(st and st["ok"] is True and "budget_mi" in st["data"] and "units" in st["data"],
          "the right uid in the right cgroup is answered, in the bridge's envelope", st)
    check(out["garbage"] == {"ok": False, "code": 400, "error": "request: not one JSON object"},
          "a line that is not JSON answers 400", out["garbage"])
    check(out["long"] and out["long"]["code"] == 400 and "longer than" in out["long"]["error"],
          "a line over 1 MiB answers 400", out["long"])
    check(out["unknown"] and out["unknown"]["code"] == 400 and "unknown operation" in out["unknown"]["error"],
          "an unknown op answers 400", out["unknown"])

with tempfile.TemporaryDirectory() as tmp:
    out = asyncio.run(scenario(me, ("/system.slice/cseries-host.service/",), tmp))
    check(all(v == {"ok": False, "code": 403, "error": S.REFUSED} for v in out.values()),
          "the right uid in another cgroup is refused 403 and nothing it sends is read", out)

with tempfile.TemporaryDirectory() as tmp:
    out = asyncio.run(scenario(me + 4242, (cg.rstrip("/") + "/", "/"), tmp))
    check(all(v == {"ok": False, "code": 403, "error": S.REFUSED} for v in out.values()),
          "another uid is refused 403, whatever cgroup it is in", out)

check(S.PEER_UID == 1000 and S.PEER_CGROUPS == ("/system.slice/cseries-host.service/",),
      "on a machine the peer is uid 1000 inside cseries-host.service's cgroup")

# Socket activation: the socket systemd hands over is fd 3, and once taken
# nothing the runtime starts may be told it was handed fds.
child = r"""
import os, socket, sys
sys.path.insert(0, sys.argv[1])
from cseries_runtime import server as S
s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
s.bind(sys.argv[2]); s.listen(1)
os.dup2(s.fileno(), 3)
os.environ.update(LISTEN_PID=str(os.getpid()), LISTEN_FDS="1", LISTEN_FDNAMES="runtime.sock")
got = S.listening_socket()
left = [k for k in ("LISTEN_PID", "LISTEN_FDS", "LISTEN_FDNAMES") if k in os.environ]
print(got.fileno(), got.getsockname() == sys.argv[2], ",".join(left) or "-")
"""
with tempfile.TemporaryDirectory() as tmp:
    r = subprocess.run([sys.executable, "-I", "-c", child, os.path.join(ROOT, "vm/runtime/lib"),
                        os.path.join(tmp, "a.sock")], capture_output=True, text=True, timeout=30)
    check(r.stdout.split() == ["3", "True", "-"],
          "the runtime takes systemd's socket from fd 3 and clears the activation variables",
          r.stdout + r.stderr[-300:])

# ── the readiness probe never follows a redirect ───────────────────────────
#
# The probe runs as root on the machine against an app's own code. Two
# servers on loopback: the "app" answers 302 to the "machine service", which
# records whether it was asked. The 3xx counts as an answer (ready), and the
# second server is never reached.
import http.server  # noqa: E402
import threading  # noqa: E402

hits = []


class _Machine(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        hits.append(self.path)
        self.send_response(200)
        self.end_headers()

    def log_message(self, *a):
        pass


machine = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Machine)
target = f"http://127.0.0.1:{machine.server_address[1]}/admin?do=reset"


class _App(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        code = 302 if self.path == "/redirect" else (200 if self.path == "/health" else 500)
        self.send_response(code)
        if code == 302:
            self.send_header("Location", target)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *a):
        pass


app = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _App)
for srv in (machine, app):
    threading.Thread(target=srv.serve_forever, daemon=True).start()
base = f"http://127.0.0.1:{app.server_address[1]}"
try:
    r302 = asyncio.run(S.probe_http(base + "/redirect"))
    r200 = asyncio.run(S.probe_http(base + "/health"))
    r500 = asyncio.run(S.probe_http(base + "/broken"))
finally:
    machine.shutdown()
    app.shutdown()
check(not hits, "a readiness probe does not follow an app's redirect to another address", hits)
check(r302 is True and r200 is True and r500 is False,
      "a 2xx or 3xx answer is ready, a 5xx is not", (r302, r200, r500))

print()
print("FAIL" if failed else "all runtime socket checks passed")
sys.exit(failed)
