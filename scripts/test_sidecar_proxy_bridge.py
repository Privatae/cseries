#!/usr/bin/env python3
"""
Sidecar proxy bridge — idle-transparency gate (2026-08-21).

Every harness sidecar routes its CLI's HTTPS through cli/cseries_sidecar.py's
loopback bridge when the operator hands it an IPv6-literal proxy (it always
does: HTTPS_PROXY=http://[$(HOST_IP)]:3128). The bridge opened its upstream
socket with `socket.create_connection(..., timeout=10)` — and in Python that
timeout STAYS on the socket after the connect, so the pump's recv() raised
TimeoutError after 10 s of upstream silence and the pair was closed as if the
peer had hung up.

MEASURED on one Host's seven-harness fan-out (session 3ba241d3, 2026-08-21):
  * grok: four think→prose cycles, each cut when the model went quiet to
    produce its tool call; the final error frame landed 10.0 s after the last
    streamed token — "reqwest error stream: Transport error: error decoding
    response body", 883 s, no file written, exit 1.
  * antigravity: three error_message steps 15 s apart after an agent_response
    that streamed 0 output tokens for 13 s (time-to-first-byte > 10 s on a
    124k-token context) — "There was a network issue connecting to the
    server, please try again.", exit 1.

Gates:
  1. _bridge_connect returns a socket with NO timeout (connect stays bounded
     by BRIDGE_CONNECT_TIMEOUT_S, which is > 0).
  2. End to end through the real bridge: a client speaks to a fake upstream
     that stays SILENT for longer than the old 10 s and then answers — the
     answer arrives intact (before the fix the pump closed the pair at 10 s).
  3. NEGATIVE CONTROL: the same upstream through a pump whose socket carries
     the old 10 s timeout is cut — proves gate 2 tests the real thing.

Runs in ~13 s (the silence must outlast the old timeout).
Exit 0 = all pass; exit 1 + FAIL lines otherwise.
"""
import importlib
import os
import socket
import sys
import threading
import time

FAILS = []
SILENCE_S = 11.0          # > the old 10 s create_connection timeout
OLD_TIMEOUT_S = 10.0


def check(name, ok, detail=""):
    print(("ok   " if ok else "FAIL ") + name + ((" -- " + str(detail)) if not ok and detail else ""))
    if not ok:
        FAILS.append(name)


def _silent_upstream(silence_s: float):
    """A TCP server on [::1] that reads one request, stays quiet for
    silence_s, then sends a reply and closes. Returns (port, stop)."""
    srv = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("::1", 0))
    srv.listen(8)
    port = srv.getsockname()[1]

    def _serve():
        while True:
            try:
                c, _ = srv.accept()
            except OSError:
                return
            def _one(c=c):
                try:
                    c.recv(65536)
                    time.sleep(silence_s)
                    c.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 5\r\n\r\nLATE!")
                except OSError:
                    pass
                finally:
                    try:
                        c.close()
                    except OSError:
                        pass
            threading.Thread(target=_one, daemon=True).start()

    threading.Thread(target=_serve, daemon=True).start()
    return port, srv.close


def _load_sidecar(proxy_url: str):
    """Import cli/cseries_sidecar fresh under HTTPS_PROXY=proxy_url. The bridge
    is installed by Sidecar.serve(), so importing does not start it; we call
    _bridge_ipv6_proxy() ourselves."""
    os.environ["HTTPS_PROXY"] = proxy_url
    os.environ["HTTP_PROXY"] = proxy_url
    os.environ.setdefault("PORT", "8845")
    import cseries_sidecar
    return importlib.reload(cseries_sidecar)


def _roundtrip_via(host: str, port: int, timeout: float) -> bytes:
    c = socket.create_connection((host, port), timeout=timeout)
    try:
        c.sendall(b"GET / HTTP/1.1\r\nHost: x\r\n\r\n")
        out = b""
        while True:
            d = c.recv(65536)
            if not d:
                break
            out += d
        return out
    finally:
        c.close()


def main() -> int:
    app_root = os.environ.get("APP_ROOT", "/app")
    sys.path.insert(0, os.path.join(app_root, "cli"))

    up_port, stop_upstream = _silent_upstream(SILENCE_S)
    try:
        mod = _load_sidecar(f"http://[::1]:{up_port}")
        check("cseries_sidecar exposes _bridge_connect (the fix is present)",
              hasattr(mod, "_bridge_connect"))
        if not hasattr(mod, "_bridge_connect"):
            return 1

        # ── 1. the upstream socket carries no timeout after connect ──────────
        s = mod._bridge_connect("::1", up_port)
        try:
            check("_bridge_connect: connect timeout is bounded (> 0)",
                  float(getattr(mod, "BRIDGE_CONNECT_TIMEOUT_S", 0)) > 0)
            check("_bridge_connect: the connected socket has NO timeout (gettimeout() is None)",
                  s.gettimeout() is None, s.gettimeout())
        finally:
            s.close()

        # ── 2. end to end through the real bridge ───────────────────────────
        mod._bridge_ipv6_proxy()
        bridged = os.environ.get("HTTPS_PROXY", "")
        check("bridge repointed HTTPS_PROXY at 127.0.0.1", bridged.startswith("http://127.0.0.1:"), bridged)
        bport = int(bridged.rsplit(":", 1)[-1])
        t0 = time.time()
        try:
            body = _roundtrip_via("127.0.0.1", bport, SILENCE_S + 10)
        except OSError as e:
            body = b""
            print("   (bridge round trip raised: %r)" % (e,))
        dt = time.time() - t0
        check("bridge: a reply after %.0f s of upstream silence arrives intact" % SILENCE_S,
              body.endswith(b"LATE!") and dt >= SILENCE_S - 0.5,
              "got %r after %.1fs" % (body[-40:], dt))

        # ── 3. negative control: the OLD pump (timeout left on the socket) cuts ─
        def _old_pump_pair():
            l = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            l.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            l.bind(("127.0.0.1", 0))
            l.listen(4)
            lp = l.getsockname()[1]

            def _pump(a, b):
                try:
                    while True:
                        d = a.recv(65536)
                        if not d:
                            break
                        b.sendall(d)
                except OSError:
                    pass
                finally:
                    for sk in (a, b):
                        try:
                            sk.shutdown(socket.SHUT_RDWR)
                        except OSError:
                            pass
                        try:
                            sk.close()
                        except OSError:
                            pass

            def _accept():
                c, _ = l.accept()
                up = socket.create_connection(("::1", up_port), timeout=OLD_TIMEOUT_S)  # the OLD call
                threading.Thread(target=_pump, args=(c, up), daemon=True).start()
                threading.Thread(target=_pump, args=(up, c), daemon=True).start()
            threading.Thread(target=_accept, daemon=True).start()
            return lp
        old_port = _old_pump_pair()
        t0 = time.time()
        try:
            old_body = _roundtrip_via("127.0.0.1", old_port, SILENCE_S + 10)
        except OSError as e:
            old_body = b"<raised %s>" % type(e).__name__.encode()
        dt_old = time.time() - t0
        check("negative control: the OLD pump (timeout kept) cuts the pair near %.0f s, reply lost" % OLD_TIMEOUT_S,
              not old_body.endswith(b"LATE!") and dt_old < SILENCE_S,
              "got %r after %.1fs" % (old_body[-40:], dt_old))
    finally:
        stop_upstream()

    print()
    if FAILS:
        print("FAILED: sidecar proxy bridge (%d)" % len(FAILS))
        for f in FAILS:
            print("  - " + f)
        return 1
    print("PASS: sidecar proxy bridge — upstream silence is never a reason to cut a harness stream")
    return 0


if __name__ == "__main__":
    sys.exit(main())
