#!/usr/bin/env python3
"""Remote access: a browser on the internet reaches the Host only through the
machine's door, from the door's own address, never from loopback.

THE HAZARD. The Host lets a request from loopback in without a session
(server/middleware/owner_session.py, OwnerSessionGate: `client in
LOOPBACK_HOSTS`). Remote access carries browsers from anywhere to the Host,
so if anything on the machine ever handed one to the Host from loopback, that
browser would be the machine's owner. This gate proves it cannot happen:

  1. The tunnel's door is never loopback: remote_access.door_address refuses
     127.0.0.1, 127.0.0.2, ::1, ::ffff:127.0.0.1, 0.0.0.0, ::, link-local and
     any name, and takes the door's container address.
  2. The shipped wiring (compose.remote.yaml, sidecars/remote-door/Caddyfile):
     the door publishes no port and sits on an internal network with the Host
     alone; the tunnel dials the door's address; the door asks and passes to
     the Host's address on that network; the Host trusts exactly the door's
     /32; the door's question and header rules are the cloud box's own.
  3. The Host refuses the loopback lane for every name on the remote address,
     even one listed in CSERIES_ALLOWED_ORIGINS, through its real gates, and
     the local address's loopback lane is unchanged.
  4. End to end, in a private network namespace with addresses of its own
     (no loopback anywhere on the path): a browser connects to the reference
     relay, the relay passes the TLS down the Host's tunnel, the tunnel hands
     it to the REAL Caddy running sidecars/remote-door/Caddyfile, and Caddy
     asks and passes to the Host's real gates and routes. Checked there:
       - without a session a page is sent to /login and anything else is 401;
       - the password is refused (403) and never reaches its route, and
         /setup is refused;
       - a phone joins with a one-time code (the code works once), makes a
         passkey for the remote address and gets a __Host- session; a passkey
         sign-in works too, and only then does a protected route answer;
       - a request whose Host header names localhost or the desktop's own
         address is refused by the door and never reaches the Host;
       - a PROXY header from anywhere but the Host is refused;
       - EVERY request the Host received came from the door's address (none
         from loopback), with the browser's own address in X-Forwarded-For.

Needs `unshare` (user and network namespaces), `ip` and `caddy` (2.11.2 is
what the door image pins); the gate re-runs itself inside the namespace.

    python3 scripts/test_remote_door_loopback.py       (from the repo root)
"""
import os
import shutil
import subprocess
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
_MARK = "CSERIES_REMOTE_DOOR_NETNS"

HOST, DOOR, RELAY, BROWSER, STRANGER = "10.77.0.2", "10.77.0.3", "10.77.0.10", "10.77.0.20", "10.77.0.30"

if os.environ.get(_MARK) != "1":
    missing = [t for t in ("unshare", "ip", "caddy") if not shutil.which(t)]
    if missing:
        print(f"FAIL  this gate needs {', '.join(missing)} on PATH")
        sys.exit(1)
    r = subprocess.run(["unshare", "-rn", sys.executable, os.path.abspath(__file__)],
                       env=dict(os.environ, **{_MARK: "1"}), check=False)
    sys.exit(r.returncode)


def _net():
    """lo, and one interface holding every party's address; each local route
    names the address its caller dials from, so the door, the Host and the
    browser each arrive from their own address."""
    cmds = [["ip", "link", "set", "lo", "up"], ["ip", "link", "add", "d0", "type", "dummy"],
            ["ip", "link", "set", "d0", "up"]]
    cmds += [["ip", "addr", "add", f"{a}/32", "dev", "d0"] for a in (HOST, DOOR, RELAY, BROWSER, STRANGER)]
    for dst, src in ((HOST, DOOR), (DOOR, HOST), (RELAY, BROWSER)):
        cmds.append(["ip", "route", "change", "local", dst, "dev", "d0", "table", "local",
                     "proto", "kernel", "scope", "host", "src", src])
    for c in cmds:
        subprocess.run(c, check=True)


_net()

import asyncio  # noqa: E402
import base64  # noqa: E402
import datetime as dt  # noqa: E402
import hashlib  # noqa: E402
import ipaddress  # noqa: E402
import json  # noqa: E402
import random  # noqa: E402
import re  # noqa: E402
import socket  # noqa: E402
import ssl  # noqa: E402
import struct  # noqa: E402
import tempfile  # noqa: E402
import time  # noqa: E402
from datetime import datetime, timezone  # noqa: E402

TMP = tempfile.mkdtemp(prefix="remote-door-")
KEY = "door-key-for-the-test-0123456789abcdefABCDEF"
with open(os.path.join(TMP, "door-secret"), "w") as _f:
    _f.write(KEY + "\n")
ZONE = "remote.privatae.ai"
AID = "k7x2m9"
NAME = f"home.{AID}.{ZONE}"
REMOTE_ORIGIN = f"https://{NAME}"
os.environ.update({
    "CSERIES_URL": "http://home.cseries.localhost:8800",
    "CSERIES_REMOTE_URL": REMOTE_ORIGIN,
    "CSERIES_TRUSTED_PROXIES": f"{DOOR}/32",
    "CSERIES_DOOR_SECRET_FILE": os.path.join(TMP, "door-secret"),
    # Listed here on purpose: the loopback lane must still refuse it.
    "CSERIES_ALLOWED_ORIGINS": NAME,
})
os.environ.pop("CSERIES_RUNTIME_NET", None)
sys.path.insert(0, ROOT)

import cbor2  # noqa: E402
import uvicorn  # noqa: E402
import yaml  # noqa: E402
from cryptography import x509  # noqa: E402
from cryptography.hazmat.primitives import hashes, serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import ec  # noqa: E402
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey  # noqa: E402
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID  # noqa: E402
from fastapi import FastAPI  # noqa: E402

from server import home_origin, owner_login as L, owner_passkeys as P, remote_join as J  # noqa: E402
sys.path.insert(0, os.path.join(ROOT, "scripts"))
from gate_loopback import IDLE_KEEP_ALIVE_S  # noqa: E402
from server.middleware import localhost_only as G  # noqa: E402
from server.middleware.app_origin_guard import AppOriginGuard  # noqa: E402
from server.middleware.host_header import HostHeaderGuard  # noqa: E402
from server.middleware.owner_session import OwnerSessionGate  # noqa: E402
from server.network import remote_access as RA  # noqa: E402
from server.network import remote_protocol as rp  # noqa: E402
from server.network import remote_relay as relay_mod  # noqa: E402
from server.network import remote_tunnel as tun  # noqa: E402
from server.routes import owner as R  # noqa: E402
from server.routes import remote_access as RR  # noqa: E402

failed = 0


def check(cond, what, detail=""):
    global failed
    print(f"{'OK  ' if cond else 'FAIL'}  {what}" + ("" if cond or not detail else f"  ({detail})"))
    if not cond:
        failed = 1


# ── 1. the door is never loopback ───────────────────────────────────────────

for bad in ("127.0.0.1", "127.0.0.2", "::1", "::ffff:127.0.0.1", "0.0.0.0", "::", "169.254.0.5",
            "fe80::1", "localhost", "door", ""):
    try:
        RA.check_door(bad, 8443)
        ok = False
    except RA.DoorAddressError:
        ok = True
    check(ok, f"the tunnel refuses a door at {bad or '(empty)'}")
check(RA.check_door("10.89.79.3", 8443) == ("10.89.79.3", 8443), "a container address is a door")
check(RA.door_address({"CSERIES_REMOTE_DOOR": "[fd00:77::3]:8443"}) == ("fd00:77::3", 8443),
      "an IPv6 door is written [address]:port")
for raw in ("127.0.0.1:8443", "[::1]:8443", "localhost:8443"):
    try:
        RA.door_address({"CSERIES_REMOTE_DOOR": raw})
        ok = False
    except RA.DoorAddressError:
        ok = True
    check(ok, f"CSERIES_REMOTE_DOOR={raw} is refused")


# ── 2. the shipped wiring ───────────────────────────────────────────────────

def _default(value):
    """`${VAR:-default}` as compose reads it with VAR unset."""
    return re.sub(r"\$\{[A-Z_]+:-([^}]*)\}", r"\1", str(value))


with open(os.path.join(ROOT, "compose.remote.yaml")) as f:
    compose = yaml.safe_load(f)
svc = compose["services"]
door_svc, host_svc, cert_svc = svc["remote-door"], svc["host"], svc["remote-cert"]
door_ip = _default(door_svc["networks"]["remote"]["ipv4_address"])
host_ip = _default(host_svc["networks"]["remote"]["ipv4_address"])
env_h, env_d = host_svc["environment"], door_svc["environment"]
check("ports" not in door_svc and "ports" not in cert_svc, "the door and its certificate job publish no port")
check(list(door_svc["networks"]) == ["remote"], "the door is on the remote network only")
check(compose["networks"]["remote"].get("internal") is True, "the remote network is internal (no way out)")
check("remote" not in (cert_svc.get("networks") or []), "the certificate job is not on the remote network")
check(_default(env_h["CSERIES_REMOTE_DOOR"]) == f"{door_ip}:8443", "the tunnel dials the door's own address",
      env_h["CSERIES_REMOTE_DOOR"])
check(_default(env_h["CSERIES_TRUSTED_PROXIES"]) == f"{door_ip}/32", "the Host trusts exactly the door's /32")
check(_default(env_d["CSERIES_HOST_UPSTREAM"]) == f"{host_ip}:8800", "the door passes to the Host's own address")
check(_default(env_d["CSERIES_TUNNEL_FROM"]) == host_ip and _default(env_d["CSERIES_DOOR_BIND"]) == door_ip,
      "the door believes a PROXY header from the Host's address only, and listens on its own")
for ip in (door_ip, host_ip):
    a = ipaddress.ip_address(ip)
    check(not (a.is_loopback or a.is_unspecified) and ipaddress.ip_network(
        _default(compose["networks"]["remote"]["ipam"]["config"][0]["subnet"])).supernet_of(
        ipaddress.ip_network(f"{ip}/32")), f"{ip} is a non-loopback address in the remote subnet")
check(RA.door_address({"CSERIES_REMOTE_DOOR": _default(env_h["CSERIES_REMOTE_DOOR"])}) == (door_ip, 8443),
      "the shipped door address passes the tunnel's own check")

with open(os.path.join(ROOT, "sidecars/remote-door/Caddyfile")) as f:
    door_cf = f.read()
with open(os.path.join(ROOT, "vm/cloud/overlay/etc/caddy/Caddyfile.in")) as f:
    cloud_cf = f.read()
code = "\n".join(ln.strip() for ln in door_cf.splitlines() if ln.strip() and not ln.strip().startswith("#"))
check("forward_auth {$CSERIES_HOST_UPSTREAM} {" in code and "reverse_proxy {$CSERIES_HOST_UPSTREAM} {" in code,
      "the door asks and passes to the Host's address, from the environment")
check(not re.search(r"127\.|localhost|\[::1\]|::1", code), "the door's Caddyfile names no loopback address")
check("allow {$CSERIES_TUNNEL_FROM}/32" in code and "fallback_policy reject" in code,
      "a PROXY header is believed from the Host's address only")
for line in ("@door path /__host/door /__host/door/*", "respond 404", "uri /__host/door",
             "header_up X-Cseries-Door {$CSERIES_DOOR_SECRET}", "header_up -X-Forwarded-Method",
             "header_up -X-Forwarded-Uri", "header_up -X-Cseries-Door", "protocols tls1.3", "protocols h1 h2"):
    check(line in code and line in cloud_cf, f"the door keeps the cloud box's rule: {line}")


# The released install's wiring: the guest's quadlets (WSL and Lima share
# vm/wsl/rootfs) hold the same rules as compose.remote.yaml, with the same
# numbers. A unit is read as Quadlet reads it: the unit, then its drop-ins in
# name order, and the last value given for an environment variable wins.

UNITS = os.path.join(ROOT, "vm/wsl/rootfs/etc/containers/systemd")


def _quadlet(name, extra=()):
    """{section: [(key, value), ...]} for a unit and its .d/*.conf drop-ins,
    with `extra` drop-ins (ones cseries-guest configure writes) among them in
    name order."""
    out = {}
    d = os.path.join(UNITS, name) + ".d"
    drops = [os.path.join(d, f) for f in os.listdir(d) if f.endswith(".conf")] if os.path.isdir(d) else []
    paths = [os.path.join(UNITS, name)] + sorted([*drops, *extra], key=os.path.basename)
    for path in paths:
        section = None
        with open(path) as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith(("#", ";")):
                    continue
                if line.startswith("[") and line.endswith("]"):
                    section = out.setdefault(line[1:-1], [])
                    continue
                k, _, v = line.partition("=")
                section.append((k.strip(), v.strip()))
    return out


def _all(unit, section, key):
    return [v for k, v in unit.get(section, []) if k == key]


def _env(unit):
    env = {}
    for v in _all(unit, "Container", "Environment"):
        for word in v.split():
            k, _, val = word.partition("=")
            env[k] = val
    return env


def _volumes(unit):
    """{volume name: (path, options)} for the named volumes a unit mounts."""
    out = {}
    for v in _all(unit, "Container", "Volume"):
        name, path, *opts = v.split(":")
        out[name] = (path, opts[0] if opts else "")
    return out


# cseries-guest configure takes the door's image for both of its units, and
# start never makes the Host wait on (or fail with) the door.
_g_tmp = tempfile.mkdtemp(prefix="remote-door-guest-")
_g_bin = os.path.join(_g_tmp, "bin")
os.makedirs(_g_bin)
for _n, _b in (("systemctl", '#!/bin/sh\necho "systemctl $*" >> "$FAKE_LOG"\n'
                             'case "$1" in is-active) echo active; exit 0 ;; esac\n'
                             'case "$*" in *cseries-remote-door*) exit 1 ;; esac\nexit 0\n'),
               ("podman", '#!/bin/sh\necho "podman $*" >> "$FAKE_LOG"\n[ "$1 $2" = "image exists" ] && exit 1\nexit 0\n')):
    with open(os.path.join(_g_bin, _n), "w") as _f:
        _f.write(_b)
    os.chmod(os.path.join(_g_bin, _n), 0o755)


def _guest(root, args, stdin=""):
    env = dict(os.environ, CSERIES_GUEST_ROOT=root, FAKE_LOG=os.path.join(root, "calls.log"),
               PATH=_g_bin + os.pathsep + os.environ.get("PATH", ""))
    os.makedirs(root, exist_ok=True)
    return subprocess.run(["sh", os.path.join(ROOT, "vm/wsl/rootfs/usr/local/bin/cseries-guest"), *args],
                          input=stdin, env=env, capture_output=True, text=True, check=False)


_D = "@sha256:" + "a" * 64
_keys = f"host=ghcr.io/privatae/cseries{_D}\npostgres=ghcr.io/privatae/cseries-postgres{_D}\nrelease=1.2.3\n"
_door = "ghcr.io/privatae/cseries-remote-door@sha256:" + "9" * 64

# The Host joins the remote network only through the drop-in configure writes
# once it has the door's image: the guest's own units for the Host name no
# remote network, so a Host without the door never waits on that network, or
# fails when it cannot be made.
_g_cfg = os.path.join(_g_tmp, "g0")
_r = _guest(_g_cfg, ["configure"], _keys + f"door={_door}\n")
_host_remote = os.path.join(_g_cfg, "etc/containers/systemd/cseries-host.container.d/remote.conf")
check(_r.returncode == 0 and os.path.isfile(_host_remote),
      "configure, given the door's image, writes the Host's remote-access drop-in", _r.stderr)
check(not any("cseries-remote" in v for kv in _quadlet("cseries-host.container").values() for _k, v in kv),
      "the guest's own units for the Host name no remote network: only configure's drop-in does")
q_net = _quadlet("cseries-remote.network")
q_host = _quadlet("cseries-host.container", extra=[_host_remote] if os.path.isfile(_host_remote) else [])
q_door, q_cert = _quadlet("cseries-remote-door.container"), _quadlet("cseries-remote-cert.container")
q_subnet = (_all(q_net, "Network", "Subnet") or [""])[-1]
q_door_ip = next((n.split(":ip=", 1)[1] for n in _all(q_door, "Container", "Network")
                  if n.startswith("cseries-remote.network:ip=")), "")
q_host_ip = next((n.split(":ip=", 1)[1] for n in _all(q_host, "Container", "Network")
                  if n.startswith("cseries-remote.network:ip=")), "")
q_env_h, q_env_d = _env(q_host), _env(q_door)
check(_all(q_net, "Network", "Internal") == ["true"] and _all(q_net, "Network", "DisableDNS") == ["true"],
      "the guest's remote network is internal (no way out) and has no resolver")
check((q_subnet, q_door_ip, q_host_ip) == (
          _default(compose["networks"]["remote"]["ipam"]["config"][0]["subnet"]), door_ip, host_ip),
      "the guest's remote network and both addresses are compose.remote.yaml's", (q_subnet, q_door_ip, q_host_ip))
check(_all(q_door, "Container", "Network") == [f"cseries-remote.network:ip={q_door_ip}"],
      "the guest's door is on the remote network only", _all(q_door, "Container", "Network"))
check(not _all(q_door, "Container", "PublishPort") and not _all(q_cert, "Container", "PublishPort"),
      "the guest's door and certificate job publish no port")
check(_all(q_cert, "Container", "Network") == ["cseries.network"],
      "the guest's certificate job is on the Host's ordinary network, never the remote one",
      _all(q_cert, "Container", "Network"))
check(f"cseries-remote.network:ip={q_host_ip}" in _all(q_host, "Container", "Network"),
      "the guest's Host joins the remote network at its own address")
check(q_env_h.get("CSERIES_REMOTE_DOOR") == f"{q_door_ip}:8443" and
      RA.door_address({"CSERIES_REMOTE_DOOR": q_env_h.get("CSERIES_REMOTE_DOOR", "")}) == (q_door_ip, 8443),
      "the guest's tunnel dials the door's own address, and it passes the tunnel's own check",
      q_env_h.get("CSERIES_REMOTE_DOOR"))
check(q_env_h.get("CSERIES_TRUSTED_PROXIES") == f"{q_door_ip}/32",
      "the guest's Host trusts exactly the door's /32 (the drop-in replaces the unit's empty value)",
      q_env_h.get("CSERIES_TRUSTED_PROXIES"))
check(q_env_d.get("CSERIES_HOST_UPSTREAM") == f"{q_host_ip}:8800" and q_env_d.get("CSERIES_TUNNEL_FROM") == q_host_ip
      and q_env_d.get("CSERIES_DOOR_BIND") == q_door_ip and q_env_d.get("CSERIES_DOOR_PORT") == "8443",
      "the guest's door listens on its own address, believes PROXY from the Host's only, and passes to the Host's",
      q_env_d)
for ip in (q_door_ip, q_host_ip):
    try:
        a = ipaddress.ip_address(ip)
        inside = ipaddress.ip_network(q_subnet).supernet_of(ipaddress.ip_network(f"{ip}/32"))
    except ValueError:
        a, inside = None, False
    check(a is not None and not (a.is_loopback or a.is_unspecified) and inside,
          f"the guest's {ip or '(missing)'} is a non-loopback address in the remote subnet")
# Where the remote subnet sits: away from what WSL, Docker, podman and home
# networks draw from, and never handed by podman here to another network.
try:
    _rs = ipaddress.ip_network(q_subnet)
except ValueError:
    _rs = None
for _net, _who in (("172.16.0.0/12", "WSL's NAT range and Docker's default networks"),
                   ("192.168.0.0/16", "home networks and Docker's later pools"),
                   ("10.88.0.0/16", "podman's own default network")):
    check(_rs is not None and not _rs.overlaps(ipaddress.ip_network(_net)),
          f"the remote subnet {q_subnet} is outside {_net} ({_who})")
with open(os.path.join(ROOT, "vm/runtime/containers/cseries-apps.network")) as _f:
    _rt = ipaddress.ip_network(next(ln.split("=", 1)[1].strip() for ln in _f if ln.startswith("Subnet=")))
check(_rs is not None and not _rs.overlaps(_rt), f"the remote subnet is not the local runtime's {_rt}")
import tomllib  # noqa: E402
with open(os.path.join(ROOT, "vm/wsl/rootfs/etc/containers/containers.conf.d/subnets.conf"), "rb") as _f:
    _pools = tomllib.load(_f)["network"]["default_subnet_pools"]
_pnets = [ipaddress.ip_network(p["base"]) for p in _pools]
# podman's own default pools (containers.conf(5), default_subnet_pools).
_podman = [ipaddress.ip_network(n) for n in
           ("10.89.0.0/16", "10.90.0.0/15", "10.92.0.0/14", "10.96.0.0/11", "10.128.0.0/9")]
check(all(p.get("size") == 24 for p in _pools) and _pnets and _pnets[0].network_address == _podman[0].network_address,
      "the guest's subnet pools hand out /24s, the first still 10.89.0.0/24, as podman's own")
check(_rs is not None and not any(n.overlaps(_rs) or n.overlaps(_rt) for n in _pnets),
      "podman here never hands the remote subnet or the runtime's to another network", _pools)
check(all(any(n.subnet_of(d) for d in _podman) for n in _pnets)
      and not any(a.overlaps(b) for i, a in enumerate(_pnets) for b in _pnets[i + 1:])
      and sum(n.num_addresses for n in _pnets) == sum(d.num_addresses for d in _podman) - 512,
      "the guest's pools are podman's own less 10.89.78.0/23 and nothing else")
for name, unit in (("door", q_door), ("certificate job", q_cert)):
    c = dict(unit.get("Container", []))
    check((c.get("User"), c.get("ReadOnly"), c.get("DropCapability"), c.get("NoNewPrivileges")) ==
          ("1000", "true", "ALL", "true") and "Image" not in c,
          f"the guest's {name} runs as 1000, read-only, with no capabilities, its image left to configure")
v_h, v_d, v_c = _volumes(q_host), _volumes(q_door), _volumes(q_cert)
check(v_d.get("cseries_remote-door-key") == ("/data/door-key", "U") and
      v_h.get("cseries_remote-door-key") == ("/run/cseries-door", "ro") and
      q_env_h.get("CSERIES_DOOR_SECRET_FILE") == "/run/cseries-door/door-secret",
      "the door makes the door key and the Host reads it, read-only, where CSERIES_DOOR_SECRET_FILE names it")
check(v_h.get("cseries_remote-acme") == (q_env_h.get("CSERIES_REMOTE_ACME_DIR"), "U") and
      v_c.get("cseries_remote-acme") == ("/etc/cseries-cloud", "ro") and
      v_d.get("cseries_remote-acme") == ("/etc/cseries-cloud", "ro"),
      "the Host writes the remote-acme volume; the certificate job and the door only read it")
check(v_c.get("cseries_remote-tls") == ("/etc/caddy/tls", "U") and v_d.get("cseries_remote-tls") == ("/etc/caddy/tls", "ro")
      and "cseries_remote-tls" not in v_h and "cseries_remote-cert-state" not in v_h
      and "cseries_remote-cert-state" not in v_d,
      "the certificate's key reaches the door, read-only, and never the Host")
check(v_c.get("cseries_remote-cert-status") == ("/var/lib/cseries-cert-status", "U") and
      v_h.get("cseries_remote-cert-status", ("", ""))[1] == "ro" and
      q_env_h.get("CSERIES_REMOTE_CERT_STATUS", "").startswith(v_h.get("cseries_remote-cert-status", ("?",))[0] + "/"),
      "the certificate job writes its status and the Host reads it, read-only, where CSERIES_REMOTE_CERT_STATUS names it")

# cseries-guest configure takes the door's image for both of its units, and
# start never makes the Host wait on (or fail with) the door.
_root = os.path.join(_g_tmp, "g1")
_r = _guest(_root, ["configure"], _keys + f"door={_door}\n")
_units = os.path.join(_root, "etc/containers/systemd")
check(_r.returncode == 0 and "changed=1" in _r.stdout and all(
      open(os.path.join(_units, f"cseries-remote-{u}.container.d/image.conf")).read() == f"[Container]\nImage={_door}\n"
      for u in ("door", "cert")), "configure writes the door's image for its door and its certificate job", _r.stderr)
_r = _guest(_root, ["configure"], _keys + f"door={_door}\n")
check(_r.returncode == 0 and "changed=0" in _r.stdout, "the same door image again changes nothing", _r.stdout)
_r = _guest(_root, ["configure"], _keys)
check(_r.returncode == 0 and "changed=0" in _r.stdout and
      os.path.exists(os.path.join(_units, "cseries-remote-door.container.d/image.conf")),
      "a release without a door keeps the one configured", _r.stdout)
_r = _guest(_root, ["configure"], _keys + "door=ghcr.io/privatae/cseries-remote-door:latest\n")
check(_r.returncode != 0 and "door image is not pinned by digest" in _r.stderr, "an unpinned door image is refused")
open(os.path.join(_root, "calls.log"), "w").close()
_r = _guest(_root, ["start"])
_calls = open(os.path.join(_root, "calls.log")).read()
check(_r.returncode == 0 and "systemctl --no-block start cseries-remote-door.service cseries-remote-cert.service" in _calls
      and "systemctl start cseries-postgres.service cseries-host.service" in _calls,
      "start starts the door's units beside the Host's, and a door that fails to start does not fail the Host's",
      (_r.returncode, _r.stderr, _calls))
_r = _guest(_root, ["status"])
check(_r.returncode == 0 and f"door_image={_door}" in _r.stdout.splitlines() and
      "door=active" in _r.stdout.splitlines() and "remote_cert=active" in _r.stdout.splitlines(),
      "status names the door's image and both units' state", _r.stdout)
open(os.path.join(_root, "calls.log"), "w").close()
_r = _guest(_root, ["pull"])
check(_r.returncode == 0 and f"podman pull {_door}" in open(os.path.join(_root, "calls.log")).read(),
      "pull fetches the door's image when one is configured")
with open(os.path.join(_g_bin, "podman"), "w") as _f:
    _f.write('#!/bin/sh\necho "podman $*" >> "$FAKE_LOG"\n[ "$1 $2" = "image exists" ] && exit 1\n'
             'case "$*" in *cseries-remote-door*) exit 125 ;; esac\nexit 0\n')
_r = _guest(_root, ["pull"])
check(_r.returncode == 0 and "door's image could not be downloaded" in _r.stderr,
      "a door image that cannot be downloaded does not fail the Host's pull", (_r.returncode, _r.stderr))
_root2 = os.path.join(_g_tmp, "g2")
_r = _guest(_root2, ["configure"], _keys)
open(os.path.join(_root2, "calls.log"), "w").close()
_r = _guest(_root2, ["start"])
check(_r.returncode == 0 and "cseries-remote" not in open(os.path.join(_root2, "calls.log")).read() and
      not os.path.exists(os.path.join(_root2, "etc/containers/systemd/cseries-remote-door.container.d")),
      "a guest given no door image has no door units and starts none")
check(not os.path.exists(os.path.join(_root2, "etc/containers/systemd/cseries-host.container.d/remote.conf")),
      "a guest given no door image never joins the Host to the remote network")
shutil.rmtree(_g_tmp, ignore_errors=True)


# ── 3. the Host's gates: no loopback lane for a remote name ─────────────────

class Store:
    """The tables the sign-in touches, as dicts."""

    def __init__(self):
        self.owner = {"email": "owner@example.com", "password_hash": None, "cookie_suffix": "0a1b2c3d"}
        self.sessions, self.passkeys, self.codes = {}, {}, {}

    async def fetchval(self, sql, *a):
        if sql.startswith("SELECT count(*) FROM owner_passkeys WHERE rp_id"):
            return sum(1 for r in self.passkeys.values() if r["rp_id"] in a[0])
        if sql.startswith("SELECT count(*) FROM owner_passkeys"):
            return len(self.passkeys)
        if sql.startswith("SELECT count(*) FROM owner_recovery_codes"):
            return 10
        if sql.startswith("SELECT EXISTS (SELECT 1 FROM owner_passkeys)"):
            return bool(self.passkeys)
        if sql.startswith("INSERT INTO owner_passkeys"):
            if a[0] in self.passkeys:
                return None
            self.passkeys[a[0]] = {"credential_id": a[0], "public_key": a[1], "sign_count": a[2],
                                   "transports": list(a[3]), "label": a[4], "rp_id": a[5],
                                   "created_at": datetime.now(timezone.utc), "last_used_at": None}
            return a[0]
        if sql.startswith("SELECT password_hash IS NOT NULL"):
            return True
        raise AssertionError(f"unexpected fetchval: {sql}")

    async def fetch(self, sql, *a):
        if sql.startswith("SELECT credential_id, transports FROM owner_passkeys"):
            return [r for r in self.passkeys.values() if r["rp_id"] in a[0]]
        if sql.startswith("SELECT credential_id, label"):
            return list(self.passkeys.values())
        raise AssertionError(f"unexpected fetch: {sql}")

    async def fetchrow(self, sql, *a):
        if "FROM owner_sessions s" in sql:
            return self.sessions.get(a[0])
        if sql.startswith("SELECT public_key, sign_count FROM owner_passkeys"):
            return self.passkeys.get(a[0])
        if "FROM owner_account" in sql:
            return self.owner
        raise AssertionError(f"unexpected fetchrow: {sql}")

    async def execute(self, sql, *a):
        if sql.startswith("INSERT INTO owner_sessions"):
            self.sessions[a[0]] = {"last_seen_at": a[1], "expires_at": a[2], "origin": a[3],
                                   "parent": None, "parent_seen": None, "parent_expires": None}
        elif sql.startswith("UPDATE owner_passkeys SET sign_count"):
            self.passkeys[a[0]]["sign_count"] = a[1]
        return "OK"


STORE = Store()
L._pool = lambda: STORE
L._owner_email, L._cookie_suffix, L._setup_code = "owner@example.com", "0a1b2c3d", None

SEEN = []      # (peer, path, host, x-forwarded-for, x-forwarded-uri) for every request the Host got


def build():
    app = FastAPI()
    app.include_router(R.router)
    app.include_router(RR.router)

    @app.get("/probe")
    async def probe():
        return {"ok": True}

    app.add_middleware(G.LocalhostOnlyMiddleware)
    app.add_middleware(G.LocalhostOnlyWebSocketGate)
    app.add_middleware(OwnerSessionGate)
    app.add_middleware(AppOriginGuard)
    app.add_middleware(HostHeaderGuard)

    async def recorder(scope, receive, send):
        if scope["type"] == "http":
            hd = {k.decode("latin-1"): v.decode("latin-1") for k, v in scope.get("headers") or ()}
            SEEN.append(((scope.get("client") or ("?",))[0], scope["path"], hd.get("host", ""),
                         hd.get("x-forwarded-for", ""), hd.get("x-forwarded-uri", "")))
        return await app(scope, receive, send)
    return recorder


APP = build()


async def asgi(path, *, peer, host, method="GET", accept="application/json"):
    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": method,
             "scheme": "http", "path": path, "raw_path": path.encode(), "query_string": b"",
             "root_path": "", "client": (peer, 40000), "server": (HOST, 8800),
             "headers": [(b"host", host.encode()), (b"accept", accept.encode())]}
    sent, done = [], asyncio.Event()

    async def receive():
        if not getattr(receive, "given", False):
            receive.given = True
            return {"type": "http.request", "body": b"", "more_body": False}
        await done.wait()
        return {"type": "http.disconnect"}

    async def send(msg):
        sent.append(msg)
        if msg["type"] == "http.response.body" and not msg.get("more_body"):
            done.set()

    await APP(scope, receive, send)
    return next(m for m in sent if m["type"] == "http.response.start")["status"]


async def part3():
    for peer in ("127.0.0.1", "::1", "::ffff:127.0.0.1"):
        st = await asgi("/probe", peer=peer, host=NAME)
        check(st in (401, 403), f"a request for the remote name from {peer} gets no loopback pass", st)
        st = await asgi("/probe", peer=peer, host=f"apps-{'a' * 20}-home.{AID}.{ZONE}")
        check(st in (400, 401, 403, 404), f"nor one for a remote app origin from {peer}", st)
        st = await asgi("/", peer=peer, host=NAME, accept="text/html")
        check(st in (303, 401, 403), f"a page on the remote name from {peer} is not served", st)
    check(await asgi("/probe", peer="127.0.0.1", host="localhost:8800") == 200,
          "the desktop's own loopback lane is unchanged (localhost from 127.0.0.1 passes)")
    check(not G.loopback_host_ok(NAME) and G.loopback_host_ok("localhost:8800"),
          "loopback_host_ok refuses the remote name even when CSERIES_ALLOWED_ORIGINS lists it")
    SEEN.clear()


asyncio.run(part3())


# ── 4. end to end, through the relay, the tunnel and the real Caddy ─────────

def _name(cn):
    return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])


def _ca(cn):
    key = ec.generate_private_key(ec.SECP256R1())
    now = dt.datetime.now(dt.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(_name(cn)).issuer_name(_name(cn))
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - dt.timedelta(minutes=5)).not_valid_after(now + dt.timedelta(days=1))
            .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
            .add_extension(x509.KeyUsage(digital_signature=True, content_commitment=False, key_encipherment=False,
                                         data_encipherment=False, key_agreement=False, key_cert_sign=True,
                                         crl_sign=True, encipher_only=False, decipher_only=False), critical=True)
            .sign(key, hashes.SHA256()))
    return key, cert


def _leaf(ca_key, ca, name, *, dns=(), uri=None, server=True):
    key = ec.generate_private_key(ec.SECP256R1())
    now = dt.datetime.now(dt.timezone.utc)
    sans = [x509.DNSName(d) for d in dns] + ([x509.UniformResourceIdentifier(uri)] if uri else [])
    cert = (x509.CertificateBuilder().subject_name(x509.Name([])).issuer_name(ca.subject)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - dt.timedelta(minutes=5)).not_valid_after(now + dt.timedelta(hours=12))
            .add_extension(x509.SubjectAlternativeName(sans), critical=True)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH if server
                                                  else ExtendedKeyUsageOID.CLIENT_AUTH]), critical=False)
            .sign(ca_key, hashes.SHA256()))
    d = os.path.join(TMP, name)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "key.pem"), "wb") as f:
        f.write(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                  serialization.NoEncryption()))
    with open(os.path.join(d, "cert.pem"), "wb") as f:
        f.write(cert.public_bytes(serialization.Encoding.PEM))
    return os.path.join(d, "cert.pem"), os.path.join(d, "key.pem"), cert.serial_number


def _pem(path, cert):
    with open(path, "wb") as f:
        f.write(cert.public_bytes(serialization.Encoding.PEM))
    return path


root_key, root = _ca("test network root")
ROOT_PEM = _pem(os.path.join(TMP, "root.pem"), root)
RELAY_CERT, RELAY_KEY, _ = _leaf(root_key, root, "relay", dns=[rp.tunnel_name(ZONE)])
PASS_CERT, PASS_KEY, PASS_SERIAL = _leaf(root_key, root, "passport", uri="urn:test:host:a", server=False)
web_key, web = _ca("test web pki")
WEB_CA = _pem(os.path.join(TMP, "web-ca.pem"), web)
_leaf(web_key, web, "door-tls", dns=[f"*.{AID}.{ZONE}"])
TICKET_KEY = Ed25519PrivateKey.generate()


def b64u(b):
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


class Authenticator:
    def __init__(self):
        self.key = ec.generate_private_key(ec.SECP256R1())
        self.cred_id = os.urandom(16)
        self.counter = 0

    def _cdj(self, kind, challenge, origin):
        return json.dumps({"type": kind, "challenge": challenge, "origin": origin, "crossOrigin": False}).encode()

    def create(self, options, origin):
        n = self.key.public_key().public_numbers()
        cose = cbor2.dumps({1: 2, 3: -7, -1: 1, -2: n.x.to_bytes(32, "big"), -3: n.y.to_bytes(32, "big")})
        auth = (hashlib.sha256(options.get("rp", {}).get("id", "").encode()).digest() + bytes([0x45]) + struct.pack(">I", 0)
                + b"\x00" * 16 + struct.pack(">H", len(self.cred_id)) + self.cred_id + cose)
        att = cbor2.dumps({"fmt": "none", "attStmt": {}, "authData": auth})
        return {"id": b64u(self.cred_id), "rawId": b64u(self.cred_id), "type": "public-key",
                "response": {"clientDataJSON": b64u(self._cdj("webauthn.create", options.get("challenge", ""), origin)),
                             "attestationObject": b64u(att), "transports": ["internal"]},
                "clientExtensionResults": {}}

    def get(self, options, origin):
        self.counter += 1
        cdj = self._cdj("webauthn.get", options.get("challenge", ""), origin)
        auth = hashlib.sha256(options.get("rpId", "").encode()).digest() + bytes([0x05]) + struct.pack(">I", self.counter)
        sig = self.key.sign(auth + hashlib.sha256(cdj).digest(), ec.ECDSA(hashes.SHA256()))
        return {"id": b64u(self.cred_id), "rawId": b64u(self.cred_id), "type": "public-key",
                "response": {"clientDataJSON": b64u(cdj), "authenticatorData": b64u(auth),
                             "signature": b64u(sig), "userHandle": b64u(P.USER_HANDLE)},
                "clientExtensionResults": {}}


def browser_ctx():
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.load_verify_locations(WEB_CA)
    return ctx


async def browse(relay, method, path, *, host=NAME, sni=NAME, headers=None, body=None):
    """One request from the browser's address, through the relay; (status,
    headers, body), or (0, {}, b"") when the connection gives no answer."""
    ctx = browser_ctx()
    try:
        r, w = await asyncio.wait_for(asyncio.open_connection(
            RELAY, relay.browser_port, ssl=ctx, server_hostname=sni, local_addr=(BROWSER, 0)), 10)
    except (OSError, ssl.SSLError, asyncio.TimeoutError) as exc:
        if os.environ.get("REMOTE_DOOR_DEBUG"):
            print("browse:", repr(exc))
        return 0, {}, b""
    data = json.dumps(body).encode() if body is not None else b""
    hd = {"Host": host, "Connection": "close", "Accept": "application/json", **(headers or {})}
    if body is not None:
        hd.update({"Content-Type": "application/json", "Content-Length": str(len(data)), "Origin": REMOTE_ORIGIN})
    w.write((f"{method} {path} HTTP/1.1\r\n" + "".join(f"{k}: {v}\r\n" for k, v in hd.items()) + "\r\n").encode()
            + data)
    raw = b""
    try:
        while True:
            chunk = await asyncio.wait_for(r.read(65536), 10)
            if not chunk:
                break
            raw += chunk
    except (OSError, ssl.SSLError, asyncio.IncompleteReadError, asyncio.TimeoutError):
        pass
    w.close()
    head, _, rest = raw.partition(b"\r\n\r\n")
    lines = head.decode("latin-1").split("\r\n")
    if not lines or not lines[0].startswith("HTTP/"):
        return 0, {}, b""
    out = {}
    for ln in lines[1:]:
        k, _, v = ln.partition(":")
        out.setdefault(k.strip().lower(), []).append(v.strip())
    return int(lines[0].split()[1]), out, rest


def session_cookie(hd):
    for c in hd.get("set-cookie", []):
        name, _, rest = c.partition("=")
        if name == L.cookie_name(NAME):
            return name, rest.split(";")[0], c
    return None


async def wait_port(addr, port, timeout=15.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        try:
            _r, w = await asyncio.open_connection(addr, port)
            w.close()
            return True
        except OSError:
            await asyncio.sleep(0.1)
    return False


async def part4():
    server = uvicorn.Server(uvicorn.Config(APP, host=HOST, port=8800, log_level="warning", lifespan="off",
                                           proxy_headers=False, access_log=False,
                                           timeout_keep_alive=IDLE_KEEP_ALIVE_S))
    serve = asyncio.ensure_future(server.serve())
    caddy_env = dict(os.environ, CSERIES_DOOR_BIND=DOOR, CSERIES_DOOR_PORT="8443", CSERIES_TUNNEL_FROM=HOST,
                     CSERIES_REMOTE_ZONE=f"{AID}.{ZONE}", CSERIES_HOST_UPSTREAM=f"{HOST}:8800",
                     CSERIES_DOOR_SECRET=KEY, CSERIES_DOOR_TLS=os.path.join(TMP, "door-tls"),
                     CSERIES_DOOR_ADMIN=os.path.join(TMP, "admin.sock"), HOME=TMP,
                     XDG_DATA_HOME=TMP, XDG_CONFIG_HOME=TMP)
    caddy = subprocess.Popen(["caddy", "run", "--config", os.path.join(ROOT, "sidecars/remote-door/Caddyfile"),
                              "--adapter", "caddyfile"], env=caddy_env, stdout=subprocess.DEVNULL,
                             stderr=open(os.path.join(TMP, "caddy.log"), "wb"))
    relay = relay_mod.Relay(zone=ZONE, ticket_keys={"t1": TICKET_KEY.public_key()},
                            tunnel_ssl=relay_mod.server_context(cert=RELAY_CERT, key=RELAY_KEY, client_ca=ROOT_PEM),
                            heartbeat=0.5, check_interval=0.2)
    client = None
    try:
        check(await wait_port(DOOR, 8443) and await wait_port(HOST, 8800), "the real Caddy and the Host are up")
        await relay.start(host=RELAY)

        async def tickets(_fresh):
            now = time.time()
            return rp.sign_ticket(TICKET_KEY, {"v": 1, "kid": "t1", "host_uuid": "host-a",
                                               "serial": format(PASS_SERIAL, "x"), "address_id": AID,
                                               "zone": ZONE, "iat": now, "exp": now + 3600})
        door = RA.door_address({"CSERIES_REMOTE_DOOR": f"{DOOR}:8443"})
        client = tun.TunnelClient(
            address_id=AID, zone=ZONE, relays=[tun.RelayAddress(RELAY, relay.tunnel_port, "test")],
            ssl_context=tun.client_context(passport_cert=PASS_CERT, passport_key=PASS_KEY, network_root=ROOT_PEM),
            tickets=tickets, door=door, heartbeat=0.5,
            delays=tun.Delays(first=(0.01, 0.03), cap=0.2, stable=60.0, refused=0.3), rng=random.Random(7))
        client.start()
        for _ in range(200):
            if client.status()[0]["state"] == "up" and AID in relay.tunnels:
                break
            await asyncio.sleep(0.05)
        check(client.status()[0]["state"] == "up", "the tunnel is up", client.status())

        st, hd, _ = await browse(relay, "GET", "/", headers={"Accept": "text/html"})
        check(st == 303 and hd.get("location") == ["/login"], "without a session a page is sent to /login", (st, hd))
        st, _hd, _ = await browse(relay, "GET", "/probe")
        check(st == 401, "without a session a protected route is 401", st)
        st, _hd, body = await browse(relay, "GET", "/login", headers={"Accept": "text/html"})
        check(st == 200 and b'id="passkey"' in body and b"/api/owner/login" not in body and b'name="password"' not in body,
              "the remote sign-in page offers a passkey and a recovery code, never the password", st)
        n = len(SEEN)
        st, _hd, _ = await browse(relay, "POST", "/api/owner/login",
                                  body={"email": "owner@example.com", "password": "a-long-enough-password"})
        check(st == 403, "the password is refused on the remote address", st)
        check(not any(p == "/api/owner/login" for _peer, p, *_ in SEEN[n:]),
              "and the password never reaches its route (the door refuses it)")
        st, _hd, _ = await browse(relay, "GET", "/setup", headers={"Accept": "text/html"})
        check(st == 403, "/setup is refused on the remote address", st)

        # A phone joins: the desktop's owner made a code (a passkey on the
        # desktop's own address exists, and a fresh check).
        desk = Authenticator()
        STORE.passkeys[desk.cred_id] = {"credential_id": desk.cred_id, "public_key": b"", "sign_count": 0,
                                        "transports": [], "label": "laptop", "rp_id": "",
                                        "created_at": datetime.now(timezone.utc), "last_used_at": None}
        desk_token = await L._new_session(STORE, home_origin.HOME.host)
        P._note_step_up(desk_token, P.PASSKEY)
        made = await J.new_code(desk_token, home_origin.HOME.host)
        check(made["url"].startswith(f"{REMOTE_ORIGIN}/remote/join#c="), "the QR code opens the remote join page")
        code = made["url"].split("#c=", 1)[1]
        st, _hd, body = await browse(relay, "GET", "/remote/join", headers={"Accept": "text/html"})
        check(st == 200 and b"Make a passkey on this phone" in body, "the join page opens without a session", st)
        st, _hd, body = await browse(relay, "POST", "/api/owner/remote/join/options", body={"code": code})
        opts = json.loads(body or b"{}")
        check(st == 200 and opts.get("options", {}).get("rp", {}).get("id") == NAME,
              "the code is spent for a passkey of the remote address", (st, body[:200]))
        phone = Authenticator()
        st, hd, body = await browse(relay, "POST", "/api/owner/remote/join/verify",
                                    body={"code": code, "ceremony": opts.get("ceremony"),
                                          "credential": phone.create(opts.get("options", {}), REMOTE_ORIGIN)})
        cookie = session_cookie(hd)
        check(st == 200 and cookie and cookie[0].startswith("__Host-") and "Secure" in cookie[2],
              "the phone's passkey is kept and starts a __Host- session", (st, body[:200]))
        st, _hd, _ = await browse(relay, "POST", "/api/owner/remote/join/options", body={"code": code})
        check(st == 403, "the join code fails on second use", st)
        if cookie:
            st, _hd, _ = await browse(relay, "GET", "/probe", headers={"Cookie": f"{cookie[0]}={cookie[1]}"})
            check(st == 200, "with the phone's session a protected route answers", st)

        st, _hd, body = await browse(relay, "POST", "/api/owner/passkey/login/options", body={})
        lo = json.loads(body or b"{}")
        st, hd, _ = await browse(relay, "POST", "/api/owner/passkey/login/verify",
                                 body={"ceremony": lo.get("ceremony"),
                                       "credential": phone.get(lo.get("options", {}), REMOTE_ORIGIN)})
        cookie = session_cookie(hd)
        check(st == 200 and cookie is not None, "the phone signs in again with its passkey", st)
        if cookie:
            st, _hd, _ = await browse(relay, "GET", "/probe", headers={"Cookie": f"{cookie[0]}={cookie[1]}"})
            check(st == 200, "and that session opens the protected route", st)

        n = len(SEEN)
        for forged in ("localhost", "localhost:8800", "127.0.0.1:8800", "home.cseries.localhost:8800"):
            st, _hd, _ = await browse(relay, "GET", "/probe", host=forged)
            check(st != 200, f"Host: {forged} through the tunnel is refused by the door", st)
        check(len(SEEN) == n, "and none of them reached the Host", SEEN[n:])

        # Someone else on the door's network, claiming to be a browser.
        n = len(SEEN)
        answered = False
        try:
            s = socket.create_connection((DOOR, 8443), timeout=5, source_address=(STRANGER, 0))
            s.sendall(rp.proxy_v2_header(("198.51.100.99", 4444), (DOOR, 8443)))
            tls = browser_ctx().wrap_socket(s, server_hostname=NAME)
            tls.sendall(f"GET /probe HTTP/1.1\r\nHost: {NAME}\r\nConnection: close\r\n\r\n".encode())
            answered = tls.recv(64).startswith(b"HTTP/")
            tls.close()
        except (OSError, ssl.SSLError):
            pass
        check(not answered and len(SEEN) == n, "a PROXY header from anywhere but the Host is refused")

        peers = {peer for peer, *_ in SEEN}
        check(bool(SEEN) and peers == {DOOR}, "every request the Host received came from the door's address",
              sorted(peers))
        check(not any(ipaddress.ip_address(p).is_loopback or p in G.LOOPBACK_HOSTS for p in peers),
              "none came from loopback")
        fwd = {xff.split(",")[-1].strip() for _p, _path, _h, xff, _u in SEEN}
        check(fwd == {BROWSER}, "the browser's own address reached the Host in X-Forwarded-For", sorted(fwd))
    finally:
        if client is not None:
            await client.stop()
        await relay.stop()
        caddy.terminate()
        try:
            caddy.wait(10)
        except subprocess.TimeoutExpired:
            caddy.kill()
        server.should_exit = True
        await asyncio.wait_for(serve, 10)


asyncio.run(asyncio.wait_for(part4(), 120))
if failed and os.environ.get("REMOTE_DOOR_DEBUG"):
    print(open(os.path.join(TMP, "caddy.log")).read()[-3000:])
shutil.rmtree(TMP, ignore_errors=True)
print("PASSED" if not failed else "FAILED")
sys.exit(failed)
