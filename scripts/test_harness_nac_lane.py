#!/usr/bin/env python3
"""
Harness NAC egress routing gate.

The operator hands every harness pod both exits — the node squid in
HTTP(S)_PROXY, and the user's signed-rule gate in NAC_EGRESS_LANE with a
per-subject credential — but only the Browser ever consumed the lane. A coding
harness read HTTP_PROXY, so it left through the node squid, whose ACLs the user
cannot write: a destination the user had DENIED in a signed rule was still
reachable from a harness run while the Network app said otherwise.

Sending everything to the lane would fix that and break something worse (see
_harness_runtime/egress_chooser.py). So the routing is INVERTED: the lane decides
destinations a rule NAMES, and everything else leaves where it always did.

Gates (exit 0 = all pass; exit 1 + [nac-lane] FAIL lines):

  ADOPTION
   1. lane + token -> all four proxy vars point at the loopback chooser, and the
      credential is NOT in the URL (the chooser injects it per request)
   2. lane, no token -> UNCHANGED. The gate would 407 every request, and a
      harness that reaches nothing is worse than one that leaves by the node
      address. This is the known asymmetry: on that path a DENY does not apply.
   3. no lane / malformed lane / non-numeric port / underivable subject ->
      UNCHANGED. The last three would otherwise crash the sidecar before serve()
      or adopt a credential the gate can only answer 407 to.

  ROUTING, end to end, over real sockets against a fake gate and a fake squid.
  The fake gate authenticates with the PRODUCTION parser
  (nac_egress._proxy_basic_subject), so the credential format is proven rather
  than assumed — that parser splits from the RIGHT because the subject carries a
  colon and the token is an HMAC-SHA256 hexdigest.
   4. a destination a rule NAMES goes to the lane, and does not leak to squid
   5. a destination NOTHING names gets 421 and is retried to the node proxy
   6. that verdict is cached: the second request never touches the gate
   7. a DENY (403) is NOT retried to the node proxy — falling back on a deny
      would be the exact bug this change exists to stop
"""
import base64
import hashlib
import hmac
import logging
import os
import socket
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PROXY_VARS = ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy")
SUBJECT = "harness:codex"
TOKEN = hmac.new(b"secret", f"nac-egress-subject:{SUBJECT}".encode(),
                 hashlib.sha256).hexdigest()

fails = []


def check(cond, msg):
    if not cond:
        fails.append(msg)
        print(f"[nac-lane] FAIL {msg}")
    else:
        print(f"[nac-lane] ok   {msg}")


def _register_chooser():
    """Import egress_chooser standalone and register it as
    `_harness_runtime.egress_chooser`, so the exec'd slice of harness_core can
    import it without pulling in cseries_sidecar (which wants a live pod)."""
    import types
    import importlib.util
    pkg = types.ModuleType("_harness_runtime")
    pkg.__path__ = [str(ROOT / "system-apps" / "_harness_runtime")]
    sys.modules.setdefault("_harness_runtime", pkg)
    spec = importlib.util.spec_from_file_location(
        "_harness_runtime.egress_chooser",
        ROOT / "system-apps" / "_harness_runtime" / "egress_chooser.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    sys.modules["_harness_runtime.egress_chooser"] = mod
    pkg.egress_chooser = mod
    return mod


def _load(path, start, end=None):
    """Exec a slice of a module without importing its package."""
    src = (ROOT / path).read_text()
    i = src.index(start)
    j = src.index(end) if end else len(src)
    ns = {"base64": base64}
    exec("import logging, os\nimport datetime as _dt\nfrom urllib.parse import urlsplit\n"
         "from typing import NamedTuple, Optional\n" + src[i:j], ns)  # noqa: S102
    return ns


chooser = _register_chooser()
adopt = _load("system-apps/_harness_runtime/harness_core.py",
              "def adopt_nac_egress_lane")["adopt_nac_egress_lane"]
_gate = _load("server/network/nac_egress.py",
              "def _split_head", "# ── the in-pod gate")
gate_parse = _gate["_proxy_basic_subject"]
wants_fallback = _gate["wants_direct_fallback"]
strip_fallback = _gate["strip_fallback_header"]

log = logging.getLogger("nac-lane-test")
logging.basicConfig(level=logging.CRITICAL)

SQUID = "http://127.0.0.1:1/"   # unroutable placeholder for the adoption gates
LANE = "http://127.0.0.1:13129"


def run(env):
    for k in PROXY_VARS:
        os.environ[k] = SQUID
    for k in ("NAC_EGRESS_LANE", "NAC_EGRESS_SUBJECT", "NAC_EGRESS_SUBJECT_TOKEN",
              "PRIVATAE_APP_ID", "HARNESS_ID"):
        os.environ.pop(k, None)
    os.environ.update(env)
    return adopt(log)


def listener(reply_for):
    """A proxy-shaped listener: records each request head, answers a status.

    Loopback is correct and required here: these are in-process test doubles for
    the gate and the node squid, reached only by the chooser running in this same
    process. Binding wider would expose a fake proxy on the build host.
    """
    seen = []
    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(16)

    def serve():
        while True:
            try:
                c, _ = srv.accept()
            except OSError:
                return
            try:
                head = b""
                while b"\r\n\r\n" not in head:
                    d = c.recv(4096)
                    if not d:
                        break
                    head += d
                if not head:
                    continue
                seen.append(head)
                line = head.split(b"\r\n", 1)[0].split(b" ")
                host = line[1].rsplit(b":", 1)[0].decode() if len(line) > 1 else ""
                reply = reply_for(host, head)
                if reply:
                    c.sendall(reply)
                    time.sleep(0.05)
            except OSError:
                pass
            finally:
                try:
                    c.close()
                except OSError:
                    pass

    threading.Thread(target=serve, daemon=True).start()
    return srv.getsockname()[1], seen


# ── adoption ───────────────────────────────────────────────────────────────

run({"NAC_EGRESS_LANE": LANE, "NAC_EGRESS_SUBJECT": SUBJECT,
     "NAC_EGRESS_SUBJECT_TOKEN": TOKEN})
check(all(os.environ[v].startswith("http://127.0.0.1:") and os.environ[v] != SQUID
          for v in PROXY_VARS),
      "lane+token points all four proxy vars at the loopback chooser")
check(TOKEN not in os.environ["HTTPS_PROXY"],
      "the credential is NOT in the proxy URL — the chooser injects it per request")

run({"NAC_EGRESS_LANE": LANE, "NAC_EGRESS_SUBJECT": SUBJECT})
check(all(os.environ[v] == SQUID for v in PROXY_VARS),
      "lane without token stays on the node proxy (the gate would 407)")

run({})
check(all(os.environ[v] == SQUID for v in PROXY_VARS), "no lane leaves the env alone")

run({"NAC_EGRESS_LANE": "not-a-url", "NAC_EGRESS_SUBJECT": SUBJECT,
     "NAC_EGRESS_SUBJECT_TOKEN": TOKEN})
check(all(os.environ[v] == SQUID for v in PROXY_VARS), "malformed lane leaves the env alone")

try:
    run({"NAC_EGRESS_LANE": "http://h:abc", "NAC_EGRESS_SUBJECT": SUBJECT,
         "NAC_EGRESS_SUBJECT_TOKEN": TOKEN})
    check(all(os.environ[v] == SQUID for v in PROXY_VARS),
          "non-numeric port falls back instead of raising")
except ValueError as e:
    check(False, f"non-numeric port raised instead of falling back: {e}")

run({"NAC_EGRESS_LANE": LANE, "NAC_EGRESS_SUBJECT_TOKEN": TOKEN})
check(all(os.environ[v] == SQUID for v in PROXY_VARS),
      "underivable subject falls back (it would authenticate as nobody)")

# ── routing, end to end ────────────────────────────────────────────────────

RULED, UNRULED, DENIED = "ruled.example", "unruled.example", "denied.example"


def gate_reply(host, head):
    subj, tok = gate_parse(head)                 # the PRODUCTION parser
    if subj != SUBJECT or tok != TOKEN:
        return b"HTTP/1.1 407 Proxy Authentication Required\r\n\r\n"
    if host == DENIED:
        return b"HTTP/1.1 403 Forbidden\r\n\r\n"
    if host == RULED:
        return b"HTTP/1.1 200 Connection established\r\n\r\n"
    return b"HTTP/1.1 421 Misdirected Request\r\n\r\n"


gate_port, gate_seen = listener(gate_reply)
squid_port, squid_seen = listener(
    lambda h, d: b"HTTP/1.1 200 Connection established\r\n\r\n")

lport = chooser.start(("127.0.0.1", gate_port), ("127.0.0.1", squid_port),
                      f"{SUBJECT}:{TOKEN}")


def request(host):
    s = socket.create_connection(("127.0.0.1", lport), timeout=5)
    s.sendall(f"CONNECT {host}:443 HTTP/1.1\r\nHost: {host}:443\r\n\r\n".encode())
    s.settimeout(5)
    try:
        return s.recv(256)
    except socket.timeout:
        return b""
    finally:
        s.close()


def saw(seen, host):
    return [h for h in seen if host.encode() in h]


r = request(RULED)
check(r.startswith(b"HTTP/1.1 200"), f"a named destination reaches the lane ({r[:24]!r})")
check(bool(saw(gate_seen, RULED)),
      "the gate saw it, and the production parser accepted the credential")
check(not saw(squid_seen, RULED), "a named destination does NOT leak to the node proxy")

r = request(UNRULED)
check(r.startswith(b"HTTP/1.1 200"), f"an unnamed destination still connects ({r[:24]!r})")
check(bool(saw(squid_seen, UNRULED)),
      "an unnamed destination is retried to the node proxy after the gate's 421")

before = len(saw(gate_seen, UNRULED))
request(UNRULED)
after = len(saw(gate_seen, UNRULED))
check(before == after, f"the verdict is cached: the gate is not asked again ({before} -> {after})")

request(DENIED)
check(not saw(squid_seen, DENIED), "a DENY is NOT retried to the node proxy")

# the node proxy must never see the lane credential or our routing hint
check(not any(TOKEN.encode() in h for h in squid_seen),
      "the node proxy never sees the lane credential")
check(not any(b"X-CSeries-Egress-Fallback" in h for h in squid_seen),
      "the node proxy never sees the fallback header")

# A transient non-421 caches 'lane'; a later 421 must NOT reach the client.
# Without the unconditional retry, npm/pip would get 421 from its proxy forever.
FLAKY = "flaky.example"
flaky_state = {"n": 0}


def flaky_gate(host, head):
    if host != FLAKY:
        return gate_reply(host, head)
    flaky_state["n"] += 1
    return (b"HTTP/1.1 502 Bad Gateway\r\n\r\n" if flaky_state["n"] == 1
            else b"HTTP/1.1 421 Misdirected Request\r\n\r\n")


gate2_port, gate2_seen = listener(flaky_gate)
squid2_port, squid2_seen = listener(
    lambda h, d: b"HTTP/1.1 200 Connection established\r\n\r\n")
chooser._verdict.clear()
lport2 = chooser.start(("127.0.0.1", gate2_port), ("127.0.0.1", squid2_port),
                       f"{SUBJECT}:{TOKEN}")


def request2(host):
    s = socket.create_connection(("127.0.0.1", lport2), timeout=5)
    s.sendall(f"CONNECT {host}:443 HTTP/1.1\r\nHost: {host}:443\r\n\r\n".encode())
    s.settimeout(5)
    try:
        return s.recv(256)
    except socket.timeout:
        return b""
    finally:
        s.close()


request2(FLAKY)                     # 502 -> would have cached 'lane'
r = request2(FLAKY)                 # 421 -> must retry, never forward the sentinel
check(not r.startswith(b"HTTP/1.1 421"),
      f"a 421 after a transient failure is never forwarded to the client ({r[:24]!r})")
check(bool(saw(squid2_seen, FLAKY)), "it is retried to the node proxy instead")

# A rejected credential falls back rather than 407ing for the pod's life
AUTHFAIL = "authfail.example"
gate3_port, _ = listener(
    lambda h, d: b"HTTP/1.1 407 Proxy Authentication Required\r\n\r\n")
squid3_port, squid3_seen = listener(
    lambda h, d: b"HTTP/1.1 200 Connection established\r\n\r\n")
chooser._verdict.clear()
lport3 = chooser.start(("127.0.0.1", gate3_port), ("127.0.0.1", squid3_port),
                       f"{SUBJECT}:{TOKEN}")
s3 = socket.create_connection(("127.0.0.1", lport3), timeout=5)
s3.sendall(f"CONNECT {AUTHFAIL}:443 HTTP/1.1\r\n\r\n".encode())
s3.settimeout(5)
try:
    r3 = s3.recv(256)
except socket.timeout:
    r3 = b""
s3.close()
check(not r3.startswith(b"HTTP/1.1 407") and bool(saw(squid3_seen, AUTHFAIL)),
      f"a rejected credential falls back to the node proxy ({r3[:24]!r})")

# the gate's own opt-in plumbing, on the production functions
optin = b"CONNECT a.example:443 HTTP/1.1\r\nX-CSeries-Egress-Fallback: 1\r\n\r\n"
plain = b"CONNECT a.example:443 HTTP/1.1\r\nHost: a.example\r\n\r\n"
check(wants_fallback(optin) and not wants_fallback(plain),
      "the gate reads the opt-in only when the caller sets it (the Browser does not)")
check(b"X-CSeries-Egress-Fallback" not in strip_fallback(optin),
      "the gate strips the opt-in before forwarding it anywhere")

# An ALLOW with no pinned exit carries a rule_id, so the 421 must NOT be
# gated on `rule_id is None` — an allowlist is "explicit allows + a final * deny",
# which would otherwise put an allowlist user's entire build through the gate.
decide = _load("server/network/nac_egress.py", "def _host_matches",
               "def _verify_rule")
d = decide["egress_decision"]([{"pattern": "*", "action": "allow", "subject": "*",
                                "id": 7}], "anything.example", SUBJECT)
check(d.allowed and d.rule_id == 7 and d.exit == "default",
      f"an allow with no pinned exit carries a rule_id and the default exit ({d})")

# ── the gate's OWN _handle, driven end to end ─────────────────────────────
# The gates above assert egress_decision's PREMISE (an allow carries a rule_id).
# They would still pass if the `rule_id is None` condition were put back on the
# 421 — the defect that would route an allowlist user's
# entire build through the entity pod. These drive the real EgressProxy._handle.

import asyncio  # noqa: E402
sys.path.insert(0, str(ROOT))
from server.network import nac_egress as gate_mod  # noqa: E402


class _W:
    """Enough of a StreamWriter for _refuse and _direct_dispatch."""

    def __init__(self):
        self.buf = b""

    def write(self, b):
        self.buf += b

    async def drain(self):
        pass

    def close(self):
        pass

    async def wait_closed(self):
        pass

    def get_extra_info(self, *a, **k):
        return None


def drive(rules, opt_in):
    """Run one CONNECT through the real gate. Returns (response, went_direct)."""
    px = gate_mod.EgressProxy(pool=None, entity_id=1, verify_pubkey=None, app_lane=True)
    px._rules_now = lambda: asyncio.sleep(0, result=rules)
    px._lane_subject_ok = lambda *a, **k: asyncio.sleep(0, result=True)
    direct = {"hit": False}

    async def fake_direct(head, reader, writer, method, target):
        direct["hit"] = True

    px._direct_dispatch = fake_direct
    head = b"CONNECT ruled.example:443 HTTP/1.1\r\n"
    head += b"Proxy-Authorization: Basic " + base64.b64encode(
        f"{SUBJECT}:{TOKEN}".encode()) + b"\r\n"
    if opt_in:
        head += b"X-CSeries-Egress-Fallback: 1\r\n"
    head += b"\r\n"

    async def go():
        r = asyncio.StreamReader()      # binds to the running loop
        r.feed_data(head)
        r.feed_eof()
        w = _W()
        await px._handle(r, w)
        return w.buf

    return asyncio.run(go()), direct["hit"]


ALLOW = [{"pattern": "*", "action": "allow", "subject": "*", "id": 7}]
DENY = [{"pattern": "ruled.example", "action": "deny", "subject": "*", "id": 8}]

resp, went_direct = drive(ALLOW, opt_in=True)
check(b"421" in resp and not went_direct,
      f"REAL GATE: an allow with a rule_id still 421s a self-routing caller "
      f"(this fails if `rule_id is None` comes back) ({resp[:32]!r})")

resp, went_direct = drive(ALLOW, opt_in=False)
check(went_direct and b"421" not in resp,
      "REAL GATE: without the opt-in the gate forwards as before (the Browser)")

resp, went_direct = drive(DENY, opt_in=True)
check(b"403" in resp and not went_direct,
      f"REAL GATE: a deny is refused, never handed to the caller ({resp[:32]!r})")

# the cache is bounded: expiry is on read, so a host asked about once and never
# again would otherwise sit there for the pod's life.
_ttl = chooser.VERDICT_TTL_S
chooser.VERDICT_TTL_S = 0.05
chooser._verdict.clear()
for i in range(chooser._VERDICT_SWEEP_AT):
    chooser._verdict[f"h{i}"] = (True, time.monotonic())
time.sleep(0.1)
chooser._remember("fresh.example", True)
check(len(chooser._verdict) == 1,
      f"the verdict cache sweeps stale entries at its bound ({len(chooser._verdict)} left)")
chooser.VERDICT_TTL_S = _ttl

print(f"\n[nac-lane] {'PASS' if not fails else f'FAIL ({len(fails)})'}")
sys.exit(1 if fails else 0)
