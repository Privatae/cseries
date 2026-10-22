#!/usr/bin/env python3
"""Pairing a bastion with a Host on a server of its own.

  1. CSERIES_LANE_HOST names where bastions on other machines reach the
     enroll and lane ports: the redeem URL and the lane address both carry
     it (an IPv6 address in brackets), on the configured ports, and both name
     the same host, because the bastion refuses a lane on any other
     (client-go pair_tcp.go checkLaneAddr). Unset or malformed, both stay on
     the machine's loopback as before.
  2. The pairing helper (server/network/door_helper.py): one JSON line out,
     one back, over a unix socket; only a public unicast address is asked
     for; a refusal, a malformed or silent answer, or a window longer than
     an hour is an error, never an open port shown to the owner.
  3. POST /api/network/bastions/setup on a Host with no platform answers the
     Host CA, the redeem URL and the lane address. On a server of its own the
     pair command is the TCP one (--redeem-url ... --host-ca host-ca.pem
     <token>) and the install step carries the CA to save. With the helper
     set, a fresh passkey check comes first, the port opens to the
     requester's address (through a trusted proxy, the address it forwarded
     for) and the answer names the window; without the check, from a private
     address, or when the helper refuses, nothing is minted. A Host with a
     platform answers as before.
  4. A Host in a VM on the person's machine (no shared socket directory, so
     the local bastion's sockets are not served) shows the TCP command on
     127.0.0.1 too, since the socket path names nothing on the machine; a
     Host whose sockets are served keeps the socket command. The Host's own
     manage_bastion tool answers the same command as the Network app.

No database; the pairing helper is a stand-in on a unix socket.

    python3 scripts/test_remote_pairing_unit.py      (from the repo root)
"""
import asyncio
import json
import os
import sys
import tempfile
from urllib.parse import urlsplit

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)

from server.network import local_bastion as LB  # noqa: E402

failed = 0


def check(cond, what, detail=""):
    global failed
    if cond:
        print(f"OK    {what}")
    else:
        failed = 1
        print(f"FAIL  {what} {detail}")


class Env:
    """Set environment variables for one block, then put them back."""

    def __init__(self, **values):
        self.values = values
        self.saved = {}

    def __enter__(self):
        for k, v in self.values.items():
            self.saved[k] = os.environ.get(k)
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def __exit__(self, *exc):
        for k, v in self.saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _lane_host_of(lane: str) -> str:
    """The host part of host:port as Go's net.SplitHostPort reads it."""
    host = lane.rsplit(":", 1)[0]
    return host[1:-1] if host.startswith("[") else host


def lane_host():
    with Env(CSERIES_LANE_HOST=None, CSERIES_ENROLL_PORT=None, CSERIES_LANE_PORT=None):
        check(LB.enroll_url() == "https://127.0.0.1:8810/v1/local/redeem"
              and LB.lane_address() == "127.0.0.1:8811" and not LB.remote_lane(),
              "unset, the pairing ports are the machine's loopback", (LB.enroll_url(), LB.lane_address()))
    with Env(CSERIES_LANE_HOST="Home.Box.Example", CSERIES_ENROLL_PORT=None, CSERIES_LANE_PORT=None):
        check(LB.enroll_url() == "https://home.box.example:8810/v1/local/redeem"
              and LB.lane_address() == "home.box.example:8811" and LB.remote_lane(),
              "a host name is the redeem URL's host and the lane's", (LB.enroll_url(), LB.lane_address()))
    with Env(CSERIES_LANE_HOST="home.box.example", CSERIES_ENROLL_PORT="18810", CSERIES_LANE_PORT="18811"):
        check(LB.enroll_url() == "https://home.box.example:18810/v1/local/redeem"
              and LB.lane_address() == "home.box.example:18811",
              "the published ports still apply", (LB.enroll_url(), LB.lane_address()))
    for raw in ("2001:db8::5", "[2001:db8::5]"):
        with Env(CSERIES_LANE_HOST=raw, CSERIES_ENROLL_PORT=None, CSERIES_LANE_PORT=None):
            url, lane = LB.enroll_url(), LB.lane_address()
            check(url == "https://[2001:db8::5]:8810/v1/local/redeem" and lane == "[2001:db8::5]:8811"
                  and urlsplit(url).hostname == _lane_host_of(lane) == "2001:db8::5",
                  f"an IPv6 address ({raw}) is bracketed and both name it", (url, lane))
    with Env(CSERIES_LANE_HOST="203.0.113.9", CSERIES_ENROLL_PORT=None, CSERIES_LANE_PORT=None):
        check(LB.lane_address() == "203.0.113.9:8811", "an IPv4 address is used as given")
    with Env(CSERIES_LANE_HOST="nas", CSERIES_ENROLL_PORT=None, CSERIES_LANE_PORT=None):
        check(LB.lane_address() == "nas:8811", "a one-label name on a home network is used as given")
    for bad in ("evil.example/x", "user@home.box.example", "home..example", "-x.example", "a_b.example", ""):
        with Env(CSERIES_LANE_HOST=bad, CSERIES_ENROLL_PORT=None, CSERIES_LANE_PORT=None):
            check(LB.lane_address() == "127.0.0.1:8811" and not LB.remote_lane(),
                  f"a value that is not a host ({bad!r}) leaves the loopback", LB.lane_address())


# A public address the tests may use: the network's own documented edge.
PUBLIC = "40.160.88.174"
FAKE_CA = "-----BEGIN CERTIFICATE-----\nMIIBinventedCAforthistestonly\n-----END CERTIFICATE-----\n"


class Helper:
    """A stand-in for the server's pairing helper on a unix socket."""

    def __init__(self, path):
        self.path = path
        self.lines = []
        self.answer = {"ok": True, "expires_in": 900}
        self.raw = None          # bytes to send instead of `answer`
        self.silent = False
        self.server = None

    async def _serve(self, reader, writer):
        line = await reader.readline()
        self.lines.append(line)
        if self.silent:
            await asyncio.sleep(5)
        try:
            writer.write(self.raw if self.raw is not None else (json.dumps(self.answer) + "\n").encode())
            await writer.drain()
        except ConnectionError:
            pass         # the silent case: the Host gave up and closed first
        writer.close()

    async def start(self):
        self.server = await asyncio.start_unix_server(self._serve, path=self.path)

    async def stop(self):
        self.server.close()
        await self.server.wait_closed()


async def helper_protocol(d):
    from server.network import door_helper as DH
    check(DH.public_address(PUBLIC) == PUBLIC and DH.public_address("::ffff:" + PUBLIC) == PUBLIC,
          "a public address passes, an IPv4-mapped one as its IPv4 form")
    for bad in ("10.88.0.9", "192.168.1.5", "127.0.0.1", "169.254.1.1", "100.64.0.1", "::1",
                "fd00:aa::5", "fe80::1", "198.51.100.20", "2001:db8::5", "224.0.0.1", "?", "", None):
        check(DH.public_address(bad) is None, f"not a public unicast address: {bad!r}")
    h = Helper(os.path.join(d, "h.sock"))
    await h.start()
    try:
        got = await DH.open_pairing(PUBLIC, path=h.path)
        sent = [json.loads(x) for x in h.lines]
        check(got == 900 and sent == [{"op": "pairing.open", "addr": PUBLIC}] and h.lines[0].endswith(b"\n"),
              "the helper is asked in one JSON line and its window comes back", (got, h.lines))
        h.answer = {"ok": False, "error": "too many opens this hour"}
        err = await _raises(DH.open_pairing(PUBLIC, path=h.path), DH.HelperError)
        check(err is not None and "too many opens" in str(err), "a refusal carries the helper's reason", err)
        for raw, what in ((b"not json\n", "an answer that is not JSON"),
                          (b'{"ok": true}\n', "an answer with no window"),
                          (b'{"ok": true, "expires_in": 86400}\n', "a window of a day"),
                          (b'{"ok": true, "expires_in": true}\n', "a window that is not a number"),
                          (b"", "no answer at all")):
            h.raw = raw
            check(await _raises(DH.open_pairing(PUBLIC, path=h.path), DH.HelperError) is not None,
                  f"an error, not an open port: {what}")
        h.raw, h.silent = None, True
        check(await _raises(DH.open_pairing(PUBLIC, path=h.path, timeout=0.3), DH.HelperError) is not None,
              "a helper that does not answer in time is an error")
        h.silent = False
    finally:
        await h.stop()
    check(await _raises(DH.open_pairing(PUBLIC, path=os.path.join(d, "none.sock")), DH.HelperError)
          is not None, "no helper listening is an error")


async def _raises(coro, exc_type):
    try:
        await coro
    except exc_type as exc:
        return exc
    return None


def _request(peer, headers=()):
    from starlette.requests import Request
    body = b"{}"
    sent = [False]

    async def receive():
        if not sent[0]:
            sent[0] = True
            return {"type": "http.request", "body": body, "more_body": False}
        return {"type": "http.disconnect"}
    hdrs = [(b"content-type", b"application/json"), (b"host", b"home.box.example")]
    hdrs += [(k.encode(), v.encode()) for k, v in headers]
    return Request({"type": "http", "method": "POST", "path": "/api/network/bastions/setup",
                    "headers": hdrs, "client": (peer, 40000), "server": ("10.88.0.5", 8800),
                    "scheme": "http", "query_string": b"", "root_path": ""}, receive)


async def setup_route(d):
    from fastapi import HTTPException
    from server import brain_service, owner_passkeys
    from server.middleware import owner_session
    from server.network import bastion_enroll, host_ca, nac_commit
    from server.routes import network as NR

    minted = []

    async def fake_handle(eid):
        return "home"

    async def fake_mint(pool, *, entity_id, handle, name):
        minted.append(name)
        return {"token": "skb_inventedtoken", "expires_in_s": 900, "name": name or "bastion",
                "run": {"install": "macOS / Linux:  curl -fsSL https://privatae.ai/bastion/install.sh | sh",
                        "pair": "privatae-bastion pair --redeem-url unix:///run/privatae-host/enroll.sock "
                                "skb_inventedtoken",
                        "note": "the socket note"}}

    async def fake_ensure(pool):
        return None

    async def fake_ca(pool):
        return FAKE_CA

    session = {"token": "tok-owner"}

    async def fake_live(scope):
        return ("owner@example.com", session["token"]) if session["token"] else (None, None)

    saved = (brain_service.get_pool, bastion_enroll.mint_pairing_token, bastion_enroll.resolve_self_handle,
             host_ca.ensure, host_ca.ca_cert_pem, LB.standalone, owner_session.live_session, LB.STATE)
    brain_service.get_pool = lambda: object()
    bastion_enroll.mint_pairing_token = fake_mint
    bastion_enroll.resolve_self_handle = fake_handle
    host_ca.ensure, host_ca.ca_cert_pem = fake_ensure, fake_ca
    owner_session.live_session = fake_live
    h = Helper(os.path.join(d, "door.sock"))
    await h.start()
    try:
        LB.standalone = lambda: True
        cloud = dict(CSERIES_LANE_HOST="home.box.example", CSERIES_DOOR_HELPER=h.path,
                     CSERIES_TRUSTED_PROXIES="10.88.0.1", CSERIES_ENROLL_PORT=None, CSERIES_LANE_PORT=None)
        via_proxy = _request("10.88.0.1", [("x-forwarded-for", PUBLIC)])

        with Env(**cloud):
            owner_passkeys._step_ups.clear()
            err = await _raises(NR.setup_bastion(via_proxy), HTTPException)
            check(err is not None and err.status_code == 403 and "/passkey" in str(err.detail)
                  and not h.lines and not minted,
                  "without a fresh passkey check: 403 naming /passkey, the port stays shut, nothing minted",
                  err and err.detail)

            owner_passkeys._note_step_up("tok-owner", owner_passkeys.PASSKEY)
            ans = await NR.setup_bastion(_request("10.88.0.1", [("x-forwarded-for", PUBLIC)]))
            cmd = ("privatae-bastion pair --redeem-url https://home.box.example:8810/v1/local/redeem "
                   "--host-ca host-ca.pem skb_inventedtoken")
            check([json.loads(x) for x in h.lines] == [{"op": "pairing.open", "addr": PUBLIC}],
                  "with the check, the port is opened to the address the trusted proxy forwarded for",
                  h.lines)
            check(ans.get("pairing_window") == {"address": PUBLIC, "expires_in_s": 900},
                  "the answer names the window", ans.get("pairing_window"))
            check(ans.get("host_ca_pem") == FAKE_CA
                  and ans.get("enroll") == "https://home.box.example:8810/v1/local/redeem"
                  and ans.get("lane_address") == "home.box.example:8811",
                  "the answer carries the Host CA, the TCP redeem URL and the lane", ans)
            check(ans.get("pair_command") == cmd and ans["run"]["pair"] == cmd,
                  "the pair command shown is the exact TCP one", ans["run"]["pair"])
            import base64
            import re
            inst = ans["run"]["install"]
            posix = re.search(r"echo (\S+) \| base64 -d > host-ca\.pem", inst)
            win = re.search(r"\[Convert\]::FromBase64String\('(\S+)'\)", inst)
            check(posix and win and base64.b64decode(posix.group(1)).decode() == FAKE_CA
                  and base64.b64decode(win.group(1)).decode() == FAKE_CA
                  and PUBLIC in ans["run"]["note"] and "15 minutes" in ans["run"]["note"],
                  "the install step carries the CA as one command per kind of machine, the note the window",
                  ans["run"])

            h.lines.clear()
            minted.clear()
            err = await _raises(NR.setup_bastion(_request("10.88.0.1", [("x-forwarded-for", "192.168.1.5")])),
                                HTTPException)
            check(err is not None and err.status_code == 400 and not h.lines and not minted,
                  "from a private address: 400, the helper is not asked, nothing minted", err and err.detail)
            err = await _raises(NR.setup_bastion(_request(PUBLIC, [("x-forwarded-for", "198.51.100.20")])),
                                HTTPException)
            check(h.lines and json.loads(h.lines[-1])["addr"] == PUBLIC,
                  "X-Forwarded-For from a peer that is not a trusted proxy is not believed", h.lines)
            h.lines.clear()
            minted.clear()
            h.answer = {"ok": False, "error": "too many opens this hour"}
            err = await _raises(NR.setup_bastion(via_proxy_again()), HTTPException)
            check(err is not None and err.status_code == 502 and "too many opens" in str(err.detail)
                  and not minted, "a helper refusal: 502 with its reason, nothing minted", err and err.detail)
            h.answer = {"ok": True, "expires_in": 900}
            session["token"] = None
            err = await _raises(NR.setup_bastion(via_proxy_again()), HTTPException)
            check(err is not None and err.status_code == 403, "no owner session: no check, 403", err)
            session["token"] = "tok-owner"

        with Env(CSERIES_LANE_HOST="home.box.example", CSERIES_DOOR_HELPER=None,
                 CSERIES_TRUSTED_PROXIES=None, CSERIES_ENROLL_PORT=None, CSERIES_LANE_PORT=None):
            owner_passkeys._step_ups.clear()
            h.lines.clear()
            ans = await NR.setup_bastion(_request("10.88.0.1"))
            check("pairing_window" not in ans and ans["run"]["pair"].startswith("privatae-bastion pair --redeem-url https://")
                  and not h.lines, "with no helper set: the TCP command, no check, no window")

        with Env(CSERIES_LANE_HOST=None, CSERIES_DOOR_HELPER=None, CSERIES_ENROLL_PORT=None,
                 CSERIES_LANE_PORT=None):
            LB.STATE = "listening"
            ans = await NR.setup_bastion(_request("127.0.0.1"))
            check(ans.get("host_ca_pem") == FAKE_CA
                  and ans.get("enroll") == "https://127.0.0.1:8810/v1/local/redeem"
                  and ans["run"]["pair"].startswith("privatae-bastion pair --redeem-url unix://"),
                  "a Host serving its sockets on this machine: CA and loopback redeem URL, the socket "
                  "pair command kept", ans["run"])

            LB.STATE = "no-shared-dir"
            local = ("privatae-bastion pair --redeem-url https://127.0.0.1:8810/v1/local/redeem "
                     "--host-ca host-ca.pem skb_inventedtoken")
            ans = await NR.setup_bastion(_request("127.0.0.1"))
            check(ans["run"]["pair"] == local and ans.get("pair_command") == local
                  and "base64 -d > host-ca.pem" in ans["run"]["install"]
                  and "127.0.0.1" in ans["run"]["note"] and "pairing_window" not in ans,
                  "a Host in a VM, whose sockets are not served: the TCP command on 127.0.0.1, the CA "
                  "to save, a note naming this computer", ans["run"])
            tool = await nac_commit.manage_bastion(object(), 1, "enroll", name="bastion")
            check(tool["run"]["pair"] == local,
                  "the Host's manage_bastion tool answers the same command", tool["run"]["pair"])

        LB.standalone = lambda: False
        with Env(CSERIES_LANE_HOST="home.box.example", CSERIES_DOOR_HELPER=h.path):
            h.lines.clear()
            ans = await NR.setup_bastion(_request("127.0.0.1"))
            check("host_ca_pem" not in ans and "pairing_window" not in ans and not h.lines,
                  "a Host with a platform answers as before", ans.keys())
    finally:
        await h.stop()
        (brain_service.get_pool, bastion_enroll.mint_pairing_token, bastion_enroll.resolve_self_handle,
         host_ca.ensure, host_ca.ca_cert_pem, LB.standalone, owner_session.live_session, LB.STATE) = saved


def via_proxy_again():
    return _request("10.88.0.1", [("x-forwarded-for", PUBLIC)])


async def async_main():
    with tempfile.TemporaryDirectory(prefix="pair") as d:
        await helper_protocol(d)
        await setup_route(d)


def main():
    lane_host()
    asyncio.run(async_main())


main()
sys.exit(failed)
