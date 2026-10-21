#!/usr/bin/env python3
"""v127 — NAC egress subject + exit. Pure-logic ratchet, no DB, no network.

Covers the things that were WRONG in the design before they were code, so a
regression puts them back rather than someone rediscovering them:

  * the relay's re-queue: one unpinned request must consume exactly ONE worker,
    not one from every connected bastion (asyncio.wait can complete several
    queue get()s in a single tick, and cancelling a completed task does not
    un-dequeue it);
  * an empty relay must not raise ValueError out of asyncio.wait(set());
  * Proxy-Authorization must never survive into a forwarded head — the bastion
    writes that head straight to the destination origin on absolute-form HTTP
    (client-go/egress.go:88);
  * Basic credentials split from the RIGHT, because a subject contains a colon;
  * a pre-v127 rule (three-key terms, no subject/exit) keeps its exact meaning;
  * an exit is never read off the ROW, only out of the verified terms.

Run: python3 scripts/test_nac_egress_subject_exit.py
"""
import asyncio
import base64
import os
import sys

sys.path.insert(0, os.environ.get("CSERIES_ROOT", os.getcwd()))

from server.network.nac_egress import (            # noqa: E402
    EXIT_DEFAULT, SUBJECT_POD, EgressDecision, _proxy_basic_subject,
    _subject_matches, egress_decision, load_egress_rules, strip_proxy_auth)
from server.network.egress_relay import EgressRelay, RelayWorker   # noqa: E402

_fails = []


def check(name, cond, extra=""):
    print(("  ok   " if cond else "  FAIL ") + name + (f"   [{extra}]" if not cond and extra else ""))
    if not cond:
        _fails.append(name)


# ── policy: back-compat first ──────────────────────────────────────────────
print("a pre-v127 world behaves exactly as it did")
check("no rules => allowed, default exit",
      egress_decision([], "api.x.ai") == EgressDecision(True, EXIT_DEFAULT, None))
check("the decision is truthy-compatible with the old `-> bool` contract",
      bool(egress_decision([], "api.x.ai")) is True)
_old = [{"id": 1, "pattern": "evil.example", "action": "deny", "priority": 100}]
check("a rule dict with NO subject/exit keys still matches",
      egress_decision(_old, "evil.example").allowed is False)
check("...and still does not touch other hosts",
      egress_decision(_old, "api.x.ai").allowed is True)
check("`if not egress_decision(...)` still reads correctly",
      (not egress_decision(_old, "evil.example")) is True)

# ── policy: subject ────────────────────────────────────────────────────────
print("subject narrows a rule to one caller")
check("* matches any caller", _subject_matches("*", "harness:browser"))
check("an exact subject matches", _subject_matches("harness:browser", "harness:browser"))
check("a subject is NOT a prefix match (app: must not match every app)",
      not _subject_matches("app:", "app:tradebot"))
check("an absent subject behaves as *", _subject_matches("", "app:x"))
check("the default caller is the pod", egress_decision([], "x.com").exit == EXIT_DEFAULT)

_rules = [
    {"id": 1, "pattern": "*", "action": "allow", "priority": 10,
     "subject": "harness:browser", "exit": "bastion:home"},
    {"id": 2, "pattern": "*", "action": "allow", "priority": 100,
     "subject": "*", "exit": EXIT_DEFAULT},
]
check("the pinned subject gets its exit",
      egress_decision(_rules, "bank.example", "harness:browser").exit == "bastion:home")
check("another caller falls through to the catch-all",
      egress_decision(_rules, "bank.example", "app:tradebot").exit == EXIT_DEFAULT)
check("the pod falls through too",
      egress_decision(_rules, "bank.example", SUBJECT_POD).exit == EXIT_DEFAULT)
check("the deciding rule is named, so a refusal can cite it",
      egress_decision(_rules, "bank.example", "harness:browser").rule_id == 1)
check("first match wins in the ORDER GIVEN — the caller pre-sorts, and a subject "
      "rule below a catch-all is genuinely shadowed (no hidden specificity ranking, "
      "which is why the UI assigns a lower priority instead)",
      egress_decision(list(reversed(_rules)), "bank.example", "harness:browser").exit
      == EXIT_DEFAULT)
_deny = [{"id": 3, "pattern": "*", "action": "deny", "priority": 1,
          "subject": "*", "exit": "bastion:home"}]
check("a deny never reports a pin (it routes nothing)",
      egress_decision(_deny, "x.com").exit == EXIT_DEFAULT
      and not egress_decision(_deny, "x.com").allowed)

# ── the credential, and keeping it out of the head ─────────────────────────
print("the app-lane credential never leaves with the request")
_tok = "b" * 64


def _head(user, token, method=b"GET http://x.com/ HTTP/1.1"):
    return (method + b"\r\nHost: x.com\r\nProxy-Authorization: Basic "
            + base64.b64encode(f"{user}:{token}".encode())
            + b"\r\nUser-Agent: z\r\n\r\nBODY")


for _subj in ("harness:browser", "app:trading-bot", "harness:kimi"):
    s, t = _proxy_basic_subject(_head(_subj, _tok))
    check(f"a subject containing a colon round-trips ({_subj})", s == _subj and t == _tok,
          f"got {s!r}/{t!r}")
check("no credential => no subject", _proxy_basic_subject(b"GET / HTTP/1.1\r\nHost: x\r\n\r\n")
      == (None, None))
_s = strip_proxy_auth(_head("harness:browser", _tok))
check("Proxy-Authorization is stripped", b"proxy-authorization" not in _s.lower())
check("the token itself is gone", _tok.encode() not in _s)
check("other headers survive", b"Host: x.com" in _s and b"User-Agent: z" in _s)
check("the request line survives", _s.startswith(b"GET http://x.com/ HTTP/1.1"))
check("body bytes read in with the head are untouched", _s.endswith(b"BODY"))
_plain = b"GET / HTTP/1.1\r\nHost: a\r\n\r\n"
check("a head with no credential is returned unchanged", strip_proxy_auth(_plain) == _plain)
_c = strip_proxy_auth(_head("harness:browser", _tok, method=b"CONNECT x.com:443 HTTP/1.1"))
check("CONNECT heads are stripped too (squid sees them on the platform path)",
      b"proxy-authorization" not in _c.lower())


# ── the relay ──────────────────────────────────────────────────────────────
async def _relay():
    print("the relay reaches ONE named bastion, and does not eat the others")

    class _WS:
        pass

    r = EgressRelay()
    w7, w9 = RelayWorker(_WS(), bastion_token_id=7), RelayWorker(_WS(), bastion_token_id=9)
    r._register_idle(w7)
    r._register_idle(w9)
    check("worker_count(None) counts every bastion", r.worker_count() == 2)
    check("worker_count(id) is per-bastion", r.worker_count(7) == 1 and r.worker_count(9) == 1)
    check("has_bastion knows who has connected",
          r.has_bastion(7) and r.has_bastion(9) and not r.has_bastion(42))

    got = await r._take_any(1.0)
    check("an unpinned take returns one worker", got in (w7, w9))
    r.discard(got)
    other = w9 if got is w7 else w7
    check("THE RE-QUEUE: the other bastion's worker was put back, not orphaned",
          r.worker_count(other.bastion_token_id) == 1,
          f"7={r.worker_count(7)} 9={r.worker_count(9)}")
    check("...and a PIN can still take it (this is the 503-while-idle bug)",
          (await asyncio.wait_for(r._q(other.bastion_token_id).get(), 1.0)) is other)

    check("an empty relay returns None rather than raising ValueError",
          (await EgressRelay()._take_any(0.05)) is None)

    r2 = EgressRelay()
    r2._register_idle(RelayWorker(_WS(), bastion_token_id=7))
    check("a pin to a bastion with no queue times out rather than stealing another's",
          (await r2.dispatch(b"", None, None, timeout=0.05, bastion_token_id=99)) is False)
    check("...and the other bastion's worker is untouched", r2.worker_count(7) == 1)

asyncio.run(_relay())


# ── _handle: the routing decision itself ───────────────────────────────────
# The checks above test the pieces. This tests the thing the feature PROMISES:
# a pinned request never leaves by the platform path, whatever goes wrong.


class _W:
    """A writer that records what the gate wrote back."""

    def __init__(self):
        self.buf = b""
        self.closed = False

    def write(self, b):
        self.buf += b

    async def drain(self):
        pass

    def close(self):
        self.closed = True


class _R:
    def __init__(self, data: bytes):
        self._data = data

    async def read(self, n=-1):
        d, self._data = self._data, b""
        return d


async def _handle_cases():
    print("_handle: a pinned request NEVER falls back to the platform")
    import server.network.nac_egress as NE

    class _Relay:
        def __init__(self, has=False, ok=False):
            self._has, self._ok = has, ok
            self.dispatched = []

        def worker_count(self, token_id=None):
            return 1 if self._has else 0

        def has_bastion(self, token_id):
            return self._has

        async def dispatch(self, head, r, w, timeout=0, bastion_token_id=None):
            self.dispatched.append(bastion_token_id)
            return self._ok

    def _rig(proxy, relay, direct):
        import server.network.egress_relay as ER
        ER.get_egress_relay = lambda: relay
        proxy._direct_dispatch = direct

    # A pinned app-lane request meets the private-address floor before the
    # bastion (EgressProxy._pinned_dispatch; its own cases are in
    # test_egress_lane_private_refused.py). These cases are about where a pin
    # goes, so the destination resolves to a public address here, and no
    # real DNS is asked.
    async def _public(host, port):
        return ["93.184.215.14"]
    NE._resolve = _public

    PIN = [{"id": 1, "pattern": "*", "action": "allow", "priority": 10,
            "subject": "harness:browser", "exit": "bastion:home"}]
    # A lane request CARRIES its credential — the subject the rule matches on is
    # parsed out of it, so a request without one is a different test (the 407
    # below). Getting this wrong once made the pinned cases silently exercise the
    # unpinned path, which is worth the extra line here.
    REQ = (b"CONNECT bank.example:443 HTTP/1.1\r\nHost: bank.example\r\n"
           b"Proxy-Authorization: Basic "
           + base64.b64encode(b"harness:browser:" + b"b" * 64) + b"\r\n\r\n")

    async def run(rules, relay, *, app_lane, subject_ok=True, resolve=(7, None),
                  req=REQ):
        px = NE.EgressProxy(pool=None, entity_id=1, verify_pubkey=None,
                            app_lane=app_lane)
        px._rules = list(rules)
        px._rules_at = 1e18                      # never refresh (no pool here)
        px._lane_subject_ok = lambda s, t: _true(subject_ok)
        px._resolve_pin = lambda n: _tuple(resolve)
        direct = {"called": False}

        async def _direct(head, r, w, method, target):
            direct["called"] = True
        _rig(px, relay, _direct)
        w = _W()
        await px._handle(_R(req), w)
        return w, direct["called"]

    async def _true(v):
        return v

    async def _tuple(v):
        return v

    # pinned + bastion offline -> 503, and NOT the platform
    w, direct = await run(PIN, _Relay(has=False), app_lane=True)
    check("pinned + offline bastion answers 503", b"503" in w.buf, w.buf[:40])
    check("...naming the machine", b"home" in w.buf, w.buf[:120])
    check("...and NEVER calls _direct_dispatch", direct is False)

    # pinned + connected but the dispatch fails -> still 503, still not platform
    w, direct = await run(PIN, _Relay(has=True, ok=False), app_lane=True)
    check("pinned + no answer in time answers 503", b"503" in w.buf, w.buf[:40])
    check("...and still never falls back", direct is False)

    # pinned + it works -> no body written by the gate, no platform
    r = _Relay(has=True, ok=True)
    w, direct = await run(PIN, r, app_lane=True)
    check("pinned + working bastion relays", w.buf == b"" and direct is False)
    check("...to THAT bastion", r.dispatched == [7], r.dispatched)

    # unresolvable name -> 503 carrying the resolver's reason
    w, direct = await run(PIN, _Relay(has=True, ok=True), app_lane=True,
                          resolve=(None, b"two of your machines"))
    check("an unresolvable pin answers 503 with the reason",
          b"503" in w.buf and b"two of your machines" in w.buf, w.buf[:160])
    check("...and does not fall back", direct is False)

    print("_handle: the lane's default is 'as you were', not 'any bastion'")
    # No rule at all, on the APP LANE -> platform path, never a bastion.
    r = _Relay(has=True, ok=True)
    w, direct = await run([], r, app_lane=True)
    check("an UNPINNED lane request takes the platform path",
          direct is True and r.dispatched == [], r.dispatched)
    # Same request on the loopback gate -> v059 behaviour, any bastion.
    r2 = _Relay(has=True, ok=True)
    w, direct = await run([], r2, app_lane=False)
    check("the loopback gate still prefers any connected bastion",
          direct is False and r2.dispatched == [None], r2.dispatched)

    print("_handle: the lane refuses an unauthenticated caller")
    w, direct = await run(PIN, _Relay(has=True, ok=True), app_lane=True,
                          subject_ok=False)
    check("no credential on the lane answers 407", b"407" in w.buf, w.buf[:40])
    check("...with a Basic challenge", b"Proxy-Authenticate: Basic" in w.buf, w.buf[:160])
    check("...and routes nothing", direct is False)

asyncio.run(_handle_cases())


# ── load_egress_rules: the terms are the authority ─────────────────────────
async def _load():
    print("subject/exit are read from the VERIFIED TERMS, never the row")

    class _Pool:
        def __init__(self, rows):
            self._rows = rows

        async def fetch(self, *a, **k):
            return self._rows

    # signature verification is skipped when unarmed... but unarmed SKIPS signed rows,
    # so arm it with a stub verifier by monkeypatching the module's checker.
    import server.network.nac_egress as NE
    real = NE._verify_rule
    NE._verify_rule = lambda pub, terms, sig: True
    try:
        # A pre-v127 row: three-key terms, and columns that say something else.
        pre = [{"id": 1, "pattern": "x.com", "action": "allow", "priority": 100,
                "signed_terms": {"pattern": "x.com", "action": "allow", "priority": 100},
                "signature": "sig"}]
        got = await load_egress_rules(_Pool(pre), b"k" * 32)
        check("a pre-v127 signed row loads as any-subject/default-exit",
              got and got[0]["subject"] == "*" and got[0]["exit"] == EXIT_DEFAULT)

        # THE ATTACK: columns mutated after approval; the signature still verifies
        # because it covers signed_terms only.
        tampered = [{"id": 2, "pattern": "x.com", "action": "allow", "priority": 100,
                     "subject": "*", "exit": "bastion:attacker",
                     "signed_terms": {"pattern": "x.com", "action": "allow",
                                      "priority": 100, "subject": "*",
                                      "exit": EXIT_DEFAULT},
                     "signature": "sig"}]
        got = await load_egress_rules(_Pool(tampered), b"k" * 32)
        check("an exit written straight into the COLUMN is not enforced",
              got and got[0]["exit"] == EXIT_DEFAULT, got and got[0]["exit"])
    finally:
        NE._verify_rule = real

asyncio.run(_load())

print()
if _fails:
    print(f"FAILED ({len(_fails)}): " + "; ".join(_fails))
    sys.exit(1)
print("all NAC egress subject/exit checks passed")
