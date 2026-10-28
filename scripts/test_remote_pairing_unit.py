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
  5. Wherever the TCP command is shown, the answer also carries the pairing
     code (skp_: exactly v, u, ca, t, the CA's SHA-256 over its DER) and one
     line per kind of machine that runs the installer with it; the steps stay.
     `pair_from` opens the window to that address only, after the same passkey
     check; anything but one public unicast address is refused, unasked.
  6. The certificate renewal loop chains the Host CA for the enroll port, as
     its first certificate was made, and not for the lane.

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
# Another public address, for a machine on another network than the browser's.
ELSEWHERE = "1.1.1.1"


def _invented_ca() -> str:
    """A CA certificate made here, so the pairing code's fingerprint is taken
    over real DER."""
    import datetime
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Host CA invented")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now).not_valid_after(now + datetime.timedelta(days=1))
            .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
            .sign(key, hashes.SHA256()))
    return cert.public_bytes(serialization.Encoding.PEM).decode()


FAKE_CA = _invented_ca()


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
                "fd12:3456:789a::5", "fe80::1", "198.51.100.20", "2001:db8::5", "224.0.0.1", "?", "", None,
                # A scope ID: ipaddress keeps any text after "%" and still calls
                # the address global, and the helper writes str() into nft.
                "2606:4700::1111%eth0", "2606:4700::1111%x}\nflush ruleset\n#", "::ffff:" + PUBLIC + "%x"):
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
        h.lines.clear()
        h.answer = {"ok": True}
        await DH.close_pairing(PUBLIC, path=h.path)
        check([json.loads(x) for x in h.lines] == [{"op": "pairing.close", "addr": PUBLIC}],
              "a close asks the helper in one JSON line, op and addr only", h.lines)
        h.answer = {"ok": False, "error": "addr must be a global unicast address"}
        err = await _raises(DH.close_pairing(PUBLIC, path=h.path), DH.HelperError)
        check(err is not None and "global unicast" in str(err), "a refused close carries the helper's reason", err)
        h.raw = b"not json\n"
        check(await _raises(DH.close_pairing(PUBLIC, path=h.path), DH.HelperError) is not None,
              "a close answered with something that is not JSON is an error")
        h.raw, h.answer = None, {"ok": True, "expires_in": 900}
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


def _request(peer, headers=(), body_obj=None, host="home.box.example"):
    from starlette.requests import Request
    body = json.dumps(body_obj or {}).encode()
    sent = [False]

    async def receive():
        if not sent[0]:
            sent[0] = True
            return {"type": "http.request", "body": body, "more_body": False}
        return {"type": "http.disconnect"}
    hdrs = [(b"content-type", b"application/json"), (b"host", host.encode())]
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
            check(ans.get("pairing_window") == {"address": PUBLIC, "expires_in_s": 900, "browser": True},
                  "the answer names the window, opened to this browser", ans.get("pairing_window"))
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

            pairing_code_checks(ans)

            # pair_from: a machine on another network than the browser's.
            h.lines.clear()
            ans = await NR.setup_bastion(_request("10.88.0.1", [("x-forwarded-for", PUBLIC)],
                                                  {"pair_from": " " + ELSEWHERE + " "}))
            check([json.loads(x) for x in h.lines] == [{"op": "pairing.open", "addr": ELSEWHERE}],
                  "pair_from: the port is opened to that address only, not the browser's", h.lines)
            check(ans.get("pairing_window") == {"address": ELSEWHERE, "expires_in_s": 900, "browser": False}
                  and ELSEWHERE in ans["run"]["note"],
                  "pair_from: the answer names that address, not this browser", ans.get("pairing_window"))
            h.lines.clear()
            ans = await NR.setup_bastion(_request("10.88.0.1", [("x-forwarded-for", PUBLIC)],
                                                  {"pair_from": "::ffff:" + ELSEWHERE}))
            check([json.loads(x)["addr"] for x in h.lines] == [ELSEWHERE],
                  "pair_from: an IPv4-mapped address opens to its IPv4 form", h.lines)
            for bad in ("192.168.1.5", "127.0.0.1", "fd12:3456:789a::5", "198.51.100.20", "1.1.1.0/24",
                        "not an address", "1" * 70, 123, ["1.1.1.1"],
                        "2606:4700::1111%x}\nflush ruleset\n#"):
                h.lines.clear()
                minted.clear()
                err = await _raises(NR.setup_bastion(_request("10.88.0.1", [("x-forwarded-for", PUBLIC)],
                                                              {"pair_from": bad})), HTTPException)
                check(err is not None and err.status_code == 400 and not h.lines and not minted,
                      f"pair_from {bad!r}: 400, the helper is not asked, nothing minted", err and err.detail)
            owner_passkeys._step_ups.clear()
            h.lines.clear()
            minted.clear()
            err = await _raises(NR.setup_bastion(_request("10.88.0.1", [("x-forwarded-for", PUBLIC)],
                                                          {"pair_from": ELSEWHERE})), HTTPException)
            check(err is not None and err.status_code == 403 and not h.lines and not minted,
                  "pair_from without a fresh passkey check: 403, the port stays shut, nothing minted",
                  err and err.detail)
            owner_passkeys._note_step_up("tok-owner", owner_passkeys.PASSKEY)

            await replaces_checks(h, minted, NR, HTTPException)

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

            # Through remote access the address X-Forwarded-For names is the
            # relay's word (it wrote the tunnel's OPEN): refused, with a fresh
            # check, whatever address it names.
            from server import home_origin
            saved_remote = home_origin.REMOTE
            home_origin.REMOTE = home_origin.parse_remote(
                f"https://{home_origin.HOME.handle}.k7x2m9.remote.example.test", home_origin.HOME)
            try:
                h.lines.clear()
                minted.clear()
                owner_passkeys._note_step_up("tok-owner", owner_passkeys.PASSKEY)
                err = await _raises(NR.setup_bastion(_request(
                    "10.88.0.1", [("x-forwarded-for", PUBLIC)],
                    host=home_origin.REMOTE.host)), HTTPException)
                check(err is not None and err.status_code == 403 and "own network" in str(err.detail)
                      and not h.lines and not minted,
                      "on the remote address: 403, set it up from the machine's own network; the "
                      "port stays shut, nothing minted", (err and err.detail, h.lines, minted))
            finally:
                home_origin.REMOTE = saved_remote
            with Env(CSERIES_REMOTE_DOOR="10.88.0.1:8443"):
                err = await _raises(NR.setup_bastion(via_proxy_again()), HTTPException)
                check(err is not None and err.status_code == 403 and not h.lines and not minted,
                      "from the remote door's own address, whatever Host it names: 403, the port "
                      "stays shut", (err and err.detail, h.lines))

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
                  and ans["run"]["pair"].startswith("privatae-bastion pair --redeem-url unix://")
                  and "one_line" not in ans["run"] and "pairing_code" not in ans,
                  "a Host serving its sockets on this machine: CA and loopback redeem URL, the socket "
                  "pair command kept, and no one line", ans["run"])

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
            code = pairing_code_checks(ans)
            check(code is not None and decode_code(code)["u"] == "https://127.0.0.1:8810/v1/local/redeem",
                  "a Host in a VM: the one line pairs on 127.0.0.1 too", code)

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


async def replaces_checks(h, minted, NR, HTTPException):
    """`replaces` (the card's own token, asked from the card): its shape and
    that it is still live are checked before the helper is asked, so neither
    spends an opening; it is ended, in the mint's transaction, only after the
    window opened, so every refusal leaves the card's line working; and when
    the new window goes to another address, the old one is shut. The revoke
    and its transaction are test_host_owns_bastion.py's part 11, on the real
    SQL."""
    import base64
    import hashlib
    from server import brain_service, owner_passkeys
    from server.network import bastion_enroll, door_helper
    card = "skb_" + base64.urlsafe_b64encode(b"home.inventedCardSecret").decode().rstrip("=")
    card_hash = hashlib.sha256(b"inventedCardSecret").digest()
    revoked = []
    live = {"ok": True, "check": True}
    dead = set()  # secret hashes of tokens redeemed or ended since their mint
    count = [0]

    async def fake_revoke(conn, secret_hash):
        revoked.append(secret_hash)
        return live["ok"]

    async def fake_live(pool, secret_hash):
        return live["check"] and secret_hash not in dead

    async def shaped_mint(pool, *, entity_id, handle, name):
        count[0] += 1
        minted.append(name)
        token = "skb_" + base64.urlsafe_b64encode(f"home.inventedSecret{count[0]}".encode()).decode().rstrip("=")
        return {"token": token, "expires_in_s": 900, "name": name or "bastion",
                "run": {"install": "macOS / Linux:  curl -fsSL https://privatae.ai/bastion/install.sh | sh",
                        "pair": "privatae-bastion pair " + token, "note": "the note"}}

    class _Tx:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    class _Conn:
        def transaction(self):
            return _Tx()

    class _Acquire:
        async def __aenter__(self):
            return _Conn()

        async def __aexit__(self, *a):
            return False

    class _Pool:
        def acquire(self):
            return _Acquire()

    saved = (bastion_enroll.revoke_pairing_token, bastion_enroll.pairing_token_live,
             bastion_enroll.mint_pairing_token, brain_service.get_pool, door_helper.close_pairing)
    bastion_enroll.revoke_pairing_token = fake_revoke
    bastion_enroll.pairing_token_live = fake_live
    bastion_enroll.mint_pairing_token = shaped_mint
    brain_service.get_pool = lambda: _Pool()
    NR._WINDOW_OF.clear()
    via = [("x-forwarded-for", PUBLIC)]

    def clear():
        h.lines.clear()
        minted.clear()
        revoked.clear()

    async def refused(body):
        return await _raises(NR.setup_bastion(_request("10.88.0.1", via, body)), HTTPException)

    try:
        clear()
        ans = await NR.setup_bastion(_request("10.88.0.1", via, {"pair_from": ELSEWHERE, "replaces": card}))
        check([json.loads(x) for x in h.lines] == [{"op": "pairing.open", "addr": ELSEWHERE}]
              and revoked == [card_hash] and len(minted) == 1 and ans.get("replaced") is True,
              "from the card: one window, to that address; the card's token ended; one new token",
              (h.lines, len(revoked), len(minted), ans.get("replaced")))
        for bad in ("skb_!!", "not-a-token", 123, ["skb_x"]):
            clear()
            err = await refused({"pair_from": ELSEWHERE, "replaces": bad})
            check(err is not None and err.status_code == 400 and not h.lines and not revoked and not minted,
                  f"replaces {bad!r}: 400 before anything opens, nothing ended or minted", err and err.detail)
        for label, body in (("a mistyped address", {"pair_from": "192.168.1.5", "replaces": card}),
                            ("an address with a scope ID", {"pair_from": "2606:4700::1111%x", "replaces": card})):
            clear()
            err = await refused(body)
            check(err is not None and err.status_code == 400 and not revoked and not minted,
                  f"from the card, {label}: 400, the card's token is not ended", err and err.detail)
        owner_passkeys._step_ups.clear()
        clear()
        err = await refused({"pair_from": ELSEWHERE, "replaces": card})
        check(err is not None and err.status_code == 403 and not h.lines and not revoked and not minted,
              "from the card without a fresh passkey: 403, the card's token is not ended", err and err.detail)
        owner_passkeys._note_step_up("tok-owner", owner_passkeys.PASSKEY)
        clear()
        h.answer = {"ok": False, "error": "10 pairing windows were opened in the last hour"}
        err = await refused({"pair_from": ELSEWHERE, "replaces": card})
        check(err is not None and err.status_code == 502 and not revoked and not minted,
              "from the card when the helper refuses: 502, the card's token is not ended", err and err.detail)
        h.answer = {"ok": True, "expires_in": 900}

        # A token already used, expired or replaced (a second click): 409
        # before the helper is asked, so no opening is spent.
        live["check"] = False
        clear()
        err = await refused({"pair_from": ELSEWHERE, "replaces": card})
        check(err is not None and err.status_code == 409 and not h.lines and not revoked and not minted,
              "the card's token no longer live: 409, the helper is never asked, nothing ended or minted",
              (err and err.detail, h.lines))
        live["check"] = True
        # Redeemed in the moment between that check and the transaction: the
        # revoke finds nothing, and still nothing is minted.
        live["ok"] = False
        clear()
        err = await refused({"pair_from": ELSEWHERE, "replaces": card})
        check(err is not None and err.status_code == 409 and revoked == [card_hash] and not minted,
              "a token redeemed between the check and its end: 409, nothing minted", err and err.detail)
        live["ok"] = True

        # The first window: shut when the new one goes to another address.
        NR._WINDOW_OF.clear()
        clear()
        first = await NR.setup_bastion(_request("10.88.0.1", via))
        check([json.loads(x) for x in h.lines] == [{"op": "pairing.open", "addr": PUBLIC}],
              "the card's first window opens to this browser", h.lines)
        clear()
        second = await NR.setup_bastion(_request("10.88.0.1", via, {"pair_from": ELSEWHERE,
                                                                     "replaces": first["token"]}))
        check([json.loads(x) for x in h.lines] == [{"op": "pairing.open", "addr": ELSEWHERE},
                                                   {"op": "pairing.close", "addr": PUBLIC}],
              "replaced from the card to another address: the new window opens, then the first is shut",
              h.lines)
        clear()
        await NR.setup_bastion(_request("10.88.0.1", via, {"pair_from": ELSEWHERE, "replaces": second["token"]}))
        check([json.loads(x) for x in h.lines] == [{"op": "pairing.open", "addr": ELSEWHERE}],
              "replaced to the same address: nothing is shut (the open gave it a fresh window)", h.lines)
        clear()
        await NR.setup_bastion(_request("10.88.0.1", via, {"pair_from": ELSEWHERE, "replaces": card}))
        check([json.loads(x)["op"] for x in h.lines] == ["pairing.open"],
              "a token whose window this Host does not remember: nothing is shut", h.lines)

        # Two setups from this browser: both windows go to its address, and
        # the helper holds that address once. Replacing the second card's
        # token to another address must not shut the window the first line,
        # still unredeemed, needs; once that one is redeemed it is shut.
        NR._WINDOW_OF.clear()
        clear()
        other = await NR.setup_bastion(_request("10.88.0.1", via))
        card2 = await NR.setup_bastion(_request("10.88.0.1", via))
        clear()
        await NR.setup_bastion(_request("10.88.0.1", via, {"pair_from": ELSEWHERE, "replaces": card2["token"]}))
        check([json.loads(x) for x in h.lines] == [{"op": "pairing.open", "addr": ELSEWHERE}],
              "replaced to another address while another live line's window is at the old one: "
              "that window is left open", h.lines)
        NR._WINDOW_OF.clear()
        clear()
        other = await NR.setup_bastion(_request("10.88.0.1", via))
        card2 = await NR.setup_bastion(_request("10.88.0.1", via))
        dead.add(bastion_enroll.pairing_token_secret_hash(other["token"]))
        clear()
        await NR.setup_bastion(_request("10.88.0.1", via, {"pair_from": ELSEWHERE, "replaces": card2["token"]}))
        check([json.loads(x) for x in h.lines] == [{"op": "pairing.open", "addr": ELSEWHERE},
                                                   {"op": "pairing.close", "addr": PUBLIC}],
              "the other line at that address already redeemed: the old window is shut", h.lines)
        dead.clear()

        async def failing_live(pool, secret_hash):
            if secret_hash == bastion_enroll.pairing_token_secret_hash(other["token"]):
                raise OSError("the database did not answer")
            return True
        NR._WINDOW_OF.clear()
        clear()
        other = await NR.setup_bastion(_request("10.88.0.1", via))
        card2 = await NR.setup_bastion(_request("10.88.0.1", via))
        bastion_enroll.pairing_token_live = failing_live
        clear()
        ans = await _raises(NR.setup_bastion(_request("10.88.0.1", via, {"pair_from": ELSEWHERE,
                                                                        "replaces": card2["token"]})),
                            HTTPException)
        bastion_enroll.pairing_token_live = fake_live
        check(ans is None and [json.loads(x)["op"] for x in h.lines] == ["pairing.open"],
              "the other line's state unreadable: the setup still answers, and the old window is "
              "left to end by itself", (ans and ans.detail, h.lines))

        async def failing_close(addr, path=None, timeout=None):
            raise door_helper.HelperError("the pairing helper did not answer (TimeoutError)")
        door_helper.close_pairing = failing_close
        NR._WINDOW_OF.clear()
        clear()
        first = await NR.setup_bastion(_request("10.88.0.1", via))
        ans = await _raises(NR.setup_bastion(_request("10.88.0.1", via, {"pair_from": ELSEWHERE,
                                                                        "replaces": first["token"]})),
                            HTTPException)
        check(ans is None, "a close that fails is not the setup's failure: the window ends by itself", ans)
    finally:
        (bastion_enroll.revoke_pairing_token, bastion_enroll.pairing_token_live,
         bastion_enroll.mint_pairing_token, brain_service.get_pool, door_helper.close_pairing) = saved
        h.answer = {"ok": True, "expires_in": 900}
        NR._WINDOW_OF.clear()


async def bastions_list_says_window():
    """GET /bastions answers pairing_window, so the app offers another
    network's address before setup only where setup opens the port."""
    from server.network import nac_commit
    from server.routes import network as NR
    real_list, real_sa = nac_commit.bastions_list, LB.standalone

    async def fake_list(pool, eid):
        return {"bastions": []}

    nac_commit.bastions_list = fake_list
    try:
        LB.standalone = lambda: True
        with Env(CSERIES_DOOR_HELPER="/run/cseries-door/helper.sock"):
            on = await NR.get_bastions()
        with Env(CSERIES_DOOR_HELPER=None):
            off = await NR.get_bastions()
        LB.standalone = lambda: False
        with Env(CSERIES_DOOR_HELPER="/run/cseries-door/helper.sock"):
            platform = await NR.get_bastions()
        check(on == {"bastions": [], "pairing_window": True} and off.get("pairing_window") is False
              and platform.get("pairing_window") is False,
              "GET /bastions says whether setup opens a pairing window", (on, off, platform))
    finally:
        nac_commit.bastions_list, LB.standalone = real_list, real_sa


def decode_code(code):
    import base64
    body = code[len("skp_"):]
    return json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))


def pairing_code_checks(ans):
    """The pairing code and the one line in a setup answer, checked against
    what the server minted. Returns the code, or None."""
    import base64
    import hashlib
    import re
    code = ans.get("pairing_code")
    if not (isinstance(code, str) and code.startswith("skp_")):
        check(False, "the answer carries an skp_ pairing code", code)
        return None
    check(re.fullmatch(r"skp_[A-Za-z0-9_-]+", code) is not None and not code.endswith("="),
          "the code is base64url with no padding, so it needs no quoting in either shell", code)
    d = decode_code(code)
    der = base64.b64decode("".join(l for l in FAKE_CA.splitlines() if "-----" not in l))
    check(list(d) == ["v", "u", "ca", "t"] and d["v"] == 1 and d["u"] == ans["enroll"]
          and d["ca"] == hashlib.sha256(der).hexdigest() and d["t"] == ans["token"],
          "the code decodes to exactly v, u, ca, t: version 1, the redeem URL, the CA's "
          "SHA-256 over its DER and the token the server minted", d)
    raw = base64.urlsafe_b64decode(code[4:] + "=" * (-len(code[4:]) % 4)).decode()
    check(raw == json.dumps(d, separators=(",", ":")), "the code's JSON is compact", raw)
    one = ans["run"].get("one_line") or {}
    check(one.get("windows") == ("& ([scriptblock]::Create((irm https://privatae.ai/bastion/install.ps1))) "
                                 f"-Pair '{code}'")
          and one.get("unix") == f"curl -fsSL https://privatae.ai/bastion/install.sh | PRIVATAE_PAIR_CODE='{code}' sh",
          "the one line per kind of machine runs the installer with the code", one)
    unix_line_keeps_the_code_out_of_argv(one.get("unix") or "", code)
    check(ans["run"]["pair"].startswith("privatae-bastion pair --redeem-url https://")
          and "base64 -d > host-ca.pem" in ans["run"]["install"],
          "the step-by-step fields stay as they were", ans["run"])
    return code


def unix_line_keeps_the_code_out_of_argv(line: str, code: str) -> None:
    """The macOS / Linux line, run by a real sh with the download swapped for
    a stand-in installer that reports what it was given: the code reaches the
    installer in PRIVATAE_PAIR_CODE, and is not in the argv of the sh that
    runs it, which any local user can read with ps."""
    import shlex
    import subprocess
    fetch = "curl -fsSL https://privatae.ai/bastion/install.sh"
    if not line.startswith(fetch + " | "):
        check(False, "the macOS / Linux line pipes the installer from privatae.ai into sh", line)
        return
    stand_in = ('printf "env=%s\\n" "$PRIVATAE_PAIR_CODE"; '
                'printf "argv=%s\\n" "$(tr "\\000" " " < /proc/$$/cmdline)"')
    run = subprocess.run(["sh", "-c", "printf '%s\\n' " + shlex.quote(stand_in) + line[len(fetch):]],
                         capture_output=True, text=True, timeout=10, env={"PATH": os.environ.get("PATH", "")})
    said = dict(l.split("=", 1) for l in run.stdout.splitlines() if "=" in l)
    check(run.returncode == 0 and said.get("env") == code and "argv" in said and code not in said["argv"],
          "run by sh, the macOS / Linux line gives the installer the code in PRIVATAE_PAIR_CODE "
          "and keeps it out of sh's argv", (run.returncode, run.stdout[-300:], run.stderr[-300:]))


def via_proxy_again():
    return _request("10.88.0.1", [("x-forwarded-for", PUBLIC)])


async def renewal_keeps_the_chain():
    """The enroll port's chain carries the Host CA, and so must its renewal:
    the loop renews each certificate the way it was first made."""
    import datetime
    import inspect
    from server.network import host_ca
    seen = []

    async def fake_renew(pool, ctx, names, *, chain_ca=False):
        seen.append((ctx, chain_ca))
        return datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=30)
    saved = host_ca.renew_lane_cert
    host_ca.renew_lane_cert = fake_renew
    try:
        past = datetime.datetime.now(datetime.timezone.utc)
        await LB._renew_due(object(), [["enroll", past, True], ["lane", past, False]], ["n"], "test")
    finally:
        host_ca.renew_lane_cert = saved
    check(seen == [("enroll", True), ("lane", False)],
          "a due renewal chains the CA for the enroll port and not for the lane", seen)
    src = inspect.getsource(LB._serve_tcp_once)
    check("[[enroll_ctx, enroll_until, True], [lane_ctx, lane_until, False]]" in src,
          "the ports' loop marks the enroll certificate as the one whose chain carries the CA")


async def async_main():
    with tempfile.TemporaryDirectory(prefix="pair") as d:
        await helper_protocol(d)
        await setup_route(d)
    await renewal_keeps_the_chain()
    await bastions_list_says_window()


def main():
    lane_host()
    asyncio.run(async_main())


main()
sys.exit(failed)
