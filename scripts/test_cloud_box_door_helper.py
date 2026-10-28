#!/usr/bin/env python3
"""The cloud box's pairing-window helper (vm/cloud/overlay/usr/local/sbin/
cseries-door-helper), its whole protocol, with nft and the clock stood in.

  1. Only uid 1000, the Host's user, is answered; the uid comes from the
     socket itself (SO_PEERCRED), checked here over a real socket pair.
  2. pairing.open takes one global unicast address and nothing else: private,
     loopback, link-local, multicast, documentation and malformed addresses
     are refused; an IPv4-mapped IPv6 address counts as its IPv4 address; the
     test networks a test machine sets are the only exception.
  3. The nft transaction is exactly an add, delete, add of that one address
     in the matching set, with the window's timeout, so it always ends with a
     fresh window and never touches anything else.
  4. At most 10 opens an hour, kept across a restart of the helper; a refused
     open and a failed nft do not count.
  5. pairing.status reads the sets from nft's JSON; any other op, extra
     fields, a non-object or an oversize line are refused.
  6. machine.restart restarts the machine for the Host, at most once every
     10 minutes; it takes op alone, and anyone but the Host is refused.

Container-free: python3 scripts/test_cloud_box_door_helper.py
"""
import importlib.util
import ipaddress
import json
import os
import socket
import sys
import tempfile
from importlib.machinery import SourceFileLoader

ROOT = os.environ.get("APP_ROOT") or os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
PATH = os.path.join(ROOT, "vm/cloud/overlay/usr/local/sbin/cseries-door-helper")

loader = SourceFileLoader("door_helper", PATH)
spec = importlib.util.spec_from_loader("door_helper", loader)
h = importlib.util.module_from_spec(spec)
loader.exec_module(h)

failed = 0


def check(cond, what, detail=""):
    global failed
    if cond:
        print(f"OK    {what}")
    else:
        failed = 1
        print(f"FAIL  {what} {detail}")


class Nft:
    def __init__(self, fail=False, listing=None):
        self.scripts = []
        self.fail = fail
        self.listing = listing or {}

    def __call__(self, script=None, args=None):
        if args:
            name = args[-1]
            return json.dumps({"nftables": [{"metainfo": {}}, {"set": {
                "family": "inet", "name": name, "table": "cseries_door",
                **({"elem": self.listing[name]} if name in self.listing else {})}}]})
        if self.fail:
            raise h.Refused("the firewall refused the change: no such table")
        self.scripts.append(script)
        return ""


class Clock:
    def __init__(self, t=1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


os.environ.pop(h.TEST_NETS_ENV, None)
os.environ.pop(h.TEST_WINDOW_ENV, None)

with tempfile.TemporaryDirectory() as tmp:
    state = os.path.join(tmp, "opens.json")
    clock = Clock()
    lim = h.Limiter(state, now=clock)

    def ask(req, uid=1000, nft=None, extra=()):
        line = req if isinstance(req, str) else json.dumps(req)
        return h.handle(line, uid, lim, runner=nft or Nft(), extra=list(extra))

    # 1. the peer
    for uid in (0, 999, 1001, 65534):
        r = ask({"op": "pairing.status"}, uid=uid)
        check(r == {"ok": False, "error": "only the Host may ask for a pairing window"},
              f"uid {uid} is refused", r)
    a, b = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    check(h.peer_uid(b) == os.getuid(), "SO_PEERCRED gives the connecting process's uid")
    a.close()
    b.close()

    # 2. addresses
    for addr in ["10.0.0.1", "172.16.5.4", "192.168.1.1", "127.0.0.1", "100.64.0.1", "169.254.169.254",
                 "0.0.0.0", "224.0.0.1", "255.255.255.255", "192.0.2.1", "198.51.100.7", "::1", "::",
                 "fe80::1", "fc00::1", "fd12:3456::1", "ff02::1", "2001:db8::1", "::ffff:10.0.0.1",
                 "not-an-ip", "203.0.113.5/32", "", "1" * 70]:
        r = ask({"op": "pairing.open", "addr": addr})
        check(r["ok"] is False, f"pairing.open refuses {addr[:24]!r}", r)
    for bad in [None, 5, ["203.0.113.5"], {"addr": "8.8.8.8"}]:
        check(ask({"op": "pairing.open", "addr": bad})["ok"] is False, f"pairing.open refuses addr={bad!r}")
    # Python's ipaddress takes any text after "%" as an IPv6 scope ID and still
    # calls the address global, and str() gives that text back, which
    # open_script would write into the nft script the helper runs as root.
    for scoped in ["2606:4700::1111%eth0", "2606:4700::1111%x}\nflush ruleset\n#", "::ffff:8.8.8.8%x"]:
        n = Nft()
        r = ask({"op": "pairing.open", "addr": scoped}, nft=n)
        check(r["ok"] is False and not n.scripts,
              f"pairing.open refuses a scope ID and runs no nft: {scoped[:24]!r}", (r, n.scripts))
    check(h.outside_address("8.8.8.8") == ipaddress.ip_address("8.8.8.8"), "a global IPv4 address is accepted")
    check(h.outside_address("2606:4700::1111") == ipaddress.ip_address("2606:4700::1111"),
          "a global IPv6 address is accepted")
    check(h.outside_address("::ffff:8.8.4.4") == ipaddress.ip_address("8.8.4.4"),
          "an IPv4-mapped address is taken as its IPv4 address (the Host listens dual-stack)")
    nets = h.test_nets("192.0.2.0/30, 2001:db8:c5::/64")
    check(h.outside_address("192.0.2.1", nets) == ipaddress.ip_address("192.0.2.1") and
          h.outside_address("2001:db8:c5::1", nets) == ipaddress.ip_address("2001:db8:c5::1"),
          "a test machine's documentation networks are accepted when set")
    check(ask({"op": "pairing.open", "addr": "192.0.2.9"}, extra=nets)["ok"] is False,
          "an address outside the test networks is still refused")
    check(h.test_nets("") == [], "no test networks unless set")

    # 3. the nft transaction
    nft = Nft()
    r = ask({"op": "pairing.open", "addr": "8.8.8.8"}, nft=nft)
    check(r == {"ok": True, "expires_in": 900}, "pairing.open answers the window", r)
    check(nft.scripts == ["add element inet cseries_door pairing_v4 { 8.8.8.8 }\n"
                          "delete element inet cseries_door pairing_v4 { 8.8.8.8 }\n"
                          "add element inet cseries_door pairing_v4 { 8.8.8.8 timeout 900s }\n"],
          "IPv4: add, delete, add with a 15-minute timeout in pairing_v4", nft.scripts)
    nft = Nft()
    ask({"op": "pairing.open", "addr": "2606:4700::1111"}, nft=nft)
    check(len(nft.scripts) == 1 and nft.scripts[0].count("pairing_v6 { 2606:4700::1111") == 3
          and "pairing_v4" not in nft.scripts[0], "IPv6 goes to pairing_v6 only", nft.scripts)
    check(h.window_s("30") == 30 and h.window_s("5") == 10 and h.window_s("9999") == 900 and
          h.window_s("") == 900 and h.window_s("x") == 900,
          "a test window is bounded to 10..900 seconds")

    # 4. the limit
    os.unlink(state)
    for i in range(8):
        ask({"op": "pairing.open", "addr": "8.8.8.8"})
    check(ask({"op": "pairing.open", "addr": "10.0.0.1"})["ok"] is False and
          ask({"op": "pairing.open", "addr": "8.8.8.8"}, nft=Nft(fail=True))["ok"] is False,
          "a refused address and a failed nft are refusals")
    r9 = ask({"op": "pairing.open", "addr": "8.8.8.8"})
    r10 = ask({"op": "pairing.open", "addr": "8.8.4.4"})
    check(r9["ok"] and r10["ok"], "they did not count: the 9th and 10th opens go through", (r9, r10))
    r11 = ask({"op": "pairing.open", "addr": "8.8.8.8"})
    check(r11["ok"] is False and "10 pairing windows" in r11["error"], "the 11th open in an hour is refused", r11)
    lim2 = h.Limiter(state, now=clock)
    check(h.handle(json.dumps({"op": "pairing.open", "addr": "8.8.8.8"}), 1000, lim2, runner=Nft(),
                   extra=[])["ok"] is False, "the count survives a restart of the helper")
    clock.t += 3601
    check(ask({"op": "pairing.open", "addr": "8.8.8.8"})["ok"], "an hour later an open goes through again")

    # 4b. closing a window (the Host replaced the token it was for)
    nft = Nft()
    r = ask({"op": "pairing.close", "addr": "8.8.8.8"}, nft=nft)
    check(r == {"ok": True} and nft.scripts == ["add element inet cseries_door pairing_v4 { 8.8.8.8 }\n"
                                                "delete element inet cseries_door pairing_v4 { 8.8.8.8 }\n"],
          "pairing.close removes that one address from pairing_v4 and touches nothing else", (r, nft.scripts))
    nft = Nft()
    ask({"op": "pairing.close", "addr": "2606:4700::1111"}, nft=nft)
    check(len(nft.scripts) == 1 and nft.scripts[0].count("pairing_v6 { 2606:4700::1111 }") == 2
          and "pairing_v4" not in nft.scripts[0] and "timeout" not in nft.scripts[0],
          "an IPv6 close removes it from pairing_v6 only, and adds no window", nft.scripts)
    check(ask({"op": "pairing.close", "addr": "8.8.8.8"}, nft=Nft(fail=True))["ok"] is False,
          "a failed nft on a close is a refusal")
    for i in range(9):
        ask({"op": "pairing.open", "addr": "8.8.8.8"})
    check(ask({"op": "pairing.open", "addr": "8.8.4.4"})["ok"] is False, "the cap is full again: 10 opens this hour")
    with open(state, encoding="utf-8") as f:
        stamps_before = f.read()
    for i in range(12):
        ask({"op": "pairing.close", "addr": "8.8.8.8"})
    with open(state, encoding="utf-8") as f:
        stamps_after = f.read()
    check(stamps_after == stamps_before, "closes leave the count of opens as it was")
    check(ask({"op": "pairing.open", "addr": "8.8.4.4"})["ok"] is False,
          "closing windows gives no opening back: the next open is still refused")
    for bad in ["2606:4700::1111%eth0", "2606:4700::1111%x}\nflush ruleset\n#", "::ffff:8.8.8.8%x",
                "10.0.0.1", "127.0.0.1", "fe80::1", "192.0.2.1", "203.0.113.5/32", "not-an-ip", "", None, 5,
                ["8.8.8.8"], "1" * 70]:
        n = Nft()
        r = ask({"op": "pairing.close", "addr": bad}, nft=n)
        check(r["ok"] is False and not n.scripts, f"pairing.close refuses {str(bad)[:24]!r} and runs no nft", r)
    n = Nft()
    check(ask({"op": "pairing.close", "addr": "8.8.8.8", "x": 1}, nft=n)["ok"] is False and not n.scripts,
          "pairing.close takes op and addr only")
    for uid in (0, 999, 1001):
        n = Nft()
        check(ask({"op": "pairing.close", "addr": "8.8.8.8"}, uid=uid, nft=n)["ok"] is False and not n.scripts,
              f"pairing.close from uid {uid} is refused")

    # 5. status and everything else
    listing = {"pairing_v4": [{"elem": {"val": "8.8.8.8", "timeout": 900, "expires": 512}}],
               "pairing_v6": [{"elem": {"val": "2606:4700::1111", "timeout": 900, "expires": 7}}]}
    r = ask({"op": "pairing.status"}, nft=Nft(listing=listing))
    check(r == {"ok": True, "open": [{"addr": "8.8.8.8", "expires_in": 512},
                                     {"addr": "2606:4700::1111", "expires_in": 7}]},
          "pairing.status lists both sets with their time left", r)
    check(ask({"op": "pairing.status"})["open"] == [], "pairing.status with no window open is an empty list")
    for req in [{"op": "pairing.flush"}, {"op": "exec", "cmd": "id"}, {},
                {"op": "pairing.status", "x": 1}, {"op": "pairing.open", "addr": "8.8.8.8", "timeout": 99999},
                "[]", "not json", "null"]:
        r = ask(req)
        check(r["ok"] is False, f"refused: {str(req)[:50]}", r)

    # The socket itself: one line in, one line out.
    a, b = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    a.sendall(b"x" * (h.MAX_LINE + 10))
    try:
        h.read_line(b)
        long_refused = False
    except h.Refused:
        long_refused = True
    check(long_refused, "a request line longer than 4 KiB is refused")
    a.close()
    b.close()

# 6. the owner's restart
with tempfile.TemporaryDirectory() as tmp:
    h.RESTART_STATE = os.path.join(tmp, "restart.json")
    lim = h.Limiter(os.path.join(tmp, "opens.json"))
    asked = []

    def restarter():
        asked.append(1)

    r = h.handle(json.dumps({"op": "machine.restart"}), 1001, lim, runner=None, restarter=restarter)
    check(r["ok"] is False and not asked, "a restart from anyone but the Host is refused", r)
    r = h.handle(json.dumps({"op": "machine.restart", "now": True}), 1000, lim, runner=None,
                 restarter=restarter)
    check(r["ok"] is False and not asked, "machine.restart takes op alone", r)
    r = h.handle(json.dumps({"op": "machine.restart"}), 1000, lim, runner=None, restarter=restarter)
    check(r == {"ok": True, "in_seconds": h.RESTART_IN_S} and asked == [1], "the Host's restart is asked for", r)
    r = h.handle(json.dumps({"op": "machine.restart"}), 1000, lim, runner=None, restarter=restarter)
    check(r["ok"] is False and asked == [1], "a second within 10 minutes is refused", r)
    with open(h.RESTART_STATE, "w") as f:
        json.dump({"at": 0}, f)
    r = h.handle(json.dumps({"op": "machine.restart"}), 1000, lim, runner=None, restarter=restarter)
    check(r["ok"] is True and asked == [1, 1], "after 10 minutes one is taken again", r)

sys.exit(failed)
