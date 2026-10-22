#!/usr/bin/env python3
"""Gate: the network gate fails CLOSED, and a rule whose match cannot be evaluated locks.

The defect. server/routes/mcp.py _nac_gate admitted a machine whenever the NAC engine
raised. An approved quarantine rule whose match is a non-empty list or a string makes
NACController._match raise (it calls .get on the match), drafting did not check the
match's type, and approving a quarantine needs no step-up code. So one malformed
approved rule let every governed machine in.

Pinned, each against the real code (a real ed25519 key, the real engine, the real gate,
a stand-in pool):
  1. an engine that raises makes the gate REFUSE the machine, and says why
  2. an approved rule whose match _match cannot evaluate (a list, a string, a number
     where a list belongs, a list where min_posture's string belongs) fails its check:
     the engine locks, the gate refuses every governed machine, the person is told once
     in words that fit, and removing that rule asks for the code
  3. drafting refuses a match that is not the shape the matcher reads, before any write,
     and approving refuses one drafted before that check existed, before the code is
     checked

Run: APP_ROOT=<repo> python3 scripts/test_nac_gate_fails_closed.py
"""
import asyncio
import base64
import json
import logging
import os
import sys
import types

ROOT = os.environ.get("APP_ROOT") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path[:0] = [ROOT, os.path.join(ROOT, "brain")]
logging.disable(logging.CRITICAL)
sys.modules.setdefault("server.main", types.ModuleType("server.main"))

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey  # noqa: E402

from server.network import nac_commit, nac_controller as nc, nac_propose, nac_stepup  # noqa: E402
from server.routes.mcp import _nac_gate  # noqa: E402

FAILS: list[str] = []


def check(label, ok, detail=""):
    print(f"  {'ok  ' if ok else 'FAIL'} {label}" + (f" — {str(detail)[:500]}" if not ok and detail != "" else ""))
    if not ok:
        FAILS.append(label)


def run(coro):
    return asyncio.run(coro)


KEY = Ed25519PrivateKey.generate()
PUB = KEY.public_key().public_bytes_raw()
DEVICE = {"id": 41, "hardware_fingerprint": "fp-invented-41", "entity_id": 9310}
MCP = [{"direction": "inbound", "lane": "mcp"}]


def signed_row(rid, enforcement, match, permits=()):
    terms = {"name": f"rule-{rid}", "priority": rid, "match": match,
             "enforcement": enforcement, "permits": list(permits)}
    return {"id": rid, "name": f"rule-{rid}", "priority": rid, "match": json.dumps(match),
            "profile_id": rid, "signed_terms": json.dumps(terms),
            "signature": base64.b64encode(KEY.sign(nc._canonical(terms))).decode()}


class RulesPool:
    """Answers the engine's two reads (profiles, approved policy) and the endpoint read."""

    def __init__(self, rows):
        self.rows = rows

    async def fetch(self, q, *a):
        if "FROM nac_policy" in q:
            return list(self.rows)
        return []

    async def fetchrow(self, q, *a):
        return {"token_id": a[0], "fingerprint": "fp-invented-41", "classification": "server",
                "posture": "healthy"}


def engine(rows):
    told = []

    async def notify(notice):
        told.append(notice)
        return {"ok": True}

    return nc.NACController(pool=RulesPool(rows), verify_pubkey=PUB, notify=notify), told


# ── 1. an engine error refuses ───────────────────────────────────────────────────────────
print("1. an engine that raises makes the gate refuse")


class Broken:
    async def evaluate(self, token_id):
        raise RuntimeError("invented engine fault")


out = run(_nac_gate(DEVICE, nac=Broken()))
check("the gate refuses the machine when the engine raises",
      isinstance(out, dict) and out.get("allowed") is False, repr(out))
check("...and says the rules could not be evaluated",
      "could not be evaluated" in (out or {}).get("why", ""), repr(out))
check("a machine with no device fingerprint is still not asked about (bearer-only)",
      run(_nac_gate({"id": 42, "hardware_fingerprint": None}, nac=Broken())) is None)

# ── 2. a match the matcher cannot evaluate fails its check and locks ─────────────────────
print("2. an approved rule whose match cannot be evaluated locks the gate")
BAD = {
    "a list as the match": ["posture", "degraded"],
    "a string as the match": "posture=degraded",
    "a number where classification's list belongs": {"classification": 5},
    "a list where min_posture's string belongs": {"min_posture": ["healthy"]},
}
for label, match in BAD.items():
    nac, told = engine([signed_row(7, "quarantine", match)])
    out = run(_nac_gate(DEVICE, nac=nac))
    check(f"{label}: the gate refuses the machine",
          isinstance(out, dict) and out.get("allowed") is False, repr(out))
    check(f"{label}: the engine locked on the rule rather than raising",
          nac._tamper is True
          and nac._verdicts.get(7) == {"integrity": "failed", "reason": nc.FAIL_MATCH},
          (nac._tamper, nac._verdicts))
    check(f"{label}: the person is told, in words about an unreadable rule",
          len(told) == 1 and "cannot read" in told[0]["body"]
          and "signature was removed" not in told[0]["body"]
          and told[0]["payload"]["reasons"] == [nc.FAIL_MATCH], told)
    run(_nac_gate(DEVICE, nac=nac))
    check(f"{label}: a second request is refused and not told again", len(told) == 1, len(told))
    check(f"{label}: removing it asks for the code (the engine reads it as keeping machines out)",
          nc.rule_admits_inbound(signed_row(7, "quarantine", match), None, verify_pubkey=PUB) is False)

nac, _told = engine([signed_row(8, "allow", ["posture", "healthy"], MCP)])
check("an allow with an unreadable match locks too",
      run(_nac_gate(DEVICE, nac=nac)) is not None and nac._tamper is True)
check("...and removing it asks for the code",
      nc.rule_admits_inbound(signed_row(8, "allow", ["posture", "healthy"], MCP), None,
                             verify_pubkey=PUB) is False)
nac, told = engine([signed_row(9, "quarantine", {"posture": ["degraded", "unknown"]})])
check("a well-formed quarantine still decides by its match (a healthy machine is admitted)",
      run(_nac_gate(DEVICE, nac=nac)) is None and nac._tamper is False and not told)
nac, _told = engine([signed_row(10, "quarantine", [])])
check("an empty match is still the catch-all, not a failure",
      run(_nac_gate(DEVICE, nac=nac)) is not None and nac._tamper is False)
_n = nc.NACController(verify_pubkey=PUB)
_n._ingest([], [signed_row(7, "quarantine", "x")])
check("the engine's own reason names the match", "match cannot be evaluated" in _n.decide({})[1],
      _n.decide({}))

# ── 2b. one value written as a string keeps the answer it had ────────────────────────────
print("2b. an approved rule with one value written as a string still decides as it did")


def _old_answer(m, ep):
    """The matcher as it was before drafting checked a match's shape, in effect: a string
    condition was tested by substring (`value in string`)."""
    cls = m.get("classification")
    if cls and ep.get("classification") not in cls:
        return False
    plist = m.get("posture")
    if plist and ep.get("posture", "unknown") not in plist:
        return False
    return True


def _decides(match, ep):
    n = nc.NACController(verify_pubkey=PUB)
    n._ingest([], [signed_row(12, "quarantine", match)])
    prof, _why = n.decide(ep)
    return n, prof is not None and prof.get("name") == "rule-12"


for _match in ({"classification": "server"}, {"classification": "workstation"},
               {"posture": "degraded"}, {"posture": "unknown"},
               {"classification": "server", "posture": "healthy"}):
    _same = True
    for _c in nc.CLASSIFICATIONS:
        for _p in nc.POSTURE_RANK:
            _ep = {"classification": _c, "posture": _p}
            _n, _hit = _decides(_match, _ep)
            if _n._tamper or _hit != _old_answer(_match, _ep):
                _same = False
    check(f"{_match}: no lock, and every reported machine gets the answer the old matcher gave",
          _same, _match)
_n, _hit = _decides({"classification": "server"}, {"token_id": 5, "posture": "unknown"})
check("a machine that has not reported is not matched by a one-value rule (the old matcher "
      "raised here, which the old gate turned into an admit)", not _n._tamper and _hit is False)
nac, told = engine([signed_row(13, "quarantine", {"classification": "server"})])
out = run(_nac_gate(DEVICE, nac=nac))
check("through the gate: a quarantine on classification \"server\" refuses a server, and nobody "
      "is told of a lock", isinstance(out, dict) and out.get("allowed") is False
      and nac._tamper is False and not told and "rule-13" in out.get("why", ""), (out, told))
check("removing that intact quarantine asks for the code, as for its list form",
      nc.rule_admits_inbound(signed_row(13, "quarantine", {"classification": "server"}), None,
                             verify_pubkey=PUB) is False)
check("an allow on posture \"healthy\" admits on the mcp lane, so removing it is free",
      nc.rule_admits_inbound(signed_row(14, "allow", {"posture": "healthy"}, MCP), None,
                             verify_pubkey=PUB) is True)
for _label, _match in (("a dict where classification's list belongs", {"classification": {"a": "server"}}),
                       ("a string holding two values", {"classification": "server,workstation"}),
                       ("a string holding a value inside a longer word", {"posture": "unhealthy"})):
    nac, told = engine([signed_row(15, "quarantine", _match)])
    out = run(_nac_gate(DEVICE, nac=nac))
    check(f"{_label}: still locks as an unreadable match",
          isinstance(out, dict) and out.get("allowed") is False and nac._tamper is True
          and nac._verdicts.get(15) == {"integrity": "failed", "reason": nc.FAIL_MATCH},
          (out, nac._verdicts))
check("drafting stores one known value written as a string as its one-item list",
      nc.normalize_match({"classification": "server", "posture": "degraded"})
      == {"classification": ["server"], "posture": ["degraded"]})
for _bad in ({"classification": "server,workstation"}, {"posture": "great"}):
    try:
        nc.normalize_match(_bad)
        _res = "accepted"
    except ValueError as e:
        _res = str(e)
    check(f"drafting still refuses {_bad}", _res != "accepted", _res)

# ── 3. drafting and approving refuse it ──────────────────────────────────────────────────
print("3. drafting and approving refuse a match the matcher does not read")


class NoDb:
    def acquire(self):
        raise AssertionError("the draft reached the database")


for label, match in list(BAD.items()) + [
        ("an unknown key", {"ports": [22]}),
        ("a posture value the engine never reports", {"posture": ["great"]}),
        ("an empty classification list", {"classification": []}),
        ("min_posture outside the ranks", {"min_posture": "excellent"})]:
    try:
        run(nac_propose.propose_policy(NoDb(), name="r-invented", enforcement="quarantine",
                                       match=match))
        res = "drafted"
    except ValueError as e:
        res = str(e)
    except AssertionError as e:
        res = f"reached the db: {e}"
    check(f"drafting refuses {label}", res != "drafted" and not res.startswith("reached"), res)
_good = {"posture": ["degraded"], "classification": ["server"], "min_posture": "healthy",
         "fingerprint": "fp-invented-9"}
check("a well-formed match is accepted unchanged", nc.normalize_match(dict(_good)) == _good)
check("no match is the catch-all", nc.normalize_match(None) == {})


class _Conn:
    def __init__(self, row):
        self.row, self.executed = row, []

    async def fetchrow(self, q, *a):
        return self.row

    async def execute(self, q, *a):
        self.executed.append(q)
        return "UPDATE 1"


class _Acq:
    def __init__(self, c):
        self.c = c

    async def __aenter__(self):
        return self.c

    async def __aexit__(self, *a):
        return False


class _Pool:
    def __init__(self, row):
        self.conn = _Conn(row)

    def acquire(self):
        return _Acq(self.conn)


stepups = []


async def _stepup(pool, code):
    stepups.append(code)
    return True


async def _sign(pool, eid, terms):
    return "entity_sign", "sig-invented"


nac_stepup.verify_stepup, nac_stepup.sign_policy_terms = _stepup, _sign
for enforcement, permits in (("quarantine", []), ("allow", MCP)):
    pool = _Pool({"id": 3, "name": "old-draft", "priority": 5, "match": json.dumps(["posture"]),
                  "enforcement": enforcement, "permits": json.dumps(permits), "approved_at": None})
    stepups.clear()
    try:
        run(nac_commit.approve_policy(pool, 9310, 3, stepup_code="000000"))
        res = None
    except nac_commit.CommitError as e:
        res = e
    check(f"approving a {enforcement} drafted with a list as its match is refused (400)",
          res is not None and res.status == 400, repr(res))
    check("...before the code is checked, with nothing signed or written",
          not stepups and not pool.conn.executed, (stepups, pool.conn.executed))
pool = _Pool({"id": 4, "name": "good", "priority": 5, "match": json.dumps({"posture": ["degraded"]}),
              "enforcement": "quarantine", "permits": "[]", "approved_at": None})
check("a well-formed quarantine draft still approves",
      run(nac_commit.approve_policy(pool, 9310, 4)).get("approved") is True)

# ── 4. an approved rule is never read from its profile, and drafting says so ─────────────
print("4. an approved rule with no signed terms is not read from its profile")
_n = nc.NACController(verify_pubkey=PUB)
_n._ingest([{"id": 5, "name": "shared", "enforcement": "allow", "is_default": False,
             "permits": json.dumps(MCP)}],
           [{"id": 5, "name": "shared", "priority": 1, "match": "{}", "profile_id": 5,
             "signed_terms": None, "signature": None}])
check("an approved rule whose profile a re-draft rewrote to an allow is not enforced as one",
      _n._tamper is True and _n._verdicts.get(5, {}).get("reason") == nc.FAIL_UNSIGNED
      and _n.permits_inbound(_n.decide({})[0]) is False, (_n._tamper, _n._verdicts))
_doc = " ".join((nac_propose.propose_policy.__doc__ or "").split())
check("propose_policy's docstring no longer says the engine reads the profile of an approved rule",
      "the engine reads that profile" not in _doc and "signed terms only" in _doc, _doc[:400])

# ── 5. no engine loaded refuses a governed machine ───────────────────────────────────────
print("5. with no engine loaded, a governed machine is refused")
_stub_main = types.ModuleType("server.main")
_stub_main.get_nac_controller = lambda: None
_saved_main = sys.modules.get("server.main")
sys.modules["server.main"] = _stub_main


def _deep_nac():
    """The "nac" line of /api/system/health/deep. This process has no pool or runtime,
    so the probe answers 503 and its body rides on the exception."""
    from fastapi import HTTPException
    from server.routes import system as _system
    try:
        body = run(_system.health_deep())
    except HTTPException as e:
        body = e.detail
    return (body.get("checks") or {}).get("nac")


try:
    out = run(_nac_gate(DEVICE))
    bearer = run(_nac_gate({"id": 43, "hardware_fingerprint": None}))
    internal = run(_nac_gate({"_internal": True}))
    deep_none = _deep_nac()
    _stub_main.get_nac_controller = lambda: nc.NACController(pool=None, entity_id=None)
    deep_some = _deep_nac()
finally:
    if _saved_main is not None:
        sys.modules["server.main"] = _saved_main
    else:
        sys.modules.pop("server.main", None)
check("no engine loaded: a governed machine is refused, not admitted",
      out is not None and out.get("allowed") is False, out)
check("no engine loaded: a bearer-only token and the in-pod loopback stay exempt",
      bearer is None and internal is None, (bearer, internal))
check("no engine loaded: /api/system/health/deep says so, and that fingerprinted machines are refused",
      isinstance(deep_none, str) and deep_none.startswith("warn: no network engine loaded")
      and "refused" in deep_none, deep_none)
check("an engine loaded: /api/system/health/deep reports nac ok", deep_some == "ok", deep_some)

# ── 6. with no engine built at start, the inbox says so ──────────────────────────────────
print("6. with no engine built at start, the person is told in the inbox")
import ast  # noqa: E402

from server.routes import mcp as _mcp  # noqa: E402

_tell = getattr(_mcp, "tell_no_engine", None)
check("the gate's module has the rules-not-loaded notice", callable(_tell))
if callable(_tell):
    _inbox = []

    async def _fake_append(notice):
        _inbox.append(notice)
        return {"ok": True}

    from server.bridge import memory_handlers as _mh
    _saved_append = _mh.handle_inbox_append
    _mh.handle_inbox_append = _fake_append
    try:
        _stored = run(_tell())
        run(_tell())
    finally:
        _mh.handle_inbox_append = _saved_append
    _n0 = _inbox[0] if _inbox else {}
    _body = _n0.get("body", "")
    check("it goes to the Host's inbox through handle_inbox_append, as one system_notification",
          _stored is True and len(_inbox) == 2 and _n0.get("message_kind") == "system_notification",
          _inbox)
    check("it says paired machines are refused because the network rules did not load",
          "refusing every machine you paired" in _body and "rules did not load" in _body, _body)
    check("...and that the Host tries again at each start", "each time it starts" in _body, _body)
    check("each start's notice has its own id, so the inbox keeps a later one",
          len(_inbox) == 2 and _inbox[0]["message_id"] != _inbox[1]["message_id"], _inbox)
    check("its payload names the network app and the kind",
          _n0.get("payload") == {"app": "network", "kind": "nac_not_loaded"}, _n0.get("payload"))

    async def _raising(notice):
        raise RuntimeError("invented inbox fault")

    async def _refused(notice):
        return {"ok": False, "error": "pool not ready"}

    check("a notice that cannot be written does not raise into the start",
          run(_tell(notify=_raising)) is False and run(_tell(notify=_refused)) is False)

_main_src = open(os.path.join(ROOT, "server", "main.py"), encoding="utf-8").read()


def _no_engine_branch_tells(src):
    """Does main.py's NAC-init error handler, in its `_nac_controller_ref is None` branch,
    await tell_no_engine()?"""
    for node in ast.walk(ast.parse(src)):
        if not isinstance(node, ast.ExceptHandler):
            continue
        for stmt in node.body:
            if (isinstance(stmt, ast.If) and isinstance(stmt.test, ast.Compare)
                    and isinstance(stmt.test.left, ast.Name)
                    and stmt.test.left.id == "_nac_controller_ref"
                    and isinstance(stmt.test.ops[0], ast.Is)):
                for sub in ast.walk(ast.Module(body=stmt.body, type_ignores=[])):
                    if (isinstance(sub, ast.Await) and isinstance(sub.value, ast.Call)
                            and getattr(sub.value.func, "id", None) == "tell_no_engine"):
                        return True
    return False


check("main.py: the NAC init error handler tells the inbox when no engine was built",
      _no_engine_branch_tells(_main_src))

print(f"\n{'PASS' if not FAILS else 'FAIL'}: the network gate fails closed"
      + (f" — {len(FAILS)} failing: {FAILS}" if FAILS else ""))
sys.exit(1 if FAILS else 0)
