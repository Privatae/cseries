#!/usr/bin/env python3
"""Each harness image's Docker HEALTHCHECK works in control-socket mode too.

With CSERIES_SIDECAR_CONTROL_SOCK set (a coding run on a user's machine, `--network
none`), the sidecar serves its control plane on that unix socket and listens on
no TCP port. A HEALTHCHECK that only dials http://127.0.0.1:<port>/health then
fails every 15 s and Docker marks a working container `(unhealthy)`.

For every sidecars/harness-*/Dockerfile this reads the HEALTHCHECK command
exactly as Docker builds it (continuation lines joined, run by /bin/sh -c) and:
  1. socket mode: runs it with real curl against a real sidecar serving only on
     a unix socket -> exit 0;
  2. socket mode, nothing listening -> non-zero (it really checks the socket);
  3. socket mode: the curl it runs is exactly
     `-fsS --unix-socket <path> http://localhost/health`, seen through a curl
     shim;
  4. socket variable unset: the curl it runs is today's, byte for byte
     (`-fsS http://127.0.0.1:<EXPOSE port>/health`), seen through the shim.

There is no proxy flag and no proxy check: curl never sends a --unix-socket
request through a proxy, whatever HTTP_PROXY says. Measured 2026-10-13 with
the images' own curl (7.88.1, bookworm) and dead proxy variables set: the
socket request answered 200, while a TCP request in the same environment went
to the proxy and failed. A `--noproxy '*'` on the socket branch, and a gate
that set HTTP_PROXY to prove it mattered, passed just the same without it.

Needs `curl` on PATH (the images have it; so does the CI host). No container,
no network.

Run: APP_ROOT=<repo> python3 scripts/test_harness_healthcheck_socket.py
"""
import http.client
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

FAILS = []


def check(name: str, ok: bool, detail: str = ""):
    tag = "ok  " if ok else "FAIL"
    print(f"[healthcheck-sock] {tag} {name}" + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILS.append(name)


CHILD = r"""
import os, sys
sys.path.insert(0, os.environ["T_CLI"])
import cseries_sidecar as s

@s.sidecar.handler("GET", "/health")
def health():
    return {"ok": True}

s.sidecar.serve()
"""


def healthcheck(dockerfile: Path):
    """(shell command, EXPOSE port) as Docker sees them, or (None, None)."""
    text = dockerfile.read_text()
    # Docker removes a trailing backslash and its newline, joining the lines.
    joined = re.sub(r"\\\n", "", text)
    cmd = port = None
    for line in joined.splitlines():
        s = line.strip()
        if s.startswith("HEALTHCHECK "):
            m = re.search(r"\sCMD\s+(.*)$", s)
            cmd = m.group(1) if m else None
        elif s.startswith("EXPOSE "):
            port = s.split()[1]
    return cmd, port


class UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, path: str):
        super().__init__("localhost", timeout=5)
        self._path = path

    def connect(self):
        sk = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sk.settimeout(self.timeout)
        sk.connect(self._path)
        self.sock = sk


def _up(path: str) -> bool:
    try:
        c = UnixHTTPConnection(path)
        c.request("GET", "/health")
        ok = c.getresponse().status == 200
        c.close()
        return ok
    except OSError:
        return False


def main() -> int:
    app_root = Path(os.environ.get("APP_ROOT", "/app"))
    dockerfiles = sorted(app_root.glob("sidecars/harness-*/Dockerfile"))
    check("found the harness Dockerfiles", len(dockerfiles) >= 7,
          repr([str(d) for d in dockerfiles]))
    curl = shutil.which("curl")
    check("curl is on PATH (the HEALTHCHECK's own tool)", curl is not None)
    if not dockerfiles or curl is None:
        print(f"[healthcheck-sock] FAIL ({len(FAILS)} failing): {FAILS}")
        return 1

    tmp = Path(tempfile.mkdtemp(prefix="hc-sock-"))
    ctl = str(tmp / "control.sock")
    env = {k: v for k, v in os.environ.items()
           if k not in ("PORT", "CSERIES_SIDECAR_CONTROL_SOCK", "CSERIES_SIDECAR_EGRESS_SOCK",
                        "CSERIES_HARNESS_CAP", "CSERIES_HARNESS_CAP_ENFORCE",
                        "PRIVATAE_SIDECAR_CONTROL_SOCK", "PRIVATAE_SIDECAR_EGRESS_SOCK",
                        "PRIVATAE_HARNESS_CAP", "PRIVATAE_HARNESS_CAP_ENFORCE")}
    child_env = dict(env, T_CLI=str(app_root / "cli"), PRIVATAE_APP_ID="hc-test",
                     CSERIES_SIDECAR_CONTROL_SOCK=ctl, PYTHONUNBUFFERED="1",
                     # The capability wall is on, as in a real run: /health
                     # must still answer the HEALTHCHECK, which sends no cap.
                     CSERIES_HARNESS_CAP="cap-x", CSERIES_HARNESS_CAP_ENFORCE="1")
    p = subprocess.Popen([sys.executable, "-c", CHILD], env=child_env,
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    end = time.time() + 10
    while time.time() < end and not _up(ctl):
        time.sleep(0.05)
    up = _up(ctl)
    check("a socket-mode sidecar is serving /health on the unix socket", up)

    # A curl shim for the TCP case: records its argv, answers 0.
    shim_dir = tmp / "shim"
    shim_dir.mkdir()
    argv_log = tmp / "argv.json"
    (shim_dir / "curl").write_text(
        f"#!{sys.executable}\n"
        "import json, sys\n"
        f"open({str(argv_log)!r}, 'w').write(json.dumps(sys.argv[1:]))\n")
    (shim_dir / "curl").chmod(0o755)

    sock_env = dict(env, CSERIES_SIDECAR_CONTROL_SOCK=ctl)
    shim_path = f"{shim_dir}:{env.get('PATH', '/usr/bin:/bin')}"

    for df in dockerfiles:
        name = df.parent.name
        cmd, port = healthcheck(df)
        check(f"{name}: has a shell-form HEALTHCHECK and an EXPOSE port",
              bool(cmd) and not cmd.startswith("[") and bool(port), f"{cmd!r} {port!r}")
        if not cmd or not port:
            continue
        if up:
            r = subprocess.run(["/bin/sh", "-c", cmd], env=sock_env,
                               capture_output=True, text=True, timeout=15)
            check(f"{name}: socket mode -> healthy (exit 0)", r.returncode == 0,
                  f"rc={r.returncode} {r.stderr.strip()[:200]}")
            # A bastion coder run passes only the PRIVATAE_ name; with both
            # set, the PRIVATAE_ one is the socket checked.
            r = subprocess.run(["/bin/sh", "-c", cmd], env=dict(env, PRIVATAE_SIDECAR_CONTROL_SOCK=ctl),
                               capture_output=True, text=True, timeout=15)
            check(f"{name}: PRIVATAE_SIDECAR_CONTROL_SOCK alone -> healthy (exit 0)", r.returncode == 0,
                  f"rc={r.returncode} {r.stderr.strip()[:200]}")
            r = subprocess.run(["/bin/sh", "-c", cmd],
                               env=dict(env, PRIVATAE_SIDECAR_CONTROL_SOCK=ctl,
                                        CSERIES_SIDECAR_CONTROL_SOCK=str(tmp / "absent.sock")),
                               capture_output=True, text=True, timeout=15)
            check(f"{name}: both names set -> the PRIVATAE_ socket is checked", r.returncode == 0,
                  f"rc={r.returncode} {r.stderr.strip()[:200]}")
        r = subprocess.run(["/bin/sh", "-c", cmd],
                           env=dict(sock_env, CSERIES_SIDECAR_CONTROL_SOCK=str(tmp / "absent.sock")),
                           capture_output=True, text=True, timeout=15)
        check(f"{name}: socket mode, nothing listening -> unhealthy (non-zero)",
              r.returncode != 0, f"rc={r.returncode}")
        if argv_log.exists():
            argv_log.unlink()
        r = subprocess.run(["/bin/sh", "-c", cmd], env=dict(sock_env, PATH=shim_path),
                           capture_output=True, text=True, timeout=15)
        argv = json.loads(argv_log.read_text()) if argv_log.exists() else None
        check(f"{name}: socket mode -> exactly curl -fsS --unix-socket <path> /health",
              r.returncode == 0
              and argv == ["-fsS", "--unix-socket", ctl, "http://localhost/health"],
              f"rc={r.returncode} argv={argv}")
        if argv_log.exists():
            argv_log.unlink()
        tcp_env = dict(env, PATH=shim_path)
        r = subprocess.run(["/bin/sh", "-c", cmd], env=tcp_env,
                           capture_output=True, text=True, timeout=15)
        argv = json.loads(argv_log.read_text()) if argv_log.exists() else None
        check(f"{name}: variable unset -> today's check, unchanged",
              r.returncode == 0 and argv == ["-fsS", f"http://127.0.0.1:{port}/health"],
              f"rc={r.returncode} argv={argv}")

    if p.poll() is None:
        p.terminate()
    try:
        p.communicate(timeout=5)
    except subprocess.TimeoutExpired:
        p.kill()
        p.communicate()
    shutil.rmtree(tmp, ignore_errors=True)

    if FAILS:
        print(f"[healthcheck-sock] FAIL ({len(FAILS)} failing): {FAILS}")
        return 1
    print("[healthcheck-sock] PASS (0 failing)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
