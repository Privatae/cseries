#!/usr/bin/env python3
"""Invariant: the bastion git lane (server/network/git_relay.py) relays CONNECT to the
platform's forge on :443 and NOTHING else — no absolute-form requests, no other host or
port, no bytes smuggled after the CONNECT head (with
absolute-form allowed, a second request after the first blank line was spliced to squid
unchecked — general egress from a DMZ address for any local process). Pure; no sockets.
Run: python3 scripts/check_git_lane.py   (CSERIES_ROOT=… for a tree other than /app)
"""
import asyncio
import os
import sys

sys.path.insert(0, os.environ.get("CSERIES_ROOT", "/app"))
from server.network import git_relay  # noqa: E402
from server.network.nac_egress import EgressProxy  # noqa: E402


class _WS:
    def __init__(self, frames):
        self.f, self.sent = list(frames), []

    async def receive_bytes(self):
        if not self.f:
            raise RuntimeError("closed")
        return self.f.pop(0)

    async def send_bytes(self, b):
        self.sent.append(b)


def main() -> int:
    os.environ["GITEA_URL"] = "https://forge.example"
    calls = []

    class _Up:
        """A fake node squid: answers the CONNECT with `reply`, records what it was sent."""
        def __init__(self, reply): self.reply, self.got, self.closed = reply, b"", False
        async def read(self, n):
            r, self.reply = self.reply, b""
            return r
        def write(self, b): self.got += b
        async def drain(self): pass
        def close(self): self.closed = True
    upstreams = []
    async def fake_open(host, port):
        u = _Up(fake_open.reply); upstreams.append(u); return u, u, True
    fake_open.reply = b"HTTP/1.1 200 Connection Established\r\n\r\n"
    git_relay._open_upstream = fake_open
    calls.clear()
    async def fake_pipe(cr, cw, rr, rw):
        calls.append(("CONNECT", "spliced"))
    EgressProxy._pipe = staticmethod(fake_pipe)
    proxy = EgressProxy(pool=None, entity_id=0, verify_pubkey=None)

    async def run(frames):
        calls.clear()
        ws = _WS(frames)
        await git_relay.GitWorker(ws, bastion_token_id=1, proxy=proxy).serve()
        await asyncio.sleep(0.01)
        return list(calls), b"".join(ws.sent)

    fails = []
    ok = asyncio.run(run([b"DCONNECT forge.example:443 HTTP/1.1\r\nHost: forge.example:443\r\n\r\n"]))
    if not (ok[0] and ok[0][0][0] == "CONNECT"):
        fails.append("CONNECT to the forge:443 must be relayed")
    split = asyncio.run(run([b"DCONNECT FORGE.example:443 HTTP/1.1\r\n", b"D\r\n"]))
    if not split[0]:
        fails.append("a head split across frames (and upper-case host) must still be relayed")
    for label, frames in (
        ("absolute-form GET", [b"DGET http://forge.example/a HTTP/1.1\r\nHost: forge.example\r\n\r\n"]),
        ("smuggled second request", [b"DGET http://forge.example/a HTTP/1.1\r\nHost: forge.example\r\n",
                                     b"D\r\n\r\nGET http://evil.example/ HTTP/1.1\r\nHost: evil.example\r\n\r\n"]),
        ("bytes after the CONNECT head", [b"DCONNECT forge.example:443 HTTP/1.1\r\n\r\nGET http://evil.example/ HTTP/1.1\r\n\r\n"]),
        ("port 22", [b"DCONNECT forge.example:22 HTTP/1.1\r\n\r\n"]),
        ("another host", [b"DCONNECT evil.example:443 HTTP/1.1\r\n\r\n"]),
        ("forge as a suffix of another host", [b"DCONNECT forge.example.evil.com:443 HTTP/1.1\r\n\r\n"]),
    ):
        r = asyncio.run(run(frames))
        if r[0] or b"403" not in r[1]:
            fails.append(f"{label} must be refused with 403 (got dispatch={r[0]})")
    # a DENIED CONNECT must close, never splice — squid keeps the connection alive on a
    # 403 and would parse the client's next bytes as a new request (re-review 2026-08-28)
    fake_open.reply = b"HTTP/1.1 403 Forbidden\r\nConnection: keep-alive\r\nContent-Length: 0\r\n\r\n"
    r = asyncio.run(run([b"DCONNECT forge.example:443 HTTP/1.1\r\n\r\n",
                         b"DGET http://evil.example/ HTTP/1.1\r\nHost: evil.example\r\n\r\n"]))
    if r[0] or b"403" not in r[1] or b"evil" in upstreams[-1].got:
        fails.append(f"a denied CONNECT must relay the refusal and close, never splice (dispatch={r[0]}, "
                     f"upstream got evil={b'evil' in upstreams[-1].got})")
    fake_open.reply = b"HTTP/1.1 200 Connection Established\r\n\r\n"
    if fails:
        print("GIT-LANE CHECK FAILED:")
        for f in fails:
            print("  -", f)
        return 1
    print("GIT-LANE INVARIANTS OK — CONNECT to the platform forge:443 only; everything else 403")
    return 0


if __name__ == "__main__":
    sys.exit(main())
