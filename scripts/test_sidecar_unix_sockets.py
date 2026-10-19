#!/usr/bin/env python3
"""The sidecar's unix-socket mode: control plane on a socket, egress through one.

Bastion coder mode: a coding run on a user's machine has `--network none`;
the bastion bind-mounts two unix sockets in:
  CSERIES_SIDECAR_CONTROL_SOCK  the sidecar serves its HTTP control plane there
                              (no TCP port, PORT not required), 0600, with the
                              same capability wall
  CSERIES_SIDECAR_EGRESS_SOCK   a loopback TCP forwarder in front of the
                              bastion's proxy socket becomes HTTP(S)_PROXY
Unset, the sidecar is exactly what it was (TCP, proxy env untouched). Set to a
relative path, serve() refuses to start.

Each case runs a real sidecar in a child process (the variables are read at
import) and talks to it over real sockets. The egress upstream is a fake proxy
on a unix socket in this process. No container, no network.

Run: APP_ROOT=<repo> python3 scripts/test_sidecar_unix_sockets.py
"""
import http.client
import json
import os
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

FAILS = []


def check(name: str, ok: bool, detail: str = ""):
    tag = "ok  " if ok else "FAIL"
    print(f"[sidecar-unix] {tag} {name}" + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILS.append(name)


CHILD = r"""
import logging, os, sys, json, urllib.request
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
sys.path.insert(0, os.environ["T_CLI"])
import cseries_sidecar as s

@s.sidecar.handler("GET", "/health")
def health():
    return {"ok": True}

@s.sidecar.handler("POST", "/echo")
def echo(req):
    return {"ok": True, "body": req.body.decode()}

@s.sidecar.handler("GET", "/env")
def env():
    return {k: os.environ.get(k) for k in (
        "HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy",
        "NO_PROXY", "no_proxy")}

@s.sidecar.handler("GET", "/fetch")
def fetch():
    # A child process of the harness: it knows only the proxy env.
    with urllib.request.urlopen("http://upstream.invalid/hello", timeout=10) as r:
        return {"status": r.status, "body": r.read().decode()}

s.sidecar.serve()
"""


class UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, path: str, timeout: float = 10.0):
        super().__init__("localhost", timeout=timeout)
        self._path = path

    def connect(self):
        sk = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sk.settimeout(self.timeout)
        sk.connect(self._path)
        self.sock = sk


def unix_req(path: str, method: str, url: str, body: bytes = b"", headers=None):
    c = UnixHTTPConnection(path)
    c.request(method, url, body=body or None, headers=headers or {})
    r = c.getresponse()
    data = r.read()
    c.close()
    try:
        return r.status, json.loads(data)
    except ValueError:
        return r.status, data


def spawn(cli: str, env_extra: dict) -> subprocess.Popen:
    env = {k: v for k, v in os.environ.items()
           if k not in ("PORT", "CSERIES_SIDECAR_CONTROL_SOCK", "CSERIES_SIDECAR_EGRESS_SOCK",
                        "PRIVATAE_SIDECAR_CONTROL_SOCK", "PRIVATAE_SIDECAR_EGRESS_SOCK",
                        "PRIVATAE_HARNESS_CAP", "PRIVATAE_HARNESS_CAP_ENFORCE",
                        "HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy",
                        "NO_PROXY", "no_proxy", "CSERIES_HARNESS_CAP",
                        "CSERIES_HARNESS_CAP_ENFORCE")}
    env.update({"T_CLI": cli, "PRIVATAE_APP_ID": "sock-test", "PYTHONUNBUFFERED": "1"})
    env.update(env_extra)
    return subprocess.Popen([sys.executable, "-c", CHILD], env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)


def wait_for(pred, timeout=10.0) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.05)
    return False


def stop(p: subprocess.Popen) -> str:
    if p.poll() is None:
        p.terminate()
    try:
        out, _ = p.communicate(timeout=5)
    except subprocess.TimeoutExpired:
        p.kill()
        out, _ = p.communicate()
    return out or ""


class FakeProxy:
    """A unix-socket HTTP proxy: answers an absolute-form GET with its own
    request line as the body, and a CONNECT with 200 then an echo."""

    def __init__(self, path: str):
        self.path = path
        self.seen: list[str] = []
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.bind(path)
        self.sock.listen(16)
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self):
        while True:
            try:
                c, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self._one, args=(c,), daemon=True).start()

    def _one(self, c):
        try:
            head = b""
            while b"\r\n\r\n" not in head:
                d = c.recv(4096)
                if not d:
                    return
                head += d
            line = head.split(b"\r\n", 1)[0].decode()
            self.seen.append(line)
            if line.startswith("CONNECT "):
                c.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
                while True:
                    d = c.recv(4096)
                    if not d:
                        return
                    c.sendall(d)
            body = line.encode()
            c.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\n"
                      b"Content-Length: " + str(len(body)).encode() +
                      b"\r\nConnection: close\r\n\r\n" + body)
        finally:
            c.close()

    def close(self):
        self.sock.close()
        try:
            os.unlink(self.path)
        except FileNotFoundError:
            pass


def main() -> int:
    app_root = os.environ.get("APP_ROOT", "/app")
    cli = os.path.join(app_root, "cli")
    tmp = Path(tempfile.mkdtemp(prefix="sc-unix-"))

    print("== 1. control plane on a unix socket ==")
    ctl_dir = tmp / "ctl"
    ctl_dir.mkdir()
    ctl = str(ctl_dir / "control.sock")
    # A stale socket file from an earlier process must not stop the bind.
    stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    stale.bind(ctl)
    stale.close()
    p = spawn(cli, {"CSERIES_SIDECAR_CONTROL_SOCK": ctl,
                    "CSERIES_HARNESS_CAP": "cap-secret-1",
                    "CSERIES_HARNESS_CAP_ENFORCE": "1"})

    def _up():
        try:
            return unix_req(ctl, "GET", "/health")[0] == 200
        except OSError:
            return False
    up = wait_for(_up)
    check("starts with no PORT and answers /health over the socket (stale file replaced)",
          up, "" if up else stop(p)[-800:])
    if up:
        mode = stat.S_IMODE(os.stat(ctl).st_mode)
        check("the socket is 0600", mode == 0o600, oct(mode))
        check("it is a socket", stat.S_ISSOCK(os.stat(ctl).st_mode))
        st, body = unix_req(ctl, "POST", "/echo", b"hi")
        check("the capability wall holds over the socket: no header -> 403",
              st == 403, f"{st} {body}")
        st, body = unix_req(ctl, "POST", "/echo", b"hi",
                            {"X-CSeries-Harness-Cap": "cap-secret-1"})
        check("...and the right capability -> 200 with the body",
              st == 200 and isinstance(body, dict) and body.get("body") == "hi",
              f"{st} {body}")
        st, body = unix_req(ctl, "GET", "/env", headers={"X-CSeries-Harness-Cap": "cap-secret-1"})
        check("control mode alone leaves the proxy env alone",
              st == 200 and all(v is None for v in body.values()), repr(body))
    log = stop(p)
    check("the startup log names the socket path", f"listening on unix:{ctl}" in log,
          log[-600:])

    print("== 2. egress through a unix socket ==")
    eg_dir = tmp / "egress"
    eg_dir.mkdir()
    eg = str(eg_dir / "egress.sock")
    proxy = FakeProxy(eg)
    ctl2 = str(ctl_dir / "control2.sock")
    p = spawn(cli, {"CSERIES_SIDECAR_CONTROL_SOCK": ctl2,
                    "CSERIES_SIDECAR_EGRESS_SOCK": eg,
                    # Whatever the image or the pod set before is replaced.
                    "HTTPS_PROXY": "http://[fd00::1]:3128"})
    up = wait_for(lambda: os.path.exists(ctl2) and _ok(ctl2))
    check("starts with both sockets", up, "" if up else stop(p)[-800:])
    lport = None
    if up:
        st, env = unix_req(ctl2, "GET", "/env")
        proxy_urls = {env.get(k) for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy")}
        ok = (len(proxy_urls) == 1 and None not in proxy_urls
              and next(iter(proxy_urls)).startswith("http://127.0.0.1:"))
        check("all four proxy variables point at one loopback forwarder", ok, repr(env))
        check("NO_PROXY and no_proxy are loopback only",
              env.get("NO_PROXY") == "localhost,127.0.0.1,::1"
              and env.get("no_proxy") == "localhost,127.0.0.1,::1", repr(env))
        if ok:
            lport = int(next(iter(proxy_urls)).rsplit(":", 1)[1])
        st, body = unix_req(ctl2, "GET", "/fetch")
        check("a request made through the proxy env reaches the unix-socket upstream",
              st == 200 and isinstance(body, dict)
              and body.get("body", "").startswith("GET http://upstream.invalid/hello HTTP/1."),
              f"{st} {body} seen={proxy.seen}")
    if lport:
        c = socket.create_connection(("127.0.0.1", lport), timeout=5)
        c.sendall(b"CONNECT example.com:443 HTTP/1.1\r\nHost: example.com:443\r\n\r\n")
        got = b""
        while b"\r\n\r\n" not in got:
            d = c.recv(4096)
            if not d:
                break
            got += d
        c.sendall(b"ping-through-the-tunnel")
        echo = b""
        while len(echo) < len(b"ping-through-the-tunnel"):
            d = c.recv(4096)
            if not d:
                break
            echo += d
        c.close()
        check("CONNECT tunnels bytes both ways through the forwarder",
              got.startswith(b"HTTP/1.1 200") and echo == b"ping-through-the-tunnel",
              repr((got, echo)))
        # Upstream gone: the forwarder closes the client instead of hanging.
        proxy.close()
        c = socket.create_connection(("127.0.0.1", lport), timeout=5)
        c.settimeout(5)
        try:
            c.sendall(b"GET http://x/ HTTP/1.1\r\n\r\n")
            d = c.recv(10)
            closed = d == b""
        except (ConnectionResetError, BrokenPipeError):
            closed = True
        except socket.timeout:
            closed = False
        c.close()
        check("an unreachable upstream closes the client connection", closed)
    else:
        proxy.close()
    log = stop(p)
    check("the log names the unix egress bridge",
          f"unix egress bridge: 127.0.0.1:{lport} -> {eg}" in log, log[-600:])

    print("== 2b. the PRIVATAE_ names alone (what a bastion coder run passes) ==")
    eg_b = str(eg_dir / "egress-b.sock")
    proxy_b = FakeProxy(eg_b)
    ctl3 = str(ctl_dir / "control3.sock")
    p = spawn(cli, {"PRIVATAE_SIDECAR_CONTROL_SOCK": ctl3,
                    "PRIVATAE_SIDECAR_EGRESS_SOCK": eg_b})
    up = wait_for(lambda: os.path.exists(ctl3) and _ok(ctl3))
    check("starts on the control socket named by PRIVATAE_SIDECAR_CONTROL_SOCK", up,
          "" if up else stop(p)[-800:])
    if up:
        st, body = unix_req(ctl3, "GET", "/fetch")
        check("egress goes through the socket named by PRIVATAE_SIDECAR_EGRESS_SOCK",
              st == 200 and isinstance(body, dict)
              and body.get("body", "").startswith("GET http://upstream.invalid/hello HTTP/1."),
              f"{st} {body} seen={proxy_b.seen}")
    proxy_b.close()
    stop(p)

    print("== 3. fail closed on a relative path ==")
    for var in ("CSERIES_SIDECAR_CONTROL_SOCK", "CSERIES_SIDECAR_EGRESS_SOCK",
                "PRIVATAE_SIDECAR_CONTROL_SOCK", "PRIVATAE_SIDECAR_EGRESS_SOCK"):
        p = spawn(cli, {var: "relative/x.sock", "PORT": "18999"})
        try:
            p.wait(timeout=10)
        except subprocess.TimeoutExpired:
            pass
        rc = p.poll()
        log = stop(p)
        check(f"{var}=relative path -> serve() raises RuntimeError and exits",
              rc not in (None, 0) and "RuntimeError" in log and var in log, f"rc={rc} {log[-300:]}")

    print("== 4. unset: TCP, unchanged ==")
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    p = spawn(cli, {"PORT": str(port), "PRIVATAE_SIDECAR_BIND": "127.0.0.1"})

    def _tcp_ok():
        try:
            c = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
            c.request("GET", "/health")
            return c.getresponse().status == 200
        except OSError:
            return False
    up = wait_for(_tcp_ok)
    check("no socket variables: serves TCP on PORT", up)
    if up:
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        c.request("GET", "/env")
        env = json.loads(c.getresponse().read())
        check("...and the proxy env is untouched (still unset)",
              all(v is None for v in env.values()), repr(env))
    log = stop(p)
    check("...and the log has no unix lines", "unix" not in log, log[-400:])
    p = spawn(cli, {})
    try:
        p.wait(timeout=10)
    except subprocess.TimeoutExpired:
        pass
    rc = p.poll()
    log = stop(p)
    check("no socket variables and no PORT: the PORT refusal still fires",
          rc not in (None, 0) and "PORT env not set" in log, f"rc={rc} {log[-300:]}")

    if FAILS:
        print(f"[sidecar-unix] FAIL ({len(FAILS)} failing): {FAILS}")
        return 1
    print("[sidecar-unix] PASS (0 failing)")
    return 0


def _ok(path: str) -> bool:
    try:
        return unix_req(path, "GET", "/health")[0] == 200
    except OSError:
        return False


if __name__ == "__main__":
    sys.exit(main())
